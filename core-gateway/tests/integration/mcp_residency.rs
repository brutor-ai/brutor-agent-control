// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! MCP residency enforcement end-to-end.
//!
//! Drives the full proxy_request path with a real Postgres and
//! `wiremock` upstream to pin the three residency outcomes:
//!
//!   * No profile → request proceeds; audit row has
//!     `residency_class = not_evaluated`.
//!   * Allowed region → request proceeds; audit row has
//!     `residency_class = allowed` AND `region_routed_to = <region>`.
//!   * Disallowed region → 403 region_not_allowed, upstream NOT called,
//!     audit row has `residency_class = blocked`,
//!     `proxy_subtype = residency_violation`.
//!
//! These tests are integration-level on purpose — the unit suite
//! in `services::residency::tests` covers the `check` semantics;
//! this file proves the wire through `routes::mcp::proxy_request`
//! actually calls it AND that audit rows reach the DB with the
//! right shape.

use std::time::Duration;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

async fn body_to_json(body: Body) -> Value {
    let bytes = body.collect().await.expect("collect body").to_bytes();
    serde_json::from_slice(&bytes).unwrap_or(Value::Null)
}

/// Seed the same topology as `mcp_proxy_http.rs::seed_full_topology`
/// — tenant, resource group, user with direct membership, mcp
/// server pointing at `mock_uri`, server_config + binding, api key.
/// Returns the raw API key.
/// `tenant_id` is per-test: the residency profile cache
/// (`services::residency::residency_cache`) is a process-global `OnceLock`
/// keyed by tenant_id that outlives each `#[sqlx::test]` ephemeral DB, so a
/// shared tenant id leaks a cached profile into the next test. A unique id
/// per test isolates it.
async fn seed_topology(pool: &PgPool, tenant_id: &str, mock_uri: &str) -> String {
    common::seed_tenant(pool, tenant_id).await;
    common::seed_resource_group(pool, "rg-res", tenant_id).await;
    common::seed_end_user(pool, 8001, tenant_id).await;
    common::add_direct_membership(pool, 8001, "rg-res", tenant_id).await;
    common::seed_mcp_server_with_base_url(pool, "ms-res-fixture", tenant_id, "fixture", mock_uri)
        .await;
    common::seed_server_config(pool, "sc-res-fixture", tenant_id, "ms-res-fixture").await;
    common::bind_server_config_to_rg(pool, "sc-res-fixture", "rg-res").await;
    common::seed_api_key(pool, tenant_id, "rg-res", Some(8001), "res-key").await
}

/// Issue a JSON-RPC tools/list against the proxy. Returns the
/// HTTP status + decoded JSON body.
async fn issue_tools_list(pool: &PgPool, tenant_id: &str, api_key: &str) -> (StatusCode, Value) {
    let state = common::build_test_state(pool.clone());
    let app = common::build_mcp_proxy_app(state);

    let req = Request::builder()
        .method("POST")
        .uri("/v1/proxy/mcp/ms-res-fixture")
        .header("X-Tenant-ID", tenant_id)
        .header("X-API-Key", api_key)
        .header("Content-Type", "application/json")
        .header("Accept", "application/json")
        .extension(axum::extract::ConnectInfo::<std::net::SocketAddr>(
            "127.0.0.1:0".parse().unwrap(),
        ))
        .body(Body::from(
            json!({"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}).to_string(),
        ))
        .expect("build request");

    let resp = app.oneshot(req).await.expect("oneshot");
    let status = resp.status();
    let body = body_to_json(resp.into_body()).await;
    (status, body)
}

/// Poll proxy_logs for a row matching the tenant; the
/// background_writer is async so the row may not be visible
/// immediately. Returns the row as JSON.
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
    panic!("background_writer never persisted a proxy_logs row for tenant {tenant_id}");
}

// ---------------------------------------------------------------------------
// Allowed region — happy path
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn allowed_region_passes_and_stamps_region_routed_to(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"tools": [{"name": "ok", "description": "ok"}]}
        })))
        .mount(&mock)
        .await;

    let tenant = "tenant-res-mcp-allowed";
    let api_key = seed_topology(&pool, tenant, &mock.uri()).await;
    common::set_mcp_server_region(&pool, "ms-res-fixture", "eu-west-1").await;
    common::set_tenant_residency(
        &pool,
        tenant,
        json!({"allowed_regions": ["eu-west-1"], "block_global": true}),
    )
    .await;

    let (status, _body) = issue_tools_list(&pool, tenant, &api_key).await;
    assert_eq!(
        status,
        StatusCode::OK,
        "allowed-region request should succeed"
    );

    let row = await_audit_row(&pool, tenant).await;
    assert_eq!(row["residency_class"].as_str(), Some("allowed"));
    assert_eq!(row["region_routed_to"].as_str(), Some("eu-west-1"));
    // Block path didn't fire — proxy_subtype is whatever the
    // happy path stamps (e.g. None or method-specific), NOT
    // residency_violation.
    assert_ne!(
        row["proxy_subtype"].as_str(),
        Some("residency_violation"),
        "allowed-region path must not stamp residency_violation"
    );
}

// ---------------------------------------------------------------------------
// Blocked region — 403 + no upstream contact
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn disallowed_region_returns_403_and_skips_upstream(pool: PgPool) {
    let mock = MockServer::start().await;
    // No mock expectation set up — if the proxy hits the upstream
    // anyway, wiremock would respond with a default 404 and the
    // request body assertion below would change shape. The real
    // test is the audit row pinning that the block fired before
    // any forwarding.
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(500))
        .expect(0) // upstream MUST NOT be called
        .mount(&mock)
        .await;

    let tenant = "tenant-res-mcp-disallowed";
    let api_key = seed_topology(&pool, tenant, &mock.uri()).await;
    // Stamp the server in us-east-1 but only allow eu-west-1.
    common::set_mcp_server_region(&pool, "ms-res-fixture", "us-east-1").await;
    common::set_tenant_residency(
        &pool,
        tenant,
        json!({"allowed_regions": ["eu-west-1"], "block_global": true}),
    )
    .await;

    let (status, _body) = issue_tools_list(&pool, tenant, &api_key).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "disallowed-region request must return 403"
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
    // The reason string lives in error_message — pin that the
    // operator-facing dashboard can show "why" without inspecting
    // logs.
    let msg = row["error_message"].as_str().unwrap_or_default();
    assert!(
        msg.contains("us-east-1") || msg.contains("not in tenant's allowed_regions"),
        "error_message should explain the region mismatch, got: {msg}"
    );
}

// ---------------------------------------------------------------------------
// block_global — refuses NULL/global region
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn block_global_refuses_null_region(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(500))
        .expect(0)
        .mount(&mock)
        .await;

    let tenant = "tenant-res-mcp-blockglobal";
    let api_key = seed_topology(&pool, tenant, &mock.uri()).await;
    // Server has NO region set (NULL — default after seed).
    // Tenant has block_global=true → must refuse.
    common::set_tenant_residency(
        &pool,
        tenant,
        json!({"allowed_regions": ["eu-west-1"], "block_global": true}),
    )
    .await;

    let (status, _body) = issue_tools_list(&pool, tenant, &api_key).await;
    assert_eq!(status, StatusCode::FORBIDDEN);

    let row = await_audit_row(&pool, tenant).await;
    assert_eq!(row["residency_class"].as_str(), Some("blocked"));
}

// ---------------------------------------------------------------------------
// No profile — request proceeds, audit class is not_evaluated
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn no_residency_profile_stamps_not_evaluated(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"tools": []}
        })))
        .mount(&mock)
        .await;

    let tenant = "tenant-res-mcp-noprofile";
    let api_key = seed_topology(&pool, tenant, &mock.uri()).await;
    common::set_mcp_server_region(&pool, "ms-res-fixture", "us-east-1").await;
    // Note: NO set_tenant_residency call — data_residency stays NULL.

    let (status, _body) = issue_tools_list(&pool, tenant, &api_key).await;
    assert_eq!(
        status,
        StatusCode::OK,
        "no-profile request must succeed (existing tenants pre-a069 keep working)"
    );

    let row = await_audit_row(&pool, tenant).await;
    assert_eq!(
        row["residency_class"].as_str(),
        Some("not_evaluated"),
        "no-profile rows must distinguish from 'allowed' so the audit dashboard can flag governance gaps"
    );
    // region_routed_to should carry the server's region for
    // visibility, even though no policy ran.
    assert_eq!(row["region_routed_to"].as_str(), Some("us-east-1"));
}
