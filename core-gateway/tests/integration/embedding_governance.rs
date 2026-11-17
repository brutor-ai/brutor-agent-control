// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Embedding governance enforcement (media mode `embedding`).
//!
//! Regression guard: `enforce_embedding` (dim whitelist / max batch size)
//! existed and was unit-tested but was never wired into the `/embeddings`
//! route, so an operator's embedding policy was silently ignored at runtime.
//! It is now enforced in `routes::llm::embeddings` the same way image/audio/
//! video routes enforce their per-mode media governance. These tests drive a
//! real request and assert the block (and, conversely, that a compliant
//! request passes governance).

use axum::body::Body;
use axum::http::StatusCode;
use serde_json::json;
use sqlx::PgPool;
use tower::ServiceExt;

use crate::common;

async fn body_to_string(body: Body) -> String {
    let bytes = axum::body::to_bytes(body, usize::MAX).await.unwrap();
    String::from_utf8_lossy(&bytes).to_string()
}

async fn seed_embedding_model(pool: &PgPool, tenant: &str, rg_id: &str, model_id: &str) {
    common::seed_llm_provider(pool, "prov-embed", tenant, "openai").await;
    common::seed_llm_model_with_api_url(
        pool,
        model_id,
        tenant,
        "prov-embed",
        "text-embedding-3-small",
        "http://unused.invalid", // governance block precedes dispatch; never called
    )
    .await;
    common::set_model_mode(pool, model_id, "embedding").await;
    common::set_model_api_key(pool, model_id, "sk-test").await;
    common::bind_llm_model_to_rg(pool, model_id, rg_id).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn embedding_dim_whitelist_blocks_non_whitelisted(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-eg").await;
    common::seed_resource_group(&pool, "rg-eg", "tenant-eg").await;
    common::seed_end_user(&pool, 9401, "tenant-eg").await;
    common::add_direct_membership(&pool, 9401, "rg-eg", "tenant-eg").await;
    seed_embedding_model(&pool, "tenant-eg", "rg-eg", "m-eg").await;

    // Only 1536-dim embeddings are allowed for this group.
    common::set_rg_media_governance(
        &pool,
        "rg-eg",
        json!({"embedding": {"dim_whitelist": [1536]}}),
    )
    .await;

    let jwt = common::seed_api_key(&pool, "tenant-eg", "rg-eg", Some(9401), "caller").await;
    let app = common::build_llm_app(common::build_test_state(pool.clone()));
    let req = common::build_post_request(
        "/v1/proxy/llm/embeddings",
        json!({"model": "m-eg", "input": "hello", "dimensions": 768}),
        Some(&jwt),
        Some("tenant-eg"),
    );
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    let body = body_to_string(resp.into_body()).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "dim 768 not in whitelist [1536] must be blocked; got {status}, body={body}"
    );
    assert!(
        body.contains("policy"),
        "expected a policy error, body={body}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn embedding_max_batch_size_blocks_oversized(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-eb").await;
    common::seed_resource_group(&pool, "rg-eb", "tenant-eb").await;
    common::seed_end_user(&pool, 9402, "tenant-eb").await;
    common::add_direct_membership(&pool, 9402, "rg-eb", "tenant-eb").await;
    seed_embedding_model(&pool, "tenant-eb", "rg-eb", "m-eb").await;

    // Batches larger than 2 inputs are rejected.
    common::set_rg_media_governance(&pool, "rg-eb", json!({"embedding": {"max_batch_size": 2}}))
        .await;

    let jwt = common::seed_api_key(&pool, "tenant-eb", "rg-eb", Some(9402), "caller").await;
    let app = common::build_llm_app(common::build_test_state(pool.clone()));
    let req = common::build_post_request(
        "/v1/proxy/llm/embeddings",
        json!({"model": "m-eb", "input": ["a", "b", "c"]}), // batch of 3 > max 2
        Some(&jwt),
        Some("tenant-eb"),
    );
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    let body = body_to_string(resp.into_body()).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "batch size 3 > max 2 must be blocked; got {status}, body={body}"
    );
    assert!(
        body.contains("policy"),
        "expected a policy error, body={body}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn embedding_compliant_request_passes_governance(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-ep").await;
    common::seed_resource_group(&pool, "rg-ep", "tenant-ep").await;
    common::seed_end_user(&pool, 9403, "tenant-ep").await;
    common::add_direct_membership(&pool, 9403, "rg-ep", "tenant-ep").await;
    seed_embedding_model(&pool, "tenant-ep", "rg-ep", "m-ep").await;

    // Whitelist the dim the request actually asks for, and allow the batch.
    common::set_rg_media_governance(
        &pool,
        "rg-ep",
        json!({"embedding": {"dim_whitelist": [768], "max_batch_size": 4}}),
    )
    .await;

    let jwt = common::seed_api_key(&pool, "tenant-ep", "rg-ep", Some(9403), "caller").await;
    let app = common::build_llm_app(common::build_test_state(pool.clone()));
    let req = common::build_post_request(
        "/v1/proxy/llm/embeddings",
        json!({"model": "m-ep", "input": ["a", "b"], "dimensions": 768}),
        Some(&jwt),
        Some("tenant-ep"),
    );
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    let body = body_to_string(resp.into_body()).await;
    // Governance passes → the request proceeds to the (unreachable) upstream and
    // fails there. The one thing it must NOT be is a governance 403 policy block.
    assert_ne!(
        status,
        StatusCode::FORBIDDEN,
        "a compliant embedding request must pass governance (no false-positive block); got {status}, body={body}"
    );
}
