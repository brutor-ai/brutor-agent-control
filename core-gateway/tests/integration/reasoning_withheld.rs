// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Claude's thinking does not carry past output policies.
//!
//! Output checks read the answer (`text` blocks) only. Where a policy can
//! block or redact, the gateway asks a Claude model that supports it for
//! `thinking.display: "omitted"`: the signed blocks still come back, so a tool
//! loop can replay them, and nothing in them is readable. Older models, which
//! cannot omit, keep their thinking; the gateway clears its text in the
//! response, keeping the signature, so the block still replays (Claude checks
//! the signature only). The native
//! `/v1/messages` passthrough forwarded `thinking` blocks and `thinking_delta`
//! events unscanned before this.

use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{body_partial_json, method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

const SECRET: &str = "zzforbidden-in-thinking";
const T: &str = "tenant-think";

/// Seed a Claude model on a native Anthropic provider at the mock and
/// return an API key; `policy` adds a block-capable output check.
async fn seed(pool: &PgPool, mock_uri: &str, model_name: &str, policy: bool) -> String {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, "rg-think", T).await;
    common::seed_end_user(pool, 9301, T).await;
    common::add_direct_membership(pool, 9301, "rg-think", T).await;
    common::seed_llm_provider(pool, "prov-claude", T, "anthropic").await;
    common::seed_llm_model_with_api_url(pool, "m-claude", T, "prov-claude", model_name, mock_uri)
        .await;
    common::bind_llm_model_to_rg(pool, "m-claude", "rg-think").await;
    if policy {
        common::seed_llm_banned_words_guardrail(pool, "gr-think", T, &[SECRET], false, true).await;
        common::bind_guardrail_to_rg(pool, "gr-think", "rg-think").await;
    }
    common::seed_api_key(pool, T, "rg-think", Some(9301), "k-think").await
}

fn native_message(model_name: &str, thinking: &str) -> Value {
    json!({
        "id": "msg_1", "type": "message", "role": "assistant", "model": model_name,
        "content": [
            {"type": "thinking", "thinking": thinking, "signature": "sig-abc"},
            {"type": "text", "text": "a clean answer"}
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5}
    })
}

fn native_stream(thinking: &str) -> String {
    let events = [
        json!({"type": "message_start", "message": {"id": "msg_1", "type": "message",
               "role": "assistant", "content": [], "usage": {"input_tokens": 10, "output_tokens": 0}}}),
        json!({"type": "content_block_start", "index": 0,
               "content_block": {"type": "thinking", "thinking": "", "signature": ""}}),
        json!({"type": "content_block_delta", "index": 0,
               "delta": {"type": "thinking_delta", "thinking": thinking}}),
        json!({"type": "content_block_delta", "index": 0,
               "delta": {"type": "signature_delta", "signature": "sig-abc"}}),
        json!({"type": "content_block_stop", "index": 0}),
        json!({"type": "content_block_start", "index": 1,
               "content_block": {"type": "text", "text": ""}}),
        json!({"type": "content_block_delta", "index": 1,
               "delta": {"type": "text_delta", "text": "a clean answer"}}),
        json!({"type": "content_block_stop", "index": 1}),
        json!({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
               "usage": {"output_tokens": 5}}),
        json!({"type": "message_stop"}),
    ];
    events
        .iter()
        .map(|e| format!("event: {}\ndata: {e}\n\n", e["type"].as_str().unwrap()))
        .collect()
}

async fn call(app: axum::Router, key: &str, path: &str, body: Value) -> (StatusCode, String) {
    let req = common::build_post_request(path, body, Some(key), Some(T));
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

async fn latest_preview(pool: &PgPool) -> String {
    for _ in 0..60 {
        let row: Option<(Option<String>,)> = sqlx::query_as(
            "SELECT response_body_preview FROM proxy_logs \
             WHERE tenant_id = $1 AND proxy_type = 'llm' AND http_status_code = 200 \
             ORDER BY timestamp DESC LIMIT 1",
        )
        .bind(T)
        .fetch_optional(pool)
        .await
        .expect("query proxy_logs");
        if let Some((Some(preview),)) = row {
            return preview;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    panic!("no successful llm proxy_logs row");
}

fn native_request(stream: bool) -> Value {
    json!({
        "model": "m-claude", "max_tokens": 2000, "stream": stream,
        "thinking": {"type": "adaptive", "display": "summarized"},
        "messages": [{"role": "user", "content": "hi"}]
    })
}

/// 4.7+: the upstream is asked for omitted thinking (the mock answers only
/// that body), and the signed block reaches the client unchanged.
#[sqlx::test(migrations = "./tests/migrations")]
async fn native_messages_ask_claude_to_omit_thinking_under_an_output_policy(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/messages"))
        .and(body_partial_json(
            json!({"thinking": {"display": "omitted"}}),
        ))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(native_message("claude-opus-4-7", "")),
        )
        .expect(1)
        .mount(&mock)
        .await;
    let key = seed(&pool, &mock.uri(), "claude-opus-4-7", true).await;
    let app = common::build_anthropic_app(common::build_test_state(pool));
    let (status, raw) = call(app, &key, "/v1/messages", native_request(false)).await;
    assert_eq!(status, StatusCode::OK, "{raw}");
    let body: Value = serde_json::from_str(&raw).unwrap();
    assert_eq!(body["content"][0]["signature"], "sig-abc", "{raw}");
}

/// Without a policy the client's own `display` choice is left alone.
#[sqlx::test(migrations = "./tests/migrations")]
async fn native_messages_keep_the_callers_display_without_a_policy(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/messages"))
        .and(body_partial_json(
            json!({"thinking": {"display": "summarized"}}),
        ))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(native_message("claude-opus-4-7", "why")),
        )
        .expect(1)
        .mount(&mock)
        .await;
    let key = seed(&pool, &mock.uri(), "claude-opus-4-7", false).await;
    let app = common::build_anthropic_app(common::build_test_state(pool));
    let (status, raw) = call(app, &key, "/v1/messages", native_request(false)).await;
    assert_eq!(status, StatusCode::OK, "{raw}");
    assert!(raw.contains("\"why\""), "{raw}");
}

/// A model that cannot omit (4.5) keeps its thinking:
/// the mock answers only a body that still asks for it. The
/// text is cleared in the body and in the log; the signature stays.
#[sqlx::test(migrations = "./tests/migrations")]
async fn native_messages_blank_thinking_on_a_model_that_cannot_omit(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/messages"))
        .and(body_partial_json(
            json!({"thinking": {"type": "adaptive", "display": "summarized"}}),
        ))
        .respond_with(ResponseTemplate::new(200).set_body_json(native_message(
            "claude-sonnet-4-5",
            &format!("about {SECRET}"),
        )))
        .expect(1)
        .mount(&mock)
        .await;
    let key = seed(&pool, &mock.uri(), "claude-sonnet-4-5", true).await;
    let assert_pool = pool.clone();
    let app = common::build_anthropic_app(common::build_test_state(pool));
    let (status, raw) = call(app, &key, "/v1/messages", native_request(false)).await;
    assert_eq!(status, StatusCode::OK, "{raw}");
    assert!(!raw.contains(SECRET), "client got the thinking: {raw}");
    let body: Value = serde_json::from_str(&raw).unwrap();
    assert_eq!(body["content"][0]["thinking"], "");
    assert_eq!(body["content"][0]["signature"], "sig-abc");
    assert_eq!(body["content"][1]["text"], "a clean answer");
    let preview = latest_preview(&assert_pool).await;
    assert!(
        !preview.contains(SECRET),
        "log kept the thinking: {preview}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn streamed_native_messages_drop_readable_thinking(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/messages"))
        .respond_with(
            ResponseTemplate::new(200)
                .insert_header("content-type", "text/event-stream")
                .set_body_string(native_stream(&format!("about {SECRET}"))),
        )
        .expect(1)
        .mount(&mock)
        .await;
    let key = seed(&pool, &mock.uri(), "claude-sonnet-4-5", true).await;
    let app = common::build_anthropic_app(common::build_test_state(pool));
    let (status, sse) = call(app, &key, "/v1/messages", native_request(true)).await;
    assert_eq!(status, StatusCode::OK, "{sse}");
    assert!(!sse.contains(SECRET), "client got the thinking: {sse}");
    assert!(
        sse.contains("sig-abc"),
        "signature must still arrive: {sse}"
    );
    assert!(sse.contains("a clean answer"), "{sse}");
}

/// Chat completions to Anthropic direct: the same request-side rule, and the
/// signed block with no text reaches the client to replay.
#[sqlx::test(migrations = "./tests/migrations")]
async fn chat_completions_ask_claude_to_omit_thinking_under_an_output_policy(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/messages"))
        .and(body_partial_json(
            json!({"thinking": {"display": "omitted"}}),
        ))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(native_message("claude-opus-4-7", "")),
        )
        .expect(1)
        .mount(&mock)
        .await;
    let key = seed(&pool, &mock.uri(), "claude-opus-4-7", true).await;
    let app = common::build_llm_app(common::build_test_state(pool));
    let (status, raw) = call(
        app,
        &key,
        "/v1/proxy/llm/chat/completions",
        json!({
            "model_id": "m-claude", "max_tokens": 2000,
            "thinking": {"type": "adaptive", "display": "summarized"},
            "messages": [{"role": "user", "content": "hi"}]
        }),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{raw}");
    let body: Value = serde_json::from_str(&raw).unwrap();
    let message = &body["choices"][0]["message"];
    assert_eq!(message["content"], "a clean answer");
    assert_eq!(
        message["thinking_blocks"][0]["signature"], "sig-abc",
        "{raw}"
    );
    assert!(message.get("reasoning_withheld").is_none(), "{raw}");
}

/// Chat completions, same rule on a model that cannot omit: thinking stays
/// on upstream, and its text does not reach the client.
#[sqlx::test(migrations = "./tests/migrations")]
async fn chat_completions_blank_thinking_on_a_model_that_cannot_omit(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/messages"))
        .and(body_partial_json(
            json!({"thinking": {"type": "enabled", "budget_tokens": 1024}}),
        ))
        .respond_with(ResponseTemplate::new(200).set_body_json(native_message(
            "claude-sonnet-4-5",
            &format!("about {SECRET}"),
        )))
        .expect(1)
        .mount(&mock)
        .await;
    let key = seed(&pool, &mock.uri(), "claude-sonnet-4-5", true).await;
    let app = common::build_llm_app(common::build_test_state(pool));
    let (status, raw) = call(
        app,
        &key,
        "/v1/proxy/llm/chat/completions",
        json!({
            "model_id": "m-claude", "max_tokens": 2000,
            "thinking": {"type": "enabled", "budget_tokens": 1024},
            "messages": [{"role": "user", "content": "hi"}]
        }),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{raw}");
    assert!(!raw.contains(SECRET), "client got the thinking: {raw}");
    let body: Value = serde_json::from_str(&raw).unwrap();
    assert_eq!(body["choices"][0]["message"]["content"], "a clean answer");
}
