// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The cost of an image from a model that
//! publishes token prices and no per-image price.
//!
//! The default image model of the catalog (`gpt-image-2.6-flare`)
//! has no `cost_per_image`: its row says `capabilities.token_priced` and
//! carries the text input price (`cost_per_input_token`), the image input
//! price (`capabilities.cost_per_image_input_token`) and the image output
//! price (`cost_per_output_token`, or `capabilities.cost_per_image_output_token`
//! when the row states one), each per 1M tokens. The route priced an image
//! as `cost_per_image × n`, so every image of the default model was recorded
//! at zero: on the audit row, on the usage record the budgets sum, and in
//! the answer. The cost now comes from the usage the provider reports and
//! the row's prices. A model with a per-image price is priced as before.

use axum::http::StatusCode;
use base64::Engine;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

const T: &str = "t-imgprice";
const RG: &str = "rg-imgprice";

/// An OpenAI images answer of `n` images that reports `usage`.
async fn provider(n: usize, usage: Option<Value>) -> MockServer {
    let png =
        base64::engine::general_purpose::STANDARD.encode(b"\x89PNG\r\n\x1a\nnot-a-real-image");
    let mut body = json!({
        "created": 1_700_000_000,
        "data": (0..n).map(|_| json!({"b64_json": png})).collect::<Vec<_>>(),
    });
    if let Some(usage) = usage {
        body["usage"] = usage;
    }
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/v1/images/generations"))
        .respond_with(ResponseTemplate::new(200).set_body_json(body))
        .mount(&server)
        .await;
    server
}

/// What OpenAI reports for a token-priced image model.
fn usage() -> Value {
    json!({
        "input_tokens": 50,
        "input_tokens_details": {"text_tokens": 30, "image_tokens": 20},
        "output_tokens": 4160,
        "total_tokens": 4210
    })
}

async fn seed(pool: &PgPool, model_id: &str, model_name: &str, upstream: &str) -> String {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_llm_provider(pool, "prov-imgprice", T, "openai").await;
    common::seed_llm_model_with_api_url(pool, model_id, T, "prov-imgprice", model_name, upstream)
        .await;
    common::set_model_mode(pool, model_id, "image_generation").await;
    common::set_model_api_key(pool, model_id, "sk-test").await;
    sqlx::query("UPDATE llm_model SET max_images_per_request = 10 WHERE id = $1")
        .bind(model_id)
        .execute(pool)
        .await
        .expect("images per request");
    common::bind_llm_model_to_rg(pool, model_id, RG).await;
    common::seed_api_key(pool, T, RG, None, "k-imgprice").await
}

/// The seed row of `gpt-image-2.6-flare`
/// (`openai_multimodal_models_init.py`): token prices, no per-image price.
async fn make_token_priced(pool: &PgPool, model_id: &str, capabilities: Value) {
    sqlx::query(
        "UPDATE llm_model SET cost_per_image = NULL, cost_per_input_token = 5.0, \
         cost_per_output_token = 30.0, capabilities = $2, \
         additional_drop_params = '[\"response_format\", \"style\"]' WHERE id = $1",
    )
    .bind(model_id)
    .bind(capabilities)
    .execute(pool)
    .await
    .expect("token-priced row");
}

async fn generate(pool: &PgPool, key: &str, model_id: &str, n: usize) -> (StatusCode, Value) {
    let app = common::build_multimodal_app(common::build_test_state(pool.clone()));
    let req = common::build_post_request(
        "/v1/proxy/llm/images/generations",
        json!({"model": model_id, "prompt": "a red apple", "n": n, "response_format": "b64_json"}),
        Some(key),
        Some(T),
    );
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

/// The cost on the audit row and on the usage record of the one image call.
async fn recorded(pool: &PgPool) -> (f64, f64) {
    for _ in 0..100 {
        let log: Option<(Option<f64>,)> = sqlx::query_as(
            "SELECT llm_estimated_cost_usd::double precision FROM proxy_logs \
             WHERE tenant_id = $1 AND endpoint = '/v1/proxy/llm/images/generations' \
               AND success = true",
        )
        .bind(T)
        .fetch_optional(pool)
        .await
        .expect("proxy_logs");
        let usage: Option<(Option<f64>,)> = sqlx::query_as(
            "SELECT estimated_cost::double precision FROM usage_logs WHERE tenant_id = $1",
        )
        .bind(T)
        .fetch_optional(pool)
        .await
        .expect("usage_logs");
        if let (Some((Some(log),)), Some((Some(usage),))) = (log, usage) {
            return (log, usage);
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    panic!("no audit row and usage record with a cost for the image call");
}

fn close(a: f64, b: f64) -> bool {
    (a - b).abs() < 1e-9
}

/// 30 text input tokens at $5, 20 image input tokens at $8 and 4,160 output
/// tokens at $30, per 1M: $0.12511. It was recorded as $0.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_token_priced_image_is_costed_from_the_reported_usage(pool: PgPool) {
    let upstream = provider(1, Some(usage())).await;
    let key = seed(&pool, "img-flare", "gpt-image-2.6-flare", &upstream.uri()).await;
    make_token_priced(
        &pool,
        "img-flare",
        json!({"modalities": ["text", "image"], "token_priced": true,
               "cost_per_image_input_token": 8.0}),
    )
    .await;

    let (status, body) = generate(&pool, &key, "img-flare", 1).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let expected = (30.0 * 5.0 + 20.0 * 8.0 + 4160.0 * 30.0) / 1_000_000.0;
    let answered = body["brutor_cost_usd"].as_f64().expect("brutor_cost_usd");
    assert!(
        close(answered, expected),
        "answered {answered}, expected {expected}"
    );
    let (log, usage) = recorded(&pool).await;
    assert!(close(log, expected), "audit row {log}, expected {expected}");
    assert!(
        close(usage, expected),
        "usage record {usage}, expected {expected}"
    );
}

/// The row states an image output price of its own: it is used for the
/// output tokens instead of the text output price.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_rows_image_output_price_is_used_where_it_states_one(pool: PgPool) {
    let upstream = provider(2, Some(usage())).await;
    let key = seed(&pool, "img-out", "gpt-image-2.6-sunburst", &upstream.uri()).await;
    make_token_priced(
        &pool,
        "img-out",
        json!({"token_priced": true, "cost_per_image_input_token": 8.0,
               "cost_per_image_output_token": 40.0}),
    )
    .await;

    let (status, body) = generate(&pool, &key, "img-out", 2).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let expected = (30.0 * 5.0 + 20.0 * 8.0 + 4160.0 * 40.0) / 1_000_000.0;
    let (log, usage) = recorded(&pool).await;
    assert!(close(log, expected), "audit row {log}, expected {expected}");
    assert!(
        close(usage, expected),
        "usage record {usage}, expected {expected}"
    );
}

/// A model with a per-image price is priced per image, as before, whatever
/// usage the provider reports and whatever token prices the row carries.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_model_with_a_per_image_price_is_priced_per_image(pool: PgPool) {
    let upstream = provider(2, Some(usage())).await;
    let key = seed(&pool, "img-flat", "dall-e-3", &upstream.uri()).await;
    sqlx::query(
        "UPDATE llm_model SET cost_per_image = 0.04, cost_per_input_token = 5.0, \
         cost_per_output_token = 30.0 WHERE id = 'img-flat'",
    )
    .execute(&pool)
    .await
    .expect("per-image price");

    let (status, body) = generate(&pool, &key, "img-flat", 2).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let (log, usage) = recorded(&pool).await;
    assert!(close(log, 0.08), "audit row {log}");
    assert!(close(usage, 0.08), "usage record {usage}");
    assert!(close(body["brutor_cost_usd"].as_f64().expect("cost"), 0.08));
}

/// Token prices on a row that does not say `token_priced` price nothing:
/// the row decides, not the presence of a `usage` object.
#[sqlx::test(migrations = "./tests/migrations")]
async fn token_prices_are_not_applied_to_a_row_that_does_not_say_token_priced(pool: PgPool) {
    let upstream = provider(1, Some(usage())).await;
    let key = seed(&pool, "img-plain", "some-image-model", &upstream.uri()).await;
    make_token_priced(&pool, "img-plain", json!({"modalities": ["text", "image"]})).await;

    let (status, body) = generate(&pool, &key, "img-plain", 1).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let (log, usage) = recorded(&pool).await;
    assert!(close(log, 0.0) && close(usage, 0.0), "{log} {usage}");
}
