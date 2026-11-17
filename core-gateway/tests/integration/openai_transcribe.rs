// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! `gpt-transcribe` on `v1/audio/transcriptions`.
//!
//! OpenAI's API reference for "Create transcription"
//! (<https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create>,
//! as read) lists two request parameters for the input language:
//! `language` (a string) and `languages` (an array, "Supported by
//! `gpt-transcribe`"). The `json` answer, the only format the catalog row of
//! `gpt-transcribe` states, is `text`, `languages` and `usage`; `usage` is
//! `{type: "duration", seconds}` for a model billed by audio duration or
//! `{type: "tokens", input_tokens, output_tokens, …}` for one billed by
//! tokens. A top-level `duration` exists only in `verbose_json` and
//! `diarized_json`.
//!
//! The gateway dropped a caller's `languages`, and read the audio's length
//! from the top-level `duration` only: a `json` answer was recorded as zero
//! seconds at zero cost, and the model's duration cap was not applied.

use axum::Router;
use axum::body::Body;
use axum::extract::ConnectInfo;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

const T: &str = "tenant-oait";
const RG: &str = "rg-oait";
const MODEL: &str = "m-oait";

async fn openai(answer: Value) -> MockServer {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/v1/audio/transcriptions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(answer))
        .mount(&server)
        .await;
    server
}

/// The seed row of `gpt-transcribe` (`openai_multimodal_models_init.py`):
/// `json` only, $0.0045 per audio minute, token prices of zero.
async fn seed(pool: &PgPool, uri: &str) -> String {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_llm_provider(pool, "prov-oait", T, "openai").await;
    common::seed_llm_model_with_api_url(pool, MODEL, T, "prov-oait", "gpt-transcribe", uri).await;
    common::set_model_mode(pool, MODEL, "audio_transcription").await;
    common::set_model_api_key(pool, MODEL, "sk-test").await;
    sqlx::query(
        "UPDATE llm_model SET cost_per_audio_minute = 0.0045, cost_per_input_token = 0.0, \
         cost_per_output_token = 0.0, supported_response_formats = '[\"json\"]' WHERE id = $1",
    )
    .bind(MODEL)
    .execute(pool)
    .await
    .expect("the gpt-transcribe row");
    common::bind_llm_model_to_rg(pool, MODEL, RG).await;
    common::seed_api_key(pool, T, RG, None, "oait-key").await
}

fn request(key: &str, fields: &[(&str, &str)]) -> Request<Body> {
    let b = "----brutoroait";
    let mut body = format!(
        "--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.mp3\"\r\n\
         Content-Type: audio/mpeg\r\n\r\n\x00\r\n\
         --{b}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{MODEL}\r\n"
    );
    for (name, value) in fields {
        body.push_str(&format!(
            "--{b}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n"
        ));
    }
    body.push_str(&format!("--{b}--\r\n"));
    let mut req = Request::builder()
        .method("POST")
        .uri("/v1/proxy/llm/audio/transcriptions")
        .header("content-type", format!("multipart/form-data; boundary={b}"))
        .header("X-Tenant-ID", T)
        .header("authorization", format!("Bearer {key}"))
        .body(Body::from(body))
        .expect("multipart request");
    req.extensions_mut().insert(ConnectInfo(
        "127.0.0.1:55555".parse::<std::net::SocketAddr>().unwrap(),
    ));
    req
}

async fn transcribe(pool: &PgPool, key: &str, fields: &[(&str, &str)]) -> (StatusCode, Value) {
    let app: Router = common::build_multimodal_app(common::build_test_state(pool.clone()));
    let resp = app.oneshot(request(key, fields)).await.expect("router");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.expect("body").to_bytes();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

/// The multipart form the provider received, as text.
async fn received_form(mock: &MockServer) -> String {
    let requests = mock.received_requests().await.unwrap_or_default();
    assert_eq!(requests.len(), 1, "one call to the provider");
    String::from_utf8_lossy(&requests[0].body).into_owned()
}

/// The seconds and the cost on the audit row of the one transcription.
async fn recorded(pool: &PgPool) -> (f64, f64) {
    for _ in 0..100 {
        let row: Option<(Option<f64>, Option<f64>)> = sqlx::query_as(
            "SELECT media_audio_seconds_input::double precision, \
                    llm_estimated_cost_usd::double precision FROM proxy_logs \
             WHERE tenant_id = $1 AND endpoint = '/v1/proxy/llm/audio/transcriptions' \
               AND success = true",
        )
        .bind(T)
        .fetch_optional(pool)
        .await
        .expect("proxy_logs");
        if let Some((Some(seconds), Some(cost))) = row {
            return (seconds, cost);
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    panic!("no audit row for the transcription");
}

fn json_answer(usage: Value) -> Value {
    json!({"text": "hello there", "languages": [{"code": "en"}], "usage": usage})
}

/// A caller's `languages` reach the provider, as the array the reference
/// describes (`languages[]` parts, however the caller spelled the field),
/// and so does `language` when that is what the caller sent. `languages`
/// was dropped.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_callers_languages_reach_the_provider(pool: PgPool) {
    let mock = openai(json_answer(json!({"type": "duration", "seconds": 3}))).await;
    let key = seed(&pool, &mock.uri()).await;
    let (status, body) = transcribe(
        &pool,
        &key,
        &[
            ("languages[]", "en"),
            ("languages", "sv"),
            ("language", "en"),
        ],
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let form = received_form(&mock).await;
    for expected in [
        "name=\"languages[]\"\r\n\r\nen\r\n",
        "name=\"languages[]\"\r\n\r\nsv\r\n",
        "name=\"language\"\r\n\r\nen\r\n",
        "name=\"response_format\"\r\n\r\njson\r\n",
    ] {
        assert!(form.contains(expected), "missing {expected:?} in:\n{form}");
    }
    assert_eq!(body["languages"], json!([{"code": "en"}]), "{body}");
}

/// A `json` answer carries the audio's length in `usage.seconds`. 90
/// seconds at $0.0045 a minute is $0.00675. It was recorded as 0 seconds at
/// $0.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_json_answer_is_costed_from_the_duration_in_usage(pool: PgPool) {
    let mock = openai(json_answer(json!({"type": "duration", "seconds": 90}))).await;
    let key = seed(&pool, &mock.uri()).await;
    let (status, body) = transcribe(&pool, &key, &[]).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let (seconds, cost) = recorded(&pool).await;
    assert_eq!(seconds, 90.0);
    assert!((cost - 0.00675).abs() < 1e-9, "{cost}");
}

/// The model's duration cap is applied to the length `usage` reports. It
/// was applied to zero.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_duration_cap_is_applied_to_the_duration_in_usage(pool: PgPool) {
    let mock = openai(json_answer(json!({"type": "duration", "seconds": 90}))).await;
    let key = seed(&pool, &mock.uri()).await;
    sqlx::query("UPDATE llm_model SET max_audio_duration_seconds = 60 WHERE id = $1")
        .bind(MODEL)
        .execute(&pool)
        .await
        .expect("cap");
    let (status, body) = transcribe(&pool, &key, &[]).await;
    assert_eq!(status, StatusCode::PAYLOAD_TOO_LARGE, "{body}");
}

/// A model billed by tokens reports `usage.type = "tokens"` and no
/// duration. A row that states token prices is costed from them: 1,000
/// input tokens at $2.50 and 200 output tokens at $10, per 1M, is $0.0045.
/// It was recorded at $0.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_token_billed_answer_is_costed_from_the_rows_token_prices(pool: PgPool) {
    let mock = openai(json_answer(json!({
        "type": "tokens", "input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200,
        "input_token_details": {"audio_tokens": 990, "text_tokens": 10}
    })))
    .await;
    let key = seed(&pool, &mock.uri()).await;
    sqlx::query(
        "UPDATE llm_model SET cost_per_audio_minute = NULL, cost_per_input_token = 2.5, \
         cost_per_output_token = 10.0 WHERE id = $1",
    )
    .bind(MODEL)
    .execute(&pool)
    .await
    .expect("token prices");
    let (status, body) = transcribe(&pool, &key, &[]).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let (seconds, cost) = recorded(&pool).await;
    assert_eq!(seconds, 0.0, "no duration is reported");
    assert!((cost - 0.0045).abs() < 1e-9, "{cost}");
}
