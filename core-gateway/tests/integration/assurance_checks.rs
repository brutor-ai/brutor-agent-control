// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Continuous Assurance Checks, tier A — against a
//! live Postgres.
//!
//! What is worth defending here:
//!
//!   * `evaluate_on_finalize` writes exactly one result row per
//!     (check version, run) — re-finalizing a reopened run updates in
//!     place rather than duplicating (the partial unique upsert).
//!   * A threshold breach raises a `check_breach` inbox item; a clean run
//!     does not.
//!   * Scope filtering: a check scoped to system A never evaluates a run
//!     attributed to system B.
//!   * The backtest evaluates a version SNAPSHOT (not the live row),
//!     writes only `backtest = true` sample rows, and raises nothing.
//!   * Cross-tenant isolation: tenant t2's checks never see t1's runs.

use brutor_gateway_core::services::assurance_checks::{
    backtest, evaluate_on_finalize, invalidate_tenant,
};
use brutor_gateway_core::services::run_ledger::sample_run_facts;
use sqlx::PgPool;

pub(crate) async fn insert_check(
    pool: &PgPool,
    tenant: &str,
    id: &str,
    expression: &str,
    scope: serde_json::Value,
    threshold: serde_json::Value,
) {
    sqlx::query(
        "INSERT INTO assurance_checks \
            (id, tenant_id, name, version, spec_hash, scope, tier, evaluator, \
             output, trigger, threshold, route_to_inbox, enabled) \
         VALUES ($1, $2, $3, 1, 'hash-' || $1, $4, 'deterministic', $5, \
                 'boolean', 'on_finalize', $6, true, true)",
    )
    .bind(id)
    .bind(tenant)
    .bind(format!("check {id}"))
    .bind(scope)
    .bind(serde_json::json!({ "expression": expression }))
    .bind(threshold)
    .execute(pool)
    .await
    .unwrap();
    // Version snapshot, as the CP publish flow writes it.
    sqlx::query(
        "INSERT INTO assurance_check_versions \
            (id, tenant_id, check_id, version, spec_hash, spec) \
         VALUES ('ckv-' || $1, $2, $1, 1, 'hash-' || $1, $3)",
    )
    .bind(id)
    .bind(tenant)
    .bind(serde_json::json!({
        "tier": "deterministic",
        "evaluator": { "expression": expression },
        "output": "boolean",
        "trigger": "on_finalize",
        "scope": {},
        "threshold": { "kind": "value", "op": "eq", "value": true,
                        "severity": "critical" },
    }))
    .execute(pool)
    .await
    .unwrap();
    invalidate_tenant(tenant);
}

pub(crate) fn breach_threshold() -> serde_json::Value {
    serde_json::json!({
        "kind": "value", "op": "eq", "value": true, "severity": "critical"
    })
}

pub(crate) async fn result_rows(
    pool: &PgPool,
    check_id: &str,
) -> Vec<(Option<String>, serde_json::Value, bool)> {
    sqlx::query_as(
        "SELECT run_id, value, backtest FROM assurance_check_results \
          WHERE check_id = $1 ORDER BY evaluated_at",
    )
    .bind(check_id)
    .fetch_all(pool)
    .await
    .unwrap()
}

pub(crate) async fn inbox_count(pool: &PgPool, tenant: &str, category: &str) -> i64 {
    sqlx::query_scalar(
        "SELECT COUNT(*) FROM assurance_inbox_items \
          WHERE tenant_id = $1 AND category = $2",
    )
    .bind(tenant)
    .bind(category)
    .fetch_one(pool)
    .await
    .unwrap()
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn on_finalize_records_result_and_raises_breach(pool: PgPool) {
    insert_check(
        &pool,
        "t1",
        "chk-breach",
        "run.guardrail_block_count > 0",
        serde_json::json!({}),
        breach_threshold(),
    )
    .await;

    // A clean run: result recorded, no inbox item.
    let clean = sample_run_facts();
    evaluate_on_finalize(&pool, "t1", Some("sys-1"), "run-clean", &clean).await;
    let rows = result_rows(&pool, "chk-breach").await;
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].1, serde_json::json!({ "value": false }));
    assert_eq!(inbox_count(&pool, "t1", "check_breach").await, 0);

    // A blocked run: breach raised.
    let mut blocked = sample_run_facts();
    blocked["guardrail_block_count"] = serde_json::json!(2);
    evaluate_on_finalize(&pool, "t1", Some("sys-1"), "run-blocked", &blocked).await;
    assert_eq!(inbox_count(&pool, "t1", "check_breach").await, 1);

    let fp: String = sqlx::query_scalar(
        "SELECT fingerprint FROM assurance_inbox_items \
          WHERE tenant_id = 't1' AND category = 'check_breach'",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(fp, "check_breach:chk-breach:v1");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn refinalize_updates_in_place(pool: PgPool) {
    insert_check(
        &pool,
        "t1",
        "chk-idem",
        "run.error_count > 0",
        serde_json::json!({}),
        breach_threshold(),
    )
    .await;

    let mut facts = sample_run_facts();
    evaluate_on_finalize(&pool, "t1", None, "run-1", &facts).await;
    // The run reopens with a late erroring action and re-finalizes.
    facts["error_count"] = serde_json::json!(1);
    evaluate_on_finalize(&pool, "t1", None, "run-1", &facts).await;

    let rows = result_rows(&pool, "chk-idem").await;
    assert_eq!(rows.len(), 1, "re-finalize must update, not duplicate");
    assert_eq!(rows[0].1, serde_json::json!({ "value": true }));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn scope_filter_skips_out_of_scope_runs(pool: PgPool) {
    insert_check(
        &pool,
        "t1",
        "chk-scoped",
        "run.action_count > 0",
        serde_json::json!({ "ai_system_ids": ["sys-a"] }),
        breach_threshold(),
    )
    .await;

    let facts = sample_run_facts();
    evaluate_on_finalize(&pool, "t1", Some("sys-b"), "run-b", &facts).await;
    evaluate_on_finalize(&pool, "t1", None, "run-none", &facts).await;
    assert!(result_rows(&pool, "chk-scoped").await.is_empty());

    evaluate_on_finalize(&pool, "t1", Some("sys-a"), "run-a", &facts).await;
    assert_eq!(result_rows(&pool, "chk-scoped").await.len(), 1);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn tenant_isolation(pool: PgPool) {
    insert_check(
        &pool,
        "t2",
        "chk-t2",
        "run.action_count > 0",
        serde_json::json!({}),
        breach_threshold(),
    )
    .await;

    // t1 has no checks: nothing recorded anywhere.
    evaluate_on_finalize(&pool, "t1", None, "run-t1", &sample_run_facts()).await;
    assert!(result_rows(&pool, "chk-t2").await.is_empty());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn backtest_uses_snapshot_writes_backtest_rows_raises_nothing(pool: PgPool) {
    insert_check(
        &pool,
        "t1",
        "chk-bt",
        "run.total_cost_usd > 0.5",
        serde_json::json!({}),
        breach_threshold(),
    )
    .await;

    // Two recorded runs: one over the cost line, one under.
    crate::common::seed_tenant(&pool, "t1").await;
    for (id, cost) in [("run-cheap", 0.01f64), ("run-dear", 2.5f64)] {
        sqlx::query(
            "INSERT INTO ai_system_runs \
                (id, tenant_id, root_task_id, started_at, ended_at, \
                 terminal_state, finalized_by, chain_integrity, action_count, \
                 llm_call_count, tool_call_count, skill_call_count, \
                 a2a_call_count, turn_count, total_cost_usd, is_replay) \
             VALUES ($1, 't1', 'task-' || $1, NOW() - interval '1 hour', \
                     NOW() - interval '30 minutes', 'completed', 'explicit', \
                     'intact', 3, 1, 1, 0, 0, 1, $2, false)",
        )
        .bind(id)
        .bind(cost)
        .execute(&pool)
        .await
        .unwrap();
    }

    let summary = backtest(&pool, "t1", "chk-bt", 1, 7).await.unwrap();
    assert_eq!(summary.evaluated, 2);
    assert_eq!(summary.breaches, 1);
    assert_eq!(summary.errors, 0);

    // Sample rows persisted, all backtest-flagged; no inbox item raised.
    let rows = result_rows(&pool, "chk-bt").await;
    assert!(!rows.is_empty());
    assert!(rows.iter().all(|(_, _, bt)| *bt));
    assert_eq!(inbox_count(&pool, "t1", "check_breach").await, 0);

    // Unknown version is a clean error, not a panic.
    let err = backtest(&pool, "t1", "chk-bt", 99, 7).await.unwrap_err();
    assert!(err.contains("not found"), "{err}");
}
