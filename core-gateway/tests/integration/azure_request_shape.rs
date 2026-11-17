// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The request the gateway sends Azure OpenAI.
//!
//! What Microsoft documents:
//!
//! - Deployment-based endpoint
//!   (<https://learn.microsoft.com/en-us/azure/ai-foundry/openai/reference>):
//!   "The service APIs are versioned using the `api-version` query
//!   parameter", e.g. `POST https://YOUR_RESOURCE_NAME.openai.azure.com/openai/
//!   deployments/YOUR_DEPLOYMENT_NAME/chat/completions?api-version=2024-06-01`;
//!   `api-version` is listed "In: query, Required: Yes"; the key travels in
//!   the `api-key` header.
//! - v1 endpoint
//!   (<https://learn.microsoft.com/en-us/azure/ai-foundry/openai/api-version-lifecycle>):
//!   `https://YOUR-RESOURCE-NAME.openai.azure.com/openai/v1/chat/completions`,
//!   "`api-version` is no longer a required parameter with the v1 GA API",
//!   and the body's `model` is the deployment: `"model": "MAI-DS-R1"`
//!   ("Replace with your model deployment name").
//!
//! What the gateway sent: `api-version` as an HTTP header (the query
//! parameter the deployment endpoint requires was never sent), and as
//! `model` the catalog id with its `azure/` prefix (`azure/gpt-5.2`), which
//! names no deployment: a deployment name cannot contain `/`.
//!
//! The model row's `api_url` is the endpoint up to and excluding
//! `/chat/completions` (either form); the request that reaches the mock is
//! the evidence.

use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

const T: &str = "tenant-azure";
const RG: &str = "rg-azure";

async fn azure() -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "chatcmpl-azure", "object": "chat.completion", "created": 1_700_000_000,
            "model": "gpt-5.2",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}
        })))
        .mount(&mock)
        .await;
    mock
}

/// One Azure model whose endpoint is `api_url`, and a key that reaches it.
async fn seed(
    pool: &PgPool,
    provider: &str,
    model_name: &str,
    api_url: &str,
    api_version: Option<&str>,
) -> String {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_llm_provider(pool, "prov-azure", T, provider).await;
    common::seed_llm_model_with_api_url(pool, "m-azure", T, "prov-azure", model_name, api_url)
        .await;
    common::set_model_api_key(pool, "m-azure", "azure-test-key").await;
    sqlx::query("UPDATE llm_model SET api_version = $1 WHERE id = 'm-azure'")
        .bind(api_version)
        .execute(pool)
        .await
        .expect("api_version");
    common::bind_llm_model_to_rg(pool, "m-azure", RG).await;
    common::seed_api_key(pool, T, RG, None, "azure-key").await
}

/// One chat call; the one request the mock received.
async fn chat(pool: PgPool, mock: &MockServer, key: &str) -> wiremock::Request {
    let app = common::build_llm_app(common::build_test_state(pool));
    let req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({"model_id": "m-azure", "messages": [{"role": "user", "content": "hi"}]}),
        Some(key),
        Some(T),
    );
    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let body = response
        .into_body()
        .collect()
        .await
        .expect("body")
        .to_bytes();
    assert_eq!(status, StatusCode::OK, "{}", String::from_utf8_lossy(&body));
    let mut received = mock.received_requests().await.unwrap_or_default();
    assert_eq!(received.len(), 1, "one upstream request");
    received.remove(0)
}

fn header<'a>(request: &'a wiremock::Request, name: &str) -> Option<&'a str> {
    request.headers.get(name).and_then(|v| v.to_str().ok())
}

fn body(request: &wiremock::Request) -> Value {
    serde_json::from_slice(&request.body).expect("a JSON body")
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_deployment_endpoint_gets_api_version_as_a_query_parameter(pool: PgPool) {
    let mock = azure().await;
    let key = seed(
        &pool,
        "azure",
        "azure/gpt-5.2",
        &format!("{}/openai/deployments/my-deployment", mock.uri()),
        Some("2024-10-21"),
    )
    .await;
    let sent = chat(pool, &mock, &key).await;

    assert_eq!(
        sent.url.path(),
        "/openai/deployments/my-deployment/chat/completions"
    );
    assert_eq!(
        sent.url.query(),
        Some("api-version=2024-10-21"),
        "Microsoft: `api-version`, in query, required"
    );
    assert_eq!(
        header(&sent, "api-version"),
        None,
        "it is a query parameter, not a header"
    );
    assert_eq!(header(&sent, "api-key"), Some("azure-test-key"));
    assert_eq!(header(&sent, "authorization"), None);
    assert_eq!(
        body(&sent)["model"],
        json!("gpt-5.2"),
        "the catalog's `azure/` prefix is not part of any Azure name"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_v1_endpoint_gets_the_deployment_as_model_and_no_api_version(pool: PgPool) {
    let mock = azure().await;
    // The operator's deployment is the row's model name, under the
    // catalog's prefix; no API version is set for the v1 endpoint.
    let key = seed(
        &pool,
        "azure",
        "azure/my-deployment",
        &format!("{}/openai/v1", mock.uri()),
        None,
    )
    .await;
    let sent = chat(pool, &mock, &key).await;

    assert_eq!(sent.url.path(), "/openai/v1/chat/completions");
    assert_eq!(
        sent.url.query(),
        None,
        "no `api-version` on the v1 endpoint"
    );
    assert_eq!(header(&sent, "api-version"), None);
    assert_eq!(header(&sent, "api-key"), Some("azure-test-key"));
    assert_eq!(
        body(&sent)["model"],
        json!("my-deployment"),
        "Microsoft: `model` is the model deployment name"
    );
}

/// Azure AI Foundry rows carry `azure_ai/`; the same rule.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_foundry_id_is_sent_without_its_catalog_prefix(pool: PgPool) {
    let mock = azure().await;
    let key = seed(
        &pool,
        "azure_ai",
        "azure_ai/DeepSeek-V3.2",
        &format!("{}/openai/v1", mock.uri()),
        None,
    )
    .await;
    let sent = chat(pool, &mock, &key).await;
    assert_eq!(body(&sent)["model"], json!("DeepSeek-V3.2"));
}

/// A name with no catalog prefix is sent as it is, and a prefix that is not
/// the provider's own is not stripped.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_name_without_the_catalog_prefix_is_sent_as_it_is(pool: PgPool) {
    let mock = azure().await;
    let key = seed(
        &pool,
        "azure",
        "prod-gpt5",
        &format!("{}/openai/v1", mock.uri()),
        None,
    )
    .await;
    let sent = chat(pool, &mock, &key).await;
    assert_eq!(body(&sent)["model"], json!("prod-gpt5"));
}

/// An operator pastes the endpoint as Azure shows
/// it, query string included (`…/deployments/d?api-version=…`). The endpoint
/// path was appended after the query (`…?api-version=2024-10-21/chat/completions`),
/// so the request went to the deployment's root with a version no API has.
/// The query of an `api_url` stays the query; the path gets the endpoint.
/// The version the URL states is the one sent: the row's `api_version` does
/// not add a second `api-version`.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_api_url_with_a_query_string_keeps_it_as_the_query(pool: PgPool) {
    let mock = azure().await;
    let key = seed(
        &pool,
        "azure",
        "azure/gpt-5.2",
        &format!(
            "{}/openai/deployments/my-deployment?api-version=2024-10-21",
            mock.uri()
        ),
        Some("2024-02-01"),
    )
    .await;
    let sent = chat(pool, &mock, &key).await;
    assert_eq!(
        sent.url.path(),
        "/openai/deployments/my-deployment/chat/completions"
    );
    assert_eq!(sent.url.query(), Some("api-version=2024-10-21"));
}

/// The row's `api_version` joins a query that carries other parameters and
/// no `api-version`.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_rows_api_version_joins_an_api_urls_other_parameters(pool: PgPool) {
    let mock = azure().await;
    let key = seed(
        &pool,
        "azure",
        "azure/gpt-5.2",
        &format!("{}/openai/deployments/my-deployment/?trace=1", mock.uri()),
        Some("2024-10-21"),
    )
    .await;
    let sent = chat(pool, &mock, &key).await;
    assert_eq!(
        sent.url.path(),
        "/openai/deployments/my-deployment/chat/completions"
    );
    assert_eq!(sent.url.query(), Some("trace=1&api-version=2024-10-21"));
}
