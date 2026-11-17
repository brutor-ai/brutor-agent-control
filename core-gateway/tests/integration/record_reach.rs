// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Cross-run record reach against real Postgres.
//!
//! The slow-exfiltration pattern: 25 runs, each in scope for its
//! own customer, all opened by one requester. No per-run control can object.
//! The count across runs can.

use chrono::{DateTime, Duration, Utc};
use serde_json::json;
use sqlx::PgPool;

use brutor_gateway_core::services::{data_scope, record_reach};

use crate::common;

const T: &str = "tenant-reach";
const SYS: &str = "sys-reach";

async fn system(pool: &PgPool) {
    common::seed_tenant(pool, T).await;
    sqlx::query(
        "INSERT INTO resource_groups (id, tenant_id, hierarchy_level, inherit_from_parent, \
         name, is_active, is_system, group_type, system_kind) \
         VALUES ($1, $2, 0, true, $1, true, false, 'AI_SYSTEM', 'agent')",
    )
    .bind(SYS)
    .bind(T)
    .execute(pool)
    .await
    .unwrap();
}

async fn rule(pool: &PgPool, id: &str, group_by: &str, max: i32) {
    sqlx::query(
        "INSERT INTO ai_system_reach_rules \
            (id, tenant_id, ai_system_id, scope_key, group_by, window_hours, max_distinct, severity) \
         VALUES ($1, $2, $3, 'customer', $4, 24, $5, 'high')",
    )
    .bind(id)
    .bind(T)
    .bind(SYS)
    .bind(group_by)
    .bind(max)
    .execute(pool)
    .await
    .unwrap();
}

/// One run for `requester` that read `customer`, `ago` before now.
async fn run(
    pool: &PgPool,
    run: &str,
    requester: Option<&str>,
    customer: &str,
    verdict: &str,
    at: DateTime<Utc>,
) {
    if let Some(r) = requester {
        sqlx::query(
            "INSERT INTO ai_system_run_scopes (id, tenant_id, root_task_id, scope_key, scope_values, declared_at) \
             VALUES ($1, $2, $3, 'requester', $4, $5)",
        )
        .bind(format!("rs-{run}"))
        .bind(T)
        .bind(run)
        .bind(json!([r]))
        .bind(at)
        .execute(pool)
        .await
        .unwrap();
    }
    sqlx::query(
        "INSERT INTO ai_system_record_access \
            (id, tenant_id, ai_system_id, root_task_id, tool, scope_key, record_ref, verdict, mode, accessed_at) \
         VALUES ($1, $2, $3, $4, 'crm_get_customer', 'customer', $5, $6, 'audit', $7)",
    )
    .bind(format!("ra-{run}"))
    .bind(T)
    .bind(SYS)
    .bind(run)
    .bind(data_scope::record_ref(T, "customer", customer))
    .bind(verdict)
    .bind(at)
    .execute(pool)
    .await
    .unwrap();
}

async fn reach_events(pool: &PgPool) -> Vec<(String, f64, serde_json::Value)> {
    sqlx::query_as(
        "SELECT metric_key, observed_value, cause_evidence FROM ai_system_drift_events \
          WHERE tenant_id = $1 AND metric_key LIKE 'reach:%' ORDER BY detected_at",
    )
    .bind(T)
    .fetch_all(pool)
    .await
    .unwrap()
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn twenty_five_in_scope_runs_by_one_requester_are_one_finding(pool: PgPool) {
    system(&pool).await;
    rule(&pool, "rrl-1", "scope:requester", 20).await;
    let now = crate::common::db_now();
    for i in 0..25 {
        // Paced, every run in scope on its own.
        run(
            &pool,
            &format!("s7-{i}"),
            Some("S7"),
            &format!("C{i:04}"),
            "in_scope",
            now - Duration::minutes(20 * i),
        )
        .await;
    }
    for i in 0..5 {
        run(
            &pool,
            &format!("s8-{i}"),
            Some("S8"),
            &format!("C{i:04}"),
            "in_scope",
            now - Duration::minutes(i),
        )
        .await;
    }

    let (rules, found) = record_reach::sweep(&pool, now).await;
    assert_eq!((rules, found), (1, 1));
    let events = reach_events(&pool).await;
    assert_eq!(events.len(), 1, "the second requester is under the limit");
    let (key, observed, evidence) = &events[0];
    assert!(key.starts_with("reach:rrl-1:"), "{key}");
    assert_eq!(*observed, 25.0);
    assert_eq!(evidence["group"], "S7");
    assert_eq!(evidence["distinct_records"], 25);
    assert_eq!(evidence["root_task_ids"].as_array().unwrap().len(), 5);

    // Still open: the next tick is the same fact.
    record_reach::sweep(&pool, now).await;
    assert_eq!(reach_events(&pool).await.len(), 1);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn refused_attempts_count_and_leaving_out_the_requester_does_not_escape(pool: PgPool) {
    system(&pool).await;
    rule(&pool, "rrl-2", "scope:requester", 3).await;
    let now = crate::common::db_now();
    for i in 0..4 {
        run(
            &pool,
            &format!("u-{i}"),
            None,
            &format!("C{i:04}"),
            "out_of_scope",
            now - Duration::minutes(i),
        )
        .await;
    }
    record_reach::sweep(&pool, now).await;
    let events = reach_events(&pool).await;
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].2["group"], record_reach::UNDECLARED);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn accesses_outside_the_window_do_not_count_and_old_ones_are_pruned(pool: PgPool) {
    system(&pool).await;
    rule(&pool, "rrl-3", "system", 2).await;
    let now = crate::common::db_now();
    for i in 0..3 {
        run(
            &pool,
            &format!("old-{i}"),
            None,
            &format!("C{i:04}"),
            "in_scope",
            now - Duration::days(40),
        )
        .await;
    }
    run(
        &pool,
        "new-0",
        None,
        "C0100",
        "in_scope",
        now - Duration::hours(1),
    )
    .await;
    let (_, found) = record_reach::sweep(&pool, now).await;
    assert_eq!(found, 0);
    let (left,): (i64,) =
        sqlx::query_as("SELECT COUNT(*) FROM ai_system_record_access WHERE tenant_id = $1")
            .bind(T)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(left, 1, "only rows past retention are pruned");
}
