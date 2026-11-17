// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A call refused because the guardrails could not be loaded is on the record.
//!
//! The gateway refuses (503) when it cannot read the guardrail configuration:
//! it will not pass a call it cannot check. If that refusal left
//! no `proxy_logs` row on the LLM, MCP and skills surfaces, the record would have
//! no trace of it; written as a guardrail block on A2A (403
//! `POLICY_BLOCKED`), it would read as if a policy had judged the message. The row is
//! written and sealed `engine_failure` everywhere: the gateway refused, and no
//! policy decided it.

use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::{Mock, MockServer, ResponseTemplate, matchers::method};

use brutor_gateway_core::middleware;
use brutor_gateway_core::routes;

use crate::common;

const T: &str = "t-gr-unavail";
const RG: &str = "rg-gr-unavail";
const CARD: &str = "card-gu";
const CARD_NAME: &str = "ledger-agent";

/// Nothing may reach the upstream.
async fn upstream() -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({})))
        .expect(0)
        .mount(&mock)
        .await;
    mock
}

async fn topology(pool: &PgPool, mock_uri: &str) -> String {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_end_user(pool, 8921, T).await;
    common::add_direct_membership(pool, 8921, RG, T).await;
    common::seed_llm_provider(pool, "prov-gu", T, "openai").await;
    common::seed_llm_model_with_api_url(pool, "m-gu", T, "prov-gu", "test-gpt", mock_uri).await;
    common::bind_llm_model_to_rg(pool, "m-gu", RG).await;
    common::seed_mcp_server_with_base_url(pool, "ms-gu", T, "crm", mock_uri).await;
    common::seed_server_config(pool, "sc-gu", T, "ms-gu").await;
    common::bind_server_config_to_rg(pool, "sc-gu", RG).await;
    common::seed_agent_skill(pool, "sk-gu", T, "report", "# Report", "Report").await;
    common::bind_skill_to_rg(pool, "sk-gu", RG).await;
    common::seed_agent_card(pool, CARD, T, CARD_NAME).await;
    sqlx::query("UPDATE agent_cards SET card_json = $2 WHERE id = $1")
        .bind(CARD)
        .bind(json!({"name": CARD_NAME, "url": mock_uri, "capabilities": [{"name": "echo"}]}))
        .execute(pool)
        .await
        .expect("card url");
    sqlx::query(
        "INSERT INTO agent_card_capabilities (id, agent_card_id, capability_name, handler_type, enabled) \
         VALUES ('cap-gu', $1, 'echo', 'noop', true)",
    )
    .bind(CARD)
    .execute(pool)
    .await
    .expect("capability");
    common::bind_agent_card_to_rg(pool, CARD, RG).await;
    common::seed_api_key(pool, T, RG, Some(8921), "gu-key").await
}

/// The guardrail configuration cannot be read.
async fn break_guardrails(pool: &PgPool) {
    sqlx::query("ALTER TABLE guardrail_configs RENAME TO guardrail_configs_unreadable")
        .execute(pool)
        .await
        .expect("rename guardrail_configs");
}

async fn send(app: axum::Router, key: &str, path: &str, body: Value) -> (StatusCode, String) {
    let req = common::build_post_request(path, body, Some(key), Some(T));
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (status, String::from_utf8_lossy(&bytes).into_owned())
}

/// The newest row of `proxy_type` in this tenant: (status, subtype).
async fn row(pool: &PgPool, proxy_type: &str) -> Option<(i32, Option<String>)> {
    for _ in 0..80 {
        let r: Option<(Option<i32>, Option<String>)> = sqlx::query_as(
            "SELECT http_status_code, proxy_subtype FROM proxy_logs \
              WHERE tenant_id = $1 AND proxy_type = $2 ORDER BY timestamp DESC LIMIT 1",
        )
        .bind(T)
        .bind(proxy_type)
        .fetch_optional(pool)
        .await
        .expect("proxy_logs query");
        if let Some((status, subtype)) = r {
            return Some((status.unwrap_or(0), subtype));
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    None
}

fn engine_failure() -> Option<(i32, Option<String>)> {
    Some((503, Some("engine_failure".to_string())))
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_llm_call_refused_for_unreadable_guardrails_is_recorded(pool: PgPool) {
    let mock = upstream().await;
    let key = topology(&pool, &mock.uri()).await;
    break_guardrails(&pool).await;
    let app = common::build_llm_app(common::build_test_state(pool.clone()));
    let (status, body) = send(
        app,
        &key,
        "/v1/proxy/llm/chat/completions",
        json!({"model_id": "m-gu", "messages": [{"role": "user", "content": "hi"}]}),
    )
    .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    assert_eq!(row(&pool, "llm").await, engine_failure());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_mcp_call_refused_for_unreadable_guardrails_is_recorded(pool: PgPool) {
    let mock = upstream().await;
    let key = topology(&pool, &mock.uri()).await;
    break_guardrails(&pool).await;
    let app = common::build_mcp_proxy_app(common::build_test_state(pool.clone()));
    let (status, body) = send(
        app,
        &key,
        "/v1/proxy/mcp/ms-gu",
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "crm_get_customer", "arguments": {}}}),
    )
    .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    assert_eq!(row(&pool, "mcp").await, engine_failure());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_skill_refused_for_unreadable_guardrails_is_recorded(pool: PgPool) {
    let mock = upstream().await;
    let key = topology(&pool, &mock.uri()).await;
    break_guardrails(&pool).await;
    let app = common::build_multimodal_app(common::build_test_state(pool.clone()));
    let (status, body) = send(
        app,
        &key,
        "/v1/proxy/skills/execute",
        json!({"skill_name": "report", "input_params": {"period": "Q2"}}),
    )
    .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    assert_eq!(row(&pool, "skill").await, engine_failure());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_a2a_v1_message_refused_for_unreadable_guardrails_is_recorded(pool: PgPool) {
    let mock = upstream().await;
    let key = topology(&pool, &mock.uri()).await;
    break_guardrails(&pool).await;
    let app = common::build_a2a_v1_app(common::build_test_state(pool.clone()));
    let (status, body) = send(
        app,
        &key,
        &format!("/a2a/{T}/{CARD_NAME}/message:send"),
        json!({"message": {"messageId": "m-gu-1", "role": "ROLE_USER",
                           "parts": [{"text": "post the ledger"}]}}),
    )
    .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    assert!(body.contains("UNAVAILABLE"), "{body}");
    assert_eq!(row(&pool, "a2a").await, engine_failure());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_legacy_a2a_call_refused_for_unreadable_guardrails_is_recorded(pool: PgPool) {
    let mock = upstream().await;
    let key = topology(&pool, &mock.uri()).await;
    break_guardrails(&pool).await;
    let state = common::build_test_state(pool.clone());
    let app = axum::Router::new()
        .nest("/v1/proxy/a2a", routes::a2a::router())
        .layer(middleware::tenant::TenantLayer::new(state.clone()))
        .layer(middleware::run_end::RunEndLayer::new(
            state.mcp_sessions.clone(),
        ))
        .with_state(state);
    let (status, body) = send(
        app,
        &key,
        "/v1/proxy/a2a/outbound",
        json!({"target_card_id": CARD, "capability": "echo",
               "message": {"parts": [{"kind": "text", "text": "post the ledger"}]}}),
    )
    .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    assert_eq!(row(&pool, "a2a").await, engine_failure());
}
