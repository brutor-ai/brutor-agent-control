// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The one approval-window resolver, against a real schema.
//!
//! Precedence: the policy's own window > the Resource Group chain (min-wins,
//! always inherited) > the tenant default > the platform's 300 s. The
//! table-driven surface tests live beside the run-attribution ones
//! (`approval_hold_attribution.rs`: MCP, A2A outbound, A2A v1, skills ×
//! grant / demoted); this file pins the resolver itself, the capability
//! filter's own window, and the skill execution-mode parking.

use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::services::approval_window::{
    self, PLATFORM_DEFAULT_SECONDS, ResolvedWindow, WindowSource,
};

use crate::common;

const T: &str = "t-apwin";

async fn chain(pool: &PgPool) {
    common::seed_tenant(pool, T).await;
    for g in ["aw-org", "aw-dept", "aw-team"] {
        common::seed_resource_group(pool, g, T).await;
    }
    common::set_rg_parent(pool, "aw-dept", "aw-org").await;
    common::set_rg_parent(pool, "aw-team", "aw-dept").await;
}

async fn resolve(pool: &PgPool, group: &str, policy: Option<i32>) -> ResolvedWindow {
    approval_window::resolve(pool, T, Some(group), policy).await
}

// ── the resolver ─────────────────────────────────────────────────────────

#[sqlx::test(migrations = "./tests/migrations")]
async fn nothing_stated_is_the_platform_default(pool: PgPool) {
    chain(&pool).await;
    assert_eq!(
        resolve(&pool, "aw-team", None).await,
        ResolvedWindow {
            seconds: PLATFORM_DEFAULT_SECONDS,
            source: WindowSource::Platform
        }
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_organisation_window_reaches_a_grandchild(pool: PgPool) {
    chain(&pool).await;
    common::set_rg_approval_window(&pool, "aw-org", Some(86_400)).await;
    assert_eq!(
        resolve(&pool, "aw-team", None).await,
        ResolvedWindow {
            seconds: 86_400,
            source: WindowSource::ResourceGroup
        }
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_smallest_window_in_the_chain_wins_not_the_nearest(pool: PgPool) {
    chain(&pool).await;
    common::set_rg_approval_window(&pool, "aw-org", Some(3_600)).await;
    common::set_rg_approval_window(&pool, "aw-team", Some(86_400)).await;
    assert_eq!(resolve(&pool, "aw-team", None).await.seconds, 3_600);
    common::set_rg_approval_window(&pool, "aw-team", Some(600)).await;
    assert_eq!(resolve(&pool, "aw-team", None).await.seconds, 600);
    // A sibling branch is not an ancestor.
    common::seed_resource_group(&pool, "aw-other", T).await;
    common::set_rg_parent(&pool, "aw-other", "aw-org").await;
    assert_eq!(resolve(&pool, "aw-other", None).await.seconds, 3_600);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_tenant_default_fills_in_below_the_groups(pool: PgPool) {
    chain(&pool).await;
    common::set_tenant_approval_window(&pool, T, Some(7_200)).await;
    assert_eq!(
        resolve(&pool, "aw-team", None).await,
        ResolvedWindow {
            seconds: 7_200,
            source: WindowSource::Tenant
        }
    );
    // No group at all (a caller outside every group) still gets the tenant's.
    assert_eq!(
        approval_window::resolve(&pool, T, None, None).await.source,
        WindowSource::Tenant
    );
    common::set_rg_approval_window(&pool, "aw-dept", Some(86_400)).await;
    assert_eq!(
        resolve(&pool, "aw-team", None).await.source,
        WindowSource::ResourceGroup
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_policy_window_overrides_the_default_both_ways(pool: PgPool) {
    chain(&pool).await;
    common::set_rg_approval_window(&pool, "aw-org", Some(86_400)).await;
    assert_eq!(
        resolve(&pool, "aw-team", Some(120)).await,
        ResolvedWindow {
            seconds: 120,
            source: WindowSource::Policy
        }
    );
    common::set_rg_approval_window(&pool, "aw-org", Some(600)).await;
    assert_eq!(
        resolve(&pool, "aw-team", Some(86_400)).await.seconds,
        86_400
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn another_tenants_group_is_not_read(pool: PgPool) {
    chain(&pool).await;
    common::seed_tenant(&pool, "t-apwin-other").await;
    common::seed_resource_group(&pool, "aw-foreign", "t-apwin-other").await;
    common::set_rg_approval_window(&pool, "aw-foreign", Some(86_400)).await;
    assert_eq!(
        resolve(&pool, "aw-foreign", None).await.source,
        WindowSource::Platform
    );
}

// ── the MCP capability filter's own window ───────────────────────────────

async fn mcp_topology(pool: &PgPool, upstream: &str, user: i32) -> String {
    chain(pool).await;
    common::seed_end_user(pool, user, T).await;
    common::add_direct_membership(pool, user, "aw-team", T).await;
    common::seed_mcp_server_with_base_url(pool, "ms-aw", T, "awsrv", upstream).await;
    common::seed_server_config(pool, "sc-aw", T, "ms-aw").await;
    common::bind_server_config_to_rg(pool, "sc-aw", "aw-team").await;
    common::seed_api_key(pool, T, "aw-team", Some(user), "aw-key").await
}

async fn call_tool(pool: &PgPool, key: &str) -> (StatusCode, Value) {
    let app = common::build_mcp_proxy_app(common::build_test_state(pool.clone()));
    let req = common::build_post_request(
        "/v1/proxy/mcp/ms-aw",
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "applications_add_note", "arguments": {"note": "x"}}}),
        Some(key),
        Some(T),
    );
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.expect("body").to_bytes();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

async fn upstream_never_called() -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"unused": true})))
        .expect(0)
        .mount(&mock)
        .await;
    mock
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_approval_required_tool_without_a_window_takes_the_groups(pool: PgPool) {
    // The live case: 20 `applications_add_note` holds lapsed at 300 s.
    let mock = upstream_never_called().await;
    let key = mcp_topology(&pool, &mock.uri(), 9731).await;
    common::set_mcp_capability_filter(
        &pool,
        T,
        "aw-team",
        "sc-aw",
        "tool",
        "applications_add_note",
        "approval_required",
    )
    .await;
    common::set_rg_approval_window(&pool, "aw-org", Some(86_400)).await;

    let (status, body) = call_tool(&pool, &key).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    assert_eq!(body["timeout_seconds"], json!(86_400), "{body}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_tools_own_window_overrides_the_groups(pool: PgPool) {
    let mock = upstream_never_called().await;
    let key = mcp_topology(&pool, &mock.uri(), 9732).await;
    common::set_mcp_capability_filter_with_timeout(
        &pool,
        T,
        "aw-team",
        "sc-aw",
        "tool",
        "applications_add_note",
        "approval_required",
        Some(120),
    )
    .await;
    common::set_rg_approval_window(&pool, "aw-org", Some(86_400)).await;

    let (status, body) = call_tool(&pool, &key).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    assert_eq!(body["timeout_seconds"], json!(120), "{body}");
    let (source,): (Option<String>,) = sqlx::query_as(
        "SELECT request_metadata->>'approval_window_source' FROM tool_approval_requests \
         WHERE tenant_id = $1",
    )
    .bind(T)
    .fetch_one(&pool)
    .await
    .expect("hold row");
    assert_eq!(source.as_deref(), Some("policy"));
}

// ── a skill held by its execution mode ───────────────────────────────────

async fn hold_skill(pool: &PgPool, governance: Value, user: i32) -> i64 {
    hold_skill_with(pool, governance, None, user).await
}

/// Seconds left on the approval request a skill call raised under the
/// group's `governance`, and the skill's own governance on its assignment
/// to the group (`resource_group_agent_skills.governance`) when given.
async fn hold_skill_with(
    pool: &PgPool,
    governance: Value,
    skill_governance: Option<Value>,
    user: i32,
) -> i64 {
    chain(pool).await;
    common::seed_end_user(pool, user, T).await;
    common::add_direct_membership(pool, user, "aw-team", T).await;
    common::seed_agent_skill(pool, "sk-aw", T, "note-writer", "# Notes", "Writes notes").await;
    common::bind_skill_to_rg(pool, "sk-aw", "aw-team").await;
    common::set_rg_skill_governance(pool, "aw-team", governance).await;
    if let Some(own) = skill_governance {
        sqlx::query(
            "UPDATE resource_group_agent_skills SET governance = $1 \
             WHERE resource_group_id = 'aw-team' AND agent_skill_id = 'sk-aw'",
        )
        .bind(own)
        .execute(pool)
        .await
        .expect("the skill's own governance");
    }
    let key = common::seed_api_key(pool, T, "aw-team", Some(user), "aw-skill-key").await;
    let app = common::build_multimodal_app(common::build_test_state(pool.clone()));
    let resp = app
        .oneshot(common::build_post_request(
            "/v1/proxy/skills/execute",
            json!({"skill_id": "sk-aw", "input_params": {"n": "1"}}),
            Some(&key),
            Some(T),
        ))
        .await
        .expect("router call");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.expect("body").to_bytes();
    let body: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    let hold = body["approval_request_id"]
        .as_str()
        .unwrap_or_else(|| panic!("the call is held: {body}"))
        .to_string();
    let (expires, timeout): (chrono::DateTime<chrono::Utc>, i32) = sqlx::query_as(
        "SELECT expires_at, timeout_seconds FROM tool_approval_requests \
         WHERE id = $1 AND capability_type = 'skill_exec'",
    )
    .bind(&hold)
    .fetch_one(pool)
    .await
    .expect("the hold row");
    let (executions,): (i64,) =
        sqlx::query_as("SELECT COUNT(*) FROM agent_skill_executions WHERE tenant_id = $1")
            .bind(T)
            .fetch_one(pool)
            .await
            .expect("executions");
    assert_eq!(executions, 0, "a held call is not an execution");
    let left = (expires - crate::common::db_now()).num_seconds();
    assert!(
        (i64::from(timeout) - 10..=i64::from(timeout)).contains(&left),
        "the deadline is the window: {left}s of {timeout}s"
    );
    left
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_skill_held_by_its_mode_takes_the_groups_window(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "aw-org", T).await;
    common::set_rg_approval_window(&pool, "aw-org", Some(86_400)).await;
    let left = hold_skill(&pool, json!({"execution_mode": "approval_required"}), 9741).await;
    assert!(
        (86_390..=86_400).contains(&left),
        "the execution mode states no window, so the group's day applies: {left}s"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_skill_held_by_its_mode_keeps_the_modes_own_window(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "aw-org", T).await;
    common::set_rg_approval_window(&pool, "aw-org", Some(86_400)).await;
    let left = hold_skill(
        &pool,
        json!({"execution_mode": "approval_required", "approval_timeout_seconds": 120}),
        9742,
    )
    .await;
    assert!(
        (110..=120).contains(&left),
        "policy window honoured: {left}s"
    );
}

/// A skill's own approval timeout, set on its assignment to the group, is
/// the window of its hold: its own when set (shorter or longer than the
/// group's), else the group's.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_skill_held_by_its_mode_takes_the_skills_own_window_when_it_states_one(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "aw-org", T).await;
    let left = hold_skill_with(
        &pool,
        json!({"execution_mode": "approval_required", "approval_timeout_seconds": 900}),
        Some(json!({"approval_timeout_seconds": 1800})),
        9743,
    )
    .await;
    assert!(
        (1790..=1800).contains(&left),
        "the skill states 1800 s of its own; the group's 900 s applied: {left}s"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_skill_held_by_its_mode_without_a_window_of_its_own_takes_the_groups(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "aw-org", T).await;
    let left = hold_skill_with(
        &pool,
        json!({"execution_mode": "approval_required", "approval_timeout_seconds": 900}),
        Some(json!({"execution_mode": "approval_required"})),
        9744,
    )
    .await;
    assert!((890..=900).contains(&left), "{left}s");
}
