// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # MCP Server Cache
//!
//! In-memory cache for MCP server configurations. Eliminates DB queries
//! on every MCP proxy request.

use std::sync::Arc;
use std::time::Duration;

use moka::future::Cache;
use sqlx::PgPool;

use crate::models::db::{McpServer, ServerConfig};

use super::{Generation, ResourceCache, insert_fresh};

const DEFAULT_TTL_SECS: u64 = 300;
const DEFAULT_MAX_ENTRIES: u64 = 5_000;

/// In-memory cache for MCP server and server config lookups.
///
/// `Clone` shares the underlying moka caches — see the matching note
/// on `ModelCache`. The cache invalidator must hold the same instance
/// the request pipeline uses.
#[derive(Clone)]
pub struct McpServerCache {
    /// MCP servers by `{tenant_id}:{server_id}`
    servers: Cache<String, Arc<McpServer>>,
    /// Server configs by `{tenant_id}:{config_id}`
    configs: Cache<String, Arc<ServerConfig>>,
    db: PgPool,
    /// Bumped by every invalidation; loads insert with [`insert_fresh`].
    generation: Generation,
    // hit/miss counters reported to the operator
    // console. Combined across both `servers` and `configs` because
    // the UI surfaces them as one logical 'tool cache' layer.
    hits: Arc<std::sync::atomic::AtomicU64>,
    misses: Arc<std::sync::atomic::AtomicU64>,
}

impl McpServerCache {
    pub fn new(db: PgPool) -> Self {
        // `support_invalidation_closures` is REQUIRED for
        // `invalidate_entries_if` to actually evict — see the matching
        // note on `ModelCache::new`.
        Self {
            servers: Cache::builder()
                .max_capacity(DEFAULT_MAX_ENTRIES)
                .time_to_live(Duration::from_secs(DEFAULT_TTL_SECS))
                .support_invalidation_closures()
                .build(),
            configs: Cache::builder()
                .max_capacity(DEFAULT_MAX_ENTRIES)
                .time_to_live(Duration::from_secs(DEFAULT_TTL_SECS))
                .support_invalidation_closures()
                .build(),
            db,
            generation: Generation::default(),
            hits: Arc::new(std::sync::atomic::AtomicU64::new(0)),
            misses: Arc::new(std::sync::atomic::AtomicU64::new(0)),
        }
    }

    /// Get an MCP server by ID (cached).
    pub async fn get_server(
        &self,
        tenant_id: &str,
        server_id: &str,
    ) -> Result<Option<Arc<McpServer>>, sqlx::Error> {
        let key = format!("{tenant_id}:{server_id}");

        if let Some(server) = self.servers.get(&key).await {
            self.hits.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            return Ok(Some(server));
        }
        self.misses
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);

        let started = self.generation.current();
        let Some(server) = sqlx::query_as::<_, McpServer>(
            "SELECT * FROM mcp_servers WHERE id = $1 AND tenant_id = $2 AND enabled = true",
        )
        .bind(server_id)
        .bind(tenant_id)
        .fetch_optional(&self.db)
        .await?
        else {
            return Ok(None);
        };

        let server = Arc::new(server);
        let g = &self.generation;
        insert_fresh(&self.servers, key, server.clone(), g, started).await;
        // Also insert by name for name-based lookups
        let by_name = format!("{tenant_id}:{}", server.name);
        insert_fresh(&self.servers, by_name, server.clone(), g, started).await;

        Ok(Some(server))
    }

    /// Get a server config by ID (cached).
    pub async fn get_config(
        &self,
        tenant_id: &str,
        config_id: &str,
    ) -> Result<Option<Arc<ServerConfig>>, sqlx::Error> {
        let key = format!("{tenant_id}:{config_id}");

        if let Some(config) = self.configs.get(&key).await {
            self.hits.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            return Ok(Some(config));
        }
        self.misses
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);

        let started = self.generation.current();
        let Some(config) = sqlx::query_as::<_, ServerConfig>(
            "SELECT * FROM server_configs WHERE id = $1 AND tenant_id = $2 AND enabled = true",
        )
        .bind(config_id)
        .bind(tenant_id)
        .fetch_optional(&self.db)
        .await?
        else {
            return Ok(None);
        };

        let config = Arc::new(config);
        insert_fresh(
            &self.configs,
            key,
            config.clone(),
            &self.generation,
            started,
        )
        .await;

        Ok(Some(config))
    }

    /// Snapshot for the cache stats fan-out.
    /// `entries` sums both internal moka caches.
    pub fn stats(&self) -> crate::services::cache::CacheStatsSnapshot {
        crate::services::cache::CacheStatsSnapshot {
            hits: self.hits.load(std::sync::atomic::Ordering::Relaxed),
            misses: self.misses.load(std::sync::atomic::Ordering::Relaxed),
            entries: self.servers.entry_count() + self.configs.entry_count(),
        }
    }
}

#[async_trait::async_trait]
impl ResourceCache for McpServerCache {
    fn name(&self) -> &str {
        "mcp_server_cache"
    }

    async fn invalidate(&self, tenant_id: &str, resource_id: &str) {
        self.generation.bump();
        let key = format!("{tenant_id}:{resource_id}");
        self.servers.invalidate(&key).await;
        self.configs.invalidate(&key).await;
        // The server is also cached under its name, which the message does
        // not carry: evict every entry holding this server.
        let prefix = format!("{tenant_id}:");
        let id = resource_id.to_string();
        let _ = self
            .servers
            .invalidate_entries_if(move |k, v| k.starts_with(&prefix) && v.id == id);
        self.servers.run_pending_tasks().await;
    }

    async fn invalidate_tenant(&self, tenant_id: &str) {
        // moka's predicate eviction is lazy; force a maintenance
        // pass so the next lookup is guaranteed to miss. See the
        // matching note on `ModelCache::invalidate_tenant`.
        self.generation.bump();
        let prefix = format!("{tenant_id}:");
        let prefix2 = prefix.clone();
        let _ = self
            .servers
            .invalidate_entries_if(move |k, _| k.starts_with(&prefix));
        let _ = self
            .configs
            .invalidate_entries_if(move |k, _| k.starts_with(&prefix2));
        self.servers.run_pending_tasks().await;
        self.configs.run_pending_tasks().await;
    }

    async fn invalidate_all(&self) {
        self.generation.bump();
        self.servers.invalidate_all();
        self.configs.invalidate_all();
    }
}

// ---------------------------------------------------------------------------
// Test-only helpers
// ---------------------------------------------------------------------------

#[cfg(any(test, feature = "db-tests"))]
impl McpServerCache {
    pub(super) async fn _peek_server(
        &self,
        tenant_id: &str,
        server_id: &str,
    ) -> Option<Arc<McpServer>> {
        self.servers.get(&format!("{tenant_id}:{server_id}")).await
    }

    pub(super) async fn _peek_config(
        &self,
        tenant_id: &str,
        config_id: &str,
    ) -> Option<Arc<ServerConfig>> {
        self.configs.get(&format!("{tenant_id}:{config_id}")).await
    }

    pub(super) async fn insert_server_for_test(&self, tenant_id: &str, server_id: &str) {
        let server = McpServer {
            id: server_id.to_string(),
            tenant_id: tenant_id.to_string(),
            name: format!("Synth {server_id}"),
            base_url: "http://localhost".to_string(),
            mcp_endpoint_path: None,
            auth_type: None,
            auth_token: None,
            timeout: None,
            enabled: true,
            mcp_apps_enabled: None,
            capabilities: None,
            health_status: None,
            idp_id: None,
            max_concurrent_requests: None,
            retry_attempts: None,
            retry_after_ms: None,
            failure_threshold: None,
            recovery_timeout_secs: None,
            wants_listener: None,
            region: None,
            protocol_version: None,
            dialect: None,
        };
        self.servers
            .insert(format!("{tenant_id}:{server_id}"), Arc::new(server))
            .await;
    }

    #[cfg(test)]
    pub(super) async fn insert_config_for_test(&self, tenant_id: &str, config_id: &str) {
        let cfg = ServerConfig {
            id: config_id.to_string(),
            tenant_id: tenant_id.to_string(),
            mcp_server_id: Some("mcp-x".to_string()),
            oauth_client_id: None,
            name: format!("Synth {config_id}"),
            priority: None,
            is_default: false,
            enabled: true,
        };
        self.configs
            .insert(format!("{tenant_id}:{config_id}"), Arc::new(cfg))
            .await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::cache::ResourceCache;
    use sqlx::postgres::PgPoolOptions;

    fn build_cache() -> McpServerCache {
        let pool = PgPoolOptions::new()
            .max_connections(1)
            .connect_lazy("postgres://test:test@localhost:5432/test")
            .expect("connect_lazy never fails on a syntactically-valid URL");
        McpServerCache::new(pool)
    }

    // -----------------------------------------------------------------------
    // Tenant isolation across both sub-caches
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn invalidate_tenant_drops_servers_and_configs_for_target_only() {
        let cache = build_cache();
        cache.insert_server_for_test("tenant-a", "weather").await;
        cache.insert_server_for_test("tenant-a", "search").await;
        cache.insert_config_for_test("tenant-a", "cfg-1").await;
        cache.insert_server_for_test("tenant-b", "weather").await;
        cache.insert_config_for_test("tenant-b", "cfg-1").await;

        cache.invalidate_tenant("tenant-a").await;

        // tenant-a fully cleared (both sub-caches)
        assert!(cache._peek_server("tenant-a", "weather").await.is_none());
        assert!(cache._peek_server("tenant-a", "search").await.is_none());
        assert!(cache._peek_config("tenant-a", "cfg-1").await.is_none());
        // tenant-b untouched
        assert!(cache._peek_server("tenant-b", "weather").await.is_some());
        assert!(cache._peek_config("tenant-b", "cfg-1").await.is_some());
    }

    #[tokio::test]
    async fn key_prefix_is_tenant_colon_not_substring() {
        let cache = build_cache();
        cache.insert_server_for_test("acme", "weather").await;
        cache.insert_server_for_test("acme-corp", "weather").await;

        cache.invalidate_tenant("acme").await;

        assert!(cache._peek_server("acme", "weather").await.is_none());
        assert!(cache._peek_server("acme-corp", "weather").await.is_some());
    }

    // -----------------------------------------------------------------------
    // Per-resource invalidation
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn invalidate_evicts_only_specific_id() {
        let cache = build_cache();
        cache.insert_server_for_test("tenant-a", "weather").await;
        cache.insert_server_for_test("tenant-a", "search").await;

        cache.invalidate("tenant-a", "weather").await;

        assert!(cache._peek_server("tenant-a", "weather").await.is_none());
        assert!(cache._peek_server("tenant-a", "search").await.is_some());
    }

    #[tokio::test]
    async fn invalidate_clears_both_sub_caches_for_same_id() {
        // Per the inherent impl: invalidate(tenant, id) clears both
        // servers AND configs under that key. This tests the contract.
        let cache = build_cache();
        cache.insert_server_for_test("tenant-a", "weather").await;
        cache.insert_config_for_test("tenant-a", "weather").await;

        cache.invalidate("tenant-a", "weather").await;

        assert!(cache._peek_server("tenant-a", "weather").await.is_none());
        assert!(cache._peek_config("tenant-a", "weather").await.is_none());
    }

    // -----------------------------------------------------------------------
    // Clone shares state
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn clone_shares_state_with_original() {
        let cache = build_cache();
        cache.insert_server_for_test("tenant-a", "weather").await;

        let cloned = cache.clone();
        cloned.invalidate("tenant-a", "weather").await;

        assert!(
            cache._peek_server("tenant-a", "weather").await.is_none(),
            "moka Clone shares the inner store — invalidation through \
             cloned handle must propagate to original"
        );
    }

    // -----------------------------------------------------------------------
    // Whole-cache reset
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn invalidate_all_drops_every_entry() {
        let cache = build_cache();
        cache.insert_server_for_test("a", "x").await;
        cache.insert_config_for_test("b", "y").await;

        cache.invalidate_all().await;
        cache.servers.run_pending_tasks().await;
        cache.configs.run_pending_tasks().await;

        assert!(cache._peek_server("a", "x").await.is_none());
        assert!(cache._peek_config("b", "y").await.is_none());
    }
}
