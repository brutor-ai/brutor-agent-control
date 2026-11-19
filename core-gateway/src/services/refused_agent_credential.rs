// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The record of a refused agent credential.
//!
//! A credential of an agent identity that does not stand (disabled, revoked,
//! expired, not `active`) is refused at authentication with 401: an API key
//! bound to the agent (`auth::authenticate_request`) and a verified workload
//! token of the agent (`workload_identity::resolve_agent_from_token`). The
//! caller learns nothing but 401. The operator must still be able to see
//! that a dead agent's credential is in use, so each refusal is counted here
//! and written as an `agent_authz_block` audit row (`proxy_logs`, decision
//! `deny`, status 401, `is_auth_event`), attributed to the agent and to the
//! tenant that owns the credential.
//!
//! **Sealed.** The row is a row of the surface the request was refused on
//! (`llm`, `mcp`, `a2a`, `skill`), so the evidence writer seals it
//! as a `denied` record, as it seals the agent gate's refusals. The record
//! states the route ([`route_of`]: the path's fixed words, every other
//! segment as `{}`, so no name, id or secret a caller put in a path enters
//! it), how many refusals it stands for, and which kind of credential was
//! refused. No key, no token and no user id enters it. A refusal on a route
//! of no evidence surface (OAuth, approvals, the audit and evidence APIs)
//! stays a plain audit row, endpoint `authentication`. A route an extension
//! mounts is on the surface the extension registers it with
//! ([`RouteSurfaces::register`], `AppState::route_surfaces`): Community
//! code does not name the Enterprise batch routes
//! (`tests/batch_extension_guard.rs`), so the batch extension says that
//! its mount is the batch surface, and a refusal there is sealed like the
//! others (it was an unsealed `oauth` row).
//!
//! **How the route gets here.** Authentication does not know the route and
//! is called from thirty places. The tenant middleware, which every route
//! is behind and which reads the path for its own rule, opens a
//! [`RequestScope`] around the request: the route, and this instance's
//! [`Recorder`]. [`record`] reads it, as it reads the request's
//! implementation carriers. No signature changed.
//!
//! **Bounded.** A dead key can be replayed at any rate, and a sealed record
//! per replay would let an attacker grow the evidence chain, so refusals
//! are not one record each. A request counts once, however often its
//! handler authenticates. Per credential (per key id, or per agent
//! for a workload token) at most one record is written every
//! [`RECORD_INTERVAL_SECS`]; it carries how many refusals it stands for and
//! the route of the first of them. The first refusal is written at the next
//! flush ([`FLUSH_INTERVAL_SECS`]), so it is seen promptly. Only a
//! credential that matched a real row gets here (a key whose hash is a row
//! of `api_keys`, a token whose signature verified), so the number of
//! entries is bounded by the credentials that exist; [`MAX_TRACKED`] bounds
//! it regardless, and what does not fit is counted in
//! `brutor_refused_agent_credentials_untracked_total`.
//!
//! **Across instances.** With Redis, the interval is shared: an instance
//! writes a credential's record only if it sets
//! `brutor:refused-agent-credential:<sha256 of the slot>` with `SET NX PX`
//! (the slot names a key id or an agent id, never a credential, and only
//! its hash is a Redis key). An instance that does not get it keeps its
//! count and asks again at its next flush, so N instances write one record
//! per credential per interval and no refusal goes uncounted. When Redis
//! cannot be asked the instance writes: the bound is then per process, as
//! it is without Redis. At most one flush interval of refusals is lost on a
//! crash; a graceful stop writes what is pending without asking.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use sha2::{Digest, Sha256};
use tokio_util::sync::CancellationToken;

use crate::models::auth::{AuthContext, AuthType};
use crate::services::SharedState;

/// How often due records are written.
pub const FLUSH_INTERVAL_SECS: u64 = 2;
/// The least time between two records of one credential.
pub const RECORD_INTERVAL_SECS: u64 = 60;
/// The most credentials tracked at once.
pub const MAX_TRACKED: usize = 10_000;

/// `proxy_logs.endpoint` of a record on no evidence surface.
pub const ENDPOINT: &str = "authentication";

/// `custom_metadata` keys of the row, read by the evidence writer.
pub const META_ROUTE: &str = "refused_route";
pub const META_REFUSALS: &str = "refusals";
pub const META_CREDENTIAL: &str = "refused_credential";

/// Which kind of credential was refused.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Credential {
    ApiKey,
    WorkloadToken,
}

impl Credential {
    pub fn as_str(self) -> &'static str {
        match self {
            Credential::ApiKey => "api_key",
            Credential::WorkloadToken => "workload_token",
        }
    }

    /// Is `s` the name of a credential kind?
    pub fn is_kind(s: &str) -> bool {
        s == Credential::ApiKey.as_str() || s == Credential::WorkloadToken.as_str()
    }
}

/// Why the agent's credential was refused.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Why {
    /// The agent identity is disabled, revoked, expired or not `active`
    /// (401: the caller is told nothing more).
    AgentDoesNotStand,
    /// The agent stands but is a member of no active group of the tenant
    /// (403 `AGENT_NOT_BOUND`).
    AgentNotBound,
}

impl Why {
    /// The status the caller was answered with.
    pub fn status(self) -> u16 {
        match self {
            Why::AgentDoesNotStand => 401,
            Why::AgentNotBound => 403,
        }
    }

    fn slot(self) -> &'static str {
        match self {
            Why::AgentDoesNotStand => "",
            Why::AgentNotBound => "unbound:",
        }
    }
}

// ---------------------------------------------------------------------------
// The route
// ---------------------------------------------------------------------------

/// The route a request was refused on.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Route {
    /// `proxy_logs.proxy_type` of the route's surface.
    pub proxy_type: &'static str,
    /// The path with every segment that is not a fixed route word as `{}`.
    pub label: String,
}

/// The words a route label may carry: the fixed segments of the gateway's
/// action routes. Anything else in a path (a tenant, an agent card, a model
/// name, an id, whatever a caller typed) becomes `{}`.
const ROUTE_WORDS: &[&str] = &[
    "v1",
    "v1beta",
    "proxy",
    "llm",
    "chat",
    "completions",
    "responses",
    "embeddings",
    "models",
    "messages",
    "count_tokens",
    "images",
    "generations",
    "audio",
    "speech",
    "transcriptions",
    "video",
    "jobs",
    "mcp",
    "vmcp",
    "a2a",
    "skills",
    "execute",
    "message:send",
    "message:stream",
    "tasks",
    // Vocabulary of routes an extension mounts and registers
    // ([`RouteSurfaces::register`]). A word here mounts nothing and names no
    // route; a segment that is not in this list is `{}` in a label.
    "batch",
    "cancel",
    "results",
];

/// The most segments a label keeps.
const ROUTE_SEGMENTS: usize = 8;

/// True when `path` is `prefix` or a path below it.
fn is_under(path: &str, prefix: &str) -> bool {
    path.strip_prefix(prefix)
        .is_some_and(|rest| rest.is_empty() || rest.starts_with('/'))
}

/// The route of a request path, when the path is on an evidence surface of
/// the Community Edition. Never the query: a Gemini client sends its key
/// there.
pub fn route_of(path: &str) -> Option<Route> {
    COMMUNITY_MOUNTS
        .iter()
        .find(|(mount, _)| is_under(path, mount))
        .map(|(_, proxy_type)| Route {
            proxy_type,
            label: label_of(path),
        })
}

/// The mounts of the Community Edition's evidence surfaces, with the
/// `proxy_type` of their rows.
const COMMUNITY_MOUNTS: &[(&str, &str)] = {
    use crate::constants::proxy;
    &[
        ("/v1/proxy/llm", proxy::LLM),
        ("/v1/messages", proxy::LLM),
        ("/v1beta", proxy::LLM),
        ("/v1/proxy/mcp", proxy::MCP),
        ("/v1/proxy/vmcp", proxy::VIRTUAL_MCP),
        ("/v1/proxy/a2a", proxy::A2A),
        ("/a2a", proxy::A2A),
        ("/v1/proxy/skills", proxy::SKILL),
    ]
};

/// The label of a path: its fixed route words, every other segment `{}`.
fn label_of(path: &str) -> String {
    let mut label = String::new();
    for segment in path
        .split('/')
        .filter(|s| !s.is_empty())
        .take(ROUTE_SEGMENTS)
    {
        label.push('/');
        label.push_str(if ROUTE_WORDS.contains(&segment) {
            segment
        } else {
            "{}"
        });
    }
    label
}

/// The surfaces of the routes extensions mount: the registry an extension
/// states its mount's surface in (`AppState::route_surfaces`, filled in
/// [`crate::builder::Extension::install_state`]). The Community routes need
/// no entry ([`route_of`]).
#[derive(Clone, Debug, Default)]
pub struct RouteSurfaces {
    /// `(mount, proxy_type)`.
    mounts: Vec<(&'static str, &'static str)>,
}

/// Why a mount was not registered.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MountRefused(pub &'static str, pub &'static str);

impl std::fmt::Display for MountRefused {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "route surface of `{}` not registered: {}",
            self.0, self.1
        )
    }
}

impl std::error::Error for MountRefused {}

impl RouteSurfaces {
    /// State that the routes under `mount` are on the surface whose rows
    /// carry `proxy_type` (a `constants::proxy` value the evidence writer
    /// knows a surface for). Refused when the mount is not an absolute path
    /// of route words, when it is a Community mount, lies under one or
    /// holds one, or when a registered mount is, lies under or holds it:
    /// nothing overrides a surface in place.
    pub fn register(
        &mut self,
        mount: &'static str,
        proxy_type: &'static str,
    ) -> Result<(), MountRefused> {
        use crate::constants::proxy;
        if !is_route_label(mount) || mount.contains("{}") {
            return Err(MountRefused(mount, "not an absolute path of route words"));
        }
        if ![
            proxy::LLM,
            proxy::BATCH,
            proxy::MCP,
            proxy::VIRTUAL_MCP,
            proxy::A2A,
            proxy::SKILL,
        ]
        .contains(&proxy_type)
        {
            return Err(MountRefused(mount, "its rows are of no evidence surface"));
        }
        let overlaps = |m: &str| is_under(mount, m) || is_under(m, mount);
        if COMMUNITY_MOUNTS.iter().any(|(m, _)| overlaps(m)) {
            return Err(MountRefused(mount, "overlaps a Community route"));
        }
        if self.mounts.iter().any(|(m, _)| overlaps(m)) {
            return Err(MountRefused(mount, "overlaps a registered mount"));
        }
        self.mounts.push((mount, proxy_type));
        Ok(())
    }

    /// The route of a request path in this composition: a Community route
    /// ([`route_of`]), else a mount an extension registered; `None` when the
    /// path is on no evidence surface.
    pub fn route_of(&self, path: &str) -> Option<Route> {
        route_of(path).or_else(|| {
            self.mounts
                .iter()
                .find(|(mount, _)| is_under(path, mount))
                .map(|(_, proxy_type)| Route {
                    proxy_type,
                    label: label_of(path),
                })
        })
    }
}

/// Is `s` a label [`route_of`] can produce? The evidence writer puts no
/// other string in a record as a route.
pub fn is_route_label(s: &str) -> bool {
    let mut segments = s.split('/');
    segments.next() == Some("")
        && s.len() > 1
        && segments.clone().count() <= ROUTE_SEGMENTS
        && segments.all(|seg| seg == "{}" || ROUTE_WORDS.contains(&seg))
}

// ---------------------------------------------------------------------------
// The request scope
// ---------------------------------------------------------------------------

/// What the tenant middleware knows about the request that authentication
/// does not.
pub struct RequestScope {
    route: Option<Route>,
    /// The path's label ([`label_of`]), whatever its surface.
    label: String,
    /// The request's HTTP method, when the middleware stated it.
    method: Option<String>,
    recorder: Arc<Recorder>,
    /// One request is one refusal, however often it authenticates.
    counted: AtomicBool,
}

impl RequestScope {
    pub fn new(path: &str, surfaces: &RouteSurfaces, recorder: Arc<Recorder>) -> Self {
        Self {
            route: surfaces.route_of(path),
            label: label_of(path),
            method: None,
            recorder,
            counted: AtomicBool::new(false),
        }
    }

    /// The request's HTTP method, for the rows written in its scope.
    pub fn with_method(mut self, method: &str) -> Self {
        self.method = Some(method.to_string());
        self
    }
}

/// What the current request's scope knows: its route on an evidence surface
/// (`None` off them), its path label (no name, id or secret a caller put in
/// the path) and its method. `None` outside a request scope.
pub fn current_request() -> Option<(Option<Route>, String, Option<String>)> {
    REQUEST
        .try_with(|request| {
            (
                request.route.clone(),
                request.label.clone(),
                request.method.clone(),
            )
        })
        .ok()
}

tokio::task_local! {
    static REQUEST: RequestScope;
}

/// Run `fut` as one request of `scope`.
pub async fn scope<F: std::future::Future>(scope: RequestScope, fut: F) -> F::Output {
    REQUEST.scope(scope, fut).await
}

// ---------------------------------------------------------------------------
// The table
// ---------------------------------------------------------------------------

/// One refusal, as the authenticator saw it.
#[derive(Debug, Clone)]
pub struct Refused {
    pub credential: Credential,
    pub why: Why,
    /// The tenant that owns the credential (the key's, the agent's).
    pub tenant_id: String,
    pub agent_id: String,
    pub api_key_id: Option<String>,
    pub api_key_name: Option<String>,
    pub group_id: Option<String>,
    pub client_ip: Option<String>,
}

impl Refused {
    fn slot_key(&self) -> String {
        let why = self.why.slot();
        match &self.api_key_id {
            Some(id) => format!("{why}key:{id}"),
            None => format!("{why}agent:{}:{}", self.tenant_id, self.agent_id),
        }
    }
}

/// The first refusal since the last record: the one the record describes.
struct First {
    refused: Refused,
    route: Option<Route>,
    carriers: crate::services::agent_impl::Carriers,
}

struct Slot {
    first: Option<First>,
    /// Refusals since the last record.
    count: u64,
    last_written: Option<Instant>,
}

/// One record that is due.
struct Due {
    first: First,
    count: u64,
}

/// The refusals of one gateway instance that are not yet written.
#[derive(Default)]
pub struct Recorder {
    slots: Mutex<HashMap<String, Slot>>,
}

impl Recorder {
    pub fn new() -> Self {
        Self::default()
    }

    /// The table of a call outside any request scope (a route not behind
    /// the tenant middleware, a unit test).
    fn process() -> &'static Arc<Recorder> {
        static PROCESS: OnceLock<Arc<Recorder>> = OnceLock::new();
        PROCESS.get_or_init(|| Arc::new(Recorder::new()))
    }

    fn count(&self, refused: Refused, route: Option<Route>) {
        let key = refused.slot_key();
        let mut map = self.slots.lock().unwrap_or_else(|p| p.into_inner());
        if !map.contains_key(&key) && map.len() >= MAX_TRACKED {
            metrics::counter!("brutor_refused_agent_credentials_untracked_total").increment(1);
            tracing::error!(
                tracked = map.len(),
                "refused agent credential not recorded: the tracking table is full"
            );
            return;
        }
        let slot = map.entry(key).or_insert(Slot {
            first: None,
            count: 0,
            last_written: None,
        });
        slot.count += 1;
        if slot.first.is_none() {
            slot.first = Some(First {
                refused,
                route,
                carriers: crate::services::agent_impl::request_carriers(),
            });
        }
    }

    /// The slots whose interval is over at `now` and that have a refusal
    /// pending; entries with nothing pending whose interval has passed are
    /// dropped.
    fn due_keys(&self, now: Instant, interval: Duration) -> Vec<String> {
        let mut map = self.slots.lock().unwrap_or_else(|p| p.into_inner());
        let mut due = Vec::new();
        map.retain(|key, slot| {
            let waited = slot
                .last_written
                .is_none_or(|at| now.duration_since(at) >= interval);
            if !waited {
                return true;
            }
            if slot.first.is_some() {
                due.push(key.clone());
                true
            } else {
                // Nothing since the last record and the interval is over:
                // forget the credential.
                false
            }
        });
        due
    }

    /// Take the pending record of `key` out, starting its next interval.
    fn take(&self, key: &str, now: Instant) -> Option<Due> {
        let mut map = self.slots.lock().unwrap_or_else(|p| p.into_inner());
        let slot = map.get_mut(key)?;
        let first = slot.first.take()?;
        let count = std::mem::take(&mut slot.count);
        slot.last_written = Some(now);
        Some(Due { first, count })
    }

    /// Number of credentials tracked (test and diagnostic hook).
    pub fn tracked(&self) -> usize {
        self.slots.lock().unwrap_or_else(|p| p.into_inner()).len()
    }
}

/// Count one refusal. Never blocks on I/O; called on the request path, in
/// the request's scope (the route, the instance's table and the caller's
/// implementation carriers are read here).
pub fn record(refused: Refused) {
    let scoped = REQUEST.try_with(|request| {
        if request.counted.swap(true, Ordering::Relaxed) {
            return None;
        }
        Some((request.recorder.clone(), request.route.clone()))
    });
    let (recorder, route) = match scoped {
        // This request's refusal is already counted.
        Ok(None) => return,
        Ok(Some(request)) => request,
        Err(_) => (Recorder::process().clone(), None),
    };
    metrics::counter!(
        "brutor_refused_agent_credentials_total",
        "credential" => refused.credential.as_str()
    )
    .increment(1);
    recorder.count(refused, route);
}

fn build_row(due: &Due) -> crate::models::db::ProxyLogInsert {
    let Due { first, count } = due;
    let refused = &first.refused;
    // The credential that was presented and refused.
    let presented = match refused.credential {
        Credential::ApiKey => crate::models::auth::Credential::api_key(
            refused.api_key_id.as_deref().unwrap_or_default(),
            false,
        ),
        Credential::WorkloadToken => crate::models::auth::Credential {
            kind: crate::models::auth::CredentialKind::WorkloadToken,
            assurance: Some(crate::models::auth::CredentialAssurance::Workload),
            reference: None,
        },
    };
    let auth = AuthContext {
        credential_kind: presented.kind,
        credential_assurance: presented.assurance,
        credential_ref: presented.reference,
        credential_detail: Default::default(),
        identity_verdict: None,
        auth_type: match refused.credential {
            Credential::ApiKey => AuthType::ApiKey,
            Credential::WorkloadToken => AuthType::IdpOauth,
        },
        tenant_id: refused.tenant_id.clone(),
        gateway_user_id: None,
        gateway_username: None,
        gateway_tenant_id: Some(refused.tenant_id.clone()),
        idp_user_id: None,
        idp_username: None,
        idp_email: None,
        api_key_id: refused.api_key_id.clone(),
        api_key_name: refused.api_key_name.clone(),
        api_key_scope: None,
        api_key_group_id: refused.group_id.clone(),
        api_key_is_platform: false,
        api_key_user_id: None,
        allowed_skill_ids: None,
        client_ip: refused.client_ip.clone(),
        session_key: None,
        session_token_type: None,
        resolved_group_id: refused.group_id.clone(),
        actor_agent_id: Some(refused.agent_id.clone()),
        agent_impl: first.carriers.clone(),
        admission_hold: None,
    };
    let plural = if *count == 1 { "" } else { "s" };
    let reason = match refused.why {
        Why::AgentDoesNotStand => format!(
            "agent_credential_refused: the agent identity is disabled, revoked, expired or not \
             active ({}; {} refusal{} since the previous record)",
            refused.credential.as_str(),
            count,
            plural
        ),
        Why::AgentNotBound => format!(
            "AGENT_NOT_BOUND: the agent is a member of no active resource group of this tenant \
             ({}; {} refusal{} since the previous record)",
            refused.credential.as_str(),
            count,
            plural
        ),
    };
    let correlation_id = ulid::Ulid::new().to_string();
    let (proxy_type, endpoint) = match &first.route {
        Some(route) => (route.proxy_type, route.label.as_str()),
        None => (crate::constants::proxy::OAUTH, ENDPOINT),
    };
    let mut log = crate::services::proxy_logger::build_proxy_log(
        proxy_type,
        &auth,
        "AUTH",
        endpoint,
        None,
        None,
        refused.why.status(),
        0.0,
        false,
        Some(&reason),
        &correlation_id,
    );
    log.proxy_subtype = Some(crate::constants::proxy_subtype::AGENT_AUTHZ_BLOCK.to_string());
    log.agent_authz_decision = Some(crate::constants::agent_decision::DENY.to_string());
    log.resource_group_id = refused.group_id.clone();
    let mut meta = serde_json::Map::new();
    meta.insert(META_REFUSALS.into(), serde_json::json!(count));
    meta.insert(
        META_CREDENTIAL.into(),
        serde_json::json!(refused.credential.as_str()),
    );
    if let Some(route) = &first.route {
        meta.insert(META_ROUTE.into(), serde_json::json!(route.label));
    }
    log.custom_metadata = Some(serde_json::Value::Object(meta));
    crate::services::proxy_logger::apply_auth_event(&mut log);
    log
}

/// The Redis key of a slot's shared interval: the hash of the slot's name.
pub fn interval_key(slot_key: &str) -> String {
    format!(
        "brutor:refused-agent-credential:{}",
        hex::encode(Sha256::digest(slot_key.as_bytes()))
    )
}

/// May this instance write the record of `slot_key` now? `Ok(false)` when
/// another instance wrote one inside the interval.
async fn claim_interval(
    redis: &redis::aio::ConnectionManager,
    slot_key: &str,
    interval: Duration,
) -> Result<bool, redis::RedisError> {
    let mut redis = redis.clone();
    let set: Option<String> = redis::cmd("SET")
        .arg(interval_key(slot_key))
        .arg(1)
        .arg("NX")
        .arg("PX")
        .arg(interval.as_millis().max(1) as u64)
        .query_async(&mut redis)
        .await?;
    Ok(set.is_some())
}

/// Write the records that are due. Returns how many rows were handed to the
/// audit writer.
pub async fn flush_once(state: &SharedState) -> usize {
    flush_due(
        state,
        Instant::now(),
        Duration::from_secs(RECORD_INTERVAL_SECS),
    )
    .await
}

/// [`flush_once`] at a stated time and interval (tests move the clock). An
/// interval of zero writes everything pending without asking Redis: the
/// flush of a graceful stop.
pub async fn flush_due(state: &SharedState, now: Instant, interval: Duration) -> usize {
    let mut written = 0;
    for recorder in [&state.refused_credentials, Recorder::process()] {
        for key in recorder.due_keys(now, interval) {
            if !interval.is_zero()
                && let Some(redis) = &state.redis
            {
                match claim_interval(redis, &key, interval).await {
                    Ok(true) => {}
                    // Another instance wrote this credential's record inside
                    // the interval. The count stays and is written with the
                    // record this instance next gets to write.
                    Ok(false) => continue,
                    Err(e) => {
                        metrics::counter!("brutor_refused_agent_credentials_unshared_total")
                            .increment(1);
                        tracing::warn!(
                            error = %e.category(),
                            "refused agent credential: Redis could not be asked; \
                             the record interval is this instance's own"
                        );
                    }
                }
            }
            let Some(due) = recorder.take(&key, now) else {
                continue;
            };
            let refused = &due.first.refused;
            tracing::warn!(
                tenant_id = %refused.tenant_id,
                agent_id = %refused.agent_id,
                api_key_id = ?refused.api_key_id,
                credential = refused.credential.as_str(),
                route = ?due.first.route.as_ref().map(|r| r.label.as_str()),
                refusals = due.count,
                "refused agent credential in use"
            );
            let log = build_row(&due);
            state
                .background_writer
                .write_proxy_log(state.db.clone(), log);
            written += 1;
        }
    }
    written
}

/// Flush every [`FLUSH_INTERVAL_SECS`] until `shutdown` fires. What is
/// still pending then is written by the shutdown sequence itself, before
/// the audit writer drains (`builder.rs`), and once more here in case this
/// task is what runs first.
pub fn spawn_flusher(state: SharedState, shutdown: CancellationToken) {
    tokio::spawn(async move {
        let mut tick = tokio::time::interval(Duration::from_secs(FLUSH_INTERVAL_SECS));
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            tokio::select! {
                _ = shutdown.cancelled() => break,
                _ = tick.tick() => { flush_once(&state).await; }
            }
        }
        flush_due(&state, Instant::now(), Duration::ZERO).await;
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn refused(key: &str) -> Refused {
        Refused {
            credential: Credential::ApiKey,
            why: Why::AgentDoesNotStand,
            tenant_id: "t".into(),
            agent_id: "a".into(),
            api_key_id: Some(key.into()),
            api_key_name: None,
            group_id: None,
            client_ip: None,
        }
    }

    /// One record per credential per interval, however often it is
    /// replayed, and the record says how many refusals it stands for.
    #[test]
    fn a_replayed_credential_is_one_record_per_interval() {
        let interval = Duration::from_secs(60);
        let t0 = Instant::now();
        let recorder = Recorder::new();
        let key = "key:unit-replayed-417";
        let take = |now: Instant| -> Vec<u64> {
            recorder
                .due_keys(now, interval)
                .iter()
                .filter_map(|k| recorder.take(k, now))
                .map(|due| due.count)
                .collect()
        };
        for _ in 0..500 {
            recorder.count(refused("unit-replayed-417"), None);
        }
        assert_eq!(take(t0), vec![500]);
        for _ in 0..300 {
            recorder.count(refused("unit-replayed-417"), None);
        }
        assert!(take(t0 + Duration::from_secs(59)).is_empty());
        assert_eq!(take(t0 + Duration::from_secs(60)), vec![300]);
        // Quiet for an interval: the entry is dropped.
        assert!(take(t0 + Duration::from_secs(120)).is_empty());
        assert!(!recorder.slots.lock().unwrap().contains_key(key));
        assert_eq!(recorder.tracked(), 0);
    }

    /// A due record that is not taken (another instance holds the interval)
    /// keeps its count and stays due.
    #[test]
    fn a_record_another_instance_wrote_keeps_its_count() {
        let interval = Duration::from_secs(60);
        let t0 = Instant::now();
        let recorder = Recorder::new();
        recorder.count(refused("unit-kept-418"), None);
        assert_eq!(recorder.due_keys(t0, interval).len(), 1);
        // Not taken. More refusals arrive.
        recorder.count(refused("unit-kept-418"), None);
        recorder.count(refused("unit-kept-418"), None);
        let keys = recorder.due_keys(t0 + Duration::from_secs(2), interval);
        assert_eq!(keys, ["key:unit-kept-418"]);
        assert_eq!(recorder.take(&keys[0], t0).map(|d| d.count), Some(3));
    }

    /// The route of a path: the surface and a label of fixed words only.
    #[test]
    fn a_route_label_carries_fixed_words_only() {
        use crate::constants::proxy;
        for (path, proxy_type, label) in [
            (
                "/v1/proxy/llm/chat/completions",
                proxy::LLM,
                "/v1/proxy/llm/chat/completions",
            ),
            ("/v1/messages", proxy::LLM, "/v1/messages"),
            (
                "/v1beta/models/gemini-3-pro:generateContent",
                proxy::LLM,
                "/v1beta/models/{}",
            ),
            (
                "/v1/proxy/llm/audio/speech",
                proxy::LLM,
                "/v1/proxy/llm/audio/speech",
            ),
            ("/v1/proxy/mcp/srv-secret-1", proxy::MCP, "/v1/proxy/mcp/{}"),
            ("/v1/proxy/vmcp/x", proxy::VIRTUAL_MCP, "/v1/proxy/vmcp/{}"),
            (
                "/a2a/tenant-9/billing-agent/message:send",
                proxy::A2A,
                "/a2a/{}/{}/message:send",
            ),
            (
                "/v1/proxy/skills/execute",
                proxy::SKILL,
                "/v1/proxy/skills/execute",
            ),
        ] {
            let route = route_of(path).unwrap_or_else(|| panic!("{path}"));
            assert_eq!(
                (route.proxy_type, route.label.as_str()),
                (proxy_type, label)
            );
            assert!(is_route_label(&route.label), "{label}");
        }
        for path in [
            "/v1/proxy/oauth/token",
            "/v1/approvals/abc/poll",
            "/v1/proxy/llmx",
            "/health",
            "/",
        ] {
            assert_eq!(route_of(path), None, "{path}");
        }
        // Whatever a caller types in a path, the label has none of it.
        let typed = route_of("/v1/proxy/mcp/sk-live-0123456789abcdef/a/b/c/d/e/f/g/h").unwrap();
        assert_eq!(typed.label, "/v1/proxy/mcp/{}/{}/{}/{}/{}");
        for not_a_label in ["", "/", "v1/proxy", "/v1/proxy/mcp/srv-1", "/v1//x", "/{}x"] {
            assert!(!is_route_label(not_a_label), "{not_a_label:?}");
        }
    }

    /// An extension states the surface of its mount; nothing else is
    /// a surface, and nothing in place is overridden.
    #[test]
    fn an_extension_registers_the_surface_of_its_mount() {
        use crate::constants::proxy;
        let mut surfaces = RouteSurfaces::default();
        // A made-up mount: Community code names no Enterprise route.
        assert_eq!(surfaces.route_of("/v1/proxy/results/jobs/j-1"), None);
        surfaces
            .register("/v1/proxy/results", proxy::BATCH)
            .expect("a new mount");
        let route = surfaces
            .route_of("/v1/proxy/results/jobs/job-secret-1/cancel")
            .expect("under the mount");
        assert_eq!(
            (route.proxy_type, route.label.as_str()),
            (proxy::BATCH, "/v1/proxy/results/jobs/{}/cancel")
        );
        assert!(is_route_label(&route.label));
        // The mount itself, and nothing beside it.
        assert!(surfaces.route_of("/v1/proxy/results").is_some());
        assert_eq!(surfaces.route_of("/v1/proxy/resultsx/jobs"), None);
        assert_eq!(surfaces.route_of("/v1/proxy/oauth/token"), None);
        // The Community routes stay what they are.
        assert_eq!(
            surfaces.route_of("/v1/proxy/mcp/srv-1"),
            route_of("/v1/proxy/mcp/srv-1")
        );
        // Refused: twice, under or over a mount in place, a Community
        // route, a path that is not one of route words, a type of no
        // evidence surface.
        assert!(
            RouteSurfaces::default()
                .register("/v1/proxy", proxy::LLM)
                .is_err(),
            "a mount that holds the Community routes"
        );
        for (mount, proxy_type) in [
            ("/v1/proxy/results", proxy::BATCH),
            ("/v1/proxy/results/jobs", proxy::LLM),
            ("/v1/proxy", proxy::LLM),
            ("/v1", proxy::LLM),
            ("/v1/proxy/llm", proxy::BATCH),
            ("/v1/proxy/mcp/tasks", proxy::BATCH),
            ("/v1/proxy/{}", proxy::BATCH),
            ("/v1/proxy/Secret-Name", proxy::BATCH),
            ("v1/proxy/cancel", proxy::BATCH),
            ("", proxy::BATCH),
            ("/v1/proxy/cancel", proxy::OAUTH),
            ("/v1/proxy/cancel", "anything"),
        ] {
            assert!(
                surfaces.register(mount, proxy_type).is_err(),
                "{mount} as {proxy_type}"
            );
        }
        surfaces
            .register("/v1/proxy/cancel", proxy::LLM)
            .expect("another mount");
    }

    /// A request counts once, however often it authenticates; two
    /// requests count twice; the route is the request's.
    #[tokio::test]
    async fn a_request_counts_once() {
        let recorder = Arc::new(Recorder::new());
        for _ in 0..2 {
            scope(
                RequestScope::new(
                    "/v1/proxy/llm/audio/speech",
                    &RouteSurfaces::default(),
                    recorder.clone(),
                ),
                async {
                    record(refused("unit-once-419"));
                    record(refused("unit-once-419"));
                    record(refused("unit-once-419"));
                },
            )
            .await;
        }
        let now = Instant::now();
        let keys = recorder.due_keys(now, Duration::from_secs(60));
        let due = recorder.take(&keys[0], now).expect("due");
        assert_eq!(due.count, 2);
        assert_eq!(
            due.first.route.map(|r| r.label),
            Some("/v1/proxy/llm/audio/speech".to_string())
        );
    }

    /// The Redis key is a hash: it names neither the key id nor the agent.
    #[test]
    fn the_shared_interval_key_is_a_hash() {
        let key = interval_key("agent:tenant-420:agent-unit-420");
        assert!(!key.contains("tenant-420") && !key.contains("agent-unit-420"));
        assert_eq!(key.len(), "brutor:refused-agent-credential:".len() + 64);
        assert_ne!(key, interval_key("key:unit-420"));
    }
}
