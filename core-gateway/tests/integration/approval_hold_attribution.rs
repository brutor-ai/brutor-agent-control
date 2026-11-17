// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! An approval hold names the run it was raised on, on every surface.
//!
//! The run ledger (`awaiting_human_ms`) and the live run clock find a run's
//! holds by `tool_approval_requests.originating_trace_root_id`. MCP stamped
//! it; A2A outbound, A2A v1 and skills passed `None`, so the time those runs
//! waited on a person counted as runtime in the ledger and on the duration
//! ceiling. Table-driven over every surface that raises a hold; the static
//! guard (`tests/approval_hold_attribution_guard.rs`) covers surfaces that do
//! not exist yet.

use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::middleware;
use brutor_gateway_core::routes;
use brutor_gateway_core::services::SharedState;
use brutor_gateway_core::services::call_chain::{asserted_root, derive_tenant_key};

use crate::common;

const T: &str = "t-holdrun";
const RUN: &str = "run-hold-1";
const CARD: &str = "card-holdrun";
const CARD_NAME: &str = "holdrun-agent";
const SKILL: &str = "sk-holdrun";
const TOOL: &str = "send_email";

#[derive(Clone, Copy, Debug)]
enum Surface {
    Mcp,
    A2aOutbound,
    A2aV1,
    Skill,
}

impl Surface {
    /// The agent grant that parks this surface's action on a human.
    fn grant(self) -> (&'static str, &'static str) {
        match self {
            Surface::Mcp => ("mcp_tool", TOOL),
            Surface::A2aOutbound | Surface::A2aV1 => ("a2a_call", CARD),
            Surface::Skill => ("skill_exec", SKILL),
        }
    }
}

async fn topology(pool: &PgPool, upstream: &str, surface: Surface) -> String {
    topology_with(pool, upstream, surface, Park::Grant).await
}

/// What parks the action on a human.
#[derive(Clone, Copy, Debug, PartialEq)]
enum Park {
    /// The agent's grant says `approval_required`.
    Grant,
    /// The grant allows it, but the AI System was demoted to
    /// `approval_required` (the live demo: a drift response).
    Demoted,
}

fn rg_of(surface: Surface) -> String {
    format!("rg-holdrun-{surface:?}").to_lowercase()
}

async fn topology_with(pool: &PgPool, upstream: &str, surface: Surface, park: Park) -> String {
    let rg = rg_of(surface);
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, &rg, T).await;
    common::seed_end_user(pool, 9951, T).await;
    common::add_direct_membership(pool, 9951, &rg, T).await;
    common::seed_mcp_server_with_base_url(pool, "ms-holdrun", T, "holdrunsrv", upstream).await;
    common::seed_server_config(pool, "sc-holdrun", T, "ms-holdrun").await;
    common::bind_server_config_to_rg(pool, "sc-holdrun", &rg).await;
    common::seed_agent_card(pool, CARD, T, CARD_NAME).await;
    common::bind_agent_card_to_rg(pool, CARD, &rg).await;
    common::seed_agent_skill(pool, SKILL, T, "holdrun-skill", "# Skill", "A skill").await;
    common::bind_skill_to_rg(pool, SKILL, &rg).await;
    let key = common::seed_api_key(pool, T, &rg, Some(9951), "holdrun-key").await;

    let (action, target) = surface.grant();
    sqlx::query(
        "INSERT INTO agent_identities (id, tenant_id, name, source, enabled, status, default_deny) \
         VALUES ('agent-holdrun', $1, 'agent-holdrun', 'manual', true, 'active', true)",
    )
    .bind(T)
    .execute(pool)
    .await
    .expect("agent identity");
    sqlx::query(
        "INSERT INTO agent_grants (id, tenant_id, agent_id, action_type, target, effect, enabled) \
         VALUES ('g-holdrun', $1, 'agent-holdrun', $2, $3, $4, true)",
    )
    .bind(T)
    .bind(action)
    .bind(target)
    .bind(match park {
        Park::Grant => "approval_required",
        Park::Demoted => "allow",
    })
    .execute(pool)
    .await
    .expect("agent grant");
    sqlx::query("UPDATE api_keys SET agent_id = 'agent-holdrun' WHERE tenant_id = $1")
        .bind(T)
        .execute(pool)
        .await
        .expect("bind key to agent");
    if park == Park::Demoted {
        sqlx::query(
            "UPDATE resource_groups SET group_type = 'AI_SYSTEM', system_kind = 'agent', \
             autonomy_level = 'approval_required' WHERE id = $1",
        )
        .bind(&rg)
        .execute(pool)
        .await
        .expect("demote the system");
        sqlx::query(
            "INSERT INTO resource_group_agent_members (tenant_id, agent_id, group_id, role) \
             VALUES ($1, 'agent-holdrun', $2, 'member') ON CONFLICT DO NOTHING",
        )
        .bind(T)
        .bind(&rg)
        .execute(pool)
        .await
        .expect("agent member of the system");
    }
    key
}

fn run_root(state: &SharedState, key: &str) -> String {
    let tenant_key = derive_tenant_key(state.config.encryption_key.as_bytes(), T);
    let auth = format!("Bearer {key}");
    let get = |n: &str| match n {
        "authorization" => Some(auth.clone()),
        "x-brutor-run-id" => Some(RUN.to_string()),
        _ => None,
    };
    asserted_root(&get, &tenant_key)
        .expect("an authenticated caller may assert its run")
        .to_string()
}

async fn raise_hold(state: &SharedState, surface: Surface, key: &str) -> axum::http::StatusCode {
    let (path, body): (String, Value) = match surface {
        Surface::Mcp => (
            "/v1/proxy/mcp/ms-holdrun".into(),
            json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": TOOL, "arguments": {"to": "cfo@example.com"}}}),
        ),
        Surface::A2aOutbound => (
            "/v1/proxy/a2a/outbound".into(),
            json!({"target_card_id": CARD, "capability": "echo", "message": {"text": "hi"}}),
        ),
        Surface::A2aV1 => (
            format!("/a2a/{T}/{CARD_NAME}/message:send"),
            json!({"message": {"messageId": "01holdrun", "role": "ROLE_USER",
                               "parts": [{"text": "hi"}]}}),
        ),
        Surface::Skill => (
            "/v1/proxy/skills/execute".into(),
            json!({"skill_id": SKILL, "input_params": {"period": "Q2"}}),
        ),
    };
    let app = match surface {
        Surface::Mcp => common::build_mcp_proxy_app(state.clone()),
        Surface::A2aV1 => common::build_a2a_v1_app(state.clone()),
        Surface::Skill => common::build_multimodal_app(state.clone()),
        Surface::A2aOutbound => axum::Router::new()
            .nest("/v1/proxy/a2a", routes::a2a::router())
            .layer(middleware::tenant::TenantLayer::new(state.clone()))
            .layer(middleware::run_end::RunEndLayer::new(
                state.mcp_sessions.clone(),
            ))
            .with_state(state.clone()),
    };
    let mut req = common::build_post_request(&path, body, Some(key), Some(T));
    req.headers_mut()
        .insert("x-brutor-run-id", RUN.parse().expect("run header"));
    app.oneshot(req).await.expect("router call").status()
}

async fn hold_names_its_run(pool: PgPool, surface: Surface) {
    let upstream = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"unused": true})))
        .mount(&upstream)
        .await;
    let key = topology(&pool, &upstream.uri(), surface).await;
    let state = common::build_test_state(pool.clone());
    let root = run_root(&state, &key);

    let status = raise_hold(&state, surface, &key).await;
    assert!(
        !status.is_success() || status == axum::http::StatusCode::ACCEPTED,
        "{surface:?}: the action must be parked on a human, got {status}"
    );

    let row: Option<(Option<String>, Option<String>, String)> = sqlx::query_as(
        "SELECT originating_trace_root_id, originating_parent_task_id, status \
         FROM tool_approval_requests WHERE tenant_id = $1",
    )
    .bind(T)
    .fetch_optional(&pool)
    .await
    .expect("approval query");
    let (hold_root, hold_parent, hold_status) =
        row.unwrap_or_else(|| panic!("{surface:?}: no hold was raised (status {status})"));
    assert_eq!(hold_status, "pending", "{surface:?}");
    assert_eq!(
        hold_root.as_deref(),
        Some(root.as_str()),
        "{surface:?}: the hold must name the run it was raised on — the run ledger \
         and the run clock find holds by this column"
    );
    // Every surface names the step — A2A v1 too: it mints the task id before
    // its gates, so the hold names the task its call creates.
    assert!(
        hold_parent.is_some(),
        "{surface:?}: the hold must name the step that raised it"
    );

    // The ledger sees the wait: the run is awaiting a human right now.
    let held = brutor_gateway_core::services::run_ledger::held_ms_between(
        &pool,
        T,
        &root,
        crate::common::db_now() - chrono::Duration::hours(1),
        crate::common::db_now() + chrono::Duration::seconds(1),
    )
    .await
    .expect("held_ms_between");
    assert!(
        held > 0,
        "{surface:?}: the run's hold time must be visible to the ledger, got {held} ms"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_mcp_hold_names_its_run(pool: PgPool) {
    hold_names_its_run(pool, Surface::Mcp).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_a2a_outbound_hold_names_its_run(pool: PgPool) {
    hold_names_its_run(pool, Surface::A2aOutbound).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_a2a_v1_hold_names_its_run(pool: PgPool) {
    hold_names_its_run(pool, Surface::A2aV1).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_skill_hold_names_its_run(pool: PgPool) {
    hold_names_its_run(pool, Surface::Skill).await;
}

// ---------------------------------------------------------------------------
// Every hold takes the group's approval window
// ---------------------------------------------------------------------------
//
// On a running demo system 76 holds expired in a day, all after 300 s:
// skill runs, notes and every hold raised while the system was demoted to
// `approval_required` passed a literal window. The organisation above the
// system now states a day; every surface, whatever parked the action, must
// hold for a day — and say where the window came from.

async fn hold_takes_the_groups_window(pool: PgPool, surface: Surface, park: Park) {
    let upstream = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"unused": true})))
        .mount(&upstream)
        .await;
    let key = topology_with(&pool, &upstream.uri(), surface, park).await;
    common::seed_resource_group(&pool, "rg-holdrun-org", T).await;
    common::set_rg_parent(&pool, &rg_of(surface), "rg-holdrun-org").await;
    common::set_rg_approval_window(&pool, "rg-holdrun-org", Some(86_400)).await;
    // A tenant default must not win over the group chain.
    common::set_tenant_approval_window(&pool, T, Some(3_600)).await;
    let state = common::build_test_state(pool.clone());

    let status = raise_hold(&state, surface, &key).await;
    let row: Option<(i32, f64, Option<String>)> = sqlx::query_as(
        "SELECT timeout_seconds, EXTRACT(EPOCH FROM (expires_at - created_at))::float8, \
                request_metadata->>'approval_window_source' \
         FROM tool_approval_requests WHERE tenant_id = $1",
    )
    .bind(T)
    .fetch_optional(&pool)
    .await
    .expect("approval query");
    let (timeout, span, source) =
        row.unwrap_or_else(|| panic!("{surface:?}/{park:?}: no hold was raised (status {status})"));
    assert_eq!(
        timeout, 86_400,
        "{surface:?}/{park:?}: the hold must last the group's day"
    );
    assert!(
        (span - 86_400.0).abs() < 1.0,
        "{surface:?}/{park:?}: expires_at must follow the window, got {span}s"
    );
    assert_eq!(
        source.as_deref(),
        Some("resource_group"),
        "{surface:?}/{park:?}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_mcp_grant_hold_takes_the_groups_window(pool: PgPool) {
    hold_takes_the_groups_window(pool, Surface::Mcp, Park::Grant).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_a2a_outbound_grant_hold_takes_the_groups_window(pool: PgPool) {
    hold_takes_the_groups_window(pool, Surface::A2aOutbound, Park::Grant).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_a2a_v1_grant_hold_takes_the_groups_window(pool: PgPool) {
    hold_takes_the_groups_window(pool, Surface::A2aV1, Park::Grant).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_skill_grant_hold_takes_the_groups_window(pool: PgPool) {
    hold_takes_the_groups_window(pool, Surface::Skill, Park::Grant).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_mcp_hold_on_a_demoted_system_takes_the_groups_window(pool: PgPool) {
    hold_takes_the_groups_window(pool, Surface::Mcp, Park::Demoted).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_skill_hold_on_a_demoted_system_takes_the_groups_window(pool: PgPool) {
    // The live case: 56 skill-run holds lapsed at 300 s while the demo was
    // demoted by a drift response.
    hold_takes_the_groups_window(pool, Surface::Skill, Park::Demoted).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_a2a_hold_on_a_demoted_system_takes_the_groups_window(pool: PgPool) {
    hold_takes_the_groups_window(pool, Surface::A2aOutbound, Park::Demoted).await;
}
