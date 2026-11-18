// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Whose cloud identity a tenant's row is called with, and where a cloud
//! row may go.
//!
//! * A tenant's model row is never served with the gateway
//!   operator's own cloud identity. A Vertex AI row with no credential of
//!   its own used the process's Application Default Credentials; it is
//!   refused 501 `provider_credential_missing` with an audit row and nothing
//!   is connected to. (The Bedrock guardrail's half is proved in
//!   `services::guardrails::orchestrator`'s tests.)
//! * A Vertex AI row with no project is not sent to a default project:
//!   it is refused 501 `provider_endpoint_invalid`.
//! * The Bedrock guardrail does not put its guardrail id in the path as it
//!   is configured; an ARN has `:` and `/`. The id is one
//!   percent-encoded segment and the request is signed by the gateway's one
//!   SigV4 signer; verified by the SigV4 written out in `bedrock_runtime.rs`.
//! * The provider `vertex` is the provider `vertex_ai`.
//! * A row of a cloud provider (Amazon Bedrock, Vertex AI)
//!   is served by that provider's own backend on the route in question or
//!   refused at dispatch (501 `unsupported_provider_api`); it never reaches
//!   a forwarder that authenticates with an API key, with or without an
//!   `api_url`. A credential in `api_key` (`AKIA…:secret`, a base64 service
//!   account) is not a cloud credential: that lives in `provider_config`.
//!
//! No request leaves this machine: every upstream is a mock on loopback or
//! the recorder of `bedrock_runtime.rs`, which answers 502 and forwards
//! nothing. Every credential is made up. The one test that needs an ambient
//! cloud credential runs its body in a child process, the only process that
//! has one, and that one is made up too.

use axum::http::StatusCode;
use base64::Engine;
use serde_json::{Value, json};
use sqlx::PgPool;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::services::guardrails::bedrock::BedrockGuardrailDetector;
use brutor_gateway_core::services::guardrails::{Detection, Detector};

use super::bedrock_claude::{
    chat, google_identity, invoke_mock, messages, post_gemini, post_messages, set_config, text,
};
use super::bedrock_runtime::{
    Egress, KEY_ID, REGION, SECRET, T, assert_signed, log_row, post, received, seed_row, topology,
};
use crate::common;

// ---------------------------------------------------------------------------
// A child process with a made-up ambient credential
// ---------------------------------------------------------------------------

/// Set in the child, and only there.
const CHILD_MARKER: &str = "BRUTOR_TEST_AMBIENT_CHILD";

/// The access token the made-up AMBIENT Google identity is given by its
/// (loopback) token endpoint. No row may ever be called with it.
const AMBIENT_TOKEN: &str = "ya29.f805-the-operators-own-identity-made-up";

/// The name of the test `name` of this module, as the test harness knows it.
fn test_name(name: &str) -> String {
    let module = module_path!();
    // Without the crate: `integration::cloud_identity` is `cloud_identity`.
    let module = module.split_once("::").map_or(module, |(_, rest)| rest);
    format!("{module}::{name}")
}

/// Fails if this process has a cloud credential variable of its own: the
/// test does not run next to a real ambient credential, and that is a
/// failure, not a pass. Only the names are read, never a value.
fn refuse_a_real_ambient_credential() {
    let present: Vec<String> = std::env::vars_os()
        .filter_map(|(name, _)| name.into_string().ok())
        .filter(|name| name.starts_with("AWS_") || name.starts_with("GOOGLE_"))
        .collect();
    assert!(
        present.is_empty(),
        "this process has cloud credential variables of its own ({present:?}). The test does \
         not run next to a real ambient credential: unset them."
    );
}

/// Runs the ignored test `child` of this module in a child process whose
/// home directory is an empty scratch directory (no credential file of the
/// developer's can be found) and whose `GOOGLE_APPLICATION_CREDENTIALS`
/// names a file in it, which the child writes: a made-up service account.
fn run_next_to_an_ambient_google_credential(child: &str) {
    refuse_a_real_ambient_credential();
    let home = std::env::temp_dir().join(format!(
        "brutor-f805-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |d| d.as_nanos())
    ));
    std::fs::create_dir_all(&home).expect("scratch home");
    let output = std::process::Command::new(std::env::current_exe().expect("this test binary"))
        .args([
            test_name(child).as_str(),
            "--exact",
            "--ignored",
            "--nocapture",
        ])
        .env(CHILD_MARKER, "1")
        .env("HOME", &home)
        .env("GOOGLE_APPLICATION_CREDENTIALS", home.join("adc.json"))
        .output()
        .expect("run the child");
    let _removed = std::fs::remove_dir_all(&home);
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        output.status.success() && stdout.contains("1 passed"),
        "the child `{child}` did not pass:\n{stdout}\n{stderr}"
    );
}

// ---------------------------------------------------------------------------
// A Vertex AI row is never called with the gateway's own identity
// ---------------------------------------------------------------------------

/// A Vertex AI row with no credential of its own, in a gateway whose
/// process has an ambient Google credential (the operator's: on GKE the
/// node's service account, elsewhere `GOOGLE_APPLICATION_CREDENTIALS`).
/// The row was called with that identity: any tenant that can create a
/// model row spent the operator's Google project and read whatever the
/// operator's identity may read. Now the row is refused.
#[test]
fn a_vertex_row_without_a_credential_is_not_called_with_the_gateways_identity() {
    run_next_to_an_ambient_google_credential("child_vertex_rows_next_to_an_ambient_credential");
}

#[sqlx::test(migrations = "./tests/migrations")]
#[ignore = "run by a_vertex_row_without_a_credential_is_not_called_with_the_gateways_identity, \
            in a child process whose ambient Google credential is made up"]
async fn child_vertex_rows_next_to_an_ambient_credential(pool: PgPool) {
    assert!(
        std::env::var_os(CHILD_MARKER).is_some(),
        "run by its parent, in a process whose ambient credential is made up"
    );
    // The ambient credential: a made-up service account whose token
    // endpoint is a mock here. The file is in the scratch home.
    let adc = std::path::PathBuf::from(
        std::env::var_os("GOOGLE_APPLICATION_CREDENTIALS").expect("set by the parent"),
    );
    let home = std::path::PathBuf::from(std::env::var_os("HOME").expect("set by the parent"));
    assert!(
        adc.starts_with(&home) && home.starts_with(std::env::temp_dir()),
        "the ambient credential file is not in the scratch home"
    );
    let (ambient_token_endpoint, ambient_account) = google_identity(
        "operator@gateway-operator.iam.gserviceaccount.com",
        AMBIENT_TOKEN,
    )
    .await;
    std::fs::write(&adc, ambient_account.to_string()).expect("write the made-up credential");

    let vertex = invoke_mock().await;
    let endpoint = format!(
        "{}/v1/projects/p/locations/global/publishers/anthropic/models/claude-sonnet-5-5",
        vertex.uri()
    );
    let key = topology(&pool).await;
    // Two Claude rows the mock stands for (one with the service-account
    // auth type and no service account), and a Gemini row, whose URL is
    // always built from its location and project.
    for (id, name, api_url, auth_type) in [
        (
            "m-f805-claude",
            "vertex_ai/claude-sonnet-5-5",
            Some(endpoint.as_str()),
            None,
        ),
        (
            "m-f805-typed",
            "vertex_ai/claude-sonnet-5-5",
            Some(endpoint.as_str()),
            Some("gcp_service_account"),
        ),
        (
            "m-f805-gemini",
            "vertex_ai/gemini-3.5-flash-lite",
            None,
            None,
        ),
    ] {
        seed_row(&pool, id, "vertex_ai", name, api_url, "chat", (1.0, 1.0)).await;
        sqlx::query("UPDATE llm_model SET auth_type = $2, api_key = NULL WHERE id = $1")
            .bind(id)
            .bind(auth_type)
            .execute(&pool)
            .await
            .expect("no credential on the row");
        set_config(
            &pool,
            id,
            Some(json!({"gcp_project_id": "p", "gcp_location": "global"})),
        )
        .await;
    }

    let state = common::build_test_state(pool.clone());
    let egress = Egress::start().await;
    let recorded = egress.state(pool.clone());
    let answers = vec![
        post(
            &state,
            "/chat/completions",
            &key,
            chat("m-f805-claude", false),
        )
        .await,
        post(
            &state,
            "/chat/completions",
            &key,
            chat("m-f805-claude", true),
        )
        .await,
        post(
            &state,
            "/chat/completions",
            &key,
            chat("m-f805-typed", false),
        )
        .await,
        post_messages(&state, &key, messages("m-f805-claude", false)).await,
        post(
            &recorded,
            "/chat/completions",
            &key,
            chat("m-f805-gemini", false),
        )
        .await,
        post_gemini(&recorded, &key, "m-f805-gemini").await,
    ];

    // What reached the row's endpoint, and with whose token.
    let with_ambient: Vec<String> = received(&vertex)
        .await
        .iter()
        .map(|r| {
            let token = r
                .headers
                .get("authorization")
                .and_then(|v| v.to_str().ok())
                .unwrap_or_default();
            format!(
                "{} {} ({})",
                r.method,
                r.url.path(),
                if token.contains(AMBIENT_TOKEN) {
                    "with the gateway's own Google access token"
                } else {
                    "with another token"
                }
            )
        })
        .collect();
    assert!(
        with_ambient.is_empty(),
        "a row with no credential was called with the gateway's own identity: {with_ambient:?}"
    );
    assert!(
        received(&ambient_token_endpoint).await.is_empty(),
        "the gateway asked for a token of its own identity for a tenant's row"
    );
    assert!(
        egress.seen().is_empty(),
        "the gateway connected somewhere for a row with no credential: {:?}",
        egress.seen()
    );
    for (status, bytes) in &answers {
        assert_eq!(*status, StatusCode::NOT_IMPLEMENTED, "{}", text(bytes));
        assert!(
            text(bytes).contains("provider_credential_missing"),
            "{}",
            text(bytes)
        );
    }
    // Each refusal is on the record.
    let row = log_row(&pool, 501).await;
    assert!(
        row.4
            .as_deref()
            .is_some_and(|e| e.contains("provider_credential_missing")),
        "the audit row: {row:?}"
    );
    // The rows are written by the background writer, one batch at a time:
    // `log_row` saw the first; wait for every refusal's before counting.
    let expected = answers.len() as i64;
    let mut refused = 0_i64;
    for _ in 0..200 {
        refused = sqlx::query_scalar(
            "SELECT count(*) FROM proxy_logs WHERE tenant_id = $1 AND http_status_code = 501",
        )
        .bind(T)
        .fetch_one(&pool)
        .await
        .expect("count the refusals");
        if refused >= expected {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    assert_eq!(refused, expected, "one audit row per refusal");
}

// ---------------------------------------------------------------------------
// A Vertex AI row with no project
// ---------------------------------------------------------------------------

async fn vertex_row(
    pool: &PgPool,
    id: &str,
    provider: &str,
    name: &str,
    api_url: Option<&str>,
    config: Value,
) {
    seed_row(pool, id, provider, name, api_url, "chat", (1.0, 1.0)).await;
    sqlx::query(
        "UPDATE llm_model SET auth_type = 'gcp_service_account', api_key = NULL WHERE id = $1",
    )
    .bind(id)
    .execute(pool)
    .await
    .expect("a service-account row");
    set_config(pool, id, Some(config)).await;
}

/// A Vertex AI row that names no project was sent to
/// `…/projects/my-project/…` with the row's own access token: another
/// customer's project, if one has that id. It is refused and nothing is
/// connected to, not even the token endpoint.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_vertex_row_without_a_project_is_not_sent_anywhere(pool: PgPool) {
    let egress = Egress::start().await;
    let (token_endpoint, account) =
        google_identity("row@p.iam.gserviceaccount.com", "ya29.f804-row").await;
    let key = topology(&pool).await;
    for (id, name) in [
        ("m-f804-claude", "vertex_ai/claude-sonnet-5-5"),
        ("m-f804-gemini", "vertex_ai/gemini-3.5-flash-lite"),
    ] {
        vertex_row(
            &pool,
            id,
            "vertex_ai",
            name,
            None,
            json!({"gcp_location": "us-central1", "gcp_credentials_json": account.to_string()}),
        )
        .await;
    }
    let state = egress.state(pool.clone());
    let answers = vec![
        post(
            &state,
            "/chat/completions",
            &key,
            chat("m-f804-claude", false),
        )
        .await,
        post_messages(&state, &key, messages("m-f804-claude", false)).await,
        post(
            &state,
            "/chat/completions",
            &key,
            chat("m-f804-gemini", false),
        )
        .await,
        post_gemini(&state, &key, "m-f804-gemini").await,
    ];
    assert!(
        egress.seen().is_empty(),
        "a row with no project was sent to a project that is not its own: {:?}",
        egress.seen()
    );
    assert!(
        received(&token_endpoint).await.is_empty(),
        "a token was fetched for a row that cannot be sent"
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
// A cloud row that names no region or location
// ---------------------------------------------------------------------------

/// A Bedrock row that named no region was signed for and sent to
/// `us-east-1`, and a Vertex AI row that named no location to
/// `us-central1`: a default in the gateway decided where a tenant's prompts
/// were processed (the family of the default project). The row is
/// refused 501 `provider_endpoint_invalid` and nothing is connected to. A
/// Bedrock row with an `api_url` is refused too: the region is part of the
/// credential scope it is signed with.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_cloud_row_without_a_region_or_a_location_is_not_sent_anywhere(pool: PgPool) {
    let egress = Egress::start().await;
    let bedrock_mock = MockServer::start().await;
    let (token_endpoint, account) =
        google_identity("row@p.iam.gserviceaccount.com", "ya29.f965-row").await;
    let key = topology(&pool).await;
    let no_region = json!({"aws_access_key_id": KEY_ID, "aws_secret_access_key": SECRET});
    for (id, name, api_url, mode) in [
        (
            "m-f965-claude",
            "bedrock/global.anthropic.claude-sonnet-5-5",
            None,
            "chat",
        ),
        ("m-f965-nova", "amazon.nova-lite-v1:0", None, "chat"),
        (
            "m-f965-embed",
            "amazon.titan-embed-text-v2:0",
            None,
            "embedding",
        ),
        (
            "m-f965-claude-url",
            "bedrock/global.anthropic.claude-sonnet-5-5",
            Some(bedrock_mock.uri()),
            "chat",
        ),
    ] {
        seed_row(
            &pool,
            id,
            "bedrock",
            name,
            api_url.as_deref(),
            mode,
            (1.0, 1.0),
        )
        .await;
        set_config(&pool, id, Some(no_region.clone())).await;
    }
    for (id, name) in [
        ("m-f965-vclaude", "vertex_ai/claude-sonnet-5-5"),
        ("m-f965-gemini", "vertex_ai/gemini-3.5-flash-lite"),
    ] {
        vertex_row(
            &pool,
            id,
            "vertex_ai",
            name,
            None,
            json!({"gcp_project_id": "p", "gcp_credentials_json": account.to_string()}),
        )
        .await;
    }
    let state = egress.state(pool.clone());
    let answers = vec![
        post(
            &state,
            "/chat/completions",
            &key,
            chat("m-f965-claude", false),
        )
        .await,
        post_messages(&state, &key, messages("m-f965-claude", false)).await,
        post(
            &state,
            "/chat/completions",
            &key,
            chat("m-f965-nova", false),
        )
        .await,
        post(
            &state,
            "/embeddings",
            &key,
            json!({"model_id": "m-f965-embed", "input": "ping"}),
        )
        .await,
        post(
            &state,
            "/chat/completions",
            &key,
            chat("m-f965-claude-url", false),
        )
        .await,
        post(
            &state,
            "/chat/completions",
            &key,
            chat("m-f965-vclaude", false),
        )
        .await,
        post_messages(&state, &key, messages("m-f965-vclaude", false)).await,
        post(
            &state,
            "/chat/completions",
            &key,
            chat("m-f965-gemini", false),
        )
        .await,
        post_gemini(&state, &key, "m-f965-gemini").await,
    ];
    assert!(
        egress.seen().is_empty(),
        "a row with no region or location was sent to one the gateway chose: {:?}",
        egress.seen()
    );
    assert!(
        received(&bedrock_mock).await.is_empty(),
        "a row with no region was signed for a region the gateway chose"
    );
    assert!(
        received(&token_endpoint).await.is_empty(),
        "a token was fetched for a row that cannot be sent"
    );
    for (i, (status, bytes)) in answers.into_iter().enumerate() {
        assert_eq!(status, StatusCode::NOT_IMPLEMENTED, "{i}: {}", text(&bytes));
        assert!(
            text(&bytes).contains("provider_endpoint_invalid"),
            "{i}: {}",
            text(&bytes)
        );
        assert!(
            !text(&bytes).contains("us-east-1") && !text(&bytes).contains("us-central1"),
            "{i}: {}",
            text(&bytes)
        );
    }
}

// ---------------------------------------------------------------------------
// A service account is parsed once per credential
// ---------------------------------------------------------------------------

/// `gcp_auth` parses a service account into a token provider with an HTTPS
/// client of its own (measured: 73 ms each on the development machine, in a
/// release build). A Vertex AI row's account was parsed at dispatch, to see
/// that it is one, and again when its token was asked for: twice on every
/// request, and the token provider that was already kept for the credential
/// was not asked. The dispatch check of a credential that has been parsed
/// is a lookup.
#[tokio::test]
async fn a_service_account_is_parsed_once_per_credential_not_per_request() {
    use brutor_gateway_core::models::db::LlmModel;
    use brutor_gateway_core::services::providers::{ProviderApi, dispatch_refusal};
    use std::time::Instant;

    // A credential no other test of this process has parsed.
    let (_token_endpoint, account) = google_identity(
        "f966-parsed-once@p.iam.gserviceaccount.com",
        "ya29.f966-row",
    )
    .await;
    let model = LlmModel {
        provider: "vertex_ai".into(),
        model_name: "gemini-3.5-flash-lite".into(),
        auth_type: Some("gcp_service_account".into()),
        provider_config: Some(
            json!({"gcp_project_id": "p", "gcp_location": "us-central1",
                   "gcp_credentials_json": account.to_string()})
            .to_string(),
        ),
        ..Default::default()
    };
    let started = Instant::now();
    assert!(dispatch_refusal(&model, ProviderApi::GeminiNative).is_none());
    let first = started.elapsed();
    let started = Instant::now();
    for _ in 0..20 {
        assert!(dispatch_refusal(&model, ProviderApi::GeminiNative).is_none());
    }
    let twenty_more = started.elapsed();
    assert!(
        twenty_more < first,
        "the first check of the credential took {first:?} and the next twenty {twenty_more:?}: \
         the account is parsed again on every check"
    );

    // An account that is not one is still none, each time it is asked.
    let mut broken = model.clone();
    broken.provider_config = Some(
        json!({"gcp_project_id": "p", "gcp_credentials_json": "{\"type\": \"nothing\"}"})
            .to_string(),
    );
    for _ in 0..2 {
        assert_eq!(
            dispatch_refusal(&broken, ProviderApi::GeminiNative).map(|r| r.error_type),
            Some("provider_credential_missing")
        );
    }
}

// ---------------------------------------------------------------------------
// `vertex` is `vertex_ai`
// ---------------------------------------------------------------------------

/// A Claude row of the provider `vertex` was sent to the native bridge by
/// the chat route, which found no backend for that name (500), and to the
/// OpenAI translation by the Messages route. The two names are one
/// provider: the row is served as a `vertex_ai` row is, with its own token.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_provider_vertex_is_served_as_vertex_ai(pool: PgPool) {
    let vertex = invoke_mock().await;
    let endpoint = format!(
        "{}/v1/projects/p/locations/global/publishers/anthropic/models/claude-sonnet-5-5",
        vertex.uri()
    );
    let (_token_endpoint, account) =
        google_identity("row@p.iam.gserviceaccount.com", "ya29.f815-row").await;
    let key = topology(&pool).await;
    vertex_row(
        &pool,
        "m-f815",
        "vertex",
        "vertex/claude-sonnet-5-5",
        Some(&endpoint),
        json!({"gcp_project_id": "p", "gcp_location": "global",
               "gcp_credentials_json": account.to_string()}),
    )
    .await;
    let state = common::build_test_state(pool.clone());
    let (chat_status, chat_bytes) =
        post(&state, "/chat/completions", &key, chat("m-f815", false)).await;
    let (messages_status, messages_bytes) =
        post_messages(&state, &key, messages("m-f815", false)).await;
    let sent: Vec<(String, String)> = received(&vertex)
        .await
        .iter()
        .map(|r| {
            (
                r.url.path().to_string(),
                r.headers
                    .get("authorization")
                    .and_then(|v| v.to_str().ok())
                    .unwrap_or_default()
                    .to_string(),
            )
        })
        .collect();
    assert_eq!(
        (chat_status, messages_status),
        (StatusCode::OK, StatusCode::OK),
        "chat: {} / messages: {} / sent: {sent:?}",
        text(&chat_bytes),
        text(&messages_bytes)
    );
    let raw_predict = "/v1/projects/p/locations/global/publishers/anthropic/models/\
                       claude-sonnet-5-5:rawPredict"
        .to_string();
    assert_eq!(
        sent,
        vec![
            (raw_predict.clone(), "Bearer ya29.f815-row".to_string()),
            (raw_predict, "Bearer ya29.f815-row".to_string()),
        ],
        "both routes call Vertex AI's rawPredict with the row's own token"
    );
}

// ---------------------------------------------------------------------------
// The Bedrock guardrail's id is one encoded path segment, signed
// ---------------------------------------------------------------------------

/// A guardrail is named by its id or by its ARN
/// (`arn:aws:bedrock:<region>:<account>:guardrail/<id>`). The ARN went into
/// the path as it is: its `/` made two segments of one, and the signature
/// was computed over the unencoded path, which is not what AWS computes
/// from the request it receives.
#[tokio::test]
async fn the_bedrock_guardrail_id_is_one_encoded_segment_and_the_signature_verifies() {
    let bedrock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"action": "NONE"})))
        .mount(&bedrock)
        .await;
    for (guardrail_id, path) in [
        (
            "arn:aws:bedrock:eu-west-1:123456789012:guardrail/f808abc123",
            "/guardrail/arn%3Aaws%3Abedrock%3Aeu-west-1%3A123456789012%3Aguardrail%2Ff808abc123\
             /version/1/apply",
        ),
        ("f808abc123", "/guardrail/f808abc123/version/1/apply"),
    ] {
        let detector = BedrockGuardrailDetector::build(
            &json!({
                "provider": "bedrock",
                "endpoint": bedrock.uri(),
                "config": {
                    "guardrail_id": guardrail_id,
                    "guardrail_version": "1",
                    "region": REGION,
                    "aws_access_key_id": KEY_ID,
                    "aws_secret_access_key": SECRET,
                }
            }),
            reqwest::Client::new(),
        )
        .expect("a configured guardrail");
        assert_eq!(
            detector
                .detect("ping")
                .await
                .expect("the guardrail answers"),
            Detection::Pass
        );
        let sent = received(&bedrock).await;
        let request = sent.last().expect("one ApplyGuardrail call");
        assert_eq!(request.url.path(), path, "guardrail id {guardrail_id}");
        assert_signed(request);
    }
}

// ---------------------------------------------------------------------------
// A cloud row never reaches a forwarder that authenticates
// with an API key
// ---------------------------------------------------------------------------

/// An AWS credential in the `access key id:secret access key` form a row's
/// `api_key` used to be read as. Made up.
const LEGACY_AWS: &str = "AKIAF811LEGACYMADEUP:f811LegacyMadeUpSecretNotARealKey0001";
const LEGACY_AWS_SECRET: &str = "f811LegacyMadeUpSecretNotARealKey0001";

/// The cloud rows of the tests below: `(id suffix, provider, model name)`.
const CLOUD_ROWS: &[(&str, &str, &str)] = &[
    (
        "bedrock-claude",
        "bedrock",
        "bedrock/global.anthropic.claude-sonnet-5-5",
    ),
    (
        "converse-claude",
        "bedrock_converse",
        "bedrock/converse/us.anthropic.claude-haiku-5-5",
    ),
    ("vertex-claude", "vertex_ai", "vertex_ai/claude-sonnet-5-5"),
    (
        "vertex-gemini",
        "vertex_ai",
        "vertex_ai/gemini-3.5-flash-lite",
    ),
];

/// An upstream that answers anything 200, and records it.
async fn any_upstream() -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(wiremock::matchers::any())
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "f811", "object": "chat.completion", "model": "f811",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "pong"}}],
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        })))
        .mount(&mock)
        .await;
    mock
}

/// What an upstream received, without any credential: the request line and
/// how it was authenticated.
async fn arrivals(mock: &MockServer, credentials: &[&str]) -> Vec<String> {
    received(mock)
        .await
        .iter()
        .map(|r| {
            let credential_headers: Vec<&str> = r
                .headers
                .iter()
                .filter(|(_, value)| {
                    let value = value.to_str().unwrap_or_default();
                    credentials.iter().any(|c| value.contains(c))
                })
                .map(|(name, _)| name.as_str())
                .collect();
            let signed = r
                .headers
                .get("authorization")
                .and_then(|v| v.to_str().ok())
                .is_some_and(|v| v.starts_with("AWS4-HMAC-SHA256"));
            format!(
                "{} {} (signed: {signed}; the row's credential in: {credential_headers:?})",
                r.method,
                r.url.path()
            )
        })
        .collect()
}

/// A row whose only credential is in `api_key` (the legacy shape), with no
/// `auth_type`, at `api_url`.
async fn legacy_row(
    pool: &PgPool,
    id: &str,
    provider: &str,
    name: &str,
    api_url: &str,
    mode: &str,
    api_key: &str,
) {
    seed_row(pool, id, provider, name, Some(api_url), mode, (1.0, 1.0)).await;
    sqlx::query("UPDATE llm_model SET auth_type = NULL, provider_config = NULL WHERE id = $1")
        .bind(id)
        .execute(pool)
        .await
        .expect("a legacy row");
    common::set_model_api_key(pool, id, api_key).await;
}

/// The legacy credential of a provider's rows: `AKIA…:secret`, or a
/// base64-encoded service account.
fn legacy_credential(provider: &str, account: &Value) -> String {
    if provider.starts_with("bedrock") {
        LEGACY_AWS.to_string()
    } else {
        base64::engine::general_purpose::STANDARD.encode(account.to_string())
    }
}

/// Claude on Bedrock and on Vertex AI, and Gemini on Vertex AI, on the
/// routes outside chat and Messages. A row with a legacy credential in
/// `api_key`, an `api_url` and no `auth_type` went to the generic
/// forwarder: an unsigned OpenAI-shaped request to the row's `api_url`, the
/// credential (the AWS secret access key, the service-account key) in an
/// API-key header. A row with a cloud `auth_type` was refused there
/// (`provider_auth_unsupported`). Both are refused at dispatch as what they
/// are: the route is not implemented for the provider.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_cloud_row_is_refused_on_the_routes_its_backend_does_not_serve(pool: PgPool) {
    let upstream = any_upstream().await;
    let (token_endpoint, account) =
        google_identity("row@p.iam.gserviceaccount.com", "ya29.f811-row").await;
    let account_b64 = legacy_credential("vertex_ai", &account);
    let key = topology(&pool).await;
    let state = common::build_test_state(pool.clone());
    let mut answers = Vec::new();
    for (suffix, provider, name) in CLOUD_ROWS {
        for (route, mode, body) in [
            ("/responses", "chat", json!({"input": "ping"})),
            ("/completions", "chat", json!({"prompt": "ping"})),
            ("/embeddings", "embedding", json!({"input": "ping"})),
        ] {
            for legacy in [true, false] {
                let id = format!(
                    "m-f811-{suffix}{}-{}",
                    route.replace('/', "-"),
                    if legacy { "legacy" } else { "typed" }
                );
                if legacy {
                    legacy_row(
                        &pool,
                        &id,
                        provider,
                        name,
                        &upstream.uri(),
                        mode,
                        &legacy_credential(provider, &account),
                    )
                    .await;
                } else if provider.starts_with("bedrock") {
                    // As `seed_row` leaves it: `aws_iam`, the credential in
                    // `provider_config`.
                    seed_row(
                        &pool,
                        &id,
                        provider,
                        name,
                        Some(&upstream.uri()),
                        mode,
                        (1.0, 1.0),
                    )
                    .await;
                } else {
                    vertex_row(
                        &pool,
                        &id,
                        provider,
                        name,
                        Some(&upstream.uri()),
                        json!({"gcp_project_id": "p", "gcp_location": "global",
                               "gcp_credentials_json": account.to_string()}),
                    )
                    .await;
                    common::set_model_mode(&pool, &id, mode).await;
                }
                let mut request = body.clone();
                request["model_id"] = json!(id);
                let (status, bytes) = post(&state, route, &key, request).await;
                answers.push((id, status, text(&bytes)));
            }
        }
    }
    let sent = arrivals(
        &upstream,
        &[LEGACY_AWS_SECRET, &account_b64, SECRET, "ya29.f811-row"],
    )
    .await;
    assert!(
        sent.is_empty(),
        "a cloud row reached a forwarder that authenticates with an API key:\n{}",
        sent.join("\n")
    );
    assert!(
        received(&token_endpoint).await.is_empty(),
        "a Google token was fetched for a route Vertex AI is not served on"
    );
    for (id, status, answer) in answers {
        assert_eq!(status, StatusCode::NOT_IMPLEMENTED, "{id}: {answer}");
        assert!(
            answer.contains("unsupported_provider_api"),
            "{id}: {answer}"
        );
    }
}

/// The same rows as fallback candidates of a chat model that fails: a
/// candidate is sent by the chat forwarder, which serves Bedrock's Converse
/// rows and no other cloud row. A cloud row with a legacy credential went
/// out there as an unsigned request with the credential in an API-key
/// header. It is not a candidate: the caller gets the failure of the model
/// it asked for.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_cloud_row_is_not_a_chat_fallback_candidate_of_the_api_key_forwarder(pool: PgPool) {
    let failing = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(500).set_body_json(json!({"error": "boom"})))
        .mount(&failing)
        .await;
    let upstream = any_upstream().await;
    let (_token_endpoint, account) =
        google_identity("row@p.iam.gserviceaccount.com", "ya29.f811-row").await;
    let account_b64 = legacy_credential("vertex_ai", &account);
    let key = topology(&pool).await;
    common::seed_llm_provider(&pool, "prov-f811-openai", T, "openai").await;
    let mut statuses = Vec::new();
    for (suffix, provider, name) in CLOUD_ROWS {
        let primary = format!("m-f811-primary-{suffix}");
        let fallback = format!("m-f811-fallback-{suffix}");
        common::seed_llm_model_with_api_url(
            &pool,
            &primary,
            T,
            "prov-f811-openai",
            &format!("gpt-primary-{suffix}"),
            &failing.uri(),
        )
        .await;
        common::bind_llm_model_to_rg(&pool, &primary, super::bedrock_runtime::RG).await;
        legacy_row(
            &pool,
            &fallback,
            provider,
            name,
            &upstream.uri(),
            "chat",
            &legacy_credential(provider, &account),
        )
        .await;
        common::set_model_fallback_models(&pool, &primary, &[fallback.as_str()]).await;
        let state = common::build_test_state(pool.clone());
        let (status, bytes) = post(&state, "/chat/completions", &key, chat(&primary, false)).await;
        statuses.push((primary, status, text(&bytes)));
    }
    let sent = arrivals(&upstream, &[LEGACY_AWS_SECRET, &account_b64]).await;
    assert!(
        sent.is_empty(),
        "a cloud row was sent as a chat fallback by the API-key forwarder:\n{}",
        sent.join("\n")
    );
    for (primary, status, answer) in statuses {
        assert!(
            status.is_server_error(),
            "{primary}: the failure of the requested model is the answer: {status} {answer}"
        );
    }
}

/// A credential in `api_key` is not a cloud credential. A Bedrock row with
/// `AKIA…:secret` there was signed with it, and a Vertex AI row with a
/// base64 service account there fetched a token with it. The credential of
/// a cloud row is in its `provider_config`; a row without one there has
/// none (501 `provider_credential_missing`), on the routes that serve it.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_credential_in_api_key_is_not_a_cloud_credential(pool: PgPool) {
    let upstream = any_upstream().await;
    let (token_endpoint, account) =
        google_identity("row@p.iam.gserviceaccount.com", "ya29.f811-row").await;
    let account_b64 = legacy_credential("vertex_ai", &account);
    let key = topology(&pool).await;
    let state = common::build_test_state(pool.clone());
    let mut answers = Vec::new();
    for (suffix, provider, name) in CLOUD_ROWS
        .iter()
        .copied()
        .filter(|(suffix, _, _)| *suffix != "vertex-gemini")
        .chain([("nova", "bedrock", "bedrock/amazon.nova-pro-v1:0")])
    {
        let id = format!("m-f811-served-{suffix}");
        legacy_row(
            &pool,
            &id,
            provider,
            name,
            &upstream.uri(),
            "chat",
            &legacy_credential(provider, &account),
        )
        .await;
        // The row's location and project are stated: the credential is the
        // only thing missing.
        if provider.starts_with("vertex") {
            set_config(
                &pool,
                &id,
                Some(json!({"gcp_project_id": "p", "gcp_location": "global"})),
            )
            .await;
        } else {
            set_config(&pool, &id, Some(json!({"aws_region": REGION}))).await;
        }
        let (status, bytes) = post(&state, "/chat/completions", &key, chat(&id, false)).await;
        answers.push((format!("{id} chat"), status, text(&bytes)));
        if suffix != "nova" {
            let (status, bytes) = post_messages(&state, &key, messages(&id, false)).await;
            answers.push((format!("{id} messages"), status, text(&bytes)));
        }
    }
    let sent = arrivals(&upstream, &[LEGACY_AWS_SECRET, &account_b64]).await;
    assert!(
        sent.is_empty(),
        "a cloud row was authenticated with what is in its api_key:\n{}",
        sent.join("\n")
    );
    assert!(
        received(&token_endpoint).await.is_empty(),
        "a Google token was fetched with a service account read from api_key"
    );
    for (what, status, answer) in answers {
        assert_eq!(status, StatusCode::NOT_IMPLEMENTED, "{what}: {answer}");
        assert!(
            answer.contains("provider_credential_missing"),
            "{what}: {answer}"
        );
    }
}

/// A Claude row of the catalog's `bedrock_converse` provider is served by
/// the Claude backend on the Messages route, as on the chat route: signed
/// InvokeModel, the Anthropic body. The Messages route sent it to the
/// OpenAI translation and the generic forwarder.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_converse_claude_row_is_served_by_the_claude_backend_on_the_messages_route(pool: PgPool) {
    let bedrock = invoke_mock().await;
    let key = topology(&pool).await;
    seed_row(
        &pool,
        "m-f811-converse-native",
        "bedrock_converse",
        "bedrock/converse/us.anthropic.claude-haiku-5-5",
        Some(&bedrock.uri()),
        "chat",
        (1.0, 1.0),
    )
    .await;
    let state = common::build_test_state(pool.clone());
    let (status, bytes) =
        post_messages(&state, &key, messages("m-f811-converse-native", false)).await;
    assert_eq!(status, StatusCode::OK, "{}", text(&bytes));
    let sent = received(&bedrock).await;
    assert_eq!(sent.len(), 1, "one InvokeModel call");
    assert_eq!(
        sent[0].url.path(),
        "/model/us.anthropic.claude-haiku-5-5/invoke"
    );
    assert_signed(&sent[0]);
    let body: Value = serde_json::from_slice(&sent[0].body).expect("a JSON body");
    assert!(body["messages"].is_array(), "the Anthropic body: {body}");
}
