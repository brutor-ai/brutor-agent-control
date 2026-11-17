// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Governance must reach every descendant, whatever a group sets for itself.
//!
//! Three paths read a group's policy and each had to walk the ancestor chain
//! to be correct. Two did not:
//!
//! * the MCP group-wide default (`resolve_mcp_global_default`) read a single
//!   group's `mcp_global_governance`, so an organisation that set
//!   `default_tool_access: disabled` did not reach a department with no MCP
//!   governance of its own — the department's tools stayed callable;
//! * the skill CATALOG gate read `skill_global_governance` straight off the
//!   row `usage::resolve_target_group` returned, while the EXECUTION path
//!   resolved through `skill_orchestrator::resolve_group`, which bakes the
//!   ancestor merge in. A parent that turned discovery off closed execution
//!   and left the catalog open.
//!
//! Both are fixed; these tests pin them against a real hierarchy, because
//! the bugs were invisible to unit tests that only ever built one group.

use serde_json::json;
use sqlx::PgPool;

use brutor_gateway_core::models::auth::{AuthContext, AuthType};
use brutor_gateway_core::services::usage::{self, CapabilityAccess};

use crate::common;

/// An API key scoped to one group — the simplest way to make
/// `resolve_target_group` land on a specific node of the hierarchy.
fn auth_for_group(group_id: &str) -> AuthContext {
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
        api_key_group_id: Some(group_id.to_string()),
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

const T: &str = "t-gov-inherit";

/// org → dept → team, with nothing set below the org.
async fn seed_chain(pool: &PgPool, prefix: &str) -> (String, String, String) {
    let org = format!("{prefix}-org");
    let dept = format!("{prefix}-dept");
    let team = format!("{prefix}-team");
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, &org, T).await;
    common::seed_resource_group(pool, &dept, T).await;
    common::seed_resource_group(pool, &team, T).await;
    common::set_group_parent(pool, &dept, &org, &format!("{org}/{dept}"), 1).await;
    common::set_group_parent(pool, &team, &dept, &format!("{org}/{dept}/{team}"), 2).await;
    (org, dept, team)
}

async fn group_row(pool: &PgPool, id: &str) -> brutor_gateway_core::models::db::ResourceGroup {
    sqlx::query_as::<_, brutor_gateway_core::models::db::ResourceGroup>(
        "SELECT rg.*, rg.group_type::text AS group_type FROM resource_groups rg WHERE rg.id = $1",
    )
    .bind(id)
    .fetch_one(pool)
    .await
    .expect("group row")
}

// ── MCP group-wide default access ───────────────────────────────────────

#[sqlx::test(migrations = "./tests/migrations")]
async fn mcp_default_denial_reaches_a_grandchild_that_sets_nothing(pool: PgPool) {
    let (org, _dept, team) = seed_chain(&pool, "mcp-a").await;
    common::seed_mcp_server(&pool, "srv-a", T, "server-a").await;
    common::seed_server_config(&pool, "cfg-a", T, "srv-a").await;
    common::bind_server_config_to_rg(&pool, "cfg-a", &team).await;

    // The organisation disables tools; nothing below it says anything.
    common::set_rg_mcp_governance(&pool, &org, json!({"default_tool_access": "disabled"})).await;

    let access = usage::check_mcp_capability_access(
        &pool,
        "srv-a",
        "tool",
        "anything",
        &[group_row(&pool, &team).await],
    )
    .await;

    assert!(
        matches!(access, CapabilityAccess::Denied),
        "an ancestor's default_tool_access must bind a descendant, got {access:?}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn mcp_default_approval_reaches_a_grandchild_with_its_timeout(pool: PgPool) {
    let (org, _dept, team) = seed_chain(&pool, "mcp-b").await;
    common::seed_mcp_server(&pool, "srv-b", T, "server-b").await;
    common::seed_server_config(&pool, "cfg-b", T, "srv-b").await;
    common::bind_server_config_to_rg(&pool, "cfg-b", &team).await;
    common::set_rg_mcp_governance(
        &pool,
        &org,
        json!({"default_tool_access": "approval_required", "approval_timeout_seconds": 45}),
    )
    .await;

    let access = usage::check_mcp_capability_access(
        &pool,
        "srv-b",
        "tool",
        "anything",
        &[group_row(&pool, &team).await],
    )
    .await;

    match access {
        CapabilityAccess::ApprovalRequired { window_seconds } => {
            assert_eq!(
                window_seconds,
                Some(45),
                "the ancestor's timeout must travel too"
            );
        }
        other => panic!("expected ApprovalRequired, got {other:?}"),
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_strictest_level_in_the_chain_wins(pool: PgPool) {
    let (org, dept, team) = seed_chain(&pool, "mcp-c").await;
    common::seed_mcp_server(&pool, "srv-c", T, "server-c").await;
    common::seed_server_config(&pool, "cfg-c", T, "srv-c").await;
    common::bind_server_config_to_rg(&pool, "cfg-c", &team).await;

    // The department is more permissive than the organisation; the
    // organisation must still win.
    common::set_rg_mcp_governance(&pool, &org, json!({"default_tool_access": "disabled"})).await;
    common::set_rg_mcp_governance(&pool, &dept, json!({"default_tool_access": "enabled"})).await;

    let access = usage::check_mcp_capability_access(
        &pool,
        "srv-c",
        "tool",
        "anything",
        &[group_row(&pool, &team).await],
    )
    .await;
    assert!(
        matches!(access, CapabilityAccess::Denied),
        "a child cannot re-enable what an ancestor disabled, got {access:?}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_chain_with_no_governance_still_allows(pool: PgPool) {
    let (_org, _dept, team) = seed_chain(&pool, "mcp-d").await;
    common::seed_mcp_server(&pool, "srv-d", T, "server-d").await;
    common::seed_server_config(&pool, "cfg-d", T, "srv-d").await;
    common::bind_server_config_to_rg(&pool, "cfg-d", &team).await;

    let access = usage::check_mcp_capability_access(
        &pool,
        "srv-d",
        "tool",
        "anything",
        &[group_row(&pool, &team).await],
    )
    .await;
    assert!(
        matches!(access, CapabilityAccess::Allowed),
        "no policy anywhere in the chain means no restriction, got {access:?}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_per_capability_filter_still_takes_precedence_over_the_default(pool: PgPool) {
    let (org, _dept, team) = seed_chain(&pool, "mcp-e").await;
    common::seed_mcp_server(&pool, "srv-e", T, "server-e").await;
    common::seed_server_config(&pool, "cfg-e", T, "srv-e").await;
    common::bind_server_config_to_rg(&pool, "cfg-e", &team).await;

    // Organisation-wide default is deny, but this one tool is named and
    // allowed on the team. Named filters are the more specific statement.
    common::set_rg_mcp_governance(&pool, &org, json!({"default_tool_access": "disabled"})).await;
    common::set_mcp_capability_filter(&pool, T, &team, "cfg-e", "tool", "safe_tool", "enabled")
        .await;

    let named = usage::check_mcp_capability_access(
        &pool,
        "srv-e",
        "tool",
        "safe_tool",
        &[group_row(&pool, &team).await],
    )
    .await;
    assert!(
        matches!(named, CapabilityAccess::Allowed),
        "a named filter must still decide its own capability, got {named:?}"
    );

    // A tool the filters do not name falls through to the inherited default,
    // and a filter existing for this type means unlisted names are denied.
    let unnamed = usage::check_mcp_capability_access(
        &pool,
        "srv-e",
        "tool",
        "other_tool",
        &[group_row(&pool, &team).await],
    )
    .await;
    assert!(
        matches!(unnamed, CapabilityAccess::Denied),
        "an unlisted tool must not be more permissive than the default, got {unnamed:?}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_enabled_filter_row_still_reports_its_hold_window(pool: PgPool) {
    // A band can hold a tool the filter leaves `enabled`.
    // The only place an operator can lengthen that hold without making the
    // tool approval-required is the filter row's approval_timeout_seconds,
    // which the resolver used to discard for `enabled` rows.
    let (_org, _dept, team) = seed_chain(&pool, "mcp-f").await;
    common::seed_mcp_server(&pool, "srv-f", T, "server-f").await;
    common::seed_server_config(&pool, "cfg-f", T, "srv-f").await;
    common::bind_server_config_to_rg(&pool, "cfg-f", &team).await;
    common::set_mcp_capability_filter_with_timeout(
        &pool,
        T,
        &team,
        "cfg-f",
        "tool",
        "applications_set_recommendation",
        "enabled",
        Some(86_400),
    )
    .await;

    let resolution = usage::check_mcp_capability_access_with_hold_window(
        &pool,
        "srv-f",
        "tool",
        "applications_set_recommendation",
        &[group_row(&pool, &team).await],
    )
    .await;
    assert!(
        matches!(resolution.access, CapabilityAccess::Allowed),
        "an enabled row with a timeout is still enabled, got {:?}",
        resolution.access
    );
    assert_eq!(
        resolution.hold_window_seconds,
        Some(86_400),
        "the enabled row's window must survive resolution"
    );

    // The narrow accessor is unchanged for its existing callers.
    let access = usage::check_mcp_capability_access(
        &pool,
        "srv-f",
        "tool",
        "applications_set_recommendation",
        &[group_row(&pool, &team).await],
    )
    .await;
    assert!(matches!(access, CapabilityAccess::Allowed));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_hold_window_is_the_smallest_in_the_chain(pool: PgPool) {
    // Same restrictive merge as every other timeout: the organisation says a
    // day, the team says an hour; the hold gets an hour.
    let (org, _dept, team) = seed_chain(&pool, "mcp-g").await;
    common::seed_mcp_server(&pool, "srv-g", T, "server-g").await;
    common::seed_server_config(&pool, "cfg-g", T, "srv-g").await;
    common::bind_server_config_to_rg(&pool, "cfg-g", &team).await;
    common::set_mcp_capability_filter_with_timeout(
        &pool,
        T,
        &org,
        "cfg-g",
        "tool",
        "t",
        "enabled",
        Some(86_400),
    )
    .await;
    common::set_mcp_capability_filter_with_timeout(
        &pool,
        T,
        &team,
        "cfg-g",
        "tool",
        "t",
        "enabled",
        Some(3_600),
    )
    .await;

    let resolution = usage::check_mcp_capability_access_with_hold_window(
        &pool,
        "srv-g",
        "tool",
        "t",
        &[group_row(&pool, &team).await],
    )
    .await;
    assert!(matches!(resolution.access, CapabilityAccess::Allowed));
    assert_eq!(resolution.hold_window_seconds, Some(3_600));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_row_without_a_window_reports_none(pool: PgPool) {
    let (_org, _dept, team) = seed_chain(&pool, "mcp-h").await;
    common::seed_mcp_server(&pool, "srv-h", T, "server-h").await;
    common::seed_server_config(&pool, "cfg-h", T, "srv-h").await;
    common::bind_server_config_to_rg(&pool, "cfg-h", &team).await;
    common::set_mcp_capability_filter(&pool, T, &team, "cfg-h", "tool", "t", "enabled").await;

    let resolution = usage::check_mcp_capability_access_with_hold_window(
        &pool,
        "srv-h",
        "tool",
        "t",
        &[group_row(&pool, &team).await],
    )
    .await;
    assert!(matches!(resolution.access, CapabilityAccess::Allowed));
    assert_eq!(
        resolution.hold_window_seconds, None,
        "no window set anywhere means the caller falls back to the default"
    );
}

// ── Skill discovery ─────────────────────────────────────────────────────

#[sqlx::test(migrations = "./tests/migrations")]
async fn skill_discovery_disabled_by_an_ancestor_closes_the_catalog(pool: PgPool) {
    let (org, _dept, team) = seed_chain(&pool, "skill-a").await;
    // The organisation turns discovery off; the team says nothing.
    common::set_rg_skill_governance(&pool, &org, json!({"discovery_enabled": false})).await;

    let auth = auth_for_group(&team);
    let result =
        brutor_gateway_core::services::skill_orchestrator::get_catalog(&pool, &auth, T, None, None)
            .await;

    assert!(
        result.is_err(),
        "a parent that turned discovery off must close the catalog for its descendants"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn skill_discovery_stays_open_when_no_ancestor_closed_it(pool: PgPool) {
    let (org, _dept, team) = seed_chain(&pool, "skill-b").await;
    common::set_rg_skill_governance(&pool, &org, json!({"discovery_enabled": true})).await;

    let auth = auth_for_group(&team);
    let result =
        brutor_gateway_core::services::skill_orchestrator::get_catalog(&pool, &auth, T, None, None)
            .await;

    assert!(result.is_ok(), "discovery left on must not be blocked");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_child_cannot_reopen_discovery_its_parent_closed(pool: PgPool) {
    let (org, _dept, team) = seed_chain(&pool, "skill-c").await;
    common::set_rg_skill_governance(&pool, &org, json!({"discovery_enabled": false})).await;
    common::set_rg_skill_governance(&pool, &team, json!({"discovery_enabled": true})).await;

    let auth = auth_for_group(&team);
    let result =
        brutor_gateway_core::services::skill_orchestrator::get_catalog(&pool, &auth, T, None, None)
            .await;

    assert!(
        result.is_err(),
        "discovery_enabled merges with AND — a child cannot re-open it"
    );
}
