// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Replay execution against a real Postgres.
//!
//! The diff and the summary arithmetic are pure and covered by unit tests.
//! What can only be proven here is the thing the promotion gate rests on:
//! that **unbinding a resource turns a previously-successful action into a
//! denial**, and that the run this produces is a failure rather than a pass.
//!
//! The failure being guarded against is quiet in the worst way. A replay
//! that evaluates nothing — because the actions aged out, because the suite is
//! empty, because the case was skipped — must never report a clean sheet. A
//! gate satisfied by an empty suite is worse than no gate: it produces a
//! green tick with no evidence behind it, which is exactly what somebody
//! shows an auditor.

use sqlx::PgPool;

use brutor_gateway_core::services::replay::{
    diff::{Trajectory, verdict},
    executor::{self, ActionVerdict},
    runner, status,
};

use super::common;

// ---------------------------------------------------------------------------
// Seeding
// ---------------------------------------------------------------------------

async fn seed_system(pool: &PgPool, tenant: &str, id: &str, autonomy: Option<&str>) {
    sqlx::query(
        "INSERT INTO resource_groups (id, tenant_id, hierarchy_level, inherit_from_parent, \
         name, is_active, is_system, group_type, system_kind, autonomy_level) \
         VALUES ($1, $2, 0, true, $1, true, false, 'AI_SYSTEM', 'agent', $3) \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant)
    .bind(autonomy)
    .execute(pool)
    .await
    .expect("seed_system");
}

async fn seed_model(pool: &PgPool, tenant: &str, id: &str, name: &str) {
    sqlx::query(
        "INSERT INTO llm_providers (id, tenant_id, key, display_name, enabled, \
         is_system, import_enabled) \
         VALUES ('prov-1', $1, 'openai', 'OpenAI', true, false, false) \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(tenant)
    .execute(pool)
    .await
    .expect("seed_provider");

    sqlx::query(
        "INSERT INTO llm_model \
         (id, tenant_id, name, model_name, provider_id, mode, enabled, \
          api_url_auto_discovered, thinking_enabled) \
         VALUES ($1, $2, $3, $3, 'prov-1', 'chat', true, false, false) \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant)
    .bind(name)
    .execute(pool)
    .await
    .expect("seed_model");
}

async fn bind_model(pool: &PgPool, group: &str, model: &str) {
    sqlx::query(
        "INSERT INTO resource_group_llm_models \
         (id, resource_group_id, llm_model_id, created_at) \
         VALUES ($1, $2, $3, NOW()) ON CONFLICT DO NOTHING",
    )
    .bind(format!("{group}-{model}"))
    .bind(group)
    .bind(model)
    .execute(pool)
    .await
    .expect("bind_model");
}

#[allow(clippy::too_many_arguments)]
async fn seed_action(
    pool: &PgPool,
    tenant: &str,
    id: &str,
    root: &str,
    proxy_type: &str,
    model: Option<&str>,
    success: bool,
) {
    sqlx::query(
        // `end_user_session_id`, `http_method` and `endpoint` are NOT NULL
        // without defaults; a replay only reads the governance columns but
        // the row still has to be well-formed.
        "INSERT INTO proxy_logs \
         (id, tenant_id, timestamp, proxy_type, request_id, root_task_id, \
          llm_model_requested, success, guardrail_blocked, argument_policy_denied, \
          end_user_session_id, http_method, endpoint) \
         VALUES ($1, $2, NOW(), $3, $1, $4, $5, $6, false, false, \
                 'sess-1', 'POST', '/v1/proxy/llm/chat/completions')",
    )
    .bind(id)
    .bind(tenant)
    .bind(proxy_type)
    .bind(root)
    .bind(model)
    .bind(success)
    .execute(pool)
    .await
    .expect("seed_action");
}

async fn seed_suite(pool: &PgPool, tenant: &str, id: &str, system: &str, replayable: bool) {
    sqlx::query(
        "INSERT INTO ai_system_replay_suites \
         (id, tenant_id, ai_system_id, name, pass_threshold, replayable, enabled) \
         VALUES ($1, $2, $3, $1, 0.9, $4, true) ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant)
    .bind(system)
    .bind(replayable)
    .execute(pool)
    .await
    .expect("seed_suite");
}

#[allow(clippy::too_many_arguments)]
async fn seed_case(
    pool: &PgPool,
    tenant: &str,
    id: &str,
    suite: &str,
    root: Option<&str>,
    critical: bool,
    side_effect: &str,
) {
    let trajectory = serde_json::json!({
        "terminal_state": "completed",
        "action_count": 2,
        "tool_set_signature": "sig",
        "policy_denial_count": 0,
        "approval_request_count": 0,
        "guardrail_block_count": 0,
    });
    sqlx::query(
        "INSERT INTO ai_system_replay_cases \
         (id, tenant_id, suite_id, source_root_task_id, recorded_request, \
          recorded_trajectory, criticality, side_effect_policy, enabled) \
         VALUES ($1, $2, $3, $4, '{}'::jsonb, $5, $6, $7, true)",
    )
    .bind(id)
    .bind(tenant)
    .bind(suite)
    .bind(root)
    .bind(trajectory)
    .bind(if critical { "critical" } else { "normal" })
    .bind(side_effect)
    .execute(pool)
    .await
    .expect("seed_case");
}

fn recorded() -> Trajectory {
    Trajectory {
        terminal_state: Some("completed".into()),
        action_count: Some(2),
        tool_set_signature: Some("sig".into()),
        total_cost_usd: None,
        guardrail_block_count: Some(0),
        policy_denial_count: Some(0),
        approval_request_count: Some(0),
    }
}

async fn run_row(pool: &PgPool, run_id: &str) -> (String, Option<f64>, i32, Option<String>) {
    sqlx::query_as(
        "SELECT status, pass_rate, critical_outcome_changed, error_message \
           FROM ai_system_replay_runs WHERE id = $1",
    )
    .bind(run_id)
    .fetch_one(pool)
    .await
    .unwrap()
}

// ---------------------------------------------------------------------------
// Hop-level verdicts
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_bound_model_still_passes(pool: PgPool) {
    let t = "t-rp-bound";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", None).await;
    seed_model(&pool, t, "m1", "gpt-4o").await;
    bind_model(&pool, "sys", "m1").await;
    seed_action(&pool, t, "h1", "r1", "llm", Some("gpt-4o"), true).await;

    let actions = executor::load_actions(&pool, t, "r1").await.unwrap();
    assert_eq!(actions.len(), 1);
    assert_eq!(
        executor::evaluate_action(&pool, t, "sys", &actions[0])
            .await
            .unwrap(),
        ActionVerdict::Allowed
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn unbinding_the_model_denies_a_action_that_used_to_work(pool: PgPool) {
    // The property the promotion gate exists for: a config change that
    // removes a resource turns working traffic into denied traffic, and
    // replay is what surfaces that *before* it ships.
    let t = "t-rp-unbound";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", None).await;
    seed_model(&pool, t, "m1", "gpt-4o").await;
    // Deliberately not bound.
    seed_action(&pool, t, "h1", "r1", "llm", Some("gpt-4o"), true).await;

    let actions = executor::load_actions(&pool, t, "r1").await.unwrap();
    let verdict = executor::evaluate_action(&pool, t, "sys", &actions[0])
        .await
        .unwrap();
    match verdict {
        ActionVerdict::Denied(reason) => {
            assert!(reason.contains("no longer bound"), "reason was: {reason}")
        }
        other => panic!("expected a denial, got {other:?}"),
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_suspended_system_denies_every_action(pool: PgPool) {
    let t = "t-rp-susp";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", Some("suspended")).await;
    seed_model(&pool, t, "m1", "gpt-4o").await;
    bind_model(&pool, "sys", "m1").await;
    seed_action(&pool, t, "h1", "r1", "llm", Some("gpt-4o"), true).await;

    let actions = executor::load_actions(&pool, t, "r1").await.unwrap();
    assert!(matches!(
        executor::evaluate_action(&pool, t, "sys", &actions[0])
            .await
            .unwrap(),
        ActionVerdict::Denied(_)
    ));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_restricted_system_still_allows_completions(pool: PgPool) {
    // `restricted` means read-only, and a completion is read-only. If this
    // denied, replay would report a catastrophic diff for a change that is
    // actually safe.
    let t = "t-rp-restricted";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", Some("restricted")).await;
    seed_model(&pool, t, "m1", "gpt-4o").await;
    bind_model(&pool, "sys", "m1").await;
    seed_action(&pool, t, "h1", "r1", "llm", Some("gpt-4o"), true).await;

    let actions = executor::load_actions(&pool, t, "r1").await.unwrap();
    assert_eq!(
        executor::evaluate_action(&pool, t, "sys", &actions[0])
            .await
            .unwrap(),
        ActionVerdict::Allowed
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn approval_required_is_reported_as_such_not_as_a_denial(pool: PgPool) {
    // The run would still complete, with a human in the loop. Reporting it
    // as an outcome change would block a promotion that is merely stricter.
    let t = "t-rp-appr";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", Some("approval_required")).await;
    seed_action(&pool, t, "h1", "r1", "mcp", None, true).await;

    let actions = executor::load_actions(&pool, t, "r1").await.unwrap();
    assert!(matches!(
        executor::evaluate_action(&pool, t, "sys", &actions[0])
            .await
            .unwrap(),
        ActionVerdict::ApprovalRequired(_)
    ));
}

// ---------------------------------------------------------------------------
// Case-level verdicts
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unchanged_configuration_matches(pool: PgPool) {
    let t = "t-rp-match";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", None).await;
    seed_model(&pool, t, "m1", "gpt-4o").await;
    bind_model(&pool, "sys", "m1").await;
    seed_action(&pool, t, "h1", "r1", "llm", Some("gpt-4o"), true).await;

    let outcome = executor::replay_case(
        &pool,
        t,
        "sys",
        "c1",
        Some("r1"),
        &recorded(),
        false,
        "mock",
    )
    .await
    .unwrap();
    assert_eq!(outcome.diff.verdict, verdict::MATCHED);
    assert!(outcome.changed_actions.is_empty());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_newly_denied_action_is_an_outcome_change(pool: PgPool) {
    // A action that used to succeed and is now denied means the task would not
    // have completed. That is an outcome change, not a policy nuance.
    let t = "t-rp-outcome";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", None).await;
    seed_model(&pool, t, "m1", "gpt-4o").await;
    seed_action(&pool, t, "h1", "r1", "llm", Some("gpt-4o"), true).await;

    let outcome = executor::replay_case(
        &pool,
        t,
        "sys",
        "c1",
        Some("r1"),
        &recorded(),
        false,
        "mock",
    )
    .await
    .unwrap();
    assert_eq!(outcome.diff.verdict, verdict::OUTCOME_CHANGED);
    assert_eq!(outcome.changed_actions.len(), 1);
    assert_eq!(outcome.changed_actions[0].target, "gpt-4o");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_new_approval_requirement_is_a_policy_change_not_an_outcome_change(pool: PgPool) {
    let t = "t-rp-policy";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", Some("approval_required")).await;
    seed_action(&pool, t, "h1", "r1", "mcp", None, true).await;

    let outcome = executor::replay_case(
        &pool,
        t,
        "sys",
        "c1",
        Some("r1"),
        &recorded(),
        false,
        "mock",
    )
    .await
    .unwrap();
    assert_eq!(outcome.diff.verdict, verdict::POLICY_CHANGED);
    assert!(!outcome.diff.passes());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_case_whose_actions_aged_out_fails_rather_than_passing(pool: PgPool) {
    // A case that cannot be evaluated is not evidence. Silently passing it
    // would let retention hollow out a suite while the gate stayed green.
    let t = "t-rp-gone";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", None).await;

    let outcome = executor::replay_case(
        &pool,
        t,
        "sys",
        "c1",
        Some("r-missing"),
        &recorded(),
        false,
        "mock",
    )
    .await
    .unwrap();
    assert_eq!(outcome.diff.verdict, verdict::FAILED);
    assert!(outcome.diff.reasons[0].contains("retention"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_case_with_no_root_task_fails(pool: PgPool) {
    let t = "t-rp-noroot";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", None).await;

    let outcome = executor::replay_case(&pool, t, "sys", "c1", None, &recorded(), false, "mock")
        .await
        .unwrap();
    assert_eq!(outcome.diff.verdict, verdict::FAILED);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_skip_case_is_skipped_not_passed(pool: PgPool) {
    let t = "t-rp-skip";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", None).await;
    seed_action(&pool, t, "h1", "r1", "mcp", None, true).await;

    let outcome = executor::replay_case(
        &pool,
        t,
        "sys",
        "c1",
        Some("r1"),
        &recorded(),
        false,
        "skip_case",
    )
    .await
    .unwrap();
    assert_eq!(outcome.diff.verdict, verdict::SKIPPED);
    assert!(!outcome.diff.passes());
}

// ---------------------------------------------------------------------------
// Suite runs
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_clean_suite_completes_and_passes(pool: PgPool) {
    let t = "t-rp-suite-ok";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", None).await;
    seed_model(&pool, t, "m1", "gpt-4o").await;
    bind_model(&pool, "sys", "m1").await;
    seed_action(&pool, t, "h1", "r1", "llm", Some("gpt-4o"), true).await;
    seed_suite(&pool, t, "suite-1", "sys", true).await;
    seed_case(&pool, t, "c1", "suite-1", Some("r1"), false, "mock").await;

    let run_id = runner::run_suite(&pool, t, "suite-1", None, "manual", Some("alice"))
        .await
        .expect("run");

    let (state, rate, critical, _) = run_row(&pool, &run_id).await;
    assert_eq!(state, status::COMPLETED);
    assert_eq!(rate, Some(1.0));
    assert_eq!(critical, 0);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn one_broken_critical_case_is_recorded_separately(pool: PgPool) {
    // A 200-case suite at 95% has ten failures; if one of them is the refund
    // path the arithmetic must not wave it through.
    let t = "t-rp-suite-crit";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", None).await;
    seed_model(&pool, t, "m1", "gpt-4o").await;
    // Unbound → the action is denied.
    seed_action(&pool, t, "h1", "r1", "llm", Some("gpt-4o"), true).await;
    seed_suite(&pool, t, "suite-1", "sys", true).await;
    seed_case(&pool, t, "c1", "suite-1", Some("r1"), true, "mock").await;

    let run_id = runner::run_suite(&pool, t, "suite-1", None, "manual", None)
        .await
        .expect("run");

    let (state, rate, critical, _) = run_row(&pool, &run_id).await;
    assert_eq!(state, status::COMPLETED);
    assert_eq!(rate, Some(0.0));
    assert_eq!(critical, 1);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_empty_suite_fails_rather_than_reporting_a_perfect_score(pool: PgPool) {
    // The single most dangerous thing this module could produce: a green
    // tick with no evidence behind it.
    let t = "t-rp-empty";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", None).await;
    seed_suite(&pool, t, "suite-1", "sys", true).await;

    let run_id = runner::run_suite(&pool, t, "suite-1", None, "manual", None)
        .await
        .expect("run");

    let (state, rate, _, error) = run_row(&pool, &run_id).await;
    assert_eq!(state, status::FAILED);
    assert_eq!(rate, None);
    assert!(error.unwrap().contains("no enabled cases"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unreplayable_suite_fails_with_its_reason(pool: PgPool) {
    let t = "t-rp-unreplayable";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", None).await;
    seed_suite(&pool, t, "suite-1", "sys", false).await;
    seed_case(&pool, t, "c1", "suite-1", Some("r1"), false, "mock").await;

    let run_id = runner::run_suite(&pool, t, "suite-1", None, "manual", None)
        .await
        .expect("run");

    let (state, rate, _, error) = run_row(&pool, &run_id).await;
    assert_eq!(state, status::FAILED);
    assert_eq!(rate, None);
    assert!(error.unwrap().contains("not replayable"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_entirely_skipped_suite_has_no_pass_rate(pool: PgPool) {
    let t = "t-rp-allskip";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", None).await;
    seed_action(&pool, t, "h1", "r1", "mcp", None, true).await;
    seed_suite(&pool, t, "suite-1", "sys", true).await;
    seed_case(&pool, t, "c1", "suite-1", Some("r1"), false, "skip_case").await;

    let run_id = runner::run_suite(&pool, t, "suite-1", None, "manual", None)
        .await
        .expect("run");

    let (state, rate, _, _) = run_row(&pool, &run_id).await;
    assert_eq!(state, status::COMPLETED);
    assert_eq!(
        rate, None,
        "a confident 100% derived from cases nobody ran would mislead"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_per_case_results_name_the_action_that_changed(pool: PgPool) {
    // A failing gate that says "3 cases changed" and stops has handed the
    // investigation back to the operator.
    let t = "t-rp-detail";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", None).await;
    seed_model(&pool, t, "m1", "gpt-4o").await;
    seed_action(&pool, t, "h1", "r1", "llm", Some("gpt-4o"), true).await;
    seed_suite(&pool, t, "suite-1", "sys", true).await;
    seed_case(&pool, t, "c1", "suite-1", Some("r1"), false, "mock").await;

    let run_id = runner::run_suite(&pool, t, "suite-1", None, "manual", None)
        .await
        .expect("run");

    let results: (serde_json::Value,) =
        sqlx::query_as("SELECT case_results FROM ai_system_replay_runs WHERE id = $1")
            .bind(&run_id)
            .fetch_one(&pool)
            .await
            .unwrap();

    let first = &results.0[0];
    assert_eq!(first["verdict"], "outcome_changed");
    assert_eq!(first["changed_actions"][0]["target"], "gpt-4o");
    assert!(!first["reasons"].as_array().unwrap().is_empty());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_run_is_visible_while_it_is_still_running(pool: PgPool) {
    // The row is created before the cases are evaluated so a process that
    // dies halfway leaves evidence rather than nothing — and the gate treats
    // anything that is not `completed` as a non-pass.
    let t = "t-rp-visible";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", None).await;
    seed_suite(&pool, t, "suite-1", "sys", true).await;

    let run_id = runner::run_suite(&pool, t, "suite-1", None, "manual", None)
        .await
        .expect("run");
    let (_, _, _, _) = run_row(&pool, &run_id).await;

    let count: (i64,) =
        sqlx::query_as("SELECT COUNT(*) FROM ai_system_replay_runs WHERE tenant_id = $1")
            .bind(t)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(count.0, 1);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_gate_helper_runs_the_bound_suite(pool: PgPool) {
    let t = "t-rp-gate";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", None).await;
    seed_model(&pool, t, "m1", "gpt-4o").await;
    bind_model(&pool, "sys", "m1").await;
    seed_action(&pool, t, "h1", "r1", "llm", Some("gpt-4o"), true).await;
    seed_suite(&pool, t, "suite-1", "sys", true).await;
    seed_case(&pool, t, "c1", "suite-1", Some("r1"), false, "mock").await;
    sqlx::query("UPDATE resource_groups SET replay_suite_id = 'suite-1' WHERE id = 'sys'")
        .execute(&pool)
        .await
        .unwrap();

    let run_id = runner::run_for_system(&pool, t, "sys", Some("ctr-1"))
        .await
        .unwrap();
    assert!(run_id.is_some());

    let trigger: (String,) =
        sqlx::query_as("SELECT triggered_by FROM ai_system_replay_runs WHERE id = $1")
            .bind(run_id.unwrap())
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(trigger.0, "promotion_gate");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_system_with_no_bound_suite_runs_nothing(pool: PgPool) {
    let t = "t-rp-nosuite";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", None).await;
    assert!(
        runner::run_for_system(&pool, t, "sys", None)
            .await
            .unwrap()
            .is_none()
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn replay_dispatches_nothing_upstream(pool: PgPool) {
    // The safety property is structural, not a policy someone could flip:
    // evaluating a case writes no proxy_logs and no runs of its own.
    let t = "t-rp-nodispatch";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", None).await;
    seed_model(&pool, t, "m1", "gpt-4o").await;
    bind_model(&pool, "sys", "m1").await;
    seed_action(&pool, t, "h1", "r1", "mcp", None, true).await;
    seed_suite(&pool, t, "suite-1", "sys", true).await;
    seed_case(&pool, t, "c1", "suite-1", Some("r1"), false, "mock").await;

    let before: (i64,) = sqlx::query_as("SELECT COUNT(*) FROM proxy_logs WHERE tenant_id = $1")
        .bind(t)
        .fetch_one(&pool)
        .await
        .unwrap();

    runner::run_suite(&pool, t, "suite-1", None, "manual", None)
        .await
        .expect("run");

    let after: (i64,) = sqlx::query_as("SELECT COUNT(*) FROM proxy_logs WHERE tenant_id = $1")
        .bind(t)
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(before.0, after.0, "replay must not produce traffic");

    let runs: (i64,) = sqlx::query_as("SELECT COUNT(*) FROM ai_system_runs WHERE tenant_id = $1")
        .bind(t)
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(
        runs.0, 0,
        "replay must not produce runs that could reach a baseline"
    );
}

// ---------------------------------------------------------------------------
// The sweeper must not re-execute a run already in flight
// ---------------------------------------------------------------------------
//
// `runner.rs` reasons about this in its comments and nothing tested it. The
// cost of getting it wrong is not abstract: every case in a suite dispatches
// judge and LLM work, so a duplicated run is billed twice and races two
// writers onto the same result row — and the second writer can turn a run
// that completed into one that failed.
//
// Two independent guards exist, and each gets its own test, because either
// one silently regressing would leave the other masking it in the common
// case.

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_sweeper_executes_a_freshly_requested_run_on_its_next_tick(pool: PgPool) {
    // Guard 1 used to be an age filter that left any `running` row younger
    // than ten minutes alone. The control plane's `request_replay_run`
    // writes exactly such a row and executes nothing itself, so every
    // operator-requested replay sat for ten minutes with the console saying
    // "running". The only executor that inserts-and-runs inline
    // (`run_suite`) now claims before it inserts, so an unclaimed `running`
    // row of any age is work nobody is doing — and the sweep does it.
    let t = "t-rp-fresh";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", None).await;
    seed_suite(&pool, t, "suite-1", "sys", true).await;

    sqlx::query(
        "INSERT INTO ai_system_replay_runs \
         (id, tenant_id, suite_id, ai_system_id, status, triggered_by, started_at) \
         VALUES ('rpr-fresh', $1, 'suite-1', 'sys', 'running', 'manual', now())",
    )
    .bind(t)
    .execute(&pool)
    .await
    .unwrap();

    assert_eq!(
        runner::sweep_pending(&pool, 10).await,
        1,
        "a requested run nobody has claimed is executed on the very next sweep"
    );

    let done: (String, Option<chrono::DateTime<chrono::Utc>>) = sqlx::query_as(
        "SELECT status, finished_at FROM ai_system_replay_runs WHERE id = 'rpr-fresh'",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_ne!(done.0, status::RUNNING, "the run must have been evaluated");
    assert!(done.1.is_some(), "an executed run carries a finish time");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_sweeper_skips_a_stale_run_another_executor_still_holds(pool: PgPool) {
    // Guard 2: the advisory claim. Even past the staleness window — the case
    // the filter is meant to let through — a run whose lock is held must be
    // left to its holder. Simulated by taking the same lock on a dedicated
    // connection, which is exactly what a second replica's executor looks
    // like from this process's point of view.
    use sqlx::Acquire;

    let t = "t-rp-claimed";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", None).await;
    seed_suite(&pool, t, "suite-1", "sys", true).await;

    sqlx::query(
        "INSERT INTO ai_system_replay_runs \
         (id, tenant_id, suite_id, ai_system_id, status, triggered_by, started_at) \
         VALUES ('rpr-stale', $1, 'suite-1', 'sys', 'running', 'manual', \
                 now() - interval '2 hours')",
    )
    .bind(t)
    .execute(&pool)
    .await
    .unwrap();

    // Sanity: without a holder the sweeper WOULD take this one. Asserting it
    // here means the skip below is attributable to the claim rather than to
    // the row being invisible for some unrelated reason.
    let mut holder = pool.acquire().await.unwrap();
    let key = runner::run_lock_key("rpr-stale");
    let (locked,): (bool,) = sqlx::query_as("SELECT pg_try_advisory_lock($1)")
        .bind(key)
        .fetch_one(holder.acquire().await.unwrap())
        .await
        .unwrap();
    assert!(locked, "the test needs to hold the claim itself");

    assert_eq!(
        runner::sweep_pending(&pool, 10).await,
        0,
        "a claimed run belongs to whoever claimed it"
    );

    let still: (String, Option<chrono::DateTime<chrono::Utc>>) = sqlx::query_as(
        "SELECT status, finished_at FROM ai_system_replay_runs WHERE id = 'rpr-stale'",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(still.0, status::RUNNING);
    assert!(still.1.is_none());

    // Release, and the same sweep now takes it — proving the skip was the
    // claim doing its job and not the run being permanently unreachable.
    sqlx::query("SELECT pg_advisory_unlock($1)")
        .bind(key)
        .execute(holder.acquire().await.unwrap())
        .await
        .unwrap();
    drop(holder);

    runner::sweep_pending(&pool, 10).await;
    let after: (String,) =
        sqlx::query_as("SELECT status FROM ai_system_replay_runs WHERE id = 'rpr-stale'")
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_ne!(
        after.0,
        status::RUNNING,
        "once the claim is free the run is executed exactly once, not never"
    );
}

/// The cases could not be read. Was: the error became "no cases" and
/// the run was finished as `failed: suite has no enabled cases`, a verdict
/// about the suite that the suite did not earn. Now the run errors.
#[sqlx::test(migrations = "./tests/migrations")]
async fn unreadable_cases_are_an_error_not_an_empty_suite(pool: PgPool) {
    let t = "t-rp-dberr";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", None).await;
    seed_suite(&pool, t, "suite-1", "sys", true).await;
    seed_case(&pool, t, "c1", "suite-1", Some("r1"), false, "mock").await;
    sqlx::query("ALTER TABLE ai_system_replay_cases RENAME TO rpc_unreadable")
        .execute(&pool)
        .await
        .unwrap();

    let out = runner::run_suite(&pool, t, "suite-1", None, "manual", None).await;
    assert!(out.is_err(), "got {out:?}");
    let reasons: Vec<(Option<String>,)> =
        sqlx::query_as("SELECT error_message FROM ai_system_replay_runs WHERE tenant_id = $1")
            .bind(t)
            .fetch_all(&pool)
            .await
            .unwrap();
    assert!(
        !reasons
            .iter()
            .any(|(r,)| r.as_deref() == Some("suite has no enabled cases")),
        "{reasons:?}"
    );
}
