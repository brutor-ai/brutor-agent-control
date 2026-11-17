// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Route Integration Tests
//!
//! These tests exercise real HTTP routes against a real Postgres: the
//! database named by `DATABASE_URL`, else the throwaway `brutor_core_test`
//! (never the live `ai_gateway`; see `test_helpers::test_database_url`).
//! `test_helpers::ensure_test_schema` creates the tables they need.
//!
//! Run with: `cargo test --test route_tests`

mod test_helpers;

use test_helpers::spawn_app;

// ===========================================================================
// Gateway routes — /health, /healthz, /v1/version
// ===========================================================================

#[tokio::test]
async fn health_returns_200_with_status_json() {
    let app = spawn_app().await;
    let resp = app.get("/health").await;

    assert_eq!(resp.status(), 200);
    let json: serde_json::Value = resp.json().await.unwrap();
    assert!(
        json["status"].as_str().is_some(),
        "health response must contain a 'status' field"
    );
    assert_eq!(json["service"], "brutor-gateway-core");
}

#[tokio::test]
async fn healthz_returns_same_as_health() {
    let app = spawn_app().await;
    let resp = app.get("/healthz").await;

    assert_eq!(resp.status(), 200);
    let json: serde_json::Value = resp.json().await.unwrap();
    assert_eq!(json["service"], "brutor-gateway-core");
}

#[tokio::test]
async fn health_reports_database_up() {
    let app = spawn_app().await;
    let resp = app.get("/health").await;

    let json: serde_json::Value = resp.json().await.unwrap();
    assert_eq!(json["components"]["database"], "up");
}

#[tokio::test]
async fn health_reports_redis_not_configured() {
    // Tests run without Redis
    let app = spawn_app().await;
    let resp = app.get("/health").await;

    let json: serde_json::Value = resp.json().await.unwrap();
    assert_eq!(json["components"]["redis"], "not_configured");
}

#[tokio::test]
async fn health_includes_stats_section() {
    let app = spawn_app().await;
    let resp = app.get("/health").await;

    let json: serde_json::Value = resp.json().await.unwrap();
    assert!(
        json["stats"].is_object(),
        "health response must include a 'stats' object"
    );
    assert!(
        json["stats"]["background_writes"].is_object(),
        "stats must include background_writes"
    );
}

#[tokio::test]
async fn version_returns_200_via_full_app() {
    let app = spawn_app().await;
    let resp = app.get("/v1/version").await;

    assert_eq!(resp.status(), 200);
    let json: serde_json::Value = resp.json().await.unwrap();
    assert_eq!(json["service"], "brutor-gateway-core");
    assert_eq!(json["runtime"], "rust/tokio");
    assert_eq!(json["edition"], "community");
}

// ===========================================================================
// Method validation
// ===========================================================================

#[tokio::test]
async fn put_to_health_returns_405() {
    let app = spawn_app().await;
    let resp = app.request(reqwest::Method::PUT, "/health").await;

    assert_eq!(resp.status(), 405);
}

#[tokio::test]
async fn delete_to_version_returns_405_via_full_app() {
    let app = spawn_app().await;
    let resp = app.request(reqwest::Method::DELETE, "/v1/version").await;

    assert_eq!(resp.status(), 405);
}

#[tokio::test]
async fn post_to_health_returns_405() {
    let app = spawn_app().await;
    let resp = app.request(reqwest::Method::POST, "/health").await;

    assert_eq!(resp.status(), 405);
}

// ===========================================================================
// Unknown routes
// ===========================================================================

/// Was `unknown_route_returns_404`. A path nobody registered with the tenant
/// layer gets its most restrictive rule: a request that names
/// no tenant is refused before the router is asked, so it no longer learns
/// from a 404 that the path does not exist. With a tenant the router's 404
/// answers as before: `tests/integration/tenant_resolution_matrix.rs`, the
/// `(no route)` rows.
#[tokio::test]
async fn unknown_route_without_a_tenant_is_refused_by_the_tenant_layer() {
    let app = spawn_app().await;
    let resp = app.get("/v1/does-not-exist").await;

    assert_eq!(resp.status(), 401);
    assert!(resp.headers().contains_key("www-authenticate"));
}

// ===========================================================================
// Icon routes — /v1/assets/mcp-servers/{server_id}/icon
// ===========================================================================

#[tokio::test]
async fn icon_nonexistent_server_returns_404() {
    let app = spawn_app().await;
    let resp = app
        .get("/v1/assets/mcp-servers/nonexistent-server-id-99999/icon")
        .await;

    assert_eq!(resp.status(), 404);
}

#[tokio::test]
async fn icon_with_existing_server_returns_200_or_404() {
    // Query the DB for a server that has icon_data.
    // If none exists, the test gracefully passes (no icon data seeded).
    let app = spawn_app().await;

    let row: Option<(String,)> =
        sqlx::query_as("SELECT id FROM mcp_servers WHERE icon_data IS NOT NULL LIMIT 1")
            .fetch_optional(&app.state.db)
            .await
            .unwrap();

    if let Some((server_id,)) = row {
        let resp = app
            .get(&format!("/v1/assets/mcp-servers/{}/icon", server_id))
            .await;

        assert_eq!(resp.status(), 200);

        // Verify caching headers
        let cache_control = resp
            .headers()
            .get("cache-control")
            .unwrap()
            .to_str()
            .unwrap();
        assert!(
            cache_control.contains("max-age=86400"),
            "Expected cache-control with max-age=86400, got: {cache_control}"
        );

        let etag = resp.headers().get("etag");
        assert!(etag.is_some(), "Icon response must include an ETag header");

        let content_type = resp
            .headers()
            .get("content-type")
            .unwrap()
            .to_str()
            .unwrap();
        assert!(
            content_type.starts_with("image/"),
            "Expected image content-type, got: {content_type}"
        );
    }
    // If no server with icon data exists, test passes silently
}

#[tokio::test]
async fn icon_etag_returns_304_not_modified() {
    let app = spawn_app().await;

    let row: Option<(String,)> =
        sqlx::query_as("SELECT id FROM mcp_servers WHERE icon_data IS NOT NULL LIMIT 1")
            .fetch_optional(&app.state.db)
            .await
            .unwrap();

    if let Some((server_id,)) = row {
        let path = format!("/v1/assets/mcp-servers/{}/icon", server_id);

        // First request to get the ETag
        let resp1 = app.get(&path).await;
        assert_eq!(resp1.status(), 200);
        let etag = resp1
            .headers()
            .get("etag")
            .unwrap()
            .to_str()
            .unwrap()
            .to_string();

        // Second request with If-None-Match
        let resp2 = app
            .get_with_headers(&path, vec![("if-none-match", &etag)])
            .await;

        assert_eq!(
            resp2.status(),
            304,
            "Expected 304 Not Modified when ETag matches"
        );
    }
}

// ===========================================================================
// Proxy routes — auth required (tenant context via X-Tenant-ID or JWT)
// ===========================================================================

#[tokio::test]
async fn llm_chat_completions_without_auth_returns_401() {
    // Proxy routes require auth before tenant context. The auth check
    // (with WWW-Authenticate on its 401) is ahead of the
    // tenant check, so unauthenticated requests now bounce at 401 with
    // a WWW-Authenticate header rather than 400 "missing tenant".
    let app = spawn_app().await;
    let resp = app
        .post_json(
            "/v1/proxy/llm/chat/completions",
            &serde_json::json!({"messages": []}),
        )
        .await;

    assert_eq!(resp.status(), 401);
}

#[tokio::test]
async fn mcp_proxy_without_auth_returns_401() {
    let app = spawn_app().await;
    let resp = app
        .post_json("/v1/proxy/mcp/some-server-id", &serde_json::json!({}))
        .await;

    assert_eq!(resp.status(), 401);
}

#[tokio::test]
async fn skills_execute_without_auth_returns_401() {
    let app = spawn_app().await;
    let resp = app
        .post_json("/v1/proxy/skills/execute", &serde_json::json!({}))
        .await;

    assert_eq!(resp.status(), 401);
}

// ===========================================================================
// Portal auth routes
// ===========================================================================

// ===========================================================================
// Metrics endpoint
// ===========================================================================

#[tokio::test]
async fn metrics_returns_404_when_not_enabled() {
    // Test state is created without Prometheus, so /metrics should return 404
    let app = spawn_app().await;
    let resp = app.get("/metrics").await;

    assert_eq!(resp.status(), 404);
}

// ===========================================================================
// CORS headers on full app
// ===========================================================================

#[tokio::test]
async fn cors_headers_present_on_health() {
    let app = spawn_app().await;

    let resp = app
        .get_with_headers("/health", vec![("origin", "https://example.com")])
        .await;

    assert_eq!(resp.status(), 200);
    let acao = resp
        .headers()
        .get("access-control-allow-origin")
        .expect("CORS allow-origin header must be present")
        .to_str()
        .unwrap();
    assert_eq!(acao, "*");
}

#[tokio::test]
async fn cors_preflight_on_full_app() {
    let app = spawn_app().await;

    let resp = app
        .client
        .request(
            reqwest::Method::OPTIONS,
            format!("{}/v1/version", app.base_url),
        )
        .header("origin", "https://example.com")
        .header("access-control-request-method", "GET")
        .send()
        .await
        .unwrap();

    assert_eq!(resp.status(), 200);
}

// ===========================================================================
// Proxy routes with tenant header but no valid API key / JWT
// ===========================================================================

#[tokio::test]
async fn llm_with_tenant_header_but_no_api_key_returns_401() {
    let app = spawn_app().await;

    let row: Option<(String,)> = sqlx::query_as("SELECT id FROM tenants LIMIT 1")
        .fetch_optional(&app.state.db)
        .await
        .unwrap();

    if let Some((tenant_id,)) = row {
        let resp = app
            .post_json_with_headers(
                "/v1/proxy/llm/chat/completions",
                vec![("x-tenant-id", &tenant_id)],
                &serde_json::json!({"messages": [{"role": "user", "content": "hi"}], "model": "gpt-4o"}),
            )
            .await;

        // Tenant resolved, but no valid auth credential -> 401
        assert_eq!(resp.status(), 401);
    }
}

#[tokio::test]
async fn skills_with_tenant_header_but_no_api_key_returns_401() {
    let app = spawn_app().await;

    let row: Option<(String,)> = sqlx::query_as("SELECT id FROM tenants LIMIT 1")
        .fetch_optional(&app.state.db)
        .await
        .unwrap();

    if let Some((tenant_id,)) = row {
        let resp = app
            .post_json_with_headers(
                "/v1/proxy/skills/execute",
                vec![("x-tenant-id", &tenant_id)],
                &serde_json::json!({"skill_id": "test"}),
            )
            .await;

        assert_eq!(resp.status(), 401);
    }
}

// ===========================================================================
// Portal alerts routes
// ===========================================================================

// ===========================================================================
// Batch job details with nonexistent ID
// ===========================================================================

/// The default database name of an installation
/// is `brutor` (the Community package creates it); developer machines keep
/// an `ai_gateway`. The test-safety guard refuses both, and a database
/// whose name only starts with one of them is a test database.
#[test]
fn the_test_database_guard_refuses_both_live_names() {
    for (url, live) in [
        ("postgresql://u:p@localhost:5432/ai_gateway", true),
        ("postgresql://u:p@localhost:5432/brutor", true),
        (
            "postgresql://u:p@localhost:5432/brutor?sslmode=disable",
            true,
        ),
        (
            "postgresql://u:p@localhost:5432/ai_gateway?sslmode=disable",
            true,
        ),
        ("postgresql://u:p@localhost:5432/brutor_core_test", false),
        ("postgresql://u:p@localhost:5432/brutor_test", false),
        ("postgresql://brutor:p@localhost:5432/postgres", false),
    ] {
        assert_eq!(test_helpers::names_a_live_database(url), live, "{url}");
    }
}
