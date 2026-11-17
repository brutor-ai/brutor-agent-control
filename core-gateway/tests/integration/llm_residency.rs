// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! LLM residency enforcement end-to-end.
//!
//! Mirrors `mcp_residency.rs` for the LLM `/v1/proxy/llm/chat/completions`
//! path. Four scenarios pin the audit-row shapes against a real
//! Postgres + wiremock upstream:
//!
//!   * No profile (NULL data_residency)  → upstream called,
//!     audit row has `residency_class=not_evaluated`.
//!   * Allowed region                    → upstream called,
//!     audit row has `allowed` + `region_routed_to=<region>`.
//!   * Disallowed region                 → 403 region_not_allowed,
//!     upstream NEVER called (wiremock `.expect(0)`),
//!     audit row has `blocked` + `proxy_subtype=residency_violation`.
//!   * block_global + unstamped region   → 403, upstream skipped.
//!
//! These tests prove the LLM route's residency wire — the unit
//! tests in `services::residency` and `services::llm_router`
//! cover the decision logic and fallback skip respectively;
//! this file proves `routes::llm::chat_completions` actually
//! invokes them and stamps the right shapes onto the audit row.

use std::time::Duration;

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

fn openai_chat_completion_fixture() -> Value {
    json!({
        "id": "chatcmpl-residency-fixture",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "test-gpt",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop"
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
    })
}

/// Seed a tenant, RG, user, provider+model+binding, and API key.
/// Returns the raw API key.
///
/// `tenant_id` is per-test: the residency profile cache
/// (`services::residency::residency_cache`) is a process-global `OnceLock`
/// keyed by tenant_id and outlives each `#[sqlx::test]` ephemeral DB, so
/// reusing one tenant id across tests leaks a cached profile into the next
/// test and flips its `residency_class`. A unique id per test isolates it.
async fn seed_topology(pool: &PgPool, tenant_id: &str, mock_uri: &str) -> String {
    common::seed_tenant(pool, tenant_id).await;
    common::seed_resource_group(pool, "rg-res-llm", tenant_id).await;
    common::seed_end_user(pool, 9001, tenant_id).await;
    common::add_direct_membership(pool, 9001, "rg-res-llm", tenant_id).await;
    common::seed_llm_provider(pool, "prov-res", tenant_id, "openai").await;
    common::seed_llm_model_with_api_url(pool, "m-res", tenant_id, "prov-res", "test-gpt", mock_uri)
        .await;
    common::bind_llm_model_to_rg(pool, "m-res", "rg-res-llm").await;
    common::seed_api_key(pool, tenant_id, "rg-res-llm", Some(9001), "res-llm-key").await
}

/// Drive a chat-completion call through the proxy. Returns
/// (status, body) so individual tests can inspect either.
async fn issue_chat_completion(
    pool: &PgPool,
    tenant_id: &str,
    api_key: &str,
) -> (StatusCode, Value) {
    let state = common::build_test_state(pool.clone());
    let app = common::build_llm_app(state);

    let req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({
            "model_id": "m-res",
            "messages": [{"role": "user", "content": "hi"}]
        }),
        Some(api_key),
        Some(tenant_id),
    );

    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    let body = body_to_json(resp.into_body()).await;
    (status, body)
}

/// Background_writer is async — poll proxy_logs until our row
/// shows up. Returns the row as JSON.
async fn await_audit_row(pool: &PgPool, tenant_id: &str) -> Value {
    for _ in 0..40 {
        let row = sqlx::query_as::<_, (sqlx::types::Json<Value>,)>(
            "SELECT to_jsonb(p.*) FROM proxy_logs p \
             WHERE tenant_id = $1 ORDER BY timestamp DESC LIMIT 1",
        )
        .bind(tenant_id)
        .fetch_optional(pool)
        .await
        .ok()
        .flatten();
        if let Some((j,)) = row {
            return j.0;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("background_writer never persisted a proxy_logs row for {tenant_id}");
}

// ---------------------------------------------------------------------------
// Allowed region — happy path
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_allowed_region_passes_and_stamps_region_routed_to(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(openai_chat_completion_fixture()))
        .expect(1) // upstream MUST be called
        .mount(&mock)
        .await;

    let tenant = "tenant-res-llm-allowed";
    let api_key = seed_topology(&pool, tenant, &mock.uri()).await;
    common::set_llm_provider_region(&pool, "prov-res", "eu-west-1").await;
    common::set_tenant_residency(
        &pool,
        tenant,
        json!({"allowed_regions": ["eu-west-1"], "block_global": true}),
    )
    .await;

    let (status, _body) = issue_chat_completion(&pool, tenant, &api_key).await;
    assert_eq!(
        status,
        StatusCode::OK,
        "allowed-region request should succeed"
    );

    let row = await_audit_row(&pool, tenant).await;
    assert_eq!(row["residency_class"].as_str(), Some("allowed"));
    assert_eq!(row["region_routed_to"].as_str(), Some("eu-west-1"));
    assert_ne!(
        row["proxy_subtype"].as_str(),
        Some("residency_violation"),
        "allowed-region path must not stamp residency_violation"
    );
}

// ---------------------------------------------------------------------------
// Blocked region — 403 + upstream not contacted
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_disallowed_region_returns_403_and_skips_upstream(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(500))
        .expect(0) // upstream MUST NOT be called
        .mount(&mock)
        .await;

    let tenant = "tenant-res-llm-disallowed";
    let api_key = seed_topology(&pool, tenant, &mock.uri()).await;
    common::set_llm_provider_region(&pool, "prov-res", "us-east-1").await;
    common::set_tenant_residency(
        &pool,
        tenant,
        json!({"allowed_regions": ["eu-west-1"], "block_global": true}),
    )
    .await;

    let (status, body) = issue_chat_completion(&pool, tenant, &api_key).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "disallowed-region request must return 403"
    );
    // Error envelope mentions the residency block + region.
    let err_str = body.to_string();
    assert!(
        err_str.contains("region_not_allowed") || err_str.contains("us-east-1"),
        "error body should explain the residency mismatch, got {err_str}",
    );

    let row = await_audit_row(&pool, tenant).await;
    assert_eq!(row["residency_class"].as_str(), Some("blocked"));
    assert!(
        row["region_routed_to"].is_null(),
        "blocked rows must NOT stamp region_routed_to"
    );
    assert_eq!(
        row["proxy_subtype"].as_str(),
        Some("residency_violation"),
        "blocked rows must carry proxy_subtype=residency_violation"
    );
    assert_eq!(row["http_status_code"].as_i64(), Some(403));
}

// ---------------------------------------------------------------------------
// block_global — unspecified provider region refused
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_block_global_refuses_unspecified_region(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(500))
        .expect(0)
        .mount(&mock)
        .await;

    let tenant = "tenant-res-llm-blockglobal";
    let api_key = seed_topology(&pool, tenant, &mock.uri()).await;
    // Provider has NO region (NULL — default after seed). Tenant
    // has block_global=true → must refuse.
    common::set_tenant_residency(
        &pool,
        tenant,
        json!({"allowed_regions": ["eu-west-1"], "block_global": true}),
    )
    .await;

    let (status, _body) = issue_chat_completion(&pool, tenant, &api_key).await;
    assert_eq!(status, StatusCode::FORBIDDEN);

    let row = await_audit_row(&pool, tenant).await;
    assert_eq!(row["residency_class"].as_str(), Some("blocked"));
    assert_eq!(row["proxy_subtype"].as_str(), Some("residency_violation"),);
}

// ---------------------------------------------------------------------------
// No profile — request proceeds, audit class is not_evaluated
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_no_residency_profile_stamps_not_evaluated(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(openai_chat_completion_fixture()))
        .expect(1)
        .mount(&mock)
        .await;

    let tenant = "tenant-res-llm-noprofile";
    let api_key = seed_topology(&pool, tenant, &mock.uri()).await;
    common::set_llm_provider_region(&pool, "prov-res", "us-east-1").await;
    // Note: NO set_tenant_residency call — data_residency stays
    // NULL. Existing pre-a069 tenants behave this way.

    let (status, _body) = issue_chat_completion(&pool, tenant, &api_key).await;
    assert_eq!(
        status,
        StatusCode::OK,
        "no-profile request must succeed (pre-a069 tenants keep working)"
    );

    let row = await_audit_row(&pool, tenant).await;
    assert_eq!(
        row["residency_class"].as_str(),
        Some("not_evaluated"),
        "no-profile rows must distinguish from 'allowed' so the dashboard can flag governance gaps",
    );
    // region_routed_to carries the provider's region for
    // visibility even though no policy ran.
    assert_eq!(row["region_routed_to"].as_str(), Some("us-east-1"));
}

// ---------------------------------------------------------------------------
// Every OpenAI-shaped route runs the residency check
// ---------------------------------------------------------------------------
//
// `/chat/completions`, `/responses` and `/embeddings` ran
// `llm_preflight::enforce_residency`; `/completions` reaches the same
// providers with the same keys and did not: a tenant whose profile forbids a
// provider's region had its prompt sent there by changing endpoint.

/// The four routes, with a body each accepts and the provider path it calls.
const RESIDENCY_ROUTES: [(&str, &str, &str); 4] = [
    (
        "/v1/proxy/llm/chat/completions",
        r#"{"model_id":"m-res","messages":[{"role":"user","content":"hi"}]}"#,
        "/chat/completions",
    ),
    (
        "/v1/proxy/llm/responses",
        r#"{"model_id":"m-res","input":"hi"}"#,
        "/responses",
    ),
    (
        "/v1/proxy/llm/completions",
        r#"{"model_id":"m-res","prompt":"hi"}"#,
        "/completions",
    ),
    (
        "/v1/proxy/llm/embeddings",
        r#"{"model_id":"m-res","input":"hi"}"#,
        "/embeddings",
    ),
];

async fn issue(pool: &PgPool, tenant: &str, api_key: &str, route: usize) -> (StatusCode, Value) {
    let (path, body, _) = RESIDENCY_ROUTES[route];
    let app = common::build_llm_app(common::build_test_state(pool.clone()));
    let req = common::build_post_request(
        path,
        serde_json::from_str(body).expect("body"),
        Some(api_key),
        Some(tenant),
    );
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    (status, body_to_json(resp.into_body()).await)
}

/// A provider that answers every one of the four endpoints.
async fn provider_for_every_route() -> MockServer {
    let mock = MockServer::start().await;
    let usage = json!({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2});
    for (_, _, upstream) in RESIDENCY_ROUTES {
        let body = match upstream {
            "/chat/completions" => openai_chat_completion_fixture(),
            "/responses" => json!({
                "id": "resp_res", "object": "response", "created_at": 1_700_000_000,
                "model": "test-gpt", "status": "completed",
                "output": [{"type": "message", "id": "msg_1", "status": "completed",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "ok", "annotations": []}]}],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
            }),
            "/completions" => json!({
                "id": "cmpl-res", "object": "text_completion", "created": 1_700_000_000,
                "model": "test-gpt",
                "choices": [{"index": 0, "text": "ok", "finish_reason": "stop"}],
                "usage": usage
            }),
            _ => json!({
                "object": "list", "model": "test-gpt",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
                "usage": {"prompt_tokens": 1, "total_tokens": 1}
            }),
        };
        Mock::given(method("POST"))
            .and(wm_path(upstream))
            .respond_with(ResponseTemplate::new(200).set_body_json(body))
            .mount(&mock)
            .await;
    }
    mock
}

async fn a_disallowed_region_is_refused_and_the_provider_is_not_called(pool: PgPool, route: usize) {
    let mock = provider_for_every_route().await;
    let tenant = format!("tenant-res-route-blocked-{route}");
    let api_key = seed_topology(&pool, &tenant, &mock.uri()).await;
    common::set_llm_provider_region(&pool, "prov-res", "us-east-1").await;
    common::set_tenant_residency(
        &pool,
        &tenant,
        json!({"allowed_regions": ["eu-west-1"], "block_global": true}),
    )
    .await;

    let (status, body) = issue(&pool, &tenant, &api_key, route).await;
    let path = RESIDENCY_ROUTES[route].0;
    assert_eq!(
        mock.received_requests().await.unwrap_or_default().len(),
        0,
        "{path}: the prompt was sent to a region the tenant forbids ({status}: {body})"
    );
    assert_eq!(status, StatusCode::FORBIDDEN, "{path}: {body}");
    assert!(
        body.to_string().contains("region_not_allowed"),
        "{path}: {body}"
    );
    let row = await_audit_row(&pool, &tenant).await;
    assert_eq!(row["residency_class"].as_str(), Some("blocked"), "{path}");
    assert_eq!(
        row["proxy_subtype"].as_str(),
        Some("residency_violation"),
        "{path}"
    );
    assert_eq!(row["http_status_code"].as_i64(), Some(403), "{path}");
    assert!(row["region_routed_to"].is_null(), "{path}");
}

/// The control: an allowed region is served on every route, and the row
/// records where it went.
async fn an_allowed_region_is_served_and_recorded(pool: PgPool, route: usize) {
    let mock = provider_for_every_route().await;
    let tenant = format!("tenant-res-route-allowed-{route}");
    let api_key = seed_topology(&pool, &tenant, &mock.uri()).await;
    common::set_llm_provider_region(&pool, "prov-res", "eu-west-1").await;
    common::set_tenant_residency(
        &pool,
        &tenant,
        json!({"allowed_regions": ["eu-west-1"], "block_global": true}),
    )
    .await;

    let (status, body) = issue(&pool, &tenant, &api_key, route).await;
    let path = RESIDENCY_ROUTES[route].0;
    assert_eq!(status, StatusCode::OK, "{path}: {body}");
    assert_eq!(mock.received_requests().await.unwrap_or_default().len(), 1);
    // `/completions` writes no `proxy_logs` row for a call it serves (only
    // its usage row), so there is no row to carry the
    // decision. Its refusal row is asserted above like the others'.
    if path.ends_with("/llm/completions") {
        return;
    }
    let row = await_audit_row(&pool, &tenant).await;
    assert_eq!(row["residency_class"].as_str(), Some("allowed"), "{path}");
    assert_eq!(
        row["region_routed_to"].as_str(),
        Some("eu-west-1"),
        "{path}"
    );
}

macro_rules! per_residency_route {
    ($($case:ident),+ $(,)?) => {$(
        mod $case {
            use super::*;
            #[sqlx::test(migrations = "./tests/migrations")]
            async fn chat(pool: PgPool) { super::$case(pool, 0).await; }
            #[sqlx::test(migrations = "./tests/migrations")]
            async fn responses(pool: PgPool) { super::$case(pool, 1).await; }
            #[sqlx::test(migrations = "./tests/migrations")]
            async fn completions(pool: PgPool) { super::$case(pool, 2).await; }
            #[sqlx::test(migrations = "./tests/migrations")]
            async fn embeddings(pool: PgPool) { super::$case(pool, 3).await; }
        }
    )+};
}

per_residency_route!(
    a_disallowed_region_is_refused_and_the_provider_is_not_called,
    an_allowed_region_is_served_and_recorded,
);
