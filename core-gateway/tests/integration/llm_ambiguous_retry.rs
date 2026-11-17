// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! LLM retries after an ambiguous upstream status.
//!
//! A 524 (Cloudflare: the origin accepted the request and did not answer in
//! time) or a 504 means the provider may have generated and billed a
//! completion we never saw. The inline retry used to buy it again; now it
//! does not, and the audit row records the unknown outcome. A status that
//! says the request was never processed (529 overloaded) is still retried.

use axum::http::StatusCode;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::any;
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

async fn provider_answering(status: u16) -> MockServer {
    let server = MockServer::start().await;
    Mock::given(any())
        .respond_with(
            ResponseTemplate::new(status).set_body_json(json!({"error": {"message": "edge"}})),
        )
        .mount(&server)
        .await;
    server
}

/// One tenant whose model points at `upstream`; returns its API key.
async fn tenant(pool: &PgPool, t: &str, upstream: &str) -> String {
    common::seed_tenant(pool, t).await;
    let rg = format!("{t}-rg");
    common::seed_resource_group(pool, &rg, t).await;
    common::seed_end_user(pool, 7600, t).await;
    common::add_direct_membership(pool, 7600, &rg, t).await;
    common::seed_llm_provider(pool, &format!("{t}-prov"), t, "openai").await;
    common::seed_llm_model_with_api_url(
        pool,
        &format!("{t}-m"),
        t,
        &format!("{t}-prov"),
        "gpt-x",
        upstream,
    )
    .await;
    common::bind_llm_model_to_rg(pool, &format!("{t}-m"), &rg).await;
    common::seed_api_key(pool, t, &rg, Some(7600), &format!("{t}-key")).await
}

async fn chat(pool: &PgPool, t: &str, key: &str) -> StatusCode {
    let app = common::build_llm_app(common::build_test_state(pool.clone()));
    app.oneshot(common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({"model_id": format!("{t}-m"), "messages": [{"role": "user", "content": "hi"}]}),
        Some(key),
        Some(t),
    ))
    .await
    .unwrap()
    .status()
}

async fn ambiguous_on_row(pool: &PgPool, t: &str) -> Option<Value> {
    for _ in 0..100 {
        let row: Option<(Option<Value>,)> = sqlx::query_as(
            "SELECT custom_metadata FROM proxy_logs WHERE tenant_id = $1 AND proxy_type = 'llm' \
              ORDER BY timestamp DESC LIMIT 1",
        )
        .bind(t)
        .fetch_optional(pool)
        .await
        .unwrap();
        if let Some((meta,)) = row {
            return meta.and_then(|m| m.get("ambiguous_upstream").cloned());
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    panic!("{t}: no proxy_logs row");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_origin_timeout_is_not_bought_twice(pool: PgPool) {
    let upstream = provider_answering(524).await;
    let key = tenant(&pool, "t-amb-524", &upstream.uri()).await;
    assert_ne!(chat(&pool, "t-amb-524", &key).await, StatusCode::OK);
    assert_eq!(
        upstream.received_requests().await.unwrap().len(),
        1,
        "a 524 may have been generated and billed: no second attempt"
    );
    assert_eq!(
        ambiguous_on_row(&pool, "t-amb-524").await,
        Some(json!(["llm: HTTP 524 after the request was sent"])),
        "the audit row records the unknown outcome"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_overloaded_provider_is_still_retried(pool: PgPool) {
    // Control: 529 is refused before generation, so a retry is safe.
    let upstream = provider_answering(529).await;
    let key = tenant(&pool, "t-amb-529", &upstream.uri()).await;
    let _ = chat(&pool, "t-amb-529", &key).await;
    assert_eq!(upstream.received_requests().await.unwrap().len(), 2);
    assert_eq!(ambiguous_on_row(&pool, "t-amb-529").await, None);
}
