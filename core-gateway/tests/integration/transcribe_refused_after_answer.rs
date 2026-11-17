// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A refusal after the provider answered, on `/v1/proxy/llm/audio/transcriptions`.
//!
//! A model's duration cap (`max_audio_duration_seconds`) can only be
//! applied to the length the provider reports, so it is applied after the
//! provider answered, and was billed. The 413 returned without an outcome
//! row, without the cost, without settling the group's counters and without
//! closing the dispatch guard: a caller could transcribe over-long audio at
//! the operator's expense with nothing on the books, and a daily budget
//! never saw it.
//!
//! When a fallback model serves, the cost is the serving model's and
//! the records name the model that served.

use axum::body::Body;
use axum::extract::ConnectInfo;
use axum::http::{Request, StatusCode};
use brutor_gateway_core::services::SharedState;
use brutor_gateway_core::services::cache::usage_state::QuotaWindow;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

/// A state on the suite's Redis: the media counters are admitted there.
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

async fn openai(status: u16, seconds: u64) -> MockServer {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/v1/audio/transcriptions"))
        .respond_with(ResponseTemplate::new(status).set_body_json(json!({
            "text": "hello there",
            "usage": {"type": "duration", "seconds": seconds}
        })))
        .mount(&server)
        .await;
    server
}

struct World {
    t: String,
    rg: String,
    key: String,
    model: String,
}

/// Unique ids: the counters are keyed by the group on a Redis every test
/// shares.
async fn seed(pool: &PgPool, upstream: &str, per_minute: f64) -> World {
    let id = uuid::Uuid::new_v4().simple().to_string();
    let w = World {
        t: format!("t-tra-{id}"),
        rg: format!("rg-tra-{id}"),
        key: String::new(),
        model: format!("stt-{id}"),
    };
    common::seed_tenant(pool, &w.t).await;
    common::seed_resource_group(pool, &w.rg, &w.t).await;
    common::seed_llm_provider(pool, &format!("prov-{id}"), &w.t, "openai").await;
    seed_model(pool, &w, &w.model, "gpt-transcribe", upstream, per_minute).await;
    let key = common::seed_api_key(pool, &w.t, &w.rg, None, &format!("k-{id}")).await;
    World { key, ..w }
}

async fn seed_model(
    pool: &PgPool,
    w: &World,
    model: &str,
    name: &str,
    upstream: &str,
    per_minute: f64,
) {
    let prov = format!("prov-{}", w.t.trim_start_matches("t-tra-"));
    common::seed_llm_model_with_api_url(pool, model, &w.t, &prov, name, upstream).await;
    common::set_model_mode(pool, model, "audio_transcription").await;
    common::set_model_api_key(pool, model, "sk-test").await;
    sqlx::query(
        "UPDATE llm_model SET cost_per_audio_minute = $2, cost_per_input_token = 0.0, \
         cost_per_output_token = 0.0, supported_response_formats = '[\"json\"]' WHERE id = $1",
    )
    .bind(model)
    .bind(per_minute)
    .execute(pool)
    .await
    .expect("the transcription row");
    common::bind_llm_model_to_rg(pool, model, &w.rg).await;
}

async fn transcribe(state: &SharedState, w: &World) -> (StatusCode, Value) {
    let b = "----brutortra";
    let body = format!(
        "--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.mp3\"\r\n\
         Content-Type: audio/mpeg\r\n\r\n\x00\r\n\
         --{b}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{}\r\n--{b}--\r\n",
        w.model
    );
    let mut req = Request::builder()
        .method("POST")
        .uri("/v1/proxy/llm/audio/transcriptions")
        .header("content-type", format!("multipart/form-data; boundary={b}"))
        .header("X-Tenant-ID", &w.t)
        .header("authorization", format!("Bearer {}", w.key))
        .body(Body::from(body))
        .expect("multipart request");
    req.extensions_mut().insert(ConnectInfo(
        "127.0.0.1:55555".parse::<std::net::SocketAddr>().unwrap(),
    ));
    let resp = common::build_multimodal_app(state.clone())
        .oneshot(req)
        .await
        .expect("router");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.expect("body").to_bytes();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

type LogRow = (
    Option<i32>,
    bool,
    Option<f64>,
    Option<f64>,
    Option<String>,
    Option<String>,
    Option<String>,
);

/// The outcome rows of the tenant's transcriptions with `status`, once
/// there are `n`.
async fn outcome_rows(pool: &PgPool, w: &World, status: i32, n: usize) -> Vec<LogRow> {
    let mut rows = Vec::new();
    for _ in 0..60 {
        rows = sqlx::query_as(
            "SELECT http_status_code, success, llm_estimated_cost_usd::double precision, \
                    media_audio_seconds_input::double precision, llm_model_used, \
                    llm_provider, server_id \
               FROM proxy_logs \
              WHERE tenant_id = $1 AND endpoint = '/v1/proxy/llm/audio/transcriptions' \
                AND http_status_code = $2 ORDER BY timestamp",
        )
        .bind(&w.t)
        .bind(status)
        .fetch_all(pool)
        .await
        .expect("proxy_logs");
        if rows.len() >= n {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    rows
}

async fn cost_counter(state: &SharedState, rg: &str, at_least: i64) -> i64 {
    let key = format!("media:audio_transcription:grp:{rg}:cost");
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

/// 90 seconds on a model capped at 60, $0.0045 a minute: the answer
/// is refused (413) and the $0.00675 the provider charged is on an outcome
/// row and a usage row, with the 90 seconds; the group's media cost counter
/// carries it; the dispatch guard is closed, and not as a provider failure
/// (the provider answered). With a $0.01 daily budget the second such call
/// is still admitted (the day stands at $0.00675) and the third is refused
/// before the provider: a refused answer is not free.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_answer_refused_for_its_length_is_recorded_settled_and_closed(pool: PgPool) {
    let upstream = openai(200, 90).await;
    let w = seed(&pool, &upstream.uri(), 0.0045).await;
    sqlx::query("UPDATE llm_model SET max_audio_duration_seconds = 60 WHERE id = $1")
        .bind(&w.model)
        .execute(&pool)
        .await
        .expect("cap");
    common::set_rg_media_limits(
        &pool,
        &w.rg,
        json!({"audio_transcription": {"daily_budget_usd": 0.01}}),
    )
    .await;
    let state = replica(&pool).await;

    let (status, body) = transcribe(&state, &w).await;
    assert_eq!(status, StatusCode::PAYLOAD_TOO_LARGE, "{body}");
    assert!(
        body.get("text").is_none(),
        "the transcript is withheld: {body}"
    );

    let rows = outcome_rows(&pool, &w, 413, 1).await;
    assert_eq!(rows.len(), 1, "one outcome row for the refused answer");
    let (_, success, cost, seconds, ..) = rows[0].clone();
    assert!(!success);
    assert!((cost.unwrap_or(0.0) - 0.00675).abs() < 1e-9, "{cost:?}");
    assert_eq!(seconds, Some(90.0));

    let mut usage: Option<(Option<f64>, Option<i32>)> = None;
    for _ in 0..60 {
        usage = sqlx::query_as(
            "SELECT estimated_cost::double precision, status_code FROM usage_logs \
              WHERE tenant_id = $1",
        )
        .bind(&w.t)
        .fetch_optional(&pool)
        .await
        .expect("usage_logs");
        if usage.is_some() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    let (usage_cost, usage_status) = usage.expect("a usage row for the refused answer");
    assert!(
        (usage_cost.unwrap_or(0.0) - 0.00675).abs() < 1e-9,
        "{usage_cost:?}"
    );
    assert_eq!(usage_status, Some(413));

    assert_eq!(cost_counter(&state, &w.rg, 6_750).await, 6_750);
    assert_eq!(
        brutor_gateway_core::services::llm_resilience::leaked_guards(),
        0,
        "the dispatch guard was closed"
    );

    let (second, _) = transcribe(&state, &w).await;
    assert_eq!(second, StatusCode::PAYLOAD_TOO_LARGE);
    assert_eq!(cost_counter(&state, &w.rg, 13_500).await, 13_500);
    let (third, body) = transcribe(&state, &w).await;
    assert_eq!(third, StatusCode::TOO_MANY_REQUESTS, "{body}");
    assert_eq!(
        upstream.received_requests().await.unwrap_or_default().len(),
        2,
        "the call over the budget does not reach the provider"
    );
    assert_eq!(
        brutor_gateway_core::services::llm_resilience::leaked_guards(),
        0
    );

    // The provider answered every time: its breaker saw no failure, so a
    // caller sending long audio cannot open it. An in-cap answer is served.
    sqlx::query("UPDATE llm_model SET max_audio_duration_seconds = 600 WHERE id = $1")
        .bind(&w.model)
        .execute(&pool)
        .await
        .expect("cap");
    common::set_rg_media_limits(&pool, &w.rg, json!({})).await;
    for _ in 0..8 {
        let (status, body) = transcribe(&replica(&pool).await, &w).await;
        assert_eq!(status, StatusCode::OK, "{body}");
    }
}

/// The requested model fails (500) and its fallback serves 60
/// seconds. The requested model is $0.006 a minute, the fallback $0.06:
/// the call cost $0.06, in the answer and on the records, and the records
/// name the fallback as the model used and the requested model as
/// requested.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_fallback_that_served_is_what_is_costed_and_named(pool: PgPool) {
    let down = openai(500, 0).await;
    let up = openai(200, 60).await;
    let w = seed(&pool, &down.uri(), 0.006).await;
    let fallback = format!("{}-fb", w.model);
    seed_model(
        &pool,
        &w,
        &fallback,
        "gpt-transcribe-fallback",
        &up.uri(),
        0.06,
    )
    .await;
    common::set_model_fallback_models(&pool, &w.model, &[&fallback]).await;
    let state = replica(&pool).await;

    let (status, body) = transcribe(&state, &w).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["brutor_model_id"], json!(fallback), "{body}");
    assert!(
        (body["brutor_cost_usd"].as_f64().unwrap_or(0.0) - 0.06).abs() < 1e-9,
        "{body}"
    );
    assert_eq!(up.received_requests().await.unwrap_or_default().len(), 1);

    let rows = outcome_rows(&pool, &w, 200, 1).await;
    assert_eq!(rows.len(), 1);
    let (_, success, cost, seconds, used, _, server) = rows[0].clone();
    assert!(success);
    assert!((cost.unwrap_or(0.0) - 0.06).abs() < 1e-9, "{cost:?}");
    assert_eq!(seconds, Some(60.0));
    assert_eq!(used.as_deref(), Some("gpt-transcribe-fallback"));
    assert_eq!(server.as_deref(), Some(fallback.as_str()));
    let requested: (Option<String>,) = sqlx::query_as(
        "SELECT llm_model_requested FROM proxy_logs WHERE tenant_id = $1 AND http_status_code = 200",
    )
    .bind(&w.t)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(requested.0.as_deref(), Some("gpt-transcribe"));

    let mut usage: Option<(Option<String>, Option<String>, Option<f64>)> = None;
    for _ in 0..60 {
        usage = sqlx::query_as(
            "SELECT resource_id, model_name, estimated_cost::double precision FROM usage_logs \
              WHERE tenant_id = $1",
        )
        .bind(&w.t)
        .fetch_optional(&pool)
        .await
        .expect("usage_logs");
        if usage.is_some() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    let (resource, name, cost) = usage.expect("a usage row");
    assert_eq!(resource.as_deref(), Some(fallback.as_str()));
    assert_eq!(name.as_deref(), Some("gpt-transcribe-fallback"));
    assert!((cost.unwrap_or(0.0) - 0.06).abs() < 1e-9);
}
