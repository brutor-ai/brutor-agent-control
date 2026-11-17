// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The run's action ceiling (`max_actions_per_run`), through the HTTP
//! surfaces, on gateway states whose counters live in one REAL Redis.
//!
//! A run's model calls are capped by `max_llm_calls_per_run`. A run that
//! calls tools, runs skills or hands work to other agents without ever
//! calling a model takes no model call, so that ceiling never stops it. The
//! action ceiling counts every governed step that is not a model call:
//!
//! * every counted surface refuses a run at its ceiling, for an API key and
//!   for an agent, with the refusal its sibling run ceilings give there
//!   (same status, same body shape, recorded as `run_cap_reached`) and its
//!   own term in the message;
//! * a request is one action, on the surface that received it: a skill run
//!   by a system server's tool or for an agent-to-agent task is part of it;
//! * a step a later gate refuses gives its unit back, a dispatched one keeps
//!   it;
//! * a burst of concurrent steps on two gateway states never overshoots;
//! * a model call is no action and an action is no model call;
//! * no ceiling, or a malformed one, counts nothing; counters that cannot be
//!   operated refuse.
//!
//! Needs `TEST_REDIS_URL` (CI provides a service container).

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::services::SharedState;
use brutor_gateway_core::services::cache::usage_state::{Shape, UsageState};
use brutor_gateway_core::services::limits;
use brutor_gateway_core::services::run_budget::{self, RunCaps};

use super::run_controls_parity::{self as rc, Caller, Surface, T};
use crate::common;

const TERM: &str = "max_actions_per_run";

pub(crate) fn redis_url() -> String {
    std::env::var("TEST_REDIS_URL").unwrap_or_else(|_| "redis://127.0.0.1:6379/15".to_string())
}

pub(crate) async fn redis_conn() -> redis::aio::ConnectionManager {
    let url = redis_url();
    let client = redis::Client::open(url.as_str()).expect("redis url");
    redis::aio::ConnectionManager::new(client)
        .await
        .unwrap_or_else(|e| panic!("connect to the test Redis at {url} (set TEST_REDIS_URL): {e}"))
}

/// A gateway state whose limits live in the shared, REAL Redis.
pub(crate) async fn replica(pool: &PgPool) -> SharedState {
    let config = brutor_gateway_core::config::AppConfig::for_test(
        "postgresql://placeholder/replaced".to_string(),
    );
    std::sync::Arc::new(brutor_gateway_core::services::AppState::new(
        config,
        pool.clone(),
        Some(redis_conn().await),
        reqwest::Client::new(),
        brutor_gateway_core::services::telemetry::TelemetryState {
            prometheus_handle: None,
        },
    ))
}

/// An upstream that answers every POST, after `delay_ms`.
pub(crate) async fn upstream(delay_ms: u64) -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({
                    "id": "chatcmpl-x", "object": "chat.completion", "model": "test-gpt",
                    "jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": "done"}]},
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
                }))
                .set_delay(std::time::Duration::from_millis(delay_ms)),
        )
        .mount(&mock)
        .await;
    mock
}

/// The parity topology, with the agent card reachable at `upstream` so an
/// outbound hop is dispatched.
pub(crate) async fn world(
    pool: &PgPool,
    upstream: &MockServer,
    caller: Caller,
    rg: &str,
) -> String {
    let key = rc::topology(pool, &upstream.uri(), caller, rg).await;
    sqlx::query("UPDATE agent_cards SET card_json = $2 WHERE id = $1")
        .bind(rc::CARD)
        .bind(json!({"name": "rcpar-agent", "url": upstream.uri(),
                     "capabilities": [{"name": "echo"}]}))
        .execute(pool)
        .await
        .expect("card url");
    key
}

/// `surface`'s request of the run the caller asserts as `run`.
pub(crate) fn request(surface: Surface, key: &str, run: &str) -> Request<Body> {
    let mut req = rc::request(surface, key);
    req.headers_mut()
        .insert("x-brutor-run-id", run.parse().expect("run header"));
    req
}

async fn send(state: &SharedState, surface: Surface, key: &str, run: &str) -> (StatusCode, Value) {
    let resp = rc::app(state, surface)
        .oneshot(request(surface, key, run))
        .await
        .expect("router call");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.expect("body").to_bytes();
    let body = serde_json::from_slice(&bytes)
        .unwrap_or_else(|_| Value::String(String::from_utf8_lossy(&bytes).to_string()));
    (status, body)
}

pub(crate) fn actions_key(root: &str) -> String {
    UsageState::run_counter_key(T, root, "actions")
}

/// What the run's action counter holds (0 when it does not exist).
async fn actions_taken(state: &SharedState, root: &str) -> i64 {
    state
        .usage_state
        .counter_value(&actions_key(root))
        .await
        .expect("read the action counter")
}

/// The counter once it has settled at `want` (a refused step's unit goes
/// back off the request path).
async fn actions_settle_at(state: &SharedState, root: &str, want: i64) -> i64 {
    let mut seen = -1;
    for _ in 0..100 {
        seen = actions_taken(state, root).await;
        if seen == want {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }
    seen
}

async fn actions_key_exists(root: &str) -> bool {
    let mut c = redis_conn().await;
    let n: i64 = redis::cmd("EXISTS")
        .arg(actions_key(root))
        .query_async(&mut c)
        .await
        .expect("EXISTS");
    n == 1
}

/// Take `n` actions of the run directly on the shared counters, as `n`
/// dispatched steps would have: the run is then `n` into a ceiling of `limit`.
async fn take_actions(state: &SharedState, root: &str, limit: i64, n: i64) {
    let caps = RunCaps {
        max_actions: Some(limit),
        ..Default::default()
    };
    for _ in 0..n {
        let dim = run_budget::action_dim_for(&caps, T, root).expect("a capped run");
        limits::admit(&state.usage_state, None, vec![dim], Shape::count())
            .await
            .expect("redis")
            .expect("under the ceiling");
    }
}

/// The run-cap refusal rows of the tenant: (root, error message).
async fn run_cap_rows(pool: &PgPool, want: usize) -> Vec<(Option<String>, Option<String>)> {
    let mut rows = Vec::new();
    for _ in 0..80 {
        rows = sqlx::query_as(
            "SELECT root_task_id, error_message FROM proxy_logs \
             WHERE tenant_id = $1 AND proxy_subtype = 'run_cap_reached' ORDER BY timestamp",
        )
        .bind(T)
        .fetch_all(pool)
        .await
        .expect("proxy_logs query");
        if rows.len() >= want {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    rows
}

/// A JSON value with every leaf replaced by its type: the body's shape.
pub(crate) fn shape(v: &Value) -> Value {
    match v {
        Value::Object(m) => Value::Object(m.iter().map(|(k, v)| (k.clone(), shape(v))).collect()),
        Value::Array(a) => Value::Array(a.iter().map(shape).collect()),
        Value::String(_) => json!("string"),
        Value::Number(_) => json!("number"),
        Value::Bool(_) => json!("bool"),
        Value::Null => Value::Null,
    }
}

fn rg_name(tag: &str, surface: Surface, caller: Caller) -> String {
    format!("rg-act-{tag}-{surface:?}-{caller:?}").to_lowercase()
}

// ---------------------------------------------------------------------------
// Every counted surface refuses a run at its ceiling, as its siblings do
// ---------------------------------------------------------------------------

/// A run whose action counter stands at its ceiling takes no further step on
/// `surface`. The refusal is the one the same surface gives a run at its
/// model-call ceiling (status and body shape), recorded as `run_cap_reached`
/// on the run, and names the action term. The refused step takes no unit and
/// reaches no upstream.
async fn is_refused_at_the_ceiling(pool: PgPool, surface: Surface, caller: Caller) {
    let up = upstream(0).await;
    let rg = rg_name("ceil", surface, caller);
    let key = world(&pool, &up, caller, &rg).await;
    let state = replica(&pool).await;
    common::set_rg_operating_envelope(
        &pool,
        &rg,
        json!({"max_actions_per_run": 3, "max_llm_calls_per_run": 1}),
    )
    .await;

    // The sibling: a second run of the same caller, at its model-call
    // ceiling.
    let sibling_root = rc::run_root_of(&state, &key, "run-at-its-model-call-ceiling");
    state
        .usage_state
        .run_commit(T, &sibling_root, 10, 0.0)
        .await;
    let (sibling_status, sibling_body) =
        send(&state, surface, &key, "run-at-its-model-call-ceiling").await;
    assert!(
        sibling_status.is_client_error(),
        "{surface:?} × {caller:?}: the premise — a run at its model-call ceiling is refused \
         here, got {sibling_status}"
    );

    let root = rc::run_root_of(&state, &key, "run-at-its-action-ceiling");
    take_actions(&state, &root, 3, 3).await;
    let (status, body) = send(&state, surface, &key, "run-at-its-action-ceiling").await;

    assert_eq!(
        status, sibling_status,
        "{surface:?} × {caller:?}: the action ceiling answers with another status than the \
         model-call ceiling does here: {body}"
    );
    assert_eq!(
        shape(&body),
        shape(&sibling_body),
        "{surface:?} × {caller:?}: another body shape than the sibling's.\n action: {body}\n \
         sibling: {sibling_body}"
    );
    assert!(
        body.to_string().contains(TERM),
        "{surface:?} × {caller:?}: the refusal does not name its term: {body}"
    );
    assert!(
        !sibling_body.to_string().contains(TERM),
        "{surface:?} × {caller:?}: the model-call refusal names the action term: {sibling_body}"
    );

    let rows = run_cap_rows(&pool, 2).await;
    let mine: Vec<_> = rows
        .iter()
        .filter(|(r, _)| r.as_deref() == Some(root.as_str()))
        .collect();
    assert_eq!(
        mine.len(),
        1,
        "{surface:?} × {caller:?}: refused with {status}, but no `run_cap_reached` row on the \
         run — something else refused it: {rows:?}"
    );
    assert!(
        mine[0].1.as_deref().unwrap_or("").contains(TERM),
        "{surface:?} × {caller:?}: the row does not name the term: {:?}",
        mine[0]
    );
    assert!(
        rows.iter()
            .any(|(r, _)| r.as_deref() == Some(sibling_root.as_str())),
        "{surface:?} × {caller:?}: the sibling refusal is recorded the same way: {rows:?}"
    );

    assert_eq!(
        actions_settle_at(&state, &root, 3).await,
        3,
        "{surface:?} × {caller:?}: a refused step changed the run's count"
    );
    assert_eq!(
        up.received_requests().await.map_or(0, |r| r.len()),
        0,
        "{surface:?} × {caller:?}: a refused step reached the upstream"
    );
}

macro_rules! refused_at_the_ceiling {
    ($($name:ident => ($s:ident, $c:ident)),* $(,)?) => {
        $(
            #[sqlx::test(migrations = "./tests/migrations")]
            async fn $name(pool: PgPool) {
                is_refused_at_the_ceiling(pool, Surface::$s, Caller::$c).await;
            }
        )*
    };
}

refused_at_the_ceiling! {
    mcp_api_key_at_the_action_ceiling => (Mcp, ApiKey),
    mcp_agent_at_the_action_ceiling => (Mcp, Agent),
    a2a_outbound_api_key_at_the_action_ceiling => (A2aOutbound, ApiKey),
    a2a_outbound_agent_at_the_action_ceiling => (A2aOutbound, Agent),
    a2a_inbound_api_key_at_the_action_ceiling => (A2aInbound, ApiKey),
    a2a_inbound_agent_at_the_action_ceiling => (A2aInbound, Agent),
    a2a_v1_api_key_at_the_action_ceiling => (A2aV1, ApiKey),
    a2a_v1_agent_at_the_action_ceiling => (A2aV1, Agent),
    skill_api_key_at_the_action_ceiling => (Skill, ApiKey),
    skill_agent_at_the_action_ceiling => (Skill, Agent),
    skill_server_list_api_key_at_the_action_ceiling => (SkillServerList, ApiKey),
    skill_server_list_agent_at_the_action_ceiling => (SkillServerList, Agent),
    skill_server_load_api_key_at_the_action_ceiling => (SkillServerLoad, ApiKey),
    skill_server_read_resource_api_key_at_the_action_ceiling => (SkillServerReadResource, ApiKey),
    skill_server_render_template_api_key_at_the_action_ceiling => (SkillServerRenderTemplate, ApiKey),
    skill_server_run_script_api_key_at_the_action_ceiling => (SkillServerRunScript, ApiKey),
    skill_server_run_script_agent_at_the_action_ceiling => (SkillServerRunScript, Agent),
    search_server_web_api_key_at_the_action_ceiling => (SearchServerWeb, ApiKey),
    search_server_web_agent_at_the_action_ceiling => (SearchServerWeb, Agent),
}

/// An MCP capability call that is not a tool call (reading a resource,
/// getting a prompt) is a governed step of the run like a tool call, and is
/// refused at the ceiling like one.
#[sqlx::test(migrations = "./tests/migrations")]
async fn reading_a_resource_or_getting_a_prompt_is_an_action(pool: PgPool) {
    let up = upstream(0).await;
    let rg = "rg-act-resource-prompt";
    let key = world(&pool, &up, Caller::ApiKey, rg).await;
    let state = replica(&pool).await;
    common::set_rg_operating_envelope(&pool, rg, json!({"max_actions_per_run": 2})).await;
    let root = rc::run_root_of(&state, &key, "run-rp");

    let call = |body: Value| {
        let mut req =
            common::build_post_request("/v1/proxy/mcp/ms-rcpar", body, Some(&key), Some(T));
        req.headers_mut()
            .insert("x-brutor-run-id", "run-rp".parse().unwrap());
        req
    };
    let app = || common::build_mcp_proxy_app(state.clone());
    let read = json!({"jsonrpc": "2.0", "id": 1, "method": "resources/read",
                      "params": {"uri": "file:///notes.md"}});
    let prompt = json!({"jsonrpc": "2.0", "id": 2, "method": "prompts/get",
                        "params": {"name": "summarise"}});

    let r = app().oneshot(call(read.clone())).await.unwrap();
    assert!(r.status().is_success(), "{}", r.status());
    assert_eq!(actions_taken(&state, &root).await, 1);
    let r = app().oneshot(call(prompt.clone())).await.unwrap();
    assert!(r.status().is_success(), "{}", r.status());
    assert_eq!(actions_taken(&state, &root).await, 2);

    for body in [read, prompt] {
        let r = app().oneshot(call(body)).await.unwrap();
        assert_eq!(r.status(), StatusCode::TOO_MANY_REQUESTS);
    }
    assert_eq!(run_cap_rows(&pool, 2).await.len(), 2);
    assert_eq!(up.received_requests().await.unwrap().len(), 2);
    // Protocol chatter is no step: it is not counted and not refused.
    let list = json!({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}});
    let r = app().oneshot(call(list)).await.unwrap();
    assert!(r.status().is_success(), "{}", r.status());
    assert_eq!(actions_taken(&state, &root).await, 2);
}

// ---------------------------------------------------------------------------
// A dispatched step is one action; the step after the ceiling is refused
// ---------------------------------------------------------------------------

/// Two steps of a run with a ceiling of two are dispatched and counted, one
/// each; the third is refused by the action ceiling and counts nothing.
async fn counts_each_dispatched_step_once(pool: PgPool, surface: Surface) {
    let up = upstream(0).await;
    let rg = rg_name("count", surface, Caller::ApiKey);
    let key = world(&pool, &up, Caller::ApiKey, &rg).await;
    let state = replica(&pool).await;
    common::set_rg_operating_envelope(&pool, &rg, json!({"max_actions_per_run": 2})).await;
    let root = rc::run_root_of(&state, &key, "run-count");

    for n in 1..=2 {
        let (status, body) = send(&state, surface, &key, "run-count").await;
        assert!(
            !body.to_string().contains(TERM),
            "{surface:?}: step {n} of 2 was refused by the action ceiling ({status}): {body}"
        );
        assert_eq!(
            actions_settle_at(&state, &root, n).await,
            n,
            "{surface:?}: after {n} step(s) ({status}: {body})"
        );
    }
    assert!(
        run_cap_rows(&pool, 0).await.is_empty(),
        "{surface:?}: a step under the ceiling was recorded as refused by a run cap"
    );

    let (status, body) = send(&state, surface, &key, "run-count").await;
    assert!(
        status.is_client_error() && body.to_string().contains(TERM),
        "{surface:?}: the third step of a run capped at two actions: {status} {body}"
    );
    let rows = run_cap_rows(&pool, 1).await;
    assert_eq!(rows.len(), 1, "{surface:?}: {rows:?}");
    assert_eq!(rows[0].0.as_deref(), Some(root.as_str()));
    assert_eq!(actions_settle_at(&state, &root, 2).await, 2);

    // Another run of the same caller has its own count.
    let (status, body) = send(&state, surface, &key, "another-run").await;
    assert!(
        !body.to_string().contains(TERM),
        "{surface:?}: another run was refused by this run's ceiling ({status}): {body}"
    );
}

macro_rules! counted_once {
    ($($name:ident => $s:ident),* $(,)?) => {
        $(
            #[sqlx::test(migrations = "./tests/migrations")]
            async fn $name(pool: PgPool) {
                counts_each_dispatched_step_once(pool, Surface::$s).await;
            }
        )*
    };
}

counted_once! {
    an_mcp_tool_call_is_one_action => Mcp,
    an_a2a_outbound_hop_is_one_action => A2aOutbound,
    an_a2a_inbound_task_is_one_action => A2aInbound,
    an_a2a_v1_message_is_one_action => A2aV1,
    a_skill_execution_is_one_action => Skill,
    a_skill_server_list_call_is_one_action => SkillServerList,
    a_skill_server_load_call_is_one_action => SkillServerLoad,
    a_search_server_call_is_one_action => SearchServerWeb,
}

/// A `skills__run_script` call and the script run it starts are ONE action:
/// the tool call, not the tool call and the skill execution. The premise is
/// asserted: the call reached the skill execution (there is an execution
/// row), which is where a second count would be taken. Without it this
/// passed with the script run counted twice, because the skill had no such
/// script and the call never got as far as executing it.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_skill_server_script_run_is_one_action(pool: PgPool) {
    let up = upstream(0).await;
    let rg = "rg-act-run-script";
    let key = world(&pool, &up, Caller::ApiKey, rg).await;
    sqlx::query(
        "INSERT INTO agent_skill_scripts \
         (id, tenant_id, skill_id, filename, script_content, script_hash, language, execution_mode) \
         SELECT 'scr-act-run', $1, id, 'run.py', 'print(1)', 'h', 'PYTHON', \
                'SANDBOX'::scriptexecutionmode \
         FROM agent_skills WHERE tenant_id = $1 AND name = 'rcpar-skill'",
    )
    .bind(T)
    .execute(&pool)
    .await
    .expect("the script the call runs");
    let state = replica(&pool).await;
    common::set_rg_operating_envelope(&pool, rg, json!({"max_actions_per_run": 10})).await;
    let surface = Surface::SkillServerRunScript;

    for n in 1..=2 {
        let root = rc::run_root_of(&state, &key, "run-script");
        let (status, body) = send(&state, surface, &key, "run-script").await;
        assert!(
            !body.to_string().contains(TERM),
            "step {n} was refused by the action ceiling ({status}): {body}"
        );
        let executions: i64 =
            sqlx::query_scalar("SELECT count(*) FROM agent_skill_executions WHERE tenant_id = $1")
                .bind(T)
                .fetch_one(&pool)
                .await
                .unwrap();
        assert_eq!(
            executions, n,
            "the premise: the tool call reached the skill execution ({status}: {body})"
        );
        // Long enough for a second unit to land if one was taken.
        tokio::time::sleep(std::time::Duration::from_millis(300)).await;
        assert_eq!(
            actions_settle_at(&state, &root, n).await,
            n,
            "a script run is one action, after {n} call(s) ({status}: {body})"
        );
    }
}

/// An agent-to-agent task whose handler is a skill is one action: the task,
/// not the task and the skill execution it starts. On both A2A surfaces.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_a2a_task_that_runs_a_skill_is_one_action(pool: PgPool) {
    let up = upstream(0).await;
    let rg = "rg-act-a2a-skill";
    let key = world(&pool, &up, Caller::ApiKey, rg).await;
    sqlx::query(
        "UPDATE agent_card_capabilities SET handler_type = 'skill', handler_ref = $1 \
         WHERE id = 'cap-rcpar'",
    )
    .bind("rcpar-skill")
    .execute(&pool)
    .await
    .expect("bind the capability to the skill");
    let state = replica(&pool).await;
    common::set_rg_operating_envelope(&pool, rg, json!({"max_actions_per_run": 10})).await;

    for (surface, run) in [
        (Surface::A2aInbound, "run-inbound-skill"),
        (Surface::A2aV1, "run-v1-skill"),
    ] {
        let root = rc::run_root_of(&state, &key, run);
        let (status, body) = send(&state, surface, &key, run).await;
        assert!(
            !body.to_string().contains(TERM),
            "{surface:?}: {status} {body}"
        );
        let executions: i64 =
            sqlx::query_scalar("SELECT count(*) FROM agent_skill_executions WHERE tenant_id = $1")
                .bind(T)
                .fetch_one(&pool)
                .await
                .unwrap();
        assert!(
            executions >= 1,
            "{surface:?}: the premise — the task ran the skill ({status}: {body})"
        );
        assert_eq!(
            actions_settle_at(&state, &root, 1).await,
            1,
            "{surface:?}: a task that runs a skill is one action ({status}: {body})"
        );
    }
}

// ---------------------------------------------------------------------------
// A step a later gate refuses gives its unit back
// ---------------------------------------------------------------------------

/// The run controls admit the step before the authorization planes. An
/// agent with no grant is refused by agent authorization after that: the
/// step never ran, and the run has taken no action.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_step_refused_by_a_later_gate_is_not_counted(pool: PgPool) {
    let up = upstream(0).await;
    let rg = "rg-act-later-gate";
    let key = world(&pool, &up, Caller::Agent, rg).await;
    let state = replica(&pool).await;
    common::set_rg_operating_envelope(&pool, rg, json!({"max_actions_per_run": 2})).await;

    for surface in [Surface::Mcp, Surface::A2aOutbound, Surface::Skill] {
        let run = format!("run-later-{surface:?}");
        let root = rc::run_root_of(&state, &key, &run);
        // More attempts than the ceiling: were refused steps counted, the
        // later ones would be refused by the ceiling instead.
        for _ in 0..4 {
            let (status, body) = send(&state, surface, &key, &run).await;
            assert!(
                status.is_client_error() && !body.to_string().contains(TERM),
                "{surface:?}: an agent with no grant is refused by authorization, not by the \
                 action ceiling: {status} {body}"
            );
        }
        assert_eq!(
            actions_settle_at(&state, &root, 0).await,
            0,
            "{surface:?}: steps that never ran were counted"
        );
    }
    assert!(run_cap_rows(&pool, 0).await.is_empty());
    assert_eq!(up.received_requests().await.unwrap().len(), 0);
}

// ---------------------------------------------------------------------------
// A burst on two gateway states never overshoots
// ---------------------------------------------------------------------------

/// Forty tool calls of one run arrive together on two gateway states that
/// share the counters, against an upstream slow enough that none completes
/// before all have been judged. Exactly the ceiling are dispatched.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_burst_on_two_gateway_states_never_overshoots_the_ceiling(pool: PgPool) {
    let up = upstream(400).await;
    let rg = "rg-act-burst";
    let key = world(&pool, &up, Caller::ApiKey, rg).await;
    let (a, b) = (replica(&pool).await, replica(&pool).await);
    common::set_rg_operating_envelope(&pool, rg, json!({"max_actions_per_run": 7})).await;
    let root = rc::run_root_of(&a, &key, "run-burst");

    let tasks: Vec<_> = (0..40)
        .map(|i| {
            let state = if i % 2 == 0 { a.clone() } else { b.clone() };
            let key = key.clone();
            tokio::spawn(async move {
                let (status, body) = send(&state, Surface::Mcp, &key, "run-burst").await;
                (status, body.to_string())
            })
        })
        .collect();
    let mut dispatched = 0;
    let mut refused = 0;
    for t in tasks {
        let (status, body) = t.await.unwrap();
        if status.is_success() {
            dispatched += 1;
        } else {
            assert_eq!(status, StatusCode::TOO_MANY_REQUESTS, "{body}");
            assert!(body.contains(TERM), "{body}");
            refused += 1;
        }
    }
    assert_eq!(
        (dispatched, refused),
        (7, 33),
        "a ceiling of 7 dispatched {dispatched}"
    );
    assert_eq!(
        up.received_requests().await.unwrap().len(),
        7,
        "the upstream saw more calls than the run may take"
    );
    assert_eq!(actions_settle_at(&a, &root, 7).await, 7);
    assert_eq!(actions_taken(&b, &root).await, 7, "one count, two states");
    assert_eq!(run_cap_rows(&pool, 33).await.len(), 33);
}

/// The same across surfaces: tool calls, skill executions and inbound tasks
/// of one run, together on two states, share one ceiling.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_mixed_burst_shares_one_ceiling(pool: PgPool) {
    let up = upstream(300).await;
    let rg = "rg-act-mixed";
    let key = world(&pool, &up, Caller::ApiKey, rg).await;
    let (a, b) = (replica(&pool).await, replica(&pool).await);
    common::set_rg_operating_envelope(&pool, rg, json!({"max_actions_per_run": 5})).await;
    let root = rc::run_root_of(&a, &key, "run-mixed");
    let surfaces = [
        Surface::Mcp,
        Surface::A2aInbound,
        Surface::SkillServerList,
        Surface::A2aOutbound,
    ];

    let tasks: Vec<_> = (0..24)
        .map(|i| {
            let state = if i % 2 == 0 { a.clone() } else { b.clone() };
            let key = key.clone();
            let surface = surfaces[i % surfaces.len()];
            tokio::spawn(async move {
                let (_, body) = send(&state, surface, &key, "run-mixed").await;
                body.to_string().contains(TERM)
            })
        })
        .collect();
    let mut refused_by_the_ceiling = 0;
    for t in tasks {
        if t.await.unwrap() {
            refused_by_the_ceiling += 1;
        }
    }
    assert_eq!(refused_by_the_ceiling, 19, "24 steps, a ceiling of 5");
    assert_eq!(actions_settle_at(&a, &root, 5).await, 5);
}

// ---------------------------------------------------------------------------
// A model call is no action; an action is no model call
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn model_calls_and_actions_are_counted_apart(pool: PgPool) {
    let up = upstream(0).await;
    let rg = "rg-act-apart";
    let key = world(&pool, &up, Caller::ApiKey, rg).await;
    let state = replica(&pool).await;
    common::set_rg_operating_envelope(
        &pool,
        rg,
        json!({"max_actions_per_run": 1, "max_llm_calls_per_run": 3}),
    )
    .await;
    let root = rc::run_root_of(&state, &key, "run-apart");
    let calls_key = UsageState::run_counter_key(T, &root, "calls");
    let model_calls = || async { state.usage_state.counter_value(&calls_key).await.unwrap() };

    // Two model calls: no action.
    for n in 1..=2 {
        let (status, body) = send(&state, Surface::LlmChat, &key, "run-apart").await;
        assert_eq!(status, StatusCode::OK, "{body}");
        assert_eq!(model_calls().await, n);
    }
    assert!(
        !actions_key_exists(&root).await,
        "a model call was counted as an action"
    );

    // One action: no model call.
    let (status, body) = send(&state, Surface::Mcp, &key, "run-apart").await;
    assert!(status.is_success(), "{status} {body}");
    assert_eq!(actions_taken(&state, &root).await, 1);
    assert_eq!(
        model_calls().await,
        2,
        "an action was counted as a model call"
    );

    // The run is at its action ceiling: the next action is refused...
    let (status, body) = send(&state, Surface::Mcp, &key, "run-apart").await;
    assert_eq!(status, StatusCode::TOO_MANY_REQUESTS, "{body}");
    assert!(body.to_string().contains(TERM), "{body}");
    // ...and a model call is not: the model-call ceiling keeps its meaning.
    let (status, body) = send(&state, Surface::LlmChat, &key, "run-apart").await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(model_calls().await, 3);
    assert_eq!(actions_taken(&state, &root).await, 1);
}

// ---------------------------------------------------------------------------
// No ceiling, a malformed ceiling, counters that cannot be operated
// ---------------------------------------------------------------------------

/// Without the term a run's actions are not counted at all: no counter is
/// written, so the default costs no round trip to the shared counters.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_run_without_the_ceiling_counts_nothing(pool: PgPool) {
    let up = upstream(0).await;
    let rg = "rg-act-none";
    let key = world(&pool, &up, Caller::ApiKey, rg).await;
    let state = replica(&pool).await;

    for (run, envelope) in [
        ("run-no-envelope", None),
        (
            "run-other-terms",
            Some(json!({"max_llm_calls_per_run": 50})),
        ),
        ("run-zero", Some(json!({"max_actions_per_run": 0}))),
        ("run-negative", Some(json!({"max_actions_per_run": -1}))),
        ("run-text", Some(json!({"max_actions_per_run": "three"}))),
        ("run-null", Some(json!({"max_actions_per_run": null}))),
    ] {
        if let Some(e) = envelope {
            common::set_rg_operating_envelope(&pool, rg, e).await;
        }
        let root = rc::run_root_of(&state, &key, run);
        for surface in [Surface::Mcp, Surface::A2aInbound, Surface::SkillServerList] {
            for _ in 0..3 {
                let (status, body) = send(&state, surface, &key, run).await;
                assert!(
                    status.is_success(),
                    "{run} × {surface:?}: no ceiling refuses nothing: {status} {body}"
                );
            }
        }
        assert!(
            !actions_key_exists(&root).await,
            "{run}: an uncapped run's actions were counted"
        );
    }
    assert!(run_cap_rows(&pool, 0).await.is_empty());
}

/// A counter the admission cannot operate on refuses the step (fail closed):
/// a ceiling nobody can check is not a ceiling nobody set.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_counter_that_cannot_be_operated_refuses_the_step(pool: PgPool) {
    let up = upstream(0).await;
    let rg = "rg-act-broken";
    let key = world(&pool, &up, Caller::ApiKey, rg).await;
    let state = replica(&pool).await;
    common::set_rg_operating_envelope(&pool, rg, json!({"max_actions_per_run": 100})).await;
    let root = rc::run_root_of(&state, &key, "run-broken");
    let mut c = redis_conn().await;
    let _: () = redis::cmd("LPUSH")
        .arg(actions_key(&root))
        .arg("not a counter")
        .query_async(&mut c)
        .await
        .expect("LPUSH");

    for surface in [Surface::Mcp, Surface::A2aInbound, Surface::Skill] {
        let (status, body) = send(&state, surface, &key, "run-broken").await;
        assert!(
            status.is_client_error(),
            "{surface:?}: a step whose ceiling cannot be checked went through: {status} {body}"
        );
    }
    let rows = run_cap_rows(&pool, 3).await;
    assert_eq!(rows.len(), 3, "{rows:?}");
    assert!(
        rows.iter()
            .all(|(_, m)| m.as_deref().unwrap_or("").contains("cannot be")),
        "{rows:?}"
    );
    assert_eq!(up.received_requests().await.unwrap().len(), 0);
    let _: () = redis::cmd("DEL")
        .arg(actions_key(&root))
        .query_async(&mut c)
        .await
        .expect("DEL");
}

/// A group that declares only the action ceiling costs a step no read of
/// the run's counters: the ceiling is admitted, not read and compared, so
/// the run's clock (started by the first read of its usage) is never
/// started for it.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_action_ceiling_alone_reads_nothing_of_the_run(pool: PgPool) {
    let up = upstream(0).await;
    let rg = "rg-act-no-read";
    let key = world(&pool, &up, Caller::ApiKey, rg).await;
    let state = replica(&pool).await;
    let clock_exists = |root: String| async move {
        let mut c = redis_conn().await;
        let n: i64 = redis::cmd("EXISTS")
            .arg(UsageState::run_counter_key(T, &root, "t0"))
            .query_async(&mut c)
            .await
            .expect("EXISTS");
        n == 1
    };

    common::set_rg_operating_envelope(&pool, rg, json!({"max_actions_per_run": 5})).await;
    let root = rc::run_root_of(&state, &key, "run-no-read");
    let (status, body) = send(&state, Surface::Mcp, &key, "run-no-read").await;
    assert!(status.is_success(), "{status} {body}");
    assert_eq!(actions_taken(&state, &root).await, 1);
    assert!(
        !clock_exists(root).await,
        "the run's usage was read for a ceiling that is admitted"
    );

    // The premise: a term that IS judged by reading the run starts its clock.
    common::set_rg_operating_envelope(
        &pool,
        rg,
        json!({"max_actions_per_run": 5, "max_duration_seconds_per_run": 600}),
    )
    .await;
    let root = rc::run_root_of(&state, &key, "run-read");
    let (status, body) = send(&state, Surface::Mcp, &key, "run-read").await;
    assert!(status.is_success(), "{status} {body}");
    assert!(clock_exists(root).await);
}
