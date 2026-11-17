// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Per-mode multimodal guardrail surfaces:
//!   * `audio_stt_output` — transcript scanned on `/audio/transcriptions`
//!   * `image_gen_output` — `revised_prompt` scanned on `/images/generations`
//!   * `audio_tts_input`  — input text scanned on `/audio/speech`
//!
//! These pin the surface-token wiring for the multimodal routes (the same bug
//! class as the chat_output surface-token fix). All use the OpenAI adapter
//! (provider key = "openai") against a wiremock; banned-words is in-process so
//! no external detector call is made.

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

async fn body_to_string(body: Body) -> String {
    let bytes = body.collect().await.expect("collect body").to_bytes();
    String::from_utf8_lossy(&bytes).to_string()
}

/// The caller is an API key bound to `rg-mm`, so these run
/// on a Community gateway; the group's guardrails govern the key's calls. The
/// Portal-session variants are the Enterprise suite's `multimodal_guardrails.rs`.
///
/// Seed tenant/RG/provider(openai)/model(api_url=mock, given mode, api_key
/// set)/binding, and return the key.
async fn seed_multimodal(
    pool: &PgPool,
    tenant: &str,
    model_id: &str,
    mode: &str,
    mock_uri: &str,
) -> String {
    common::seed_tenant(pool, tenant).await;
    common::seed_resource_group(pool, "rg-mm", tenant).await;
    common::seed_llm_provider(pool, "prov-mm", tenant, "openai").await;
    common::seed_llm_model_with_api_url(pool, model_id, tenant, "prov-mm", "mm-model", mock_uri)
        .await;
    common::set_model_mode(pool, model_id, mode).await;
    common::set_model_api_key(pool, model_id, "sk-test").await;
    common::bind_llm_model_to_rg(pool, model_id, "rg-mm").await;
    common::seed_api_key(pool, tenant, "rg-mm", None, "k-mm").await
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn stt_output_guardrail_blocks_banned_transcript(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/v1/audio/transcriptions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "text": "my forbidden-stt-secret data",
            "duration": 1.0
        })))
        .expect(1)
        .mount(&mock)
        .await;

    let key = seed_multimodal(
        &pool,
        "tenant-stt",
        "m-stt",
        "audio_transcription",
        &mock.uri(),
    )
    .await;
    common::seed_banned_words_guardrail_surfaces(
        &pool,
        "gr-stt",
        "tenant-stt",
        &["forbidden-stt-secret"],
        &["audio_stt_output"],
    )
    .await;
    common::bind_guardrail_to_rg(&pool, "gr-stt", "rg-mm").await;

    let state = common::build_test_state(pool.clone());
    let app = common::build_multimodal_app(state);

    // multipart/form-data: a 1-byte `file` part + `model` part.
    let boundary = "----brutortestboundary";
    let body = format!(
        "--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.mp3\"\r\n\
         Content-Type: application/octet-stream\r\n\r\n\x00\r\n\
         --{b}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nm-stt\r\n\
         --{b}--\r\n",
        b = boundary
    );
    let mut req = Request::builder()
        .method("POST")
        .uri("/v1/proxy/llm/audio/transcriptions")
        .header("X-Tenant-ID", "tenant-stt")
        .header("authorization", format!("Bearer {key}"))
        .header(
            "content-type",
            format!("multipart/form-data; boundary={boundary}"),
        )
        .body(Body::from(body))
        .expect("build multipart request");
    req.extensions_mut().insert(ConnectInfo(
        "127.0.0.1:55555".parse::<std::net::SocketAddr>().unwrap(),
    ));

    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let out = body_to_string(response.into_body()).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "audio_stt_output guardrail must block a banned transcript; got {status}, body={out}"
    );
    assert!(
        !out.contains("forbidden-stt-secret data"),
        "banned transcript leaked: {out}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn image_gen_output_guardrail_blocks_banned_revised_prompt(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/v1/images/generations"))
        // `b64_json` required by the handler; `UE5H` = base64("PNG").
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "data": [{"b64_json": "UE5H", "revised_prompt": "a forbidden-img-secret painting"}]
        })))
        .expect(1)
        .mount(&mock)
        .await;

    let key = seed_multimodal(
        &pool,
        "tenant-img",
        "m-img",
        "image_generation",
        &mock.uri(),
    )
    .await;
    common::seed_banned_words_guardrail_surfaces(
        &pool,
        "gr-img",
        "tenant-img",
        &["forbidden-img-secret"],
        &["image_gen_output"],
    )
    .await;
    common::bind_guardrail_to_rg(&pool, "gr-img", "rg-mm").await;

    let state = common::build_test_state(pool.clone());
    let app = common::build_multimodal_app(state);

    let req = common::build_post_request(
        "/v1/proxy/llm/images/generations",
        json!({"model": "m-img", "prompt": "a cat", "n": 1, "size": "1024x1024"}),
        Some(&key),
        Some("tenant-img"),
    );

    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let out = body_to_string(response.into_body()).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "image_gen_output guardrail must block a banned revised_prompt; got {status}, body={out}"
    );
    assert!(
        !out.contains("forbidden-img-secret painting"),
        "banned revised_prompt leaked: {out}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn tts_input_guardrail_blocks_banned_input(pool: PgPool) {
    // No upstream call expected — the input guardrail blocks in preflight,
    // before provider dispatch.
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/v1/audio/speech"))
        .respond_with(ResponseTemplate::new(200))
        .expect(0)
        .mount(&mock)
        .await;

    let key = seed_multimodal(&pool, "tenant-tts", "m-tts", "audio_speech", &mock.uri()).await;
    common::seed_banned_words_guardrail_surfaces(
        &pool,
        "gr-tts",
        "tenant-tts",
        &["forbidden-tts-secret"],
        &["audio_tts_input"],
    )
    .await;
    common::bind_guardrail_to_rg(&pool, "gr-tts", "rg-mm").await;

    let state = common::build_test_state(pool.clone());
    let app = common::build_multimodal_app(state);

    let req = common::build_post_request(
        "/v1/proxy/llm/audio/speech",
        json!({"model": "m-tts", "voice": "alloy", "input": "please say forbidden-tts-secret aloud"}),
        Some(&key),
        Some("tenant-tts"),
    );

    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let out = body_to_string(response.into_body()).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "audio_tts_input guardrail must block banned input; got {status}, body={out}"
    );

    drop(mock);
}
