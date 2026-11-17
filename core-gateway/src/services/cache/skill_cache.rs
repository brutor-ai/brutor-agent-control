// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Agent Skill Cache
//!
//! In-memory cache for agent skill configurations. Eliminates DB queries
//! on every skill execution request.

use std::sync::Arc;
use std::time::Duration;

use moka::future::Cache;
use sqlx::PgPool;

use crate::models::db::AgentSkill;

use super::{Generation, ResourceCache, insert_fresh};

const DEFAULT_TTL_SECS: u64 = 300;
const DEFAULT_MAX_ENTRIES: u64 = 5_000;

/// In-memory cache for agent skill lookups.
///
/// `Clone` shares the underlying moka cache — see the matching note on
/// `ModelCache`. The cache invalidator must hold the same instance the
/// request pipeline uses.
#[derive(Clone)]
pub struct AgentSkillCache {
    /// Skills by `{tenant_id}:{skill_id}` or `{tenant_id}:{skill_name}`
    skills: Cache<String, Arc<AgentSkill>>,
    db: PgPool,
    /// Bumped by every invalidation; loads insert with [`insert_fresh`].
    generation: Generation,
}

impl AgentSkillCache {
    pub fn new(db: PgPool) -> Self {
        // `support_invalidation_closures` is REQUIRED for
        // `invalidate_entries_if` to actually evict — see the matching
        // note on `ModelCache::new`.
        Self {
            skills: Cache::builder()
                .max_capacity(DEFAULT_MAX_ENTRIES)
                .time_to_live(Duration::from_secs(DEFAULT_TTL_SECS))
                .support_invalidation_closures()
                .build(),
            db,
            generation: Generation::default(),
        }
    }

    /// Get a skill by ID (cached).
    pub async fn get_skill(
        &self,
        tenant_id: &str,
        skill_id: &str,
    ) -> Result<Option<Arc<AgentSkill>>, sqlx::Error> {
        let key = format!("{tenant_id}:{skill_id}");

        if let Some(skill) = self.skills.get(&key).await {
            return Ok(Some(skill));
        }

        let started = self.generation.current();
        let Some(skill) = sqlx::query_as::<_, AgentSkill>(
            "SELECT * FROM agent_skills WHERE id = $1 AND tenant_id = $2 AND enabled = true",
        )
        .bind(skill_id)
        .bind(tenant_id)
        .fetch_optional(&self.db)
        .await?
        else {
            return Ok(None);
        };

        let skill = Arc::new(skill);
        let g = &self.generation;
        insert_fresh(&self.skills, key, skill.clone(), g, started).await;
        // Also cache by name
        let by_name = format!("{tenant_id}:{}", skill.name);
        insert_fresh(&self.skills, by_name, skill.clone(), g, started).await;

        Ok(Some(skill))
    }

    /// Get a skill by name (cached).
    pub async fn get_skill_by_name(
        &self,
        tenant_id: &str,
        skill_name: &str,
    ) -> Result<Option<Arc<AgentSkill>>, sqlx::Error> {
        let key = format!("{tenant_id}:{skill_name}");

        if let Some(skill) = self.skills.get(&key).await {
            return Ok(Some(skill));
        }

        let started = self.generation.current();
        let Some(skill) = sqlx::query_as::<_, AgentSkill>(
            "SELECT * FROM agent_skills WHERE name = $1 AND tenant_id = $2 AND enabled = true",
        )
        .bind(skill_name)
        .bind(tenant_id)
        .fetch_optional(&self.db)
        .await?
        else {
            return Ok(None);
        };

        let skill = Arc::new(skill);
        let g = &self.generation;
        insert_fresh(&self.skills, key, skill.clone(), g, started).await;
        let by_id = format!("{tenant_id}:{}", skill.id);
        insert_fresh(&self.skills, by_id, skill.clone(), g, started).await;

        Ok(Some(skill))
    }

    pub fn stats(&self) -> u64 {
        self.skills.entry_count()
    }
}

#[async_trait::async_trait]
impl ResourceCache for AgentSkillCache {
    fn name(&self) -> &str {
        "agent_skill_cache"
    }

    async fn invalidate(&self, tenant_id: &str, resource_id: &str) {
        self.generation.bump();
        let key = format!("{tenant_id}:{resource_id}");
        self.skills.invalidate(&key).await;
        // The skill is also cached under its name, which the message does
        // not carry: evict every entry holding this skill.
        let prefix = format!("{tenant_id}:");
        let id = resource_id.to_string();
        let _ = self
            .skills
            .invalidate_entries_if(move |k, v| k.starts_with(&prefix) && v.id == id);
        self.skills.run_pending_tasks().await;
    }

    async fn invalidate_tenant(&self, tenant_id: &str) {
        // Force pending maintenance so the next lookup is guaranteed
        // to miss — moka's predicate eviction is otherwise lazy.
        // See the matching note on `ModelCache::invalidate_tenant`.
        self.generation.bump();
        let prefix = format!("{tenant_id}:");
        let _ = self
            .skills
            .invalidate_entries_if(move |k, _| k.starts_with(&prefix));
        self.skills.run_pending_tasks().await;
    }

    async fn invalidate_all(&self) {
        self.generation.bump();
        self.skills.invalidate_all();
    }
}

// ---------------------------------------------------------------------------
// Test-only helpers
// ---------------------------------------------------------------------------

#[cfg(any(test, feature = "db-tests"))]
impl AgentSkillCache {
    pub(super) async fn _peek(
        &self,
        tenant_id: &str,
        skill_lookup: &str,
    ) -> Option<Arc<AgentSkill>> {
        self.skills
            .get(&format!("{tenant_id}:{skill_lookup}"))
            .await
    }

    pub(super) async fn insert_skill_for_test(&self, tenant_id: &str, skill_id: &str) {
        let skill = AgentSkill {
            id: skill_id.to_string(),
            tenant_id: tenant_id.to_string(),
            name: format!("synth-{skill_id}"),
            description: "synth".to_string(),
            license: None,
            compatibility: None,
            allowed_tools: None,
            version: "1.0.0".to_string(),
            status: "published".to_string(),
            enabled: true,
            metadata: None,
            skill_md_content: None,
            execution_count: 0,
            last_executed_at: None,
            published_at: None,
            created_at: None,
            source_repo_id: None,
        };
        self.skills
            .insert(format!("{tenant_id}:{skill_id}"), Arc::new(skill))
            .await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::cache::ResourceCache;
    use sqlx::postgres::PgPoolOptions;

    fn build_cache() -> AgentSkillCache {
        let pool = PgPoolOptions::new()
            .max_connections(1)
            .connect_lazy("postgres://test:test@localhost:5432/test")
            .expect("connect_lazy never fails on a syntactically-valid URL");
        AgentSkillCache::new(pool)
    }

    // -----------------------------------------------------------------------
    // Tenant isolation
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn invalidate_tenant_drops_only_target_tenant_skills() {
        let cache = build_cache();
        cache.insert_skill_for_test("tenant-a", "deploy").await;
        cache.insert_skill_for_test("tenant-a", "rollback").await;
        cache.insert_skill_for_test("tenant-b", "deploy").await;

        cache.invalidate_tenant("tenant-b").await;

        assert!(cache._peek("tenant-a", "deploy").await.is_some());
        assert!(cache._peek("tenant-a", "rollback").await.is_some());
        assert!(cache._peek("tenant-b", "deploy").await.is_none());
    }

    #[tokio::test]
    async fn key_prefix_is_tenant_colon_not_substring() {
        let cache = build_cache();
        cache.insert_skill_for_test("acme", "deploy").await;
        cache.insert_skill_for_test("acme-corp", "deploy").await;

        cache.invalidate_tenant("acme").await;

        assert!(cache._peek("acme", "deploy").await.is_none());
        assert!(cache._peek("acme-corp", "deploy").await.is_some());
    }

    // -----------------------------------------------------------------------
    // Per-resource invalidation
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn invalidate_evicts_only_specific_skill() {
        let cache = build_cache();
        cache.insert_skill_for_test("tenant-a", "deploy").await;
        cache.insert_skill_for_test("tenant-a", "rollback").await;

        cache.invalidate("tenant-a", "deploy").await;

        assert!(cache._peek("tenant-a", "deploy").await.is_none());
        assert!(cache._peek("tenant-a", "rollback").await.is_some());
    }

    // -----------------------------------------------------------------------
    // Clone shares state
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn clone_shares_inner_state() {
        let cache = build_cache();
        cache.insert_skill_for_test("tenant-a", "deploy").await;

        let cloned = cache.clone();
        cloned.invalidate("tenant-a", "deploy").await;

        assert!(cache._peek("tenant-a", "deploy").await.is_none());
    }

    // -----------------------------------------------------------------------
    // Whole-cache reset
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn invalidate_all_drops_every_entry() {
        let cache = build_cache();
        cache.insert_skill_for_test("a", "x").await;
        cache.insert_skill_for_test("b", "y").await;

        cache.invalidate_all().await;
        cache.skills.run_pending_tasks().await;

        assert!(cache._peek("a", "x").await.is_none());
        assert!(cache._peek("b", "y").await.is_none());
    }
}
