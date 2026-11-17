// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Every LLM `proxy_logs` row names the resource group the call ran in.
//!
//! The case: Anthropic-native
//! `/v1/messages` calls whose `usage_logs` rows were billed to the caller's
//! group while their `proxy_logs` rows named none: the native adapter and the
//! `/v1/messages` handler built their rows without the target group, and so
//! did the chat-completions upstream-failure rows. A row with no group drops
//! out of every per-group view (Mission Control, the AI System's activity,
//! the group's compliance tags) while its cost still lands on the group.
//!
//! Each test drives a real route and asserts every row the tenant got names
//! the group (and that usage, where written, agrees). The source guard
//! `tests/llm_log_group_guard.rs` covers the writers these do not drive.

use std::time::Duration;

use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

/// Tenant, group, user, a model of `provider` at `upstream`, and an API key.
async fn topology(pool: &PgPool, t: &str, provider: &str, upstream: &str) -> String {
    let rg = format!("rg-{t}");
    common::seed_tenant(pool, t).await;
    common::seed_resource_group(pool, &rg, t).await;
    common::seed_end_user(pool, 9300, t).await;
    common::add_direct_membership(pool, 9300, &rg, t).await;
    let prov = format!("prov-{t}");
    let model = format!("m-{t}");
    common::seed_llm_provider(pool, &prov, t, provider).await;
    common::seed_llm_model_with_api_url(pool, &model, t, &prov, "test-model", upstream).await;
    common::set_model_api_key(pool, &model, "sk-test").await;
    common::bind_llm_model_to_rg(pool, &model, &rg).await;
    common::seed_api_key(pool, t, &rg, Some(9300), &format!("key-{t}")).await
}

async fn send(app: axum::Router, path: &str, key: &str, t: &str, body: Value) -> StatusCode {
    let req = common::build_post_request(path, body, Some(key), Some(t));
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    // Drain the body: a streamed response writes its row when the stream ends.
    let _ = resp.into_body().collect().await;
    status
}

/// Wait for the tenant's rows, then assert every one names `rg-{t}`.
async fn assert_rows_name_the_group(pool: &PgPool, t: &str) {
    let rg = format!("rg-{t}");
    for _ in 0..100 {
        if common::count_proxy_logs(pool, t).await >= 1 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    // Let any further row flush through the batched writer.
    tokio::time::sleep(Duration::from_millis(400)).await;
    let rows: Vec<(String, u16, Option<String>)> = sqlx::query_as::<_, (String, i32, Option<String>)>(
        "SELECT endpoint, http_status_code, resource_group_id FROM proxy_logs WHERE tenant_id = $1",
    )
    .bind(t)
    .fetch_all(pool)
    .await
    .expect("proxy_logs")
    .into_iter()
    .map(|(e, s, g)| (e, s as u16, g))
    .collect();
    assert!(!rows.is_empty(), "{t}: the call wrote no proxy_logs row");
    for (endpoint, status, group) in &rows {
        assert_eq!(
            group.as_deref(),
            Some(rg.as_str()),
            "{t}: the {endpoint} row (status {status}) must name the group the call ran in"
        );
    }
    let usage: Vec<(Option<String>,)> =
        sqlx::query_as("SELECT group_id FROM usage_logs WHERE tenant_id = $1")
            .bind(t)
            .fetch_all(pool)
            .await
            .expect("usage_logs");
    for (group,) in &usage {
        assert_eq!(
            group.as_deref(),
            Some(rg.as_str()),
            "{t}: usage and log agree"
        );
    }
}

fn anthropic_message() -> Value {
    json!({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "test-model",
        "content": [{"type": "text", "text": "hello"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 3}
    })
}

fn anthropic_sse() -> String {
    let ev = |name: &str, v: Value| format!("event: {name}\ndata: {v}\n\n");
    [
        ev(
            "message_start",
            json!({"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "test-model",
            "content": [], "stop_reason": null,
            "usage": {"input_tokens": 5, "output_tokens": 0}}}),
        ),
        ev(
            "content_block_start",
            json!({"type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""}}),
        ),
        ev(
            "content_block_delta",
            json!({"type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "hello"}}),
        ),
        ev(
            "content_block_stop",
            json!({"type": "content_block_stop", "index": 0}),
        ),
        ev(
            "message_delta",
            json!({"type": "message_delta",
            "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}}),
        ),
        ev("message_stop", json!({"type": "message_stop"})),
    ]
    .concat()
}

async fn upstream(path: &str, resp: ResponseTemplate) -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path(path))
        .respond_with(resp)
        .mount(&mock)
        .await;
    mock
}

fn messages_body(t: &str, stream: bool) -> Value {
    json!({"model": format!("m-{t}"), "max_tokens": 50, "stream": stream,
           "messages": [{"role": "user", "content": "hi"}]})
}

fn chat_body(t: &str, stream: bool) -> Value {
    json!({"model_id": format!("m-{t}"), "stream": stream,
           "messages": [{"role": "user", "content": "hi"}]})
}

// ── /v1/messages on a native Anthropic provider (the E2E finding) ────────

#[sqlx::test(migrations = "./tests/migrations")]
async fn native_messages_row_names_the_group(pool: PgPool) {
    let t = "t-grp-native";
    let mock = upstream(
        "/messages",
        ResponseTemplate::new(200).set_body_json(anthropic_message()),
    )
    .await;
    let key = topology(&pool, t, "anthropic", &mock.uri()).await;
    let app = common::build_anthropic_app(common::build_test_state(pool.clone()));
    let status = send(app, "/v1/messages", &key, t, messages_body(t, false)).await;
    assert_eq!(status, StatusCode::OK);
    assert_rows_name_the_group(&pool, t).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn native_messages_stream_row_names_the_group(pool: PgPool) {
    let t = "t-grp-native-s";
    let mock = upstream(
        "/messages",
        ResponseTemplate::new(200)
            .insert_header("content-type", "text/event-stream")
            .set_body_string(anthropic_sse()),
    )
    .await;
    let key = topology(&pool, t, "anthropic", &mock.uri()).await;
    let app = common::build_anthropic_app(common::build_test_state(pool.clone()));
    let status = send(app, "/v1/messages", &key, t, messages_body(t, true)).await;
    assert_eq!(status, StatusCode::OK);
    assert_rows_name_the_group(&pool, t).await;
}

// ── /v1/messages bridged to an OpenAI-shaped provider ───────────────────

#[sqlx::test(migrations = "./tests/migrations")]
async fn bridged_messages_row_names_the_group(pool: PgPool) {
    let t = "t-grp-bridge";
    let mock = upstream(
        "/chat/completions",
        ResponseTemplate::new(200).set_body_json(json!({
            "id": "c", "object": "chat.completion", "created": 1, "model": "test-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 3, "total_tokens": 6}
        })),
    )
    .await;
    let key = topology(&pool, t, "openai", &mock.uri()).await;
    let app = common::build_anthropic_app(common::build_test_state(pool.clone()));
    let status = send(app, "/v1/messages", &key, t, messages_body(t, false)).await;
    assert_eq!(status, StatusCode::OK);
    assert_rows_name_the_group(&pool, t).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn bridged_messages_upstream_failure_row_names_the_group(pool: PgPool) {
    let t = "t-grp-bridge-f";
    let mock = upstream(
        "/chat/completions",
        ResponseTemplate::new(500).set_body_json(json!({"error": {"message": "boom"}})),
    )
    .await;
    let key = topology(&pool, t, "openai", &mock.uri()).await;
    let app = common::build_anthropic_app(common::build_test_state(pool.clone()));
    let status = send(app, "/v1/messages", &key, t, messages_body(t, false)).await;
    assert!(!status.is_success(), "{status}");
    assert_rows_name_the_group(&pool, t).await;
}

// ── chat completions: the upstream-failure rows ─────────────────────────

#[sqlx::test(migrations = "./tests/migrations")]
async fn chat_upstream_failure_row_names_the_group(pool: PgPool) {
    let t = "t-grp-chat-f";
    let mock = upstream(
        "/chat/completions",
        ResponseTemplate::new(500).set_body_json(json!({"error": {"message": "boom"}})),
    )
    .await;
    let key = topology(&pool, t, "openai", &mock.uri()).await;
    let app = common::build_llm_app(common::build_test_state(pool.clone()));
    let status = send(
        app,
        "/v1/proxy/llm/chat/completions",
        &key,
        t,
        chat_body(t, false),
    )
    .await;
    assert!(!status.is_success(), "{status}");
    assert_rows_name_the_group(&pool, t).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn chat_stream_upstream_failure_row_names_the_group(pool: PgPool) {
    let t = "t-grp-chat-sf";
    let mock = upstream(
        "/chat/completions",
        ResponseTemplate::new(500).set_body_json(json!({"error": {"message": "boom"}})),
    )
    .await;
    let key = topology(&pool, t, "openai", &mock.uri()).await;
    let app = common::build_llm_app(common::build_test_state(pool.clone()));
    let status = send(
        app,
        "/v1/proxy/llm/chat/completions",
        &key,
        t,
        chat_body(t, true),
    )
    .await;
    assert!(!status.is_success(), "{status}");
    assert_rows_name_the_group(&pool, t).await;
}
