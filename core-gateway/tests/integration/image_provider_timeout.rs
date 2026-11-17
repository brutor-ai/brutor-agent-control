// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! An image provider that runs out of time is reported as a timeout.
//!
//! A fal app cold-starting (live: `fal-ai/stable-diffusion-v35-medium` sat at
//! queue position 0 for ~150 s) outlasted the model's timeout (120 s by
//! default), and the caller got 502 "HTTP send: error sending request for
//! url …": a provider failure, with nothing saying the gateway stopped
//! waiting, or that the model's timeout is the setting to raise. It is now a
//! 504 naming the timeout.

use std::time::Duration;

use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::json;
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_slow_image_provider_is_a_named_timeout(pool: PgPool) {
    let slow = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({"images": []}))
                .set_delay(Duration::from_secs(4)),
        )
        .mount(&slow)
        .await;

    common::seed_tenant(&pool, "t1").await;
    common::seed_resource_group(&pool, "rg1", "t1").await;
    common::seed_llm_provider(&pool, "prov-fal", "t1", "fal_ai").await;
    common::seed_llm_model_with_api_url(
        &pool,
        "img-slow",
        "t1",
        "prov-fal",
        "fal-ai/slow-app",
        &slow.uri(),
    )
    .await;
    common::set_model_mode(&pool, "img-slow", "image_generation").await;
    common::set_model_api_key(&pool, "img-slow", "fal-test").await;
    sqlx::query("UPDATE llm_model SET timeout = 1 WHERE id = 'img-slow'")
        .execute(&pool)
        .await
        .unwrap();
    common::bind_llm_model_to_rg(&pool, "img-slow", "rg1").await;

    // An API key bound to the group; the Portal-session
    // variant is the Enterprise suite's.
    let key = common::seed_api_key(&pool, "t1", "rg1", None, "k-img").await;
    let app = common::build_multimodal_app(common::build_test_state(pool));
    let req = common::build_post_request(
        "/v1/proxy/llm/images/generations",
        json!({"model": "img-slow", "prompt": "a red apple", "response_format": "b64_json"}),
        Some(&key),
        Some("t1"),
    );
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    let body =
        String::from_utf8_lossy(&resp.into_body().collect().await.unwrap().to_bytes()).into_owned();
    assert_eq!(status, StatusCode::GATEWAY_TIMEOUT, "{body}");
    assert!(
        body.contains("did not answer within 1s") && body.contains("timeout"),
        "the caller must learn it was the model's timeout: {body}"
    );
}
