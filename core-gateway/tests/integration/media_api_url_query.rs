// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! If the image, speech, transcription and video routes
//! built the provider URL by appending the path to the row's `api_url` as a
//! string, an `api_url` with a query (`…/gw?api-version=2099-01-01`, as
//! a gateway or an Azure endpoint is pasted) would send the path inside the query
//! and the request to the wrong place. One builder
//! (`providers::join_api_url`, the one `providers::build_url` uses): the
//! path after the base's path, the base's query kept as the query.

use axum::body::Body;
use axum::extract::ConnectInfo;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::json;
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

const T: &str = "tenant-f489";
const RG: &str = "rg-f489";
const MODEL: &str = "m-f489";
const QUERY: &str = "api-version=2099-01-01";

async fn provider() -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/gw/v1/audio/speech"))
        .respond_with(ResponseTemplate::new(200).set_body_bytes(vec![0x49, 0x44, 0x33, 0x04]))
        .mount(&mock)
        .await;
    Mock::given(method("POST"))
        .and(wm_path("/gw/v1/images/generations"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "created": 1_700_000_000, "data": [{"b64_json": "aGVsbG8="}]
        })))
        .mount(&mock)
        .await;
    Mock::given(method("POST"))
        .and(wm_path("/gw/v1/audio/transcriptions"))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(json!({"text": "hello", "duration": 1.0})),
        )
        .mount(&mock)
        .await;
    Mock::given(method("POST"))
        .and(wm_path("/gw/videos"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({"id": "video_query", "status": "queued"})),
        )
        .mount(&mock)
        .await;
    mock
}

async fn seed(pool: &PgPool, mode: &str, mock: &MockServer) -> String {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_llm_provider(pool, "prov-f489", T, "openai").await;
    // A trailing slash before the query, as pasted.
    let api_url = format!("{}/gw/?{QUERY}", mock.uri());
    common::seed_llm_model_with_api_url(pool, MODEL, T, "prov-f489", MODEL, &api_url).await;
    common::set_model_mode(pool, MODEL, mode).await;
    common::set_model_api_key(pool, MODEL, "sk-test").await;
    common::bind_llm_model_to_rg(pool, MODEL, RG).await;
    common::seed_api_key(pool, T, RG, None, "f489-key").await
}

async fn send(
    state: &brutor_gateway_core::services::SharedState,
    req: Request<Body>,
) -> (StatusCode, String) {
    let resp = common::build_multimodal_app(state.clone())
        .oneshot(req)
        .await
        .expect("router");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.expect("body").to_bytes();
    (status, String::from_utf8_lossy(&bytes).into_owned())
}

/// The one request the provider received went to `path` with the base's
/// query as its query.
async fn assert_reached(mock: &MockServer, path: &str) {
    let requests = mock.received_requests().await.unwrap_or_default();
    assert_eq!(
        requests.len(),
        1,
        "one request to the provider: {requests:?}"
    );
    assert_eq!(requests[0].url.path(), path);
    assert_eq!(requests[0].url.query(), Some(QUERY));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn speech_keeps_the_query_of_the_api_url(pool: PgPool) {
    let mock = provider().await;
    let key = seed(&pool, "audio_speech", &mock).await;
    let state = common::build_test_state(pool.clone());
    let req = common::build_post_request(
        "/v1/proxy/llm/audio/speech",
        json!({"model": MODEL, "voice": "alloy", "input": "hello"}),
        Some(&key),
        Some(T),
    );
    let (status, body) = send(&state, req).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_reached(&mock, "/gw/v1/audio/speech").await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn images_keep_the_query_of_the_api_url(pool: PgPool) {
    let mock = provider().await;
    let key = seed(&pool, "image_generation", &mock).await;
    let state = common::build_test_state(pool.clone());
    let req = common::build_post_request(
        "/v1/proxy/llm/images/generations",
        json!({"model": MODEL, "prompt": "a red apple", "response_format": "b64_json"}),
        Some(&key),
        Some(T),
    );
    let (status, body) = send(&state, req).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_reached(&mock, "/gw/v1/images/generations").await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn transcription_keeps_the_query_of_the_api_url(pool: PgPool) {
    let mock = provider().await;
    let key = seed(&pool, "audio_transcription", &mock).await;
    let state = common::build_test_state(pool.clone());
    let b = "----brutorf489";
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
    let (status, body) = send(&state, req).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_reached(&mock, "/gw/v1/audio/transcriptions").await;
}

/// Video is accepted by the route and submitted by the pipeline's tick.
#[sqlx::test(migrations = "./tests/migrations")]
async fn video_keeps_the_query_of_the_api_url(pool: PgPool) {
    let mock = provider().await;
    let key = seed(&pool, "video_generation", &mock).await;
    let state = common::build_test_state(pool.clone());
    let req = common::build_post_request(
        "/v1/proxy/llm/video/generations",
        json!({"model": MODEL, "prompt": "a red apple", "duration_seconds": 5}),
        Some(&key),
        Some(T),
    );
    let (status, body) = send(&state, req).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    brutor_gateway_core::services::video_pipeline::run_once(
        &pool,
        state.media_storage.clone(),
        &state.http_client,
        &state.encryption,
    )
    .await;
    // The tick submits and then polls the job: both keep the query.
    let requests = mock.received_requests().await.unwrap_or_default();
    let reached: Vec<(String, Option<String>)> = requests
        .iter()
        .map(|r| (r.url.path().to_string(), r.url.query().map(str::to_string)))
        .collect();
    assert_eq!(
        reached,
        [
            ("/gw/videos".to_string(), Some(QUERY.to_string())),
            (
                "/gw/videos/video_query".to_string(),
                Some(QUERY.to_string())
            ),
        ]
    );
}
