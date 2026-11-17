// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The tool calls a model asks for are judged against the
//! run's data scope, on every LLM surface, against real Postgres.
//!
//! An agent can run a tool itself (a local function, a direct REST call)
//! instead of through the MCP proxy. Then the only call the gateway sees is
//! the model asking for it. If nothing compared that with
//! the run's scope, a run bound to customer C0001 could be steered into
//! fetching C0002 and the gateway passed the instruction on. Rules now name
//! `model:<function name>`.

use axum::http::{HeaderValue, StatusCode};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{body_string_contains, method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::services::data_scope;

use crate::common;

const T: &str = "tenant-scope-model";
const RG: &str = "rg-scope-model";
const TOOL: &str = "get_customer";

/// One Claude, OpenAI or Gemini model at the mock, a data scope on the AI
/// System naming `model:get_customer`, and an API key.
async fn topology(
    pool: &PgPool,
    mock_uri: &str,
    provider: &str,
    model: &str,
    mode: &str,
) -> String {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_end_user(pool, 9851, T).await;
    common::add_direct_membership(pool, 9851, RG, T).await;
    common::seed_llm_provider(pool, "prov-sm", T, provider).await;
    common::seed_llm_model_with_api_url(pool, "m-sm", T, "prov-sm", model, mock_uri).await;
    // The Gemini endpoint is refused without a key on the model.
    common::set_model_api_key(pool, "m-sm", "dummy-key").await;
    common::bind_llm_model_to_rg(pool, "m-sm", RG).await;
    data_scope::invalidate_all();
    sqlx::query(
        "INSERT INTO ai_system_data_scopes \
            (id, tenant_id, ai_system_id, mode, on_unbound, keys, rules, unscoped_tools) \
         VALUES ('ds-sm', $1, $2, $3, 'deny', $4, $5, '[]'::jsonb)",
    )
    .bind(T)
    .bind(RG)
    .bind(mode)
    .bind(json!([{"key": "customer", "source": "declared"}]))
    .bind(json!([
        {"tool": format!("model:{TOOL}"), "argument": "customer_id", "key": "customer"},
    ]))
    .execute(pool)
    .await
    .expect("seed data scope");
    common::seed_api_key(pool, T, RG, Some(9851), "sm-key").await
}

/// A request on run `run-sm`, which declares customer C0001.
async fn send(app: axum::Router, key: &str, path: &str, body: Value) -> (StatusCode, String) {
    let mut req = common::build_post_request(path, body, Some(key), Some(T));
    let h = req.headers_mut();
    h.insert("x-brutor-run-id", HeaderValue::from_static("run-sm"));
    h.insert(
        data_scope::SCOPE_HEADER,
        HeaderValue::from_static("customer=C0001"),
    );
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (status, String::from_utf8_lossy(&bytes).into_owned())
}

/// (tool, verdict) rows recorded in this tenant, oldest first.
async fn accesses(pool: &PgPool) -> Vec<(String, String)> {
    sqlx::query_as(
        "SELECT tool, verdict FROM ai_system_record_access WHERE tenant_id = $1 \
          ORDER BY accessed_at, id",
    )
    .bind(T)
    .fetch_all(pool)
    .await
    .unwrap()
}

fn judged(verdicts: &[&str]) -> Vec<(String, String)> {
    verdicts
        .iter()
        .map(|v| (format!("model:{TOOL}"), v.to_string()))
        .collect()
}

/// The newest LLM row's (status, subtype), polled: the row is written in
/// the background.
async fn refusal_row(pool: &PgPool) -> Option<(i32, Option<String>)> {
    for _ in 0..80 {
        let r: Option<(Option<i32>, Option<String>)> = sqlx::query_as(
            "SELECT http_status_code, proxy_subtype FROM proxy_logs \
              WHERE tenant_id = $1 AND http_status_code >= 400 \
              ORDER BY timestamp DESC LIMIT 1",
        )
        .bind(T)
        .fetch_optional(pool)
        .await
        .unwrap();
        if let Some((s, sub)) = r {
            return Some((s.unwrap_or(0), sub));
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    None
}

fn chat_tool_call(customer: &str) -> Value {
    json!({
        "id": "chatcmpl-sm", "object": "chat.completion", "created": 1, "model": "test-gpt",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": null,
            "tool_calls": [{"id": "call_1", "type": "function", "function": {
                "name": TOOL,
                "arguments": format!("{{\"customer_id\":\"{customer}\"}}")
            }}]
        }}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    })
}

/// The upstream asks for the customer the prompt names.
async fn mount_chat(mock: &MockServer) {
    for c in ["C0001", "C0002"] {
        Mock::given(method("POST"))
            .and(wm_path("/chat/completions"))
            .and(body_string_contains(format!("about {c}")))
            .respond_with(ResponseTemplate::new(200).set_body_json(chat_tool_call(c)))
            .mount(mock)
            .await;
    }
}

fn chat(customer: &str) -> Value {
    json!({"model_id": "m-sm", "messages": [{"role": "user", "content": format!("about {customer}")}],
           "tools": [{"type": "function", "function": {"name": TOOL, "parameters": {"type": "object"}}}]})
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_chat_tool_call_for_another_customer_is_refused_in_enforce(pool: PgPool) {
    let mock = MockServer::start().await;
    mount_chat(&mock).await;
    let key = topology(&pool, &mock.uri(), "openai", "test-gpt", "enforce").await;
    let state = common::build_test_state(pool.clone());
    let path = "/v1/proxy/llm/chat/completions";
    let (ok, body) = send(
        common::build_llm_app(state.clone()),
        &key,
        path,
        chat("C0001"),
    )
    .await;
    assert_eq!(ok, StatusCode::OK, "the run's own customer: {body}");
    assert!(body.contains("C0001"), "{body}");
    let (refused, body) = send(common::build_llm_app(state), &key, path, chat("C0002")).await;
    assert_eq!(refused, StatusCode::FORBIDDEN, "{body}");
    assert!(
        body.contains("out_of_scope"),
        "the refusal says why: {body}"
    );
    assert!(
        !body.contains("C0002"),
        "the instruction never reaches the agent: {body}"
    );
    assert_eq!(accesses(&pool).await, judged(&["in_scope", "out_of_scope"]));
    assert_eq!(
        refusal_row(&pool).await,
        Some((403, Some("access_denied".to_string())))
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn audit_mode_records_a_model_tool_call_and_passes_it(pool: PgPool) {
    let mock = MockServer::start().await;
    mount_chat(&mock).await;
    let key = topology(&pool, &mock.uri(), "openai", "test-gpt", "audit").await;
    let app = common::build_llm_app(common::build_test_state(pool.clone()));
    let (status, body) = send(app, &key, "/v1/proxy/llm/chat/completions", chat("C0002")).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(accesses(&pool).await, judged(&["out_of_scope"]));
}

/// A streamed answer: the finish chunk is held until the check, and a
/// refusal replaces it with an error event, so the client never sees the
/// turn end with a tool call to run.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_streamed_tool_call_for_another_customer_never_finishes(pool: PgPool) {
    let mock = MockServer::start().await;
    let chunk = |delta: Value, finish: Value| {
        format!(
            "data: {}\n\n",
            json!({"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "test-gpt",
                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})
        )
    };
    let sse = [
        chunk(
            json!({"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                    "function": {"name": TOOL, "arguments": "{\"customer_id\":"}}]}),
            Value::Null,
        ),
        chunk(
            json!({"tool_calls": [{"index": 0, "function": {"arguments": "\"C0002\"}"}}]}),
            Value::Null,
        ),
        chunk(json!({}), json!("tool_calls")),
        "data: [DONE]\n\n".to_string(),
    ]
    .concat();
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(
            ResponseTemplate::new(200)
                .insert_header("content-type", "text/event-stream")
                .set_body_string(sse),
        )
        .mount(&mock)
        .await;
    let key = topology(&pool, &mock.uri(), "openai", "test-gpt", "enforce").await;
    let app = common::build_llm_app(common::build_test_state(pool.clone()));
    let mut body = chat("C0002");
    body["stream"] = json!(true);
    let (status, stream) = send(app, &key, "/v1/proxy/llm/chat/completions", body).await;
    assert_eq!(status, StatusCode::OK, "{stream}");
    assert!(
        !stream.contains("\"finish_reason\":\"tool_calls\""),
        "the turn must not end with the call to run: {stream}"
    );
    assert!(stream.contains("event: error"), "{stream}");
    assert_eq!(accesses(&pool).await, judged(&["out_of_scope"]));
}

/// Responses API: `output[].type == "function_call"`.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_responses_api_function_call_is_judged(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/responses"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "resp_sm", "object": "response", "created_at": 1, "model": "test-gpt",
            "status": "completed",
            "output": [{"type": "function_call", "id": "fc_1", "call_id": "call_1",
                        "name": TOOL, "arguments": "{\"customer_id\":\"C0002\"}",
                        "status": "completed"}],
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        })))
        .mount(&mock)
        .await;
    let key = topology(&pool, &mock.uri(), "openai", "test-gpt", "enforce").await;
    let app = common::build_llm_app(common::build_test_state(pool.clone()));
    let (status, body) = send(
        app,
        &key,
        "/v1/proxy/llm/responses",
        json!({"model_id": "m-sm", "input": "about C0002"}),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert_eq!(accesses(&pool).await, judged(&["out_of_scope"]));
}

/// Native `/v1/messages`: a `tool_use` block. The run declared its scope on
/// this request alone, so the verdict is `out_of_scope` only if the
/// declaration was stored here too (otherwise it is `unbound`).
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_native_claude_tool_use_is_judged_against_a_scope_declared_there(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/messages"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "msg_sm", "type": "message", "role": "assistant", "model": "claude-sonnet-4-5",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": TOOL,
                         "input": {"customer_id": "C0002"}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5}
        })))
        .mount(&mock)
        .await;
    let key = topology(
        &pool,
        &mock.uri(),
        "anthropic",
        "claude-sonnet-4-5",
        "enforce",
    )
    .await;
    let app = common::build_anthropic_app(common::build_test_state(pool.clone()));
    let (status, body) = send(
        app,
        &key,
        "/v1/messages",
        json!({"model": "m-sm", "max_tokens": 200,
               "messages": [{"role": "user", "content": "about C0002"}]}),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert_eq!(accesses(&pool).await, judged(&["out_of_scope"]));
}

/// Gemini `functionCall`, with the scope declared on the Gemini request.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_gemini_function_call_is_judged_against_a_scope_declared_there(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/v1beta/models/test-gem:generateContent"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "candidates": [{"content": {"role": "model", "parts": [
                {"functionCall": {"name": TOOL, "args": {"customer_id": "C0002"}}}
            ]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15}
        })))
        .mount(&mock)
        .await;
    let key = topology(&pool, &mock.uri(), "gemini", "test-gem", "enforce").await;
    let app = common::build_gemini_app(common::build_test_state(pool.clone()));
    let (status, body) = send(
        app,
        &key,
        "/v1beta/models/m-sm:generateContent",
        json!({"contents": [{"role": "user", "parts": [{"text": "about C0002"}]}]}),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert_eq!(accesses(&pool).await, judged(&["out_of_scope"]));
}

/// One SSE frame.
fn frame(event: Option<&str>, data: Value) -> String {
    match event {
        Some(e) => format!("event: {e}\ndata: {data}\n\n"),
        None => format!("data: {data}\n\n"),
    }
}

/// Mount `sse(customer)` at `path` for prompts about each customer.
async fn mount_streams(mock: &MockServer, path: &str, sse: impl Fn(&str) -> String) {
    for c in ["C0001", "C0002"] {
        Mock::given(method("POST"))
            .and(wm_path(path))
            .and(body_string_contains(format!("about {c}")))
            .respond_with(
                ResponseTemplate::new(200)
                    .insert_header("content-type", "text/event-stream")
                    .set_body_string(sse(c)),
            )
            .mount(mock)
            .await;
    }
}

fn claude_stream(customer: &str) -> String {
    let args = format!("{{\"customer_id\":\"{customer}\"}}");
    [
        frame(
            Some("message_start"),
            json!({"type": "message_start", "message": {
            "id": "msg_s", "type": "message", "role": "assistant", "model": "claude-sonnet-4-5",
            "content": [], "usage": {"input_tokens": 10, "output_tokens": 1}}}),
        ),
        frame(
            Some("content_block_start"),
            json!({"type": "content_block_start", "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": TOOL, "input": {}}}),
        ),
        frame(
            Some("content_block_delta"),
            json!({"type": "content_block_delta", "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": args}}),
        ),
        frame(
            Some("content_block_stop"),
            json!({"type": "content_block_stop", "index": 0}),
        ),
        frame(
            Some("message_delta"),
            json!({"type": "message_delta",
            "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}}),
        ),
        frame(Some("message_stop"), json!({"type": "message_stop"})),
    ]
    .concat()
}

/// Native `/v1/messages` stream: the turn's end (`message_delta` with
/// `stop_reason: tool_use`, `message_stop`) is held until the call is judged.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_streamed_claude_tool_use_for_another_customer_never_ends_the_turn(pool: PgPool) {
    let mock = MockServer::start().await;
    mount_streams(&mock, "/messages", claude_stream).await;
    let key = topology(
        &pool,
        &mock.uri(),
        "anthropic",
        "claude-sonnet-4-5",
        "enforce",
    )
    .await;
    let state = common::build_test_state(pool.clone());
    let req = |c: &str| {
        json!({"model": "m-sm", "max_tokens": 200, "stream": true,
               "messages": [{"role": "user", "content": format!("about {c}")}]})
    };
    let app = || common::build_anthropic_app(state.clone());
    let (_, ok) = send(app(), &key, "/v1/messages", req("C0001")).await;
    assert!(ok.contains("message_stop"), "an in-scope turn ends: {ok}");
    let (_, refused) = send(app(), &key, "/v1/messages", req("C0002")).await;
    assert!(!refused.contains("message_stop"), "{refused}");
    assert!(
        !refused.contains("\"stop_reason\":\"tool_use\""),
        "no stop_reason tool_use: {refused}"
    );
    assert!(refused.contains("event: error"), "{refused}");
    assert_eq!(accesses(&pool).await, judged(&["in_scope", "out_of_scope"]));
}

/// Gemini stream: from the chunk carrying the `functionCall`, the turn is
/// held, so the call itself never reaches the client when refused.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_streamed_gemini_function_call_for_another_customer_is_never_sent(pool: PgPool) {
    let mock = MockServer::start().await;
    mount_streams(&mock, "/v1beta/models/test-gem:streamGenerateContent", |c| {
        [
            frame(None, json!({"candidates": [{"content": {"role": "model",
                "parts": [{"text": "Looking that up."}]}, "index": 0}]})),
            frame(None, json!({"candidates": [{"content": {"role": "model", "parts": [
                {"functionCall": {"name": TOOL, "args": {"customer_id": c}}}]},
                "finishReason": "STOP", "index": 0}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15}})),
        ]
        .concat()
    })
    .await;
    let key = topology(&pool, &mock.uri(), "gemini", "test-gem", "enforce").await;
    let state = common::build_test_state(pool.clone());
    let req = |c: &str| json!({"contents": [{"role": "user", "parts": [{"text": format!("about {c}")}]}]});
    let path = "/v1beta/models/m-sm:streamGenerateContent?alt=sse";
    let app = || common::build_gemini_app(state.clone());
    let (_, ok) = send(app(), &key, path, req("C0001")).await;
    assert!(
        ok.contains("functionCall"),
        "an in-scope call is released: {ok}"
    );
    let (_, refused) = send(app(), &key, path, req("C0002")).await;
    assert!(!refused.contains("functionCall"), "{refused}");
    assert!(
        refused.contains("Looking that up."),
        "text before the call still flows: {refused}"
    );
    assert_eq!(accesses(&pool).await, judged(&["in_scope", "out_of_scope"]));
}

/// Responses API stream: from the first function-call event, the turn is
/// held until `response.completed` is judged.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_streamed_responses_function_call_for_another_customer_is_never_sent(pool: PgPool) {
    let mock = MockServer::start().await;
    mount_streams(&mock, "/responses", |c| {
        let args = format!("{{\"customer_id\":\"{c}\"}}");
        let item = json!({"type": "function_call", "id": "fc_1", "call_id": "call_1",
                          "name": TOOL, "arguments": args, "status": "completed"});
        [
            frame(
                Some("response.created"),
                json!({"type": "response.created",
                "response": {"id": "r", "status": "in_progress", "output": []}}),
            ),
            frame(
                Some("response.output_item.added"),
                json!({"type": "response.output_item.added",
                "output_index": 0, "item": {"type": "function_call", "id": "fc_1",
                "call_id": "call_1", "name": TOOL, "arguments": ""}}),
            ),
            frame(
                Some("response.function_call_arguments.delta"),
                json!({
                "type": "response.function_call_arguments.delta", "item_id": "fc_1",
                "output_index": 0, "delta": args}),
            ),
            frame(
                Some("response.output_item.done"),
                json!({"type": "response.output_item.done",
                "output_index": 0, "item": item}),
            ),
            frame(
                Some("response.completed"),
                json!({"type": "response.completed",
                "response": {"id": "r", "status": "completed", "output": [item],
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}}),
            ),
        ]
        .concat()
    })
    .await;
    let key = topology(&pool, &mock.uri(), "openai", "test-gpt", "enforce").await;
    let state = common::build_test_state(pool.clone());
    let req = |c: &str| json!({"model_id": "m-sm", "stream": true, "input": format!("about {c}")});
    let app = || common::build_llm_app(state.clone());
    let (_, ok) = send(app(), &key, "/v1/proxy/llm/responses", req("C0001")).await;
    assert!(
        ok.contains("response.completed"),
        "an in-scope turn ends: {ok}"
    );
    let (_, refused) = send(app(), &key, "/v1/proxy/llm/responses", req("C0002")).await;
    assert!(!refused.contains("response.completed"), "{refused}");
    assert!(
        !refused.contains("C0002"),
        "the call never reaches the client: {refused}"
    );
    assert!(refused.contains("event: error"), "{refused}");
    assert_eq!(accesses(&pool).await, judged(&["in_scope", "out_of_scope"]));
}
