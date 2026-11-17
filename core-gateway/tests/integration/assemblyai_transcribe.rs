// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! What the gateway submits to AssemblyAI :
//! the model goes in `speech_models`, a list — the singular
//! `speech_model` is deprecated — and a row with no model name falls back to
//! `universal-3-5-pro`, not to the deprecated value `best`.

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

const T: &str = "tenant-aai";
const RG: &str = "rg-aai";
const MODEL: &str = "m-aai";

async fn assemblyai() -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/v2/upload"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({"upload_url": "https://cdn.example/audio/1"})),
        )
        .mount(&mock)
        .await;
    Mock::given(method("POST"))
        .and(wm_path("/v2/transcript"))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(json!({"id": "job-1", "status": "queued"})),
        )
        .mount(&mock)
        .await;
    Mock::given(method("GET"))
        .and(wm_path("/v2/transcript/job-1"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "job-1", "status": "completed", "text": "hello world",
            "audio_duration": 1.0, "language_code": "en"
        })))
        .mount(&mock)
        .await;
    mock
}

async fn seed(pool: &PgPool, model_name: &str, uri: &str) -> String {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_llm_provider(pool, "prov-aai", T, "assemblyai").await;
    common::seed_llm_model_with_api_url(pool, MODEL, T, "prov-aai", MODEL, uri).await;
    common::set_model_mode(pool, MODEL, "audio_transcription").await;
    common::set_model_api_key(pool, MODEL, "aai-test").await;
    sqlx::query("UPDATE llm_model SET model_name = $1 WHERE id = $2")
        .bind(model_name)
        .bind(MODEL)
        .execute(pool)
        .await
        .expect("set model_name");
    common::bind_llm_model_to_rg(pool, MODEL, RG).await;
    common::seed_api_key(pool, T, RG, None, "aai-key").await
}

fn request(key: &str) -> Request<Body> {
    let b = "----brutoraai";
    let body = format!(
        "--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.mp3\"\r\n\
         Content-Type: application/octet-stream\r\n\r\n\x00\r\n\
         --{b}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{MODEL}\r\n\
         --{b}--\r\n"
    );
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

/// Transcribe once; return the status, the answer and the submit body
/// AssemblyAI received.
async fn transcribe(pool: PgPool, model_name: &str) -> (StatusCode, String, Value) {
    let mock = assemblyai().await;
    let key = seed(&pool, model_name, &mock.uri()).await;
    let app: Router = common::build_multimodal_app(common::build_test_state(pool));
    let resp = app.oneshot(request(&key)).await.expect("router");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.expect("body").to_bytes();
    let submit = mock
        .received_requests()
        .await
        .unwrap_or_default()
        .iter()
        .find(|r| r.method.as_str() == "POST" && r.url.path() == "/v2/transcript")
        .map(|r| serde_json::from_slice::<Value>(&r.body).unwrap_or(Value::Null))
        .unwrap_or(Value::Null);
    (status, String::from_utf8_lossy(&bytes).into_owned(), submit)
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_model_is_submitted_as_a_list_in_speech_models(pool: PgPool) {
    let (status, body, submit) = transcribe(pool, "universal-2").await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert!(body.contains("hello world"), "{body}");
    assert_eq!(submit["speech_models"], json!(["universal-2"]), "{submit}");
    assert!(
        submit.get("speech_model").is_none(),
        "the singular parameter is deprecated: {submit}"
    );
    assert_eq!(submit["audio_url"], json!("https://cdn.example/audio/1"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_row_without_a_model_name_gets_the_current_default(pool: PgPool) {
    let (status, body, submit) = transcribe(pool, "").await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(
        submit["speech_models"],
        json!(["universal-3-5-pro"]),
        "`best` is a deprecated value: {submit}"
    );
    assert!(submit.get("speech_model").is_none(), "{submit}");
}
