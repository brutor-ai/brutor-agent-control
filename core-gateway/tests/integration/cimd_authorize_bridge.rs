// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! CIMD authorize-bridge route tests.
//!
//! These exercise the DB-dependent decision logic of the new inbound
//! OAuth `…/authorize` route and the rewritten discovery metadata:
//!
//!   * a **registered** (non-URL) client_id 302s straight to the IdP's
//!     authorization endpoint with its query preserved (behavior
//!     identical to the pre-bridge direct flow);
//!   * a **CIMD** (https URL) client_id needs Redis for bridge state —
//!     without it the route fails closed (503) rather than silently
//!     dropping the flow;
//!   * malformed CIMD authorize requests are rejected at the gateway
//!     (missing PKCE / redirect_uri / wrong response_type) *before* any
//!     document is fetched;
//!   * discovery metadata advertises the gateway authorize endpoint and
//!     gates `client_id_metadata_document_supported` on the IdP having a
//!     registration endpoint (the bridge needs DCR upstream).
//!
//! The full happy-path bridge (document fetch → upstream DCR → callback
//! → token) needs Redis + a live IdP + an HTTP document host and is
//! covered by the `services::cimd` unit tests plus manual/e2e runs; the
//! security-critical fetch/validate/PKCE logic is unit-tested there.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use tower::ServiceExt;

use crate::common::{build_oauth_app, build_test_state, seed_idp, seed_mcp_server, seed_tenant};

const TENANT: &str = "t-cimd";
const MCP: &str = "mcp-cimd";
const IDP: &str = "idp-cimd";
const IDP_AUTHORIZE: &str = "https://idp.test.local/authorize";

async fn get(app: &axum::Router, uri: &str) -> (StatusCode, Option<String>, String) {
    let req = Request::builder()
        .method("GET")
        .uri(uri)
        .header("X-Tenant-ID", TENANT)
        .body(Body::empty())
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let location = resp
        .headers()
        .get("location")
        .and_then(|v| v.to_str().ok())
        .map(String::from);
    let body = axum::body::to_bytes(resp.into_body(), 1 << 20)
        .await
        .map(|b| String::from_utf8_lossy(&b).to_string())
        .unwrap_or_default();
    (status, location, body)
}

fn authorize_uri(query: &str) -> String {
    format!("/v1/proxy/oauth/{MCP}/{IDP}/authorize?{query}")
}

#[sqlx::test(migrations = "tests/migrations")]
async fn registered_client_passes_through_to_idp(pool: sqlx::PgPool) {
    seed_tenant(&pool, TENANT).await;
    seed_mcp_server(&pool, MCP, TENANT, "CIMD test").await;
    seed_idp(
        &pool,
        IDP,
        TENANT,
        Some(IDP_AUTHORIZE),
        Some("https://idp.test.local/register"),
    )
    .await;
    let app = build_oauth_app(build_test_state(pool));

    let (status, location, _) = get(
        &app,
        &authorize_uri(
            "response_type=code&client_id=my-registered-client\
             &redirect_uri=https%3A%2F%2Fapp.example.com%2Fcb&state=xyz&scope=openid",
        ),
    )
    .await;

    assert_eq!(status, StatusCode::TEMPORARY_REDIRECT);
    let loc = location.expect("Location header");
    assert!(
        loc.starts_with(IDP_AUTHORIZE),
        "should 302 to the IdP: {loc}"
    );
    // Original params preserved verbatim — the gateway is a pass-through here.
    assert!(loc.contains("client_id=my-registered-client"), "{loc}");
    assert!(loc.contains("state=xyz"), "{loc}");
    assert!(loc.contains("scope=openid"), "{loc}");
}

#[sqlx::test(migrations = "tests/migrations")]
async fn cimd_client_requires_redis(pool: sqlx::PgPool) {
    seed_tenant(&pool, TENANT).await;
    seed_mcp_server(&pool, MCP, TENANT, "CIMD test").await;
    seed_idp(
        &pool,
        IDP,
        TENANT,
        Some(IDP_AUTHORIZE),
        Some("https://idp.test.local/register"),
    )
    .await;
    // build_test_state wires redis: None — the CIMD path must fail closed.
    let app = build_oauth_app(build_test_state(pool));

    let (status, _, body) = get(
        &app,
        &authorize_uri(
            "response_type=code\
             &client_id=https%3A%2F%2Fcli.example.com%2Fclient.json\
             &redirect_uri=http%3A%2F%2F127.0.0.1%3A1%2Fcb\
             &code_challenge=abc&code_challenge_method=S256",
        ),
    )
    .await;

    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert!(body.contains("Redis"), "{body}");
}

#[sqlx::test(migrations = "tests/migrations")]
async fn cimd_authorize_validates_params_before_fetch(pool: sqlx::PgPool) {
    seed_tenant(&pool, TENANT).await;
    seed_mcp_server(&pool, MCP, TENANT, "CIMD test").await;
    seed_idp(
        &pool,
        IDP,
        TENANT,
        Some(IDP_AUTHORIZE),
        Some("https://idp.test.local/register"),
    )
    .await;
    let app = build_oauth_app(build_test_state(pool));

    let cimd = "client_id=https%3A%2F%2Fcli.example.com%2Fclient.json";

    // wrong response_type
    let (s, _, _) = get(
        &app,
        &authorize_uri(&format!(
            "response_type=token&{cimd}\
        &redirect_uri=http%3A%2F%2F127.0.0.1%3A1%2Fcb&code_challenge=a&code_challenge_method=S256"
        )),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "response_type must be code");

    // missing PKCE challenge
    let (s, _, _) = get(
        &app,
        &authorize_uri(&format!(
            "response_type=code&{cimd}\
        &redirect_uri=http%3A%2F%2F127.0.0.1%3A1%2Fcb"
        )),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "PKCE challenge required");

    // non-S256 PKCE method
    let (s, _, _) = get(
        &app,
        &authorize_uri(&format!(
            "response_type=code&{cimd}\
        &redirect_uri=http%3A%2F%2F127.0.0.1%3A1%2Fcb&code_challenge=a&code_challenge_method=plain"
        )),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "S256 required");

    // missing redirect_uri
    let (s, _, _) = get(
        &app,
        &authorize_uri(&format!(
            "response_type=code&{cimd}\
        &code_challenge=a&code_challenge_method=S256"
        )),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "redirect_uri required");
}

#[sqlx::test(migrations = "tests/migrations")]
async fn metadata_advertises_bridge_when_idp_supports_dcr(pool: sqlx::PgPool) {
    seed_tenant(&pool, TENANT).await;
    seed_mcp_server(&pool, MCP, TENANT, "CIMD test").await;
    seed_idp(
        &pool,
        IDP,
        TENANT,
        Some(IDP_AUTHORIZE),
        Some("https://idp.test.local/register"),
    )
    .await;
    let app = build_oauth_app(build_test_state(pool));

    let (status, _, body) = get(
        &app,
        &format!("/v1/proxy/oauth/{MCP}/{IDP}/.well-known/openid-configuration"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let cfg: serde_json::Value = serde_json::from_str(&body).unwrap();

    // authorization_endpoint is now the gateway facade, not the raw IdP.
    let ae = cfg["authorization_endpoint"].as_str().unwrap();
    assert!(
        ae.ends_with(&format!("/v1/proxy/oauth/{MCP}/{IDP}/authorize")),
        "{ae}"
    );
    assert_eq!(
        cfg["client_id_metadata_document_supported"],
        serde_json::json!(true)
    );
    // RFC 9207 stays off for the facade (authorize not proxied for iss).
    assert_eq!(
        cfg["authorization_response_iss_parameter_supported"],
        serde_json::json!(false)
    );
}

#[sqlx::test(migrations = "tests/migrations")]
async fn metadata_disables_cimd_without_idp_dcr(pool: sqlx::PgPool) {
    seed_tenant(&pool, TENANT).await;
    seed_mcp_server(&pool, MCP, TENANT, "CIMD test").await;
    // No registration endpoint → the bridge can't mint its upstream client.
    seed_idp(&pool, IDP, TENANT, Some(IDP_AUTHORIZE), None).await;
    let app = build_oauth_app(build_test_state(pool));

    let (status, _, body) = get(
        &app,
        &format!("/v1/proxy/oauth/{MCP}/{IDP}/.well-known/openid-configuration"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let cfg: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(
        cfg["client_id_metadata_document_supported"],
        serde_json::json!(false)
    );
}
