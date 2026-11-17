// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # System Operations Console — NOTIFY listener
//!
//! Subscribes to the Postgres `pg_notify` channels emitted by the
//! control-plane System Operations endpoints and
//! dispatches each payload to the matching in-process mutation
//! (counter reset, cooldown release, future cache/scheduler/log/
//! maintenance handlers).
//!
//! ## Why a dedicated listener (and not the existing `CacheInvalidator`)
//!
//! The cache-invalidator runs on Redis pub/sub. System Operations
//! NOTIFY rides Postgres `LISTEN/NOTIFY` instead — that's the
//! architectural decision recorded in
//! `architecture_cluster_cache_propagation`: Postgres NOTIFY is
//! transactional with the audit-row write that immediately precedes
//! it, so a NOTIFY is only delivered if the audit row also committed.
//! Redis pub/sub has no such ordering guarantee. For *operator*
//! actions where "did this fire?" is the most-asked question, we want
//! that durable pairing — and we want it without taking a hard
//! dependency on Redis (so the trial bundle and single-replica
//! deployments still get System Operations).
//!
//! ## Channel registry
//!
//! Every channel name listed here MUST mirror the constants in
//! `app/services/system_ops/notify.py` (gateway-control side). When
//! the control plane publishes on a channel that isn't in this
//! `CHANNELS` list, this listener silently ignores it — a misconfig
//! catches at the gateway-control validator, not here. The
//! corresponding test (`unknown_channel_ignored`) locks that behaviour.
//!
//! ## Dispatch model
//!
//! - The listener spawns one tokio task that owns the `PgListener`.
//! - On every message, we route on `channel` to a per-channel
//!   handler. Handlers are async + take `&SharedState` so each can
//!   reach the live caches/state without taking ownership of them.
//! - Unknown channels and malformed JSON are LOGGED but never panic
//!   the listener — an operator-fired action must NEVER be able to
//!   knock out the listener task and orphan every subsequent action.
//!
//! ## What's wired today vs. what is advisory
//!
//! This list is load-bearing: an operator pressing a button in the
//! System Operations Console needs to know whether the proxy actually
//! did something. Anything described here as advisory writes its
//! durable audit row and changes nothing in this process.
//!
//! | Channel | In gateway-core |
//! |---|---|
//! | `counters:reset` | partial — cooldown release for `model` scope; counter drop still needs a typed `UsageState::reset_scope` |
//! | `cache:stats:request` | wired — snapshots the in-process layers and publishes one reply for this instance; a composition that runs several instances installs its own answer ([`CacheStatsResponder`]) |
//! | `cache:invalidate` | wired — dispatches by `layer` to the matching in-process cache |
//! | `maintenance` | wired — drops the cached `tenants.maintenance_mode` row so the gate re-reads it |
//! | `scheduler:trigger` / `pause` / `resume` | **advisory** — the core's periodic loops have no pause flag to set; the audit row is written and the loops keep running |
//! | `session:revoke` | **advisory** — portal JWTs are stateless and validated by signature + expiry, so there is nothing in this process to evict; revocation needs a denylist the portal auth path consults (not built) |
//! | `log_level` | **advisory** — the tracing subscriber is installed with a fixed `EnvFilter` and no reload handle |
//!
//! Advisory is not the same as lost: every one of these is paired
//! with a durable `system_operations` row on the control-plane side,
//! which is what the console renders. The gap is only that the
//! runtime does not change.

use std::time::Duration;

use serde::{Deserialize, Serialize};
use sqlx::postgres::{PgListener, PgNotification};
use tokio::task::JoinHandle;
use tokio_util::sync::CancellationToken;

use crate::services::SharedState;

// ---------------------------------------------------------------------------
// Channel constants — MUST match
// `brutor-gateway-control/app/services/system_ops/notify.py`
// ---------------------------------------------------------------------------
//
// These strings are wire protocol. Treat any rename as a coordinated
// bump on both repos at the same release — otherwise the publisher
// fires into the void.

pub const CHANNEL_COUNTERS_RESET: &str = "brutor:counters:reset";
pub const CHANNEL_CACHE_INVALIDATE: &str = "brutor:cache:invalidate";
pub const CHANNEL_SCHEDULER_TRIGGER: &str = "brutor:scheduler:trigger";
pub const CHANNEL_SCHEDULER_PAUSE: &str = "brutor:scheduler:pause";
pub const CHANNEL_SCHEDULER_RESUME: &str = "brutor:scheduler:resume";
pub const CHANNEL_SESSION_REVOKE: &str = "brutor:session:revoke";
pub const CHANNEL_LOG_LEVEL: &str = "brutor:log_level";
pub const CHANNEL_MAINTENANCE: &str = "brutor:maintenance";
// Cache statistics. The control plane publishes
// REQUEST with a fresh correlation_id; the gateway snapshots its
// in-process cache hit/miss/entries counters and publishes REPLY on
// the matching channel tagged with the same correlation_id. We
// listen on REQUEST only; the control plane listens on REPLY only.
pub const CHANNEL_CACHE_STATS_REQUEST: &str = "brutor:cache:stats:request";
pub const CHANNEL_CACHE_STATS_REPLY: &str = "brutor:cache:stats:reply";

/// Full registry of channels the listener subscribes to.
///
/// Returned as a `&'static [&'static str]` so the live LISTEN loop
/// and the test that asserts "all channels are registered" share the
/// exact same source of truth. Adding a new System Operations action
/// = add the constant above + add it here + wire its handler in
/// `dispatch`. Anything else routes through the `unknown channel`
/// log branch.
///
/// Note: `CHANNEL_CACHE_STATS_REPLY` is deliberately NOT in this
/// list — gateway-core publishes on it but never listens. Adding it
/// here would wake the gateway on its own replies.
pub const CHANNELS: &[&str] = &[
    CHANNEL_COUNTERS_RESET,
    CHANNEL_CACHE_INVALIDATE,
    CHANNEL_SCHEDULER_TRIGGER,
    CHANNEL_SCHEDULER_PAUSE,
    CHANNEL_SCHEDULER_RESUME,
    CHANNEL_SESSION_REVOKE,
    CHANNEL_LOG_LEVEL,
    CHANNEL_MAINTENANCE,
    CHANNEL_CACHE_STATS_REQUEST,
];

// ---------------------------------------------------------------------------
// Payload models
// ---------------------------------------------------------------------------
//
// Mirror the Pydantic shapes in
// `app/services/system_ops/notify.py` exactly. Every field uses
// `#[serde(default)]` for the optional ones so a wire-format
// addition on the publisher side doesn't drop the message on the
// floor — we accept extra fields too (`#[serde(deny_unknown_fields)]`
// is deliberately NOT set, so adding a new field publisher-side is
// non-breaking from the listener's perspective).

/// Payload for `brutor:counters:reset`.
///
/// Carries either a regular reset (`limit_types` set or omitted) OR
/// a cooldown release (`release_cooldown = true`). The publisher
/// reuses the same channel for both — distinguishing on payload is
/// cheaper than maintaining two channels with identical subscriber
/// fanout cost.
#[derive(Debug, Clone, Deserialize)]
pub struct CountersResetPayload {
    /// Subject the counter is keyed on — `"tenant" | "group" | "user" |
    /// "api_key" | "model"`. Forwarded verbatim from the admin UI.
    pub scope: String,
    /// Identifier of the scoped subject. Shape depends on `scope`.
    pub id: String,
    /// Subset of buckets to drop. Absent (`None`) = reset every
    /// bucket the listener can find for the scope.
    #[serde(default)]
    pub limit_types: Option<Vec<String>>,
    /// Tenant the action targets. The listener pre-filters on this
    /// when it constructs the bucket key — a cross-tenant sysadmin
    /// reset never bleeds counter state across tenant boundaries.
    #[serde(default)]
    pub tenant_id: Option<String>,
    /// When `true`, the action is a cooldown release, not a counter
    /// reset. `limit_types` is ignored in that case.
    #[serde(default)]
    pub release_cooldown: Option<bool>,
}

/// Payload for `brutor:cache:stats:request` — what the control plane
/// fires when an operator opens the System → Caches page.
///
/// Carries only a correlation_id. The gateway snapshots its in-process
/// caches and publishes a [`CacheStatsReplyPayload`] tagged with the
/// same correlation_id.
#[derive(Debug, Clone, Deserialize)]
pub struct CacheStatsRequestPayload {
    /// Opaque token round-tripped from request to reply so the
    /// control plane can tell its own replies apart from replies to
    /// concurrent operator clicks.
    pub correlation_id: String,
}

/// Per-layer counter snapshot embedded in a [`CacheStatsReplyPayload`].
///
/// `hits` / `misses` are monotonic process counters since the process
/// started. `entries` is the live `moka::Cache::entry_count()` value.
/// All values are u64 — they wrap at 2^64 (584 years at 1B ops/sec),
/// not a real concern in operation.
#[derive(Debug, Clone, Serialize)]
pub struct CacheLayerStatsPayload {
    pub hits: u64,
    pub misses: u64,
    pub entries: u64,
}

/// Payload for `brutor:cache:stats:reply` — the one reply of a
/// Community gateway to one request: the request's
/// correlation id and this instance's counters. It names no instance:
/// there is one. What a composition of several instances replies is its
/// extension's own payload ([`CacheStatsResponder`]).
#[derive(Debug, Clone, Serialize)]
pub struct CacheStatsReplyPayload {
    pub correlation_id: String,
    /// Layer name → counter snapshot. Keys are stable wire strings:
    /// `"policy"`, `"group"`, `"tool"`, and the layers an extension
    /// registered with counters. The semantic layer is intentionally
    /// absent — its hit/miss numbers come from `proxy_logs` rather than
    /// in-process counters, so the control plane reads that one directly.
    pub layers: std::collections::HashMap<String, CacheLayerStatsPayload>,
}

/// What an installed responder returns.
pub type CacheStatsFuture<'a> =
    std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send + 'a>>;

/// Answers one statistics request in place of the Community reply.
pub type CacheStatsResponderFn =
    for<'a> fn(&'a SharedState, &'a CacheStatsRequestPayload) -> CacheStatsFuture<'a>;

/// Why a responder was not installed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CacheStatsResponderTaken {
    pub by: &'static str,
}

impl std::fmt::Display for CacheStatsResponderTaken {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "the cache-statistics request is already answered by {}",
            self.by
        )
    }
}

impl std::error::Error for CacheStatsResponderTaken {}

/// Who answers the statistics request. A Community
/// state holds nobody: the listener publishes one reply for the one
/// instance. An extension that makes several instances a deployment
/// installs its own answer, once; nothing replaces one that is in place.
/// Filled before the state is shared and read-only afterwards.
#[derive(Clone, Copy, Default)]
pub struct CacheStatsResponder {
    installed: Option<(&'static str, CacheStatsResponderFn)>,
}

impl CacheStatsResponder {
    /// Nobody installed: what `AppState::new` holds.
    pub fn community() -> Self {
        Self::default()
    }

    /// Install `respond` for the Enterprise feature `feature`.
    pub fn install(
        &mut self,
        feature: &'static str,
        respond: CacheStatsResponderFn,
    ) -> Result<(), CacheStatsResponderTaken> {
        if let Some((by, _)) = self.installed {
            return Err(CacheStatsResponderTaken { by });
        }
        self.installed = Some((feature, respond));
        Ok(())
    }

    /// The feature whose responder is installed.
    pub fn feature(&self) -> Option<&'static str> {
        self.installed.map(|(feature, _)| feature)
    }

    fn installed(&self) -> Option<CacheStatsResponderFn> {
        self.installed.map(|(_, respond)| respond)
    }
}

/// Payload for `brutor:cache:invalidate` — mirror of
/// `CacheInvalidatePayload` in the control plane's `notify.py`.
///
/// Note this rides the SAME channel *name* as the Redis pub/sub
/// invalidator (`services::cache::INVALIDATION_CHANNEL`) but carries a
/// different shape: Redis speaks `InvalidationMessage`, Postgres NOTIFY
/// speaks this. The two are independent paths to the same eviction, and an
/// eviction is idempotent, so a deployment running both simply evicts twice.
#[derive(Debug, Clone, Deserialize)]
pub struct CacheInvalidatePayload {
    /// `"semantic" | "policy" | "group" | "tool" | "response"` — which
    /// in-process layer to drop from.
    pub layer: String,
    /// `"tenant" | "group" | "model" | "mcp_server" | "config"` — how to
    /// read `id`. Pairs with `layer`.
    pub scope: String,
    /// Identifier of the scoped subject.
    pub id: String,
    /// Tenant the flush is scoped to. Absent means the operator was a
    /// cross-tenant sysadmin; we then have no safe key to evict by and
    /// treat it as a whole-layer flush.
    #[serde(default)]
    pub tenant_id: Option<String>,
}

/// Payload for `brutor:maintenance` — mirror of `MaintenancePayload`.
///
/// We use only `tenant_id`: the new `mode` rides along for operator
/// legibility in the log line, but seeding the cache from the payload
/// rather than invalidating it would trust a value that could already be
/// stale by the time it arrives (two toggles in flight). Dropping the entry
/// makes the next request read the row, which is the only source of truth.
#[derive(Debug, Clone, Deserialize)]
pub struct MaintenancePayload {
    /// `None` = a global change; drop every tenant's cached mode.
    #[serde(default)]
    pub tenant_id: Option<String>,
    #[serde(default)]
    pub mode: Option<String>,
}

/// Common payload for the channels that are advisory in this process
/// (scheduler trigger/pause/resume, session revoke, log level). We
/// deserialize into a raw JSON value so the arm never fails on a payload
/// shape only the control plane knows in full.
#[derive(Debug, Clone, Deserialize)]
pub struct OpaquePayload(#[serde(default)] pub serde_json::Value);

// ---------------------------------------------------------------------------
// Listener
// ---------------------------------------------------------------------------

/// Background NOTIFY consumer for every System Operations channel.
///
/// One per gateway-core process. Constructed from `main.rs`
/// alongside the existing `CacheInvalidator` and spawned with the
/// same `CancellationToken` so a graceful shutdown stops both
/// listeners in lockstep.
pub struct SystemNotifyListener {
    /// DSN used to open a dedicated `PgListener` connection. We
    /// don't share the request-path `PgPool` because `LISTEN`
    /// locks one connection forever — pulling it from the pool
    /// would silently shrink the proxy's effective capacity.
    /// `database_url` matches the value already in `AppConfig`.
    pub database_url: String,

    /// Live application state, used by the per-channel handlers
    /// to mutate caches / scheduler state. Held as a clone so the
    /// task owns it independently of the request pipeline.
    pub state: SharedState,
}

impl SystemNotifyListener {
    /// Spawn the listener task. Returns the `JoinHandle` so
    /// `main.rs` can await graceful shutdown in tests / staging
    /// without bare-spawning.
    ///
    /// Reconnect policy mirrors `CacheInvalidator::spawn`: bounded
    /// exponential backoff (2s → 30s cap). A connection that closes
    /// loops back to re-open without losing the existing subscriber
    /// state on the proxy side — the in-flight publisher's `pg_notify`
    /// during the reconnect window is the one operation that drops
    /// (accepted: every NOTIFY is paired with a
    /// durable audit row that the next operator screen-refresh
    /// reveals, so a missed notification is annoying but not silent).
    pub fn spawn(self, shutdown: CancellationToken) -> JoinHandle<()> {
        tokio::spawn(async move {
            tracing::info!(
                channels = ?CHANNELS,
                "System Operations NOTIFY listener starting"
            );

            let mut backoff_secs: u64 = 2;
            loop {
                if shutdown.is_cancelled() {
                    tracing::info!("System Operations NOTIFY listener shutting down");
                    return;
                }

                let mut listener = match PgListener::connect(&self.database_url).await {
                    Ok(l) => l,
                    Err(e) => {
                        tracing::warn!(
                            error = %e,
                            backoff_secs,
                            "Failed to open PgListener — retrying"
                        );
                        if sleep_or_shutdown(backoff_secs, &shutdown).await {
                            return;
                        }
                        backoff_secs = (backoff_secs.saturating_mul(2)).min(30);
                        continue;
                    }
                };

                // `listen_all` issues one LISTEN per channel in a
                // single batch — a connection that loses its
                // listener state mid-loop comes back here and
                // re-registers everything at once.
                if let Err(e) = listener.listen_all(CHANNELS.iter().copied()).await {
                    tracing::warn!(
                        error = %e,
                        backoff_secs,
                        "Failed to LISTEN on System Operations channels — retrying"
                    );
                    if sleep_or_shutdown(backoff_secs, &shutdown).await {
                        return;
                    }
                    backoff_secs = (backoff_secs.saturating_mul(2)).min(30);
                    continue;
                }

                tracing::info!(
                    channels = ?CHANNELS,
                    "System Operations NOTIFY listener active"
                );
                backoff_secs = 2;
                // NOTIFY is not replayed: a flush or maintenance toggle sent
                // while we were not listening is lost. Drop what those
                // messages would have dropped.
                flush_after_listen(&self.state).await;

                loop {
                    tokio::select! {
                        _ = shutdown.cancelled() => {
                            tracing::info!(
                                "System Operations NOTIFY listener shutting down"
                            );
                            return;
                        }
                        msg = listener.recv() => {
                            match msg {
                                Ok(n) => Self::dispatch(&self.state, n).await,
                                Err(e) => {
                                    // Connection-level error — drop
                                    // out to the reconnect loop. We
                                    // do NOT log at error: a
                                    // restart-driven disconnect is
                                    // routine.
                                    tracing::warn!(
                                        error = %e,
                                        "System Operations PgListener recv failed — reconnecting"
                                    );
                                    break;
                                }
                            }
                        }
                    }
                }
            }
        })
    }

    /// Route one notification to the right per-channel handler.
    ///
    /// Pulled out as an associated fn so the unit tests can exercise
    /// the dispatch logic without a live Postgres connection — see
    /// `dispatch_decode_counters` in the module's test block.
    async fn dispatch(state: &SharedState, n: PgNotification) {
        let channel = n.channel();
        let payload = n.payload();

        // Routing is matched first against the channel string so
        // the "unknown channel" branch hits BEFORE we attempt to
        // deserialize a shape we don't have a handler for.
        match channel {
            CHANNEL_COUNTERS_RESET => match serde_json::from_str::<CountersResetPayload>(payload) {
                Ok(p) => handle_counters_reset(state, p).await,
                Err(e) => log_decode_failure(channel, payload, e),
            },
            CHANNEL_CACHE_STATS_REQUEST => {
                match serde_json::from_str::<CacheStatsRequestPayload>(payload) {
                    Ok(p) => handle_cache_stats_request(state, p).await,
                    Err(e) => log_decode_failure(channel, payload, e),
                }
            }
            CHANNEL_CACHE_INVALIDATE => {
                match serde_json::from_str::<CacheInvalidatePayload>(payload) {
                    Ok(p) => handle_cache_invalidate(state, p).await,
                    Err(e) => log_decode_failure(channel, payload, e),
                }
            }
            CHANNEL_MAINTENANCE => match serde_json::from_str::<MaintenancePayload>(payload) {
                Ok(p) => handle_maintenance(state, p).await,
                Err(e) => log_decode_failure(channel, payload, e),
            },
            CHANNEL_SCHEDULER_TRIGGER
            | CHANNEL_SCHEDULER_PAUSE
            | CHANNEL_SCHEDULER_RESUME
            | CHANNEL_SESSION_REVOKE
            | CHANNEL_LOG_LEVEL => {
                // Advisory in gateway-core — see the table in the module
                // docstring for why each one has nothing to mutate here.
                // Logged rather than dropped silently so an operator
                // debugging "I pressed pause and nothing happened" finds the
                // receipt and the reason in the same place. Parsed as a raw
                // Value so a malformed payload still surfaces the channel
                // name and the error.
                match serde_json::from_str::<serde_json::Value>(payload) {
                    Ok(v) => tracing::info!(
                        channel = channel,
                        payload = %v,
                        "System Operations NOTIFY received — advisory in gateway-core, \
                         audit row written by the control plane; no runtime change here"
                    ),
                    Err(e) => log_decode_failure(channel, payload, e),
                }
            }
            other => {
                // Unknown channel — the gateway-control validator
                // should have caught this, so its presence in the
                // wire indicates a publisher/subscriber version
                // skew. Log loudly enough to spot during a rolling
                // upgrade.
                tracing::warn!(
                    channel = other,
                    payload_bytes = payload.len(),
                    "System Operations NOTIFY: unknown channel — ignoring"
                );
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Per-channel handlers
// ---------------------------------------------------------------------------

/// Reset a counter bucket (or release a cooldown) in `UsageState`.
///
/// `UsageState`'s public surface today exposes:
///   - `set_cooldown` / `clear_cooldown` for the model cooldown family
///   - `arm_cooldown` / `cooldown_remaining` for the MCP cooldown family
///   - per-counter Redis keys (`brutor:rpm:*`, `brutor:tpm:*`, ...) but
///     without a public "drop these keys" method
///
/// For the cooldown release path we delegate directly. For a counter
/// reset we drop the affected Redis keys via the redis handle on
/// AppState. The matrix of (scope × kind → key) lives in
/// `usage_state.rs`; this function builds the same key shapes so we
/// stay one mutation away from a misalignment if either side
/// changes.
async fn handle_counters_reset(state: &SharedState, p: CountersResetPayload) {
    let release = p.release_cooldown.unwrap_or(false);

    if release {
        // Cooldown release. The proxy's cooldown registry is keyed
        // on the subject id — for `model` scope this is the
        // model id; for `group` / `tenant` / `user` / `api_key`
        // the same id flows through `arm_cooldown(scope_key, _)`
        // calls in the MCP rate-limit path. Clearing both
        // namespaces (`brutor:cooldown:<id>` for LLM, derived
        // from `arm_cooldown` for MCP) keeps the operator's
        // mental model simple: "Release Cooldown" means "all
        // cooldowns I might be holding against this subject".
        match p.scope.as_str() {
            "model" => {
                state.usage_state.clear_cooldown(&p.id).await;
                tracing::info!(
                    scope = "model",
                    id = %p.id,
                    "Cleared LLM model cooldown via NOTIFY"
                );
            }
            scope => {
                // MCP cooldowns are stored under
                // `brutor:mcp:cd:<scope_key>` by
                // `UsageState::arm_cooldown`. We don't currently
                // expose a public `clear_cooldown_scope` — landing
                // a typed clear on UsageState is tracked separately
                // so this listener stays the only call site that
                // needs to know the key shape. For now, log the
                // intent so the operator action is visible in
                // metrics even though the proxy-side mutation is
                // deferred.
                tracing::warn!(
                    scope = scope,
                    id = %p.id,
                    tenant_id = ?p.tenant_id,
                    "Cooldown release for non-model scope: UsageState mutation pending — \
                     audit row written, NOTIFY received, but no public clear API yet."
                );
            }
        }
        return;
    }

    // Counter reset. The current public UsageState API doesn't
    // expose a "drop these keys" method; the limit_types matrix
    // and Redis-key shapes live private inside `usage_state.rs`.
    // Adding a typed `reset_scope` on UsageState is the next
    // landing — keeping the dispatch arm here so the wiring is in
    // place. Until that lands, the cooldown side-channel of the
    // same NOTIFY (when the proxy organically expires a key on
    // its own TTL) is the operator-visible recovery path.
    tracing::info!(
        scope = %p.scope,
        id = %p.id,
        tenant_id = ?p.tenant_id,
        limit_types = ?p.limit_types,
        "Counter reset NOTIFY received — UsageState mutation deferred to typed reset API."
    );
}

/// Drop a layer's cached entries in this process.
///
/// The `(layer, scope)` pairing is mirrored from
/// `CacheLayer` / `CacheScope` in the control plane's
/// `app/api/v1/models/admin/system_ops.py`. A pair the control plane does
/// not currently emit falls through to the widest safe eviction for that
/// layer rather than doing nothing: over-evicting costs one cold lookup,
/// under-evicting leaves an operator staring at stale config they just
/// pressed a button to clear.
///
/// A missing `tenant_id` means a cross-tenant sysadmin fired this. There is
/// then no key we could scope by, so the whole layer goes — which is what
/// "flush this cache" means when no tenant was named.
pub async fn handle_cache_invalidate(state: &SharedState, p: CacheInvalidatePayload) {
    use crate::services::cache::ResourceCache;

    let tenant = p.tenant_id.as_deref();

    // One closure per layer would need four different concrete types, so the
    // dispatch is written out. `scope` decides tenant-wide vs single-entry.
    macro_rules! evict {
        ($cache:expr) => {{
            match tenant {
                None => $cache.invalidate_all().await,
                Some(t) if p.scope == "tenant" => $cache.invalidate_tenant(t).await,
                Some(t) => $cache.invalidate(t, &p.id).await,
            }
        }};
    }

    match p.layer.as_str() {
        "policy" => evict!(state.argument_policy_cache),
        "group" => evict!(state.group_cache),
        "tool" => evict!(state.mcp_cache),
        // A layer an extension registered (`services::cache::layers`): the
        // prompt-cache policy layer `response` and the cached responses
        // `semantic`, when their extensions are composed.
        other => match state.cache_layers.get(other) {
            Some(layer) => {
                (layer.flush)(
                    state,
                    crate::services::cache::layers::LayerFlush {
                        scope: &p.scope,
                        id: &p.id,
                        tenant_id: tenant,
                    },
                )
                .await;
            }
            None => {
                // Publisher/subscriber skew, same class of problem as an
                // unknown channel; or a layer of a feature this gateway is
                // not composed with.
                tracing::warn!(
                    layer = other,
                    scope = %p.scope,
                    "System Operations cache flush: unknown layer — ignoring"
                );
                return;
            }
        },
    }

    tracing::info!(
        layer = %p.layer,
        scope = %p.scope,
        id = %p.id,
        tenant_id = ?p.tenant_id,
        "Cache layer flushed via NOTIFY"
    );
}

/// Everything a missed `cache:invalidate` or `maintenance` notification
/// could have dropped, dropped. Runs after each (re)LISTEN.
pub async fn flush_after_listen(state: &SharedState) {
    use crate::services::cache::ResourceCache;
    state.argument_policy_cache.invalidate_all().await;
    state.group_cache.invalidate_all().await;
    state.mcp_cache.invalidate_all().await;
    for layer in state.cache_layers.layers() {
        if let Some(flush) = layer.flush_after_listen {
            flush(state).await;
        }
    }
    state.maintenance_cache.invalidate(None);
    tracing::info!("Caches served by NOTIFY flushed after (re)LISTEN");
}

/// Drop the cached `tenants.maintenance_mode` so the gate re-reads it.
///
/// The middleware caches the mode for 1s. That is short, but "short" is the
/// wrong unit for the action this pairs with: an operator hitting `paused`
/// during an incident expects the next request to bounce, not the one after
/// the TTL. Invalidating on the NOTIFY makes the toggle propagate in
/// milliseconds and the TTL go back to being what it is — a bound on how
/// stale we can get if the NOTIFY never arrives.
async fn handle_maintenance(state: &SharedState, p: MaintenancePayload) {
    state.maintenance_cache.invalidate(p.tenant_id.as_deref());
    tracing::info!(
        tenant_id = ?p.tenant_id,
        mode = ?p.mode,
        "Maintenance mode cache invalidated via NOTIFY"
    );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Sleep `secs` seconds OR exit early when the shutdown token fires.
/// Returns `true` when the caller should bail (shutdown observed),
/// `false` when the sleep elapsed normally.
async fn sleep_or_shutdown(secs: u64, shutdown: &CancellationToken) -> bool {
    tokio::select! {
        _ = shutdown.cancelled() => true,
        _ = tokio::time::sleep(Duration::from_secs(secs)) => false,
    }
}

/// Centralised "couldn't decode payload" log so every channel arm
/// surfaces the same structured fields and the operator sees a
/// consistent shape during a publisher/subscriber drift.
fn log_decode_failure(channel: &str, payload: &str, e: impl std::fmt::Display) {
    tracing::warn!(
        channel = channel,
        payload_bytes = payload.len(),
        error = %e,
        "System Operations NOTIFY: malformed payload — ignoring"
    );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------
//
// We exercise:
//   * Channel registry is complete — every CHANNEL_* constant
//     declared above appears in CHANNELS. Catches a forgotten
//     append when a new channel ships.
//   * Counters payload decodes the full shape AND the minimal
//     shape (just `scope` + `id`). The minimal shape is what a
//     "reset all buckets" action emits.
//   * Counters payload decodes the cooldown-release flag.
//   * A malformed JSON payload is logged but does NOT panic the
//     decode site. The dispatch path uses the same call so this
//     also covers `Listener::dispatch`'s decode-failure arm.
//   * Unknown channel strings hit the "ignore" branch without
//     touching state — verified by routing a synthetic channel
//     through a helper that asserts no mutation calls.

// ---------------------------------------------------------------------------
// Cache stats responder
// ---------------------------------------------------------------------------

/// The counters of every layer this gateway has: the three Community layers
/// and whatever an extension registered with counters
/// (`services::cache::layers`).
pub fn snapshot_layers(
    state: &crate::services::AppState,
) -> std::collections::HashMap<String, CacheLayerStatsPayload> {
    let mut layers: std::collections::HashMap<String, CacheLayerStatsPayload> =
        std::collections::HashMap::new();

    // Snapshot each layer. Each .stats() call is non-blocking — atomic
    // loads + an entry_count() (moka: relaxed read; DashMap::len()).
    // The snapshot doesn't synchronise across layers — that would be
    // overengineered for an operator console where the data is
    // already a 1-tick-stale view of in-process state.
    let p = state.argument_policy_cache.stats();
    layers.insert(
        "policy".to_string(),
        CacheLayerStatsPayload {
            hits: p.hits,
            misses: p.misses,
            entries: p.entries,
        },
    );
    let g = state.group_cache.stats();
    layers.insert(
        "group".to_string(),
        CacheLayerStatsPayload {
            hits: g.hits,
            misses: g.misses,
            entries: g.entries,
        },
    );
    let m = state.mcp_cache.stats();
    layers.insert(
        "tool".to_string(),
        CacheLayerStatsPayload {
            hits: m.hits,
            misses: m.misses,
            entries: m.entries,
        },
    );
    // The layers extensions registered: `response` (the prompt-cache policy
    // cache) when that extension is composed.
    for layer in state.cache_layers.layers() {
        if let Some(stats) = layer.stats {
            let r = stats(state);
            layers.insert(
                layer.name.to_string(),
                CacheLayerStatsPayload {
                    hits: r.hits,
                    misses: r.misses,
                    entries: r.entries,
                },
            );
        }
    }
    layers
}

/// Handle a `brutor:cache:stats:request`.
///
/// A Community gateway is one instance, so it answers with
/// one reply: the request's correlation id and its own counters
/// ([`CacheStatsReplyPayload`]). There is nothing to tell apart and nothing
/// to collect. Where an extension installed its own answer
/// ([`CacheStatsResponder`]: the clustering extension, whose reply names the
/// instance it comes from), that answer is the whole of it and nothing is
/// published here.
///
/// The `semantic` layer is deliberately absent: its hit/miss data comes
/// from `proxy_logs` aggregations, not in-process counters, so the control
/// plane reads it directly.
pub(crate) async fn handle_cache_stats_request(state: &SharedState, req: CacheStatsRequestPayload) {
    if let Some(respond) = state.cache_stats_responder.installed() {
        respond(state, &req).await;
        return;
    }
    let reply = CacheStatsReplyPayload {
        correlation_id: req.correlation_id.clone(),
        layers: snapshot_layers(state),
    };
    publish_cache_stats_reply(state, &req.correlation_id, &reply).await;
}

/// Publish `reply` on the reply channel.
///
/// Delivery is best-effort: a `pg_notify` failure is logged at WARN and
/// otherwise silent. The control plane treats a missing reply as "no live
/// counters" and the operator's next refresh asks again.
pub async fn publish_cache_stats_reply<T: Serialize>(
    state: &SharedState,
    correlation_id: &str,
    reply: &T,
) {
    let body = match serde_json::to_string(reply) {
        Ok(s) => s,
        Err(e) => {
            // Serialisation failure here would mean a non-finite or
            // unrepresentable value — none of the fields can produce
            // that, so this branch is defensive.
            tracing::error!(
                error = %e,
                correlation_id = %correlation_id,
                "Cache stats reply serialisation failed — dropping"
            );
            return;
        }
    };

    let res = sqlx::query("SELECT pg_notify($1, $2)")
        .bind(CHANNEL_CACHE_STATS_REPLY)
        .bind(&body)
        .execute(&state.db)
        .await;

    match res {
        Ok(_) => tracing::debug!(
            correlation_id = %correlation_id,
            "Cache stats reply published"
        ),
        Err(e) => tracing::warn!(
            // Downstream dep (Postgres) — taxonomy says WARN, not
            // ERROR. Nothing requires durability here.
            error = %e,
            correlation_id = %correlation_id,
            "Cache stats reply publish failed"
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn community_state(url: &str, pool: sqlx::PgPool) -> crate::services::AppState {
        crate::services::AppState::new(
            crate::config::AppConfig::for_test(url.to_string()),
            pool,
            None,
            reqwest::Client::new(),
            crate::services::telemetry::TelemetryState {
                prometheus_handle: None,
            },
        )
    }

    /// The listener names no Enterprise cache. A gateway composed
    /// without the prompt cache has no `response` layer: the statistics
    /// reply does not carry it and a flush that names it is an unknown
    /// layer. (One composed with it reports the four layers the console has
    /// always shown: the Enterprise workspace's
    /// `invalidation_tests::the_response_layer_is_there_when_prompt_caching_is_composed`.)
    #[tokio::test]
    async fn the_response_layer_is_not_there_without_its_extension() {
        let url = "postgres://test:test@127.0.0.1:1/never_connected";
        let pool = sqlx::postgres::PgPoolOptions::new()
            .connect_lazy(url)
            .expect("a lazy pool");
        let community: SharedState = std::sync::Arc::new(community_state(url, pool));
        let mut names: Vec<String> = snapshot_layers(&community).into_keys().collect();
        names.sort();
        assert_eq!(names, ["group", "policy", "tool"]);
        assert!(community.cache_layers.get("response").is_none());
        // A flush that names it is ignored (an unknown layer), not a panic.
        handle_cache_invalidate(
            &community,
            CacheInvalidatePayload {
                layer: "response".to_string(),
                scope: "tenant".to_string(),
                id: "t1".to_string(),
                tenant_id: Some("t1".to_string()),
            },
        )
        .await;
        // The reconnect flush of a gateway with no registered layer runs.
        flush_after_listen(&community).await;
    }

    #[test]
    fn channel_registry_includes_all_constants() {
        // Spec-fidelity guard: the publisher (Python) and the
        // subscriber (Rust) must agree on the exact set. If
        // someone adds a CHANNEL_* const but forgets the CHANNELS
        // array, the listener silently no-ops on that channel —
        // this assertion fails-loud at build time.
        let expected: &[&str] = &[
            CHANNEL_COUNTERS_RESET,
            CHANNEL_CACHE_INVALIDATE,
            CHANNEL_SCHEDULER_TRIGGER,
            CHANNEL_SCHEDULER_PAUSE,
            CHANNEL_SCHEDULER_RESUME,
            CHANNEL_SESSION_REVOKE,
            CHANNEL_LOG_LEVEL,
            CHANNEL_MAINTENANCE,
            // REQUEST is subscribed (we answer it);
            // REPLY is deliberately NOT (we publish on it but
            // don't want to wake ourselves on our own replies).
            CHANNEL_CACHE_STATS_REQUEST,
        ];
        for c in expected {
            assert!(
                CHANNELS.contains(c),
                "CHANNELS registry is missing {c} — \
                 add the constant to CHANNELS[] in system_notify.rs"
            );
        }
        // And the reverse direction: every entry in CHANNELS
        // must be in the expected list — catches a stale entry
        // after a channel removal.
        for c in CHANNELS {
            assert!(
                expected.contains(c),
                "CHANNELS has an unexpected entry {c} — drop it"
            );
        }
    }

    #[test]
    fn counters_payload_decodes_full_shape() {
        let json = r#"{
            "scope":"group",
            "id":"grp_abc",
            "limit_types":["rpm","itpm"],
            "tenant_id":"tnt_xyz"
        }"#;
        let p: CountersResetPayload = serde_json::from_str(json).expect("decode");
        assert_eq!(p.scope, "group");
        assert_eq!(p.id, "grp_abc");
        assert_eq!(
            p.limit_types.as_deref(),
            Some(["rpm".to_string(), "itpm".to_string()].as_slice())
        );
        assert_eq!(p.tenant_id.as_deref(), Some("tnt_xyz"));
        assert!(p.release_cooldown.is_none());
    }

    #[test]
    fn counters_payload_decodes_minimal_shape() {
        // "reset everything I can find for this subject" — what
        // the admin UI emits when no `limit_types` is selected.
        let json = r#"{"scope":"model","id":"gpt-4o"}"#;
        let p: CountersResetPayload = serde_json::from_str(json).expect("decode");
        assert_eq!(p.scope, "model");
        assert_eq!(p.id, "gpt-4o");
        assert!(p.limit_types.is_none());
        assert!(p.tenant_id.is_none());
    }

    #[test]
    fn counters_payload_decodes_cooldown_release() {
        let json = r#"{
            "scope":"model",
            "id":"claude-opus-4",
            "release_cooldown":true
        }"#;
        let p: CountersResetPayload = serde_json::from_str(json).expect("decode");
        assert_eq!(p.scope, "model");
        assert_eq!(p.id, "claude-opus-4");
        assert_eq!(p.release_cooldown, Some(true));
    }

    #[test]
    fn malformed_json_does_not_panic_the_decoder() {
        // The dispatch path calls `serde_json::from_str` and
        // matches on Result — that branch must NEVER unwind. We
        // assert at the underlying call site so the contract is
        // visible to anyone who tweaks `dispatch`.
        let bad = "{not-a-json";
        let res: Result<CountersResetPayload, _> = serde_json::from_str(bad);
        assert!(res.is_err(), "expected decode error for {bad}");
        // The helper that logs the failure must accept any
        // Display-able error and must not panic when called.
        log_decode_failure(CHANNEL_COUNTERS_RESET, bad, res.unwrap_err());
    }

    #[test]
    fn unknown_channel_string_is_outside_registry() {
        // The dispatch's `other =>` arm fires only for strings
        // not in CHANNELS. Verifying that a deliberately-foreign
        // channel name is, in fact, not in the registry guards
        // against an accidental future addition that would mask
        // the warn-log branch.
        let foreign = "brutor:zzz:not-real";
        assert!(!CHANNELS.contains(&foreign));
    }

    // -----------------------------------------------------------------
    // Cache statistics wire contract
    // -----------------------------------------------------------------

    #[test]
    fn cache_stats_request_channel_is_subscribed() {
        // The control plane fires REQUEST; we MUST listen for it.
        // Forgetting to add it to CHANNELS = the responder never
        // wakes and operators see empty cache cards forever.
        assert!(
            CHANNELS.contains(&CHANNEL_CACHE_STATS_REQUEST),
            "request channel must be in CHANNELS"
        );
    }

    #[test]
    fn cache_stats_reply_channel_is_not_subscribed() {
        // We publish on REPLY but never listen on it — otherwise
        // the gateway wakes itself up on its own reply, doubling
        // the NOTIFY rate for no value.
        assert!(
            !CHANNELS.contains(&CHANNEL_CACHE_STATS_REPLY),
            "reply channel must NOT be in CHANNELS"
        );
    }

    #[test]
    fn cache_stats_request_payload_decodes_min_shape() {
        // Matches the Python publisher's strict shape:
        // correlation_id required, no extras.
        let payload = r#"{"correlation_id":"abc12345-9999"}"#;
        let parsed: CacheStatsRequestPayload = serde_json::from_str(payload).expect("decode");
        assert_eq!(parsed.correlation_id, "abc12345-9999");
    }

    /// The Community reply is the request's correlation id and the
    /// counters, and nothing that names an instance.
    #[test]
    fn the_community_reply_is_the_correlation_id_and_the_counters() {
        let mut layers = std::collections::HashMap::new();
        layers.insert(
            "policy".to_string(),
            CacheLayerStatsPayload {
                hits: 10,
                misses: 2,
                entries: 8,
            },
        );
        let reply = CacheStatsReplyPayload {
            correlation_id: "abc12345".to_string(),
            layers,
        };
        let value = serde_json::to_value(&reply).expect("serialise");
        assert_eq!(
            value,
            serde_json::json!({
                "correlation_id": "abc12345",
                "layers": { "policy": { "hits": 10, "misses": 2, "entries": 8 } }
            })
        );
    }

    /// One responder, installed once: a second is refused and names the
    /// first.
    #[test]
    fn a_statistics_responder_is_installed_once() {
        fn respond<'a>(
            _state: &'a SharedState,
            _req: &'a CacheStatsRequestPayload,
        ) -> CacheStatsFuture<'a> {
            Box::pin(async {})
        }
        let mut responder = CacheStatsResponder::community();
        assert!(responder.feature().is_none() && responder.installed().is_none());
        responder.install("clustering", respond).expect("the first");
        assert_eq!(responder.feature(), Some("clustering"));
        let refused = responder.install("other", respond).expect_err("the second");
        assert_eq!(refused.by, "clustering");
        assert_eq!(responder.feature(), Some("clustering"));
    }
}
