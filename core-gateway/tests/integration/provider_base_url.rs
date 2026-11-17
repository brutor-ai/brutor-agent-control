// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Where a request may go.
//!
//! **No default base.** A model row with no `api_url`, of a provider the gateway knows
//! no base URL for, is never sent to `https://api.openai.com/v1`, where the prompt and
//! the row's credential would go to OpenAI. The rule: a request goes to the
//! row's own `api_url` or to the one known base of the row's own provider;
//! a row with neither is refused at dispatch (501 `provider_api_url_missing`,
//! an audit row), before any connection.
//!
//! No request leaves this machine: the gateway under test is built with an
//! HTTP client whose every connection goes to a recorder on loopback (an
//! HTTP proxy that answers 502). What the gateway tried to reach is the
//! recorder's log: `CONNECT api.openai.com:443` is the fallback.
//!
//! **Fallbacks.** A fallback candidate the gateway cannot dispatch to (a
//! non-Claude Bedrock row) is not sent anywhere, on every loop that walks
//! candidates.

use std::sync::{Arc, Mutex};

use axum::body::Body;
use axum::extract::ConnectInfo;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::json;
use sqlx::PgPool;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::config::AppConfig;
use brutor_gateway_core::services::telemetry::TelemetryState;
use brutor_gateway_core::services::{AppState, SharedState};

use crate::common;

const T: &str = "tenant-f525";
const RG: &str = "rg-f525";
const MODEL: &str = "m-f525";
/// A provider the gateway has no adapter and no base URL for: a self-hosted
/// OpenAI-compatible server registered under its own name.
const PROVIDER: &str = "selfhosted-f525-9c41e7";
/// The row's credential. It may appear nowhere outside the gateway.
const CREDENTIAL: &str = "sk-f525-credential-7d3b0a5e91c4";

/// Every destination the gateway's HTTP client tried to reach.
struct Egress {
    addr: std::net::SocketAddr,
    seen: Arc<Mutex<Vec<String>>>,
}

impl Egress {
    async fn start() -> Self {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind the egress recorder");
        let addr = listener.local_addr().expect("recorder address");
        let seen = Arc::new(Mutex::new(Vec::new()));
        let log = seen.clone();
        tokio::spawn(async move {
            loop {
                let Ok((mut socket, _)) = listener.accept().await else {
                    return;
                };
                let log = log.clone();
                tokio::spawn(async move {
                    let mut buf = Vec::new();
                    let mut chunk = [0u8; 4096];
                    while !buf.windows(4).any(|w| w == b"\r\n\r\n") && buf.len() < 65_536 {
                        match socket.read(&mut chunk).await {
                            Ok(0) | Err(_) => break,
                            Ok(n) => buf.extend_from_slice(&chunk[..n]),
                        }
                    }
                    let head = String::from_utf8_lossy(&buf);
                    // `CONNECT host:443 HTTP/1.1` or `POST http://host/path HTTP/1.1`.
                    let line = head.lines().next().unwrap_or_default().to_string();
                    let carried_credential = head.contains(CREDENTIAL);
                    log.lock().expect("egress log").push(if carried_credential {
                        format!("{line} [with the row's credential]")
                    } else {
                        line
                    });
                    let _ = socket
                        .write_all(
                            b"HTTP/1.1 502 Bad Gateway\r\ncontent-length: 0\r\nconnection: close\r\n\r\n",
                        )
                        .await;
                    let _ = socket.shutdown().await;
                });
            }
        });
        Self { addr, seen }
    }

    fn seen(&self) -> Vec<String> {
        self.seen.lock().expect("egress log").clone()
    }

    /// The gateway of the edition under test, every outbound connection of
    /// its HTTP client going to this recorder.
    fn state(&self, pool: PgPool) -> SharedState {
        let client = reqwest::Client::builder()
            .proxy(reqwest::Proxy::all(format!("http://{}", self.addr)).expect("proxy"))
            .build()
            .expect("client");
        let state = AppState::new(
            AppConfig::for_test("postgresql://placeholder/replaced".to_string()),
            pool,
            None,
            client,
            TelemetryState {
                prometheus_handle: None,
            },
        );
        common::compose(state, crate::composition::extensions()).state
    }
}

/// A model of `provider` with a credential and NO `api_url`, bound to the
/// caller's group. Returns the caller's key.
async fn seed_row_without_api_url(pool: &PgPool, provider: &str, mode: &str) -> String {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_llm_provider(pool, "prov-f525", T, provider).await;
    common::seed_llm_model_with_mode(pool, MODEL, T, "prov-f525", MODEL, "x", mode).await;
    sqlx::query("UPDATE llm_model SET api_url = NULL WHERE id = $1")
        .bind(MODEL)
        .execute(pool)
        .await
        .expect("clear api_url");
    common::set_model_api_key(pool, MODEL, CREDENTIAL).await;
    common::bind_llm_model_to_rg(pool, MODEL, RG).await;
    common::seed_api_key(pool, T, RG, None, "f525-key").await
}

async fn answer(resp: axum::response::Response) -> (StatusCode, String) {
    let status = resp.status();
    let bytes = resp.into_body().collect().await.expect("body").to_bytes();
    (status, String::from_utf8_lossy(&bytes).into_owned())
}

fn transcription_request(key: &str, model: &str, tenant: &str) -> Request<Body> {
    let b = "----brutorf525";
    let body = format!(
        "--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.mp3\"\r\n\
         Content-Type: application/octet-stream\r\n\r\n\x00\r\n\
         --{b}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{model}\r\n\
         --{b}--\r\n"
    );
    let mut req = Request::builder()
        .method("POST")
        .uri("/v1/proxy/llm/audio/transcriptions")
        .header("content-type", format!("multipart/form-data; boundary={b}"))
        .header("X-Tenant-ID", tenant)
        .header("authorization", format!("Bearer {key}"))
        .body(Body::from(body))
        .expect("multipart request");
    req.extensions_mut().insert(ConnectInfo(
        "127.0.0.1:55555".parse::<std::net::SocketAddr>().unwrap(),
    ));
    req
}

#[derive(Clone, Copy, Debug)]
enum Surface {
    Chat,
    ChatStream,
    Responses,
    Completions,
    Embeddings,
    AnthropicMessages,
    Images,
    Speech,
    Transcription,
    Video,
}

impl Surface {
    fn mode(self) -> &'static str {
        match self {
            Surface::Embeddings => "embedding",
            Surface::Images => "image_generation",
            Surface::Speech => "audio_speech",
            Surface::Transcription => "audio_transcription",
            Surface::Video => "video_generation",
            _ => "chat",
        }
    }

    async fn call(self, state: &SharedState, key: &str) -> (StatusCode, String) {
        let chat =
            json!({"model_id": MODEL, "messages": [{"role": "user", "content": "the prompt"}]});
        let (app, req) = match self {
            Surface::Chat => (
                common::build_llm_app(state.clone()),
                common::build_post_request(
                    "/v1/proxy/llm/chat/completions",
                    chat,
                    Some(key),
                    Some(T),
                ),
            ),
            Surface::ChatStream => {
                let mut body = chat;
                body["stream"] = json!(true);
                (
                    common::build_llm_app(state.clone()),
                    common::build_post_request(
                        "/v1/proxy/llm/chat/completions",
                        body,
                        Some(key),
                        Some(T),
                    ),
                )
            }
            Surface::Responses => (
                common::build_llm_app(state.clone()),
                common::build_post_request(
                    "/v1/proxy/llm/responses",
                    json!({"model_id": MODEL, "input": "the prompt"}),
                    Some(key),
                    Some(T),
                ),
            ),
            Surface::Completions => (
                common::build_llm_app(state.clone()),
                common::build_post_request(
                    "/v1/proxy/llm/completions",
                    json!({"model_id": MODEL, "prompt": "the prompt"}),
                    Some(key),
                    Some(T),
                ),
            ),
            Surface::Embeddings => (
                common::build_llm_app(state.clone()),
                common::build_post_request(
                    "/v1/proxy/llm/embeddings",
                    json!({"model_id": MODEL, "input": "the prompt"}),
                    Some(key),
                    Some(T),
                ),
            ),
            Surface::AnthropicMessages => (
                common::build_anthropic_app(state.clone()),
                common::build_post_request(
                    "/v1/messages",
                    json!({"model": MODEL, "max_tokens": 16,
                           "messages": [{"role": "user", "content": "the prompt"}]}),
                    Some(key),
                    Some(T),
                ),
            ),
            Surface::Images => (
                common::build_multimodal_app(state.clone()),
                common::build_post_request(
                    "/v1/proxy/llm/images/generations",
                    json!({"model": MODEL, "prompt": "the prompt", "response_format": "b64_json"}),
                    Some(key),
                    Some(T),
                ),
            ),
            Surface::Speech => (
                common::build_multimodal_app(state.clone()),
                common::build_post_request(
                    "/v1/proxy/llm/audio/speech",
                    json!({"model": MODEL, "voice": "alloy", "input": "the prompt"}),
                    Some(key),
                    Some(T),
                ),
            ),
            Surface::Transcription => (
                common::build_multimodal_app(state.clone()),
                transcription_request(key, MODEL, T),
            ),
            Surface::Video => (
                common::build_multimodal_app(state.clone()),
                common::build_post_request(
                    "/v1/proxy/llm/video/generations",
                    json!({"model": MODEL, "prompt": "the prompt", "duration_seconds": 5}),
                    Some(key),
                    Some(T),
                ),
            ),
        };
        answer(app.oneshot(req).await.expect("router")).await
    }
}

/// The refusal row of the dispatch check: 501, the error type, on the
/// caller's group.
async fn refusal_row(pool: &PgPool) -> Option<(Option<String>, Option<String>)> {
    for _ in 0..80 {
        let row: Option<(Option<String>, Option<String>)> = sqlx::query_as(
            "SELECT error_message, resource_group_id FROM proxy_logs \
              WHERE tenant_id = $1 AND http_status_code = 501",
        )
        .bind(T)
        .fetch_optional(pool)
        .await
        .expect("read proxy_logs");
        if row.is_some() {
            return row;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    None
}

/// A row with no `api_url`, of a provider with no known base, on `surface`:
/// nothing is connected to, the caller gets 501 naming the provider and the
/// missing `api_url`, and the refusal is recorded.
async fn a_row_with_no_destination_is_refused(pool: PgPool, surface: Surface) {
    let egress = Egress::start().await;
    let key = seed_row_without_api_url(&pool, PROVIDER, surface.mode()).await;
    let state = egress.state(pool.clone());
    let (status, body) = surface.call(&state, &key).await;
    if matches!(surface, Surface::Video) {
        // Video is submitted by the pipeline's tick, not by the route.
        brutor_gateway_core::services::video_pipeline::run_once(
            &pool,
            state.media_storage.clone(),
            &state.http_client,
            &state.encryption,
        )
        .await;
    }
    let seen = egress.seen();
    assert!(
        seen.is_empty(),
        "{surface:?}: a row with no api_url of provider {PROVIDER} has no destination; \
         the gateway tried to reach: {seen:?} (answer {status}: {body})"
    );
    assert_eq!(status, StatusCode::NOT_IMPLEMENTED, "{surface:?}: {body}");
    assert!(
        body.contains("provider_api_url_missing")
            && body.contains(PROVIDER)
            && body.contains("api_url"),
        "{surface:?}: the refusal names the provider and the missing api_url: {body}"
    );
    assert!(!body.contains(CREDENTIAL), "{surface:?}: {body}");
    let (error, group) = refusal_row(&pool)
        .await
        .unwrap_or_else(|| panic!("{surface:?}: a refusal row"));
    assert!(
        error
            .as_deref()
            .unwrap_or_default()
            .contains("provider_api_url_missing"),
        "{surface:?}: {error:?}"
    );
    assert_eq!(group.as_deref(), Some(RG), "{surface:?}");
}

macro_rules! no_destination {
    ($($name:ident = $surface:expr),+ $(,)?) => {$(
        #[sqlx::test(migrations = "./tests/migrations")]
        async fn $name(pool: PgPool) {
            a_row_with_no_destination_is_refused(pool, $surface).await;
        }
    )+};
}

no_destination!(
    chat_refuses_a_row_with_no_destination = Surface::Chat,
    streaming_chat_refuses_a_row_with_no_destination = Surface::ChatStream,
    responses_refuses_a_row_with_no_destination = Surface::Responses,
    completions_refuses_a_row_with_no_destination = Surface::Completions,
    embeddings_refuses_a_row_with_no_destination = Surface::Embeddings,
    anthropic_messages_refuses_a_row_with_no_destination = Surface::AnthropicMessages,
    images_refuses_a_row_with_no_destination = Surface::Images,
    speech_refuses_a_row_with_no_destination = Surface::Speech,
    transcription_refuses_a_row_with_no_destination = Surface::Transcription,
    video_refuses_a_row_with_no_destination = Surface::Video,
);

/// The recorder records: a row of a provider with a known base and no
/// `api_url` goes to that provider's host, and to no other. Without this the
/// tests above would pass against a recorder that hears nothing.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_row_of_a_known_provider_goes_to_its_own_base(pool: PgPool) {
    let egress = Egress::start().await;
    let key = seed_row_without_api_url(&pool, "mistral", "chat").await;
    let state = egress.state(pool.clone());
    let (status, body) = Surface::Chat.call(&state, &key).await;
    assert_ne!(status, StatusCode::NOT_IMPLEMENTED, "{body}");
    let seen = egress.seen();
    assert!(
        !seen.is_empty()
            && seen
                .iter()
                .all(|l| l.starts_with("CONNECT api.mistral.ai:443")),
        "{seen:?}"
    );
}

/// A fallback candidate with no destination, behind a requested model that
/// fails: the candidate is not sent anywhere. One case per loop that walks
/// model-level fallbacks through the generic forwarder.
async fn a_fallback_with_no_destination_is_not_sent(pool: PgPool, surface: Surface) {
    let egress = Egress::start().await;
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, RG, T).await;
    common::seed_llm_provider(&pool, "prov-f525-ok", T, "openai").await;
    common::seed_llm_provider(&pool, "prov-f525", T, PROVIDER).await;
    // The requested model: every request of the gateway's client reaches
    // the recorder, which answers 502, so it fails and the chain is walked.
    common::seed_llm_model_with_mode(
        &pool,
        MODEL,
        T,
        "prov-f525-ok",
        MODEL,
        "http://primary.f525.invalid",
        surface.mode(),
    )
    .await;
    common::seed_llm_model_with_mode(
        &pool,
        "m-f525-fallback",
        T,
        "prov-f525",
        "m-f525-fallback",
        "x",
        surface.mode(),
    )
    .await;
    sqlx::query("UPDATE llm_model SET api_url = NULL WHERE id = 'm-f525-fallback'")
        .execute(&pool)
        .await
        .expect("clear api_url");
    common::set_model_api_key(&pool, "m-f525-fallback", CREDENTIAL).await;
    common::set_model_api_key(&pool, MODEL, "sk-primary").await;
    common::bind_llm_model_to_rg(&pool, MODEL, RG).await;
    common::set_model_fallback_models(&pool, MODEL, &["m-f525-fallback"]).await;
    let key = common::seed_api_key(&pool, T, RG, None, "f525-fb-key").await;

    let state = egress.state(pool.clone());
    let (status, body) = surface.call(&state, &key).await;
    let seen = egress.seen();
    assert!(
        !seen.is_empty(),
        "{surface:?}: the requested model was tried ({status}: {body})"
    );
    let elsewhere: Vec<&String> = seen
        .iter()
        .filter(|l| !l.contains("primary.f525.invalid"))
        .collect();
    assert!(
        elsewhere.is_empty(),
        "{surface:?}: the fallback has no destination; the gateway tried to reach: {elsewhere:?}"
    );
    assert!(!status.is_success(), "{surface:?}: {status} {body}");
}

macro_rules! fallback_no_destination {
    ($($name:ident = $surface:expr),+ $(,)?) => {$(
        #[sqlx::test(migrations = "./tests/migrations")]
        async fn $name(pool: PgPool) {
            a_fallback_with_no_destination_is_not_sent(pool, $surface).await;
        }
    )+};
}

fallback_no_destination!(
    a_chat_fallback_with_no_destination_is_not_sent = Surface::Chat,
    a_streaming_chat_fallback_with_no_destination_is_not_sent = Surface::ChatStream,
    an_embeddings_fallback_with_no_destination_is_not_sent = Surface::Embeddings,
);

// ---------------------------------------------------------------------------
// A candidate the gateway cannot dispatch to, on every candidate loop
// ---------------------------------------------------------------------------

/// The requested model fails (500 from its provider) and its fallback is a
/// non-Claude Bedrock row with an `api_url`: the row the dispatch check
/// refuses with `unsupported_provider_api`. Nothing may reach the
/// fallback's `api_url`.
async fn an_unsupported_fallback_is_not_sent(pool: PgPool, surface: Surface) {
    let primary = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(500).set_body_json(json!({"error": "down"})))
        .mount(&primary)
        .await;
    let fallback = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
            "model": "x", "usage": {"prompt_tokens": 1, "total_tokens": 1}
        })))
        .mount(&fallback)
        .await;
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, RG, T).await;
    common::seed_llm_provider(&pool, "prov-f524-ok", T, "openai").await;
    common::seed_llm_provider(&pool, "prov-f524-bedrock", T, "bedrock").await;
    common::seed_llm_model_with_mode(
        &pool,
        MODEL,
        T,
        "prov-f524-ok",
        MODEL,
        &primary.uri(),
        surface.mode(),
    )
    .await;
    common::seed_llm_model_with_mode(
        &pool,
        "m-f524-bedrock",
        T,
        "prov-f524-bedrock",
        // A Bedrock family the gateway does not implement (the Titan
        // text embeddings this test used are now served on embeddings).
        "bedrock/meta.llama3-3-70b-instruct-v1:0",
        &fallback.uri(),
        surface.mode(),
    )
    .await;
    common::set_model_api_key(&pool, MODEL, "sk-primary").await;
    common::set_model_api_key(&pool, "m-f524-bedrock", CREDENTIAL).await;
    common::bind_llm_model_to_rg(&pool, MODEL, RG).await;
    common::set_model_fallback_models(&pool, MODEL, &["m-f524-bedrock"]).await;
    let key = common::seed_api_key(&pool, T, RG, None, "f524-key").await;

    let state = common::build_test_state(pool.clone());
    let (status, body) = surface.call(&state, &key).await;
    let tried = primary.received_requests().await.unwrap_or_default();
    assert!(
        !tried.is_empty(),
        "{surface:?}: the requested model was tried ({status}: {body})"
    );
    let sent: Vec<String> = fallback
        .received_requests()
        .await
        .unwrap_or_default()
        .iter()
        .map(|r| {
            format!(
                "{} {} {}",
                r.method,
                r.url.path(),
                String::from_utf8_lossy(&r.body)
            )
        })
        .collect();
    assert!(
        sent.is_empty(),
        "{surface:?}: a Bedrock row the gateway cannot dispatch to was sent: {sent:?}"
    );
    assert!(!status.is_success(), "{surface:?}: {status} {body}");
}

macro_rules! unsupported_fallback {
    ($($name:ident = $surface:expr),+ $(,)?) => {$(
        #[sqlx::test(migrations = "./tests/migrations")]
        async fn $name(pool: PgPool) {
            an_unsupported_fallback_is_not_sent(pool, $surface).await;
        }
    )+};
}

unsupported_fallback!(
    an_unsupported_chat_fallback_is_not_sent = Surface::Chat,
    an_unsupported_streaming_chat_fallback_is_not_sent = Surface::ChatStream,
    an_unsupported_embeddings_fallback_is_not_sent = Surface::Embeddings,
    an_unsupported_images_fallback_is_not_sent = Surface::Images,
    an_unsupported_speech_fallback_is_not_sent = Surface::Speech,
    an_unsupported_transcription_fallback_is_not_sent = Surface::Transcription,
);
