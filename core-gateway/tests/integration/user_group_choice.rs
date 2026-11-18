// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Which of a user's resource groups a call runs in.
//!
//! A user in two top-level groups (the live case: the trial user, in Default
//! and E2ETests, with E2ETests as their primary workspace) had their Agent
//! Skills calls billed to Default. The skill was scoped to the workspace the
//! portal sent, but billing, MCP group resolution, the user's guardrails and
//! the portal's MCP group each picked "the deepest membership" on their own,
//! with no tie-break: two groups at the same depth came back in whatever
//! order the database returned them.
//!
//! The rule now, in one query (`usage::user_groups_ordered`): the workspace
//! the caller asked for when they really are a member, then their primary
//! group, then the deepest, then by id, so the answer is the same on every
//! call and every surface.

use sqlx::PgPool;

use brutor_gateway_core::models::auth::{AuthContext, AuthType};
use brutor_gateway_core::services::usage;

use crate::common;

const T: &str = "tenant-group-choice";
const USER: i32 = 4201;

fn user_auth(workspace: Option<&str>) -> AuthContext {
    AuthContext {
        credential_kind: brutor_gateway_core::models::auth::CredentialKind::HumanSession,
        credential_assurance: None,
        credential_ref: None,
        credential_detail: Default::default(),
        identity_verdict: None,
        auth_type: AuthType::GatewayJwt,
        tenant_id: T.to_string(),
        gateway_user_id: Some(USER.to_string()),
        gateway_username: Some("trial".to_string()),
        gateway_tenant_id: Some(T.to_string()),
        idp_user_id: None,
        idp_username: None,
        idp_email: None,
        api_key_id: None,
        api_key_name: None,
        api_key_scope: None,
        api_key_group_id: None,
        api_key_is_platform: false,
        api_key_user_id: None,
        allowed_skill_ids: None,
        session_key: None,
        client_ip: None,
        resolved_group_id: workspace.map(str::to_string),
        actor_agent_id: None,
        session_token_type: None,
        agent_impl: Default::default(),
        admission_hold: None,
    }
}

/// The live shape: "rg-default" is created (and joined) first, the user's
/// primary workspace is "rg-e2e", both at the top level, and both carry the
/// same MCP server (the Agent Skills server is bound to both live).
async fn two_top_level_groups(pool: &PgPool, primary: Option<&str>) {
    common::seed_tenant(pool, T).await;
    common::seed_end_user(pool, USER, T).await;
    for g in ["rg-default", "rg-e2e"] {
        common::seed_resource_group(pool, g, T).await;
        common::add_direct_membership(pool, USER, g, T).await;
    }
    common::seed_resource_group(pool, "rg-stranger", T).await;
    sqlx::query("UPDATE end_users SET primary_group_id = $1 WHERE id = $2")
        .bind(primary)
        .bind(USER)
        .execute(pool)
        .await
        .unwrap();
    common::seed_mcp_server(pool, "srv-skills", T, "srv-skills").await;
    for g in ["rg-default", "rg-e2e"] {
        let cfg = format!("cfg-skills-{g}");
        common::seed_server_config(pool, &cfg, T, "srv-skills").await;
        common::bind_server_config_to_rg(pool, &cfg, g).await;
    }
}

/// Every surface's answer for this caller, in one place.
async fn chosen(pool: &PgPool, auth: &AuthContext) -> [Option<String>; 3] {
    let billed = usage::resolve_target_group(pool, auth, T)
        .await
        .map(|g| g.id);
    let mcp = usage::resolve_mcp_server_access(pool, auth, T, "srv-skills")
        .await
        .unwrap()
        .map(|g| g.id);
    let mcp_all_first = usage::resolve_all_mcp_server_groups(pool, auth, T, "srv-skills")
        .await
        .unwrap()
        .first()
        .map(|g| g.id.clone());
    [billed, mcp, mcp_all_first]
}

fn all(id: &str) -> [Option<String>; 3] {
    [Some(id.into()), Some(id.into()), Some(id.into())]
}

/// Was: billed to rg-default, the group the database happened to return
/// first, although the user works in rg-e2e.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_call_runs_in_the_users_primary_group(pool: PgPool) {
    two_top_level_groups(&pool, Some("rg-e2e")).await;
    assert_eq!(chosen(&pool, &user_auth(None)).await, all("rg-e2e"));
}

/// The workspace the portal sends wins when the user really is a member.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_requested_workspace_wins_when_the_user_is_in_it(pool: PgPool) {
    two_top_level_groups(&pool, Some("rg-e2e")).await;
    assert_eq!(
        chosen(&pool, &user_auth(Some("rg-default"))).await,
        all("rg-default")
    );
}

/// A workspace the user is not in is a hint about nothing: it never moves the
/// call into that group, and never off the primary.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_workspace_the_user_is_not_in_changes_nothing(pool: PgPool) {
    two_top_level_groups(&pool, Some("rg-e2e")).await;
    assert_eq!(
        chosen(&pool, &user_auth(Some("rg-stranger"))).await,
        all("rg-e2e")
    );
}

/// No primary, two groups at the same depth: the same group every time,
/// whatever order they were created in (here the later-created one sorts
/// first by id).
#[sqlx::test(migrations = "./tests/migrations")]
async fn without_a_primary_the_tie_breaks_the_same_way_every_time(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_end_user(&pool, USER, T).await;
    for g in ["rg-b", "rg-a"] {
        common::seed_resource_group(&pool, g, T).await;
        common::add_direct_membership(&pool, USER, g, T).await;
    }
    let billed = usage::resolve_target_group(&pool, &user_auth(None), T)
        .await
        .map(|g| g.id);
    assert_eq!(billed.as_deref(), Some("rg-a"));
}

/// The user's guardrails come from the same group their calls run in: was
/// rg-default's guardrail, for a user working in rg-e2e.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_users_guardrails_come_from_their_primary_group(pool: PgPool) {
    two_top_level_groups(&pool, Some("rg-e2e")).await;
    for (gr, g) in [("gr-default", "rg-default"), ("gr-e2e", "rg-e2e")] {
        common::seed_banned_words_guardrail_surfaces(&pool, gr, T, &["x"], &["chat_input"]).await;
        common::bind_guardrail_to_rg(&pool, gr, g).await;
    }
    let state = common::build_test_state(pool.clone());
    let configs = state
        .guardrails_cache
        .get_configs(T, Some(&USER.to_string()), None, &pool)
        .await
        .expect("load");
    assert_eq!(
        configs.iter().map(|c| c.id.as_str()).collect::<Vec<_>>(),
        vec!["gr-e2e"]
    );
}
