// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Model Cache
//!
//! In-memory cache for LLM models and routing groups. Eliminates 2-3 DB queries
//! per request from the hot path. Cache misses load from PostgreSQL and populate
//! the cache automatically.

use std::sync::Arc;
use std::time::Duration;

use moka::future::Cache;
use sqlx::PgPool;

use crate::models::db::{LlmModel, LlmRoutingGroup};
use crate::services::encryption::EncryptionService;

use super::provider_cache::ProviderCache;
use super::{Generation, ResourceCache, insert_fresh};

/// Default TTL for cached models (safety net — primary invalidation is push-based).
const DEFAULT_TTL_SECS: u64 = 300; // 5 minutes
/// Maximum number of cached model entries.
const DEFAULT_MAX_MODELS: u64 = 10_000;
/// Maximum number of cached routing group entries.
const DEFAULT_MAX_ROUTING_GROUPS: u64 = 1_000;

// ---------------------------------------------------------------------------
// ModelCache
// ---------------------------------------------------------------------------

/// In-memory cache for LLM models and routing groups.
///
/// Caches models by multiple keys (id, model_name, display name) for fast
/// lookup regardless of how the client identifies the model. Routing groups
/// and their member model lists are cached separately.
///
/// `Clone` shares the underlying moka cache instances (moka caches are
/// internally `Arc`-shared, so cloning is cheap and produces handles
/// pointing to the same data). This is what lets `main.rs` hand the
/// cache invalidator a shared instance without rebuilding it — the
/// alternative (calling `ModelCache::new` again) silently produces a
/// throwaway cache that pub/sub invalidations evict instead of the
/// live one used by request handlers.
#[derive(Clone)]
pub struct ModelCache {
    /// Models by `{tenant_id}:{lookup_key}` where lookup_key is id, model_name, or name
    models: Cache<String, Arc<LlmModel>>,
    /// Routing groups by `{tenant_id}:{name}`
    routing_groups: Cache<String, Arc<LlmRoutingGroup>>,
    db: PgPool,
    /// Bumped by every invalidation; loads insert with [`insert_fresh`].
    generation: Generation,
    encryption: EncryptionService,
    /// Looked up at load time to populate `LlmModel.provider` (the
    /// adapter dispatch slug) without a JOIN. Shared `Arc` because
    /// `AppState` and the cache invalidator both hold it.
    provider_cache: Arc<ProviderCache>,
}

impl ModelCache {
    pub fn new(
        db: PgPool,
        encryption: EncryptionService,
        provider_cache: Arc<ProviderCache>,
    ) -> Self {
        // CRITICAL: every moka cache that uses `invalidate_entries_if`
        // must call `support_invalidation_closures()` on the builder.
        // Without this flag the predicate-based eviction silently
        // returns `Err(InvalidationClosuresDisabled)` — the existing
        // `let _ = self.x.invalidate_entries_if(...)` pattern swallows
        // the error, so tenant-wide invalidations would no-op and
        // stale data would survive until TTL. Tenant isolation tests
        // catch this regression.
        Self {
            models: Cache::builder()
                .max_capacity(DEFAULT_MAX_MODELS)
                .time_to_live(Duration::from_secs(DEFAULT_TTL_SECS))
                .support_invalidation_closures()
                .build(),
            routing_groups: Cache::builder()
                .max_capacity(DEFAULT_MAX_ROUTING_GROUPS)
                .time_to_live(Duration::from_secs(DEFAULT_TTL_SECS))
                .support_invalidation_closures()
                .build(),
            db,
            generation: Generation::default(),
            encryption,
            provider_cache,
        }
    }

    /// Stamp `model.provider` from the provider cache, looked up by
    /// `model.provider_id`. Called by every loader. Returns whether
    /// resolution succeeded — callers may want to log / drop the
    /// model if the slug is unresolvable, since adapter dispatch
    /// will fail downstream regardless.
    async fn stamp_provider_slug(&self, model: &mut LlmModel) -> bool {
        match self
            .provider_cache
            .slug(&model.tenant_id, &model.provider_id)
            .await
        {
            Some(slug) => {
                model.provider = slug;
                true
            }
            None => {
                tracing::warn!(
                    tenant_id = %model.tenant_id,
                    model_id = %model.id,
                    provider_id = %model.provider_id,
                    "Could not resolve provider slug for model — adapter dispatch will fail",
                );
                model.provider = String::new();
                false
            }
        }
    }

    /// Look up a model by name or ID (cached). Returns None on miss + DB miss.
    pub async fn get_model(&self, tenant_id: &str, lookup: &str) -> Option<Arc<LlmModel>> {
        let key = format!("{tenant_id}:{lookup}");

        // Cache hit
        if let Some(model) = self.models.get(&key).await {
            return Some(model);
        }

        // Cache miss — load the bare model row. The slug
        // (`provider`) is stamped from `provider_cache` post-load;
        // see `stamp_provider_slug`. No JOIN needed — the cache is
        // small (a tenant has <40 providers) and amortises across
        // every model lookup for the same tenant.
        let started = self.generation.current();
        let result = sqlx::query_as::<_, LlmModel>(concat!(
            "SELECT * FROM llm_model \
                 WHERE (model_name = $1 OR name = $1 OR id = $1) AND tenant_id = $2 AND ",
            crate::model_available_sql!(""),
            " ORDER BY routing_weight DESC NULLS LAST LIMIT 1"
        ))
        .bind(lookup)
        .bind(tenant_id)
        .fetch_optional(&self.db)
        .await;
        let model = match result {
            Ok(opt) => opt?,
            Err(e) => {
                tracing::error!(
                    tenant_id,
                    lookup,
                    error = %e,
                    "ModelCache::get_model DB lookup failed",
                );
                return None;
            }
        };

        // Stamp the provider slug + decrypt at-rest fields before
        // we wrap in Arc, so the cached entry has everything the
        // request pipeline needs.
        let mut model = model;
        self.stamp_provider_slug(&mut model).await;
        crate::services::llm_router::open_model_credentials(&mut model, &self.encryption);

        let model = Arc::new(model);

        // Insert under all possible lookup keys
        let g = &self.generation;
        let mut keys = vec![
            key,
            format!("{tenant_id}:{}", model.id),
            format!("{tenant_id}:{}", model.model_name),
        ];
        if model.name != model.model_name {
            keys.push(format!("{tenant_id}:{}", model.name));
        }
        for k in keys {
            insert_fresh(&self.models, k, model.clone(), g, started).await;
        }

        Some(model)
    }

    /// Look up a model by explicit ID (cached).
    pub async fn get_model_by_id(&self, tenant_id: &str, model_id: &str) -> Option<Arc<LlmModel>> {
        let key = format!("{tenant_id}:{model_id}");

        if let Some(model) = self.models.get(&key).await {
            return Some(model);
        }

        let started = self.generation.current();
        let result = sqlx::query_as::<_, LlmModel>(concat!(
            "SELECT * FROM llm_model WHERE id = $1 AND tenant_id = $2 AND ",
            crate::model_available_sql!("")
        ))
        .bind(model_id)
        .bind(tenant_id)
        .fetch_optional(&self.db)
        .await;
        let model = match result {
            Ok(opt) => opt?,
            Err(e) => {
                tracing::error!(
                    tenant_id,
                    model_id,
                    error = %e,
                    "ModelCache::get_model_by_id DB lookup failed",
                );
                return None;
            }
        };

        let mut model = model;
        self.stamp_provider_slug(&mut model).await;
        crate::services::llm_router::open_model_credentials(&mut model, &self.encryption);

        let model = Arc::new(model);
        let g = &self.generation;
        insert_fresh(&self.models, key, model.clone(), g, started).await;
        let by_name = format!("{tenant_id}:{}", model.model_name);
        insert_fresh(&self.models, by_name, model.clone(), g, started).await;

        Some(model)
    }

    /// Look up a routing group by name (cached).
    pub async fn get_routing_group(
        &self,
        tenant_id: &str,
        group_name: &str,
    ) -> Result<Option<Arc<LlmRoutingGroup>>, sqlx::Error> {
        let key = format!("{tenant_id}:{group_name}");

        if let Some(group) = self.routing_groups.get(&key).await {
            return Ok(Some(group));
        }

        let started = self.generation.current();
        let group = sqlx::query_as::<_, LlmRoutingGroup>(
            "SELECT * FROM llm_routing_group WHERE name = $1 AND tenant_id = $2 AND enabled = true",
        )
        .bind(group_name)
        .bind(tenant_id)
        .fetch_optional(&self.db)
        .await?;
        // A failed read is returned, never "no such routing group".
        let Some(group) = group else {
            return Ok(None);
        };

        let group = Arc::new(group);
        insert_fresh(
            &self.routing_groups,
            key,
            group.clone(),
            &self.generation,
            started,
        )
        .await;

        Ok(Some(group))
    }

    /// Cache stats for monitoring.
    pub fn stats(&self) -> (u64, u64) {
        (self.models.entry_count(), self.routing_groups.entry_count())
    }
}

#[async_trait::async_trait]
impl ResourceCache for ModelCache {
    fn name(&self) -> &str {
        "model_cache"
    }

    async fn invalidate(&self, tenant_id: &str, resource_id: &str) {
        self.generation.bump();
        // Every cached model is stored under up to THREE keys (see
        // `get_model`): `{tenant}:{id}`, `{tenant}:{model_name}` and
        // `{tenant}:{name}`. The control plane invalidates by id, while
        // request bodies and health probes resolve by model_name, so every
        // key is evicted: the model's identifiers are discovered
        // (cache-first; one DB SELECT when the direct key is not warm) and
        // each lookup key is dropped. (The members of a routing group are
        // not cached: the router reads them per request, with the
        // availability predicate.)
        let direct_key = format!("{tenant_id}:{resource_id}");

        let identifiers: Option<(String, String, String)> =
            if let Some(m) = self.models.get(&direct_key).await {
                Some((m.id.clone(), m.model_name.clone(), m.name.clone()))
            } else {
                match sqlx::query_as::<_, (String, String, String)>(
                    r#"SELECT id, model_name, name
                       FROM llm_model
                       WHERE tenant_id = $1
                         AND (id = $2 OR model_name = $2 OR name = $2)
                       LIMIT 1"#,
                )
                .bind(tenant_id)
                .bind(resource_id)
                .fetch_optional(&self.db)
                .await
                {
                    Ok(opt) => opt,
                    Err(e) => {
                        tracing::warn!(
                            tenant_id,
                            resource_id,
                            error = %e,
                            "ModelCache::invalidate DB fallback failed; \
                             only the direct key will be evicted (other \
                             keys, if any, will TTL out within 5 min)",
                        );
                        None
                    }
                }
            };

        // ── (1) model_cache: evict every lookup key ────────────────
        self.models.invalidate(&direct_key).await;
        if let Some((id, model_name, name)) = identifiers.as_ref() {
            if id.as_str() != resource_id {
                self.models.invalidate(&format!("{tenant_id}:{id}")).await;
            }
            if model_name.as_str() != resource_id {
                self.models
                    .invalidate(&format!("{tenant_id}:{model_name}"))
                    .await;
            }
            if !name.is_empty()
                && name.as_str() != resource_id
                && name.as_str() != model_name.as_str()
            {
                self.models.invalidate(&format!("{tenant_id}:{name}")).await;
            }
        }

        // ── (1b) routing_groups — keyed by NAME, announced by ID.
        //        The control plane publishes an edited
        //        routing group as its id; nothing here evicted this
        //        sub-cache, so an edited or disabled group was served as it
        //        was until the TTL. Evict the tenant's entry whose row has
        //        that id (or that name, for a caller that passes one).
        self.routing_groups.invalidate(&direct_key).await;
        let rg_prefix = format!("{tenant_id}:");
        let rg_id = resource_id.to_string();
        if let Err(e) = self.routing_groups.invalidate_entries_if(move |k, v| {
            k.starts_with(&rg_prefix) && (v.id == rg_id || v.name == rg_id)
        }) {
            // Fail closed: the predicate could not be registered, so drop
            // every routing group rather than keep a stale one.
            tracing::error!(error = %e, "routing-group eviction failed; clearing the sub-cache");
            self.routing_groups.invalidate_all();
        }
        self.routing_groups.run_pending_tasks().await;

        tracing::debug!(
            tenant_id,
            resource_id,
            "ModelCache entry invalidated (all lookup keys)"
        );
    }

    async fn invalidate_tenant(&self, tenant_id: &str) {
        self.generation.bump();
        // moka's `invalidate_entries_if` is LAZY — it registers a
        // predicate that runs during background maintenance. To make
        // pub/sub invalidations strictly consistent (the next `get`
        // is guaranteed to miss), we force a maintenance pass with
        // `run_pending_tasks` after registering each predicate.
        // Without this, an invalidation message can fire and the
        // very next request still sees a stale entry until moka
        // schedules its next maintenance — typically tens of
        // milliseconds, but unbounded in pathological cases. With
        // this, the cost is one maintenance sweep per
        // invalidation (microseconds), and the behaviour is
        // deterministic both in production and under test.
        let prefix = format!("{tenant_id}:");
        let _ = self
            .models
            .invalidate_entries_if(move |k, _| k.starts_with(&prefix));
        let prefix2 = format!("{tenant_id}:");
        let _ = self
            .routing_groups
            .invalidate_entries_if(move |k, _| k.starts_with(&prefix2));
        self.models.run_pending_tasks().await;
        self.routing_groups.run_pending_tasks().await;
        tracing::debug!(tenant_id, "ModelCache tenant invalidated");
    }

    async fn invalidate_all(&self) {
        self.generation.bump();
        self.models.invalidate_all();
        self.routing_groups.invalidate_all();
        tracing::debug!("ModelCache fully invalidated");
    }
}

// ---------------------------------------------------------------------------
// Test-only helpers
// ---------------------------------------------------------------------------

#[cfg(any(test, feature = "db-tests"))]
impl ModelCache {
    /// Direct probe of the model sub-cache by key, used only in tests.
    /// Avoids the `get_model` DB fallback so test assertions reflect
    /// pure cache state.
    pub(super) async fn _peek_model(&self, tenant_id: &str, lookup: &str) -> Option<Arc<LlmModel>> {
        self.models.get(&format!("{tenant_id}:{lookup}")).await
    }

    /// Direct probe of the routing-group sub-cache.
    pub(super) async fn _peek_routing_group(
        &self,
        tenant_id: &str,
        group_name: &str,
    ) -> Option<Arc<LlmRoutingGroup>> {
        self.routing_groups
            .get(&format!("{tenant_id}:{group_name}"))
            .await
    }

    /// Inserts a synthesised `LlmModel` under `(tenant_id, lookup_key)`
    /// without touching the database. Used by both this module's own
    /// tests and the dispatcher tests in `cache/mod.rs` to probe
    /// invalidation routing without needing a live DB.
    pub(super) async fn insert_model_for_test(&self, tenant_id: &str, lookup_key: &str) {
        let key = format!("{tenant_id}:{lookup_key}");
        let model = synth_model_for_test(tenant_id, lookup_key);
        self.models.insert(key, Arc::new(model)).await;
    }

    /// Mirrors what `get_model` does on a cache miss: inserts the model
    /// under all three lookup keys (id, model_name, name). Used by tests
    /// that exercise the multi-key invalidation contract — without this
    /// helper, every test inserts under exactly one key and the
    /// "evict every key" semantics of `invalidate` go uncovered.
    #[cfg(test)]
    pub(super) async fn insert_model_under_all_keys_for_test(
        &self,
        tenant_id: &str,
        id: &str,
        model_name: &str,
        name: &str,
    ) {
        let mut model = synth_model_for_test(tenant_id, id);
        model.id = id.to_string();
        model.model_name = model_name.to_string();
        model.name = name.to_string();
        let model = Arc::new(model);

        self.models
            .insert(format!("{tenant_id}:{id}"), model.clone())
            .await;
        self.models
            .insert(format!("{tenant_id}:{model_name}"), model.clone())
            .await;
        if name != model_name {
            self.models
                .insert(format!("{tenant_id}:{name}"), model.clone())
                .await;
        }
    }

    #[cfg(test)]
    pub(super) async fn insert_routing_group_for_test(&self, tenant_id: &str, group_name: &str) {
        let key = format!("{tenant_id}:{group_name}");
        let rg = synth_routing_group_for_test(tenant_id, group_name);
        self.routing_groups.insert(key, Arc::new(rg)).await;
    }
}

#[cfg(any(test, feature = "db-tests"))]
fn synth_model_for_test(tenant_id: &str, id: &str) -> LlmModel {
    LlmModel {
        id: id.to_string(),
        tenant_id: tenant_id.to_string(),
        name: format!("Synth {id}"),
        description: None,
        usage: None,
        provider_id: "prov-test".into(),
        provider: "openai".into(),
        provider_additional_drop_params: None,
        model_name: id.to_string(),
        mode: None,
        parameters: None,
        tags: None,
        api_url_auto_discovered: None,
        api_url: None,
        auth_type: None,
        api_key: None,
        default_temperature: None,
        default_max_tokens: None,
        system_prompt: None,
        top_p: None,
        n: None,
        stop_sequences: None,
        seed: None,
        presence_penalty: None,
        frequency_penalty: None,
        logit_bias: None,
        response_format: None,
        logprobs: None,
        top_logprobs: None,
        stream_options: None,
        supports_streaming: None,
        supports_function_calling: None,
        supports_vision: None,
        tools: None,
        tool_choice: None,
        parallel_tool_calls: None,
        user_identifier: None,
        api_version: None,
        extra_headers: None,
        model_metadata: None,
        drop_params: None,
        additional_drop_params: None,
        thinking_enabled: None,
        routing_group_id: None,
        routed_via_group_id: None,
        routed_stream_timeout: None,
        routed_via_tier: None,
        routed_tier_signals: None,
        routing_weight: None,
        fallback_models: None,
        rate_limit_rpm: None,
        rate_limit_tpm: None,
        cost_per_input_token: None,
        cost_per_output_token: None,
        cost_per_image: None,
        supported_image_sizes: None,
        supported_image_qualities: None,
        max_images_per_request: None,
        cost_per_audio_char: None,
        supported_voices: None,
        supported_audio_formats: None,
        cost_per_audio_minute: None,
        max_audio_duration_seconds: None,
        supported_input_formats: None,
        supported_response_formats: None,
        cost_per_video_second: None,
        supported_video_resolutions: None,
        supported_video_durations: None,
        max_video_duration_seconds: None,
        timeout: None,
        max_concurrent_requests: None,
        allowed_fails: None,
        cooldown_time: None,
        enabled: true,
        capabilities: None,
        health_status: None,
        last_health_check: None,
        last_health_success: None,
        consecutive_health_failures: None,
        health_check_error: None,
        total_requests: None,
        failed_requests: None,
        total_tokens_consumed: None,
        avg_latency_ms: None,
        supports_batch: None,
        provider_config: None,
        credential_unreadable: false,
        credentials_opened: false,
        created_at: None,
        updated_at: None,
    }
}

#[cfg(test)]
fn synth_routing_group_for_test(tenant_id: &str, name: &str) -> LlmRoutingGroup {
    LlmRoutingGroup {
        id: format!("rg-{name}"),
        tenant_id: tenant_id.to_string(),
        name: name.to_string(),
        description: None,
        routing_strategy: None,
        routing_strategy_args: None,
        complexity_config: None,
        complexity_floor_tier: None,
        cooldown_time: None,
        disable_cooldowns: None,
        fallback_models: None,
        context_window_fallbacks: None,
        content_policy_fallbacks: None,
        default_fallbacks: None,
        max_fallbacks: None,
        required_capabilities: None,
        num_retries: None,
        retry_after: None,
        stream_timeout: None,
        retry_policy: None,
        allowed_fails_policy: None,
        routing_max_retries: None,
        routing_retry_delay: None,
        ttl_for_blocked_models: None,
        max_parallel_requests: None,
        rpm_limit: None,
        tpm_limit: None,
        alerting_enabled: None,
        alerting_threshold: None,
        alerting_webhook_url: None,
        alerting_config: None,
        router_settings: None,
        custom_metadata: None,
        enabled: true,
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------
//
// These tests use a `connect_lazy` PgPool — the pool is never asked
// to open a connection because every test populates the cache via the
// `insert_*_for_test` helpers (which write straight to moka, bypassing
// the DB loaders). This keeps the suite hermetic: no postgres, no
// network, no flakiness.
//
// Coverage matrix (mirrored across every cache module so the proxy's
// runtime memory layer is rock solid):
//
//   * tenant isolation — invalidating tenant A leaves tenants B/C alone
//   * per-resource invalidation — invalidating one id leaves siblings alone
//   * Clone shares state — a cloned handle reads writes from the original
//     and vice versa (the bug-fix invariant for `CacheInvalidator`)
//   * invalidate_all wipes every entry across every tenant
//   * sub-cache fan-out — `ModelCache` has 2 internal moka caches
//     (models, routing_groups); a tenant invalidation must clear both
//     or stale entries linger after admin edits
//   * TTL safety net — entries expire even with no invalidation signal

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::cache::ResourceCache;
    use crate::services::cache::provider_cache::ProviderCache;
    use crate::services::encryption::EncryptionService;
    use sqlx::postgres::PgPoolOptions;

    /// Build a `ModelCache` whose pool is never asked to connect.
    /// Every test populates state via `insert_*_for_test` helpers,
    /// so the only DB calls would happen on a real cache miss —
    /// none of these tests trigger that path.
    fn build_cache() -> ModelCache {
        let pool = PgPoolOptions::new()
            .max_connections(1)
            .connect_lazy("postgres://test:test@localhost:5432/test")
            .expect("connect_lazy never fails on a syntactically-valid URL");
        let encryption = EncryptionService::new("test-encryption-key-32-chars-ok!");
        let provider_cache = Arc::new(ProviderCache::new(pool.clone()));
        ModelCache::new(pool, encryption, provider_cache)
    }

    // -----------------------------------------------------------------------
    // Tenant isolation (the most important invariant — a multi-tenant SaaS
    // proxy that leaks state across tenants is broken in the worst way).
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn invalidate_tenant_drops_only_target_tenant_models() {
        let cache = build_cache();
        cache.insert_model_for_test("tenant-a", "gpt-5").await;
        cache.insert_model_for_test("tenant-a", "claude").await;
        cache.insert_model_for_test("tenant-b", "gpt-5").await;
        cache.insert_model_for_test("tenant-c", "gpt-5").await;

        cache.invalidate_tenant("tenant-b").await;

        assert!(cache._peek_model("tenant-a", "gpt-5").await.is_some());
        assert!(cache._peek_model("tenant-a", "claude").await.is_some());
        assert!(
            cache._peek_model("tenant-b", "gpt-5").await.is_none(),
            "tenant-b's entry must be evicted"
        );
        assert!(
            cache._peek_model("tenant-c", "gpt-5").await.is_some(),
            "tenant-c must be untouched"
        );
    }

    #[tokio::test]
    async fn invalidate_tenant_clears_both_subcaches() {
        // ModelCache wraps two moka caches: models and routing_groups.
        // invalidate_tenant must clear both for
        // the target tenant — otherwise an admin renames a routing
        // group on tenant A, the entry stays cached, and the next
        // request finds the stale group.
        let cache = build_cache();
        cache.insert_model_for_test("tenant-a", "gpt-5").await;
        cache
            .insert_routing_group_for_test("tenant-a", "openai-fallback")
            .await;
        cache.insert_model_for_test("tenant-b", "gpt-5").await;
        cache
            .insert_routing_group_for_test("tenant-b", "openai-fallback")
            .await;

        cache.invalidate_tenant("tenant-a").await;

        assert!(cache._peek_model("tenant-a", "gpt-5").await.is_none());
        assert!(
            cache
                ._peek_routing_group("tenant-a", "openai-fallback")
                .await
                .is_none()
        );
        // tenant-b entries survive
        assert!(cache._peek_model("tenant-b", "gpt-5").await.is_some());
        assert!(
            cache
                ._peek_routing_group("tenant-b", "openai-fallback")
                .await
                .is_some()
        );
    }

    // -----------------------------------------------------------------------
    // Per-resource invalidation
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn invalidate_evicts_only_specific_id() {
        let cache = build_cache();
        cache.insert_model_for_test("tenant-a", "gpt-5").await;
        cache.insert_model_for_test("tenant-a", "claude").await;
        cache.insert_model_for_test("tenant-b", "gpt-5").await;

        cache.invalidate("tenant-a", "gpt-5").await;

        assert!(cache._peek_model("tenant-a", "gpt-5").await.is_none());
        assert!(
            cache._peek_model("tenant-a", "claude").await.is_some(),
            "sibling resource on same tenant must be untouched"
        );
        assert!(
            cache._peek_model("tenant-b", "gpt-5").await.is_some(),
            "same-id different-tenant must be untouched"
        );
    }

    /// Regression test for the "added API key but health-check still
    /// reports missing" bug. Models are cached under three keys
    /// (`id`, `model_name`, `name`) but the old `invalidate` only
    /// evicted the directly-named key. The control plane invalidates
    /// by id after every model update; the post-update health probe
    /// sends `"model": "gpt-4o"` (model_name) which hits the still-
    /// stale by-name entry and fails until the 5-min TTL expires.
    /// Asserts every key is gone after a single `invalidate(tenant, id)`.
    #[tokio::test]
    async fn invalidate_by_id_evicts_all_three_lookup_keys() {
        let cache = build_cache();
        cache
            .insert_model_under_all_keys_for_test(
                "tenant-a",
                "llm-01ABC",         // id (what control-plane invalidates by)
                "gpt-4o",            // model_name (what request bodies use)
                "Production GPT-4o", // display name
            )
            .await;

        // All three lookups must hit before invalidation.
        assert!(cache._peek_model("tenant-a", "llm-01ABC").await.is_some());
        assert!(cache._peek_model("tenant-a", "gpt-4o").await.is_some());
        assert!(
            cache
                ._peek_model("tenant-a", "Production GPT-4o")
                .await
                .is_some()
        );

        // Control-plane-style invalidation: by id.
        cache.invalidate("tenant-a", "llm-01ABC").await;

        // ALL three keys must now miss — the bug was that only the
        // by-id key was evicted, leaving stale by-name entries that
        // a `model: "gpt-4o"` request would resolve against.
        assert!(
            cache._peek_model("tenant-a", "llm-01ABC").await.is_none(),
            "by-id lookup key must be evicted"
        );
        assert!(
            cache._peek_model("tenant-a", "gpt-4o").await.is_none(),
            "by-model_name lookup key must be evicted — this is the \
             key the post-update health probe resolves against; if it \
             stays cached the probe sees an api_key=None model and \
             reports 'missing API Key'"
        );
        assert!(
            cache
                ._peek_model("tenant-a", "Production GPT-4o")
                .await
                .is_none(),
            "by-display-name lookup key must be evicted"
        );
    }

    /// Same scenario, but the control plane invalidates by `model_name`
    /// instead of `id`. The fix's cache-first lookup must find the
    /// model under any of the three keys and evict the other two.
    #[tokio::test]
    async fn invalidate_by_model_name_also_evicts_id_and_display_name_keys() {
        let cache = build_cache();
        cache
            .insert_model_under_all_keys_for_test(
                "tenant-a",
                "llm-01XYZ",
                "claude-sonnet-4-5",
                "Claude Sonnet (prod)",
            )
            .await;

        cache.invalidate("tenant-a", "claude-sonnet-4-5").await;

        assert!(cache._peek_model("tenant-a", "llm-01XYZ").await.is_none());
        assert!(
            cache
                ._peek_model("tenant-a", "claude-sonnet-4-5")
                .await
                .is_none()
        );
        assert!(
            cache
                ._peek_model("tenant-a", "Claude Sonnet (prod)")
                .await
                .is_none()
        );
    }

    // -----------------------------------------------------------------------
    // Clone shares state — the bug-fix invariant for CacheInvalidator
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn clone_observes_writes_via_original_handle() {
        let cache = build_cache();
        let cloned = cache.clone();

        cache.insert_model_for_test("tenant-a", "gpt-5").await;

        assert!(
            cloned._peek_model("tenant-a", "gpt-5").await.is_some(),
            "writing through `cache` must be visible via `cache.clone()` \
             — moka's Clone shares the inner Arc; if this assertion \
             fails the bug fix that share `state.model_cache.clone()` \
             with the invalidator has regressed."
        );
    }

    #[tokio::test]
    async fn original_observes_invalidations_via_clone() {
        let cache = build_cache();
        cache.insert_model_for_test("tenant-a", "gpt-5").await;
        let cloned = cache.clone();

        cloned.invalidate("tenant-a", "gpt-5").await;

        assert!(
            cache._peek_model("tenant-a", "gpt-5").await.is_none(),
            "invalidating through the cloned handle must propagate \
             back to the original — this is the exact scenario \
             CacheInvalidator depends on (it holds a clone, not the \
             original)."
        );
    }

    #[tokio::test]
    async fn clone_strong_count_does_not_block_inner_arc() {
        // moka caches use Arc internally; cloning ModelCache should
        // increment that Arc's strong count. We can't directly observe
        // moka's count, but we can verify behaviour: if state were
        // somehow split, a series of mixed-handle operations would
        // diverge.
        let cache = build_cache();
        let clone1 = cache.clone();
        let clone2 = clone1.clone();

        cache.insert_model_for_test("t1", "a").await;
        clone1.insert_model_for_test("t1", "b").await;
        clone2.insert_model_for_test("t1", "c").await;

        // Every handle observes every write
        for handle in [&cache, &clone1, &clone2] {
            assert!(handle._peek_model("t1", "a").await.is_some());
            assert!(handle._peek_model("t1", "b").await.is_some());
            assert!(handle._peek_model("t1", "c").await.is_some());
        }

        clone1.invalidate_tenant("t1").await;
        for handle in [&cache, &clone1, &clone2] {
            assert!(handle._peek_model("t1", "a").await.is_none());
            assert!(handle._peek_model("t1", "b").await.is_none());
            assert!(handle._peek_model("t1", "c").await.is_none());
        }
    }

    // -----------------------------------------------------------------------
    // Whole-cache reset
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn invalidate_all_drops_every_tenants_entries() {
        let cache = build_cache();
        cache.insert_model_for_test("tenant-a", "x").await;
        cache.insert_model_for_test("tenant-b", "y").await;
        cache.insert_routing_group_for_test("tenant-c", "z").await;

        cache.invalidate_all().await;
        // moka's invalidate_all is technically eventual; force pending
        // tasks so the assertions are deterministic on any platform.
        cache.models.run_pending_tasks().await;
        cache.routing_groups.run_pending_tasks().await;

        assert!(cache._peek_model("tenant-a", "x").await.is_none());
        assert!(cache._peek_model("tenant-b", "y").await.is_none());
        assert!(cache._peek_routing_group("tenant-c", "z").await.is_none());
    }

    // -----------------------------------------------------------------------
    // Key-format assumptions baked into invalidate_tenant — if the key
    // shape ever changes (e.g., to include a sub-prefix like
    // `models:{tenant}:{id}`), the prefix-match logic in
    // invalidate_tenant must change in lockstep. This test pins the
    // assumption so the change can't land silently.
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn key_prefix_format_is_tenant_colon() {
        let cache = build_cache();
        cache.insert_model_for_test("acme", "gpt-5").await;
        // Insert an entry whose name STARTS WITH another tenant's id —
        // a naive `contains` would over-evict here.
        cache
            .insert_model_for_test("acme-corp", "gpt-5") // distinct tenant
            .await;

        cache.invalidate_tenant("acme").await;

        assert!(
            cache._peek_model("acme", "gpt-5").await.is_none(),
            "tenant 'acme' must be evicted"
        );
        assert!(
            cache._peek_model("acme-corp", "gpt-5").await.is_some(),
            "tenant 'acme-corp' must NOT be evicted by an 'acme' \
             invalidation — the prefix check uses '{{tenant}}:' \
             specifically (with the colon), not a bare prefix"
        );
    }
}
