// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Anthropic-native `/v1/messages` guardrail wiring.
//!
//! Two regressions:
//!  * INPUT: the handler passed surface `"llm"` (no `surface_enabled` arm) to
//!    `apply_guardrails_with_redaction`, so input guardrails were silently
//!    skipped on this path (the OpenAI chat path uses `chat_input`).
//!  * OUTPUT: the non-streaming branch ran NO output guardrails at all (only
//!    the streaming branch used StreamGuard) — model output was returned with
//!    no PII/banned/secrets/toxic enforcement.

use axum::body::Body;
use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

pub(crate) async fn body_to_string(body: Body) -> String {
    let bytes = body.collect().await.expect("collect body").to_bytes();
    String::from_utf8_lossy(&bytes).to_string()
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn messages_input_guardrail_blocks_banned_words(pool: PgPool) {
    let mock = MockServer::start().await;
    // The upstream MUST NOT be called — the input block short-circuits before
    // the provider is contacted. `.expect(0)` is verified on drop.
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "chatcmpl-should-not-be-used",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "x"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        })))
        .expect(0)
        .mount(&mock)
        .await;

    common::seed_tenant(&pool, "tenant-anth").await;
    common::seed_resource_group(&pool, "rg-anth", "tenant-anth").await;
    common::seed_end_user(&pool, 9001, "tenant-anth").await;
    common::add_direct_membership(&pool, 9001, "rg-anth", "tenant-anth").await;
    common::seed_llm_provider(&pool, "prov-anth", "tenant-anth", "openai").await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-anth",
        "tenant-anth",
        "prov-anth",
        "test-gpt",
        &mock.uri(),
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "m-anth", "rg-anth").await;

    common::seed_llm_banned_words_guardrail(
        &pool,
        "gr-anth",
        "tenant-anth",
        &["forbidden-phrase"],
        true,  // chat_input
        false, // chat_output
    )
    .await;
    common::bind_guardrail_to_rg(&pool, "gr-anth", "rg-anth").await;

    let api_key = common::seed_api_key(&pool, "tenant-anth", "rg-anth", Some(9001), "k-anth").await;

    let state = common::build_test_state(pool.clone());
    let app = common::build_anthropic_app(state);

    // Anthropic-native body shape (model addressed by id; resolve_model
    // matches id = $1).
    let req = common::build_post_request(
        "/v1/messages",
        json!({
            "model": "m-anth",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "please do forbidden-phrase now"}]
        }),
        Some(&api_key),
        Some("tenant-anth"),
    );

    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let body_str = body_to_string(response.into_body()).await;

    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "Anthropic-native input guardrail must block the banned phrase (403); got {status}, body={body_str}"
    );
    let body: Value = serde_json::from_str(&body_str).unwrap_or(Value::Null);
    assert_eq!(
        body.pointer("/error/code").and_then(|v| v.as_str()),
        Some("guardrail_blocked"),
        "expected guardrail_blocked error envelope, got {body_str}"
    );

    // `.expect(0)` on the mock is asserted here: the upstream was never called.
    drop(mock);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn messages_output_guardrail_blocks_banned_words(pool: PgPool) {
    // Upstream (OpenAI-shaped, since the provider is openai) returns the banned
    // phrase; the non-streaming output guardrail must block it BEFORE the
    // Anthropic-shaped body reaches the client.
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "chatcmpl-out",
            "object": "chat.completion",
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": "sure: forbidden-phrase here"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}
        })))
        .expect(1)
        .mount(&mock)
        .await;

    common::seed_tenant(&pool, "tenant-anth").await;
    common::seed_resource_group(&pool, "rg-anth", "tenant-anth").await;
    common::seed_end_user(&pool, 9002, "tenant-anth").await;
    common::add_direct_membership(&pool, 9002, "rg-anth", "tenant-anth").await;
    common::seed_llm_provider(&pool, "prov-anth", "tenant-anth", "openai").await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-anth",
        "tenant-anth",
        "prov-anth",
        "test-gpt",
        &mock.uri(),
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "m-anth", "rg-anth").await;

    common::seed_llm_banned_words_guardrail(
        &pool,
        "gr-anth-out",
        "tenant-anth",
        &["forbidden-phrase"],
        false, // chat_input
        true,  // chat_output
    )
    .await;
    common::bind_guardrail_to_rg(&pool, "gr-anth-out", "rg-anth").await;

    let api_key = common::seed_api_key(&pool, "tenant-anth", "rg-anth", Some(9002), "k-anth").await;

    let state = common::build_test_state(pool.clone());
    let app = common::build_anthropic_app(state);

    let req = common::build_post_request(
        "/v1/messages",
        json!({
            "model": "m-anth",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "anything clean"}]
        }),
        Some(&api_key),
        Some("tenant-anth"),
    );

    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let body_str = body_to_string(response.into_body()).await;

    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "Anthropic-native output guardrail must block the banned phrase (403); got {status}, body={body_str}"
    );
    assert!(
        !body_str.contains("forbidden-phrase here"),
        "banned phrase leaked into the Anthropic-native response: {body_str}"
    );

    common::assert_single_audit_row(&pool, "tenant-anth").await;

    drop(mock);
}

/// The native Anthropic surface returns the run root like `/v1/proxy/llm`
/// does (`x-brutor-run-id`), so an Anthropic-SDK agent can close its run with
/// `POST /v1/runs/{root}/end` or attribute feedback — without it these
/// agents have no way to learn their root.
#[sqlx::test(migrations = "./tests/migrations")]
async fn messages_response_carries_the_run_root(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "chatcmpl-run",
            "object": "chat.completion",
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": "hello"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}
        })))
        .expect(1)
        .mount(&mock)
        .await;

    common::seed_tenant(&pool, "tenant-anth-run").await;
    common::seed_resource_group(&pool, "rg-anth-run", "tenant-anth-run").await;
    common::seed_end_user(&pool, 9003, "tenant-anth-run").await;
    common::add_direct_membership(&pool, 9003, "rg-anth-run", "tenant-anth-run").await;
    common::seed_llm_provider(&pool, "prov-anth-run", "tenant-anth-run", "openai").await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-anth-run",
        "tenant-anth-run",
        "prov-anth-run",
        "test-gpt",
        &mock.uri(),
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "m-anth-run", "rg-anth-run").await;
    let api_key = common::seed_api_key(
        &pool,
        "tenant-anth-run",
        "rg-anth-run",
        Some(9003),
        "k-anth-run",
    )
    .await;

    let app = common::build_anthropic_app(common::build_test_state(pool.clone()));
    let req = common::build_post_request(
        "/v1/messages",
        json!({
            "model": "m-anth-run",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}]
        }),
        Some(&api_key),
        Some("tenant-anth-run"),
    );

    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let root = response
        .headers()
        .get("x-brutor-run-id")
        .and_then(|v| v.to_str().ok())
        .map(str::to_string);
    let body_str = body_to_string(response.into_body()).await;
    assert_eq!(status, StatusCode::OK, "body={body_str}");
    let root = root.expect("/v1/messages must return x-brutor-run-id");
    assert!(!root.is_empty());

    drop(mock);
}
