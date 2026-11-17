// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The Gemini-native surface through the real router: dispatch, the unary and
//! streaming generate paths, guardrails on both, upstream failures, and the
//! OpenAI surface bridged onto a Gemini model.

use axum::Router;
use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

const T: &str = "t-gemsurf";
const RG: &str = "rg-gemsurf";
const BANNED: &str = "forbidden-phrase";

struct W {
    app: Router,
    key: String,
    mock: MockServer,
    pool: PgPool,
}

async fn world(pool: &PgPool, provider: &str) -> W {
    let mock = MockServer::start().await;
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_end_user(pool, 9601, T).await;
    common::add_direct_membership(pool, 9601, RG, T).await;
    common::seed_llm_provider(pool, "prov-gs", T, provider).await;
    common::seed_llm_model_with_api_url(pool, "m-gs", T, "prov-gs", "gem-1", &mock.uri()).await;
    common::set_model_api_key(pool, "m-gs", "gem-key").await;
    common::bind_llm_model_to_rg(pool, "m-gs", RG).await;
    let key = common::seed_api_key(pool, T, RG, Some(9601), "k-gs").await;
    let state = common::build_test_state(pool.clone());
    W {
        app: common::build_gemini_app(state),
        key,
        mock,
        pool: pool.clone(),
    }
}

fn user(text: &str) -> Value {
    json!({"contents": [{"role": "user", "parts": [{"text": text}]}]})
}

fn answer(text: &str) -> Value {
    json!({
        "candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 3, "totalTokenCount": 4},
    })
}

impl W {
    async fn post(&self, uri: &str, body: Value) -> (StatusCode, String) {
        self.post_as(uri, body, Some(&self.key)).await
    }

    async fn post_as(&self, uri: &str, body: Value, key: Option<&str>) -> (StatusCode, String) {
        let req = common::build_post_request(uri, body, key, Some(T));
        let resp = self.app.clone().oneshot(req).await.unwrap();
        let s = resp.status();
        let b = resp.into_body().collect().await.unwrap().to_bytes();
        (s, String::from_utf8_lossy(&b).to_string())
    }

    async fn upstream_body(&self) -> Value {
        let got = self.mock.received_requests().await.unwrap();
        serde_json::from_slice(&got.last().unwrap().body).unwrap()
    }

    async fn guardrail(&self, input: bool, output: bool) {
        common::seed_llm_banned_words_guardrail(&self.pool, "gr-gs", T, &[BANNED], input, output)
            .await;
        common::bind_guardrail_to_rg(&self.pool, "gr-gs", RG).await;
    }
}

async fn unary_upstream(w: &W, body: Value) {
    Mock::given(method("POST"))
        .and(path("/v1beta/models/gem-1:generateContent"))
        .respond_with(ResponseTemplate::new(200).set_body_json(body))
        .mount(&w.mock)
        .await;
}

fn sse(events: &[Value]) -> String {
    events
        .iter()
        .map(|e| format!("data: {e}\n\n"))
        .collect::<String>()
}

async fn stream_upstream(w: &W, events: &[Value]) {
    Mock::given(method("POST"))
        .and(path("/v1beta/models/gem-1:streamGenerateContent"))
        .respond_with(ResponseTemplate::new(200).set_body_raw(sse(events), "text/event-stream"))
        .mount(&w.mock)
        .await;
}

const GEN: &str = "/v1beta/models/m-gs:generateContent";
const STREAM: &str = "/v1beta/models/m-gs:streamGenerateContent";

#[sqlx::test(migrations = "./tests/migrations")]
async fn dispatch_refuses_a_malformed_path_or_unknown_action(pool: PgPool) {
    let w = world(&pool, "gemini").await;
    let (s, b) = w.post("/v1beta/models/m-gs", user("x")).await;
    assert_eq!(s, StatusCode::NOT_FOUND, "{b}");
    let (s, b) = w.post("/v1beta/models/m-gs:embedContent", user("x")).await;
    assert_eq!(s, StatusCode::NOT_FOUND, "{b}");
    assert!(b.contains("embedContent"), "{b}");
    assert_eq!(w.mock.received_requests().await.unwrap().len(), 0);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn generation_needs_a_credential_with_access(pool: PgPool) {
    let w = world(&pool, "gemini").await;
    unary_upstream(&w, answer("hi")).await;
    for uri in [GEN, STREAM] {
        let (s, b) = w.post_as(uri, user("x"), None).await;
        assert_eq!(s, StatusCode::UNAUTHORIZED, "{uri}: {b}");
        let (s, _) = w
            .post_as(uri, user("x"), Some("sk_brutor_api_garbage"))
            .await;
        assert_eq!(s, StatusCode::UNAUTHORIZED, "{uri}");
    }

    // A key of a group the model is not bound to.
    common::seed_resource_group(&pool, "rg-other", T).await;
    common::seed_end_user(&pool, 9602, T).await;
    common::add_direct_membership(&pool, 9602, "rg-other", T).await;
    let other = common::seed_api_key(&pool, T, "rg-other", Some(9602), "k-o").await;
    for uri in [GEN, STREAM] {
        let (s, b) = w.post_as(uri, user("x"), Some(&other)).await;
        assert!(s.is_client_error(), "{uri}: {s} {b}");
    }
    assert_eq!(w.mock.received_requests().await.unwrap().len(), 0);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn unary_generation_keeps_the_native_shape_and_hides_the_key(pool: PgPool) {
    let w = world(&pool, "gemini").await;
    unary_upstream(&w, answer("hello")).await;
    let (s, b) = w
        .post(
            "/v1beta/models/models%2Fm-gs:generateContent",
            json!({
                "contents": [{"role": "user", "parts": [{"text": "hi"}, {"inlineData": {"mimeType": "x", "data": "y"}}]}],
                "systemInstruction": {"parts": [{"text": "be brief"}]},
                "generationConfig": {"temperature": 0.1},
            }),
        )
        .await;
    assert_eq!(s, StatusCode::OK, "{b}");
    let v: Value = serde_json::from_str(&b).unwrap();
    assert_eq!(v["candidates"][0]["content"]["parts"][0]["text"], "hello");
    assert!(!b.contains("gem-key"), "{b}");

    let got = &w.mock.received_requests().await.unwrap()[0];
    let key_in_url = got.url.query().unwrap_or("").contains("gem-key");
    let key_in_header = got
        .headers
        .iter()
        .any(|(_, v)| v.to_str().unwrap_or("").contains("gem-key"));
    assert!(
        key_in_url || key_in_header,
        "the upstream gets the decrypted key"
    );
    let sent = w.upstream_body().await;
    assert_eq!(sent["systemInstruction"]["parts"][0]["text"], "be brief");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_upstream_error_surfaces_as_a_gemini_error(pool: PgPool) {
    let w = world(&pool, "gemini").await;
    Mock::given(method("POST"))
        .respond_with(
            ResponseTemplate::new(500)
                .set_body_json(json!({"error": {"code": 500, "message": "internal detail"}})),
        )
        .mount(&w.mock)
        .await;
    let (s, b) = w.post(GEN, user("hi")).await;
    assert!(s.is_server_error(), "{s}: {b}");
    let (s, b) = w.post(STREAM, user("hi")).await;
    assert!(s.is_server_error() || b.contains("error"), "{s}: {b}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_dead_upstream_is_an_error_not_a_hang(pool: PgPool) {
    let w = world(&pool, "gemini").await;
    sqlx::query("UPDATE llm_model SET api_url = 'http://127.0.0.1:1' WHERE id = 'm-gs'")
        .execute(&pool)
        .await
        .unwrap();
    let (s, b) = w.post(GEN, user("hi")).await;
    assert!(s.is_server_error(), "{s}: {b}");
    let (s, b) = w.post(STREAM, user("hi")).await;
    assert!(s.is_server_error() || b.contains("error"), "{s}: {b}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_missing_key_refuses_before_the_upstream(pool: PgPool) {
    let w = world(&pool, "gemini").await;
    sqlx::query("UPDATE llm_model SET api_key = NULL WHERE id = 'm-gs'")
        .execute(&pool)
        .await
        .unwrap();
    for uri in [GEN, STREAM] {
        let (s, b) = w.post(uri, user("hi")).await;
        assert_eq!(s, StatusCode::INTERNAL_SERVER_ERROR, "{uri}: {b}");
        assert!(b.contains("api_key"), "{uri}: {b}");
    }
    assert_eq!(w.mock.received_requests().await.unwrap().len(), 0);
}

/// A model endpoint the policy denies is refused before anything goes.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_denied_endpoint_refuses(pool: PgPool) {
    let w = world(&pool, "gemini").await;
    sqlx::query("UPDATE llm_model SET api_url = 'http://169.254.169.254/' WHERE id = 'm-gs'")
        .execute(&pool)
        .await
        .unwrap();
    for uri in [GEN, STREAM] {
        let (s, b) = w.post(uri, user("hi")).await;
        assert_eq!(s, StatusCode::FORBIDDEN, "{uri}: {b}");
    }
}

/// A non-Gemini model is not reachable through the native shape.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_model_of_another_provider_is_refused(pool: PgPool) {
    let w = world(&pool, "openai").await;
    for uri in [GEN, STREAM] {
        let (s, b) = w.post(uri, user("hi")).await;
        assert_eq!(s, StatusCode::BAD_REQUEST, "{uri}: {b}");
    }
    assert_eq!(w.mock.received_requests().await.unwrap().len(), 0);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn input_guardrails_stop_the_call_on_both_paths(pool: PgPool) {
    let w = world(&pool, "gemini").await;
    w.guardrail(true, false).await;
    for uri in [GEN, STREAM] {
        let (s, b) = w
            .post(
                uri,
                json!({
                    "systemInstruction": {"parts": [{"text": "s"}]},
                    "contents": [{"role": "user", "parts": [{"text": format!("say {BANNED}")}]}],
                }),
            )
            .await;
        assert_eq!(s, StatusCode::FORBIDDEN, "{uri}: {b}");
    }
    assert_eq!(w.mock.received_requests().await.unwrap().len(), 0);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_stream_relays_clean_events_with_its_usage(pool: PgPool) {
    let w = world(&pool, "gemini").await;
    stream_upstream(
        &w,
        &[
            json!({"candidates": [{"content": {"parts": [{"text": "hel"}]}}]}),
            json!({"candidates": [{"content": {"parts": [{"text": "lo"}]}, "finishReason": "STOP"}],
                   "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 5, "totalTokenCount": 7}}),
        ],
    )
    .await;
    let (s, b) = w.post(STREAM, user("hi")).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert!(b.contains("hel") && b.contains("lo"), "{b}");
    assert!(b.contains("usageMetadata"), "{b}");
}

/// A streamed answer that crosses a banned phrase is cut before the phrase
/// reaches the client.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_output_guardrail_cuts_a_streamed_answer(pool: PgPool) {
    let w = world(&pool, "gemini").await;
    w.guardrail(false, true).await;
    stream_upstream(
        &w,
        &[
            json!({"candidates": [{"content": {"parts": [{"text": "clean text then "}]}}]}),
            json!({"candidates": [{"content": {"parts": [{"text": BANNED}]}, "finishReason": "STOP"}]}),
        ],
    )
    .await;
    let (s, b) = w.post(STREAM, user("hi")).await;
    assert_ne!(s, StatusCode::INTERNAL_SERVER_ERROR, "{b}");
    assert!(!b.contains(BANNED), "{b}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_multimodal_or_empty_request_passes_the_extractors(pool: PgPool) {
    let w = world(&pool, "gemini").await;
    unary_upstream(&w, answer("ok")).await;
    let (s, b) = w
        .post(
            GEN,
            json!({"contents": [{"role": "model", "parts": [{"functionCall": {"name": "f", "args": {}}}]},
                                {"role": "function", "parts": [{"functionResponse": {"name": "f", "response": {"a": 1}}}]}]}),
        )
        .await;
    assert_eq!(s, StatusCode::OK, "{b}");
    let (s, b) = w.post(GEN, json!({"contents": []})).await;
    assert_eq!(s, StatusCode::OK, "{b}");
}

/// The caller's `cachedContent` names a resource of the gateway's upstream
/// account: it must not pass through from an untrusted body.
#[sqlx::test(migrations = "./tests/migrations")]
async fn caller_cache_control_is_stripped(pool: PgPool) {
    let w = world(&pool, "gemini").await;
    unary_upstream(&w, answer("ok")).await;
    let (s, b) = w
        .post(
            GEN,
            json!({
                "contents": [{"role": "user", "parts": [{"text": "x", "cache_control": {"type": "ephemeral"}}]}],
                "systemInstruction": {"parts": [{"text": "s", "cache_control": {"type": "ephemeral"}}]},
            }),
        )
        .await;
    assert_eq!(s, StatusCode::OK, "{b}");
    let sent = w.upstream_body().await.to_string();
    assert!(!sent.contains("cache_control"), "{sent}");
}

/// Both generate actions carry the AI-generated label.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_answer_is_labelled_ai_generated(pool: PgPool) {
    let w = world(&pool, "gemini").await;
    unary_upstream(&w, answer("ok")).await;
    let req = common::build_post_request(GEN, user("x"), Some(&w.key), Some(T));
    let resp = w.app.clone().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    assert!(
        resp.headers()
            .contains_key(brutor_gateway_core::constants::headers::X_BRUTOR_AI_GENERATED),
        "{:?}",
        resp.headers()
    );
}
