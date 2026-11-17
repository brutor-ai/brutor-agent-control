// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The OAuth proxy routes end to end: a real router, a real database, a real
//! Redis where the route needs one, and a mock IdP and MCP upstream.
//!
//! What is pinned here is mostly the refusals: an unusable credential, a code
//! replayed or redeemed in the wrong place, a PKCE verifier that does not
//! match, a token from another tenant or server, and the upstream credential
//! that must never reach the client.

use std::sync::Arc;

use axum::Router;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use jsonwebtoken::{EncodingKey, Header, encode};
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{body_string_contains, header, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::config::AppConfig;
use brutor_gateway_core::services::cimd::{self, BridgeState, CodeState};
use brutor_gateway_core::services::oauth_token_manager::OAuthTokenManager;
use brutor_gateway_core::services::telemetry::TelemetryState;
use brutor_gateway_core::services::{AppState, SharedState};

use crate::common;

pub(crate) const T: &str = "t-oauth";
pub(crate) const OTHER_T: &str = "t-oauth-other";
pub(crate) const SRV: &str = "srv-oauth";
pub(crate) const IDP: &str = "idp-oauth";
pub(crate) const CIMD_URL: &str = "https://cli.example.com/client.json";
pub(crate) const UPSTREAM_ACCESS: &str = "upstream-access-token-never-for-the-client";
pub(crate) const UPSTREAM_REFRESH: &str = "upstream-refresh-token-never-for-the-client";

pub(crate) fn redis_url() -> String {
    std::env::var("TEST_REDIS_URL").unwrap_or_else(|_| "redis://127.0.0.1:6379/15".to_string())
}

pub(crate) async fn redis() -> redis::aio::ConnectionManager {
    let url = redis_url();
    let client = redis::Client::open(url.as_str()).expect("redis url");
    redis::aio::ConnectionManager::new(client)
        .await
        .unwrap_or_else(|e| panic!("connect to the test Redis at {url} (set TEST_REDIS_URL): {e}"))
}

pub(crate) fn state_with_redis(pool: &PgPool, redis: redis::aio::ConnectionManager) -> SharedState {
    let config = AppConfig::for_test("postgresql://placeholder/replaced".to_string());
    Arc::new(AppState::new(
        config,
        pool.clone(),
        Some(redis),
        reqwest::Client::new(),
        TelemetryState {
            prometheus_handle: None,
        },
    ))
}

/// One tenant, one MCP server and one IdP, both pointing at mock servers.
pub(crate) struct World {
    pub(crate) idp: MockServer,
    pub(crate) mcp: MockServer,
    pub(crate) state: SharedState,
    pub(crate) app: Router,
}

pub(crate) async fn seed_world(pool: &PgPool, idp: &MockServer, mcp: &MockServer) {
    common::seed_tenant(pool, T).await;
    common::seed_mcp_server_with_base_url(pool, SRV, T, "oauth", &mcp.uri()).await;
    common::seed_idp(pool, IDP, T, Some("https://idp.test.local/authorize"), None).await;
    sqlx::query("UPDATE idp_servers SET token_endpoint = $1, userinfo_endpoint = $2 WHERE id = $3")
        .bind(format!("{}/token", idp.uri()))
        .bind(format!("{}/userinfo", idp.uri()))
        .bind(IDP)
        .execute(pool)
        .await
        .unwrap();
    // The server's IdP: a code is exchanged only for
    // a server and IdP the tenant paired.
    sqlx::query("UPDATE mcp_servers SET idp_id = $1 WHERE id = $2")
        .bind(IDP)
        .bind(SRV)
        .execute(pool)
        .await
        .unwrap();
}

pub(crate) async fn world(pool: &PgPool) -> World {
    world_on(pool, common::build_test_state(pool.clone())).await
}

/// [`world`] over a state the test composed.
pub(crate) async fn world_on(pool: &PgPool, state: SharedState) -> World {
    let (idp, mcp) = (MockServer::start().await, MockServer::start().await);
    seed_world(pool, &idp, &mcp).await;
    let app = common::build_oauth_app(state.clone());
    World {
        idp,
        mcp,
        state,
        app,
    }
}

pub(crate) async fn redis_world(pool: &PgPool) -> World {
    let (idp, mcp) = (MockServer::start().await, MockServer::start().await);
    seed_world(pool, &idp, &mcp).await;
    let state = state_with_redis(pool, redis().await);
    let app = common::build_oauth_app(state.clone());
    World {
        idp,
        mcp,
        state,
        app,
    }
}

pub(crate) async fn idp_answers(idp: &MockServer) {
    Mock::given(method("POST"))
        .and(path("/token"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "access_token": UPSTREAM_ACCESS,
            "refresh_token": UPSTREAM_REFRESH,
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "openid email",
        })))
        .mount(idp)
        .await;
    Mock::given(method("GET"))
        .and(path("/userinfo"))
        .and(header(
            "authorization",
            format!("Bearer {UPSTREAM_ACCESS}").as_str(),
        ))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "sub": "idp-user-1",
            "preferred_username": "alice",
            "email": "alice@example.com",
            "name": "Alice",
        })))
        .mount(idp)
        .await;
}

pub(crate) async fn send(
    app: &Router,
    req: Request<Body>,
) -> (StatusCode, Value, axum::http::HeaderMap) {
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let headers = resp.headers().clone();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let body = serde_json::from_slice(&bytes)
        .unwrap_or_else(|_| Value::String(String::from_utf8_lossy(&bytes).into_owned()));
    (status, body, headers)
}

pub(crate) fn form(pairs: &[(&str, &str)]) -> String {
    serde_urlencoded::to_string(pairs).unwrap()
}

pub(crate) fn token_req_at(srv: &str, idp: &str, pairs: &[(&str, &str)]) -> Request<Body> {
    Request::builder()
        .method("POST")
        .uri(format!("/v1/proxy/oauth/{srv}/{idp}/token"))
        .header("content-type", "application/x-www-form-urlencoded")
        .body(Body::from(form(pairs)))
        .unwrap()
}

pub(crate) fn token_req(pairs: &[(&str, &str)]) -> Request<Body> {
    token_req_at(SRV, IDP, pairs)
}

pub(crate) fn unique_code() -> String {
    format!("code-{}", uuid::Uuid::new_v4())
}

pub(crate) fn code_form(code: &str) -> Vec<(&str, &str)> {
    vec![
        ("grant_type", "authorization_code"),
        ("code", code),
        ("client_id", "client-1"),
        ("code_verifier", "verifier-1"),
        ("redirect_uri", "https://app.example/cb"),
    ]
}

pub(crate) async fn stored_tokens(pool: &PgPool) -> i64 {
    sqlx::query_scalar("SELECT count(*) FROM oauth_tokens")
        .fetch_one(pool)
        .await
        .unwrap()
}

pub(crate) async fn seed_stored_token(
    state: &SharedState,
    tenant: &str,
    user: &str,
    srv: &str,
    expires_in: i64,
    refresh: Option<&str>,
    issuer: Option<&str>,
) {
    state
        .oauth_token_manager
        .store_token(
            &state.db,
            None,
            user,
            srv,
            tenant,
            IDP,
            &brutor_gateway_core::services::oauth_token_manager::ExtractedTokens {
                access_token: "stored-access".into(),
                refresh_token: refresh.map(String::from),
                expires_in,
                token_type: "Bearer".into(),
                scope: Some("openid".into()),
            },
            Some("client-1"),
            Some("client-secret-1"),
            issuer,
        )
        .await
        .expect("store token");
}

pub(crate) fn mcp_jwt(state: &SharedState, tenant: &str, user: &str, srv: &str) -> String {
    mcp_jwt_of(state, tenant, user, srv, IDP)
}

/// An MCP JWT for a subject of the IdP `idp`.
pub(crate) fn mcp_jwt_of(
    state: &SharedState,
    tenant: &str,
    user: &str,
    srv: &str,
    idp: &str,
) -> String {
    state
        .oauth_token_manager
        .create_mcp_jwt(tenant, user, srv, idp, Some("alice"), None, None)
        .unwrap()
}

/// An MCP JWT with arbitrary issuer, type, expiry or signing key.
pub(crate) fn forged_jwt(secret: &str, iss: &str, typ: &str, exp_offset: i64) -> String {
    let now = common::db_now().timestamp();
    encode(
        &Header::default(),
        &json!({
            "sub": "idp-user-1", "iat": now, "exp": now + exp_offset, "iss": iss,
            "jti": "j", "tenant_id": T, "user_id": "idp-user-1", "mcp_server_id": SRV,
            "idp_id": IDP, "type": typ,
        }),
        &EncodingKey::from_secret(secret.as_bytes()),
    )
    .unwrap()
}

// ── The URLs the gateway advertises ────────────────────────────────────

/// The issuer, metadata and redirect URIs are built from
/// `BRUTOR_GATEWAY_PUBLIC_URL` only. They were built from `X-Forwarded-Proto`
/// / `X-Forwarded-Host` / `Host` whenever the public URL said "localhost", so
/// any caller chose the issuer and token endpoint a client was told to use.
#[sqlx::test(migrations = "./tests/migrations")]
async fn oauth_urls_come_from_the_public_url_never_from_request_headers(pool: PgPool) {
    let w = world(&pool).await;
    let public = w.state.config.public_url.trim_end_matches('/').to_string();
    assert!(
        public.contains("localhost"),
        "the case that used the headers"
    );
    let issuer = format!("{public}/v1/proxy/oauth/{SRV}/{IDP}");
    let get = |path: &str| {
        Request::builder()
            .uri(format!("/v1/proxy/oauth/{SRV}/{IDP}/.well-known/{path}"))
            .header("host", "host.attacker.example")
            .header("x-forwarded-proto", "https")
            .header("x-forwarded-host", "forwarded.attacker.example")
            .body(Body::empty())
            .unwrap()
    };

    let (s, b, _) = send(&w.app, get("openid-configuration")).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert_eq!(b["issuer"], json!(issuer), "{b}");
    assert_eq!(b["token_endpoint"], json!(format!("{issuer}/token")), "{b}");
    assert!(!b.to_string().contains("attacker"), "{b}");

    let (s, b, _) = send(&w.app, get("oauth-protected-resource")).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert_eq!(b["authorization_servers"], json!([issuer]), "{b}");
}

// ── Grant routing and required parameters ──────────────────────────────

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unsupported_or_absent_grant_type_is_refused(pool: PgPool) {
    let w = world(&pool).await;
    let (s, b, _) = send(&w.app, token_req(&[("grant_type", "password")])).await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "{b}");
    assert!(b.to_string().contains("Unsupported grant_type"), "{b}");
    let (s, _, _) = send(&w.app, token_req(&[])).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_exchange_without_code_or_client_never_reaches_the_idp(pool: PgPool) {
    let w = world(&pool).await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(500))
        .expect(0)
        .mount(&w.idp)
        .await;
    for pairs in [
        vec![("grant_type", "authorization_code"), ("client_id", "c")],
        vec![("grant_type", "authorization_code"), ("code", "x")],
    ] {
        let (s, _, _) = send(&w.app, token_req(&pairs)).await;
        assert_eq!(s, StatusCode::BAD_REQUEST);
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unknown_server_has_no_tenant_context(pool: PgPool) {
    let w = world(&pool).await;
    let code = unique_code();
    let (s, _, _) = send(&w.app, token_req_at("srv-none", IDP, &code_form(&code))).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_disabled_server_or_idp_is_not_found(pool: PgPool) {
    let w = world(&pool).await;
    let code = unique_code();

    sqlx::query("UPDATE idp_servers SET enabled = false WHERE id = $1")
        .bind(IDP)
        .execute(&pool)
        .await
        .unwrap();
    let (s, _, _) = send(&w.app, token_req(&code_form(&code))).await;
    assert_eq!(s, StatusCode::NOT_FOUND, "a disabled IdP");

    sqlx::query("UPDATE mcp_servers SET enabled = false WHERE id = $1")
        .bind(SRV)
        .execute(&pool)
        .await
        .unwrap();
    let (s, _, _) = send(&w.app, token_req(&code_form(&code))).await;
    assert_eq!(s, StatusCode::NOT_FOUND, "a disabled server");
}

/// SAML is not served by this release: an identity provider stored with the
/// SAML protocol is no identity provider of the OAuth proxy, even with the
/// id the request names.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_saml_identity_provider_is_not_found(pool: PgPool) {
    let w = world(&pool).await;
    sqlx::query("UPDATE idp_servers SET protocol = 'saml' WHERE id = $1")
        .bind(IDP)
        .execute(&pool)
        .await
        .unwrap();
    let (s, _, _) = send(&w.app, token_req(&code_form(&unique_code()))).await;
    assert_eq!(s, StatusCode::NOT_FOUND, "a SAML identity provider");
}

/// The proxy POSTs to the IdP and the MCP server; a tenant-configured
/// cloud-metadata literal is refused before anything is sent.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_destination_the_outbound_policy_denies_is_refused(pool: PgPool) {
    let w = world(&pool).await;
    let code = unique_code();
    sqlx::query("UPDATE idp_servers SET token_endpoint = 'http://169.254.169.254/t' WHERE id = $1")
        .bind(IDP)
        .execute(&pool)
        .await
        .unwrap();
    let (s, b, _) = send(&w.app, token_req(&code_form(&code))).await;
    assert_eq!(s, StatusCode::FORBIDDEN, "{b}");

    sqlx::query("UPDATE mcp_servers SET base_url = 'http://169.254.169.254/' WHERE id = $1")
        .bind(SRV)
        .execute(&pool)
        .await
        .unwrap();
    let (s, b, _) = send(&w.app, token_req(&code_form(&code))).await;
    assert_eq!(s, StatusCode::FORBIDDEN, "{b}");
}

/// The server's tenant decides: an IdP that belongs to another tenant does not
/// exist, whatever the path says.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_idp_of_another_tenant_is_not_reachable_through_this_server(pool: PgPool) {
    let w = world(&pool).await;
    common::seed_tenant(&pool, OTHER_T).await;
    common::seed_idp(&pool, "idp-other", OTHER_T, None, None).await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200))
        .expect(0)
        .mount(&w.idp)
        .await;
    let code = unique_code();
    let (s, _, _) = send(&w.app, token_req_at(SRV, "idp-other", &code_form(&code))).await;
    assert_eq!(s, StatusCode::NOT_FOUND);
    assert_eq!(stored_tokens(&pool).await, 0);
}

// ── Authorization-code exchange ────────────────────────────────────────

/// The whole point of the proxy: the IdP's credentials stay on the server.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_client_gets_a_gateway_jwt_and_never_the_upstream_tokens(pool: PgPool) {
    let w = world(&pool).await;
    idp_answers(&w.idp).await;
    let code = unique_code();
    let (s, b, _) = send(&w.app, token_req(&code_form(&code))).await;
    assert_eq!(s, StatusCode::OK, "{b}");

    let raw = b.to_string();
    assert!(!raw.contains(UPSTREAM_ACCESS), "access token leaked: {raw}");
    assert!(
        !raw.contains(UPSTREAM_REFRESH),
        "refresh token leaked: {raw}"
    );

    let jwt = b["access_token"].as_str().unwrap();
    let claims = w.state.oauth_token_manager.verify_mcp_jwt(jwt).unwrap();
    assert_eq!(claims.user_id, "idp-user-1");
    assert_eq!(claims.tenant_id, T);
    assert_eq!(claims.mcp_server_id, SRV);
    assert_eq!(b["user"]["email"], "alice@example.com");
    assert_eq!(b["token_info"]["auto_refresh"], true);

    // A standard client sends back what it was given as `refresh_token`, and
    // the refresh grant accepts exactly that: the gateway JWT.
    let handle = b["refresh_token"].as_str().unwrap();
    assert!(
        w.state
            .oauth_token_manager
            .verify_mcp_jwt_allow_expired(handle)
            .is_ok()
    );
    let (s, b, _) = send(
        &w.app,
        token_req(&[("grant_type", "refresh_token"), ("refresh_token", handle)]),
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{b}");
}

/// No upstream refresh token, nothing to offer a refresh grant with.
#[sqlx::test(migrations = "./tests/migrations")]
async fn no_refresh_handle_is_issued_without_an_upstream_refresh_token(pool: PgPool) {
    let w = world(&pool).await;
    Mock::given(method("POST"))
        .and(path("/token"))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(json!({"access_token": UPSTREAM_ACCESS})),
        )
        .mount(&w.idp)
        .await;
    Mock::given(method("GET"))
        .and(path("/userinfo"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"id": 4711})))
        .mount(&w.idp)
        .await;
    let code = unique_code();
    let (s, b, _) = send(&w.app, token_req(&code_form(&code))).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert!(b["refresh_token"].is_null(), "{b}");
    assert_eq!(b["token_info"]["auto_refresh"], false);
    assert_eq!(b["user"]["user_id"], "4711", "a numeric id is still an id");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn stored_credentials_are_encrypted_and_bound_to_the_issuer(pool: PgPool) {
    let w = world(&pool).await;
    idp_answers(&w.idp).await;
    let code = unique_code();
    let mut pairs = code_form(&code);
    pairs.push(("client_secret", "form-secret"));
    let (s, b, _) = send(&w.app, token_req(&pairs)).await;
    assert_eq!(s, StatusCode::OK, "{b}");

    let (access, refresh, secret, issuer, idp_id): (
        String,
        Option<String>,
        Option<String>,
        Option<String>,
        String,
    ) = sqlx::query_as(
        "SELECT encrypted_access_token, encrypted_refresh_token, encrypted_client_secret, \
         issuer, idp_id FROM oauth_tokens WHERE tenant_id = $1 AND idp_user_id = 'idp-user-1' \
         AND mcp_server_id = $2",
    )
    .bind(T)
    .bind(SRV)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert!(!access.contains(UPSTREAM_ACCESS));
    assert_eq!(
        w.state.encryption.decrypt(&access).unwrap(),
        UPSTREAM_ACCESS
    );
    assert_eq!(
        w.state.encryption.decrypt(&refresh.unwrap()).unwrap(),
        UPSTREAM_REFRESH
    );
    assert_eq!(
        w.state.encryption.decrypt(&secret.unwrap()).unwrap(),
        "form-secret"
    );
    assert_eq!(issuer.as_deref(), Some("https://idp.test.local"));
    assert_eq!(idp_id, IDP);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_exchange_forwards_the_clients_pkce_and_redirect_to_the_idp(pool: PgPool) {
    let w = world(&pool).await;
    Mock::given(method("POST"))
        .and(path("/token"))
        .and(body_string_contains("grant_type=authorization_code"))
        .and(body_string_contains("code_verifier=verifier-1"))
        .and(body_string_contains("client_id=client-1"))
        .and(body_string_contains(
            "redirect_uri=https%3A%2F%2Fapp.example%2Fcb",
        ))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(json!({"access_token": UPSTREAM_ACCESS})),
        )
        .expect(1)
        .mount(&w.idp)
        .await;
    Mock::given(method("GET"))
        .and(path("/userinfo"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"sub": "u"})))
        .mount(&w.idp)
        .await;
    let code = unique_code();
    let (s, b, _) = send(&w.app, token_req(&code_form(&code))).await;
    assert_eq!(s, StatusCode::OK, "{b}");
}

/// A wrong code, or a PKCE verifier the IdP rejects, stores nothing and gets
/// the client no token.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_code_the_idp_refuses_stores_nothing(pool: PgPool) {
    let w = world(&pool).await;
    Mock::given(method("POST"))
        .and(path("/token"))
        .respond_with(ResponseTemplate::new(400).set_body_json(json!({"error": "invalid_grant"})))
        .mount(&w.idp)
        .await;
    let code = unique_code();
    let (s, b, _) = send(&w.app, token_req(&code_form(&code))).await;
    assert_eq!(s, StatusCode::BAD_GATEWAY, "{b}");
    assert!(b.to_string().contains("invalid_grant"), "{b}");
    assert!(b.get("access_token").is_none());
    assert_eq!(stored_tokens(&pool).await, 0);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_idp_answer_without_an_access_token_is_a_failure(pool: PgPool) {
    let w = world(&pool).await;
    Mock::given(method("POST"))
        .and(path("/token"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"token_type": "Bearer"})))
        .mount(&w.idp)
        .await;
    let code = unique_code();
    let (s, _, _) = send(&w.app, token_req(&code_form(&code))).await;
    assert_eq!(s, StatusCode::BAD_GATEWAY);
    assert_eq!(stored_tokens(&pool).await, 0);
}

/// Some IdPs answer form-encoded (GitHub's default).
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_form_encoded_token_answer_is_understood(pool: PgPool) {
    let w = world(&pool).await;
    Mock::given(method("POST"))
        .and(path("/token"))
        .respond_with(ResponseTemplate::new(200).set_body_string(format!(
            "access_token={UPSTREAM_ACCESS}&expires_in=120&token_type=bearer"
        )))
        .mount(&w.idp)
        .await;
    Mock::given(method("GET"))
        .and(path("/userinfo"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({"login": "octocat", "id": "g-1", "user": "octocat"})),
        )
        .mount(&w.idp)
        .await;
    let code = unique_code();
    let (s, b, _) = send(&w.app, token_req(&code_form(&code))).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert_eq!(b["user"]["username"], "octocat");
    assert_eq!(b["user"]["user_id"], "g-1");
}

/// A credential with no owner is not storable.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_token_with_no_subject_is_refused_and_not_stored(pool: PgPool) {
    let w = world(&pool).await;
    Mock::given(method("POST"))
        .and(path("/token"))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(json!({"access_token": UPSTREAM_ACCESS})),
        )
        .mount(&w.idp)
        .await;
    // The userinfo call itself fails.
    Mock::given(method("GET"))
        .and(path("/userinfo"))
        .respond_with(ResponseTemplate::new(401))
        .mount(&w.idp)
        .await;
    let code = unique_code();
    let (s, b, _) = send(&w.app, token_req(&code_form(&code))).await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "{b}");
    assert!(b.to_string().contains("oauth_subject_unavailable"), "{b}");
    assert_eq!(stored_tokens(&pool).await, 0);

    // The IdP has no userinfo endpoint at all.
    sqlx::query("UPDATE idp_servers SET userinfo_endpoint = NULL WHERE id = $1")
        .bind(IDP)
        .execute(&pool)
        .await
        .unwrap();
    let code = unique_code();
    let (s, b, _) = send(&w.app, token_req(&code_form(&code))).await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "{b}");
    assert_eq!(stored_tokens(&pool).await, 0);
}

/// An empty or absent subject is no subject: every such user's token would
/// be filed under the same key, as with the literal "unknown".
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_empty_subject_from_userinfo_is_no_subject(pool: PgPool) {
    let w = world(&pool).await;
    Mock::given(method("POST"))
        .and(path("/token"))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(json!({"access_token": UPSTREAM_ACCESS})),
        )
        .mount(&w.idp)
        .await;
    Mock::given(method("GET"))
        .and(path("/userinfo"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"sub": ""})))
        .mount(&w.idp)
        .await;
    let code = unique_code();
    let (s, b, _) = send(&w.app, token_req(&code_form(&code))).await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "{b}");
    assert_eq!(stored_tokens(&pool).await, 0, "no token under an empty key");
}

/// A session of another tenant authenticates nobody here, so it cannot lend
/// the exchange an identity.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_gateway_session_of_another_tenant_lends_no_identity(pool: PgPool) {
    let w = world(&pool).await;
    idp_answers(&w.idp).await;
    sqlx::query("UPDATE idp_servers SET userinfo_endpoint = NULL WHERE id = $1")
        .bind(IDP)
        .execute(&pool)
        .await
        .unwrap();
    let jwt = common::make_portal_jwt(8802, "user-8802", OTHER_T);
    let code = unique_code();
    let mut req = token_req(&code_form(&code));
    req.headers_mut().insert(
        "x-gateway-authorization",
        format!("Bearer {jwt}").parse().unwrap(),
    );
    let (s, b, _) = send(&w.app, req).await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "{b}");
    assert_eq!(stored_tokens(&pool).await, 0);
}

// ── Client secret resolution ───────────────────────────────────────────

pub(crate) async fn seed_client(
    state: &SharedState,
    pool: &PgPool,
    client_type: &str,
    secret: Option<&str>,
    active: bool,
) {
    sqlx::query(
        "INSERT INTO oauth_app_clients (id, tenant_id, name, app_type, idp_id, client_id, \
         encrypted_client_secret, redirect_uri, scopes, client_type, supports_pkce, \
         allowed_grant_types, is_active) \
         VALUES ('oac-1', $1, 'oac', 'web', $2, 'client-1', $3, 'https://app.example/cb', \
                 '[]'::json, $4, true, '[]'::json, $5)",
    )
    .bind(T)
    .bind(IDP)
    .bind(secret.map(|s| state.encryption.encrypt(s).unwrap()))
    .bind(client_type)
    .bind(active)
    .execute(pool)
    .await
    .unwrap();
    common::seed_server_config(pool, "sc-oauth", T, SRV).await;
    sqlx::query("UPDATE server_configs SET oauth_client_id = 'oac-1' WHERE id = 'sc-oauth'")
        .execute(pool)
        .await
        .unwrap();
}

pub(crate) fn no_pkce_form(code: &str) -> Vec<(&str, &str)> {
    vec![
        ("grant_type", "authorization_code"),
        ("code", code),
        ("client_id", "client-1"),
    ]
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_confidential_clients_stored_secret_goes_to_the_idp_not_the_client(pool: PgPool) {
    let w = world(&pool).await;
    seed_client(&w.state, &pool, "confidential", Some("stored-s3cret"), true).await;
    Mock::given(method("POST"))
        .and(path("/token"))
        .and(body_string_contains("client_secret=stored-s3cret"))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(json!({"access_token": UPSTREAM_ACCESS})),
        )
        .expect(1)
        .mount(&w.idp)
        .await;
    Mock::given(method("GET"))
        .and(path("/userinfo"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"sub": "u1"})))
        .mount(&w.idp)
        .await;
    let code = unique_code();
    let (s, b, _) = send(&w.app, token_req(&no_pkce_form(&code))).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert!(!b.to_string().contains("stored-s3cret"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_public_pkce_client_sends_no_secret(pool: PgPool) {
    let w = world(&pool).await;
    seed_client(&w.state, &pool, "public", None, true).await;
    idp_answers(&w.idp).await;
    let code = unique_code();
    let (s, b, _) = send(&w.app, token_req(&code_form(&code))).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    let sent = w.idp.received_requests().await.unwrap();
    let body = String::from_utf8_lossy(&sent[0].body).to_string();
    assert!(!body.contains("client_secret"), "{body}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_confidential_client_with_no_usable_secret_fails_before_the_idp(pool: PgPool) {
    let w = world(&pool).await;
    seed_client(&w.state, &pool, "confidential", None, true).await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200))
        .expect(0)
        .mount(&w.idp)
        .await;
    let code = unique_code();
    let (s, b, _) = send(&w.app, token_req(&no_pkce_form(&code))).await;
    assert_eq!(s, StatusCode::INTERNAL_SERVER_ERROR, "{b}");
    assert!(b.to_string().contains("no stored secret"), "{b}");

    // A secret that cannot be decrypted is a failure too, not "no secret".
    sqlx::query("UPDATE oauth_app_clients SET encrypted_client_secret = 'not-a-ciphertext'")
        .execute(&pool)
        .await
        .unwrap();
    let code = unique_code();
    let (s, b, _) = send(&w.app, token_req(&no_pkce_form(&code))).await;
    assert_eq!(s, StatusCode::INTERNAL_SERVER_ERROR, "{b}");
    assert!(b.to_string().contains("decrypt"), "{b}");
}

/// Each reason a registered client does not match is named, and none of them
/// gets as far as the IdP.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unmatched_client_is_refused_with_its_reason(pool: PgPool) {
    let w = world(&pool).await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200))
        .expect(0)
        .mount(&w.idp)
        .await;
    let code = unique_code();

    // No server config at all.
    let (s, b, _) = send(&w.app, token_req(&no_pkce_form(&code))).await;
    assert_eq!(s, StatusCode::NOT_FOUND, "{b}");
    assert!(b.to_string().contains("no server_config"), "{b}");

    seed_client(&w.state, &pool, "confidential", Some("s"), false).await;
    let (s, b, _) = send(&w.app, token_req(&no_pkce_form(&code))).await;
    assert_eq!(s, StatusCode::NOT_FOUND, "{b}");
    assert!(b.to_string().contains("is_active=false"), "{b}");

    // A client id the record does not carry.
    let (s, b, _) = send(
        &w.app,
        token_req(&[
            ("grant_type", "authorization_code"),
            ("code", &code),
            ("client_id", "client-other"),
        ]),
    )
    .await;
    assert_eq!(s, StatusCode::NOT_FOUND, "{b}");
    assert!(b.to_string().contains("client_id mismatch"), "{b}");

    // An IdP the record does not carry.
    common::seed_idp(&pool, "idp-2", T, None, None).await;
    let (s, b, _) = send(&w.app, token_req_at(SRV, "idp-2", &no_pkce_form(&code))).await;
    assert_eq!(s, StatusCode::NOT_FOUND, "{b}");
    assert!(b.to_string().contains("idp_id mismatch"), "{b}");

    // Everything matches but the client is off: the join is what refused it.
    sqlx::query("UPDATE oauth_app_clients SET is_active = true, client_id = 'client-x'")
        .execute(&pool)
        .await
        .unwrap();
    let (s, b, _) = send(
        &w.app,
        token_req(&[
            ("grant_type", "authorization_code"),
            ("code", &code),
            ("client_id", "client-1"),
        ]),
    )
    .await;
    assert_eq!(s, StatusCode::NOT_FOUND, "{b}");
}

// ── Capability discovery on the first exchange ─────────────────────────

pub(crate) async fn discovered(
    pool: &PgPool,
) -> (Option<Value>, Option<bool>, Option<String>, Option<String>) {
    sqlx::query_as(
        "SELECT capabilities::json, wants_listener, protocol_version, dialect \
         FROM mcp_servers WHERE id = $1",
    )
    .bind(SRV)
    .fetch_one(pool)
    .await
    .unwrap()
}

pub(crate) async fn answer_list(mcp: &MockServer, rpc: &str, key: &str, item: &str) {
    Mock::given(method("POST"))
        .and(body_string_contains(format!("\"method\":\"{rpc}\"")))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "jsonrpc": "2.0", "id": 2, "result": {key: [{"name": item}]},
        })))
        .mount(mcp)
        .await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_first_exchange_discovers_a_stateless_server(pool: PgPool) {
    let w = world(&pool).await;
    idp_answers(&w.idp).await;
    Mock::given(method("POST"))
        .and(body_string_contains("\"method\":\"server/discover\""))
        .and(header(
            "authorization",
            format!("Bearer {UPSTREAM_ACCESS}").as_str(),
        ))
        .and(header("mcp-method", "server/discover"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "supportedVersions": ["2025-01-01", "2026-07-28"],
                "ttlMs": 60000,
                "capabilities": {"extensions": {"io.modelcontextprotocol/tasks": {}}},
            },
        })))
        .mount(&w.mcp)
        .await;
    answer_list(&w.mcp, "tools/list", "tools", "t1").await;
    answer_list(&w.mcp, "resources/list", "resources", "r1").await;
    answer_list(&w.mcp, "prompts/list", "prompts", "p1").await;
    answer_list(
        &w.mcp,
        "resources/templates/list",
        "resourceTemplates",
        "rt1",
    )
    .await;

    let code = unique_code();
    let (s, b, _) = send(&w.app, token_req(&code_form(&code))).await;
    assert_eq!(s, StatusCode::OK, "{b}");

    let (caps, listener, version, dialect) = discovered(&pool).await;
    let caps = caps.expect("capabilities");
    assert_eq!(caps["tools"][0]["name"], "t1");
    assert_eq!(caps["resource_templates"][0]["name"], "rt1");
    assert_eq!(caps["_brutor"]["ttl_ms"], 60000);
    assert!(
        caps["extensions"]
            .get("io.modelcontextprotocol/tasks")
            .is_some()
    );
    assert_eq!(listener, Some(false), "stateless servers have no listener");
    assert_eq!(version.as_deref(), Some("2026-07-28"));
    assert_eq!(dialect.as_deref(), Some("stateless"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_first_exchange_falls_back_to_the_legacy_handshake(pool: PgPool) {
    let w = world(&pool).await;
    idp_answers(&w.idp).await;
    Mock::given(method("POST"))
        .and(body_string_contains("\"method\":\"server/discover\""))
        .respond_with(ResponseTemplate::new(404))
        .mount(&w.mcp)
        .await;
    Mock::given(method("POST"))
        .and(body_string_contains("\"method\":\"initialize\""))
        .respond_with(
            ResponseTemplate::new(200)
                .insert_header("mcp-session-id", "sess-1")
                .set_body_json(json!({
                    "jsonrpc": "2.0", "id": 1,
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {"tools": {"listChanged": true}},
                    },
                })),
        )
        .mount(&w.mcp)
        .await;
    Mock::given(method("POST"))
        .and(body_string_contains("notifications/initialized"))
        .and(header("mcp-session-id", "sess-1"))
        .respond_with(ResponseTemplate::new(202))
        .expect(1)
        .mount(&w.mcp)
        .await;
    // An SSE answer to a plain request, as some servers send.
    Mock::given(method("POST"))
        .and(body_string_contains("\"method\":\"tools/list\""))
        .and(header("mcp-session-id", "sess-1"))
        .respond_with(ResponseTemplate::new(200).set_body_raw(
            "event: progress\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"note\"}\n\n\
             data: {\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{\"tools\":[{\"name\":\"sse-tool\"}]}}\n\n",
            "text/event-stream",
        ))
        .mount(&w.mcp)
        .await;
    // The other lists are unsupported: best-effort, empty.
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(405))
        .mount(&w.mcp)
        .await;

    let code = unique_code();
    let (s, b, _) = send(&w.app, token_req(&code_form(&code))).await;
    assert_eq!(s, StatusCode::OK, "{b}");

    let (caps, listener, version, dialect) = discovered(&pool).await;
    let caps = caps.expect("capabilities");
    assert_eq!(caps["tools"][0]["name"], "sse-tool");
    assert_eq!(caps["resources"], json!([]));
    assert_eq!(listener, Some(true), "listChanged wants the listener");
    assert_eq!(version.as_deref(), Some("2025-03-26"));
    assert_eq!(dialect.as_deref(), Some("handshake"));
}

/// A broken upstream must not cost the user their authorization.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_server_that_cannot_be_discovered_does_not_fail_the_exchange(pool: PgPool) {
    let w = world(&pool).await;
    idp_answers(&w.idp).await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_string("this is not json"))
        .mount(&w.mcp)
        .await;
    let code = unique_code();
    let (s, b, _) = send(&w.app, token_req(&code_form(&code))).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert_eq!(stored_tokens(&pool).await, 1);
    let (caps, _, _, _) = discovered(&pool).await;
    assert!(caps.is_none() || caps == Some(json!({})), "{caps:?}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_server_with_capabilities_is_not_discovered_again(pool: PgPool) {
    let w = world(&pool).await;
    idp_answers(&w.idp).await;
    sqlx::query("UPDATE mcp_servers SET capabilities = $1 WHERE id = $2")
        .bind(json!({"tools": [{"name": "known"}]}))
        .bind(SRV)
        .execute(&pool)
        .await
        .unwrap();
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(500))
        .expect(0)
        .mount(&w.mcp)
        .await;
    let code = unique_code();
    let (s, b, _) = send(&w.app, token_req(&code_form(&code))).await;
    assert_eq!(s, StatusCode::OK, "{b}");
}

// ── Refresh grant ──────────────────────────────────────────────────────

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_refresh_token_that_is_not_our_jwt_is_refused(pool: PgPool) {
    let w = world(&pool).await;
    let secret = common::TEST_JWT_SECRET;
    let cases = [
        ("garbage", "not-a-jwt".to_string(), StatusCode::UNAUTHORIZED),
        (
            "another signing key",
            forged_jwt(
                "a-completely-different-secret-of-sufficient-length",
                "brutor-agent-control",
                "mcp_auth",
                3600,
            ),
            StatusCode::UNAUTHORIZED,
        ),
        (
            "another issuer",
            forged_jwt(secret, "someone-else", "mcp_auth", 3600),
            StatusCode::UNAUTHORIZED,
        ),
        (
            "another token type",
            forged_jwt(secret, "brutor-agent-control", "portal", 3600),
            StatusCode::UNAUTHORIZED,
        ),
        (
            "a portal session",
            common::make_portal_jwt(1, "u", T),
            StatusCode::UNAUTHORIZED,
        ),
    ];
    for (what, token, expected) in cases {
        let (s, b, _) = send(
            &w.app,
            token_req(&[("grant_type", "refresh_token"), ("refresh_token", &token)]),
        )
        .await;
        assert_eq!(s, expected, "{what}: {b}");
        assert!(b.get("access_token").is_none(), "{what}: {b}");
    }
    let (s, _, _) = send(&w.app, token_req(&[("grant_type", "refresh_token")])).await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "missing refresh_token");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_jwt_for_another_server_cannot_refresh_here(pool: PgPool) {
    let w = world(&pool).await;
    seed_stored_token(&w.state, T, "idp-user-1", "srv-2", 3600, None, None).await;
    let jwt = mcp_jwt(&w.state, T, "idp-user-1", "srv-2");
    let (s, b, _) = send(
        &w.app,
        token_req(&[("grant_type", "refresh_token"), ("refresh_token", &jwt)]),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "{b}");
    assert!(b.to_string().contains("does not match"), "{b}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_expired_jwt_refreshes_while_the_stored_token_holds(pool: PgPool) {
    let w = world(&pool).await;
    seed_stored_token(&w.state, T, "idp-user-1", SRV, 3600, None, None).await;
    let expired = forged_jwt(
        common::TEST_JWT_SECRET,
        "brutor-agent-control",
        "mcp_auth",
        -3600,
    );
    assert!(
        w.state
            .oauth_token_manager
            .verify_mcp_jwt(&expired)
            .is_err(),
        "expired for normal use"
    );
    // No call to the IdP: the stored token is still good.
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(500))
        .expect(0)
        .mount(&w.idp)
        .await;
    let (s, b, _) = send(
        &w.app,
        token_req(&[("grant_type", "refresh_token"), ("refresh_token", &expired)]),
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert_eq!(b["token_info"]["auto_refresh"], false);
    let claims = w
        .state
        .oauth_token_manager
        .verify_mcp_jwt(b["access_token"].as_str().unwrap())
        .unwrap();
    assert_eq!(claims.user_id, "idp-user-1");
    assert!(!b.to_string().contains("stored-access"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn no_stored_token_means_no_new_jwt(pool: PgPool) {
    let w = world(&pool).await;
    let jwt = mcp_jwt(&w.state, T, "idp-user-1", SRV);
    let (s, b, _) = send(
        &w.app,
        token_req(&[("grant_type", "refresh_token"), ("refresh_token", &jwt)]),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_GATEWAY, "{b}");
    assert!(b.get("access_token").is_none());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_expired_stored_token_without_refresh_needs_reauthentication(pool: PgPool) {
    let w = world(&pool).await;
    seed_stored_token(&w.state, T, "idp-user-1", SRV, -60, None, None).await;
    let jwt = mcp_jwt(&w.state, T, "idp-user-1", SRV);
    let (s, b, _) = send(
        &w.app,
        token_req(&[("grant_type", "refresh_token"), ("refresh_token", &jwt)]),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_GATEWAY, "{b}");
    assert!(b.to_string().contains("re-authenticate"), "{b}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_expired_stored_token_is_refreshed_upstream_with_its_stored_credentials(pool: PgPool) {
    let w = world(&pool).await;
    seed_stored_token(
        &w.state,
        T,
        "idp-user-1",
        SRV,
        -60,
        Some("old-refresh"),
        Some("https://idp.test.local"),
    )
    .await;
    Mock::given(method("POST"))
        .and(path("/token"))
        .and(body_string_contains("grant_type=refresh_token"))
        .and(body_string_contains("refresh_token=old-refresh"))
        .and(body_string_contains("client_id=client-1"))
        .and(body_string_contains("client_secret=client-secret-1"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "access_token": "new-access", "expires_in": "1800",
        })))
        .expect(1)
        .mount(&w.idp)
        .await;
    let jwt = mcp_jwt(&w.state, T, "idp-user-1", SRV);
    let (s, b, _) = send(
        &w.app,
        token_req(&[("grant_type", "refresh_token"), ("refresh_token", &jwt)]),
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert_eq!(b["token_info"]["auto_refresh"], true);

    let got = w
        .state
        .oauth_token_manager
        .get_valid_token(&pool, None, "idp-user-1", SRV, T, IDP, false)
        .await;
    assert_eq!(got.as_deref(), Some("new-access"));
    // The IdP sent no new refresh token: the old one stays usable.
    let enc: Option<String> =
        sqlx::query_scalar("SELECT encrypted_refresh_token FROM oauth_tokens")
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(
        w.state.encryption.decrypt(&enc.unwrap()).unwrap(),
        "old-refresh"
    );
}

/// The IdP says the grant is gone: the token is revoked, not retried forever.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_401_from_the_idp_revokes_the_stored_token(pool: PgPool) {
    let w = world(&pool).await;
    seed_stored_token(&w.state, T, "idp-user-1", SRV, -60, Some("old"), None).await;
    Mock::given(method("POST"))
        .and(path("/token"))
        .respond_with(ResponseTemplate::new(401))
        .mount(&w.idp)
        .await;
    let jwt = mcp_jwt(&w.state, T, "idp-user-1", SRV);
    let (s, _, _) = send(
        &w.app,
        token_req(&[("grant_type", "refresh_token"), ("refresh_token", &jwt)]),
    )
    .await;
    assert_eq!(s, StatusCode::UNAUTHORIZED);
    let revoked: bool = sqlx::query_scalar("SELECT revoked FROM oauth_tokens")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert!(revoked);

    // And the same JWT now finds nothing to refresh.
    let (s, _, _) = send(
        &w.app,
        token_req(&[("grant_type", "refresh_token"), ("refresh_token", &jwt)]),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_GATEWAY);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_idp_outage_on_refresh_keeps_the_token_and_fails(pool: PgPool) {
    let w = world(&pool).await;
    seed_stored_token(&w.state, T, "idp-user-1", SRV, -60, Some("old"), None).await;
    Mock::given(method("POST"))
        .and(path("/token"))
        .respond_with(ResponseTemplate::new(503).set_body_string("maintenance"))
        .mount(&w.idp)
        .await;
    let jwt = mcp_jwt(&w.state, T, "idp-user-1", SRV);
    let (s, b, _) = send(
        &w.app,
        token_req(&[("grant_type", "refresh_token"), ("refresh_token", &jwt)]),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_GATEWAY, "{b}");
    let revoked: bool = sqlx::query_scalar("SELECT revoked FROM oauth_tokens")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert!(!revoked, "an outage is not a revocation");
}

/// SEP-2352 — a token minted under one issuer is never refreshed against
/// another, and is revoked rather than sent there.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_token_minted_under_another_issuer_is_revoked_not_refreshed(pool: PgPool) {
    let w = world(&pool).await;
    seed_stored_token(
        &w.state,
        T,
        "idp-user-1",
        SRV,
        -60,
        Some("old"),
        Some("https://previous-issuer.example"),
    )
    .await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200))
        .expect(0)
        .mount(&w.idp)
        .await;
    let jwt = mcp_jwt(&w.state, T, "idp-user-1", SRV);
    let (s, b, _) = send(
        &w.app,
        token_req(&[("grant_type", "refresh_token"), ("refresh_token", &jwt)]),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_GATEWAY, "{b}");
    assert!(b.to_string().contains("issuer changed"), "{b}");
    let revoked: bool = sqlx::query_scalar("SELECT revoked FROM oauth_tokens")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert!(revoked);
}

// ── Revocation and listing ─────────────────────────────────────────────

pub(crate) fn revoke_req(srv: &str, bearer: Option<&str>, form_body: &str) -> Request<Body> {
    let mut b = Request::builder()
        .method("POST")
        .uri(format!("/v1/proxy/oauth/{srv}/{IDP}/revoke"))
        .header("content-type", "application/x-www-form-urlencoded");
    if let Some(t) = bearer {
        b = b.header("authorization", format!("Bearer {t}"));
    }
    b.body(Body::from(form_body.to_string())).unwrap()
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn revoking_needs_a_valid_token_and_works_once(pool: PgPool) {
    let w = world(&pool).await;
    seed_stored_token(&w.state, T, "idp-user-1", SRV, 3600, None, None).await;
    let jwt = mcp_jwt(&w.state, T, "idp-user-1", SRV);

    let (s, _, _) = send(&w.app, revoke_req(SRV, None, "")).await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "no token at all");
    let (s, _, _) = send(&w.app, revoke_req(SRV, Some("garbage"), "")).await;
    assert_eq!(s, StatusCode::UNAUTHORIZED);
    let (s, _, _) = send(&w.app, revoke_req(SRV, None, "token=garbage")).await;
    assert_eq!(s, StatusCode::UNAUTHORIZED);
    assert!(
        w.state
            .oauth_token_manager
            .get_valid_token(&pool, None, "idp-user-1", SRV, T, IDP, false)
            .await
            .is_some(),
        "a refused revocation changes nothing"
    );

    let (s, b, _) = send(&w.app, revoke_req(SRV, Some(&jwt), "")).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert_eq!(b["revoked"], true);
    assert!(
        w.state
            .oauth_token_manager
            .get_valid_token(&pool, None, "idp-user-1", SRV, T, IDP, false)
            .await
            .is_none(),
        "a revoked token is not served"
    );
    let (s, _, _) = send(&w.app, revoke_req(SRV, Some(&jwt), "")).await;
    assert_eq!(s, StatusCode::NOT_FOUND, "nothing left to revoke");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn revoking_through_the_form_field_works(pool: PgPool) {
    let w = world(&pool).await;
    seed_stored_token(&w.state, T, "idp-user-1", SRV, 3600, None, None).await;
    let jwt = mcp_jwt(&w.state, T, "idp-user-1", SRV);
    let (s, b, _) = send(
        &w.app,
        revoke_req(SRV, None, &form(&[("token", jwt.as_str())])),
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{b}");
}

/// A JWT authorizes its own (tenant, user, server) and nothing else, or the
/// same IdP subject in another tenant could have their credential revoked.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_jwt_cannot_revoke_in_another_tenant_or_on_another_server(pool: PgPool) {
    let w = world(&pool).await;
    seed_stored_token(&w.state, T, "shared-subject", SRV, 3600, None, None).await;
    seed_stored_token(&w.state, T, "shared-subject", "srv-2", 3600, None, None).await;

    let foreign = mcp_jwt(&w.state, OTHER_T, "shared-subject", SRV);
    let (s, b, _) = send(&w.app, revoke_req(SRV, Some(&foreign), "")).await;
    assert_eq!(s, StatusCode::FORBIDDEN, "other tenant: {b}");

    let other_server = mcp_jwt(&w.state, T, "shared-subject", "srv-2");
    let (s, b, _) = send(&w.app, revoke_req(SRV, Some(&other_server), "")).await;
    assert_eq!(s, StatusCode::FORBIDDEN, "other server: {b}");

    let live: i64 = sqlx::query_scalar("SELECT count(*) FROM oauth_tokens WHERE NOT revoked")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(live, 2, "nothing was revoked");
}

pub(crate) fn list_req(bearer: Option<&str>) -> Request<Body> {
    let mut b = Request::builder()
        .method("GET")
        .uri(format!("/v1/proxy/oauth/{SRV}/{IDP}/tokens"));
    if let Some(t) = bearer {
        b = b.header("authorization", format!("Bearer {t}"));
    }
    b.body(Body::empty()).unwrap()
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn listing_needs_authentication_and_shows_metadata_only(pool: PgPool) {
    let w = world(&pool).await;
    seed_stored_token(&w.state, T, "idp-user-1", SRV, 3600, Some("r"), None).await;
    seed_stored_token(&w.state, T, "idp-user-2", SRV, 3600, None, None).await;

    let (s, _, _) = send(&w.app, list_req(None)).await;
    assert_eq!(s, StatusCode::UNAUTHORIZED);
    let (s, _, _) = send(&w.app, list_req(Some("garbage"))).await;
    assert_eq!(s, StatusCode::UNAUTHORIZED);

    let jwt = mcp_jwt(&w.state, T, "idp-user-1", SRV);
    let (s, b, _) = send(&w.app, list_req(Some(&jwt))).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    let tokens = b["tokens"].as_array().unwrap();
    assert_eq!(tokens.len(), 1, "another user's token is not listed: {b}");
    assert_eq!(tokens[0]["has_refresh_token"], true);
    assert_eq!(tokens[0]["scopes"], json!(["openid"]));
    let raw = b.to_string();
    assert!(!raw.contains("encrypted"), "{raw}");
    assert!(!raw.contains("stored-access"), "{raw}");
    assert!(!raw.contains("client-secret-1"), "{raw}");

    // An expired JWT is not a credential for this route (only for a refresh).
    let expired = forged_jwt(
        common::TEST_JWT_SECRET,
        "brutor-agent-control",
        "mcp_auth",
        -3600,
    );
    let (s, _, _) = send(&w.app, list_req(Some(&expired))).await;
    assert_eq!(s, StatusCode::UNAUTHORIZED);

    // A portal session lists what is stored under its gateway user id —
    // where portal sessions are a credential (the Portal extension). A
    // Community gateway refuses the token as it refuses any it cannot verify.
    common::seed_end_user(&pool, 8810, T).await;
    seed_stored_token(&w.state, T, "8810", SRV, 3600, None, None).await;
    let session = common::make_portal_jwt(8810, "user-8810", T);
    let (s, b, _) = send(&w.app, list_req(Some(&session))).await;
    if common::edition_composes("portal") {
        assert_eq!(s, StatusCode::OK, "{b}");
        assert_eq!(b["user_id"], "8810");
        assert_eq!(b["tokens"].as_array().unwrap().len(), 1);
    } else {
        assert_eq!(s, StatusCode::UNAUTHORIZED, "{b}");
    }
}

/// The JWT tenant is checked against the server's tenant before any listing.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_jwt_of_another_tenant_lists_nothing_here(pool: PgPool) {
    let w = world(&pool).await;
    seed_stored_token(&w.state, T, "shared-subject", SRV, 3600, None, None).await;
    let foreign = mcp_jwt(&w.state, OTHER_T, "shared-subject", SRV);
    let (s, b, _) = send(&w.app, list_req(Some(&foreign))).await;
    assert_eq!(s, StatusCode::UNAUTHORIZED, "{b}");
    assert!(b.get("tokens").is_none());
}

// ── Discovery documents ────────────────────────────────────────────────

pub(crate) fn discovery(doc: &str, extra: &[(&str, &str)]) -> Request<Body> {
    let mut b = Request::builder()
        .method("GET")
        .uri(format!("/v1/proxy/oauth/{SRV}/{IDP}/.well-known/{doc}"));
    for (k, v) in extra {
        b = b.header(*k, *v);
    }
    b.body(Body::empty()).unwrap()
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn protected_resource_metadata_points_through_the_gateway(pool: PgPool) {
    let w = world(&pool).await;
    let (s, b, _) = send(
        &w.app,
        discovery(
            "oauth-protected-resource",
            &[
                ("x-forwarded-proto", "https"),
                ("x-forwarded-host", "gw.example"),
            ],
        ),
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{b}");
    // Through the gateway at its public URL; the forwarded headers are not
    // believed (they were, whenever the public URL said localhost).
    assert_eq!(
        b["authorization_servers"][0],
        format!(
            "{}/v1/proxy/oauth/{SRV}/{IDP}",
            w.state.config.public_url.trim_end_matches('/')
        )
    );
    assert_eq!(b["resource"], w.mcp.uri());
    assert_eq!(b["bearer_methods_supported"], json!(["header"]));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_configured_public_url_beats_forwarded_headers(pool: PgPool) {
    let (idp, mcp) = (MockServer::start().await, MockServer::start().await);
    seed_world(&pool, &idp, &mcp).await;
    let mut config = AppConfig::for_test("postgresql://placeholder/replaced".to_string());
    config.public_url = "https://gateway.brutor.example/".to_string();
    let state: SharedState = Arc::new(AppState::new(
        config,
        pool.clone(),
        None,
        reqwest::Client::new(),
        TelemetryState {
            prometheus_handle: None,
        },
    ));
    let app = common::build_oauth_app(state);
    let (s, b, _) = send(
        &app,
        discovery(
            "openid-configuration",
            &[("x-forwarded-host", "attacker.example")],
        ),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    let raw = b.to_string();
    assert!(!raw.contains("attacker.example"), "{raw}");
    assert_eq!(
        b["token_endpoint"],
        format!("https://gateway.brutor.example/v1/proxy/oauth/{SRV}/{IDP}/token")
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_openid_document_rewrites_what_the_gateway_fronts_and_keeps_the_rest(pool: PgPool) {
    let w = world(&pool).await;
    sqlx::query(
        "UPDATE idp_servers SET cached_config = $1, jwks_uri = 'https://idp.test.local/jwks', \
         scopes_supported = $2, registration_endpoint = 'https://idp.test.local/reg' WHERE id = $3",
    )
    .bind(json!({
        "issuer": "https://idp.test.local",
        "token_endpoint": "https://idp.test.local/token",
        "authorization_response_iss_parameter_supported": true,
        "custom_field": "kept",
        "response_types_supported": ["code", "id_token"],
    }))
    .bind(json!(["openid", "tasks"]))
    .bind(IDP)
    .execute(&pool)
    .await
    .unwrap();

    let (s, b, _) = send(&w.app, discovery("openid-configuration", &[])).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    let gw = format!("http://localhost:8100/v1/proxy/oauth/{SRV}/{IDP}");
    assert_eq!(b["issuer"], gw);
    assert_eq!(b["token_endpoint"], format!("{gw}/token"));
    assert_eq!(b["authorization_endpoint"], format!("{gw}/authorize"));
    assert_eq!(b["registration_endpoint"], format!("{gw}/register"));
    assert_eq!(b["custom_field"], "kept");
    assert_eq!(b["response_types_supported"], json!(["code", "id_token"]));
    assert_eq!(b["jwks_uri"], "https://idp.test.local/jwks");
    assert_eq!(b["scopes_supported"], json!(["openid", "tasks"]));
    assert_eq!(b["code_challenge_methods_supported"], json!(["S256"]));
    assert_eq!(b["authorization_response_iss_parameter_supported"], false);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn no_authorize_endpoint_is_advertised_without_one_upstream(pool: PgPool) {
    let w = world(&pool).await;
    sqlx::query("UPDATE idp_servers SET authorization_endpoint = NULL WHERE id = $1")
        .bind(IDP)
        .execute(&pool)
        .await
        .unwrap();
    let (s, b, _) = send(&w.app, discovery("openid-configuration", &[])).await;
    assert_eq!(s, StatusCode::OK);
    assert!(b.get("authorization_endpoint").is_none(), "{b}");
    assert!(b.get("client_id_metadata_document_supported").is_none());
}

// ── Authorize ──────────────────────────────────────────────────────────

pub(crate) fn get_req(uri: &str) -> Request<Body> {
    Request::builder()
        .method("GET")
        .uri(uri)
        .body(Body::empty())
        .unwrap()
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn authorize_refuses_an_unknown_server_or_an_idp_with_no_endpoint(pool: PgPool) {
    let w = world(&pool).await;
    let (s, _, _) = send(
        &w.app,
        get_req(&format!(
            "/v1/proxy/oauth/srv-none/{IDP}/authorize?client_id=c"
        )),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    let (s, _, _) = send(
        &w.app,
        get_req(&format!(
            "/v1/proxy/oauth/{SRV}/idp-none/authorize?client_id=c"
        )),
    )
    .await;
    assert_eq!(s, StatusCode::NOT_FOUND);

    sqlx::query("UPDATE idp_servers SET authorization_endpoint = '' WHERE id = $1")
        .bind(IDP)
        .execute(&pool)
        .await
        .unwrap();
    let (s, _, _) = send(
        &w.app,
        get_req(&format!(
            "/v1/proxy/oauth/{SRV}/{IDP}/authorize?client_id=c"
        )),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn authorize_keeps_a_query_already_on_the_idp_endpoint(pool: PgPool) {
    let w = world(&pool).await;
    sqlx::query(
        "UPDATE idp_servers SET authorization_endpoint = 'https://idp.test.local/a?tenant=x' \
         WHERE id = $1",
    )
    .bind(IDP)
    .execute(&pool)
    .await
    .unwrap();
    let (s, _, h) = send(
        &w.app,
        get_req(&format!(
            "/v1/proxy/oauth/{SRV}/{IDP}/authorize?client_id=c1&state=s1"
        )),
    )
    .await;
    assert_eq!(s, StatusCode::TEMPORARY_REDIRECT);
    let loc = h["location"].to_str().unwrap();
    assert!(
        loc.starts_with("https://idp.test.local/a?tenant=x&"),
        "{loc}"
    );
    assert!(loc.contains("state=s1"), "{loc}");
}

/// SSRF: a CIMD document on a private or loopback address is never fetched,
/// and no redirect to the IdP is issued.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_cimd_document_on_a_blocked_address_is_refused(pool: PgPool) {
    let w = redis_world(&pool).await;
    let q = form(&[
        ("response_type", "code"),
        ("client_id", "https://127.0.0.1:9/client.json"),
        ("redirect_uri", "http://127.0.0.1:1/cb"),
        ("code_challenge", "abc"),
        ("code_challenge_method", "S256"),
    ]);
    let (s, b, h) = send(
        &w.app,
        get_req(&format!("/v1/proxy/oauth/{SRV}/{IDP}/authorize?{q}")),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "{b}");
    assert!(b.to_string().contains("blocked"), "{b}");
    assert!(!h.contains_key("location"));
}

// ── CIMD callback ──────────────────────────────────────────────────────

pub(crate) fn bridge(idp_secret_enc: Option<String>, verifier_challenge: &str) -> BridgeState {
    BridgeState {
        tenant_id: T.into(),
        mcp_server_id: SRV.into(),
        idp_server_id: IDP.into(),
        cimd_url: CIMD_URL.into(),
        client_redirect_uri: "https://cli.example.com/cb".into(),
        client_state: Some("client-state".into()),
        client_code_challenge: verifier_challenge.into(),
        gateway_verifier: "gateway-verifier".into(),
        gateway_callback: "https://gw.example/callback".into(),
        idp_client_id: "gateway-owned-client".into(),
        idp_client_secret_enc: idp_secret_enc,
    }
}

pub(crate) async fn put_bridge(b: &BridgeState) -> String {
    let id = cimd::random_token(24);
    let mut r = redis().await;
    cimd::redis_put_json(
        &mut r,
        &format!("{}{id}", cimd::BRIDGE_KEY_PREFIX),
        b,
        cimd::BRIDGE_TTL_SECS,
    )
    .await
    .unwrap();
    id
}

pub(crate) async fn put_code(cs: &CodeState) -> String {
    let code = format!("{}{}", cimd::GW_CODE_PREFIX, cimd::random_token(32));
    let mut r = redis().await;
    cimd::redis_put_json(
        &mut r,
        &format!("{}{code}", cimd::CODE_KEY_PREFIX),
        cs,
        cimd::CODE_TTL_SECS,
    )
    .await
    .unwrap();
    code
}

pub(crate) fn callback_uri(q: &str) -> String {
    format!("/v1/proxy/oauth/{SRV}/{IDP}/callback?{q}")
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_callback_fails_closed_without_redis(pool: PgPool) {
    let w = world(&pool).await;
    let (s, _, _) = send(&w.app, get_req(&callback_uri("state=x&code=y"))).await;
    assert_eq!(s, StatusCode::SERVICE_UNAVAILABLE);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_callback_needs_a_state_it_issued_and_each_state_works_once(pool: PgPool) {
    let w = redis_world(&pool).await;

    let (s, _, _) = send(&w.app, get_req(&callback_uri("code=y"))).await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "no state");
    let (s, _, h) = send(&w.app, get_req(&callback_uri("code=y&state=forged"))).await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "a state we never issued");
    assert!(!h.contains_key("location"));

    let id = put_bridge(&bridge(None, "ch")).await;
    let (s, b, h) = send(
        &w.app,
        get_req(&callback_uri(&format!("state={id}&code=idp-code"))),
    )
    .await;
    assert_eq!(s, StatusCode::TEMPORARY_REDIRECT, "{b}");
    let loc = h["location"].to_str().unwrap().to_string();
    assert!(
        loc.starts_with("https://cli.example.com/cb?code=gwc_"),
        "{loc}"
    );
    assert!(loc.contains("state=client-state"), "{loc}");
    assert!(
        !loc.contains("idp-code"),
        "the upstream code stays here: {loc}"
    );

    let (s, _, _) = send(
        &w.app,
        get_req(&callback_uri(&format!("state={id}&code=idp-code"))),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "a replayed state");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_callback_without_a_code_is_refused(pool: PgPool) {
    let w = redis_world(&pool).await;
    let id = put_bridge(&bridge(None, "ch")).await;
    let (s, _, h) = send(&w.app, get_req(&callback_uri(&format!("state={id}")))).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert!(!h.contains_key("location"));
}

/// RFC 6749 §4.1.2.1 — the IdP's refusal goes back to the client, with its
/// state, on the redirect the client registered.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_idp_error_returns_to_the_clients_redirect_with_its_state(pool: PgPool) {
    let w = redis_world(&pool).await;
    let id = put_bridge(&bridge(None, "ch")).await;
    let (s, _, h) = send(
        &w.app,
        get_req(&callback_uri(&format!(
            "state={id}&error=access_denied&error_description=no+thanks"
        ))),
    )
    .await;
    assert_eq!(s, StatusCode::TEMPORARY_REDIRECT);
    let loc = h["location"].to_str().unwrap();
    assert!(loc.starts_with("https://cli.example.com/cb?"), "{loc}");
    assert!(loc.contains("error=access_denied"), "{loc}");
    assert!(loc.contains("error_description=no+thanks"), "{loc}");
    assert!(loc.contains("state=client-state"), "{loc}");
}

/// A redirect URI that already has a query keeps it: a second `?` would
/// corrupt the client's own parameter and lose the error.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_redirect_with_a_query_stays_well_formed(pool: PgPool) {
    let w = redis_world(&pool).await;
    let mut b = bridge(None, "ch");
    b.client_redirect_uri = "https://cli.example.com/cb?tab=1".into();

    let id = put_bridge(&b).await;
    let (s, _, h) = send(
        &w.app,
        get_req(&callback_uri(&format!("state={id}&code=c"))),
    )
    .await;
    assert_eq!(s, StatusCode::TEMPORARY_REDIRECT);
    assert!(
        h["location"]
            .to_str()
            .unwrap()
            .starts_with("https://cli.example.com/cb?tab=1&code=gwc_"),
        "{:?}",
        h["location"]
    );

    let id = put_bridge(&b).await;
    let (s, _, h) = send(
        &w.app,
        get_req(&callback_uri(&format!("state={id}&error=access_denied"))),
    )
    .await;
    assert_eq!(s, StatusCode::TEMPORARY_REDIRECT);
    assert!(
        h["location"]
            .to_str()
            .unwrap()
            .starts_with("https://cli.example.com/cb?tab=1&error=access_denied"),
        "{:?}",
        h["location"]
    );
}

/// A bridge opened for one server and IdP is completed through that path.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_bridge_cannot_be_completed_through_another_servers_callback(pool: PgPool) {
    let w = redis_world(&pool).await;
    let id = put_bridge(&bridge(None, "ch")).await;
    let (s, _, h) = send(
        &w.app,
        get_req(&format!(
            "/v1/proxy/oauth/srv-other/{IDP}/callback?state={id}&code=c"
        )),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert!(!h.contains_key("location"));
}

// ── CIMD token leg ─────────────────────────────────────────────────────

pub(crate) const CLIENT_VERIFIER: &str = "client-pkce-verifier-0123456789";

pub(crate) fn cimd_form<'a>(code: &'a str, verifier: Option<&'a str>) -> Vec<(&'a str, &'a str)> {
    let mut f = vec![
        ("grant_type", "authorization_code"),
        ("code", code),
        ("client_id", CIMD_URL),
        ("redirect_uri", "https://cli.example.com/cb"),
    ];
    if let Some(v) = verifier {
        f.push(("code_verifier", v));
    }
    f
}

pub(crate) fn code_state(secret_enc: Option<String>) -> CodeState {
    CodeState {
        bridge: bridge(secret_enc, &cimd::s256_challenge(CLIENT_VERIFIER)),
        upstream_code: "the-idp-code".into(),
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_bridged_code_without_redis_fails_closed(pool: PgPool) {
    let w = world(&pool).await;
    let (s, _, _) = send(
        &w.app,
        token_req(&cimd_form("gwc_x", Some(CLIENT_VERIFIER))),
    )
    .await;
    assert_eq!(s, StatusCode::SERVICE_UNAVAILABLE);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_bridged_exchange_uses_the_gateway_leg_upstream(pool: PgPool) {
    let w = redis_world(&pool).await;
    let enc = w.state.encryption.encrypt("gateway-client-secret").unwrap();
    let code = put_code(&code_state(Some(enc))).await;
    Mock::given(method("POST"))
        .and(path("/token"))
        .and(body_string_contains("code=the-idp-code"))
        .and(body_string_contains("client_id=gateway-owned-client"))
        .and(body_string_contains("client_secret=gateway-client-secret"))
        .and(body_string_contains("code_verifier=gateway-verifier"))
        .and(body_string_contains(
            "redirect_uri=https%3A%2F%2Fgw.example%2Fcallback",
        ))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "access_token": UPSTREAM_ACCESS, "refresh_token": UPSTREAM_REFRESH,
        })))
        .expect(1)
        .mount(&w.idp)
        .await;
    Mock::given(method("GET"))
        .and(path("/userinfo"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"sub": "cimd-user"})))
        .mount(&w.idp)
        .await;

    let (s, b, _) = send(&w.app, token_req(&cimd_form(&code, Some(CLIENT_VERIFIER)))).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    let raw = b.to_string();
    assert!(!raw.contains(UPSTREAM_ACCESS) && !raw.contains(UPSTREAM_REFRESH));
    assert!(!raw.contains("gateway-client-secret"));
    assert_eq!(b["user"]["user_id"], "cimd-user");
}

/// RFC 7636: the code is bound to the client that started the flow. Each bad
/// proof consumes the code, so a guess gets one try.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_bridged_code_needs_the_right_client_and_pkce_proof(pool: PgPool) {
    let w = redis_world(&pool).await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200))
        .expect(0)
        .mount(&w.idp)
        .await;

    let code = put_code(&code_state(None)).await;
    let (s, b, _) = send(
        &w.app,
        token_req(&cimd_form(&code, Some("the-wrong-verifier"))),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "{b}");
    assert!(b.to_string().contains("PKCE"), "{b}");

    // The attempt spent the code: the right verifier is now too late.
    let (s, b, _) = send(&w.app, token_req(&cimd_form(&code, Some(CLIENT_VERIFIER)))).await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "{b}");
    assert!(b.to_string().contains("already-used"), "{b}");

    let code = put_code(&code_state(None)).await;
    let (s, b, _) = send(&w.app, token_req(&cimd_form(&code, None))).await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "{b}");
    assert!(b.to_string().contains("code_verifier"), "{b}");

    let code = put_code(&code_state(None)).await;
    let (s, b, _) = send(
        &w.app,
        token_req(&[
            ("grant_type", "authorization_code"),
            ("code", &code),
            ("client_id", "https://evil.example/client.json"),
            ("code_verifier", CLIENT_VERIFIER),
        ]),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "{b}");
    assert!(b.to_string().contains("does not match"), "{b}");

    let (s, _, _) = send(
        &w.app,
        token_req(&cimd_form("gwc_never-minted", Some(CLIENT_VERIFIER))),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert_eq!(stored_tokens(&pool).await, 0);
}

/// RFC 6749 §4.1.3 — the redirect the client names must be the one it used.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_bridged_code_is_refused_for_a_different_redirect_uri(pool: PgPool) {
    let w = redis_world(&pool).await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200))
        .expect(0)
        .mount(&w.idp)
        .await;
    let code = put_code(&code_state(None)).await;
    let (s, b, _) = send(
        &w.app,
        token_req(&[
            ("grant_type", "authorization_code"),
            ("code", &code),
            ("client_id", CIMD_URL),
            ("code_verifier", CLIENT_VERIFIER),
            ("redirect_uri", "https://evil.example/cb"),
        ]),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "{b}");
    assert!(b.to_string().contains("redirect_uri"), "{b}");
}

/// A code is redeemed where it was issued. Otherwise a code minted for one
/// tenant's server files the upstream credential under another's.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_bridged_code_cannot_be_redeemed_at_another_server_or_tenant(pool: PgPool) {
    let w = redis_world(&pool).await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200))
        .expect(0)
        .mount(&w.idp)
        .await;
    common::seed_tenant(&pool, OTHER_T).await;
    common::seed_mcp_server_with_base_url(&pool, "srv-other", OTHER_T, "other", &w.mcp.uri()).await;
    common::seed_idp(&pool, "idp-other", OTHER_T, None, None).await;
    sqlx::query("UPDATE idp_servers SET token_endpoint = $1 WHERE id = 'idp-other'")
        .bind(format!("{}/token", w.idp.uri()))
        .execute(&pool)
        .await
        .unwrap();

    let code = put_code(&code_state(None)).await;
    let (s, b, _) = send(
        &w.app,
        token_req_at(
            "srv-other",
            "idp-other",
            &cimd_form(&code, Some(CLIENT_VERIFIER)),
        ),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "other tenant: {b}");
    assert_eq!(stored_tokens(&pool).await, 0);

    common::seed_mcp_server_with_base_url(&pool, "srv-sibling", T, "sib", &w.mcp.uri()).await;
    let code = put_code(&code_state(None)).await;
    let (s, b, _) = send(
        &w.app,
        token_req_at("srv-sibling", IDP, &cimd_form(&code, Some(CLIENT_VERIFIER))),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "sibling server: {b}");
    assert_eq!(stored_tokens(&pool).await, 0);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_bridge_secret_that_cannot_be_decrypted_fails_closed(pool: PgPool) {
    let w = redis_world(&pool).await;
    let code = put_code(&code_state(Some("not-a-ciphertext".into()))).await;
    let (s, b, _) = send(&w.app, token_req(&cimd_form(&code, Some(CLIENT_VERIFIER)))).await;
    assert_eq!(s, StatusCode::INTERNAL_SERVER_ERROR, "{b}");
}

// ── Idempotency ────────────────────────────────────────────────────────

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_retried_exchange_gets_the_same_answer_without_a_second_idp_call(pool: PgPool) {
    let w = redis_world(&pool).await;
    idp_answers(&w.idp).await;
    let code = unique_code();
    let (s, first, _) = send(&w.app, token_req(&code_form(&code))).await;
    assert_eq!(s, StatusCode::OK, "{first}");
    let (s, second, _) = send(&w.app, token_req(&code_form(&code))).await;
    assert_eq!(s, StatusCode::OK, "{second}");
    assert_eq!(first, second, "the cached result");
    let token_calls = w
        .idp
        .received_requests()
        .await
        .unwrap()
        .iter()
        .filter(|r| r.url.path() == "/token")
        .count();
    assert_eq!(token_calls, 1);
}

/// The cached answer is a live JWT. An intercepted code alone must not fetch
/// it: only the caller that holds the same proof is "retrying".
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_replayed_code_with_another_proof_does_not_get_the_cached_token(pool: PgPool) {
    let w = redis_world(&pool).await;
    idp_answers(&w.idp).await;
    let code = unique_code();
    let (s, first, _) = send(&w.app, token_req(&code_form(&code))).await;
    assert_eq!(s, StatusCode::OK, "{first}");

    let stolen = [
        ("grant_type", "authorization_code"),
        ("code", code.as_str()),
        ("client_id", "client-1"),
        ("code_verifier", "attacker-guess"),
    ];
    w.idp.reset().await;
    Mock::given(method("POST"))
        .and(path("/token"))
        .respond_with(ResponseTemplate::new(400).set_body_json(json!({"error": "invalid_grant"})))
        .mount(&w.idp)
        .await;
    let (s, b, _) = send(&w.app, token_req(&stolen)).await;
    assert_eq!(s, StatusCode::BAD_GATEWAY, "{b}");
    assert!(b.get("access_token").is_none(), "{b}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_duplicate_in_flight_exchange_is_a_conflict(pool: PgPool) {
    let w = redis_world(&pool).await;
    let code = unique_code();
    let key =
        OAuthTokenManager::create_idempotency_key(IDP, &code, SRV, "client-1", Some("verifier-1"));
    let mut r = redis().await;
    assert!(
        OAuthTokenManager::check_idempotency(&mut r, &key)
            .await
            .unwrap()
            .is_none(),
        "the first caller proceeds"
    );
    let (s, b, _) = send(&w.app, token_req(&code_form(&code))).await;
    assert_eq!(s, StatusCode::CONFLICT, "{b}");
    assert_eq!(w.idp.received_requests().await.unwrap().len(), 0);
    OAuthTokenManager::cleanup_idempotency(&mut r, &key).await;
}

/// A failure releases the code, or the client's retry would see a 409.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_failed_exchange_can_be_retried(pool: PgPool) {
    let w = redis_world(&pool).await;
    let code = unique_code();
    Mock::given(method("POST"))
        .and(path("/token"))
        .respond_with(ResponseTemplate::new(500))
        .up_to_n_times(1)
        .mount(&w.idp)
        .await;
    let (s, _, _) = send(&w.app, token_req(&code_form(&code))).await;
    assert_eq!(s, StatusCode::BAD_GATEWAY);

    idp_answers(&w.idp).await;
    let (s, b, _) = send(&w.app, token_req(&code_form(&code))).await;
    assert_eq!(s, StatusCode::OK, "{b}");
}

/// The subject refusal too: it is a definite answer, repeated, not a 409.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_refused_subject_releases_the_code(pool: PgPool) {
    let w = redis_world(&pool).await;
    sqlx::query("UPDATE idp_servers SET userinfo_endpoint = NULL WHERE id = $1")
        .bind(IDP)
        .execute(&pool)
        .await
        .unwrap();
    idp_answers(&w.idp).await;
    let code = unique_code();
    for _ in 0..2 {
        let (s, b, _) = send(&w.app, token_req(&code_form(&code))).await;
        assert_eq!(s, StatusCode::BAD_REQUEST, "{b}");
    }
}

// ── Dynamic client registration ────────────────────────────────────────

pub(crate) fn register_req(tenant: Option<&str>, body: &str) -> Request<Body> {
    let mut b = Request::builder()
        .method("POST")
        .uri(format!("/v1/proxy/oauth/{SRV}/{IDP}/register"))
        .header("content-type", "application/json");
    if let Some(t) = tenant {
        b = b.header("x-tenant-id", t);
    }
    b.body(Body::from(body.to_string())).unwrap()
}

pub(crate) async fn with_registration(w: &World, pool: &PgPool) {
    sqlx::query("UPDATE idp_servers SET registration_endpoint = $1 WHERE id = $2")
        .bind(format!("{}/register", w.idp.uri()))
        .bind(IDP)
        .execute(pool)
        .await
        .unwrap();
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn registration_refusals(pool: PgPool) {
    let w = world(&pool).await;
    let (s, _, _) = send(&w.app, register_req(None, "{}")).await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "no tenant");
    let (s, _, _) = send(&w.app, register_req(Some(OTHER_T), "{}")).await;
    assert_eq!(s, StatusCode::NOT_FOUND, "an IdP of another tenant");
    let (s, _, _) = send(&w.app, register_req(Some(T), "{}")).await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "no registration endpoint");

    sqlx::query(
        "UPDATE idp_servers SET registration_endpoint = 'http://169.254.169.254/r' WHERE id = $1",
    )
    .bind(IDP)
    .execute(&pool)
    .await
    .unwrap();
    let (s, _, _) = send(&w.app, register_req(Some(T), "{}")).await;
    assert_eq!(s, StatusCode::FORBIDDEN, "a denied destination");
}

/// SEP-837 — a native client that names no type is not registered as a web
/// client; an explicit type is never overridden.
#[sqlx::test(migrations = "./tests/migrations")]
async fn registration_infers_the_application_type_only_when_absent(pool: PgPool) {
    let w = world(&pool).await;
    with_registration(&w, &pool).await;
    Mock::given(method("POST"))
        .and(path("/register"))
        .respond_with(ResponseTemplate::new(201).set_body_json(json!({"client_id": "new"})))
        .mount(&w.idp)
        .await;

    let cases = [
        (r#"{"redirect_uris":["http://localhost:9/cb"]}"#, "native"),
        (r#"{"redirect_uris":["http://127.0.0.1:9/cb"]}"#, "native"),
        (r#"{"redirect_uris":["com.app:/cb"]}"#, "native"),
        (r#"{"redirect_uris":["https://app.example/cb"]}"#, "web"),
        (r#"{"client_name":"none"}"#, "web"),
        (
            r#"{"application_type":"web","redirect_uris":["http://localhost:9/cb"]}"#,
            "web",
        ),
    ];
    for (i, (body, expected)) in cases.iter().enumerate() {
        let (s, b, _) = send(&w.app, register_req(Some(T), body)).await;
        assert_eq!(s, StatusCode::CREATED, "{b}");
        let got = &w.idp.received_requests().await.unwrap()[i];
        let sent: Value = serde_json::from_slice(&got.body).unwrap();
        assert_eq!(sent["application_type"], *expected, "{body}");
    }

    // Not JSON: forwarded as it came, for the IdP to refuse.
    let (s, _, _) = send(&w.app, register_req(Some(T), "not-json")).await;
    assert_eq!(s, StatusCode::CREATED);
    let got = w.idp.received_requests().await.unwrap();
    assert_eq!(got.last().unwrap().body, b"not-json");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_registration_error_passes_through_and_an_outage_is_a_502(pool: PgPool) {
    let w = world(&pool).await;
    with_registration(&w, &pool).await;
    Mock::given(method("POST"))
        .and(path("/register"))
        .respond_with(
            ResponseTemplate::new(400).set_body_json(json!({"error": "invalid_redirect_uri"})),
        )
        .mount(&w.idp)
        .await;
    let (s, b, _) = send(&w.app, register_req(Some(T), "{}")).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert_eq!(b["error"], "invalid_redirect_uri");

    sqlx::query("UPDATE idp_servers SET registration_endpoint = 'http://127.0.0.1:1/r'")
        .execute(&pool)
        .await
        .unwrap();
    let (s, _, _) = send(&w.app, register_req(Some(T), "{}")).await;
    assert_eq!(s, StatusCode::BAD_GATEWAY);
}

// ── The token manager on its own ───────────────────────────────────────

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_expired_mcp_jwt_fails_verify_and_passes_only_the_refresh_verify(pool: PgPool) {
    let state = common::build_test_state(pool);
    let m = &state.oauth_token_manager;
    let secret = common::TEST_JWT_SECRET;
    let expired = forged_jwt(secret, "brutor-agent-control", "mcp_auth", -3600);
    assert!(m.verify_mcp_jwt(&expired).is_err());
    assert!(m.verify_mcp_jwt_allow_expired(&expired).is_ok());

    for token in [
        forged_jwt(secret, "brutor-agent-control", "portal", 3600),
        forged_jwt(secret, "other", "mcp_auth", 3600),
        forged_jwt(
            "wrong-key-wrong-key-wrong-key-wrong-key-wrong-key",
            "brutor-agent-control",
            "mcp_auth",
            3600,
        ),
    ] {
        assert!(m.verify_mcp_jwt(&token).is_err(), "{token}");
        assert!(m.verify_mcp_jwt_allow_expired(&token).is_err(), "{token}");
    }
    let good = mcp_jwt(&state, T, "u", SRV);
    assert_eq!(m.verify_mcp_jwt(&good).unwrap().user_id, "u");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn get_valid_token_refreshes_only_when_allowed(pool: PgPool) {
    let w = world(&pool).await;
    let m = &w.state.oauth_token_manager;
    assert!(
        m.get_valid_token(&pool, None, "nobody", SRV, T, IDP, true)
            .await
            .is_none()
    );

    seed_stored_token(&w.state, T, "u1", SRV, -60, Some("r1"), None).await;
    assert!(
        m.get_valid_token(&pool, None, "u1", SRV, T, IDP, false)
            .await
            .is_none(),
        "auto_refresh off: an expired token is not served"
    );

    Mock::given(method("POST"))
        .and(path("/token"))
        .and(body_string_contains("refresh_token=r1"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "access_token": "fresh", "refresh_token": "r2",
        })))
        .expect(1)
        .mount(&w.idp)
        .await;
    assert_eq!(
        m.get_valid_token(&pool, Some("gw-7"), "u1", SRV, T, IDP, true)
            .await
            .as_deref(),
        Some("fresh")
    );
    // Now valid: no second refresh.
    assert_eq!(
        m.get_valid_token(&pool, None, "u1", SRV, T, IDP, true)
            .await
            .as_deref(),
        Some("fresh")
    );
    let (gw, refresh_count): (Option<String>, i32) =
        sqlx::query_as("SELECT gateway_user_id, refresh_count FROM oauth_tokens")
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(gw.as_deref(), Some("gw-7"));
    assert_eq!(refresh_count, 1);
}

/// Fail closed: a token that cannot be used is no token, and the failure is
/// raised as a downstream alert instead of being retried in silence.
#[sqlx::test(migrations = "./tests/migrations")]
async fn get_valid_token_returns_nothing_when_refresh_fails(pool: PgPool) {
    let w = world(&pool).await;
    let m = &w.state.oauth_token_manager;
    seed_stored_token(&w.state, T, "u1", SRV, -60, Some("r1"), None).await;

    Mock::given(method("POST"))
        .and(path("/token"))
        .respond_with(ResponseTemplate::new(500).set_body_string("boom"))
        .mount(&w.idp)
        .await;
    assert!(
        m.get_valid_token(&pool, None, "u1", SRV, T, IDP, true)
            .await
            .is_none()
    );

    // No expired token, no refresh token: nothing to try.
    seed_stored_token(&w.state, T, "u2", SRV, -60, None, None).await;
    assert!(
        m.get_valid_token(&pool, None, "u2", SRV, T, IDP, true)
            .await
            .is_none()
    );

    // A stored ciphertext that does not decrypt is not served as anything.
    seed_stored_token(&w.state, T, "u3", SRV, 3600, None, None).await;
    sqlx::query(
        "UPDATE oauth_tokens SET encrypted_access_token = 'garbage' WHERE idp_user_id = 'u3'",
    )
    .execute(&pool)
    .await
    .unwrap();
    assert!(
        m.get_valid_token(&pool, None, "u3", SRV, T, IDP, true)
            .await
            .is_none()
    );

    // A corrupt refresh token or client secret fails the refresh.
    seed_stored_token(&w.state, T, "u4", SRV, -60, Some("r4"), None).await;
    sqlx::query(
        "UPDATE oauth_tokens SET encrypted_refresh_token = 'garbage' WHERE idp_user_id = 'u4'",
    )
    .execute(&pool)
    .await
    .unwrap();
    assert!(
        m.get_valid_token(&pool, None, "u4", SRV, T, IDP, true)
            .await
            .is_none()
    );
    seed_stored_token(&w.state, T, "u5", SRV, -60, Some("r5"), None).await;
    sqlx::query(
        "UPDATE oauth_tokens SET encrypted_client_secret = 'garbage' WHERE idp_user_id = 'u5'",
    )
    .execute(&pool)
    .await
    .unwrap();
    assert!(
        m.get_valid_token(&pool, None, "u5", SRV, T, IDP, true)
            .await
            .is_none()
    );

    // A disabled IdP cannot refresh.
    seed_stored_token(&w.state, T, "u6", SRV, -60, Some("r6"), None).await;
    sqlx::query("UPDATE idp_servers SET enabled = false WHERE id = $1")
        .bind(IDP)
        .execute(&pool)
        .await
        .unwrap();
    assert!(
        m.get_valid_token(&pool, None, "u6", SRV, T, IDP, true)
            .await
            .is_none()
    );
}

/// The token lookup is keyed on the tenant.
#[sqlx::test(migrations = "./tests/migrations")]
async fn tokens_do_not_cross_tenants(pool: PgPool) {
    let w = world(&pool).await;
    let m = &w.state.oauth_token_manager;
    seed_stored_token(&w.state, T, "shared", SRV, 3600, None, None).await;
    assert!(
        m.get_valid_token(&pool, None, "shared", SRV, OTHER_T, IDP, true)
            .await
            .is_none()
    );
    assert!(
        !m.revoke_token(&pool, "shared", SRV, OTHER_T, IDP)
            .await
            .unwrap(),
        "another tenant's revocation touches nothing"
    );
    assert!(
        m.get_all_tokens_for_user(&pool, "shared", OTHER_T, None)
            .await
            .unwrap()
            .is_empty()
    );
    assert_eq!(
        m.get_all_tokens_for_user(&pool, "shared", T, None)
            .await
            .unwrap()
            .len(),
        1
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn storing_again_updates_the_one_row_and_keeps_what_the_idp_did_not_resend(pool: PgPool) {
    let w = world(&pool).await;
    seed_stored_token(&w.state, T, "u", SRV, 3600, Some("r1"), Some("iss-1")).await;
    sqlx::query("UPDATE oauth_tokens SET revoked = true, revoked_at = now()")
        .execute(&pool)
        .await
        .unwrap();
    seed_stored_token(&w.state, T, "u", SRV, 3600, None, None).await;
    let (n, revoked, count, issuer, refresh): (i64, bool, i32, Option<String>, Option<String>) =
        sqlx::query_as(
            "SELECT count(*) OVER (), revoked, refresh_count, issuer, encrypted_refresh_token \
             FROM oauth_tokens",
        )
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(n, 1);
    assert!(!revoked, "a new grant reinstates the row");
    assert_eq!(count, 1);
    assert_eq!(issuer.as_deref(), Some("iss-1"));
    assert_eq!(w.state.encryption.decrypt(&refresh.unwrap()).unwrap(), "r1");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn cleanup_respects_the_tenant(pool: PgPool) {
    let w = world(&pool).await;
    let m = &w.state.oauth_token_manager;
    common::seed_tenant(&pool, OTHER_T).await;
    seed_stored_token(&w.state, T, "live", SRV, 3600, Some("r"), None).await;
    seed_stored_token(&w.state, T, "old", SRV, -40 * 86_400, None, None).await;
    seed_stored_token(&w.state, OTHER_T, "old", SRV, -40 * 86_400, None, None).await;
    seed_stored_token(&w.state, T, "gone", SRV, 3600, None, None).await;
    m.revoke_token(&pool, "gone", SRV, T, IDP).await.unwrap();

    assert_eq!(
        m.cleanup_expired_tokens(&pool, Some(T), 30).await.unwrap(),
        1,
        "only this tenant's old row"
    );
    assert_eq!(
        m.cleanup_expired_tokens(&pool, None, 30).await.unwrap(),
        1,
        "then everyone's"
    );
    assert_eq!(m.cleanup_expired_tokens(&pool, None, 30).await.unwrap(), 0);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn user_info_parses_each_identity_shape_and_refuses_the_rest(pool: PgPool) {
    let state = common::build_test_state(pool);
    let m = &state.oauth_token_manager;
    let idp = MockServer::start().await;
    let at = format!("{}/ui", idp.uri());

    for (body, expect_id, expect_username) in [
        (json!({"sub": "s1", "login": "l"}), "s1", Some("l")),
        (json!({"id": 42}), "42", None),
        (json!({"id_str": "x"}), "x", None),
        (json!({"user": "bob"}), "bob", Some("bob")),
        (json!({"user_id": "uid"}), "uid", None),
        (
            json!({"userid": "uid2", "username": "n"}),
            "uid2",
            Some("n"),
        ),
        (
            json!({"hub_id": 9007199254740993_u64}),
            "9007199254740993",
            None,
        ),
    ] {
        idp.reset().await;
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(200).set_body_json(body.clone()))
            .mount(&idp)
            .await;
        let info = m.get_user_info("tok", &at).await.unwrap();
        assert_eq!(info.user_id, expect_id, "{body}");
        assert_eq!(info.username.as_deref(), expect_username, "{body}");
    }

    for (status, body) in [
        (200, json!({"email": "only@example.com"})),
        (200, json!({"sub": ""})),
        (500, json!({})),
    ] {
        idp.reset().await;
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(status).set_body_json(body.clone()))
            .mount(&idp)
            .await;
        assert!(m.get_user_info("tok", &at).await.is_err(), "{body}");
    }

    idp.reset().await;
    Mock::given(method("GET"))
        .respond_with(ResponseTemplate::new(200).set_body_string("not json"))
        .mount(&idp)
        .await;
    assert!(m.get_user_info("tok", &at).await.is_err());

    // A denied destination never gets the token.
    assert!(
        m.get_user_info("tok", "http://169.254.169.254/ui")
            .await
            .is_err()
    );
}

/// HubSpot puts the token in the URL. It goes there, not in a header, and it
/// must not surface in the error a failed call produces (that error is logged).
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_token_in_the_userinfo_url_stays_out_of_the_error(pool: PgPool) {
    let state = common::build_test_state(pool);
    let m = &state.oauth_token_manager;
    let idp = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/oauth/v1/access-tokens/secret-tok"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"hub_id": 7})))
        .mount(&idp)
        .await;
    let info = m
        .get_user_info(
            "secret-tok",
            &format!("{}/oauth/v1/access-tokens/{{token}}", idp.uri()),
        )
        .await
        .unwrap();
    assert_eq!(info.user_id, "7");
    let sent = &idp.received_requests().await.unwrap()[0];
    assert!(!sent.headers.contains_key("authorization"));

    let err = m
        .get_user_info("secret-tok", "http://127.0.0.1:1/at/{token}")
        .await
        .unwrap_err()
        .to_string();
    assert!(!err.contains("secret-tok"), "token in the error: {err}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn manager_token_calls_surface_transport_and_config_failures(pool: PgPool) {
    use brutor_gateway_core::models::db::IdpServer;
    let w = world(&pool).await;
    let m = &w.state.oauth_token_manager;
    let mut idp: IdpServer = sqlx::query_as("SELECT * FROM idp_servers WHERE id = $1")
        .bind(IDP)
        .fetch_one(&pool)
        .await
        .unwrap();

    idp.token_endpoint = None;
    assert!(
        m.exchange_authorization_code(&idp, "c", "i", None, None, None)
            .await
            .is_err()
    );
    assert!(
        m.refresh_token_with_idp(&idp, "r", "i", None)
            .await
            .is_err()
    );

    idp.token_endpoint = Some("http://127.0.0.1:1/token".into());
    let e = m
        .exchange_authorization_code(&idp, "c", "i", None, None, None)
        .await
        .unwrap_err();
    assert_eq!(e.status_code(), StatusCode::BAD_GATEWAY);
    let e = m
        .refresh_token_with_idp(&idp, "r", "i", None)
        .await
        .unwrap_err();
    assert_eq!(e.status_code(), StatusCode::BAD_GATEWAY);

    idp.token_endpoint = Some("http://169.254.169.254/token".into());
    assert!(
        m.exchange_authorization_code(&idp, "c", "i", None, None, None)
            .await
            .is_err()
    );
    assert!(
        m.refresh_token_with_idp(&idp, "r", "i", None)
            .await
            .is_err()
    );
}

#[test]
fn idempotency_keys_differ_by_every_part() {
    let base = OAuthTokenManager::create_idempotency_key("i", "c", "s", "cl", Some("p"));
    assert_eq!(
        base,
        OAuthTokenManager::create_idempotency_key("i", "c", "s", "cl", Some("p"))
    );
    for other in [
        OAuthTokenManager::create_idempotency_key("x", "c", "s", "cl", Some("p")),
        OAuthTokenManager::create_idempotency_key("i", "x", "s", "cl", Some("p")),
        OAuthTokenManager::create_idempotency_key("i", "c", "x", "cl", Some("p")),
        OAuthTokenManager::create_idempotency_key("i", "c", "s", "x", Some("p")),
        OAuthTokenManager::create_idempotency_key("i", "c", "s", "cl", Some("x")),
        OAuthTokenManager::create_idempotency_key("i", "c", "s", "cl", None),
    ] {
        assert_ne!(base, other);
    }
    assert!(base.starts_with("token_exchange:"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn idempotency_state_machine(_pool: PgPool) {
    let mut r = redis().await;
    let key = format!("token_exchange:test-{}", uuid::Uuid::new_v4());
    assert!(
        OAuthTokenManager::check_idempotency(&mut r, &key)
            .await
            .unwrap()
            .is_none()
    );
    assert_eq!(
        OAuthTokenManager::check_idempotency(&mut r, &key)
            .await
            .unwrap_err()
            .status_code(),
        StatusCode::CONFLICT
    );
    OAuthTokenManager::mark_exchange_completed(&mut r, &key, &json!({"access_token": "j"})).await;
    assert_eq!(
        OAuthTokenManager::check_idempotency(&mut r, &key)
            .await
            .unwrap(),
        Some(json!({"access_token": "j"}))
    );
    OAuthTokenManager::cleanup_idempotency(&mut r, &key).await;
    assert!(
        OAuthTokenManager::check_idempotency(&mut r, &key)
            .await
            .unwrap()
            .is_none()
    );
    OAuthTokenManager::cleanup_idempotency(&mut r, &key).await;
}

// ── The MCP proxy and the MCP JWT ──────────────

pub(crate) const SRV_Y: &str = "srv-oauth-y";

/// [`world`] plus a second OAuth server with its own upstream, both servers
/// in one resource group, and an API key of that group: the caller's gateway
/// credential, beside which the MCP JWT travels in `Authorization`.
pub(crate) async fn proxy_world(pool: &PgPool) -> (World, MockServer, Router, String) {
    let w = world(pool).await;
    let y = MockServer::start().await;
    common::seed_mcp_server_with_base_url(pool, SRV_Y, T, "oauth-y", &y.uri()).await;
    common::seed_resource_group(pool, "rg-oauth", T).await;
    common::seed_end_user(pool, 9811, T).await;
    common::add_direct_membership(pool, 9811, "rg-oauth", T).await;
    for (sc, srv) in [("sc-px-x", SRV), ("sc-px-y", SRV_Y)] {
        common::seed_server_config(pool, sc, T, srv).await;
        common::bind_server_config_to_rg(pool, sc, "rg-oauth").await;
    }
    let key = common::seed_api_key(pool, T, "rg-oauth", Some(9811), "k-oauth-proxy").await;
    for upstream in [&w.mcp, &y] {
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "jsonrpc": "2.0", "id": 1, "result": {"tools": []},
            })))
            .mount(upstream)
            .await;
    }
    let app = common::build_mcp_app(w.state.clone());
    (w, y, app, key)
}

/// A stored credential for `user` on `srv` whose access token is `access`.
pub(crate) async fn store_access(state: &SharedState, user: &str, srv: &str, access: &str) {
    state
        .oauth_token_manager
        .store_token(
            &state.db,
            None,
            user,
            srv,
            T,
            IDP,
            &brutor_gateway_core::services::oauth_token_manager::ExtractedTokens {
                access_token: access.into(),
                refresh_token: None,
                expires_in: 3600,
                token_type: "Bearer".into(),
                scope: None,
            },
            Some("client-1"),
            None,
            None,
        )
        .await
        .expect("store token");
}

/// `tools/list` on the proxy route of `srv`: the API key in `x-api-key`, the
/// MCP JWT (when there is one) as the bearer.
pub(crate) async fn proxy_list(
    app: &Router,
    key: &str,
    srv: &str,
    jwt: Option<&str>,
) -> (StatusCode, Value) {
    let mut req = common::build_post_request(
        &format!("/v1/proxy/mcp/{srv}"),
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}),
        None,
        Some(T),
    );
    req.headers_mut().insert("x-api-key", key.parse().unwrap());
    if let Some(jwt) = jwt {
        req.headers_mut()
            .insert("authorization", format!("Bearer {jwt}").parse().unwrap());
    }
    let (s, b, _) = send(app, req).await;
    (s, b)
}

/// The distinct `Authorization` values `upstream` received (the proxied call
/// and, when a credential was injected, the background capability discovery
/// that follows it). Empty when nothing reached it.
pub(crate) async fn upstream_authorizations(upstream: &MockServer) -> Vec<Option<String>> {
    let mut seen: Vec<Option<String>> = upstream
        .received_requests()
        .await
        .unwrap()
        .iter()
        .map(|r| {
            r.headers
                .get("authorization")
                .map(|v| v.to_str().unwrap().to_string())
        })
        .collect();
    seen.sort();
    seen.dedup();
    seen
}

/// The IdP subject the proxy stamped on the audit rows of `srv`.
pub(crate) async fn audited_idp_users(pool: &PgPool, srv: &str) -> Vec<Option<String>> {
    for _ in 0..100 {
        let rows: Vec<Option<String>> = sqlx::query_scalar(
            "SELECT idp_user_id FROM proxy_logs WHERE tenant_id = $1 AND server_id = $2",
        )
        .bind(T)
        .bind(srv)
        .fetch_all(pool)
        .await
        .unwrap();
        if !rows.is_empty() {
            return rows;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    panic!("no audit row for {srv}");
}

/// The positive case: the JWT of a server, on that server's route, injects
/// that server's stored credential and names its IdP user in the audit row.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_mcp_jwt_injects_its_own_servers_credential_on_its_own_route(pool: PgPool) {
    let (w, y, app, key) = proxy_world(&pool).await;
    store_access(&w.state, "idp-user-1", SRV, "x-access").await;
    store_access(&w.state, "idp-user-1", SRV_Y, "y-access").await;
    let jwt_y = mcp_jwt(&w.state, T, "idp-user-1", SRV_Y);

    let (s, b) = proxy_list(&app, &key, SRV_Y, Some(&jwt_y)).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert_eq!(
        upstream_authorizations(&y).await,
        vec![Some("Bearer y-access".to_string())]
    );
    assert!(upstream_authorizations(&w.mcp).await.is_empty());
    assert_eq!(
        audited_idp_users(&pool, SRV_Y).await,
        vec![Some("idp-user-1".to_string())]
    );
}

/// An MCP JWT speaks for the server it was minted
/// for. Sent on another server's route it is no credential there: the user's
/// upstream token for X is not handed to Y's upstream (nor is the user's own
/// token for Y, which this JWT does not name), and the audit row of Y carries
/// no IdP user taken from it.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_mcp_jwt_for_another_server_injects_nothing_on_this_route(pool: PgPool) {
    let (w, y, app, key) = proxy_world(&pool).await;
    store_access(&w.state, "idp-user-1", SRV, "x-access").await;
    store_access(&w.state, "idp-user-1", SRV_Y, "y-access").await;
    let jwt_x = mcp_jwt(&w.state, T, "idp-user-1", SRV);

    let (s, b) = proxy_list(&app, &key, SRV_Y, Some(&jwt_x)).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert_eq!(
        upstream_authorizations(&y).await,
        vec![None],
        "Y's upstream must receive no Authorization header"
    );
    assert_eq!(audited_idp_users(&pool, SRV_Y).await, vec![None]);
}

/// The same for the tenant: a JWT minted in another tenant names a subject of
/// that tenant's IdP. The same subject string here is another person.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_mcp_jwt_of_another_tenant_injects_nothing_on_this_route(pool: PgPool) {
    let (w, _y, app, key) = proxy_world(&pool).await;
    store_access(&w.state, "idp-user-1", SRV, "x-access").await;
    let foreign = mcp_jwt(&w.state, OTHER_T, "idp-user-1", SRV);

    let (s, b) = proxy_list(&app, &key, SRV, Some(&foreign)).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert_eq!(
        upstream_authorizations(&w.mcp).await,
        vec![None],
        "the upstream must receive no Authorization header"
    );
    assert_eq!(audited_idp_users(&pool, SRV).await, vec![None]);
}

// ── Server and IdP pairing; the IdP in the store ─

pub(crate) const IDP_B: &str = "idp-oauth-b";
pub(crate) const B_ACCESS: &str = "idp-b-access-token";

/// A second enabled IdP of the same tenant, with its own mock. It answers a
/// code exchange with its own access token and names the SAME subject as the
/// first IdP does (`idp-user-1`): two IdPs number their users independently.
/// Nothing links it to [`SRV`].
pub(crate) async fn second_idp(pool: &PgPool) -> MockServer {
    let b = MockServer::start().await;
    common::seed_idp(
        pool,
        IDP_B,
        T,
        Some("https://idp-b.test.local/authorize"),
        None,
    )
    .await;
    sqlx::query(
        "UPDATE idp_servers SET issuer = 'https://idp-b.test.local', token_endpoint = $1, \
         userinfo_endpoint = $2 WHERE id = $3",
    )
    .bind(format!("{}/token", b.uri()))
    .bind(format!("{}/userinfo", b.uri()))
    .bind(IDP_B)
    .execute(pool)
    .await
    .unwrap();
    b
}

pub(crate) async fn idp_b_answers(b: &MockServer) {
    Mock::given(method("POST"))
        .and(path("/token"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "access_token": B_ACCESS, "expires_in": 3600, "token_type": "Bearer",
        })))
        .mount(b)
        .await;
    Mock::given(method("GET"))
        .and(path("/userinfo"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"sub": "idp-user-1"})))
        .mount(b)
        .await;
}

pub(crate) async fn idp_b_is_never_called(b: &MockServer) {
    Mock::given(wiremock::matchers::any())
        .respond_with(ResponseTemplate::new(500))
        .expect(0)
        .mount(b)
        .await;
}

/// The path names the IdP, and for a PKCE client
/// nothing checked that the tenant configured that IdP for that server: any
/// enabled IdP of the tenant could be named for any server, and the credential
/// it issued was filed under the server.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_pkce_exchange_for_an_unpaired_server_and_idp_is_refused(pool: PgPool) {
    let w = world(&pool).await;
    let b = second_idp(&pool).await;
    idp_b_is_never_called(&b).await;
    let code = unique_code();
    let (s, body, _) = send(&w.app, token_req_at(SRV, IDP_B, &code_form(&code))).await;
    assert_eq!(s, StatusCode::FORBIDDEN, "{body}");
    assert!(
        body.to_string()
            .contains("not configured for this MCP server"),
        "{body}"
    );
    assert_eq!(stored_tokens(&pool).await, 0);
}

/// The same door for a client that brings its own secret: the secret proves
/// the client to the IdP, not that the IdP belongs to the server.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_client_secret_in_the_form_does_not_pair_a_server_with_an_idp(pool: PgPool) {
    let w = world(&pool).await;
    let b = second_idp(&pool).await;
    idp_b_is_never_called(&b).await;
    let code = unique_code();
    let mut pairs = no_pkce_form(&code);
    pairs.push(("client_secret", "its-own-secret"));
    let (s, body, _) = send(&w.app, token_req_at(SRV, IDP_B, &pairs)).await;
    assert_eq!(s, StatusCode::FORBIDDEN, "{body}");
    assert_eq!(stored_tokens(&pool).await, 0);
}

/// And for a CIMD-bridged code: the bridge binds the code to the pair it was
/// started on, which says nothing about whether that pair is configured.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_bridged_code_for_an_unpaired_server_and_idp_is_refused(pool: PgPool) {
    let w = redis_world(&pool).await;
    let b = second_idp(&pool).await;
    idp_b_is_never_called(&b).await;
    let mut cs = code_state(None);
    cs.bridge.idp_server_id = IDP_B.into();
    let code = put_code(&cs).await;
    let (s, body, _) = send(
        &w.app,
        token_req_at(SRV, IDP_B, &cimd_form(&code, Some(CLIENT_VERIFIER))),
    )
    .await;
    assert_eq!(s, StatusCode::FORBIDDEN, "{body}");
    assert_eq!(stored_tokens(&pool).await, 0);
}

/// What pairs them: the server's own `idp_id`, or an active OAuth client of
/// that IdP attached to the server. A dynamically registered public client
/// (its `client_id` is in no client row) works on a paired server either way.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_dynamic_pkce_client_works_on_a_server_paired_either_way(pool: PgPool) {
    let w = world(&pool).await;
    let b = second_idp(&pool).await;
    idp_b_answers(&b).await;
    let dynamic = |code: &'static str| {
        vec![
            ("grant_type", "authorization_code"),
            ("code", code),
            ("client_id", "dcr-client-the-idp-issued"),
            ("code_verifier", "verifier-1"),
        ]
    };

    // Paired by the server's `idp_id`.
    sqlx::query("UPDATE mcp_servers SET idp_id = $1 WHERE id = $2")
        .bind(IDP_B)
        .bind(SRV)
        .execute(&pool)
        .await
        .unwrap();
    let (s, body, _) = send(&w.app, token_req_at(SRV, IDP_B, &dynamic("code-pair-1"))).await;
    assert_eq!(s, StatusCode::OK, "{body}");

    // Paired by an active client of IdP B on the server, and by nothing else.
    sqlx::query("UPDATE mcp_servers SET idp_id = NULL WHERE id = $1")
        .bind(SRV)
        .execute(&pool)
        .await
        .unwrap();
    seed_client(&w.state, &pool, "public", None, true).await;
    sqlx::query("UPDATE oauth_app_clients SET idp_id = $1")
        .bind(IDP_B)
        .execute(&pool)
        .await
        .unwrap();
    let (s, body, _) = send(&w.app, token_req_at(SRV, IDP_B, &dynamic("code-pair-2"))).await;
    assert_eq!(s, StatusCode::OK, "{body}");

    // An inactive client pairs nothing.
    sqlx::query("UPDATE oauth_app_clients SET is_active = false")
        .execute(&pool)
        .await
        .unwrap();
    let (s, body, _) = send(&w.app, token_req_at(SRV, IDP_B, &dynamic("code-pair-3"))).await;
    assert_eq!(s, StatusCode::FORBIDDEN, "{body}");

    // Nor does an active client of ANOTHER IdP on the same server.
    sqlx::query("UPDATE oauth_app_clients SET is_active = true, idp_id = $1")
        .bind(IDP)
        .execute(&pool)
        .await
        .unwrap();
    let (s, body, _) = send(&w.app, token_req_at(SRV, IDP_B, &dynamic("code-pair-4"))).await;
    assert_eq!(s, StatusCode::FORBIDDEN, "{body}");
}

/// A credential of `idp` for `user` on [`SRV`], with everything a refresh
/// needs, as an exchange at that IdP would have stored it.
pub(crate) async fn store_for_idp(
    state: &SharedState,
    user: &str,
    idp: &str,
    access: &str,
    refresh: Option<&str>,
    expires_in: i64,
) -> Result<(), brutor_gateway_core::services::oauth_token_manager::OAuthError> {
    let issuer: String = sqlx::query_scalar("SELECT issuer FROM idp_servers WHERE id = $1")
        .bind(idp)
        .fetch_one(&state.db)
        .await
        .unwrap();
    state
        .oauth_token_manager
        .store_token(
            &state.db,
            None,
            user,
            SRV,
            T,
            idp,
            &brutor_gateway_core::services::oauth_token_manager::ExtractedTokens {
                access_token: access.into(),
                refresh_token: refresh.map(String::from),
                expires_in,
                token_type: "Bearer".into(),
                scope: None,
            },
            Some(&format!("client-of-{idp}")),
            refresh.map(|_| "secret-of-the-first-client"),
            Some(&issuer),
        )
        .await
}

type StoredRow = (
    String,
    Option<String>,
    String,
    Option<String>,
    Option<String>,
    Option<String>,
    i32,
    bool,
);

/// idp_id, issuer, access (decrypted), refresh (decrypted), client secret
/// (decrypted), client_id, refresh_count, revoked — of the one stored row.
pub(crate) async fn the_stored_row(state: &SharedState) -> StoredRow {
    let rows = stored_rows(state, None).await;
    assert_eq!(rows.len(), 1, "exactly one stored row");
    rows.into_iter().next().unwrap()
}

/// The same, of the one row `idp` holds.
pub(crate) async fn the_stored_row_of(state: &SharedState, idp: &str) -> StoredRow {
    let rows = stored_rows(state, Some(idp)).await;
    assert_eq!(rows.len(), 1, "exactly one stored row of {idp}");
    rows.into_iter().next().unwrap()
}

async fn stored_rows(state: &SharedState, idp: Option<&str>) -> Vec<StoredRow> {
    let rows: Vec<StoredRow> = sqlx::query_as(
        "SELECT idp_id, issuer, encrypted_access_token, encrypted_refresh_token,          encrypted_client_secret, client_id, refresh_count, revoked FROM oauth_tokens          WHERE $1::text IS NULL OR idp_id = $1 ORDER BY id",
    )
    .bind(idp)
    .fetch_all(&state.db)
    .await
    .unwrap();
    let dec = |v: String| state.encryption.decrypt(&v).unwrap();
    rows.into_iter()
        .map(
            |(idp_id, issuer, access, refresh, secret, client_id, count, revoked)| {
                (
                    idp_id,
                    issuer,
                    dec(access),
                    refresh.map(dec),
                    secret.map(dec),
                    client_id,
                    count,
                    revoked,
                )
            },
        )
        .collect()
}

/// Every lookup names the IdP. The same subject
/// string under a second IdP is another person: they do not get the first
/// IdP's credential served, refreshed, revoked, listed or stamped.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_stored_credential_serves_only_the_idp_that_issued_it(pool: PgPool) {
    let w = world(&pool).await;
    let _b = second_idp(&pool).await;
    let m = &w.state.oauth_token_manager;
    store_for_idp(&w.state, "u", IDP, "a-access", None, 3600)
        .await
        .unwrap();

    assert!(
        m.get_valid_token(&pool, None, "u", SRV, T, IDP_B, true)
            .await
            .is_none(),
        "the second IdP's subject is served the first IdP's access token"
    );
    assert_eq!(
        m.get_valid_token(&pool, None, "u", SRV, T, IDP, true)
            .await
            .as_deref(),
        Some("a-access")
    );

    m.record_agent_use(&pool, T, "u", SRV, IDP_B, "agent-of-b")
        .await;
    let actor: Option<String> = sqlx::query_scalar("SELECT last_actor_agent_id FROM oauth_tokens")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(actor, None, "another IdP's use is not this row's use");

    assert!(
        m.get_all_tokens_for_user(&pool, "u", T, Some(IDP_B))
            .await
            .unwrap()
            .is_empty()
    );
    assert_eq!(
        m.get_all_tokens_for_user(&pool, "u", T, Some(IDP))
            .await
            .unwrap()
            .len(),
        1
    );

    assert!(
        !m.revoke_token(&pool, "u", SRV, T, IDP_B).await.unwrap(),
        "the second IdP's subject revoked the first IdP's credential"
    );
    assert!(!the_stored_row(&w.state).await.7);
    assert!(m.revoke_token(&pool, "u", SRV, T, IDP).await.unwrap());
}

/// An expired credential is not refreshed for another IdP's subject either:
/// the first IdP's refresh token is not spent on their behalf.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_expired_credential_is_not_refreshed_for_another_idps_subject(pool: PgPool) {
    let w = world(&pool).await;
    let _b = second_idp(&pool).await;
    Mock::given(wiremock::matchers::any())
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"access_token": "fresh"})))
        .expect(0)
        .mount(&w.idp)
        .await;
    store_for_idp(&w.state, "u", IDP, "a-access", Some("r-a"), -60)
        .await
        .unwrap();
    assert!(
        w.state
            .oauth_token_manager
            .get_valid_token(&pool, None, "u", SRV, T, IDP_B, true)
            .await
            .is_none()
    );
    // Nor through the refresh grant with a JWT that names the other IdP.
    let jwt_b = mcp_jwt_of(&w.state, T, "u", SRV, IDP_B);
    let (s, b, _) = send(
        &w.app,
        token_req_at(
            SRV,
            IDP_B,
            &[("grant_type", "refresh_token"), ("refresh_token", &jwt_b)],
        ),
    )
    .await;
    assert_ne!(s, StatusCode::OK, "{b}");
    assert!(b.get("access_token").is_none(), "{b}");
}

/// The store's unique key includes the identity provider: the same subject
/// string at two identity providers is two people, and each holds their own
/// credential for one server. Storing the second writes nothing into the
/// first, each is served only to its own, and each one's re-grant renews its
/// own row.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_same_subject_at_two_idps_holds_two_independent_credentials(pool: PgPool) {
    let w = world(&pool).await;
    let _b = second_idp(&pool).await;
    let m = &w.state.oauth_token_manager;
    store_for_idp(&w.state, "u", IDP, "a-access", Some("r-a"), 3600)
        .await
        .unwrap();
    let a_before = the_stored_row(&w.state).await;

    store_for_idp(&w.state, "u", IDP_B, "b-access", None, 3600)
        .await
        .expect("the second identity provider stores its own credential");
    assert_eq!(
        the_stored_row_of(&w.state, IDP).await,
        a_before,
        "the second identity provider's credential changed the first one's"
    );
    let b = the_stored_row_of(&w.state, IDP_B).await;
    assert_eq!(b.1.as_deref(), Some("https://idp-b.test.local"));
    assert_eq!(b.2, "b-access");
    assert_eq!(
        b.3, None,
        "the first IdP's refresh token is in the second's row"
    );
    assert_eq!(
        b.4, None,
        "the first client's secret is in the second's row"
    );
    assert_eq!(b.5.as_deref(), Some("client-of-idp-oauth-b"));
    assert_eq!((b.6, b.7), (0, false));

    assert_eq!(
        m.get_valid_token(&pool, None, "u", SRV, T, IDP, true)
            .await
            .as_deref(),
        Some("a-access")
    );
    assert_eq!(
        m.get_valid_token(&pool, None, "u", SRV, T, IDP_B, true)
            .await
            .as_deref(),
        Some("b-access")
    );
    for idp in [IDP, IDP_B] {
        let listed = m
            .get_all_tokens_for_user(&pool, "u", T, Some(idp))
            .await
            .unwrap();
        assert_eq!(listed.len(), 1, "{idp} lists its own credential only");
        assert_eq!(listed[0].idp_id, idp);
    }

    // An agent's use is stamped on the credential it used.
    m.record_agent_use(&pool, T, "u", SRV, IDP_B, "agent-of-b")
        .await;
    let actors: Vec<(String, Option<String>)> =
        sqlx::query_as("SELECT idp_id, last_actor_agent_id FROM oauth_tokens ORDER BY idp_id")
            .fetch_all(&pool)
            .await
            .unwrap();
    assert_eq!(
        actors,
        vec![
            (IDP.to_string(), None),
            (IDP_B.to_string(), Some("agent-of-b".to_string()))
        ]
    );

    // Each identity provider's re-grant renews its own row and no other.
    store_for_idp(&w.state, "u", IDP, "a-access-2", None, 3600)
        .await
        .unwrap();
    let a = the_stored_row_of(&w.state, IDP).await;
    assert_eq!((a.0.as_str(), a.2.as_str()), (IDP, "a-access-2"));
    assert_eq!(a.3.as_deref(), Some("r-a"), "kept: the IdP sent none");
    assert_eq!(a.6, 1);
    assert_eq!(the_stored_row_of(&w.state, IDP_B).await, b);
}

/// Revoking one identity provider's credential leaves the other's: it is
/// still live, still served, and nothing of it changed. The revoked one stays
/// revoked while the other is stored or renewed, and a new authorization at
/// its own identity provider brings it back without touching the other.
#[sqlx::test(migrations = "./tests/migrations")]
async fn revoking_one_idps_credential_leaves_the_other_idps(pool: PgPool) {
    let w = world(&pool).await;
    let _b = second_idp(&pool).await;
    let m = &w.state.oauth_token_manager;
    store_for_idp(&w.state, "u", IDP, "a-access", Some("r-a"), 3600)
        .await
        .unwrap();
    store_for_idp(&w.state, "u", IDP_B, "b-access", Some("r-b"), 3600)
        .await
        .unwrap();
    let b_before = the_stored_row_of(&w.state, IDP_B).await;

    assert!(m.revoke_token(&pool, "u", SRV, T, IDP).await.unwrap());
    assert!(the_stored_row_of(&w.state, IDP).await.7);
    assert_eq!(
        the_stored_row_of(&w.state, IDP_B).await,
        b_before,
        "revoking the first IdP's credential changed the second IdP's"
    );
    assert!(
        m.get_valid_token(&pool, None, "u", SRV, T, IDP, true)
            .await
            .is_none(),
        "a revoked credential is served, or the other IdP's is served in its place"
    );
    assert_eq!(
        m.get_valid_token(&pool, None, "u", SRV, T, IDP_B, true)
            .await
            .as_deref(),
        Some("b-access")
    );
    assert!(
        !m.revoke_token(&pool, "u", SRV, T, IDP).await.unwrap(),
        "nothing live is left to revoke at the first IdP"
    );
    assert!(!the_stored_row_of(&w.state, IDP_B).await.7);

    // The second IdP's re-grant does not bring the first one's back.
    store_for_idp(&w.state, "u", IDP_B, "b-access-2", None, 3600)
        .await
        .unwrap();
    let a = the_stored_row_of(&w.state, IDP).await;
    assert!(a.7, "another IdP's grant revived a revoked credential");
    assert_eq!((a.2.as_str(), a.3.as_deref()), ("a-access", Some("r-a")));

    // The first IdP's own new authorization does, in its own row.
    store_for_idp(&w.state, "u", IDP, "a-access-2", None, 3600)
        .await
        .unwrap();
    let a = the_stored_row_of(&w.state, IDP).await;
    assert_eq!((a.2.as_str(), a.7), ("a-access-2", false));
    let b = the_stored_row_of(&w.state, IDP_B).await;
    assert_eq!(
        (b.2.as_str(), b.3.as_deref(), b.7),
        ("b-access-2", Some("r-b"), false)
    );
}

/// The number of requests `upstream` has received once no more arrive: a
/// background discovery started by an earlier call has finished.
async fn settled_request_count(upstream: &MockServer) -> usize {
    let mut seen = upstream.received_requests().await.unwrap().len();
    loop {
        tokio::time::sleep(std::time::Duration::from_millis(200)).await;
        let now = upstream.received_requests().await.unwrap().len();
        if now == seen {
            return seen;
        }
        seen = now;
    }
}

/// The whole flow: the tenant has two IdPs, one person authorised [`SRV`] at
/// the first, and a subject of the same name at the second completes a code
/// exchange for the same server. Both exchanges succeed, each JWT names its
/// own IdP, and on the proxy each JWT carries its own IdP's credential
/// upstream: neither replaced or reaches the other's.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_exchange_at_a_second_idp_stores_its_own_credential_beside_the_first(pool: PgPool) {
    let (w, _y, app, key) = proxy_world(&pool).await;
    let b = second_idp(&pool).await;
    idp_b_answers(&b).await;
    idp_answers(&w.idp).await;
    let oauth = common::build_oauth_app(w.state.clone());

    let (s, body, _) = send(&oauth, token_req(&code_form(&unique_code()))).await;
    assert_eq!(s, StatusCode::OK, "{body}");
    let first_jwt = body["access_token"].as_str().unwrap().to_string();
    let claims = w
        .state
        .oauth_token_manager
        .verify_mcp_jwt(&first_jwt)
        .unwrap();
    assert_eq!(claims.idp_id, IDP, "the JWT names the IdP of its subject");
    let first_subject = claims.user_id;

    // The tenant pairs the server with the second IdP as well.
    seed_client(&w.state, &pool, "public", None, true).await;
    sqlx::query("UPDATE oauth_app_clients SET idp_id = $1")
        .bind(IDP_B)
        .execute(&pool)
        .await
        .unwrap();
    let (s, body, _) = send(&oauth, token_req_at(SRV, IDP_B, &code_form(&unique_code()))).await;
    assert_eq!(s, StatusCode::OK, "{body}");
    assert!(!body.to_string().contains(B_ACCESS), "{body}");
    let second_jwt = body["access_token"].as_str().unwrap().to_string();
    let claims = w
        .state
        .oauth_token_manager
        .verify_mcp_jwt(&second_jwt)
        .unwrap();
    assert_eq!(claims.idp_id, IDP_B);
    assert_eq!(
        claims.user_id, first_subject,
        "the test is about one subject string at two IdPs"
    );

    let a_row = the_stored_row_of(&w.state, IDP).await;
    assert_eq!(a_row.2, UPSTREAM_ACCESS);
    assert_eq!(a_row.1.as_deref(), Some("https://idp.test.local"));
    let b_row = the_stored_row_of(&w.state, IDP_B).await;
    assert_eq!(b_row.2, B_ACCESS);
    assert_eq!(b_row.1.as_deref(), Some("https://idp-b.test.local"));

    // What each JWT carries upstream: the requests the upstream received
    // since the call started (the proxied call and the capability discovery
    // that follows it), all with that JWT's own IdP's credential.
    for (jwt, access) in [(&first_jwt, UPSTREAM_ACCESS), (&second_jwt, B_ACCESS)] {
        let before = settled_request_count(&w.mcp).await;
        let (s, body) = proxy_list(&app, &key, SRV, Some(jwt)).await;
        assert_eq!(s, StatusCode::OK, "{body}");
        settled_request_count(&w.mcp).await;
        let carried: Vec<Option<String>> = w.mcp.received_requests().await.unwrap()[before..]
            .iter()
            .map(|r| {
                r.headers
                    .get("authorization")
                    .map(|v| v.to_str().unwrap().to_string())
            })
            .collect();
        assert!(!carried.is_empty(), "the upstream was not called");
        assert!(
            carried
                .iter()
                .all(|v| v.as_deref() == Some(&format!("Bearer {access}"))),
            "{carried:?}"
        );
    }

    // Revoking with the first JWT revokes the first IdP's credential only.
    let (s, body, _) = send(&oauth, revoke_req(SRV, Some(&first_jwt), "")).await;
    assert_eq!(s, StatusCode::OK, "{body}");
    assert!(the_stored_row_of(&w.state, IDP).await.7);
    assert!(!the_stored_row_of(&w.state, IDP_B).await.7);
    let (s, body) = proxy_list(&app, &key, SRV, Some(&first_jwt)).await;
    assert_eq!(s, StatusCode::UNAUTHORIZED, "{body}");
    let (s, body) = proxy_list(&app, &key, SRV, Some(&second_jwt)).await;
    assert_eq!(s, StatusCode::OK, "{body}");
}

/// On the proxy, a JWT that names another IdP than the stored credential's
/// finds no credential: the caller is told to re-authorise and the upstream
/// is not called with the first IdP's token.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_jwt_naming_another_idp_finds_no_credential_on_the_proxy(pool: PgPool) {
    let (w, _y, app, key) = proxy_world(&pool).await;
    let _b = second_idp(&pool).await;
    store_access(&w.state, "idp-user-1", SRV, "x-access").await;
    let jwt_b = mcp_jwt_of(&w.state, T, "idp-user-1", SRV, IDP_B);
    let (s, body) = proxy_list(&app, &key, SRV, Some(&jwt_b)).await;
    assert_eq!(s, StatusCode::UNAUTHORIZED, "{body}");
    assert_eq!(body["error"], "oauth_reauth_required", "{body}");
    assert!(upstream_authorizations(&w.mcp).await.is_empty());
}

/// No backwards compatibility: a JWT minted before the `idp_id` claim existed
/// names no IdP, so it stands for no credential. It is refused everywhere a
/// JWT is verified, live or expired.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_mcp_jwt_without_an_idp_is_refused(pool: PgPool) {
    let (w, _y, app, key) = proxy_world(&pool).await;
    store_access(&w.state, "idp-user-1", SRV, "x-access").await;
    let now = common::db_now().timestamp();
    let old = encode(
        &Header::default(),
        &json!({
            "sub": "idp-user-1", "iat": now, "exp": now + 3600,
            "iss": "brutor-agent-control", "jti": "j", "tenant_id": T,
            "user_id": "idp-user-1", "mcp_server_id": SRV, "type": "mcp_auth",
        }),
        &EncodingKey::from_secret(common::TEST_JWT_SECRET.as_bytes()),
    )
    .unwrap();
    let m = &w.state.oauth_token_manager;
    assert!(m.verify_mcp_jwt(&old).is_err());
    assert!(m.verify_mcp_jwt_allow_expired(&old).is_err());

    let (s, body) = proxy_list(&app, &key, SRV, Some(&old)).await;
    assert_eq!(s, StatusCode::OK, "{body}");
    assert_eq!(upstream_authorizations(&w.mcp).await, vec![None]);

    let oauth = common::build_oauth_app(w.state.clone());
    let (s, _, _) = send(
        &oauth,
        token_req(&[("grant_type", "refresh_token"), ("refresh_token", &old)]),
    )
    .await;
    assert_eq!(s, StatusCode::UNAUTHORIZED);
    let (s, _, _) = send(&oauth, revoke_req(SRV, Some(&old), "")).await;
    assert_eq!(s, StatusCode::UNAUTHORIZED);
    assert!(!the_stored_row(&w.state).await.7, "nothing was revoked");
}

/// The refresh and revoke routes name an IdP in their path. A JWT of another
/// IdP is not a credential there, as a JWT of another server is not.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_jwt_of_another_idp_cannot_refresh_revoke_or_list_here(pool: PgPool) {
    let w = world(&pool).await;
    let _b = second_idp(&pool).await;
    store_for_idp(&w.state, "u", IDP, "a-access", Some("r-a"), 3600)
        .await
        .unwrap();
    let jwt_b = mcp_jwt_of(&w.state, T, "u", SRV, IDP_B);

    // On the first IdP's routes.
    let (s, b, _) = send(
        &w.app,
        token_req(&[("grant_type", "refresh_token"), ("refresh_token", &jwt_b)]),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "{b}");
    assert!(b.get("access_token").is_none(), "{b}");
    let (s, b, _) = send(&w.app, revoke_req(SRV, Some(&jwt_b), "")).await;
    assert_eq!(s, StatusCode::FORBIDDEN, "{b}");
    assert!(!the_stored_row(&w.state).await.7, "nothing was revoked");

    let (s, b, _) = send(&w.app, list_req(Some(&jwt_b))).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert_eq!(b["tokens"], json!([]), "another IdP's subject: {b}");
    let jwt_a = mcp_jwt(&w.state, T, "u", SRV);
    let (_, b, _) = send(&w.app, list_req(Some(&jwt_a))).await;
    assert_eq!(b["tokens"].as_array().unwrap().len(), 1, "{b}");
}

// ── A refresh in flight, a revocation, and the other identity provider
//     ──────────────────────────────────────────────

/// The first IdP answers a refresh grant slowly: the window in which the
/// stored row can change under a refresh that has already read it.
async fn idp_refresh_is_slow(idp: &MockServer, answer: ResponseTemplate) {
    Mock::given(method("POST"))
        .and(path("/token"))
        .respond_with(answer.set_delay(std::time::Duration::from_millis(700)))
        .mount(idp)
        .await;
}

/// Start a refresh of `u`'s expired credential of the first IdP and return
/// once the IdP has the request (so the row was read before what follows).
async fn refresh_in_flight(w: &World) -> tokio::task::JoinHandle<Option<String>> {
    let state = w.state.clone();
    let handle = tokio::spawn(async move {
        state
            .oauth_token_manager
            .get_valid_token(&state.db, None, "u", SRV, T, IDP, true)
            .await
    });
    for _ in 0..100 {
        if !w.idp.received_requests().await.unwrap().is_empty() {
            return handle;
        }
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
    }
    panic!("the refresh never reached the IdP");
}

/// A revocation holds against a refresh that was already in
/// flight. The refresh wrote its result back through the code-exchange
/// upsert, which sets `revoked = false`: a credential revoked while the IdP
/// was answering came back to life, with a new access and refresh token.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_refresh_in_flight_does_not_bring_a_revoked_credential_back(pool: PgPool) {
    let w = world(&pool).await;
    idp_refresh_is_slow(
        &w.idp,
        ResponseTemplate::new(200).set_body_json(json!({
            "access_token": "fresh-access", "refresh_token": "fresh-refresh",
            "expires_in": 3600, "token_type": "Bearer",
        })),
    )
    .await;
    store_for_idp(&w.state, "u", IDP, "a-access", Some("r-a"), -60)
        .await
        .unwrap();

    let refresh = refresh_in_flight(&w).await;
    assert!(
        w.state
            .oauth_token_manager
            .revoke_token(&pool, "u", SRV, T, IDP)
            .await
            .unwrap()
    );
    assert_eq!(
        refresh.await.unwrap(),
        None,
        "a revoked credential's refresh was served"
    );

    let row = the_stored_row(&w.state).await;
    assert!(row.7, "the refresh brought the revoked credential back");
    assert_eq!(row.2, "a-access", "the refresh wrote over a revoked row");
    assert_eq!(row.3.as_deref(), Some("r-a"));
    assert!(
        w.state
            .oauth_token_manager
            .get_valid_token(&pool, None, "u", SRV, T, IDP, true)
            .await
            .is_none()
    );
}

/// A refresh of one identity provider's credential that is in flight while
/// that credential is revoked and the other identity provider stores its own
/// ends on its own row: told `401`, it revokes nothing of the other's.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_stale_refresh_cannot_revoke_another_idps_credential(pool: PgPool) {
    let w = world(&pool).await;
    let _b = second_idp(&pool).await;
    idp_refresh_is_slow(&w.idp, ResponseTemplate::new(401)).await;
    store_for_idp(&w.state, "u", IDP, "a-access", Some("r-a"), -60)
        .await
        .unwrap();
    let m = &w.state.oauth_token_manager;

    let refresh = refresh_in_flight(&w).await;
    assert!(m.revoke_token(&pool, "u", SRV, T, IDP).await.unwrap());
    store_for_idp(&w.state, "u", IDP_B, "b-access", None, 3600)
        .await
        .expect("the second identity provider stores its own credential");
    assert_eq!(refresh.await.unwrap(), None);

    assert!(the_stored_row_of(&w.state, IDP).await.7);
    assert!(
        !the_stored_row_of(&w.state, IDP_B).await.7,
        "the first IdP's stale refresh revoked the second IdP's credential"
    );
    assert_eq!(
        m.get_valid_token(&pool, None, "u", SRV, T, IDP_B, true)
            .await
            .as_deref(),
        Some("b-access")
    );
}

/// The same for a refresh that SUCCEEDS: its tokens belong to the first
/// IdP's revoked credential and are written nowhere, not into the revoked row
/// and not into the other identity provider's.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_stale_refresh_writes_nothing_into_another_idps_credential(pool: PgPool) {
    let w = world(&pool).await;
    let _b = second_idp(&pool).await;
    idp_refresh_is_slow(
        &w.idp,
        ResponseTemplate::new(200).set_body_json(json!({
            "access_token": "fresh-access", "refresh_token": "fresh-refresh",
            "expires_in": 3600, "token_type": "Bearer",
        })),
    )
    .await;
    store_for_idp(&w.state, "u", IDP, "a-access", Some("r-a"), -60)
        .await
        .unwrap();
    let m = &w.state.oauth_token_manager;

    let refresh = refresh_in_flight(&w).await;
    assert!(m.revoke_token(&pool, "u", SRV, T, IDP).await.unwrap());
    store_for_idp(&w.state, "u", IDP_B, "b-access", None, 3600)
        .await
        .expect("the second identity provider stores its own credential");
    assert_eq!(refresh.await.unwrap(), None);

    let b = the_stored_row_of(&w.state, IDP_B).await;
    assert_eq!(
        (b.2.as_str(), b.3.as_deref(), b.7),
        ("b-access", None, false),
        "the second IdP's row carries something of the first IdP's refresh"
    );
    let a = the_stored_row_of(&w.state, IDP).await;
    assert_eq!(
        (a.2.as_str(), a.3.as_deref(), a.7),
        ("a-access", Some("r-a"), true),
        "the refresh wrote into, or revived, the revoked credential"
    );
}

/// The statements a refresh ends with are keyed on the row id AND the
/// identity provider the refresh read. A row whose identity provider is no
/// longer that one (nothing in the gateway does this; another writer of the
/// table could) is neither revoked nor written by a refresh that read it
/// before.
async fn a_refresh_ends_only_on_a_row_still_held_by_the_idp_it_read(
    pool: &PgPool,
    answer: ResponseTemplate,
) {
    let w = world(pool).await;
    let _b = second_idp(pool).await;
    idp_refresh_is_slow(&w.idp, answer).await;
    store_for_idp(&w.state, "u", IDP, "a-access", Some("r-a"), -60)
        .await
        .unwrap();

    let refresh = refresh_in_flight(&w).await;
    sqlx::query("UPDATE oauth_tokens SET idp_id = $1")
        .bind(IDP_B)
        .execute(pool)
        .await
        .unwrap();
    assert_eq!(refresh.await.unwrap(), None);

    let row = the_stored_row(&w.state).await;
    assert_eq!(
        (row.0.as_str(), row.2.as_str(), row.3.as_deref(), row.7),
        (IDP_B, "a-access", Some("r-a"), false),
        "a refresh ended on a row that is no longer its identity provider's"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_refused_refresh_does_not_revoke_a_row_another_idp_holds_by_then(pool: PgPool) {
    a_refresh_ends_only_on_a_row_still_held_by_the_idp_it_read(&pool, ResponseTemplate::new(401))
        .await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_granted_refresh_does_not_write_a_row_another_idp_holds_by_then(pool: PgPool) {
    a_refresh_ends_only_on_a_row_still_held_by_the_idp_it_read(
        &pool,
        ResponseTemplate::new(200).set_body_json(json!({
            "access_token": "fresh-access", "refresh_token": "fresh-refresh",
            "expires_in": 3600, "token_type": "Bearer",
        })),
    )
    .await;
}
