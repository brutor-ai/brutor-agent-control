// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A drift resolution the detector honours, against a real
//! Postgres.
//!
//! The failure this file exists for:
//! an operator resolved a finding and restored autonomy, and
//! 40 minutes later the sweeper fired the same finding from the same
//! samples. Pinned here, as classes, each through the real sweeper:
//!
//! * `accepted_change` — the same shift does not re-fire; the metric's
//!   baseline re-learns (`learning` until the minimum is met AFTER the
//!   epoch, then `ready` on post-epoch data only); new evidence after it
//!   still fires.
//! * `cause_fixed` — samples inside the incident window are in neither the
//!   baseline nor the observation window; new bad behaviour after the
//!   window fires.
//! * `false_positive` — its epoch (at detection time) floors the metric.
//! * the sweeper's second line — a closed key is not re-opened by evidence
//!   the closure settled, even with the epoch row missing.
//! * a tenant-wide window applies to every system; a system's window only
//!   to it; a window on a run's root covers the calls it made.
//! * per-caller segment baselines honour their own and the `'all'` epoch.
//! * liveness share detectors ignore runs inside a window.
//! * the cause ladder names a window a change point sits at the edge of.

use chrono::{DateTime, Duration, Utc};
use serde_json::json;
use sqlx::PgPool;

use brutor_gateway_core::constants::drift_resolution::{cause_category, kind};
use brutor_gateway_core::services::drift::{
    baseline::{self, SampleSource},
    basis, cause, metric,
    sweeper::{self, DriftCandidate, Finding, SweepOutcome},
};
use brutor_gateway_core::services::liveness;

use super::common;

// ---------------------------------------------------------------------------
// Seeding
// ---------------------------------------------------------------------------

async fn seed_system(pool: &PgPool, tenant: &str, id: &str) {
    sqlx::query(
        "INSERT INTO resource_groups (id, tenant_id, hierarchy_level, inherit_from_parent, \
         name, is_active, is_system, group_type, system_kind) \
         VALUES ($1, $2, 0, true, $1, true, false, 'AI_SYSTEM', 'agent') \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant)
    .execute(pool)
    .await
    .expect("seed_system");
}

/// One completed, intact run. Only `action_count` varies between runs, so
/// every finding in this file is about exactly one metric.
async fn seed_run(
    pool: &PgPool,
    tenant: &str,
    id: &str,
    system_id: &str,
    at: DateTime<Utc>,
    actions: i32,
) {
    sqlx::query(
        "INSERT INTO ai_system_runs \
         (id, tenant_id, root_task_id, ai_system_id, started_at, ended_at, duration_ms, \
          terminal_state, finalized_by, action_count, tool_call_count, max_delegation_depth, \
          total_cost_usd, tool_error_count, input_bytes, first_prompt_tokens, \
          tool_set_signature, client_label, chain_integrity, reopened_count, is_replay, \
          outcome_id, outcome_conformance, outcome_counts_toward_baseline) \
         VALUES ($1, $2, $1, $3, $4, $4, 1000, 'completed', 'idle', $5, 4, 1, \
                 0.004, 0, 2048, 400, 'search|fetch', 'sdk', 'intact', 0, false, \
                 'task_completed', 'declared', true)",
    )
    .bind(id)
    .bind(tenant)
    .bind(system_id)
    .bind(at)
    .bind(actions)
    .execute(pool)
    .await
    .expect("seed_run");
}

/// `n` runs at `level` (+0..2), every `every` minutes from `from`.
#[allow(clippy::too_many_arguments)]
async fn seed_series(
    pool: &PgPool,
    tenant: &str,
    system_id: &str,
    tag: &str,
    from: DateTime<Utc>,
    every_min: i64,
    n: usize,
    level: i32,
) {
    for i in 0..n {
        seed_run(
            pool,
            tenant,
            &format!("run-{system_id}-{tag}-{i}"),
            system_id,
            from + Duration::minutes(every_min * i as i64),
            level + (i % 3) as i32,
        )
        .await;
    }
}

/// A steady history (actions 5..7) from ~13 days back up to `until`.
async fn seed_steady(
    pool: &PgPool,
    tenant: &str,
    system_id: &str,
    now: DateTime<Utc>,
    until: DateTime<Utc>,
) {
    let from = now - Duration::days(13);
    let n = 240;
    let every = ((until - from).num_minutes() / n as i64).max(1);
    seed_series(pool, tenant, system_id, "steady", from, every, n, 5).await;
}

fn candidate(tenant: &str, id: &str) -> DriftCandidate {
    DriftCandidate {
        tenant_id: tenant.to_string(),
        ai_system_id: id.to_string(),
        system_name: Some(id.to_string()),
        system_kind: Some("agent".into()),
        autonomy_level: None,
        risk_tier: None,
        eligible_runs: 0,
    }
}

async fn sweep(
    pool: &PgPool,
    tenant: &str,
    sys: &str,
    now: DateTime<Utc>,
) -> (Vec<Finding>, SweepOutcome) {
    let mut outcome = SweepOutcome::default();
    let found = sweeper::sweep_system(pool, &candidate(tenant, sys), now, &mut outcome).await;
    (found, outcome)
}

fn action_count(found: &[Finding]) -> Option<&Finding> {
    found
        .iter()
        .find(|f| f.detection.metric_key == metric::ACTION_COUNT)
}

/// Sweep, record every finding, and return the action_count one with its
/// event id.
async fn detect_and_record(
    pool: &PgPool,
    tenant: &str,
    sys: &str,
    now: DateTime<Utc>,
) -> (Finding, String) {
    let (found, mut outcome) = sweep(pool, tenant, sys, now).await;
    let f = action_count(&found)
        .expect("the seeded shift in action_count must be detected before it is resolved")
        .clone();
    sweeper::record(pool, &candidate(tenant, sys), &f, now, &mut outcome).await;
    let (id,): (String,) = sqlx::query_as(
        "SELECT id FROM ai_system_drift_events WHERE tenant_id = $1 AND ai_system_id = $2 \
           AND metric_key = $3 ORDER BY detected_at DESC LIMIT 1",
    )
    .bind(tenant)
    .bind(sys)
    .bind(metric::ACTION_COUNT)
    .fetch_one(pool)
    .await
    .expect("the finding was recorded");
    (f, id)
}

/// Close an event the way the control plane does: status + kind + when.
async fn close(pool: &PgPool, event_id: &str, resolution_kind: &str, at: DateTime<Utc>) {
    let status = if resolution_kind == kind::FALSE_POSITIVE {
        "false_positive"
    } else {
        "resolved"
    };
    sqlx::query(
        "UPDATE ai_system_drift_events SET status = $2, resolution_kind = $3, \
                resolved_at = $4, resolved_by = 'operator', resolution_note = 'drift resolution test' \
          WHERE id = $1",
    )
    .bind(event_id)
    .bind(status)
    .bind(resolution_kind)
    .bind(at)
    .execute(pool)
    .await
    .expect("close");
}

#[allow(clippy::too_many_arguments)]
async fn insert_epoch(
    pool: &PgPool,
    tenant: &str,
    sys: &str,
    metric_key: &str,
    basis_: &str,
    caller: &str,
    starts_at: DateTime<Utc>,
    epoch_kind: &str,
    event_id: Option<&str>,
) {
    sqlx::query(
        "INSERT INTO ai_system_baseline_epochs \
         (id, tenant_id, ai_system_id, metric_key, basis, caller_system_id, starts_at, kind, \
          drift_event_id) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
    )
    .bind(format!("ep-{}", ulid::Ulid::new()))
    .bind(tenant)
    .bind(sys)
    .bind(metric_key)
    .bind(basis_)
    .bind(caller)
    .bind(starts_at)
    .bind(epoch_kind)
    .bind(event_id)
    .execute(pool)
    .await
    .expect("insert_epoch");
}

async fn insert_window(
    pool: &PgPool,
    tenant: &str,
    sys: Option<&str>,
    starts_at: DateTime<Utc>,
    ends_at: DateTime<Utc>,
    event_id: Option<&str>,
) {
    sqlx::query(
        "INSERT INTO ai_system_incident_windows \
         (id, tenant_id, ai_system_id, starts_at, ends_at, cause_category, reason, drift_event_id) \
         VALUES ($1, $2, $3, $4, $5, $6, 'provider credits exhausted', $7)",
    )
    .bind(format!("iw-{}", ulid::Ulid::new()))
    .bind(tenant)
    .bind(sys)
    .bind(starts_at)
    .bind(ends_at)
    .bind(cause_category::PROVIDER_OUTAGE)
    .bind(event_id)
    .execute(pool)
    .await
    .expect("insert_window");
}

/// `(state, sample_count, window_start)` of a baseline row.
async fn baseline_row(
    pool: &PgPool,
    tenant: &str,
    sys: &str,
    basis_: &str,
    caller: &str,
    metric_key: &str,
) -> (String, i32, Option<DateTime<Utc>>, Option<f64>) {
    sqlx::query_as(
        "SELECT state, sample_count, window_start, median FROM ai_system_baselines \
          WHERE tenant_id = $1 AND ai_system_id = $2 AND basis = $3 AND caller_system_id = $4 \
            AND metric_key = $5",
    )
    .bind(tenant)
    .bind(sys)
    .bind(basis_)
    .bind(caller)
    .bind(metric_key)
    .fetch_one(pool)
    .await
    .expect("baseline row")
}

async fn event_count(pool: &PgPool, tenant: &str, sys: &str) -> i64 {
    let (n,): (i64,) = sqlx::query_as(
        "SELECT COUNT(*) FROM ai_system_drift_events WHERE tenant_id = $1 AND ai_system_id = $2",
    )
    .bind(tenant)
    .bind(sys)
    .fetch_one(pool)
    .await
    .unwrap();
    n
}

/// Runs a baseline may learn from at `now`: before the observation window and,
/// when given, at or after an epoch.
async fn reference_runs(
    pool: &PgPool,
    tenant: &str,
    now: DateTime<Utc>,
    since: Option<DateTime<Utc>>,
) -> i64 {
    sqlx::query_as::<_, (i64,)>(
        "SELECT COUNT(*) FROM ai_system_runs WHERE tenant_id = $1 AND started_at < $2 \
           AND ($3::timestamptz IS NULL OR started_at >= $3)",
    )
    .bind(tenant)
    .bind(sweeper::observation_start(now))
    .bind(since)
    .fetch_one(pool)
    .await
    .unwrap()
    .0
}

/// The same for a service's calls, optionally from one caller.
async fn reference_calls(
    pool: &PgPool,
    tenant: &str,
    now: DateTime<Utc>,
    caller: Option<&str>,
    since: Option<DateTime<Utc>>,
) -> i32 {
    sqlx::query_as::<_, (i64,)>(
        "SELECT COUNT(*) FROM ai_system_run_segments WHERE tenant_id = $1 \
           AND first_action_at < $2 \
           AND ($3::text IS NULL OR caller_system_id = $3) \
           AND ($4::timestamptz IS NULL OR first_action_at >= $4)",
    )
    .bind(tenant)
    .bind(sweeper::observation_start(now))
    .bind(caller)
    .bind(since)
    .fetch_one(pool)
    .await
    .unwrap()
    .0 as i32
}

async fn samples_in_window(
    pool: &PgPool,
    source: SampleSource,
    tenant: &str,
    sys: &str,
    now: DateTime<Utc>,
) -> usize {
    let (from, to) = baseline::window(now);
    baseline::load_samples(pool, source, tenant, sys, from, to, 3)
        .await
        .len()
}

// ---------------------------------------------------------------------------
// accepted_change
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_accepted_change_does_not_refire_and_the_baseline_relearns_from_the_epoch(pool: PgPool) {
    let (t, sys) = ("t-r24-accept", "sys");
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, sys).await;
    seed_steady(&pool, t, sys, now, now - Duration::hours(21)).await;
    // The retune: 40 runs every 30 min over the last 20h at a new level.
    let change = now - Duration::hours(20);
    seed_series(&pool, t, sys, "new", change, 30, 40, 20).await;

    let (f, event) = detect_and_record(&pool, t, sys, now).await;
    let cp = f
        .change_point
        .expect("a continuous shift has a change point");
    assert!(cp >= change, "the change point is on the new behaviour");

    // The operator accepts the change: the CP writes the epoch at the change point.
    close(&pool, &event, kind::ACCEPTED_CHANGE, now).await;
    insert_epoch(
        &pool,
        t,
        sys,
        metric::ACTION_COUNT,
        basis::RUNS,
        "all",
        cp,
        kind::ACCEPTED_CHANGE,
        Some(&event),
    )
    .await;

    // The same samples, swept again: nothing, and the metric re-learns.
    let (found, _) = sweep(&pool, t, sys, now).await;
    assert!(
        action_count(&found).is_none(),
        "the accepted shift re-fired from the samples the operator accepted: {:?}",
        found
            .iter()
            .map(|f| &f.detection.metric_key)
            .collect::<Vec<_>>()
    );
    // A baseline learns only from before the observation window, and this
    // epoch is inside it: there is nothing to learn from yet.
    let post_epoch = reference_runs(&pool, t, now, Some(cp)).await;
    assert_eq!(post_epoch, 0);
    let (state, n, window_start, _) =
        baseline_row(&pool, t, sys, basis::RUNS, "all", metric::ACTION_COUNT).await;
    assert_eq!(
        state, "learning",
        "never 'ready' on data the operator accepted a change away from"
    );
    assert_eq!(
        i64::from(n),
        post_epoch,
        "only post-epoch samples are its baseline"
    );
    assert_eq!(
        window_start,
        Some(cp),
        "the row says it re-learns since the epoch"
    );
    let (cost_state, cost_n, _, _) =
        baseline_row(&pool, t, sys, basis::RUNS, "all", metric::COST_PER_RUN).await;
    assert_eq!(cost_state, "ready", "an epoch is per metric");
    assert_eq!(i64::from(cost_n), reference_runs(&pool, t, now, None).await);

    // More of the new normal: ready once the minimum is met after the epoch,
    // on post-epoch data only.
    seed_series(
        &pool,
        t,
        sys,
        "more",
        change + Duration::minutes(15),
        30,
        30,
        20,
    )
    .await;
    // A day on, those runs have aged out of the observation window.
    let (found, _) = sweep(&pool, t, sys, now + Duration::hours(24)).await;
    assert!(action_count(&found).is_none());
    let (state, n, _, median) =
        baseline_row(&pool, t, sys, basis::RUNS, "all", metric::ACTION_COUNT).await;
    assert_eq!(state, "ready");
    assert!(i64::from(n) >= 50);
    assert_eq!(median, Some(21.0), "the new normal, not the old one");

    // New evidence after the resolution still fires, and is recorded.
    let later = now + Duration::hours(20);
    seed_series(&pool, t, sys, "again", now + Duration::hours(1), 30, 38, 60).await;
    let (found, mut outcome) = sweep(&pool, t, sys, later).await;
    let f2 = action_count(&found)
        .expect("a new shift after the accepted one must fire")
        .clone();
    assert!(f2.change_point.unwrap() > cp);
    sweeper::record(&pool, &candidate(t, sys), &f2, later, &mut outcome).await;
    assert_eq!(outcome.settled, 0);
    assert_eq!(
        event_count(&pool, t, sys).await,
        2,
        "the new finding is recorded"
    );
}

// ---------------------------------------------------------------------------
// The sweeper's second line
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_closed_key_is_not_reopened_by_evidence_its_closure_settled(pool: PgPool) {
    // Every kind, with its durable fact deliberately MISSING: the guard
    // reads the event alone, so a lost epoch or window row cannot let the
    // next sweep undo an operator's restore.
    for (i, k) in kind::HUMAN.iter().enumerate() {
        let t = format!("t-r24-guard-{i}");
        let sys = &format!("sys-{i}");
        let now = crate::common::db_now();
        common::seed_tenant(&pool, &t).await;
        seed_system(&pool, &t, sys).await;
        seed_steady(&pool, &t, sys, now, now - Duration::hours(21)).await;
        seed_series(&pool, &t, sys, "new", now - Duration::hours(20), 30, 40, 20).await;

        let (f, event) = detect_and_record(&pool, &t, sys, now).await;
        close(&pool, &event, k, now).await;

        // Past the cooldown, so only the closure can stop it.
        let later = now + Duration::hours(7);
        let mut outcome = SweepOutcome::default();
        sweeper::record(&pool, &candidate(&t, sys), &f, later, &mut outcome).await;
        assert_eq!(
            outcome.settled, 1,
            "{k}: the settled finding was re-recorded"
        );
        assert_eq!(event_count(&pool, &t, sys).await, 1, "{k}");
    }
}

// ---------------------------------------------------------------------------
// cause_fixed
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_fixed_cause_excludes_its_window_and_new_bad_behaviour_after_it_fires(pool: PgPool) {
    let (t, sys) = ("t-r24-fixed", "sys");
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, sys).await;
    seed_steady(&pool, t, sys, now, now - Duration::hours(41)).await;
    // The outage: 60 bad runs every 30 min over [now-40h, now-10h) ...
    let outage = now - Duration::hours(40);
    let fixed = now - Duration::hours(10);
    seed_series(&pool, t, sys, "outage", outage, 30, 60, 20).await;
    // ... then healthy again.
    seed_series(
        &pool,
        t,
        sys,
        "healthy",
        fixed + Duration::minutes(15),
        30,
        20,
        5,
    )
    .await;
    let before = samples_in_window(&pool, SampleSource::Runs, t, sys, now).await;

    let (f, event) = detect_and_record(&pool, t, sys, now).await;

    // Fixed: the window runs from the outage's start (edited back from the
    // detector's change point) to when it was fixed.
    close(&pool, &event, kind::CAUSE_FIXED, fixed).await;
    insert_window(
        &pool,
        t,
        Some(sys),
        outage - Duration::minutes(1),
        fixed,
        Some(&event),
    )
    .await;

    assert_eq!(
        samples_in_window(&pool, SampleSource::Runs, t, sys, now).await,
        before - 60,
        "runs inside the window are not samples"
    );
    let (found, _) = sweep(&pool, t, sys, now).await;
    assert!(
        action_count(&found).is_none(),
        "the fixed outage re-fired from its own samples"
    );
    let (state, n, _, median) =
        baseline_row(&pool, t, sys, basis::RUNS, "all", metric::ACTION_COUNT).await;
    assert_eq!(state, "ready");
    assert_eq!(
        n, 240,
        "the steady history only: none of the outage, and the healthy runs \
         since the fix are still inside the observation window"
    );
    assert_eq!(median, Some(6.0));

    // The second line agrees, on the finding the outage produced.
    let mut outcome = SweepOutcome::default();
    sweeper::record(
        &pool,
        &candidate(t, sys),
        &f,
        now + Duration::hours(7),
        &mut outcome,
    )
    .await;
    assert_eq!(outcome.settled, 1);

    // New bad behaviour after the fix fires normally.
    seed_series(
        &pool,
        t,
        sys,
        "relapse",
        now - Duration::hours(5),
        10,
        30,
        20,
    )
    .await;
    let (found, mut outcome) = sweep(&pool, t, sys, now).await;
    let f2 = action_count(&found)
        .expect("bad behaviour after the window must fire")
        .clone();
    assert!(f2.change_point.unwrap() >= fixed);
    sweeper::record(
        &pool,
        &candidate(t, sys),
        &f2,
        now + Duration::hours(7),
        &mut outcome,
    )
    .await;
    assert_eq!(outcome.settled, 0);
    assert_eq!(event_count(&pool, t, sys).await, 2);
}

// ---------------------------------------------------------------------------
// false_positive
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_false_positive_floors_its_metric_at_detection_time(pool: PgPool) {
    let (t, sys) = ("t-r24-fp", "sys");
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, sys).await;
    seed_steady(&pool, t, sys, now, now - Duration::hours(21)).await;
    seed_series(&pool, t, sys, "new", now - Duration::hours(20), 30, 40, 20).await;

    let (_, event) = detect_and_record(&pool, t, sys, now).await;
    let (detected_at,): (DateTime<Utc>,) =
        sqlx::query_as("SELECT detected_at FROM ai_system_drift_events WHERE id = $1")
            .bind(&event)
            .fetch_one(&pool)
            .await
            .unwrap();
    close(&pool, &event, kind::FALSE_POSITIVE, now).await;
    insert_epoch(
        &pool,
        t,
        sys,
        metric::ACTION_COUNT,
        basis::RUNS,
        "all",
        detected_at,
        kind::FALSE_POSITIVE,
        Some(&event),
    )
    .await;

    let (found, _) = sweep(&pool, t, sys, now).await;
    assert!(
        action_count(&found).is_none(),
        "the false positive re-fired from its own evidence"
    );
    let (state, n, _, _) =
        baseline_row(&pool, t, sys, basis::RUNS, "all", metric::ACTION_COUNT).await;
    assert_eq!(state, "learning");
    assert_eq!(n, 0, "nothing after the detection yet");
}

// ---------------------------------------------------------------------------
// Scope of a window
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_tenant_wide_window_applies_to_every_system_and_a_systems_window_only_to_it(
    pool: PgPool,
) {
    let t = "t-r24-scope";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    let outage = now - Duration::hours(40);
    let fixed = now - Duration::hours(10);
    for sys in ["sys-a", "sys-b"] {
        seed_system(&pool, t, sys).await;
        seed_steady(&pool, t, sys, now, now - Duration::hours(41)).await;
        seed_series(&pool, t, sys, "outage", outage, 30, 60, 20).await;
        seed_series(
            &pool,
            t,
            sys,
            "healthy",
            fixed + Duration::minutes(15),
            30,
            20,
            5,
        )
        .await;
    }
    // Another tenant's window never applies.
    common::seed_tenant(&pool, "t-r24-other").await;
    insert_window(
        &pool,
        "t-r24-other",
        None,
        outage - Duration::minutes(1),
        fixed,
        None,
    )
    .await;
    for sys in ["sys-a", "sys-b"] {
        assert_eq!(
            samples_in_window(&pool, SampleSource::Runs, t, sys, now).await,
            320
        );
    }

    // A window on sys-a only.
    insert_window(
        &pool,
        t,
        Some("sys-a"),
        outage - Duration::minutes(1),
        fixed,
        None,
    )
    .await;
    assert_eq!(
        samples_in_window(&pool, SampleSource::Runs, t, "sys-a", now).await,
        260
    );
    assert_eq!(
        samples_in_window(&pool, SampleSource::Runs, t, "sys-b", now).await,
        320
    );
    let (found, _) = sweep(&pool, t, "sys-b", now).await;
    assert!(
        action_count(&found).is_some(),
        "sys-a's window must not hide sys-b's outage"
    );

    // A provider outage, declared once for the whole tenant.
    insert_window(&pool, t, None, outage - Duration::minutes(1), fixed, None).await;
    for sys in ["sys-a", "sys-b"] {
        assert_eq!(
            samples_in_window(&pool, SampleSource::Runs, t, sys, now).await,
            260,
            "{sys}"
        );
        let (found, _) = sweep(&pool, t, sys, now).await;
        assert!(
            action_count(&found).is_none(),
            "{sys}: the tenant-wide window was not honoured"
        );
    }
}

// ---------------------------------------------------------------------------
// Segments: per-caller epochs, and a window on the run's root
// ---------------------------------------------------------------------------

/// A call `caller` made to `service` (the caller is also the run's root).
async fn seed_call(
    pool: &PgPool,
    tenant: &str,
    id: &str,
    service: &str,
    caller: &str,
    at: DateTime<Utc>,
    actions: i32,
) {
    seed_run(pool, tenant, id, caller, at, actions + 2).await;
    sqlx::query(
        "INSERT INTO ai_system_run_segments \
         (id, run_id, tenant_id, root_task_id, root_system_id, segment_system_id, min_depth, \
          first_action_at, last_action_at, active_ms, action_count, llm_call_count, \
          tool_call_count, error_count, tool_error_count, total_tokens, cost_usd, models_used, \
          tool_set_signature, outcome, caller_system_id) \
         VALUES ($1, $2, $3, $2, $4, $5, 1, $6, $6, 700, $7, 2, 1, 0, 0, 800, 0.002, $8, \
                 'score', 'answered', $4)",
    )
    .bind(format!("seg-{id}"))
    .bind(id)
    .bind(tenant)
    .bind(caller)
    .bind(service)
    .bind(at)
    .bind(actions)
    .bind(json!({"gpt-x": 2}))
    .execute(pool)
    .await
    .expect("seed_call");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn per_caller_segment_baselines_honour_their_own_and_the_all_callers_epoch(pool: PgPool) {
    let (t, svc) = ("t-r24-seg", "svc");
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    for s in [svc, "caller-a", "caller-b"] {
        seed_system(&pool, t, s).await;
    }
    // 150 calls from A and 100 from B, hourly-ish over 13 days, oldest first.
    let from = now - Duration::days(13);
    for i in 0..250usize {
        let caller = if i % 5 < 3 { "caller-a" } else { "caller-b" };
        let at = from + Duration::minutes(74 * i as i64);
        seed_call(
            &pool,
            t,
            &format!("c-{i}"),
            svc,
            caller,
            at,
            3 + (i % 3) as i32,
        )
        .await;
    }
    let row = |caller: &'static str| {
        baseline_row(&pool, t, svc, basis::SEGMENTS, caller, metric::ACTION_COUNT)
    };

    sweep(&pool, t, svc, now).await;
    assert_eq!(row("all").await.0, "ready");
    assert_eq!(row("caller-a").await.0, "ready");
    assert_eq!(row("caller-b").await.0, "ready");

    // An epoch on caller A only: A re-learns, B and the aggregate do not.
    let epoch_a = from + Duration::minutes(74 * 200);
    insert_epoch(
        &pool,
        t,
        svc,
        metric::ACTION_COUNT,
        basis::SEGMENTS,
        "caller-a",
        epoch_a,
        kind::ACCEPTED_CHANGE,
        None,
    )
    .await;
    sweep(&pool, t, svc, now).await;
    let a = row("caller-a").await;
    assert_eq!(a.0, "learning", "caller A's own epoch");
    let a_since_own_epoch = reference_calls(&pool, t, now, Some("caller-a"), Some(epoch_a)).await;
    assert_eq!(a.1, a_since_own_epoch, "A's calls at or after the epoch");
    assert_eq!(a.2, Some(epoch_a));
    assert_eq!(
        row("caller-b").await.1,
        reference_calls(&pool, t, now, Some("caller-b"), None).await,
        "a caller's epoch is that caller's only"
    );
    assert_eq!(
        row("all").await.1,
        reference_calls(&pool, t, now, None, None).await
    );
    let (cost_state, cost_n, _, _) = baseline_row(
        &pool,
        t,
        svc,
        basis::SEGMENTS,
        "caller-a",
        metric::COST_PER_RUN,
    )
    .await;
    assert_eq!(
        (cost_state.as_str(), cost_n),
        (
            "ready",
            reference_calls(&pool, t, now, Some("caller-a"), None).await
        ),
        "per metric"
    );

    // An 'all' epoch covers the aggregate AND every caller.
    let epoch_all = from + Duration::minutes(74 * 150);
    insert_epoch(
        &pool,
        t,
        svc,
        metric::ACTION_COUNT,
        basis::SEGMENTS,
        "all",
        epoch_all,
        kind::ACCEPTED_CHANGE,
        None,
    )
    .await;
    sweep(&pool, t, svc, now).await;
    assert_eq!(
        row("all").await.1,
        reference_calls(&pool, t, now, None, Some(epoch_all)).await
    );
    assert_eq!(
        row("caller-b").await.1,
        reference_calls(&pool, t, now, Some("caller-b"), Some(epoch_all)).await
    );
    assert_eq!(
        row("caller-a").await.1,
        a_since_own_epoch,
        "the later of its own and the 'all' epoch"
    );
    assert_eq!(
        row("all").await.0,
        "ready",
        "the post-epoch calls still meet the minimum"
    );
    for c in ["caller-a", "caller-b"] {
        assert_eq!(row(c).await.0, "learning", "{c}");
    }

    // A window on caller A (the root of its runs) covers A's calls to the
    // service; the runs basis of the service itself is untouched.
    let before = samples_in_window(&pool, SampleSource::Segments, t, svc, now).await;
    insert_window(
        &pool,
        t,
        Some("caller-a"),
        from,
        from + Duration::minutes(74 * 100),
        None,
    )
    .await;
    assert_eq!(
        samples_in_window(&pool, SampleSource::Segments, t, svc, now).await,
        before - 60,
        "a window on the run's root covers the calls it made"
    );
}

// ---------------------------------------------------------------------------
// Liveness
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn liveness_shares_ignore_runs_inside_a_declared_window(pool: PgPool) {
    let (t, sys) = ("t-r24-live", "sys");
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, sys).await;
    sqlx::query(
        "INSERT INTO ai_system_liveness_expectations \
         (id, tenant_id, ai_system_id, mode, expected_min_runs, window_seconds, grace_seconds, enabled) \
         VALUES ('live-r24', $1, $2, 'continuous', 1, 3600, 300, true)",
    )
    .bind(t)
    .bind(sys)
    .execute(&pool)
    .await
    .unwrap();
    for i in 0..10 {
        sqlx::query(
            "INSERT INTO ai_system_runs (id, tenant_id, root_task_id, ai_system_id, started_at, \
             terminal_state, finalized_by) VALUES ($1, $2, $1, $3, $4, 'errored', 'idle')",
        )
        .bind(format!("lv-{i}"))
        .bind(t)
        .bind(sys)
        .bind(now - Duration::minutes(5 + i))
        .execute(&pool)
        .await
        .unwrap();
    }
    let load = || async {
        liveness::load_candidates(&pool)
            .await
            .into_iter()
            .find(|c| c.tenant_id == t)
            .expect("candidate")
    };

    let c = load().await;
    assert_eq!(
        liveness::evaluate(&c, now).map(|f| f.detector),
        Some(liveness::detector::HARD_FAIL_CLIFF)
    );

    // The outage is declared (tenant-wide, as a provider outage would be).
    insert_window(
        &pool,
        t,
        None,
        now - Duration::hours(1),
        now + Duration::hours(1),
        None,
    )
    .await;
    let c = load().await;
    assert_eq!(c.runs_in_window, 10, "the runs still happened");
    assert_eq!(c.excluded_in_window, 10);
    assert_eq!(c.errored_in_window, 0, "none of them is judged");
    assert!(
        liveness::evaluate(&c, now).is_none(),
        "an explained outage kept paging as a cliff"
    );
}

// ---------------------------------------------------------------------------
// The cause ladder
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_change_at_the_edge_of_a_declared_window_is_attributed_to_it(pool: PgPool) {
    // The fix was declared too early: the bad behaviour carries on past the
    // window's end. What fires is the window's tail, and the cause says so.
    let (t, sys) = ("t-r24-cause", "sys");
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, sys).await;
    seed_steady(&pool, t, sys, now, now - Duration::hours(13)).await;
    seed_series(&pool, t, sys, "bad", now - Duration::hours(12), 10, 72, 20).await;
    insert_window(
        &pool,
        t,
        None,
        now - Duration::hours(12) - Duration::minutes(1),
        now - Duration::hours(8),
        None,
    )
    .await;

    let (found, mut outcome) = sweep(&pool, t, sys, now).await;
    let f = action_count(&found)
        .expect("the tail after the window fires")
        .clone();
    assert_eq!(
        f.attribution.cause,
        cause::kind::INCIDENT_WINDOW,
        "{}",
        f.attribution.summary
    );
    assert_eq!(f.attribution.evidence["tenant_wide"], true);
    assert_eq!(f.attribution.evidence["cause_category"], "provider_outage");
    sweeper::record(&pool, &candidate(t, sys), &f, now, &mut outcome).await;
    let (cause_col,): (String,) =
        sqlx::query_as("SELECT attributed_cause FROM ai_system_drift_events WHERE tenant_id = $1")
            .bind(t)
            .fetch_one(&pool)
            .await
            .expect("recorded: attributed_cause fits the column");
    assert_eq!(cause_col, "incident_window");
}
