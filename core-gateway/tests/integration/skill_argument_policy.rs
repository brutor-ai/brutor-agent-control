// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Argument policy on a skill's input is one control on every way a skill
//! runs.
//!
//! A `skill_input` argument policy used to be evaluated by two of the three
//! callers of `skill_orchestrator::execute_skill`, each in its own way:
//!
//! * the REST route (`POST /v1/proxy/skills/execute`) evaluated it, on a
//!   group it looked up itself — and skipped the policy when that lookup
//!   failed;
//! * `skills__run_script` (the system MCP server) evaluated it and acted on
//!   `deny` only: a rule that should hold the call for a person let it run;
//! * an A2A capability bound to a skill (legacy inbound and v1.0
//!   `message:send`) did not evaluate it at all — a caller could
//!   drive a skill past its input policy by choosing the surface.
//!
//! The check now lives in `execute_skill`, on the group the execution is
//! authorised against, so no caller can skip it. These tests pin the same
//! band on each surface: refused above it, held for a person inside it.
//!
//! The system MCP server's own `mcp_input` policy on a tool
//! (`skills__run_script` as a tool name) is covered too, and is
//! pinned here as well.

use axum::body::Body;
use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::constants::system_server::agent_skill;
use brutor_gateway_core::middleware;
use brutor_gateway_core::routes;

use crate::common;

const T: &str = "t-skill-argpol";
const RG: &str = "rg-skill-argpol";
const CARD: &str = "card-skill-argpol";
const CARD_NAME: &str = "payments-a2a";
const SKILL_ID: &str = "skill-argpol-transfer";
const SKILL_NAME: &str = "transfer-funds";
const SKILLS_SERVER: &str = "ms-skills-argpol";

/// `BRUTOR_SKILL_RUNNER_URL` is process-wide; see `a2a_skill_settle::SERIAL`.
static SERIAL: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

/// A skill runner that answers every call, so a test can count whether the
/// skill ran. Nothing here depends on what it answers.
async fn runner() -> MockServer {
    let runner = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "success": true,
            "output": "ran",
            "error": null,
            "script_results": {},
            "loaded_assets": {},
            "loaded_resources": {},
            "unresolved_refs": [],
            "duration_ms": 1.0
        })))
        .mount(&runner)
        .await;
    unsafe {
        std::env::set_var("BRUTOR_SKILL_RUNNER_URL", runner.uri());
        std::env::set_var(
            "BRUTOR_SKILL_RUNNER_JWT_SECRET",
            "skill-argpol-runner-secret",
        );
    }
    brutor_gateway_core::env_vars::reload_for_tests();
    runner
}

async fn runner_calls(runner: &MockServer) -> usize {
    runner.received_requests().await.unwrap_or_default().len()
}

/// Tenant, group, member key; a skill bound to the group with one script;
/// an A2A card whose capability is bound to the skill; the system skills
/// MCP server bound to the group.
async fn topology(pool: &PgPool) -> String {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_end_user(pool, 9730, T).await;
    common::add_direct_membership(pool, 9730, RG, T).await;
    common::seed_agent_skill(
        pool,
        SKILL_ID,
        T,
        SKILL_NAME,
        "# Transfer\nMoves money.",
        "Moves money",
    )
    .await;
    common::bind_skill_to_rg(pool, SKILL_ID, RG).await;
    sqlx::query(
        "INSERT INTO agent_skill_scripts \
         (id, tenant_id, skill_id, filename, script_content, script_hash, language, execution_mode) \
         VALUES ('scr-argpol', $1, $2, 'transfer.py', 'print(1)', 'h', 'PYTHON', \
                 'SANDBOX'::scriptexecutionmode)",
    )
    .bind(T)
    .bind(SKILL_ID)
    .execute(pool)
    .await
    .expect("seed script");

    common::seed_agent_card(pool, CARD, T, CARD_NAME).await;
    common::bind_agent_card_to_rg(pool, CARD, RG).await;
    sqlx::query(
        "INSERT INTO agent_card_capabilities \
         (id, agent_card_id, capability_name, handler_type, handler_ref, enabled) \
         VALUES ('cap-skill-argpol', $1, 'transfer', 'skill', $2, true)",
    )
    .bind(CARD)
    .bind(SKILL_NAME)
    .execute(pool)
    .await
    .expect("seed skill capability");

    common::seed_mcp_server_with_base_url(pool, SKILLS_SERVER, T, "skills", agent_skill::BASE_URL)
        .await;
    common::seed_server_config(pool, "sc-skills-argpol", T, SKILLS_SERVER).await;
    common::bind_server_config_to_rg(pool, "sc-skills-argpol", RG).await;

    common::seed_api_key(pool, T, RG, Some(9730), "k-skill-argpol").await
}

async fn seed_policy(pool: &PgPool, id: &str, surface: &str, target: &str, rules: Value) {
    seed_policy_with_severity(pool, id, surface, target, "warn", rules).await;
}

/// A policy whose OWN severity is `severity`: what a rule that states none
/// takes.
async fn seed_policy_with_severity(
    pool: &PgPool,
    id: &str,
    surface: &str,
    target: &str,
    severity: &str,
    rules: Value,
) {
    sqlx::query(
        "INSERT INTO argument_policies \
         (id, tenant_id, name, surface, target, argument_key, analyzer, \
          analyzer_config, rules, enabled, severity, created_at, updated_at) \
         VALUES ($1, $2, $1, $3, $4, '*', 'json', NULL, $5::jsonb, true, $6, now(), now())",
    )
    .bind(id)
    .bind(T)
    .bind(surface)
    .bind(target)
    .bind(rules.to_string())
    .bind(severity)
    .execute(pool)
    .await
    .expect("seed policy");
    sqlx::query(
        "INSERT INTO resource_group_argument_policies (resource_group_id, argument_policy_id) \
         VALUES ($1, $2)",
    )
    .bind(RG)
    .bind(id)
    .execute(pool)
    .await
    .expect("bind policy to group");
}

/// The band on the skill's input: refuse over 10,000, hold over 1,000.
/// `amount` is the field's address on every surface: in the caller's object
/// (the REST route, `run_script`, an opaque legacy A2A message) and in the
/// data parts of an A2A Message.
async fn seed_skill_band(pool: &PgPool) {
    seed_policy(
        pool,
        "skill-payments-threshold",
        "skill_input",
        SKILL_NAME,
        json!([
            {"deny_if": "field_gt", "field": "amount", "value": 10000,
             "severity": "deny", "message": "Transfers over 10,000 are not permitted"},
            {"deny_if": "field_gt", "field": "amount", "value": 1000,
             "severity": "approval_required", "message": "Transfers over 1,000 require approval"}
        ]),
    )
    .await;
}

async fn body_json(body: Body) -> Value {
    let bytes = body.collect().await.expect("collect body").to_bytes();
    serde_json::from_slice(&bytes).unwrap_or(Value::Null)
}

async fn send(app: axum::Router, req: axum::http::Request<Body>) -> (StatusCode, Value) {
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    (status, body_json(resp.into_body()).await)
}

async fn rest(pool: &PgPool, key: &str, amount: i64) -> (StatusCode, Value) {
    let app = common::build_multimodal_app(common::build_test_state(pool.clone()));
    let req = common::build_post_request(
        "/v1/proxy/skills/execute",
        json!({"skill_name": SKILL_NAME, "input_params": {"amount": amount}}),
        Some(key),
        Some(T),
    );
    send(app, req).await
}

async fn a2a_legacy(pool: &PgPool, key: &str, amount: i64) -> (StatusCode, Value) {
    let state = common::build_test_state(pool.clone());
    let app = axum::Router::new()
        .nest("/v1/proxy/a2a", routes::a2a::router())
        .layer(middleware::tenant::TenantLayer::new(state.clone()))
        .layer(middleware::run_end::RunEndLayer::new(
            state.mcp_sessions.clone(),
        ))
        .with_state(state);
    let req = common::build_post_request(
        "/v1/proxy/a2a/inbound",
        json!({"target_card_id": CARD, "capability": "transfer", "message": {"amount": amount}}),
        Some(key),
        Some(T),
    );
    send(app, req).await
}

async fn a2a_v1(pool: &PgPool, key: &str, amount: i64) -> (StatusCode, Value) {
    let app = common::build_a2a_v1_app(common::build_test_state(pool.clone()));
    let req = common::build_post_request(
        &format!("/a2a/{T}/{CARD_NAME}/message:send"),
        json!({"message": {"messageId": ulid::Ulid::new().to_string(), "role": "ROLE_USER",
                           "parts": [{"data": {"amount": amount}}]}}),
        Some(key),
        Some(T),
    );
    send(app, req).await
}

/// `skills__run_script` on the system skills server; the JSON-RPC answer.
async fn run_script(
    pool: &PgPool,
    key: &str,
    amount: i64,
    approval_token: Option<&str>,
) -> (StatusCode, Value) {
    let app = common::build_mcp_proxy_app(common::build_test_state(pool.clone()));
    let mut req = common::build_post_request(
        &format!("/v1/proxy/mcp/{SKILLS_SERVER}"),
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": agent_skill::TOOL_RUN_SCRIPT,
                          "arguments": {"skill_name": SKILL_NAME, "script": "transfer.py",
                                        "args": {"amount": amount}}}}),
        Some(key),
        Some(T),
    );
    if let Some(token) = approval_token {
        req.headers_mut()
            .insert("X-Approval-Token", token.parse().unwrap());
    }
    send(app, req).await
}

async fn executions(pool: &PgPool) -> i64 {
    let (n,): (i64,) =
        sqlx::query_as("SELECT count(*) FROM agent_skill_executions WHERE tenant_id = $1")
            .bind(T)
            .fetch_one(pool)
            .await
            .expect("executions");
    n
}

/// Every approval request raised: (id, request_metadata).
async fn approvals(pool: &PgPool) -> Vec<(String, Option<Value>)> {
    sqlx::query_as("SELECT id, request_metadata FROM tool_approval_requests WHERE tenant_id = $1")
        .bind(T)
        .fetch_all(pool)
        .await
        .expect("approvals")
}

/// The first value of `key` anywhere in `v`.
fn find<'a>(v: &'a Value, key: &str) -> Option<&'a Value> {
    match v {
        Value::Object(m) => m.get(key).or_else(|| m.values().find_map(|x| find(x, key))),
        Value::Array(a) => a.iter().find_map(|x| find(x, key)),
        _ => None,
    }
}

// ---------------------------------------------------------------------------
// A deny policy on the skill's input, on every surface
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn rest_refuses_a_skill_input_the_policy_denies(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let key = topology(&pool).await;
    seed_skill_band(&pool).await;

    let (status, body) = rest(&pool, &key, 50_000).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert_eq!(
        body["error"]["code"],
        json!("argument_policy_violation"),
        "{body}"
    );
    assert_eq!(
        body["error"]["policy"],
        json!("skill-payments-threshold"),
        "{body}"
    );
    assert_eq!(body["error"]["rule"], json!("field_gt"), "{body}");
    assert_eq!(executions(&pool).await, 0, "nothing was admitted");

    // The audit row is an argument-policy denial on the caller's group.
    let mut row = None;
    for _ in 0..100 {
        row = sqlx::query_as::<_, (i32, bool, Option<String>, Option<String>)>(
            "SELECT http_status_code, argument_policy_denied, resource_group_id, error_message \
             FROM proxy_logs WHERE tenant_id = $1",
        )
        .bind(T)
        .fetch_optional(&pool)
        .await
        .expect("proxy_logs");
        if row.is_some() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    let (status, denied, group, error) = row.expect("an audit row for the refusal");
    assert_eq!(status, 403);
    assert!(denied, "the row is flagged as an argument-policy denial");
    assert_eq!(group.as_deref(), Some(RG));
    assert!(
        error
            .as_deref()
            .unwrap_or("")
            .contains("Argument policy (skill-payments-threshold/field_gt)"),
        "{error:?}"
    );

    // Below the band the skill still executes.
    let (status, body) = rest(&pool, &key, 500).await;
    assert!(status.is_success(), "{status} {body}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a2a_legacy_refuses_a_skill_input_the_policy_denies_as_rest_does(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_skill_band(&pool).await;

    let (status, body) = a2a_legacy(&pool, &key, 50_000).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "a skill with a deny policy on its input must be refused over A2A: {body}"
    );
    assert!(
        body.to_string().contains("skill-payments-threshold"),
        "the refusal names the policy: {body}"
    );
    assert_eq!(runner_calls(&runner).await, 0, "the skill must not run");
    assert_eq!(executions(&pool).await, 0, "nothing was admitted");

    // Recorded as the argument-policy denial it is, as on REST.
    let mut row = None;
    for _ in 0..100 {
        row = sqlx::query_as::<_, (i32, bool, Option<String>)>(
            "SELECT http_status_code, argument_policy_denied, error_message \
             FROM proxy_logs WHERE tenant_id = $1 AND proxy_type = 'a2a' \
             AND error_message LIKE 'Argument policy%'",
        )
        .bind(T)
        .fetch_optional(&pool)
        .await
        .expect("proxy_logs");
        if row.is_some() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    let (status, denied, error) = row.expect("an audit row for the refusal");
    assert_eq!(status, 403);
    assert!(denied, "flagged as an argument-policy denial: {error:?}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a2a_legacy_holds_a_banded_skill_input_for_a_person(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_skill_band(&pool).await;

    let (status, body) = a2a_legacy(&pool, &key, 8_000).await;
    assert_eq!(
        runner_calls(&runner).await,
        0,
        "a banded call must be held, not run: {status} {body}"
    );
    assert_eq!(executions(&pool).await, 0, "held, not executed: {body}");
    assert_band_metadata(&approvals(&pool).await, "skill-payments-threshold");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a2a_v1_refuses_a_skill_input_the_policy_denies_as_rest_does(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_skill_band(&pool).await;

    let (_status, body) = a2a_v1(&pool, &key, 50_000).await;
    assert_eq!(
        runner_calls(&runner).await,
        0,
        "the skill must not run: {body}"
    );
    assert_eq!(executions(&pool).await, 0, "nothing was admitted: {body}");
    assert_eq!(
        find(&body, "state").and_then(Value::as_str),
        Some("TASK_STATE_FAILED"),
        "{body}"
    );
    assert!(
        body.to_string().contains("skill-payments-threshold"),
        "the refusal names the policy: {body}"
    );
}

// ---------------------------------------------------------------------------
// `approval_required` holds the call, on every surface
// ---------------------------------------------------------------------------

fn assert_band_metadata(approvals: &[(String, Option<Value>)], policy: &str) {
    assert_eq!(approvals.len(), 1, "one call, one hold: {approvals:?}");
    let meta = approvals[0].1.as_ref().expect("request_metadata");
    assert_eq!(meta["policy_name"], json!(policy), "{meta}");
    assert_eq!(meta["rule"], json!("field_gt"), "{meta}");
    assert_eq!(meta["observed"], json!(8000.0), "{meta}");
    assert_eq!(meta["threshold"], json!(1000.0), "{meta}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn rest_holds_a_banded_skill_input_for_a_person(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let key = topology(&pool).await;
    seed_skill_band(&pool).await;

    let (status, body) = rest(&pool, &key, 8_000).await;
    assert!(
        status == StatusCode::ACCEPTED || status == StatusCode::PRECONDITION_REQUIRED,
        "inside the band the call is held: {status} {body}"
    );
    assert_eq!(body["approval_required"], json!(true), "{body}");
    assert_eq!(executions(&pool).await, 0, "held, not executed");
    assert_band_metadata(&approvals(&pool).await, "skill-payments-threshold");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn run_script_holds_a_banded_skill_input_for_a_person(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_skill_band(&pool).await;

    let (status, body) = run_script(&pool, &key, 8_000, None).await;
    assert_eq!(status, StatusCode::OK, "MCP transport answers 200: {body}");
    assert_eq!(
        runner_calls(&runner).await,
        0,
        "a banded call must be held, not run: {body}"
    );
    assert_eq!(executions(&pool).await, 0, "held, not executed: {body}");
    assert_eq!(
        body.pointer("/result/isError"),
        Some(&Value::Bool(true)),
        "{body}"
    );
    let text = body.to_string();
    assert!(text.contains("approval_required"), "{body}");
    let held = approvals(&pool).await;
    assert_band_metadata(&held, "skill-payments-threshold");
    assert!(
        text.contains(&held[0].0),
        "the caller is told which approval to wait for: {body}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn run_script_refuses_a_skill_input_the_policy_denies(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_skill_band(&pool).await;

    let (_, body) = run_script(&pool, &key, 50_000, None).await;
    assert_eq!(
        body.pointer("/result/isError"),
        Some(&Value::Bool(true)),
        "{body}"
    );
    assert!(
        body.to_string()
            .contains("Argument policy (skill-payments-threshold/field_gt)"),
        "{body}"
    );
    assert_eq!(runner_calls(&runner).await, 0);
    assert_eq!(executions(&pool).await, 0);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_band_on_a_system_server_tool_holds_the_call_for_a_person(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    // The `mcp_input` policy an operator attaches to a system-server TOOL
    // (here `skills__run_script` itself). `approval_required` used to be
    // dropped on this path: the tool ran.
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_policy(
        &pool,
        "tool-payments-threshold",
        "mcp_input",
        agent_skill::TOOL_RUN_SCRIPT,
        json!([
            {"deny_if": "field_gt", "field": "args.amount", "value": 10000,
             "severity": "deny", "message": "Transfers over 10,000 are not permitted"},
            {"deny_if": "field_gt", "field": "args.amount", "value": 1000,
             "severity": "approval_required", "message": "Transfers over 1,000 require approval"}
        ]),
    )
    .await;

    let (status, body) = run_script(&pool, &key, 8_000, None).await;
    assert_eq!(
        runner_calls(&runner).await,
        0,
        "a banded call must be held, not run: {status} {body}"
    );
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    assert_eq!(body["approval_required"], json!(true), "{body}");
    let held = approvals(&pool).await;
    assert_band_metadata(&held, "tool-payments-threshold");
    assert_eq!(
        body["approval_request_id"],
        json!(held[0].0),
        "the envelope names the hold: {body}"
    );
    // The hold is on the record as a hold.
    let mut subtype = None;
    for _ in 0..100 {
        subtype = sqlx::query_as::<_, (Option<String>,)>(
            "SELECT proxy_subtype FROM proxy_logs WHERE tenant_id = $1",
        )
        .bind(T)
        .fetch_optional(&pool)
        .await
        .expect("proxy_logs");
        if subtype.is_some() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    assert_eq!(
        subtype.expect("an audit row for the hold").0.as_deref(),
        Some("approval_required")
    );

    // It is a real hold: once a person approves, the same call goes through
    // with the one-time token — and a different amount does not.
    sqlx::query(
        "UPDATE tool_approval_requests \
         SET status = 'approved', approval_token = 'tok-sys-band' WHERE id = $1",
    )
    .bind(&held[0].0)
    .execute(&pool)
    .await
    .expect("approve");
    let (status, body) = run_script(&pool, &key, 9_000, Some("tok-sys-band")).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "a token approved for 8000 must not authorise 9000: {body}"
    );
    assert_eq!(runner_calls(&runner).await, 0);
    let (status, body) = run_script(&pool, &key, 8_000, Some("tok-sys-band")).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(
        runner_calls(&runner).await,
        1,
        "the approved call runs: {body}"
    );

    // Above the band the tool is still refused outright.
    let (status, body) = run_script(&pool, &key, 50_000, None).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
}

// ---------------------------------------------------------------------------
// A policy whose own severity is `approval_required`
// ---------------------------------------------------------------------------

/// A threshold rule that states no severity, in a policy whose own severity
/// is `approval_required`: the rule takes the policy's.
async fn seed_supervised_skill(pool: &PgPool) {
    seed_policy_with_severity(
        pool,
        "skill-payments-supervised",
        "skill_input",
        SKILL_NAME,
        "approval_required",
        json!([{"deny_if": "field_gt", "field": "amount", "value": 1000,
                "message": "Transfers over 1,000 need a person"}]),
    )
    .await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn rest_holds_a_skill_input_under_a_policy_level_approval_required(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_supervised_skill(&pool).await;

    let (status, body) = rest(&pool, &key, 8_000).await;
    assert!(
        status == StatusCode::ACCEPTED || status == StatusCode::PRECONDITION_REQUIRED,
        "held, not refused: {status} {body}"
    );
    assert_eq!(body["approval_required"], json!(true), "{body}");
    assert_eq!(runner_calls(&runner).await, 0, "held, not run: {body}");
    assert_eq!(executions(&pool).await, 0, "held, not executed");
    assert_band_metadata(&approvals(&pool).await, "skill-payments-supervised");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn run_script_holds_a_skill_input_under_a_policy_level_approval_required(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_supervised_skill(&pool).await;

    let (status, body) = run_script(&pool, &key, 8_000, None).await;
    assert_eq!(status, StatusCode::OK, "MCP transport answers 200: {body}");
    assert_eq!(runner_calls(&runner).await, 0, "held, not run: {body}");
    assert!(body.to_string().contains("approval_required"), "{body}");
    assert_band_metadata(&approvals(&pool).await, "skill-payments-supervised");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a2a_holds_a_skill_input_under_a_policy_level_approval_required(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_supervised_skill(&pool).await;

    let (status, body) = a2a_legacy(&pool, &key, 8_000).await;
    assert_eq!(
        runner_calls(&runner).await,
        0,
        "held, not run: {status} {body}"
    );
    assert_band_metadata(&approvals(&pool).await, "skill-payments-supervised");
}

/// The `mcp_input` policy on a system-server tool, with the severity on the
/// policy and none on the rule.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_system_server_tool_is_held_under_a_policy_level_approval_required(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_policy_with_severity(
        &pool,
        "tool-payments-supervised",
        "mcp_input",
        agent_skill::TOOL_RUN_SCRIPT,
        "approval_required",
        json!([{"deny_if": "field_gt", "field": "args.amount", "value": 1000,
                "message": "Transfers over 1,000 need a person"}]),
    )
    .await;

    let (status, body) = run_script(&pool, &key, 8_000, None).await;
    assert_eq!(runner_calls(&runner).await, 0, "held, not run: {body}");
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    assert_eq!(body["approval_required"], json!(true), "{body}");
    assert_band_metadata(&approvals(&pool).await, "tool-payments-supervised");
}

// ---------------------------------------------------------------------------
// What a `skill_input` policy addresses on each surface
// ---------------------------------------------------------------------------

async fn a2a_v1_parts(pool: &PgPool, key: &str, parts: Value) -> (StatusCode, Value) {
    let app = common::build_a2a_v1_app(common::build_test_state(pool.clone()));
    let req = common::build_post_request(
        &format!("/a2a/{T}/{CARD_NAME}/message:send"),
        json!({"message": {"messageId": ulid::Ulid::new().to_string(), "role": "ROLE_USER",
                           "parts": parts}}),
        Some(key),
        Some(T),
    );
    send(app, req).await
}

async fn a2a_legacy_message(pool: &PgPool, key: &str, message: Value) -> (StatusCode, Value) {
    let state = common::build_test_state(pool.clone());
    let app = axum::Router::new()
        .nest("/v1/proxy/a2a", routes::a2a::router())
        .layer(middleware::tenant::TenantLayer::new(state.clone()))
        .layer(middleware::run_end::RunEndLayer::new(
            state.mcp_sessions.clone(),
        ))
        .with_state(state);
    let req = common::build_post_request(
        "/v1/proxy/a2a/inbound",
        json!({"target_card_id": CARD, "capability": "transfer", "message": message}),
        Some(key),
        Some(T),
    );
    send(app, req).await
}

/// A field of a skill's input has ONE address, `amount`, on every surface.
/// Over A2A the structured input is in the message's data parts, and which
/// part carries it is the caller's choice, so the policy judges the data
/// parts' values as one set of fields: the threshold on `amount` sees the
/// amount behind a text part, and an amount given by two data parts has no
/// one value and is refused.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_v1_data_part_is_addressed_by_its_fields_wherever_it_sits(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_skill_band(&pool).await;

    // A text part first does not move the amount out of the band's sight.
    let shifted = json!([{"text": "please"}, {"data": {"amount": 50_000}}]);
    let (_, body) = a2a_v1_parts(&pool, &key, shifted).await;
    assert_eq!(
        runner_calls(&runner).await,
        0,
        "the threshold on `amount` judges the data part wherever it sits: {body}"
    );
    assert!(
        body.to_string().contains("skill-payments-threshold"),
        "{body}"
    );

    // Two data parts that both state the amount: no one value to compare.
    let twice = json!([{"data": {"amount": 500}}, {"data": {"amount": 50_000}}]);
    let (_, body) = a2a_v1_parts(&pool, &key, twice).await;
    assert_eq!(
        runner_calls(&runner).await,
        0,
        "an amount given twice is undecidable, not the smaller one: {body}"
    );
    assert!(
        body.to_string().contains("more than one data part"),
        "{body}"
    );

    // Fields split across data parts are one input.
    let split = json!([{"data": {"memo": "invoice 12"}}, {"text": "pay it"},
                       {"data": {"amount": 500}}]);
    let (_, body) = a2a_v1_parts(&pool, &key, split).await;
    assert_eq!(runner_calls(&runner).await, 1, "{body}");

    // A presence guard works unchanged with this view: a message with no
    // amount in any data part is refused, one with it is served.
    seed_policy(
        &pool,
        "skill-payments-guard",
        "skill_input",
        SKILL_NAME,
        json!([{"allow_only_if": "field_present", "values": ["amount"],
                "severity": "deny", "message": "A transfer states its amount"}]),
    )
    .await;
    for parts in [
        json!([{"text": "transfer 50000 please"}]),
        json!([{"data": {"memo": "no amount"}}]),
    ] {
        let (_, body) = a2a_v1_parts(&pool, &key, parts).await;
        assert_eq!(runner_calls(&runner).await, 1, "{body}");
        assert!(body.to_string().contains("skill-payments-guard"), "{body}");
    }
    let (_, body) = a2a_v1(&pool, &key, 500).await;
    assert_eq!(runner_calls(&runner).await, 2, "{body}");

    // The same policy, the same address, on the REST route.
    let (status, body) = rest(&pool, &key, 500).await;
    assert!(status.is_success(), "{status} {body}");
    let ran = runner_calls(&runner).await;
    let (status, body) = rest(&pool, &key, 50_000).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert_eq!(body["error"]["rule"], json!("field_gt"), "{body}");
    assert_eq!(runner_calls(&runner).await, ran, "{body}");
}

/// The legacy inbound route takes an opaque `message`. A caller that sends
/// a Message there (parts) is judged on its data parts' values, as on v1.0;
/// an opaque body is judged whole, and stays opaque when it has a `parts`
/// member beside fields of its own, so that member cannot hide them.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_legacy_message_with_data_parts_is_judged_on_their_fields(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_skill_band(&pool).await;

    for message in [
        json!({"kind": "message", "messageId": "m-1", "role": "user",
               "parts": [{"kind": "text", "text": "please"},
                         {"kind": "data", "data": {"amount": 50_000}}]}),
        json!({"parts": [{"kind": "data", "data": {"amount": 500}},
                         {"kind": "data", "data": {"amount": 50_000}}]}),
        json!({"amount": 50_000, "parts": []}),
        json!({"amount": 50_000}),
    ] {
        let (status, body) = a2a_legacy_message(&pool, &key, message.clone()).await;
        assert_eq!(status, StatusCode::FORBIDDEN, "{message}: {body}");
        assert!(
            body.to_string().contains("skill-payments-threshold"),
            "{message}: {body}"
        );
    }
    assert_eq!(runner_calls(&runner).await, 0);

    let below = json!({"parts": [{"kind": "data", "data": {"amount": 500}}]});
    let (status, body) = a2a_legacy_message(&pool, &key, below).await;
    assert!(status.is_success(), "{status} {body}");
    assert_eq!(runner_calls(&runner).await, 1, "{body}");
}

// ---------------------------------------------------------------------------
// A skill call held for approval over A2A proceeds after approval
// ---------------------------------------------------------------------------

async fn approve(pool: &PgPool, approval_id: &str, token: &str) {
    sqlx::query(
        "UPDATE tool_approval_requests \
         SET status = 'approved', approval_token = $2, resolved_at = now() WHERE id = $1",
    )
    .bind(approval_id)
    .bind(token)
    .execute(pool)
    .await
    .expect("approve");
}

async fn a2a_legacy_with_token(
    pool: &PgPool,
    key: &str,
    amount: i64,
    token: &str,
) -> (StatusCode, Value) {
    let state = common::build_test_state(pool.clone());
    let app = axum::Router::new()
        .nest("/v1/proxy/a2a", routes::a2a::router())
        .layer(middleware::tenant::TenantLayer::new(state.clone()))
        .layer(middleware::run_end::RunEndLayer::new(
            state.mcp_sessions.clone(),
        ))
        .with_state(state);
    let mut req = common::build_post_request(
        "/v1/proxy/a2a/inbound",
        json!({"target_card_id": CARD, "capability": "transfer", "message": {"amount": amount}}),
        Some(key),
        Some(T),
    );
    req.headers_mut()
        .insert("X-Approval-Token", token.parse().unwrap());
    send(app, req).await
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_skill_call_held_over_legacy_a2a_proceeds_after_approval(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_skill_band(&pool).await;

    // Held: the answer is the approval envelope every other surface gives,
    // so the caller knows which approval to poll.
    let (status, body) = a2a_legacy(&pool, &key, 8_000).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    assert_eq!(body["approval_required"], json!(true), "{body}");
    let held = approvals(&pool).await;
    assert_band_metadata(&held, "skill-payments-threshold");
    assert_eq!(body["approval_request_id"], json!(held[0].0), "{body}");
    assert!(
        body["poll_url"]
            .as_str()
            .is_some_and(|u| u.contains(&held[0].0)),
        "{body}"
    );
    assert_eq!(runner_calls(&runner).await, 0);

    approve(&pool, &held[0].0, "tok-legacy-a2a").await;
    // The approval is for the call it was asked about.
    let (status, body) = a2a_legacy_with_token(&pool, &key, 9_000, "tok-legacy-a2a").await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "a token approved for 8000 must not authorise 9000: {body}"
    );
    assert_eq!(runner_calls(&runner).await, 0);
    let (status, body) = a2a_legacy_with_token(&pool, &key, 8_000, "tok-legacy-a2a").await;
    assert!(
        status.is_success(),
        "the approved call runs: {status} {body}"
    );
    assert_eq!(runner_calls(&runner).await, 1, "{body}");
    assert_eq!(approvals(&pool).await.len(), 1, "no second hold was raised");
    // One approval, one execution.
    let (status, body) = a2a_legacy_with_token(&pool, &key, 8_000, "tok-legacy-a2a").await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert_eq!(runner_calls(&runner).await, 1);
}

/// `message:send` with the parts given, optionally on an existing task and
/// with an approval token.
async fn a2a_v1_send(
    pool: &PgPool,
    key: &str,
    parts: Value,
    task_id: Option<&str>,
    token: Option<&str>,
) -> (StatusCode, Value) {
    let app = common::build_a2a_v1_app(common::build_test_state(pool.clone()));
    let mut message = json!({"messageId": ulid::Ulid::new().to_string(), "role": "ROLE_USER",
                             "parts": parts});
    if let Some(task_id) = task_id {
        message["taskId"] = json!(task_id);
    }
    let mut req = common::build_post_request(
        &format!("/a2a/{T}/{CARD_NAME}/message:send"),
        json!({ "message": message }),
        Some(key),
        Some(T),
    );
    if let Some(token) = token {
        req.headers_mut()
            .insert("X-Approval-Token", token.parse().unwrap());
    }
    send(app, req).await
}

fn task_state(body: &Value) -> Option<&str> {
    find(body, "state").and_then(Value::as_str)
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_skill_call_held_over_a2a_v1_proceeds_after_approval(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_skill_band(&pool).await;
    let parts = json!([{"text": "pay the supplier"}, {"data": {"amount": 8_000}}]);

    // Held: the task waits for an authorisation, and its status says which
    // approval to poll.
    let (status, body) = a2a_v1_send(&pool, &key, parts.clone(), None, None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(
        task_state(&body),
        Some("TASK_STATE_AUTH_REQUIRED"),
        "{body}"
    );
    let held = approvals(&pool).await;
    assert_band_metadata(&held, "skill-payments-threshold");
    assert_eq!(
        find(&body, "approvalRequestId"),
        Some(&json!(held[0].0)),
        "{body}"
    );
    assert!(
        find(&body, "pollUrl")
            .and_then(Value::as_str)
            .is_some_and(|u| u.contains(&held[0].0)),
        "{body}"
    );
    let task_id = find(&body, "id")
        .and_then(Value::as_str)
        .expect("task id")
        .to_string();
    assert_eq!(runner_calls(&runner).await, 0);

    // The hold is on the record as a hold.
    let mut subtype = None;
    for _ in 0..100 {
        subtype = sqlx::query_as::<_, (Option<String>,)>(
            "SELECT proxy_subtype FROM proxy_logs \
             WHERE tenant_id = $1 AND proxy_subtype = 'approval_required'",
        )
        .bind(T)
        .fetch_optional(&pool)
        .await
        .expect("proxy_logs");
        if subtype.is_some() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    assert!(subtype.is_some(), "an audit row records the hold");

    approve(&pool, &held[0].0, "tok-v1-a2a").await;
    // The approval is for the call it was asked about: another amount and
    // another instruction are other calls.
    for other in [
        json!([{"text": "pay the supplier"}, {"data": {"amount": 9_000}}]),
        json!([{"text": "pay someone else"}, {"data": {"amount": 8_000}}]),
    ] {
        let (_, body) = a2a_v1_send(&pool, &key, other.clone(), None, Some("tok-v1-a2a")).await;
        assert_eq!(runner_calls(&runner).await, 0, "{other}: {body}");
        assert_eq!(
            task_state(&body),
            Some("TASK_STATE_FAILED"),
            "{other}: {body}"
        );
    }

    // The same message again, on the held task, with the token: it runs.
    let (status, body) = a2a_v1_send(
        &pool,
        &key,
        parts.clone(),
        Some(&task_id),
        Some("tok-v1-a2a"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(
        runner_calls(&runner).await,
        1,
        "the approved call runs: {body}"
    );
    assert_eq!(task_state(&body), Some("TASK_STATE_COMPLETED"), "{body}");
    assert_eq!(find(&body, "id"), Some(&json!(task_id)), "{body}");
    assert_eq!(approvals(&pool).await.len(), 1, "no second hold was raised");

    // One approval, one execution.
    let (_, body) = a2a_v1_send(&pool, &key, parts, None, Some("tok-v1-a2a")).await;
    assert_eq!(runner_calls(&runner).await, 1, "{body}");
}

/// A policy denial over A2A v1.0 is on the record as a policy denial, as it
/// is over REST and the legacy route.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a2a_v1_records_a_policy_denial_as_a_policy_denial(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_skill_band(&pool).await;

    let (_, body) = a2a_v1(&pool, &key, 50_000).await;
    assert_eq!(task_state(&body), Some("TASK_STATE_FAILED"), "{body}");
    assert_eq!(runner_calls(&runner).await, 0);
    let mut row = None;
    for _ in 0..100 {
        row = sqlx::query_as::<_, (i32, bool, Option<String>)>(
            "SELECT http_status_code, argument_policy_denied, error_message \
             FROM proxy_logs WHERE tenant_id = $1 AND proxy_type = 'a2a' \
             AND error_message LIKE 'Argument policy%'",
        )
        .bind(T)
        .fetch_optional(&pool)
        .await
        .expect("proxy_logs");
        if row.is_some() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    let (status, denied, error) = row.expect("an audit row for the refusal");
    assert_eq!(status, 403);
    assert!(denied, "flagged as an argument-policy denial: {error:?}");
}

// ---------------------------------------------------------------------------
// A failed group lookup is an error, not "no group"
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_failed_group_lookup_refuses_as_unavailable_not_as_no_group(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    use brutor_gateway_core::models::auth::{AuthContext, AuthType};
    use brutor_gateway_core::services::skill_orchestrator;

    topology(&pool).await;
    // A signed-in member of the group, no API key: the lookup is the
    // membership query.
    let auth = AuthContext {
        auth_type: AuthType::GatewayJwt,
        tenant_id: T.to_string(),
        gateway_user_id: Some("9730".to_string()),
        gateway_username: Some("member".to_string()),
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
        resolved_group_id: None,
        actor_agent_id: None,
        session_token_type: None,
        agent_impl: Default::default(),
        admission_hold: None,
    };

    let group = skill_orchestrator::resolve_group(&pool, &auth, T, None)
        .await
        .unwrap_or_else(|e| panic!("the member's group resolves: {}", e.message));
    assert_eq!(group.id, RG);

    // The membership tables are unreadable (an outage, a bad migration).
    crate::db_error_fail_closed::make_unreadable(&pool, "resource_group_memberships").await;
    let err = match skill_orchestrator::resolve_group(&pool, &auth, T, None).await {
        Ok(g) => panic!("a failed lookup must not resolve a group: {}", g.id),
        Err(e) => e,
    };
    assert_eq!(
        err.status_code, 503,
        "a lookup that FAILED is not `no group` (403): {}",
        err.message
    );
    assert_eq!(err.refusal, Some("engine_failure"));
}
