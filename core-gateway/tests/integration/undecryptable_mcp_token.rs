// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! An MCP server's stored static token, on the MCP proxy.
//!
//! `mcp_servers.auth_token` of a `bearer` server is decrypted before it is
//! used: a ciphertext that decrypts is sent as its plaintext, and anything
//! else (a ciphertext written under another `ENCRYPTION_KEY`, an envelope
//! the gateway cannot unwrap, a value that is not ciphertext) is never
//! sent: the call is
//! refused 502, as a static token that is not a valid header value is.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::json;
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::services::encryption::EncryptionService;

use crate::common;

const T: &str = "tenant-mcptoken";
const RG: &str = "rg-mcptoken";
const SERVER: &str = "ms-mcptoken";
/// The master key of `AppConfig::for_test`.
const THIS_KEY: &str = "test-encryption-key-32-chars-ok!";
const PLAIN: &str = "mcp-static-made-up";

/// An MCP server at `upstream` whose stored token is `stored`, bound to the
/// caller's group. Returns the caller's key.
async fn seed(pool: &PgPool, upstream: &MockServer, stored: &str) -> String {
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "jsonrpc": "2.0", "id": 1, "result": {"tools": []}
        })))
        .mount(upstream)
        .await;
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_mcp_server_with_base_url(pool, SERVER, T, "static", &upstream.uri()).await;
    sqlx::query("UPDATE mcp_servers SET auth_type = 'bearer', auth_token = $1 WHERE id = $2")
        .bind(stored)
        .bind(SERVER)
        .execute(pool)
        .await
        .expect("store the token");
    common::seed_server_config(pool, "sc-mcptoken", T, SERVER).await;
    common::bind_server_config_to_rg(pool, "sc-mcptoken", RG).await;
    common::seed_api_key(pool, T, RG, None, "mcptoken-key").await
}

async fn tools_list(pool: PgPool, key: &str) -> (StatusCode, String) {
    let app = common::build_mcp_proxy_app(common::build_test_state(pool));
    let req = Request::builder()
        .method("POST")
        .uri(format!("/v1/proxy/mcp/{SERVER}"))
        .header("X-Tenant-ID", T)
        .header("X-API-Key", key)
        .header("Content-Type", "application/json")
        .header("Accept", "application/json")
        .extension(axum::extract::ConnectInfo::<std::net::SocketAddr>(
            "127.0.0.1:0".parse().unwrap(),
        ))
        .body(Body::from(
            json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).to_string(),
        ))
        .expect("request");
    let resp = app.oneshot(req).await.expect("response");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.expect("body").to_bytes();
    (status, String::from_utf8_lossy(&bytes).into_owned())
}

async fn a_stored_token_that_does_not_decrypt_is_never_sent(pool: PgPool, stored: String) {
    let upstream = MockServer::start().await;
    let key = seed(&pool, &upstream, &stored).await;
    let (status, body) = tools_list(pool, &key).await;
    let received = upstream.received_requests().await.expect("recording");
    assert!(
        received.is_empty(),
        "the MCP server was called {} time(s); Authorization carried the stored ciphertext: {}",
        received.len(),
        received.iter().any(|r| r
            .headers
            .get("authorization")
            .and_then(|v| v.to_str().ok())
            .is_some_and(|v| v.contains(&stored)))
    );
    assert_eq!(status, StatusCode::BAD_GATEWAY, "{body}");
    assert!(body.contains("cannot be read"), "{body}");
    assert!(
        !body.contains(&stored),
        "the answer repeats the stored value"
    );
}

async fn the_upstream_receives(pool: PgPool, stored: &str, expected: &str) {
    let upstream = MockServer::start().await;
    let key = seed(&pool, &upstream, stored).await;
    let (status, body) = tools_list(pool, &key).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let received = upstream.received_requests().await.expect("recording");
    assert_eq!(received.len(), 1);
    assert_eq!(
        received[0]
            .headers
            .get("authorization")
            .and_then(|v| v.to_str().ok()),
        Some(expected)
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_token_encrypted_under_another_master_key_is_never_sent(pool: PgPool) {
    let stored = EncryptionService::new("another-installations-master-key")
        .encrypt(PLAIN)
        .expect("encrypt");
    a_stored_token_that_does_not_decrypt_is_never_sent(pool, stored).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_token_in_an_envelope_the_gateway_cannot_unwrap_is_never_sent(pool: PgPool) {
    let inner = EncryptionService::new("a-data-key-the-gateway-never-sees")
        .encrypt(PLAIN)
        .expect("encrypt");
    a_stored_token_that_does_not_decrypt_is_never_sent(
        pool,
        format!("BAEv1:aws_kms:d3JhcHBlZC1kYXRhLWtleQ:{inner}"),
    )
    .await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_token_encrypted_under_this_master_key_is_sent_in_clear(pool: PgPool) {
    let stored = EncryptionService::new(THIS_KEY)
        .encrypt(PLAIN)
        .expect("encrypt");
    the_upstream_receives(pool, &stored, &format!("Bearer {PLAIN}")).await;
}

/// The column holds ciphertext and nothing else. A stored value that is not
/// a ciphertext the gateway can open is not a token: it is never sent as
/// the bearer, and the call is refused as for any stored token that does
/// not decrypt (the Control Plane refuses such a value the same way).
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_stored_value_that_is_not_ciphertext_is_never_sent(pool: PgPool) {
    a_stored_token_that_does_not_decrypt_is_never_sent(pool, PLAIN.to_string()).await;
}

// ---------------------------------------------------------------------------
// The stored token is a bearer token: it is sent to a `bearer` server only
// ---------------------------------------------------------------------------

async fn set_auth_type(pool: &PgPool, auth_type: Option<&str>) {
    sqlx::query("UPDATE mcp_servers SET auth_type = $1 WHERE id = $2")
        .bind(auth_type)
        .bind(SERVER)
        .execute(pool)
        .await
        .expect("set auth_type");
}

/// A server whose `auth_type` is not `bearer` is called without the stored
/// token, whatever is stored: the call goes out with no `Authorization`.
async fn the_stored_token_is_not_sent(pool: PgPool, auth_type: Option<&str>, stored: &str) {
    let upstream = MockServer::start().await;
    let key = seed(&pool, &upstream, stored).await;
    set_auth_type(&pool, auth_type).await;
    let (status, body) = tools_list(pool, &key).await;
    assert_eq!(status, StatusCode::OK, "{auth_type:?}: {body}");
    let received = upstream.received_requests().await.expect("recording");
    assert_eq!(received.len(), 1, "{auth_type:?}");
    assert_eq!(
        received[0]
            .headers
            .get("authorization")
            .and_then(|v| v.to_str().ok()),
        None,
        "auth_type {auth_type:?}: the stored static token was sent"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_server_with_no_authentication_is_never_sent_the_stored_token(pool: PgPool) {
    the_stored_token_is_not_sent(pool, Some("none"), PLAIN).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_oauth_server_is_never_sent_the_stored_token(pool: PgPool) {
    let stored = EncryptionService::new(THIS_KEY)
        .encrypt(PLAIN)
        .expect("encrypt");
    the_stored_token_is_not_sent(pool, Some("oauth"), &stored).await;
}

/// No `auth_type`, and one the gateway does not know: the most restrictive
/// reading, the token stays where it is.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_server_with_no_or_an_unknown_auth_type_is_never_sent_the_stored_token(pool: PgPool) {
    the_stored_token_is_not_sent(pool.clone(), None, PLAIN).await;
    set_auth_type(&pool, Some("Bearer ")).await;
    let upstream = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "jsonrpc": "2.0", "id": 1, "result": {"tools": []}
        })))
        .mount(&upstream)
        .await;
    sqlx::query("UPDATE mcp_servers SET base_url = $1 WHERE id = $2")
        .bind(upstream.uri())
        .bind(SERVER)
        .execute(&pool)
        .await
        .expect("point at the second upstream");
    let key = common::seed_api_key(&pool, T, RG, None, "mcptoken-key-2").await;
    let (status, body) = tools_list(pool, &key).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let received = upstream.received_requests().await.expect("recording");
    assert_eq!(received.len(), 1);
    assert!(received[0].headers.get("authorization").is_none());
}

/// The token of a server that is not `bearer` is not read at all: a stored
/// ciphertext that does not decrypt refuses nothing there.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_token_that_is_not_used_is_not_read(pool: PgPool) {
    let stored = EncryptionService::new("another-installations-master-key")
        .encrypt(PLAIN)
        .expect("encrypt");
    the_stored_token_is_not_sent(pool, Some("none"), &stored).await;
}
