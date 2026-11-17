// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A provider's error that is not JSON reaches the caller.
//!
//! The Responses, Completions and Embeddings handlers (and the router's
//! streaming error path) read the provider's reply with
//! `resp.json().await.unwrap_or_default()`. A plain-text or HTML error (Google
//! answers an unknown model with one) became `null`: the live E2E sweep got
//! "404: null" for `gemini/text-embedding-004`, and neither the caller nor the
//! audit row said what the provider objected to. The reply is now read as text
//! and, when it is not JSON, carried as an error message with its status.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::json;
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

const T: &str = "t-upstream-text";
const PROVIDER_TEXT: &str = "Error 404 (Not Found)!! The requested model was not found.";

async fn send(
    pool: &PgPool,
    key: &str,
    path: &str,
    body: serde_json::Value,
) -> (StatusCode, String) {
    let app = common::build_llm_app(common::build_test_state(pool.clone()));
    let req: Request<Body> =
        common::build_post_request(&format!("/v1/proxy/llm{path}"), body, Some(key), Some(T));
    let resp = app.oneshot(req).await.expect("router");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (status, String::from_utf8_lossy(&bytes).into_owned())
}

/// A provider that answers everything with a plain-text 404.
async fn setup(pool: &PgPool) -> String {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(404).set_body_string(PROVIDER_TEXT))
        .mount(&mock)
        .await;
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, "rg-u", T).await;
    common::seed_llm_provider(pool, "prov-t", T, "openai").await;
    common::seed_llm_model_with_api_url(pool, "m-t", T, "prov-t", "text-model", &mock.uri()).await;
    common::bind_llm_model_to_rg(pool, "m-t", "rg-u").await;
    // Keep the mock alive for the test's lifetime.
    std::mem::forget(mock);
    common::seed_key(pool, T, Some("rg-u"), false, "k").await
}

fn carries_provider_text(path: &str, status: StatusCode, body: &str) {
    assert!(!status.is_success(), "{path}: {status} {body}");
    assert_ne!(
        body.trim(),
        "null",
        "{path}: the provider's error was dropped"
    );
    assert!(
        body.contains("The requested model was not found"),
        "{path}: the caller must see what the provider said: {body}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn embeddings_keep_a_plain_text_provider_error(pool: PgPool) {
    let key = setup(&pool).await;
    let (status, body) = send(
        &pool,
        &key,
        "/embeddings",
        json!({"model": "text-model", "input": "hi"}),
    )
    .await;
    carries_provider_text("/embeddings", status, &body);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn responses_keep_a_plain_text_provider_error(pool: PgPool) {
    let key = setup(&pool).await;
    let (status, body) = send(
        &pool,
        &key,
        "/responses",
        json!({"model": "text-model", "input": "hi"}),
    )
    .await;
    carries_provider_text("/responses", status, &body);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn completions_keep_a_plain_text_provider_error(pool: PgPool) {
    let key = setup(&pool).await;
    let (status, body) = send(
        &pool,
        &key,
        "/completions",
        json!({"model": "text-model", "prompt": "hi"}),
    )
    .await;
    carries_provider_text("/completions", status, &body);
}
