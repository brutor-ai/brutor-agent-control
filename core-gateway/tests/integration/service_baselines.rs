// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Service baselines and drift from run segments,
//! against a real Postgres.
//!
//! What is pinned here, as classes:
//!
//! * **Sample selection** — a service is baselined from the segments it
//!   served, under the same honesty clauses as runs (replays, untrusted
//!   chains and over-reopened runs excluded), with the caller attached.
//! * **Basis separation** — a system that both roots runs and serves
//!   segments keeps two independent baseline sets, and re-sweeping upserts
//!   in place on the baseline key rather than accumulating.
//! * **Per-caller threshold** — sub-baselines only for callers with >= 20 %
//!   share AND the minimum on their own; a caller that stops qualifying is
//!   retired to `stale`, never left looking live.
//! * **Drift events** — carry `basis` and `caller_system_id`; cooldown is
//!   per basis and the aggregate speaks for its callers.
//! * **Cause facts** — contract transitions, dependency-edge changes and new
//!   callers are read from the ledger and win the ladder when they line up.

use chrono::{DateTime, Duration, Utc};
use serde_json::json;
use sqlx::PgPool;

use brutor_gateway_core::services::drift::{
    baseline::{self, SampleSource},
    basis,
    cause::{self, Attribution},
    detect::Detection,
    drift_class, metric, profiles,
    sweeper::{self, DriftCandidate, Finding},
};

use super::common;

// ---------------------------------------------------------------------------
// Seeding
// ---------------------------------------------------------------------------

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

/// One completed, intact run rooted by `system_id`.
async fn seed_run(
    pool: &PgPool,
    tenant: &str,
    id: &str,
    system_id: &str,
    at: DateTime<Utc>,
    action_count: i32,
) {
    sqlx::query(
        "INSERT INTO ai_system_runs \
         (id, tenant_id, root_task_id, ai_system_id, started_at, ended_at, duration_ms, \
          terminal_state, finalized_by, action_count, tool_call_count, max_delegation_depth, \
          total_cost_usd, tool_error_count, input_bytes, first_prompt_tokens, \
          tool_set_signature, client_label, chain_integrity, reopened_count, is_replay) \
         VALUES ($1, $2, $1, $3, $4, $4, 1000, 'completed', 'idle', $5, $5, 1, \
                 0.004, 0, 2048, 400, 'search|fetch', 'sdk', 'intact', 0, false)",
    )
    .bind(id)
    .bind(tenant)
    .bind(system_id)
    .bind(at)
    .bind(action_count)
    .execute(pool)
    .await
    .expect("seed_run");
}

/// A call `caller` made to `service`: the caller's run plus the service's
/// segment inside it, the way the ledger writes them. A direct call: the
/// caller is also the run's root.
#[allow(clippy::too_many_arguments)]
async fn seed_call(
    pool: &PgPool,
    tenant: &str,
    id: &str,
    service: &str,
    caller: &str,
    at: DateTime<Utc>,
    actions: i32,
    contract_id: Option<&str>,
) {
    seed_chain_call(
        pool,
        tenant,
        id,
        service,
        caller,
        Some(caller),
        at,
        actions,
        contract_id,
    )
    .await;
}

/// A segment of `service` inside a run rooted at `root`, entered from
/// `caller` — the IMMEDIATE calling system (`caller_system_id`). In
/// A→B→C, C's segment has root A and caller B.
#[allow(clippy::too_many_arguments)]
async fn seed_chain_call(
    pool: &PgPool,
    tenant: &str,
    id: &str,
    service: &str,
    root: &str,
    caller: Option<&str>,
    at: DateTime<Utc>,
    actions: i32,
    contract_id: Option<&str>,
) {
    seed_run(pool, tenant, id, root, at, actions + 2).await;
    sqlx::query(
        "INSERT INTO ai_system_run_segments \
         (id, run_id, tenant_id, root_task_id, root_system_id, segment_system_id, min_depth, \
          first_action_at, last_action_at, active_ms, action_count, llm_call_count, \
          tool_call_count, error_count, tool_error_count, total_tokens, cost_usd, models_used, \
          tool_set_signature, outcome, contract_id, caller_system_id) \
         VALUES ($1, $2, $3, $2, $4, $5, 1, $6, $6, 700, $7, $7, 1, 0, 0, $8, $9, $10, \
                 'score', 'answered', $11, $12)",
    )
    .bind(format!("seg-{id}"))
    .bind(id)
    .bind(tenant)
    .bind(root)
    .bind(service)
    .bind(at)
    .bind(actions)
    .bind(i64::from(actions) * 400)
    .bind(0.001 * f64::from(actions))
    .bind(json!({"gpt-x": actions}))
    .bind(contract_id)
    .bind(caller)
    .execute(pool)
    .await
    .expect("seed_chain_call");
}

/// Where a history's change begins: the last 40 calls land every 30 min
/// over the final 20 hours, so the change sits inside the 24h observation
/// window with calls dense enough for the cause window (±2h) to see it.
fn change_start(now: DateTime<Utc>) -> DateTime<Utc> {
    now - Duration::minutes(40 * 30) - Duration::minutes(5)
}

/// `total` calls from `caller`: steady ones spread over 13 days, then 40
/// dense recent ones, scaled by `step` and served under `recent_contract`.
#[allow(clippy::too_many_arguments)]
async fn serve_history(
    pool: &PgPool,
    tenant: &str,
    service: &str,
    caller: &str,
    now: DateTime<Utc>,
    total: usize,
    step: Option<i32>,
    contracts: (Option<&str>, Option<&str>),
) {
    const RECENT: usize = 40;
    let steady = total.saturating_sub(RECENT);
    let start = change_start(now);
    for i in 0..total {
        let (at, actions, contract) = if i < steady {
            // Oldest ~13 days back, the newest steady call a day before the change.
            let span_min = (12 * 24 * 60) as f64;
            let at = start
                - Duration::hours(24)
                - Duration::minutes((span_min * (1.0 - i as f64 / steady.max(1) as f64)) as i64);
            (at, 3 + (i % 3) as i32 - 1, contracts.0)
        } else {
            let k = (i - steady) as i64;
            let at = start + Duration::minutes(30 * k);
            let base = 3 + (i % 3) as i32 - 1;
            (at, base * step.unwrap_or(1), contracts.1)
        };
        seed_call(
            pool,
            tenant,
            &format!("r-{service}-{caller}-{i}"),
            service,
            caller,
            at,
            actions,
            contract,
        )
        .await;
    }
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

async fn baseline_keys(pool: &PgPool, tenant: &str, system: &str) -> Vec<(String, String, i64)> {
    sqlx::query_as(
        "SELECT basis, caller_system_id, COUNT(*) FROM ai_system_baselines \
          WHERE tenant_id = $1 AND ai_system_id = $2 AND state <> 'stale' \
          GROUP BY basis, caller_system_id ORDER BY basis, caller_system_id",
    )
    .bind(tenant)
    .bind(system)
    .fetch_all(pool)
    .await
    .unwrap()
}

// ---------------------------------------------------------------------------
// Sample selection
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn segment_samples_carry_the_caller_and_exclude_untrustworthy_runs(pool: PgPool) {
    let t = "t-sb-samples";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "svc", "agent").await;
    seed_system(&pool, t, "root", "agent").await;

    for (i, id) in ["good", "replay", "broken", "churned"].iter().enumerate() {
        seed_call(
            &pool,
            t,
            id,
            "svc",
            "root",
            now - Duration::hours(3 + i as i64),
            4,
            Some("ctr-1"),
        )
        .await;
    }
    sqlx::query("UPDATE ai_system_runs SET is_replay = true WHERE id = 'replay'")
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("UPDATE ai_system_runs SET chain_integrity = 'fragmented' WHERE id = 'broken'")
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("UPDATE ai_system_runs SET reopened_count = 9 WHERE id = 'churned'")
        .execute(&pool)
        .await
        .unwrap();

    let (from, to) = baseline::window(now);
    let seg = baseline::load_samples(&pool, SampleSource::Segments, t, "svc", from, to, 3).await;
    assert_eq!(
        seg.len(),
        1,
        "a segment of a replay, an untrusted chain or an over-reopened run is \
         a fragment of a fragment — it must not teach the service's baseline"
    );
    let s = &seg[0];
    assert_eq!(s.caller_system_id.as_deref(), Some("root"));
    assert_eq!(
        s.terminal_state, "answered",
        "the outcome is the terminal state"
    );
    assert_eq!(s.llm_call_count, Some(4));
    assert_eq!(s.total_tokens, Some(1600));
    assert_eq!(s.active_duration_ms, 700);
    assert!(s.max_delegation_depth.is_none() && s.input_bytes.is_none());

    // The same service has no runs of its own: the run basis is empty, not
    // polluted by the segments.
    let runs = baseline::load_samples(&pool, SampleSource::Runs, t, "svc", from, to, 3).await;
    assert!(runs.is_empty());
}

// ---------------------------------------------------------------------------
// Basis separation + upsert on the baseline key
// ---------------------------------------------------------------------------

/// A delegation stopped by a hold for a person is governance
/// waiting on someone, not the service's work: it never teaches the
/// service's baseline (a hold is not degradation).
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_held_delegation_is_not_a_sample_of_the_service(pool: PgPool) {
    let t = "t-sb-held";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "svc", "agent").await;
    seed_system(&pool, t, "root", "agent").await;
    for (i, id) in ["served", "held"].iter().enumerate() {
        seed_call(
            &pool,
            t,
            id,
            "svc",
            "root",
            now - Duration::hours(3 + i as i64),
            4,
            None,
        )
        .await;
    }
    sqlx::query(
        "UPDATE ai_system_run_segments SET outcome = 'failed', error_cause = 'blocked_by_policy', \
         held_approval_request_id = 'apr-1' WHERE run_id = 'held'",
    )
    .execute(&pool)
    .await
    .unwrap();

    let (from, to) = baseline::window(now);
    let seg = baseline::load_samples(&pool, SampleSource::Segments, t, "svc", from, to, 3).await;
    assert_eq!(seg.len(), 1, "only the served call is the service's sample");
    assert_eq!(seg[0].terminal_state, "answered");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_system_with_runs_and_segments_keeps_two_independent_baselines(pool: PgPool) {
    let t = "t-sb-both";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "svc", "agent").await;
    seed_system(&pool, t, "root", "agent").await;

    // Its own work: 6 actions a run. The work it serves: ~3 a call.
    for i in 0..60 {
        seed_run(
            &pool,
            t,
            &format!("own-{i}"),
            "svc",
            now - Duration::hours(30 + i * 4),
            6,
        )
        .await;
    }
    serve_history(&pool, t, "svc", "root", now, 60, None, (None, None)).await;

    let mut outcome = sweeper::SweepOutcome::default();
    sweeper::sweep_system(&pool, &candidate(t, "svc"), now, &mut outcome).await;

    let n_runs = profiles::collected_metrics(basis::RUNS, Some("agent")).len() as i64;
    let n_seg = profiles::collected_metrics(basis::SEGMENTS, Some("agent")).len() as i64;
    let expect = vec![
        ("runs".to_string(), "all".to_string(), n_runs),
        ("segments".to_string(), "all".to_string(), n_seg),
        ("segments".to_string(), "root".to_string(), n_seg),
    ];
    assert_eq!(baseline_keys(&pool, t, "svc").await, expect);

    let medians: Vec<(String, String, Option<f64>)> = sqlx::query_as(
        "SELECT basis, caller_system_id, median FROM ai_system_baselines \
          WHERE tenant_id = $1 AND ai_system_id = 'svc' AND metric_key = $2 \
          ORDER BY basis, caller_system_id",
    )
    .bind(t)
    .bind(metric::ACTION_COUNT)
    .fetch_all(&pool)
    .await
    .unwrap();
    assert_eq!(
        medians,
        vec![
            ("runs".into(), "all".into(), Some(6.0)),
            ("segments".into(), "all".into(), Some(3.0)),
            ("segments".into(), "root".into(), Some(3.0)),
        ],
        "pooling the work a system starts with the work it is called for \
         would let either population mask drift in the other"
    );

    // A second sweep rewrites the same rows: the baseline key is the arbiter.
    sweeper::sweep_system(&pool, &candidate(t, "svc"), now, &mut outcome).await;
    assert_eq!(baseline_keys(&pool, t, "svc").await, expect);
    let (total,): (i64,) = sqlx::query_as(
        "SELECT COUNT(*) FROM ai_system_baselines WHERE tenant_id = $1 AND ai_system_id = 'svc'",
    )
    .bind(t)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(total, n_runs + 2 * n_seg);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_root_that_serves_nobody_writes_no_segment_baselines(pool: PgPool) {
    let t = "t-sb-root-only";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "root", "agent").await;
    for i in 0..10 {
        seed_run(
            &pool,
            t,
            &format!("r-{i}"),
            "root",
            now - Duration::hours(i + 1),
            5,
        )
        .await;
    }
    let mut outcome = sweeper::SweepOutcome::default();
    sweeper::sweep_system(&pool, &candidate(t, "root"), now, &mut outcome).await;
    let keys = baseline_keys(&pool, t, "root").await;
    assert!(
        keys.iter().all(|(b, _, _)| b == "runs"),
        "segment baselines are for systems that serve segments: {keys:?}"
    );
}

// ---------------------------------------------------------------------------
// Per-caller threshold + retirement
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn per_caller_rows_exist_only_for_qualifying_callers_and_retire_when_they_stop(pool: PgPool) {
    let t = "t-sb-callers";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    for s in ["svc", "a", "b", "c"] {
        seed_system(&pool, t, s, "agent").await;
    }
    // 300 calls: a = 200 (67 %), b = 60 (20 %, meets 50), c = 40 (13 %, < 50).
    serve_history(&pool, t, "svc", "a", now, 200, None, (None, None)).await;
    serve_history(&pool, t, "svc", "b", now, 60, None, (None, None)).await;
    serve_history(&pool, t, "svc", "c", now, 40, None, (None, None)).await;

    let mut outcome = sweeper::SweepOutcome::default();
    sweeper::sweep_system(&pool, &candidate(t, "svc"), now, &mut outcome).await;

    let callers: Vec<String> = baseline_keys(&pool, t, "svc")
        .await
        .into_iter()
        .filter(|(b, _, _)| b == "segments")
        .map(|(_, c, _)| c)
        .collect();
    assert_eq!(
        callers,
        vec!["a", "all", "b"],
        "c has neither the share nor the minimum"
    );

    // b stops calling: its sub-baseline is retired, not left 'ready'.
    sqlx::query("DELETE FROM ai_system_run_segments WHERE tenant_id = $1 AND root_system_id = 'b'")
        .bind(t)
        .execute(&pool)
        .await
        .unwrap();
    sweeper::sweep_system(&pool, &candidate(t, "svc"), now, &mut outcome).await;
    let stale: Vec<(String,)> = sqlx::query_as(
        "SELECT DISTINCT caller_system_id FROM ai_system_baselines \
          WHERE tenant_id = $1 AND ai_system_id = 'svc' AND state = 'stale'",
    )
    .bind(t)
    .fetch_all(&pool)
    .await
    .unwrap();
    assert_eq!(stale, vec![("b".to_string(),)]);

    // Nobody calls any more: every segment row is stale; the run basis is
    // not the segment retirement's business.
    sqlx::query("DELETE FROM ai_system_run_segments WHERE tenant_id = $1")
        .bind(t)
        .execute(&pool)
        .await
        .unwrap();
    sweeper::sweep_system(&pool, &candidate(t, "svc"), now, &mut outcome).await;
    let live: Vec<(String, String)> = sqlx::query_as(
        "SELECT DISTINCT basis, caller_system_id FROM ai_system_baselines \
          WHERE tenant_id = $1 AND ai_system_id = 'svc' AND state <> 'stale'",
    )
    .bind(t)
    .fetch_all(&pool)
    .await
    .unwrap();
    assert_eq!(live, vec![("runs".to_string(), "all".to_string())]);
}

// ---------------------------------------------------------------------------
// Drift over segment baselines
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn service_drift_is_recorded_on_the_segment_basis(pool: PgPool) {
    let t = "t-sb-drift";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "svc", "agent").await;
    seed_system(&pool, t, "root", "agent").await;
    serve_history(&pool, t, "svc", "root", now, 160, Some(5), (None, None)).await;

    let mut outcome = sweeper::SweepOutcome::default();
    let found = sweeper::sweep_system(&pool, &candidate(t, "svc"), now, &mut outcome).await;
    assert!(
        !found.is_empty(),
        "a sustained 5x change in served calls is drift"
    );
    assert!(
        found.iter().all(|f| f.basis == basis::SEGMENTS),
        "svc roots no runs"
    );
    assert_eq!(
        found[0].caller_system_id, None,
        "the aggregate is detected first so it speaks for its callers"
    );
    assert!(found[0].change_point.is_some());

    for f in &found {
        sweeper::record(&pool, &candidate(t, "svc"), f, now, &mut outcome).await;
    }

    type EventRow = (
        String,
        Option<String>,
        String,
        Option<DateTime<Utc>>,
        Option<String>,
    );
    let rows: Vec<EventRow> = sqlx::query_as(
        "SELECT d.basis, d.caller_system_id, d.drift_class, d.change_point_at, a.error_code \
               FROM ai_system_drift_events d \
               LEFT JOIN system_alerts a ON a.id = d.system_alert_id \
              WHERE d.tenant_id = $1",
    )
    .bind(t)
    .fetch_all(&pool)
    .await
    .unwrap();
    assert_eq!(
        rows.len(),
        1,
        "the single caller's sub-baseline sees the same change — one event, \
         not one per population: {rows:?}"
    );
    let (b, caller, class, cp, code) = &rows[0];
    assert_eq!(b, "segments");
    assert_eq!(caller, &None);
    assert!(drift_class::ALL.contains(&class.as_str()));
    assert!(cp.is_some(), "the change point is persisted, not dropped");
    assert!(
        code.as_deref().is_some_and(|c| c.ends_with(":segments")),
        "the alert's dedup identity names the basis: {code:?}"
    );
    assert!(outcome.suppressed >= 1);
}

fn finding(basis: &'static str, caller: Option<&str>) -> Finding {
    Finding {
        basis,
        caller_system_id: caller.map(String::from),
        bucket: "all".into(),
        detection: Detection {
            metric_key: metric::ACTION_COUNT.into(),
            drift_class: drift_class::BEHAVIOURAL,
            baseline_value: Some(3.0),
            observed_value: Some(15.0),
            effect_size: 8.0,
            direction: "up",
            severity: "high",
            change_point_index: None,
        },
        attribution: Attribution::unexplained(vec![]),
        change_point: None,
        evidence: Default::default(),
        headline: None,
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn cooldown_is_per_basis_and_the_aggregate_speaks_for_its_callers(pool: PgPool) {
    let t = "t-sb-cooldown";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    for s in ["svc", "a", "b"] {
        seed_system(&pool, t, s, "agent").await;
    }
    let c = candidate(t, "svc");
    let mut o = sweeper::SweepOutcome::default();

    // Two callers drift independently: both are reported.
    sweeper::record(&pool, &c, &finding(basis::SEGMENTS, Some("a")), now, &mut o).await;
    sweeper::record(&pool, &c, &finding(basis::SEGMENTS, Some("b")), now, &mut o).await;
    // The same caller again: suppressed.
    sweeper::record(&pool, &c, &finding(basis::SEGMENTS, Some("a")), now, &mut o).await;
    // The aggregate is not silenced by a caller's event…
    sweeper::record(&pool, &c, &finding(basis::SEGMENTS, None), now, &mut o).await;
    // …and the system's own runs are not silenced by its service events.
    sweeper::record(&pool, &c, &finding(basis::RUNS, None), now, &mut o).await;
    assert_eq!(o.suppressed, 1);

    // A fresh system: once the aggregate has spoken, callers are silenced.
    let c2 = candidate(t, "a");
    let mut o2 = sweeper::SweepOutcome::default();
    sweeper::record(&pool, &c2, &finding(basis::SEGMENTS, None), now, &mut o2).await;
    sweeper::record(
        &pool,
        &c2,
        &finding(basis::SEGMENTS, Some("b")),
        now,
        &mut o2,
    )
    .await;
    assert_eq!(o2.suppressed, 1);

    let rows: Vec<(String, String, Option<String>)> = sqlx::query_as(
        "SELECT ai_system_id, basis, caller_system_id FROM ai_system_drift_events \
          WHERE tenant_id = $1 ORDER BY ai_system_id, basis, caller_system_id NULLS FIRST",
    )
    .bind(t)
    .fetch_all(&pool)
    .await
    .unwrap();
    assert_eq!(
        rows,
        vec![
            ("a".into(), "segments".into(), None),
            ("svc".into(), "runs".into(), None),
            ("svc".into(), "segments".into(), None),
            ("svc".into(), "segments".into(), Some("a".into())),
            ("svc".into(), "segments".into(), Some("b".into())),
        ]
    );
}

/// The control plane opens `dependency:<target>` events on a ROOT when a
/// critical dependency is unhealthy. They are a different producer and a
/// different fact: they must not silence the core's own dependency-class
/// alert (tool errors) on that root for the cooldown.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_control_plane_dependency_finding_does_not_silence_the_cores_own_alert(pool: PgPool) {
    let t = "t-sb-cp-dep";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "root", "agent").await;
    sqlx::query(
        "INSERT INTO ai_system_drift_events \
             (id, tenant_id, ai_system_id, drift_class, metric_key, detected_at, basis) \
         VALUES ('drift-cp', $1, 'root', $2, $3, $4, 'runs')",
    )
    .bind(t)
    .bind(drift_class::DEPENDENCY)
    .bind(format!("{}svc", metric::CP_DEPENDENCY_PREFIX))
    .bind(now - Duration::minutes(5))
    .execute(&pool)
    .await
    .unwrap();

    let mut core_own = finding(basis::RUNS, None);
    core_own.detection.metric_key = metric::TOOL_ERROR_RATE.into();
    core_own.detection.drift_class = drift_class::DEPENDENCY;
    let mut o = sweeper::SweepOutcome::default();
    sweeper::record(&pool, &candidate(t, "root"), &core_own, now, &mut o).await;
    assert_eq!(
        o.suppressed, 0,
        "the CP's dependency finding is not the core's own drift"
    );

    // The core's own event still cools down its next one.
    sweeper::record(&pool, &candidate(t, "root"), &core_own, now, &mut o).await;
    assert_eq!(o.suppressed, 1);
}

// ---------------------------------------------------------------------------
// Cause attribution on the segment basis
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_service_contract_promotion_is_named_as_the_cause(pool: PgPool) {
    let t = "t-sb-contract";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "svc", "agent").await;
    seed_system(&pool, t, "root", "agent").await;
    serve_history(
        &pool,
        t,
        "svc",
        "root",
        now,
        160,
        Some(5),
        (Some("ctr-1"), Some("ctr-2")),
    )
    .await;

    let mut outcome = sweeper::SweepOutcome::default();
    let found = sweeper::sweep_system(&pool, &candidate(t, "svc"), now, &mut outcome).await;
    let first = found.first().expect("a finding");
    assert_eq!(
        first.attribution.cause,
        cause::kind::CONTRACT_CHANGE,
        "every served call records its contract; a transition at the change \
         point is the explanation: {:?}",
        first.attribution
    );
    assert_eq!(first.attribution.evidence["from_contract_id"], "ctr-1");
    assert_eq!(first.attribution.evidence["to_contract_id"], "ctr-2");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unchanged_contract_is_not_blamed(pool: PgPool) {
    let t = "t-sb-contract-same";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "svc", "agent").await;
    seed_system(&pool, t, "root", "agent").await;
    serve_history(
        &pool,
        t,
        "svc",
        "root",
        now,
        160,
        Some(5),
        (Some("ctr-1"), Some("ctr-1")),
    )
    .await;
    let cp = change_start(now) + Duration::minutes(60);
    assert!(
        cause::find_segment_contract_change(&pool, t, "svc", cp)
            .await
            .unwrap()
            .is_none()
    );
}

async fn declare_edge(pool: &PgPool, tenant: &str, caller: &str, service: &str, at: DateTime<Utc>) {
    sqlx::query(
        "INSERT INTO ai_system_dependencies \
         (id, tenant_id, ai_system_id, role, depends_on_system_id, declared_at, updated_at) \
         VALUES ($1, $2, $3, 'shared_service', $4, $5, $5)",
    )
    .bind(format!("dep-{caller}-{service}"))
    .bind(tenant)
    .bind(caller)
    .bind(service)
    .bind(at)
    .execute(pool)
    .await
    .expect("declare_edge");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_dependency_edge_change_near_the_change_point_is_a_composition_change(pool: PgPool) {
    let t = "t-sb-composition";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    for s in ["svc", "a", "b"] {
        seed_system(&pool, t, s, "agent").await;
    }
    let cp = now - Duration::hours(6);
    declare_edge(&pool, t, "a", "svc", cp - Duration::minutes(20)).await;
    declare_edge(&pool, t, "b", "svc", cp - Duration::days(5)).await;

    let agg = cause::find_segment_composition_change(&pool, t, "svc", None, cp)
        .await
        .unwrap()
        .expect("a's edge lines up");
    assert!(
        agg.description
            .contains("'a' declared its critical dependency on this system")
    );

    let for_a = cause::find_segment_composition_change(&pool, t, "svc", Some("a"), cp)
        .await
        .unwrap();
    assert!(for_a.is_some(), "a's own sub-baseline sees its own edge");
    let for_b = cause::find_segment_composition_change(&pool, t, "svc", Some("b"), cp)
        .await
        .unwrap();
    assert!(
        for_b.is_none(),
        "b's sub-baseline must not be blamed on a's edge, nor on its own edge from days ago"
    );

    let facts = cause::CauseFacts {
        composition_change: Some(agg),
        new_caller: Some("a".into()),
        ..Default::default()
    };
    assert_eq!(
        cause::attribute(&facts, Some(cp)).cause,
        cause::kind::COMPOSITION_CHANGE
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_caller_first_seen_at_the_change_point_is_a_traffic_mix_change(pool: PgPool) {
    let t = "t-sb-newcaller";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    for s in ["svc", "old", "new"] {
        seed_system(&pool, t, s, "agent").await;
    }
    for i in 0..10 {
        seed_call(
            &pool,
            t,
            &format!("old-{i}"),
            "svc",
            "old",
            now - Duration::days(i + 1),
            3,
            None,
        )
        .await;
    }
    let cp = now - Duration::hours(4);
    seed_call(
        &pool,
        t,
        "new-0",
        "svc",
        "new",
        cp + Duration::minutes(30),
        3,
        None,
    )
    .await;
    seed_call(
        &pool,
        t,
        "new-1",
        "svc",
        "new",
        cp + Duration::hours(1),
        3,
        None,
    )
    .await;

    let (from, _) = baseline::window(now);
    assert_eq!(
        cause::find_new_caller(&pool, t, "svc", from, cp)
            .await
            .unwrap()
            .as_deref(),
        Some("new")
    );
    assert_eq!(
        cause::find_new_caller(&pool, t, "svc", from, now - Duration::days(3))
            .await
            .unwrap(),
        None,
        "a long-standing caller is not new"
    );
}

// ---------------------------------------------------------------------------
// One definition of "caller": the IMMEDIATE calling AI System
// ---------------------------------------------------------------------------

/// A→B→C: every call reaches C through B inside runs rooted at A. C's
/// per-caller cohort, its samples and its new-caller cause all name B —
/// never the root A. Segments with no caller count only toward the
/// aggregate.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_caller_is_the_immediate_system_not_the_runs_root(pool: PgPool) {
    let t = "t-sb-immediate";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    for s in ["sys-a", "sys-b", "sys-c"] {
        seed_system(&pool, t, s, "agent").await;
    }
    let (from, to) = baseline::window(now);
    for i in 0..60 {
        seed_chain_call(
            &pool,
            t,
            &format!("abc-{i}"),
            "sys-c",
            "sys-a",
            Some("sys-b"),
            now - Duration::hours(1 + i),
            3,
            None,
        )
        .await;
    }
    // Calls from a step in no AI System: aggregate only.
    for i in 0..40 {
        seed_chain_call(
            &pool,
            t,
            &format!("anon-{i}"),
            "sys-c",
            "sys-a",
            None,
            now - Duration::hours(1 + i) - Duration::minutes(10),
            3,
            None,
        )
        .await;
    }

    let seg = baseline::load_samples(&pool, SampleSource::Segments, t, "sys-c", from, to, 3).await;
    assert_eq!(seg.len(), 100);
    assert!(
        seg.iter()
            .all(|s| s.caller_system_id.is_none() || s.caller_system_id.as_deref() == Some("sys-b")),
        "the root sys-a must never appear as C's caller"
    );
    let cohorts = baseline::cohorts(SampleSource::Segments, &seg);
    let keys: Vec<&str> = cohorts
        .iter()
        .map(|c| c.caller_system_id.as_str())
        .collect();
    assert_eq!(keys, vec![baseline::ALL_CALLERS, "sys-b"]);
    assert_eq!(
        cohorts[0].samples.len(),
        100,
        "NULL-caller segments count in the aggregate"
    );
    assert_eq!(cohorts[1].samples.len(), 60, "…and never toward a caller");

    // The new-caller cause names the immediate caller too.
    let t2 = "t-sb-immediate-new";
    common::seed_tenant(&pool, t2).await;
    for s in ["sys-a", "sys-b", "sys-c", "sys-old"] {
        seed_system(&pool, t2, s, "agent").await;
    }
    for i in 0..10 {
        seed_call(
            &pool,
            t2,
            &format!("o-{i}"),
            "sys-c",
            "sys-old",
            now - Duration::days(i + 1),
            3,
            None,
        )
        .await;
    }
    let cp = now - Duration::hours(4);
    // sys-a has been the ROOT of old runs too — only B is new as a caller.
    for i in 0..3 {
        seed_chain_call(
            &pool,
            t2,
            &format!("r-{i}"),
            "sys-c",
            "sys-a",
            Some("sys-old"),
            now - Duration::days(i + 2),
            3,
            None,
        )
        .await;
    }
    seed_chain_call(
        &pool,
        t2,
        "n-0",
        "sys-c",
        "sys-a",
        Some("sys-b"),
        cp + Duration::minutes(30),
        3,
        None,
    )
    .await;
    assert_eq!(
        cause::find_new_caller(&pool, t2, "sys-c", from, cp)
            .await
            .unwrap()
            .as_deref(),
        Some("sys-b")
    );
}
