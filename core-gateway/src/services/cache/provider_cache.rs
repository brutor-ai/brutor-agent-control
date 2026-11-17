// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Provider Cache
//!
//! Providers are first-class DB entities
//! (`llm_providers`) — but the *slug* (`key`, e.g. `"openai"`) is
//! what every adapter dispatcher in the proxy actually matches on.
//!
//! Before this cache the slug was denormalised onto every model
//! query via `LEFT JOIN llm_providers ON llm_model.provider_id =
//! llm_providers.id`. That JOIN ran on *every* model lookup, and a
//! single orphaned `provider_id` (an FK pointing at a deleted row)
//! would produce a NULL `provider` column that sqlx couldn't decode
//! into `LlmModel`'s non-optional `String` field. The decode error
//! was being silently swallowed and the model became invisible —
//! operators saw a confusing 404 from the proxy.
//!
//! The fix: stop carrying the slug on `LlmModel`. Resolve it once
//! at model-load boundaries via this cache, keyed on `provider_id`.
//! Every model lookup now does a plain `SELECT * FROM llm_model`
//! and the loader stamps `model.provider` from the cache. The cache
//! is a small per-tenant moka cache (a tenant typically has <40
//! providers), with TTL as a safety net and Redis pub/sub for
//! push-based invalidation through `CacheInvalidator`.

use std::sync::Arc;
use std::time::Duration;

use moka::future::Cache;
use sqlx::PgPool;

use super::{Generation, ResourceCache, insert_fresh};

/// Default TTL — providers change rarely, but we want eventual
/// consistency if a Redis pub/sub message is missed.
const DEFAULT_TTL_SECS: u64 = 600; // 10 minutes
/// A tenant has well under 40 providers in practice; cap generously.
const DEFAULT_MAX_PROVIDERS: u64 = 10_000;

/// Minimal provider row needed by adapter dispatch.
///
/// We don't load icon bytes or notes here — the dispatcher only
/// needs the slug. Other consumers (admin REST API, icon endpoint)
/// query the table directly.
#[derive(Debug, Clone, sqlx::FromRow)]
pub struct LlmProvider {
    pub id: String,
    pub tenant_id: String,
    pub key: String,
    pub display_name: String,
    pub category: Option<String>,
    pub default_api_url: Option<String>,
    pub default_auth_type: Option<String>,
    /// Provider-wide request-body parameter drop list. The proxy
    /// unions this with the model row's own `additional_drop_params`
    /// before forwarding upstream. Null = no provider-wide drops.
    pub additional_drop_params: Option<serde_json::Value>,
    pub enabled: bool,
    /// Region this provider serves traffic
    /// from. Read by `services::residency::check_llm_request`.
    /// `#[sqlx(default)]` keeps a pre-a069 schema readable.
    #[sqlx(default)]
    pub region: Option<String>,
}

/// Cache `(tenant_id, provider_id) → LlmProvider`.
pub struct ProviderCache {
    providers: Cache<String, Arc<LlmProvider>>,
    db: PgPool,
    /// Bumped by every invalidation; loads insert with [`insert_fresh`].
    generation: Generation,
}

impl ProviderCache {
    pub fn new(db: PgPool) -> Self {
        // `support_invalidation_closures` is REQUIRED for
        // `invalidate_entries_if` to actually evict — see the matching
        // note on `ModelCache::new`.
        Self {
            providers: Cache::builder()
                .max_capacity(DEFAULT_MAX_PROVIDERS)
                .time_to_live(Duration::from_secs(DEFAULT_TTL_SECS))
                .support_invalidation_closures()
                .build(),
            db,
            generation: Generation::default(),
        }
    }

    fn key(tenant_id: &str, provider_id: &str) -> String {
        format!("{tenant_id}:{provider_id}")
    }

    /// Look up a provider's slug (`key`) by its FK id. Returns
    /// `None` when the FK is orphaned — callers MUST handle this
    /// (treat the model as un-dispatchable rather than swallowing).
    pub async fn slug(&self, tenant_id: &str, provider_id: &str) -> Option<String> {
        self.get(tenant_id, provider_id)
            .await
            .map(|p| p.key.clone())
    }

    /// Look up the full provider row by FK id.
    pub async fn get(&self, tenant_id: &str, provider_id: &str) -> Option<Arc<LlmProvider>> {
        let cache_key = Self::key(tenant_id, provider_id);
        if let Some(hit) = self.providers.get(&cache_key).await {
            return Some(hit);
        }

        let started = self.generation.current();
        let result = sqlx::query_as::<_, LlmProvider>(
            r#"SELECT id, tenant_id, key, display_name, category,
                      default_api_url, default_auth_type,
                      additional_drop_params, enabled, region
               FROM llm_providers
               WHERE id = $1 AND tenant_id = $2"#,
        )
        .bind(provider_id)
        .bind(tenant_id)
        .fetch_optional(&self.db)
        .await;

        match result {
            Ok(Some(row)) => {
                let arc = Arc::new(row);
                insert_fresh(
                    &self.providers,
                    cache_key,
                    arc.clone(),
                    &self.generation,
                    started,
                )
                .await;
                Some(arc)
            }
            Ok(None) => {
                tracing::warn!(
                    tenant_id,
                    provider_id,
                    "ProviderCache miss — provider FK is orphaned (no row in llm_providers)",
                );
                None
            }
            Err(e) => {
                tracing::error!(
                    tenant_id,
                    provider_id,
                    error = %e,
                    "ProviderCache DB lookup failed",
                );
                None
            }
        }
    }

    /// Stamp `model.provider` and `model.provider_additional_drop_params`
    /// on a slice of bare-loaded models. One round-trip for the whole
    /// batch via `providers_for`. Use this at every model-load boundary
    /// that previously relied on the `LEFT JOIN llm_providers` trick.
    pub async fn stamp_models(&self, models: &mut [crate::models::db::LlmModel]) {
        if models.is_empty() {
            return;
        }
        // All models in a batch share a tenant — assert defensively
        // and bail if a caller mixes tenants (would point to a code
        // bug, not a runtime condition).
        let tenant_id = models[0].tenant_id.clone();
        debug_assert!(
            models.iter().all(|m| m.tenant_id == tenant_id),
            "stamp_models called with mixed tenants",
        );
        let pids: Vec<String> = models.iter().map(|m| m.provider_id.clone()).collect();
        let by_pid = self.providers_for(&tenant_id, &pids).await;
        for m in models {
            match by_pid.get(&m.provider_id) {
                Some(p) => {
                    m.provider = p.key.clone();
                    m.provider_additional_drop_params = p.additional_drop_params.clone();
                }
                None => {
                    m.provider = String::new();
                    m.provider_additional_drop_params = None;
                }
            }
        }
    }

    /// Stamp a single model — convenience wrapper around
    /// `stamp_models`. Always returns the slug so the caller can
    /// log / decide whether to drop the row.
    pub async fn stamp_one(&self, model: &mut crate::models::db::LlmModel) -> String {
        match self.get(&model.tenant_id, &model.provider_id).await {
            Some(p) => {
                model.provider = p.key.clone();
                model.provider_additional_drop_params = p.additional_drop_params.clone();
                p.key.clone()
            }
            None => {
                model.provider = String::new();
                model.provider_additional_drop_params = None;
                String::new()
            }
        }
    }

    /// Bulk slug resolver — fewer cache + DB round-trips when a
    /// caller has a batch of distinct `provider_id`s (e.g. the
    /// router populating `Vec<LlmModel>` for a routing group).
    pub async fn slugs_for(
        &self,
        tenant_id: &str,
        provider_ids: &[String],
    ) -> std::collections::HashMap<String, String> {
        self.providers_for(tenant_id, provider_ids)
            .await
            .into_iter()
            .map(|(id, p)| (id, p.key.clone()))
            .collect()
    }

    /// Bulk full-row resolver. Returns one `Arc<LlmProvider>` per
    /// resolved `provider_id`. Same caching + bulk-fetch behaviour
    /// as `slugs_for`. Used by `stamp_models` to surface both the
    /// slug AND the provider's `additional_drop_params` onto every
    /// model.
    pub async fn providers_for(
        &self,
        tenant_id: &str,
        provider_ids: &[String],
    ) -> std::collections::HashMap<String, Arc<LlmProvider>> {
        use std::collections::HashMap;

        let mut out: HashMap<String, Arc<LlmProvider>> = HashMap::with_capacity(provider_ids.len());
        let mut missing: Vec<&str> = Vec::new();
        for pid in provider_ids {
            if let Some(hit) = self.providers.get(&Self::key(tenant_id, pid)).await {
                out.insert(pid.clone(), hit);
            } else {
                missing.push(pid);
            }
        }
        if missing.is_empty() {
            return out;
        }

        // Fetch missing in a single round-trip.
        let started = self.generation.current();
        let result = sqlx::query_as::<_, LlmProvider>(
            r#"SELECT id, tenant_id, key, display_name, category,
                      default_api_url, default_auth_type,
                      additional_drop_params, enabled, region
               FROM llm_providers
               WHERE tenant_id = $1 AND id = ANY($2)"#,
        )
        .bind(tenant_id)
        .bind(missing.iter().map(|s| s.to_string()).collect::<Vec<_>>())
        .fetch_all(&self.db)
        .await;

        match result {
            Ok(rows) => {
                for row in rows {
                    let id = row.id.clone();
                    let arc = Arc::new(row);
                    let key = Self::key(tenant_id, &id);
                    insert_fresh(&self.providers, key, arc.clone(), &self.generation, started)
                        .await;
                    out.insert(id, arc);
                }
            }
            Err(e) => {
                tracing::error!(
                    tenant_id,
                    missing_count = missing.len(),
                    error = %e,
                    "ProviderCache bulk lookup failed",
                );
            }
        }
        out
    }
}

#[async_trait::async_trait]
impl ResourceCache for ProviderCache {
    fn name(&self) -> &str {
        "provider_cache"
    }

    async fn invalidate(&self, tenant_id: &str, resource_id: &str) {
        self.generation.bump();
        let key = Self::key(tenant_id, resource_id);
        self.providers.invalidate(&key).await;
        tracing::debug!(tenant_id, resource_id, "ProviderCache entry invalidated");
    }

    async fn invalidate_tenant(&self, tenant_id: &str) {
        // Force pending maintenance so the next lookup is guaranteed
        // to miss — moka's predicate eviction is otherwise lazy.
        // See the matching note on `ModelCache::invalidate_tenant`.
        self.generation.bump();
        let prefix = format!("{tenant_id}:");
        self.providers
            .invalidate_entries_if(move |k, _| k.starts_with(&prefix))
            .ok();
        self.providers.run_pending_tasks().await;
        tracing::debug!(tenant_id, "ProviderCache tenant invalidated");
    }

    async fn invalidate_all(&self) {
        self.generation.bump();
        self.providers.invalidate_all();
        tracing::debug!("ProviderCache fully invalidated");
    }
}

// ---------------------------------------------------------------------------
// Test-only helpers
// ---------------------------------------------------------------------------

#[cfg(any(test, feature = "db-tests"))]
impl ProviderCache {
    /// Direct probe — bypasses the DB fallback so tests reflect pure
    /// cache state.
    pub(super) async fn _peek(
        &self,
        tenant_id: &str,
        provider_id: &str,
    ) -> Option<Arc<LlmProvider>> {
        self.providers.get(&Self::key(tenant_id, provider_id)).await
    }

    /// Inserts a synthesised `LlmProvider` under `(tenant_id, provider_id)`
    /// without touching the database. The synthesised slug equals the
    /// `provider_id` so dispatcher tests can also verify slug stamping.
    pub(super) async fn insert_provider_for_test(&self, tenant_id: &str, provider_id: &str) {
        let prov = LlmProvider {
            id: provider_id.to_string(),
            tenant_id: tenant_id.to_string(),
            key: provider_id.to_string(),
            display_name: format!("Synth {provider_id}"),
            category: None,
            default_api_url: None,
            default_auth_type: None,
            additional_drop_params: None,
            enabled: true,
            region: None,
        };
        self.providers
            .insert(Self::key(tenant_id, provider_id), Arc::new(prov))
            .await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use sqlx::postgres::PgPoolOptions;

    fn build_cache() -> Arc<ProviderCache> {
        let pool = PgPoolOptions::new()
            .max_connections(1)
            .connect_lazy("postgres://test:test@localhost:5432/test")
            .expect("connect_lazy never fails on a syntactically-valid URL");
        Arc::new(ProviderCache::new(pool))
    }

    #[test]
    fn key_format_is_stable() {
        assert_eq!(
            ProviderCache::key("tenant-a", "prov-01"),
            "tenant-a:prov-01"
        );
    }

    // -----------------------------------------------------------------------
    // Tenant isolation
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn invalidate_tenant_drops_only_target_tenant_providers() {
        let cache = build_cache();
        cache.insert_provider_for_test("tenant-a", "openai").await;
        cache
            .insert_provider_for_test("tenant-a", "anthropic")
            .await;
        cache.insert_provider_for_test("tenant-b", "openai").await;
        cache.insert_provider_for_test("tenant-c", "openai").await;

        cache.invalidate_tenant("tenant-b").await;

        assert!(cache._peek("tenant-a", "openai").await.is_some());
        assert!(cache._peek("tenant-a", "anthropic").await.is_some());
        assert!(cache._peek("tenant-b", "openai").await.is_none());
        assert!(cache._peek("tenant-c", "openai").await.is_some());
    }

    #[tokio::test]
    async fn key_prefix_is_tenant_colon_not_substring() {
        // 'acme' must not evict 'acme-corp' (substring trap).
        let cache = build_cache();
        cache.insert_provider_for_test("acme", "openai").await;
        cache.insert_provider_for_test("acme-corp", "openai").await;

        cache.invalidate_tenant("acme").await;

        assert!(cache._peek("acme", "openai").await.is_none());
        assert!(cache._peek("acme-corp", "openai").await.is_some());
    }

    // -----------------------------------------------------------------------
    // Per-resource invalidation
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn invalidate_evicts_only_specific_provider() {
        let cache = build_cache();
        cache.insert_provider_for_test("tenant-a", "openai").await;
        cache
            .insert_provider_for_test("tenant-a", "anthropic")
            .await;
        cache.insert_provider_for_test("tenant-b", "openai").await;

        cache.invalidate("tenant-a", "openai").await;

        assert!(cache._peek("tenant-a", "openai").await.is_none());
        assert!(cache._peek("tenant-a", "anthropic").await.is_some());
        assert!(cache._peek("tenant-b", "openai").await.is_some());
    }

    // -----------------------------------------------------------------------
    // Arc-shared instance — `ProviderCache` is wired through `Arc<...>`
    // rather than `Clone`, but the invariant is the same: invalidation
    // through one handle must propagate to all others.
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn arc_clone_observes_mutations_via_other_handle() {
        let cache = build_cache();
        let cloned = Arc::clone(&cache);

        cache.insert_provider_for_test("tenant-a", "openai").await;
        assert!(cloned._peek("tenant-a", "openai").await.is_some());

        cloned.invalidate("tenant-a", "openai").await;
        assert!(
            cache._peek("tenant-a", "openai").await.is_none(),
            "invalidating through Arc<ProviderCache> handle B must \
             affect handle A — they share the same inner moka cache"
        );
    }

    // -----------------------------------------------------------------------
    // Slug retrieval — the request-hot-path entrypoint
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn slug_returns_stamped_value_from_cache() {
        let cache = build_cache();
        cache.insert_provider_for_test("tenant-a", "openai").await;

        let slug = cache.slug("tenant-a", "openai").await;
        assert_eq!(
            slug,
            Some("openai".to_string()),
            "slug() must read from the cache; the synth provider's \
             `key` field equals its id, so we expect 'openai'"
        );
    }

    #[tokio::test]
    async fn slug_returns_none_for_missing_provider_without_db() {
        // No provider inserted → cache miss → DB miss (lazy pool
        // never connects on a `slug` call when the cache has the
        // tenant prefix; if it tries to connect, the test errors
        // out, which would also be a useful signal).
        let cache = build_cache();
        let slug = cache.slug("tenant-a", "missing-provider").await;
        // Either None (clean miss) or the test panics on DB connect —
        // both outcomes are acceptable as long as we don't return
        // stale data. We assert None as the expected behaviour.
        assert_eq!(slug, None);
    }

    // -----------------------------------------------------------------------
    // Whole-cache reset
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn invalidate_all_drops_all_tenants() {
        let cache = build_cache();
        cache.insert_provider_for_test("a", "x").await;
        cache.insert_provider_for_test("b", "y").await;
        cache.insert_provider_for_test("c", "z").await;

        cache.invalidate_all().await;
        cache.providers.run_pending_tasks().await;

        assert!(cache._peek("a", "x").await.is_none());
        assert!(cache._peek("b", "y").await.is_none());
        assert!(cache._peek("c", "z").await.is_none());
    }
}
