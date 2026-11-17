// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! An inbound A2A call to a capability bound to a SKILL must run the
//! skill and settle its execution, on both inbound paths.
//!
//! Both A2A paths (v1.0 `message:send` and the legacy
//! `/v1/proxy/a2a/inbound`) called `skill_orchestrator::execute_skill` — which
//! only authorises, gates and records a RUNNING execution — and then answered
//! "Skill '<name>' v<ver> dispatched … status=RUNNING". Nothing ran the skill
//! and nothing settled the row, so every such call leaked a RUNNING execution
//! (seen live on 0.11.25: rows RUNNING for 40+ minutes). RUNNING executions
//! count against the group's `max_concurrent_executions`, so the leak
//! eventually refused every skill in the group.
//!
//! These tests pin, per path: the execution is COMPLETED after a successful
//! run and carries the skill's output back to the caller; FAILED when the
//! runner errors; and a skill whose execution mode asks for a person is held
//! in the approval queue: no execution exists and nothing runs until the
//! same call is retried with the approval token.
//!
//! The skill runner is a wiremock server reached through `BRUTOR_SKILL_RUNNER_URL`.
//! The env is process-wide and each test sets its own value, so [`SERIAL`]
//! keeps the tests from overlapping when they share a process (`cargo test`).

use axum::body::Body;
use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::middleware;
use brutor_gateway_core::routes;
use brutor_gateway_core::services::SharedState;

use crate::common;

const T: &str = "t-a2a-skill";
const RG: &str = "rg-a2a-skill";
const CARD: &str = "card-a2a-skill";
const CARD_NAME: &str = "hello-world-a2a";
const SKILL_ID: &str = "skill-a2a-hello";
const SKILL_NAME: &str = "hello-world";
const STDOUT: &str = "Hello from the hello-world skill";

/// Under `cargo test` every test in this binary shares one process, so two tests
/// here would overwrite each other's `BRUTOR_SKILL_RUNNER_URL`. Under nextest (one process
/// per test) this lock is uncontended.
static SERIAL: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

/// Point the runner client at `runner` (process-wide; see [`SERIAL`]).
fn use_runner(runner: &MockServer) {
    unsafe {
        std::env::set_var("BRUTOR_SKILL_RUNNER_URL", runner.uri());
        std::env::set_var(
            "BRUTOR_SKILL_RUNNER_JWT_SECRET",
            "a2a-skill-settle-runner-secret",
        );
    }
    brutor_gateway_core::env_vars::reload_for_tests();
}

/// A runner that runs the skill's one script and prints [`STDOUT`].
async fn ok_runner() -> MockServer {
    let runner = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/v1/runner/execute"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "success": true,
            "output": "# Hello World\nThe assembled SKILL.md prompt.",
            "error": null,
            "script_results": {
                "hello.py": {
                    "success": true,
                    "stdout": format!("{STDOUT}\n"),
                    "stderr": "",
                    "exit_code": 0,
                    "duration_ms": 1.0,
                    "error": null,
                    "language": "python",
                    "execution_mode": "sandbox"
                }
            },
            "loaded_assets": {},
            "loaded_resources": {},
            "unresolved_refs": [],
            "duration_ms": 2.0
        })))
        .mount(&runner)
        .await;
    use_runner(&runner);
    runner
}

/// A runner that answers every call with HTTP 500.
async fn failing_runner() -> MockServer {
    let runner = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(500).set_body_string("runner exploded"))
        .mount(&runner)
        .await;
    use_runner(&runner);
    runner
}

/// Tenant, group, member key, a published skill bound to the group, and a
/// card (bound to the group) whose one capability is bound to the skill by
/// name — the seeded `hello-world-a2a` shape.
async fn topology(pool: &PgPool, approval_required: bool) -> String {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_end_user(pool, 9511, T).await;
    common::add_direct_membership(pool, 9511, RG, T).await;
    common::seed_agent_skill(
        pool,
        SKILL_ID,
        T,
        SKILL_NAME,
        "# Hello World\nSay hello.",
        "Says hello",
    )
    .await;
    common::bind_skill_to_rg(pool, SKILL_ID, RG).await;
    if approval_required {
        common::set_rg_skill_governance(pool, RG, json!({"execution_mode": "approval_required"}))
            .await;
    }
    common::seed_agent_card(pool, CARD, T, CARD_NAME).await;
    common::bind_agent_card_to_rg(pool, CARD, RG).await;
    sqlx::query(
        "INSERT INTO agent_card_capabilities \
         (id, agent_card_id, capability_name, handler_type, handler_ref, enabled) \
         VALUES ('cap-a2a-skill', $1, 'hello', 'skill', $2, true)",
    )
    .bind(CARD)
    .bind(SKILL_NAME)
    .execute(pool)
    .await
    .expect("seed skill capability");
    common::seed_api_key(pool, T, RG, Some(9511), "a2a-skill-key").await
}

async fn body_json(body: Body) -> Value {
    let bytes = body.collect().await.expect("collect body").to_bytes();
    serde_json::from_slice(&bytes).unwrap_or(Value::Null)
}

/// v1.0 `message:send` to the card.
async fn send_v1(state: &SharedState, key: &str) -> (StatusCode, Value) {
    send_v1_with(state, key, None, None).await
}

/// The same, on `task_id` and carrying an approval token when given.
async fn send_v1_with(
    state: &SharedState,
    key: &str,
    task_id: Option<&str>,
    token: Option<&str>,
) -> (StatusCode, Value) {
    let mut message = json!({"messageId": ulid::Ulid::new().to_string(), "role": "ROLE_USER",
                             "parts": [{"text": "say hello"}]});
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
            .insert("X-Approval-Token", token.parse().expect("token"));
    }
    let resp = common::build_a2a_v1_app(state.clone())
        .oneshot(req)
        .await
        .expect("router call");
    let status = resp.status();
    (status, body_json(resp.into_body()).await)
}

/// Legacy `/v1/proxy/a2a/inbound` to the card.
async fn send_legacy(state: &SharedState, key: &str) -> (StatusCode, Value) {
    send_legacy_with(state, key, None).await
}

async fn send_legacy_with(
    state: &SharedState,
    key: &str,
    token: Option<&str>,
) -> (StatusCode, Value) {
    let app = axum::Router::new()
        .nest("/v1/proxy/a2a", routes::a2a::router())
        .layer(middleware::tenant::TenantLayer::new(state.clone()))
        .layer(middleware::run_end::RunEndLayer::new(
            state.mcp_sessions.clone(),
        ))
        .with_state(state.clone());
    let mut req = common::build_post_request(
        "/v1/proxy/a2a/inbound",
        json!({"target_card_id": CARD, "capability": "hello", "message": {"text": "say hello"}}),
        Some(key),
        Some(T),
    );
    if let Some(token) = token {
        req.headers_mut()
            .insert("X-Approval-Token", token.parse().expect("token"));
    }
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    (status, body_json(resp.into_body()).await)
}

/// The one pending approval request of the tenant, approved with `token`.
async fn approve_the_hold(pool: &PgPool, token: &str) -> String {
    let held: Vec<(String, String, String)> = sqlx::query_as(
        "SELECT id, capability_type, status FROM tool_approval_requests WHERE tenant_id = $1",
    )
    .bind(T)
    .fetch_all(pool)
    .await
    .expect("approvals");
    assert_eq!(held.len(), 1, "one approval request: {held:?}");
    assert_eq!(
        (held[0].1.as_str(), held[0].2.as_str()),
        ("skill_exec", "pending"),
        "{held:?}"
    );
    sqlx::query(
        "UPDATE tool_approval_requests \
         SET status = 'approved', approval_token = $2, resolved_at = now() WHERE id = $1",
    )
    .bind(&held[0].0)
    .bind(token)
    .execute(pool)
    .await
    .expect("approve");
    held[0].0.clone()
}

/// Every execution of the skill: (execution_status, approval_status, error_type).
async fn executions(pool: &PgPool) -> Vec<(String, String, Option<String>)> {
    sqlx::query_as(
        "SELECT execution_status::text, approval_status::text, error_type \
         FROM agent_skill_executions WHERE tenant_id = $1 AND skill_id = $2",
    )
    .bind(T)
    .bind(SKILL_ID)
    .fetch_all(pool)
    .await
    .expect("executions query")
}

/// The first value of `key` anywhere in `v`.
fn find<'a>(v: &'a Value, key: &str) -> Option<&'a Value> {
    match v {
        Value::Object(m) => m.get(key).or_else(|| m.values().find_map(|x| find(x, key))),
        Value::Array(a) => a.iter().find_map(|x| find(x, key)),
        _ => None,
    }
}

async fn runner_calls(runner: &MockServer) -> usize {
    runner.received_requests().await.unwrap_or_default().len()
}

// ---------------------------------------------------------------------------
// v1.0 message:send
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn v1_skill_capability_runs_the_skill_and_completes_the_execution(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = ok_runner().await;
    let key = topology(&pool, false).await;
    let state = common::build_test_state(pool.clone());

    let (status, body) = send_v1(&state, &key).await;
    assert_eq!(status, StatusCode::OK, "body={body}");

    assert_eq!(
        runner_calls(&runner).await,
        1,
        "the skill must actually run"
    );
    let execs = executions(&pool).await;
    assert_eq!(execs.len(), 1, "{execs:?}");
    assert_eq!(
        execs[0].0, "COMPLETED",
        "a run skill execution must be settled, never left RUNNING: {execs:?}"
    );

    let text = body.to_string();
    assert!(
        text.contains(STDOUT),
        "the artifact must carry the skill's output, not a 'dispatched' line; body={body}"
    );
    assert!(!text.contains("dispatched"), "body={body}");
    assert_eq!(
        find(&body, "state").and_then(Value::as_str),
        Some("TASK_STATE_COMPLETED"),
        "body={body}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn v1_skill_runner_error_settles_the_execution_failed(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = failing_runner().await;
    let key = topology(&pool, false).await;
    let state = common::build_test_state(pool.clone());

    let (_status, body) = send_v1(&state, &key).await;

    assert_eq!(runner_calls(&runner).await, 1);
    let execs = executions(&pool).await;
    assert_eq!(execs.len(), 1, "{execs:?}");
    assert_eq!(execs[0].0, "FAILED", "{execs:?}");
    assert_eq!(execs[0].2.as_deref(), Some("skill_execution_error"));
    assert_eq!(
        find(&body, "state").and_then(Value::as_str),
        Some("TASK_STATE_FAILED"),
        "a failed run must fail the task; body={body}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn v1_skill_held_by_its_execution_mode_waits_then_runs_with_the_token(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = ok_runner().await;
    let key = topology(&pool, true).await;
    let state = common::build_test_state(pool.clone());

    let (status, body) = send_v1(&state, &key).await;
    assert_eq!(status, StatusCode::OK, "body={body}");
    assert_eq!(
        find(&body, "state").and_then(Value::as_str),
        Some("TASK_STATE_AUTH_REQUIRED"),
        "a held task waits for an authorisation: body={body}"
    );
    assert_eq!(
        runner_calls(&runner).await,
        0,
        "a held call must not run before approval"
    );
    assert!(
        executions(&pool).await.is_empty(),
        "a held call is not an execution"
    );
    let hold = approve_the_hold(&pool, "tok-v1").await;
    assert_eq!(
        find(&body, "approvalRequestId").and_then(Value::as_str),
        Some(hold.as_str()),
        "body={body}"
    );

    let task_id = find(&body, "id")
        .and_then(Value::as_str)
        .map(str::to_string);
    let (status, body) = send_v1_with(&state, &key, task_id.as_deref(), Some("tok-v1")).await;
    assert_eq!(status, StatusCode::OK, "body={body}");
    assert_eq!(
        find(&body, "state").and_then(Value::as_str),
        Some("TASK_STATE_COMPLETED"),
        "body={body}"
    );
    assert_eq!(runner_calls(&runner).await, 1);
    let execs = executions(&pool).await;
    assert_eq!(execs.len(), 1, "{execs:?}");
    assert_eq!(execs[0].0, "COMPLETED", "{execs:?}");
}

// ---------------------------------------------------------------------------
// Legacy /v1/proxy/a2a/inbound
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn legacy_skill_capability_runs_the_skill_and_completes_the_execution(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = ok_runner().await;
    let key = topology(&pool, false).await;
    let state = common::build_test_state(pool.clone());

    let (status, body) = send_legacy(&state, &key).await;
    assert!(status.is_success(), "status={status} body={body}");

    assert_eq!(
        runner_calls(&runner).await,
        1,
        "the skill must actually run"
    );
    let execs = executions(&pool).await;
    assert_eq!(execs.len(), 1, "{execs:?}");
    assert_eq!(execs[0].0, "COMPLETED", "{execs:?}");
    assert_eq!(body["execution_status"], json!("COMPLETED"), "body={body}");
    assert_eq!(body["output"], json!(STDOUT), "body={body}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn legacy_skill_runner_error_settles_the_execution_failed(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = failing_runner().await;
    let key = topology(&pool, false).await;
    let state = common::build_test_state(pool.clone());

    let (_status, body) = send_legacy(&state, &key).await;

    assert_eq!(runner_calls(&runner).await, 1);
    let execs = executions(&pool).await;
    assert_eq!(execs.len(), 1, "{execs:?}");
    assert_eq!(execs[0].0, "FAILED", "{execs:?}");
    assert_eq!(execs[0].2.as_deref(), Some("skill_execution_error"));
    assert_eq!(body["execution_status"], json!("FAILED"), "body={body}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn legacy_skill_held_by_its_execution_mode_waits_then_runs_with_the_token(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let runner = ok_runner().await;
    let key = topology(&pool, true).await;
    let state = common::build_test_state(pool.clone());

    let (status, body) = send_legacy(&state, &key).await;
    assert_eq!(status, StatusCode::ACCEPTED, "body={body}");
    assert_eq!(runner_calls(&runner).await, 0);
    assert!(
        executions(&pool).await.is_empty(),
        "a held call is not an execution"
    );
    let hold = approve_the_hold(&pool, "tok-legacy").await;
    assert_eq!(
        body["approval_request_id"],
        json!(hold),
        "the answer is the approval envelope: body={body}"
    );
    assert!(body.get("execution_status").is_none(), "body={body}");

    let (status, body) = send_legacy_with(&state, &key, Some("tok-legacy")).await;
    assert!(status.is_success(), "status={status} body={body}");
    assert_eq!(runner_calls(&runner).await, 1);
    let execs = executions(&pool).await;
    assert_eq!(execs.len(), 1, "{execs:?}");
    assert_eq!(execs[0].0, "COMPLETED", "{execs:?}");
    assert_eq!(body["execution_status"], json!("COMPLETED"), "body={body}");
}

// ---------------------------------------------------------------------------
// Safety net: abandoned RUNNING executions are reaped
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn abandoned_running_executions_are_reaped_and_fresh_ones_are_not(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    use brutor_gateway_core::services::skill_orchestrator;

    topology(&pool, false).await;
    for (id, age_secs) in [("exec-stale", 3600_i64), ("exec-fresh", 5)] {
        sqlx::query(
            "INSERT INTO agent_skill_executions \
             (id, tenant_id, skill_id, group_id, request_id, execution_status, approval_status, \
              started_at, running_at) \
             VALUES ($1, $2, $3, $4, $1, 'RUNNING'::executionstatus, \
                     'NOT_REQUIRED'::approvalstatus, \
                     NOW() - make_interval(secs => $5), NOW() - make_interval(secs => $5))",
        )
        .bind(id)
        .bind(T)
        .bind(SKILL_ID)
        .bind(RG)
        .bind(age_secs as f64)
        .execute(&pool)
        .await
        .expect("seed execution");
    }

    let reaped = skill_orchestrator::reap_abandoned_skill_executions(
        &pool,
        skill_orchestrator::ABANDONED_EXECUTION_AFTER_SECS,
    )
    .await
    .expect("reap");
    assert_eq!(reaped, 1);

    let rows: Vec<(String, String, Option<String>)> = sqlx::query_as(
        "SELECT id, execution_status::text, error_type FROM agent_skill_executions \
         WHERE tenant_id = $1 ORDER BY id",
    )
    .bind(T)
    .fetch_all(&pool)
    .await
    .expect("executions");
    assert_eq!(
        rows,
        vec![
            ("exec-fresh".into(), "RUNNING".into(), None),
            (
                "exec-stale".into(),
                "FAILED".into(),
                Some("abandoned".into())
            ),
        ]
    );
}
