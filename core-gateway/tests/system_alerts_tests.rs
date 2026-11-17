// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Integration tests for the `system_alerts` helper. These exercise
//! the real dedup semantics against a live Postgres: first raise
//! INSERTs, re-raise UPSERTs (bumps occurrence_count), resolved +
//! re-raise creates a fresh active row, acknowledged rows still
//! dedup.

mod test_helpers;

use sqlx::PgPool;
use sqlx::postgres::PgPoolOptions;

use brutor_gateway_core::services::system_alerts::{
    AlertCategory, Severity, SystemAlertInput, compute_dedup_key, raise,
};

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

/// Clean up rows created by a specific test run. We tag the source
/// with a unique suffix so parallel test runs don't clobber each
/// other.
async fn cleanup(db: &PgPool, tag: &str) {
    sqlx::query("DELETE FROM system_alerts WHERE source LIKE $1")
        .bind(format!("%{tag}%"))
        .execute(db)
        .await
        .ok();
}

fn unique_tag() -> String {
    format!("test-{}", ulid::Ulid::new().to_string().to_lowercase())
}

fn make_input(tag: &str, error_code: &str) -> SystemAlertInput {
    SystemAlertInput::downstream_provider(
        Some(format!("tenant-{tag}")),
        &format!("openai.{tag}"),
        Some(format!("model-{tag}")),
        Some("GPT-4 Test".to_string()),
        Some(error_code.to_string()),
        "integration-test provider failure",
    )
}

// ---------------------------------------------------------------------------
// First raise: INSERT new row
// ---------------------------------------------------------------------------

#[tokio::test]
async fn first_raise_inserts_with_occurrence_one() {
    let db = test_pool().await;
    let tag = unique_tag();
    cleanup(&db, &tag).await;

    let outcome = raise(&db, make_input(&tag, "503")).await.expect("raise");

    assert!(outcome.was_new, "first raise should be a new insert");
    assert_eq!(outcome.occurrence_count, 1);
    assert!(outcome.alert_id.starts_with("sysalert-"));

    // Verify the row is persisted with status=active.
    let (status, count): (String, i32) =
        sqlx::query_as("SELECT status, occurrence_count FROM system_alerts WHERE id = $1")
            .bind(&outcome.alert_id)
            .fetch_one(&db)
            .await
            .expect("fetch inserted row");

    assert_eq!(status, "active");
    assert_eq!(count, 1);

    cleanup(&db, &tag).await;
}

// ---------------------------------------------------------------------------
// Re-raise of same dedup_key: UPDATE (bumps occurrence_count, no new row)
// ---------------------------------------------------------------------------

#[tokio::test]
async fn re_raise_same_dedup_key_bumps_occurrence_count() {
    let db = test_pool().await;
    let tag = unique_tag();
    cleanup(&db, &tag).await;

    let first = raise(&db, make_input(&tag, "503")).await.expect("first");
    let second = raise(&db, make_input(&tag, "503")).await.expect("second");
    let third = raise(&db, make_input(&tag, "503")).await.expect("third");

    // All three should return the SAME alert_id — the partial unique
    // index forced the upsert to update the existing row.
    assert_eq!(first.alert_id, second.alert_id);
    assert_eq!(second.alert_id, third.alert_id);

    assert!(first.was_new);
    assert!(!second.was_new, "re-raise should be an update");
    assert!(!third.was_new);

    assert_eq!(third.occurrence_count, 3);

    // Only one row exists for this dedup_key.
    let dedup = compute_dedup_key(
        Some(&format!("tenant-{tag}")),
        AlertCategory::DownstreamProvider,
        &format!("llm.openai.{tag}"),
        Some("503"),
    );
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM system_alerts WHERE dedup_key = $1")
        .bind(&dedup)
        .fetch_one(&db)
        .await
        .expect("count");
    assert_eq!(count, 1);

    cleanup(&db, &tag).await;
}

// ---------------------------------------------------------------------------
// Different error_code → different dedup_key → separate active row
// ---------------------------------------------------------------------------

#[tokio::test]
async fn different_error_code_creates_separate_alert() {
    let db = test_pool().await;
    let tag = unique_tag();
    cleanup(&db, &tag).await;

    let a = raise(&db, make_input(&tag, "503")).await.expect("503");
    let b = raise(&db, make_input(&tag, "timeout"))
        .await
        .expect("timeout");

    assert!(a.was_new);
    assert!(b.was_new);
    assert_ne!(a.alert_id, b.alert_id);

    cleanup(&db, &tag).await;
}

// ---------------------------------------------------------------------------
// After resolve, a new occurrence INSERTs a fresh active row
// ---------------------------------------------------------------------------

#[tokio::test]
async fn resolved_row_does_not_block_new_occurrence() {
    let db = test_pool().await;
    let tag = unique_tag();
    cleanup(&db, &tag).await;

    let first = raise(&db, make_input(&tag, "503")).await.expect("first");

    // Manually mark the first row as resolved (simulates either the
    // admin UI ack-and-resolve flow or the auto-resolver sweep).
    sqlx::query(
        "UPDATE system_alerts SET status = 'resolved', resolved_at = NOW(), \
         resolved_by = 'test' WHERE id = $1",
    )
    .bind(&first.alert_id)
    .execute(&db)
    .await
    .expect("mark resolved");

    // Re-raise: should NOT hit the conflict (resolved rows aren't in
    // the partial unique index) and should create a fresh active row.
    let second = raise(&db, make_input(&tag, "503")).await.expect("second");
    assert!(second.was_new, "after resolve, new raise should insert");
    assert_ne!(first.alert_id, second.alert_id);
    assert_eq!(second.occurrence_count, 1);

    // Now we have two rows for the same dedup_key — one resolved,
    // one active. Verify.
    let dedup = compute_dedup_key(
        Some(&format!("tenant-{tag}")),
        AlertCategory::DownstreamProvider,
        &format!("llm.openai.{tag}"),
        Some("503"),
    );
    let rows: Vec<(String, String)> = sqlx::query_as(
        "SELECT id, status FROM system_alerts WHERE dedup_key = $1 ORDER BY first_seen_at",
    )
    .bind(&dedup)
    .fetch_all(&db)
    .await
    .expect("fetch history");

    assert_eq!(rows.len(), 2);
    assert_eq!(rows[0].1, "resolved");
    assert_eq!(rows[1].1, "active");

    cleanup(&db, &tag).await;
}

// ---------------------------------------------------------------------------
// Acknowledged row still dedups a re-raise (operator is already aware)
// ---------------------------------------------------------------------------

#[tokio::test]
async fn acknowledged_row_still_dedups() {
    let db = test_pool().await;
    let tag = unique_tag();
    cleanup(&db, &tag).await;

    let first = raise(&db, make_input(&tag, "503")).await.expect("first");

    sqlx::query(
        "UPDATE system_alerts SET status = 'acknowledged', \
         acknowledged_at = NOW(), acknowledged_by = 'test-admin' WHERE id = $1",
    )
    .bind(&first.alert_id)
    .execute(&db)
    .await
    .expect("ack");

    let second = raise(&db, make_input(&tag, "503")).await.expect("second");

    // Same alert ID, occurrence_count bumped, status stays acknowledged.
    assert_eq!(first.alert_id, second.alert_id);
    assert!(!second.was_new);
    assert_eq!(second.occurrence_count, 2);

    let status: String = sqlx::query_scalar("SELECT status FROM system_alerts WHERE id = $1")
        .bind(&first.alert_id)
        .fetch_one(&db)
        .await
        .expect("fetch status");
    assert_eq!(
        status, "acknowledged",
        "re-raise must not flip acknowledged back to active"
    );

    cleanup(&db, &tag).await;
}

// ---------------------------------------------------------------------------
// Message / title / context are refreshed on re-raise (latest wins)
// ---------------------------------------------------------------------------

#[tokio::test]
async fn re_raise_refreshes_message_and_context() {
    let db = test_pool().await;
    let tag = unique_tag();
    cleanup(&db, &tag).await;

    let first_input = make_input(&tag, "503").with_context(serde_json::json!({ "attempt": 1 }));
    let first = raise(&db, first_input).await.expect("first");

    let second_input = make_input(&tag, "503")
        .with_title("Provider now fully offline")
        .with_context(serde_json::json!({ "attempt": 2, "observed_for_secs": 180 }));
    let second = raise(&db, second_input).await.expect("second");

    assert_eq!(first.alert_id, second.alert_id);

    let (title, context): (String, serde_json::Value) =
        sqlx::query_as("SELECT title, context FROM system_alerts WHERE id = $1")
            .bind(&first.alert_id)
            .fetch_one(&db)
            .await
            .expect("fetch");

    assert_eq!(title, "Provider now fully offline");
    assert_eq!(context["attempt"], 2);
    assert_eq!(context["observed_for_secs"], 180);

    cleanup(&db, &tag).await;
}

// ---------------------------------------------------------------------------
// Internal error constructor maps to critical severity in the DB
// ---------------------------------------------------------------------------

#[tokio::test]
async fn internal_error_persists_as_critical() {
    let db = test_pool().await;
    let tag = unique_tag();
    cleanup(&db, &tag).await;

    let input = SystemAlertInput::internal_error(
        Some(format!("tenant-{tag}")),
        format!("portal_kb.test.{tag}"),
        Some("schema_drift".to_string()),
        "Test internal error",
    );
    let outcome = raise(&db, input).await.expect("raise");

    let (severity, category): (String, String) =
        sqlx::query_as("SELECT severity, category FROM system_alerts WHERE id = $1")
            .bind(&outcome.alert_id)
            .fetch_one(&db)
            .await
            .expect("fetch");

    assert_eq!(severity, "critical");
    assert_eq!(category, "internal_error");

    cleanup(&db, &tag).await;
}

// ---------------------------------------------------------------------------
// Platform-wide alert (tenant_id = NULL) persists correctly
// ---------------------------------------------------------------------------

#[tokio::test]
async fn platform_wide_alert_uses_null_tenant() {
    let db = test_pool().await;
    let tag = unique_tag();
    cleanup(&db, &tag).await;

    let input = SystemAlertInput::downstream_cache(
        &format!("redis.{tag}"),
        Some("connect_refused".to_string()),
        "Redis not reachable",
    )
    .with_severity(Severity::High);
    let outcome = raise(&db, input).await.expect("raise");

    let (tenant_id, severity): (Option<String>, String) =
        sqlx::query_as("SELECT tenant_id, severity FROM system_alerts WHERE id = $1")
            .bind(&outcome.alert_id)
            .fetch_one(&db)
            .await
            .expect("fetch");

    assert!(
        tenant_id.is_none(),
        "platform alerts must have NULL tenant_id"
    );
    assert_eq!(severity, "high");

    cleanup(&db, &tag).await;
}
