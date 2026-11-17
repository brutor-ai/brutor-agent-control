// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! `store_bodies` applied in the audit writer.
//!
//! The policy is read from `tenants.data_retention` at flush time, so it
//! holds for every surface without per-route wiring. These tests drive the
//! real writer and read back what landed.

use std::time::Duration;

use sqlx::PgPool;
use ulid::Ulid;

use brutor_gateway_core::services::background_writer::BackgroundWriter;
use brutor_gateway_core::services::proxy_logger;

use super::background_writer_chain::test_auth;
use super::common;

async fn write_one(pool: &PgPool, tenant: &str, req: &str, resp: &str) -> String {
    let writer = BackgroundWriter::new();
    let auth = test_auth(tenant);
    let corr = format!("corr-{}", Ulid::new());
    let mut log = proxy_logger::build_proxy_log(
        "llm",
        &auth,
        "POST",
        "/v1/chat/completions",
        None,
        None,
        200,
        3.0,
        true,
        None,
        &corr,
    );
    log.request_body_preview = Some(req.to_string());
    log.response_body_preview = Some(resp.to_string());
    log.request_headers = Some("{\"x-trace\":\"1\"}".into());
    writer.write_proxy_log(pool.clone(), log);
    for _ in 0..40 {
        let row: Option<(String,)> =
            sqlx::query_as("SELECT id FROM proxy_logs WHERE correlation_id = $1")
                .bind(&corr)
                .fetch_optional(pool)
                .await
                .expect("query");
        if row.is_some() {
            return corr;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("row never landed");
}

async fn stored(pool: &PgPool, corr: &str) -> (Option<String>, Option<String>, Option<String>) {
    sqlx::query_as(
        "SELECT request_body_preview, response_body_preview, request_headers \
         FROM proxy_logs WHERE correlation_id = $1",
    )
    .bind(corr)
    .fetch_one(pool)
    .await
    .expect("stored row")
}

async fn set_policy(pool: &PgPool, tenant: &str, mode: &str) {
    sqlx::query("UPDATE tenants SET data_retention = $2::jsonb WHERE id = $1")
        .bind(tenant)
        .bind(serde_json::json!({ "store_bodies": mode }))
        .execute(pool)
        .await
        .expect("policy");
    brutor_gateway_core::services::data_retention::invalidate(tenant);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn no_policy_stores_bodies_in_full(pool: PgPool) {
    let t = "t-ret-full";
    common::seed_tenant(&pool, t).await;
    let corr = write_one(&pool, t, "hello bob@example.com", "answer").await;
    let (req, resp, hdr) = stored(&pool, &corr).await;
    assert_eq!(req.as_deref(), Some("hello bob@example.com"));
    assert_eq!(resp.as_deref(), Some("answer"));
    assert!(hdr.is_some());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn none_stores_no_content_but_keeps_the_record(pool: PgPool) {
    let t = "t-ret-none";
    common::seed_tenant(&pool, t).await;
    set_policy(&pool, t, "none").await;
    let corr = write_one(&pool, t, "secret prompt", "secret answer").await;
    let (req, resp, hdr) = stored(&pool, &corr).await;
    assert!(req.is_none() && resp.is_none() && hdr.is_none());
    let (endpoint, ok): (String, bool) =
        sqlx::query_as("SELECT endpoint, success FROM proxy_logs WHERE correlation_id = $1")
            .bind(&corr)
            .fetch_one(&pool)
            .await
            .expect("record");
    assert_eq!(endpoint, "/v1/chat/completions");
    assert!(ok);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn redacted_scrubs_pii_regardless_of_guardrails(pool: PgPool) {
    let t = "t-ret-redact";
    common::seed_tenant(&pool, t).await;
    set_policy(&pool, t, "redacted").await;
    let corr = write_one(
        &pool,
        t,
        "card 4111-1111-1111-1111, mail bob@example.com",
        "sent to bob@example.com",
    )
    .await;
    let (req, resp, _) = stored(&pool, &corr).await;
    let req = req.unwrap();
    assert!(
        !req.contains("4111") && !req.contains("bob@example.com"),
        "{req}"
    );
    assert!(req.contains("[REDACTED-"), "{req}");
    assert!(!resp.unwrap().contains("bob@example.com"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn none_still_seals_the_content_digests(pool: PgPool) {
    // The policy decides whether content is *stored*; what the content
    // *was* is sealed before the policy runs and stays on the row.
    use sha2::Digest as _;
    let t = "t-ret-digest";
    common::seed_tenant(&pool, t).await;
    set_policy(&pool, t, "none").await;
    let corr = write_one(&pool, t, "secret prompt", "secret answer").await;
    let (req, resp, _) = stored(&pool, &corr).await;
    assert!(req.is_none() && resp.is_none());
    let (h_req, h_resp): (Option<String>, Option<String>) = sqlx::query_as(
        "SELECT request_content_hash, response_content_hash FROM proxy_logs WHERE correlation_id = $1",
    )
    .bind(&corr)
    .fetch_one(&pool)
    .await
    .expect("digests");
    assert_eq!(
        h_req,
        Some(hex::encode(sha2::Sha256::digest(b"secret prompt")))
    );
    assert_eq!(
        h_resp,
        Some(hex::encode(sha2::Sha256::digest(b"secret answer")))
    );
}
