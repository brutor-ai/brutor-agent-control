// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A model asked for by name is the one the caller can reach.
//!
//! Two configured models may share a provider model name (the live case:
//! "Anthropic Claude Haiku 4.5" in E2ETests and "Sales — Claude Haiku 4.5" in
//! Sales, both `claude-haiku-4-5-20251001`). Name resolution picked one row
//! for the whole tenant before access was considered, so a caller whose group
//! held the other row got 403 "your resource-group memberships does not grant
//! access" for a model their group does have. The name now resolves to the
//! first of the same-named rows the caller can reach, under the same access
//! rule the call is then checked with; when none is reachable the refusal is
//! unchanged.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::json;
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

const T: &str = "t-dup-names";

async fn upstream() -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "chatcmpl-dup", "object": "chat.completion", "created": 1_700_000_000,
            "model": "dup-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}
        })))
        .mount(&mock)
        .await;
    mock
}

/// Two rows named "dup-model": the other team's is weighted higher, so a
/// tenant-wide pick lands on it.
async fn two_rows_one_name(pool: &PgPool, mock: &MockServer) -> (String, String) {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, "rg-mine", T).await;
    common::seed_resource_group(pool, "rg-sales", T).await;
    common::seed_llm_provider(pool, "prov-d", T, "openai").await;
    common::seed_llm_model_with_api_url(pool, "m-sales", T, "prov-d", "dup-model", &mock.uri())
        .await;
    common::seed_llm_model_with_api_url(pool, "m-mine", T, "prov-d", "dup-model", &mock.uri())
        .await;
    sqlx::query("UPDATE llm_model SET routing_weight = 100 WHERE id = 'm-sales'")
        .execute(pool)
        .await
        .unwrap();
    common::bind_llm_model_to_rg(pool, "m-sales", "rg-sales").await;
    common::bind_llm_model_to_rg(pool, "m-mine", "rg-mine").await;
    (
        common::seed_key(pool, T, Some("rg-mine"), false, "mine").await,
        common::seed_key(pool, T, Some("rg-sales"), false, "sales").await,
    )
}

async fn chat(pool: &PgPool, key: &str) -> (StatusCode, String) {
    let app = common::build_llm_app(common::build_test_state(pool.clone()));
    let req: Request<Body> = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({"model": "dup-model", "messages": [{"role": "user", "content": "hi"}]}),
        Some(key),
        Some(T),
    );
    let resp = app.oneshot(req).await.expect("router");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (status, String::from_utf8_lossy(&bytes).into_owned())
}

/// Was: 403 — the name resolved to the Sales row.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_shared_name_resolves_to_the_row_the_callers_group_holds(pool: PgPool) {
    let mock = upstream().await;
    let (mine, sales) = two_rows_one_name(&pool, &mock).await;
    let (status, body) = chat(&pool, &mine).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    // And the other team still gets theirs.
    let (status, body) = chat(&pool, &sales).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(mock.received_requests().await.unwrap().len(), 2);
}

/// A caller whose group holds none of the same-named rows is refused as
/// before, and nothing reaches the provider.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_caller_who_holds_none_of_them_is_still_refused(pool: PgPool) {
    let mock = upstream().await;
    two_rows_one_name(&pool, &mock).await;
    common::seed_resource_group(&pool, "rg-none", T).await;
    let key = common::seed_key(&pool, T, Some("rg-none"), false, "none").await;
    let (status, body) = chat(&pool, &key).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert_eq!(mock.received_requests().await.unwrap().len(), 0);
}
