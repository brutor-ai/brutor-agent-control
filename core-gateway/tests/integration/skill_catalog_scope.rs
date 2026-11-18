// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The skill catalog (`GET /v1/proxy/skills/catalog`, the portal's Skills tab)
//! lists exactly what the Agent Skills server lets the caller load.
//!
//! Regression (portal): a user whose primary workspace was a new
//! top-level AI System saw its model and MCP server in the portal but an
//! empty Skills tab. The catalog resolved its own group, the user's DEEPEST
//! membership, and ignored the active workspace the portal sends
//! (`x-resource-group`); a deeper team group with no skills won. The skills
//! server (`system_server::resolve_scoping_group_ids`) already honoured the
//! workspace, so the two disagreed. The class guard below compares the
//! catalog with the skills server's allowed set directly.

use std::collections::BTreeSet;

use brutor_gateway_core::models::auth::{AuthContext, AuthType};
use brutor_gateway_core::services::{skill_orchestrator, system_server};
use sqlx::PgPool;

use crate::common;

const T: &str = "t-skill-catalog";
const USER: i32 = 7001;

/// A portal user (gateway JWT) whose portal sent `active` as the workspace.
fn portal_user(active: Option<&str>) -> AuthContext {
    AuthContext {
        credential_kind: brutor_gateway_core::models::auth::CredentialKind::HumanSession,
        credential_assurance: None,
        credential_ref: None,
        credential_detail: Default::default(),
        identity_verdict: None,
        auth_type: AuthType::GatewayJwt,
        tenant_id: T.to_string(),
        gateway_user_id: Some(USER.to_string()),
        gateway_username: Some(format!("user-{USER}")),
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
        resolved_group_id: active.map(String::from),
        actor_agent_id: None,
        session_token_type: None,
        agent_impl: Default::default(),
        admission_hold: None,
    }
}

async fn set_root_path(pool: &PgPool, id: &str) {
    sqlx::query(
        "UPDATE resource_groups SET hierarchy_path = $1, hierarchy_level = 0 WHERE id = $1",
    )
    .bind(id)
    .execute(pool)
    .await
    .expect("set_root_path");
}

/// The bug's shape: the user belongs to a top-level AI System (its primary
/// workspace, with one skill) AND to a team two levels down (no skills).
async fn seed(pool: &PgPool) {
    common::seed_tenant(pool, T).await;
    common::seed_end_user(pool, USER, T).await;

    common::seed_resource_group(pool, "assistant", T).await;
    set_root_path(pool, "assistant").await;
    common::seed_agent_skill(
        pool,
        "sk-draft",
        T,
        "draft-reply",
        "# draft",
        "Drafts a reply",
    )
    .await;
    common::bind_skill_to_rg(pool, "sk-draft", "assistant").await;

    common::seed_resource_group(pool, "org", T).await;
    set_root_path(pool, "org").await;
    common::seed_resource_group(pool, "dept", T).await;
    common::seed_resource_group(pool, "team", T).await;
    common::set_group_parent(pool, "dept", "org", "org/dept", 1).await;
    common::set_group_parent(pool, "team", "dept", "org/dept/team", 2).await;

    common::add_direct_membership(pool, USER, "assistant", T).await;
    common::add_direct_membership(pool, USER, "team", T).await;
}

async fn catalog_names(pool: &PgPool, auth: &AuthContext) -> BTreeSet<String> {
    skill_orchestrator::get_catalog(pool, auth, T, None, None)
        .await
        .expect("catalog")
        .skills
        .iter()
        .filter_map(|s| s.get("name").and_then(|v| v.as_str()).map(String::from))
        .collect()
}

/// What the Agent Skills server would let this caller load.
async fn loadable_names(pool: &PgPool, auth: &AuthContext) -> BTreeSet<String> {
    let groups = system_server::resolve_scoping_group_ids(pool, auth)
        .await
        .unwrap()
        .expect("a portal user is group-scoped");
    system_server::allowed_skill_names_for_groups(pool, T, &groups)
        .await
        .unwrap()
        .into_iter()
        .collect()
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_active_workspace_scopes_the_catalog(pool: PgPool) {
    seed(&pool).await;
    let auth = portal_user(Some("assistant"));
    let names = catalog_names(&pool, &auth).await;
    assert_eq!(names, BTreeSet::from(["draft-reply".to_string()]));

    let catalog = skill_orchestrator::get_catalog(&pool, &auth, T, None, None)
        .await
        .unwrap();
    assert_eq!(catalog.group_id.as_deref(), Some("assistant"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_catalog_lists_what_the_skills_server_lets_you_load(pool: PgPool) {
    seed(&pool).await;
    // Inherited too: a skill on the org reaches the team that inherits.
    common::seed_agent_skill(&pool, "sk-org", T, "org-policy", "# org", "Org policy").await;
    common::bind_skill_to_rg(&pool, "sk-org", "org").await;

    for active in [Some("assistant"), Some("team"), None] {
        let auth = portal_user(active);
        assert_eq!(
            catalog_names(&pool, &auth).await,
            loadable_names(&pool, &auth).await,
            "catalog and skills server disagree for workspace {active:?}"
        );
    }
    assert!(
        catalog_names(&pool, &portal_user(Some("team")))
            .await
            .contains("org-policy")
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_workspace_the_user_is_not_in_does_not_widen_the_catalog(pool: PgPool) {
    seed(&pool).await;
    common::seed_resource_group(&pool, "elsewhere", T).await;
    set_root_path(&pool, "elsewhere").await;
    common::seed_agent_skill(&pool, "sk-x", T, "not-yours", "# x", "Not yours").await;
    common::bind_skill_to_rg(&pool, "sk-x", "elsewhere").await;

    let names = catalog_names(&pool, &portal_user(Some("elsewhere"))).await;
    assert!(
        !names.contains("not-yours"),
        "a spoofed workspace header widened the catalog: {names:?}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn discovery_closed_on_the_active_workspace_closes_its_catalog(pool: PgPool) {
    seed(&pool).await;
    common::set_rg_skill_governance(
        &pool,
        "assistant",
        serde_json::json!({"discovery_enabled": false}),
    )
    .await;
    let res =
        skill_orchestrator::get_catalog(&pool, &portal_user(Some("assistant")), T, None, None)
            .await;
    assert!(
        res.is_err(),
        "discovery off on the workspace must close the catalog"
    );
}
