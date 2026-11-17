// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Agent Card Cache
//!
//! In-memory cache for A2A `agent_cards` rows + their capabilities.
//! Avoids 1-3 DB queries per A2A invocation (card lookup by id or name,
//! plus per-capability lookup).
//!
//! ## Indexing
//!
//! Each card is stored under TWO keys pointing at the same `Arc`:
//!   * `{tenant_id}:id:{card_id}` — used by `routes/a2a.rs` (capability invoke)
//!   * `{tenant_id}:name:{card_name}` — used by `routes/a2a_v1.rs` (well-known
//!     card discovery, capability invoke by card name) and the discovery
//!     handler in `routes/a2a.rs`
//!
//! The key prefix (`id:` vs `name:`) prevents accidental collision when a
//! card name happens to match another card's id.
//!
//! ## Capability storage
//!
//! Capabilities are loaded once when the card is materialised and stored
//! inline as `BTreeMap<String, AgentCardCapability>` keyed by
//! `capability_name`. Capability invocation paths read the capability
//! straight off the cached card — no extra query.
//!
//! ## Invalidation
//!
//! Both index keys for a card share the same Arc, but `invalidate(tenant, id)`
//! only knows the id. To make sure a name-keyed entry doesn't outlive an
//! id-keyed eviction, the load step records the card's name on the cached
//! struct and the invalidator walks the name index by tenant prefix when a
//! per-id invalidation lands. For the bulk `invalidate_tenant` case both
//! indexes are wiped together via the tenant prefix.

use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::Duration;

use chrono::{DateTime, Utc};
use moka::future::Cache;
use serde_json::Value;
use sqlx::PgPool;

use super::{Generation, ResourceCache, insert_fresh};

const DEFAULT_TTL_SECS: u64 = 300; // 5 minutes — push-invalidation is primary
const DEFAULT_MAX_ENTRIES: u64 = 5_000;

/// Materialised agent card row + its capabilities.
///
/// Field set is the union of every column read by `routes/a2a.rs` and
/// `routes/a2a_v1.rs`: the per-handler row structs project from this
/// single source of truth. Schema-version-conditional columns
/// (`extended_card_json`, `signature*`, capability `handler_*`) are
/// `Option` so the cache works on both pre-a012 and current schemas
/// — the route helpers already tolerated their absence.
#[derive(Debug, Clone)]
pub struct CachedAgentCard {
    pub id: String,
    pub tenant_id: String,
    pub name: String,
    pub version: String,
    pub card_json: Value,
    pub trust_tier: String,
    pub enabled: bool,
    pub revoked_at: Option<DateTime<Utc>>,
    pub extended_card_json: Option<Value>,
    pub signature: Option<String>,
    pub signature_algorithm: Option<String>,
    pub signed_by: Option<String>,
    pub signed_at: Option<DateTime<Utc>>,
    /// Tenant-wide A2A limits for this card
    /// (Resources → Agents → <card> → Limits). Read by the outbound
    /// route's frequency-quota and delegation-depth checks — cached so
    /// the hot path never re-SELECTs the row it already loaded. `None`
    /// when the card states none.
    pub tenant_limits: Option<Value>,
    /// The card's own A2A policy (Resources → Agents → <card> → Policies):
    /// the same document a resource group's A2A policy is, judged with the
    /// caller's group chain on every A2A route
    /// (`services::a2a_governance`).
    pub tenant_governance: Option<Value>,
    /// Capability rows indexed by `capability_name`. `BTreeMap` keeps
    /// iteration deterministic for tests; capability count per card
    /// is small (~10), so the difference vs `HashMap` is noise.
    pub capabilities: BTreeMap<String, AgentCardCapability>,
}

#[derive(Debug, Clone)]
pub struct AgentCardCapability {
    pub id: String,
    pub capability_name: String,
    pub enabled: bool,
    pub timeout_secs: Option<i32>,
    /// The capability's own hourly rate: one more dimension of the call's
    /// rate admission (`usage::a2a_capability_dims`).
    pub rate_limit_per_hour: Option<i32>,
    pub data_classification: Option<Value>,
    pub handler_type: Option<String>,
    pub handler_ref: Option<String>,
}

/// In-memory agent card cache with dual-key indexing (id, name).
///
/// `Clone` shares the moka-backed maps with the cache invalidator —
/// see the matching note on `ModelCache`.
#[derive(Clone)]
pub struct AgentCardCache {
    /// `{tenant_id}:id:{card_id}` → card
    by_id: Cache<String, Arc<CachedAgentCard>>,
    /// `{tenant_id}:name:{card_name}` → card (same Arc as by_id)
    by_name: Cache<String, Arc<CachedAgentCard>>,
    db: PgPool,
    /// Bumped by every invalidation; loads insert with [`insert_fresh`].
    generation: Generation,
}

impl AgentCardCache {
    pub fn new(db: PgPool) -> Self {
        // `support_invalidation_closures` is REQUIRED for
        // `invalidate_entries_if` to actually evict — see the matching
        // note on `ModelCache::new`.
        Self {
            by_id: Cache::builder()
                .max_capacity(DEFAULT_MAX_ENTRIES)
                .time_to_live(Duration::from_secs(DEFAULT_TTL_SECS))
                .support_invalidation_closures()
                .build(),
            by_name: Cache::builder()
                .max_capacity(DEFAULT_MAX_ENTRIES)
                .time_to_live(Duration::from_secs(DEFAULT_TTL_SECS))
                .support_invalidation_closures()
                .build(),
            db,
            generation: Generation::default(),
        }
    }

    /// Look up a card by ID. Returns `None` if the card doesn't exist
    /// for this tenant.
    /// A database error is returned, never read as "no such card".
    pub async fn get_by_id(
        &self,
        tenant_id: &str,
        card_id: &str,
    ) -> Result<Option<Arc<CachedAgentCard>>, sqlx::Error> {
        let key = format!("{tenant_id}:id:{card_id}");
        if let Some(card) = self.by_id.get(&key).await {
            return Ok(Some(card));
        }
        let started = self.generation.current();
        let Some(card) = self.load_by_id(tenant_id, card_id).await? else {
            return Ok(None);
        };
        let card = Arc::new(card);
        self.insert_both(&card, started).await;
        Ok(Some(card))
    }

    /// Look up a card by name (unique within a tenant).
    pub async fn get_by_name(
        &self,
        tenant_id: &str,
        card_name: &str,
    ) -> Result<Option<Arc<CachedAgentCard>>, sqlx::Error> {
        let key = format!("{tenant_id}:name:{card_name}");
        if let Some(card) = self.by_name.get(&key).await {
            return Ok(Some(card));
        }
        let started = self.generation.current();
        let Some(card) = self.load_by_name(tenant_id, card_name).await? else {
            return Ok(None);
        };
        let card = Arc::new(card);
        self.insert_both(&card, started).await;
        Ok(Some(card))
    }

    async fn insert_both(&self, card: &Arc<CachedAgentCard>, started: u64) {
        let id_key = format!("{}:id:{}", card.tenant_id, card.id);
        let name_key = format!("{}:name:{}", card.tenant_id, card.name);
        // Same Arc inserted under both keys — concurrent eviction of
        // one leaves the other valid until invalidation fires.
        let g = &self.generation;
        insert_fresh(&self.by_id, id_key, card.clone(), g, started).await;
        insert_fresh(&self.by_name, name_key, card.clone(), g, started).await;
    }

    /// Cache stats for `/health/cache` introspection.
    pub fn stats(&self) -> (u64, u64) {
        (self.by_id.entry_count(), self.by_name.entry_count())
    }

    // -----------------------------------------------------------------------
    // Loaders — kept private. Both call `load_capabilities` to populate the
    // capabilities map in the same DB round trip the route used to make on
    // its own, so a cache miss costs one card SELECT + one capability SELECT
    // total (vs. two SELECTs every request without the cache).
    // -----------------------------------------------------------------------

    async fn load_by_id(
        &self,
        tenant_id: &str,
        card_id: &str,
    ) -> Result<Option<CachedAgentCard>, sqlx::Error> {
        // One schema: a column the database lacks is an error, and the
        // caller refuses the call. There is no narrower projection to fall
        // back to (one would answer with a card that has no limits and no
        // policy).
        match self
            .load_card_row(
                r#"SELECT id, tenant_id, name, version, card_json, trust_tier, enabled, revoked_at,
                          extended_card_json, signature, signature_algorithm, signed_by, signed_at,
                          tenant_limits, tenant_governance
                   FROM agent_cards
                   WHERE id = $1 AND tenant_id = $2
                   LIMIT 1"#,
                card_id,
                tenant_id,
            )
            .await
        {
            Ok(row) => {
                if let Some(mut card) = row {
                    card.capabilities = self.load_capabilities(&card.id).await?;
                    Ok(Some(card))
                } else {
                    Ok(None)
                }
            }
            Err(e) => Err(e),
        }
    }

    async fn load_by_name(
        &self,
        tenant_id: &str,
        card_name: &str,
    ) -> Result<Option<CachedAgentCard>, sqlx::Error> {
        match self
            .load_card_row(
                r#"SELECT id, tenant_id, name, version, card_json, trust_tier, enabled, revoked_at,
                          extended_card_json, signature, signature_algorithm, signed_by, signed_at,
                          tenant_limits, tenant_governance
                   FROM agent_cards
                   WHERE name = $1 AND tenant_id = $2
                   LIMIT 1"#,
                card_name,
                tenant_id,
            )
            .await
        {
            Ok(row) => {
                if let Some(mut card) = row {
                    card.capabilities = self.load_capabilities(&card.id).await?;
                    Ok(Some(card))
                } else {
                    Ok(None)
                }
            }
            Err(e) => Err(e),
        }
    }

    /// The card row loader.
    async fn load_card_row(
        &self,
        sql: &str,
        key: &str,
        tenant_id: &str,
    ) -> Result<Option<CachedAgentCard>, sqlx::Error> {
        type ModernRow = (
            String,                // id
            String,                // tenant_id
            String,                // name
            String,                // version
            Value,                 // card_json
            String,                // trust_tier
            bool,                  // enabled
            Option<DateTime<Utc>>, // revoked_at
            Option<Value>,         // extended_card_json
            Option<String>,        // signature
            Option<String>,        // signature_algorithm
            Option<String>,        // signed_by
            Option<DateTime<Utc>>, // signed_at
            Option<Value>,         // tenant_limits
            Option<Value>,         // tenant_governance
        );
        let row: Option<ModernRow> = sqlx::query_as::<_, ModernRow>(sql)
            .bind(key)
            .bind(tenant_id)
            .fetch_optional(&self.db)
            .await?;

        Ok(row.map(|r| CachedAgentCard {
            id: r.0,
            tenant_id: r.1,
            name: r.2,
            version: r.3,
            card_json: r.4,
            trust_tier: r.5,
            enabled: r.6,
            revoked_at: r.7,
            extended_card_json: r.8,
            signature: r.9,
            signature_algorithm: r.10,
            signed_by: r.11,
            signed_at: r.12,
            tenant_limits: r.13,
            tenant_governance: r.14,
            capabilities: BTreeMap::new(),
        }))
    }

    async fn load_capabilities(
        &self,
        card_id: &str,
    ) -> Result<BTreeMap<String, AgentCardCapability>, sqlx::Error> {
        // The handler columns are what the dispatcher picks a capability's
        // handler by.
        type CapRow = (
            String,         // id
            String,         // capability_name
            bool,           // enabled
            Option<i32>,    // timeout_secs
            Option<Value>,  // data_classification
            Option<String>, // handler_type
            Option<String>, // handler_ref
            Option<i32>,    // rate_limit_per_hour
        );
        let rows: Vec<CapRow> = sqlx::query_as::<_, CapRow>(
            r#"SELECT id, capability_name, enabled, timeout_secs, data_classification,
                      handler_type, handler_ref, rate_limit_per_hour
               FROM agent_card_capabilities
               WHERE agent_card_id = $1"#,
        )
        .bind(card_id)
        .fetch_all(&self.db)
        .await?;

        Ok(rows
            .into_iter()
            .map(|r| {
                (
                    r.1.clone(),
                    AgentCardCapability {
                        id: r.0,
                        capability_name: r.1,
                        enabled: r.2,
                        timeout_secs: r.3,
                        data_classification: r.4,
                        handler_type: r.5,
                        handler_ref: r.6,
                        rate_limit_per_hour: r.7,
                    },
                )
            })
            .collect())
    }
}

#[async_trait::async_trait]
impl ResourceCache for AgentCardCache {
    fn name(&self) -> &str {
        "agent_card_cache"
    }

    /// Per-card eviction — clears both the id-key and any name-key
    /// entry that points at the same card. We can't compute the
    /// name-key without first reading the row, so we walk the
    /// name index by tenant prefix and drop entries whose Arc id
    /// matches; that avoids a DB round-trip just to find the
    /// stale name. With ~5K cards per tenant the prefix walk is
    /// O(tenant_card_count) and runs only on admin writes, never
    /// on the request path.
    async fn invalidate(&self, tenant_id: &str, resource_id: &str) {
        self.generation.bump();
        let id_key = format!("{tenant_id}:id:{resource_id}");
        self.by_id.invalidate(&id_key).await;
        // The name entry may outlive the id entry (capacity eviction drops
        // them separately), so find it by the card it holds rather than
        // through the id entry.
        let name_prefix = format!("{tenant_id}:name:");
        let id = resource_id.to_string();
        let _ = self
            .by_name
            .invalidate_entries_if(move |k, v| k.starts_with(&name_prefix) && v.id == id);
        self.by_name.run_pending_tasks().await;
        tracing::debug!(tenant_id, resource_id, "AgentCardCache entry invalidated");
    }

    async fn invalidate_tenant(&self, tenant_id: &str) {
        // Force pending maintenance so the next lookup is guaranteed
        // to miss — moka's predicate eviction is otherwise lazy.
        // See the matching note on `ModelCache::invalidate_tenant`.
        self.generation.bump();
        let id_prefix = format!("{tenant_id}:id:");
        let _ = self
            .by_id
            .invalidate_entries_if(move |k, _| k.starts_with(&id_prefix));
        let name_prefix = format!("{tenant_id}:name:");
        let _ = self
            .by_name
            .invalidate_entries_if(move |k, _| k.starts_with(&name_prefix));
        self.by_id.run_pending_tasks().await;
        self.by_name.run_pending_tasks().await;
        tracing::debug!(tenant_id, "AgentCardCache tenant invalidated");
    }

    async fn invalidate_all(&self) {
        self.generation.bump();
        self.by_id.invalidate_all();
        self.by_name.invalidate_all();
        tracing::debug!("AgentCardCache fully invalidated");
    }
}

// ---------------------------------------------------------------------------
// Test-only helpers
// ---------------------------------------------------------------------------

#[cfg(any(test, feature = "db-tests"))]
impl AgentCardCache {
    pub(super) async fn _peek_by_id(
        &self,
        tenant_id: &str,
        card_id: &str,
    ) -> Option<Arc<CachedAgentCard>> {
        self.by_id.get(&format!("{tenant_id}:id:{card_id}")).await
    }

    pub(super) async fn _peek_by_name(
        &self,
        tenant_id: &str,
        card_name: &str,
    ) -> Option<Arc<CachedAgentCard>> {
        self.by_name
            .get(&format!("{tenant_id}:name:{card_name}"))
            .await
    }

    /// Inserts a synthesised card under BOTH the id-key and the
    /// name-key (sharing the same Arc), mirroring the production
    /// `insert_both` path. Tests rely on this to verify dual-key
    /// invalidation.
    pub(super) async fn insert_card_for_test(
        &self,
        tenant_id: &str,
        card_id: &str,
        card_name: &str,
    ) {
        let card = Arc::new(CachedAgentCard {
            id: card_id.to_string(),
            tenant_id: tenant_id.to_string(),
            name: card_name.to_string(),
            version: "1.0".to_string(),
            card_json: serde_json::json!({}),
            trust_tier: "trusted".to_string(),
            enabled: true,
            revoked_at: None,
            extended_card_json: None,
            signature: None,
            signature_algorithm: None,
            signed_by: None,
            signed_at: None,
            tenant_limits: None,
            tenant_governance: None,
            capabilities: BTreeMap::new(),
        });
        let started = self.generation.current();
        self.insert_both(&card, started).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::cache::ResourceCache;
    use sqlx::postgres::PgPoolOptions;

    fn build_cache() -> AgentCardCache {
        let pool = PgPoolOptions::new()
            .max_connections(1)
            .connect_lazy("postgres://test:test@localhost:5432/test")
            .expect("connect_lazy never fails on a syntactically-valid URL");
        AgentCardCache::new(pool)
    }

    // -----------------------------------------------------------------------
    // Pre-existing sanity tests, kept for coverage of the data shape
    // -----------------------------------------------------------------------

    #[test]
    fn capability_map_inserts_by_name() {
        let mut caps: BTreeMap<String, AgentCardCapability> = BTreeMap::new();
        caps.insert(
            "billing.refund".into(),
            AgentCardCapability {
                id: "cap-1".into(),
                capability_name: "billing.refund".into(),
                enabled: true,
                timeout_secs: Some(30),
                rate_limit_per_hour: None,
                data_classification: None,
                handler_type: Some("noop".into()),
                handler_ref: None,
            },
        );
        assert_eq!(caps.len(), 1);
        assert_eq!(caps.get("billing.refund").unwrap().id, "cap-1");
    }

    #[test]
    fn cached_agent_card_clones_cheaply() {
        let card = CachedAgentCard {
            id: "card-1".into(),
            tenant_id: "t1".into(),
            name: "billing-agent".into(),
            version: "1.0".into(),
            card_json: serde_json::json!({}),
            trust_tier: "trusted".into(),
            enabled: true,
            revoked_at: None,
            extended_card_json: None,
            signature: None,
            signature_algorithm: None,
            signed_by: None,
            signed_at: None,
            tenant_limits: None,
            tenant_governance: None,
            capabilities: BTreeMap::new(),
        };
        let arc = Arc::new(card);
        let clone = Arc::clone(&arc);
        assert_eq!(clone.id, "card-1");
        assert_eq!(Arc::strong_count(&arc), 2);
    }

    // -----------------------------------------------------------------------
    // Dual-key indexing — the unique feature of AgentCardCache. id-key
    // and name-key entries point at the same Arc and must invalidate
    // together; otherwise an admin renames a card and discovery
    // returns the old name from the name-index while invocation by id
    // returns the new name.
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn insert_populates_both_keys_with_same_arc() {
        let cache = build_cache();
        cache
            .insert_card_for_test("tenant-a", "card-1", "billing-agent")
            .await;

        let by_id = cache._peek_by_id("tenant-a", "card-1").await.unwrap();
        let by_name = cache
            ._peek_by_name("tenant-a", "billing-agent")
            .await
            .unwrap();

        assert!(
            Arc::ptr_eq(&by_id, &by_name),
            "id-key and name-key must point at the same Arc — \
             otherwise the cached card's identity diverges between \
             discovery (by name) and invocation (by id)"
        );
    }

    #[tokio::test]
    async fn invalidate_drops_both_id_and_name_keys() {
        // Per the inherent ResourceCache impl, invalidate(tenant, id)
        // walks the captured Arc back to its name and drops the
        // name-key entry too. This test guards that contract.
        let cache = build_cache();
        cache
            .insert_card_for_test("tenant-a", "card-1", "billing-agent")
            .await;
        cache
            .insert_card_for_test("tenant-a", "card-2", "support-agent")
            .await;

        cache.invalidate("tenant-a", "card-1").await;

        // card-1 cleared from BOTH indexes
        assert!(cache._peek_by_id("tenant-a", "card-1").await.is_none());
        assert!(
            cache
                ._peek_by_name("tenant-a", "billing-agent")
                .await
                .is_none(),
            "invalidating by id must also drop the matching name-key \
             entry — otherwise discovery serves a card the invoker \
             can't reach"
        );
        // card-2 untouched
        assert!(cache._peek_by_id("tenant-a", "card-2").await.is_some());
        assert!(
            cache
                ._peek_by_name("tenant-a", "support-agent")
                .await
                .is_some()
        );
    }

    #[tokio::test]
    async fn invalidate_tenant_drops_both_indexes() {
        let cache = build_cache();
        cache
            .insert_card_for_test("tenant-a", "card-1", "billing")
            .await;
        cache
            .insert_card_for_test("tenant-a", "card-2", "support")
            .await;
        cache
            .insert_card_for_test("tenant-b", "card-1", "billing")
            .await;

        cache.invalidate_tenant("tenant-a").await;

        assert!(cache._peek_by_id("tenant-a", "card-1").await.is_none());
        assert!(cache._peek_by_name("tenant-a", "billing").await.is_none());
        assert!(cache._peek_by_id("tenant-a", "card-2").await.is_none());
        assert!(cache._peek_by_name("tenant-a", "support").await.is_none());
        // tenant-b untouched
        assert!(cache._peek_by_id("tenant-b", "card-1").await.is_some());
        assert!(cache._peek_by_name("tenant-b", "billing").await.is_some());
    }

    #[tokio::test]
    async fn key_prefix_does_not_collide_with_substring_tenants() {
        // 'acme' inv must NOT evict 'acme-corp' even though both
        // include 'acme:id:' as the prefix-fragment if we were
        // careless. With the explicit ":id:" / ":name:" segment,
        // the prefix "acme:id:" is unique to tenant 'acme'.
        let cache = build_cache();
        cache
            .insert_card_for_test("acme", "card-1", "billing")
            .await;
        cache
            .insert_card_for_test("acme-corp", "card-1", "billing")
            .await;

        cache.invalidate_tenant("acme").await;

        assert!(cache._peek_by_id("acme", "card-1").await.is_none());
        assert!(cache._peek_by_id("acme-corp", "card-1").await.is_some());
    }

    #[tokio::test]
    async fn id_key_and_name_key_namespaces_do_not_collide() {
        // Sanity: a card named "card-1" must not collide with a card
        // whose id is "card-1" — the explicit ":id:" / ":name:"
        // segment in the key format prevents this.
        let cache = build_cache();
        // Card with id="card-1", name="alpha"
        cache.insert_card_for_test("t1", "card-1", "alpha").await;
        // Card with id="beta", name="card-1"  — name and id collide
        cache.insert_card_for_test("t1", "beta", "card-1").await;

        // Both should exist independently
        let by_id_1 = cache._peek_by_id("t1", "card-1").await.unwrap();
        let by_name_card1 = cache._peek_by_name("t1", "card-1").await.unwrap();
        assert_eq!(by_id_1.name, "alpha");
        assert_eq!(by_name_card1.id, "beta");
    }

    // -----------------------------------------------------------------------
    // Clone shares state
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn clone_shares_state_with_original() {
        let cache = build_cache();
        cache
            .insert_card_for_test("tenant-a", "card-1", "billing")
            .await;

        let cloned = cache.clone();
        cloned.invalidate("tenant-a", "card-1").await;

        assert!(cache._peek_by_id("tenant-a", "card-1").await.is_none());
        assert!(cache._peek_by_name("tenant-a", "billing").await.is_none());
    }

    // -----------------------------------------------------------------------
    // Whole-cache reset
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn invalidate_all_drops_both_indexes_for_all_tenants() {
        let cache = build_cache();
        cache.insert_card_for_test("a", "c1", "n1").await;
        cache.insert_card_for_test("b", "c2", "n2").await;

        cache.invalidate_all().await;
        cache.by_id.run_pending_tasks().await;
        cache.by_name.run_pending_tasks().await;

        assert!(cache._peek_by_id("a", "c1").await.is_none());
        assert!(cache._peek_by_name("a", "n1").await.is_none());
        assert!(cache._peek_by_id("b", "c2").await.is_none());
        assert!(cache._peek_by_name("b", "n2").await.is_none());
    }
}
