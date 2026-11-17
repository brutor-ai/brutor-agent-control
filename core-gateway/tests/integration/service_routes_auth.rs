// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Route contract for the control-plane-facing routes (`/v1/audit/*`,
//! `/v1/assurance/*`).
//!
//! These were shipped with no authentication at all. The contract pinned
//! here: no credential → 401; a bare `X-Tenant-ID` header → 401; a
//! tenant-scoped API key or portal end-user token → 403; a tenant's shared
//! (`global`) key → 403 (a tenant admin mints those, and they used to
//! pass as the service key); the platform service key → allowed; a system admin session → allowed; a tenant
//! admin session → allowed only on tenant-scoped routes for its own
//! tenant; a logged-out admin session → 401 (proxy-side revocation).

use axum::Router;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use jsonwebtoken::{EncodingKey, Header, encode};
use sqlx::PgPool;
use tower::ServiceExt;
use tower_http::cors::CorsLayer;

use brutor_gateway_core::middleware;
use brutor_gateway_core::models::auth::GatewayJwtClaims;
use brutor_gateway_core::routes;
use brutor_gateway_core::services::SharedState;
use brutor_gateway_core::services::auth::clear_admin_session_cache;

use super::common::{build_test_state, seed_end_user, seed_resource_group, seed_tenant};

const TENANT: &str = "svc-auth-tenant";
const OTHER_TENANT: &str = "svc-auth-other";

fn build_app(state: SharedState) -> Router {
    Router::new()
        .nest("/v1/audit", routes::audit::router())
        .nest("/v1/assurance", routes::assurance_checks::router())
        .layer(middleware::tenant::TenantLayer::new(state.clone()))
        .layer(CorsLayer::permissive())
        .with_state(state)
}

fn jwt_secret(state: &SharedState) -> String {
    state.config.jwt_secret.clone()
}

fn now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs()
}

fn mint(state: &SharedState, claims: &GatewayJwtClaims) -> String {
    encode(
        &Header::default(),
        claims,
        &EncodingKey::from_secret(jwt_secret(state).as_bytes()),
    )
    .unwrap()
}

/// A system admin with an active session row, exactly as the control
/// plane's `unified_auth_service.py` writes it.
async fn seed_system_admin_session(pool: &PgPool, state: &SharedState, active: bool) -> String {
    let admin_id = format!("sysadm-{}", uuid::Uuid::new_v4().simple());
    let jti = uuid::Uuid::new_v4().to_string();
    sqlx::query(
        "INSERT INTO system_admins (id, username, email, password_hash, is_active, is_locked, \
         failed_login_attempts, permissions, totp_enabled) \
         VALUES ($1, $1, $1 || '@test.local', 'x', true, false, 0, '[]'::json, false)",
    )
    .bind(&admin_id)
    .execute(pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO system_admin_sessions (id, admin_id, token_hash, risk_score, expires_at, is_active) \
         VALUES ($1, $2, 'h', 0.0, NOW() + interval '1 hour', $3)",
    )
    .bind(&jti)
    .bind(&admin_id)
    .bind(active)
    .execute(pool)
    .await
    .unwrap();
    mint(
        state,
        &GatewayJwtClaims {
            typ: Some("admin".into()),
            user_id: Some(admin_id),
            admin_type: Some("system".into()),
            exp: Some(now() + 600),
            iat: Some(now()),
            jti: Some(jti),
            ..Default::default()
        },
    )
}

async fn seed_tenant_admin_session(pool: &PgPool, state: &SharedState, tenant: &str) -> String {
    let admin_id = format!("tenadm-{}", uuid::Uuid::new_v4().simple());
    let role_id = format!("role-{}", uuid::Uuid::new_v4().simple());
    let jti = uuid::Uuid::new_v4().to_string();
    sqlx::query(
        "INSERT INTO tenant_roles (id, tenant_id, name, is_system) VALUES ($1, $2, $1, false)",
    )
    .bind(&role_id)
    .bind(tenant)
    .execute(pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO tenant_admins (id, tenant_id, role_id, username, email, password_hash, \
         is_active, is_locked, failed_login_attempts, totp_enabled) \
         VALUES ($1, $2, $3, $1, $1 || '@test.local', 'x', true, false, 0, false)",
    )
    .bind(&admin_id)
    .bind(tenant)
    .bind(&role_id)
    .execute(pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO tenant_admin_sessions (id, tenant_id, admin_id, token_hash, risk_score, \
         expires_at, is_active) \
         VALUES ($1, $2, $3, 'h', 0.0, NOW() + interval '1 hour', true)",
    )
    .bind(&jti)
    .bind(tenant)
    .bind(&admin_id)
    .execute(pool)
    .await
    .unwrap();
    mint(
        state,
        &GatewayJwtClaims {
            typ: Some("admin".into()),
            user_id: Some(admin_id),
            tenant_id: Some(tenant.into()),
            admin_type: Some("tenant".into()),
            exp: Some(now() + 600),
            iat: Some(now()),
            jti: Some(jti),
            ..Default::default()
        },
    )
}

/// What a service route answers a portal end user's token: not a service
/// principal (403) where portal sessions are a credential (the Portal
/// extension); a credential nobody verifies (401) in a Community gateway.
fn end_user_refusal() -> StatusCode {
    if super::common::edition_composes("portal") {
        StatusCode::FORBIDDEN
    } else {
        StatusCode::UNAUTHORIZED
    }
}

/// What a system-scoped service route answers a system administrator with a
/// live session: served where system administrators are composed,
/// refused (401) by a Community gateway, which has none.
fn live_system_admin() -> StatusCode {
    if super::common::edition_composes("multi_tenant") {
        StatusCode::OK
    } else {
        StatusCode::UNAUTHORIZED
    }
}

fn portal_token(state: &SharedState, tenant: &str, user_id: i32) -> String {
    mint(
        state,
        &GatewayJwtClaims {
            typ: Some("portal".into()),
            user_id: Some(user_id.to_string()),
            tenant_id: Some(tenant.into()),
            user_type: Some("end_user".into()),
            exp: Some(now() + 600),
            iat: Some(now()),
            jti: Some(uuid::Uuid::new_v4().to_string()),
            ..Default::default()
        },
    )
}

async fn call(
    app: &Router,
    method: &str,
    path: &str,
    bearer: Option<&str>,
    tenant_header: Option<&str>,
    body: Option<serde_json::Value>,
) -> StatusCode {
    let mut req = Request::builder().method(method).uri(path);
    if let Some(b) = bearer {
        req = req.header("authorization", format!("Bearer {b}"));
    }
    if let Some(t) = tenant_header {
        req = req.header("x-tenant-id", t);
    }
    let mut req = match body {
        Some(v) => req
            .header("content-type", "application/json")
            .body(Body::from(v.to_string()))
            .unwrap(),
        None => req.body(Body::empty()).unwrap(),
    };
    req.extensions_mut().insert(test_peer());
    app.clone().oneshot(req).await.unwrap().status()
}

/// The peer address the server puts on every request.
fn test_peer() -> axum::extract::ConnectInfo<std::net::SocketAddr> {
    axum::extract::ConnectInfo(std::net::SocketAddr::from(([127, 0, 0, 1], 0)))
}

fn compile_body() -> serde_json::Value {
    serde_json::json!({ "expression": "run.turn_count > 0", "output": "boolean" })
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn audit_routes_refuse_anonymous_and_header_only_callers(pool: PgPool) {
    clear_admin_session_cache();
    seed_tenant(&pool, TENANT).await;
    let state = build_test_state(pool.clone());
    let app = build_app(state.clone());

    for (method, path) in [
        ("GET", "/v1/audit/chains"),
        ("POST", "/v1/audit/chains/some-chain/verify"),
        ("POST", "/v1/audit/export-manifest/sign"),
    ] {
        let body = (method == "POST" && path.ends_with("sign"))
            .then(|| serde_json::json!({ "kind": "export" }));
        assert_eq!(
            call(&app, method, path, None, None, body.clone()).await,
            StatusCode::UNAUTHORIZED,
            "{method} {path} with no credential"
        );
        assert_eq!(
            call(&app, method, path, None, Some(TENANT), body).await,
            StatusCode::UNAUTHORIZED,
            "{method} {path} with only X-Tenant-ID"
        );
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn audit_routes_refuse_tenant_scoped_keys_and_end_users(pool: PgPool) {
    clear_admin_session_cache();
    seed_tenant(&pool, TENANT).await;
    seed_resource_group(&pool, "svc-auth-rg", TENANT).await;
    seed_end_user(&pool, 9101, TENANT).await;
    let group_key = super::common::seed_api_key(&pool, TENANT, "svc-auth-rg", None, "grp").await;
    let state = build_test_state(pool.clone());
    let app = build_app(state.clone());
    let portal = portal_token(&state, TENANT, 9101);

    assert_eq!(
        call(
            &app,
            "GET",
            "/v1/audit/chains",
            Some(&group_key),
            Some(TENANT),
            None
        )
        .await,
        StatusCode::FORBIDDEN,
        "a group-scoped API key is not a service credential"
    );
    // A tenant's shared key (scope 'global', bound to a group) is what
    // a tenant admin creates as "shared access". It used to pass as the
    // control plane's service key on every system- and tenant-scoped route.
    let shared_key =
        super::common::seed_shared_group_api_key(&pool, TENANT, "svc-auth-rg", "shared").await;
    assert_eq!(
        call(
            &app,
            "GET",
            "/v1/audit/chains",
            Some(&shared_key),
            Some(TENANT),
            None
        )
        .await,
        StatusCode::FORBIDDEN,
        "a tenant's shared key is not the platform service key"
    );
    assert_eq!(
        call(
            &app,
            "GET",
            "/v1/audit/chains",
            Some(&portal),
            Some(TENANT),
            None
        )
        .await,
        end_user_refusal(),
        "a portal end-user token is not a service credential"
    );
    let tenant_admin = seed_tenant_admin_session(&pool, &state, TENANT).await;
    assert_eq!(
        call(
            &app,
            "GET",
            "/v1/audit/chains",
            Some(&tenant_admin),
            Some(TENANT),
            None
        )
        .await,
        StatusCode::FORBIDDEN,
        "audit chains are system-scoped; a tenant admin is refused"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn audit_routes_admit_the_service_key_and_a_system_admin(pool: PgPool) {
    clear_admin_session_cache();
    seed_tenant(&pool, TENANT).await;
    seed_resource_group(&pool, "svc-auth-rg", TENANT).await;
    // The platform service key, as the control plane seeds it.
    let service_key =
        super::common::seed_key(&pool, TENANT, None, true, "system-health-check").await;
    let state = build_test_state(pool.clone());
    let app = build_app(state.clone());

    assert_eq!(
        call(
            &app,
            "GET",
            "/v1/audit/chains",
            Some(&service_key),
            None,
            None
        )
        .await,
        StatusCode::OK
    );
    // Signing without a configured key is 501, never 401/403 — the gate
    // passed and the route answered on its own terms.
    let signed = call(
        &app,
        "POST",
        "/v1/audit/export-manifest/sign",
        Some(&service_key),
        None,
        Some(serde_json::json!({ "kind": "export" })),
    )
    .await;
    assert!(
        signed == StatusCode::OK
            || signed == StatusCode::NOT_IMPLEMENTED
            || signed == StatusCode::UNPROCESSABLE_ENTITY,
        "service key must pass the gate on sign, got {signed}"
    );

    let sysadmin = seed_system_admin_session(&pool, &state, true).await;
    assert_eq!(
        call(&app, "GET", "/v1/audit/chains", Some(&sysadmin), None, None).await,
        live_system_admin()
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_logged_out_admin_session_is_refused_at_the_proxy(pool: PgPool) {
    clear_admin_session_cache();
    seed_tenant(&pool, TENANT).await;
    let state = build_test_state(pool.clone());
    let app = build_app(state.clone());

    let revoked = seed_system_admin_session(&pool, &state, false).await;
    assert_eq!(
        call(&app, "GET", "/v1/audit/chains", Some(&revoked), None, None).await,
        StatusCode::UNAUTHORIZED,
        "an inactive session row must be refused even though the token verifies"
    );

    // And a live session that is deactivated afterwards is refused once the
    // 30 s answer cache is cleared (what logout/revoke does through time).
    let live = seed_system_admin_session(&pool, &state, true).await;
    assert_eq!(
        call(&app, "GET", "/v1/audit/chains", Some(&live), None, None).await,
        live_system_admin()
    );
    sqlx::query("UPDATE system_admin_sessions SET is_active = false")
        .execute(&pool)
        .await
        .unwrap();
    clear_admin_session_cache();
    assert_eq!(
        call(&app, "GET", "/v1/audit/chains", Some(&live), None, None).await,
        StatusCode::UNAUTHORIZED
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn assurance_routes_require_a_service_principal_for_the_named_tenant(pool: PgPool) {
    clear_admin_session_cache();
    seed_tenant(&pool, TENANT).await;
    seed_tenant(&pool, OTHER_TENANT).await;
    seed_resource_group(&pool, "svc-auth-rg", TENANT).await;
    seed_end_user(&pool, 9102, TENANT).await;
    // The platform service key, as the control plane seeds it.
    let service_key =
        super::common::seed_key(&pool, TENANT, None, true, "system-health-check").await;
    let state = build_test_state(pool.clone());
    let app = build_app(state.clone());
    let path = "/v1/assurance/checks/compile";

    assert_eq!(
        call(&app, "POST", path, None, None, Some(compile_body())).await,
        StatusCode::UNAUTHORIZED,
        "no tenant, no credential"
    );
    assert_eq!(
        call(&app, "POST", path, None, Some(TENANT), Some(compile_body())).await,
        StatusCode::UNAUTHORIZED,
        "X-Tenant-ID alone was the whole bug"
    );
    let portal = portal_token(&state, TENANT, 9102);
    assert_eq!(
        call(
            &app,
            "POST",
            path,
            Some(&portal),
            Some(TENANT),
            Some(compile_body())
        )
        .await,
        end_user_refusal(),
        "an end user of the tenant is not a service principal"
    );
    assert_eq!(
        call(
            &app,
            "POST",
            path,
            Some(&service_key),
            Some(TENANT),
            Some(compile_body())
        )
        .await,
        StatusCode::OK
    );

    let own_admin = seed_tenant_admin_session(&pool, &state, TENANT).await;
    assert_eq!(
        call(
            &app,
            "POST",
            path,
            Some(&own_admin),
            Some(TENANT),
            Some(compile_body())
        )
        .await,
        StatusCode::OK,
        "a tenant admin may compile checks for its own tenant"
    );
    assert_eq!(
        call(
            &app,
            "POST",
            path,
            Some(&own_admin),
            Some(OTHER_TENANT),
            Some(compile_body())
        )
        .await,
        StatusCode::FORBIDDEN,
        "…but not for another tenant"
    );

    let sysadmin = seed_system_admin_session(&pool, &state, true).await;
    assert_eq!(
        call(
            &app,
            "POST",
            path,
            Some(&sysadmin),
            Some(OTHER_TENANT),
            Some(compile_body())
        )
        .await,
        live_system_admin(),
        "a system admin may act for any tenant"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_2fa_challenge_token_is_not_a_credential_anywhere(pool: PgPool) {
    clear_admin_session_cache();
    seed_tenant(&pool, TENANT).await;
    let state = build_test_state(pool.clone());
    let app = build_app(state.clone());

    // Exactly the shape `unified_auth_service.py` mints after the password
    // step: signed with the shared secret, carrying `type`, no `jti`.
    #[derive(serde::Serialize)]
    struct Challenge<'a> {
        #[serde(rename = "type")]
        kind: &'a str,
        admin_id: &'a str,
        tenant_id: Option<&'a str>,
        admin_type: &'a str,
        iat: u64,
        exp: u64,
    }
    let challenge = encode(
        &Header::default(),
        &Challenge {
            kind: "2fa_challenge",
            admin_id: "adm-1",
            tenant_id: None,
            admin_type: "system",
            iat: now(),
            exp: now() + 300,
        },
        &EncodingKey::from_secret(jwt_secret(&state).as_bytes()),
    )
    .unwrap();

    assert_eq!(
        call(
            &app,
            "GET",
            "/v1/audit/chains",
            Some(&challenge),
            None,
            None
        )
        .await,
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        call(
            &app,
            "POST",
            "/v1/assurance/checks/compile",
            Some(&challenge),
            Some(TENANT),
            Some(compile_body())
        )
        .await,
        StatusCode::UNAUTHORIZED
    );
}

// ---------------------------------------------------------------------------
// The LLM admin routes
// ---------------------------------------------------------------------------
//
// `POST /v1/proxy/llm/admin/{model_id}/{reset-resilience,invalidate-cache,
// probe-catalog}` are the control plane's: it calls them after a model is
// saved or tested. They read no credential at all, so anyone who could
// reach the proxy port and name a tenant could declare a failing model
// healthy, flush a tenant's caches, or make the gateway call a provider's
// catalogue with the tenant's stored key. They now require the platform
// service key, the credential the control plane sends.

const ADMIN_MODEL: &str = "m-admin-routes";
const ADMIN_ROUTES: [&str; 3] = ["reset-resilience", "invalidate-cache", "probe-catalog"];

fn admin_path(action: &str) -> String {
    format!("/v1/proxy/llm/admin/{ADMIN_MODEL}/{action}")
}

async fn seed_admin_routes_world(pool: &PgPool) -> SharedState {
    clear_admin_session_cache();
    seed_tenant(pool, TENANT).await;
    seed_tenant(pool, OTHER_TENANT).await;
    seed_resource_group(pool, "svc-auth-rg", TENANT).await;
    seed_end_user(pool, 9150, TENANT).await;
    super::common::seed_llm_provider(pool, "prov-admin-routes", TENANT, "openai").await;
    super::common::seed_llm_model_with_api_url(
        pool,
        ADMIN_MODEL,
        TENANT,
        "prov-admin-routes",
        "test-gpt",
        "http://127.0.0.1:1",
    )
    .await;
    mark_unhealthy(pool).await;
    build_test_state(pool.clone())
}

async fn mark_unhealthy(pool: &PgPool) {
    sqlx::query("UPDATE llm_model SET health_status = 'unhealthy' WHERE id = $1")
        .bind(ADMIN_MODEL)
        .execute(pool)
        .await
        .unwrap();
}

async fn health(pool: &PgPool) -> Option<String> {
    sqlx::query_scalar("SELECT health_status FROM llm_model WHERE id = $1")
        .bind(ADMIN_MODEL)
        .fetch_one(pool)
        .await
        .unwrap()
}

/// Every credential that is not the platform service key, and no credential,
/// is refused on all three routes, and nothing is done for it.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_llm_admin_routes_refuse_everything_but_the_platform_service_key(pool: PgPool) {
    let state = seed_admin_routes_world(&pool).await;
    let app = super::common::build_llm_app(state.clone());
    let group_key = super::common::seed_api_key(&pool, TENANT, "svc-auth-rg", None, "grp").await;
    let shared_key =
        super::common::seed_shared_group_api_key(&pool, TENANT, "svc-auth-rg", "shared").await;
    let revoked_platform =
        super::common::seed_key(&pool, TENANT, None, true, "platform-revoked").await;
    sqlx::query("UPDATE api_keys SET revoked = true WHERE name = 'platform-revoked'")
        .execute(&pool)
        .await
        .unwrap();
    let portal = portal_token(&state, TENANT, 9150);
    let tenant_admin = seed_tenant_admin_session(&pool, &state, TENANT).await;
    let system_admin = seed_system_admin_session(&pool, &state, true).await;
    // An administrator's session is a credential where administrators are
    // composed (403: not the platform key) and no credential elsewhere.
    let system_admin_refusal = if super::common::edition_composes("multi_tenant") {
        StatusCode::FORBIDDEN
    } else {
        StatusCode::UNAUTHORIZED
    };

    for action in ADMIN_ROUTES {
        let path = admin_path(action);
        let cases: [(&str, Option<&str>, StatusCode); 8] = [
            ("no credential", None, StatusCode::UNAUTHORIZED),
            ("a group key", Some(&group_key), StatusCode::FORBIDDEN),
            (
                "a tenant's shared (global-scope) key",
                Some(&shared_key),
                StatusCode::FORBIDDEN,
            ),
            (
                "a revoked platform key",
                Some(&revoked_platform),
                StatusCode::UNAUTHORIZED,
            ),
            ("an end user's session", Some(&portal), end_user_refusal()),
            (
                "a tenant administrator's session",
                Some(&tenant_admin),
                StatusCode::FORBIDDEN,
            ),
            (
                "a system administrator's session",
                Some(&system_admin),
                system_admin_refusal,
            ),
            (
                "an unknown key",
                Some("sk_brutor_api_test_nobody_has_this_key"),
                StatusCode::UNAUTHORIZED,
            ),
        ];
        for (who, bearer, expected) in cases {
            assert_eq!(
                call(&app, "POST", &path, bearer, Some(TENANT), None).await,
                expected,
                "POST {path} with {who}"
            );
        }
        // No tenant named and no credential.
        assert_eq!(
            call(&app, "POST", &path, None, None, None).await,
            StatusCode::UNAUTHORIZED,
            "POST {path} with nothing"
        );
    }
    assert_eq!(
        health(&pool).await.as_deref(),
        Some("unhealthy"),
        "a refused caller reset the model's health"
    );
}

/// The platform service key, as the control plane sends it (the key in
/// `Authorization` and `X-API-Key`, the model's tenant in `X-Tenant-ID`), is
/// served on all three.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_llm_admin_routes_serve_the_platform_service_key(pool: PgPool) {
    let state = seed_admin_routes_world(&pool).await;
    let app = super::common::build_llm_app(state.clone());
    let platform = super::common::seed_key(&pool, TENANT, None, true, "platform").await;
    let as_the_control_plane_calls = |action: &str| {
        let mut req = Request::builder()
            .method("POST")
            .uri(admin_path(action))
            .header("content-type", "application/json")
            .header("x-tenant-id", TENANT)
            .header("authorization", format!("Bearer {platform}"))
            .header("x-api-key", &platform)
            .body(Body::empty())
            .unwrap();
        req.extensions_mut().insert(test_peer());
        req
    };

    let reset = app
        .clone()
        .oneshot(as_the_control_plane_calls("reset-resilience"))
        .await
        .unwrap();
    assert_eq!(reset.status(), StatusCode::OK);
    assert_eq!(health(&pool).await.as_deref(), Some("healthy"));

    let invalidate = app
        .clone()
        .oneshot(as_the_control_plane_calls("invalidate-cache"))
        .await
        .unwrap();
    assert_eq!(invalidate.status(), StatusCode::OK);

    // The provider has no catalogue URL: the route's own answer, which the
    // control plane reads as "fall back to the synthetic probe".
    let probe = app
        .clone()
        .oneshot(as_the_control_plane_calls("probe-catalog"))
        .await
        .unwrap();
    let probe_status = probe.status();
    let probe_body = http_body_util::BodyExt::collect(probe.into_body())
        .await
        .unwrap()
        .to_bytes();
    assert_eq!(
        probe_status,
        StatusCode::UNPROCESSABLE_ENTITY,
        "{}",
        String::from_utf8_lossy(&probe_body)
    );

    // A model of another tenant is not found for the tenant named.
    let mut other = Request::builder()
        .method("POST")
        .uri(admin_path("reset-resilience"))
        .header("x-tenant-id", OTHER_TENANT)
        .header("x-api-key", &platform)
        .body(Body::empty())
        .unwrap();
    other.extensions_mut().insert(test_peer());
    mark_unhealthy(&pool).await;
    let status = app.clone().oneshot(other).await.unwrap().status();
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(health(&pool).await.as_deref(), Some("unhealthy"));
}

/// A tenant administrator whose password was generated for it must change it
/// before anything else: the control plane answers 403 on its own routes,
/// and the gateway does not accept that administrator's session either
/// (otherwise the gateway would be the way around the rule). Once the
/// password is changed, the same session is admitted.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_tenant_admin_who_must_change_the_password_is_refused_until_it_is_changed(pool: PgPool) {
    clear_admin_session_cache();
    seed_tenant(&pool, TENANT).await;
    let state = build_test_state(pool.clone());
    let app = build_app(state.clone());
    let path = "/v1/assurance/checks/compile";
    let admin = seed_tenant_admin_session(&pool, &state, TENANT).await;
    sqlx::query("UPDATE tenant_admins SET password_change_required = true WHERE tenant_id = $1")
        .bind(TENANT)
        .execute(&pool)
        .await
        .unwrap();
    assert_eq!(
        call(
            &app,
            "POST",
            path,
            Some(&admin),
            Some(TENANT),
            Some(compile_body())
        )
        .await,
        StatusCode::UNAUTHORIZED,
        "a generated password not yet changed admits nothing at the gateway"
    );

    sqlx::query("UPDATE tenant_admins SET password_change_required = false WHERE tenant_id = $1")
        .bind(TENANT)
        .execute(&pool)
        .await
        .unwrap();
    clear_admin_session_cache();
    assert_eq!(
        call(
            &app,
            "POST",
            path,
            Some(&admin),
            Some(TENANT),
            Some(compile_body())
        )
        .await,
        StatusCode::OK
    );
}
