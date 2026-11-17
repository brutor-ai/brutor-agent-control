// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Running a suite.
//!
//! Orchestration around [`super::executor`]: load the suite's cases,
//! re-decide each one, write the run. Kept separate from the executor so
//! the per-case judgement stays testable without a suite around it.
//!
//! ## A replay run is a claim, so it is written honestly
//!
//! The row is created **before** the cases are evaluated, in `running`
//! state. A process that dies halfway leaves a visible `running` row rather
//! than nothing at all — and the promotion gate treats anything that is not
//! `completed` as a non-pass, so a crashed replay can never be mistaken for
//! a clean one.
//!
//! A suite marked `replayable = false`, or one with no enabled cases, is
//! **failed with a reason** rather than completed at 100%. An empty suite
//! that reports a perfect score is the single most dangerous thing this
//! module could produce: it would satisfy the gate while demonstrating
//! nothing.

use chrono::Utc;
use sqlx::{FromRow, PgPool};

use super::{executor, status, trigger};
use crate::services::db_errors::DbResultExt;

/// A suite as the runner needs it.
#[derive(Debug, Clone, FromRow)]
pub struct SuiteRow {
    pub id: String,
    pub tenant_id: String,
    pub ai_system_id: String,
    pub name: String,
    pub pass_threshold: f64,
    pub replayable: bool,
    pub enabled: bool,
}

#[derive(Debug, Clone, FromRow)]
pub struct CaseRow {
    pub id: String,
    pub source_root_task_id: Option<String>,
    pub recorded_trajectory: serde_json::Value,
    pub criticality: String,
    pub side_effect_policy: String,
}

/// A database error is returned, not read as "the suite is gone".
pub async fn load_suite(
    db: &PgPool,
    tenant_id: &str,
    suite_id: &str,
) -> Result<Option<SuiteRow>, sqlx::Error> {
    sqlx::query_as::<_, SuiteRow>(
        "SELECT id, tenant_id, ai_system_id, name, pass_threshold, replayable, enabled \
         FROM ai_system_replay_suites WHERE id = $1 AND tenant_id = $2",
    )
    .bind(suite_id)
    .bind(tenant_id)
    .fetch_optional(db)
    .await
}

/// A database error is returned: an empty list would fail the run as
/// "suite has no enabled cases", a reason that is not true.
pub async fn load_cases(
    db: &PgPool,
    tenant_id: &str,
    suite_id: &str,
) -> Result<Vec<CaseRow>, sqlx::Error> {
    sqlx::query_as::<_, CaseRow>(
        "SELECT id, source_root_task_id, recorded_trajectory, criticality, \
                side_effect_policy \
           FROM ai_system_replay_cases \
          WHERE tenant_id = $1 AND suite_id = $2 AND enabled = true \
          ORDER BY id",
    )
    .bind(tenant_id)
    .bind(suite_id)
    .fetch_all(db)
    .await
}

/// Execute an already-requested run.
///
/// Split from [`run_suite`] so the **control plane can request a run** — the
/// admin UI talks to one backend — and this side executes it. The two are
/// joined by the row rather than by a call, which is the same seam the
/// closed loop uses and for the same reason: neither side goes down with
/// the other, and a request written while this process was restarting is
/// picked up on the next sweep instead of being lost.
pub async fn execute_run(db: &PgPool, run_id: &str) -> Result<(), sqlx::Error> {
    let row: Option<(String, String, String, Option<String>)> = sqlx::query_as(
        "SELECT tenant_id, suite_id, ai_system_id, contract_id \
           FROM ai_system_replay_runs WHERE id = $1 AND status = $2",
    )
    .bind(run_id)
    .bind(status::RUNNING)
    .fetch_optional(db)
    .await?;

    let Some((tenant_id, suite_id, _system, _contract)) = row else {
        // Already finished, or gone. Not an error: two sweepers racing must
        // not turn a completed run into a failed one.
        return Ok(());
    };

    let Some(suite) = load_suite(db, &tenant_id, &suite_id).await? else {
        return finish_failed(db, run_id, "suite no longer exists").await;
    };

    execute_into(db, &tenant_id, &suite, run_id).await
}

/// Advisory-lock key for one replay run.
///
/// Replay runs were claimed by nobody: `run_suite` inserted
/// the row as `running` and executed it inline for minutes, while
/// `sweep_pending` selected every `running` row every 15 seconds with no claim
/// and no age filter and executed it again — in the same process, and once
/// more on every replica. The suite's cases dispatch judge and LLM work, so a
/// duplicate run costs real money and races two writers onto the same result
/// row.
// `pub` so the integration tests can take the same lock a rival executor
// would, which is the only way to prove the skip from outside the module.
pub fn run_lock_key(run_id: &str) -> i64 {
    use std::hash::{Hash, Hasher};
    let mut h = std::collections::hash_map::DefaultHasher::new();
    run_id.hash(&mut h);
    h.finish() as i64
}

/// Run `f` while holding the advisory lock for `run_id`; skip if another
/// executor holds it.
///
/// The lock is session-scoped, so it is taken and released on ONE pinned
/// connection — a pooled `execute` could unlock on a different session and
/// leave the lock held for the life of the pool.
async fn with_run_claim<F, Fut>(db: &PgPool, run_id: &str, f: F) -> Result<bool, sqlx::Error>
where
    F: FnOnce() -> Fut,
    Fut: std::future::Future<Output = Result<(), sqlx::Error>>,
{
    use sqlx::Acquire;
    let mut conn = db.acquire().await?;
    let key = run_lock_key(run_id);
    let (claimed,): (bool,) = sqlx::query_as("SELECT pg_try_advisory_lock($1)")
        .bind(key)
        .fetch_one(conn.acquire().await?)
        .await?;
    if !claimed {
        tracing::debug!(
            run_id,
            "replay run already claimed by another executor; skipping"
        );
        return Ok(false);
    }
    let result = f().await;
    // Released even on error: a failed run must not wedge the id forever.
    let _ = sqlx::query("SELECT pg_advisory_unlock($1)")
        .bind(key)
        .execute(conn.acquire().await?)
        .await;
    result.map(|()| true)
}

/// Execute every unclaimed `running` row, oldest first. Returns how many ran.
///
/// A `running` row with no executor holding its claim is work nobody is
/// doing: the control plane's `request_replay_run` writes exactly such a row
/// and relies on this sweep to run it (the two sides are joined by the row,
/// not a call). There is deliberately **no age filter** any more. The old
/// ten-minute "staleness" window kept the sweep off a row `run_suite`
/// had inserted but not yet claimed — and made every operator-requested
/// replay sit untouched for ten minutes while the console said "running".
/// `run_suite` now claims before it inserts, so the advisory lock alone
/// separates "being executed" from "abandoned".
pub async fn sweep_pending(db: &PgPool, limit: i64) -> usize {
    let ids: Vec<(String,)> = sqlx::query_as(
        "SELECT id FROM ai_system_replay_runs \
          WHERE status = $1 AND finished_at IS NULL \
          ORDER BY started_at ASC LIMIT $2",
    )
    .bind(status::RUNNING)
    .bind(limit)
    .fetch_all(db)
    .await
    // Logged and counted; the runs stay `running` for the next sweep.
    .or_logged_default();

    let mut ran = 0;
    for (id,) in ids {
        // One failure must not stop the batch: a single malformed suite
        // should not wedge replay for the whole fleet.
        match with_run_claim(db, &id, || execute_run(db, &id)).await {
            Ok(true) => ran += 1,
            Ok(false) => {} // another executor has it
            Err(e) => tracing::warn!(error = %e, run_id = %id, "replay run failed"),
        }
    }
    ran
}

/// Execute a suite and persist the result. Returns the replay-run id.
pub async fn run_suite(
    db: &PgPool,
    tenant_id: &str,
    suite_id: &str,
    contract_id: Option<&str>,
    triggered_by: &str,
    actor: Option<&str>,
) -> Result<String, sqlx::Error> {
    let Some(suite) = load_suite(db, tenant_id, suite_id).await? else {
        return Err(sqlx::Error::RowNotFound);
    };

    let run_id = format!("rpr-{}", ulid::Ulid::new());

    // Hold the same claim the sweeper honours, so an inline
    // run and a sweep can never execute one suite twice. The claim is taken
    // BEFORE the row exists: the sweeper picks up every unclaimed `running`
    // row on its next tick (a row the control plane wrote is exactly that),
    // so an inline run that inserted first and claimed second would race
    // its own sweep in the gap between the two.
    let claimed = with_run_claim(db, &run_id, || async {
        sqlx::query(
            "INSERT INTO ai_system_replay_runs \
             (id, tenant_id, suite_id, ai_system_id, contract_id, status, triggered_by, \
              triggered_by_actor) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
        )
        .bind(&run_id)
        .bind(tenant_id)
        .bind(&suite.id)
        .bind(&suite.ai_system_id)
        .bind(contract_id)
        .bind(status::RUNNING)
        .bind(triggered_by)
        .bind(actor)
        .execute(db)
        .await?;
        execute_into(db, tenant_id, &suite, &run_id).await
    })
    .await?;
    debug_assert!(
        claimed,
        "a freshly generated run id cannot already be claimed"
    );
    Ok(run_id)
}

/// The shared body: evaluate a suite's cases into an existing run row.
async fn execute_into(
    db: &PgPool,
    tenant_id: &str,
    suite: &SuiteRow,
    run_id: &str,
) -> Result<(), sqlx::Error> {
    // Refusals that must never look like a pass.
    let refusal = if !suite.replayable {
        Some("suite is marked not replayable".to_string())
    } else if !suite.enabled {
        Some("suite is disabled".to_string())
    } else {
        None
    };
    if let Some(reason) = refusal {
        return finish_failed(db, run_id, &reason).await;
    }

    let cases = load_cases(db, tenant_id, &suite.id).await?;
    if cases.is_empty() {
        // An empty suite reporting 100% would satisfy the gate while
        // demonstrating nothing at all.
        return finish_failed(db, run_id, "suite has no enabled cases").await;
    }

    let mut outcomes = Vec::with_capacity(cases.len());
    for case in &cases {
        let recorded = executor::trajectory_from_json(&case.recorded_trajectory);
        outcomes.push(
            executor::replay_case(
                db,
                tenant_id,
                &suite.ai_system_id,
                &case.id,
                case.source_root_task_id.as_deref(),
                &recorded,
                case.criticality == "critical",
                &case.side_effect_policy,
            )
            .await?,
        );
    }

    let summary = executor::summarize_outcomes(&outcomes);
    let results = executor::case_results_json(&outcomes);

    sqlx::query(
        "UPDATE ai_system_replay_runs SET \
            status = $2, finished_at = $3, total_cases = $4, matched = $5, \
            shape_changed = $6, policy_changed = $7, outcome_changed = $8, \
            failed = $9, skipped = $10, pass_rate = $11, \
            critical_outcome_changed = $12, case_results = $13 \
          WHERE id = $1",
    )
    .bind(run_id)
    .bind(status::COMPLETED)
    .bind(Utc::now())
    .bind(summary.total_cases as i32)
    .bind(summary.matched as i32)
    .bind(summary.shape_changed as i32)
    .bind(summary.policy_changed as i32)
    .bind(summary.outcome_changed as i32)
    .bind(summary.failed as i32)
    .bind(summary.skipped as i32)
    .bind(summary.pass_rate)
    .bind(summary.critical_outcome_changed as i32)
    .bind(results)
    .execute(db)
    .await?;

    tracing::info!(
        suite = %suite.name,
        run_id = %run_id,
        pass_rate = ?summary.pass_rate,
        critical_outcome_changed = summary.critical_outcome_changed,
        "replay run completed"
    );

    Ok(())
}

async fn finish_failed(db: &PgPool, run_id: &str, reason: &str) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE ai_system_replay_runs SET status = $2, finished_at = $3, \
                error_message = $4, pass_rate = NULL WHERE id = $1",
    )
    .bind(run_id)
    .bind(status::FAILED)
    .bind(Utc::now())
    .bind(reason)
    .execute(db)
    .await?;
    Ok(())
}

/// Convenience for the promotion gate: run the suite bound to a system.
pub async fn run_for_system(
    db: &PgPool,
    tenant_id: &str,
    ai_system_id: &str,
    contract_id: Option<&str>,
) -> Result<Option<String>, sqlx::Error> {
    let suite_id: Option<String> = sqlx::query_scalar(
        "SELECT replay_suite_id FROM resource_groups WHERE id = $1 AND tenant_id = $2",
    )
    .bind(ai_system_id)
    .bind(tenant_id)
    .fetch_optional(db)
    .await?
    .flatten();

    let Some(suite_id) = suite_id else {
        return Ok(None);
    };
    run_suite(
        db,
        tenant_id,
        &suite_id,
        contract_id,
        trigger::PROMOTION_GATE,
        None,
    )
    .await
    .map(Some)
}

/// How often to pick up requested runs. Replay is operator-initiated and
/// gate-initiated, so a request is worth acting on quickly — but it is never
/// so urgent that a tighter poll earns anything.
pub const SWEEP_INTERVAL_SECONDS: u64 = 15;

/// Bounded per tick so a backlog drains steadily rather than executing four
/// hundred suites in one pass.
pub const MAX_PER_SWEEP: i64 = 5;

/// The loop. Beacons before doing work, so a tick that finds nothing still
/// reports healthy rather than looking like a dead scheduler.
pub async fn run_replay_loop(
    db: PgPool,
    interval_secs: u64,
    shutdown: tokio_util::sync::CancellationToken,
) {
    let mut ticker = tokio::time::interval(std::time::Duration::from_secs(interval_secs));
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

    loop {
        tokio::select! {
            _ = shutdown.cancelled() => {
                tracing::info!("replay loop shutting down");
                return;
            }
            _ = ticker.tick() => {
                if let Err(e) = crate::services::system_ops_beacon::write_beacon(
                    &db,
                    crate::services::system_ops_beacon::schedulers::AI_SYSTEM_REPLAY,
                ).await {
                    tracing::warn!(error = %e, "replay beacon write failed");
                }
                let ran = sweep_pending(&db, MAX_PER_SWEEP).await;
                if ran > 0 {
                    tracing::info!(count = ran, "replay runs executed");
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_run_id_is_prefixed_so_it_is_greppable() {
        let id = format!("rpr-{}", ulid::Ulid::new());
        assert!(id.starts_with("rpr-"));
    }

    #[test]
    fn the_sweep_is_bounded_so_a_backlog_drains_rather_than_stampedes() {
        // Compile-time: `MAX_PER_SWEEP` is a const, so a runtime assertion
        // here could never fail at a moment anyone would notice. This way
        // the bound is enforced by the build.
        const _: () = assert!(MAX_PER_SWEEP > 0 && MAX_PER_SWEEP <= 20);
    }

    #[test]
    fn triggers_match_the_control_plane_vocabulary() {
        assert_eq!(trigger::PROMOTION_GATE, "promotion_gate");
        assert_eq!(trigger::MANUAL, "manual");
        assert_eq!(status::RUNNING, "running");
        assert_eq!(status::COMPLETED, "completed");
        assert_eq!(status::FAILED, "failed");
    }
}
