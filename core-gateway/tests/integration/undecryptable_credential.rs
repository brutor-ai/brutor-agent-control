// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! An undecryptable credential, on the model surface.
//!
//! A model row's `api_key` that is a ciphertext the gateway cannot decrypt
//! (written under another `ENCRYPTION_KEY`, or a `BAEv1:` envelope it cannot
//! unwrap) was sent to the provider AS the key: the model cache left a value
//! that did not decrypt in place, and the forwarder put it in the
//! `Authorization` header. Such a row is refused at dispatch (501
//! `provider_credential_missing`) and nothing is sent.

use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::json;
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::config::AppConfig;
use brutor_gateway_core::services::encryption::EncryptionService;
use brutor_gateway_core::services::telemetry::TelemetryState;
use brutor_gateway_core::services::{AppState, SharedState};

use crate::common;

const T: &str = "tenant-f1102";
const RG: &str = "rg-f1102";
const MODEL: &str = "m-f1102";

fn state(pool: PgPool) -> SharedState {
    let state = AppState::new(
        AppConfig::for_test("postgresql://placeholder/replaced".to_string()),
        pool,
        None,
        reqwest::Client::new(),
        TelemetryState {
            prometheus_handle: None,
        },
    );
    common::compose(state, crate::composition::extensions()).state
}

/// A chat model whose `api_url` is `upstream` and whose stored `api_key` is
/// `stored`, bound to the caller's group. Returns the caller's key.
async fn seed(pool: &PgPool, upstream: &MockServer, stored: &str) -> String {
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "c", "object": "chat.completion", "model": MODEL,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        })))
        .mount(upstream)
        .await;
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_llm_provider(pool, "prov-f1102", T, "openai").await;
    common::seed_llm_model_with_api_url(pool, MODEL, T, "prov-f1102", MODEL, &upstream.uri()).await;
    sqlx::query("UPDATE llm_model SET api_key = $1 WHERE id = $2")
        .bind(stored)
        .bind(MODEL)
        .execute(pool)
        .await
        .expect("store the api_key");
    common::bind_llm_model_to_rg(pool, MODEL, RG).await;
    common::seed_api_key(pool, T, RG, None, "f1102-key").await
}

async fn chat(state: &SharedState, key: &str) -> (StatusCode, String) {
    let resp = common::build_llm_app(state.clone())
        .oneshot(common::build_post_request(
            "/v1/proxy/llm/chat/completions",
            json!({"model_id": MODEL, "messages": [{"role": "user", "content": "the prompt"}]}),
            Some(key),
            Some(T),
        ))
        .await
        .expect("response");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.expect("body").to_bytes();
    (status, String::from_utf8_lossy(&bytes).into_owned())
}

async fn a_stored_key_that_does_not_decrypt_is_never_sent(pool: PgPool, stored: String) {
    let upstream = MockServer::start().await;
    let key = seed(&pool, &upstream, &stored).await;
    let (status, body) = chat(&state(pool), &key).await;
    let received = upstream.received_requests().await.expect("recording");
    assert!(
        received.is_empty(),
        "the provider was called {} time(s); Authorization carried the stored ciphertext: {}",
        received.len(),
        received.iter().any(|r| r
            .headers
            .get("authorization")
            .and_then(|v| v.to_str().ok())
            .is_some_and(|v| v.contains(&stored)))
    );
    assert_eq!(status, StatusCode::NOT_IMPLEMENTED, "{body}");
    assert!(body.contains("provider_credential_missing"), "{body}");
    assert!(
        !body.contains(&stored),
        "the answer repeats the stored value"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_key_encrypted_under_another_master_key_is_never_sent(pool: PgPool) {
    let stored = EncryptionService::new("another-installations-master-key")
        .encrypt("sk-made-up-f1102")
        .expect("encrypt");
    a_stored_key_that_does_not_decrypt_is_never_sent(pool, stored).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_key_in_an_envelope_the_gateway_cannot_unwrap_is_never_sent(pool: PgPool) {
    let inner = EncryptionService::new("a-data-key-the-gateway-never-sees")
        .encrypt("sk-made-up-f1102")
        .expect("encrypt");
    a_stored_key_that_does_not_decrypt_is_never_sent(
        pool,
        format!("BAEv1:aws_kms:d3JhcHBlZC1kYXRhLWtleQ:{inner}"),
    )
    .await;
}

/// The positive control: a key encrypted under this gateway's master key is
/// decrypted and sent.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_key_encrypted_under_this_master_key_is_sent_in_clear(pool: PgPool) {
    let upstream = MockServer::start().await;
    let stored = EncryptionService::new("test-encryption-key-32-chars-ok!")
        .encrypt("sk-made-up-f1102")
        .expect("encrypt");
    let key = seed(&pool, &upstream, &stored).await;
    let (status, body) = chat(&state(pool), &key).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let received = upstream.received_requests().await.expect("recording");
    assert_eq!(received.len(), 1);
    assert_eq!(
        received[0]
            .headers
            .get("authorization")
            .and_then(|v| v.to_str().ok()),
        Some("Bearer sk-made-up-f1102")
    );
}
