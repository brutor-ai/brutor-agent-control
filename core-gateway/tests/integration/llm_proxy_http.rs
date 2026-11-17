// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! End-to-end tests for the LLM proxy hot path.
//!
//! Wires up:
//!   - A `wiremock` `MockServer` standing in for the upstream OpenAI API
//!   - A seeded `llm_provider` + `llm_model` pointing `api_url` at the
//!     mock, bound to a resource group
//!   - A group-scoped API key authorising the call
//!
//! Then drives `POST /v1/proxy/llm/chat/completions` through the live
//! router with `tower::ServiceExt::oneshot` and asserts on the proxied
//! request + response. Closes the gap that the in-source unit tests
//! and the response-shape conformance suite can't reach without a
//! real upstream.

use axum::body::Body;
use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{header, method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

async fn body_to_json(body: Body) -> Value {
    let bytes = body.collect().await.expect("collect body").to_bytes();
    serde_json::from_slice(&bytes).unwrap_or(Value::Null)
}

fn openai_chat_completion_fixture(content: &str) -> Value {
    json!({
        "id": "chatcmpl-test-fixture",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "test-gpt",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop"
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    })
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn happy_path_chat_completion_round_trips_through_proxy(pool: PgPool) {
    // ── upstream mock ───────────────────────────────────────────
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .and(header("content-type", "application/json"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(openai_chat_completion_fixture("hello from upstream")),
        )
        .expect(1)
        .mount(&mock)
        .await;

    // ── topology ────────────────────────────────────────────────
    common::seed_tenant(&pool, "tenant-1").await;
    common::seed_resource_group(&pool, "rg-llm", "tenant-1").await;
    common::seed_end_user(&pool, 7001, "tenant-1").await;
    common::add_direct_membership(&pool, 7001, "rg-llm", "tenant-1").await;
    common::seed_llm_provider(&pool, "prov-openai", "tenant-1", "openai").await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-test",
        "tenant-1",
        "prov-openai",
        "test-gpt",
        &mock.uri(),
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "m-test", "rg-llm").await;
    let api_key = common::seed_api_key(&pool, "tenant-1", "rg-llm", Some(7001), "test-key").await;

    let state = common::build_test_state(pool);
    let app = common::build_llm_app(state);

    let req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({
            "model_id": "m-test",
            "messages": [{"role": "user", "content": "hi"}]
        }),
        Some(&api_key),
        Some("tenant-1"),
    );

    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let json = body_to_json(response.into_body()).await;

    assert_eq!(
        status,
        StatusCode::OK,
        "happy path must return 200; got {status} body={json}"
    );

    // The mock's `.expect(1)` already asserts the upstream was called
    // exactly once with the right path + content-type. Verify the
    // response was passed back largely unchanged.
    assert_eq!(json["object"], "chat.completion");
    assert_eq!(
        json["choices"][0]["message"]["content"], "hello from upstream",
        "response content must come from the mocked upstream"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn upstream_5xx_returns_proxy_error_envelope(pool: PgPool) {
    let mock = MockServer::start().await;
    // The proxy retries on 5xx (transient-failure handling) before
    // giving up. Accept any positive count — the contract is "client
    // ultimately sees a 5xx", not "upstream is called exactly once".
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(503).set_body_string("upstream is down"))
        .mount(&mock)
        .await;

    common::seed_tenant(&pool, "tenant-1").await;
    common::seed_resource_group(&pool, "rg-llm", "tenant-1").await;
    common::seed_end_user(&pool, 7002, "tenant-1").await;
    common::add_direct_membership(&pool, 7002, "rg-llm", "tenant-1").await;
    common::seed_llm_provider(&pool, "prov-openai", "tenant-1", "openai").await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-test",
        "tenant-1",
        "prov-openai",
        "test-gpt",
        &mock.uri(),
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "m-test", "rg-llm").await;
    let api_key = common::seed_api_key(&pool, "tenant-1", "rg-llm", Some(7002), "test-key").await;

    let state = common::build_test_state(pool);
    let app = common::build_llm_app(state);

    let req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({
            "model_id": "m-test",
            "messages": [{"role": "user", "content": "hi"}]
        }),
        Some(&api_key),
        Some("tenant-1"),
    );

    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let json = body_to_json(response.into_body()).await;

    // The proxy classifies upstream 5xx as a downstream failure and
    // returns a 5xx of its own. We accept the upstream status echoed
    // back (503) or a gateway-translated 502 — the contract is "client
    // sees a 5xx with an error envelope", not the specific code.
    assert!(
        status.is_server_error(),
        "upstream 503 must produce a 5xx to the client; got {status} body={json}"
    );
    assert!(
        json.get("error").is_some() || json.get("message").is_some(),
        "5xx must carry an error envelope; got {json}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn model_not_bound_to_users_group_returns_4xx(pool: PgPool) {
    let mock = MockServer::start().await;
    // Mock has no .expect() — the proxy must NOT reach the upstream
    // because access denial fires before forwarding.
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({})))
        .mount(&mock)
        .await;

    common::seed_tenant(&pool, "tenant-1").await;
    common::seed_resource_group(&pool, "rg-llm", "tenant-1").await;
    common::seed_resource_group(&pool, "rg-other", "tenant-1").await;
    common::seed_end_user(&pool, 7003, "tenant-1").await;
    common::add_direct_membership(&pool, 7003, "rg-llm", "tenant-1").await;
    common::seed_llm_provider(&pool, "prov-openai", "tenant-1", "openai").await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-locked",
        "tenant-1",
        "prov-openai",
        "test-gpt",
        &mock.uri(),
    )
    .await;
    // CRITICAL: bind model to a DIFFERENT group than the user is in.
    common::bind_llm_model_to_rg(&pool, "m-locked", "rg-other").await;
    let api_key = common::seed_api_key(&pool, "tenant-1", "rg-llm", Some(7003), "test-key").await;

    let state = common::build_test_state(pool);
    let app = common::build_llm_app(state);

    let req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({
            "model_id": "m-locked",
            "messages": [{"role": "user", "content": "hi"}]
        }),
        Some(&api_key),
        Some("tenant-1"),
    );

    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    assert!(
        status.is_client_error(),
        "model not bound to caller's group must be a 4xx (not 5xx, not 200); got {status}"
    );
}

/// Regression: `GET /v1/proxy/llm/models` (OpenAI-compatible list) must
/// return the caller's group-accessible models.
///
/// The handler previously ran `SELECT DISTINCT m.*` over `llm_model`,
/// which Postgres rejects at planning time — `llm_model` carries
/// `json`-typed columns (`features`, `settings`, …) and the `json`
/// type has no equality operator (only `jsonb` does). The error was
/// swallowed by `.unwrap_or_default()`, so the endpoint silently
/// returned an empty list even though the user had models AND could
/// chat with them. This test seeds two group-bound models and asserts
/// both appear exactly once. The `tests/migrations` schema declares
/// those columns as `json`, so it reproduces the production failure.
#[sqlx::test(migrations = "./tests/migrations")]
async fn list_models_returns_group_bound_models(pool: PgPool) {
    use axum::http::Request;
    use std::net::SocketAddr;

    common::seed_tenant(&pool, "tenant-1").await;
    common::seed_resource_group(&pool, "rg-eng", "tenant-1").await;
    common::seed_end_user(&pool, 7100, "tenant-1").await;
    common::add_direct_membership(&pool, 7100, "rg-eng", "tenant-1").await;
    common::seed_llm_provider(&pool, "prov-openai", "tenant-1", "openai").await;
    common::seed_llm_model(&pool, "m-gpt5", "tenant-1", "prov-openai", "gpt-5").await;
    common::seed_llm_model(&pool, "m-gpt4", "tenant-1", "prov-openai", "gpt-4").await;
    common::bind_llm_model_to_rg(&pool, "m-gpt5", "rg-eng").await;
    common::bind_llm_model_to_rg(&pool, "m-gpt4", "rg-eng").await;

    let jwt = common::seed_api_key(&pool, "tenant-1", "rg-eng", Some(7100), "caller").await;
    let state = common::build_test_state(pool);
    let app = common::build_llm_app(state);

    let mut req = Request::builder()
        .method("GET")
        .uri("/v1/proxy/llm/models")
        .header("X-Tenant-ID", "tenant-1")
        .header("X-Gateway-Authorization", format!("Bearer {jwt}"))
        .header("authorization", format!("Bearer {jwt}"))
        .body(Body::empty())
        .expect("build request");
    let peer: SocketAddr = "127.0.0.1:55555".parse().expect("peer addr");
    req.extensions_mut()
        .insert(axum::extract::ConnectInfo(peer));

    let response = app.oneshot(req).await.expect("router call");
    assert_eq!(
        response.status(),
        StatusCode::OK,
        "group-member must get 200 from /v1/proxy/llm/models"
    );

    let json = body_to_json(response.into_body()).await;
    // OpenAI-compat shape: each entry's `id` is the model name.
    let ids: Vec<String> = json["data"]
        .as_array()
        .expect("data array in OpenAI-compat models response")
        .iter()
        .map(|m| m["id"].as_str().unwrap_or_default().to_string())
        .collect();

    assert!(
        ids.contains(&"gpt-5".to_string()) && ids.contains(&"gpt-4".to_string()),
        "both group-bound models must be listed; got {ids:?}"
    );
    assert_eq!(
        ids.iter().filter(|id| *id == "gpt-5").count(),
        1,
        "a model bound once must appear exactly once (no duplicate rows); got {ids:?}"
    );
}

/// Regression: `GET /v1/proxy/llm/models` with a GLOBAL API key (no bound
/// user, no bound group) must not 500.
///
/// A global key gets a synthetic subject (`api_key_<id>`) rather than a numeric
/// user id. `resolve_user_group_ids` fed that string straight into a
/// `WHERE user_id = $1::int` cast, so Postgres raised
/// `invalid input syntax for type integer: "api_key_…"`, the handler swallowed
/// it into a 500, and model discovery broke for every global/service key. The
/// fix skips the user-membership query for a non-numeric subject (a global key
/// has no user/end-user memberships). It should resolve no membership groups
/// and return 200 with an (empty) list rather than erroring.
#[sqlx::test(migrations = "./tests/migrations")]
async fn list_models_global_api_key_does_not_500(pool: PgPool) {
    use axum::http::Request;
    use std::net::SocketAddr;

    common::seed_tenant(&pool, "tenant-1").await;
    common::seed_resource_group(&pool, "rg-eng", "tenant-1").await;
    common::seed_llm_provider(&pool, "prov-openai", "tenant-1", "openai").await;
    common::seed_llm_model(&pool, "m-gpt5", "tenant-1", "prov-openai", "gpt-5").await;
    common::bind_llm_model_to_rg(&pool, "m-gpt5", "rg-eng").await;

    // A GLOBAL key with no user and no group → auth derives the synthetic
    // `api_key_<id>` subject that used to crash the int cast. The only
    // key allowed to have no group is the platform service key (any other is
    // refused at auth), so this is the platform key.
    use sha2::{Digest, Sha256};
    let suffix = uuid::Uuid::new_v4().simple().to_string();
    let raw = format!("sk_brutor_api_test_{suffix}");
    let hash = format!("{:x}", Sha256::digest(raw.as_bytes()));
    sqlx::query(
        "INSERT INTO api_keys \
         (id, tenant_id, key_hash, key_prefix, name, scope, user_id, group_id, \
          enabled, revoked, usage_count, is_platform_service) \
         VALUES ($1, $2, $3, $4, $5, 'global', NULL, NULL, true, false, 0, true)",
    )
    .bind(format!("key-{suffix}"))
    .bind("tenant-1")
    .bind(&hash)
    .bind(&raw[..raw.len().min(20)])
    .bind("global-key")
    .execute(&pool)
    .await
    .expect("seed global api key");

    let state = common::build_test_state(pool);
    let app = common::build_llm_app(state);

    let mut req = Request::builder()
        .method("GET")
        .uri("/v1/proxy/llm/models")
        .header("X-Tenant-ID", "tenant-1")
        .header("authorization", format!("Bearer {raw}"))
        .body(Body::empty())
        .expect("build request");
    let peer: SocketAddr = "127.0.0.1:55555".parse().expect("peer addr");
    req.extensions_mut()
        .insert(axum::extract::ConnectInfo(peer));

    let response = app.oneshot(req).await.expect("router call");
    assert_eq!(
        response.status(),
        StatusCode::OK,
        "global API key must get 200 (not 500) from /v1/proxy/llm/models"
    );

    let json = body_to_json(response.into_body()).await;
    assert!(
        json["data"].is_array(),
        "OpenAI-compat models response must carry a data array; got {json:?}"
    );
}

// ============================================================================
// Streaming output governance (end-to-end): a streamed tool call whose
// arguments hit a banned-argument pattern must be BLOCKED before the client
// can act on it — the SSE stream emits a guardrail error, withholds the
// finish chunk + [DONE], and a 403 audit row is written. Closes the gap
// where streamed tool calls bypassed governance entirely.
// ============================================================================

fn sse_data_event(v: &Value) -> String {
    format!("data: {}\n\n", serde_json::to_string(v).unwrap())
}

/// OpenAI-style streaming chunks for a tool call whose arguments contain
/// `DROP TABLE`, followed by the terminal finish chunk and `[DONE]`.
fn banned_tool_call_stream() -> String {
    let tool_delta = json!({
        "id": "chatcmpl-stream",
        "object": "chat.completion.chunk",
        "model": "test-gpt",
        "choices": [{
            "index": 0,
            "delta": {
                "role": "assistant",
                "tool_calls": [{
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "run_sql",
                        // arguments is a STRING containing JSON — banned pattern target.
                        "arguments": "{\"query\":\"DROP TABLE users\"}"
                    }
                }]
            },
            "finish_reason": null
        }]
    });
    let finish = json!({
        "id": "chatcmpl-stream",
        "object": "chat.completion.chunk",
        "model": "test-gpt",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]
    });
    format!(
        "{}{}data: [DONE]\n\n",
        sse_data_event(&tool_delta),
        sse_data_event(&finish)
    )
}

async fn seed_llm_governance(
    pool: &PgPool,
    tenant_id: &str,
    rg_id: &str,
    model_id: &str,
    governance: Value,
) {
    sqlx::query(
        "INSERT INTO resource_group_llm_governance \
         (id, tenant_id, resource_group_id, llm_model_id, governance, created_at, updated_at) \
         VALUES ($1, $2, $3, $4, $5, NOW(), NOW())",
    )
    .bind(format!("rglg-{rg_id}-{model_id}"))
    .bind(tenant_id)
    .bind(rg_id)
    .bind(model_id)
    .bind(governance)
    .execute(pool)
    .await
    .expect("seed llm governance");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn streaming_banned_tool_argument_is_blocked(pool: PgPool) {
    // ── upstream mock: streams a tool call with banned arguments ──
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(
            ResponseTemplate::new(200)
                .insert_header("content-type", "text/event-stream")
                .set_body_string(banned_tool_call_stream()),
        )
        .mount(&mock)
        .await;

    // ── topology ──────────────────────────────────────────────────
    common::seed_tenant(&pool, "tenant-1").await;
    common::seed_resource_group(&pool, "rg-llm", "tenant-1").await;
    common::seed_end_user(&pool, 7010, "tenant-1").await;
    common::add_direct_membership(&pool, 7010, "rg-llm", "tenant-1").await;
    common::seed_llm_provider(&pool, "prov-openai", "tenant-1", "openai").await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-test",
        "tenant-1",
        "prov-openai",
        "test-gpt",
        &mock.uri(),
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "m-test", "rg-llm").await;
    // Governance: ban any tool-call argument containing `DROP TABLE`.
    seed_llm_governance(
        &pool,
        "tenant-1",
        "rg-llm",
        "m-test",
        json!({"tool_use": {"banned_argument_patterns": ["DROP TABLE"]}}),
    )
    .await;
    let api_key = common::seed_api_key(&pool, "tenant-1", "rg-llm", Some(7010), "test-key").await;

    let assert_pool = pool.clone();
    let state = common::build_test_state(pool);
    let app = common::build_llm_app(state);

    let req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({
            "model_id": "m-test",
            "stream": true,
            "messages": [{"role": "user", "content": "run a query"}]
        }),
        Some(&api_key),
        Some("tenant-1"),
    );

    let response = app.oneshot(req).await.expect("router call");
    // SSE starts 200; the block is signalled via an in-stream error event
    // (the HTTP status can't change once the stream has started).
    assert_eq!(response.status(), StatusCode::OK);

    let bytes = response
        .into_body()
        .collect()
        .await
        .expect("collect SSE body")
        .to_bytes();
    let body = String::from_utf8_lossy(&bytes);

    // The client must receive a guardrail error...
    assert!(
        body.contains("guardrail_block"),
        "blocked stream must emit a guardrail error event; got: {body}"
    );
    // ...and must NOT receive the finish chunk or a clean [DONE], so a
    // well-behaved SDK never acts on the banned tool call.
    assert!(
        !body.contains("[DONE]"),
        "blocked stream must NOT send a clean [DONE]; got: {body}"
    );
    assert!(
        !body.contains("\"finish_reason\":\"tool_calls\""),
        "the finish chunk must be withheld on block; got: {body}"
    );

    // The audit row is written by the background writer (async) — poll briefly
    // for the 403 that records the blocked response.
    let mut found_403 = false;
    for _ in 0..40 {
        let row: Option<(i32,)> = sqlx::query_as(
            "SELECT http_status_code FROM proxy_logs \
             WHERE proxy_type = 'llm' AND http_status_code = 403 LIMIT 1",
        )
        .fetch_optional(&assert_pool)
        .await
        .expect("query proxy_logs");
        if row.is_some() {
            found_403 = true;
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    assert!(
        found_403,
        "a 403 audit row must be written for the blocked streamed tool call"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn streaming_allowed_tool_call_passes_through(pool: PgPool) {
    // Same topology + governance, but the tool args do NOT hit the banned
    // pattern — the stream must complete normally (finish chunk + [DONE]).
    let mock = MockServer::start().await;
    let ok_stream = {
        let tool_delta = json!({
            "id": "c", "object": "chat.completion.chunk", "model": "test-gpt",
            "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{
                "index": 0, "id": "call_1", "type": "function",
                "function": {"name": "run_sql", "arguments": "{\"query\":\"SELECT 1\"}"}
            }]}, "finish_reason": null}]
        });
        let finish = json!({
            "id": "c", "object": "chat.completion.chunk", "model": "test-gpt",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]
        });
        format!(
            "{}{}data: [DONE]\n\n",
            sse_data_event(&tool_delta),
            sse_data_event(&finish)
        )
    };
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(
            ResponseTemplate::new(200)
                .insert_header("content-type", "text/event-stream")
                .set_body_string(ok_stream),
        )
        .mount(&mock)
        .await;

    common::seed_tenant(&pool, "tenant-1").await;
    common::seed_resource_group(&pool, "rg-llm", "tenant-1").await;
    common::seed_end_user(&pool, 7011, "tenant-1").await;
    common::add_direct_membership(&pool, 7011, "rg-llm", "tenant-1").await;
    common::seed_llm_provider(&pool, "prov-openai", "tenant-1", "openai").await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-test",
        "tenant-1",
        "prov-openai",
        "test-gpt",
        &mock.uri(),
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "m-test", "rg-llm").await;
    seed_llm_governance(
        &pool,
        "tenant-1",
        "rg-llm",
        "m-test",
        json!({"tool_use": {"banned_argument_patterns": ["DROP TABLE"]}}),
    )
    .await;
    let api_key = common::seed_api_key(&pool, "tenant-1", "rg-llm", Some(7011), "test-key").await;

    let state = common::build_test_state(pool);
    let app = common::build_llm_app(state);
    let req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({"model_id": "m-test", "stream": true, "messages": [{"role": "user", "content": "q"}]}),
        Some(&api_key),
        Some("tenant-1"),
    );

    let response = app.oneshot(req).await.expect("router call");
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = response
        .into_body()
        .collect()
        .await
        .expect("collect")
        .to_bytes();
    let body = String::from_utf8_lossy(&bytes);

    // Allowed: no guardrail error; finish chunk + [DONE] reach the client.
    assert!(
        !body.contains("guardrail_block"),
        "allowed call must not error; got: {body}"
    );
    assert!(
        body.contains("[DONE]"),
        "allowed stream must terminate with [DONE]; got: {body}"
    );
    assert!(
        body.contains("\"finish_reason\":\"tool_calls\""),
        "allowed stream must release the finish chunk; got: {body}"
    );
}

// ===========================================================================
// /responses — the audit row carries usage and cost
//
// 0.10.94 wrote every `/v1/proxy/llm/responses` proxy_logs row with NULL
// `llm_prompt_tokens` / `llm_completion_tokens` / `llm_total_tokens` /
// `llm_estimated_cost_usd` while the same call reached usage_logs with a
// cost and charged the budget counter. The row was written before the
// usage was read. The run ledger, assurance cost signals and Mission
// Control run cost all read proxy_logs, so a run could be refused for
// budget while its ledger said $0. These pin both branches.
// ===========================================================================

fn openai_responses_fixture(text: &str) -> Value {
    json!({
        "id": "resp_test_fixture",
        "object": "response",
        "created_at": 1_700_000_000,
        "model": "test-gpt",
        "status": "completed",
        "output": [{
            "type": "message",
            "id": "msg_1",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}]
        }],
        "usage": {
            "input_tokens": 100,
            "input_tokens_details": {"cached_tokens": 20},
            "output_tokens": 50,
            "output_tokens_details": {"reasoning_tokens": 10},
            "total_tokens": 150
        }
    })
}

/// A Responses-API SSE stream: usage is only on the terminal
/// `response.completed` event; the earlier events carry `usage: null`.
fn openai_responses_sse_stream(text: &str) -> String {
    let created = json!({
        "type": "response.created",
        "response": {"id": "resp_test_fixture", "status": "in_progress", "usage": null}
    });
    let delta = json!({"type": "response.output_text.delta", "delta": text});
    let completed = json!({
        "type": "response.completed",
        "response": openai_responses_fixture(text)
    });
    format!(
        "event: response.created\ndata: {}\n\nevent: response.output_text.delta\ndata: {}\n\nevent: response.completed\ndata: {}\n\n",
        created, delta, completed
    )
}

/// `(prompt, completion, total, cost, model_used, streaming, cache_read)`
type ResponsesLogRow = (
    Option<i32>,
    Option<i32>,
    Option<i32>,
    Option<f64>,
    Option<String>,
    Option<bool>,
    Option<i32>,
);

/// Poll for the `/responses` audit row (the background writer is async)
/// until it lands WITH a token count, then return its usage columns.
async fn wait_for_responses_log_row(pool: &PgPool) -> ResponsesLogRow {
    for _ in 0..60 {
        let row: Option<ResponsesLogRow> = sqlx::query_as(
            "SELECT llm_prompt_tokens, llm_completion_tokens, llm_total_tokens, \
                    llm_estimated_cost_usd, llm_model_used, llm_streaming, \
                    prompt_cache_read_tokens \
             FROM proxy_logs \
             WHERE proxy_type = 'llm' AND proxy_subtype = 'responses' \
               AND http_status_code = 200 \
             ORDER BY timestamp DESC LIMIT 1",
        )
        .fetch_optional(pool)
        .await
        .expect("query proxy_logs");
        if let Some(r) = row
            && r.0.is_some()
        {
            return r;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    panic!("no /responses proxy_logs row with token counts was written");
}

async fn wait_for_llm_usage_log_cost(pool: &PgPool) -> (Option<i32>, Option<i32>, Option<f64>) {
    for _ in 0..60 {
        let row: Option<(Option<i32>, Option<i32>, Option<f64>)> = sqlx::query_as(
            "SELECT prompt_tokens, completion_tokens, estimated_cost FROM usage_logs \
             WHERE resource_type = 'llm' ORDER BY created_at DESC LIMIT 1",
        )
        .fetch_optional(pool)
        .await
        .expect("query usage_logs");
        if let Some(r) = row {
            return r;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    panic!("no llm usage_logs row was written");
}

fn assert_responses_row_matches_usage_log(
    log: &ResponsesLogRow,
    usage: &(Option<i32>, Option<i32>, Option<f64>),
    streaming: bool,
) {
    // Pricing seeded at $2 / $8 per million: 100 × 2e-6 + 50 × 8e-6 = $0.0006.
    let expected_cost = 0.0006_f64;
    assert_eq!(log.0, Some(100), "llm_prompt_tokens");
    assert_eq!(log.1, Some(50), "llm_completion_tokens");
    assert_eq!(log.2, Some(150), "llm_total_tokens");
    let cost = log.3.expect("llm_estimated_cost_usd must be set");
    assert!(
        (cost - expected_cost).abs() < 1e-12,
        "llm_estimated_cost_usd = {cost}, expected {expected_cost}"
    );
    assert_eq!(log.4.as_deref(), Some("test-gpt"), "llm_model_used");
    assert_eq!(log.5, Some(streaming), "llm_streaming");
    assert_eq!(
        log.6,
        Some(20),
        "prompt_cache_read_tokens must come from input_tokens_details.cached_tokens"
    );

    // The two ledgers must agree — the budget counter reads usage_logs,
    // the run ledger reads proxy_logs.
    assert_eq!(usage.0, log.0, "usage_logs.prompt_tokens");
    assert_eq!(usage.1, log.1, "usage_logs.completion_tokens");
    let usage_cost = usage.2.expect("usage_logs.estimated_cost must be set");
    assert!(
        (usage_cost - cost).abs() < 1e-12,
        "usage_logs.estimated_cost = {usage_cost} but proxy_logs cost = {cost}"
    );
}

async fn seed_responses_topology(pool: &PgPool, mock_uri: &str) -> String {
    common::seed_tenant(pool, "tenant-1").await;
    common::seed_resource_group(pool, "rg-llm", "tenant-1").await;
    common::seed_end_user(pool, 7020, "tenant-1").await;
    common::add_direct_membership(pool, 7020, "rg-llm", "tenant-1").await;
    common::seed_llm_provider(pool, "prov-openai", "tenant-1", "openai").await;
    common::seed_llm_model_with_api_url(
        pool,
        "m-test",
        "tenant-1",
        "prov-openai",
        "test-gpt",
        mock_uri,
    )
    .await;
    common::set_llm_model_costs(pool, "m-test", 2.0, 8.0).await;
    common::bind_llm_model_to_rg(pool, "m-test", "rg-llm").await;
    common::seed_api_key(pool, "tenant-1", "rg-llm", Some(7020), "test-key").await
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn responses_audit_row_carries_usage_and_cost(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/responses"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(openai_responses_fixture("hello from upstream")),
        )
        .expect(1)
        .mount(&mock)
        .await;

    let api_key = seed_responses_topology(&pool, &mock.uri()).await;
    let assert_pool = pool.clone();
    let state = common::build_test_state(pool);
    let app = common::build_llm_app(state);

    let req = common::build_post_request(
        "/v1/proxy/llm/responses",
        json!({"model_id": "m-test", "input": "hi"}),
        Some(&api_key),
        Some("tenant-1"),
    );
    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let body = body_to_json(response.into_body()).await;
    assert_eq!(status, StatusCode::OK, "body={body}");
    assert_eq!(
        body["output"][0]["content"][0]["text"],
        "hello from upstream"
    );

    let log = wait_for_responses_log_row(&assert_pool).await;
    let usage = wait_for_llm_usage_log_cost(&assert_pool).await;
    assert_responses_row_matches_usage_log(&log, &usage, false);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn streamed_responses_audit_row_carries_usage_and_cost(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/responses"))
        .respond_with(
            ResponseTemplate::new(200)
                .insert_header("content-type", "text/event-stream")
                .set_body_string(openai_responses_sse_stream("hello from upstream")),
        )
        .expect(1)
        .mount(&mock)
        .await;

    let api_key = seed_responses_topology(&pool, &mock.uri()).await;
    let assert_pool = pool.clone();
    let state = common::build_test_state(pool);
    let app = common::build_llm_app(state);

    let req = common::build_post_request(
        "/v1/proxy/llm/responses",
        json!({"model_id": "m-test", "input": "hi", "stream": true}),
        Some(&api_key),
        Some("tenant-1"),
    );
    let response = app.oneshot(req).await.expect("router call");
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = response
        .into_body()
        .collect()
        .await
        .expect("collect sse body")
        .to_bytes();
    let sse = String::from_utf8_lossy(&bytes);
    assert!(
        sse.contains("response.completed"),
        "terminal event must be forwarded; got: {sse}"
    );

    let log = wait_for_responses_log_row(&assert_pool).await;
    let usage = wait_for_llm_usage_log_cost(&assert_pool).await;
    assert_responses_row_matches_usage_log(&log, &usage, true);
}

// ===========================================================================
// /responses — reasoning does not carry past output policies
//
// Output guardrails scan the answer text only. The Responses API also
// returns the model's reasoning summary (`output[].type == "reasoning"`,
// and in a stream the `response.reasoning_summary_*` / `reasoning_text`
// events), which went to the client and the log unscanned, so a policy
// that blocks or redacts a value let the same value through in the
// reasoning. The chat route withholds it under the same condition.
// ===========================================================================

const REASONING_SECRET: &str = "zzforbidden-in-reasoning";

fn responses_fixture_with_reasoning(text: &str) -> Value {
    let mut body = openai_responses_fixture(text);
    let reasoning = json!({
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": format!("I will mention {REASONING_SECRET}")}],
        "encrypted_content": "opaque-provider-blob"
    });
    body["output"]
        .as_array_mut()
        .expect("output array")
        .insert(0, reasoning);
    body
}

fn responses_sse_stream_with_reasoning(text: &str) -> String {
    let summary = format!("I will mention {REASONING_SECRET}");
    let events = [
        (
            "response.created",
            json!({"type": "response.created",
                   "response": {"id": "resp_test_fixture", "status": "in_progress", "usage": null}}),
        ),
        (
            "response.output_item.added",
            json!({"type": "response.output_item.added", "output_index": 0,
                   "item": {"type": "reasoning", "id": "rs_1", "summary": []}}),
        ),
        (
            "response.reasoning_summary_part.added",
            json!({"type": "response.reasoning_summary_part.added", "item_id": "rs_1",
                   "summary_index": 0, "part": {"type": "summary_text", "text": ""}}),
        ),
        (
            "response.reasoning_summary_text.delta",
            json!({"type": "response.reasoning_summary_text.delta", "item_id": "rs_1",
                   "summary_index": 0, "delta": summary}),
        ),
        (
            "response.reasoning_summary_text.done",
            json!({"type": "response.reasoning_summary_text.done", "item_id": "rs_1",
                   "summary_index": 0, "text": summary}),
        ),
        (
            "response.output_item.done",
            json!({"type": "response.output_item.done", "output_index": 0,
                   "item": {"type": "reasoning", "id": "rs_1",
                            "summary": [{"type": "summary_text", "text": summary}]}}),
        ),
        (
            "response.output_text.delta",
            json!({"type": "response.output_text.delta", "delta": text}),
        ),
        (
            "response.completed",
            json!({"type": "response.completed",
                   "response": responses_fixture_with_reasoning(text)}),
        ),
    ];
    events
        .iter()
        .map(|(name, data)| format!("event: {name}\ndata: {data}\n\n"))
        .collect()
}

async fn mount_responses(mock: &MockServer, template: ResponseTemplate) {
    Mock::given(method("POST"))
        .and(wm_path("/responses"))
        .respond_with(template)
        .expect(1)
        .mount(mock)
        .await;
}

/// The logged response preview of the latest successful `/responses` row.
async fn wait_for_responses_preview(pool: &PgPool) -> String {
    for _ in 0..60 {
        let row: Option<(Option<String>,)> = sqlx::query_as(
            "SELECT response_body_preview FROM proxy_logs \
             WHERE proxy_type = 'llm' AND proxy_subtype = 'responses' \
               AND http_status_code = 200 \
             ORDER BY timestamp DESC LIMIT 1",
        )
        .fetch_optional(pool)
        .await
        .expect("query proxy_logs");
        if let Some((Some(preview),)) = row {
            return preview;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    panic!("no /responses proxy_logs row was written");
}

/// Call `/responses` once; `output_policy` adds a banned-words output check
/// on the reasoning secret (block-capable, so the stream runs sync).
async fn call_responses(
    pool: PgPool,
    mock: &MockServer,
    stream: bool,
    output_policy: bool,
) -> (StatusCode, String) {
    let api_key = seed_responses_topology(&pool, &mock.uri()).await;
    if output_policy {
        common::seed_llm_banned_words_guardrail(
            &pool,
            "gr-out",
            "tenant-1",
            &[REASONING_SECRET],
            false,
            true,
        )
        .await;
        common::bind_guardrail_to_rg(&pool, "gr-out", "rg-llm").await;
    }
    let app = common::build_llm_app(common::build_test_state(pool));
    let mut req_body = json!({"model_id": "m-test", "input": "hi"});
    if stream {
        req_body["stream"] = json!(true);
    }
    let req = common::build_post_request(
        "/v1/proxy/llm/responses",
        req_body,
        Some(&api_key),
        Some("tenant-1"),
    );
    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let bytes = response
        .into_body()
        .collect()
        .await
        .expect("collect body")
        .to_bytes();
    (status, String::from_utf8_lossy(&bytes).into_owned())
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn responses_reasoning_is_withheld_where_an_output_policy_applies(pool: PgPool) {
    let mock = MockServer::start().await;
    mount_responses(
        &mock,
        ResponseTemplate::new(200)
            .set_body_json(responses_fixture_with_reasoning("a clean answer")),
    )
    .await;
    let assert_pool = pool.clone();
    let (status, raw) = call_responses(pool, &mock, false, true).await;
    assert_eq!(status, StatusCode::OK, "body={raw}");
    assert!(
        !raw.contains(REASONING_SECRET),
        "client got the reasoning: {raw}"
    );
    let body: Value = serde_json::from_str(&raw).expect("json body");
    let reasoning = &body["output"][0];
    assert_eq!(reasoning["type"], "reasoning");
    assert_eq!(reasoning["summary"], json!([]));
    // The opaque blob stays: the client cannot read it and needs it to
    // send the item back on the next turn.
    assert_eq!(reasoning["encrypted_content"], "opaque-provider-blob");
    assert_eq!(body["reasoning_withheld"], true);
    assert_eq!(body["output"][1]["content"][0]["text"], "a clean answer");

    let preview = wait_for_responses_preview(&assert_pool).await;
    assert!(
        !preview.contains(REASONING_SECRET),
        "log kept the reasoning: {preview}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn streamed_responses_reasoning_is_withheld_where_an_output_policy_applies(pool: PgPool) {
    let mock = MockServer::start().await;
    mount_responses(
        &mock,
        ResponseTemplate::new(200)
            .insert_header("content-type", "text/event-stream")
            .set_body_string(responses_sse_stream_with_reasoning("a clean answer")),
    )
    .await;
    let assert_pool = pool.clone();
    let (status, sse) = call_responses(pool, &mock, true, true).await;
    assert_eq!(status, StatusCode::OK);
    assert!(
        !sse.contains(REASONING_SECRET),
        "client got the reasoning: {sse}"
    );
    assert!(
        !sse.contains("response.reasoning_summary_text.delta"),
        "reasoning events must not be forwarded: {sse}"
    );
    assert!(
        sse.contains("a clean answer"),
        "answer must still arrive: {sse}"
    );
    assert!(sse.contains("response.completed"), "terminal event: {sse}");
    assert!(sse.contains("\"reasoning_withheld\":true"), "{sse}");

    let preview = wait_for_responses_preview(&assert_pool).await;
    assert!(
        !preview.contains(REASONING_SECRET),
        "log kept the reasoning: {preview}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn responses_reasoning_passes_through_without_an_output_policy(pool: PgPool) {
    let mock = MockServer::start().await;
    mount_responses(
        &mock,
        ResponseTemplate::new(200)
            .set_body_json(responses_fixture_with_reasoning("a clean answer")),
    )
    .await;
    let (status, raw) = call_responses(pool, &mock, false, false).await;
    assert_eq!(status, StatusCode::OK, "body={raw}");
    assert!(
        raw.contains(REASONING_SECRET),
        "nothing to withhold from: {raw}"
    );
    let body: Value = serde_json::from_str(&raw).expect("json body");
    assert!(body.get("reasoning_withheld").is_none());
}
