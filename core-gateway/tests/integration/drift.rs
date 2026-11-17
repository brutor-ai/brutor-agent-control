// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Drift detection against a real Postgres.
//!
//! The statistics are pure and covered exhaustively by the unit corpus
//! (`services::drift::corpus`), which proves the detector fires on injected
//! changes and stays silent on no-change controls. None of that touches a
//! database, and that is the gap this file exists to close.
//!
//! The failure mode being pinned here is specific and was **real**: the
//! candidate query selected `rg.autonomy_level`, a column that does not
//! exist. `load_candidates` swallows a query error into a
//! `WARN` and returns an empty vector, so every sweep would have found
//! zero candidates, written zero baselines, raised zero alerts — and
//! logged nothing an operator would read as broken. A perfectly healthy
//! looking dead loop.
//!
//! No unit test can catch that, because the bug lives entirely in the
//! agreement between a SQL string and a schema. So the rule this file
//! encodes is: **every SQL statement in the drift module gets executed
//! against the real schema at least once.**

use chrono::{DateTime, Duration, Utc};
use sqlx::PgPool;

use brutor_gateway_core::services::drift::{
    MIN_RUNS_FOR_BASELINE,
    baseline::{self, SampleSource},
    basis, drift_class, drift_status, exclusion, metric,
    sweeper::{self, DriftCandidate},
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

/// One completed run with a given step count and cost.
#[allow(clippy::too_many_arguments)]
async fn seed_run(
    pool: &PgPool,
    tenant: &str,
    id: &str,
    system_id: &str,
    at: DateTime<Utc>,
    action_count: i32,
    cost: f64,
) {
    sqlx::query(
        "INSERT INTO ai_system_runs \
         (id, tenant_id, root_task_id, ai_system_id, started_at, ended_at, duration_ms, \
          terminal_state, finalized_by, action_count, tool_call_count, max_delegation_depth, \
          total_cost_usd, tool_error_count, input_bytes, first_prompt_tokens, \
          tool_set_signature, client_label, chain_integrity, reopened_count, is_replay, \
          outcome_id, outcome_conformance, outcome_counts_toward_baseline) \
         VALUES ($1, $2, $1, $3, $4, $4, 1000, 'completed', 'idle', $5, $5, 1, \
                 $6, 0, 2048, 400, 'search|fetch', 'sdk', 'intact', 0, false, \
                 'task_completed', 'declared', true)",
    )
    .bind(id)
    .bind(tenant)
    .bind(system_id)
    .bind(at)
    .bind(action_count)
    .bind(cost)
    .execute(pool)
    .await
    .expect("seed_run");
}

/// A steady history followed by an optional sustained step change, which is
/// the shape the corpus proves the detector responds to.
async fn seed_history(
    pool: &PgPool,
    tenant: &str,
    system_id: &str,
    now: DateTime<Utc>,
    total: usize,
    step_multiplier: Option<f64>,
) {
    // Spread across the baseline window, oldest first. The last 24h is the
    // observation window, so anything after `change_at` lands in it.
    let base_steps = 6.0_f64;
    for i in 0..total {
        // Deterministic jitter — a reproducible failure beats a realistic one.
        let jitter = ((i % 5) as f64 - 2.0) * 0.1;
        let in_observation_window = i >= total.saturating_sub(total / 4);
        let mult = match step_multiplier {
            Some(m) if in_observation_window => m,
            _ => 1.0,
        };
        let steps = (base_steps * mult + jitter).round().max(1.0);

        // Oldest run ~13 days back, newest a few minutes ago.
        let age_hours = (13 * 24) as f64 * (1.0 - i as f64 / total as f64);
        seed_run(
            pool,
            tenant,
            &format!("run-{system_id}-{i}"),
            system_id,
            now - Duration::minutes((age_hours * 60.0) as i64) - Duration::minutes(5),
            steps as i32,
            0.004 * mult,
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

// ---------------------------------------------------------------------------
// The candidate query — the statement that was broken
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_candidate_query_runs_against_the_real_schema(pool: PgPool) {
    // This test exists because `load_candidates` degrades a SQL error into
    // an empty result. A referenced-but-nonexistent column therefore turns
    // the entire drift feature into a silent no-op. Executing the statement
    // once against the real schema is the only thing that catches it.
    let t = "t-df-query";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys-a", "agent").await;

    let mine: Vec<_> = sweeper::load_candidates(&pool, 14)
        .await
        .into_iter()
        .filter(|c| c.tenant_id == t)
        .collect();

    assert_eq!(
        mine.len(),
        1,
        "no candidates came back — if the SQL referenced a column that does \
         not exist, this is exactly what a broken sweep looks like: quiet"
    );
    assert_eq!(mine[0].ai_system_id, "sys-a");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn opted_out_and_inactive_systems_are_not_swept(pool: PgPool) {
    let t = "t-df-skip";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys-out", "agent").await;
    seed_system(&pool, t, "sys-off", "agent").await;
    seed_system(&pool, t, "sys-ok", "agent").await;
    sqlx::query("UPDATE resource_groups SET baseline_opt_out = true WHERE id = 'sys-out'")
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("UPDATE resource_groups SET is_active = false WHERE id = 'sys-off'")
        .execute(&pool)
        .await
        .unwrap();

    let ids: Vec<String> = sweeper::load_candidates(&pool, 14)
        .await
        .into_iter()
        .filter(|c| c.tenant_id == t)
        .map(|c| c.ai_system_id)
        .collect();

    assert_eq!(
        ids,
        vec!["sys-ok"],
        "opt-out is the escape hatch for legitimately erratic systems — if \
         it does not hold, the operator mutes the whole alert category instead"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn ordinary_resource_groups_are_not_ai_systems(pool: PgPool) {
    let t = "t-df-rg";
    common::seed_tenant(&pool, t).await;
    common::seed_resource_group(&pool, "plain-rg", t).await;

    let mine: Vec<_> = sweeper::load_candidates(&pool, 14)
        .await
        .into_iter()
        .filter(|c| c.tenant_id == t)
        .collect();
    assert!(
        mine.is_empty(),
        "drift is scoped to AI Systems; a plain access group has no behaviour \
         of its own to have a baseline for"
    );
}

// ---------------------------------------------------------------------------
// Sample loading — the other load-bearing WHERE clause
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn samples_exclude_replays_broken_chains_and_reopened_runs(pool: PgPool) {
    let t = "t-df-samples";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;

    seed_run(&pool, t, "good", "sys", now - Duration::hours(2), 6, 0.004).await;
    seed_run(
        &pool,
        t,
        "replay",
        "sys",
        now - Duration::hours(2),
        6,
        0.004,
    )
    .await;
    seed_run(
        &pool,
        t,
        "broken",
        "sys",
        now - Duration::hours(2),
        6,
        0.004,
    )
    .await;
    seed_run(
        &pool,
        t,
        "churned",
        "sys",
        now - Duration::hours(2),
        6,
        0.004,
    )
    .await;
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
    let samples = baseline::load_samples(&pool, SampleSource::Runs, t, "sys", from, to, 3).await;

    assert_eq!(
        samples.len(),
        1,
        "a baseline is a claim about what this system normally does — a \
         simulation, a run whose chain could not be trusted, and a run \
         stitched back together three times are none of them evidence of that"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_sample_cap_does_not_disturb_a_history_below_it(pool: PgPool) {
    // `SAMPLES_SQL` bounds the load with an inner `ORDER BY started_at
    // DESC LIMIT` and re-sorts ascending on the way out, so that a busy
    // system cannot pull a 14-day history into the proxy process. Below
    // the cap that must be a no-op: the same rows, in the same order.
    // Ordering is load-bearing downstream — the change-point walk and the
    // sweeper's observation-window suffix both assume oldest-first.
    let t = "t-df-cap";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;
    seed_history(&pool, t, "sys", now, 60, None).await;

    let (from, to) = baseline::window(now);
    let samples = baseline::load_samples(&pool, SampleSource::Runs, t, "sys", from, to, 3).await;

    assert_eq!(samples.len(), 60, "every eligible run below the cap loads");
    assert!(
        samples
            .windows(2)
            .all(|w| w[0].started_at <= w[1].started_at),
        "samples must come back oldest-first"
    );
}

// ---------------------------------------------------------------------------
// Baseline persistence
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn baselines_upsert_in_place_rather_than_accumulating(pool: PgPool) {
    let t = "t-df-upsert";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;
    seed_history(&pool, t, "sys", now, 60, None).await;

    let (from, to) = baseline::window(now);
    let samples = baseline::load_samples(&pool, SampleSource::Runs, t, "sys", from, to, 3).await;
    let computed = baseline::compute_all(
        &samples,
        basis::RUNS,
        Some("agent"),
        &exclusion::MetricFloors::none(),
    );
    assert!(
        !computed.is_empty(),
        "a 60-run history must produce baselines"
    );

    for _ in 0..2 {
        for c in &computed {
            baseline::upsert(
                &pool,
                t,
                "sys",
                basis::RUNS,
                baseline::ALL_CALLERS,
                c,
                from,
                to,
            )
            .await
            .expect("upsert");
        }
    }

    let (rows,): (i64,) =
        sqlx::query_as("SELECT COUNT(*) FROM ai_system_baselines WHERE tenant_id = $1")
            .bind(t)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(
        rows as usize,
        computed.len(),
        "the sweeper runs every 15 minutes — an INSERT that is not an upsert \
         would grow this table without bound and make 'the' baseline ambiguous"
    );

    let (ready,): (i64,) = sqlx::query_as(
        "SELECT COUNT(*) FROM ai_system_baselines WHERE tenant_id = $1 AND state = 'ready'",
    )
    .bind(t)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert!(ready > 0, "60 runs is past the learning threshold");
}

// ---------------------------------------------------------------------------
// End-to-end sweep
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_learning_system_records_baselines_but_raises_nothing(pool: PgPool) {
    let t = "t-df-learning";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;
    // Deliberately under the threshold, and deliberately erratic: even a
    // wild swing must not produce a finding while learning.
    seed_history(&pool, t, "sys", now, 12, Some(6.0)).await;

    let mut outcome = sweeper::SweepOutcome::default();
    let found = sweeper::sweep_system(&pool, &candidate(t, "sys"), now, &mut outcome).await;

    assert!(
        found.is_empty(),
        "under {MIN_RUNS_FOR_BASELINE} runs the honest answer is 'not enough \
         history yet' — an operator paged by a baseline built from twelve runs \
         does not give the feature a second chance"
    );
    assert_eq!(outcome.learning, 1);
    assert!(
        outcome.baselines_written > 0,
        "the UI needs 'ready in ~N runs'"
    );

    let (events,): (i64,) =
        sqlx::query_as("SELECT COUNT(*) FROM ai_system_drift_events WHERE tenant_id = $1")
            .bind(t)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(events, 0);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_steady_system_produces_no_findings(pool: PgPool) {
    let t = "t-df-steady";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;
    seed_history(&pool, t, "sys", now, 120, None).await;

    let mut outcome = sweeper::SweepOutcome::default();
    let found = sweeper::sweep_system(&pool, &candidate(t, "sys"), now, &mut outcome).await;

    assert!(
        found.is_empty(),
        "a system doing what it has always done must stay silent: \
         if v1 pages people about nothing, there is no v2"
    );
}

/// The regression: with the baseline computed
/// over a window ending now, the runs being judged are part of "normal" and
/// a large minority of bad runs dilutes itself below the threshold.
#[sqlx::test(migrations = "./tests/migrations")]
async fn runs_being_judged_are_not_part_of_their_own_baseline(pool: PgPool) {
    let t = "t-df-own-window";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;

    // 60 older runs, all the normal shape; 100 in the last 24h, 40% a new shape.
    for i in 0..60 {
        let id = format!("old-{i}");
        let at = now - Duration::days(3) - Duration::minutes(i * 30);
        seed_run(&pool, t, &id, "sys", at, 6, 0.01).await;
    }
    for i in 0..100 {
        let id = format!("new-{i}");
        let at = now - Duration::minutes(i * 10 + 5);
        seed_run(&pool, t, &id, "sys", at, 6, 0.01).await;
        if i < 40 {
            sqlx::query(
                "UPDATE ai_system_runs SET tool_set_signature = 'search|exfil' WHERE id = $1",
            )
            .bind(&id)
            .execute(&pool)
            .await
            .unwrap();
        }
    }

    let mut outcome = sweeper::SweepOutcome::default();
    let found = sweeper::sweep_system(&pool, &candidate(t, "sys"), now, &mut outcome).await;
    assert!(
        found
            .iter()
            .any(|f| f.detection.metric_key == metric::TOOL_MIX),
        "40% of today's runs using a new tool set must register against a history \
         that never used it; found {:?}",
        found
            .iter()
            .map(|f| &f.detection.metric_key)
            .collect::<Vec<_>>()
    );

    let (dist, n): (serde_json::Value, i32) = sqlx::query_as(
        "SELECT distribution, sample_count FROM ai_system_baselines \
          WHERE tenant_id = $1 AND metric_key = $2 AND bucket = 'all' AND basis = 'runs'",
    )
    .bind(t)
    .bind(metric::TOOL_MIX)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(
        n, 60,
        "the baseline holds the history, not the runs judged against it"
    );
    assert!(dist.get("search|exfil").is_none(), "{dist}");
}

/// Runs in the observation window are not history: they start counting towards
/// readiness only once they have aged out of it.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_system_with_only_todays_runs_is_still_learning(pool: PgPool) {
    let t = "t-df-day-one";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;
    for i in 0..120 {
        let id = format!("today-{i}");
        seed_run(
            &pool,
            t,
            &id,
            "sys",
            now - Duration::minutes(i * 5 + 1),
            6,
            0.01,
        )
        .await;
    }
    let mut outcome = sweeper::SweepOutcome::default();
    let found = sweeper::sweep_system(&pool, &candidate(t, "sys"), now, &mut outcome).await;
    assert!(
        found.is_empty(),
        "no history means nothing to compare against"
    );
    assert_eq!(outcome.learning, 1);
}

async fn set_tools(pool: &PgPool, id: &str, tools: &[&str]) {
    sqlx::query("UPDATE ai_system_runs SET tools_used = $2 WHERE id = $1")
        .bind(id)
        .bind(serde_json::json!(tools))
        .execute(pool)
        .await
        .unwrap();
}

/// The case: one exfiltration run in 73 moved no statistic.
#[sqlx::test(migrations = "./tests/migrations")]
async fn one_run_with_a_never_seen_tool_is_recorded_and_names_it(pool: PgPool) {
    let t = "t-df-new-tool";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;
    for i in 0..60 {
        let id = format!("old-{i}");
        seed_run(
            &pool,
            t,
            &id,
            "sys",
            now - Duration::days(3) - Duration::minutes(i),
            6,
            0.01,
        )
        .await;
        set_tools(&pool, &id, &["crm_get", "crm_note"]).await;
    }
    for i in 0..30 {
        let id = format!("ok-{i}");
        seed_run(
            &pool,
            t,
            &id,
            "sys",
            now - Duration::minutes(i + 10),
            6,
            0.01,
        )
        .await;
        set_tools(&pool, &id, &["crm_get", "crm_note"]).await;
    }
    seed_run(&pool, t, "bad", "sys", now - Duration::minutes(5), 6, 0.01).await;
    set_tools(&pool, "bad", &["crm_get", "http_post_external"]).await;

    let mut outcome = sweeper::SweepOutcome::default();
    let found = sweeper::sweep_system(&pool, &candidate(t, "sys"), now, &mut outcome).await;
    let shape = found
        .iter()
        .find(|f| f.detection.metric_key == "shape:new_tool:http_post_external")
        .expect("one run with a new tool is enough");
    assert_eq!(shape.detection.severity, "high", "nothing explains it");
    for f in &found {
        sweeper::record(&pool, &candidate(t, "sys"), f, now, &mut outcome).await;
    }

    let (cls, sev, evidence): (String, String, serde_json::Value) = sqlx::query_as(
        "SELECT drift_class, severity, cause_evidence FROM ai_system_drift_events \
          WHERE tenant_id = $1 AND metric_key = 'shape:new_tool:http_post_external'",
    )
    .bind(t)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(
        (cls.as_str(), sev.as_str()),
        (drift_class::BEHAVIOURAL, "high")
    );
    assert_eq!(evidence["tools"], serde_json::json!(["http_post_external"]));
    assert_eq!(evidence["run_ids"], serde_json::json!(["bad"]));
    let (title, message): (String, String) = sqlx::query_as(
        "SELECT title, message FROM system_alerts WHERE tenant_id = $1 \
          AND error_code LIKE '%shape:new_tool%'",
    )
    .bind(t)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert!(title.contains("new tool"), "{title}");
    assert!(message.starts_with("A run used a tool"), "{message}");
    assert!(
        message.contains("http_post_external") && message.contains("bad"),
        "{message}"
    );

    // Still open: more runs on the next tick are the same fact, one finding.
    seed_run(
        &pool,
        t,
        "bad-2",
        "sys",
        now - Duration::minutes(1),
        6,
        0.01,
    )
    .await;
    set_tools(&pool, "bad-2", &["http_post_external"]).await;
    let mut outcome = sweeper::SweepOutcome::default();
    let again = sweeper::sweep_system(&pool, &candidate(t, "sys"), now, &mut outcome).await;
    for f in &again {
        sweeper::record(&pool, &candidate(t, "sys"), f, now, &mut outcome).await;
    }
    let (events,): (i64,) = sqlx::query_as(
        "SELECT COUNT(*) FROM ai_system_drift_events WHERE tenant_id = $1 \
          AND metric_key LIKE 'shape:%'",
    )
    .bind(t)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(events, 1);

    // It neither sets nor reads the behavioural cooldown: a statistical finding
    // recorded now is not silenced by it.
    let mut stat = again
        .iter()
        .find(|f| f.detection.metric_key.starts_with("shape:"))
        .expect("the shape is still detected")
        .clone();
    stat.detection.metric_key = metric::ACTION_COUNT.to_string();
    stat.headline = None;
    let mut outcome = sweeper::SweepOutcome::default();
    sweeper::record(&pool, &candidate(t, "sys"), &stat, now, &mut outcome).await;
    assert_eq!(outcome.suppressed, 0);
    assert_eq!(outcome.alerted, 1);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn runs_from_before_tool_names_were_recorded_make_nothing_look_new(pool: PgPool) {
    let t = "t-df-upgrade-day";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;
    for i in 0..60 {
        let id = format!("old-{i}");
        seed_run(
            &pool,
            t,
            &id,
            "sys",
            now - Duration::days(3) - Duration::minutes(i),
            6,
            0.01,
        )
        .await;
    }
    seed_run(
        &pool,
        t,
        "after",
        "sys",
        now - Duration::minutes(5),
        6,
        0.01,
    )
    .await;
    set_tools(&pool, "after", &["crm_get"]).await;
    let mut outcome = sweeper::SweepOutcome::default();
    let found = sweeper::sweep_system(&pool, &candidate(t, "sys"), now, &mut outcome).await;
    assert!(
        !found
            .iter()
            .any(|f| f.detection.metric_key.starts_with("shape:")),
        "upgrade day must not alert on every tool"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_sustained_step_change_is_recorded_with_its_cause_slot_filled(pool: PgPool) {
    let t = "t-df-step";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;
    seed_history(&pool, t, "sys", now, 160, Some(5.0)).await;

    let mut outcome = sweeper::SweepOutcome::default();
    let found = sweeper::sweep_system(&pool, &candidate(t, "sys"), now, &mut outcome).await;
    assert!(
        !found.is_empty(),
        "a sustained 5x step-count change is drift"
    );

    for finding in &found {
        sweeper::record(&pool, &candidate(t, "sys"), finding, now, &mut outcome).await;
    }

    let rows: Vec<(String, String, String, String)> = sqlx::query_as(
        "SELECT drift_class, metric_key, attributed_cause, status \
           FROM ai_system_drift_events WHERE tenant_id = $1",
    )
    .bind(t)
    .fetch_all(&pool)
    .await
    .unwrap();

    assert!(
        !rows.is_empty(),
        "the finding must be persisted, not just returned"
    );
    for (class, metric_key, cause, status) in &rows {
        assert!(
            drift_class::ALL.contains(&class.as_str()),
            "unknown drift class {class} would fail the control-plane validator"
        );
        assert!(!metric_key.is_empty());
        assert_eq!(status, drift_status::ACTIVE);
        // With no model/tool/config change seeded there is nothing to blame,
        // and the honest answer is to say so rather than invent a rung.
        assert!(
            !cause.is_empty(),
            "every event carries an attributed_cause, even if it is 'unexplained'"
        );
    }
    assert!(
        rows.iter().any(|(_, m, _, _)| m == metric::ACTION_COUNT),
        "the injected change was in action_count"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_second_finding_in_the_same_class_is_suppressed(pool: PgPool) {
    let t = "t-df-cooldown";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;
    seed_history(&pool, t, "sys", now, 160, Some(5.0)).await;

    let mut outcome = sweeper::SweepOutcome::default();
    let found = sweeper::sweep_system(&pool, &candidate(t, "sys"), now, &mut outcome).await;
    let finding = found.first().expect("a finding");
    let detection = &finding.detection;

    for _ in 0..3 {
        sweeper::record(&pool, &candidate(t, "sys"), finding, now, &mut outcome).await;
    }

    let (n,): (i64,) = sqlx::query_as(
        "SELECT COUNT(*) FROM ai_system_drift_events WHERE tenant_id = $1 AND drift_class = $2",
    )
    .bind(t)
    .bind(detection.drift_class)
    .fetch_one(&pool)
    .await
    .unwrap();

    assert_eq!(
        n, 1,
        "the sweeper re-detects the same standing change every 15 minutes; \
         without the cooldown one drift becomes 96 alerts a day"
    );
    assert!(outcome.suppressed >= 2);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_drift_event_links_to_a_persisted_alert(pool: PgPool) {
    // `system_alerts.category` carries a CHECK. If the constraint and
    // the categories the sweeper writes ever disagree, the INSERT fails and
    // the finding lands with no alert behind it — the same class of bug as
    // a liveness category the constraint does not know.
    let t = "t-df-alert";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;
    seed_history(&pool, t, "sys", now, 160, Some(5.0)).await;

    let mut outcome = sweeper::SweepOutcome::default();
    let found = sweeper::sweep_system(&pool, &candidate(t, "sys"), now, &mut outcome).await;
    let finding = found.first().expect("a finding");
    sweeper::record(&pool, &candidate(t, "sys"), finding, now, &mut outcome).await;

    let linked: Option<(String, String)> = sqlx::query_as(
        "SELECT a.category, a.severity \
           FROM ai_system_drift_events d \
           JOIN system_alerts a ON a.id = d.system_alert_id \
          WHERE d.tenant_id = $1",
    )
    .bind(t)
    .fetch_optional(&pool)
    .await
    .unwrap();

    let (category, _severity) = linked.expect(
        "the drift event must reference a real alert row — a CHECK-rejected \
         INSERT here would leave findings invisible to the alert console",
    );
    assert!(
        category.starts_with("ai_system_"),
        "drift alerts belong to the AI System half of the taxonomy, got {category}"
    );
    assert_eq!(outcome.alerted, 1);
}

// ---------------------------------------------------------------------------
// The whole loop
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_full_sweep_walks_every_candidate(pool: PgPool) {
    let t = "t-df-sweep";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys-quiet", "agent").await;
    seed_system(&pool, t, "sys-moved", "agent").await;
    seed_history(&pool, t, "sys-quiet", now, 120, None).await;
    seed_history(&pool, t, "sys-moved", now, 160, Some(5.0)).await;

    let outcome = sweeper::sweep(&pool).await;

    assert!(outcome.checked >= 2, "both systems must be visited");
    assert!(outcome.baselines_written > 0);

    let moved: Vec<(String,)> =
        sqlx::query_as("SELECT ai_system_id FROM ai_system_drift_events WHERE tenant_id = $1")
            .bind(t)
            .fetch_all(&pool)
            .await
            .unwrap();

    assert!(
        moved.iter().all(|(id,)| id == "sys-moved"),
        "only the system whose behaviour actually changed may produce findings"
    );
    assert!(!moved.is_empty(), "the changed system must produce one");
}

// ---------------------------------------------------------------------------
// The agent_release rung, end to end through the sweeper
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_step_change_that_arrives_with_a_new_release_is_attributed_to_it(pool: PgPool) {
    let t = "t-df-release";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;
    // Dense enough (~30 min apart) that the detector's declared change
    // point, a couple of runs after the true change, is inside the cause
    // window — as it is for any system with real traffic.
    seed_history(&pool, t, "sys", now, 640, Some(5.0)).await;
    sqlx::query(
        "INSERT INTO agent_identities (id, tenant_id, name, source, enabled, status, default_deny) \
         VALUES ('agent-df', $1, 'agent-df', 'manual', true, 'active', false)",
    )
    .bind(t)
    .execute(&pool)
    .await
    .unwrap();
    // The runs whose behaviour changed are exactly the runs of the new
    // release: the last quarter of the history (see `seed_history`).
    sqlx::query(
        "UPDATE ai_system_runs r SET agent_id = 'agent-df', \
                agent_impl_name = 'screening-agent', agent_impl_trust = 'declared', \
                agent_impl_version = CASE WHEN (SELECT COUNT(*) FROM ai_system_runs o \
                     WHERE o.tenant_id = r.tenant_id AND o.started_at < r.started_at) >= 480 \
                     THEN '1.5.0' ELSE '1.4.2' END \
          WHERE tenant_id = $1",
    )
    .bind(t)
    .execute(&pool)
    .await
    .unwrap();

    let mut outcome = sweeper::SweepOutcome::default();
    let found = sweeper::sweep_system(&pool, &candidate(t, "sys"), now, &mut outcome).await;
    assert!(
        !found.is_empty(),
        "a sustained 5x step-count change is drift"
    );
    for finding in &found {
        sweeper::record(&pool, &candidate(t, "sys"), finding, now, &mut outcome).await;
    }
    let rows: Vec<(String, String, Option<serde_json::Value>)> = sqlx::query_as(
        "SELECT metric_key, attributed_cause, cause_evidence \
           FROM ai_system_drift_events WHERE tenant_id = $1",
    )
    .bind(t)
    .fetch_all(&pool)
    .await
    .unwrap();
    let (_, cause, evidence) = rows
        .iter()
        .find(|(m, _, _)| m == metric::ACTION_COUNT)
        .expect("the injected change was in action_count");
    assert_eq!(
        cause, "agent_release",
        "the new release explains the change"
    );
    let evidence = evidence.as_ref().expect("evidence");
    assert_eq!(evidence["from"]["version"], "1.4.2");
    assert_eq!(evidence["to"]["version"], "1.5.0");
    assert_eq!(evidence["version_change"], true);
}

// ---------------------------------------------------------------------------
// Baselines per outcome bucket, readiness on success runs, mixes
// ---------------------------------------------------------------------------

/// `(terminal_state, outcome_id, outcome_conformance, counts_toward_baseline)`.
type Judgement<'a> = (&'a str, Option<&'a str>, &'a str, Option<bool>);

/// One run with an explicit outcome judgement.
async fn seed_judged_run(
    pool: &PgPool,
    tenant: &str,
    id: &str,
    system_id: &str,
    at: DateTime<Utc>,
    actions: i32,
    (terminal, outcome, conformance, counts): Judgement<'_>,
) {
    sqlx::query(
        "INSERT INTO ai_system_runs \
         (id, tenant_id, root_task_id, ai_system_id, started_at, ended_at, duration_ms, \
          terminal_state, finalized_by, action_count, tool_call_count, max_delegation_depth, \
          total_cost_usd, tool_error_count, input_bytes, first_prompt_tokens, \
          tool_set_signature, client_label, chain_integrity, reopened_count, is_replay, \
          outcome_id, outcome_conformance, outcome_counts_toward_baseline) \
         VALUES ($1, $2, $1, $3, $4, $4, 1000, $6, 'idle', $5, $5, 1, \
                 0.004, 0, 2048, 400, 'search|fetch', 'sdk', 'intact', 0, false, $7, $8, $9)",
    )
    .bind(id)
    .bind(tenant)
    .bind(system_id)
    .bind(at)
    .bind(actions)
    .bind(terminal)
    .bind(outcome)
    .bind(conformance)
    .bind(counts)
    .execute(pool)
    .await
    .expect("seed_judged_run");
}

const LONG_VARIANT: &str = "application_processed.underwriter_decision_applied";

#[sqlx::test(migrations = "./tests/migrations")]
async fn outcome_buckets_are_persisted_with_their_full_id(pool: PgPool) {
    // A bucket is a full outcome id — longer than the old VARCHAR(32).
    let t = "t-df-buckets";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;
    for i in 0..60 {
        let at = now - Duration::hours(200 - i);
        seed_judged_run(
            &pool,
            t,
            &format!("a{i}"),
            "sys",
            at,
            10,
            (
                "completed",
                Some("application_processed.loan_approved"),
                "declared",
                Some(true),
            ),
        )
        .await;
        seed_judged_run(
            &pool,
            t,
            &format!("b{i}"),
            "sys",
            at,
            1,
            ("completed", Some(LONG_VARIANT), "declared", Some(true)),
        )
        .await;
        seed_judged_run(
            &pool,
            t,
            &format!("p{i}"),
            "sys",
            at,
            1,
            ("completed", Some("nothing_to_do"), "declared", Some(false)),
        )
        .await;
    }
    let mut outcome = sweeper::SweepOutcome::default();
    sweeper::sweep_system(&pool, &candidate(t, "sys"), now, &mut outcome).await;

    let rows: Vec<(String, Option<f64>, String, i32)> = sqlx::query_as(
        "SELECT bucket, median, state, sample_count FROM ai_system_baselines \
          WHERE tenant_id = $1 AND metric_key = 'action_count' AND basis = 'runs' ORDER BY bucket",
    )
    .bind(t)
    .fetch_all(&pool)
    .await
    .unwrap();
    let by: std::collections::HashMap<&str, (Option<f64>, &str, i32)> = rows
        .iter()
        .map(|(b, m, s, n)| (b.as_str(), (*m, s.as_str(), *n)))
        .collect();
    assert_eq!(by[LONG_VARIANT], (Some(1.0), "ready", 60), "{rows:?}");
    assert_eq!(by["application_processed.loan_approved"].0, Some(10.0));
    assert_eq!(
        by["nothing_to_do"].1, "ready",
        "no-work has its own baseline"
    );
    assert_eq!(by["all"].2, 120, "all = every run but no-work: {rows:?}");

    let (mix,): (Option<serde_json::Value>,) = sqlx::query_as(
        "SELECT distribution FROM ai_system_baselines WHERE tenant_id = $1 \
           AND metric_key = $2 AND bucket = 'all'",
    )
    .bind(t)
    .bind(metric::OUTCOME_MIX)
    .fetch_one(&pool)
    .await
    .unwrap();
    let mix = mix.expect("outcome_mix distribution");
    assert!(
        (mix[LONG_VARIANT].as_f64().unwrap() - 0.5).abs() < 1e-9,
        "{mix}"
    );
    assert!(
        mix.get("nothing_to_do").is_none(),
        "only success runs: {mix}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn readiness_counts_success_runs_only(pool: PgPool) {
    // 120 runs that completed but never declared an outcome: no evidence
    // about the work, so the system stays learning and nothing alerts.
    let t = "t-df-undeclared";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;
    for i in 0..120 {
        let steps = if i >= 90 { 30 } else { 6 };
        seed_judged_run(
            &pool,
            t,
            &format!("u{i}"),
            "sys",
            now - Duration::minutes((120 - i) * 120),
            steps,
            ("completed", None, "unreported", None),
        )
        .await;
    }
    let mut outcome = sweeper::SweepOutcome::default();
    let found = sweeper::sweep_system(&pool, &candidate(t, "sys"), now, &mut outcome).await;
    assert!(found.is_empty());
    assert_eq!(outcome.learning, 1);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_mix_shift_with_unchanged_variant_shapes_raises_only_outcome_mix(pool: PgPool) {
    // Approvals take 10 actions, rejections 4 — always. The history is 90%
    // approvals; the last day is 90% rejections. The blended action count
    // moves, but no variant behaves differently: the finding is the mix.
    let t = "t-df-mix";
    let now = crate::common::db_now();
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent").await;
    let total = 200;
    for i in 0..total {
        let recent = i >= total - 40;
        let approve = if recent { i % 10 == 0 } else { i % 10 != 0 };
        let age_hours = (13 * 24) as f64 * (1.0 - i as f64 / total as f64);
        let at = now - Duration::minutes((age_hours * 60.0) as i64) - Duration::minutes(5);
        let (oid, steps) = if approve {
            ("app.loan_approved", 10)
        } else {
            ("app.loan_rejected", 4)
        };
        seed_judged_run(
            &pool,
            t,
            &format!("m{i}"),
            "sys",
            at,
            steps,
            ("completed", Some(oid), "declared", Some(true)),
        )
        .await;
    }
    let mut outcome = sweeper::SweepOutcome::default();
    let found = sweeper::sweep_system(&pool, &candidate(t, "sys"), now, &mut outcome).await;
    let f = found.first().expect("the mix moved");
    assert_eq!(f.bucket, "all");
    for finding in &found {
        assert_ne!(
            finding.bucket, "app.loan_approved",
            "the approvals did not change"
        );
        assert_ne!(finding.bucket, "app.loan_rejected");
    }
    let correlated = f.attribution.evidence["correlated_metrics"].to_string();
    assert!(
        f.detection.metric_key == metric::OUTCOME_MIX || correlated.contains("outcome_mix"),
        "outcome_mix names the shift: {} / {correlated}",
        f.detection.metric_key
    );
}
