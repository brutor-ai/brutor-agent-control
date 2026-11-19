// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Resource Cache Layer
//!
//! Enterprise-grade in-memory caching for the Core proxy hot path.
//! Uses `moka` for concurrent, TTL-based caching with push-based
//! invalidation from the Control Plane via Redis pub/sub.
//!
//! ## Architecture
//!
//! - **ModelCache**: LLM models, routing groups, group member models
//! - **ProviderCache**: LLM providers (slug, base URL, auth defaults)
//! - **ResourceGroupCache**: Materialized effective group data (models, MCPs,
//!   skills, agent cards, limits, governance) with full parent chain inheritance
//! - **McpServerCache**: MCP server + server config rows
//! - **AgentSkillCache**: Agent skill configurations
//! - **AgentCardCache**: A2A agent cards + their capabilities
//! - **GuardrailsCache**: Guardrail configurations (PII, banned words, prompt
//!   injection / toxic-content provider configs)
//!
//! The response cache is not part of this layer: it is an Enterprise
//! extension (`enterprise/src/semantic_response_cache.rs`).
//!
//! All caches use TTL as a safety net (catches missed pub/sub messages).
//! Primary invalidation is push-based via Redis pub/sub from the Control Plane.
//!
//! See `README.md` beside this file for the caches, their keys and
//! invalidation, and how to add one.

pub mod agent_card_cache;
pub mod group_cache;
pub mod invalidation;
#[cfg(any(test, feature = "db-tests"))]
pub mod invalidation_rig;
pub mod layers;
pub mod mcp_cache;
pub mod model_cache;
pub mod provider_cache;
pub mod skill_cache;
pub mod usage_state;

use serde::{Deserialize, Serialize};
use tokio_util::sync::CancellationToken;

// Re-exports
pub use agent_card_cache::{AgentCardCache, AgentCardCapability, CachedAgentCard};
pub use group_cache::{EffectiveGroupData, ResourceGroupCache};
pub use invalidation::{
    BUILTIN_INVALIDATION_HANDLERS, InvalidationHandler, InvalidationRegistry,
    InvalidationRegistryError,
};
pub use mcp_cache::McpServerCache;
pub use model_cache::ModelCache;
pub use provider_cache::{LlmProvider, ProviderCache};
pub use skill_cache::AgentSkillCache;
pub use usage_state::UsageState;

// ---------------------------------------------------------------------------
// Cache stats — shared snapshot type for the cache stats fan-out
// ---------------------------------------------------------------------------

/// One cache layer's runtime counters at a point in time.
///
/// Returned by each instrumented cache's `stats()` method and shipped
/// back to the control plane via the `brutor:cache:stats:reply`
/// NOTIFY channel. The values are intentionally u64 — they wrap at
/// 2^64 (584 years at 1B ops/sec), so the wraparound case is not
/// engineering-relevant.
///
/// `hits` and `misses` are monotonic counters from the moment the
/// replica booted, loaded with `Ordering::Relaxed` because each
/// counter is independent — there's no happens-before relationship
/// between a hit increment on one key and a miss increment on
/// another, so we don't pay for sequentially-consistent atomics.
///
/// `entries` is the live count from the underlying cache backend
/// (moka's `entry_count()`, `DashMap::len()`, or `HashMap::len()`).
/// It can lag by a small amount under churn but never reports
/// impossible values.
#[derive(Debug, Clone, Copy, Default)]
pub struct CacheStatsSnapshot {
    pub hits: u64,
    pub misses: u64,
    pub entries: u64,
}

// ---------------------------------------------------------------------------
// ResourceCache trait
// ---------------------------------------------------------------------------

/// Base trait for all resource caches.
#[async_trait::async_trait]
pub trait ResourceCache: Send + Sync {
    /// Cache name for logging and metrics
    fn name(&self) -> &str;

    /// Invalidate a specific entry by tenant + resource ID
    async fn invalidate(&self, tenant_id: &str, resource_id: &str);

    /// Invalidate all entries for a tenant
    async fn invalidate_tenant(&self, tenant_id: &str);

    /// Invalidate everything
    async fn invalidate_all(&self);
}

// ---------------------------------------------------------------------------
// Invalidation generation
// ---------------------------------------------------------------------------

/// How many invalidations one cache has seen. A load reads it before going
/// to the database and inserts with [`insert_fresh`], so a load that began
/// before an invalidation can never re-insert what it read: without
/// it, an admin edit landing during a slow load was undone by the load and
/// served stale for the whole TTL.
///
/// Ordering: an invalidation calls [`Generation::bump`] BEFORE it evicts,
/// and [`insert_fresh`] inserts BEFORE it re-reads the generation. Either
/// the loader sees the bump (and removes its entry), or the bump came after
/// its check, in which case the eviction that follows the bump also comes
/// after its insert.
#[derive(Debug, Clone, Default)]
pub struct Generation(std::sync::Arc<std::sync::atomic::AtomicU64>);

impl Generation {
    pub fn current(&self) -> u64 {
        self.0.load(std::sync::atomic::Ordering::SeqCst)
    }

    /// Call at the start of every invalidation, before evicting.
    pub fn bump(&self) {
        self.0.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    }
}

/// Insert `value` loaded since generation `started`, unless an
/// invalidation happened meanwhile (see [`Generation`]).
pub async fn insert_fresh<V>(
    cache: &moka::future::Cache<String, V>,
    key: String,
    value: V,
    generation: &Generation,
    started: u64,
) where
    V: Clone + Send + Sync + 'static,
{
    if generation.current() != started {
        return;
    }
    cache.insert(key.clone(), value).await;
    if generation.current() != started {
        cache.invalidate(&key).await;
    }
}

// ---------------------------------------------------------------------------
// Cache invalidation message (Redis pub/sub)
// ---------------------------------------------------------------------------

/// Channel name for cache invalidation messages.
pub const INVALIDATION_CHANNEL: &str = "brutor:cache:invalidate";

/// Invalidation message published by the Control Plane and consumed by Core pods.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InvalidationMessage {
    /// Resource type: "model", "routing_group", "resource_group", "skill", "guardrail"
    #[serde(rename = "type")]
    pub resource_type: String,
    /// Action: "create", "update", "delete", "assign"
    pub action: String,
    /// Tenant ID
    pub tenant_id: String,
    /// Resource ID (model ID, group ID, etc.)
    pub id: String,
}

// ---------------------------------------------------------------------------
// CacheInvalidator — Redis pub/sub subscriber
// ---------------------------------------------------------------------------

/// Listens to Redis pub/sub for cache invalidation messages from the Control Plane
/// and evicts stale entries from all in-memory caches.
///
/// Every field here MUST be a handle that shares state with the
/// instance held in `AppState` — the moka-backed caches do this via
/// their `Clone` impl (cloning gives a handle to the same internal
/// `Arc`-shared store), and `provider_cache` / `guardrails_cache` are
/// passed as `Arc` clones. (The caches of an Enterprise feature are not
/// fields here: its handlers reach them through `state`, in the extension
/// state the feature installed.) If a cache is
/// constructed fresh here instead of cloned from `state`, pub/sub
/// invalidations evict a phantom cache and the request pipeline
/// continues serving stale data until the TTL expires.
pub struct CacheInvalidator {
    pub model_cache: ModelCache,
    pub group_cache: ResourceGroupCache,
    pub mcp_cache: McpServerCache,
    pub skill_cache: AgentSkillCache,
    pub provider_cache: std::sync::Arc<ProviderCache>,
    /// Guardrail config cache. Carries the same `Arc`-shared inner
    /// state as `AppState.guardrails_cache` so admin-side updates
    /// (banned-words list edits, provider key rotations) take effect
    /// before the 5-minute TTL expires.
    pub guardrails_cache: crate::services::guardrails::GuardrailsCache,
    /// Argument-policy cache (POLICY-10). Same Clone-shares-state
    /// requirement as `guardrails_cache`.
    pub argument_policy_cache: crate::services::argument_policy::ArgumentPolicyCache,
    /// Agent card cache. Same sharing requirement — `state.agent_card_cache`
    /// and this field must be Clone-handles to the same instance.
    pub agent_card_cache: AgentCardCache,
    /// `SharedState` reference: the caches that are not fields of this
    /// struct (the semantic-policy cache, the maintenance cache) and, for
    /// an extension's handlers, the state the extension installed
    /// (`state.extensions`). Holding the Arc is safe: the listener task is
    /// spawned after `state` exists, and `CacheInvalidator` is consumed by
    /// `spawn` so the only owner is the listener task itself.
    ///
    /// `None` is tolerated for unit tests that construct a
    /// minimal invalidator without a full AppState. Production
    /// always sets this — the listener task passes `state.clone()`.
    pub state: Option<crate::services::SharedState>,
    /// Redis connection manager. Required for clearing the
    /// Redis-resident pieces of the prompt-cache state (sticky pins,
    /// Gemini cache pointers). `None` is tolerated — the listener
    /// only updates the in-process caches in that case.
    pub redis: Option<redis::aio::ConnectionManager>,
    /// What a message of each resource type evicts (`invalidation.rs`). The
    /// built-in handlers, and whatever an extension registered after them.
    pub handlers: InvalidationRegistry,
}

impl CacheInvalidator {
    /// Spawn a background task that subscribes to the invalidation channel.
    /// Runs until the shutdown token is cancelled.
    ///
    /// Reconnect policy: the inner subscribe + listen loop reconnects
    /// on Redis disconnect with exponential backoff (2s → 30s cap).
    /// A hard failure to even open the client (bad URL, DNS) still
    /// aborts the task — there's no point retrying a malformed
    /// config.
    pub fn spawn(self, redis_url: String, shutdown: CancellationToken) {
        tokio::spawn(async move {
            tracing::info!("Cache invalidation listener starting on {INVALIDATION_CHANNEL}");

            let client = match redis::Client::open(redis_url.as_str()) {
                Ok(c) => c,
                Err(e) => {
                    tracing::warn!("Failed to connect to Redis for cache invalidation: {e}");
                    tracing::warn!("Cache invalidation disabled — caches will rely on TTL only");
                    return;
                }
            };

            // Outer reconnect loop. We come back here whenever the
            // pub/sub stream ends or the subscribe handshake fails.
            // `backoff_secs` resets to 2s on every successful
            // subscription so a one-off blip doesn't poison the
            // recovery cadence.
            let mut backoff_secs: u64 = 2;
            loop {
                if shutdown.is_cancelled() {
                    tracing::info!("Cache invalidation listener shutting down");
                    return;
                }

                let mut pubsub = match client.get_async_pubsub().await {
                    Ok(ps) => ps,
                    Err(e) => {
                        tracing::warn!(
                            redis_error = %e,
                            backoff_secs,
                            "Failed to create Redis pub/sub connection — retrying"
                        );
                        if Self::sleep_or_shutdown(backoff_secs, &shutdown).await {
                            return;
                        }
                        backoff_secs = (backoff_secs.saturating_mul(2)).min(30);
                        continue;
                    }
                };

                if let Err(e) = pubsub.subscribe(INVALIDATION_CHANNEL).await {
                    tracing::warn!(
                        redis_error = %e,
                        backoff_secs,
                        "Failed to subscribe to {INVALIDATION_CHANNEL} — retrying"
                    );
                    if Self::sleep_or_shutdown(backoff_secs, &shutdown).await {
                        return;
                    }
                    backoff_secs = (backoff_secs.saturating_mul(2)).min(30);
                    continue;
                }

                tracing::info!("Cache invalidation listener active");
                backoff_secs = 2;
                // Messages published while we were not subscribed are gone
                // (pub/sub does not replay), so anything cached may have
                // missed its invalidation: drop it all and reload on demand.
                // Also on the first subscribe, which covers entries
                // loaded during startup before the listener was up.
                self.flush_config_caches().await;

                use futures::StreamExt;
                let mut stream = pubsub.on_message();

                // Inner message-pump loop. Breaks (and falls through to
                // the outer reconnect) when the stream ends; returns
                // (terminates the task) only on shutdown.
                loop {
                    tokio::select! {
                        _ = shutdown.cancelled() => {
                            tracing::info!("Cache invalidation listener shutting down");
                            return;
                        }
                        msg = stream.next() => {
                            match msg {
                                Some(msg) => {
                                    let payload: String = match msg.get_payload() {
                                        Ok(p) => p,
                                        Err(e) => {
                                            tracing::warn!("Failed to read invalidation payload: {e}");
                                            continue;
                                        }
                                    };

                                    match serde_json::from_str::<InvalidationMessage>(&payload) {
                                        Ok(inv) => {
                                            tracing::debug!(
                                                resource_type = %inv.resource_type,
                                                action = %inv.action,
                                                tenant_id = %inv.tenant_id,
                                                id = %inv.id,
                                                "Cache invalidation received"
                                            );
                                            self.handle_invalidation(&inv).await;
                                        }
                                        Err(e) => {
                                            tracing::warn!("Failed to parse invalidation message: {e}");
                                        }
                                    }
                                }
                                None => {
                                    tracing::warn!(
                                        backoff_secs,
                                        "Redis pub/sub stream ended — reconnecting"
                                    );
                                    break;
                                }
                            }
                        }
                    }
                }

                if Self::sleep_or_shutdown(backoff_secs, &shutdown).await {
                    return;
                }
                backoff_secs = (backoff_secs.saturating_mul(2)).min(30);
            }
        });
    }

    /// Drop every configuration cache this listener keeps fresh. Runs after
    /// each (re)subscribe; the price is one cold load per entry.
    ///
    /// Not flushed: the semantic RESPONSE cache (per-tenant Qdrant
    /// collections; the store-time policy check stays its own bound) and
    /// usage counters (state, not configuration).
    pub async fn flush_config_caches(&self) {
        self.model_cache.invalidate_all().await;
        self.group_cache.invalidate_all().await;
        self.mcp_cache.invalidate_all().await;
        self.skill_cache.invalidate_all().await;
        self.provider_cache.invalidate_all().await;
        self.guardrails_cache.invalidate_all().await;
        self.argument_policy_cache.invalidate_all().await;
        self.agent_card_cache.invalidate_all().await;
        crate::services::run_budget::invalidate_all();
        if let Some(ref state) = self.state {
            state.semantic_policy_cache.invalidate_all().await;
            state.maintenance_cache.invalidate(None);
            // Tenant status (suspension, switches, the identity policy
            // default).
            state.tenant_cache.invalidate_all_tenants();
        }
        // The caches a handler owns: whatever an extension registered (the
        // prompt-cache policies, when that extension is composed).
        for handler in self.handlers.handlers() {
            if let Some(flush) = handler.flush {
                flush(self).await;
            }
        }
        tracing::info!("Configuration caches flushed after (re)subscribing to invalidations");
    }

    /// Sleep for `secs` seconds OR until shutdown fires, whichever
    /// comes first. Returns `true` when shutdown won — the caller
    /// must terminate. Returns `false` when the timer elapsed and
    /// the caller should keep reconnecting.
    async fn sleep_or_shutdown(secs: u64, shutdown: &CancellationToken) -> bool {
        tokio::select! {
            _ = shutdown.cancelled() => true,
            _ = tokio::time::sleep(std::time::Duration::from_secs(secs)) => false,
        }
    }

    /// Answer one message: every handler registered for its type, in order,
    /// and for a type nobody registered the broad eviction that was the `_`
    /// arm of the `match` this used to be (see `invalidation.rs`).
    async fn handle_invalidation(&self, msg: &InvalidationMessage) {
        let mut answered = false;
        for handler in self.handlers.handlers_for(&msg.resource_type) {
            (handler.handle)(self, msg).await;
            answered = true;
        }
        if !answered {
            invalidation::unregistered_type(self, msg).await;
        }
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_invalidation_message() {
        let json = r#"{"type":"model","action":"update","tenant_id":"t1","id":"llm-01"}"#;
        let msg: InvalidationMessage = serde_json::from_str(json).unwrap();
        assert_eq!(msg.resource_type, "model");
        assert_eq!(msg.action, "update");
        assert_eq!(msg.tenant_id, "t1");
        assert_eq!(msg.id, "llm-01");
    }

    #[test]
    fn serialize_invalidation_message() {
        let msg = InvalidationMessage {
            resource_type: "resource_group".into(),
            action: "delete".into(),
            tenant_id: "t1".into(),
            id: "grp-01".into(),
        };
        let json = serde_json::to_string(&msg).unwrap();
        assert!(json.contains("\"type\":\"resource_group\""));
    }

    #[test]
    fn parse_all_resource_types() {
        for rt in [
            "model",
            "routing_group",
            "resource_group",
            "skill",
            "guardrail",
        ] {
            let json = format!(r#"{{"type":"{rt}","action":"create","tenant_id":"t","id":"x"}}"#);
            let msg: InvalidationMessage = serde_json::from_str(&json).unwrap();
            assert_eq!(msg.resource_type, rt);
        }
    }

    #[test]
    fn parse_all_actions() {
        for action in ["create", "update", "delete", "assign"] {
            let json =
                format!(r#"{{"type":"model","action":"{action}","tenant_id":"t","id":"x"}}"#);
            let msg: InvalidationMessage = serde_json::from_str(&json).unwrap();
            assert_eq!(msg.action, action);
        }
    }

    #[test]
    fn invalidation_channel_constant() {
        assert_eq!(INVALIDATION_CHANNEL, "brutor:cache:invalidate");
    }

    #[test]
    fn roundtrip_invalidation_message() {
        let original = InvalidationMessage {
            resource_type: "skill".into(),
            action: "update".into(),
            tenant_id: "tenant-abc".into(),
            id: "skill-123".into(),
        };
        let json = serde_json::to_string(&original).unwrap();
        let parsed: InvalidationMessage = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed.resource_type, original.resource_type);
        assert_eq!(parsed.action, original.action);
        assert_eq!(parsed.tenant_id, original.tenant_id);
        assert_eq!(parsed.id, original.id);
    }

    #[test]
    fn invalid_json_fails_to_parse() {
        let result = serde_json::from_str::<InvalidationMessage>("not json");
        assert!(result.is_err());
    }

    #[test]
    fn missing_fields_fails_to_parse() {
        let json = r#"{"type":"model"}"#;
        let result = serde_json::from_str::<InvalidationMessage>(json);
        assert!(result.is_err());
    }

    #[test]
    fn parse_semantic_cache_type() {
        let json = r#"{"type":"semantic_cache","action":"delete","tenant_id":"t1","id":"grp-01"}"#;
        let msg: InvalidationMessage = serde_json::from_str(json).unwrap();
        assert_eq!(msg.resource_type, "semantic_cache");
        assert_eq!(msg.action, "delete");
    }

    #[test]
    fn parse_server_config_type() {
        let json = r#"{"type":"server_config","action":"update","tenant_id":"t1","id":"sc-01"}"#;
        let msg: InvalidationMessage = serde_json::from_str(json).unwrap();
        assert_eq!(msg.resource_type, "server_config");
    }

    #[test]
    fn parse_membership_type() {
        let json = r#"{"type":"membership","action":"update","tenant_id":"t1","id":"grp-01"}"#;
        let msg: InvalidationMessage = serde_json::from_str(json).unwrap();
        assert_eq!(msg.resource_type, "membership");
    }

    #[test]
    fn parse_assign_type() {
        let json = r#"{"type":"assign","action":"assign","tenant_id":"t1","id":"llm-01"}"#;
        let msg: InvalidationMessage = serde_json::from_str(json).unwrap();
        assert_eq!(msg.resource_type, "assign");
    }

    #[test]
    fn all_proxy_cached_resource_types_parseable() {
        // Every resource type that the proxy caches must parse correctly.
        // If a new type is added to the cache invalidation system, add it here.
        for rt in [
            "model",
            "routing_group",
            "resource_group",
            "skill",
            "agent_skill",
            "guardrail",
            "mcp_server",
            "server_config",
            "membership",
            "assign",
            "semantic_cache",
            "knowledge_base",
            "ai_system_dependency",
        ] {
            let json = format!(r#"{{"type":"{rt}","action":"update","tenant_id":"t","id":"x"}}"#);
            let msg: InvalidationMessage = serde_json::from_str(&json).unwrap();
            assert_eq!(msg.resource_type, rt, "Failed to parse resource type: {rt}");
        }
    }

    #[test]
    fn invalidation_message_type_field_uses_serde_rename() {
        // The JSON field is "type" but the Rust field is "resource_type"
        // This test ensures the serde rename attribute works correctly
        let msg = InvalidationMessage {
            resource_type: "model".into(),
            action: "create".into(),
            tenant_id: "t".into(),
            id: "x".into(),
        };
        let json = serde_json::to_string(&msg).unwrap();
        assert!(
            json.contains("\"type\":\"model\""),
            "JSON should use 'type' not 'resource_type'"
        );
        assert!(
            !json.contains("resource_type"),
            "JSON should NOT contain 'resource_type'"
        );
    }

    // -----------------------------------------------------------------------
    // CacheInvalidator::handle_invalidation routing tests.
    //
    // These are the most important tests in the cache layer: they
    // prove that pub/sub messages from the control plane evict the
    // RIGHT caches (and only those caches), respect tenant
    // boundaries, and trigger the documented cross-cache cascades
    // (e.g., a model change also drops the materialised group view).
    //
    // Strategy:
    //   1. Build a `CacheInvalidator` with real cache instances backed
    //      by a never-connecting lazy PgPool.
    //   2. Pre-populate every cache with entries for two tenants.
    //   3. Dispatch one `InvalidationMessage`.
    //   4. Assert exactly which entries were evicted and which survived.
    //
    // If a future change breaks the dispatcher routing, one of these
    // tests fails with a clear "cache X for tenant Y was/wasn't
    // evicted as expected" message — vastly better than discovering
    // the breakage in production via stale data complaints.
    // -----------------------------------------------------------------------

    use super::invalidation_rig::{
        self, BROAD_EVICTION, DISPATCH_TABLE, RECORDED_HANDLERS, Rig, described, gone,
        guardrails_contains,
    };
    use crate::services::guardrails::GuardrailsCache;

    /// A Community gateway's state: nothing installed on it. (The rig of
    /// these tests do not install the prompt cache, the response cache or
    /// the knowledge base. Those are the Enterprise
    /// Edition's; what their handlers evict is tested in its workspace, on
    /// the same rig: `enterprise/src/invalidation_tests.rs`.)
    fn community_state() -> crate::services::SharedState {
        std::sync::Arc::new(invalidation_rig::bare_state())
    }

    /// Construct a fully-wired CacheInvalidator backed by a never-
    /// connecting lazy pool, consulting the built-in handlers. Every
    /// sub-cache is freshly built and independently populated by the test —
    /// the invalidator and the test share the same Clone-handles to each
    /// cache (this is the same sharing pattern the builder uses with
    /// `state.X.clone()`).
    fn build_invalidator() -> (
        CacheInvalidator,
        ModelCache,
        ResourceGroupCache,
        McpServerCache,
        AgentSkillCache,
        std::sync::Arc<ProviderCache>,
        AgentCardCache,
        GuardrailsCache,
    ) {
        let (encryption, guardrails_encryption) = invalidation_rig::test_keys();
        let rig = invalidation_rig::build_invalidator(
            encryption,
            guardrails_encryption,
            community_state(),
            InvalidationRegistry::builtin(),
        );
        (
            rig.inv,
            rig.model,
            rig.group,
            rig.mcp,
            rig.skill,
            rig.provider,
            rig.card,
            rig.guardrails,
        )
    }

    /// Insert one entry of each kind for two tenants. After this
    /// call the caches contain exactly:
    ///   tenant-a: model gpt-5, provider openai, group grp-1, mcp m1,
    ///             skill s1, agent_card card-1
    ///   tenant-b: model gpt-5, provider openai, group grp-1, mcp m1,
    ///             skill s1, agent_card card-1
    /// plus tenant-a / tenant-b guardrails entries.
    async fn populate_two_tenants(
        model_cache: &ModelCache,
        group_cache: &ResourceGroupCache,
        mcp_cache: &McpServerCache,
        skill_cache: &AgentSkillCache,
        provider_cache: &std::sync::Arc<ProviderCache>,
        agent_card_cache: &AgentCardCache,
        guardrails_cache: &GuardrailsCache,
    ) {
        for tenant in ["tenant-a", "tenant-b"] {
            model_cache.insert_model_for_test(tenant, "gpt-5").await;
            provider_cache
                .insert_provider_for_test(tenant, "openai")
                .await;
            mcp_cache.insert_server_for_test(tenant, "m1").await;
            skill_cache.insert_skill_for_test(tenant, "s1").await;
            agent_card_cache
                .insert_card_for_test(tenant, "card-1", "billing")
                .await;
            // GuardrailsCache uses internal HashMap — populate via
            // its private `cache` field (we're in the same module).
            let mut inner = guardrails_cache.cache.write().await;
            inner.insert(
                format!("{tenant}:__all__"),
                crate::services::guardrails::CacheEntry {
                    configs: vec![],
                    loaded_at: std::time::Instant::now(),
                },
            );
        }
        // Also seed group_cache via its private API.
        // (The same-module rule applies; we have access to the
        // inner moka cache and the EffectiveGroupData synthesiser
        // through the cfg-test impl block.)
        for tenant in ["tenant-a", "tenant-b"] {
            group_cache.insert_group_for_test(tenant, "grp-1").await;
        }
    }

    // -----------------------------------------------------------------------
    // model / routing_group → ModelCache(entry) + ResourceGroupCache(tenant)
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn dispatcher_model_evicts_model_entry_and_group_tenant() {
        let (inv, model_cache, group_cache, _mcp, _skill, _provider, _agent_card, _guardrails) =
            build_invalidator();
        populate_two_tenants(
            &model_cache,
            &group_cache,
            &_mcp,
            &_skill,
            &_provider,
            &_agent_card,
            &_guardrails,
        )
        .await;

        inv.handle_invalidation(&InvalidationMessage {
            resource_type: "model".into(),
            action: "update".into(),
            tenant_id: "tenant-a".into(),
            id: "gpt-5".into(),
        })
        .await;

        // tenant-a model evicted, group view also dropped (cascade)
        assert!(model_cache._peek_model("tenant-a", "gpt-5").await.is_none());
        assert!(group_cache._peek_group("tenant-a", "grp-1").await.is_none());
        // tenant-b untouched
        assert!(model_cache._peek_model("tenant-b", "gpt-5").await.is_some());
        assert!(group_cache._peek_group("tenant-b", "grp-1").await.is_some());
    }

    #[tokio::test]
    async fn dispatcher_routing_group_routes_same_as_model() {
        let (inv, model_cache, group_cache, _mcp, _skill, _provider, _agent_card, _guardrails) =
            build_invalidator();
        populate_two_tenants(
            &model_cache,
            &group_cache,
            &_mcp,
            &_skill,
            &_provider,
            &_agent_card,
            &_guardrails,
        )
        .await;

        inv.handle_invalidation(&InvalidationMessage {
            resource_type: "routing_group".into(),
            action: "update".into(),
            tenant_id: "tenant-a".into(),
            id: "rg-fallback".into(),
        })
        .await;

        // Same routing as `model`: per-id eviction on model_cache
        // (rg-fallback was never inserted, so this just verifies
        // the group-tenant cascade fires).
        assert!(group_cache._peek_group("tenant-a", "grp-1").await.is_none());
        assert!(group_cache._peek_group("tenant-b", "grp-1").await.is_some());
    }

    // -----------------------------------------------------------------------
    // llm_provider / provider → ProviderCache(entry) + ModelCache(tenant)
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn dispatcher_provider_evicts_provider_entry_and_model_tenant() {
        let (inv, model_cache, group_cache, _mcp, _skill, provider_cache, _agent_card, _guardrails) =
            build_invalidator();
        populate_two_tenants(
            &model_cache,
            &group_cache,
            &_mcp,
            &_skill,
            &provider_cache,
            &_agent_card,
            &_guardrails,
        )
        .await;

        inv.handle_invalidation(&InvalidationMessage {
            resource_type: "llm_provider".into(),
            action: "update".into(),
            tenant_id: "tenant-a".into(),
            id: "openai".into(),
        })
        .await;

        // tenant-a provider entry evicted
        assert!(provider_cache._peek("tenant-a", "openai").await.is_none());
        // tenant-a model_cache flushed (all model entries carry the
        // provider slug stamped from ProviderCache; if the slug
        // changed, every cached model is potentially stale).
        assert!(model_cache._peek_model("tenant-a", "gpt-5").await.is_none());
        // tenant-b untouched
        assert!(provider_cache._peek("tenant-b", "openai").await.is_some());
        assert!(model_cache._peek_model("tenant-b", "gpt-5").await.is_some());
    }

    // -----------------------------------------------------------------------
    // mcp_server / server_config → McpServerCache(entry) + ResourceGroupCache(tenant)
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn dispatcher_mcp_server_evicts_mcp_entry_and_group_tenant() {
        let (inv, model_cache, group_cache, mcp_cache, _skill, _provider, _agent_card, _guardrails) =
            build_invalidator();
        populate_two_tenants(
            &model_cache,
            &group_cache,
            &mcp_cache,
            &_skill,
            &_provider,
            &_agent_card,
            &_guardrails,
        )
        .await;

        inv.handle_invalidation(&InvalidationMessage {
            resource_type: "mcp_server".into(),
            action: "update".into(),
            tenant_id: "tenant-a".into(),
            id: "m1".into(),
        })
        .await;

        assert!(mcp_cache._peek_server("tenant-a", "m1").await.is_none());
        assert!(group_cache._peek_group("tenant-a", "grp-1").await.is_none());
        // tenant-b untouched
        assert!(mcp_cache._peek_server("tenant-b", "m1").await.is_some());
        assert!(group_cache._peek_group("tenant-b", "grp-1").await.is_some());
    }

    // -----------------------------------------------------------------------
    // skill / agent_skill → AgentSkillCache(entry) + ResourceGroupCache(tenant)
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn dispatcher_skill_evicts_skill_entry_and_group_tenant() {
        let (inv, model_cache, group_cache, _mcp, skill_cache, _provider, _agent_card, _guardrails) =
            build_invalidator();
        populate_two_tenants(
            &model_cache,
            &group_cache,
            &_mcp,
            &skill_cache,
            &_provider,
            &_agent_card,
            &_guardrails,
        )
        .await;

        inv.handle_invalidation(&InvalidationMessage {
            resource_type: "agent_skill".into(),
            action: "update".into(),
            tenant_id: "tenant-a".into(),
            id: "s1".into(),
        })
        .await;

        assert!(skill_cache._peek("tenant-a", "s1").await.is_none());
        assert!(group_cache._peek_group("tenant-a", "grp-1").await.is_none());
        assert!(skill_cache._peek("tenant-b", "s1").await.is_some());
        assert!(group_cache._peek_group("tenant-b", "grp-1").await.is_some());
    }

    // -----------------------------------------------------------------------
    // resource_group → ResourceGroupCache(tenant) — incl. user_groups
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn dispatcher_resource_group_evicts_tenant_group_entries() {
        let (inv, model_cache, group_cache, _mcp, _skill, _provider, _agent_card, _guardrails) =
            build_invalidator();
        populate_two_tenants(
            &model_cache,
            &group_cache,
            &_mcp,
            &_skill,
            &_provider,
            &_agent_card,
            &_guardrails,
        )
        .await;
        // A second group on tenant-a. A resource_group change — direct
        // membership, EndUserGroup bindings/membership, limits — can
        // alter the user→groups mapping, so the whole tenant group view
        // (incl. user_groups) is dropped, not just the changed entry.
        group_cache.insert_group_for_test("tenant-a", "grp-2").await;

        inv.handle_invalidation(&InvalidationMessage {
            resource_type: "resource_group".into(),
            action: "update".into(),
            tenant_id: "tenant-a".into(),
            id: "grp-1".into(),
        })
        .await;

        // Whole tenant-a group view dropped (grp-1 + sibling grp-2).
        assert!(group_cache._peek_group("tenant-a", "grp-1").await.is_none());
        assert!(
            group_cache._peek_group("tenant-a", "grp-2").await.is_none(),
            "a resource_group change drops the tenant's whole group view"
        );
        // tenant-b untouched — invalidation is tenant-scoped.
        assert!(group_cache._peek_group("tenant-b", "grp-1").await.is_some());
        // Other caches not touched.
        assert!(model_cache._peek_model("tenant-a", "gpt-5").await.is_some());
    }

    /// The accepted-caller gate reads two RG-derived facts
    /// from this cache: the service's own row (`enforce_accepted_callers`,
    /// `intended_client_systems`, in the effective group) and the tenant's
    /// card → service map (from declared edges). Both must drop on a
    /// `resource_group` change AND on an `ai_system_dependency` change, or
    /// the gate keeps enforcing yesterday's composition until the TTL.
    #[tokio::test]
    async fn dispatcher_drops_the_accepted_caller_config() {
        for rt in ["resource_group", "ai_system_dependency"] {
            let (inv, _m, group_cache, _mcp, _skill, _p, _card, _g) = build_invalidator();
            for tenant in ["tenant-a", "tenant-b"] {
                group_cache.insert_group_for_test(tenant, "svc").await;
                group_cache
                    .insert_card_services_for_test(tenant, "card-svc", "svc")
                    .await;
            }
            inv.handle_invalidation(&InvalidationMessage {
                resource_type: rt.into(),
                action: "update".into(),
                tenant_id: "tenant-a".into(),
                id: "dep-1".into(),
            })
            .await;
            assert!(
                group_cache._peek_card_services("tenant-a").await.is_none(),
                "{rt}: the card → service map must drop"
            );
            assert!(
                group_cache._peek_group("tenant-a", "svc").await.is_none(),
                "{rt}: the service's own row (enforcement flag, accepted callers) must drop"
            );
            assert!(
                group_cache._peek_card_services("tenant-b").await.is_some(),
                "{rt}: invalidation is tenant-scoped"
            );
        }
    }

    // -----------------------------------------------------------------------
    // guardrail / guardrail_config → GuardrailsCache(tenant) + ResourceGroupCache(tenant)
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn dispatcher_guardrail_evicts_guardrails_and_group_tenant() {
        let (inv, model_cache, group_cache, _mcp, _skill, _provider, _agent_card, guardrails_cache) =
            build_invalidator();
        populate_two_tenants(
            &model_cache,
            &group_cache,
            &_mcp,
            &_skill,
            &_provider,
            &_agent_card,
            &guardrails_cache,
        )
        .await;

        inv.handle_invalidation(&InvalidationMessage {
            resource_type: "guardrail".into(),
            action: "update".into(),
            tenant_id: "tenant-a".into(),
            id: "any-rule".into(),
        })
        .await;

        // tenant-a guardrails + group view dropped
        assert!(!guardrails_contains(&guardrails_cache, "tenant-a:__all__").await);
        assert!(group_cache._peek_group("tenant-a", "grp-1").await.is_none());
        // tenant-b untouched
        assert!(guardrails_contains(&guardrails_cache, "tenant-b:__all__").await);
        assert!(group_cache._peek_group("tenant-b", "grp-1").await.is_some());
    }

    #[tokio::test]
    async fn dispatcher_guardrail_config_alias_routes_same() {
        // The dispatcher accepts both "guardrail" and "guardrail_config"
        // — the gateway/control plane has used both names historically.
        let (inv, model_cache, group_cache, _mcp, _skill, _provider, _agent_card, guardrails_cache) =
            build_invalidator();
        populate_two_tenants(
            &model_cache,
            &group_cache,
            &_mcp,
            &_skill,
            &_provider,
            &_agent_card,
            &guardrails_cache,
        )
        .await;

        inv.handle_invalidation(&InvalidationMessage {
            resource_type: "guardrail_config".into(),
            action: "update".into(),
            tenant_id: "tenant-a".into(),
            id: "any-rule".into(),
        })
        .await;

        assert!(!guardrails_contains(&guardrails_cache, "tenant-a:__all__").await);
        assert!(guardrails_contains(&guardrails_cache, "tenant-b:__all__").await);
    }

    // -----------------------------------------------------------------------
    // agent_card → AgentCardCache(entry) + ResourceGroupCache(tenant)
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn dispatcher_agent_card_evicts_card_entry_and_group_tenant() {
        let (inv, model_cache, group_cache, _mcp, _skill, _provider, agent_card_cache, _guardrails) =
            build_invalidator();
        populate_two_tenants(
            &model_cache,
            &group_cache,
            &_mcp,
            &_skill,
            &_provider,
            &agent_card_cache,
            &_guardrails,
        )
        .await;

        inv.handle_invalidation(&InvalidationMessage {
            resource_type: "agent_card".into(),
            action: "update".into(),
            tenant_id: "tenant-a".into(),
            id: "card-1".into(),
        })
        .await;

        // tenant-a card evicted from BOTH indexes (id + name)
        assert!(
            agent_card_cache
                ._peek_by_id("tenant-a", "card-1")
                .await
                .is_none()
        );
        assert!(
            agent_card_cache
                ._peek_by_name("tenant-a", "billing")
                .await
                .is_none()
        );
        // group view dropped (cascade)
        assert!(group_cache._peek_group("tenant-a", "grp-1").await.is_none());
        // tenant-b untouched
        assert!(
            agent_card_cache
                ._peek_by_id("tenant-b", "card-1")
                .await
                .is_some()
        );
    }

    // -----------------------------------------------------------------------
    // membership / assign → all caches for the tenant
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn dispatcher_membership_invalidates_all_caches_for_tenant() {
        let (
            inv,
            model_cache,
            group_cache,
            mcp_cache,
            skill_cache,
            _provider,
            _agent_card,
            _guardrails,
        ) = build_invalidator();
        populate_two_tenants(
            &model_cache,
            &group_cache,
            &mcp_cache,
            &skill_cache,
            &_provider,
            &_agent_card,
            &_guardrails,
        )
        .await;

        inv.handle_invalidation(&InvalidationMessage {
            resource_type: "membership".into(),
            action: "update".into(),
            tenant_id: "tenant-a".into(),
            id: "user-42".into(),
        })
        .await;

        // tenant-a fully cleared across model/mcp/skill/group caches
        assert!(model_cache._peek_model("tenant-a", "gpt-5").await.is_none());
        assert!(mcp_cache._peek_server("tenant-a", "m1").await.is_none());
        assert!(skill_cache._peek("tenant-a", "s1").await.is_none());
        assert!(group_cache._peek_group("tenant-a", "grp-1").await.is_none());
        // tenant-b untouched
        assert!(model_cache._peek_model("tenant-b", "gpt-5").await.is_some());
        assert!(mcp_cache._peek_server("tenant-b", "m1").await.is_some());
        assert!(skill_cache._peek("tenant-b", "s1").await.is_some());
        assert!(group_cache._peek_group("tenant-b", "grp-1").await.is_some());
    }

    // -----------------------------------------------------------------------
    // Unknown type → conservative full-tenant invalidation
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn dispatcher_unknown_type_invalidates_all_caches_for_tenant() {
        let (
            inv,
            model_cache,
            group_cache,
            mcp_cache,
            skill_cache,
            _provider,
            agent_card_cache,
            guardrails_cache,
        ) = build_invalidator();
        populate_two_tenants(
            &model_cache,
            &group_cache,
            &mcp_cache,
            &skill_cache,
            &_provider,
            &agent_card_cache,
            &guardrails_cache,
        )
        .await;

        inv.handle_invalidation(&InvalidationMessage {
            resource_type: "something-we-have-not-seen-before".into(),
            action: "update".into(),
            tenant_id: "tenant-a".into(),
            id: "x".into(),
        })
        .await;

        // tenant-a fully cleared
        assert!(model_cache._peek_model("tenant-a", "gpt-5").await.is_none());
        assert!(group_cache._peek_group("tenant-a", "grp-1").await.is_none());
        assert!(mcp_cache._peek_server("tenant-a", "m1").await.is_none());
        assert!(skill_cache._peek("tenant-a", "s1").await.is_none());
        assert!(
            agent_card_cache
                ._peek_by_id("tenant-a", "card-1")
                .await
                .is_none()
        );
        assert!(!guardrails_contains(&guardrails_cache, "tenant-a:__all__").await);
        // tenant-b untouched (this is the critical multi-tenant
        // safety property — even on unknown messages we never bleed
        // across tenants)
        assert!(model_cache._peek_model("tenant-b", "gpt-5").await.is_some());
        assert!(group_cache._peek_group("tenant-b", "grp-1").await.is_some());
        assert!(mcp_cache._peek_server("tenant-b", "m1").await.is_some());
        assert!(skill_cache._peek("tenant-b", "s1").await.is_some());
        assert!(
            agent_card_cache
                ._peek_by_id("tenant-b", "card-1")
                .await
                .is_some()
        );
        assert!(guardrails_contains(&guardrails_cache, "tenant-b:__all__").await);
    }

    // -----------------------------------------------------------------------
    // The smoke-test of the whole pipeline: send several messages
    // across two tenants and verify the steady-state cache contents
    // make sense. This is closest to what production looks like
    // under heavy admin churn.
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn dispatcher_handles_burst_of_mixed_invalidations() {
        let (
            inv,
            model_cache,
            group_cache,
            mcp_cache,
            skill_cache,
            _provider,
            agent_card_cache,
            guardrails_cache,
        ) = build_invalidator();
        populate_two_tenants(
            &model_cache,
            &group_cache,
            &mcp_cache,
            &skill_cache,
            &_provider,
            &agent_card_cache,
            &guardrails_cache,
        )
        .await;

        // Burst: 4 messages, 3 distinct types, 2 tenants
        for msg in [
            InvalidationMessage {
                resource_type: "model".into(),
                action: "update".into(),
                tenant_id: "tenant-a".into(),
                id: "gpt-5".into(),
            },
            InvalidationMessage {
                resource_type: "agent_card".into(),
                action: "delete".into(),
                tenant_id: "tenant-b".into(),
                id: "card-1".into(),
            },
            InvalidationMessage {
                resource_type: "guardrail".into(),
                action: "create".into(),
                tenant_id: "tenant-a".into(),
                id: "rule-x".into(),
            },
            InvalidationMessage {
                resource_type: "skill".into(),
                action: "update".into(),
                tenant_id: "tenant-b".into(),
                id: "s1".into(),
            },
        ] {
            inv.handle_invalidation(&msg).await;
        }

        // Final state assertions:
        // tenant-a: model gone, guardrails gone, group view gone (twice, idempotent)
        // tenant-b: agent_card gone, skill gone, group view gone
        assert!(model_cache._peek_model("tenant-a", "gpt-5").await.is_none());
        assert!(!guardrails_contains(&guardrails_cache, "tenant-a:__all__").await);
        assert!(group_cache._peek_group("tenant-a", "grp-1").await.is_none());

        assert!(
            agent_card_cache
                ._peek_by_id("tenant-b", "card-1")
                .await
                .is_none()
        );
        assert!(skill_cache._peek("tenant-b", "s1").await.is_none());
        assert!(group_cache._peek_group("tenant-b", "grp-1").await.is_none());

        // The MCP cache was not directly invalidated for either
        // tenant; both tenant entries should survive.
        assert!(mcp_cache._peek_server("tenant-a", "m1").await.is_some());
        assert!(mcp_cache._peek_server("tenant-b", "m1").await.is_some());
    }

    // -----------------------------------------------------------------------
    // The whole dispatch as one table.
    //
    // The tests above pin the arms one at a time and leave half of them out.
    // This one sends every resource type the dispatcher names, and one it
    // does not, at caches seeded for two tenants, and records what each
    // message evicted. It was written against the `match` and passed before
    // the arms became registered handlers; the table is the statement that
    // nothing moved.
    //
    // Each type is sent twice: with the id of the seeded entries (`x1`) and
    // with another id. An eviction in both columns is by tenant; one in the
    // first column only is by entry.
    // -----------------------------------------------------------------------

    /// One entry of every kind under the id `x1`, for `tenant-a` and for
    /// `tenant-b`, on a Community gateway's invalidator.
    async fn seeded_rig() -> Rig {
        seeded_rig_with(&[]).await
    }

    /// `seeded_rig`, with handlers registered after the built-in ones.
    async fn seeded_rig_with(extra: &[InvalidationHandler]) -> Rig {
        invalidation_rig::seeded_rig(community_state(), InvalidationRegistry::builtin(), extra)
            .await
    }

    /// What one message for `tenant-a` evicted, on this rig. Panics if
    /// anything of `tenant-b` is gone: no message crosses tenants.
    async fn evicted_on(rig: &Rig, resource_type: &str, id: &str) -> String {
        invalidation_rig::evicted_on(rig, resource_type, id)
            .await
            .join(" ")
    }

    async fn evicted_by(resource_type: &str, id: &str) -> String {
        evicted_on(&seeded_rig().await, resource_type, id).await
    }

    /// `evicted_by` for many messages, each on a rig of its own, at the same
    /// time. Answers are in the order the messages were given.
    async fn evicted_by_each(messages: Vec<(&'static str, &'static str)>) -> Vec<String> {
        let mut rows = tokio::task::JoinSet::new();
        let mut out = vec![String::new(); messages.len()];
        for (i, (resource_type, id)) in messages.into_iter().enumerate() {
            rows.spawn(async move { (i, evicted_by(resource_type, id).await) });
        }
        while let Some(row) = rows.join_next().await {
            match row {
                Ok((i, evicted)) => out[i] = evicted,
                // A row's own assertion failed: fail with its message.
                Err(e) => std::panic::resume_unwind(e.into_panic()),
            }
        }
        out
    }

    // The table is `invalidation_rig::DISPATCH_TABLE`: every type a built-in
    // handler answers, and one nobody registered.

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn dispatch_table_every_type_evicts_what_it_did() {
        let hit = evicted_by_each(DISPATCH_TABLE.iter().map(|row| (row.0, "x1")).collect()).await;
        let miss = evicted_by_each(
            DISPATCH_TABLE
                .iter()
                .map(|row| (row.0, "another-id"))
                .collect(),
        )
        .await;
        let observed: Vec<(String, String, String)> = DISPATCH_TABLE
            .iter()
            .zip(hit.into_iter().zip(miss))
            .map(|(row, (hit, miss))| (row.0.to_string(), hit, miss))
            .collect();
        let expected: Vec<(String, String, String)> = DISPATCH_TABLE
            .iter()
            .map(|(t, hit, miss)| (t.to_string(), hit.to_string(), miss.to_string()))
            .collect();
        assert_eq!(observed, expected);
    }

    /// A type that is nearly one the dispatcher names is not that type: it
    /// gets the broad eviction, not a narrower one and not nothing.
    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn dispatch_a_near_miss_of_a_type_gets_the_broad_eviction() {
        let near_misses = [
            "Model",
            "MODEL",
            "model ",
            " model",
            "models",
            "guardrail_configs",
            "prompt_cache",
            "",
        ];
        let evicted = evicted_by_each(near_misses.iter().map(|t| (*t, "x1")).collect()).await;
        for (resource_type, evicted) in near_misses.iter().zip(evicted) {
            assert_eq!(evicted, BROAD_EVICTION, "{resource_type:?}");
        }
    }

    /// The resource types of the Enterprise Edition's handlers are types
    /// nobody registered in a Community gateway: a message of one of them
    /// (a control plane of the other edition, a leftover publisher) gets the
    /// broad eviction, never nothing. What the handlers themselves evict is
    /// the Enterprise workspace's table.
    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn dispatch_an_enterprise_type_gets_the_broad_eviction_in_a_community_gateway() {
        let types = [
            "knowledge_base",
            "semantic_cache",
            "prompt_cache_policy",
            "prompt_cache_binding",
            "router_cache_config",
        ];
        for id in ["x1", "another-id"] {
            let evicted = evicted_by_each(types.iter().map(|t| (*t, id)).collect()).await;
            for (resource_type, evicted) in types.iter().zip(evicted) {
                assert_eq!(evicted, BROAD_EVICTION, "{resource_type:?} with {id}");
            }
        }
    }

    /// After a (re)subscribe everything this listener keeps fresh is dropped,
    /// for every tenant. (The prompt-cache policies an Enterprise gateway
    /// also drops, and the compiled patterns it keeps, are asserted in its
    /// workspace.)
    #[tokio::test]
    async fn dispatch_reconnect_flush_drops_every_cache_for_every_tenant() {
        let rig = seeded_rig().await;
        rig.inv.flush_config_caches().await;
        let everything = "model provider group mcp skill card guardrails argument_policy";
        assert_eq!(gone(&rig, "tenant-a").await.join(" "), everything);
        assert_eq!(gone(&rig, "tenant-b").await.join(" "), everything);
    }

    // -----------------------------------------------------------------------
    // The registry: what is registered, and what another
    // distribution can and cannot do to it.
    // -----------------------------------------------------------------------

    #[test]
    fn registry_builtin_handlers_are_the_recorded_ones() {
        assert_eq!(
            described(&InvalidationRegistry::builtin()),
            RECORDED_HANDLERS
        );
        // No built-in handler belongs to an Enterprise feature.
        // (The full composition's seventeen — these and the extensions' —
        // are recorded in the Enterprise workspace.)
        assert!(RECORDED_HANDLERS.iter().all(|row| row.contains(": -: ")));
        assert_eq!(RECORDED_HANDLERS.len(), 11);
    }

    /// `builtin()` copies the list without the checks `register` makes, so
    /// they are made here: unique names, and resource types that are there.
    #[test]
    fn registry_builtin_handlers_would_all_register() {
        let mut names = std::collections::BTreeSet::new();
        for handler in BUILTIN_INVALIDATION_HANDLERS {
            assert!(!handler.name.is_empty());
            assert!(names.insert(handler.name), "{} twice", handler.name);
            assert!(!handler.resource_types.is_empty(), "{}", handler.name);
            assert!(
                handler.resource_types.iter().all(|t| !t.is_empty()),
                "{}",
                handler.name
            );
        }
    }

    /// The table above and the registry name the same resource types, so a
    /// handler for a new type cannot be added without a row saying what it
    /// evicts.
    #[test]
    fn registry_every_registered_type_has_a_row_in_the_dispatch_table() {
        let full = InvalidationRegistry::builtin();
        let registered: std::collections::BTreeSet<&str> = full
            .handlers()
            .iter()
            .flat_map(|h| h.resource_types.iter().copied())
            .collect();
        let rows: std::collections::BTreeSet<&str> = DISPATCH_TABLE
            .iter()
            .map(|row| row.0)
            .filter(|t| *t != "a_type_nobody_registered")
            .collect();
        assert_eq!(registered, rows);
        assert!(
            InvalidationRegistry::builtin()
                .handlers_for("a_type_nobody_registered")
                .next()
                .is_none()
        );
    }

    /// An extension's handler: drops the tenant's skills, which no built-in
    /// handler does on its own, so that it having run can be seen.
    fn ext_drops_skills<'a>(
        inv: &'a CacheInvalidator,
        msg: &'a InvalidationMessage,
    ) -> invalidation::HandlerFuture<'a> {
        Box::pin(async move {
            inv.skill_cache.invalidate_tenant(&msg.tenant_id).await;
        })
    }

    /// How often the extension's flush below ran. One test reads it, and
    /// nextest runs each test in a process of its own.
    static EXT_FLUSHES: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

    /// An extension's flush: counts, so that it having run can be seen (the
    /// built-in flush drops every seeded entry already).
    fn ext_flush<'a>(_inv: &'a CacheInvalidator) -> invalidation::HandlerFuture<'a> {
        Box::pin(async move {
            EXT_FLUSHES.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        })
    }

    const EXT_NEW_TYPE: InvalidationHandler = InvalidationHandler {
        name: "ext_report",
        resource_types: &["ext_report"],
        feature: None,
        handle: ext_drops_skills,
        flush: Some(ext_flush),
    };

    const EXT_ON_MODEL: InvalidationHandler = InvalidationHandler {
        name: "ext_on_model",
        resource_types: &["model"],
        feature: None,
        handle: ext_drops_skills,
        flush: None,
    };

    #[tokio::test]
    async fn registry_an_extensions_type_is_answered_by_its_handler_alone() {
        let rig = seeded_rig_with(&[EXT_NEW_TYPE]).await;
        assert_eq!(evicted_on(&rig, "ext_report", "x1").await, "skill");
        // Without the registration the same message is a type nobody knows.
        assert_eq!(evicted_by("ext_report", "x1").await, BROAD_EVICTION);
    }

    #[tokio::test]
    async fn registry_a_handler_added_to_a_builtin_type_runs_after_the_builtin() {
        let rig = seeded_rig_with(&[EXT_ON_MODEL]).await;
        assert_eq!(evicted_on(&rig, "model", "x1").await, "model group skill");
        // The built-in handler's other type is not the extension's.
        let rig = seeded_rig_with(&[EXT_ON_MODEL]).await;
        assert_eq!(evicted_on(&rig, "routing_group", "x1").await, "model group");

        let order: Vec<&str> = rig
            .inv
            .handlers
            .handlers_for("model")
            .map(|h| h.name)
            .collect();
        assert_eq!(order, ["model_or_routing_group", "ext_on_model"]);
    }

    #[tokio::test]
    async fn registry_an_extensions_flush_runs_on_reconnect() {
        // A handler's flush runs when it is registered, and only then.
        let without = seeded_rig().await;
        let before = EXT_FLUSHES.load(std::sync::atomic::Ordering::SeqCst);
        without.inv.flush_config_caches().await;
        assert_eq!(
            EXT_FLUSHES.load(std::sync::atomic::Ordering::SeqCst),
            before
        );

        let rig = seeded_rig_with(&[EXT_NEW_TYPE]).await;
        rig.inv.flush_config_caches().await;
        assert_eq!(
            EXT_FLUSHES.load(std::sync::atomic::Ordering::SeqCst),
            before + 1
        );
        // The built-in flush is still whole.
        assert_eq!(
            gone(&rig, "tenant-b").await.join(" "),
            "model provider group mcp skill card guardrails argument_policy"
        );
    }

    #[test]
    fn registry_registration_refuses_what_would_hide_or_do_nothing() {
        let mut registry = InvalidationRegistry::builtin();
        let before = described(&registry);
        let refused = [
            (
                InvalidationHandler {
                    name: "guardrail",
                    ..EXT_NEW_TYPE
                },
                InvalidationRegistryError::Name("guardrail".into()),
            ),
            (
                InvalidationHandler {
                    name: "",
                    ..EXT_NEW_TYPE
                },
                InvalidationRegistryError::Name(String::new()),
            ),
            (
                InvalidationHandler {
                    resource_types: &[],
                    ..EXT_NEW_TYPE
                },
                InvalidationRegistryError::ResourceTypes("ext_report".into()),
            ),
            (
                InvalidationHandler {
                    resource_types: &["ext_report", ""],
                    ..EXT_NEW_TYPE
                },
                InvalidationRegistryError::ResourceTypes("ext_report".into()),
            ),
        ];
        for (handler, why) in refused {
            assert_eq!(registry.register(handler), Err(why));
            assert_eq!(described(&registry), before);
        }

        assert_eq!(registry.register(EXT_NEW_TYPE), Ok(()));
        // Its name is now taken as well.
        assert_eq!(
            registry.register(EXT_NEW_TYPE),
            Err(InvalidationRegistryError::Name("ext_report".into()))
        );
        assert_eq!(registry.handlers().len(), before.len() + 1);
    }
}
