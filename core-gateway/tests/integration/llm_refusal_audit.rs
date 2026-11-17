// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Governed LLM refusals leave evidence (`services::llm_refusal`).
//!
//! These refusals used to return their status and write NOTHING: the
//! embeddings governance block (dimension whitelist, batch size), the LLM
//! parameter-governance block on chat, and the media-governance and
//! media-quota refusals. Each test drives the real route and proves:
//!
//! * the refusal wrote its `proxy_logs` row, with the house subtype
//!   (`llm_governance_block` / `rate_limit_media_quota`);
//! * the row lands on the run the request belongs to (the same
//!   `root_task_id` as an allowed call of that run);
//! * the provider was never called (the wiremock upstream saw no request
//!   from the refused call);
//! * the evidence writer sealed it as a `denied` record.
//!
//! The static guard `tests/llm_refusal_guard.rs` keeps the class closed.

use std::time::Duration;

use axum::http::{HeaderValue, StatusCode};
use serde_json::json;
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::constants::proxy_subtype;

use crate::common;

pub(crate) const SEED: [u8; 32] = [0x42; 32];

/// The same process-global values `evidence_runtime` / `evidence_records`
/// set, so the modules agree whatever order the harness runs them in.
pub(crate) fn enable_evidence() {
    unsafe {
        std::env::set_var("BRUTOR_EVIDENCE_SIGNING_KEY", hex::encode(SEED));
        std::env::set_var("BRUTOR_EVIDENCE_HEAD_CADENCE_ENTRIES", "2");
        std::env::set_var("BRUTOR_EVIDENCE_WITNESS_ENABLED", "off");
    }
    brutor_gateway_core::env_vars::reload_for_tests();
}

/// `(status, subtype, root_task_id, current_task_id, resource_group_id,
/// record_id, endpoint)` of a tenant's row matching `filter`.
pub(crate) type Row = (
    Option<i32>,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
    String,
);

pub(crate) async fn row(pool: &PgPool, tenant: &str, filter: &str) -> Row {
    let sql = format!(
        "SELECT http_status_code, proxy_subtype, root_task_id, current_task_id, \
                resource_group_id, record_id, endpoint \
           FROM proxy_logs WHERE tenant_id = $1 AND {filter} \
          ORDER BY timestamp DESC LIMIT 1"
    );
    for _ in 0..120 {
        if let Some(r) = sqlx::query_as::<_, Row>(&sql)
            .bind(tenant)
            .fetch_optional(pool)
            .await
            .expect("proxy_logs query")
        {
            return r;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("no proxy_logs row for {tenant} matching {filter}");
}

/// The verdict of the evidence record a row was sealed as.
pub(crate) async fn verdict(pool: &PgPool, record_id: &str) -> String {
    for _ in 0..120 {
        if let Some((v,)) = sqlx::query_as::<_, (String,)>(
            "SELECT verdict FROM action_records WHERE record_id = $1",
        )
        .bind(record_id)
        .fetch_optional(pool)
        .await
        .expect("action_records query")
        {
            return v;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("no action record {record_id}");
}

/// Poll until the refusal row has its record id (sealing is asynchronous).
pub(crate) async fn sealed_row(pool: &PgPool, tenant: &str, filter: &str) -> Row {
    for _ in 0..120 {
        let r = row(pool, tenant, filter).await;
        if r.5.is_some() {
            return r;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("refusal row for {tenant} was never sealed");
}

pub(crate) async fn embeddings_upstream() -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/embeddings"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "object": "list",
            "data": [{"object": "embedding", "embedding": [0.1, 0.2], "index": 0}],
            "model": "text-embedding-3-small",
            "usage": {"prompt_tokens": 1, "total_tokens": 1}
        })))
        .mount(&mock)
        .await;
    mock
}

pub(crate) async fn embeddings_topology(
    pool: &PgPool,
    t: &str,
    rg: &str,
    user: i32,
    upstream: &str,
) {
    common::seed_tenant(pool, t).await;
    common::seed_resource_group(pool, rg, t).await;
    common::seed_end_user(pool, user, t).await;
    common::add_direct_membership(pool, user, rg, t).await;
    common::seed_llm_provider(pool, "prov-ref", t, "openai").await;
    common::seed_llm_model_with_api_url(
        pool,
        "m-ref-emb",
        t,
        "prov-ref",
        "text-embedding-3-small",
        upstream,
    )
    .await;
    common::set_model_mode(pool, "m-ref-emb", "embedding").await;
    common::set_model_api_key(pool, "m-ref-emb", "sk-test").await;
    common::bind_llm_model_to_rg(pool, "m-ref-emb", rg).await;
}

pub(crate) fn in_run(
    mut req: axum::http::Request<axum::body::Body>,
    run: &str,
) -> axum::http::Request<axum::body::Body> {
    req.headers_mut().insert(
        brutor_gateway_core::constants::headers::X_BRUTOR_RUN_ID,
        HeaderValue::from_str(run).expect("run id header"),
    );
    req
}

pub(crate) async fn upstream_calls(mock: &MockServer) -> usize {
    mock.received_requests().await.unwrap_or_default().len()
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_embeddings_dimension_refusal_is_recorded_on_its_run_and_never_reaches_the_provider(
    pool: PgPool,
) {
    enable_evidence();
    let t = "t-ref-dim";
    let mock = embeddings_upstream().await;
    embeddings_topology(&pool, t, "rg-ref-dim", 9811, &mock.uri()).await;
    common::set_rg_media_governance(
        &pool,
        "rg-ref-dim",
        json!({"embedding": {"dim_whitelist": [1536]}}),
    )
    .await;
    let jwt = common::seed_api_key(&pool, t, "rg-ref-dim", Some(9811), "caller").await;
    let app = common::build_llm_app(common::build_test_state(pool.clone()));

    // An allowed call of run R first: it reaches the provider and names R.
    let ok = app
        .clone()
        .oneshot(in_run(
            common::build_post_request(
                "/v1/proxy/llm/embeddings",
                json!({"model": "m-ref-emb", "input": "hello", "dimensions": 1536}),
                Some(&jwt),
                Some(t),
            ),
            "run-emb-1",
        ))
        .await
        .expect("router");
    assert_eq!(ok.status(), StatusCode::OK);
    let allowed = row(&pool, t, "http_status_code = 200").await;
    assert_eq!(upstream_calls(&mock).await, 1);

    // The same run, now asking for a dimension the policy forbids.
    let resp = app
        .oneshot(in_run(
            common::build_post_request(
                "/v1/proxy/llm/embeddings",
                json!({"model": "m-ref-emb", "input": "hello", "dimensions": 768}),
                Some(&jwt),
                Some(t),
            ),
            "run-emb-1",
        ))
        .await
        .expect("router");
    assert_eq!(resp.status(), StatusCode::FORBIDDEN);

    let (status, subtype, root, current, group, record_id, endpoint) =
        sealed_row(&pool, t, "http_status_code = 403").await;
    assert_eq!(status, Some(403));
    assert_eq!(
        subtype.as_deref(),
        Some(proxy_subtype::LLM_GOVERNANCE_BLOCK)
    );
    assert_eq!(endpoint, "/embeddings");
    assert_eq!(group.as_deref(), Some("rg-ref-dim"));
    assert!(root.is_some(), "the refusal must name its run");
    assert_eq!(root, allowed.2, "the refusal lands on the run it stopped");
    assert!(current.is_some() && current != allowed.3, "its own step");
    assert_eq!(
        verdict(&pool, record_id.as_deref().expect("sealed")).await,
        "denied",
        "sealed as a denial, like every other refusal"
    );
    assert_eq!(
        upstream_calls(&mock).await,
        1,
        "the refused call must never reach the provider"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_embeddings_batch_refusal_is_recorded_and_never_reaches_the_provider(pool: PgPool) {
    let t = "t-ref-batch";
    let mock = embeddings_upstream().await;
    embeddings_topology(&pool, t, "rg-ref-batch", 9812, &mock.uri()).await;
    common::set_rg_media_governance(
        &pool,
        "rg-ref-batch",
        json!({"embedding": {"max_batch_size": 2}}),
    )
    .await;
    let jwt = common::seed_api_key(&pool, t, "rg-ref-batch", Some(9812), "caller").await;
    let app = common::build_llm_app(common::build_test_state(pool.clone()));
    let resp = app
        .oneshot(common::build_post_request(
            "/v1/proxy/llm/embeddings",
            json!({"model": "m-ref-emb", "input": ["a", "b", "c"]}),
            Some(&jwt),
            Some(t),
        ))
        .await
        .expect("router");
    assert_eq!(resp.status(), StatusCode::FORBIDDEN);
    let (status, subtype, root, current, group, _, _) = row(&pool, t, "true").await;
    assert_eq!(status, Some(403));
    assert_eq!(
        subtype.as_deref(),
        Some(proxy_subtype::LLM_GOVERNANCE_BLOCK)
    );
    assert_eq!(group.as_deref(), Some("rg-ref-batch"));
    assert!(root.is_some() && current.is_some(), "run and step ids");
    assert_eq!(
        upstream_calls(&mock).await,
        0,
        "the provider was never called"
    );
    // Exactly one row: the refusal, no duplicate.
    tokio::time::sleep(Duration::from_millis(400)).await;
    assert_eq!(common::count_proxy_logs(&pool, t).await, 1);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_chat_parameter_governance_refusal_is_recorded(pool: PgPool) {
    let t = "t-ref-chat";
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "c", "object": "chat.completion", "created": 1, "model": "gpt-4o",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        })))
        .mount(&mock)
        .await;
    common::seed_tenant(&pool, t).await;
    common::seed_resource_group(&pool, "rg-ref-chat", t).await;
    common::seed_end_user(&pool, 9813, t).await;
    common::add_direct_membership(&pool, 9813, "rg-ref-chat", t).await;
    common::seed_llm_provider(&pool, "prov-ref-chat", t, "openai").await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-ref-chat",
        t,
        "prov-ref-chat",
        "gpt-4o",
        &mock.uri(),
    )
    .await;
    common::set_model_api_key(&pool, "m-ref-chat", "sk-test").await;
    common::bind_llm_model_to_rg(&pool, "m-ref-chat", "rg-ref-chat").await;
    common::set_rg_llm_governance(
        &pool,
        "rg-ref-chat",
        json!({"temperature": {"max": 0.5, "enforce": "reject"}}),
    )
    .await;
    let jwt = common::seed_api_key(&pool, t, "rg-ref-chat", Some(9813), "caller").await;
    let app = common::build_llm_app(common::build_test_state(pool.clone()));
    let resp = app
        .oneshot(common::build_post_request(
            "/v1/proxy/llm/chat/completions",
            json!({"model": "m-ref-chat", "temperature": 0.9,
                   "messages": [{"role": "user", "content": "hello"}]}),
            Some(&jwt),
            Some(t),
        ))
        .await
        .expect("router");
    assert_eq!(resp.status(), StatusCode::FORBIDDEN);
    let (status, subtype, root, _, group, _, endpoint) = row(&pool, t, "true").await;
    assert_eq!(status, Some(403));
    assert_eq!(
        subtype.as_deref(),
        Some(proxy_subtype::LLM_GOVERNANCE_BLOCK)
    );
    assert_eq!(endpoint, "/chat/completions");
    assert_eq!(group.as_deref(), Some("rg-ref-chat"));
    assert!(root.is_some());
    assert_eq!(upstream_calls(&mock).await, 0);
}

// ---------------------------------------------------------------------------
// The media surface, called with an API key. The
// Portal-session variants are the Enterprise suite's
// `portal_llm_refusal_audit.rs`.
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_keys_media_governance_refusal_is_recorded_on_its_run(pool: PgPool) {
    let t = "t-ref-tts";
    speech_topology(&pool, t, "rg-ref-tts", 9814).await;
    common::set_rg_media_governance(
        &pool,
        "rg-ref-tts",
        json!({"audio_speech": {"voice_whitelist": ["alloy"]}}),
    )
    .await;
    let key = common::seed_api_key(&pool, t, "rg-ref-tts", None, "k-ref-tts").await;
    let app = common::build_multimodal_app(common::build_test_state(pool.clone()));
    let resp = app
        .oneshot(common::build_post_request(
            "/v1/proxy/llm/audio/speech",
            json!({"model": "m-ref-tts", "voice": "nova", "input": "hello"}),
            Some(&key),
            Some(t),
        ))
        .await
        .expect("router");
    assert_eq!(resp.status(), StatusCode::FORBIDDEN);
    let (status, subtype, root, current, group, _, endpoint) = row(&pool, t, "true").await;
    assert_eq!(status, Some(403));
    assert_eq!(
        subtype.as_deref(),
        Some(proxy_subtype::LLM_GOVERNANCE_BLOCK)
    );
    assert_eq!(endpoint, "/v1/proxy/llm/audio/speech");
    assert_eq!(group.as_deref(), Some("rg-ref-tts"));
    assert!(
        root.is_some() && current.is_some(),
        "the media surface names its run now"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_keys_media_quota_refusal_is_recorded(pool: PgPool) {
    let t = "t-ref-mq";
    speech_topology(&pool, t, "rg-ref-mq", 9815).await;
    common::set_rg_media_limits(
        &pool,
        "rg-ref-mq",
        json!({"audio_speech": {"daily_requests": 1}}),
    )
    .await;
    common::seed_media_proxy_log(&pool, t, "rg-ref-mq", 10, 0.001).await;
    let before = common::count_proxy_logs(&pool, t).await;
    let key = common::seed_api_key(&pool, t, "rg-ref-mq", None, "k-ref-mq").await;
    let app = common::build_multimodal_app(common::build_test_state(pool.clone()));
    let resp = app
        .oneshot(common::build_post_request(
            "/v1/proxy/llm/audio/speech",
            json!({"model": "m-ref-tts", "voice": "alloy", "input": "hello"}),
            Some(&key),
            Some(t),
        ))
        .await
        .expect("router");
    assert_eq!(resp.status(), StatusCode::TOO_MANY_REQUESTS);
    let (status, subtype, root, _, group, _, _) =
        row(&pool, t, "proxy_subtype LIKE 'rate_limit%'").await;
    assert_eq!(status, Some(429));
    assert_eq!(subtype.as_deref(), Some("rate_limit_media_quota"));
    assert_eq!(group.as_deref(), Some("rg-ref-mq"));
    assert!(root.is_some());
    tokio::time::sleep(Duration::from_millis(400)).await;
    assert_eq!(common::count_proxy_logs(&pool, t).await, before + 1);
}

/// A speech model bound to `rg`, and an end user who is a member of it (the
/// Enterprise suite's Portal-session variants sign in as that user).
pub(crate) async fn speech_topology(pool: &PgPool, t: &str, rg: &str, user: i32) {
    common::seed_tenant(pool, t).await;
    common::seed_resource_group(pool, rg, t).await;
    common::seed_end_user(pool, user, t).await;
    common::add_direct_membership(pool, user, rg, t).await;
    common::seed_llm_provider(pool, "prov-ref-tts", t, "openai").await;
    common::seed_llm_model_with_api_url(
        pool,
        "m-ref-tts",
        t,
        "prov-ref-tts",
        "tts-1",
        "http://unused.invalid",
    )
    .await;
    common::set_model_mode(pool, "m-ref-tts", "audio_speech").await;
    common::set_model_api_key(pool, "m-ref-tts", "sk-test").await;
    common::bind_llm_model_to_rg(pool, "m-ref-tts", rg).await;
}
