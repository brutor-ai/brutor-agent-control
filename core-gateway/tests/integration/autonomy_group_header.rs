// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The autonomy kill switch on the LLM surface, for a caller with an API key.
//!
//! A key is bound to one resource group. `X-Resource-Group` is a client
//! header and names a group only for a person (a Portal session, whose
//! variants are the Enterprise suite's `autonomy_group_header.rs`); for a key
//! it is not a way out of its group. Pinned here, on the composition of the
//! edition running the suite: a key of a suspended AI System is refused
//! whatever group the header names, and a key of a permissive group is
//! served — also when the header names the suspended group, because the
//! switch is judged for the key's own group.

use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

const TENANT: &str = "auto-hdr-tenant";
const PAUSED: &str = "rg-auto-paused";
const OPEN: &str = "rg-auto-open";

async fn body_json(body: axum::body::Body) -> Value {
    let bytes = body.collect().await.expect("collect body").to_bytes();
    serde_json::from_slice(&bytes).unwrap_or(Value::Null)
}

async fn seed(pool: &PgPool) -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "chatcmpl-auto-hdr",
            "object": "chat.completion",
            "created": 1_700_000_000,
            "model": "test-gpt",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        })))
        .mount(&mock)
        .await;

    common::seed_tenant(pool, TENANT).await;
    common::seed_resource_group(pool, PAUSED, TENANT).await;
    common::seed_resource_group(pool, OPEN, TENANT).await;
    sqlx::query("UPDATE resource_groups SET autonomy_level = 'suspended' WHERE id = $1")
        .bind(PAUSED)
        .execute(pool)
        .await
        .unwrap();
    sqlx::query("UPDATE resource_groups SET autonomy_level = 'autonomous' WHERE id = $1")
        .bind(OPEN)
        .execute(pool)
        .await
        .unwrap();

    common::seed_llm_provider(pool, "prov-auto", TENANT, "openai").await;
    common::seed_llm_model_with_api_url(
        pool,
        "m-auto",
        TENANT,
        "prov-auto",
        "test-gpt",
        &mock.uri(),
    )
    .await;
    common::bind_llm_model_to_rg(pool, "m-auto", PAUSED).await;
    common::bind_llm_model_to_rg(pool, "m-auto", OPEN).await;
    mock
}

fn chat(token: &str, group_header: Option<&str>) -> axum::http::Request<axum::body::Body> {
    let mut req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({ "model_id": "m-auto", "messages": [{"role": "user", "content": "hi"}] }),
        Some(token),
        Some(TENANT),
    );
    if let Some(g) = group_header {
        req.headers_mut()
            .insert("x-resource-group", g.parse().unwrap());
    }
    req
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_key_of_a_suspended_system_naming_a_permissive_group_stays_suspended(pool: PgPool) {
    let mock = seed(&pool).await;
    let key = common::seed_api_key(&pool, TENANT, PAUSED, None, "k-paused").await;
    let app = common::build_llm_app(common::build_test_state(pool));

    let resp = app.oneshot(chat(&key, Some(OPEN))).await.unwrap();
    let status = resp.status();
    let body = body_json(resp.into_body()).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "body={body}");
    assert_ne!(body["choices"][0]["message"]["content"], "ok");
    assert!(
        mock.received_requests()
            .await
            .unwrap_or_default()
            .is_empty(),
        "a suspended system's key reached the model"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_key_of_a_suspended_system_stays_suspended_without_the_header(pool: PgPool) {
    let mock = seed(&pool).await;
    let key = common::seed_api_key(&pool, TENANT, PAUSED, None, "k-paused").await;
    let app = common::build_llm_app(common::build_test_state(pool));

    let resp = app.oneshot(chat(&key, None)).await.unwrap();
    let status = resp.status();
    let body = body_json(resp.into_body()).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "body={body}");
    assert!(
        mock.received_requests()
            .await
            .unwrap_or_default()
            .is_empty(),
        "a suspended system's key reached the model"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_key_of_a_permissive_group_is_served(pool: PgPool) {
    let _mock = seed(&pool).await;
    let key = common::seed_api_key(&pool, TENANT, OPEN, None, "k-open").await;
    let app = common::build_llm_app(common::build_test_state(pool));

    let resp = app.oneshot(chat(&key, Some(OPEN))).await.unwrap();
    let status = resp.status();
    let body = body_json(resp.into_body()).await;
    assert_eq!(status, StatusCode::OK, "body={body}");
    assert_eq!(body["choices"][0]["message"]["content"], "ok");
}

/// The switch is judged for the key's own group: naming the suspended group
/// in the header neither suspends a permissive group's key nor moves the
/// call's attribution to the group named.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_header_does_not_move_a_key_to_another_group(pool: PgPool) {
    let _mock = seed(&pool).await;
    let key = common::seed_api_key(&pool, TENANT, OPEN, None, "k-open").await;
    let app = common::build_llm_app(common::build_test_state(pool.clone()));

    let resp = app.oneshot(chat(&key, Some(PAUSED))).await.unwrap();
    let status = resp.status();
    let body = body_json(resp.into_body()).await;
    assert_eq!(status, StatusCode::OK, "body={body}");
    let mut group: Option<String> = None;
    for _ in 0..120 {
        group = sqlx::query_scalar(
            "SELECT resource_group_id FROM proxy_logs WHERE tenant_id = $1 AND success \
             ORDER BY timestamp DESC LIMIT 1",
        )
        .bind(TENANT)
        .fetch_optional(&pool)
        .await
        .expect("proxy_logs")
        .flatten();
        if group.is_some() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    assert_eq!(
        group.as_deref(),
        Some(OPEN),
        "the call is the key's group's"
    );
}
