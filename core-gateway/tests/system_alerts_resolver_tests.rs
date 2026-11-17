// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Integration tests for the system_alerts auto-resolver. These
//! exercise the SQL predicates directly (via `sweep_once`) against
//! a live Postgres, using crafted fixtures in `system_alerts`,
//! `mcp_servers`, and `llm_models`.
//!
//! Each test uses a ULID-based tag so parallel test runs don't
//! clobber each other's fixtures.

mod test_helpers;

use chrono::{Duration as ChronoDuration, Utc};
use sqlx::PgPool;
use sqlx::postgres::PgPoolOptions;

use brutor_gateway_core::services::system_alerts::{
    AlertCategory, Severity, SystemAlertInput, raise,
};
use brutor_gateway_core::services::system_alerts_resolver::sweep_once;

async fn test_pool() -> PgPool {
    let url = test_helpers::test_database_url();
    let pool = PgPoolOptions::new()
        .max_connections(2)
        .connect(&url)
        .await
        .expect("connect to test database");
    // This binary opens its own pool rather than going through
    // `spawn_app`, so nothing else here creates the tables. Without this
    // it passes only when another test binary has already run against the
    // same database — and cargo stops at the first failing binary, so that
    // dependency stays invisible until something upstream is fixed.
    test_helpers::ensure_test_schema(&pool).await;
    pool
}

/// Every test in this file exercises `sweep_once`, which inspects
/// **all** active `system_alerts` rows regardless of tenant. Without
/// serialisation, Test A's sweep can resolve Test B's alert before B
/// gets to assert on its own state.
///
/// Under nextest (one process per test) the `shared-db-serial` test group
/// in `.config/nextest.toml` runs these one at a time; the process-local
/// async Mutex below does the same under `cargo test`, where they share a
/// process. It is taken at the top of each test (via
/// [`acquire_test_lock`]) and held for the test's whole lifetime. A DB-level `pg_advisory_lock` doesn't work because sqlx'
/// `PgPool` hands out a different connection for each query, and
/// advisory locks are session-scoped.
use std::sync::OnceLock;
use tokio::sync::{Mutex as AsyncMutex, MutexGuard};

fn sweep_mutex() -> &'static AsyncMutex<()> {
    static LOCK: OnceLock<AsyncMutex<()>> = OnceLock::new();
    LOCK.get_or_init(|| AsyncMutex::new(()))
}

/// Acquire the file-wide sweep lock and, on first call per test-binary
/// invocation, purge orphan fixtures from any prior panicked run.
/// Returns a guard that must be held for the rest of the test.
async fn acquire_test_lock() -> MutexGuard<'static, ()> {
    // Take the lock BEFORE the purge so we don't race a test that
    // might already be inserting.
    let guard = sweep_mutex().lock().await;
    let pool = test_pool().await;
    purge_orphans_once(&pool).await;
    guard
}

/// Sweep up orphan fixture rows from any previous test run that panicked
/// before its cleanup ran. Runs at most once per test-binary invocation.
///
/// Without this, a panicked earlier run leaves rows behind that:
///   * Break the admin `/v1/admin/mcp-servers` endpoint (the Pydantic
///     `ServerListResponse` rejects NULL `auth_type` / `timeout`).
///   * Inflate the `sweep_once` candidate set, masking real regressions.
///
/// The `rst-` prefix is unique to `unique_tag()` in this file, so this
/// targets our test data only — never touches real fixtures.
async fn purge_orphans_once(db: &PgPool) {
    use std::sync::atomic::{AtomicBool, Ordering};
    static DONE: AtomicBool = AtomicBool::new(false);
    if DONE.swap(true, Ordering::SeqCst) {
        return;
    }
    sqlx::query("DELETE FROM system_alerts WHERE source LIKE '%rst-%'")
        .execute(db)
        .await
        .ok();
    sqlx::query("DELETE FROM mcp_servers WHERE id LIKE 'srv-rst-%'")
        .execute(db)
        .await
        .ok();
    sqlx::query("DELETE FROM llm_model WHERE id LIKE 'm-rst-%'")
        .execute(db)
        .await
        .ok();
}

fn unique_tag() -> String {
    format!("rst-{}", ulid::Ulid::new().to_string().to_lowercase())
}

async fn cleanup(db: &PgPool, tag: &str) {
    // Alerts reference resources by id only, no FK — safe to clear any order.
    sqlx::query("DELETE FROM system_alerts WHERE source LIKE $1")
        .bind(format!("%{tag}%"))
        .execute(db)
        .await
        .ok();
    sqlx::query("DELETE FROM mcp_servers WHERE id LIKE $1")
        .bind(format!("%{tag}%"))
        .execute(db)
        .await
        .ok();
    sqlx::query("DELETE FROM llm_model WHERE id LIKE $1")
        .bind(format!("%{tag}%"))
        .execute(db)
        .await
        .ok();
}

/// Return a tenant id that exists in the target DB. The test DB may be a
/// full dev schema (every NOT NULL column populated by real data) or a
/// minimal CI schema (only id + name required). Rather than reproduce the
/// real tenant fixture shape, we reuse whatever's already there. Falls
/// back to inserting a row using only the two always-required columns.
async fn any_tenant(db: &PgPool) -> String {
    if let Ok(Some((id,))) =
        sqlx::query_as::<_, (String,)>("SELECT id FROM tenants ORDER BY created_at LIMIT 1")
            .fetch_optional(db)
            .await
    {
        return id;
    }
    sqlx::query(
        "INSERT INTO tenants (id, name) VALUES ('default', 'default') ON CONFLICT DO NOTHING",
    )
    .execute(db)
    .await
    .expect("insert fallback tenant");
    "default".to_string()
}

async fn insert_mcp_server(
    db: &PgPool,
    id: &str,
    tenant_id: &str,
    health_status: &str,
    last_success_secs_ago: Option<i64>,
) {
    let last_success = last_success_secs_ago.map(|s| Utc::now() - ChronoDuration::seconds(s));
    // Set `auth_type` and `timeout` explicitly. They're nullable at the
    // DB level but the admin ServerListResponse Pydantic model rejects
    // NULLs, so a leaked orphan row (e.g. after a test panic) would
    // break the admin UI's server list endpoint. Defensive test-data
    // hygiene: don't hand the production API shapes it can't parse,
    // even for rows that *should* have been cleaned up.
    sqlx::query(
        "INSERT INTO mcp_servers (id, tenant_id, name, base_url, mcp_endpoint_path, \
         auth_type, timeout, enabled, health_status, last_health_check, \
         last_health_success, consecutive_health_failures) \
         VALUES ($1, $2, $3, 'http://test.invalid', '/mcp', 'none', 30.0, true, \
         $4, NOW(), $5, 0)",
    )
    .bind(id)
    .bind(tenant_id)
    .bind(format!("test-{id}"))
    .bind(health_status)
    .bind(last_success)
    .execute(db)
    .await
    .expect("insert mcp_server");
}

async fn insert_llm_model(
    db: &PgPool,
    id: &str,
    tenant_id: &str,
    health_status: &str,
    last_success_secs_ago: Option<i64>,
) {
    let last_success = last_success_secs_ago.map(|s| Utc::now() - ChronoDuration::seconds(s));
    // Minimal NOT NULL set on `llm_model` in dev: id, tenant_id, name,
    // provider, model_name, mode, api_url_auto_discovered. Set
    // auth_type='none' for the same defensive reason as in
    // Seed a provider row first — `llm_model.provider_id` is a NOT NULL
    // FK since the provider-normalisation refactor that retired the
    // free-text `llm_model.provider` column.
    // llm_model.provider_id is a NOT NULL FK. Each helper invocation
    // upserts an `openai` provider for the tenant — the unique key is
    // (tenant_id, key), NOT id, so an existing row gets reused and we
    // look up the id rather than minting a fresh one.
    let provider_id: String = sqlx::query_scalar(
        "INSERT INTO llm_providers (id, tenant_id, key, display_name, enabled, is_system) \
         VALUES ($1, $2, 'openai', 'OpenAI', true, false) \
         ON CONFLICT (tenant_id, key) DO UPDATE SET display_name = EXCLUDED.display_name \
         RETURNING id",
    )
    .bind(format!("prov-{id}"))
    .bind(tenant_id)
    .fetch_one(db)
    .await
    .expect("upsert llm_providers");

    // insert llm_model — leaked rows should still parse through the
    // admin API. `thinking_enabled` is NOT NULL (added by a later
    // Alembic migration) so seed false explicitly.
    sqlx::query(
        "INSERT INTO llm_model (id, tenant_id, name, model_name, provider_id, mode, \
         api_url_auto_discovered, auth_type, thinking_enabled, \
         health_status, last_health_check, \
         last_health_success, consecutive_health_failures) \
         VALUES ($1, $2, $3, $3, $4, 'chat', false, 'none', false, $5, NOW(), $6, 0)",
    )
    .bind(id)
    .bind(tenant_id)
    .bind(format!("test-{id}"))
    .bind(&provider_id)
    .bind(health_status)
    .bind(last_success)
    .execute(db)
    .await
    .expect("insert llm_model");
}

/// Hand-craft a resource_health alert with a given source/resource_id
/// so the fixture joins back to an mcp_server/llm_model row we control.
async fn raise_resource_health_alert(
    db: &PgPool,
    tenant_id: &str,
    resource_type: &str,
    resource_id: &str,
    source: &str,
) -> String {
    let mut input = SystemAlertInput::resource_health_change(
        Some(tenant_id.to_string()),
        resource_type,
        resource_id,
        None,
        "unhealthy",
        "test: resource went unhealthy",
    );
    // Override the default `source` so cleanup can scope by tag.
    input.source = source.to_string();
    raise(db, input).await.expect("raise").alert_id
}

/// Push an alert's last_seen_at back in time — simulates a downstream
/// outage that happened N seconds ago and hasn't recurred since.
async fn age_last_seen(db: &PgPool, alert_id: &str, secs: i64) {
    sqlx::query(
        "UPDATE system_alerts SET last_seen_at = NOW() - MAKE_INTERVAL(secs => $1) WHERE id = $2",
    )
    .bind(secs)
    .bind(alert_id)
    .execute(db)
    .await
    .expect("age alert");
}

async fn fetch_status(db: &PgPool, alert_id: &str) -> String {
    sqlx::query_scalar("SELECT status FROM system_alerts WHERE id = $1")
        .bind(alert_id)
        .fetch_one(db)
        .await
        .expect("fetch status")
}

// ===========================================================================
// Health recovery: auto-resolver closes alert when underlying resource
// has been healthy for >= 5 minutes.
// ===========================================================================

#[tokio::test]
async fn mcp_server_healthy_for_window_resolves_alert() {
    let _lock = acquire_test_lock().await;
    let db = test_pool().await;
    let tag = unique_tag();
    cleanup(&db, &tag).await;

    let tenant = any_tenant(&db).await;
    let server = format!("srv-{tag}");
    let source = format!("mcp.server.{tag}");

    // Underlying server is healthy and recovered > 10 min ago.
    insert_mcp_server(&db, &server, &tenant, "healthy", Some(10 * 60)).await;
    let alert_id = raise_resource_health_alert(&db, &tenant, "mcp_server", &server, &source).await;

    let report = sweep_once(&db).await.expect("sweep");
    assert!(report.health_resolved >= 1);
    assert_eq!(fetch_status(&db, &alert_id).await, "resolved");

    // `tenant` unused in assertions but referenced by the fixture path;
    // silence the unused-var warning without removing context from the test.
    let _ = tenant;

    cleanup(&db, &tag).await;
}

#[tokio::test]
async fn llm_model_healthy_for_window_resolves_alert() {
    let _lock = acquire_test_lock().await;
    let db = test_pool().await;
    let tag = unique_tag();
    cleanup(&db, &tag).await;

    let tenant = any_tenant(&db).await;
    let model = format!("m-{tag}");
    let source = format!("llm.model.{tag}");
    insert_llm_model(&db, &model, &tenant, "healthy", Some(10 * 60)).await;
    let alert_id = raise_resource_health_alert(&db, &tenant, "llm_model", &model, &source).await;

    sweep_once(&db).await.expect("sweep");
    assert_eq!(fetch_status(&db, &alert_id).await, "resolved");

    cleanup(&db, &tag).await;
}

#[tokio::test]
async fn resource_still_unhealthy_keeps_alert_active() {
    let _lock = acquire_test_lock().await;
    let db = test_pool().await;
    let tag = unique_tag();
    cleanup(&db, &tag).await;

    let tenant = any_tenant(&db).await;
    let server = format!("srv-{tag}");
    let source = format!("mcp.server.{tag}");
    insert_mcp_server(&db, &server, &tenant, "unhealthy", None).await;
    let alert_id = raise_resource_health_alert(&db, &tenant, "mcp_server", &server, &source).await;

    sweep_once(&db).await.expect("sweep");

    // Must stay active — resource hasn't recovered.
    assert_eq!(fetch_status(&db, &alert_id).await, "active");

    cleanup(&db, &tag).await;
}

#[tokio::test]
async fn resource_healthy_only_briefly_does_not_resolve() {
    let _lock = acquire_test_lock().await;
    let db = test_pool().await;
    let tag = unique_tag();
    cleanup(&db, &tag).await;

    let tenant = any_tenant(&db).await;
    let server = format!("srv-{tag}");
    let source = format!("mcp.server.{tag}");
    // Healthy for only 60 seconds — below the 5-minute recovery window.
    insert_mcp_server(&db, &server, &tenant, "healthy", Some(60)).await;
    let alert_id = raise_resource_health_alert(&db, &tenant, "mcp_server", &server, &source).await;

    sweep_once(&db).await.expect("sweep");

    assert_eq!(
        fetch_status(&db, &alert_id).await,
        "active",
        "alert must not resolve before the recovery window has elapsed"
    );

    cleanup(&db, &tag).await;
}

#[tokio::test]
async fn acknowledged_health_alert_is_never_auto_resolved() {
    let _lock = acquire_test_lock().await;
    let db = test_pool().await;
    let tag = unique_tag();
    cleanup(&db, &tag).await;

    let tenant = any_tenant(&db).await;
    let server = format!("srv-{tag}");
    let source = format!("mcp.server.{tag}");
    insert_mcp_server(&db, &server, &tenant, "healthy", Some(10 * 60)).await;
    let alert_id = raise_resource_health_alert(&db, &tenant, "mcp_server", &server, &source).await;

    sqlx::query(
        "UPDATE system_alerts SET status = 'acknowledged', acknowledged_at = NOW(), \
         acknowledged_by = 'test-operator' WHERE id = $1",
    )
    .bind(&alert_id)
    .execute(&db)
    .await
    .expect("ack");

    sweep_once(&db).await.expect("sweep");

    assert_eq!(
        fetch_status(&db, &alert_id).await,
        "acknowledged",
        "acknowledged alerts must never auto-resolve — operator owns them"
    );

    cleanup(&db, &tag).await;
}

// ===========================================================================
// Downstream quiet-window: auto-resolver closes alert that hasn't
// recurred in >= 15 minutes.
// ===========================================================================

#[tokio::test]
async fn downstream_alert_stale_beyond_quiet_window_resolves() {
    let _lock = acquire_test_lock().await;
    let db = test_pool().await;
    let tag = unique_tag();
    cleanup(&db, &tag).await;

    let tenant = format!("tenant-{tag}");
    let mut input = SystemAlertInput::downstream_provider(
        Some(tenant.clone()),
        "openai-test",
        Some(format!("m-{tag}")),
        None,
        Some("503".to_string()),
        "test: provider 503",
    );
    input.source = format!("llm.openai.{tag}");
    let outcome = raise(&db, input).await.expect("raise");

    // Push the alert's last_seen 20 minutes into the past.
    age_last_seen(&db, &outcome.alert_id, 20 * 60).await;

    sweep_once(&db).await.expect("sweep");

    assert_eq!(fetch_status(&db, &outcome.alert_id).await, "resolved");

    cleanup(&db, &tag).await;
}

#[tokio::test]
async fn recent_downstream_alert_stays_active() {
    let _lock = acquire_test_lock().await;
    let db = test_pool().await;
    let tag = unique_tag();
    cleanup(&db, &tag).await;

    let tenant = format!("tenant-{tag}");
    let mut input = SystemAlertInput::downstream_cache(
        &format!("redis.{tag}"),
        Some("timeout".to_string()),
        "test: redis timing out",
    );
    input.source = format!("cache.redis.{tag}");
    // Platform-wide; tenant_id stays None.
    let _ = tenant; // keep the variable for symmetry with other tests
    let outcome = raise(&db, input).await.expect("raise");

    // Age it only 5 minutes — below the 15-minute quiet window.
    age_last_seen(&db, &outcome.alert_id, 5 * 60).await;

    sweep_once(&db).await.expect("sweep");

    assert_eq!(fetch_status(&db, &outcome.alert_id).await, "active");

    cleanup(&db, &tag).await;
}

#[tokio::test]
async fn acknowledged_downstream_alert_is_never_auto_resolved() {
    let _lock = acquire_test_lock().await;
    let db = test_pool().await;
    let tag = unique_tag();
    cleanup(&db, &tag).await;

    let mut input = SystemAlertInput::downstream_idp(
        Some(format!("tenant-{tag}")),
        &format!("idp-{tag}"),
        None,
        Some("connect_refused".to_string()),
        "test: IdP down",
    );
    input.source = format!("idp.idp-{tag}");
    let outcome = raise(&db, input).await.expect("raise");

    age_last_seen(&db, &outcome.alert_id, 60 * 60).await; // 1h old

    sqlx::query(
        "UPDATE system_alerts SET status = 'acknowledged', acknowledged_at = NOW(), \
         acknowledged_by = 'test-operator' WHERE id = $1",
    )
    .bind(&outcome.alert_id)
    .execute(&db)
    .await
    .expect("ack");

    sweep_once(&db).await.expect("sweep");

    assert_eq!(fetch_status(&db, &outcome.alert_id).await, "acknowledged");

    cleanup(&db, &tag).await;
}

#[tokio::test]
async fn infrastructure_threshold_alerts_are_not_auto_resolved() {
    // The resolver intentionally ignores `infrastructure_threshold` and
    // `internal_error` — these belong to other lifecycle owners.
    let _lock = acquire_test_lock().await;
    let db = test_pool().await;
    let tag = unique_tag();
    cleanup(&db, &tag).await;

    let mut input = SystemAlertInput::infrastructure_threshold(
        &format!("cpu.{tag}"),
        95.0,
        80.0,
        "test: CPU spike",
    );
    input.source = format!("monitor.cpu.{tag}");
    let outcome = raise(&db, input).await.expect("raise");

    age_last_seen(&db, &outcome.alert_id, 60 * 60).await;

    sweep_once(&db).await.expect("sweep");

    assert_eq!(
        fetch_status(&db, &outcome.alert_id).await,
        "active",
        "infrastructure_threshold must not be touched by the auto-resolver"
    );

    cleanup(&db, &tag).await;
}

#[tokio::test]
async fn internal_error_alerts_are_not_auto_resolved() {
    let _lock = acquire_test_lock().await;
    let db = test_pool().await;
    let tag = unique_tag();
    cleanup(&db, &tag).await;

    let mut input = SystemAlertInput::internal_error(
        Some(format!("tenant-{tag}")),
        format!("portal_kb.test.{tag}"),
        Some("schema_drift".to_string()),
        "test: schema drift",
    );
    input.source = format!("portal_kb.test.{tag}");
    let outcome = raise(&db, input).await.expect("raise");

    age_last_seen(&db, &outcome.alert_id, 60 * 60).await;

    sweep_once(&db).await.expect("sweep");

    assert_eq!(
        fetch_status(&db, &outcome.alert_id).await,
        "active",
        "internal_error must be dismissed by a human — never auto-resolved"
    );

    cleanup(&db, &tag).await;
}

// ===========================================================================
// Sanity: the category enum string matches what the resolver's SQL
// greps for. A drift here would silently break auto-resolution.
// ===========================================================================

#[test]
fn category_enum_matches_resolver_sql_constants() {
    // Mirror of the IN (...) list in resolve_stale_downstream_alerts.
    let expected = [
        "downstream_provider",
        "downstream_idp",
        "downstream_cache",
        "downstream_vector_db",
    ];
    let actual: Vec<&str> = [
        AlertCategory::DownstreamProvider.as_str(),
        AlertCategory::DownstreamIdp.as_str(),
        AlertCategory::DownstreamCache.as_str(),
        AlertCategory::DownstreamVectorDb.as_str(),
    ]
    .to_vec();
    assert_eq!(actual, expected);

    // Ensure ResourceHealth string also matches.
    assert_eq!(AlertCategory::ResourceHealth.as_str(), "resource_health");

    // Non-auto-resolve categories.
    assert_eq!(
        AlertCategory::InfrastructureThreshold.as_str(),
        "infrastructure_threshold"
    );
    assert_eq!(AlertCategory::InternalError.as_str(), "internal_error");
    // Severity enum mirror test for completeness.
    assert_eq!(Severity::Critical.as_str(), "critical");
}
