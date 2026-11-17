// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! What the gateway sends a provider for the model families of the
//! current catalog:
//! the request that reaches the mock upstream is the
//! evidence, not the adapter's unit tests.
//!
//! - GPT-6 on Chat Completions takes `max_completion_tokens` only and
//!   refuses `temperature` / `top_p`; the rule comes from the catalog row
//!   (`capabilities.supports_reasoning`) and falls back to the name.
//! - `gpt-6-sol` / `gpt-6-luna` (and `gpt-5.6-*` on Azure) take function
//!   tools on Chat Completions only with `reasoning_effort: "none"`: the
//!   gateway sets it when the caller chose no effort, and refuses a caller's
//!   own other choice instead of overriding it or sending a request the
//!   provider refuses.
//! - Azure ids are seeded as `azure/<model>`: the family rules read the name
//!   after the prefix.
//! - Claude models that retired sampling parameters get none.

use axum::body::Body;
use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

async fn body_to_json(body: Body) -> Value {
    let bytes = body.collect().await.expect("collect body").to_bytes();
    serde_json::from_slice(&bytes).unwrap_or(Value::Null)
}

fn chat_fixture() -> Value {
    json!({
        "id": "chatcmpl-family",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "m",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop"
        }],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}
    })
}

fn anthropic_fixture() -> Value {
    json!({
        "id": "msg_family",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 3, "output_tokens": 1}
    })
}

fn tools() -> Value {
    json!([{
        "type": "function",
        "function": {"name": "lookup", "parameters": {"type": "object", "properties": {}}}
    }])
}

/// One tenant, one group, one key, one model of `provider` named
/// `model_name` whose endpoint is the mock, with the seed's default
/// temperature and the given `capabilities`. Returns the key.
async fn seed(
    pool: &PgPool,
    provider: &str,
    model_name: &str,
    mock_uri: &str,
    capabilities: Option<Value>,
) -> String {
    common::seed_tenant(pool, "tenant-1").await;
    common::seed_resource_group(pool, "rg-llm", "tenant-1").await;
    common::seed_end_user(pool, 7001, "tenant-1").await;
    common::add_direct_membership(pool, 7001, "rg-llm", "tenant-1").await;
    common::seed_llm_provider(pool, "prov", "tenant-1", provider).await;
    common::seed_llm_model_with_api_url(pool, "m-fam", "tenant-1", "prov", model_name, mock_uri)
        .await;
    // The seed's default: every catalog row gets 0.7 unless it says otherwise.
    sqlx::query(
        "UPDATE llm_model SET default_temperature = 0.7, capabilities = $1::text::json, api_key = NULL \
         WHERE id = 'm-fam'",
    )
    .bind(capabilities.map(|v| v.to_string()))
    .execute(pool)
    .await
    .expect("set model row");
    common::bind_llm_model_to_rg(pool, "m-fam", "rg-llm").await;
    common::seed_api_key(pool, "tenant-1", "rg-llm", Some(7001), "family-key").await
}

/// Send `request` (merged over a minimal chat body) and return the status,
/// the gateway's answer and every body the upstream received.
async fn call(
    pool: PgPool,
    mock: &MockServer,
    api_key: &str,
    request: Value,
) -> (StatusCode, Value, Vec<Value>) {
    let state = common::build_test_state(pool);
    let app = common::build_llm_app(state);
    let mut body = json!({
        "model_id": "m-fam",
        "messages": [{"role": "user", "content": "hi"}]
    });
    for (k, v) in request.as_object().expect("request object") {
        body[k] = v.clone();
    }
    let req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        body,
        Some(api_key),
        Some("tenant-1"),
    );
    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let answer = body_to_json(response.into_body()).await;
    let sent = mock
        .received_requests()
        .await
        .unwrap_or_default()
        .iter()
        .map(|r| serde_json::from_slice::<Value>(&r.body).unwrap_or(Value::Null))
        .collect();
    (status, answer, sent)
}

async fn openai_mock() -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(chat_fixture()))
        .mount(&mock)
        .await;
    mock
}

fn only(sent: &[Value]) -> &Value {
    assert_eq!(sent.len(), 1, "one upstream request expected: {sent:?}");
    &sent[0]
}

// ---------------------------------------------------------------------------
// C1 / C2: GPT-6 on Chat Completions
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn gpt6_gets_max_completion_tokens_and_no_sampling_by_name(pool: PgPool) {
    // No capabilities on the row: the name is the fallback.
    let mock = openai_mock().await;
    let key = seed(&pool, "openai", "gpt-6-luna", &mock.uri(), None).await;
    let (status, answer, sent) =
        call(pool, &mock, &key, json!({"max_tokens": 64, "top_p": 0.9})).await;
    assert_eq!(status, StatusCode::OK, "{answer}");
    let body = only(&sent);
    assert_eq!(body["max_completion_tokens"], json!(64), "{body}");
    assert!(body.get("max_tokens").is_none(), "{body}");
    assert!(
        body.get("temperature").is_none(),
        "the seeded default temperature must not reach a GPT-6 model: {body}"
    );
    assert!(body.get("top_p").is_none(), "{body}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_reasoning_row_needs_no_name_the_gateway_knows(pool: PgPool) {
    // The catalog row says it: an id no prefix list names gets the same
    // treatment from `capabilities.supports_reasoning`.
    let mock = openai_mock().await;
    let key = seed(
        &pool,
        "openai",
        "nova-9-reasoner",
        &mock.uri(),
        Some(json!({"supports_reasoning": true})),
    )
    .await;
    let (status, answer, sent) = call(
        pool,
        &mock,
        &key,
        json!({"max_tokens": 64, "temperature": 0.2}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{answer}");
    let body = only(&sent);
    assert_eq!(body["max_completion_tokens"], json!(64), "{body}");
    assert!(body.get("max_tokens").is_none(), "{body}");
    assert!(body.get("temperature").is_none(), "{body}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_plain_openai_compatible_model_keeps_its_parameters(pool: PgPool) {
    // The negative: neither the row nor the name says reasoning.
    let mock = openai_mock().await;
    let key = seed(&pool, "openai", "plain-chat-1", &mock.uri(), None).await;
    let (status, answer, sent) = call(
        pool,
        &mock,
        &key,
        json!({"max_tokens": 64, "tools": tools()}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{answer}");
    let body = only(&sent);
    assert_eq!(body["max_tokens"], json!(64), "{body}");
    assert_eq!(body["temperature"], json!(0.7), "{body}");
    assert!(body.get("reasoning_effort").is_none(), "{body}");
}

// ---------------------------------------------------------------------------
// C2: function tools need `reasoning_effort: "none"`
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn gpt6_tools_without_an_effort_are_sent_with_none(pool: PgPool) {
    for_each_tool_model(pool, |sent| {
        let body = only(sent);
        assert_eq!(body["reasoning_effort"], json!("none"), "{body}");
        assert!(body["tools"].is_array(), "{body}");
    })
    .await;
}

/// `gpt-6-sol` with tools and no effort; the closure checks what was sent.
async fn for_each_tool_model(pool: PgPool, check: impl Fn(&[Value])) {
    let mock = openai_mock().await;
    let key = seed(
        &pool,
        "openai",
        "gpt-6-sol",
        &mock.uri(),
        Some(json!({"supports_reasoning": true, "supports_function_calling": true})),
    )
    .await;
    let (status, answer, sent) = call(pool, &mock, &key, json!({"tools": tools()})).await;
    assert_eq!(status, StatusCode::OK, "{answer}");
    check(&sent);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn gpt6_without_tools_keeps_the_providers_default_effort(pool: PgPool) {
    let mock = openai_mock().await;
    let key = seed(&pool, "openai", "gpt-6-sol", &mock.uri(), None).await;
    let (status, answer, sent) = call(pool, &mock, &key, json!({})).await;
    assert_eq!(status, StatusCode::OK, "{answer}");
    assert!(only(&sent).get("reasoning_effort").is_none(), "{sent:?}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn gpt6_tools_with_the_callers_none_are_sent_as_asked(pool: PgPool) {
    let mock = openai_mock().await;
    let key = seed(&pool, "openai", "gpt-6-luna", &mock.uri(), None).await;
    let (status, answer, sent) = call(
        pool,
        &mock,
        &key,
        json!({"tools": tools(), "reasoning_effort": "none"}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{answer}");
    assert_eq!(only(&sent)["reasoning_effort"], json!("none"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn gpt6_tools_with_the_callers_other_effort_are_refused_not_overridden(pool: PgPool) {
    let mock = openai_mock().await;
    let key = seed(&pool, "openai", "gpt-6-luna", &mock.uri(), None).await;
    let (status, answer, sent) = call(
        pool,
        &mock,
        &key,
        json!({"tools": tools(), "reasoning_effort": "high"}),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{answer}");
    let text = answer.to_string();
    assert!(
        text.contains("reasoning_effort") && text.contains("none"),
        "the refusal says what to change: {text}"
    );
    assert!(
        sent.is_empty(),
        "a request the provider refuses is not sent: {sent:?}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_tool_rule_follows_the_row_when_the_row_says_it(pool: PgPool) {
    // A row can state the restriction for an id the gateway does not name.
    let mock = openai_mock().await;
    let key = seed(
        &pool,
        "openai",
        "nova-9-reasoner",
        &mock.uri(),
        Some(json!({"supports_reasoning": true, "tools_require_reasoning_effort_none": true})),
    )
    .await;
    let (status, answer, sent) = call(pool, &mock, &key, json!({"tools": tools()})).await;
    assert_eq!(status, StatusCode::OK, "{answer}");
    assert_eq!(only(&sent)["reasoning_effort"], json!("none"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_tool_rule_is_off_when_the_row_says_so(pool: PgPool) {
    // ...and can lift it for an id the gateway does name (the provider
    // removed the restriction; the catalog row is corrected, not the code).
    let mock = openai_mock().await;
    let key = seed(
        &pool,
        "openai",
        "gpt-6-sol",
        &mock.uri(),
        Some(json!({"supports_reasoning": true, "tools_require_reasoning_effort_none": false})),
    )
    .await;
    let (status, answer, sent) = call(
        pool,
        &mock,
        &key,
        json!({"tools": tools(), "reasoning_effort": "high"}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{answer}");
    assert_eq!(only(&sent)["reasoning_effort"], json!("high"));
}

// ---------------------------------------------------------------------------
// C3: Azure ids carry the `azure/` prefix
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_seeded_azure_id_meets_the_family_rules(pool: PgPool) {
    let mock = openai_mock().await;
    let key = seed(
        &pool,
        "azure",
        "azure/gpt-5.2",
        &mock.uri(),
        Some(json!({"supports_reasoning": true})),
    )
    .await;
    let (status, answer, sent) =
        call(pool, &mock, &key, json!({"max_tokens": 64, "top_p": 0.5})).await;
    assert_eq!(status, StatusCode::OK, "{answer}");
    let body = only(&sent);
    assert_eq!(body["max_completion_tokens"], json!(64), "{body}");
    assert!(body.get("max_tokens").is_none(), "{body}");
    assert!(
        body.get("temperature").is_none(),
        "Azure reasoning models refuse temperature: {body}"
    );
    assert!(body.get("top_p").is_none(), "{body}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_seeded_azure_id_meets_the_family_rules_by_name_alone(pool: PgPool) {
    // No capabilities: `azure/gpt-5.2` must match the `gpt-5` fallback.
    let mock = openai_mock().await;
    let key = seed(&pool, "azure", "azure/gpt-5.2", &mock.uri(), None).await;
    let (status, answer, sent) = call(pool, &mock, &key, json!({"max_tokens": 64})).await;
    assert_eq!(status, StatusCode::OK, "{answer}");
    let body = only(&sent);
    assert_eq!(body["max_completion_tokens"], json!(64), "{body}");
    assert!(body.get("max_tokens").is_none(), "{body}");
    assert!(body.get("temperature").is_none(), "{body}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn azure_gpt56_tools_without_an_effort_are_sent_with_none(pool: PgPool) {
    let mock = openai_mock().await;
    let key = seed(&pool, "azure", "azure/gpt-5.6-terra", &mock.uri(), None).await;
    let (status, answer, sent) = call(pool, &mock, &key, json!({"tools": tools()})).await;
    assert_eq!(status, StatusCode::OK, "{answer}");
    assert_eq!(only(&sent)["reasoning_effort"], json!("none"));
}

// ---------------------------------------------------------------------------
// C6: Claude models that retired sampling parameters
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn claude_haiku_55_gets_no_sampling_parameters(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/messages"))
        .respond_with(ResponseTemplate::new(200).set_body_json(anthropic_fixture()))
        .mount(&mock)
        .await;
    let key = seed(&pool, "anthropic", "claude-haiku-5-5", &mock.uri(), None).await;
    let (status, answer, sent) = call(
        pool,
        &mock,
        &key,
        json!({"temperature": 0.3, "top_p": 0.8, "max_tokens": 32}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{answer}");
    let body = only(&sent);
    assert!(body.get("temperature").is_none(), "{body}");
    assert!(body.get("top_p").is_none(), "{body}");
    assert_eq!(body["max_tokens"], json!(32), "{body}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_rows_drop_list_removes_sampling_parameters_on_any_platform(pool: PgPool) {
    // The catalog rows of the Claude 5.5 models carry
    // `additional_drop_params: ["temperature", "top_p"]`; a row can add
    // `top_k`. The row is enough: no version rule is needed for a platform
    // id (here an OpenAI-compatible provider serving a Claude model).
    let mock = openai_mock().await;
    let key = seed(
        &pool,
        "openai",
        "claude-haiku-5-5-compat",
        &mock.uri(),
        None,
    )
    .await;
    sqlx::query(
        "UPDATE llm_model SET additional_drop_params = '[\"temperature\",\"top_p\",\"top_k\"]' \
         WHERE id = 'm-fam'",
    )
    .execute(&pool)
    .await
    .expect("set drop list");
    let (status, answer, sent) = call(
        pool,
        &mock,
        &key,
        json!({"temperature": 0.3, "top_p": 0.8, "top_k": 40}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{answer}");
    let body = only(&sent);
    for key in ["temperature", "top_p", "top_k"] {
        assert!(body.get(key).is_none(), "{key}: {body}");
    }
}

// ---------------------------------------------------------------------------
// Claude 5.5 on Bedrock
// ---------------------------------------------------------------------------
//
// Per the catalog, Claude 5.5 refuses non-default sampling
// parameters (`temperature`, `top_p`: retired from 4.8 up) and a thinking
// `budget_tokens` (adaptive thinking only, from 4.7 up a fixed budget is a
// 400). The Anthropic adapter follows both rules for `provider = anthropic`.
// A Bedrock Claude model goes through the native bridge
// (`anthropic_adapter::forward_via_openai_surface_*`), and what reaches
// Bedrock is the evidence.

/// A Bedrock mock: any `POST …/invoke` answers a Messages response.
async fn bedrock_mock() -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(anthropic_fixture()))
        .mount(&mock)
        .await;
    mock
}

/// Gives the row `m-fam` the cloud credential `config` in its
/// `provider_config`, where a cloud row's credential is.
async fn set_cloud_credential(pool: &PgPool, auth_type: &str, config: Value) {
    use brutor_gateway_core::services::encryption::EncryptionService;
    let encrypted = EncryptionService::new("test-encryption-key-32-chars-ok!")
        .encrypt(&config.to_string())
        .expect("encrypt provider_config");
    sqlx::query(
        "UPDATE llm_model SET api_key = NULL, auth_type = $1, provider_config = $2 \
          WHERE id = 'm-fam'",
    )
    .bind(auth_type)
    .bind(encrypted)
    .execute(pool)
    .await
    .expect("set the cloud credential");
}

/// A made-up AWS credential: it signs requests to a mock on loopback.
async fn set_made_up_aws_credential(pool: &PgPool) {
    set_cloud_credential(
        pool,
        "aws_iam",
        json!({"aws_access_key_id": "AKIAFAMILYMADEUP0001",
               "aws_secret_access_key": "familyRulesMadeUpSecretNotARealKey",
               "aws_region": "us-east-1"}),
    )
    .await;
}

/// One call to the seeded Bedrock Sonnet 5.5 row; the path and the body
/// that reached Bedrock.
async fn bedrock_sonnet_55(pool: PgPool, request: Value) -> (String, Value) {
    let mock = bedrock_mock().await;
    let key = seed(
        &pool,
        "bedrock",
        "bedrock/global.anthropic.claude-sonnet-5-5",
        &mock.uri(),
        // The seed row's capabilities (`bedrock_models_init.py`).
        Some(
            json!({"context_window": 1_000_000, "max_output_tokens": 128_000,
                    "modalities": ["text", "vision"], "supports_function_calling": true,
                    "supports_reasoning": true, "supports_response_schema": true}),
        ),
    )
    .await;
    // A Bedrock request is sent signed or not at all: the row has a
    // made-up AWS credential.
    set_made_up_aws_credential(&pool).await;
    let (status, answer, sent) = call(pool, &mock, &key, request).await;
    assert_eq!(status, StatusCode::OK, "{answer}");
    let body = only(&sent).clone();
    let path = mock.received_requests().await.unwrap_or_default()[0]
        .url
        .path()
        .to_string();
    (path, body)
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn bedrock_claude_55_gets_no_sampling_parameters(pool: PgPool) {
    let (path, body) = bedrock_sonnet_55(
        pool,
        json!({"temperature": 0.2, "top_p": 0.9, "max_tokens": 64}),
    )
    .await;
    assert!(
        body.get("temperature").is_none() && body.get("top_p").is_none(),
        "Claude 5.5 refuses sampling parameters: {body}"
    );
    assert_eq!(body["max_tokens"], json!(64), "{body}");
    assert_eq!(
        path, "/model/global.anthropic.claude-sonnet-5-5/invoke",
        "the Bedrock id is the row's name without the catalog's `bedrock/` prefix"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn bedrock_claude_55_gets_adaptive_thinking_and_no_budget(pool: PgPool) {
    let (_, body) =
        bedrock_sonnet_55(pool, json!({"reasoning_effort": "high", "max_tokens": 64})).await;
    assert!(
        body.get("reasoning_effort").is_none(),
        "not a Messages parameter: {body}"
    );
    assert!(
        body.pointer("/thinking/budget_tokens").is_none(),
        "Claude 5.5 refuses a thinking budget: {body}"
    );
    if let Some(thinking) = body.get("thinking") {
        assert_eq!(thinking["type"], json!("adaptive"), "{body}");
    }
    assert!(body.get("temperature").is_none(), "{body}");
}

// ---------------------------------------------------------------------------
// The Converse rows of the catalog
// ---------------------------------------------------------------------------

/// One call to a seeded Bedrock Converse Claude row
/// (`bedrock_converse_models_init.py`); the status, the path and the body
/// that reached the mock.
async fn bedrock_converse_sonnet_55(pool: PgPool, request: Value) -> (StatusCode, String, Value) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(anthropic_fixture()))
        .mount(&mock)
        .await;
    let key = seed(
        &pool,
        "bedrock_converse",
        "bedrock/converse/us.anthropic.claude-sonnet-5-5",
        &mock.uri(),
        Some(
            json!({"context_window": 1_000_000, "max_output_tokens": 128_000,
                    "modalities": ["text", "vision"], "supports_function_calling": true,
                    "supports_reasoning": true, "supports_response_schema": true}),
        ),
    )
    .await;
    set_made_up_aws_credential(&pool).await;
    let (status, answer, sent) = call(pool, &mock, &key, request).await;
    let received = mock.received_requests().await.unwrap_or_default();
    let path = received
        .first()
        .map(|r| r.url.path().to_string())
        .unwrap_or_default();
    assert_eq!(status, StatusCode::OK, "{answer}");
    (status, path, sent.first().cloned().unwrap_or(Value::Null))
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_bedrock_converse_claude_55_row_gets_no_sampling_parameters(pool: PgPool) {
    let (status, path, body) = bedrock_converse_sonnet_55(
        pool,
        json!({"temperature": 0.2, "top_p": 0.9, "max_tokens": 64}),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        path, "/model/us.anthropic.claude-sonnet-5-5/invoke",
        "InvokeModel with the row's id, without the catalog's `bedrock/converse/` prefix"
    );
    assert_eq!(body["max_tokens"], json!(64), "{body}");
    assert!(
        body.get("temperature").is_none()
            && body.get("top_p").is_none()
            && body.pointer("/inferenceConfig/temperature").is_none()
            && body.pointer("/inferenceConfig/topP").is_none(),
        "Claude 5.5 refuses sampling parameters: {path} {body}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_bedrock_converse_claude_55_row_gets_no_thinking_budget(pool: PgPool) {
    let (status, path, body) =
        bedrock_converse_sonnet_55(pool, json!({"reasoning_effort": "high", "max_tokens": 64}))
            .await;
    assert_eq!(status, StatusCode::OK);
    assert!(
        !body.to_string().contains("budget_tokens") && body.get("reasoning_effort").is_none(),
        "Claude 5.5 refuses a thinking budget: {path} {body}"
    );
}

// ---------------------------------------------------------------------------
// Claude 5.5 on Vertex AI
// ---------------------------------------------------------------------------

/// One call to the seeded Vertex Claude Sonnet 5.5 row
/// (`vertex_ai/claude-sonnet-5-5`) through the native bridge; the path and
/// the body that reached the mock's `:rawPredict`. The Google access token
/// comes from the same mock: the model's key is a service account whose
/// `token_uri` is the mock's.
async fn vertex_sonnet_55(pool: PgPool, request: Value) -> (String, Value) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/token"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "access_token": "ya29.test", "expires_in": 3600, "token_type": "Bearer"
        })))
        .mount(&mock)
        .await;
    Mock::given(method("POST"))
        .and(wiremock::matchers::path_regex(":rawPredict$"))
        .respond_with(ResponseTemplate::new(200).set_body_json(anthropic_fixture()))
        .mount(&mock)
        .await;
    let endpoint = format!(
        "{}/v1/projects/p/locations/global/publishers/anthropic/models/claude-sonnet-5-5",
        mock.uri()
    );
    let key = seed(
        &pool,
        "vertex_ai",
        "vertex_ai/claude-sonnet-5-5",
        &endpoint,
        Some(
            json!({"context_window": 1_000_000, "max_output_tokens": 128_000,
                    "modalities": ["text", "vision"], "supports_function_calling": true,
                    "supports_reasoning": true, "supports_response_schema": true}),
        ),
    )
    .await;
    let service_account = json!({
        "type": "service_account",
        "project_id": "p",
        "private_key_id": "k1",
        "private_key": crate::workload_identity::TEST_RSA_PEM,
        "client_email": "gateway-test@p.iam.gserviceaccount.com",
        "client_id": "1",
        "token_uri": format!("{}/token", mock.uri()),
    });
    set_cloud_credential(
        &pool,
        "gcp_service_account",
        json!({"gcp_project_id": "p", "gcp_location": "global",
               "gcp_credentials_json": service_account.to_string()}),
    )
    .await;

    let (status, answer, _) = call(pool, &mock, &key, request).await;
    assert_eq!(status, StatusCode::OK, "{answer}");
    let received = mock.received_requests().await.unwrap_or_default();
    let sent = received
        .iter()
        .find(|r| r.url.path().ends_with(":rawPredict"))
        .expect("a rawPredict call");
    assert_eq!(
        sent.headers
            .get("authorization")
            .and_then(|v| v.to_str().ok()),
        Some("Bearer ya29.test")
    );
    (
        sent.url.path().to_string(),
        serde_json::from_slice(&sent.body).expect("a JSON body"),
    )
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn vertex_claude_55_gets_no_sampling_parameters(pool: PgPool) {
    let (path, body) = vertex_sonnet_55(
        pool,
        json!({"temperature": 0.2, "top_p": 0.9, "max_tokens": 64}),
    )
    .await;
    assert!(
        body.get("temperature").is_none() && body.get("top_p").is_none(),
        "Claude 5.5 refuses sampling parameters: {path} {body}"
    );
    assert_eq!(body["max_tokens"], json!(64), "{body}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn vertex_claude_55_gets_adaptive_thinking_and_no_budget(pool: PgPool) {
    let (path, body) =
        vertex_sonnet_55(pool, json!({"reasoning_effort": "high", "max_tokens": 64})).await;
    assert!(
        body.get("reasoning_effort").is_none() && body.get("effort").is_none(),
        "not Messages parameters: {path} {body}"
    );
    assert!(
        body.pointer("/thinking/budget_tokens").is_none(),
        "Claude 5.5 refuses a thinking budget: {path} {body}"
    );
    if let Some(thinking) = body.get("thinking") {
        assert_eq!(thinking["type"], json!("adaptive"), "{body}");
    }
    assert!(body.get("temperature").is_none(), "{body}");
}

// The Bedrock rows the gateway does not implement
// ---------------------------------------------------------------------------
//
// On Bedrock the gateway speaks InvokeModel with an Anthropic body for
// Claude rows (`anthropic_adapter::backend::bedrock`), Converse for the
// Nova, Mistral and DeepSeek chat rows and InvokeModel for the Titan Text
// and Cohere v3 embedding rows (`bedrock_runtime`; those rows are
// tested in `bedrock_runtime.rs`). A row of any other family of `bedrock`
// / `bedrock_converse` went to the generic forwarder: an OpenAI chat
// completion, `Authorization: Bearer` or no credential, unsigned, the
// catalog name as `model`, to the row's `api_url` + `/chat/completions`.
// No Bedrock endpoint accepts that. Such a row is refused at dispatch with
// a message naming the provider API, before anything is sent.

async fn a_non_claude_bedrock_row_is_refused(pool: PgPool, provider: &str, model_name: &str) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(chat_fixture()))
        .mount(&mock)
        .await;
    let key = seed(&pool, provider, model_name, &mock.uri(), None).await;
    let (status, answer, _) = call(pool.clone(), &mock, &key, json!({"max_tokens": 16})).await;
    let received = mock.received_requests().await.unwrap_or_default();
    let went_out: Vec<String> = received
        .iter()
        .map(|r| {
            format!(
                "{} {} authorization={:?} body={}",
                r.method,
                r.url.path(),
                r.headers.get("authorization").map(|v| v
                    .to_str()
                    .unwrap_or("")
                    .split(' ')
                    .next()
                    .unwrap_or("")
                    .to_string()),
                String::from_utf8_lossy(&r.body)
            )
        })
        .collect();
    assert!(
        received.is_empty(),
        "{provider} / {model_name}: nothing may be sent; this went out: {went_out:?}"
    );
    assert_eq!(status, StatusCode::NOT_IMPLEMENTED, "{answer}");
    let message = answer.to_string();
    assert!(
        message.contains("unsupported_provider_api") && message.contains(provider),
        "the refusal names the provider API: {message}"
    );
    // Recorded, on the caller's group.
    let mut row: Option<(Option<i32>, Option<String>, Option<String>)> = None;
    for _ in 0..60 {
        row = sqlx::query_as(
            "SELECT http_status_code, error_message, resource_group_id FROM proxy_logs \
              WHERE tenant_id = 'tenant-1' AND http_status_code = 501",
        )
        .fetch_optional(&pool)
        .await
        .unwrap();
        if row.is_some() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    let (_, error, group) = row.expect("a refusal row");
    assert!(
        error
            .unwrap_or_default()
            .contains("unsupported_provider_api")
    );
    assert_eq!(group.as_deref(), Some("rg-llm"));
}

macro_rules! non_claude_bedrock_rows {
    ($($name:ident = ($provider:expr, $model:expr)),+ $(,)?) => {$(
        #[sqlx::test(migrations = "./tests/migrations")]
        async fn $name(pool: PgPool) {
            a_non_claude_bedrock_row_is_refused(pool, $provider, $model).await;
        }
    )+};
}

non_claude_bedrock_rows!(
    a_bedrock_llama_row_is_refused = ("bedrock", "bedrock/meta.llama3-3-70b-instruct-v1:0"),
    a_bedrock_titan_text_row_is_refused = ("bedrock", "bedrock/amazon.titan-text-express-v1"),
    a_bedrock_cohere_command_row_is_refused = ("bedrock", "bedrock/cohere.command-r-plus-v1:0"),
    a_bedrock_converse_llama_row_is_refused = (
        "bedrock_converse",
        "bedrock/converse/us.meta.llama4-maverick-17b-instruct-v1:0"
    ),
    a_bedrock_converse_qwen_row_is_refused =
        ("bedrock_converse", "bedrock/converse/qwen.qwen3-32b-v1:0"),
    a_bedrock_converse_jamba_row_is_refused = (
        "bedrock_converse",
        "bedrock/converse/ai21.jamba-1-5-large-v1:0"
    ),
);

// A structured-output request to a Claude model
// ---------------------------------------------------------------------------

/// A caller's `response_format` JSON schema reaches Anthropic as
/// `output_config.format` (the Messages API's structured output; the schema
/// without the constraints it does not take). The adapter filtered the
/// schema and then removed `response_format`: nothing reached the provider
/// and the caller got free text with no error.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_json_schema_response_format_reaches_claude_as_output_config(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(anthropic_fixture()))
        .mount(&mock)
        .await;
    let key = seed(
        &pool,
        "anthropic",
        "claude-haiku-5-5",
        &mock.uri(),
        Some(json!({"supports_response_schema": true})),
    )
    .await;
    let (status, answer, sent) = call(
        pool,
        &mock,
        &key,
        json!({"max_tokens": 64, "response_format": {"type": "json_schema", "json_schema": {
            "name": "verdict", "strict": true,
            "schema": {"type": "object",
                       "properties": {"score": {"type": "integer", "minimum": 0}},
                       "required": ["score"], "additionalProperties": false}}}}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{answer}");
    let body = only(&sent);
    assert!(body.get("response_format").is_none(), "{body}");
    assert_eq!(
        body.pointer("/output_config/format"),
        Some(&json!({"type": "json_schema", "schema": {"type": "object",
            "properties": {"score": {"type": "integer", "description": "Constraints: minimum=0"}},
            "required": ["score"], "additionalProperties": false}})),
        "{body}"
    );
}
