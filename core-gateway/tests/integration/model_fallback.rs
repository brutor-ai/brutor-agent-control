// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! End-to-end tests for MODEL-LEVEL failover: a model's ordered
//! `fallback_models` chain is walked when the model's own upstream call fails.
//!
//! Each test wires two `wiremock` upstreams — a FAILING primary and a WORKING
//! fallback — seeds two `llm_model` rows pointing at them, sets
//! `primary.fallback_models = [fallback_id]`, then drives the real router and
//! asserts the answer came from the fallback. Also pins that the fallback
//! candidate's encrypted api_key must be decrypted before the upstream call
//! and the "don't fail over a 400" rule.

use axum::body::Body;
use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{header, method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

pub(crate) async fn body_to_json(body: Body) -> Value {
    let bytes = body.collect().await.expect("collect body").to_bytes();
    serde_json::from_slice(&bytes).unwrap_or(Value::Null)
}

pub(crate) fn chat_fixture(content: &str) -> Value {
    json!({
        "id": "chatcmpl-fb",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "fb",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    })
}

/// Seed the standard tenant/RG/user/api-key topology and return the api key.
pub(crate) async fn seed_topology(pool: &PgPool) -> String {
    common::seed_tenant(pool, "t1").await;
    common::seed_resource_group(pool, "rg1", "t1").await;
    common::seed_end_user(pool, 9001, "t1").await;
    common::add_direct_membership(pool, 9001, "rg1", "t1").await;
    common::seed_llm_provider(pool, "prov-openai", "t1", "openai").await;
    common::seed_api_key(pool, "t1", "rg1", Some(9001), "fb-key").await
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn chat_completion_fails_over_to_fallback_model(pool: PgPool) {
    // Primary upstream: always 500. Fallback upstream: 200 with a marker.
    let primary_up = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(500).set_body_json(json!({"error": "boom"})))
        .mount(&primary_up)
        .await;
    let fallback_up = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(chat_fixture("from the fallback model")),
        )
        .expect(1)
        .mount(&fallback_up)
        .await;

    let key = seed_topology(&pool).await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-primary",
        "t1",
        "prov-openai",
        "gpt-primary",
        &primary_up.uri(),
    )
    .await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-fallback",
        "t1",
        "prov-openai",
        "gpt-fallback",
        &fallback_up.uri(),
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "m-primary", "rg1").await;
    common::set_model_fallback_models(&pool, "m-primary", &["m-fallback"]).await;

    let app = common::build_llm_app(common::build_test_state(pool));
    let req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({"model_id": "m-primary", "messages": [{"role": "user", "content": "hi"}]}),
        Some(&key),
        Some("t1"),
    );
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    let jb = body_to_json(resp.into_body()).await;

    assert_eq!(
        status,
        StatusCode::OK,
        "primary 500 must fail over to the fallback; got {status} body={jb}"
    );
    assert_eq!(
        jb["choices"][0]["message"]["content"], "from the fallback model",
        "answer must come from the fallback model"
    );
    // fallback_up's `.expect(1)` asserts the fallback upstream was actually hit.
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn embeddings_fail_over_to_fallback_model(pool: PgPool) {
    let primary_up = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/embeddings"))
        .respond_with(ResponseTemplate::new(503).set_body_json(json!({"error": "down"})))
        .mount(&primary_up)
        .await;
    let fallback_up = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/embeddings"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.11, 0.22, 0.33]}],
            "model": "embed-fallback",
            "usage": {"prompt_tokens": 4, "total_tokens": 4}
        })))
        .expect(1)
        .mount(&fallback_up)
        .await;

    let key = seed_topology(&pool).await;
    common::seed_llm_model_with_mode(
        &pool,
        "e-primary",
        "t1",
        "prov-openai",
        "embed-primary",
        &primary_up.uri(),
        "embedding",
    )
    .await;
    common::seed_llm_model_with_mode(
        &pool,
        "e-fallback",
        "t1",
        "prov-openai",
        "embed-fallback",
        &fallback_up.uri(),
        "embedding",
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "e-primary", "rg1").await;
    common::set_model_fallback_models(&pool, "e-primary", &["e-fallback"]).await;

    let app = common::build_llm_app(common::build_test_state(pool));
    let req = common::build_post_request(
        "/v1/proxy/llm/embeddings",
        json!({"model_id": "e-primary", "input": "hello"}),
        Some(&key),
        Some("t1"),
    );
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    let jb = body_to_json(resp.into_body()).await;

    assert_eq!(
        status,
        StatusCode::OK,
        "embeddings must fail over; got {status} body={jb}"
    );
    assert_eq!(
        jb["data"][0]["embedding"][0], 0.11,
        "embedding must come from the fallback upstream"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn fallback_candidate_api_key_is_decrypted(pool: PgPool) {
    // The fallback upstream ONLY answers when it receives
    // the DECRYPTED bearer token. The fallback model's api_key is stored
    // Fernet-encrypted; if the router forwards the ciphertext, the header
    // matcher misses and the fallback never returns 200.
    let primary_up = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(500))
        .mount(&primary_up)
        .await;
    let fallback_up = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .and(header("authorization", "Bearer sk-fallback-plaintext"))
        .respond_with(ResponseTemplate::new(200).set_body_json(chat_fixture("decrypted ok")))
        .expect(1)
        .mount(&fallback_up)
        .await;

    let key = seed_topology(&pool).await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-primary",
        "t1",
        "prov-openai",
        "gpt-primary",
        &primary_up.uri(),
    )
    .await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-fallback",
        "t1",
        "prov-openai",
        "gpt-fallback",
        &fallback_up.uri(),
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "m-primary", "rg1").await;
    common::set_model_fallback_models(&pool, "m-primary", &["m-fallback"]).await;

    let state = common::build_test_state(pool.clone());
    // Store the fallback's api_key Fernet-encrypted (as the control plane does).
    let ciphertext = state
        .encryption
        .encrypt("sk-fallback-plaintext")
        .expect("encrypt fallback key");
    sqlx::query("UPDATE llm_model SET api_key = $1 WHERE id = 'm-fallback'")
        .bind(&ciphertext)
        .execute(&pool)
        .await
        .expect("set encrypted fallback key");

    let app = common::build_llm_app(state);
    let req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({"model_id": "m-primary", "messages": [{"role": "user", "content": "hi"}]}),
        Some(&key),
        Some("t1"),
    );
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    let jb = body_to_json(resp.into_body()).await;

    assert_eq!(
        status,
        StatusCode::OK,
        "fallback must authenticate with the DECRYPTED key; got {status} body={jb}"
    );
    assert_eq!(jb["choices"][0]["message"]["content"], "decrypted ok");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn no_failover_on_bad_request_400(pool: PgPool) {
    // A 400 is the caller's fault — every model rejects it, so we must NOT burn
    // the fallback. The fallback upstream asserts it is never called.
    let primary_up = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(
            ResponseTemplate::new(400).set_body_json(json!({"error": {"message": "bad request"}})),
        )
        .mount(&primary_up)
        .await;
    let fallback_up = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(chat_fixture("should not be reached")),
        )
        .expect(0)
        .mount(&fallback_up)
        .await;

    let key = seed_topology(&pool).await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-primary",
        "t1",
        "prov-openai",
        "gpt-primary",
        &primary_up.uri(),
    )
    .await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-fallback",
        "t1",
        "prov-openai",
        "gpt-fallback",
        &fallback_up.uri(),
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "m-primary", "rg1").await;
    common::set_model_fallback_models(&pool, "m-primary", &["m-fallback"]).await;

    let app = common::build_llm_app(common::build_test_state(pool));
    let req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({"model_id": "m-primary", "messages": [{"role": "user", "content": "hi"}]}),
        Some(&key),
        Some("t1"),
    );
    let resp = app.oneshot(req).await.expect("router call");
    assert_eq!(
        resp.status(),
        StatusCode::BAD_REQUEST,
        "a 400 must be surfaced, not failed over"
    );
    // fallback_up's `.expect(0)` asserts it was never called.
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn chat_streaming_fails_over_to_fallback_model(pool: PgPool) {
    // Streaming can only fail over BEFORE the stream starts: primary errors on
    // the initial handshake (500), fallback opens the stream (200).
    let primary_up = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(500))
        .mount(&primary_up)
        .await;
    let fallback_up = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(
            ResponseTemplate::new(200)
                .insert_header("content-type", "text/event-stream")
                .set_body_string(
                    "data: {\"choices\":[{\"delta\":{\"content\":\"hi\"}}]}\n\ndata: [DONE]\n\n",
                ),
        )
        .expect(1)
        .mount(&fallback_up)
        .await;

    let key = seed_topology(&pool).await;
    common::seed_llm_model_with_api_url(
        &pool,
        "s-primary",
        "t1",
        "prov-openai",
        "gpt-primary",
        &primary_up.uri(),
    )
    .await;
    common::seed_llm_model_with_api_url(
        &pool,
        "s-fallback",
        "t1",
        "prov-openai",
        "gpt-fallback",
        &fallback_up.uri(),
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "s-primary", "rg1").await;
    common::set_model_fallback_models(&pool, "s-primary", &["s-fallback"]).await;

    let app = common::build_llm_app(common::build_test_state(pool));
    let req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({"model_id": "s-primary", "stream": true, "messages": [{"role": "user", "content": "hi"}]}),
        Some(&key),
        Some("t1"),
    );
    let resp = app.oneshot(req).await.expect("router call");
    assert_eq!(
        resp.status(),
        StatusCode::OK,
        "streaming must fail over on the initial handshake"
    );
    // fallback_up's `.expect(1)` asserts the fallback stream was opened.
}

// ---------------------------------------------------------------------------
// The media surface, called with an API key. The
// Portal-session variants are the Enterprise suite's `portal_model_fallback.rs`.
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_keys_audio_speech_fails_over_to_fallback_model(pool: PgPool) {
    let primary_up = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/v1/audio/speech"))
        .respond_with(ResponseTemplate::new(500))
        .mount(&primary_up)
        .await;
    let fallback_up = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/v1/audio/speech"))
        .respond_with(ResponseTemplate::new(200).set_body_bytes(vec![0x49, 0x44, 0x33, 0x04])) // "ID3" mp3 magic
        .expect(1)
        .mount(&fallback_up)
        .await;

    seed_topology(&pool).await; // tenant/rg/user/membership/provider
    seed_media_model(&pool, "tts-primary", &primary_up.uri(), "audio_speech").await;
    seed_media_model(&pool, "tts-fallback", &fallback_up.uri(), "audio_speech").await;
    common::bind_llm_model_to_rg(&pool, "tts-primary", "rg1").await;
    common::set_model_fallback_models(&pool, "tts-primary", &["tts-fallback"]).await;

    // An API key bound to the group.
    let key = common::seed_api_key(&pool, "t1", "rg1", None, "k-tts").await;
    let app = common::build_multimodal_app(common::build_test_state(pool));
    let req = common::build_post_request(
        "/v1/proxy/llm/audio/speech",
        json!({"model": "tts-primary", "voice": "nova", "input": "hello"}),
        Some(&key),
        Some("t1"),
    );
    let resp = app.oneshot(req).await.expect("router call");
    let st = resp.status();
    let jb = body_to_json(resp.into_body()).await;
    assert_eq!(st, StatusCode::OK, "TTS must fail over; got {st} body={jb}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_keys_image_generation_fails_over_to_fallback_model(pool: PgPool) {
    let primary_up = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/v1/images/generations"))
        .respond_with(ResponseTemplate::new(500))
        .mount(&primary_up)
        .await;
    let fallback_up = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/v1/images/generations"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "created": 1_700_000_000,
            "data": [{"b64_json": "aGVsbG8="}]
        })))
        .expect(1)
        .mount(&fallback_up)
        .await;

    seed_topology(&pool).await;
    seed_media_model(&pool, "img-primary", &primary_up.uri(), "image_generation").await;
    seed_media_model(
        &pool,
        "img-fallback",
        &fallback_up.uri(),
        "image_generation",
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "img-primary", "rg1").await;
    common::set_model_fallback_models(&pool, "img-primary", &["img-fallback"]).await;

    let key = common::seed_api_key(&pool, "t1", "rg1", None, "k-img").await;
    let app = common::build_multimodal_app(common::build_test_state(pool));
    let req = common::build_post_request(
        "/v1/proxy/llm/images/generations",
        json!({"model": "img-primary", "prompt": "a cat", "response_format": "b64_json"}),
        Some(&key),
        Some("t1"),
    );
    let resp = app.oneshot(req).await.expect("router call");
    let st = resp.status();
    let jb = body_to_json(resp.into_body()).await;
    assert_eq!(
        st,
        StatusCode::OK,
        "image generation must fail over; got {st} body={jb}"
    );
}

/// Seed a media model (image/audio) pointing at `api_url` with a `mode` + key.
pub(crate) async fn seed_media_model(pool: &PgPool, id: &str, api_url: &str, mode: &str) {
    common::seed_llm_model_with_api_url(pool, id, "t1", "prov-openai", id, api_url).await;
    common::set_model_mode(pool, id, mode).await;
    common::set_model_api_key(pool, id, "sk-test").await;
}

// ---------------------------------------------------------------------------
// A routing group's `stream_timeout`: the longest a streamed call waits for
// the first chunk of the member it was routed to
// ---------------------------------------------------------------------------

/// A routing group `rgs` with one member (`st-slow`, at `slow`) whose own
/// fallback is `st-fast` (at `fast`); returns the caller's key.
async fn seed_stream_timeout_topology(pool: &PgPool, slow: &str, fast: &str) -> String {
    let key = seed_topology(pool).await;
    common::seed_routing_group(pool, "rgs", "t1", "prov-openai", "simple").await;
    common::bind_llm_model_to_rg(pool, "rg-rgs", "rg1").await;
    common::seed_llm_model_with_api_url(pool, "st-slow", "t1", "prov-openai", "gpt-slow", slow)
        .await;
    common::seed_llm_model_with_api_url(pool, "st-fast", "t1", "prov-openai", "gpt-fast", fast)
        .await;
    sqlx::query("UPDATE llm_model SET routing_group_id = 'rgs' WHERE id = 'st-slow'")
        .execute(pool)
        .await
        .expect("member of the routing group");
    common::bind_llm_model_to_rg(pool, "st-slow", "rg1").await;
    common::bind_llm_model_to_rg(pool, "st-fast", "rg1").await;
    common::set_model_fallback_models(pool, "st-slow", &["st-fast"]).await;
    key
}

fn sse_answer() -> ResponseTemplate {
    ResponseTemplate::new(200)
        .insert_header("content-type", "text/event-stream")
        .set_body_string(
            "data: {\"choices\":[{\"delta\":{\"content\":\"hi\"}}]}\n\ndata: [DONE]\n\n",
        )
}

async fn stream_through_the_group(pool: PgPool, key: &str) -> (StatusCode, std::time::Duration) {
    let app = common::build_llm_app(common::build_test_state(pool));
    let req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({"model_id": "rg-rgs", "stream": true,
               "messages": [{"role": "user", "content": "hi"}]}),
        Some(key),
        Some("t1"),
    );
    let started = std::time::Instant::now();
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    let _ = axum::body::to_bytes(resp.into_body(), 1 << 20).await;
    (status, started.elapsed())
}

/// A member that sends no first chunk within the group's `stream_timeout`
/// is given up on, and the call is served by the next model.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_member_that_sends_no_first_chunk_in_time_is_given_up_on(pool: PgPool) {
    let slow = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(sse_answer().set_delay(std::time::Duration::from_secs(6)))
        .mount(&slow)
        .await;
    let fast = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(sse_answer())
        .mount(&fast)
        .await;
    let key = seed_stream_timeout_topology(&pool, &slow.uri(), &fast.uri()).await;
    sqlx::query("UPDATE llm_routing_group SET stream_timeout = 1.0 WHERE id = 'rgs'")
        .execute(&pool)
        .await
        .expect("stream_timeout");

    let (status, took) = stream_through_the_group(pool, &key).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        fast.received_requests().await.unwrap_or_default().len(),
        1,
        "the call was not handed to the next model"
    );
    assert!(
        took < std::time::Duration::from_secs(5),
        "the call waited {took:?} for a member the group gives 1 s"
    );
}

/// With no `stream_timeout` the member is waited for, as before.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_group_without_a_stream_timeout_waits_for_its_member(pool: PgPool) {
    let slow = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(sse_answer().set_delay(std::time::Duration::from_secs(2)))
        .mount(&slow)
        .await;
    let fast = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(sse_answer())
        .mount(&fast)
        .await;
    let key = seed_stream_timeout_topology(&pool, &slow.uri(), &fast.uri()).await;
    let (status, _) = stream_through_the_group(pool, &key).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(fast.received_requests().await.unwrap_or_default().len(), 0);
    assert_eq!(slow.received_requests().await.unwrap_or_default().len(), 1);
}
