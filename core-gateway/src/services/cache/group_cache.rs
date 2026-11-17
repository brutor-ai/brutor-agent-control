// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Resource Group Cache
//!
//! Materializes the "effective" view of a resource group including all
//! inheritance from parent groups. This eliminates the most expensive
//! hot-path queries: access control chain walks (4-8 DB queries per request).
//!
//! On cache miss, loads the full group hierarchy and computes effective
//! model IDs, MCP server IDs, skill IDs, limits, and governance policies.
//! This is expensive (~10-20 queries) but happens once per cache miss.
//! Subsequent requests for the same group are served from memory.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::time::Duration;

use moka::future::Cache;
use sqlx::PgPool;

use crate::models::db::ResourceGroup;

use super::{Generation, ResourceCache, insert_fresh};

/// Default TTL (safety net — primary invalidation is push-based).
const DEFAULT_TTL_SECS: u64 = 300; // 5 minutes
const DEFAULT_MAX_GROUPS: u64 = 5_000;
const DEFAULT_MAX_USER_GROUPS: u64 = 50_000;

// ---------------------------------------------------------------------------
// EffectiveGroupData — materialized view of a group
// ---------------------------------------------------------------------------

/// Fully materialized group state — everything needed for access control,
/// quota enforcement, and governance on the request hot path.
///
/// Computed once on cache miss by walking the full parent chain and
/// aggregating resources from all ancestor groups.
#[derive(Debug, Clone)]
pub struct EffectiveGroupData {
    /// The group itself
    pub group: ResourceGroup,
    /// All LLM model IDs accessible (direct + inherited from parent chain)
    pub effective_model_ids: HashSet<String>,
    /// All MCP server IDs accessible (direct + inherited)
    pub effective_mcp_ids: HashSet<String>,
    /// All agent skill IDs accessible (direct + inherited)
    pub effective_skill_ids: HashSet<String>,
    /// All A2A agent card IDs accessible (direct + inherited).
    /// Mirrors the other `effective_*_ids` fields and lets the A2A
    /// proxy routes reject invocations against cards the caller's
    /// group isn't allowed to see without a fresh DB round-trip.
    pub effective_agent_card_ids: HashSet<String>,
    /// Effective A2A global limits, merged restrictively across the
    /// parent chain. `None` = no per-group rule configured anywhere
    /// above this group; the route then relies on the absolute proxy
    /// cap alone.
    pub effective_a2a_limits: Option<serde_json::Value>,
    /// The A2A policy document of every group in the chain that states
    /// one (this group and each ancestor), unmerged: the evaluation judges
    /// each level on its own and takes the strictest verdict
    /// (`services::a2a_governance`). Empty = no level states a policy.
    pub a2a_governance_levels: Vec<serde_json::Value>,
    /// Member user IDs (direct membership, not inherited)
    pub member_user_ids: HashSet<i32>,
    /// Ancestor group IDs (parent chain from this group to root)
    pub ancestor_ids: Vec<String>,
    /// What the registered loaders added: the
    /// per-group settings of the features that are composed in. Empty in a
    /// gateway composed with none.
    pub extras: GroupExtras,
}

// ---------------------------------------------------------------------------
// The loader seam
// ---------------------------------------------------------------------------

/// What an extension's loader added to a group: one value per type, set
/// while the group is loaded and immutable afterwards. The group cache does
/// not know the types; whoever registered the loader reads its own
/// ([`GroupExtras::get`]).
///
/// The extras are part of the effective group: loaded in the same load,
/// cached in the same entry (one TTL), dropped by the same invalidation.
#[derive(Clone, Default)]
pub struct GroupExtras {
    values: HashMap<std::any::TypeId, Arc<dyn std::any::Any + Send + Sync>>,
    /// The type names, in insertion order, for diagnostics.
    names: Vec<&'static str>,
}

impl GroupExtras {
    /// Set the one value of type `T`. A second value of the same type is
    /// two loaders writing one setting, so it replaces nothing and is
    /// reported: the loader that sees `false` must fail its load.
    #[must_use = "a value that was not set is a loader conflict: fail the load"]
    pub fn insert<T: std::any::Any + Send + Sync>(&mut self, value: T) -> bool {
        let id = std::any::TypeId::of::<T>();
        if self.values.contains_key(&id) {
            return false;
        }
        self.values.insert(id, Arc::new(value));
        self.names.push(std::any::type_name::<T>());
        true
    }

    /// The value of type `T`, if a loader set one for this group.
    pub fn get<T: std::any::Any + Send + Sync>(&self) -> Option<&T> {
        self.values
            .get(&std::any::TypeId::of::<T>())
            .and_then(|value| value.downcast_ref::<T>())
    }

    pub fn is_empty(&self) -> bool {
        self.values.is_empty()
    }

    /// The type names of the values present, in insertion order.
    pub fn type_names(&self) -> &[&'static str] {
        &self.names
    }
}

impl std::fmt::Debug for GroupExtras {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("GroupExtras")
            .field("types", &self.names)
            .finish()
    }
}

/// What a loader is told about the group being loaded.
#[derive(Debug, Clone, Copy)]
pub struct GroupLoad<'a> {
    pub tenant_id: &'a str,
    pub group: &'a ResourceGroup,
    /// The group and every ancestor up the parent chain, whatever their
    /// inheritance settings.
    pub chain_group_ids: &'a [String],
    /// The group and the ancestors whose resources it inherits
    /// (`inherit_resources`): what the group's own resources are read from.
    pub resource_group_ids: &'a [String],
}

/// A contribution to the group load: reads the settings one feature keeps
/// per group and sets them on the group's extras.
///
/// **Fail closed.** The loader runs inside the load. An error it returns
/// fails the load as a whole: no group is built, none is cached, and the
/// enforcement paths refuse (`try_get_effective_group`). A loader never
/// turns a failed read into "no settings": to the feature that would be a
/// group with the feature off, served for the whole TTL.
#[async_trait::async_trait]
pub trait GroupLoader: Send + Sync + 'static {
    /// Unique, stable name.
    fn name(&self) -> &'static str;

    /// The Enterprise feature the loader belongs to.
    fn feature(&self) -> &'static str;

    async fn load(
        &self,
        db: &PgPool,
        group: &GroupLoad<'_>,
        extras: &mut GroupExtras,
    ) -> Result<(), sqlx::Error>;
}

/// Why a loader was not registered.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GroupLoaderNameTaken(pub String);

impl std::fmt::Display for GroupLoaderNameTaken {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "group loader name {:?} is empty or already registered",
            self.0
        )
    }
}

impl std::error::Error for GroupLoaderNameTaken {}

// ---------------------------------------------------------------------------
// ResourceGroupCache
// ---------------------------------------------------------------------------

/// In-memory cache for resource group data with full inheritance resolution.
///
/// `Clone` shares the underlying moka caches — see the matching note on
/// `ModelCache`. The cache invalidator must hold the same instance the
/// request pipeline uses or pub/sub messages evict nothing.
#[derive(Clone)]
pub struct ResourceGroupCache {
    /// `{tenant_id}:{group_id}` → EffectiveGroupData
    groups: Cache<String, Arc<EffectiveGroupData>>,
    /// `{tenant_id}:{user_id}` → Vec<group_id> (user's group memberships)
    user_groups: Cache<String, Arc<Vec<String>>>,
    /// `{api_key_hash}` → (tenant_id, Option<group_id>)
    api_key_groups: Cache<String, Arc<(String, Option<String>)>>,
    /// `{tenant_id}` → agent card id → the AI System behind it, from the
    /// tenant's declared `shared_service` edges (see
    /// [`Self::card_service_system`]). Dropped with the tenant's other
    /// group entries, so a `resource_group` / `ai_system_dependency`
    /// invalidation (Redis pub/sub or the `brutor:cache:invalidate` NOTIFY)
    /// reaches it like every other RG-derived fact.
    card_services: Cache<String, Arc<HashMap<String, String>>>,
    db: PgPool,
    /// The loader contributions, run in registration order inside
    /// every group load. Shared by every clone of this cache, so a loader
    /// registered on the state's cache is the invalidator's too.
    loaders: Arc<std::sync::RwLock<Vec<Arc<dyn GroupLoader>>>>,
    /// Bumped by every invalidation of any sub-cache; loads insert with
    /// [`insert_fresh`].
    generation: Generation,
    // hit/miss counters reported to the operator
    // console. Combined across all three internal caches because the
    // UI surfaces them as one logical 'group cache' layer.
    // Test-only `_peek_*` methods deliberately do NOT increment these.
    hits: Arc<std::sync::atomic::AtomicU64>,
    misses: Arc<std::sync::atomic::AtomicU64>,
}

impl ResourceGroupCache {
    pub fn new(db: PgPool) -> Self {
        // `support_invalidation_closures` is REQUIRED for
        // `invalidate_entries_if` to actually evict — see the matching
        // note on `ModelCache::new`.
        Self {
            groups: Cache::builder()
                .max_capacity(DEFAULT_MAX_GROUPS)
                .time_to_live(Duration::from_secs(DEFAULT_TTL_SECS))
                .support_invalidation_closures()
                .build(),
            user_groups: Cache::builder()
                .max_capacity(DEFAULT_MAX_USER_GROUPS)
                .time_to_live(Duration::from_secs(DEFAULT_TTL_SECS))
                .support_invalidation_closures()
                .build(),
            api_key_groups: Cache::builder()
                .max_capacity(DEFAULT_MAX_USER_GROUPS)
                .time_to_live(Duration::from_secs(DEFAULT_TTL_SECS))
                .support_invalidation_closures()
                .build(),
            card_services: Cache::builder()
                .max_capacity(DEFAULT_MAX_GROUPS)
                .time_to_live(Duration::from_secs(DEFAULT_TTL_SECS))
                .build(),
            db,
            loaders: Arc::new(std::sync::RwLock::new(Vec::new())),
            generation: Generation::default(),
            hits: Arc::new(std::sync::atomic::AtomicU64::new(0)),
            misses: Arc::new(std::sync::atomic::AtomicU64::new(0)),
        }
    }

    /// Add a loader contribution: from now on it runs inside every group
    /// load, after the built-in reads and after the loaders registered
    /// before it. Refused when the name is empty or taken. Meant for the
    /// assembly, before the state is shared; registering bumps the
    /// generation all the same, so a load in flight cannot cache a group
    /// that lacks the new loader's extras.
    pub fn register_loader(
        &self,
        loader: Arc<dyn GroupLoader>,
    ) -> Result<(), GroupLoaderNameTaken> {
        let mut loaders = self
            .loaders
            .write()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let name = loader.name();
        if name.is_empty() || loaders.iter().any(|l| l.name() == name) {
            return Err(GroupLoaderNameTaken(name.to_string()));
        }
        loaders.push(loader);
        self.generation.bump();
        Ok(())
    }

    /// `(name, feature)` of every registered loader, in registration order.
    pub fn loader_names(&self) -> Vec<(&'static str, &'static str)> {
        self.registered_loaders()
            .iter()
            .map(|l| (l.name(), l.feature()))
            .collect()
    }

    /// The loaders as registered now.
    fn registered_loaders(&self) -> Vec<Arc<dyn GroupLoader>> {
        self.loaders
            .read()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone()
    }

    /// Get the effective group data for a group (cached).
    /// Loads and materializes the full parent chain on cache miss.
    ///
    /// A load failure reads as `None` here and is never cached. Enforcement
    /// paths must use [`Self::try_get_effective_group`] instead and fail
    /// closed: to them an unreadable group is not an absent one.
    pub async fn get_effective_group(
        &self,
        tenant_id: &str,
        group_id: &str,
    ) -> Option<Arc<EffectiveGroupData>> {
        match self.try_get_effective_group(tenant_id, group_id).await {
            Ok(data) => data,
            Err(e) => {
                tracing::error!(error = %e, tenant_id, group_id, "effective group load failed; not cached");
                None
            }
        }
    }

    /// The refusal message when a caller's group governance cannot be loaded.
    pub const UNAVAILABLE_MESSAGE: &'static str = "Refused: the caller's resource-group governance could not be loaded, so it cannot be enforced";

    /// [`Self::get_effective_group`], reporting a load failure as `Err`
    /// rather than folding it into "no such group".
    ///
    /// Every query of the load propagates its error, so a partial group — an
    /// ancestry cut short, a dropped A2A governance row — is never built, let
    /// alone cached: that would hand out looser limits and policies than the
    /// operator configured for the whole cache TTL.
    pub async fn try_get_effective_group(
        &self,
        tenant_id: &str,
        group_id: &str,
    ) -> Result<Option<Arc<EffectiveGroupData>>, sqlx::Error> {
        let key = format!("{tenant_id}:{group_id}");

        if let Some(data) = self.groups.get(&key).await {
            self.hits.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            return Ok(Some(data));
        }
        self.misses
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);

        // Cache miss — load and materialize
        let started = self.generation.current();
        let Some(data) = self.load_effective_group(tenant_id, group_id).await? else {
            return Ok(None);
        };
        let data = Arc::new(data);
        insert_fresh(&self.groups, key, data.clone(), &self.generation, started).await;

        Ok(Some(data))
    }

    /// Get all group IDs a user belongs to (cached).
    ///
    /// A database error is returned and not cached. It used to read
    /// as "member of no group", which is closed for access but OPEN for the
    /// autonomy gate: no group, no suspension.
    pub async fn get_user_group_ids(
        &self,
        tenant_id: &str,
        user_id: &str,
    ) -> Result<Vec<String>, sqlx::Error> {
        let key = format!("{tenant_id}:{user_id}");

        if let Some(ids) = self.user_groups.get(&key).await {
            self.hits.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            return Ok(ids.as_ref().clone());
        }
        self.misses
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);

        // Union direct + EndUserGroup-mediated access.
        // NOTE on cache invalidation: this cache is keyed on
        // (tenant, user). It is invalidated by the existing
        // `invalidate_user(...)` calls when an admin mutates direct
        // RG memberships. EndUserGroup membership mutations from the
        // admin endpoints must also invalidate via the same path — wire
        // that in cache_invalidation.rs next.
        let started = self.generation.current();
        let groups: Result<Vec<(String,)>, sqlx::Error> = sqlx::query_as(
            r#"SELECT rg.id
               FROM resource_groups rg
               WHERE rg.tenant_id = $2
                 AND rg.is_active = true
                 AND rg.id IN (
                     SELECT group_id
                     FROM resource_group_memberships
                     WHERE user_id = $1::int
                       AND tenant_id = $2
                     UNION
                     SELECT rgeug.resource_group_id
                     FROM resource_group_end_user_groups rgeug
                     JOIN end_user_group_members eugm
                       ON eugm.end_user_group_id = rgeug.end_user_group_id
                     WHERE eugm.end_user_id = $1::int
                 )"#,
        )
        .bind(user_id)
        .bind(tenant_id)
        .fetch_all(&self.db)
        .await;
        // Returned, not cached: an error answer would lock the user out
        // (or, for autonomy, let them past a suspension) for the whole TTL.
        let groups = groups?;

        let ids: Vec<String> = groups.into_iter().map(|(id,)| id).collect();
        insert_fresh(
            &self.user_groups,
            key,
            Arc::new(ids.clone()),
            &self.generation,
            started,
        )
        .await;

        Ok(ids)
    }

    /// Get tenant_id and group_id for an API key hash (cached).
    /// A database error is returned, never read as "unknown key".
    pub async fn get_api_key_tenant(
        &self,
        key_hash: &str,
    ) -> Result<Option<(String, Option<String>)>, sqlx::Error> {
        if let Some(data) = self.api_key_groups.get(&key_hash.to_string()).await {
            self.hits.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            return Ok(Some(data.as_ref().clone()));
        }
        self.misses
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);

        let started = self.generation.current();
        let result: Option<(String, Option<String>)> = sqlx::query_as(
            "SELECT tenant_id, group_id FROM api_keys WHERE key_hash = $1 AND enabled = true AND revoked = false",
        )
        .bind(key_hash)
        .fetch_optional(&self.db)
        .await?;

        if let Some(ref data) = result {
            insert_fresh(
                &self.api_key_groups,
                key_hash.to_string(),
                Arc::new(data.clone()),
                &self.generation,
                started,
            )
            .await;
        }

        Ok(result)
    }

    /// The AI System an agent card resolves to, if any (cached per tenant).
    ///
    /// Nothing in the schema links a card to the system behind it; a card
    /// resolves only through a declared `shared_service` edge that pairs
    /// them — ANYWHERE in the tenant. This is the control plane's rule
    /// (`CompositionIndex.card_system` in `ai_system_composition.py`), edge
    /// for edge: edges read in `(ai_system_id, declared_at)` order and the
    /// FIRST edge naming a card wins. The accepted-caller gate and the
    /// composition view therefore always agree on which service a card is.
    ///
    /// A lookup failure is returned, not cached and not read as "no
    /// service": the caller decides how to fail (the gate fails closed).
    pub async fn card_service_system(
        &self,
        tenant_id: &str,
        card_id: &str,
    ) -> Result<Option<String>, sqlx::Error> {
        if let Some(map) = self.card_services.get(tenant_id).await {
            self.hits.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            return Ok(map.get(card_id).cloned());
        }
        self.misses
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let started = self.generation.current();
        let rows: Vec<(String, String)> = sqlx::query_as(
            "SELECT DISTINCT ON (agent_card_id) agent_card_id, depends_on_system_id \
               FROM ai_system_dependencies \
              WHERE tenant_id = $1 AND role = 'shared_service' \
                AND agent_card_id IS NOT NULL AND depends_on_system_id IS NOT NULL \
              ORDER BY agent_card_id, ai_system_id, declared_at, id",
        )
        .bind(tenant_id)
        .fetch_all(&self.db)
        .await?;
        let map: HashMap<String, String> = rows.into_iter().collect();
        let hit = map.get(card_id).cloned();
        insert_fresh(
            &self.card_services,
            tenant_id.to_string(),
            Arc::new(map),
            &self.generation,
            started,
        )
        .await;
        Ok(hit)
    }

    /// Check if a model is accessible in a group (using cached effective data).
    pub async fn model_accessible(&self, tenant_id: &str, group_id: &str, model_id: &str) -> bool {
        if let Some(data) = self.get_effective_group(tenant_id, group_id).await {
            return data.effective_model_ids.contains(model_id);
        }
        false
    }

    /// Snapshot for the cache stats fan-out.
    /// `entries` sums all three internal moka caches.
    pub fn stats(&self) -> crate::services::cache::CacheStatsSnapshot {
        crate::services::cache::CacheStatsSnapshot {
            hits: self.hits.load(std::sync::atomic::Ordering::Relaxed),
            misses: self.misses.load(std::sync::atomic::Ordering::Relaxed),
            entries: self.groups.entry_count()
                + self.user_groups.entry_count()
                + self.api_key_groups.entry_count()
                + self.card_services.entry_count(),
        }
    }

    // -----------------------------------------------------------------------
    // Internal: load and materialize effective group data from DB
    // -----------------------------------------------------------------------

    async fn load_effective_group(
        &self,
        tenant_id: &str,
        group_id: &str,
    ) -> Result<Option<EffectiveGroupData>, sqlx::Error> {
        // 1. Load the group itself
        let group: Option<ResourceGroup> = sqlx::query_as(
            "SELECT *, group_type::text AS group_type FROM resource_groups WHERE id = $1 AND tenant_id = $2 AND is_active = true",
        )
        .bind(group_id)
        .bind(tenant_id)
        .fetch_optional(&self.db)
        .await?;
        let Some(group) = group else {
            return Ok(None);
        };

        // 2. Walk the parent chain ONCE, producing two views of the ancestry:
        //
        //   * `all_group_ids`  — self + every ancestor, unconditionally. Used for
        //     limits / governance / A2A settings, which are ALWAYS inherited
        //     (restrictive-wins) regardless of the `inherit_from_parent` flag.
        //
        //   * `resource_group_ids` — self + only the ancestors that are actually
        //     reachable through resource inheritance. A group inherits resources
        //     (models, MCP servers, skills, agent cards, KB collections) from an
        //     ancestor only while every node from this group up to (but not
        //     including) that ancestor has `inherit_from_parent = true`. This
        //     mirrors `usage::model_in_group_chain` /
        //     `mcp_server_in_group_chain` EXACTLY, so the materialized cache
        //     agrees with the authoritative per-request access checks. (Before
        //     this, the cache walked unconditionally — leaking a parent's agent
        //     cards / models to a child with `inherit_from_parent = false`.)
        let mut ancestor_ids = Vec::new();
        let mut all_group_ids = vec![group_id.to_string()];
        let mut resource_group_ids = vec![group_id.to_string()];

        // `gate` is the AND of the `inherit_from_parent` flags of every node
        // walked so far (this group, then each ancestor). It tells us whether the
        // NEXT ancestor we discover is still reachable via resource inheritance.
        let mut gate = true;
        let mut current_id = group_id.to_string();

        for _ in 0..10 {
            let parent: Option<(Option<String>, bool)> = sqlx::query_as(
                "SELECT parent_group_id, inherit_from_parent FROM resource_groups WHERE id = $1 AND tenant_id = $2",
            )
            .bind(&current_id)
            .bind(tenant_id)
            .fetch_optional(&self.db)
            .await?;

            match parent {
                Some((Some(pid), current_inherits)) => {
                    // `current_inherits` is the flag of `current_id` — i.e.
                    // whether `current_id` inherits from `pid`. Fold it into the
                    // gate; if it (or any node below) is false, `pid` and
                    // everything above it are NOT resource-inherited.
                    gate = gate && current_inherits;
                    ancestor_ids.push(pid.clone());
                    all_group_ids.push(pid.clone());
                    if gate {
                        resource_group_ids.push(pid.clone());
                    }
                    current_id = pid;
                }
                _ => break,
            }
        }

        // 4. Effective model IDs (direct + inherited)
        let model_rows: Vec<(String,)> = sqlx::query_as(
            "SELECT DISTINCT llm_model_id FROM resource_group_llm_models WHERE resource_group_id = ANY($1)",
        )
        .bind(&resource_group_ids)
        .fetch_all(&self.db)
        .await?;
        let effective_model_ids: HashSet<String> = model_rows.into_iter().map(|(id,)| id).collect();

        // Also include models from routing groups that are in the effective model set
        let routing_model_rows: Vec<(String,)> = sqlx::query_as(
            r#"SELECT DISTINCT m.id FROM llm_model m
               WHERE m.routing_group_id IN (
                   SELECT DISTINCT m2.routing_group_id FROM llm_model m2
                   WHERE m2.id = ANY($1) AND m2.routing_group_id IS NOT NULL
               )
               AND m.tenant_id = $2 AND m.enabled = true"#,
        )
        .bind(effective_model_ids.iter().cloned().collect::<Vec<_>>())
        .bind(tenant_id)
        .fetch_all(&self.db)
        .await?;
        let mut effective_model_ids = effective_model_ids;
        for (id,) in routing_model_rows {
            effective_model_ids.insert(id);
        }

        // 5. Effective MCP server IDs
        //
        // Resource groups don't link to MCP servers directly — the
        // join goes through `server_configs`, which wraps each
        // tenant's `mcp_server` with gateway-specific config (auth,
        // headers, capabilities, …). So a group has *server
        // configs*, and each config carries the underlying
        // `mcp_server_id`.
        //
        // Earlier code joined a non-existent `resource_group_mcp_
        // servers` table, which Postgres surfaced as
        // `ERROR: relation "resource_group_mcp_servers" does not
        // exist`. The control-plane schema only ever defined the
        // through-table `resource_group_server_configs` (see
        // `app/database/models/resource_group.py`).
        //
        // A Virtual MCP server's config has `virtual_server_id` and a NULL
        // `mcp_server_id`; it is not an MCP server of its own. Reading that
        // NULL into a `String` failed the whole group load, and every
        // enforcement path then refused the group's traffic.
        let mcp_rows: Vec<(String,)> = sqlx::query_as(
            r#"SELECT DISTINCT sc.mcp_server_id
               FROM resource_group_server_configs rgsc
               JOIN server_configs sc ON sc.id = rgsc.server_config_id
               WHERE rgsc.resource_group_id = ANY($1)
                 AND sc.mcp_server_id IS NOT NULL"#,
        )
        .bind(&resource_group_ids)
        .fetch_all(&self.db)
        .await?;
        let effective_mcp_ids: HashSet<String> = mcp_rows.into_iter().map(|(id,)| id).collect();

        // 6. Effective skill IDs
        let skill_rows: Vec<(String,)> = sqlx::query_as(
            "SELECT DISTINCT agent_skill_id FROM resource_group_agent_skills WHERE resource_group_id = ANY($1)",
        )
        .bind(&resource_group_ids)
        .fetch_all(&self.db)
        .await?;
        let effective_skill_ids: HashSet<String> = skill_rows.into_iter().map(|(id,)| id).collect();

        // 6b. Effective agent card IDs (A2A). Guarded select — on a
        // DB without it (e.g. a test harness that seeds a partial
        // schema only) the association table doesn't exist; fall back
        // to an empty set rather than crashing the cache load.
        let agent_card_rows: Result<Vec<(String,)>, _> = sqlx::query_as(
            "SELECT DISTINCT agent_card_id \
             FROM resource_group_agent_cards \
             WHERE resource_group_id = ANY($1)",
        )
        .bind(&resource_group_ids)
        .fetch_all(&self.db)
        .await;
        let effective_agent_card_ids: HashSet<String> = match agent_card_rows {
            Ok(rows) => rows.into_iter().map(|(id,)| id).collect(),
            Err(sqlx::Error::Database(e)) if e.code().as_deref() == Some("42P01") => HashSet::new(),
            Err(e) => return Err(e),
        };

        // 6c. Effective A2A limits + governance. Guarded against the
        // pre-a011 schema the same way as the agent-card bindings —
        // if the columns don't exist yet we treat the group as having
        // no A2A rule at all.
        let a2a_settings: Result<Vec<(Option<serde_json::Value>, Option<serde_json::Value>)>, _> =
            sqlx::query_as(
                "SELECT a2a_global_limits, a2a_global_governance \
             FROM resource_groups WHERE id = ANY($1)",
            )
            .bind(&all_group_ids)
            .fetch_all(&self.db)
            .await;
        let (effective_a2a_limits, a2a_governance_levels) = match a2a_settings {
            Ok(rows) => merge_a2a_settings_along_chain(&all_group_ids, rows),
            Err(sqlx::Error::ColumnNotFound(_)) => (None, Vec::new()),
            Err(sqlx::Error::Database(e))
                if e.code().as_deref() == Some("42703") // undefined_column
                    || e.code().as_deref() == Some("42P01") =>
            {
                (None, Vec::new())
            }
            // Not a missing column: a failed read of RESTRICTIVE settings must
            // not cache as "no A2A rule".
            Err(e) => return Err(e),
        };

        // 7. Direct member user IDs (not inherited)
        let member_rows: Vec<(i32,)> =
            sqlx::query_as("SELECT user_id FROM resource_group_memberships WHERE group_id = $1")
                .bind(group_id)
                .fetch_all(&self.db)
                .await?;
        let member_user_ids: HashSet<i32> = member_rows.into_iter().map(|(id,)| id).collect();

        // 8. What the composed features keep per group:
        //    every registered loader, in registration order, inside
        //    this load. `?` is the point: a loader that cannot read fails
        //    the load as a whole, so a group is never built, and never
        //    cached, without the extras of a feature that is composed in.
        let mut extras = GroupExtras::default();
        {
            let load = GroupLoad {
                tenant_id,
                group: &group,
                chain_group_ids: &all_group_ids,
                resource_group_ids: &resource_group_ids,
            };
            for loader in self.registered_loaders() {
                loader.load(&self.db, &load, &mut extras).await?;
            }
        }

        Ok(Some(EffectiveGroupData {
            group,
            effective_model_ids,
            effective_mcp_ids,
            effective_skill_ids,
            effective_agent_card_ids,
            effective_a2a_limits,
            a2a_governance_levels,
            member_user_ids,
            ancestor_ids,
            extras,
        }))
    }
}

// ---------------------------------------------------------------------------
// A2A settings along the chain. Limits are merged restrictively into one
// view (`usage::restrictive_merge`, the merge of every limit family: an
// inherited limit can be tightened, never removed). Policy documents are NOT
// merged: each level's own document is kept and `services::a2a_governance`
// judges every level and takes the strictest verdict.
// ---------------------------------------------------------------------------

/// The effective A2A limits of a chain and the policy document of each of
/// its levels.
///
/// Input rows come straight out of the resource_groups table; order is the
/// order PG returned them, which may not be root-to-self. The limits merge
/// is the same in either order for the limits it keeps (the smallest
/// stated number, bool OR, a level's `null` or unreadable value never
/// replacing another's), and the policy levels are judged independently.
///
/// Takes `group_ids` only so we can short-circuit when there are no
/// rows; in practice the caller already filtered by these ids.
fn merge_a2a_settings_along_chain(
    _group_ids: &[String],
    rows: Vec<(Option<serde_json::Value>, Option<serde_json::Value>)>,
) -> (Option<serde_json::Value>, Vec<serde_json::Value>) {
    use crate::services::usage::{LimitFamily, restrictive_merge};
    let mut effective_limits: Option<serde_json::Value> = None;
    let mut governance_levels: Vec<serde_json::Value> = Vec::new();

    for (limits, gov) in rows {
        if let Some(v) = limits.filter(|v| !v.is_null()) {
            effective_limits = Some(match effective_limits.take() {
                None => v,
                Some(mut base) => {
                    restrictive_merge(&mut base, &v, LimitFamily::A2a);
                    base
                }
            });
        }
        if let Some(v) = gov.filter(|v| !v.is_null()) {
            governance_levels.push(v);
        }
    }
    (effective_limits, governance_levels)
}

#[async_trait::async_trait]
impl ResourceCache for ResourceGroupCache {
    fn name(&self) -> &str {
        "resource_group_cache"
    }

    async fn invalidate(&self, tenant_id: &str, resource_id: &str) {
        self.generation.bump();
        let key = format!("{tenant_id}:{resource_id}");
        self.groups.invalidate(&key).await;
        tracing::debug!(
            tenant_id,
            resource_id,
            "ResourceGroupCache entry invalidated"
        );
    }

    async fn invalidate_tenant(&self, tenant_id: &str) {
        // Force pending maintenance so the next lookup is guaranteed
        // to miss — moka's predicate eviction is otherwise lazy.
        // See the matching note on `ModelCache::invalidate_tenant`.
        self.generation.bump();
        let prefix = format!("{tenant_id}:");
        let _ = self
            .groups
            .invalidate_entries_if(move |k, _| k.starts_with(&prefix));
        let prefix2 = format!("{tenant_id}:");
        let _ = self
            .user_groups
            .invalidate_entries_if(move |k, _| k.starts_with(&prefix2));
        // API key cache doesn't have tenant prefix — clear all on tenant change
        // (this is acceptable since admin changes are rare)
        self.api_key_groups.invalidate_all();
        self.card_services.invalidate(tenant_id).await;
        self.groups.run_pending_tasks().await;
        self.user_groups.run_pending_tasks().await;
        self.api_key_groups.run_pending_tasks().await;
        tracing::debug!(tenant_id, "ResourceGroupCache tenant invalidated");
    }

    async fn invalidate_all(&self) {
        self.generation.bump();
        self.groups.invalidate_all();
        self.user_groups.invalidate_all();
        self.api_key_groups.invalidate_all();
        self.card_services.invalidate_all();
        tracing::debug!("ResourceGroupCache fully invalidated");
    }
}

// ---------------------------------------------------------------------------
// Test-only helpers (visible to sibling modules under cache/)
// ---------------------------------------------------------------------------

#[cfg(any(test, feature = "db-tests"))]
impl ResourceGroupCache {
    pub(super) async fn _peek_group(
        &self,
        tenant_id: &str,
        group_id: &str,
    ) -> Option<Arc<EffectiveGroupData>> {
        self.groups.get(&format!("{tenant_id}:{group_id}")).await
    }

    pub(super) async fn _peek_user_groups(
        &self,
        tenant_id: &str,
        user_id: &str,
    ) -> Option<Arc<Vec<String>>> {
        self.user_groups
            .get(&format!("{tenant_id}:{user_id}"))
            .await
    }

    pub(super) async fn _peek_api_key(
        &self,
        key_hash: &str,
    ) -> Option<Arc<(String, Option<String>)>> {
        self.api_key_groups.get(&key_hash.to_string()).await
    }

    #[cfg(test)]
    pub(super) async fn insert_card_services_for_test(
        &self,
        tenant_id: &str,
        card: &str,
        sys: &str,
    ) {
        self.card_services
            .insert(
                tenant_id.to_string(),
                Arc::new(HashMap::from([(card.to_string(), sys.to_string())])),
            )
            .await;
    }

    pub(super) async fn _peek_card_services(
        &self,
        tenant_id: &str,
    ) -> Option<Arc<HashMap<String, String>>> {
        self.card_services.get(tenant_id).await
    }

    pub(super) async fn insert_group_for_test(&self, tenant_id: &str, group_id: &str) {
        let data = EffectiveGroupData {
            group: ResourceGroup {
                id: group_id.to_string(),
                tenant_id: tenant_id.to_string(),
                parent_group_id: None,
                hierarchy_level: Some(0),
                hierarchy_path: None,
                inherit_resources: true,
                name: format!("synth-{group_id}"),
                display_name: None,
                description: None,
                icon: None,
                color: None,
                group_type: Some("team".to_string()),
                system_kind: None,
                is_active: true,
                is_system: false,
                llm_global_limits: None,
                mcp_global_limits: None,
                llm_global_governance: None,
                mcp_global_governance: None,
                skill_global_governance: None,
                skill_global_limits: None,
                a2a_global_limits: None,
                a2a_global_governance: None,
                media_global_limits: None,
                media_global_governance: None,
                intended_client_systems: None,
                enforce_accepted_callers: false,
                created_by: None,
                updated_by: None,
                created_at: None,
                updated_at: None,
            },
            effective_model_ids: HashSet::new(),
            effective_mcp_ids: HashSet::new(),
            effective_skill_ids: HashSet::new(),
            effective_agent_card_ids: HashSet::new(),
            effective_a2a_limits: None,
            a2a_governance_levels: Vec::new(),
            member_user_ids: HashSet::new(),
            ancestor_ids: vec![],
            extras: GroupExtras::default(),
        };
        self.groups
            .insert(format!("{tenant_id}:{group_id}"), Arc::new(data))
            .await;
    }

    #[cfg(test)]
    pub(super) async fn insert_user_groups_for_test(
        &self,
        tenant_id: &str,
        user_id: &str,
        group_ids: Vec<String>,
    ) {
        self.user_groups
            .insert(format!("{tenant_id}:{user_id}"), Arc::new(group_ids))
            .await;
    }

    #[cfg(test)]
    pub(super) async fn insert_api_key_for_test(
        &self,
        key_hash: &str,
        tenant_id: &str,
        group_id: Option<String>,
    ) {
        self.api_key_groups
            .insert(
                key_hash.to_string(),
                Arc::new((tenant_id.to_string(), group_id)),
            )
            .await;
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn effective_group_data_model_check() {
        let mut model_ids = HashSet::new();
        model_ids.insert("llm-01".to_string());
        model_ids.insert("llm-02".to_string());

        assert!(model_ids.contains("llm-01"));
        assert!(model_ids.contains("llm-02"));
        assert!(!model_ids.contains("llm-03"));
    }

    #[test]
    fn effective_group_data_mcp_check() {
        let mut mcp_ids = HashSet::new();
        mcp_ids.insert("mcp-weather".to_string());
        mcp_ids.insert("mcp-search".to_string());

        assert!(mcp_ids.contains("mcp-weather"));
        assert!(!mcp_ids.contains("mcp-unknown"));
    }

    #[test]
    fn effective_group_data_skill_check() {
        let mut skill_ids = HashSet::new();
        skill_ids.insert("skill-deploy".to_string());

        assert!(skill_ids.contains("skill-deploy"));
        assert!(!skill_ids.contains("skill-admin"));
    }

    #[test]
    fn effective_group_data_member_check() {
        let mut members = HashSet::new();
        members.insert(1);
        members.insert(42);

        assert!(members.contains(&1));
        assert!(members.contains(&42));
        assert!(!members.contains(&999));
    }

    #[test]
    fn ancestor_chain_ordering() {
        let ancestors = [
            "parent-01".to_string(),
            "grandparent-01".to_string(),
            "root".to_string(),
        ];

        assert_eq!(ancestors[0], "parent-01");
        assert_eq!(ancestors.last().unwrap(), "root");
        assert_eq!(ancestors.len(), 3);
    }

    #[tokio::test]
    async fn moka_cache_basic_operations() {
        let cache: moka::future::Cache<String, Arc<Vec<String>>> = moka::future::Cache::builder()
            .max_capacity(100)
            .time_to_live(Duration::from_secs(60))
            .build();

        let data = Arc::new(vec!["group-a".to_string(), "group-b".to_string()]);
        cache.insert("t1:user-1".into(), data.clone()).await;

        let result = cache.get(&"t1:user-1".to_string()).await;
        assert!(result.is_some());
        assert_eq!(result.unwrap().len(), 2);

        let miss = cache.get(&"t1:user-999".to_string()).await;
        assert!(miss.is_none());
    }

    #[tokio::test]
    async fn moka_cache_invalidation() {
        let cache: moka::future::Cache<String, Arc<String>> = moka::future::Cache::builder()
            .max_capacity(100)
            .time_to_live(Duration::from_secs(60))
            .build();

        cache
            .insert("t1:grp-1".into(), Arc::new("data".into()))
            .await;
        assert!(cache.get(&"t1:grp-1".to_string()).await.is_some());

        cache.invalidate(&"t1:grp-1".to_string()).await;
        assert!(cache.get(&"t1:grp-1".to_string()).await.is_none());
    }

    // -----------------------------------------------------------------------
    // ResourceGroupCache integration tests — verify the wrapper struct
    // (not just raw moka). These cover the 3-subcache invalidation
    // semantics and Clone-shares-state invariant that the
    // CacheInvalidator depends on.
    //
    // The test helpers are declared at module level with `pub(super)`
    // visibility (see top of file, after the inherent impl block) so
    // the dispatcher tests in `cache/mod.rs::tests` can also use them.
    // -----------------------------------------------------------------------

    use super::super::ResourceCache;
    use sqlx::postgres::PgPoolOptions;

    fn build_cache() -> ResourceGroupCache {
        let pool = PgPoolOptions::new()
            .max_connections(1)
            .connect_lazy("postgres://test:test@localhost:5432/test")
            .expect("connect_lazy never fails on a syntactically-valid URL");
        ResourceGroupCache::new(pool)
    }

    // -----------------------------------------------------------------------
    // Tenant isolation across all 3 sub-caches
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn invalidate_tenant_drops_groups_and_user_groups_for_target_only() {
        let cache = build_cache();
        cache.insert_group_for_test("tenant-a", "grp-1").await;
        cache.insert_group_for_test("tenant-a", "grp-2").await;
        cache
            .insert_user_groups_for_test("tenant-a", "42", vec!["grp-1".into()])
            .await;
        cache.insert_group_for_test("tenant-b", "grp-1").await;
        cache
            .insert_user_groups_for_test("tenant-b", "42", vec!["grp-1".into()])
            .await;

        cache.invalidate_tenant("tenant-a").await;

        // tenant-a fully cleared
        assert!(cache._peek_group("tenant-a", "grp-1").await.is_none());
        assert!(cache._peek_group("tenant-a", "grp-2").await.is_none());
        assert!(cache._peek_user_groups("tenant-a", "42").await.is_none());
        // tenant-b untouched
        assert!(cache._peek_group("tenant-b", "grp-1").await.is_some());
        assert!(cache._peek_user_groups("tenant-b", "42").await.is_some());
    }

    #[tokio::test]
    async fn invalidate_tenant_clears_entire_api_key_cache() {
        // The api_key_groups sub-cache is keyed only by `{api_key_hash}`
        // (no tenant prefix), so per the inherent impl the whole
        // api-key cache is cleared on tenant invalidation. This is
        // documented as acceptable because admin changes are rare.
        // The test pins this behaviour so it can't change silently.
        let cache = build_cache();
        cache
            .insert_api_key_for_test("hash-a", "tenant-a", Some("grp-1".into()))
            .await;
        cache
            .insert_api_key_for_test("hash-b", "tenant-b", None)
            .await;

        cache.invalidate_tenant("tenant-a").await;

        assert!(
            cache._peek_api_key("hash-a").await.is_none(),
            "tenant-a's API key cache must be cleared"
        );
        assert!(
            cache._peek_api_key("hash-b").await.is_none(),
            "the entire api_key_groups cache is cleared on any tenant \
             invalidation — this is a deliberate trade-off, not a bug \
             (documented in `invalidate_tenant`)"
        );
    }

    #[tokio::test]
    async fn key_prefix_is_tenant_colon_not_substring() {
        let cache = build_cache();
        cache.insert_group_for_test("acme", "grp-1").await;
        cache.insert_group_for_test("acme-corp", "grp-1").await;

        cache.invalidate_tenant("acme").await;

        assert!(cache._peek_group("acme", "grp-1").await.is_none());
        assert!(cache._peek_group("acme-corp", "grp-1").await.is_some());
    }

    // -----------------------------------------------------------------------
    // Per-resource invalidation
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn invalidate_evicts_only_specific_group() {
        let cache = build_cache();
        cache.insert_group_for_test("tenant-a", "grp-1").await;
        cache.insert_group_for_test("tenant-a", "grp-2").await;

        cache.invalidate("tenant-a", "grp-1").await;

        assert!(cache._peek_group("tenant-a", "grp-1").await.is_none());
        assert!(cache._peek_group("tenant-a", "grp-2").await.is_some());
    }

    // -----------------------------------------------------------------------
    // Clone shares state — propagation across handles
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn clone_shares_state_with_original() {
        let cache = build_cache();
        cache.insert_group_for_test("tenant-a", "grp-1").await;

        let cloned = cache.clone();
        cloned.invalidate("tenant-a", "grp-1").await;

        assert!(cache._peek_group("tenant-a", "grp-1").await.is_none());
    }

    // -----------------------------------------------------------------------
    // Whole-cache reset
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn invalidate_all_drops_every_subcache() {
        let cache = build_cache();
        cache.insert_group_for_test("a", "grp-1").await;
        cache
            .insert_user_groups_for_test("a", "1", vec!["grp-1".into()])
            .await;
        cache
            .insert_api_key_for_test("hash-1", "a", Some("grp-1".into()))
            .await;

        cache.invalidate_all().await;
        cache.groups.run_pending_tasks().await;
        cache.user_groups.run_pending_tasks().await;
        cache.api_key_groups.run_pending_tasks().await;

        assert!(cache._peek_group("a", "grp-1").await.is_none());
        assert!(cache._peek_user_groups("a", "1").await.is_none());
        assert!(cache._peek_api_key("hash-1").await.is_none());
    }

    // -----------------------------------------------------------------------
    // A2A ancestor-chain restrictive merge — `merge_a2a_settings_along_chain`
    // (limits through `usage::restrictive_merge`, policy levels kept).
    //
    // This is what makes a parent group able to only ever TIGHTEN a child's
    // A2A limits.
    // -----------------------------------------------------------------------

    use serde_json::json;

    fn merged(levels: Vec<serde_json::Value>) -> serde_json::Value {
        let rows = levels.into_iter().map(|l| (Some(l), None)).collect();
        merge_a2a_settings_along_chain(&[], rows).0.expect("limits")
    }

    #[test]
    fn a2a_limits_numeric_min_wins() {
        // Parent 10, child 4 → min = 4, an integer as stated. And the reverse.
        let l = merged(vec![
            json!({"frequency": {"max_calls_per_minute": 10}}),
            json!({"frequency": {"max_calls_per_minute": 4}}),
        ]);
        assert_eq!(l["frequency"]["max_calls_per_minute"], json!(4));
        let l = merged(vec![
            json!({"frequency": {"max_calls_per_minute": 4}}),
            json!({"frequency": {"max_calls_per_minute": 10}}),
        ]);
        assert_eq!(l["frequency"]["max_calls_per_minute"], json!(4));
    }

    #[test]
    fn a2a_limits_bool_true_wins_and_string_child_wins() {
        // Bool: true is more restrictive (e.g. hard_stop) → OR.
        let l = merged(vec![
            json!({"hard_stop": false, "mode": "warn"}),
            json!({"hard_stop": true, "mode": "enforce"}),
        ]);
        assert_eq!(l["hard_stop"], json!(true));
        // Two strings: the later level's.
        assert_eq!(l["mode"], json!("enforce"));

        // true stays true even if the other level says false.
        let l = merged(vec![
            json!({"hard_stop": true}),
            json!({"hard_stop": false}),
        ]);
        assert_eq!(l["hard_stop"], json!(true));
    }

    #[test]
    fn a2a_limits_missing_key_inserts_and_recurses() {
        // Key present only in one level is taken verbatim.
        let l = merged(vec![
            json!({"chain": {"max_delegation_depth": 3}}),
            json!({"frequency": {"max_calls_per_day": 100}, "chain": {"max_delegation_depth": 2}}),
        ]);
        assert_eq!(l["frequency"]["max_calls_per_day"], json!(100));
        // Nested object recursion took the min for the shared key.
        assert_eq!(l["chain"]["max_delegation_depth"], json!(2));
    }

    #[test]
    fn merge_a2a_chain_three_levels_composes_limits_and_gov() {
        // root → mid → leaf. Limits: numeric min across all three. Policy:
        // every level's document is kept as it is; the strictest verdict of
        // the levels is taken where a call is judged
        // (`services::a2a_governance`, whose tests cover access strictness,
        // the shortest window, the denied tags of every level and the
        // allow-list of every level).
        let rows = vec![
            (
                Some(
                    json!({"frequency": {"max_calls_per_minute": 100, "max_calls_per_day": 5000}}),
                ),
                Some(json!({
                    "capability": {"default_access": "enabled"},
                    "remote": {"allowed_globs": ["*.a.com", "*.b.com", "*.c.com"]}
                })),
            ),
            (
                Some(json!({"frequency": {"max_calls_per_minute": 60}})),
                Some(json!({
                    "capability": {"default_access": "approval_required"},
                    "remote": {"allowed_globs": ["*.b.com", "*.c.com"]},
                    "data": {"deny_classifications": ["pii"]}
                })),
            ),
            (
                Some(json!({"frequency": {"max_calls_per_minute": 30, "max_calls_per_day": 2000}})),
                Some(json!({
                    "capability": {"default_access": "disabled"},
                    "remote": {"allowed_globs": ["*.c.com"]},
                    "data": {"deny_classifications": ["phi"]}
                })),
            ),
        ];

        let (limits, gov) = merge_a2a_settings_along_chain(&[], rows);
        let limits = limits.expect("limits present");
        // Numeric min across the whole chain.
        assert_eq!(limits["frequency"]["max_calls_per_minute"], json!(30));
        assert_eq!(limits["frequency"]["max_calls_per_day"], json!(2000));

        // Every level's policy document, unmerged.
        assert_eq!(gov.len(), 3);
        let access: Vec<&str> = gov
            .iter()
            .filter_map(|g| g["capability"]["default_access"].as_str())
            .collect();
        assert_eq!(access, ["enabled", "approval_required", "disabled"]);
        assert_eq!(gov[2]["remote"]["allowed_globs"], json!(["*.c.com"]));
    }

    /// A level's `null` or malformed A2A limit does not erase another
    /// level's, in either order (the rows come in no particular order).
    #[test]
    fn a_levels_null_or_malformed_a2a_limit_keeps_the_others() {
        let parent = json!({
            "chain": {"max_delegation_depth": 3},
            "invocations": {"daily_limit": 100, "hard_stop": true},
            "frequency": {"max_calls_per_minute": 10}
        });
        for child in [
            json!({
                "chain": {"max_delegation_depth": null},
                "invocations": {"daily_limit": null, "hard_stop": null},
                "frequency": {"max_calls_per_minute": null}
            }),
            json!({
                "chain": {"max_delegation_depth": "deep"},
                "invocations": {"daily_limit": 0, "hard_stop": "no"},
                "frequency": {"max_calls_per_minute": -1}
            }),
            json!({"chain": null, "invocations": null, "frequency": null}),
        ] {
            for rows in [
                vec![(Some(parent.clone()), None), (Some(child.clone()), None)],
                vec![(Some(child.clone()), None), (Some(parent.clone()), None)],
            ] {
                let (limits, _) = merge_a2a_settings_along_chain(&[], rows);
                let l = limits.expect("limits");
                assert_eq!(
                    l["chain"]["max_delegation_depth"].as_i64(),
                    Some(3),
                    "{child}: {l}"
                );
                assert_eq!(
                    l["invocations"]["daily_limit"].as_i64(),
                    Some(100),
                    "{child}: {l}"
                );
                assert_eq!(l["invocations"]["hard_stop"], json!(true), "{child}: {l}");
                assert_eq!(
                    l["frequency"]["max_calls_per_minute"].as_i64(),
                    Some(10),
                    "{child}: {l}"
                );
            }
        }
    }

    /// The A2A readers take integer limits (`as_i64`): two levels' integers
    /// merge to an integer, not to a float the readers cannot read.
    #[test]
    fn merged_a2a_limits_stay_readable_integers() {
        let rows = vec![
            (
                Some(json!({"chain": {"max_delegation_depth": 3},
                            "invocations": {"daily_limit": 100},
                            "frequency": {"max_calls_per_minute": 10}})),
                None,
            ),
            (
                Some(json!({"chain": {"max_delegation_depth": 2},
                            "invocations": {"daily_limit": 50},
                            "frequency": {"max_calls_per_minute": 4}})),
                None,
            ),
        ];
        let (limits, _) = merge_a2a_settings_along_chain(&[], rows);
        let l = limits.unwrap();
        assert_eq!(l["chain"]["max_delegation_depth"].as_i64(), Some(2), "{l}");
        assert_eq!(l["invocations"]["daily_limit"].as_i64(), Some(50), "{l}");
        assert_eq!(
            l["frequency"]["max_calls_per_minute"].as_i64(),
            Some(4),
            "{l}"
        );
    }

    /// `chain.max_delegation_depth: 0` is a stated cap (no delegation at
    /// all; the control plane accepts 0..32): it is the strictest value, not
    /// an unset one.
    #[test]
    fn a_delegation_depth_of_zero_is_the_strictest() {
        for rows in [
            vec![
                (Some(json!({"chain": {"max_delegation_depth": 3}})), None),
                (Some(json!({"chain": {"max_delegation_depth": 0}})), None),
            ],
            vec![
                (Some(json!({"chain": {"max_delegation_depth": 0}})), None),
                (Some(json!({"chain": {"max_delegation_depth": 3}})), None),
            ],
        ] {
            let (limits, _) = merge_a2a_settings_along_chain(&[], rows);
            let l = limits.unwrap();
            assert_eq!(l["chain"]["max_delegation_depth"].as_i64(), Some(0), "{l}");
        }
    }

    #[test]
    fn a_level_without_a_policy_adds_no_level() {
        let rows = vec![
            (None, None),
            (None, Some(serde_json::Value::Null)),
            (
                None,
                Some(json!({"capability": {"default_access": "disabled"}})),
            ),
        ];
        let (limits, gov) = merge_a2a_settings_along_chain(&[], rows);
        assert!(limits.is_none());
        assert_eq!(gov.len(), 1);
    }
}
