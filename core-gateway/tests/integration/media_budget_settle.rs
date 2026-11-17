// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The per-mode media daily budget and a
//! call whose cost is not known when it is admitted.
//!
//! The per-mode media limits (`media_global_limits[mode]`) are admitted on
//! the shared counters `media:{mode}:grp:{group}:{req,units,cost}`. The
//! admission reserved the cost the route knew before the call and nothing
//! was added afterwards. A token-priced image (`capabilities.token_priced`,
//! no `cost_per_image`) is priced from the usage the provider reports, so it
//! was admitted at $0 and the cost counter never moved: a daily budget on
//! image generation refused nothing, however much the day's images cost.
//!
//! Now the admission reserves what the row can price before the call (the
//! prompt's text at the row's text input price; the row states no number of
//! output tokens per image, so the output cannot be reserved) and the
//! counters are raised to what the call consumed once the provider has
//! answered. Against a REAL Redis: the counters are the shared ones.

use axum::http::StatusCode;
use base64::Engine;
use brutor_gateway_core::services::SharedState;
use brutor_gateway_core::services::cache::usage_state::QuotaWindow;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

/// 30 text input tokens at $5, 20 image input tokens at $8 and 4,160 output
/// tokens at $30, per 1M: $0.12511 an image call.
const CALL_MICROS: i64 = 125_110;

async fn replica(pool: &PgPool) -> SharedState {
    let url =
        std::env::var("TEST_REDIS_URL").unwrap_or_else(|_| "redis://127.0.0.1:6379/15".to_string());
    let client = redis::Client::open(url.as_str()).expect("redis url");
    let conn = redis::aio::ConnectionManager::new(client)
        .await
        .unwrap_or_else(|e| panic!("connect to the test Redis at {url} (set TEST_REDIS_URL): {e}"));
    let config = brutor_gateway_core::config::AppConfig::for_test(
        "postgresql://placeholder/replaced".to_string(),
    );
    std::sync::Arc::new(brutor_gateway_core::services::AppState::new(
        config,
        pool.clone(),
        Some(conn),
        reqwest::Client::new(),
        brutor_gateway_core::services::telemetry::TelemetryState {
            prometheus_handle: None,
        },
    ))
}

async fn provider(usage: Option<Value>) -> MockServer {
    let png =
        base64::engine::general_purpose::STANDARD.encode(b"\x89PNG\r\n\x1a\nnot-a-real-image");
    let mut body = json!({"created": 1_700_000_000, "data": [{"b64_json": png}]});
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

fn usage() -> Value {
    json!({
        "input_tokens": 50,
        "input_tokens_details": {"text_tokens": 30, "image_tokens": 20},
        "output_tokens": 4160,
        "total_tokens": 4210
    })
}

/// A tenant, a group of its own (the counters are keyed by it, on a Redis
/// every test shares) and the seed row of `gpt-image-2.6-flare`.
async fn seed(pool: &PgPool, upstream: &str, limits: Value) -> (String, String, String) {
    let id = uuid::Uuid::new_v4().simple().to_string();
    let (t, rg, model) = (
        format!("t-mbs-{id}"),
        format!("rg-mbs-{id}"),
        format!("img-{id}"),
    );
    common::seed_tenant(pool, &t).await;
    common::seed_resource_group(pool, &rg, &t).await;
    common::seed_llm_provider(pool, &format!("prov-{id}"), &t, "openai").await;
    common::seed_llm_model_with_api_url(
        pool,
        &model,
        &t,
        &format!("prov-{id}"),
        "gpt-image-2.6-flare",
        upstream,
    )
    .await;
    common::set_model_mode(pool, &model, "image_generation").await;
    common::set_model_api_key(pool, &model, "sk-test").await;
    sqlx::query(
        "UPDATE llm_model SET max_images_per_request = 10, cost_per_image = NULL, \
         cost_per_input_token = 5.0, cost_per_output_token = 30.0, \
         capabilities = '{\"token_priced\": true, \"cost_per_image_input_token\": 8.0}', \
         additional_drop_params = '[\"response_format\", \"style\"]' WHERE id = $1",
    )
    .bind(&model)
    .execute(pool)
    .await
    .expect("token-priced row");
    common::bind_llm_model_to_rg(pool, &model, &rg).await;
    common::set_rg_media_limits(pool, &rg, json!({"image_generation": limits})).await;
    let key = common::seed_api_key(pool, &t, &rg, None, &format!("k-{id}")).await;
    (t, rg, format!("{key}|{model}"))
}

async fn generate(state: &SharedState, t: &str, key_and_model: &str) -> (StatusCode, Value) {
    let (key, model) = key_and_model.split_once('|').expect("key|model");
    let req = common::build_post_request(
        "/v1/proxy/llm/images/generations",
        json!({"model": model, "prompt": "a red apple", "n": 1, "response_format": "b64_json"}),
        Some(key),
        Some(t),
    );
    let resp = common::build_multimodal_app(state.clone())
        .oneshot(req)
        .await
        .expect("router call");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

/// The day's cost counter of the group's image generation, once it has
/// reached `at_least` (the top-up after the response is not awaited by the
/// request), or what it holds after two seconds.
async fn cost_counter(state: &SharedState, rg: &str, at_least: i64) -> i64 {
    let key = format!("media:image_generation:grp:{rg}:cost");
    let mut seen = 0;
    for _ in 0..40 {
        seen = state.usage_state.read_usage(&key, QuotaWindow::Day).await;
        if seen >= at_least {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    seen
}

/// A $0.30 daily budget on image generation and calls of $0.12511:
/// three fit (the third takes the day to $0.375, a call's cost is not known
/// before it), the fourth is refused. Every call was admitted.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_spent_media_budget_refuses_a_token_priced_image(pool: PgPool) {
    let upstream = provider(Some(usage())).await;
    let (t, rg, km) = seed(&pool, &upstream.uri(), json!({"daily_budget_usd": 0.30})).await;
    let state = replica(&pool).await;

    let mut statuses = Vec::new();
    for call in 1..=5 {
        let (status, body) = generate(&state, &t, &km).await;
        if status == StatusCode::OK {
            cost_counter(&state, &rg, call * CALL_MICROS).await;
        } else {
            assert!(
                body.to_string().contains("media_daily_budget")
                    || body.to_string().to_lowercase().contains("budget"),
                "{body}"
            );
        }
        statuses.push(status.as_u16());
    }
    assert_eq!(statuses, [200, 200, 200, 429, 429]);
    assert_eq!(
        upstream.received_requests().await.unwrap_or_default().len(),
        3,
        "a refused call does not reach the provider"
    );
}

/// The counter. After one call the cost counter carries what the
/// call cost. It carried nothing, whether or not a budget is configured:
/// here only a request limit is, and a budget set later in the day would
/// have started from a counter that had missed the day's spend.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_media_cost_counter_carries_what_a_token_priced_image_cost(pool: PgPool) {
    let upstream = provider(Some(usage())).await;
    for limits in [
        json!({"daily_budget_usd": 100.0}),
        json!({"daily_requests": 100}),
    ] {
        let (t, rg, km) = seed(&pool, &upstream.uri(), limits.clone()).await;
        let state = replica(&pool).await;
        let (status, body) = generate(&state, &t, &km).await;
        assert_eq!(status, StatusCode::OK, "{body}");
        assert_eq!(
            cost_counter(&state, &rg, CALL_MICROS).await,
            CALL_MICROS,
            "{limits}"
        );
    }
}

/// No usage reported. The call is recorded at zero cost, with a
/// warning, as every other media kind records a call it has nothing to
/// price from (a transcription whose provider reports no duration). What
/// was reserved at admission stays on the counter: "a red apple" is 11
/// characters, 3 tokens at $5 per 1M, 15 micro-dollars. It is not free on
/// the budget, and it was.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_image_with_no_reported_usage_keeps_its_reservation(pool: PgPool) {
    let upstream = provider(None).await;
    let (t, rg, km) = seed(&pool, &upstream.uri(), json!({"daily_budget_usd": 1.0})).await;
    let state = replica(&pool).await;
    let (status, body) = generate(&state, &t, &km).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["brutor_cost_usd"].as_f64(), Some(0.0), "{body}");
    assert_eq!(cost_counter(&state, &rg, 15).await, 15);
}
