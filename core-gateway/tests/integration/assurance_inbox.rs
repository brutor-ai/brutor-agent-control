// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Assurance Inbox core-side emitters.
//!
//! What is worth defending against a live Postgres:
//!
//!   * The dedup upsert really is race-safe against the PARTIAL unique
//!     index: a repeat raise bumps the ONE open row (occurrence_count,
//!     last_seen, severity max via the CASE — 'critical' < 'info'
//!     lexically, so a naive GREATEST would downgrade).
//!   * Exactly one `created` trail event per item — repeats add none
//!     (the counter is the record on the core side).
//!   * A RESOLVED row does not absorb new occurrences: the partial index
//!     lets a fresh item insert alongside it.
//!   * `expire_pending_approvals` both expires AND raises inbox items,
//!     and keeps returning the expired count.

use brutor_gateway_core::routes::approvals::expire_pending_approvals;
use brutor_gateway_core::services::assurance_inbox::{
    SEV_CRITICAL, SEV_INFO, SEV_WARNING, raise_item, raise_run_failure,
};
use sqlx::PgPool;

async fn item_row(pool: &PgPool, fingerprint: &str) -> Vec<(String, String, i32, String)> {
    sqlx::query_as(
        "SELECT id, severity, occurrence_count, state FROM assurance_inbox_items \
         WHERE tenant_id = 't1' AND fingerprint = $1 ORDER BY first_seen_at",
    )
    .bind(fingerprint)
    .fetch_all(pool)
    .await
    .unwrap()
}

async fn event_count(pool: &PgPool, item_id: &str, event: &str) -> i64 {
    sqlx::query_scalar(
        "SELECT COUNT(*) FROM assurance_inbox_events WHERE item_id = $1 AND event = $2",
    )
    .bind(item_id)
    .bind(event)
    .fetch_one(pool)
    .await
    .unwrap()
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn dedup_upsert_bumps_one_open_row(pool: PgPool) {
    let fp = "drift:sys-1:behavioural:step_count";
    let first = raise_item(
        &pool,
        "t1",
        Some("sys-1"),
        "drift",
        SEV_WARNING,
        fp,
        "Drift detected",
        serde_json::json!({"a": 1}),
    )
    .await
    .expect("first raise");
    let second = raise_item(
        &pool,
        "t1",
        Some("sys-1"),
        "drift",
        SEV_WARNING,
        fp,
        "Drift detected",
        serde_json::json!({"b": 2}),
    )
    .await
    .expect("second raise");
    assert_eq!(first, second, "repeat raise must hit the same open item");

    let rows = item_row(&pool, fp).await;
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].2, 2, "occurrence_count bumped");
    // Evidence merged, not replaced.
    let evidence: serde_json::Value =
        sqlx::query_scalar("SELECT evidence FROM assurance_inbox_items WHERE id = $1")
            .bind(&first)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(evidence["a"], 1);
    assert_eq!(evidence["b"], 2);
    // Exactly one created event.
    assert_eq!(event_count(&pool, &first, "created").await, 1);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn severity_escalates_and_never_downgrades(pool: PgPool) {
    let fp = "guardrail_block:rg-1:cfg-1:pii";
    raise_item(
        &pool,
        "t1",
        None,
        "guardrail_block",
        SEV_INFO,
        fp,
        "t",
        serde_json::json!({}),
    )
    .await
    .unwrap();
    raise_item(
        &pool,
        "t1",
        None,
        "guardrail_block",
        SEV_CRITICAL,
        fp,
        "t",
        serde_json::json!({}),
    )
    .await
    .unwrap();
    // A later info hit must not downgrade the escalated item.
    raise_item(
        &pool,
        "t1",
        None,
        "guardrail_block",
        SEV_INFO,
        fp,
        "t",
        serde_json::json!({}),
    )
    .await
    .unwrap();
    let rows = item_row(&pool, fp).await;
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].1, "critical");
    assert_eq!(rows[0].2, 3);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn resolved_items_do_not_absorb_new_occurrences(pool: PgPool) {
    let fp = "liveness:sys-1:silence";
    let first = raise_item(
        &pool,
        "t1",
        Some("sys-1"),
        "liveness",
        SEV_CRITICAL,
        fp,
        "Silent",
        serde_json::json!({}),
    )
    .await
    .unwrap();
    sqlx::query(
        "UPDATE assurance_inbox_items SET state = 'resolved', disposition = 'fixed', \
         resolution_note = 'restarted', resolved_at = NOW(), resolved_by = 'op' \
         WHERE id = $1",
    )
    .bind(&first)
    .execute(&pool)
    .await
    .unwrap();

    let fresh = raise_item(
        &pool,
        "t1",
        Some("sys-1"),
        "liveness",
        SEV_CRITICAL,
        fp,
        "Silent",
        serde_json::json!({}),
    )
    .await
    .unwrap();
    assert_ne!(
        first, fresh,
        "a resolved row must not be reopened by a new signal"
    );
    let rows = item_row(&pool, fp).await;
    assert_eq!(rows.len(), 2);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn run_failures_raise_only_for_errored_and_exhausted(pool: PgPool) {
    raise_run_failure(&pool, "t1", Some("sys-1"), "run-1", "completed", None).await;
    // A refused run is errored/blocked_by_policy: the guardrail
    // emitter's sharper item, not a second run-failure item.
    raise_run_failure(
        &pool,
        "t1",
        Some("sys-1"),
        "run-2",
        "errored",
        Some("blocked_by_policy"),
    )
    .await;
    raise_run_failure(
        &pool,
        "t1",
        Some("sys-1"),
        "run-3",
        "errored",
        Some("model_unavailable"),
    )
    .await;
    raise_run_failure(
        &pool,
        "t1",
        Some("sys-1"),
        "run-4",
        "exhausted",
        Some("iteration_limit"),
    )
    .await;

    let categories: Vec<(String,)> = sqlx::query_as(
        "SELECT fingerprint FROM assurance_inbox_items WHERE tenant_id = 't1' ORDER BY fingerprint",
    )
    .fetch_all(&pool)
    .await
    .unwrap();
    assert_eq!(
        categories.iter().map(|c| c.0.as_str()).collect::<Vec<_>>(),
        vec!["run_failure:sys-1:errored", "run_failure:sys-1:exhausted",],
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn approval_expiry_sweep_raises_inbox_items(pool: PgPool) {
    sqlx::query(
        r#"INSERT INTO tool_approval_requests
           (id, tenant_id, server_config_id, server_name, capability_type,
            capability_name, requester_type, requester_group_id, status,
            timeout_seconds, expires_at, created_at)
           VALUES
           ('apr-1', 't1', 'srv-1', 'payments', 'tool', 'send_payment',
            'user', 'rg-1', 'pending', 60,
            NOW() - INTERVAL '5 minutes', NOW() - INTERVAL '6 minutes'),
           ('apr-2', 't1', 'srv-1', 'payments', 'tool', 'send_payment',
            'user', 'rg-1', 'pending', 60,
            NOW() + INTERVAL '1 hour', NOW())"#,
    )
    .execute(&pool)
    .await
    .expect("seed approvals");

    let expired = expire_pending_approvals(&pool).await.unwrap();
    assert_eq!(expired, 1, "only the past-deadline request expires");

    let status: String =
        sqlx::query_scalar("SELECT status FROM tool_approval_requests WHERE id = 'apr-1'")
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(status, "expired");

    let items = item_row(&pool, "approval_timeout:send_payment").await;
    assert_eq!(items.len(), 1);
    assert_eq!(items[0].1, "info");
}
