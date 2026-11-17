// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Claude on Amazon Bedrock (InvokeModel), the hosts of the clouds, and the
//! cloud credentials of a row.
//!
//! * A region (AWS) or a location (Google Cloud) that is not a
//!   name builds no host: nothing is connected to, the request is refused
//!   501 `provider_endpoint_invalid`.
//! * No cloud credential of a row is put in a request header.
//! * A Claude row on Bedrock with no credential is not sent.
//! * The Claude request is signed over the URI-encoded path, the
//!   bytes that are sent and the host as sent; verified by the SigV4
//!   written out in `bedrock_runtime.rs`.
//! * The response stream is decoded by one checked decoder:
//!   a truncated, oversized, garbage or wrong-checksum frame ends the
//!   stream with an error event and panics nothing.
//!
//! No request leaves this machine: the upstreams are mocks on loopback, or
//! a recorder that answers 502 and never forwards. The credentials are made
//! up.

use axum::http::StatusCode;
use base64::Engine;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::services::SharedState;
use brutor_gateway_core::services::anthropic_adapter::backend::{
    BedrockEventStreamConverter, StreamConverter,
};

use super::bedrock_runtime::{
    Egress, KEY_ID, MODEL, REGION, SECRET, T, assert_signed, body_bytes, encrypted, es_event,
    es_exception, es_frame, log_row, post, received, seed_row, sse_data, topology,
};
use crate::common;

const CLAUDE: &str = "bedrock/global.anthropic.claude-sonnet-5-5";
const NOT_A_REGION: &str = "x.attacker.example/";

pub(crate) fn anthropic_fixture() -> Value {
    json!({
        "id": "msg_signed",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [{"type": "text", "text": "pong"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 3, "output_tokens": 1}
    })
}

pub(crate) async fn invoke_mock() -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(anthropic_fixture()))
        .mount(&mock)
        .await;
    mock
}

pub(crate) async fn set_config(pool: &PgPool, id: &str, config: Option<Value>) {
    sqlx::query("UPDATE llm_model SET provider_config = $2 WHERE id = $1")
        .bind(id)
        .bind(config.map(|c| encrypted(&c.to_string())))
        .execute(pool)
        .await
        .expect("set provider_config");
}

pub(crate) fn chat(id: &str, stream: bool) -> Value {
    json!({"model_id": id, "stream": stream, "max_tokens": 64,
           "messages": [{"role": "user", "content": "ping"}]})
}

pub(crate) fn messages(id: &str, stream: bool) -> Value {
    json!({"model": id, "stream": stream, "max_tokens": 64,
           "messages": [{"role": "user", "content": "ping"}]})
}

pub(crate) async fn post_messages(
    state: &SharedState,
    key: &str,
    body: Value,
) -> (StatusCode, Vec<u8>) {
    let app = common::build_anthropic_app(state.clone());
    let req = common::build_post_request("/v1/messages", body, Some(key), Some(T));
    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    (status, body_bytes(response.into_body()).await)
}

pub(crate) async fn post_gemini(state: &SharedState, key: &str, id: &str) -> (StatusCode, Vec<u8>) {
    let app = common::build_gemini_app(state.clone());
    let req = common::build_post_request(
        &format!("/v1beta/models/{id}:generateContent"),
        json!({"contents": [{"role": "user", "parts": [{"text": "ping"}]}]}),
        Some(key),
        Some(T),
    );
    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    (status, body_bytes(response.into_body()).await)
}

pub(crate) fn text(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes).to_string()
}

// ---------------------------------------------------------------------------
// The host of a Bedrock request is the region's, or there is none
// ---------------------------------------------------------------------------

/// A Claude row whose region is `x.attacker.example/` built the host
/// `bedrock-runtime.x.attacker.example`: the signed request and the prompt
/// went to another domain. Now nothing is connected to and the request is
/// refused 501 with an audit row, on the chat route (streaming and not) and
/// on the Messages route.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_claude_row_whose_region_is_not_a_region_is_not_sent_anywhere(pool: PgPool) {
    let egress = Egress::start().await;
    let key = topology(&pool).await;
    seed_row(&pool, MODEL, "bedrock", CLAUDE, None, "chat", (3.0, 15.0)).await;
    set_config(
        &pool,
        MODEL,
        Some(
            json!({"aws_access_key_id": KEY_ID, "aws_secret_access_key": SECRET,
                    "aws_region": NOT_A_REGION}),
        ),
    )
    .await;
    let state = egress.state(pool.clone());
    let mut answers = Vec::new();
    for stream in [false, true] {
        answers.push(post(&state, "/chat/completions", &key, chat(MODEL, stream)).await);
        answers.push(post_messages(&state, &key, messages(MODEL, stream)).await);
    }
    assert!(
        egress.seen().is_empty(),
        "the gateway connected to a host built from the row's region: {:?}",
        egress.seen()
    );
    for (status, bytes) in answers {
        assert_eq!(status, StatusCode::NOT_IMPLEMENTED, "{}", text(&bytes));
        assert!(
            text(&bytes).contains("provider_endpoint_invalid"),
            "{}",
            text(&bytes)
        );
    }
    let row = log_row(&pool, 501).await;
    assert!(
        row.4
            .as_deref()
            .unwrap_or_default()
            .contains("provider_endpoint_invalid"),
        "the refusal is an audit row: {row:?}"
    );
}

/// A well-formed Claude row with no `api_url` goes to the Bedrock Runtime
/// host of its region and nowhere else.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_claude_row_goes_only_to_the_bedrock_runtime_host_of_its_region(pool: PgPool) {
    let egress = Egress::start().await;
    let key = topology(&pool).await;
    seed_row(&pool, MODEL, "bedrock", CLAUDE, None, "chat", (3.0, 15.0)).await;
    let state = egress.state(pool.clone());
    for stream in [false, true] {
        let before = egress.seen().len();
        let (status, _) = post(&state, "/chat/completions", &key, chat(MODEL, stream)).await;
        assert!(!status.is_success(), "the recorder answers 502");
        let (status, _) = post_messages(&state, &key, messages(MODEL, stream)).await;
        assert!(!status.is_success(), "the recorder answers 502");
        assert!(egress.seen().len() >= before + 2, "both were sent for");
    }
    let host = format!("CONNECT bedrock-runtime.{REGION}.amazonaws.com:443 ");
    let elsewhere: Vec<String> = egress
        .seen()
        .into_iter()
        .filter(|l| !l.starts_with(&host))
        .collect();
    assert!(elsewhere.is_empty(), "{elsewhere:?}");
}

// ---------------------------------------------------------------------------
// Vertex AI: the location is part of the host
// ---------------------------------------------------------------------------

/// A Vertex AI row (Claude and Gemini) whose location is
/// `x.attacker.example/` built the host `x.attacker.example`: the request
/// went there with the row's Google access token. The token here comes from
/// a mock on loopback (the service account's `token_uri`).
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_vertex_row_whose_location_is_not_a_location_is_not_sent_anywhere(pool: PgPool) {
    let egress = Egress::start().await;
    let token = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/token"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "access_token": "ya29.f602-made-up", "expires_in": 3600, "token_type": "Bearer"
        })))
        .mount(&token)
        .await;
    let service_account = json!({
        "type": "service_account",
        "project_id": "p",
        "private_key_id": "k1",
        "private_key": crate::workload_identity::TEST_RSA_PEM,
        "client_email": "gateway-test@p.iam.gserviceaccount.com",
        "client_id": "1",
        "token_uri": format!("{}/token", token.uri()),
    });
    let key = topology(&pool).await;
    for (id, name) in [
        ("m-f602-vclaude", "vertex_ai/claude-sonnet-5-5"),
        ("m-f602-vgemini", "vertex_ai/gemini-3.5-flash-lite"),
    ] {
        seed_row(&pool, id, "vertex_ai", name, None, "chat", (1.0, 1.0)).await;
        sqlx::query("UPDATE llm_model SET auth_type = 'gcp_service_account' WHERE id = $1")
            .bind(id)
            .execute(&pool)
            .await
            .unwrap();
        set_config(
            &pool,
            id,
            Some(json!({"gcp_project_id": "p", "gcp_location": NOT_A_REGION,
                        "gcp_credentials_json": service_account.to_string()})),
        )
        .await;
    }
    let state = egress.state(pool.clone());
    let answers = vec![
        post(
            &state,
            "/chat/completions",
            &key,
            chat("m-f602-vclaude", false),
        )
        .await,
        post_messages(&state, &key, messages("m-f602-vclaude", false)).await,
        post(
            &state,
            "/chat/completions",
            &key,
            chat("m-f602-vgemini", false),
        )
        .await,
        post_gemini(&state, &key, "m-f602-vgemini").await,
    ];
    assert!(
        egress.seen().is_empty(),
        "the gateway connected to a host built from the row's location: {:?}",
        egress.seen()
    );
    for (status, bytes) in answers {
        assert_eq!(status, StatusCode::NOT_IMPLEMENTED, "{}", text(&bytes));
        assert!(
            text(&bytes).contains("provider_endpoint_invalid"),
            "{}",
            text(&bytes)
        );
    }
}

// ---------------------------------------------------------------------------
// A Vertex AI row is called with its own Google credential
// ---------------------------------------------------------------------------

/// A Google token endpoint that answers `token`, and the service account
/// whose `token_uri` it is.
pub(crate) async fn google_identity(email: &str, token: &str) -> (MockServer, Value) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/token"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "access_token": token, "expires_in": 3600, "token_type": "Bearer"
        })))
        .mount(&mock)
        .await;
    let account = json!({
        "type": "service_account",
        "project_id": "p",
        "private_key_id": "k1",
        "private_key": crate::workload_identity::TEST_RSA_PEM,
        "client_email": email,
        "client_id": "1",
        "token_uri": format!("{}/token", mock.uri()),
    });
    (mock, account)
}

/// Two tenants, each with a Claude row on Vertex AI and a service account
/// of its own. The gateway kept the first token provider it built for the
/// whole process: the second tenant's request went out with the first
/// tenant's Google access token (billed to, and authorised as, the other
/// tenant's service account). Each row is called with its own.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_vertex_row_is_called_with_its_own_service_account(pool: PgPool) {
    const T2: &str = "tenant-f802-other";
    let vertex = invoke_mock().await;
    let endpoint = format!(
        "{}/v1/projects/p/locations/global/publishers/anthropic/models/claude-sonnet-5-5",
        vertex.uri()
    );
    let (_token_a, account_a) =
        google_identity("a@p.iam.gserviceaccount.com", "ya29.tenant-a").await;
    let (_token_b, account_b) =
        google_identity("b@p.iam.gserviceaccount.com", "ya29.tenant-b").await;

    // Tenant A: the suite's tenant.
    let key_a = topology(&pool).await;
    seed_row(
        &pool,
        "m-f802-a",
        "vertex_ai",
        "vertex_ai/claude-sonnet-5-5",
        Some(&endpoint),
        "chat",
        (1.0, 1.0),
    )
    .await;
    // Tenant B: another tenant, group, user, key and row.
    common::seed_tenant(&pool, T2).await;
    common::seed_resource_group(&pool, "rg-f802-b", T2).await;
    common::seed_end_user(&pool, 7802, T2).await;
    common::add_direct_membership(&pool, 7802, "rg-f802-b", T2).await;
    let key_b = common::seed_api_key(&pool, T2, "rg-f802-b", Some(7802), "f802-key-b").await;
    common::seed_llm_provider(&pool, "prov-f802-b", T2, "vertex_ai").await;
    common::seed_llm_model_with_mode(
        &pool,
        "m-f802-b",
        T2,
        "prov-f802-b",
        "vertex_ai/claude-sonnet-5-5",
        &endpoint,
        "chat",
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "m-f802-b", "rg-f802-b").await;
    for (id, account) in [("m-f802-a", &account_a), ("m-f802-b", &account_b)] {
        sqlx::query(
            "UPDATE llm_model SET api_key = NULL, auth_type = 'gcp_service_account' WHERE id = $1",
        )
        .bind(id)
        .execute(&pool)
        .await
        .unwrap();
        set_config(
            &pool,
            id,
            Some(json!({"gcp_project_id": "p", "gcp_location": "global",
                        "gcp_credentials_json": account.to_string()})),
        )
        .await;
    }

    let state = common::build_test_state(pool.clone());
    let call = |tenant: &'static str, key: String, id: &'static str| {
        let state = state.clone();
        async move {
            let app = common::build_llm_app(state);
            let req = common::build_post_request(
                "/v1/proxy/llm/chat/completions",
                chat(id, false),
                Some(&key),
                Some(tenant),
            );
            let response = app.oneshot(req).await.expect("router call");
            let status = response.status();
            assert_eq!(
                status,
                StatusCode::OK,
                "{id}: {}",
                text(&body_bytes(response.into_body()).await)
            );
        }
    };
    // A, then B, then A again: in one process, as one gateway serves them.
    call(T, key_a.clone(), "m-f802-a").await;
    call(T2, key_b, "m-f802-b").await;
    call(T, key_a, "m-f802-a").await;
    let tokens: Vec<String> = received(&vertex)
        .await
        .iter()
        .map(|r| {
            r.headers
                .get("authorization")
                .and_then(|v| v.to_str().ok())
                .unwrap_or_default()
                .to_string()
        })
        .collect();
    assert_eq!(
        tokens,
        [
            "Bearer ya29.tenant-a",
            "Bearer ya29.tenant-b",
            "Bearer ya29.tenant-a"
        ],
        "each tenant's row is called with its own service account's token"
    );
}

// ---------------------------------------------------------------------------
// A cloud credential is never a request header
// ---------------------------------------------------------------------------

const AZURE_SECRET: &str = "f603MadeUpAzureClientSecret4d1c";
const GCP_KEY: &str = "f603MadeUpServiceAccountKey77a2";

/// A row of an OpenAI-compatible provider with a cloud `auth_type`: the
/// generic forwarder copied the row's AWS secret access key, Azure client
/// secret or Google service-account key into `x-brutor-*` headers of the
/// request, and the upstream received them. Now such a row is refused 501
/// `provider_auth_unsupported` and nothing is sent; an Azure AD row that
/// also has an API key is sent with that key and nothing else.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_cloud_credential_of_a_row_is_never_sent_to_its_upstream(pool: PgPool) {
    let upstream = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "c1", "object": "chat.completion", "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        })))
        .mount(&upstream)
        .await;
    let key = topology(&pool).await;
    let gcp_json = json!({"type": "service_account", "private_key": GCP_KEY}).to_string();
    let rows = [
        (
            "m-f603-aws",
            "aws_iam",
            json!({"aws_access_key_id": KEY_ID, "aws_secret_access_key": SECRET,
                   "aws_region": REGION, "aws_session_token": "f603MadeUpSessionToken"}),
            None,
        ),
        (
            "m-f603-azure",
            "azure_ad",
            json!({"azure_client_id": "app", "azure_client_secret": AZURE_SECRET,
                   "azure_tenant_id": "tid"}),
            None,
        ),
        (
            "m-f603-gcp",
            "gcp_service_account",
            json!({"gcp_project_id": "p", "gcp_location": "us-central1",
                   "gcp_credentials_json": gcp_json}),
            None,
        ),
        (
            "m-f603-azure-key",
            "azure_ad",
            json!({"azure_client_id": "app", "azure_client_secret": AZURE_SECRET,
                   "azure_tenant_id": "tid"}),
            Some("f603-resource-api-key"),
        ),
    ];
    for (id, auth_type, config, api_key) in &rows {
        seed_row(
            &pool,
            id,
            "selfhosted-f603",
            "some-model",
            Some(&upstream.uri()),
            "chat",
            (1.0, 1.0),
        )
        .await;
        sqlx::query("UPDATE llm_model SET auth_type = $2 WHERE id = $1")
            .bind(id)
            .bind(auth_type)
            .execute(&pool)
            .await
            .unwrap();
        set_config(&pool, id, Some(config.clone())).await;
        if let Some(api_key) = api_key {
            common::set_model_api_key(&pool, id, api_key).await;
        }
    }
    let state = common::build_test_state(pool.clone());
    let mut answers = Vec::new();
    for (id, _, _, api_key) in &rows {
        for stream in [false, true] {
            let (status, bytes) = post(&state, "/chat/completions", &key, chat(id, stream)).await;
            answers.push((*id, api_key.is_some(), status, text(&bytes)));
        }
    }

    // What the upstream received, whatever the gateway answered.
    let gcp_b64 = base64::engine::general_purpose::STANDARD.encode(&gcp_json);
    let sent = received(&upstream).await;
    for req in &sent {
        for (name, value) in req.headers.iter() {
            let value = value.to_str().unwrap_or_default();
            assert!(
                !["x-brutor-aws", "x-brutor-azure", "x-brutor-gcp"]
                    .iter()
                    .any(|p| name.as_str().starts_with(p)),
                "the upstream received the header {name}"
            );
            for secret in [
                SECRET,
                KEY_ID,
                AZURE_SECRET,
                GCP_KEY,
                gcp_b64.as_str(),
                "f603MadeUpSessionToken",
            ] {
                assert!(
                    !value.contains(secret),
                    "the upstream received a cloud credential in the header {name}"
                );
            }
        }
    }
    for (id, has_key, status, answer) in answers {
        if has_key {
            assert!(status.is_success(), "{id}: {status} {answer}");
        } else {
            assert_eq!(status, StatusCode::NOT_IMPLEMENTED, "{id}: {answer}");
            assert!(
                answer.contains("provider_auth_unsupported"),
                "{id}: {answer}"
            );
        }
    }
    // Only the row with an API key was sent, with that key.
    assert_eq!(sent.len(), 2, "two requests of the Azure row with a key");
    for req in &sent {
        let auth: Vec<&str> = req
            .headers
            .iter()
            .filter_map(|(_, v)| v.to_str().ok())
            .filter(|v| v.contains("f603-resource-api-key"))
            .collect();
        assert_eq!(auth.len(), 1, "sent with the row's API key");
    }
}

// ---------------------------------------------------------------------------
// A Claude row with no credential is not sent
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_claude_row_without_a_credential_is_not_sent(pool: PgPool) {
    let mock = invoke_mock().await;
    let key = topology(&pool).await;
    seed_row(
        &pool,
        MODEL,
        "bedrock",
        CLAUDE,
        Some(&mock.uri()),
        "chat",
        (3.0, 15.0),
    )
    .await;
    // Half a credential is none.
    set_config(
        &pool,
        MODEL,
        Some(json!({"aws_access_key_id": KEY_ID, "aws_region": REGION})),
    )
    .await;
    let state = common::build_test_state(pool.clone());
    for stream in [false, true] {
        for (status, bytes) in [
            post(&state, "/chat/completions", &key, chat(MODEL, stream)).await,
            post_messages(&state, &key, messages(MODEL, stream)).await,
        ] {
            assert_eq!(status, StatusCode::NOT_IMPLEMENTED, "{}", text(&bytes));
            assert!(
                text(&bytes).contains("provider_credential_missing"),
                "{}",
                text(&bytes)
            );
        }
    }
    assert!(
        received(&mock).await.is_empty(),
        "a Bedrock request was sent unsigned"
    );
    let row = log_row(&pool, 501).await;
    assert!(
        row.4
            .as_deref()
            .unwrap_or_default()
            .contains("provider_credential_missing"),
        "{row:?}"
    );
}

// ---------------------------------------------------------------------------
// The signature of a Claude request verifies
// ---------------------------------------------------------------------------

/// A Claude row is sent to `/model/<id>/invoke` with the id as one
/// percent-encoded segment and a SigV4 signature that verifies over the
/// path as it arrived, the host, the date and the bytes of the body: for a
/// versioned id (`…-v1:0`) and for an inference-profile ARN, on the chat
/// route and on the Messages route. One `Authorization` header.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_claude_request_is_signed_over_what_is_sent(pool: PgPool) {
    let key = topology(&pool).await;
    for (id, name, segment) in [
        (
            "m-f601-versioned",
            "bedrock/anthropic.claude-sonnet-4-5-20250929-v1:0",
            "anthropic.claude-sonnet-4-5-20250929-v1%3A0",
        ),
        (
            "m-f601-arn",
            "arn:aws:bedrock:eu-west-1:123456789012:inference-profile/eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "arn%3Aaws%3Abedrock%3Aeu-west-1%3A123456789012%3Ainference-profile%2Feu.anthropic.claude-sonnet-4-5-20250929-v1%3A0",
        ),
        (
            "m-f601-profile",
            CLAUDE,
            "global.anthropic.claude-sonnet-5-5",
        ),
    ] {
        let mock = invoke_mock().await;
        seed_row(
            &pool,
            id,
            "bedrock",
            name,
            Some(&mock.uri()),
            "chat",
            (3.0, 15.0),
        )
        .await;
        let state = common::build_test_state(pool.clone());
        let (status, bytes) = post(&state, "/chat/completions", &key, chat(id, false)).await;
        assert_eq!(status, StatusCode::OK, "{name}: {}", text(&bytes));
        let (status, bytes) = post_messages(&state, &key, messages(id, false)).await;
        assert_eq!(status, StatusCode::OK, "{name}: {}", text(&bytes));
        let sent = received(&mock).await;
        assert_eq!(sent.len(), 2, "{name}");
        for req in &sent {
            assert_eq!(
                req.url.path(),
                format!("/model/{segment}/invoke"),
                "{name}: the id is one encoded path segment"
            );
            assert_eq!(
                req.headers.get_all("authorization").iter().count(),
                1,
                "{name}: one Authorization header"
            );
            assert_signed(req);
            let body: Value = serde_json::from_slice(&req.body).expect("a JSON body");
            assert_eq!(body["anthropic_version"], json!("2023-06-01"), "{name}");
            assert_eq!(body["messages"][0]["role"], json!("user"), "{name}");
        }
    }
}

// ---------------------------------------------------------------------------
// The response stream: Bedrock's event stream, decoded and checked
// ---------------------------------------------------------------------------

/// One `chunk` event of InvokeModelWithResponseStream: the Anthropic event,
/// base64, in `bytes`.
fn chunk(event: Value) -> Vec<u8> {
    es_event(
        "chunk",
        json!({"bytes": base64::engine::general_purpose::STANDARD.encode(event.to_string())}),
    )
}

fn claude_stream_events() -> Vec<Vec<u8>> {
    vec![
        chunk(json!({"type": "message_start", "message": {
            "id": "msg_s", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "content": [], "usage": {"input_tokens": 5, "output_tokens": 0}}})),
        chunk(json!({"type": "content_block_start", "index": 0,
                     "content_block": {"type": "text", "text": ""}})),
        chunk(json!({"type": "content_block_delta", "index": 0,
                     "delta": {"type": "text_delta", "text": "Hello from Bedrock"}})),
        chunk(json!({"type": "content_block_stop", "index": 0})),
        chunk(
            json!({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                     "usage": {"output_tokens": 4}}),
        ),
        chunk(json!({"type": "message_stop"})),
    ]
}

async fn event_stream_mock(bytes: Vec<u8>) -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(
            ResponseTemplate::new(200)
                .insert_header("content-type", "application/vnd.amazon.eventstream")
                .set_body_bytes(bytes),
        )
        .mount(&mock)
        .await;
    mock
}

/// The caller's key and a Claude row, whose endpoint each stream sets.
async fn stream_setup(pool: &PgPool) -> String {
    let key = topology(pool).await;
    seed_row(
        pool,
        MODEL,
        "bedrock",
        CLAUDE,
        Some("http://127.0.0.1:9"),
        "chat",
        (3.0, 15.0),
    )
    .await;
    key
}

/// What the chat route and the Messages route answer when Bedrock's
/// response stream is `bytes`.
async fn streamed(pool: &PgPool, key: &str, bytes: Vec<u8>) -> (String, String) {
    let mock = event_stream_mock(bytes).await;
    sqlx::query("UPDATE llm_model SET api_url = $2 WHERE id = $1")
        .bind(MODEL)
        .bind(mock.uri())
        .execute(pool)
        .await
        .expect("point the row at the mock");
    let state = common::build_test_state(pool.clone());
    let (status, chat_bytes) = post(&state, "/chat/completions", key, chat(MODEL, true)).await;
    assert_eq!(status, StatusCode::OK, "{}", text(&chat_bytes));
    let (status, native_bytes) = post_messages(&state, key, messages(MODEL, true)).await;
    assert_eq!(status, StatusCode::OK, "{}", text(&native_bytes));
    let sent = received(&mock).await;
    assert_eq!(sent.len(), 2);
    for req in &sent {
        assert!(
            req.url.path().ends_with("/invoke-with-response-stream"),
            "{}",
            req.url.path()
        );
        assert_signed(req);
    }
    (text(&chat_bytes), text(&native_bytes))
}

/// A Claude stream on Bedrock reaches the caller: as chat completion chunks
/// on the chat route, as Anthropic events on the Messages route. (The
/// stream was read as SSE text; Bedrock's is a binary event stream, and the
/// caller received no event.)
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_claude_stream_on_bedrock_reaches_the_caller(pool: PgPool) {
    let key = stream_setup(&pool).await;
    let (chat_sse, native_sse) = streamed(&pool, &key, claude_stream_events().concat()).await;
    let chat_text: String = sse_data(chat_sse.as_bytes())
        .iter()
        .filter_map(|d| serde_json::from_str::<Value>(d).ok())
        .filter_map(|v| {
            v.pointer("/choices/0/delta/content")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .collect();
    assert_eq!(chat_text, "Hello from Bedrock", "{chat_sse}");
    assert!(native_sse.contains("Hello from Bedrock"), "{native_sse}");
    assert!(native_sse.contains("message_stop"), "{native_sse}");
}

fn is_error_stream(sse: &str) -> bool {
    sse_data(sse.as_bytes())
        .iter()
        .filter_map(|d| serde_json::from_str::<Value>(d).ok())
        .any(|v| v.get("error").is_some() || v.get("type").and_then(Value::as_str) == Some("error"))
}

/// A response stream that is not a well-formed event stream ends with an
/// error event, on both routes, and the events before the bad frame are
/// delivered. Nothing panics: the request answers.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_malformed_claude_stream_ends_with_an_error_event(pool: PgPool) {
    let good = claude_stream_events();
    let head: Vec<u8> = good[..3].concat();
    let mut wrong_message_crc = good[3].clone();
    let last = wrong_message_crc.len() - 1;
    wrong_message_crc[last] ^= 0xFF;
    let mut wrong_prelude_crc = good[3].clone();
    wrong_prelude_crc[9] ^= 0xFF;
    // The frame says its headers are longer than the frame.
    let mut headers_past_the_end = good[3].clone();
    headers_past_the_end[4..8].copy_from_slice(&0x00FF_FFFFu32.to_be_bytes());
    // The frame says it is 4 GiB long.
    let mut oversized = good[3].clone();
    oversized[..4].copy_from_slice(&u32::MAX.to_be_bytes());
    // The frame says it is 8 bytes long.
    let mut undersized = good[3].clone();
    undersized[..4].copy_from_slice(&8u32.to_be_bytes());
    let key = stream_setup(&pool).await;
    for (what, bad) in [
        ("a wrong message checksum", wrong_message_crc),
        ("a wrong prelude checksum", wrong_prelude_crc),
        ("a headers length past the frame", headers_past_the_end),
        ("a 4 GiB length", oversized),
        ("an 8 byte length", undersized),
        ("garbage", b"<html>502 Bad Gateway</html>\n\n".to_vec()),
    ] {
        let mut bytes = head.clone();
        bytes.extend_from_slice(&bad);
        bytes.extend(good[4..].concat());
        let (chat_sse, native_sse) = streamed(&pool, &key, bytes).await;
        assert!(is_error_stream(&chat_sse), "{what}: chat: {chat_sse}");
        assert!(
            is_error_stream(&native_sse),
            "{what}: messages: {native_sse}"
        );
        assert!(
            native_sse.contains("Hello from Bedrock"),
            "{what}: the events before the bad frame are delivered: {native_sse}"
        );
        assert!(
            !native_sse.contains("message_stop"),
            "{what}: nothing after the bad frame is read: {native_sse}"
        );
    }
}

/// An exception message in the stream (throttling mid-stream) is an error
/// event with Bedrock's message.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_exception_in_a_claude_stream_is_an_error_event(pool: PgPool) {
    let mut bytes: Vec<u8> = claude_stream_events()[..3].concat();
    bytes.extend(es_exception(
        "throttlingException",
        "Too many requests, slow down",
    ));
    let key = stream_setup(&pool).await;
    let (chat_sse, native_sse) = streamed(&pool, &key, bytes).await;
    assert!(is_error_stream(&chat_sse), "{chat_sse}");
    assert!(is_error_stream(&native_sse), "{native_sse}");
    assert!(native_sse.contains("Too many requests"), "{native_sse}");
}

// ---------------------------------------------------------------------------
// The converter, fed directly (these panicked)
// ---------------------------------------------------------------------------

fn error_events(events: &[(String, String)]) -> usize {
    events.iter().filter(|(kind, _)| kind == "error").count()
}

#[test]
fn a_frame_whose_lengths_lie_is_an_error_event_not_a_panic() {
    let good = chunk(json!({"type": "message_stop"}));
    // Headers length past the end of the frame: `try_parse_frame` sliced
    // `buffer[12..12 + headers_length]` and panicked.
    let mut headers_past_the_end = good.clone();
    headers_past_the_end[4..8].copy_from_slice(&0x00FF_FFFFu32.to_be_bytes());
    // A total length under the 16 bytes of an empty frame: `total - 4`
    // underflowed, or the payload range was inverted.
    let mut tiny = good.clone();
    tiny[..4].copy_from_slice(&2u32.to_be_bytes());
    let mut zero = good.clone();
    zero[..4].copy_from_slice(&0u32.to_be_bytes());
    for (what, bytes) in [
        ("headers past the end", headers_past_the_end),
        ("a 2 byte frame", tiny),
        ("a 0 byte frame", zero),
        ("garbage", vec![0xAB; 64]),
        (
            "text",
            b"HTTP/1.1 502 Bad Gateway\r\n\r\nnot an event stream".to_vec(),
        ),
    ] {
        let mut converter = BedrockEventStreamConverter::new();
        let events = converter.convert(&bytes);
        assert_eq!(error_events(&events), 1, "{what}: {events:?}");
        // The stream is over: nothing more is read, and no second error.
        assert!(converter.convert(&good).is_empty(), "{what}");
    }
}

#[test]
fn a_truncated_frame_waits_and_a_wrong_checksum_is_an_error() {
    let good = chunk(json!({"type": "content_block_stop", "index": 0}));
    // Truncated: not an error, the rest may come.
    for cut in [0, 1, 11, 12, 20, good.len() - 1] {
        let mut converter = BedrockEventStreamConverter::new();
        assert!(converter.convert(&good[..cut]).is_empty(), "cut at {cut}");
        let events = converter.convert(&good[cut..]);
        assert_eq!(events.len(), 1, "cut at {cut}: {events:?}");
        assert_eq!(events[0].0, "content_block_stop");
    }
    // Every single-bit corruption of the frame is caught by a checksum.
    for at in 0..good.len() {
        let mut bad = good.clone();
        bad[at] ^= 0x01;
        let mut converter = BedrockEventStreamConverter::new();
        let events = converter.convert(&bad);
        assert!(
            events.iter().all(|(kind, _)| kind == "error"),
            "a corrupted byte {at} was delivered: {events:?}"
        );
        // A corrupted total length may leave the decoder waiting for a
        // longer frame; it never delivers the corrupted one.
        assert!(error_events(&events) <= 1);
    }
    // A wrong prelude checksum under a message checksum that is right for
    // the bytes as they are: only the prelude check can see it.
    let mut bad_prelude = good.clone();
    bad_prelude[8] ^= 0x40;
    let covered = bad_prelude.len() - 4;
    let message_crc = super::bedrock_runtime::crc32(&bad_prelude[..covered]);
    bad_prelude[covered..].copy_from_slice(&message_crc.to_be_bytes());
    let events = BedrockEventStreamConverter::new().convert(&bad_prelude);
    assert_eq!(
        error_events(&events),
        1,
        "a wrong prelude checksum: {events:?}"
    );
    assert_eq!(events.len(), 1, "{events:?}");
    // A frame built with a wrong checksum from the start.
    let mut frame = es_frame(&[(":event-type", "chunk")], b"{}");
    let last = frame.len() - 1;
    frame[last] = frame[last].wrapping_add(1);
    let events = BedrockEventStreamConverter::new().convert(&frame);
    assert_eq!(error_events(&events), 1, "{events:?}");
}
