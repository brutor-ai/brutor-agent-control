// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! One request, several approval gates.
//!
//! A request can pass more than one gate that asks for a person: the route's
//! own (an agent's grant, the A2A policy, a tool's capability filter, an
//! argument-policy band on a system-server tool) and, behind it, the gates
//! of the skill the request runs. The rule
//! (`agent_approval::RequestApprovals`):
//!
//! * gates that ask the same question (same requester, same action, same
//!   canonical arguments) are satisfied by one approval;
//! * a gate that asks a different question needs its own approval, raised in
//!   the same held answer; the caller retries once with every token;
//! * a token that answers no gate of the request refuses it, and no token is
//!   redeemed.
//!
//! Each of these calls could never complete before: the first gate redeemed
//! the request's one token and the second found none.

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

const T: &str = "t-two-holds";
const RG: &str = "rg-two-holds";
const CARD: &str = "card-two-holds";
const CARD_NAME: &str = "payments-two-holds";
const SKILL_ID: &str = "skill-two-holds-transfer";
const SKILL_NAME: &str = "transfer-two-holds";
const SKILLS_SERVER: &str = "ms-skills-two-holds";
const AGENT: &str = "agent-two-holds";

/// `BRUTOR_SKILL_RUNNER_URL` is process-wide.
static SERIAL: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

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
        std::env::set_var("BRUTOR_SKILL_RUNNER_JWT_SECRET", "two-holds-runner-secret");
    }
    brutor_gateway_core::env_vars::reload_for_tests();
    runner
}

async fn runner_calls(runner: &MockServer) -> usize {
    runner.received_requests().await.unwrap_or_default().len()
}

/// Tenant, group, member key; a skill with one script bound to the group; an
/// A2A card whose capability runs the skill; the system skills server.
async fn topology(pool: &PgPool) -> String {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_end_user(pool, 9760, T).await;
    common::add_direct_membership(pool, 9760, RG, T).await;
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
         VALUES ('scr-two-holds', $1, $2, 'transfer.py', 'print(1)', 'h', 'PYTHON', \
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
         VALUES ('cap-two-holds', $1, 'transfer', 'skill', $2, true)",
    )
    .bind(CARD)
    .bind(SKILL_NAME)
    .execute(pool)
    .await
    .expect("seed skill capability");

    common::seed_mcp_server_with_base_url(pool, SKILLS_SERVER, T, "skills", agent_skill::BASE_URL)
        .await;
    common::seed_server_config(pool, "sc-skills-two-holds", T, SKILLS_SERVER).await;
    common::bind_server_config_to_rg(pool, "sc-skills-two-holds", RG).await;

    common::seed_api_key(pool, T, RG, Some(9760), "k-two-holds").await
}

async fn seed_policy(pool: &PgPool, id: &str, surface: &str, target: &str, rules: Value) {
    sqlx::query(
        "INSERT INTO argument_policies \
         (id, tenant_id, name, surface, target, argument_key, analyzer, \
          analyzer_config, rules, enabled, severity, created_at, updated_at) \
         VALUES ($1, $2, $1, $3, $4, '*', 'json', NULL, $5::jsonb, true, 'warn', now(), now())",
    )
    .bind(id)
    .bind(T)
    .bind(surface)
    .bind(target)
    .bind(rules.to_string())
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

/// The skill's own gate: its input is held for a person over 1,000.
async fn seed_skill_band(pool: &PgPool) {
    seed_policy(
        pool,
        "two-holds-skill-band",
        "skill_input",
        SKILL_NAME,
        json!([{"deny_if": "field_gt", "field": "amount", "value": 1000,
                "severity": "approval_required",
                "message": "Transfers over 1,000 require approval"}]),
    )
    .await;
}

/// The A2A route's gate: the group's A2A policy holds every call that does
/// work.
async fn seed_a2a_policy_hold(pool: &PgPool) {
    sqlx::query("UPDATE resource_groups SET a2a_global_governance = $2 WHERE id = $1")
        .bind(RG)
        .bind(json!({"capability": {"default_access": "approval_required"}}))
        .execute(pool)
        .await
        .expect("group policy");
}

/// The key becomes an agent's, with one grant.
async fn bind_agent(pool: &PgPool, action_type: &str, target: &str, effect: &str) {
    sqlx::query(
        "INSERT INTO agent_identities (id, tenant_id, name, source, enabled, status, default_deny) \
         VALUES ($1, $2, $1, 'manual', true, 'active', false) ON CONFLICT DO NOTHING",
    )
    .bind(AGENT)
    .bind(T)
    .execute(pool)
    .await
    .expect("agent identity");
    sqlx::query(
        "INSERT INTO agent_grants (id, tenant_id, agent_id, action_type, target, effect, enabled) \
         VALUES ($1, $2, $3, $4, $5, $6, true)",
    )
    .bind(format!("g-{action_type}"))
    .bind(T)
    .bind(AGENT)
    .bind(action_type)
    .bind(target)
    .bind(effect)
    .execute(pool)
    .await
    .expect("agent grant");
    sqlx::query("UPDATE api_keys SET agent_id = $1 WHERE tenant_id = $2 AND name = 'k-two-holds'")
        .bind(AGENT)
        .bind(T)
        .execute(pool)
        .await
        .expect("bind key to agent");
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

/// Each token in its own `X-Approval-Token` header.
fn with_tokens(mut req: axum::http::Request<Body>, tokens: &[&str]) -> axum::http::Request<Body> {
    for token in tokens {
        req.headers_mut()
            .append("X-Approval-Token", token.parse().expect("token"));
    }
    req
}

async fn a2a_legacy(pool: &PgPool, key: &str, amount: i64, tokens: &[&str]) -> (StatusCode, Value) {
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
    send(app, with_tokens(req, tokens)).await
}

async fn a2a_v1(
    pool: &PgPool,
    key: &str,
    amount: i64,
    task_id: Option<&str>,
    tokens: &[&str],
) -> (StatusCode, Value) {
    let app = common::build_a2a_v1_app(common::build_test_state(pool.clone()));
    let mut message = json!({"messageId": ulid::Ulid::new().to_string(), "role": "ROLE_USER",
                             "parts": [{"data": {"amount": amount}}]});
    if let Some(task_id) = task_id {
        message["taskId"] = json!(task_id);
    }
    let req = common::build_post_request(
        &format!("/a2a/{T}/{CARD_NAME}/message:send"),
        json!({ "message": message }),
        Some(key),
        Some(T),
    );
    send(app, with_tokens(req, tokens)).await
}

/// `skills__run_script` on the system skills server.
async fn run_script(
    pool: &PgPool,
    key: &str,
    arguments: Value,
    tokens: &[&str],
) -> (StatusCode, Value) {
    let app = common::build_mcp_proxy_app(common::build_test_state(pool.clone()));
    let req = common::build_post_request(
        &format!("/v1/proxy/mcp/{SKILLS_SERVER}"),
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": agent_skill::TOOL_RUN_SCRIPT, "arguments": arguments}}),
        Some(key),
        Some(T),
    );
    send(app, with_tokens(req, tokens)).await
}

fn script_arguments(amount: i64) -> Value {
    json!({"skill_name": SKILL_NAME, "script": "transfer.py", "args": {"amount": amount}})
}

/// Every approval request: (id, surface, capability_type, status).
async fn approvals(pool: &PgPool) -> Vec<(String, String, String, String)> {
    sqlx::query_as(
        "SELECT id, request_metadata->>'surface', capability_type, status \
         FROM tool_approval_requests WHERE tenant_id = $1 ORDER BY created_at, id",
    )
    .bind(T)
    .fetch_all(pool)
    .await
    .expect("approvals")
}

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

/// The approval of the given capability type among `held`.
fn of_type<'a>(held: &'a [(String, String, String, String)], capability_type: &str) -> &'a str {
    held.iter()
        .find(|h| h.2 == capability_type)
        .unwrap_or_else(|| panic!("no {capability_type} approval among {held:?}"))
        .0
        .as_str()
}

fn statuses(held: &[(String, String, String, String)]) -> Vec<&str> {
    held.iter().map(|h| h.3.as_str()).collect()
}

/// The ids listed under `approvals` of a held answer.
fn listed(body: &Value) -> Vec<String> {
    body["approvals"]
        .as_array()
        .map(|a| {
            a.iter()
                .filter_map(|h| h["approval_request_id"].as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default()
}

fn find<'a>(v: &'a Value, key: &str) -> Option<&'a Value> {
    match v {
        Value::Object(m) => m.get(key).or_else(|| m.values().find_map(|x| find(x, key))),
        Value::Array(a) => a.iter().find_map(|x| find(x, key)),
        _ => None,
    }
}

// ---------------------------------------------------------------------------
// Legacy A2A: the route's policy hold and the skill's band
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_policy_hold_and_a_skill_band_are_raised_together_and_redeemed_together(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_a2a_policy_hold(&pool).await;
    seed_skill_band(&pool).await;

    // One held answer names both approvals.
    let (status, body) = a2a_legacy(&pool, &key, 8_000, &[]).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    let held = approvals(&pool).await;
    assert_eq!(held.len(), 2, "both holds are raised at once: {held:?}");
    let card_hold = of_type(&held, "a2a_call").to_string();
    let skill_hold = of_type(&held, "skill_exec").to_string();
    let mut ids = listed(&body);
    ids.sort();
    let mut expected = vec![card_hold.clone(), skill_hold.clone()];
    expected.sort();
    assert_eq!(ids, expected, "the answer lists both: {body}");
    assert_eq!(body["approvals_needed"], json!(2), "{body}");
    assert_eq!(runner_calls(&runner).await, 0);

    approve(&pool, &card_hold, "tok-card").await;
    approve(&pool, &skill_hold, "tok-skill").await;

    // The caller retries once, carrying both tokens: the call runs.
    let (status, body) = a2a_legacy(&pool, &key, 8_000, &["tok-card", "tok-skill"]).await;
    assert!(
        status.is_success(),
        "the approved call runs: {status} {body}"
    );
    assert_eq!(runner_calls(&runner).await, 1, "{body}");
    let held = approvals(&pool).await;
    assert_eq!(statuses(&held), ["consumed", "consumed"], "{held:?}");

    // Each token was for one call.
    let (status, body) = a2a_legacy(&pool, &key, 8_000, &["tok-card", "tok-skill"]).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert_eq!(runner_calls(&runner).await, 1);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn two_tokens_in_one_comma_separated_header_are_two_tokens(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_a2a_policy_hold(&pool).await;
    seed_skill_band(&pool).await;

    let (status, body) = a2a_legacy(&pool, &key, 8_000, &[]).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    let held = approvals(&pool).await;
    approve(&pool, of_type(&held, "a2a_call"), "tok-card").await;
    approve(&pool, of_type(&held, "skill_exec"), "tok-skill").await;

    let (status, body) = a2a_legacy(&pool, &key, 8_000, &["tok-skill, tok-card"]).await;
    assert!(status.is_success(), "{status} {body}");
    assert_eq!(runner_calls(&runner).await, 1, "{body}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_token_for_one_of_two_holds_is_kept_while_the_other_is_asked_for(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_a2a_policy_hold(&pool).await;
    seed_skill_band(&pool).await;

    let (status, body) = a2a_legacy(&pool, &key, 8_000, &[]).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    let held = approvals(&pool).await;
    let card_hold = of_type(&held, "a2a_call").to_string();
    let skill_hold = of_type(&held, "skill_exec").to_string();
    approve(&pool, &card_hold, "tok-card").await;

    // Only the route's approval was granted so far. The call is held for
    // the skill's alone, and the token it carried is not redeemed.
    let (status, body) = a2a_legacy(&pool, &key, 8_000, &["tok-card"]).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    assert_eq!(runner_calls(&runner).await, 0, "{body}");
    let held = approvals(&pool).await;
    assert_eq!(held.len(), 3, "one more hold, for the skill: {held:?}");
    let card_row = held.iter().find(|h| h.0 == card_hold).expect("card hold");
    assert_eq!(card_row.3, "approved", "the token was not burned: {held:?}");
    let new_skill_hold = held
        .iter()
        .find(|h| h.2 == "skill_exec" && h.0 != skill_hold)
        .expect("a new skill hold")
        .0
        .clone();
    assert_eq!(listed(&body), vec![new_skill_hold.clone()], "{body}");
    assert_eq!(body["approvals_needed"], json!(2), "{body}");
    assert!(
        body["message"]
            .as_str()
            .is_some_and(|m| m.contains("stay valid")),
        "the caller is told its token is still good: {body}"
    );

    approve(&pool, &new_skill_hold, "tok-skill").await;
    let (status, body) = a2a_legacy(&pool, &key, 8_000, &["tok-card", "tok-skill"]).await;
    assert!(status.is_success(), "{status} {body}");
    assert_eq!(runner_calls(&runner).await, 1, "{body}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_token_that_answers_no_gate_refuses_the_request_and_no_token_is_burned(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_a2a_policy_hold(&pool).await;
    seed_skill_band(&pool).await;

    let (status, body) = a2a_legacy(&pool, &key, 8_000, &[]).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    let held = approvals(&pool).await;
    approve(&pool, of_type(&held, "a2a_call"), "tok-card").await;
    approve(&pool, of_type(&held, "skill_exec"), "tok-skill").await;

    // The skill's approval was for 8,000: it answers no gate of a call for
    // 9,000. The call is refused and both tokens stay good.
    let (status, body) = a2a_legacy(&pool, &key, 9_000, &["tok-card", "tok-skill"]).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert!(
        body.to_string().contains("Token argument mismatch"),
        "the refusal says why: {body}"
    );
    assert_eq!(runner_calls(&runner).await, 0);
    let after = approvals(&pool).await;
    assert_eq!(statuses(&after), ["approved", "approved"], "{after:?}");

    // A token nobody issued refuses too, and burns nothing.
    let (status, body) =
        a2a_legacy(&pool, &key, 8_000, &["tok-card", "tok-skill", "tok-nobody"]).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert!(body.to_string().contains("Token not found"), "{body}");
    let after = approvals(&pool).await;
    assert_eq!(statuses(&after), ["approved", "approved"], "{after:?}");

    // The right call with the right tokens still runs.
    let (status, body) = a2a_legacy(&pool, &key, 8_000, &["tok-card", "tok-skill"]).await;
    assert!(status.is_success(), "{status} {body}");
    assert_eq!(runner_calls(&runner).await, 1, "{body}");
}

// ---------------------------------------------------------------------------
// Legacy A2A: an agent's grant and the skill's band
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_agent_grant_hold_and_a_skill_band_are_raised_together(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    bind_agent(&pool, "a2a_call", CARD, "approval_required").await;
    seed_skill_band(&pool).await;

    let (status, body) = a2a_legacy(&pool, &key, 8_000, &[]).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    let held = approvals(&pool).await;
    assert_eq!(held.len(), 2, "{held:?} {body}");
    assert_eq!(listed(&body).len(), 2, "{body}");
    approve(&pool, of_type(&held, "a2a_call"), "tok-grant").await;
    approve(&pool, of_type(&held, "skill_exec"), "tok-skill").await;

    let (status, body) = a2a_legacy(&pool, &key, 8_000, &["tok-grant", "tok-skill"]).await;
    assert!(status.is_success(), "{status} {body}");
    assert_eq!(runner_calls(&runner).await, 1, "{body}");
}

/// The agent's grant and the group's A2A policy ask the same question: one
/// approval answers both, and the skill's is the second.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_grant_and_a_policy_that_ask_the_same_question_share_one_approval(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    bind_agent(&pool, "a2a_call", CARD, "approval_required").await;
    seed_a2a_policy_hold(&pool).await;
    seed_skill_band(&pool).await;

    let (status, body) = a2a_legacy(&pool, &key, 8_000, &[]).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    let held = approvals(&pool).await;
    assert_eq!(held.len(), 2, "two questions, two approvals: {held:?}");
    approve(&pool, of_type(&held, "a2a_call"), "tok-card").await;
    approve(&pool, of_type(&held, "skill_exec"), "tok-skill").await;

    let (status, body) = a2a_legacy(&pool, &key, 8_000, &["tok-card", "tok-skill"]).await;
    assert!(status.is_success(), "{status} {body}");
    assert_eq!(runner_calls(&runner).await, 1, "{body}");
    assert_eq!(approvals(&pool).await.len(), 2, "no further hold");
}

// ---------------------------------------------------------------------------
// A2A v1.0
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a2a_v1_names_both_holds_and_runs_with_both_tokens(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_a2a_policy_hold(&pool).await;
    seed_skill_band(&pool).await;

    let (status, body) = a2a_v1(&pool, &key, 8_000, None, &[]).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    let held = approvals(&pool).await;
    assert_eq!(held.len(), 2, "both holds are raised at once: {held:?}");
    let card_hold = of_type(&held, "a2a_call").to_string();
    let skill_hold = of_type(&held, "skill_exec").to_string();
    let named = find(&body, "approvalRequestIds")
        .and_then(Value::as_str)
        .unwrap_or_else(|| panic!("the answer names every approval: {body}"));
    assert!(
        named.contains(&card_hold) && named.contains(&skill_hold),
        "{body}"
    );
    let task_id = find(&body, "taskId")
        .and_then(Value::as_str)
        .map(str::to_string);
    assert_eq!(runner_calls(&runner).await, 0);

    approve(&pool, &card_hold, "tok-card").await;
    approve(&pool, &skill_hold, "tok-skill").await;
    let (status, body) = a2a_v1(
        &pool,
        &key,
        8_000,
        task_id.as_deref(),
        &["tok-card", "tok-skill"],
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(runner_calls(&runner).await, 1, "{body}");
    assert_eq!(
        find(&body, "state").and_then(Value::as_str),
        Some("TASK_STATE_COMPLETED"),
        "{body}"
    );
    let held = approvals(&pool).await;
    assert_eq!(statuses(&held), ["consumed", "consumed"], "{held:?}");
}

// ---------------------------------------------------------------------------
// skills__run_script: a band on the tool and the skill's band
// ---------------------------------------------------------------------------

/// The tool's own gate: `skills__run_script` is held for a person when the
/// script's `args.amount` is over 500.
async fn seed_tool_band(pool: &PgPool) {
    seed_policy(
        pool,
        "two-holds-tool-band",
        "mcp_input",
        agent_skill::TOOL_RUN_SCRIPT,
        json!([{"deny_if": "field_gt", "field": "args.amount", "value": 500,
                "severity": "approval_required",
                "message": "Script runs over 500 require approval"}]),
    )
    .await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn run_script_under_a_tool_band_and_a_skill_band_names_both_and_runs_with_both(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_tool_band(&pool).await;
    seed_skill_band(&pool).await;

    let (status, body) = run_script(&pool, &key, script_arguments(8_000), &[]).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    let held = approvals(&pool).await;
    assert_eq!(
        held.len(),
        2,
        "both holds are raised at once: {held:?} {body}"
    );
    let tool_hold = of_type(&held, "tool").to_string();
    let skill_hold = of_type(&held, "skill_exec").to_string();
    assert_eq!(listed(&body).len(), 2, "{body}");
    assert_eq!(runner_calls(&runner).await, 0);

    approve(&pool, &tool_hold, "tok-tool").await;
    approve(&pool, &skill_hold, "tok-skill").await;
    let (status, body) = run_script(
        &pool,
        &key,
        script_arguments(8_000),
        &["tok-tool", "tok-skill"],
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(
        runner_calls(&runner).await,
        1,
        "the approved script runs: {body}"
    );
    let held = approvals(&pool).await;
    assert_eq!(statuses(&held), ["consumed", "consumed"], "{held:?}");
}

/// A client that cannot set headers carries its tokens in the tool's
/// `approval_token` argument. The argument is a credential, not content: it
/// is not part of what was approved.
#[sqlx::test(migrations = "./tests/migrations")]
async fn run_script_takes_its_tokens_from_the_approval_token_argument(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_tool_band(&pool).await;
    seed_skill_band(&pool).await;

    let (status, body) = run_script(&pool, &key, script_arguments(8_000), &[]).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    let held = approvals(&pool).await;
    approve(&pool, of_type(&held, "tool"), "tok-tool").await;
    approve(&pool, of_type(&held, "skill_exec"), "tok-skill").await;

    let mut arguments = script_arguments(8_000);
    arguments["approval_token"] = json!("tok-tool,tok-skill");
    let (status, body) = run_script(&pool, &key, arguments, &[]).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(runner_calls(&runner).await, 1, "{body}");
}

// ---------------------------------------------------------------------------
// MCP: an agent's grant and the tool's capability filter ask one question
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_grant_and_a_capability_filter_on_one_tool_call_share_one_approval(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let upstream = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "jsonrpc": "2.0", "id": 7,
            "result": {"content": [{"type": "text", "text": "sent"}]}
        })))
        .mount(&upstream)
        .await;
    let key = topology(&pool).await;
    common::seed_mcp_server_with_base_url(&pool, "ms-two-holds", T, "mail", &upstream.uri()).await;
    common::seed_server_config(&pool, "sc-two-holds", T, "ms-two-holds").await;
    common::bind_server_config_to_rg(&pool, "sc-two-holds", RG).await;
    bind_agent(&pool, "mcp_tool", "send_email", "approval_required").await;
    sqlx::query(
        "INSERT INTO resource_group_mcp_capabilities \
         (id, tenant_id, resource_group_id, server_config_id, capability_type, \
          capability_name, access, approval_timeout_seconds, created_at, updated_at) \
         VALUES ('cap-two-holds', $1, $2, 'sc-two-holds', 'tool', 'send_email', \
                 'approval_required', 600, now(), now())",
    )
    .bind(T)
    .bind(RG)
    .execute(&pool)
    .await
    .expect("seed capability filter");

    let call = |tokens: Vec<&'static str>| {
        let pool = pool.clone();
        let key = key.clone();
        async move {
            let app = common::build_mcp_proxy_app(common::build_test_state(pool));
            let req = common::build_post_request(
                "/v1/proxy/mcp/ms-two-holds",
                json!({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                       "params": {"name": "send_email", "arguments": {"to": "a@example.com"}}}),
                Some(&key),
                Some(T),
            );
            send(app, with_tokens(req, &tokens)).await
        }
    };

    let (status, body) = call(vec![]).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    let held = approvals(&pool).await;
    assert_eq!(held.len(), 1, "one question, one approval: {held:?}");
    approve(&pool, &held[0].0, "tok-mail").await;

    let (status, body) = call(vec!["tok-mail"]).await;
    assert_eq!(status, StatusCode::OK, "the approved call is sent: {body}");
    assert_eq!(
        upstream.received_requests().await.unwrap_or_default().len(),
        1,
        "{body}"
    );
    let held = approvals(&pool).await;
    assert_eq!(statuses(&held), ["consumed"], "{held:?}");

    // One approval, one call.
    let (status, body) = call(vec!["tok-mail"]).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
}

// ---------------------------------------------------------------------------
// What a person is asked to approve is what the handler would be given
// ---------------------------------------------------------------------------

const EMAIL: &str = "jane.doe@example.com";

/// The input guardrail redacts what enters the tenant. A hold raised for
/// the skill behind an A2A task is asked about the redacted input, the one
/// the skill would run on: the approval queue never holds what the
/// guardrail removes, and the retry (redacted the same way) matches it.
async fn seed_redacting_guardrail(pool: &PgPool) {
    common::seed_llm_pii_guardrail(pool, "gr-two-holds", T, "redact", false, false).await;
    sqlx::query(
        "UPDATE guardrail_configs SET a2a_inbound_enabled = true WHERE id = 'gr-two-holds'",
    )
    .execute(pool)
    .await
    .expect("guardrail on the inbound surface");
    common::bind_guardrail_to_rg(pool, "gr-two-holds", RG).await;
}

async fn stored_arguments(pool: &PgPool) -> String {
    let rows: Vec<(Option<Value>,)> =
        sqlx::query_as("SELECT call_arguments FROM tool_approval_requests WHERE tenant_id = $1")
            .bind(T)
            .fetch_all(pool)
            .await
            .expect("approval arguments");
    rows.into_iter()
        .map(|(v,)| v.map(|v| v.to_string()).unwrap_or_default())
        .collect::<Vec<_>>()
        .join("\n")
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_hold_for_the_skill_behind_a_legacy_task_is_asked_about_the_redacted_input(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_a2a_policy_hold(&pool).await;
    seed_skill_band(&pool).await;
    seed_redacting_guardrail(&pool).await;

    let send = |tokens: Vec<&'static str>| {
        let pool = pool.clone();
        let key = key.clone();
        async move {
            let state = common::build_test_state(pool);
            let app = axum::Router::new()
                .nest("/v1/proxy/a2a", routes::a2a::router())
                .layer(middleware::tenant::TenantLayer::new(state.clone()))
                .layer(middleware::run_end::RunEndLayer::new(
                    state.mcp_sessions.clone(),
                ))
                .with_state(state);
            let req = common::build_post_request(
                "/v1/proxy/a2a/inbound",
                json!({"target_card_id": CARD, "capability": "transfer",
                       "message": {"amount": 8_000, "note": format!("receipt to {EMAIL}")}}),
                Some(&key),
                Some(T),
            );
            send(app, with_tokens(req, &tokens)).await
        }
    };

    let (status, body) = send(vec![]).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    let held = approvals(&pool).await;
    assert_eq!(held.len(), 2, "{held:?} {body}");
    let stored = stored_arguments(&pool).await;
    assert!(
        !stored.contains(EMAIL),
        "the approval queue holds what the guardrail redacts: {stored}"
    );
    approve(&pool, of_type(&held, "a2a_call"), "tok-card").await;
    approve(&pool, of_type(&held, "skill_exec"), "tok-skill").await;
    let (status, body) = send(vec!["tok-card", "tok-skill"]).await;
    assert!(status.is_success(), "{status} {body}");
    assert_eq!(runner_calls(&runner).await, 1, "{body}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_hold_for_the_skill_behind_a_v1_message_is_asked_about_the_redacted_input(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_a2a_policy_hold(&pool).await;
    seed_skill_band(&pool).await;
    seed_redacting_guardrail(&pool).await;

    let send_v1 = |task_id: Option<String>, tokens: Vec<&'static str>| {
        let pool = pool.clone();
        let key = key.clone();
        async move {
            let app = common::build_a2a_v1_app(common::build_test_state(pool));
            let mut message = json!({"messageId": ulid::Ulid::new().to_string(),
                "role": "ROLE_USER",
                "parts": [{"text": format!("receipt to {EMAIL}")}, {"data": {"amount": 8_000}}]});
            if let Some(task_id) = task_id {
                message["taskId"] = json!(task_id);
            }
            let req = common::build_post_request(
                &format!("/a2a/{T}/{CARD_NAME}/message:send"),
                json!({ "message": message }),
                Some(&key),
                Some(T),
            );
            send(app, with_tokens(req, &tokens)).await
        }
    };

    let (status, body) = send_v1(None, vec![]).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    let held = approvals(&pool).await;
    assert_eq!(held.len(), 2, "{held:?} {body}");
    let stored = stored_arguments(&pool).await;
    assert!(
        !stored.contains(EMAIL),
        "the approval queue holds what the guardrail redacts: {stored}"
    );
    let task_id = find(&body, "taskId")
        .and_then(Value::as_str)
        .map(str::to_string);
    approve(&pool, of_type(&held, "a2a_call"), "tok-card").await;
    approve(&pool, of_type(&held, "skill_exec"), "tok-skill").await;
    let (status, body) = send_v1(task_id, vec!["tok-card", "tok-skill"]).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(runner_calls(&runner).await, 1, "{body}");
}

/// The surfaces whose input guardrail runs between the route's gates and
/// the skill.
#[derive(Clone, Copy, Debug)]
enum Guarded {
    Legacy,
    V1,
}

/// One message with `note` beside the amount, on `surface`.
async fn guarded_send(
    pool: &PgPool,
    key: &str,
    surface: Guarded,
    note: &str,
    task_id: Option<&str>,
    tokens: &[&str],
) -> (StatusCode, Value) {
    let state = common::build_test_state(pool.clone());
    match surface {
        Guarded::Legacy => {
            let app = axum::Router::new()
                .nest("/v1/proxy/a2a", routes::a2a::router())
                .layer(middleware::tenant::TenantLayer::new(state.clone()))
                .layer(middleware::run_end::RunEndLayer::new(
                    state.mcp_sessions.clone(),
                ))
                .with_state(state);
            let req = common::build_post_request(
                "/v1/proxy/a2a/inbound",
                json!({"target_card_id": CARD, "capability": "transfer",
                       "message": {"amount": 8_000, "note": note}}),
                Some(key),
                Some(T),
            );
            send(app, with_tokens(req, tokens)).await
        }
        Guarded::V1 => {
            let mut message = json!({"messageId": ulid::Ulid::new().to_string(),
                "role": "ROLE_USER",
                "parts": [{"text": note}, {"data": {"amount": 8_000}}]});
            if let Some(task_id) = task_id {
                message["taskId"] = json!(task_id);
            }
            let req = common::build_post_request(
                &format!("/a2a/{T}/{CARD_NAME}/message:send"),
                json!({ "message": message }),
                Some(key),
                Some(T),
            );
            send(common::build_a2a_v1_app(state), with_tokens(req, tokens)).await
        }
    }
}

/// An approval is for what the caller sent. The guardrail's redaction of it
/// is what a person is shown and what the skill is given, and it may differ
/// from one evaluation to the next (a guardrail that asks a model, a
/// detector service): the approved retry of the same message still runs,
/// and the token does not run another message that happens to redact to
/// the same text.
async fn an_approval_is_bound_to_what_the_caller_sent(pool: PgPool, surface: Guarded) {
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_a2a_policy_hold(&pool).await;
    seed_skill_band(&pool).await;
    seed_redacting_guardrail(&pool).await;
    let note = format!("receipt to {EMAIL}");

    let (_, body) = guarded_send(&pool, &key, surface, &note, None, &[]).await;
    let held = approvals(&pool).await;
    assert_eq!(held.len(), 2, "{held:?} {body}");
    let stored = stored_arguments(&pool).await;
    assert!(!stored.contains(EMAIL), "{stored}");
    let everything: Vec<(Option<Value>,)> =
        sqlx::query_as("SELECT request_metadata FROM tool_approval_requests WHERE tenant_id = $1")
            .bind(T)
            .fetch_all(&pool)
            .await
            .expect("approval metadata");
    assert!(
        !format!("{everything:?}").contains(EMAIL),
        "the approval's metadata holds what the guardrail redacts"
    );
    let task_id = find(&body, "taskId")
        .and_then(Value::as_str)
        .map(str::to_string);
    approve(&pool, of_type(&held, "a2a_call"), "tok-card").await;
    approve(&pool, of_type(&held, "skill_exec"), "tok-skill").await;

    // Another message that redacts to the same text is another call.
    let (status, body) = guarded_send(
        &pool,
        &key,
        surface,
        "receipt to john.roe@example.org",
        task_id.as_deref(),
        &["tok-card", "tok-skill"],
    )
    .await;
    assert!(
        !status.is_success(),
        "{surface:?}: an approval ran a message it was not given for: {status} {body}"
    );
    assert_eq!(runner_calls(&runner).await, 0, "{body}");
    let unspent: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM tool_approval_requests WHERE tenant_id = $1 AND status = 'approved'",
    )
    .bind(T)
    .fetch_one(&pool)
    .await
    .expect("approved rows");
    assert_eq!(unspent, 2, "a refused retry redeemed a token");

    // The guardrail redacts the retry differently from the request that was
    // held (here: not at all). The message is the one that was approved.
    sqlx::query(
        "UPDATE guardrail_configs SET pii_detection_enabled = false WHERE id = 'gr-two-holds'",
    )
    .execute(&pool)
    .await
    .expect("the guardrail changes its mind");
    let (status, body) = guarded_send(
        &pool,
        &key,
        surface,
        &note,
        task_id.as_deref(),
        &["tok-card", "tok-skill"],
    )
    .await;
    assert!(
        status.is_success(),
        "{surface:?}: the approved message was refused because the guardrail redacted it \
         differently: {status} {body}"
    );
    assert_eq!(runner_calls(&runner).await, 1, "{body}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_approval_is_bound_to_what_the_caller_sent_on_a_legacy_task(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    an_approval_is_bound_to_what_the_caller_sent(pool, Guarded::Legacy).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_approval_is_bound_to_what_the_caller_sent_on_a_v1_message(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    an_approval_is_bound_to_what_the_caller_sent(pool, Guarded::V1).await;
}

// ---------------------------------------------------------------------------
// A call the skill would refuse is refused before anything is held
// ---------------------------------------------------------------------------

/// The group's data scope gets a rule for the skill that this run cannot
/// satisfy (the run declared no scope, and an undeclared key is refused).
/// Nothing in it constrains the A2A route itself.
async fn seed_skill_data_scope(pool: &PgPool) {
    sqlx::query(
        "INSERT INTO ai_system_data_scopes \
            (id, tenant_id, ai_system_id, mode, on_unbound, keys, rules, unscoped_tools) \
         VALUES ('ds-two-holds', $1, $2, 'enforce', 'deny', $3, $4, '[]'::jsonb)",
    )
    .bind(T)
    .bind(RG)
    .bind(json!([{"key": "customer", "source": "declared"}]))
    .bind(json!([
        {"tool": format!("skill:{SKILL_NAME}"), "argument": "customer_id", "key": "customer"},
    ]))
    .execute(pool)
    .await
    .expect("seed data scope");
    brutor_gateway_core::services::data_scope::invalidate_all();
}

async fn drop_skill_data_scope(pool: &PgPool) {
    sqlx::query("DELETE FROM ai_system_data_scopes WHERE id = 'ds-two-holds'")
        .execute(pool)
        .await
        .expect("drop data scope");
    brutor_gateway_core::services::data_scope::invalidate_all();
}

/// One message naming a customer record.
async fn scoped_send(
    pool: &PgPool,
    key: &str,
    surface: Guarded,
    tokens: &[&str],
) -> (StatusCode, Value) {
    let state = common::build_test_state(pool.clone());
    let (app, req) = match surface {
        Guarded::Legacy => (
            axum::Router::new()
                .nest("/v1/proxy/a2a", routes::a2a::router())
                .layer(middleware::tenant::TenantLayer::new(state.clone()))
                .layer(middleware::run_end::RunEndLayer::new(
                    state.mcp_sessions.clone(),
                ))
                .with_state(state),
            common::build_post_request(
                "/v1/proxy/a2a/inbound",
                json!({"target_card_id": CARD, "capability": "transfer",
                       "message": {"amount": 10, "customer_id": "C9999"}}),
                Some(key),
                Some(T),
            ),
        ),
        Guarded::V1 => (
            common::build_a2a_v1_app(state),
            common::build_post_request(
                &format!("/a2a/{T}/{CARD_NAME}/message:send"),
                json!({"message": {"messageId": "01twoholdsscopedmessage000",
                    "role": "ROLE_USER",
                    "parts": [{"data": {"amount": 10, "customer_id": "C9999"}}]}}),
                Some(key),
                Some(T),
            ),
        ),
    };
    send(app, with_tokens(req, tokens)).await
}

async fn scope_accesses(pool: &PgPool) -> Vec<(String, String)> {
    sqlx::query_as(
        "SELECT tool, verdict FROM ai_system_record_access WHERE tenant_id = $1 \
          ORDER BY accessed_at, id",
    )
    .bind(T)
    .fetch_all(pool)
    .await
    .expect("record accesses")
}

/// The route's gate has to hold the call, and the skill behind it would
/// refuse it (here: the run's data scope). The call is refused, with the
/// skill's refusal, and nothing is put in the approval queue: a hold for a
/// call that cannot run is a question nobody can usefully answer. A token
/// the request carries for the route's gate is not spent on it either.
async fn a_call_the_skill_would_refuse_is_refused_before_any_hold(pool: PgPool, surface: Guarded) {
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_a2a_policy_hold(&pool).await;

    // Nothing refuses the skill: the route's gate holds the call.
    let (_, body) = scoped_send(&pool, &key, surface, &[]).await;
    let held = approvals(&pool).await;
    assert_eq!(held.len(), 1, "{held:?} {body}");
    approve(&pool, of_type(&held, "a2a_call"), "tok-card").await;
    assert!(scope_accesses(&pool).await.is_empty());

    // The skill would refuse it. With no token: refused, and no hold.
    seed_skill_data_scope(&pool).await;
    let (status, body) = scoped_send(&pool, &key, surface, &[]).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{surface:?}: {body}");
    assert!(
        body.to_string().contains("Data scope"),
        "{surface:?}: the skill's own refusal is the answer: {body}"
    );
    assert_eq!(
        approvals(&pool).await.len(),
        1,
        "{surface:?}: a hold was raised for a call the skill refuses: {body}"
    );

    // Carrying the route's approved token: refused, and the token is still
    // there for the call it was approved for.
    let (status, body) = scoped_send(&pool, &key, surface, &["tok-card"]).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{surface:?}: {body}");
    assert_eq!(
        statuses(&approvals(&pool).await),
        ["approved"],
        "{surface:?}: a refused call spent the approval"
    );
    assert_eq!(runner_calls(&runner).await, 0);

    // Each refused call recorded its access once, as a refused one.
    let accesses = scope_accesses(&pool).await;
    assert_eq!(accesses.len(), 2, "{accesses:?}");
    assert!(
        accesses
            .iter()
            .all(|(tool, verdict)| tool == &format!("skill:{SKILL_NAME}") && verdict != "in_scope"),
        "{accesses:?}"
    );

    // Nothing refuses the skill any more: the approved call runs.
    drop_skill_data_scope(&pool).await;
    let (status, body) = scoped_send(&pool, &key, surface, &["tok-card"]).await;
    assert!(status.is_success(), "{surface:?}: {status} {body}");
    assert_eq!(runner_calls(&runner).await, 1, "{body}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_legacy_task_the_skill_would_refuse_is_refused_before_any_hold(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    a_call_the_skill_would_refuse_is_refused_before_any_hold(pool, Guarded::Legacy).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_v1_message_the_skill_would_refuse_is_refused_before_any_hold(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    a_call_the_skill_would_refuse_is_refused_before_any_hold(pool, Guarded::V1).await;
}

// ---------------------------------------------------------------------------
// An approval token is a credential: no audit row holds one
// ---------------------------------------------------------------------------

/// What one audit row says: headers, request, response, error.
type LoggedRow = (
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
);

/// Everything the audit rows of this tenant say, once they are written.
async fn everything_logged(pool: &PgPool, at_least: i64) -> String {
    let mut rows: Vec<LoggedRow> = Vec::new();
    for _ in 0..100 {
        rows = sqlx::query_as(
            "SELECT request_headers, request_body_preview, response_body_preview, error_message \
             FROM proxy_logs WHERE tenant_id = $1",
        )
        .bind(T)
        .fetch_all(pool)
        .await
        .expect("proxy rows");
        if rows.len() as i64 >= at_least {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    assert!(
        rows.len() as i64 >= at_least,
        "{} row(s) written",
        rows.len()
    );
    rows.into_iter()
        .map(|r| {
            [r.0, r.1, r.2, r.3]
                .map(Option::unwrap_or_default)
                .join("\n")
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// A token carried in the tool's `approval_token` argument, in the skill
/// route's body and in the `X-Approval-Token` header is in no audit row,
/// whether it was redeemed or kept.
#[sqlx::test(migrations = "./tests/migrations")]
async fn no_audit_row_holds_an_approval_token(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let _runner = runner().await;
    let key = topology(&pool).await;

    // `skills__run_script`, the token in the argument and in a header.
    let mut arguments = script_arguments(100);
    arguments["approval_token"] = json!("tok-secret-argument");
    let (status, body) = run_script(&pool, &key, arguments, &["tok-secret-header"]).await;
    assert_eq!(status, StatusCode::OK, "{body}");

    // The skill route, the token in the body.
    let app = common::build_multimodal_app(common::build_test_state(pool.clone()));
    let req = common::build_post_request(
        "/v1/proxy/skills/execute",
        json!({"skill_name": SKILL_NAME, "input_params": {"amount": 100},
               "approval_token": "tok-secret-body"}),
        Some(&key),
        Some(T),
    );
    let (status, body) = send(app, req).await;
    assert!(status.is_success(), "{status} {body}");

    let logged = everything_logged(&pool, 2).await;
    for token in [
        "tok-secret-argument",
        "tok-secret-header",
        "tok-secret-body",
    ] {
        assert!(
            !logged.contains(token),
            "an audit row holds the approval token `{token}`"
        );
    }
    // What was asked is still on the record.
    assert!(logged.contains(SKILL_NAME), "{logged}");
}

// ---------------------------------------------------------------------------
// The skill's execution mode `approval_required`
//
// A skill whose execution mode asks for a person is held like any other
// call that does: one approval request in the queue, nothing recorded as an
// execution and nothing charged while it waits, and the same call retried
// with the token runs. The mode has three sources (the group's skill
// governance, the skill's own metadata, the skill's assignment to a group);
// any one of them holds the call.
// ---------------------------------------------------------------------------

async fn group_mode(pool: &PgPool, governance: Value) {
    common::set_rg_skill_governance(pool, RG, governance).await;
}

/// The governance on the skill's assignment to `group`.
async fn assignment_governance(pool: &PgPool, group: &str, governance: Value) {
    let changed = sqlx::query(
        "UPDATE resource_group_agent_skills SET governance = $3 \
         WHERE resource_group_id = $1 AND agent_skill_id = $2",
    )
    .bind(group)
    .bind(SKILL_ID)
    .bind(governance)
    .execute(pool)
    .await
    .expect("assignment governance")
    .rows_affected();
    assert_eq!(changed, 1, "the skill is assigned to {group}");
}

async fn skill_asks_for_approval(pool: &PgPool) {
    sqlx::query("UPDATE agent_skills SET metadata = $2 WHERE id = $1")
        .bind(SKILL_ID)
        .bind(json!({"approval_required": true}))
        .execute(pool)
        .await
        .expect("skill metadata");
}

/// `POST /v1/proxy/skills/execute` on `state`, as part of `run` when given.
async fn rest_on(
    state: brutor_gateway_core::services::SharedState,
    key: &str,
    amount: i64,
    tokens: &[&str],
    run: Option<&str>,
) -> (StatusCode, Value) {
    let app = common::build_multimodal_app(state);
    let mut req = common::build_post_request(
        "/v1/proxy/skills/execute",
        json!({"skill_id": SKILL_ID, "input_params": {"amount": amount}}),
        Some(key),
        Some(T),
    );
    if let Some(run) = run {
        req.headers_mut()
            .insert("x-brutor-run-id", run.parse().expect("run header"));
    }
    send(app, with_tokens(req, tokens)).await
}

async fn rest(pool: &PgPool, key: &str, amount: i64, tokens: &[&str]) -> (StatusCode, Value) {
    rest_on(
        common::build_test_state(pool.clone()),
        key,
        amount,
        tokens,
        None,
    )
    .await
}

/// Every execution row of the tenant: (execution_status, approval_status).
async fn executions(pool: &PgPool) -> Vec<(String, String)> {
    sqlx::query_as(
        "SELECT execution_status::text, approval_status::text \
         FROM agent_skill_executions WHERE tenant_id = $1 ORDER BY created_at, id",
    )
    .bind(T)
    .fetch_all(pool)
    .await
    .expect("executions")
}

/// The one approval the held answer names, which is the only one raised.
async fn the_one_hold(pool: &PgPool, body: &Value) -> String {
    let held = approvals(pool).await;
    assert_eq!(held.len(), 1, "one approval request: {held:?} {body}");
    assert_eq!(
        (held[0].1.as_str(), held[0].2.as_str(), held[0].3.as_str()),
        ("skill", "skill_exec", "pending"),
        "{held:?}"
    );
    assert_eq!(
        body["approval_request_id"],
        json!(held[0].0),
        "the answer is the approval envelope: {body}"
    );
    assert!(
        body["poll_url"]
            .as_str()
            .is_some_and(|u| u.ends_with(&format!("/v1/approvals/{}/poll", held[0].0))),
        "{body}"
    );
    held[0].0.clone()
}

async fn hold_metadata(pool: &PgPool, id: &str) -> Value {
    sqlx::query_scalar::<_, Value>(
        "SELECT request_metadata::jsonb FROM tool_approval_requests WHERE id = $1",
    )
    .bind(id)
    .fetch_one(pool)
    .await
    .expect("hold metadata")
}

/// Held, then run by the retry that carries the token: no execution exists
/// while the call waits, exactly one after it ran.
async fn is_held_then_runs_with_the_token(pool: &PgPool, key: &str, runner: &MockServer) {
    let (status, body) = rest(pool, key, 100, &[]).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    let hold = the_one_hold(pool, &body).await;
    assert!(body.get("execution_id").is_none(), "{body}");
    assert_eq!(runner_calls(runner).await, 0);
    assert!(
        executions(pool).await.is_empty(),
        "a held call is not an execution"
    );

    approve(pool, &hold, "tok-mode").await;
    let (status, body) = rest(pool, key, 100, &["tok-mode"]).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["execution_status"], json!("COMPLETED"), "{body}");
    assert_eq!(runner_calls(runner).await, 1, "{body}");
    assert_eq!(
        executions(pool).await,
        [("COMPLETED".to_string(), "NOT_REQUIRED".to_string())]
    );
    let held = approvals(pool).await;
    assert_eq!(statuses(&held), ["consumed"], "no second hold: {held:?}");
}

/// The status of `POST /v1/proxy/skills/execute` says what happened: 200
/// for a run that has ended (its `execution_status` says how), 202 for a
/// call that is held for a person. A client does not have to read the body
/// to tell a result from a request to wait.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_finished_run_answers_200_and_a_held_call_202(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;

    let (status, body) = rest(&pool, &key, 100, &[]).await;
    assert_eq!(status, StatusCode::OK, "a run that completed: {body}");
    assert_eq!(body["execution_status"], json!("COMPLETED"), "{body}");
    assert!(body["execution_id"].is_string(), "{body}");
    assert_eq!(runner_calls(&runner).await, 1);
    let (logged,): (i32,) = crate::evidence_runtime::wait_for_row(
        &pool,
        "SELECT http_status_code FROM proxy_logs WHERE skill_execution_id = $1 \
           AND http_status_code IS NOT NULL",
        body["execution_id"].as_str().unwrap_or_default(),
    )
    .await;
    assert_eq!(
        logged, 200,
        "the audit row records the status that was answered"
    );

    group_mode(&pool, json!({"execution_mode": "approval_required"})).await;
    let (status, body) = rest(&pool, &key, 100, &[]).await;
    assert_eq!(status, StatusCode::ACCEPTED, "a held call: {body}");
    assert_eq!(body["approval_required"], json!(true), "{body}");
    assert!(body.get("execution_id").is_none(), "{body}");
    assert_eq!(runner_calls(&runner).await, 1, "a held call ran nothing");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_groups_execution_mode_alone_holds_the_call(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    group_mode(&pool, json!({"execution_mode": "approval_required"})).await;
    is_held_then_runs_with_the_token(&pool, &key, &runner).await;
    let held = approvals(&pool).await;
    assert_eq!(
        hold_metadata(&pool, &held[0].0).await["execution_mode"]["required_by"],
        json!(["group"])
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_skills_own_default_alone_holds_the_call(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    skill_asks_for_approval(&pool).await;
    is_held_then_runs_with_the_token(&pool, &key, &runner).await;
    let held = approvals(&pool).await;
    assert_eq!(
        hold_metadata(&pool, &held[0].0).await["execution_mode"]["required_by"],
        json!(["skill"])
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_assignments_execution_mode_alone_holds_the_call(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    assignment_governance(&pool, RG, json!({"execution_mode": "approval_required"})).await;
    is_held_then_runs_with_the_token(&pool, &key, &runner).await;
    let held = approvals(&pool).await;
    assert_eq!(
        hold_metadata(&pool, &held[0].0).await["execution_mode"]["required_by"],
        json!(["assignment"])
    );
}

/// The mode and a band on the input ask about the same call: one approval
/// request, carrying what each of them said, and one token.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_mode_and_a_band_share_one_approval(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    group_mode(&pool, json!({"execution_mode": "approval_required"})).await;
    seed_skill_band(&pool).await;

    let (status, body) = rest(&pool, &key, 8_000, &[]).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    let hold = the_one_hold(&pool, &body).await;
    let metadata = hold_metadata(&pool, &hold).await;
    assert_eq!(
        metadata["execution_mode"]["required_by"],
        json!(["group"]),
        "{metadata}"
    );
    assert!(
        metadata.to_string().contains("two-holds-skill-band"),
        "the band's detail rides along: {metadata}"
    );

    approve(&pool, &hold, "tok-one").await;
    let (status, body) = rest(&pool, &key, 8_000, &["tok-one"]).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["execution_status"], json!("COMPLETED"), "{body}");
    assert_eq!(runner_calls(&runner).await, 1);
    assert_eq!(statuses(&approvals(&pool).await), ["consumed"]);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_mode_and_an_agent_grant_share_one_approval(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    bind_agent(&pool, "skill_exec", SKILL_ID, "approval_required").await;
    assignment_governance(&pool, RG, json!({"execution_mode": "approval_required"})).await;

    let (status, body) = rest(&pool, &key, 100, &[]).await;
    assert!(
        status == StatusCode::ACCEPTED || status == StatusCode::PRECONDITION_REQUIRED,
        "{status} {body}"
    );
    let hold = the_one_hold(&pool, &body).await;
    approve(&pool, &hold, "tok-one").await;
    let (status, body) = rest(&pool, &key, 100, &["tok-one"]).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["execution_status"], json!("COMPLETED"), "{body}");
    assert_eq!(runner_calls(&runner).await, 1);
    assert_eq!(statuses(&approvals(&pool).await), ["consumed"]);
}

/// An agent whose grant is observed only (dry run) is still held by the mode.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_dry_run_grant_does_not_take_the_call_past_the_mode(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    bind_agent(&pool, "skill_exec", SKILL_ID, "approval_required").await;
    sqlx::query("UPDATE agent_identities SET enforcement_mode = 'dry_run' WHERE id = $1")
        .bind(AGENT)
        .execute(&pool)
        .await
        .expect("dry run");
    group_mode(&pool, json!({"execution_mode": "approval_required"})).await;

    let (status, body) = rest(&pool, &key, 100, &[]).await;
    assert!(
        status == StatusCode::ACCEPTED || status == StatusCode::PRECONDITION_REQUIRED,
        "{status} {body}"
    );
    the_one_hold(&pool, &body).await;
    assert_eq!(runner_calls(&runner).await, 0);
    assert!(executions(&pool).await.is_empty());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_token_for_another_input_does_not_run_a_skill_held_by_its_mode(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    group_mode(&pool, json!({"execution_mode": "approval_required"})).await;

    let (_, body) = rest(&pool, &key, 100, &[]).await;
    let hold = the_one_hold(&pool, &body).await;
    approve(&pool, &hold, "tok-100").await;

    let (status, body) = rest(&pool, &key, 9_999, &["tok-100"]).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert_eq!(runner_calls(&runner).await, 0);
    assert!(executions(&pool).await.is_empty());
    assert_eq!(
        statuses(&approvals(&pool).await),
        ["approved"],
        "not burned"
    );
}

/// While the call waits it has used nothing: no skill execution is counted
/// toward the group's quota, and the run has taken no action. The approved
/// retry is counted once.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_call_held_by_its_mode_is_charged_nothing_until_it_runs(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    use brutor_gateway_core::services::cache::usage_state::{QuotaWindow, UsageState};
    let runner = runner().await;
    let key = topology(&pool).await;
    group_mode(&pool, json!({"execution_mode": "approval_required"})).await;
    common::set_skill_global_limits(&pool, RG, json!({"execution": {"daily_limit": 1_000_000}}))
        .await;
    // One action for the whole run: a held attempt that kept its unit would
    // leave none for the approved retry.
    common::set_rg_operating_envelope(&pool, RG, json!({"max_actions_per_run": 1})).await;
    let state = super::run_actions::replica(&pool).await;
    let run = format!("run-{}", ulid::Ulid::new());
    let counted = || async {
        state
            .usage_state
            .read_usage(&format!("skx:grp:{RG}"), QuotaWindow::Day)
            .await
    };
    let before = counted().await;

    let mut hold = String::new();
    for _ in 0..3 {
        let (status, body) = rest_on(state.clone(), &key, 100, &[], Some(&run)).await;
        assert_eq!(status, StatusCode::ACCEPTED, "{body}");
        hold = body["approval_request_id"]
            .as_str()
            .unwrap_or_else(|| panic!("held: {body}"))
            .to_string();
    }
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    assert_eq!(
        counted().await,
        before,
        "a held call counted as an execution"
    );
    let (root,): (Option<String>,) = sqlx::query_as(
        "SELECT originating_trace_root_id FROM tool_approval_requests WHERE id = $1",
    )
    .bind(&hold)
    .fetch_one(&pool)
    .await
    .expect("the hold's run");
    let root = root.expect("the hold names its run");
    let actions = UsageState::run_counter_key(T, &root, "actions");
    assert_eq!(
        state
            .usage_state
            .counter_value(&actions)
            .await
            .expect("action counter"),
        0,
        "a held call took an action of its run"
    );
    assert!(executions(&pool).await.is_empty());

    approve(&pool, &hold, "tok-mode").await;
    let (status, body) = rest_on(state.clone(), &key, 100, &["tok-mode"], Some(&run)).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["execution_status"], json!("COMPLETED"), "{body}");
    assert_eq!(runner_calls(&runner).await, 1);
    assert_eq!(counted().await, before + 1, "the run is counted once");
    assert_eq!(
        state
            .usage_state
            .counter_value(&actions)
            .await
            .expect("action counter"),
        1
    );
}

/// On the evidence log the held call is `awaiting_human` under its approval
/// request, and the run the approval allowed is `executed` by a person's
/// authority, superseding that record.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_approved_run_of_a_held_skill_supersedes_the_record_of_its_hold(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    use super::evidence_runtime::{assert_verifies, enable_evidence, records, wait_for_row};
    enable_evidence();
    let runner = runner().await;
    let key = topology(&pool).await;
    group_mode(&pool, json!({"execution_mode": "approval_required"})).await;

    let (_, body) = rest(&pool, &key, 100, &[]).await;
    let hold = the_one_hold(&pool, &body).await;
    let (awaiting_id,): (String,) = wait_for_row(
        &pool,
        "SELECT record_id FROM action_records WHERE verdict = 'awaiting_human' \
           AND record->'context'->>'approval_request_id' = $1",
        &hold,
    )
    .await;

    approve(&pool, &hold, "tok-evidence").await;
    let (status, body) = rest(&pool, &key, 100, &["tok-evidence"]).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["execution_status"], json!("COMPLETED"), "{body}");
    assert_eq!(runner_calls(&runner).await, 1);
    let execution = body["execution_id"].as_str().expect("execution id");

    let (run_record,): (String,) = wait_for_row(
        &pool,
        "SELECT record_id FROM proxy_logs WHERE skill_execution_id = $1 AND record_id IS NOT NULL",
        execution,
    )
    .await;
    let sealed = records(&pool, T).await;
    let record = &sealed
        .iter()
        .find(|r| r.0 == run_record)
        .expect("the run's record")
        .3;
    assert_eq!(record["verdict"], "executed");
    assert_eq!(record["disposition"]["approver"], "human");
    assert_eq!(record["disposition"]["human_disposed"], true);
    assert_eq!(record["disposition"]["authority"], hold.as_str());
    assert_eq!(record["chain"]["relation"], "supersedes");
    assert_eq!(record["chain"]["parent_record_id"], awaiting_id.as_str());
    assert_verifies(record);
}

/// `disabled` is a refusal, from any source, and raises nothing.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_disabled_mode_refuses_and_raises_no_hold(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    // The other sources ask for a person; the strictest wins.
    skill_asks_for_approval(&pool).await;
    group_mode(&pool, json!({"execution_mode": "approval_required"})).await;
    assignment_governance(&pool, RG, json!({"execution_mode": "disabled"})).await;

    let (status, body) = rest(&pool, &key, 100, &[]).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert!(approvals(&pool).await.is_empty(), "{body}");

    assignment_governance(&pool, RG, json!({})).await;
    group_mode(&pool, json!({"execution_mode": "disabled"})).await;
    let (status, body) = rest(&pool, &key, 100, &[]).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert!(approvals(&pool).await.is_empty(), "{body}");
    assert_eq!(runner_calls(&runner).await, 0);
    assert!(executions(&pool).await.is_empty());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_rejected_approval_does_not_run_the_skill(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    group_mode(&pool, json!({"execution_mode": "approval_required"})).await;

    let (_, body) = rest(&pool, &key, 100, &[]).await;
    let hold = the_one_hold(&pool, &body).await;
    sqlx::query(
        "UPDATE tool_approval_requests \
         SET status = 'rejected', approval_token = 'tok-rejected', resolved_at = now() \
         WHERE id = $1",
    )
    .bind(&hold)
    .execute(&pool)
    .await
    .expect("reject");

    let (status, body) = rest(&pool, &key, 100, &["tok-rejected"]).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert_eq!(runner_calls(&runner).await, 0);
    assert!(executions(&pool).await.is_empty());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_expired_hold_leaves_no_execution(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    group_mode(&pool, json!({"execution_mode": "approval_required"})).await;

    let (_, body) = rest(&pool, &key, 100, &[]).await;
    let hold = the_one_hold(&pool, &body).await;
    sqlx::query(
        "UPDATE tool_approval_requests SET expires_at = now() - interval '1 second' WHERE id = $1",
    )
    .bind(&hold)
    .execute(&pool)
    .await
    .expect("lapse");
    let expired = routes::approvals::expire_pending_approvals(&pool)
        .await
        .expect("sweep");
    assert_eq!(expired, 1);
    assert_eq!(statuses(&approvals(&pool).await), ["expired"]);
    assert!(executions(&pool).await.is_empty());
    assert_eq!(runner_calls(&runner).await, 0);
}

/// The window the mode states is the hold's: the assignment's when it states
/// one, else the group's.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_modes_window_is_the_holds(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let _runner = runner().await;
    let key = topology(&pool).await;
    group_mode(
        &pool,
        json!({"execution_mode": "approval_required", "approval_timeout_seconds": 900}),
    )
    .await;
    assignment_governance(&pool, RG, json!({"approval_timeout_seconds": 240})).await;

    let (_, body) = rest(&pool, &key, 100, &[]).await;
    the_one_hold(&pool, &body).await;
    assert_eq!(body["timeout_seconds"], json!(240), "{body}");
}

/// An assignment whose governance cannot be read may say `disabled`: the
/// call is refused, on every surface, and nothing is raised or run.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_assignment_that_cannot_be_read_refuses_the_call(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    sqlx::query(
        "ALTER TABLE resource_group_agent_skills RENAME COLUMN governance TO governance_unreadable",
    )
    .execute(&pool)
    .await
    .expect("make the governance unreadable");

    let (status, body) = rest(&pool, &key, 100, &[]).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    let (status, body) = a2a_legacy(&pool, &key, 100, &[]).await;
    assert!(!status.is_success(), "{status} {body}");
    assert!(approvals(&pool).await.is_empty());
    assert!(executions(&pool).await.is_empty());
    assert_eq!(runner_calls(&runner).await, 0);
}

// ── an assignment inherited from an ancestor group ───────────────────────

const PARENT: &str = "rg-two-holds-parent";

/// The skill is assigned to `PARENT` only; `RG` is its child and inherits.
async fn inherit_the_skill_from_a_parent(pool: &PgPool) {
    common::seed_resource_group(pool, PARENT, T).await;
    sqlx::query("UPDATE resource_groups SET hierarchy_path = '/' || id WHERE id = $1")
        .bind(PARENT)
        .execute(pool)
        .await
        .expect("parent path");
    sqlx::query(
        "UPDATE resource_groups SET parent_group_id = $2, inherit_from_parent = true, \
         hierarchy_level = 1, hierarchy_path = '/' || $2 || '/' || id WHERE id = $1",
    )
    .bind(RG)
    .bind(PARENT)
    .execute(pool)
    .await
    .expect("child of the parent");
    sqlx::query(
        "UPDATE resource_group_agent_skills \
         SET resource_group_id = $2, id = concat('rgas-', $3, '-', $2) \
         WHERE resource_group_id = $1 AND agent_skill_id = $3",
    )
    .bind(RG)
    .bind(PARENT)
    .bind(SKILL_ID)
    .execute(pool)
    .await
    .expect("assign to the parent");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_mode_of_an_inherited_assignment_holds_the_call(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    inherit_the_skill_from_a_parent(&pool).await;
    assignment_governance(
        &pool,
        PARENT,
        json!({"execution_mode": "approval_required", "approval_timeout_seconds": 300}),
    )
    .await;

    let (status, body) = rest(&pool, &key, 100, &[]).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    let hold = the_one_hold(&pool, &body).await;
    assert_eq!(
        body["timeout_seconds"],
        json!(300),
        "the inherited assignment's window: {body}"
    );
    assert_eq!(runner_calls(&runner).await, 0);
    assert!(executions(&pool).await.is_empty());

    approve(&pool, &hold, "tok-inherited").await;
    let (status, body) = rest(&pool, &key, 100, &["tok-inherited"]).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["execution_status"], json!("COMPLETED"), "{body}");
    assert_eq!(runner_calls(&runner).await, 1);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_inherited_assignment_that_is_disabled_refuses_the_call(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    inherit_the_skill_from_a_parent(&pool).await;
    assignment_governance(&pool, PARENT, json!({"execution_mode": "disabled"})).await;

    let (status, body) = rest(&pool, &key, 100, &[]).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert!(approvals(&pool).await.is_empty());
    assert_eq!(runner_calls(&runner).await, 0);
    assert!(executions(&pool).await.is_empty());
}

/// The group's own assignment cannot relax the one it also inherits: the
/// strictest of the two applies.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_own_assignment_does_not_relax_an_inherited_one(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    inherit_the_skill_from_a_parent(&pool).await;
    assignment_governance(
        &pool,
        PARENT,
        json!({"execution_mode": "approval_required"}),
    )
    .await;
    common::bind_skill_to_rg(&pool, SKILL_ID, RG).await;
    assignment_governance(&pool, RG, json!({"execution_mode": "unrestricted"})).await;

    let (status, body) = rest(&pool, &key, 100, &[]).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    the_one_hold(&pool, &body).await;
    assert_eq!(runner_calls(&runner).await, 0);

    // A group that does not inherit reads its own assignment only.
    sqlx::query("UPDATE resource_groups SET inherit_from_parent = false WHERE id = $1")
        .bind(RG)
        .execute(&pool)
        .await
        .expect("stop inheriting");
    let (status, body) = rest(&pool, &key, 200, &[]).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["execution_status"], json!("COMPLETED"), "{body}");
    assert_eq!(runner_calls(&runner).await, 1);
}

// ── the surfaces in front of the skill ───────────────────────────────────

/// The A2A route's policy hold and the skill's mode are two questions: both
/// are raised in one answer and the call runs on one retry with both tokens.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_policy_hold_and_the_skills_mode_are_raised_together_on_a_legacy_task(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_a2a_policy_hold(&pool).await;
    group_mode(
        &pool,
        json!({"execution_mode": "approval_required", "approval_timeout_seconds": 240}),
    )
    .await;

    let (status, body) = a2a_legacy(&pool, &key, 100, &[]).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    let held = approvals(&pool).await;
    assert_eq!(held.len(), 2, "both holds are raised at once: {held:?}");
    let (window,): (i32,) =
        sqlx::query_as("SELECT timeout_seconds FROM tool_approval_requests WHERE id = $1")
            .bind(of_type(&held, "skill_exec"))
            .fetch_one(&pool)
            .await
            .expect("the skill's hold");
    assert_eq!(
        window, 240,
        "the skill's hold, raised by the route in front, has the mode's window"
    );
    assert_eq!(listed(&body).len(), 2, "{body}");
    assert!(executions(&pool).await.is_empty());
    approve(&pool, of_type(&held, "a2a_call"), "tok-card").await;
    approve(&pool, of_type(&held, "skill_exec"), "tok-skill").await;

    let (status, body) = a2a_legacy(&pool, &key, 100, &["tok-card", "tok-skill"]).await;
    assert!(status.is_success(), "{status} {body}");
    assert_eq!(body["execution_status"], json!("COMPLETED"), "{body}");
    assert!(body.get("approval_status").is_none(), "{body}");
    assert_eq!(runner_calls(&runner).await, 1, "{body}");
    let held = approvals(&pool).await;
    assert_eq!(statuses(&held), ["consumed", "consumed"], "{held:?}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_policy_hold_and_the_skills_mode_are_raised_together_on_a_v1_message(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_a2a_policy_hold(&pool).await;
    assignment_governance(&pool, RG, json!({"execution_mode": "approval_required"})).await;

    let (status, body) = a2a_v1(&pool, &key, 100, None, &[]).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    let held = approvals(&pool).await;
    assert_eq!(held.len(), 2, "both holds are raised at once: {held:?}");
    let named = find(&body, "approvalRequestIds")
        .and_then(Value::as_str)
        .unwrap_or_else(|| panic!("the answer names every approval: {body}"));
    assert!(
        held.iter().all(|h| named.contains(&h.0)),
        "{named} {held:?}"
    );
    let task_id = find(&body, "taskId")
        .and_then(Value::as_str)
        .map(str::to_string);
    approve(&pool, of_type(&held, "a2a_call"), "tok-card").await;
    approve(&pool, of_type(&held, "skill_exec"), "tok-skill").await;

    let (status, body) = a2a_v1(
        &pool,
        &key,
        100,
        task_id.as_deref(),
        &["tok-card", "tok-skill"],
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(
        find(&body, "state").and_then(Value::as_str),
        Some("TASK_STATE_COMPLETED"),
        "{body}"
    );
    assert_eq!(runner_calls(&runner).await, 1, "{body}");
}

/// With no gate of its own in front, a v1 task whose skill is held by its
/// mode waits for the authorisation; it is not `working`.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_v1_task_whose_skill_is_held_by_its_mode_waits_for_authorisation(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    group_mode(&pool, json!({"execution_mode": "approval_required"})).await;

    let (status, body) = a2a_v1(&pool, &key, 100, None, &[]).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(
        find(&body, "state").and_then(Value::as_str),
        Some("TASK_STATE_AUTH_REQUIRED"),
        "{body}"
    );
    let held = approvals(&pool).await;
    assert_eq!(held.len(), 1, "{held:?}");
    assert_eq!(
        find(&body, "approvalRequestId").and_then(Value::as_str),
        Some(held[0].0.as_str()),
        "{body}"
    );
    assert!(find(&body, "pollUrl").is_some(), "{body}");
    assert_eq!(runner_calls(&runner).await, 0);
    assert!(executions(&pool).await.is_empty());

    let task_id = find(&body, "taskId")
        .or_else(|| find(&body, "id"))
        .and_then(Value::as_str)
        .map(str::to_string);
    approve(&pool, &held[0].0, "tok-skill").await;
    let (status, body) = a2a_v1(&pool, &key, 100, task_id.as_deref(), &["tok-skill"]).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(
        find(&body, "state").and_then(Value::as_str),
        Some("TASK_STATE_COMPLETED"),
        "{body}"
    );
    assert_eq!(runner_calls(&runner).await, 1);
}

/// `skills__run_script` on a skill held by its mode: the tool result is an
/// error that carries the approval envelope, and the call retried with the
/// token runs.
#[sqlx::test(migrations = "./tests/migrations")]
async fn run_script_under_the_mode_names_the_hold_and_runs_with_the_token(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    group_mode(&pool, json!({"execution_mode": "approval_required"})).await;

    let (status, body) = run_script(&pool, &key, script_arguments(100), &[]).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let held = approvals(&pool).await;
    assert_eq!(held.len(), 1, "{held:?} {body}");
    assert_eq!(body["result"]["isError"], json!(true), "{body}");
    let text = body["result"]["content"][0]["text"]
        .as_str()
        .unwrap_or_default();
    assert!(
        text.contains("approval_required")
            && text.contains(&held[0].0)
            && text.contains("poll_url"),
        "the result names the approval to wait for: {body}"
    );
    assert!(!text.contains("pending_approval"), "{body}");
    assert_eq!(runner_calls(&runner).await, 0);
    assert!(executions(&pool).await.is_empty());

    approve(&pool, &held[0].0, "tok-skill").await;
    let (status, body) = run_script(&pool, &key, script_arguments(100), &["tok-skill"]).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(
        runner_calls(&runner).await,
        1,
        "the approved script runs: {body}"
    );
    assert_eq!(statuses(&approvals(&pool).await), ["consumed"]);
}

/// A band on the tool and the skill's mode: two questions, raised together.
#[sqlx::test(migrations = "./tests/migrations")]
async fn run_script_under_a_tool_band_and_the_skills_mode_names_both_and_runs_with_both(
    pool: PgPool,
) {
    let _serial = SERIAL.lock().await;
    let runner = runner().await;
    let key = topology(&pool).await;
    seed_tool_band(&pool).await;
    skill_asks_for_approval(&pool).await;

    let (status, body) = run_script(&pool, &key, script_arguments(8_000), &[]).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    let held = approvals(&pool).await;
    assert_eq!(
        held.len(),
        2,
        "both holds are raised at once: {held:?} {body}"
    );
    assert_eq!(listed(&body).len(), 2, "{body}");
    approve(&pool, of_type(&held, "tool"), "tok-tool").await;
    approve(&pool, of_type(&held, "skill_exec"), "tok-skill").await;

    let (status, body) = run_script(
        &pool,
        &key,
        script_arguments(8_000),
        &["tok-tool", "tok-skill"],
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(runner_calls(&runner).await, 1, "{body}");
    let held = approvals(&pool).await;
    assert_eq!(statuses(&held), ["consumed", "consumed"], "{held:?}");
}
