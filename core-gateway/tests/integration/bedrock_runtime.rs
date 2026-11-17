// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The Amazon Bedrock rows that are not Claude.
//!
//! The gateway refused every non-Claude row of `bedrock` and
//! `bedrock_converse` with 501 `unsupported_provider_api`. It now
//! serves the chat families `amazon.nova*`, `mistral.*` and `deepseek.*`
//! through the Bedrock Runtime `Converse` / `ConverseStream` operations and
//! the embedding families `amazon.titan-embed-text*` and `cohere.embed-*-v3`
//! through `InvokeModel`, every request SigV4-signed with the row's own
//! credential. Everything else on Bedrock is still refused.
//!
//! No request leaves this machine and no real credential is read: the
//! Bedrock endpoint is a mock on loopback (the row's `api_url`), or a
//! recorder every connection of the gateway's client goes to; the AWS
//! credential is made up here. The SigV4 signature the mock received is
//! checked by an implementation in this file, not by the code under test.

use std::sync::{Arc, Mutex};

use axum::body::Body;
use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sqlx::PgPool;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::config::AppConfig;
use brutor_gateway_core::services::encryption::EncryptionService;
use brutor_gateway_core::services::telemetry::TelemetryState;
use brutor_gateway_core::services::{AppState, SharedState};

use crate::common;

pub(crate) const T: &str = "tenant-f600";
pub(crate) const RG: &str = "rg-f600";
pub(crate) const MODEL: &str = "m-f600";
/// A made-up AWS credential. Neither half may appear outside the gateway,
/// except the key id inside the `Authorization` header AWS defines.
pub(crate) const KEY_ID: &str = "AKIAF600MADEUP00KEY1";
pub(crate) const SECRET: &str = "f600MadeUpSecret9b1e47c2a05d3f68NotARealKey";
pub(crate) const SESSION_TOKEN: &str = "f600MadeUpSessionToken7c11d0e2a94b";
pub(crate) const REGION: &str = "eu-west-1";

// ---------------------------------------------------------------------------
// Seeding and calling
// ---------------------------------------------------------------------------

pub(crate) async fn topology(pool: &PgPool) -> String {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_end_user(pool, 7600, T).await;
    common::add_direct_membership(pool, 7600, RG, T).await;
    common::seed_api_key(pool, T, RG, Some(7600), "f600-key").await
}

pub(crate) fn encrypted(plain: &str) -> String {
    EncryptionService::new("test-encryption-key-32-chars-ok!")
        .encrypt(plain)
        .expect("encrypt")
}

fn aws_config(session: bool) -> String {
    let mut config = json!({
        "aws_access_key_id": KEY_ID,
        "aws_secret_access_key": SECRET,
        "aws_region": REGION,
    });
    if session {
        config["aws_session_token"] = json!(SESSION_TOKEN);
    }
    encrypted(&config.to_string())
}

/// A row of `provider` named `model_name`, with the made-up AWS credential
/// in its `provider_config`, bound to the caller's group. `api_url` is the
/// mock, or `None` for a row as the catalog seeds it.
pub(crate) async fn seed_row(
    pool: &PgPool,
    id: &str,
    provider: &str,
    model_name: &str,
    api_url: Option<&str>,
    mode: &str,
    prices: (f64, f64),
) {
    let provider_id = format!("prov-f600-{provider}");
    common::seed_llm_provider(pool, &provider_id, T, provider).await;
    common::seed_llm_model_with_mode(
        pool,
        id,
        T,
        &provider_id,
        model_name,
        api_url.unwrap_or(""),
        mode,
    )
    .await;
    sqlx::query(
        "UPDATE llm_model SET api_url = $2, api_key = NULL, auth_type = 'aws_iam', \
                provider_config = $3, cost_per_input_token = $4, cost_per_output_token = $5 \
          WHERE id = $1",
    )
    .bind(id)
    .bind(api_url)
    .bind(aws_config(false))
    .bind(prices.0)
    .bind(prices.1)
    .execute(pool)
    .await
    .expect("set the row");
    common::bind_llm_model_to_rg(pool, id, RG).await;
}

pub(crate) async fn body_bytes(body: Body) -> Vec<u8> {
    body.collect()
        .await
        .expect("collect body")
        .to_bytes()
        .to_vec()
}

pub(crate) async fn post(
    state: &SharedState,
    route: &str,
    key: &str,
    body: Value,
) -> (StatusCode, Vec<u8>) {
    let app = common::build_llm_app(state.clone());
    let req =
        common::build_post_request(&format!("/v1/proxy/llm{route}"), body, Some(key), Some(T));
    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    (status, body_bytes(response.into_body()).await)
}

async fn post_json(pool: &PgPool, route: &str, key: &str, body: Value) -> (StatusCode, Value) {
    let state = common::build_test_state(pool.clone());
    let (status, bytes) = post(&state, route, key, body).await;
    (
        status,
        serde_json::from_slice(&bytes)
            .unwrap_or_else(|_| json!({"raw": String::from_utf8_lossy(&bytes).to_string()})),
    )
}

fn chat_body(extra: Value) -> Value {
    let mut body = json!({
        "model_id": MODEL,
        "messages": [{"role": "user", "content": "ping"}]
    });
    for (k, v) in extra.as_object().expect("object") {
        body[k] = v.clone();
    }
    body
}

/// The `data:` payloads of an SSE answer.
pub(crate) fn sse_data(bytes: &[u8]) -> Vec<String> {
    String::from_utf8_lossy(bytes)
        .lines()
        .filter_map(|l| l.strip_prefix("data:").map(|d| d.trim().to_string()))
        .collect()
}

// ---------------------------------------------------------------------------
// Bedrock fixtures (shapes from the AWS API reference)
// ---------------------------------------------------------------------------

/// A `Converse` response:
/// <https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html>.
fn converse_fixture(text: &str) -> Value {
    json!({
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 100, "outputTokens": 50, "totalTokens": 150},
        "metrics": {"latencyMs": 12}
    })
}

async fn converse_mock(reply: Value) -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(reply))
        .mount(&mock)
        .await;
    mock
}

/// CRC-32 (the GZIP polynomial), for the event-stream checksums.
pub(crate) fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xFFFF_FFFFu32;
    for b in data {
        crc ^= u32::from(*b);
        for _ in 0..8 {
            crc = if crc & 1 == 1 {
                (crc >> 1) ^ 0xEDB8_8320
            } else {
                crc >> 1
            };
        }
    }
    !crc
}

/// One `application/vnd.amazon.eventstream` message: prelude (total length,
/// headers length, prelude CRC), headers (name length, name, value type 7,
/// 2-byte value length, value), payload, message CRC.
pub(crate) fn es_frame(headers: &[(&str, &str)], payload: &[u8]) -> Vec<u8> {
    let mut h = Vec::new();
    for (name, value) in headers {
        h.push(name.len() as u8);
        h.extend_from_slice(name.as_bytes());
        h.push(7);
        h.extend_from_slice(&(value.len() as u16).to_be_bytes());
        h.extend_from_slice(value.as_bytes());
    }
    let total = 12 + h.len() + payload.len() + 4;
    let mut out = Vec::with_capacity(total);
    out.extend_from_slice(&(total as u32).to_be_bytes());
    out.extend_from_slice(&(h.len() as u32).to_be_bytes());
    let prelude_crc = crc32(&out);
    out.extend_from_slice(&prelude_crc.to_be_bytes());
    out.extend_from_slice(&h);
    out.extend_from_slice(payload);
    let message_crc = crc32(&out);
    out.extend_from_slice(&message_crc.to_be_bytes());
    out
}

pub(crate) fn es_event(kind: &str, payload: Value) -> Vec<u8> {
    es_frame(
        &[
            (":event-type", kind),
            (":content-type", "application/json"),
            (":message-type", "event"),
        ],
        payload.to_string().as_bytes(),
    )
}

pub(crate) fn es_exception(kind: &str, message: &str) -> Vec<u8> {
    es_frame(
        &[
            (":exception-type", kind),
            (":content-type", "application/json"),
            (":message-type", "exception"),
        ],
        json!({"message": message}).to_string().as_bytes(),
    )
}

/// A `ConverseStream` answer: text, then one tool call, then the stop and
/// the metadata event with the usage.
fn converse_stream_fixture() -> Vec<u8> {
    [
        es_event("messageStart", json!({"role": "assistant"})),
        es_event(
            "contentBlockDelta",
            json!({"contentBlockIndex": 0, "delta": {"text": "Looking "}}),
        ),
        es_event(
            "contentBlockDelta",
            json!({"contentBlockIndex": 0, "delta": {"text": "it up."}}),
        ),
        es_event("contentBlockStop", json!({"contentBlockIndex": 0})),
        es_event(
            "contentBlockStart",
            json!({"contentBlockIndex": 1,
                   "start": {"toolUse": {"toolUseId": "tooluse_1", "name": "get_weather"}}}),
        ),
        es_event(
            "contentBlockDelta",
            json!({"contentBlockIndex": 1, "delta": {"toolUse": {"input": "{\"city\":"}}}),
        ),
        es_event(
            "contentBlockDelta",
            json!({"contentBlockIndex": 1, "delta": {"toolUse": {"input": "\"Oslo\"}"}}}),
        ),
        es_event("contentBlockStop", json!({"contentBlockIndex": 1})),
        es_event("messageStop", json!({"stopReason": "tool_use"})),
        es_event(
            "metadata",
            json!({"usage": {"inputTokens": 100, "outputTokens": 50, "totalTokens": 150},
                   "metrics": {"latencyMs": 20}}),
        ),
    ]
    .concat()
}

async fn stream_mock(bytes: Vec<u8>) -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(
            ResponseTemplate::new(200).set_body_raw(bytes, "application/vnd.amazon.eventstream"),
        )
        .mount(&mock)
        .await;
    mock
}

// ---------------------------------------------------------------------------
// SigV4, computed here
// ---------------------------------------------------------------------------

/// HMAC-SHA256 (RFC 2104), written out here: the block is 64 bytes.
fn hmac(key: &[u8], data: &[u8]) -> Vec<u8> {
    let mut block = [0u8; 64];
    if key.len() > 64 {
        block[..32].copy_from_slice(&Sha256::digest(key));
    } else {
        block[..key.len()].copy_from_slice(key);
    }
    let mut inner = Sha256::new();
    inner.update(block.map(|b| b ^ 0x36));
    inner.update(data);
    let mut outer = Sha256::new();
    outer.update(block.map(|b| b ^ 0x5c));
    outer.update(inner.finalize());
    outer.finalize().to_vec()
}

fn sha256_hex(data: &[u8]) -> String {
    hex::encode(Sha256::digest(data))
}

/// `UriEncode()` of the SigV4 reference: every byte except the unreserved
/// characters `A-Z a-z 0-9 - . _ ~`, as `%XX` with uppercase hex
/// (<https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv-create-signed-request.html>).
fn uri_encode(s: &str) -> String {
    let mut out = String::new();
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'.' | b'_' | b'~') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// The signature of a request as AWS computes it: from the method, the
/// path as it arrived, the headers the `Authorization` header names, and
/// the body. `secret` is the caller's.
fn expected_signature(
    path_as_sent: &str,
    signed_headers: &[(String, String)],
    body: &[u8],
    amz_date: &str,
    scope: &str,
    secret: &str,
) -> String {
    // CanonicalUri: "the URI-encoded version of the absolute path component
    // URI": each segment of the path as sent, encoded.
    let canonical_uri = path_as_sent
        .split('/')
        .map(uri_encode)
        .collect::<Vec<_>>()
        .join("/");
    let canonical_headers: String = signed_headers
        .iter()
        .map(|(k, v)| format!("{k}:{}\n", v.trim()))
        .collect();
    let names = signed_headers
        .iter()
        .map(|(k, _)| k.as_str())
        .collect::<Vec<_>>()
        .join(";");
    let canonical_request = format!(
        "POST\n{canonical_uri}\n\n{canonical_headers}\n{names}\n{}",
        sha256_hex(body)
    );
    let string_to_sign = format!(
        "AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{}",
        sha256_hex(canonical_request.as_bytes())
    );
    let mut parts = scope.split('/');
    let (date, region, service) = (
        parts.next().unwrap_or_default(),
        parts.next().unwrap_or_default(),
        parts.next().unwrap_or_default(),
    );
    let k_date = hmac(format!("AWS4{secret}").as_bytes(), date.as_bytes());
    let k_region = hmac(&k_date, region.as_bytes());
    let k_service = hmac(&k_region, service.as_bytes());
    let k_signing = hmac(&k_service, b"aws4_request");
    hex::encode(hmac(&k_signing, string_to_sign.as_bytes()))
}

/// The request the mock received carries a valid SigV4 signature of the
/// made-up credential, for the Bedrock service in the row's region, over
/// the path, the host, the date and the body as they arrived.
pub(crate) fn assert_signed(req: &wiremock::Request) {
    let header = |name: &str| -> String {
        req.headers
            .get(name)
            .and_then(|v| v.to_str().ok())
            .unwrap_or_default()
            .to_string()
    };
    let auth = header("authorization");
    assert!(
        auth.starts_with("AWS4-HMAC-SHA256 "),
        "the request is not SigV4-signed: authorization scheme {:?}",
        auth.split(' ').next()
    );
    let field = |name: &str| -> String {
        auth.split([' ', ','])
            .find_map(|p| p.strip_prefix(&format!("{name}=")))
            .unwrap_or_default()
            .to_string()
    };
    let credential = field("Credential");
    let (key_id, scope) = credential.split_once('/').expect("credential scope");
    assert_eq!(key_id, KEY_ID);
    let amz_date = header("x-amz-date");
    assert_eq!(
        scope,
        format!("{}/{REGION}/bedrock/aws4_request", &amz_date[..8]),
        "credential scope"
    );
    let names: Vec<String> = field("SignedHeaders")
        .split(';')
        .map(str::to_string)
        .collect();
    for required in ["host", "x-amz-date", "x-amz-content-sha256"] {
        assert!(
            names.iter().any(|n| n == required),
            "{required} is not a signed header: {names:?}"
        );
    }
    assert_eq!(header("x-amz-content-sha256"), sha256_hex(&req.body));
    let signed: Vec<(String, String)> = names.iter().map(|n| (n.clone(), header(n))).collect();
    let expected = expected_signature(req.url.path(), &signed, &req.body, &amz_date, scope, SECRET);
    assert_eq!(
        field("Signature"),
        expected,
        "the SigV4 signature does not verify for path {}",
        req.url.path()
    );
    // The credential's secret half travels nowhere.
    for (name, value) in req.headers.iter() {
        assert!(
            !value.to_str().unwrap_or_default().contains(SECRET),
            "header {name} carries the secret access key"
        );
        assert!(
            !name.as_str().starts_with("x-brutor-aws"),
            "the credential is sent as a header: {name}"
        );
    }
    assert!(!String::from_utf8_lossy(&req.body).contains(SECRET));
}

pub(crate) async fn received(mock: &MockServer) -> Vec<wiremock::Request> {
    mock.received_requests().await.unwrap_or_default()
}

/// `(prompt, completion, cost, status, error)` of the tenant's audit row
/// for `path_like`, once it carries what `ready` wants.
pub(crate) type LogRow = (
    Option<i32>,
    Option<i32>,
    Option<f64>,
    Option<i32>,
    Option<String>,
);

pub(crate) async fn log_row(pool: &PgPool, status: i32) -> LogRow {
    for _ in 0..80 {
        let row: Option<LogRow> = sqlx::query_as(
            "SELECT llm_prompt_tokens, llm_completion_tokens, llm_estimated_cost_usd, \
                    http_status_code, error_message \
               FROM proxy_logs WHERE tenant_id = $1 AND http_status_code = $2 \
              ORDER BY created_at DESC LIMIT 1",
        )
        .bind(T)
        .bind(status)
        .fetch_optional(pool)
        .await
        .expect("proxy_logs");
        if let Some(r) = row {
            return r;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    panic!("no proxy_logs row with status {status}");
}

pub(crate) async fn usage_row(pool: &PgPool) -> (Option<i32>, Option<i32>, Option<f64>) {
    for _ in 0..80 {
        let row: Option<(Option<i32>, Option<i32>, Option<f64>)> = sqlx::query_as(
            "SELECT prompt_tokens, completion_tokens, estimated_cost FROM usage_logs \
              WHERE tenant_id = $1 ORDER BY created_at DESC LIMIT 1",
        )
        .bind(T)
        .fetch_optional(pool)
        .await
        .expect("usage_logs");
        if let Some(r) = row {
            return r;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    panic!("no usage_logs row");
}

fn close(a: f64, b: f64) -> bool {
    (a - b).abs() < 1e-9
}

// ---------------------------------------------------------------------------
// 1. Chat: each of the twelve chat rows is served through Converse
// ---------------------------------------------------------------------------

/// A seeded chat row answers a chat completion: one signed `Converse` call
/// to `/model/<id>/converse` (the id percent-encoded), the reply as an
/// OpenAI chat completion, usage and cost from the row's prices.
async fn a_chat_row_is_served(
    pool: PgPool,
    provider: &str,
    model_name: &str,
    path: &str,
    prices: (f64, f64),
) {
    let mock = converse_mock(converse_fixture("pong")).await;
    let key = topology(&pool).await;
    seed_row(
        &pool,
        MODEL,
        provider,
        model_name,
        Some(&mock.uri()),
        "chat",
        (prices.0, prices.1),
    )
    .await;
    let (status, answer) = post_json(
        &pool,
        "/chat/completions",
        &key,
        chat_body(json!({"max_tokens": 64})),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::OK,
        "{provider} / {model_name}: {answer}"
    );
    assert_eq!(answer["object"], json!("chat.completion"), "{answer}");
    assert_eq!(answer["choices"][0]["message"]["role"], json!("assistant"));
    assert_eq!(answer["choices"][0]["message"]["content"], json!("pong"));
    assert_eq!(answer["choices"][0]["finish_reason"], json!("stop"));
    assert_eq!(
        answer["usage"],
        json!({"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}),
        "{answer}"
    );

    let sent = received(&mock).await;
    assert_eq!(sent.len(), 1, "one Converse call");
    assert_eq!(sent[0].url.path(), path, "{provider} / {model_name}");
    assert_signed(&sent[0]);
    let body: Value = serde_json::from_slice(&sent[0].body).expect("a JSON body");
    assert_eq!(
        body,
        json!({
            "messages": [{"role": "user", "content": [{"text": "ping"}]}],
            "inferenceConfig": {"maxTokens": 64}
        }),
        "the Converse body"
    );

    // Cost and usage as for every other provider: from the row's prices,
    // per million tokens.
    let expected = 100.0 * prices.0 * 1e-6 + 50.0 * prices.1 * 1e-6;
    let log = log_row(&pool, 200).await;
    assert_eq!((log.0, log.1), (Some(100), Some(50)), "audit row tokens");
    assert!(
        close(log.2.expect("cost on the audit row"), expected),
        "audit cost {:?}, expected {expected}",
        log.2
    );
    let usage = usage_row(&pool).await;
    assert_eq!(
        (usage.0, usage.1),
        (Some(100), Some(50)),
        "usage row tokens"
    );
    assert!(
        close(usage.2.expect("cost on the usage row"), expected),
        "usage cost {:?}, expected {expected}",
        usage.2
    );
}

macro_rules! chat_rows_served {
    ($($name:ident = ($provider:expr, $model:expr, $path:expr, $prices:expr)),+ $(,)?) => {$(
        #[sqlx::test(migrations = "./tests/migrations")]
        async fn $name(pool: PgPool) {
            a_chat_row_is_served(pool, $provider, $model, $path, $prices).await;
        }
    )+};
}

// The seed rows of `bedrock_models_init.py` and
// `bedrock_converse_models_init.py`: name, and prices per million tokens.
chat_rows_served!(
    bedrock_nova_pro_is_served = (
        "bedrock",
        "bedrock/amazon.nova-pro-v1:0",
        "/model/amazon.nova-pro-v1%3A0/converse",
        (0.80, 3.20)
    ),
    bedrock_nova_lite_is_served = (
        "bedrock",
        "bedrock/amazon.nova-lite-v1:0",
        "/model/amazon.nova-lite-v1%3A0/converse",
        (0.06, 0.24)
    ),
    bedrock_nova_micro_is_served = (
        "bedrock",
        "bedrock/amazon.nova-micro-v1:0",
        "/model/amazon.nova-micro-v1%3A0/converse",
        (0.035, 0.14)
    ),
    bedrock_nova_2_lite_is_served = (
        "bedrock",
        "bedrock/global.amazon.nova-2-lite-v1:0",
        "/model/global.amazon.nova-2-lite-v1%3A0/converse",
        (0.3, 2.5)
    ),
    bedrock_mistral_large_3_is_served = (
        "bedrock",
        "bedrock/mistral.mistral-large-3-675b-instruct",
        "/model/mistral.mistral-large-3-675b-instruct/converse",
        (0.5, 1.5)
    ),
    bedrock_deepseek_v32_is_served = (
        "bedrock",
        "bedrock/deepseek.v3.2",
        "/model/deepseek.v3.2/converse",
        (0.62, 1.85)
    ),
    bedrock_converse_nova_pro_is_served = (
        "bedrock_converse",
        "bedrock/converse/us.amazon.nova-pro-v1:0",
        "/model/us.amazon.nova-pro-v1%3A0/converse",
        (0.80, 3.20)
    ),
    bedrock_converse_nova_lite_is_served = (
        "bedrock_converse",
        "bedrock/converse/us.amazon.nova-lite-v1:0",
        "/model/us.amazon.nova-lite-v1%3A0/converse",
        (0.06, 0.24)
    ),
    bedrock_converse_nova_micro_is_served = (
        "bedrock_converse",
        "bedrock/converse/us.amazon.nova-micro-v1:0",
        "/model/us.amazon.nova-micro-v1%3A0/converse",
        (0.035, 0.14)
    ),
    bedrock_converse_nova_2_lite_is_served = (
        "bedrock_converse",
        "bedrock/converse/us.amazon.nova-2-lite-v1:0",
        "/model/us.amazon.nova-2-lite-v1%3A0/converse",
        (0.33, 2.75)
    ),
    bedrock_converse_mistral_large_3_is_served = (
        "bedrock_converse",
        "bedrock/converse/mistral.mistral-large-3-675b-instruct",
        "/model/mistral.mistral-large-3-675b-instruct/converse",
        (0.5, 1.5)
    ),
    bedrock_converse_deepseek_v32_is_served = (
        "bedrock_converse",
        "bedrock/converse/deepseek.v3.2",
        "/model/deepseek.v3.2/converse",
        (0.62, 1.85)
    ),
);

/// The whole translation: system messages, text and image parts, tool
/// definitions, an assistant tool call and its result in the history, the
/// inference parameters; and the parameters Converse has no field for are
/// not sent.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_chat_request_is_translated_to_converse(pool: PgPool) {
    let mock = converse_mock(json!({
        "output": {"message": {"role": "assistant", "content": [
            {"text": "Checking."},
            {"toolUse": {"toolUseId": "tooluse_9", "name": "get_weather",
                         "input": {"city": "Bergen"}}}
        ]}},
        "stopReason": "tool_use",
        "usage": {"inputTokens": 321, "outputTokens": 12, "totalTokens": 333,
                  "cacheReadInputTokens": 7},
        "metrics": {"latencyMs": 40}
    }))
    .await;
    let key = topology(&pool).await;
    seed_row(
        &pool,
        MODEL,
        "bedrock",
        "bedrock/amazon.nova-pro-v1:0",
        Some(&mock.uri()),
        "chat",
        (0.8, 3.2),
    )
    .await;
    let schema = json!({"type": "object", "properties": {"city": {"type": "string"}},
                        "required": ["city"]});
    let (status, answer) = post_json(
        &pool,
        "/chat/completions",
        &key,
        json!({
            "model_id": MODEL,
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": [
                    {"type": "text", "text": "what is in the picture, and the weather in Oslo?"},
                    {"type": "image_url",
                     "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}
                ]},
                {"role": "assistant", "content": null, "tool_calls": [
                    {"id": "call_1", "type": "function",
                     "function": {"name": "get_weather", "arguments": "{\"city\":\"Oslo\"}"}}
                ]},
                {"role": "tool", "tool_call_id": "call_1", "content": "4C, rain"},
                {"role": "user", "content": "thanks, and Bergen?"}
            ],
            "tools": [{"type": "function", "function": {
                "name": "get_weather", "description": "Weather by city", "parameters": schema}}],
            "tool_choice": "auto",
            "max_tokens": 256,
            "temperature": 0.2,
            "top_p": 0.9,
            "stop": ["END"],
            "seed": 7,
            "presence_penalty": 0.5,
            "frequency_penalty": 0.5,
            "logit_bias": {"50256": -100},
            "user": "u-1"
        }),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{answer}");
    let sent = received(&mock).await;
    assert_eq!(sent.len(), 1);
    assert_signed(&sent[0]);
    let body: Value = serde_json::from_slice(&sent[0].body).expect("a JSON body");
    assert_eq!(
        body,
        json!({
            "system": [{"text": "be brief"}],
            "messages": [
                {"role": "user", "content": [
                    {"text": "what is in the picture, and the weather in Oslo?"},
                    {"image": {"format": "png", "source": {"bytes": "iVBORw0KGgo="}}}
                ]},
                {"role": "assistant", "content": [
                    {"toolUse": {"toolUseId": "call_1", "name": "get_weather",
                                 "input": {"city": "Oslo"}}}
                ]},
                {"role": "user", "content": [
                    {"toolResult": {"toolUseId": "call_1", "content": [{"text": "4C, rain"}]}},
                    {"text": "thanks, and Bergen?"}
                ]}
            ],
            "toolConfig": {
                "tools": [{"toolSpec": {"name": "get_weather", "description": "Weather by city",
                                        "inputSchema": {"json": schema}}}],
                "toolChoice": {"auto": {}}
            },
            "inferenceConfig": {"maxTokens": 256, "temperature": 0.2, "topP": 0.9,
                                "stopSequences": ["END"]}
        }),
        "the Converse body"
    );

    // And back: text, the tool call, `tool_use` as `tool_calls`, the usage.
    let message = &answer["choices"][0]["message"];
    assert_eq!(message["content"], json!("Checking."), "{answer}");
    assert_eq!(
        message["tool_calls"],
        json!([{"id": "tooluse_9", "type": "function",
                "function": {"name": "get_weather", "arguments": "{\"city\":\"Bergen\"}"}}]),
        "{answer}"
    );
    assert_eq!(answer["choices"][0]["finish_reason"], json!("tool_calls"));
    assert_eq!(answer["usage"]["prompt_tokens"], json!(321));
    assert_eq!(answer["usage"]["completion_tokens"], json!(12));
    assert_eq!(answer["usage"]["total_tokens"], json!(333));
    assert_eq!(answer["usage"]["cache_read_input_tokens"], json!(7));
}

/// `max_completion_tokens`, a string `stop`, a forced tool and a session
/// token: the other spellings.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_other_spellings_reach_converse(pool: PgPool) {
    let mock = converse_mock(converse_fixture("ok")).await;
    let key = topology(&pool).await;
    seed_row(
        &pool,
        MODEL,
        "bedrock_converse",
        "bedrock/converse/us.amazon.nova-lite-v1:0",
        Some(&mock.uri()),
        "chat",
        (0.06, 0.24),
    )
    .await;
    sqlx::query("UPDATE llm_model SET provider_config = $1 WHERE id = $2")
        .bind(aws_config(true))
        .bind(MODEL)
        .execute(&pool)
        .await
        .unwrap();
    let (status, answer) = post_json(
        &pool,
        "/chat/completions",
        &key,
        chat_body(json!({
            "max_completion_tokens": 99,
            "stop": "END",
            "tools": [{"type": "function", "function": {"name": "lookup"}}],
            "tool_choice": {"type": "function", "function": {"name": "lookup"}}
        })),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{answer}");
    let sent = received(&mock).await;
    assert_signed(&sent[0]);
    assert_eq!(
        sent[0]
            .headers
            .get("x-amz-security-token")
            .and_then(|v| v.to_str().ok()),
        Some(SESSION_TOKEN),
        "the session token header"
    );
    let body: Value = serde_json::from_slice(&sent[0].body).unwrap();
    assert_eq!(
        body["inferenceConfig"],
        json!({"maxTokens": 99, "stopSequences": ["END"]})
    );
    assert_eq!(
        body["toolConfig"],
        json!({
            "tools": [{"toolSpec": {"name": "lookup",
                       "inputSchema": {"json": {"type": "object", "properties": {}}}}}],
            "toolChoice": {"tool": {"name": "lookup"}}
        })
    );
}

/// `stopReason` to `finish_reason`.
#[sqlx::test(migrations = "./tests/migrations")]
async fn stop_reasons_become_finish_reasons(pool: PgPool) {
    let key = topology(&pool).await;
    let mock = MockServer::start().await;
    seed_row(
        &pool,
        MODEL,
        "bedrock",
        "bedrock/deepseek.v3.2",
        Some(&mock.uri()),
        "chat",
        (0.62, 1.85),
    )
    .await;
    for (stop_reason, finish_reason) in [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("model_context_window_exceeded", "length"),
        ("guardrail_intervened", "content_filter"),
        ("content_filtered", "content_filter"),
    ] {
        mock.reset().await;
        let mut reply = converse_fixture("x");
        reply["stopReason"] = json!(stop_reason);
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(200).set_body_json(reply))
            .mount(&mock)
            .await;
        let (status, answer) =
            post_json(&pool, "/chat/completions", &key, chat_body(json!({}))).await;
        assert_eq!(status, StatusCode::OK, "{stop_reason}: {answer}");
        assert_eq!(
            answer["choices"][0]["finish_reason"],
            json!(finish_reason),
            "{stop_reason}"
        );
    }
}

// ---------------------------------------------------------------------------
// 1b. Streaming chat through ConverseStream
// ---------------------------------------------------------------------------

async fn streamed(pool: PgPool, provider: &str, model_name: &str, path: &str) {
    let mock = stream_mock(converse_stream_fixture()).await;
    let key = topology(&pool).await;
    seed_row(
        &pool,
        MODEL,
        provider,
        model_name,
        Some(&mock.uri()),
        "chat",
        (0.8, 3.2),
    )
    .await;
    let state = common::build_test_state(pool.clone());
    let (status, bytes) = post(
        &state,
        "/chat/completions",
        &key,
        chat_body(json!({
            "stream": true,
            "tools": [{"type": "function", "function": {"name": "get_weather"}}]
        })),
    )
    .await;
    let text = String::from_utf8_lossy(&bytes).to_string();
    assert_eq!(status, StatusCode::OK, "{text}");

    let sent = received(&mock).await;
    assert_eq!(sent.len(), 1, "one ConverseStream call");
    assert_eq!(sent[0].url.path(), path);
    assert_signed(&sent[0]);
    let body: Value = serde_json::from_slice(&sent[0].body).unwrap();
    assert!(body.get("stream").is_none(), "not a Converse field: {body}");
    assert!(body.get("stream_options").is_none(), "{body}");

    let data = sse_data(&bytes);
    assert_eq!(data.last().map(String::as_str), Some("[DONE]"), "{text}");
    let chunks: Vec<Value> = data
        .iter()
        .filter(|d| d.as_str() != "[DONE]")
        .map(|d| serde_json::from_str(d).unwrap_or_else(|_| panic!("not a JSON chunk: {d}")))
        .collect();
    for c in &chunks {
        assert_eq!(c["object"], json!("chat.completion.chunk"), "{c}");
    }
    let content: String = chunks
        .iter()
        .filter_map(|c| c["choices"][0]["delta"]["content"].as_str())
        .collect();
    assert_eq!(content, "Looking it up.", "{text}");

    // The tool call: a first delta with the id and the name, then the
    // argument fragments, all on index 0.
    let tool_deltas: Vec<&Value> = chunks
        .iter()
        .filter_map(|c| c["choices"][0]["delta"]["tool_calls"].get(0))
        .collect();
    assert!(!tool_deltas.is_empty(), "tool-call deltas: {text}");
    assert_eq!(tool_deltas[0]["index"], json!(0));
    assert_eq!(tool_deltas[0]["id"], json!("tooluse_1"));
    assert_eq!(tool_deltas[0]["type"], json!("function"));
    assert_eq!(tool_deltas[0]["function"]["name"], json!("get_weather"));
    let arguments: String = tool_deltas
        .iter()
        .filter_map(|d| d["function"]["arguments"].as_str())
        .collect();
    assert_eq!(arguments, "{\"city\":\"Oslo\"}", "{text}");

    let finish: Vec<&str> = chunks
        .iter()
        .filter_map(|c| c["choices"][0]["finish_reason"].as_str())
        .collect();
    assert_eq!(finish, vec!["tool_calls"], "{text}");
    let usage: Vec<&Value> = chunks
        .iter()
        .filter(|c| c.get("usage").is_some_and(|u| u.is_object()))
        .map(|c| &c["usage"])
        .collect();
    assert_eq!(
        usage,
        vec![&json!({"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150})],
        "the usage of the metadata event: {text}"
    );

    // The stream's usage is the cost: 100 in, 50 out at 0.80 / 3.20.
    let expected = 100.0 * 0.8e-6 + 50.0 * 3.2e-6;
    let log = log_row(&pool, 200).await;
    assert_eq!((log.0, log.1), (Some(100), Some(50)), "audit row tokens");
    assert!(
        close(log.2.unwrap_or(0.0), expected),
        "cost {:?}, expected {expected}",
        log.2
    );
    let usage = usage_row(&pool).await;
    assert_eq!((usage.0, usage.1), (Some(100), Some(50)));
    assert!(close(usage.2.unwrap_or(0.0), expected), "{:?}", usage.2);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_bedrock_nova_row_streams_through_converse_stream(pool: PgPool) {
    streamed(
        pool,
        "bedrock",
        "bedrock/amazon.nova-pro-v1:0",
        "/model/amazon.nova-pro-v1%3A0/converse-stream",
    )
    .await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_bedrock_converse_nova_row_streams_through_converse_stream(pool: PgPool) {
    streamed(
        pool,
        "bedrock_converse",
        "bedrock/converse/us.amazon.nova-lite-v1:0",
        "/model/us.amazon.nova-lite-v1%3A0/converse-stream",
    )
    .await;
}

/// An exception event in the stream reaches the caller as an error and the
/// stream is not reported as complete.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_stream_with_an_exception_event_ends_with_an_error(pool: PgPool) {
    let bytes = [
        es_event("messageStart", json!({"role": "assistant"})),
        es_event(
            "contentBlockDelta",
            json!({"contentBlockIndex": 0, "delta": {"text": "par"}}),
        ),
        es_exception("modelStreamErrorException", "the model stream broke"),
    ]
    .concat();
    let mock = stream_mock(bytes).await;
    let key = topology(&pool).await;
    seed_row(
        &pool,
        MODEL,
        "bedrock",
        "bedrock/mistral.mistral-large-3-675b-instruct",
        Some(&mock.uri()),
        "chat",
        (0.5, 1.5),
    )
    .await;
    let state = common::build_test_state(pool.clone());
    let (status, bytes) = post(
        &state,
        "/chat/completions",
        &key,
        chat_body(json!({"stream": true})),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let data = sse_data(&bytes);
    let text = data.join("\n");
    assert!(text.contains("\"par\""), "{text}");
    assert!(
        text.contains("modelStreamErrorException") && text.contains("the model stream broke"),
        "the exception reaches the caller: {text}"
    );
    assert!(
        !data.iter().any(|d| d == "[DONE]"),
        "a broken stream is not reported as complete: {text}"
    );
}

// ---------------------------------------------------------------------------
// 1c. Bedrock's errors
// ---------------------------------------------------------------------------

async fn error_mock(status: u16, error_type: &str, message: &str) -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(
            ResponseTemplate::new(status)
                .insert_header("x-amzn-errortype", error_type)
                .set_body_json(json!({"message": message})),
        )
        .mount(&mock)
        .await;
    mock
}

async fn an_error_is_mapped(pool: PgPool, stream: bool, status: u16, error_type: &str) {
    let mock = error_mock(status, error_type, "bedrock said no").await;
    let key = topology(&pool).await;
    seed_row(
        &pool,
        MODEL,
        "bedrock",
        "bedrock/amazon.nova-micro-v1:0",
        Some(&mock.uri()),
        "chat",
        (0.035, 0.14),
    )
    .await;
    let state = common::build_test_state(pool.clone());
    let (got, bytes) = post(
        &state,
        "/chat/completions",
        &key,
        chat_body(json!({"stream": stream})),
    )
    .await;
    let text = String::from_utf8_lossy(&bytes).to_string();
    assert_eq!(got.as_u16(), status, "{error_type}: {text}");
    assert!(text.contains("bedrock said no"), "{error_type}: {text}");
    assert!(!text.contains(SECRET) && !text.contains(KEY_ID), "{text}");
    // Throttling is a provider rate limit for the resilience layer: the
    // model is cooling down. A validation error is the caller's: it is not.
    let cooling = state.router_state.cooldown_remaining(MODEL).is_some();
    assert_eq!(
        cooling,
        status == 429,
        "{error_type} (HTTP {status}): cooldown = {cooling}"
    );
}

macro_rules! errors_mapped {
    ($($name:ident = ($stream:expr, $status:expr, $type:expr)),+ $(,)?) => {$(
        #[sqlx::test(migrations = "./tests/migrations")]
        async fn $name(pool: PgPool) {
            an_error_is_mapped(pool, $stream, $status, $type).await;
        }
    )+};
}

errors_mapped!(
    throttling_is_a_provider_rate_limit = (false, 429, "ThrottlingException"),
    throttling_is_a_provider_rate_limit_streaming = (true, 429, "ThrottlingException"),
    model_not_ready_is_a_provider_rate_limit = (false, 429, "ModelNotReadyException"),
    a_validation_error_is_the_callers = (false, 400, "ValidationException"),
    a_validation_error_is_the_callers_streaming = (true, 400, "ValidationException"),
);

/// Access denied (403) is the row's credential, not the caller's request:
/// the caller is told, with Bedrock's message.
#[sqlx::test(migrations = "./tests/migrations")]
async fn access_denied_is_reported(pool: PgPool) {
    let mock = error_mock(403, "AccessDeniedException", "not authorized to invoke").await;
    let key = topology(&pool).await;
    seed_row(
        &pool,
        MODEL,
        "bedrock",
        "bedrock/amazon.nova-micro-v1:0",
        Some(&mock.uri()),
        "chat",
        (0.035, 0.14),
    )
    .await;
    let (status, answer) = post_json(&pool, "/chat/completions", &key, chat_body(json!({}))).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{answer}");
    assert!(
        answer.to_string().contains("not authorized to invoke"),
        "{answer}"
    );
}

// ---------------------------------------------------------------------------
// 1d. A feature a model does not support is refused, not sent
// ---------------------------------------------------------------------------

async fn a_feature_is_refused(
    pool: PgPool,
    provider: &str,
    model_name: &str,
    request: Value,
    names: &str,
) {
    let mock = converse_mock(converse_fixture("should not be asked")).await;
    let key = topology(&pool).await;
    seed_row(
        &pool,
        MODEL,
        provider,
        model_name,
        Some(&mock.uri()),
        "chat",
        (1.0, 1.0),
    )
    .await;
    let (status, answer) = post_json(&pool, "/chat/completions", &key, chat_body(request)).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{model_name}: {answer}");
    assert!(
        answer.to_string().contains(names),
        "{model_name}: the refusal names `{names}`: {answer}"
    );
    assert!(
        received(&mock).await.is_empty(),
        "{model_name}: nothing may be sent"
    );
}

fn image_message() -> Value {
    json!([{"role": "user", "content": [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQ"}}
    ]}])
}

fn one_tool() -> Value {
    json!([{"type": "function", "function": {"name": "lookup"}}])
}

macro_rules! features_refused {
    ($($name:ident = ($provider:expr, $model:expr, $request:expr, $names:expr)),+ $(,)?) => {$(
        #[sqlx::test(migrations = "./tests/migrations")]
        async fn $name(pool: PgPool) {
            a_feature_is_refused(pool, $provider, $model, $request, $names).await;
        }
    )+};
}

features_refused!(
    an_image_to_nova_micro_is_refused = (
        "bedrock",
        "bedrock/amazon.nova-micro-v1:0",
        json!({"messages": image_message()}),
        "image input"
    ),
    an_image_to_deepseek_is_refused = (
        "bedrock_converse",
        "bedrock/converse/deepseek.v3.2",
        json!({"messages": image_message()}),
        "image input"
    ),
    tools_to_mistral_large_3_are_refused = (
        "bedrock",
        "bedrock/mistral.mistral-large-3-675b-instruct",
        json!({"tools": one_tool()}),
        "tools"
    ),
    tools_to_deepseek_are_refused = (
        "bedrock",
        "bedrock/deepseek.v3.2",
        json!({"tools": one_tool()}),
        "tools"
    ),
    streamed_tools_to_nova_2_lite_are_refused = (
        "bedrock",
        "bedrock/global.amazon.nova-2-lite-v1:0",
        json!({"tools": one_tool(), "stream": true}),
        "tools with streaming"
    ),
    an_image_by_url_is_refused = (
        "bedrock",
        "bedrock/amazon.nova-pro-v1:0",
        json!({"messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://example.invalid/cat.png"}}
        ]}]}),
        "image URL"
    ),
    an_audio_part_is_refused = (
        "bedrock",
        "bedrock/amazon.nova-pro-v1:0",
        json!({"messages": [{"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}}
        ]}]}),
        "input_audio"
    ),
);

// ---------------------------------------------------------------------------
// 1e. The rest of the pipeline runs: guardrails, fallbacks, routing groups
// ---------------------------------------------------------------------------

/// An input guardrail blocks before anything is sent to Bedrock.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_input_guardrail_blocks_before_bedrock(pool: PgPool) {
    let mock = converse_mock(converse_fixture("should not be asked")).await;
    let key = topology(&pool).await;
    seed_row(
        &pool,
        MODEL,
        "bedrock",
        "bedrock/amazon.nova-lite-v1:0",
        Some(&mock.uri()),
        "chat",
        (0.06, 0.24),
    )
    .await;
    common::seed_llm_banned_words_guardrail(&pool, "g-f600", T, &["zebracorn"], true, true).await;
    common::bind_guardrail_to_rg(&pool, "g-f600", RG).await;
    let (status, answer) = post_json(
        &pool,
        "/chat/completions",
        &key,
        json!({"model_id": MODEL,
               "messages": [{"role": "user", "content": "tell me about the zebracorn"}]}),
    )
    .await;
    assert!(
        status.is_client_error(),
        "the input guardrail refuses: {status} {answer}"
    );
    assert!(
        received(&mock).await.is_empty(),
        "nothing may reach Bedrock"
    );
}

/// An output guardrail reads what Bedrock answered.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_output_guardrail_reads_bedrocks_answer(pool: PgPool) {
    let mock = converse_mock(converse_fixture("the zebracorn is real")).await;
    let key = topology(&pool).await;
    seed_row(
        &pool,
        MODEL,
        "bedrock",
        "bedrock/amazon.nova-lite-v1:0",
        Some(&mock.uri()),
        "chat",
        (0.06, 0.24),
    )
    .await;
    common::seed_llm_banned_words_guardrail(&pool, "g-f600", T, &["zebracorn"], false, true).await;
    common::bind_guardrail_to_rg(&pool, "g-f600", RG).await;
    let (status, answer) = post_json(&pool, "/chat/completions", &key, chat_body(json!({}))).await;
    assert_eq!(received(&mock).await.len(), 1, "Bedrock was asked");
    assert!(
        !answer.to_string().contains("zebracorn is real"),
        "the output guardrail withholds the answer: {status} {answer}"
    );
}

fn openai_fixture(content: &str) -> Value {
    json!({
        "id": "chatcmpl-f600", "object": "chat.completion", "created": 1_700_000_000,
        "model": "gpt-test",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    })
}

async fn seed_openai_row(pool: &PgPool, id: &str, api_url: &str) {
    common::seed_llm_provider(pool, "prov-f600-openai", T, "openai").await;
    common::seed_llm_model_with_api_url(pool, id, T, "prov-f600-openai", id, api_url).await;
    common::set_model_api_key(pool, id, "sk-f600-openai").await;
    common::bind_llm_model_to_rg(pool, id, RG).await;
}

/// An OpenAI row fails; its fallback is a Converse row.
async fn openai_falls_back_to_converse(pool: PgPool, stream: bool) {
    let primary = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(500).set_body_json(json!({"error": "down"})))
        .mount(&primary)
        .await;
    let bedrock = if stream {
        stream_mock(converse_stream_fixture()).await
    } else {
        converse_mock(converse_fixture("from bedrock")).await
    };
    let key = topology(&pool).await;
    seed_openai_row(&pool, MODEL, &primary.uri()).await;
    seed_row(
        &pool,
        "m-f600-nova",
        "bedrock",
        "bedrock/amazon.nova-pro-v1:0",
        Some(&bedrock.uri()),
        "chat",
        (0.8, 3.2),
    )
    .await;
    common::set_model_fallback_models(&pool, MODEL, &["m-f600-nova"]).await;
    let state = common::build_test_state(pool.clone());
    let (status, bytes) = post(
        &state,
        "/chat/completions",
        &key,
        chat_body(json!({"stream": stream})),
    )
    .await;
    let text = String::from_utf8_lossy(&bytes).to_string();
    assert_eq!(status, StatusCode::OK, "{text}");
    let sent = received(&bedrock).await;
    assert_eq!(sent.len(), 1, "the fallback was asked: {text}");
    assert_signed(&sent[0]);
    if stream {
        assert!(
            text.contains("Looking ") && text.contains("[DONE]"),
            "{text}"
        );
    } else {
        assert!(text.contains("from bedrock"), "{text}");
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_openai_row_falls_back_to_a_converse_row(pool: PgPool) {
    openai_falls_back_to_converse(pool, false).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_openai_row_falls_back_to_a_converse_row_streaming(pool: PgPool) {
    openai_falls_back_to_converse(pool, true).await;
}

/// A Converse row fails; its fallback is an OpenAI row.
async fn converse_falls_back_to_openai(pool: PgPool, stream: bool) {
    let bedrock = error_mock(500, "InternalServerException", "internal").await;
    let fallback = MockServer::start().await;
    let reply = if stream {
        ResponseTemplate::new(200).set_body_raw(
            format!(
                "data: {}\n\ndata: [DONE]\n\n",
                json!({"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
                       "choices": [{"index": 0, "delta": {"content": "from openai"},
                                    "finish_reason": "stop"}]})
            ),
            "text/event-stream",
        )
    } else {
        ResponseTemplate::new(200).set_body_json(openai_fixture("from openai"))
    };
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(reply)
        .mount(&fallback)
        .await;
    let key = topology(&pool).await;
    seed_row(
        &pool,
        MODEL,
        "bedrock_converse",
        "bedrock/converse/mistral.mistral-large-3-675b-instruct",
        Some(&bedrock.uri()),
        "chat",
        (0.5, 1.5),
    )
    .await;
    seed_openai_row(&pool, "m-f600-openai", &fallback.uri()).await;
    common::set_model_fallback_models(&pool, MODEL, &["m-f600-openai"]).await;
    let state = common::build_test_state(pool.clone());
    let (status, bytes) = post(
        &state,
        "/chat/completions",
        &key,
        chat_body(json!({"stream": stream})),
    )
    .await;
    let text = String::from_utf8_lossy(&bytes).to_string();
    assert_eq!(status, StatusCode::OK, "{text}");
    assert!(text.contains("from openai"), "{text}");
    let tried = received(&bedrock).await;
    assert_eq!(tried.len(), 1, "Bedrock was tried first");
    assert_signed(&tried[0]);
    // The OpenAI fallback gets an OpenAI request with its own key, and
    // nothing of the AWS credential.
    let sent = received(&fallback).await;
    assert_eq!(sent.len(), 1);
    assert_eq!(
        sent[0]
            .headers
            .get("authorization")
            .and_then(|v| v.to_str().ok()),
        Some("Bearer sk-f600-openai")
    );
    for (name, value) in sent[0].headers.iter() {
        let value = value.to_str().unwrap_or_default();
        assert!(
            !value.contains(SECRET) && !value.contains(KEY_ID) && !name.as_str().contains("amz"),
            "the fallback got AWS material in {name}"
        );
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_converse_row_falls_back_to_an_openai_row(pool: PgPool) {
    converse_falls_back_to_openai(pool, false).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_converse_row_falls_back_to_an_openai_row_streaming(pool: PgPool) {
    converse_falls_back_to_openai(pool, true).await;
}

/// A routing group whose only member is a Converse row.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_converse_row_is_a_routing_group_member(pool: PgPool) {
    let bedrock = converse_mock(converse_fixture("from the member")).await;
    let key = topology(&pool).await;
    seed_row(
        &pool,
        "m-f600-member",
        "bedrock",
        "bedrock/amazon.nova-lite-v1:0",
        Some(&bedrock.uri()),
        "chat",
        (0.06, 0.24),
    )
    .await;
    common::seed_routing_group(&pool, "grp-f600", T, "prov-f600-bedrock", "simple").await;
    sqlx::query("UPDATE llm_model SET routing_group_id = 'grp-f600' WHERE id = 'm-f600-member'")
        .execute(&pool)
        .await
        .unwrap();
    common::bind_llm_model_to_rg(&pool, "rg-grp-f600", RG).await;
    let (status, answer) = post_json(
        &pool,
        "/chat/completions",
        &key,
        json!({"model_id": "rg-grp-f600", "messages": [{"role": "user", "content": "ping"}]}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{answer}");
    assert_eq!(
        answer["choices"][0]["message"]["content"],
        json!("from the member")
    );
    let sent = received(&bedrock).await;
    assert_eq!(sent.len(), 1);
    assert_eq!(
        sent[0].url.path(),
        "/model/amazon.nova-lite-v1%3A0/converse"
    );
    assert_signed(&sent[0]);
}

// ---------------------------------------------------------------------------
// 2. Embeddings through InvokeModel
// ---------------------------------------------------------------------------

/// Titan answers one text per call. The calls of one request overlap, so
/// the mock cannot number them: the vector it answers is derived from the
/// text (its length), which is what lets a test check the order.
struct Titan;

impl wiremock::Respond for Titan {
    fn respond(&self, req: &wiremock::Request) -> ResponseTemplate {
        let body: Value = serde_json::from_slice(&req.body).unwrap_or(Value::Null);
        let text = body["inputText"].as_str().unwrap_or_default();
        if text.contains("FAIL") {
            return ResponseTemplate::new(400)
                .insert_header("x-amzn-errortype", "ValidationException")
                .set_body_json(json!({"message": "Malformed input request"}));
        }
        ResponseTemplate::new(200).set_body_json(json!({
            "embedding": [text.len() as f64, 0.5],
            "inputTextTokenCount": text.len(),
            "embeddingsByType": {"float": [text.len() as f64, 0.5]}
        }))
    }
}

async fn titan_setup(pool: &PgPool) -> (MockServer, String) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(Titan)
        .mount(&mock)
        .await;
    let key = topology(pool).await;
    seed_row(
        pool,
        MODEL,
        "bedrock",
        "bedrock/amazon.titan-embed-text-v2:0",
        Some(&mock.uri()),
        "embedding",
        (0.02, 0.0),
    )
    .await;
    (mock, key)
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn bedrock_titan_embed_v2_is_served(pool: PgPool) {
    let (mock, key) = titan_setup(&pool).await;
    let (status, answer) = post_json(
        &pool,
        "/embeddings",
        &key,
        json!({"model_id": MODEL, "input": "hello", "dimensions": 256,
               "encoding_format": "float"}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{answer}");
    let sent = received(&mock).await;
    assert_eq!(sent.len(), 1);
    assert_eq!(
        sent[0].url.path(),
        "/model/amazon.titan-embed-text-v2%3A0/invoke"
    );
    assert_signed(&sent[0]);
    assert_eq!(
        serde_json::from_slice::<Value>(&sent[0].body).unwrap(),
        json!({"inputText": "hello", "dimensions": 256})
    );
    assert_eq!(answer["object"], json!("list"));
    assert_eq!(
        answer["data"],
        json!([{"object": "embedding", "index": 0, "embedding": [5.0, 0.5]}])
    );
    assert_eq!(
        answer["usage"],
        json!({"prompt_tokens": 5, "total_tokens": 5})
    );
    // 5 tokens at 0.02 per million.
    let log = log_row(&pool, 200).await;
    assert_eq!(log.0, Some(5));
    assert!(close(log.2.unwrap_or(-1.0), 5.0 * 0.02e-6), "{:?}", log.2);
    let usage = usage_row(&pool).await;
    assert_eq!(usage.0, Some(5));
    assert!(
        close(usage.2.unwrap_or(-1.0), 5.0 * 0.02e-6),
        "{:?}",
        usage.2
    );
}

/// An array input is one call per item; the results come back in the
/// order of the input and the token counts are added.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_titan_array_is_one_call_per_item_in_order(pool: PgPool) {
    let (mock, key) = titan_setup(&pool).await;
    let texts: Vec<String> = (1..=11).map(|n| "x".repeat(n)).collect();
    let (status, answer) = post_json(
        &pool,
        "/embeddings",
        &key,
        // `base64` is answered with floats, as for every provider but
        // OpenAI and Azure (`providers::prepare_embedding`).
        json!({"model_id": MODEL, "input": texts, "encoding_format": "base64"}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{answer}");
    let sent = received(&mock).await;
    assert_eq!(sent.len(), 11, "one InvokeModel call per item");
    for r in &sent {
        assert_signed(r);
    }
    let data = answer["data"].as_array().expect("data");
    assert_eq!(data.len(), 11);
    for (i, item) in data.iter().enumerate() {
        assert_eq!(item["index"], json!(i));
        assert_eq!(item["embedding"], json!([(i + 1) as f64, 0.5]), "item {i}");
    }
    // 1 + 2 + … + 11.
    assert_eq!(
        answer["usage"],
        json!({"prompt_tokens": 66, "total_tokens": 66})
    );
    let usage = usage_row(&pool).await;
    assert_eq!(usage.0, Some(66));
}

/// One item fails: the request fails with Bedrock's error and nothing is
/// retried. The sibling calls that succeeded are billed by AWS: their
/// tokens are recorded, costed, on the audit row of the failed request and
/// as one usage row with the status the request failed with, and the error
/// the caller gets says so. No embedding is returned.
#[sqlx::test(migrations = "./tests/migrations")]
async fn one_failed_titan_item_fails_the_request(pool: PgPool) {
    let (mock, key) = titan_setup(&pool).await;
    let (status, answer) = post_json(
        &pool,
        "/embeddings",
        &key,
        json!({"model_id": MODEL, "input": ["abc", "FAIL", "defgh"]}),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{answer}");
    assert!(
        answer.to_string().contains("Malformed input request"),
        "{answer}"
    );
    assert!(answer.get("data").is_none(), "no partial result: {answer}");
    let sent = received(&mock).await;
    assert_eq!(sent.len(), 3, "nothing is retried: {} calls", sent.len());
    let failures = sent
        .iter()
        .filter(|r| String::from_utf8_lossy(&r.body).contains("FAIL"))
        .count();
    assert_eq!(failures, 1, "the failed item is sent once");

    // The two calls that succeeded used 3 + 5 tokens (the mock counts the
    // characters), at $0.02 per million.
    let cost = 8.0 * 0.02 / 1_000_000.0;
    assert_eq!(answer["usage"]["prompt_tokens"], json!(8), "{answer}");
    assert!(
        answer["error"]["message"]
            .as_str()
            .unwrap_or_default()
            .contains("2 of the 3 calls"),
        "the caller is told what was billed: {answer}"
    );
    let row = log_row(&pool, 400).await;
    assert_eq!(row.0, Some(8), "the audit row carries the tokens: {row:?}");
    assert!(row.2.is_some_and(|c| close(c, cost)), "{row:?}");
    let usage = usage_row(&pool).await;
    assert_eq!(usage.0, Some(8), "{usage:?}");
    assert!(usage.2.is_some_and(|c| close(c, cost)), "{usage:?}");
    let rows: Vec<(Option<i32>, Option<String>)> =
        sqlx::query_as("SELECT status_code, model_name FROM usage_logs WHERE tenant_id = $1")
            .bind(T)
            .fetch_all(&pool)
            .await
            .unwrap();
    assert_eq!(
        rows,
        vec![(
            Some(400),
            Some("bedrock/amazon.titan-embed-text-v2:0".to_string())
        )],
        "one usage row, of the row that was called, marked with the failure"
    );
}

/// A request whose every call fails records no usage.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_titan_request_with_no_successful_call_records_no_usage(pool: PgPool) {
    let (_mock, key) = titan_setup(&pool).await;
    let (status, answer) = post_json(
        &pool,
        "/embeddings",
        &key,
        json!({"model_id": MODEL, "input": ["FAIL"]}),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{answer}");
    assert!(answer.get("usage").is_none(), "{answer}");
    log_row(&pool, 400).await;
    let (n,): (i64,) = sqlx::query_as("SELECT count(*) FROM usage_logs WHERE tenant_id = $1")
        .bind(T)
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(n, 0, "a request with no successful call records no usage");
}

/// Titan takes 256, 512 or 1024 dimensions: another value is the caller's
/// error and is not sent.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_dimension_titan_does_not_have_is_refused(pool: PgPool) {
    let (mock, key) = titan_setup(&pool).await;
    let (status, answer) = post_json(
        &pool,
        "/embeddings",
        &key,
        json!({"model_id": MODEL, "input": "hello", "dimensions": 300}),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{answer}");
    assert!(answer.to_string().contains("dimensions"), "{answer}");
    assert!(received(&mock).await.is_empty());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn bedrock_cohere_embed_english_v3_is_served(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "embeddings": [[0.1, 0.2], [0.3, 0.4]],
            "id": "f600", "response_type": "embeddings_floats",
            "texts": ["first text", "second"]
        })))
        .mount(&mock)
        .await;
    let key = topology(&pool).await;
    seed_row(
        &pool,
        MODEL,
        "bedrock",
        "bedrock/cohere.embed-english-v3",
        Some(&mock.uri()),
        "embedding",
        (0.10, 0.0),
    )
    .await;
    let (status, answer) = post_json(
        &pool,
        "/embeddings",
        &key,
        json!({"model_id": MODEL, "input": ["first text", "second"]}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{answer}");
    let sent = received(&mock).await;
    assert_eq!(sent.len(), 1, "one call for the batch");
    assert_eq!(sent[0].url.path(), "/model/cohere.embed-english-v3/invoke");
    assert_signed(&sent[0]);
    assert_eq!(
        serde_json::from_slice::<Value>(&sent[0].body).unwrap(),
        json!({"texts": ["first text", "second"], "input_type": "search_document"})
    );
    assert_eq!(
        answer["data"],
        json!([
            {"object": "embedding", "index": 0, "embedding": [0.1, 0.2]},
            {"object": "embedding", "index": 1, "embedding": [0.3, 0.4]}
        ])
    );
    // AWS documents no token count in this response. The gateway records
    // the documented approximation, one token per four characters, rounded
    // up per text: "first text" is 3, "second" is 2.
    assert_eq!(
        answer["usage"],
        json!({"prompt_tokens": 5, "total_tokens": 5})
    );
    let log = log_row(&pool, 200).await;
    assert_eq!(log.0, Some(5));
    assert!(close(log.2.unwrap_or(-1.0), 5.0 * 0.10e-6), "{:?}", log.2);
}

// ---------------------------------------------------------------------------
// 3. What is still refused
// ---------------------------------------------------------------------------

async fn refused(pool: &PgPool, route: &str, key: &str, body: Value, what: &str) {
    let (status, answer) = post_json(pool, route, key, body).await;
    assert_eq!(status, StatusCode::NOT_IMPLEMENTED, "{what}: {answer}");
    assert!(
        answer.to_string().contains("unsupported_provider_api"),
        "{what}: {answer}"
    );
}

/// Every Bedrock row that is neither Claude nor an implemented family is
/// refused on every route, and an implemented family is refused on the
/// routes it is not served on. Nothing reaches the row's `api_url`.
#[sqlx::test(migrations = "./tests/migrations")]
async fn everything_else_on_bedrock_is_still_refused(pool: PgPool) {
    let mock = converse_mock(converse_fixture("should not be asked")).await;
    let key = topology(&pool).await;
    let chat = |id: &str| json!({"model_id": id, "messages": [{"role": "user", "content": "x"}]});
    let embed = |id: &str| json!({"model_id": id, "input": "x"});

    // (provider, model name, mode): families the gateway does not implement.
    let others = [
        ("bedrock", "bedrock/meta.llama3-3-70b-instruct-v1:0", "chat"),
        ("bedrock", "bedrock/amazon.titan-text-express-v1", "chat"),
        ("bedrock", "bedrock/cohere.command-r-plus-v1:0", "chat"),
        ("bedrock", "bedrock/ai21.jamba-1-5-large-v1:0", "chat"),
        ("bedrock", "bedrock/openai.gpt-oss-120b-1:0", "chat"),
        (
            "bedrock_converse",
            "bedrock/converse/us.meta.llama4-maverick-17b-instruct-v1:0",
            "chat",
        ),
        (
            "bedrock_converse",
            "bedrock/converse/qwen.qwen3-32b-v1:0",
            "chat",
        ),
        (
            "bedrock",
            "bedrock/amazon.titan-embed-image-v1",
            "embedding",
        ),
        ("bedrock", "bedrock/cohere.embed-v4:0", "embedding"),
        (
            "bedrock",
            "bedrock/amazon.nova-2-multimodal-embeddings-v1:0",
            "embedding",
        ),
        ("bedrock", "bedrock/stability.sd3-5-large-v1:0", "chat"),
        // An application inference profile names no family.
        (
            "bedrock",
            "arn:aws:bedrock:eu-west-1:123456789012:application-inference-profile/abc123def456",
            "chat",
        ),
    ];
    for (i, (provider, name, mode)) in others.iter().enumerate() {
        let id = format!("m-f600-other-{i}");
        seed_row(
            &pool,
            &id,
            provider,
            name,
            Some(&mock.uri()),
            mode,
            (1.0, 1.0),
        )
        .await;
        refused(&pool, "/chat/completions", &key, chat(&id), name).await;
        refused(&pool, "/embeddings", &key, embed(&id), name).await;
    }

    // The implemented families, on the routes they are not served on.
    let nova = "m-f600-nova-chat";
    seed_row(
        &pool,
        nova,
        "bedrock",
        "bedrock/amazon.nova-pro-v1:0",
        Some(&mock.uri()),
        "chat",
        (0.8, 3.2),
    )
    .await;
    refused(
        &pool,
        "/embeddings",
        &key,
        embed(nova),
        "Nova on /embeddings",
    )
    .await;
    refused(
        &pool,
        "/responses",
        &key,
        json!({"model_id": nova, "input": "x"}),
        "Nova on /responses",
    )
    .await;
    refused(
        &pool,
        "/completions",
        &key,
        json!({"model_id": nova, "prompt": "x"}),
        "Nova on /completions",
    )
    .await;
    for (i, name) in [
        "bedrock/amazon.titan-embed-text-v2:0",
        "bedrock/cohere.embed-english-v3",
    ]
    .iter()
    .enumerate()
    {
        let id = format!("m-f600-embed-{i}");
        // An embedding row an operator registered as a chat model.
        seed_row(
            &pool,
            &id,
            "bedrock",
            name,
            Some(&mock.uri()),
            "chat",
            (0.02, 0.0),
        )
        .await;
        refused(&pool, "/chat/completions", &key, chat(&id), name).await;
        refused(
            &pool,
            "/responses",
            &key,
            json!({"model_id": id, "input": "x"}),
            name,
        )
        .await;
    }
    assert!(
        received(&mock).await.is_empty(),
        "a refused row is not sent anywhere"
    );
}

/// The fallback-candidate check follows the same rule: an embedding row is
/// not a chat fallback, a chat row is not an embeddings fallback, and an
/// unimplemented family is neither.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_fallback_follows_the_same_rule(pool: PgPool) {
    let primary = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(500).set_body_json(json!({"error": "down"})))
        .mount(&primary)
        .await;
    let bedrock = converse_mock(converse_fixture("should not be asked")).await;
    let key = topology(&pool).await;
    seed_openai_row(&pool, MODEL, &primary.uri()).await;
    common::seed_llm_model_with_mode(
        &pool,
        "m-f600-openai-embed",
        T,
        "prov-f600-openai",
        "text-embedding-test",
        &primary.uri(),
        "embedding",
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "m-f600-openai-embed", RG).await;
    for (id, name, mode) in [
        (
            "m-f600-fb-titan",
            "bedrock/amazon.titan-embed-text-v2:0",
            "chat",
        ),
        (
            "m-f600-fb-llama",
            "bedrock/meta.llama3-3-70b-instruct-v1:0",
            "chat",
        ),
        (
            "m-f600-fb-nova",
            "bedrock/amazon.nova-pro-v1:0",
            "embedding",
        ),
    ] {
        seed_row(
            &pool,
            id,
            "bedrock",
            name,
            Some(&bedrock.uri()),
            mode,
            (1.0, 1.0),
        )
        .await;
    }
    common::set_model_fallback_models(&pool, MODEL, &["m-f600-fb-titan", "m-f600-fb-llama"]).await;
    common::set_model_fallback_models(
        &pool,
        "m-f600-openai-embed",
        &["m-f600-fb-nova", "m-f600-fb-llama"],
    )
    .await;
    for stream in [false, true] {
        let (status, answer) = post_json(
            &pool,
            "/chat/completions",
            &key,
            chat_body(json!({"stream": stream})),
        )
        .await;
        assert!(!status.is_success(), "stream={stream}: {status} {answer}");
    }
    let (status, answer) = post_json(
        &pool,
        "/embeddings",
        &key,
        json!({"model_id": "m-f600-openai-embed", "input": "x"}),
    )
    .await;
    assert!(!status.is_success(), "{status} {answer}");
    assert!(
        received(&bedrock).await.is_empty(),
        "a candidate the gateway does not serve on the route is not sent"
    );
}

// ---------------------------------------------------------------------------
// 4. Security properties
// ---------------------------------------------------------------------------

/// Everything `tracing` emits while the guard lives: every field of every
/// event and of every span, at every level.
#[derive(Clone, Default)]
struct Captured(Arc<Mutex<String>>);

impl Captured {
    fn text(&self) -> String {
        self.0.lock().expect("log buffer").clone()
    }
}

struct FieldText<'a>(&'a mut String);

impl tracing::field::Visit for FieldText<'_> {
    fn record_debug(&mut self, field: &tracing::field::Field, value: &dyn std::fmt::Debug) {
        self.0.push_str(&format!(" {}={value:?}", field.name()));
    }
}

impl tracing::Subscriber for Captured {
    fn enabled(&self, _: &tracing::Metadata<'_>) -> bool {
        true
    }
    fn new_span(&self, span: &tracing::span::Attributes<'_>) -> tracing::span::Id {
        let mut line = format!("span {}", span.metadata().name());
        span.record(&mut FieldText(&mut line));
        let mut buf = self.0.lock().expect("log buffer");
        buf.push_str(&line);
        buf.push('\n');
        tracing::span::Id::from_u64(1)
    }
    fn record(&self, _: &tracing::span::Id, values: &tracing::span::Record<'_>) {
        let mut line = String::from("record");
        values.record(&mut FieldText(&mut line));
        let mut buf = self.0.lock().expect("log buffer");
        buf.push_str(&line);
        buf.push('\n');
    }
    fn record_follows_from(&self, _: &tracing::span::Id, _: &tracing::span::Id) {}
    fn event(&self, event: &tracing::Event<'_>) {
        let mut line = format!("{} {}", event.metadata().level(), event.metadata().target());
        event.record(&mut FieldText(&mut line));
        let mut buf = self.0.lock().expect("log buffer");
        buf.push_str(&line);
        buf.push('\n');
    }
    fn enter(&self, _: &tracing::span::Id) {}
    fn exit(&self, _: &tracing::span::Id) {}
}

/// Every text and JSON column of every row of the tables a request writes.
async fn stored_text(pool: &PgPool) -> String {
    let mut all = String::new();
    for table in [
        "proxy_logs",
        "usage_logs",
        "action_records",
        "proxy_metrics",
        "evidence_receipts",
    ] {
        let rows: Vec<(String,)> =
            sqlx::query_as(&format!("SELECT row_to_json(t)::text FROM {table} t"))
                .fetch_all(pool)
                .await
                .unwrap_or_else(|e| panic!("read {table}: {e}"));
        for (row,) in rows {
            all.push_str(&row);
            all.push('\n');
        }
    }
    all
}

/// The AWS credential appears in no log line, no audit row, no evidence
/// record and no answer: on a served call, a streamed call, an error from
/// Bedrock, an embeddings call and a refused feature.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_aws_credential_stays_in_the_gateway(pool: PgPool) {
    let logs = Captured::default();
    let _guard = tracing::subscriber::set_default(logs.clone());

    let chat_mock = converse_mock(converse_fixture("pong")).await;
    let stream = stream_mock(converse_stream_fixture()).await;
    let failing = error_mock(403, "AccessDeniedException", "no access").await;
    let titan = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(Titan)
        .mount(&titan)
        .await;
    let key = topology(&pool).await;
    for (id, name, url, mode) in [
        (
            MODEL,
            "bedrock/amazon.nova-pro-v1:0",
            chat_mock.uri(),
            "chat",
        ),
        (
            "m-f600-stream",
            "bedrock/amazon.nova-lite-v1:0",
            stream.uri(),
            "chat",
        ),
        (
            "m-f600-denied",
            "bedrock/deepseek.v3.2",
            failing.uri(),
            "chat",
        ),
        (
            "m-f600-titan",
            "bedrock/amazon.titan-embed-text-v2:0",
            titan.uri(),
            "embedding",
        ),
    ] {
        seed_row(&pool, id, "bedrock", name, Some(&url), mode, (1.0, 1.0)).await;
        sqlx::query("UPDATE llm_model SET provider_config = $1 WHERE id = $2")
            .bind(aws_config(true))
            .bind(id)
            .execute(&pool)
            .await
            .unwrap();
    }
    let state = common::build_test_state(pool.clone());
    let chat = |id: &str, extra: Value| {
        let mut b = chat_body(extra);
        b["model_id"] = json!(id);
        b
    };
    let mut answers = String::new();
    for (route, body) in [
        ("/chat/completions", chat(MODEL, json!({}))),
        (
            "/chat/completions",
            chat("m-f600-stream", json!({"stream": true})),
        ),
        ("/chat/completions", chat("m-f600-denied", json!({}))),
        (
            "/chat/completions",
            chat("m-f600-denied", json!({"stream": true})),
        ),
        (
            "/chat/completions",
            chat("m-f600-denied", json!({"tools": one_tool()})),
        ),
        (
            "/embeddings",
            json!({"model_id": "m-f600-titan", "input": ["a", "bb"]}),
        ),
        (
            "/embeddings",
            json!({"model_id": "m-f600-titan", "input": ["a", "FAIL"]}),
        ),
        ("/embeddings", json!({"model_id": MODEL, "input": "x"})),
    ] {
        let (status, bytes) = post(&state, route, &key, body).await;
        answers.push_str(&format!("{status} {}\n", String::from_utf8_lossy(&bytes)));
    }
    // The rows are written in the background.
    log_row(&pool, 403).await;
    log_row(&pool, 501).await;
    tokio::time::sleep(std::time::Duration::from_millis(400)).await;
    let stored = stored_text(&pool).await;
    let logged = logs.text();

    // The capture works: the forwarder's own line is in it, and the rows
    // are there.
    assert!(
        logged.contains("amazon.nova-pro-v1:0"),
        "the log capture saw nothing of the request"
    );
    assert!(stored.contains("amazon.nova-pro-v1:0"), "no audit row read");
    assert!(answers.contains("pong") && answers.contains("no access"));

    for (what, text) in [
        ("an answer", &answers),
        ("a stored row", &stored),
        ("a log line", &logged),
    ] {
        for (name, secret) in [
            ("the secret access key", SECRET),
            ("the session token", SESSION_TOKEN),
            ("the access key id", KEY_ID),
        ] {
            assert!(
                !text.contains(secret),
                "{name} appears in {what} ({} bytes searched)",
                text.len()
            );
        }
    }
}

/// A row with no AWS credential is refused: nothing is sent unsigned, and
/// nothing is looked up outside the row.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_row_without_a_credential_is_not_sent(pool: PgPool) {
    let mock = converse_mock(converse_fixture("should not be asked")).await;
    let key = topology(&pool).await;
    for (id, name, mode) in [
        (MODEL, "bedrock/amazon.nova-pro-v1:0", "chat"),
        (
            "m-f600-titan",
            "bedrock/amazon.titan-embed-text-v2:0",
            "embedding",
        ),
    ] {
        seed_row(
            &pool,
            id,
            "bedrock",
            name,
            Some(&mock.uri()),
            mode,
            (1.0, 1.0),
        )
        .await;
        sqlx::query("UPDATE llm_model SET provider_config = NULL WHERE id = $1")
            .bind(id)
            .execute(&pool)
            .await
            .unwrap();
    }
    for (route, body) in [
        ("/chat/completions", chat_body(json!({}))),
        ("/chat/completions", chat_body(json!({"stream": true}))),
        (
            "/embeddings",
            json!({"model_id": "m-f600-titan", "input": "x"}),
        ),
    ] {
        let (status, answer) = post_json(&pool, route, &key, body).await;
        assert!(!status.is_success(), "{route}: {status} {answer}");
        assert!(
            answer.to_string().contains("AWS credential"),
            "{route}: the refusal says why: {answer}"
        );
    }
    assert!(
        received(&mock).await.is_empty(),
        "a request without a credential is not sent"
    );
}

/// Every destination the gateway's HTTP client tried to reach: an HTTP
/// proxy on loopback that answers 502 and never forwards.
pub(crate) struct Egress {
    addr: std::net::SocketAddr,
    seen: Arc<Mutex<Vec<String>>>,
}

impl Egress {
    pub(crate) async fn start() -> Self {
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
                    let line = head.lines().next().unwrap_or_default().to_string();
                    let leaked = head.contains(SECRET) || head.contains(KEY_ID);
                    log.lock().expect("egress log").push(if leaked {
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

    pub(crate) fn seen(&self) -> Vec<String> {
        self.seen.lock().expect("egress log").clone()
    }

    pub(crate) fn state(&self, pool: PgPool) -> SharedState {
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

/// A row as the catalog seeds it (no `api_url`) goes to the Bedrock Runtime
/// host of the row's region and to no other host, on every route it is
/// served on. The recorder never forwards: nothing leaves this machine.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_row_goes_only_to_the_bedrock_runtime_host_of_its_region(pool: PgPool) {
    let egress = Egress::start().await;
    let key = topology(&pool).await;
    for (id, provider, name, mode) in [
        (MODEL, "bedrock", "bedrock/amazon.nova-pro-v1:0", "chat"),
        (
            "m-f600-conv",
            "bedrock_converse",
            "bedrock/converse/us.amazon.nova-lite-v1:0",
            "chat",
        ),
        (
            "m-f600-titan",
            "bedrock",
            "bedrock/amazon.titan-embed-text-v2:0",
            "embedding",
        ),
        (
            "m-f600-cohere",
            "bedrock",
            "bedrock/cohere.embed-english-v3",
            "embedding",
        ),
    ] {
        seed_row(&pool, id, provider, name, None, mode, (1.0, 1.0)).await;
    }
    let state = egress.state(pool.clone());
    let chat = |id: &str, stream: bool| {
        json!({"model_id": id, "stream": stream,
               "messages": [{"role": "user", "content": "ping"}]})
    };
    for (route, body) in [
        ("/chat/completions", chat(MODEL, false)),
        ("/chat/completions", chat(MODEL, true)),
        ("/chat/completions", chat("m-f600-conv", false)),
        ("/chat/completions", chat("m-f600-conv", true)),
        (
            "/embeddings",
            json!({"model_id": "m-f600-titan", "input": ["a", "b"]}),
        ),
        (
            "/embeddings",
            json!({"model_id": "m-f600-cohere", "input": "a"}),
        ),
    ] {
        let before = egress.seen().len();
        let (status, bytes) = post(&state, route, &key, body).await;
        let seen = egress.seen();
        assert!(
            seen.len() > before,
            "{route}: the row was sent for ({status}: {})",
            String::from_utf8_lossy(&bytes)
        );
        assert!(!status.is_success(), "the recorder answers 502");
    }
    let host = format!("CONNECT bedrock-runtime.{REGION}.amazonaws.com:443 ");
    let elsewhere: Vec<String> = egress
        .seen()
        .into_iter()
        .filter(|l| !l.starts_with(&host))
        .collect();
    assert!(
        elsewhere.is_empty(),
        "the gateway tried to reach something other than the region's Bedrock Runtime: {elsewhere:?}"
    );
}

/// A region that is not a region name builds no host: a value like
/// `x.attacker.example/` would put the signed request on another host.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_region_that_is_not_a_region_is_not_sent_anywhere(pool: PgPool) {
    let egress = Egress::start().await;
    let key = topology(&pool).await;
    seed_row(
        &pool,
        MODEL,
        "bedrock",
        "bedrock/amazon.nova-pro-v1:0",
        None,
        "chat",
        (1.0, 1.0),
    )
    .await;
    seed_row(
        &pool,
        "m-f600-titan",
        "bedrock",
        "bedrock/amazon.titan-embed-text-v2:0",
        None,
        "embedding",
        (1.0, 1.0),
    )
    .await;
    let config = encrypted(
        &json!({"aws_access_key_id": KEY_ID, "aws_secret_access_key": SECRET,
                "aws_region": "x.attacker.example/"})
        .to_string(),
    );
    sqlx::query("UPDATE llm_model SET provider_config = $1")
        .bind(config)
        .execute(&pool)
        .await
        .unwrap();
    let state = egress.state(pool.clone());
    for (route, body) in [
        ("/chat/completions", chat_body(json!({}))),
        ("/chat/completions", chat_body(json!({"stream": true}))),
        (
            "/embeddings",
            json!({"model_id": "m-f600-titan", "input": "x"}),
        ),
    ] {
        let (status, bytes) = post(&state, route, &key, body).await;
        assert!(
            !status.is_success(),
            "{route}: {status} {}",
            String::from_utf8_lossy(&bytes)
        );
    }
    assert!(
        egress.seen().is_empty(),
        "nothing may be connected to: {:?}",
        egress.seen()
    );
}

/// The outbound policy applies: a row whose `api_url` is the cloud
/// metadata address is refused before anything is signed or sent.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_outbound_policy_applies_to_a_bedrock_row(pool: PgPool) {
    let egress = Egress::start().await;
    let key = topology(&pool).await;
    seed_row(
        &pool,
        MODEL,
        "bedrock",
        "bedrock/amazon.nova-pro-v1:0",
        Some("http://169.254.169.254/latest"),
        "chat",
        (1.0, 1.0),
    )
    .await;
    seed_row(
        &pool,
        "m-f600-titan",
        "bedrock",
        "bedrock/amazon.titan-embed-text-v2:0",
        Some("http://169.254.169.254/latest"),
        "embedding",
        (1.0, 1.0),
    )
    .await;
    let state = egress.state(pool.clone());
    for (route, body) in [
        ("/chat/completions", chat_body(json!({}))),
        ("/chat/completions", chat_body(json!({"stream": true}))),
        (
            "/embeddings",
            json!({"model_id": "m-f600-titan", "input": "x"}),
        ),
    ] {
        let (status, bytes) = post(&state, route, &key, body).await;
        assert!(
            !status.is_success(),
            "{route}: {status} {}",
            String::from_utf8_lossy(&bytes)
        );
    }
    assert!(
        egress.seen().is_empty(),
        "the metadata address was contacted: {:?}",
        egress.seen()
    );
}

/// An inference-profile ARN has `:` and `/`: the id is one percent-encoded
/// path segment, and the signature is over the canonical form of the path
/// as it was sent (`assert_signed` computes it here).
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_arn_is_encoded_in_the_path_and_signed_in_its_canonical_form(pool: PgPool) {
    let mock = converse_mock(converse_fixture("pong")).await;
    let key = topology(&pool).await;
    seed_row(
        &pool,
        MODEL,
        "bedrock_converse",
        "arn:aws:bedrock:eu-west-1:123456789012:inference-profile/eu.amazon.nova-pro-v1:0",
        Some(&mock.uri()),
        "chat",
        (0.8, 3.2),
    )
    .await;
    for stream in [false, true] {
        mock.reset().await;
        let reply = if stream {
            ResponseTemplate::new(200).set_body_raw(
                converse_stream_fixture(),
                "application/vnd.amazon.eventstream",
            )
        } else {
            ResponseTemplate::new(200).set_body_json(converse_fixture("pong"))
        };
        Mock::given(method("POST"))
            .respond_with(reply)
            .mount(&mock)
            .await;
        let state = common::build_test_state(pool.clone());
        let (status, bytes) = post(
            &state,
            "/chat/completions",
            &key,
            chat_body(json!({"stream": stream})),
        )
        .await;
        assert_eq!(
            status,
            StatusCode::OK,
            "{}",
            String::from_utf8_lossy(&bytes)
        );
        let sent = received(&mock).await;
        assert_eq!(sent.len(), 1);
        let operation = if stream {
            "converse-stream"
        } else {
            "converse"
        };
        assert_eq!(
            sent[0].url.path(),
            format!(
                "/model/arn%3Aaws%3Abedrock%3Aeu-west-1%3A123456789012%3Ainference-profile%2F\
                 eu.amazon.nova-pro-v1%3A0/{operation}"
            )
        );
        assert_signed(&sent[0]);
    }
}

/// The verifier of this file against a fixed vector: the signature of a
/// known request, computed once by hand from the SigV4 reference (the
/// AWS documentation's example secret key, a fixed date). A verifier that
/// accepted anything would make `assert_signed` worthless.
#[test]
fn the_signature_computed_here_is_sigv4() {
    let secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY";
    let body = br#"{"messages":[]}"#;
    let hash = sha256_hex(body);
    let signed = vec![
        ("content-type".to_string(), "application/json".to_string()),
        (
            "host".to_string(),
            "bedrock-runtime.us-east-1.amazonaws.com".to_string(),
        ),
        ("x-amz-content-sha256".to_string(), hash.clone()),
        ("x-amz-date".to_string(), "20261112T120000Z".to_string()),
    ];
    let path = "/model/us.amazon.nova-pro-v1%3A0/converse";
    let signature = expected_signature(
        path,
        &signed,
        body,
        "20261112T120000Z",
        "20261112/us-east-1/bedrock/aws4_request",
        secret,
    );
    // The canonical request, spelled out: the `%` of the path as sent is
    // itself encoded.
    let canonical = format!(
        "POST\n/model/us.amazon.nova-pro-v1%253A0/converse\n\ncontent-type:application/json\n\
         host:bedrock-runtime.us-east-1.amazonaws.com\nx-amz-content-sha256:{hash}\n\
         x-amz-date:20261112T120000Z\n\ncontent-type;host;x-amz-content-sha256;x-amz-date\n{hash}"
    );
    let to_sign = format!(
        "AWS4-HMAC-SHA256\n20261112T120000Z\n20261112/us-east-1/bedrock/aws4_request\n{}",
        sha256_hex(canonical.as_bytes())
    );
    let mut k = hmac(format!("AWS4{secret}").as_bytes(), b"20261112");
    for part in ["us-east-1", "bedrock", "aws4_request"] {
        k = hmac(&k, part.as_bytes());
    }
    assert_eq!(signature, hex::encode(hmac(&k, to_sign.as_bytes())));
    assert_eq!(signature.len(), 64);
    // And a different secret, path or body gives a different signature.
    for (p, b, s) in [
        ("/model/us.amazon.nova-pro-v1:0/converse", &body[..], secret),
        (path, &b"{}"[..], secret),
        (path, &body[..], "another-secret"),
    ] {
        assert_ne!(
            expected_signature(
                p,
                &signed,
                b,
                "20261112T120000Z",
                "20261112/us-east-1/bedrock/aws4_request",
                s
            ),
            signature
        );
    }
}
