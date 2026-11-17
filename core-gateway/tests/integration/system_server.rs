// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! `services/system_server.rs::filter_skills_for_user` membership query.
//!
//! When a user lists tools through the system Agent Skills MCP server,
//! we filter the skill catalog down to skills in the user's groups.
//! That filter is driven by a UNION query — direct memberships plus
//! EUG-mediated memberships. If the EUG arm regresses, an EUG-only
//! user sees no skills at all.

use sqlx::PgPool;

use crate::common;

/// Mirrors `services/system_server.rs:405` — the membership UNION used
/// by the skill filter. Kept in sync intentionally: if you change one,
/// change both.
const MEMBERSHIPS_QUERY: &str = r#"SELECT group_id FROM resource_group_memberships
   WHERE tenant_id = $1 AND user_id = $2
   UNION
   SELECT rgeug.resource_group_id
   FROM resource_group_end_user_groups rgeug
   JOIN end_user_group_members eugm
     ON eugm.end_user_group_id = rgeug.end_user_group_id
   WHERE eugm.end_user_id = $2"#;

async fn memberships(pool: &PgPool, tenant_id: &str, user_id: i32) -> Vec<String> {
    sqlx::query_scalar::<_, String>(MEMBERSHIPS_QUERY)
        .bind(tenant_id)
        .bind(user_id)
        .fetch_all(pool)
        .await
        .expect("memberships query")
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn membership_filter_returns_direct_groups(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-1").await;
    common::seed_resource_group(&pool, "rg-ops", "tenant-1").await;
    common::seed_end_user(&pool, 4001, "tenant-1").await;
    common::add_direct_membership(&pool, 4001, "rg-ops", "tenant-1").await;

    let groups = memberships(&pool, "tenant-1", 4001).await;
    assert!(
        groups.contains(&"rg-ops".to_string()),
        "missing direct edge in {groups:?}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn membership_filter_returns_eug_mediated_groups(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-1").await;
    common::seed_resource_group(&pool, "rg-sales", "tenant-1").await;
    common::seed_end_user(&pool, 4002, "tenant-1").await;
    common::seed_end_user_group(&pool, "eug-sales", "tenant-1", "Sales Team").await;
    common::add_user_to_eug(&pool, "eug-sales", 4002).await;
    common::bind_eug_to_rg(&pool, "eug-sales", "rg-sales").await;

    let groups = memberships(&pool, "tenant-1", 4002).await;
    assert!(
        groups.contains(&"rg-sales".to_string()),
        "EUG-mediated edge missing — skill filter would hide all skills \
         for an EUG-only user; got {groups:?}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn membership_filter_unions_direct_and_eug(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-1").await;
    common::seed_resource_group(&pool, "rg-ops", "tenant-1").await;
    common::seed_resource_group(&pool, "rg-sales", "tenant-1").await;
    common::seed_end_user(&pool, 4003, "tenant-1").await;

    // Direct edge.
    common::add_direct_membership(&pool, 4003, "rg-ops", "tenant-1").await;

    // EUG edge to a different group.
    common::seed_end_user_group(&pool, "eug-sales", "tenant-1", "Sales").await;
    common::add_user_to_eug(&pool, "eug-sales", 4003).await;
    common::bind_eug_to_rg(&pool, "eug-sales", "rg-sales").await;

    let groups = memberships(&pool, "tenant-1", 4003).await;
    assert!(
        groups.contains(&"rg-ops".to_string()),
        "missing direct in {groups:?}"
    );
    assert!(
        groups.contains(&"rg-sales".to_string()),
        "missing EUG in {groups:?}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn membership_filter_empty_for_non_member(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-1").await;
    common::seed_end_user(&pool, 4004, "tenant-1").await;

    let groups = memberships(&pool, "tenant-1", 4004).await;
    assert!(
        groups.is_empty(),
        "user with no edges must produce an empty set so the filter \
         returns 0 skills; got {groups:?}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn membership_filter_tenant_scopes_direct_arm(pool: PgPool) {
    // The direct-membership arm filters by tenant_id, so cross-tenant
    // direct memberships (shouldn't exist, but defense-in-depth) are
    // dropped. The EUG arm relies on user_id being globally unique.
    common::seed_tenant(&pool, "tenant-1").await;
    common::seed_tenant(&pool, "tenant-2").await;
    common::seed_resource_group(&pool, "rg-t1", "tenant-1").await;
    common::seed_resource_group(&pool, "rg-t2", "tenant-2").await;
    common::seed_end_user(&pool, 4005, "tenant-1").await;
    common::add_direct_membership(&pool, 4005, "rg-t1", "tenant-1").await;

    // Query as tenant-1 — should NOT return rg-t2.
    let groups = memberships(&pool, "tenant-1", 4005).await;
    assert!(groups.contains(&"rg-t1".to_string()));
    assert!(
        !groups.contains(&"rg-t2".to_string()),
        "tenant-2 leak in {groups:?}"
    );
}
