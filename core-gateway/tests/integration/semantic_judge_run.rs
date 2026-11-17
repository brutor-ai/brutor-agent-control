// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The semantic-policy judge's self-call is a step of the caller's run.
//!
//! The judge rules on a caller's content by calling the gateway's own chat
//! route. That call carried the caller's credential and no run, so its
//! `proxy_logs` row was a run of its own — an orphan, tied to nothing the
//! caller did. It now carries the signed child delegation the platform's
//! embedding self-calls carry (`llm_helpers::self_call_headers`): the row
//! names the caller's run, the caller's step as its parent, one more level
//! of depth, and says what it is (`tags.internal_call`).
//!
//! The judge here is the production one's request (`semantic_policy::
//! judge_self_call`) sent to the application under test instead of to the
//! loopback address.

use std::sync::{Arc, Mutex};

use axum::Router;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::services::semantic_policy::{self, Judge, JudgeVerdict, PolicyContext};

use crate::common;

const T: &str = "tenant-sjr";
const RG: &str = "rg-sjr";
const MODEL: &str = "m-sjr";
const JUDGE_MODEL_NAME: &str = "m-sjr-judge";

/// The production judge's request, answered by the application under test.
#[derive(Default)]
struct RelayJudge {
    app: Mutex<Option<Router>>,
    /// The headers of every judge call made.
    calls: Mutex<Vec<Vec<(String, String)>>>,
}

#[async_trait::async_trait]
impl Judge for RelayJudge {
    async fn evaluate(
        &self,
        model_id: &str,
        constraint: &str,
        content: &str,
        ctx: &PolicyContext,
        auth_headers: &[(String, String)],
    ) -> Result<JudgeVerdict, String> {
        let (body, headers) =
            semantic_policy::judge_self_call(model_id, constraint, content, ctx, auth_headers);
        self.calls.lock().unwrap().push(headers.clone());
        let app = self.app.lock().unwrap().clone().expect("the application");
        let mut req = Request::builder()
            .method("POST")
            .uri(brutor_gateway_core::constants::routes::LLM_CHAT_COMPLETIONS)
            .header("content-type", "application/json");
        for (name, value) in &headers {
            req = req.header(name.as_str(), value.as_str());
        }
        let mut req = req
            .body(Body::from(body.to_string()))
            .map_err(|e| e.to_string())?;
        req.extensions_mut().insert(axum::extract::ConnectInfo(
            "127.0.0.1:55555".parse::<std::net::SocketAddr>().unwrap(),
        ));
        // On a task of its own: the judge's call is another request, not a
        // frame on the caller's stack.
        let resp = tokio::spawn(app.oneshot(req))
            .await
            .map_err(|e| e.to_string())?
            .map_err(|e| e.to_string())?;
        if !resp.status().is_success() {
            return Err(format!("judge model returned {}", resp.status()));
        }
        let bytes = resp
            .into_body()
            .collect()
            .await
            .map_err(|e| e.to_string())?;
        let v: Value = serde_json::from_slice(&bytes.to_bytes()).map_err(|e| e.to_string())?;
        let reply = v["choices"][0]["message"]["content"].as_str().unwrap_or("");
        semantic_policy::parse_judge_reply(reply).ok_or_else(|| "unparseable".to_string())
    }
}

struct World {
    app: Router,
    judge: Arc<RelayJudge>,
    key: String,
    _mock: MockServer,
}

async fn world(pool: &PgPool) -> World {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "chatcmpl-sjr", "object": "chat.completion", "created": 1_700_000_000,
            "model": "test-gpt",
            "choices": [{"index": 0, "message": {"role": "assistant",
                         "content": "{\"violated\": false, \"reason\": \"fine\"}"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
        })))
        .mount(&mock)
        .await;
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_unbound_system_group(pool, "rg-sjr-unbound", T).await;
    common::seed_llm_provider(pool, "prov-sjr", T, "openai").await;
    for (id, name) in [(MODEL, "test-gpt"), (JUDGE_MODEL_NAME, "test-judge")] {
        common::seed_llm_model_with_api_url(pool, id, T, "prov-sjr", name, &mock.uri()).await;
        common::bind_llm_model_to_rg(pool, id, RG).await;
    }
    sqlx::query(
        "INSERT INTO semantic_policies \
         (id, tenant_id, name, constraint_text, action, mode, judge_model_id, enabled, \
          created_at, updated_at) \
         VALUES ('sp-sjr', $1, 'sp-sjr', 'no secrets', 'block', 'enforce', $2, true, now(), now())",
    )
    .bind(T)
    .bind(JUDGE_MODEL_NAME)
    .execute(pool)
    .await
    .expect("policy");
    let key = common::seed_api_key(pool, T, RG, None, "sjr-key").await;

    let judge = Arc::new(RelayJudge::default());
    let mut state = common::new_state(pool.clone());
    state.semantic_policy_judge = judge.clone();
    let gateway = common::compose(state, crate::composition::extensions());
    *judge.app.lock().unwrap() = Some(gateway.app.clone());
    World {
        app: gateway.app,
        judge,
        key,
        _mock: mock,
    }
}

async fn rows(pool: &PgPool, want: usize) -> Vec<Value> {
    let mut rows: Vec<Value> = Vec::new();
    for _ in 0..160 {
        rows = sqlx::query_scalar::<_, sqlx::types::Json<Value>>(
            "SELECT to_jsonb(p.*) FROM proxy_logs p WHERE tenant_id = $1 \
              AND http_status_code = 200 AND llm_model_used IS NOT NULL ORDER BY timestamp",
        )
        .bind(T)
        .fetch_all(pool)
        .await
        .expect("proxy_logs")
        .into_iter()
        .map(|j| j.0)
        .collect();
        if rows.len() >= want {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    rows
}

/// A caller's chat call that a semantic policy judges: two served calls,
/// the caller's and the judge's, in ONE run.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_judges_call_is_a_step_of_the_callers_run(pool: PgPool) {
    let w = world(&pool).await;
    let resp = w
        .app
        .clone()
        .oneshot(common::build_post_request(
            "/v1/proxy/llm/chat/completions",
            json!({"model_id": MODEL, "messages": [{"role": "user", "content": "hello"}]}),
            Some(&w.key),
            Some(T),
        ))
        .await
        .expect("router");
    assert_eq!(resp.status(), StatusCode::OK);
    assert_eq!(
        w.judge.calls.lock().unwrap().len(),
        1,
        "the judge ruled once"
    );

    let rows = rows(&pool, 2).await;
    let of = |model: &str| {
        rows.iter()
            .find(|r| r["llm_model_used"] == model)
            .unwrap_or_else(|| panic!("no served row of {model}: {rows:?}"))
    };
    let (caller, judge) = (of("test-gpt"), of("test-judge"));

    assert!(caller["root_task_id"].is_string(), "{caller}");
    assert_eq!(
        judge["root_task_id"], caller["root_task_id"],
        "the judge's call is a run of its own"
    );
    assert_eq!(
        judge["parent_task_id"], caller["current_task_id"],
        "the judge's call hangs off the caller's step"
    );
    assert_eq!(
        judge["delegation_depth"].as_i64(),
        caller["delegation_depth"].as_i64().map(|d| d + 1)
    );
    assert_ne!(judge["current_task_id"], caller["current_task_id"]);
    assert_eq!(
        judge["tags"],
        json!({"internal_call": "semantic_policy_judge"}),
        "the row says what it is"
    );
    // The caller's own row is not marked.
    assert_eq!(caller["tags"], Value::Null);
}

/// A caller cannot mark its own call as the judge's: the tag is stamped
/// only for a call that carries this process's token.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_caller_cannot_mark_its_call_as_the_judges(pool: PgPool) {
    let w = world(&pool).await;
    let mut req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({"model_id": MODEL, "messages": [{"role": "user", "content": "hello"}]}),
        Some(&w.key),
        Some(T),
    );
    req.headers_mut().insert(
        brutor_gateway_core::constants::headers::X_BRUTOR_SEMANTIC_POLICY,
        "1".parse().unwrap(),
    );
    let resp = w.app.clone().oneshot(req).await.expect("router");
    assert_eq!(resp.status(), StatusCode::OK);
    // The forged header skipped nothing: the judge still ruled.
    assert_eq!(w.judge.calls.lock().unwrap().len(), 1);
    let rows = rows(&pool, 2).await;
    let caller = rows
        .iter()
        .find(|r| r["llm_model_used"] == "test-gpt")
        .expect("the caller's row");
    assert_eq!(caller["tags"], Value::Null, "{caller}");
}

/// The same on the MCP proxy: the judge's call for a tool call hangs off the
/// tool call's run.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_judges_call_for_a_tool_call_is_a_step_of_its_run(pool: PgPool) {
    let w = world(&pool).await;
    let mcp = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text", "text": "done"}]}
        })))
        .mount(&mcp)
        .await;
    common::seed_mcp_server_with_base_url(&pool, "ms-sjr", T, "ms-sjr", &mcp.uri()).await;
    common::seed_server_config(&pool, "sc-sjr", T, "ms-sjr").await;
    common::bind_server_config_to_rg(&pool, "sc-sjr", RG).await;

    let mut req = Request::builder()
        .method("POST")
        .uri("/v1/proxy/mcp/ms-sjr")
        .header("X-Tenant-ID", T)
        .header("X-API-Key", &w.key)
        .header("Content-Type", "application/json")
        .header("Accept", "application/json")
        .body(Body::from(
            json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "lookup", "arguments": {"q": "hello"}}})
            .to_string(),
        ))
        .expect("request");
    req.extensions_mut().insert(axum::extract::ConnectInfo(
        "127.0.0.1:55555".parse::<std::net::SocketAddr>().unwrap(),
    ));
    let resp = w.app.clone().oneshot(req).await.expect("router");
    let status = resp.status();
    let body = resp.into_body().collect().await.expect("body").to_bytes();
    assert_eq!(status, StatusCode::OK, "{}", String::from_utf8_lossy(&body));
    assert_eq!(
        w.judge.calls.lock().unwrap().len(),
        1,
        "the judge ruled once"
    );

    let judge = rows(&pool, 1)
        .await
        .into_iter()
        .find(|r| r["llm_model_used"] == "test-judge")
        .expect("the judge's row");
    let mut tool: Option<Value> = None;
    for _ in 0..160 {
        tool = sqlx::query_scalar::<_, sqlx::types::Json<Value>>(
            "SELECT to_jsonb(p.*) FROM proxy_logs p WHERE tenant_id = $1 \
              AND proxy_type = 'mcp' AND http_status_code = 200 LIMIT 1",
        )
        .bind(T)
        .fetch_optional(&pool)
        .await
        .expect("proxy_logs")
        .map(|j| j.0);
        if tool.is_some() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    let tool = tool.expect("the tool call's row");
    assert!(tool["root_task_id"].is_string(), "{tool}");
    assert_eq!(judge["root_task_id"], tool["root_task_id"], "one run");
    assert_eq!(judge["parent_task_id"], tool["current_task_id"]);
    assert_eq!(
        judge["tags"],
        json!({"internal_call": "semantic_policy_judge"})
    );
}

// ---------------------------------------------------------------------------
// The other surfaces that ask the judge
// ---------------------------------------------------------------------------
//
// A2A v0.3 outbound, A2A v1 inbound and the skills route forwarded the
// caller's credential to the judge and no run, so the judge's chat call was
// recorded as a run of its own. Each now forwards the signed child of its
// own step, as the LLM surfaces and the MCP proxy do.

use brutor_gateway_core::services::a2a_delegation as chain;

/// `POST path` with the world's key; the status and the body.
async fn post(w: &World, path: &str, body: Value) -> (StatusCode, String) {
    let mut req = Request::builder()
        .method("POST")
        .uri(path)
        .header("X-Tenant-ID", T)
        .header("X-API-Key", &w.key)
        .header("Content-Type", "application/json")
        .header("Accept", "application/json")
        .body(Body::from(body.to_string()))
        .expect("request");
    req.extensions_mut().insert(axum::extract::ConnectInfo(
        "127.0.0.1:55555".parse::<std::net::SocketAddr>().unwrap(),
    ));
    let resp = w.app.clone().oneshot(req).await.expect("router");
    let status = resp.status();
    let body = resp.into_body().collect().await.expect("body").to_bytes();
    (status, String::from_utf8_lossy(&body).into_owned())
}

/// The judge was asked once, with a signed chain, and its served chat call
/// is a step of the run of the step that asked it: a row of `surface`
/// (`proxy_type`) in the same run whose step id is the judge row's parent.
async fn the_judge_ruled_inside_the_run_of(pool: &PgPool, w: &World, surface: &str) -> Value {
    let calls = w.judge.calls.lock().unwrap().clone();
    assert_eq!(calls.len(), 1, "the judge ruled once");
    let header = |name: &str| {
        calls[0]
            .iter()
            .find(|(n, _)| n.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.clone())
    };
    let root = header(chain::HEADER_ROOT).expect("the judge's call carries no run");
    let parent = header(chain::HEADER_PARENT).expect("the judge's call names no parent step");
    assert!(header(chain::HEADER_SIG).is_some(), "the chain is signed");

    let judge = rows(pool, 1)
        .await
        .into_iter()
        .find(|r| r["llm_model_used"] == "test-judge")
        .expect("the judge's row");
    assert_eq!(judge["root_task_id"], json!(root), "{judge}");
    assert_eq!(judge["parent_task_id"], json!(parent), "{judge}");
    assert_eq!(
        judge["tags"],
        json!({"internal_call": "semantic_policy_judge"})
    );

    let mut step: Option<Value> = None;
    for _ in 0..160 {
        step = sqlx::query_scalar::<_, sqlx::types::Json<Value>>(
            "SELECT to_jsonb(p.*) FROM proxy_logs p WHERE tenant_id = $1 \
              AND proxy_type = $2 AND current_task_id = $3 LIMIT 1",
        )
        .bind(T)
        .bind(surface)
        .bind(&parent)
        .fetch_optional(pool)
        .await
        .expect("proxy_logs")
        .map(|j| j.0);
        if step.is_some() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    let step = step.unwrap_or_else(|| panic!("no {surface} row of step {parent}"));
    assert_eq!(judge["root_task_id"], step["root_task_id"], "one run");
    step
}

/// An agent card whose capability `echo` is served at `url` (or by the
/// gateway's own no-op handler), bound to the world's group.
async fn seed_card(pool: &PgPool, id: &str, name: &str, url: Option<&str>) {
    common::seed_agent_card(pool, id, T, name).await;
    if let Some(url) = url {
        sqlx::query("UPDATE agent_cards SET card_json = $2 WHERE id = $1")
            .bind(id)
            .bind(json!({"name": name, "url": url, "capabilities": [{"name": "echo"}]}))
            .execute(pool)
            .await
            .expect("card json");
    }
    sqlx::query(
        "INSERT INTO agent_card_capabilities \
         (id, agent_card_id, capability_name, handler_type, enabled) \
         VALUES ($1, $2, 'echo', 'noop', true)",
    )
    .bind(format!("cap-{id}"))
    .bind(id)
    .execute(pool)
    .await
    .expect("capability");
    common::bind_agent_card_to_rg(pool, id, RG).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_judges_call_for_an_a2a_outbound_call_is_a_step_of_its_run(pool: PgPool) {
    let w = world(&pool).await;
    let peer = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"result": "ok"})))
        .mount(&peer)
        .await;
    seed_card(&pool, "card-sjr-out", "sjr-peer", Some(&peer.uri())).await;

    let (status, body) = post(
        &w,
        "/v1/proxy/a2a/outbound",
        json!({"target_card_id": "card-sjr-out", "capability": "echo",
               "message": {"text": "hello peer"}}),
    )
    .await;
    assert!(
        w.judge.calls.lock().unwrap().len() == 1,
        "the outbound call never reached the semantic policy: {status} {body}"
    );
    the_judge_ruled_inside_the_run_of(&pool, &w, "a2a").await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_judges_call_for_an_a2a_v1_message_is_a_step_of_its_run(pool: PgPool) {
    let w = world(&pool).await;
    seed_card(&pool, "card-sjr-in", "sjr-agent", None).await;

    let (status, body) = post(
        &w,
        &format!("/a2a/{T}/sjr-agent/message:send"),
        json!({"message": {"messageId": ulid::Ulid::new().to_string(), "role": "ROLE_USER",
                           "parts": [{"text": "hello agent"}]}}),
    )
    .await;
    assert!(
        w.judge.calls.lock().unwrap().len() == 1,
        "the message never reached the semantic policy: {status} {body}"
    );
    let step = the_judge_ruled_inside_the_run_of(&pool, &w, "a2a").await;
    // A message that names no task starts one, and the task is the run.
    assert_eq!(step["root_task_id"], step["current_task_id"], "{step}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_judges_call_for_a_skill_execution_is_a_step_of_its_run(pool: PgPool) {
    let w = world(&pool).await;
    common::seed_agent_skill(&pool, "sk-sjr", T, "sk-sjr", "# skill", "a skill").await;
    common::bind_skill_to_rg(&pool, "sk-sjr", RG).await;

    let (status, body) = post(
        &w,
        "/v1/proxy/skills/execute",
        json!({"skill_id": "sk-sjr", "input_params": {"q": "hello skill"}}),
    )
    .await;
    assert!(
        w.judge.calls.lock().unwrap().len() == 1,
        "the execution never reached the semantic policy: {status} {body}"
    );
    the_judge_ruled_inside_the_run_of(&pool, &w, "skill").await;
}

// ---------------------------------------------------------------------------
// The fourth site: a skill script run over MCP
// ---------------------------------------------------------------------------
//
// `skills__run_script` on the gateway's own skills server asks the judge
// about the script's arguments (`system_server`, surface `skill_input`). The
// MCP route handed it the caller's credential and no run, so the judge's
// chat call for the canonical skills path was a run of its own while the
// same judgement on the REST route (above) was a step of the caller's.

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_judges_call_for_a_script_run_over_mcp_is_a_step_of_its_run(pool: PgPool) {
    use brutor_gateway_core::constants::system_server::agent_skill;

    let w = world(&pool).await;
    common::seed_mcp_server_with_base_url(
        &pool,
        "ms-sjr-skills",
        T,
        "skills",
        agent_skill::BASE_URL,
    )
    .await;
    common::seed_server_config(&pool, "sc-sjr-skills", T, "ms-sjr-skills").await;
    common::bind_server_config_to_rg(&pool, "sc-sjr-skills", RG).await;
    common::seed_agent_skill(&pool, "sk-sjr-mcp", T, "sjr-report", "# report", "Reports").await;
    common::bind_skill_to_rg(&pool, "sk-sjr-mcp", RG).await;
    sqlx::query(
        "INSERT INTO agent_skill_scripts \
         (id, tenant_id, skill_id, filename, script_content, script_hash, language, execution_mode) \
         VALUES ('scr-sjr', $1, 'sk-sjr-mcp', 'report.py', 'print(1)', 'h', 'PYTHON', \
                 'SANDBOX'::scriptexecutionmode)",
    )
    .bind(T)
    .execute(&pool)
    .await
    .expect("script");

    let (status, body) = post(
        &w,
        "/v1/proxy/mcp/ms-sjr-skills",
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": agent_skill::TOOL_RUN_SCRIPT,
                          "arguments": {"skill_name": "sjr-report", "script": "report.py",
                                        "args": {"q": "hello script"}}}}),
    )
    .await;
    assert!(
        w.judge.calls.lock().unwrap().len() == 1,
        "the script run never reached the semantic policy: {status} {body}"
    );
    // The step that asked is the MCP call (`skills__run_script` is one
    // operation and one audit row).
    let step = the_judge_ruled_inside_the_run_of(&pool, &w, "mcp").await;
    assert_eq!(
        step["mcp_capability_name"],
        json!(agent_skill::TOOL_RUN_SCRIPT),
        "{step}"
    );
}
