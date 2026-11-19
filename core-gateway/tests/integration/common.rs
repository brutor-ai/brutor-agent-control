// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Shared seed helpers + HTTP app builder for the DB-integration tests.
//!
//! Each helper inserts a minimal row — just the NOT NULL columns — so
//! tests can compose a tenant + resource group + user + end-user-group
//! topology in a few lines. All use `ON CONFLICT DO NOTHING` so they're
//! idempotent and safe to call from helper builders.

#![allow(dead_code)]

use std::net::SocketAddr;

use axum::Router;
use axum::body::Body;
use axum::extract::ConnectInfo;
use axum::http::Request;
use sqlx::PgPool;

use brutor_gateway_core::builder::{Extension, Gateway, GatewayBuilder};
use brutor_gateway_core::config::AppConfig;
use brutor_gateway_core::middleware;
use brutor_gateway_core::routes;
use brutor_gateway_core::services::telemetry::TelemetryState;
use brutor_gateway_core::services::{AppState, SharedState};
use tower_http::cors::CorsLayer;

// ---------------------------------------------------------------------------
// Test app construction
// ---------------------------------------------------------------------------

// --- The composition seam -------------------
//
// What a test state is composed of is decided here and nowhere else in this
// suite: [`compose`] is the one place a state built by a helper becomes a
// gateway. Every helper goes through it.
//
// This suite runs against the composition of the edition that runs it
// (`crate::composition::extensions`): none in the Community workspace, the
// Enterprise distribution's in the Enterprise workspace, which re-runs these
// same files: one enforcement implementation, run on both compositions.
// [`build_test_state`] is that composition. A test that is about the
// Community default whatever the edition asks for it by name
// ([`build_community_test_state`], [`community_app`]).

/// `state` assembled by the builder with `extensions`, in the order given:
/// the state and the application over it.
pub fn compose(state: AppState, extensions: Vec<Box<dyn Extension>>) -> Gateway {
    record_composition(&extensions);
    GatewayBuilder::assemble(state, extensions).expect("the composition assembles")
}

/// The accounting of compositions: with
/// `BRUTOR_TEST_COMPOSITION_LOG=<file>` set, every state this seam composes
/// appends `binary<TAB>test<TAB>extension names` to that file, so "which
/// composition did each test run against" is read from a run and not from
/// the source. Unset (the default), nothing is written.
pub fn record_composition(extensions: &[Box<dyn Extension>]) {
    use std::io::Write;
    let Ok(file) = std::env::var("BRUTOR_TEST_COMPOSITION_LOG") else {
        return;
    };
    let test = std::env::var("NEXTEST_TEST_NAME").unwrap_or_else(|_| {
        std::thread::current()
            .name()
            .unwrap_or("unnamed")
            .to_string()
    });
    let binary = std::env::var("NEXTEST_BINARY_ID").unwrap_or_default();
    let names: Vec<&str> = extensions.iter().map(|e| e.name()).collect();
    let line = format!("{binary}\t{test}\t[{}]\n", names.join(","));
    std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(file)
        .and_then(|mut f| f.write_all(line.as_bytes()))
        .expect("the composition log is writable");
}

#[path = "../support/roots.rs"]
mod roots;

/// The root of the Community crate: where `src/`, `tests/fixtures/` and
/// `tests/migrations/` are. A file this suite reads is named from here, not
/// from the working directory: the Enterprise workspace runs these same
/// tests from its own directory. Where it is: `tests/support/roots.rs`.
pub fn community_root() -> &'static std::path::Path {
    static ROOT: std::sync::OnceLock<std::path::PathBuf> = std::sync::OnceLock::new();
    ROOT.get_or_init(|| roots::tree("community").expect("the Community root"))
}

/// The root of the Enterprise workspace, where this run has one
/// (`tests/support/roots.rs`): `None` in a Community tree alone.
pub fn enterprise_root() -> Option<&'static std::path::Path> {
    static ROOT: std::sync::OnceLock<Option<std::path::PathBuf>> = std::sync::OnceLock::new();
    ROOT.get_or_init(|| roots::tree("enterprise")).as_deref()
}

/// A state as `AppState::new` leaves it. No Redis, no real upstream HTTP:
/// purely in-process.
pub fn new_state(pool: PgPool) -> AppState {
    AppState::new(
        AppConfig::for_test("postgresql://placeholder/replaced".to_string()),
        pool,
        None,
        reqwest::Client::new(),
        TelemetryState {
            prometheus_handle: None,
        },
    )
}

/// The composition of the edition running this suite over `pool`: state and
/// application.
pub fn edition_gateway(pool: PgPool) -> Gateway {
    compose(new_state(pool), crate::composition::extensions())
}

/// The application of the edition running this suite: what its binary
/// serves.
pub fn edition_app(pool: PgPool) -> Router {
    edition_gateway(pool).app
}

/// Does the edition running this suite compose the feature `name` (an
/// extension's name: `portal`, `multi_tenant`, …)? For the few cases that
/// hold one route to what each edition answers: a portal session or a system
/// administrator is refused as an unknown credential (401) by a Community
/// gateway and judged on its merits by an Enterprise one.
pub fn edition_composes(name: &str) -> bool {
    crate::composition::extensions()
        .iter()
        .any(|e| e.name() == name)
}

/// A Community assembly over `pool`: nothing installed, no extensions,
/// whatever edition runs the suite.
pub fn community_gateway(pool: PgPool) -> Gateway {
    compose(new_state(pool), Vec::new())
}

/// The application of a Community assembly.
pub fn community_app(pool: PgPool) -> Router {
    community_gateway(pool).app
}

/// Build a `SharedState` from a sqlx::test-supplied pool. No Redis, no
/// real upstream HTTP — purely in-process. The pool is wrapped in an
/// `Arc` and the state is ready to back a router.
///
/// The state of the edition running this suite ([`edition_gateway`]): in the
/// Community workspace a Community assembly, so the request-path hooks are
/// the Community defaults and a portal session is not a credential of it.
/// A human caller is an Enterprise matter; the tests here
/// sign in with an API key.
pub fn build_test_state(pool: PgPool) -> SharedState {
    edition_gateway(pool).state
}

/// A state as a Community assembly holds it: `AppState::new` and nothing
/// installed on it, whatever edition runs the suite. No portal
/// credential verifier, so a portal session is not a credential of it.
pub fn build_community_test_state(pool: PgPool) -> SharedState {
    community_gateway(pool).state
}

/// Build the MCP proxy sub-router under test.
pub fn build_mcp_app(state: SharedState) -> Router {
    Router::new()
        .nest("/v1/proxy/mcp", routes::mcp::router())
        // Production installs the request-scoped task-locals through
        // this layer (`main.rs`). Without it `record_consumed_approval`
        // and its siblings silently no-op, so a test harness that
        // omits it cannot observe the approval → action link at all —
        // which is exactly how a surface that never records the consumed approval
        // goes unnoticed.
        .layer(middleware::run_end::RunEndLayer::new(
            state.mcp_sessions.clone(),
        ))
        .layer(middleware::tenant::TenantLayer::new(state.clone()))
        .layer(CorsLayer::permissive())
        .with_state(state)
}

/// Build a GET `Request<Body>` with `ConnectInfo<SocketAddr>` pre-set
/// in the request extensions and (optionally) JWT + tenant headers.
///
/// `tower::ServiceExt::oneshot` doesn't wrap requests in
/// `into_make_service_with_connect_info`, so routes that extract
/// `ConnectInfo<SocketAddr>` 500 with "Missing extension" unless the
/// extension is inserted manually first.
pub fn build_get_request(path: &str, jwt: Option<&str>, tenant_id: Option<&str>) -> Request<Body> {
    let mut builder = Request::builder().method("GET").uri(path);
    if let Some(t) = tenant_id {
        builder = builder.header("X-Tenant-ID", t);
    }
    if let Some(j) = jwt {
        builder = builder.header("authorization", format!("Bearer {j}"));
    }
    let mut req = builder.body(Body::empty()).expect("build request");
    let peer: SocketAddr = "127.0.0.1:55555".parse().expect("peer addr");
    req.extensions_mut().insert(ConnectInfo(peer));
    req
}

/// JWT secret shared across integration HTTP tests. Matches the value
/// `AppConfig::for_test` uses, so a token minted here passes the
/// production JWT decode path in the tenant layer / extractor.
pub const TEST_JWT_SECRET: &str = "test-jwt-secret-for-integration-tests-min32chars!!";

/// Mint a portal user JWT signed with `TEST_JWT_SECRET`.
pub fn make_portal_jwt(user_id: i32, username: &str, tenant_id: &str) -> String {
    make_portal_jwt_issued_at(user_id, username, tenant_id, chrono::Utc::now().timestamp())
}

pub fn make_portal_jwt_issued_at(
    user_id: i32,
    username: &str,
    tenant_id: &str,
    issued_at: i64,
) -> String {
    use jsonwebtoken::{EncodingKey, Header, encode};
    // The shape every issuer mints (core password and 2FA login, the control
    // plane's end-user login): `typ` and a `jti`. Portal auth refuses a
    // portal token without a jti, so a fixture without one would
    // test a token production never issues.
    let claims = serde_json::json!({
        "typ": "portal",
        "jti": uuid::Uuid::new_v4().to_string(),
        "user_id": user_id.to_string(),
        "tenant_id": tenant_id,
        "username": username,
        "user_type": "end_user",
        "exp": (chrono::Utc::now().timestamp() + 3600) as u64,
        "iat": issued_at as u64,
    });
    encode(
        &Header::default(),
        &claims,
        &EncodingKey::from_secret(TEST_JWT_SECRET.as_bytes()),
    )
    .expect("encode portal jwt")
}

// ---------------------------------------------------------------------------
// Core topology seeders (tenant, resource group, user, EUG)
// ---------------------------------------------------------------------------

pub async fn seed_tenant(pool: &PgPool, id: &str) {
    // Drop the process-global per-tenant caches before seeding.
    //
    // `sqlx::test` gives each test its OWN throwaway database but they all
    // share one process, and several services now memoise per tenant id
    // (compliance profiles, `store_bodies`). Tests reuse tenant ids like
    // `tenant-comp` across databases, so without this the first test to run
    // caches its answer and every later test reading the same id gets it —
    // which surfaces as compliance tags that are inexplicably empty, in a
    // test whose own database plainly has the profile.
    //
    // Belongs here rather than in each test: any test that seeds a tenant is
    // by definition starting from a fresh database for that id.
    brutor_gateway_core::services::compliance::invalidate(id);
    brutor_gateway_core::services::data_retention::invalidate(id);

    sqlx::query(
        "INSERT INTO tenants (id, name, display_name, primary_contact_email, \
         is_active, is_default, is_trial, suspended, features, settings) \
         VALUES ($1, $1, $1, 'admin@test.local', true, false, false, false, \
                 '{}'::json, '{}'::json) \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .execute(pool)
    .await
    .expect("seed_tenant");
}

pub async fn seed_resource_group(pool: &PgPool, id: &str, tenant_id: &str) {
    sqlx::query(
        "INSERT INTO resource_groups (id, tenant_id, hierarchy_level, \
         inherit_from_parent, name, is_active, is_system) \
         VALUES ($1, $2, 0, false, $1, true, false) \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant_id)
    .execute(pool)
    .await
    .expect("seed_resource_group");
}

pub async fn seed_end_user(pool: &PgPool, id: i32, tenant_id: &str) {
    sqlx::query(
        "INSERT INTO end_users (id, tenant_id, username, password_hash, \
         is_verified, is_active, totp_enabled) \
         VALUES ($1, $2, $3, 'x', true, true, false) \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant_id)
    .bind(format!("user-{id}"))
    .execute(pool)
    .await
    .expect("seed_end_user");
}

pub async fn seed_end_user_group(pool: &PgPool, id: &str, tenant_id: &str, name: &str) {
    sqlx::query(
        "INSERT INTO end_user_groups (id, tenant_id, name, source) \
         VALUES ($1, $2, $3, 'manual') \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant_id)
    .bind(name)
    .execute(pool)
    .await
    .expect("seed_end_user_group");
}

pub async fn add_user_to_eug(pool: &PgPool, eug_id: &str, user_id: i32) {
    sqlx::query(
        "INSERT INTO end_user_group_members \
         (end_user_group_id, end_user_id, created_at, source) \
         VALUES ($1, $2, now(), 'manual') \
         ON CONFLICT DO NOTHING",
    )
    .bind(eug_id)
    .bind(user_id)
    .execute(pool)
    .await
    .expect("add_user_to_eug");
}

pub async fn bind_eug_to_rg(pool: &PgPool, eug_id: &str, rg_id: &str) {
    sqlx::query(
        "INSERT INTO resource_group_end_user_groups \
         (resource_group_id, end_user_group_id, created_at, created_by) \
         VALUES ($1, $2, now(), 'test') \
         ON CONFLICT DO NOTHING",
    )
    .bind(rg_id)
    .bind(eug_id)
    .execute(pool)
    .await
    .expect("bind_eug_to_rg");
}

pub async fn add_direct_membership(pool: &PgPool, user_id: i32, group_id: &str, tenant_id: &str) {
    sqlx::query(
        "INSERT INTO resource_group_memberships (tenant_id, user_id, group_id) \
         VALUES ($1, $2, $3) \
         ON CONFLICT DO NOTHING",
    )
    .bind(tenant_id)
    .bind(user_id)
    .bind(group_id)
    .execute(pool)
    .await
    .expect("add_direct_membership");
}

// ---------------------------------------------------------------------------
// MCP server + server_config + binding seeders
// ---------------------------------------------------------------------------

/// Insert a minimal `mcp_servers` row. Sets the required base_url +
/// endpoint path; the test doesn't actually hit the upstream.
pub async fn seed_mcp_server(pool: &PgPool, id: &str, tenant_id: &str, name: &str) {
    sqlx::query(
        "INSERT INTO mcp_servers (id, tenant_id, name, base_url, mcp_endpoint_path, enabled) \
         VALUES ($1, $2, $3, 'http://test.local', '/mcp', true) \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant_id)
    .bind(name)
    .execute(pool)
    .await
    .expect("seed_mcp_server");
}

/// Insert an `idp_servers` row with the issuer `https://idp.test.local`.
/// `registration_endpoint` controls whether CIMD bridging is advertised
/// (needs DCR upstream). A tenant's OIDC issuers are unique: a second IdP
/// of the same tenant is seeded with [`seed_idp_with_issuer`].
pub async fn seed_idp(
    pool: &PgPool,
    id: &str,
    tenant_id: &str,
    authorization_endpoint: Option<&str>,
    registration_endpoint: Option<&str>,
) {
    seed_idp_with_issuer(
        pool,
        id,
        tenant_id,
        "https://idp.test.local",
        authorization_endpoint,
        registration_endpoint,
    )
    .await;
}

/// [`seed_idp`] with its own issuer.
pub async fn seed_idp_with_issuer(
    pool: &PgPool,
    id: &str,
    tenant_id: &str,
    issuer: &str,
    authorization_endpoint: Option<&str>,
    registration_endpoint: Option<&str>,
) {
    sqlx::query(
        "INSERT INTO idp_servers \
         (id, tenant_id, name, issuer, type, authorization_endpoint, token_endpoint, \
          registration_endpoint, supports_pkce, supports_refresh_tokens, \
          supports_client_credentials, token_endpoint_auth_methods, scopes_supported, \
          response_types_supported, grant_types_supported, discovery_cache_ttl, \
          enabled, verified, protocol) \
         VALUES ($1, $2, $1, $5, 'oidc', $3, \
                 'https://idp.test.local/token', $4, true, true, false, \
                 '[]'::json, '[]'::json, '[]'::json, '[]'::json, 3600, true, false, 'oidc') \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant_id)
    .bind(authorization_endpoint)
    .bind(registration_endpoint)
    .bind(issuer)
    .execute(pool)
    .await
    .expect("seed_idp");
}

/// Insert a `server_configs` row that points at the given mcp_server.
pub async fn seed_server_config(pool: &PgPool, id: &str, tenant_id: &str, mcp_server_id: &str) {
    sqlx::query(
        "INSERT INTO server_configs \
         (id, tenant_id, name, priority, is_default, enabled, usage_count, mcp_server_id) \
         VALUES ($1, $2, $1, 0, false, true, 0, $3) \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant_id)
    .bind(mcp_server_id)
    .execute(pool)
    .await
    .expect("seed_server_config");
}

/// Bind a server_config to a resource_group via the join table.
pub async fn bind_server_config_to_rg(pool: &PgPool, server_config_id: &str, rg_id: &str) {
    sqlx::query(
        "INSERT INTO resource_group_server_configs \
         (id, resource_group_id, server_config_id, created_at) \
         VALUES (concat('rgsc-', $1, '-', $2), $2, $1, now()) \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(server_config_id)
    .bind(rg_id)
    .execute(pool)
    .await
    .expect("bind_server_config_to_rg");
}

// ---------------------------------------------------------------------------
// LLM provider + model + binding seeders
// ---------------------------------------------------------------------------

pub async fn seed_llm_provider(pool: &PgPool, id: &str, tenant_id: &str, key: &str) {
    sqlx::query(
        "INSERT INTO llm_providers (id, tenant_id, key, display_name, enabled, is_system) \
         VALUES ($1, $2, $3, $3, true, false) \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant_id)
    .bind(key)
    .execute(pool)
    .await
    .expect("seed_llm_provider");
}

/// Insert a minimal `llm_model` row. `mode` defaults to `chat`.
pub async fn seed_llm_model(
    pool: &PgPool,
    id: &str,
    tenant_id: &str,
    provider_id: &str,
    name: &str,
) {
    sqlx::query(
        "INSERT INTO llm_model \
         (id, tenant_id, name, provider_id, model_name, mode, \
          api_url_auto_discovered, thinking_enabled, enabled, health_status) \
         VALUES ($1, $2, $3, $4, $3, 'chat', false, false, true, 'healthy') \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant_id)
    .bind(name)
    .bind(provider_id)
    .execute(pool)
    .await
    .expect("seed_llm_model");
}

/// Set the `mcp_global_limits` JSON column on a resource_group.
/// Caller supplies the full JSON shape (`{"per_tool": {...}, ...}`).
pub async fn set_mcp_global_limits(pool: &PgPool, rg_id: &str, limits: serde_json::Value) {
    sqlx::query("UPDATE resource_groups SET mcp_global_limits = $2::json WHERE id = $1")
        .bind(rg_id)
        .bind(limits)
        .execute(pool)
        .await
        .expect("set_mcp_global_limits");
}

pub async fn set_llm_global_limits(pool: &PgPool, rg_id: &str, limits: serde_json::Value) {
    sqlx::query("UPDATE resource_groups SET llm_global_limits = $2::json WHERE id = $1")
        .bind(rg_id)
        .bind(limits)
        .execute(pool)
        .await
        .expect("set_llm_global_limits");
}

pub async fn set_skill_global_limits(pool: &PgPool, rg_id: &str, limits: serde_json::Value) {
    sqlx::query("UPDATE resource_groups SET skill_global_limits = $2::json WHERE id = $1")
        .bind(rg_id)
        .bind(limits)
        .execute(pool)
        .await
        .expect("set_skill_global_limits");
}

/// Set a group's parent + hierarchy_path so ancestor-rollup enforcement
/// (`enforce_ancestor_budgets`, which aggregates descendant usage by
/// `ancestry_path` prefix) and `collect_effective_limits` (which walks
/// `parent_group_id`) have a real tree to traverse.
pub async fn set_group_parent(
    pool: &PgPool,
    rg_id: &str,
    parent_id: &str,
    hierarchy_path: &str,
    hierarchy_level: i32,
) {
    sqlx::query(
        "UPDATE resource_groups \
         SET parent_group_id = $2, hierarchy_path = $3, hierarchy_level = $4, \
             inherit_from_parent = true \
         WHERE id = $1",
    )
    .bind(rg_id)
    .bind(parent_id)
    .bind(hierarchy_path)
    .bind(hierarchy_level)
    .execute(pool)
    .await
    .expect("set_group_parent");
}

/// Insert one `usage_logs` row. Mirrors the columns the quota SUM/COUNT
/// helpers read: `tenant_id`, `group_id`, `resource_type`, `resource_id`,
/// `total_tokens`, `estimated_cost`, `created_at` (the window column), plus
/// `ancestry_path` for ancestor-rollup tests. `request_id` is auto-unique
/// (it's the `ON CONFLICT` key on this table).
#[allow(clippy::too_many_arguments)]
pub async fn insert_usage_log(
    pool: &PgPool,
    tenant_id: &str,
    group_id: &str,
    ancestry_path: Option<&str>,
    resource_type: &str,
    resource_id: &str,
    total_tokens: i32,
    estimated_cost: f64,
    created_at: chrono::DateTime<chrono::Utc>,
) {
    sqlx::query(
        "INSERT INTO usage_logs \
         (tenant_id, group_id, ancestry_path, resource_type, resource_id, \
          total_tokens, estimated_cost, currency, is_inherited, request_id, created_at) \
         VALUES ($1, $2, $3, $4, $5, $6, $7, 'USD', false, $8, $9)",
    )
    .bind(tenant_id)
    .bind(group_id)
    .bind(ancestry_path)
    .bind(resource_type)
    .bind(resource_id)
    .bind(total_tokens)
    .bind(estimated_cost)
    .bind(uuid::Uuid::new_v4().to_string())
    .bind(created_at)
    .execute(pool)
    .await
    .expect("insert_usage_log");
}

/// Load a `ResourceGroup` row by id — the integration tests for the
/// usage module need the full struct (not just the id) because
/// `enforce_mcp_rate_limits` reads its limit columns.
pub async fn load_resource_group(
    pool: &PgPool,
    id: &str,
) -> brutor_gateway_core::models::db::ResourceGroup {
    sqlx::query_as::<_, brutor_gateway_core::models::db::ResourceGroup>(
        "SELECT *, group_type::text AS group_type FROM resource_groups WHERE id = $1",
    )
    .bind(id)
    .fetch_one(pool)
    .await
    .expect("load_resource_group")
}

/// Insert an `llm_model` row pointing at a custom `api_url`. Use this
/// instead of `seed_llm_model` when the test needs the proxy to call a
/// wiremock server. Sets `provider` slug via `provider_id` resolution.
pub async fn seed_llm_model_with_api_url(
    pool: &PgPool,
    id: &str,
    tenant_id: &str,
    provider_id: &str,
    model_name: &str,
    api_url: &str,
) {
    sqlx::query(
        "INSERT INTO llm_model \
         (id, tenant_id, name, provider_id, model_name, mode, \
          api_url, api_url_auto_discovered, thinking_enabled, enabled, health_status) \
         VALUES ($1, $2, $3, $4, $3, 'chat', $5, false, false, true, 'healthy') \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant_id)
    .bind(model_name)
    .bind(provider_id)
    .bind(api_url)
    .execute(pool)
    .await
    .expect("seed_llm_model_with_api_url");
}

/// Seed an `llm_model` with a custom `mode` (chat / embedding / image /
/// audio_speech / audio_transcribe) pointing `api_url` at a mock. Used by the
/// model-level fallback tests to exercise every non-chat mode.
pub async fn seed_llm_model_with_mode(
    pool: &PgPool,
    id: &str,
    tenant_id: &str,
    provider_id: &str,
    model_name: &str,
    api_url: &str,
    mode: &str,
) {
    sqlx::query(
        "INSERT INTO llm_model \
         (id, tenant_id, name, provider_id, model_name, mode, \
          api_url, api_url_auto_discovered, thinking_enabled, enabled, health_status) \
         VALUES ($1, $2, $3, $4, $3, $6, $5, false, false, true, 'healthy') \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant_id)
    .bind(model_name)
    .bind(provider_id)
    .bind(api_url)
    .bind(mode)
    .execute(pool)
    .await
    .expect("seed_llm_model_with_mode");
}

/// Set a model's ordered `fallback_models` chain (list of model ids) — the
/// model-level failover chain the proxy walks when this model errors.
pub async fn set_model_fallback_models(pool: &PgPool, model_id: &str, fallback_ids: &[&str]) {
    let chain = serde_json::json!(fallback_ids);
    sqlx::query("UPDATE llm_model SET fallback_models = $1 WHERE id = $2")
        .bind(chain)
        .bind(model_id)
        .execute(pool)
        .await
        .expect("set_model_fallback_models");
}

/// Insert an `llm_routing_group` row + the matching virtual `llm_model`
/// row (id `rg-<group-id>`, mode `routing`) the gateway-control service
/// creates. Tests can then bind the virtual model to a resource_group
/// just like a real model and assert that the portal models endpoint
/// surfaces it. The caller must have already seeded a provider row that
/// the virtual model can FK to (production seeds a synthetic
/// "routing_group" provider on first creation — for tests, reuse any
/// existing provider).
pub async fn seed_routing_group(
    pool: &PgPool,
    group_id: &str,
    tenant_id: &str,
    provider_id: &str,
    mode: &str,
) {
    sqlx::query(
        "INSERT INTO llm_routing_group \
         (id, tenant_id, name, mode, enabled) \
         VALUES ($1, $2, $1, $3, true) \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(group_id)
    .bind(tenant_id)
    .bind(mode)
    .execute(pool)
    .await
    .expect("seed_routing_group");

    // Virtual llm_model row — id mirrors gateway-control's `rg-<id>`
    // convention so the portal can dispatch directly to the routing
    // group via the same model-id path it uses for real models.
    let virtual_id = format!("rg-{group_id}");
    sqlx::query(
        "INSERT INTO llm_model \
         (id, tenant_id, name, provider_id, model_name, mode, routing_group_id, \
          api_url_auto_discovered, thinking_enabled, enabled, health_status) \
         VALUES ($1, $2, $3, $4, $3, 'routing', $5, false, false, true, 'healthy') \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(&virtual_id)
    .bind(tenant_id)
    .bind(group_id)
    .bind(provider_id)
    .bind(group_id)
    .execute(pool)
    .await
    .expect("seed_routing_group virtual llm_model");
}

/// Insert a group-scoped API key. Returns the raw key the caller sends in
/// the `Authorization: Bearer …` header. The DB stores the SHA-256 hash
/// of the raw key, matching the production hashing scheme in
/// `services/auth.rs::authenticate_request`.
pub async fn seed_api_key(
    pool: &PgPool,
    tenant_id: &str,
    group_id: &str,
    user_id: Option<i32>,
    name: &str,
) -> String {
    use sha2::{Digest, Sha256};
    // Random-ish suffix derived from the test name so parallel sqlx::test
    // runs don't collide on the api_keys primary key.
    let suffix = uuid::Uuid::new_v4().simple().to_string();
    let raw = format!("sk_brutor_api_test_{suffix}");
    let hash = format!("{:x}", Sha256::digest(raw.as_bytes()));
    sqlx::query(
        "INSERT INTO api_keys \
         (id, tenant_id, key_hash, key_prefix, name, scope, user_id, group_id, \
          enabled, revoked, usage_count) \
         VALUES ($1, $2, $3, $4, $5, 'group', $6, $7, true, false, 0)",
    )
    .bind(format!("key-{suffix}"))
    .bind(tenant_id)
    .bind(&hash)
    .bind(&raw[..raw.len().min(20)])
    .bind(name)
    .bind(user_id)
    .bind(group_id)
    .execute(pool)
    .await
    .expect("seed_api_key");
    raw
}

/// Seed a **shared** API key as the control plane actually emits it:
/// `scope='global'` with a `group_id` and NO `user_id` — i.e.
/// "accessible by all users of the bound group" (admin UI access_mode
/// `shared`). This is distinct from [`seed_api_key`], which uses the
/// `scope='group'` value the control plane never produces. Group-scoped
/// attribution (cost, governance, compliance tags) must work for this
/// shape exactly as it does for a portal JWT user.
pub async fn seed_shared_group_api_key(
    pool: &PgPool,
    tenant_id: &str,
    group_id: &str,
    name: &str,
) -> String {
    use sha2::{Digest, Sha256};
    let suffix = uuid::Uuid::new_v4().simple().to_string();
    let raw = format!("sk_brutor_api_test_{suffix}");
    let hash = format!("{:x}", Sha256::digest(raw.as_bytes()));
    sqlx::query(
        "INSERT INTO api_keys \
         (id, tenant_id, key_hash, key_prefix, name, scope, user_id, group_id, \
          enabled, revoked, usage_count) \
         VALUES ($1, $2, $3, $4, $5, 'global', NULL, $6, true, false, 0)",
    )
    .bind(format!("key-{suffix}"))
    .bind(tenant_id)
    .bind(&hash)
    .bind(&raw[..raw.len().min(20)])
    .bind(name)
    .bind(group_id)
    .execute(pool)
    .await
    .expect("seed_shared_group_api_key");
    raw
}

/// Seed an API key with full control over the group binding and the
/// platform flag. `group_id: None` with `platform: false` is a key no group
/// governs (the core refuses it); with `platform: true` it is the platform
/// service key the control plane seeds (`system-health-check`).
pub async fn seed_key(
    pool: &PgPool,
    tenant_id: &str,
    group_id: Option<&str>,
    platform: bool,
    name: &str,
) -> String {
    use sha2::{Digest, Sha256};
    let suffix = uuid::Uuid::new_v4().simple().to_string();
    let raw = format!("sk_brutor_api_test_{suffix}");
    let hash = format!("{:x}", Sha256::digest(raw.as_bytes()));
    sqlx::query(
        "INSERT INTO api_keys \
         (id, tenant_id, key_hash, key_prefix, name, scope, user_id, group_id, \
          enabled, revoked, usage_count, is_platform_service) \
         VALUES ($1, $2, $3, $4, $5, 'global', NULL, $6, true, false, 0, $7)",
    )
    .bind(format!("key-{suffix}"))
    .bind(tenant_id)
    .bind(&hash)
    .bind(&raw[..raw.len().min(20)])
    .bind(name)
    .bind(group_id)
    .bind(platform)
    .execute(pool)
    .await
    .expect("seed_key");
    raw
}

/// The tenant's Unbound system group, as the control plane seeds it.
pub async fn seed_unbound_system_group(pool: &PgPool, id: &str, tenant_id: &str) {
    sqlx::query(
        "INSERT INTO resource_groups (id, tenant_id, hierarchy_level, \
         inherit_from_parent, name, is_active, is_system) \
         VALUES ($1, $2, 0, false, 'Unbound', true, true)",
    )
    .bind(id)
    .bind(tenant_id)
    .execute(pool)
    .await
    .expect("seed_unbound_system_group");
}

/// Build a POST `Request<Body>` with `ConnectInfo<SocketAddr>` extension
/// pre-set + JSON body + optional auth/tenant headers.
pub fn build_post_request(
    path: &str,
    body: serde_json::Value,
    api_key: Option<&str>,
    tenant_id: Option<&str>,
) -> axum::http::Request<axum::body::Body> {
    let mut builder = axum::http::Request::builder()
        .method("POST")
        .uri(path)
        .header("content-type", "application/json");
    if let Some(t) = tenant_id {
        builder = builder.header("X-Tenant-ID", t);
    }
    if let Some(k) = api_key {
        builder = builder.header("authorization", format!("Bearer {k}"));
    }
    let body_bytes = serde_json::to_vec(&body).expect("serialize body");
    let mut req = builder
        .body(axum::body::Body::from(body_bytes))
        .expect("build request");
    let peer: std::net::SocketAddr = "127.0.0.1:55555".parse().expect("peer addr");
    req.extensions_mut()
        .insert(axum::extract::ConnectInfo(peer));
    req
}

/// Seed an MCP server with a custom `base_url` (so the proxy talks to a
/// wiremock instead of a real MCP). Sets `mcp_endpoint_path=""` so the
/// proxy forwards to `{base_url}` verbatim without doubling the path.
pub async fn seed_mcp_server_with_base_url(
    pool: &PgPool,
    id: &str,
    tenant_id: &str,
    name: &str,
    base_url: &str,
) {
    sqlx::query(
        "INSERT INTO mcp_servers \
         (id, tenant_id, name, base_url, mcp_endpoint_path, enabled) \
         VALUES ($1, $2, $3, $4, '', true) \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant_id)
    .bind(name)
    .bind(base_url)
    .execute(pool)
    .await
    .expect("seed_mcp_server_with_base_url");
}

/// Stamp a region on an existing mcp_server row. Used by the
/// residency tests to flip a server between allowed and blocked
/// without resetting the rest of the topology.
pub async fn set_mcp_server_region(pool: &PgPool, id: &str, region: &str) {
    sqlx::query("UPDATE mcp_servers SET region = $1 WHERE id = $2")
        .bind(region)
        .bind(id)
        .execute(pool)
        .await
        .expect("set_mcp_server_region");
}

/// Stamp a region on an existing llm_provider row. Used by the
/// LLM residency tests for the same reason as
/// `set_mcp_server_region` — flip allowed vs blocked without
/// re-seeding the rest of the topology.
pub async fn set_llm_provider_region(pool: &PgPool, id: &str, region: &str) {
    sqlx::query("UPDATE llm_providers SET region = $1 WHERE id = $2")
        .bind(region)
        .bind(id)
        .execute(pool)
        .await
        .expect("set_llm_provider_region");
}

/// Stamp a data_residency profile on an existing tenant row.
/// `profile` is a JSON value matching the
/// `services::residency::ResidencyProfile` shape (allowed_regions
/// + block_global). Pass `serde_json::Value::Null` to clear.
pub async fn set_tenant_residency(pool: &PgPool, tenant_id: &str, profile: serde_json::Value) {
    sqlx::query("UPDATE tenants SET data_residency = $1 WHERE id = $2")
        .bind(&profile)
        .bind(tenant_id)
        .execute(pool)
        .await
        .expect("set_tenant_residency");
}

/// Stamp the tenant's compliance_profile JSONB. Used by the
/// Feature 5 integration tests to flip which regimes are
/// declared without re-seeding the rest of the topology.
/// Declare a tenant's compliance frameworks.
///
/// Invalidates the process-wide profile cache: tests share one process, and a
/// 60s-TTL'd "no profile" answer cached by an earlier case would otherwise
/// outlive this write and make the tagging assertions flaky-by-ordering.
pub async fn set_tenant_compliance_profile(
    pool: &PgPool,
    tenant_id: &str,
    profile: serde_json::Value,
) {
    sqlx::query("UPDATE tenants SET compliance_profile = $1 WHERE id = $2")
        .bind(&profile)
        .bind(tenant_id)
        .execute(pool)
        .await
        .expect("set_tenant_compliance_profile");
    brutor_gateway_core::services::compliance::invalidate(tenant_id);
}

/// Stamp eu_ai_act_risk_tier on a resource group. Mirror of the
/// residency / region helpers above.
pub async fn set_rg_eu_ai_act_tier(pool: &PgPool, rg_id: &str, tier: &str) {
    sqlx::query("UPDATE resource_groups SET eu_ai_act_risk_tier = $1 WHERE id = $2")
        .bind(tier)
        .bind(rg_id)
        .execute(pool)
        .await
        .expect("set_rg_eu_ai_act_tier");
}

/// Set the group-wide `llm_global_governance` JSONB column (the admin "LLM
/// Global Governance" form — parameter caps that apply to every model in the
/// group).
/// Price a model. Both values are USD per ONE MILLION tokens, matching the
/// catalog column semantics — a test that treats them as per-token is off by
/// six orders of magnitude and will pass or fail for the wrong reason.
pub async fn set_llm_model_costs(pool: &PgPool, model_id: &str, input: f64, output: f64) {
    sqlx::query(
        "UPDATE llm_model SET cost_per_input_token = $2, cost_per_output_token = $3 WHERE id = $1",
    )
    .bind(model_id)
    .bind(input)
    .bind(output)
    .execute(pool)
    .await
    .expect("set model costs");
}

/// Declare an AI System's operating envelope — the per-RUN ceilings
/// (`max_calls_per_run`, `max_cost_per_run`, ...) `run_budget` reads.
pub async fn set_rg_operating_envelope(pool: &PgPool, rg_id: &str, envelope: serde_json::Value) {
    sqlx::query("UPDATE resource_groups SET operating_envelope = $2::jsonb WHERE id = $1")
        .bind(rg_id)
        .bind(envelope.to_string())
        .execute(pool)
        .await
        .expect("set operating_envelope");
    // The caps cache is process-wide and 60s-TTL'd, so a test that seeds an
    // envelope after some other test has already read this group id would
    // otherwise see the stale answer.
    brutor_gateway_core::services::run_budget::invalidate(rg_id);
}

pub async fn set_rg_llm_governance(pool: &PgPool, rg_id: &str, governance: serde_json::Value) {
    sqlx::query("UPDATE resource_groups SET llm_global_governance = $1::jsonb WHERE id = $2")
        .bind(governance.to_string())
        .bind(rg_id)
        .execute(pool)
        .await
        .expect("set_rg_llm_governance");
}

/// Set the group-wide `skill_global_governance` JSONB column
/// (execution_mode / max_concurrent_executions / max_tool_calls_per_execution).
pub async fn set_rg_skill_governance(pool: &PgPool, rg_id: &str, governance: serde_json::Value) {
    sqlx::query("UPDATE resource_groups SET skill_global_governance = $1::jsonb WHERE id = $2")
        .bind(governance.to_string())
        .bind(rg_id)
        .execute(pool)
        .await
        .expect("set_rg_skill_governance");
}

/// The group's default approval window (`None` clears it).
pub async fn set_rg_approval_window(pool: &PgPool, rg_id: &str, seconds: Option<i32>) {
    sqlx::query("UPDATE resource_groups SET approval_window_seconds = $1 WHERE id = $2")
        .bind(seconds)
        .bind(rg_id)
        .execute(pool)
        .await
        .expect("set_rg_approval_window");
}

/// The tenant's default approval window (`None` clears it).
pub async fn set_tenant_approval_window(pool: &PgPool, tenant_id: &str, seconds: Option<i32>) {
    sqlx::query("UPDATE tenants SET default_approval_window_seconds = $1 WHERE id = $2")
        .bind(seconds)
        .bind(tenant_id)
        .execute(pool)
        .await
        .expect("set_tenant_approval_window");
}

/// Make `child` a child of `parent` (both already seeded).
pub async fn set_rg_parent(pool: &PgPool, child: &str, parent: &str) {
    sqlx::query("UPDATE resource_groups SET parent_group_id = $1 WHERE id = $2")
        .bind(parent)
        .bind(child)
        .execute(pool)
        .await
        .expect("set_rg_parent");
}

/// Set the group-wide `mcp_global_governance` JSONB column
/// (default_tool_access / default_resource_access / ...).
pub async fn set_rg_mcp_governance(pool: &PgPool, rg_id: &str, governance: serde_json::Value) {
    sqlx::query("UPDATE resource_groups SET mcp_global_governance = $1::jsonb WHERE id = $2")
        .bind(governance.to_string())
        .bind(rg_id)
        .execute(pool)
        .await
        .expect("set_rg_mcp_governance");
}

/// Add a per-capability access filter row for a (group, server config).
#[allow(clippy::too_many_arguments)]
pub async fn set_mcp_capability_filter(
    pool: &PgPool,
    tenant_id: &str,
    rg_id: &str,
    server_config_id: &str,
    capability_type: &str,
    capability_name: &str,
    access: &str,
) {
    set_mcp_capability_filter_with_timeout(
        pool,
        tenant_id,
        rg_id,
        server_config_id,
        capability_type,
        capability_name,
        access,
        None,
    )
    .await;
}

/// [`set_mcp_capability_filter`] with an explicit `approval_timeout_seconds`.
/// The control plane accepts the timeout on any access level, so an
/// `enabled` row can carry one too.
#[allow(clippy::too_many_arguments)]
pub async fn set_mcp_capability_filter_with_timeout(
    pool: &PgPool,
    tenant_id: &str,
    rg_id: &str,
    server_config_id: &str,
    capability_type: &str,
    capability_name: &str,
    access: &str,
    approval_timeout_seconds: Option<i32>,
) {
    sqlx::query(
        "INSERT INTO resource_group_mcp_capabilities \
         (id, tenant_id, resource_group_id, server_config_id, capability_type, \
          capability_name, access, approval_timeout_seconds, created_at, updated_at) \
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now(), now()) \
         ON CONFLICT DO NOTHING",
    )
    .bind(format!("cap-{rg_id}-{capability_name}"))
    .bind(tenant_id)
    .bind(rg_id)
    .bind(server_config_id)
    .bind(capability_type)
    .bind(capability_name)
    .bind(access)
    .bind(approval_timeout_seconds)
    .execute(pool)
    .await
    .expect("set_mcp_capability_filter_with_timeout");
}

/// Set the group-wide per-mode `media_global_governance` JSONB column.
pub async fn set_rg_media_governance(pool: &PgPool, rg_id: &str, governance: serde_json::Value) {
    sqlx::query("UPDATE resource_groups SET media_global_governance = $1::jsonb WHERE id = $2")
        .bind(governance.to_string())
        .bind(rg_id)
        .execute(pool)
        .await
        .expect("set_rg_media_governance");
}

/// Set the group-wide per-mode `media_global_limits` JSONB column.
pub async fn set_rg_media_limits(pool: &PgPool, rg_id: &str, limits: serde_json::Value) {
    sqlx::query("UPDATE resource_groups SET media_global_limits = $1::jsonb WHERE id = $2")
        .bind(limits.to_string())
        .bind(rg_id)
        .execute(pool)
        .await
        .expect("set_rg_media_limits");
}

/// Set the per-(group, model) LLM `limits` override on the
/// `resource_group_llm_models` binding row.
pub async fn set_rg_model_limits(
    pool: &PgPool,
    model_id: &str,
    rg_id: &str,
    limits: serde_json::Value,
) {
    sqlx::query(
        "UPDATE resource_group_llm_models SET limits = $1::json \
         WHERE llm_model_id = $2 AND resource_group_id = $3",
    )
    .bind(limits.to_string())
    .bind(model_id)
    .bind(rg_id)
    .execute(pool)
    .await
    .expect("set_rg_model_limits");
}

/// Set the per-(group, server) MCP `limits` override on the
/// `resource_group_server_configs` binding row.
pub async fn set_rg_server_limits(
    pool: &PgPool,
    server_config_id: &str,
    rg_id: &str,
    limits: serde_json::Value,
) {
    sqlx::query(
        "UPDATE resource_group_server_configs SET limits = $1::json \
         WHERE server_config_id = $2 AND resource_group_id = $3",
    )
    .bind(limits.to_string())
    .bind(server_config_id)
    .bind(rg_id)
    .execute(pool)
    .await
    .expect("set_rg_server_limits");
}

/// Set the per-(group, skill) `limits` override on the
/// `resource_group_agent_skills` binding row.
pub async fn set_rg_skill_limits(
    pool: &PgPool,
    skill_id: &str,
    rg_id: &str,
    limits: serde_json::Value,
) {
    sqlx::query(
        "UPDATE resource_group_agent_skills SET limits = $1::json \
         WHERE agent_skill_id = $2 AND resource_group_id = $3",
    )
    .bind(limits.to_string())
    .bind(skill_id)
    .bind(rg_id)
    .execute(pool)
    .await
    .expect("set_rg_skill_limits");
}

// ---------------------------------------------------------------------------
// TENANT-WIDE per-resource control setters (the `tenant_limits` /
// `tenant_governance` columns — one aggregate across ALL resource groups for
// a given resource, read by the `enforce_*_tenant_*` paths).
// ---------------------------------------------------------------------------

/// Set `llm_model.tenant_limits` (LLM tenant-wide quota / rate / throughput /
/// concurrency — aggregated across every RG that uses the model).
pub async fn set_llm_tenant_limits(pool: &PgPool, model_id: &str, limits: serde_json::Value) {
    sqlx::query("UPDATE llm_model SET tenant_limits = $1::json WHERE id = $2")
        .bind(limits.to_string())
        .bind(model_id)
        .execute(pool)
        .await
        .expect("set_llm_tenant_limits");
}

/// Set `llm_model.tenant_governance` (per-model tenant-wide governance folded
/// into `collect_effective_llm_governance`).
pub async fn set_llm_tenant_governance(
    pool: &PgPool,
    model_id: &str,
    governance: serde_json::Value,
) {
    sqlx::query("UPDATE llm_model SET tenant_governance = $1::json WHERE id = $2")
        .bind(governance.to_string())
        .bind(model_id)
        .execute(pool)
        .await
        .expect("set_llm_tenant_governance");
}

/// Set `mcp_servers.tenant_limits` (MCP tenant + tenant-tool rate — aggregated
/// across every RG that reaches the server).
pub async fn set_mcp_tenant_limits(pool: &PgPool, server_id: &str, limits: serde_json::Value) {
    sqlx::query("UPDATE mcp_servers SET tenant_limits = $1::json WHERE id = $2")
        .bind(limits.to_string())
        .bind(server_id)
        .execute(pool)
        .await
        .expect("set_mcp_tenant_limits");
}

/// Set `agent_skills.tenant_limits` (skill tenant-wide execution quota).
pub async fn set_skill_tenant_limits(pool: &PgPool, skill_id: &str, limits: serde_json::Value) {
    sqlx::query("UPDATE agent_skills SET tenant_limits = $1::json WHERE id = $2")
        .bind(limits.to_string())
        .bind(skill_id)
        .execute(pool)
        .await
        .expect("set_skill_tenant_limits");
}

/// Insert a per-(group, model) `resource_group_llm_governance` override row
/// (Models → <model> → Limits). `collect_effective_llm_governance` merges this
/// most-restrictive with the group-wide column and the model tenant default.
pub async fn set_rg_model_governance(
    pool: &PgPool,
    tenant_id: &str,
    rg_id: &str,
    model_id: &str,
    governance: serde_json::Value,
) {
    sqlx::query(
        "INSERT INTO resource_group_llm_governance \
         (id, tenant_id, resource_group_id, llm_model_id, governance, created_at, updated_at) \
         VALUES ($1, $2, $3, $4, $5, NOW(), NOW()) \
         ON CONFLICT (id) DO UPDATE SET governance = EXCLUDED.governance",
    )
    .bind(format!("rglg-{rg_id}-{model_id}"))
    .bind(tenant_id)
    .bind(rg_id)
    .bind(model_id)
    .bind(governance)
    .execute(pool)
    .await
    .expect("set_rg_model_governance");
}

/// Seed an `audio_speech` media usage row in `proxy_logs` (the source
/// `enforce_media_quota` aggregates), dated now, for the given group.
pub async fn seed_media_proxy_log(
    pool: &PgPool,
    tenant_id: &str,
    rg_id: &str,
    audio_chars: i64,
    cost_usd: f64,
) {
    let id = format!("pl-{}", uuid::Uuid::new_v4().simple());
    sqlx::query(
        "INSERT INTO proxy_logs \
         (id, tenant_id, timestamp, proxy_type, end_user_session_id, http_method, endpoint, \
          success, resource_group_id, media_audio_chars, llm_estimated_cost_usd, created_at) \
         VALUES ($1, $2, now(), 'llm', 'sess', 'POST', '/v1/proxy/llm/audio/speech', \
                 true, $3, $4, $5, now())",
    )
    .bind(id)
    .bind(tenant_id)
    .bind(rg_id)
    .bind(audio_chars)
    .bind(cost_usd)
    .execute(pool)
    .await
    .expect("seed_media_proxy_log");
}

/// Stamp phi_resource=true on a resource group.
pub async fn set_rg_phi_resource(pool: &PgPool, rg_id: &str, phi: bool) {
    sqlx::query("UPDATE resource_groups SET phi_resource = $1 WHERE id = $2")
        .bind(phi)
        .bind(rg_id)
        .execute(pool)
        .await
        .expect("set_rg_phi_resource");
}

/// Build the MCP proxy sub-router under test.
pub fn build_mcp_proxy_app(state: SharedState) -> Router {
    Router::new()
        .nest("/v1/proxy/mcp", routes::mcp::router())
        .layer(middleware::tenant::TenantLayer::new(state.clone()))
        // Production installs the request-scoped task-locals through
        // this layer (`main.rs`). Without it `record_consumed_approval`
        // and its siblings silently no-op, so a test harness that
        // omits it cannot observe the approval → action link at all —
        // which is exactly how a surface that never records the consumed approval
        // goes unnoticed.
        .layer(middleware::run_end::RunEndLayer::new(
            state.mcp_sessions.clone(),
        ))
        .layer(tower_http::cors::CorsLayer::permissive())
        .with_state(state)
}

/// Build the inbound OAuth sub-router under test (mounted at
/// `/v1/proxy/oauth` in prod). Covers the CIMD authorize bridge routes.
pub fn build_oauth_app(state: SharedState) -> Router {
    Router::new()
        .nest("/v1/proxy/oauth", routes::oauth::router())
        .layer(middleware::tenant::TenantLayer::new(state.clone()))
        .layer(tower_http::cors::CorsLayer::permissive())
        .with_state(state)
}

/// Build the A2A v1.0 inbound sub-router under test (mounted at `/a2a` in prod,
/// e.g. `/a2a/{tenant}/{card}/message:send`).
pub fn build_a2a_v1_app(state: SharedState) -> Router {
    Router::new()
        .nest("/a2a", routes::a2a_v1::router())
        .layer(middleware::tenant::TenantLayer::new(state.clone()))
        // Production installs the request-scoped task-locals through
        // this layer (`main.rs`). Without it `record_consumed_approval`
        // and its siblings silently no-op, so a test harness that
        // omits it cannot observe the approval → action link at all —
        // which is exactly how a surface that never records the consumed approval
        // goes unnoticed.
        .layer(middleware::run_end::RunEndLayer::new(
            state.mcp_sessions.clone(),
        ))
        .layer(tower_http::cors::CorsLayer::permissive())
        .with_state(state)
}

/// Seed a minimal enabled agent card (no signature; trust_tier `unsigned`).
pub async fn seed_agent_card(pool: &PgPool, id: &str, tenant_id: &str, name: &str) {
    sqlx::query(
        "INSERT INTO agent_cards \
         (id, tenant_id, name, version, card_json, protocol_version, trust_tier, \
          enabled, created_at, updated_at) \
         VALUES ($1, $2, $3, '1.0.0', '{}'::json, '1.0', 'unsigned', true, now(), now()) \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant_id)
    .bind(name)
    .execute(pool)
    .await
    .expect("seed_agent_card");
}

/// Bind an agent card to a resource group (so the caller's RG may invoke it).
pub async fn bind_agent_card_to_rg(pool: &PgPool, card_id: &str, rg_id: &str) {
    sqlx::query(
        "INSERT INTO resource_group_agent_cards \
         (id, resource_group_id, agent_card_id, created_at) \
         VALUES (concat('rgac-', $1, '-', $2), $2, $1, now()) \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(card_id)
    .bind(rg_id)
    .execute(pool)
    .await
    .expect("bind_agent_card_to_rg");
}

/// Build the LLM proxy sub-router under test.
pub fn build_llm_app(state: SharedState) -> Router {
    Router::new()
        .nest("/v1/proxy/llm", routes::llm::router())
        .layer(middleware::tenant::TenantLayer::new(state.clone()))
        // Production installs the request-scoped task-locals through
        // this layer (`main.rs`). Without it `record_consumed_approval`
        // and its siblings silently no-op, so a test harness that
        // omits it cannot observe the approval → action link at all —
        // which is exactly how a surface that never records the consumed approval
        // goes unnoticed.
        .layer(middleware::run_end::RunEndLayer::new(
            state.mcp_sessions.clone(),
        ))
        .layer(tower_http::cors::CorsLayer::permissive())
        .with_state(state)
}

/// Mount the Anthropic-native surface (`/v1/messages`) for testing the
/// `/v1/messages` handler's guardrail wiring directly (it does NOT share
/// `routes::llm`'s OpenAI handler).
pub fn build_anthropic_app(state: SharedState) -> Router {
    Router::new()
        .nest("/v1", routes::anthropic::router())
        .layer(middleware::tenant::TenantLayer::new(state.clone()))
        .layer(tower_http::cors::CorsLayer::permissive())
        .with_state(state)
}

/// Mount the Gemini-native surface (`/v1beta/models/{model}:{action}`).
pub fn build_gemini_app(state: SharedState) -> Router {
    Router::new()
        .merge(routes::gemini::router())
        .layer(middleware::tenant::TenantLayer::new(state.clone()))
        .layer(tower_http::cors::CorsLayer::permissive())
        .with_state(state)
}

/// Mount the multimodal + skill surfaces (image-gen, audio TTS/STT, skills)
/// at their production paths, for testing per-mode guardrail wiring.
pub fn build_multimodal_app(state: SharedState) -> Router {
    Router::new()
        .nest("/v1/proxy/llm/images/generations", routes::images::router())
        .nest("/v1/proxy/llm/audio/speech", routes::audio_speech::router())
        .nest(
            "/v1/proxy/llm/audio/transcriptions",
            routes::audio_transcribe::router(),
        )
        .nest("/v1/proxy/llm/video/generations", routes::video::router())
        .nest("/v1/proxy/llm/video/jobs", routes::video::jobs_router())
        .nest("/v1/proxy/files", routes::files::router())
        .nest("/v1/proxy/skills", routes::skills::router())
        .layer(middleware::tenant::TenantLayer::new(state.clone()))
        // Production installs the request-scoped task-locals through
        // this layer (`main.rs`). Without it `record_consumed_approval`
        // and its siblings silently no-op, so a test harness that
        // omits it cannot observe the approval → action link at all —
        // which is exactly how a surface that never records the consumed approval
        // goes unnoticed.
        .layer(middleware::run_end::RunEndLayer::new(
            state.mcp_sessions.clone(),
        ))
        .layer(tower_http::cors::CorsLayer::permissive())
        .with_state(state)
}

/// Set a model's `mode` (the multimodal handlers require an exact match:
/// `audio_transcription` / `audio_speech` / `image_generation`). The chat
/// seed helpers hardcode `mode='chat'`.
pub async fn set_model_mode(pool: &PgPool, model_id: &str, mode: &str) {
    sqlx::query("UPDATE llm_model SET mode = $1 WHERE id = $2")
        .bind(mode)
        .bind(model_id)
        .execute(pool)
        .await
        .expect("set_model_mode");
}

/// Seed a published agent_skill. `md_content` + `description` become the
/// skill's "output" scanned by the skill_output guardrail on the REST
/// `/v1/proxy/skills/execute` path (no sandbox runs on that path).
pub async fn seed_agent_skill(
    pool: &PgPool,
    id: &str,
    tenant_id: &str,
    name: &str,
    md_content: &str,
    description: &str,
) {
    seed_agent_skill_export(pool, id, tenant_id, name, md_content, description, false).await;
}

/// Like `seed_agent_skill` but lets the caller flip `allow_local_export`
///  — used by the portal skills-export tests.
#[allow(clippy::too_many_arguments)]
pub async fn seed_agent_skill_export(
    pool: &PgPool,
    id: &str,
    tenant_id: &str,
    name: &str,
    md_content: &str,
    description: &str,
    allow_local_export: bool,
) {
    sqlx::query(
        "INSERT INTO agent_skills \
         (id, tenant_id, name, description, skill_md_content, skill_md_hash, \
          version, status, disclosure_mode, enabled, allow_local_export, \
          execution_count, created_at, updated_at) \
         VALUES ($1, $2, $3, $4, $5, 'deadbeef', '1.0.0', \
                 'PUBLISHED'::skillstatus, 'BOTH'::skilldisclosuremode, \
                 true, $6, 0, now(), now())",
    )
    .bind(id)
    .bind(tenant_id)
    .bind(name)
    .bind(description)
    .bind(md_content)
    .bind(allow_local_export)
    .execute(pool)
    .await
    .expect("seed_agent_skill_export");
}

/// Bind an agent_skill to a resource group (so `check_skill_group_access` passes).
pub async fn bind_skill_to_rg(pool: &PgPool, skill_id: &str, rg_id: &str) {
    sqlx::query(
        "INSERT INTO resource_group_agent_skills \
         (id, resource_group_id, agent_skill_id, created_at) \
         VALUES (concat('rgas-', $1, '-', $2), $2, $1, now()) \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(skill_id)
    .bind(rg_id)
    .execute(pool)
    .await
    .expect("bind_skill_to_rg");
}

/// Seed a banned-words guardrail enabling an ARBITRARY set of surfaces (by
/// surface token, e.g. `["skill_output", "audio_stt_output"]`). Generalises
/// `seed_llm_banned_words_guardrail` (chat-only) to every surface column.
pub async fn seed_banned_words_guardrail_surfaces(
    pool: &PgPool,
    id: &str,
    tenant_id: &str,
    banned: &[&str],
    surfaces: &[&str],
) {
    let on = |s: &str| surfaces.contains(&s);
    let banned_json =
        serde_json::to_string(&banned.iter().map(|s| s.to_string()).collect::<Vec<_>>()).unwrap();
    sqlx::query(
        r#"INSERT INTO guardrail_configs (
              id, tenant_id, name, enabled, is_default, priority,
              chat_input_enabled, chat_output_enabled,
              embeddings_input_enabled,
              image_gen_input_enabled, image_gen_output_enabled,
              audio_tts_input_enabled, audio_tts_output_enabled,
              audio_stt_input_enabled, audio_stt_output_enabled,
              video_gen_input_enabled, video_gen_output_enabled,
              batch_input_enabled, batch_output_enabled,
              moderation_input_enabled,
              mcp_input_enabled, mcp_output_enabled, mcp_registration_enabled,
              skill_input_enabled, skill_output_enabled,
              a2a_inbound_enabled, a2a_outbound_enabled,
              pii_detection_enabled, prompt_injection_enabled,
              jailbreak_detection_enabled, toxic_content_enabled,
              banned_words, banned_words_match_mode, banned_words_surfaces,
              fail_open, log_all_checks,
              streaming_output_mode, secrets_detection_enabled, secrets_action
           ) VALUES (
              $1, $2, $3, true, false, 100,
              $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17,
              $18, $19, false, $20, $21, $22, $23,
              false, false, false, false,
              $24::json, 'substring', $25::jsonb,
              false, true,
              'auto', false, 'redact'
           )"#,
    )
    .bind(id)
    .bind(tenant_id)
    .bind(format!("e2e-{id}"))
    .bind(on("chat_input"))
    .bind(on("chat_output"))
    .bind(on("embeddings_input"))
    .bind(on("image_gen_input"))
    .bind(on("image_gen_output"))
    .bind(on("audio_tts_input"))
    .bind(on("audio_tts_output"))
    .bind(on("audio_stt_input"))
    .bind(on("audio_stt_output"))
    .bind(on("video_gen_input"))
    .bind(on("video_gen_output"))
    .bind(on("batch_input"))
    .bind(on("batch_output"))
    .bind(on("moderation_input"))
    .bind(on("mcp_input"))
    .bind(on("mcp_output"))
    .bind(on("skill_input"))
    .bind(on("skill_output"))
    .bind(on("a2a_inbound"))
    .bind(on("a2a_outbound"))
    .bind(&banned_json)
    // Per-check surface list: set it to exactly the enabled surfaces so the
    // two-layer gate (config `*_enabled` ∩ per-check `*_surfaces`) lets the
    // banned-words check run on them — the NULL default
    // (DEFAULT_SURFACES_LLM_MCP) excludes skill_* / a2a_*.
    .bind(serde_json::to_string(surfaces).unwrap())
    .execute(pool)
    .await
    .expect("seed_banned_words_guardrail_surfaces");
}

/// Seed a banned-words guardrail on the LLM chat input/output surfaces and
/// return its id. Shared by the native-surface suites (anthropic/gemini).
pub async fn seed_llm_banned_words_guardrail(
    pool: &PgPool,
    id: &str,
    tenant_id: &str,
    banned: &[&str],
    chat_input: bool,
    chat_output: bool,
) {
    let banned_json =
        serde_json::to_string(&banned.iter().map(|s| s.to_string()).collect::<Vec<_>>()).unwrap();
    sqlx::query(
        r#"INSERT INTO guardrail_configs (
              id, tenant_id, name, enabled, is_default, priority,
              chat_input_enabled, chat_output_enabled,
              embeddings_input_enabled,
              image_gen_input_enabled, image_gen_output_enabled,
              audio_tts_input_enabled, audio_tts_output_enabled,
              audio_stt_input_enabled, audio_stt_output_enabled,
              video_gen_input_enabled, video_gen_output_enabled,
              batch_input_enabled, batch_output_enabled,
              moderation_input_enabled,
              mcp_input_enabled, mcp_output_enabled,
              mcp_registration_enabled,
              skill_input_enabled, skill_output_enabled,
              a2a_inbound_enabled, a2a_outbound_enabled,
              pii_detection_enabled, prompt_injection_enabled,
              jailbreak_detection_enabled, toxic_content_enabled,
              banned_words, banned_words_match_mode,
              fail_open, log_all_checks,
              streaming_output_mode, secrets_detection_enabled, secrets_action
           ) VALUES (
              $1, $2, $3, true, false, 100,
              $4, $5,
              false,
              false, false,
              false, false,
              false, false,
              false, false,
              false, false,
              false,
              false, false,
              false,
              false, false,
              false, false,
              false, false,
              false, false,
              $6::json, 'substring',
              false, true,
              'auto', false, 'redact'
           )"#,
    )
    .bind(id)
    .bind(tenant_id)
    .bind(format!("e2e-{id}"))
    .bind(chat_input)
    .bind(chat_output)
    .bind(&banned_json)
    .execute(pool)
    .await
    .expect("seed_llm_banned_words_guardrail");
}

/// Seed a built-in PII-detection guardrail on the chat input/output surfaces.
/// `action` is `block` / `redact` / `warn`.
pub async fn seed_llm_pii_guardrail(
    pool: &PgPool,
    id: &str,
    tenant_id: &str,
    action: &str,
    chat_input: bool,
    chat_output: bool,
) {
    sqlx::query(
        r#"INSERT INTO guardrail_configs (
              id, tenant_id, name, enabled, is_default, priority,
              chat_input_enabled, chat_output_enabled,
              embeddings_input_enabled,
              image_gen_input_enabled, image_gen_output_enabled,
              audio_tts_input_enabled, audio_tts_output_enabled,
              audio_stt_input_enabled, audio_stt_output_enabled,
              video_gen_input_enabled, video_gen_output_enabled,
              batch_input_enabled, batch_output_enabled,
              moderation_input_enabled,
              mcp_input_enabled, mcp_output_enabled,
              mcp_registration_enabled,
              skill_input_enabled, skill_output_enabled,
              a2a_inbound_enabled, a2a_outbound_enabled,
              pii_detection_enabled, pii_action, pii_provider,
              prompt_injection_enabled, jailbreak_detection_enabled, toxic_content_enabled,
              fail_open, log_all_checks,
              streaming_output_mode, secrets_detection_enabled, secrets_action
           ) VALUES (
              $1, $2, $3, true, false, 100,
              $4, $5,
              false,
              false, false,
              false, false,
              false, false,
              false, false,
              false, false,
              false,
              false, false,
              false,
              false, false,
              false, false,
              true, $6, 'built_in',
              false, false, false,
              false, true,
              'auto', false, 'redact'
           )"#,
    )
    .bind(id)
    .bind(tenant_id)
    .bind(format!("e2e-{id}"))
    .bind(chat_input)
    .bind(chat_output)
    .bind(action)
    .execute(pool)
    .await
    .expect("seed_llm_pii_guardrail");
}

/// Bind a guardrail config to a resource group.
pub async fn bind_guardrail_to_rg(pool: &PgPool, guardrail_id: &str, rg_id: &str) {
    sqlx::query(
        "INSERT INTO resource_group_guardrails \
         (resource_group_id, guardrail_config_id) \
         VALUES ($1, $2) ON CONFLICT DO NOTHING",
    )
    .bind(rg_id)
    .bind(guardrail_id)
    .execute(pool)
    .await
    .expect("bind_guardrail_to_rg");
}

/// Count `proxy_logs` rows for a tenant. Used to pin that a blocked request
/// produces exactly ONE audit row (no duplicate deferred row).
pub async fn count_proxy_logs(pool: &PgPool, tenant_id: &str) -> i64 {
    sqlx::query_as::<_, (i64,)>("SELECT COUNT(*) FROM proxy_logs WHERE tenant_id = $1")
        .bind(tenant_id)
        .fetch_one(pool)
        .await
        .map(|(n,)| n)
        .unwrap_or(-1)
}

/// Wait for a blocked request's audit row, then settle and assert there's
/// exactly ONE `proxy_logs` row for the tenant — i.e. the block helper's
/// self-logged row, with no duplicate deferred row.
pub async fn assert_single_audit_row(pool: &PgPool, tenant_id: &str) {
    use std::time::Duration;
    for _ in 0..40 {
        if count_proxy_logs(pool, tenant_id).await >= 1 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    // Let any duplicate row flush through the batched background writer.
    tokio::time::sleep(Duration::from_millis(400)).await;
    let count = count_proxy_logs(pool, tenant_id).await;
    assert_eq!(
        count, 1,
        "blocked request must produce exactly ONE audit row (no duplicate), got {count}"
    );
}

/// Encrypt + store an `api_key` on a model. Providers that REQUIRE a key
/// (e.g. Gemini's `resolve_gemini_endpoint`) 500 without one. Encrypts with
/// the same master key `AppConfig::for_test` uses, so `decrypt_model_api_key`
/// round-trips it back to `plaintext` at request time.
pub async fn set_model_api_key(pool: &PgPool, model_id: &str, plaintext: &str) {
    use brutor_gateway_core::services::encryption::EncryptionService;
    let enc = EncryptionService::new("test-encryption-key-32-chars-ok!");
    let ciphertext = enc.encrypt(plaintext).expect("encrypt api_key");
    sqlx::query("UPDATE llm_model SET api_key = $1 WHERE id = $2")
        .bind(ciphertext)
        .bind(model_id)
        .execute(pool)
        .await
        .expect("set_model_api_key");
}

/// Bind an llm_model to a resource_group via the join table.
pub async fn bind_llm_model_to_rg(pool: &PgPool, model_id: &str, rg_id: &str) {
    sqlx::query(
        "INSERT INTO resource_group_llm_models \
         (id, resource_group_id, llm_model_id, created_at) \
         VALUES (concat('rglm-', $1, '-', $2), $2, $1, now()) \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(model_id)
    .bind(rg_id)
    .execute(pool)
    .await
    .expect("bind_llm_model_to_rg");
}

/// Bind a model to a resource group with `portal_visible = false` — the
/// routing-member case: bound so the routing group can resolve to it, but
/// hidden from the end-user portal model picker.
pub async fn bind_llm_model_to_rg_hidden(pool: &PgPool, model_id: &str, rg_id: &str) {
    sqlx::query(
        "INSERT INTO resource_group_llm_models \
         (id, resource_group_id, llm_model_id, created_at, portal_visible) \
         VALUES (concat('rglm-', $1, '-', $2), $2, $1, now(), false) \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(model_id)
    .bind(rg_id)
    .execute(pool)
    .await
    .expect("bind_llm_model_to_rg_hidden");
}

/// A configuration the builder can connect with: the test's own database
/// and the suite's Redis.
pub fn connectable_config(pool: &PgPool) -> AppConfig {
    use sqlx::ConnectOptions;
    let mut config = AppConfig::for_test(pool.connect_options().to_url_lossy().to_string());
    config.redis_url = Some(
        std::env::var("TEST_REDIS_URL").unwrap_or_else(|_| "redis://127.0.0.1:6379/15".to_string()),
    );
    config
}

/// An instance lease of a test's own: a key no other test
/// has, on the Redis the suite shares, with the Community gateway's
/// timings. A test that builds a gateway through `GatewayBuilder::build`
/// passes it (`.instance_lease(common::test_lease())`): with the gateway's
/// own key, two such tests running at once would be two instances.
pub fn test_lease() -> brutor_gateway_core::services::instance_lease::LeaseSettings {
    brutor_gateway_core::services::instance_lease::LeaseSettings {
        key: format!("brutor:test:instance:{}", uuid::Uuid::new_v4().simple()),
        ..brutor_gateway_core::services::instance_lease::LeaseSettings::community()
    }
}

/// `Utc::now()` at Postgres precision (microseconds). Every integration test
/// takes the current time from here, never from `Utc::now()` directly.
///
/// Linux clocks carry nanoseconds; Postgres keeps microseconds. A time a test
/// writes and then compares with what it reads back differs in the last three
/// digits on a Linux CI runner and matches on macOS (whose clock is already
/// microsecond), so suites pass locally and fail in CI.
/// `integration_tests_take_time_from_db_now` guards it.
pub fn db_now() -> chrono::DateTime<chrono::Utc> {
    use chrono::Timelike;
    let now = chrono::Utc::now();
    now.with_nanosecond(now.nanosecond() / 1_000 * 1_000)
        .unwrap_or(now)
}

#[test]
fn integration_tests_take_time_from_db_now() {
    let dir = community_root().join("tests/integration");
    let mut offenders = Vec::new();
    for entry in std::fs::read_dir(dir).expect("tests/integration") {
        let path = entry.expect("entry").path();
        let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
        if !name.ends_with(".rs") || name == "common.rs" {
            continue;
        }
        let source = std::fs::read_to_string(&path).expect("read");
        if source.contains("Utc::now()") {
            offenders.push(name.to_string());
        }
    }
    assert!(
        offenders.is_empty(),
        "use crate::common::db_now() (Postgres precision), not Utc::now(): {offenders:?}"
    );
}
