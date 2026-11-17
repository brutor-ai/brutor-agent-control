// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # GroupCache resource-inheritance gating tests
//!
//! These pin the contract that the materialized `GroupCache` honours the
//! `inherit_from_parent` flag for RESOURCES (agent cards, models, …) exactly
//! the way the authoritative per-request access checks
//! (`usage::model_in_group_chain` / `mcp_server_in_group_chain`) do.
//!
//! This is a security regression guard: A2A agent-card access is enforced from
//! `EffectiveGroupData::effective_agent_card_ids`. Before the fix the cache
//! walked the parent chain UNCONDITIONALLY, so a child group with
//! `inherit_from_parent = false` would still receive its parent's agent cards
//! (and models) — an access-widening hole, since the LLM/MCP access checks
//! correctly denied the same parent resources.
//!
//! In contrast, limits / governance are ALWAYS inherited (restrictive-wins) and
//! are intentionally NOT gated — that's covered elsewhere.
//!
//! Requires a running PostgreSQL database (see `test_helpers::test_database_url`).

mod test_helpers;

use brutor_gateway_core::services::cache::ResourceGroupCache;
use sqlx::PgPool;
use uuid::Uuid;

const TEST_TENANT: &str = "default";

fn uid() -> String {
    Uuid::new_v4().to_string()[..8].to_string()
}

async fn test_pool() -> PgPool {
    let url = test_helpers::test_database_url();
    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(2)
        .connect(&url)
        .await
        .expect("Failed to connect to test database");
    // This binary opens its own pool instead of going through `spawn_app`,
    // so nothing else here creates the tables. Without this the suite only
    // passed when another binary had already run against the same
    // database — and the cache swallows the missing-table error, so the
    // failure looked like "nothing was inherited".
    test_helpers::ensure_test_schema(&pool).await;
    pool
}

async fn create_group(
    db: &PgPool,
    id: &str,
    name: &str,
    parent_id: Option<&str>,
    inherit_from_parent: bool,
) {
    sqlx::query(
        r#"INSERT INTO resource_groups (id, tenant_id, name, parent_group_id, inherit_from_parent, group_type, hierarchy_level, is_active, is_system)
           VALUES ($1, $2, $3, $4, $5, 'DEPARTMENT', 1, true, false)
           ON CONFLICT (id) DO UPDATE SET parent_group_id = $4, inherit_from_parent = $5"#,
    )
    .bind(id)
    .bind(TEST_TENANT)
    .bind(name)
    .bind(parent_id)
    .bind(inherit_from_parent)
    .execute(db)
    .await
    .expect("create_group");
}

async fn create_agent_card(db: &PgPool, id: &str) {
    sqlx::query(
        r#"INSERT INTO agent_cards (id, tenant_id, name, card_json, enabled)
           VALUES ($1, $2, $3, '{}'::json, true)
           ON CONFLICT (id) DO NOTHING"#,
    )
    .bind(id)
    .bind(TEST_TENANT)
    .bind(format!("card {id}"))
    .execute(db)
    .await
    .expect("create_agent_card");
}

async fn bind_card_to_group(db: &PgPool, group_id: &str, card_id: &str) {
    sqlx::query(
        r#"INSERT INTO resource_group_agent_cards (id, resource_group_id, agent_card_id, created_at)
           VALUES ($1, $2, $3, now())
           ON CONFLICT (id) DO NOTHING"#,
    )
    .bind(format!("rgac-{}-{}", group_id, card_id))
    .bind(group_id)
    .bind(card_id)
    .execute(db)
    .await
    .expect("bind_card_to_group");
}

async fn cleanup(db: &PgPool, suffix: &str) {
    sqlx::query("DELETE FROM resource_group_agent_cards WHERE resource_group_id LIKE $1")
        .bind(format!("tgc-{suffix}%"))
        .execute(db)
        .await
        .ok();
    sqlx::query("DELETE FROM resource_groups WHERE id LIKE $1")
        .bind(format!("tgc-{suffix}%"))
        .execute(db)
        .await
        .ok();
    sqlx::query("DELETE FROM agent_cards WHERE id LIKE $1")
        .bind(format!("card-{suffix}%"))
        .execute(db)
        .await
        .ok();
}

/// Panic-safe cleanup of this test's rows — see `test_helpers::DbCleanupGuard`.
fn cleanup_guard(suffix: &str) -> test_helpers::DbCleanupGuard {
    let suffix = suffix.to_string();
    test_helpers::DbCleanupGuard::new(move |db| async move { cleanup(&db, &suffix).await })
}

/// A direct child with `inherit_from_parent = false` must NOT receive the
/// parent's agent cards, while a sibling with `inherit_from_parent = true`
/// must. This is the core access-widening regression guard.
#[tokio::test]
async fn agent_card_inheritance_respects_inherit_from_parent() {
    let db = test_pool().await;
    let s = uid();
    let _guard = cleanup_guard(&s);

    let parent = format!("tgc-{s}-parent");
    let child_inherit = format!("tgc-{s}-child-yes");
    let child_no_inherit = format!("tgc-{s}-child-no");

    create_group(&db, &parent, "Parent", None, false).await;
    create_group(&db, &child_inherit, "Child inherits", Some(&parent), true).await;
    create_group(
        &db,
        &child_no_inherit,
        "Child opts out",
        Some(&parent),
        false,
    )
    .await;

    let parent_card = format!("card-{s}-parent");
    let child_card = format!("card-{s}-child");
    create_agent_card(&db, &parent_card).await;
    create_agent_card(&db, &child_card).await;
    bind_card_to_group(&db, &parent, &parent_card).await;
    bind_card_to_group(&db, &child_no_inherit, &child_card).await;

    let cache = ResourceGroupCache::new(db.clone());

    // The parent itself always sees its own card.
    let eff_parent = cache
        .get_effective_group(TEST_TENANT, &parent)
        .await
        .expect("parent effective group");
    assert!(
        eff_parent.effective_agent_card_ids.contains(&parent_card),
        "parent should see its own card"
    );

    // inherit=true child sees the parent's card.
    let eff_yes = cache
        .get_effective_group(TEST_TENANT, &child_inherit)
        .await
        .expect("inherit child effective group");
    assert!(
        eff_yes.effective_agent_card_ids.contains(&parent_card),
        "inherit_from_parent=true child should inherit parent's agent card"
    );

    // inherit=false child sees ONLY its own card, never the parent's.
    let eff_no = cache
        .get_effective_group(TEST_TENANT, &child_no_inherit)
        .await
        .expect("no-inherit child effective group");
    assert!(
        eff_no.effective_agent_card_ids.contains(&child_card),
        "child should always see its directly-bound card"
    );
    assert!(
        !eff_no.effective_agent_card_ids.contains(&parent_card),
        "inherit_from_parent=false child must NOT inherit parent's agent card (access-widening guard)"
    );
}

/// A break anywhere in the chain stops resource inheritance above it.
///
///   root  (has card)
///     └── mid   (inherit_from_parent = false)
///           └── leaf  (inherit_from_parent = true)
///
/// `leaf` inherits from `mid` (its own flag is true), but `mid` does NOT inherit
/// from `root`, so `root`'s card must be invisible to `leaf`. This mirrors
/// `model_in_group_chain`, which stops walking at the first non-inheriting node.
#[tokio::test]
async fn agent_card_inheritance_breaks_at_first_non_inheriting_ancestor() {
    let db = test_pool().await;
    let s = uid();
    let _guard = cleanup_guard(&s);

    let root = format!("tgc-{s}-root");
    let mid = format!("tgc-{s}-mid");
    let leaf = format!("tgc-{s}-leaf");

    create_group(&db, &root, "Root", None, false).await;
    create_group(&db, &mid, "Mid (no inherit)", Some(&root), false).await;
    create_group(&db, &leaf, "Leaf (inherit)", Some(&mid), true).await;

    let root_card = format!("card-{s}-root");
    let mid_card = format!("card-{s}-mid");
    create_agent_card(&db, &root_card).await;
    create_agent_card(&db, &mid_card).await;
    bind_card_to_group(&db, &root, &root_card).await;
    bind_card_to_group(&db, &mid, &mid_card).await;

    let cache = ResourceGroupCache::new(db.clone());

    let eff_leaf = cache
        .get_effective_group(TEST_TENANT, &leaf)
        .await
        .expect("leaf effective group");

    assert!(
        eff_leaf.effective_agent_card_ids.contains(&mid_card),
        "leaf inherits mid's card (leaf.inherit_from_parent = true)"
    );
    assert!(
        !eff_leaf.effective_agent_card_ids.contains(&root_card),
        "leaf must NOT see root's card — mid breaks the chain (mid.inherit_from_parent = false)"
    );
}

/// Deep all-inheriting chain still inherits to the top — confirms the gate
/// doesn't over-restrict the normal additive case.
///
///   l1 (card) → l2 (inherit) → l3 (inherit)
#[tokio::test]
async fn agent_card_inheritance_deep_all_inheriting() {
    let db = test_pool().await;
    let s = uid();
    let _guard = cleanup_guard(&s);

    let l1 = format!("tgc-{s}-l1");
    let l2 = format!("tgc-{s}-l2");
    let l3 = format!("tgc-{s}-l3");

    create_group(&db, &l1, "L1", None, false).await;
    create_group(&db, &l2, "L2", Some(&l1), true).await;
    create_group(&db, &l3, "L3", Some(&l2), true).await;

    let l1_card = format!("card-{s}-l1");
    create_agent_card(&db, &l1_card).await;
    bind_card_to_group(&db, &l1, &l1_card).await;

    let cache = ResourceGroupCache::new(db.clone());

    let eff_l3 = cache
        .get_effective_group(TEST_TENANT, &l3)
        .await
        .expect("l3 effective group");

    assert!(
        eff_l3.effective_agent_card_ids.contains(&l1_card),
        "l3 should inherit l1's card through an all-inheriting chain"
    );
}
