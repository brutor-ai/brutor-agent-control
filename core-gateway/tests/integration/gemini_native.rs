// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Gemini-native `:generateContent` output-guardrail wiring.
//!
//! Regression: the non-streaming `generate_content` handler ran NO output
//! guardrails (only the streaming branch used StreamGuard), so the
//! Gemini-native response was returned with no PII/banned/secrets/toxic
//! enforcement. This pins that a banned-words check on `chat_output` now
//! blocks the native non-streaming response.

use axum::body::Body;
use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

async fn body_to_string(body: Body) -> String {
    let bytes = body.collect().await.expect("collect body").to_bytes();
    String::from_utf8_lossy(&bytes).to_string()
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn generate_content_output_guardrail_blocks_banned_words(pool: PgPool) {
    // Gemini-native upstream returns the banned phrase in a candidate part.
    // The upstream URL is `{api_url}/v1beta/models/{model_name}:generateContent`.
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/v1beta/models/test-gem:generateContent"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "candidates": [{
                "content": {"role": "model", "parts": [{"text": "sure: forbidden-phrase here"}]},
                "finishReason": "STOP"
            }],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 3, "totalTokenCount": 4}
        })))
        .expect(1)
        .mount(&mock)
        .await;

    common::seed_tenant(&pool, "tenant-gem").await;
    common::seed_resource_group(&pool, "rg-gem", "tenant-gem").await;
    common::seed_end_user(&pool, 9101, "tenant-gem").await;
    common::add_direct_membership(&pool, 9101, "rg-gem", "tenant-gem").await;
    common::seed_llm_provider(&pool, "prov-gem", "tenant-gem", "gemini").await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-gem",
        "tenant-gem",
        "prov-gem",
        "test-gem",
        &mock.uri(),
    )
    .await;
    // Gemini's resolve_gemini_endpoint 500s without an api_key.
    common::set_model_api_key(&pool, "m-gem", "dummy-key").await;
    common::bind_llm_model_to_rg(&pool, "m-gem", "rg-gem").await;

    common::seed_llm_banned_words_guardrail(
        &pool,
        "gr-gem",
        "tenant-gem",
        &["forbidden-phrase"],
        false, // chat_input
        true,  // chat_output
    )
    .await;
    common::bind_guardrail_to_rg(&pool, "gr-gem", "rg-gem").await;

    let api_key = common::seed_api_key(&pool, "tenant-gem", "rg-gem", Some(9101), "k-gem").await;

    let state = common::build_test_state(pool.clone());
    let app = common::build_gemini_app(state);

    // Client addresses the model by id (resolve_model matches id = $1); the
    // upstream URL is then built from the resolved model_name (test-gem).
    let req = common::build_post_request(
        "/v1beta/models/m-gem:generateContent",
        json!({
            "contents": [{"role": "user", "parts": [{"text": "anything clean"}]}]
        }),
        Some(&api_key),
        Some("tenant-gem"),
    );

    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let body_str = body_to_string(response.into_body()).await;

    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "Gemini-native output guardrail must block the banned phrase (403); got {status}, body={body_str}"
    );
    assert!(
        !body_str.contains("forbidden-phrase here"),
        "banned phrase leaked into the Gemini-native response: {body_str}"
    );
    let body: Value = serde_json::from_str(&body_str).unwrap_or(Value::Null);
    assert_eq!(
        body.pointer("/error/code").and_then(|v| v.as_u64()),
        Some(403),
        "expected Gemini-shaped error envelope, got {body_str}"
    );

    common::assert_single_audit_row(&pool, "tenant-gem").await;

    drop(mock);
}
