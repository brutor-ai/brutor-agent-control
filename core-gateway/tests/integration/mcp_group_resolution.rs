// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # MCP group resolution — characterization suite
//!
//! The MCP hot path resolves the caller's groups **twice** per request:
//! `resolve_mcp_server_access` for the billing group, then
//! `resolve_all_mcp_server_groups` for the capability-filter merge. Both run
//! the same membership query and the same per-group chain walk, so a user in
//! four groups pays for it twice on every `tools/call`.
//!
//! Reading them side by side, the single-group answer is exactly the first
//! element of the multi-group answer, in every branch. This suite exists to
//! prove that against a real database BEFORE the two are collapsed into one
//! call — and to keep proving it afterwards.
//!
//! Written to pass unchanged before and after the refactor. If a test here
//! needed editing to make the refactor pass, the refactor changed behaviour
//! and that is the thing to look at, not the test.
//!
//! Branches covered, from reading both functions:
//!
//! | branch                                    | single      | all            |
//! |-------------------------------------------|-------------|----------------|
//! | API key, bound group grants               | `Some(g)`   | `[g]`          |
//! | API key, bound group does not grant       | `None`      | `[]`           |
//! | API key, no such group                    | `None`      | `[]`           |
//! | no gateway user and no API key group      | `None`      | `[]`           |
//! | gateway user, no groups at all            | `None`      | `[]`           |
//! | gateway user, one group grants            | `Some(g)`   | `[g]`          |
//! | gateway user, several grant               | first       | all, in order  |
//! | gateway user, none grant, some configured | `None`      | `[]`           |
//! | gateway user, none grant, none configured | first       | all (open)     |
//! | access via EndUserGroup rather than direct| `Some(g)`   | `[g]`          |
//! | inactive group is never returned          | `None`      | `[]`           |

use sqlx::PgPool;

use brutor_gateway_core::models::auth::{AuthContext, AuthType};
use brutor_gateway_core::services::usage;

use crate::common;

const T: &str = "tenant-mcpres";

fn api_key_auth(group_id: Option<&str>) -> AuthContext {
    AuthContext {
        auth_type: AuthType::ApiKey,
        tenant_id: T.to_string(),
        gateway_user_id: None,
        gateway_username: None,
        gateway_tenant_id: None,
        idp_user_id: None,
        idp_username: None,
        idp_email: None,
        api_key_id: None,
        api_key_name: None,
        api_key_scope: None,
        api_key_group_id: group_id.map(str::to_string),
        api_key_is_platform: false,
        api_key_user_id: None,
        allowed_skill_ids: None,
        session_key: None,
        client_ip: None,
        resolved_group_id: None,
        actor_agent_id: None,
        session_token_type: None,
        agent_impl: Default::default(),
        admission_hold: None,
    }
}

fn user_auth(user_id: i32) -> AuthContext {
    AuthContext {
        auth_type: AuthType::GatewayJwt,
        gateway_user_id: Some(user_id.to_string()),
        ..api_key_auth(None)
    }
}

/// Seed a server bound to a group through a server_config.
async fn grant(pool: &PgPool, server_id: &str, group_id: &str) {
    let cfg = format!("cfg-{server_id}-{group_id}");
    common::seed_server_config(pool, &cfg, T, server_id).await;
    common::bind_server_config_to_rg(pool, &cfg, group_id).await;
}

/// The invariant the refactor rests on: the billing group is the first of the
/// granting groups. Asserted in every test rather than once, because a branch
/// that diverges is exactly the branch that would break silently.
async fn assert_consistent(pool: &PgPool, auth: &AuthContext, server_id: &str) -> Vec<String> {
    let single = usage::resolve_mcp_server_access(pool, auth, T, server_id)
        .await
        .unwrap();
    let all = usage::resolve_all_mcp_server_groups(pool, auth, T, server_id)
        .await
        .unwrap();
    assert_eq!(
        single.as_ref().map(|g| g.id.clone()),
        all.first().map(|g| g.id.clone()),
        "the billing group must be the first granting group — this is the \
         equivalence the single-resolution refactor depends on"
    );
    all.into_iter().map(|g| g.id).collect()
}

// ---------------------------------------------------------------------------
// API key
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn api_key_bound_group_that_grants(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "k-ok", T).await;
    common::seed_mcp_server(&pool, "srv-1", T, "srv-1").await;
    grant(&pool, "srv-1", "k-ok").await;

    let ids = assert_consistent(&pool, &api_key_auth(Some("k-ok")), "srv-1").await;
    assert_eq!(ids, vec!["k-ok"]);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn api_key_bound_group_that_does_not_grant(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "k-no", T).await;
    common::seed_resource_group(&pool, "k-other", T).await;
    common::seed_mcp_server(&pool, "srv-1", T, "srv-1").await;
    // Bound elsewhere, so the key's group does not grant.
    grant(&pool, "srv-1", "k-other").await;

    let ids = assert_consistent(&pool, &api_key_auth(Some("k-no")), "srv-1").await;
    assert!(
        ids.is_empty(),
        "an API key never falls back to another group"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn api_key_pointing_at_a_group_that_does_not_exist(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_mcp_server(&pool, "srv-1", T, "srv-1").await;

    let ids = assert_consistent(&pool, &api_key_auth(Some("ghost")), "srv-1").await;
    assert!(ids.is_empty());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn neither_an_api_key_group_nor_a_gateway_user(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_mcp_server(&pool, "srv-1", T, "srv-1").await;

    let ids = assert_consistent(&pool, &api_key_auth(None), "srv-1").await;
    assert!(ids.is_empty(), "no identity resolves to no group");
}

// ---------------------------------------------------------------------------
// Gateway user
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn gateway_user_with_no_groups(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_end_user(&pool, 1, T).await;
    common::seed_mcp_server(&pool, "srv-1", T, "srv-1").await;

    let ids = assert_consistent(&pool, &user_auth(1), "srv-1").await;
    assert!(ids.is_empty());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn gateway_user_one_group_grants(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_end_user(&pool, 1, T).await;
    common::seed_resource_group(&pool, "g-a", T).await;
    common::add_direct_membership(&pool, 1, "g-a", T).await;
    common::seed_mcp_server(&pool, "srv-1", T, "srv-1").await;
    grant(&pool, "srv-1", "g-a").await;

    let ids = assert_consistent(&pool, &user_auth(1), "srv-1").await;
    assert_eq!(ids, vec!["g-a"]);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn gateway_user_several_groups_grant(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_end_user(&pool, 1, T).await;
    for g in ["g-a", "g-b", "g-c"] {
        common::seed_resource_group(&pool, g, T).await;
        common::add_direct_membership(&pool, 1, g, T).await;
    }
    common::seed_mcp_server(&pool, "srv-1", T, "srv-1").await;
    // Two of the three grant.
    grant(&pool, "srv-1", "g-a").await;
    grant(&pool, "srv-1", "g-c").await;

    let ids = assert_consistent(&pool, &user_auth(1), "srv-1").await;
    assert_eq!(ids.len(), 2, "only the granting groups come back: {ids:?}");
    assert!(ids.contains(&"g-a".to_string()) && ids.contains(&"g-c".to_string()));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn gateway_user_none_grant_but_configs_exist(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_end_user(&pool, 1, T).await;
    common::seed_resource_group(&pool, "g-a", T).await;
    common::add_direct_membership(&pool, 1, "g-a", T).await;
    common::seed_mcp_server(&pool, "srv-1", T, "srv-1").await;
    common::seed_mcp_server(&pool, "srv-2", T, "srv-2").await;
    // The group is configured, just not for srv-1.
    grant(&pool, "srv-2", "g-a").await;

    let ids = assert_consistent(&pool, &user_auth(1), "srv-1").await;
    assert!(
        ids.is_empty(),
        "a group that has configs but not this one denies — the open fallback \
         is only for a group with NO configs at all"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn gateway_user_none_grant_and_nothing_is_configured(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_end_user(&pool, 1, T).await;
    for g in ["g-a", "g-b"] {
        common::seed_resource_group(&pool, g, T).await;
        common::add_direct_membership(&pool, 1, g, T).await;
    }
    common::seed_mcp_server(&pool, "srv-1", T, "srv-1").await;
    // No bindings anywhere: "unconfigured = open".

    let ids = assert_consistent(&pool, &user_auth(1), "srv-1").await;
    assert_eq!(
        ids.len(),
        2,
        "with nothing configured, every group is returned: {ids:?}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_inactive_group_never_grants(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_end_user(&pool, 1, T).await;
    common::seed_resource_group(&pool, "g-off", T).await;
    common::add_direct_membership(&pool, 1, "g-off", T).await;
    common::seed_mcp_server(&pool, "srv-1", T, "srv-1").await;
    grant(&pool, "srv-1", "g-off").await;
    sqlx::query("UPDATE resource_groups SET is_active = false WHERE id = $1")
        .bind("g-off")
        .execute(&pool)
        .await
        .expect("deactivate");

    let ids = assert_consistent(&pool, &user_auth(1), "srv-1").await;
    assert!(
        ids.is_empty(),
        "a deactivated group must not grant access, and must not trigger the \
         unconfigured-is-open fallback either"
    );
}
