// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Liveness against a real Postgres.
//!
//! The decision logic is pure and covered by unit tests. What can only be
//! proven here is the part the whole feature rests on: the candidate query
//! must return a row for a system that produced **nothing**.
//!
//! That sounds trivial and is the single most likely way to ship this
//! feature broken. An INNER JOIN to `ai_system_runs` — the obvious way to
//! write it — makes every dead system disappear from the result set, and
//! the detector then reports perfect health for exactly the systems it
//! exists to catch. Nothing would error. Nothing would look wrong.

use chrono::{Duration, Utc};
use sqlx::PgPool;

use brutor_gateway_core::services::liveness::{detector, evaluate, load_candidates, mode, sweep};

use super::common;

async fn seed_system(pool: &PgPool, tenant: &str, id: &str, kind: &str) {
    sqlx::query(
        "INSERT INTO resource_groups (id, tenant_id, hierarchy_level, inherit_from_parent, \
         name, is_active, is_system, group_type, system_kind) \
         VALUES ($1, $2, 0, true, $1, true, false, 'AI_SYSTEM', $3) \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant)
    .bind(kind)
    .execute(pool)
    .await
    .expect("seed_system");
}

#[allow(clippy::too_many_arguments)]
async fn seed_expectation(
    pool: &PgPool,
    tenant: &str,
    id: &str,
    system_id: &str,
    mode_str: &str,
    window_seconds: i32,
    grace_seconds: i32,
    expected_min_runs: i32,
) {
    sqlx::query(
        "INSERT INTO ai_system_liveness_expectations \
         (id, tenant_id, ai_system_id, mode, expected_min_runs, window_seconds, \
          grace_seconds, enabled) \
         VALUES ($1, $2, $3, $4, $5, $6, $7, true) \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant)
    .bind(system_id)
    .bind(mode_str)
    .bind(expected_min_runs)
    .bind(window_seconds)
    .bind(grace_seconds)
    .execute(pool)
    .await
    .expect("seed_expectation");
}

async fn seed_run(
    pool: &PgPool,
    tenant: &str,
    id: &str,
    system_id: &str,
    seconds_ago: i64,
    terminal_state: &str,
) {
    sqlx::query(
        "INSERT INTO ai_system_runs \
         (id, tenant_id, root_task_id, ai_system_id, started_at, terminal_state, finalized_by) \
         VALUES ($1, $2, $1, $3, $4, $5, 'idle')",
    )
    .bind(id)
    .bind(tenant)
    .bind(system_id)
    .bind(crate::common::db_now() - Duration::seconds(seconds_ago))
    .bind(terminal_state)
    .execute(pool)
    .await
    .expect("seed_run");
}

// ---------------------------------------------------------------------------
// Candidate loading — the part that must not use an INNER JOIN
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_system_with_no_runs_still_appears_as_a_candidate(pool: PgPool) {
    let t = "t-lv-norun";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;
    seed_expectation(&pool, t, "live-1", "sys", mode::CONTINUOUS, 3600, 300, 1).await;
    // Deliberately no runs at all.

    let candidates = load_candidates(&pool).await;
    let mine: Vec<_> = candidates.iter().filter(|c| c.tenant_id == t).collect();

    assert_eq!(
        mine.len(),
        1,
        "a system that produced nothing must still be evaluated — an INNER \
         JOIN here would hide exactly the systems this feature exists to catch"
    );
    assert!(!mine[0].has_ever_run);
    assert_eq!(mine[0].runs_in_window, 0);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn window_facts_are_scoped_to_the_configured_window(pool: PgPool) {
    let t = "t-lv-window";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;
    seed_expectation(&pool, t, "live-1", "sys", mode::CONTINUOUS, 3600, 300, 1).await;

    seed_run(&pool, t, "r-in-1", "sys", 60, "completed").await;
    seed_run(&pool, t, "r-in-2", "sys", 120, "errored").await;
    // Outside the 1h window — counted for `has_ever_run` but not the window.
    seed_run(&pool, t, "r-old", "sys", 99_999, "completed").await;

    let c = load_candidates(&pool)
        .await
        .into_iter()
        .find(|c| c.tenant_id == t)
        .expect("candidate");

    assert_eq!(c.runs_in_window, 2);
    assert_eq!(c.errored_in_window, 1);
    assert!(c.has_ever_run);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn on_demand_and_disabled_systems_are_not_loaded(pool: PgPool) {
    let t = "t-lv-skip";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys-od", "assistant").await;
    seed_system(&pool, t, "sys-off", "agent").await;
    seed_expectation(&pool, t, "live-od", "sys-od", mode::ON_DEMAND, 3600, 300, 1).await;
    seed_expectation(
        &pool,
        t,
        "live-off",
        "sys-off",
        mode::CONTINUOUS,
        3600,
        300,
        1,
    )
    .await;
    sqlx::query("UPDATE ai_system_liveness_expectations SET enabled = false WHERE id = 'live-off'")
        .execute(&pool)
        .await
        .unwrap();

    let mine: Vec<_> = load_candidates(&pool)
        .await
        .into_iter()
        .filter(|c| c.tenant_id == t)
        .collect();
    assert!(
        mine.is_empty(),
        "the query must not waste work on unwatched systems"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn replays_do_not_count_as_liveness(pool: PgPool) {
    let t = "t-lv-replay";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;
    seed_expectation(&pool, t, "live-1", "sys", mode::CONTINUOUS, 3600, 300, 1).await;

    sqlx::query(
        "INSERT INTO ai_system_runs \
         (id, tenant_id, root_task_id, ai_system_id, started_at, terminal_state, \
          finalized_by, is_replay) \
         VALUES ('rep-1', $1, 'rep-1', 'sys', NOW(), 'completed', 'idle', true)",
    )
    .bind(t)
    .execute(&pool)
    .await
    .unwrap();

    let c = load_candidates(&pool)
        .await
        .into_iter()
        .find(|c| c.tenant_id == t)
        .expect("candidate");

    assert!(
        !c.has_ever_run,
        "a replay is a simulation against recorded traffic — it is not the \
         system doing anything, so it must not mask a dead system"
    );
    assert_eq!(c.runs_in_window, 0);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn inactive_systems_are_not_loaded(pool: PgPool) {
    let t = "t-lv-inactive";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;
    seed_expectation(&pool, t, "live-1", "sys", mode::CONTINUOUS, 3600, 300, 1).await;
    sqlx::query("UPDATE resource_groups SET is_active = false WHERE id = 'sys'")
        .execute(&pool)
        .await
        .unwrap();

    let mine: Vec<_> = load_candidates(&pool)
        .await
        .into_iter()
        .filter(|c| c.tenant_id == t)
        .collect();
    assert!(
        mine.is_empty(),
        "a deactivated system is not expected to run"
    );
}

// ---------------------------------------------------------------------------
// The sweep, end to end
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn silent_system_raises_an_alert(pool: PgPool) {
    let t = "t-lv-alert";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;
    seed_expectation(&pool, t, "live-1", "sys", mode::CONTINUOUS, 3600, 300, 1).await;
    // Ran once, hours ago, then stopped.
    seed_run(&pool, t, "r-1", "sys", 20_000, "completed").await;

    let outcome = sweep(&pool).await;
    assert!(outcome.findings >= 1);

    let alert: (String, String, String) = sqlx::query_as(
        "SELECT category, error_code, resource_id FROM system_alerts \
         WHERE tenant_id = $1 ORDER BY first_seen_at DESC LIMIT 1",
    )
    .bind(t)
    .fetch_one(&pool)
    .await
    .expect("alert row");

    assert_eq!(alert.0, "ai_system_liveness");
    assert_eq!(alert.1, detector::SILENCE);
    assert_eq!(alert.2, "sys");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_healthy_system_raises_nothing_but_is_still_stamped(pool: PgPool) {
    let t = "t-lv-healthy";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;
    seed_expectation(&pool, t, "live-1", "sys", mode::CONTINUOUS, 3600, 300, 1).await;
    seed_run(&pool, t, "r-1", "sys", 30, "completed").await;

    let outcome = sweep(&pool).await;
    assert_eq!(outcome.findings, 0);

    let (alerts,): (i64,) =
        sqlx::query_as("SELECT COUNT(*) FROM system_alerts WHERE tenant_id = $1")
            .bind(t)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(alerts, 0);

    // Healthy and never-checked must be distinguishable, or an operator
    // cannot tell a working detector from a stopped one.
    let (checked,): (Option<chrono::DateTime<Utc>>,) = sqlx::query_as(
        "SELECT last_checked_at FROM ai_system_liveness_expectations WHERE id = 'live-1'",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert!(checked.is_some(), "every evaluated system must be stamped");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_never_run_system_is_not_alerted(pool: PgPool) {
    let t = "t-lv-unstarted";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;
    seed_expectation(&pool, t, "live-1", "sys", mode::CONTINUOUS, 3600, 300, 1).await;

    let outcome = sweep(&pool).await;
    assert_eq!(
        outcome.findings, 0,
        "registering a system before wiring it up must not page anyone"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn cooldown_prevents_re_alerting_on_the_next_tick(pool: PgPool) {
    let t = "t-lv-cooldown";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;
    seed_expectation(&pool, t, "live-1", "sys", mode::CONTINUOUS, 3600, 300, 1).await;
    seed_run(&pool, t, "r-1", "sys", 20_000, "completed").await;

    let first = sweep(&pool).await;
    assert_eq!(first.alerted, 1);

    let second = sweep(&pool).await;
    assert_eq!(
        second.alerted, 0,
        "a system silent for a week must not produce an alert every tick"
    );
    assert_eq!(second.suppressed, 1, "but the finding is still counted");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn hard_fail_cliff_is_detected_end_to_end(pool: PgPool) {
    let t = "t-lv-cliff";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;
    seed_expectation(&pool, t, "live-1", "sys", mode::CONTINUOUS, 3600, 300, 1).await;
    for i in 0..9 {
        seed_run(&pool, t, &format!("e{i}"), "sys", 60 + i, "errored").await;
    }
    seed_run(&pool, t, "ok", "sys", 70, "completed").await;

    let outcome = sweep(&pool).await;
    assert_eq!(outcome.alerted, 1);

    let (code,): (String,) =
        sqlx::query_as("SELECT error_code FROM system_alerts WHERE tenant_id = $1 LIMIT 1")
            .bind(t)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(code, detector::HARD_FAIL_CLIFF);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn stall_is_detected_end_to_end(pool: PgPool) {
    let t = "t-lv-stall";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;
    seed_expectation(&pool, t, "live-1", "sys", mode::CONTINUOUS, 3600, 300, 1).await;
    for i in 0..6 {
        seed_run(&pool, t, &format!("a{i}"), "sys", 60 + i, "abandoned").await;
    }
    for i in 0..4 {
        seed_run(&pool, t, &format!("c{i}"), "sys", 80 + i, "completed").await;
    }

    let outcome = sweep(&pool).await;
    assert_eq!(outcome.alerted, 1);
    let (code,): (String,) =
        sqlx::query_as("SELECT error_code FROM system_alerts WHERE tenant_id = $1 LIMIT 1")
            .bind(t)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(code, detector::STALL);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_sweep_is_tenant_scoped(pool: PgPool) {
    let t1 = "t-lv-iso1";
    let t2 = "t-lv-iso2";
    common::seed_tenant(&pool, t1).await;
    common::seed_tenant(&pool, t2).await;
    seed_system(&pool, t1, "sys-a", "agent").await;
    seed_system(&pool, t2, "sys-b", "agent").await;
    seed_expectation(&pool, t1, "live-a", "sys-a", mode::CONTINUOUS, 3600, 300, 1).await;
    seed_expectation(&pool, t2, "live-b", "sys-b", mode::CONTINUOUS, 3600, 300, 1).await;
    seed_run(&pool, t1, "r-a", "sys-a", 20_000, "completed").await;
    seed_run(&pool, t2, "r-b", "sys-b", 30, "completed").await;

    sweep(&pool).await;

    let (a,): (i64,) = sqlx::query_as(
        "SELECT COUNT(*) FROM system_alerts WHERE tenant_id = $1 AND category = 'ai_system_liveness'",
    )
    .bind(t1)
    .fetch_one(&pool)
    .await
    .unwrap();
    let (b,): (i64,) = sqlx::query_as(
        "SELECT COUNT(*) FROM system_alerts WHERE tenant_id = $1 AND category = 'ai_system_liveness'",
    )
    .bind(t2)
    .fetch_one(&pool)
    .await
    .unwrap();

    assert_eq!((a, b), (1, 0));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_new_alert_category_is_accepted_by_the_database(pool: PgPool) {
    // `system_alerts.category` carries a named CHECK. If the
    // constraint did not know the category, every liveness alert would fail on
    // insert — in a background task, where nobody is watching.
    let t = "t-lv-check";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;
    seed_expectation(&pool, t, "live-1", "sys", mode::SCHEDULED, 3600, 300, 1).await;
    seed_run(&pool, t, "r-1", "sys", 50_000, "completed").await;

    let outcome = sweep(&pool).await;
    assert_eq!(
        outcome.alerted, 1,
        "the ai_system_liveness category must be permitted by the CHECK"
    );
}

// ---------------------------------------------------------------------------
// Decision consistency between the loader and the pure evaluator
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn loaded_candidate_evaluates_the_same_way_as_the_unit_tests(pool: PgPool) {
    let t = "t-lv-consistency";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "integration").await;
    seed_expectation(&pool, t, "live-1", "sys", mode::SCHEDULED, 3600, 300, 1).await;
    seed_run(&pool, t, "r-1", "sys", 50_000, "completed").await;

    let c = load_candidates(&pool)
        .await
        .into_iter()
        .find(|c| c.tenant_id == t)
        .expect("candidate");

    let finding = evaluate(&c, crate::common::db_now()).expect("should fire");
    assert_eq!(finding.detector, detector::MISSED_SCHEDULE);
}
