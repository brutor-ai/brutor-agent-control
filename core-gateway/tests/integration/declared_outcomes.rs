// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Declared outcomes, against a real Postgres.
//!
//! The judgement is stored on the run at finalize (outcome id, conformance,
//! counts-toward-baseline, contract version, error cause), from the outcome
//! the client reported on a durable step row — so every close path (the
//! run-end marker, `/end`, the sweeper backstop) judges the same fact. A
//! delegate's outcome is judged against the DELEGATE's contract and stored on
//! its segment; the caller gets the checked pair back on the A2A reply.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use chrono::Duration;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::middleware;
use brutor_gateway_core::routes;
use brutor_gateway_core::services::SharedState;
use brutor_gateway_core::services::run_ledger::{
    finalize_run, finalized_by, sweep_idle_runs, terminal,
};

use super::common;

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

/// One `proxy_logs` row, with every column the judgement reads.
#[derive(Clone)]
struct Step<'a> {
    id: &'a str,
    root: &'a str,
    proxy_type: &'a str,
    subtype: Option<&'a str>,
    seconds_ago: i64,
    success: bool,
    status: Option<i32>,
    group: Option<&'a str>,
    endpoint: &'a str,
    outcome_id: Option<&'a str>,
    run_end: Option<&'a str>,
    guardrail_blocked: bool,
    server_id: Option<&'a str>,
    current: Option<&'a str>,
    parent: Option<&'a str>,
    task_state: Option<&'a str>,
}

impl<'a> Step<'a> {
    fn new(id: &'a str, root: &'a str, proxy_type: &'a str, seconds_ago: i64) -> Self {
        Self {
            id,
            root,
            proxy_type,
            subtype: None,
            seconds_ago,
            success: true,
            status: Some(200),
            group: None,
            endpoint: "/x",
            outcome_id: None,
            run_end: None,
            guardrail_blocked: false,
            server_id: None,
            current: None,
            parent: None,
            task_state: None,
        }
    }
}

async fn insert(pool: &PgPool, tenant: &str, s: Step<'_>) {
    let ts = common::db_now() - Duration::seconds(s.seconds_ago);
    sqlx::query(
        "INSERT INTO proxy_logs (
            id, tenant_id, timestamp, proxy_type, proxy_subtype, end_user_session_id,
            http_method, endpoint, success, http_status_code, root_task_id,
            delegation_depth, resource_group_id, outcome_id, run_end_state,
            guardrail_blocked, argument_policy_denied, pii_detected, trace_continuity,
            server_id, current_task_id, parent_task_id, a2a_task_state,
            request_size_bytes, response_size_bytes, is_auth_event
         ) VALUES (
            $1, $2, $3, $4, $5, 'sess', 'POST', $6, $7, $8, $9, 0, $10, $11, $12,
            $13, false, false, 'minted', $14, $15, $16, $17, 1, 1, false
         )",
    )
    .bind(s.id)
    .bind(tenant)
    .bind(ts)
    .bind(s.proxy_type)
    .bind(s.subtype)
    .bind(s.endpoint)
    .bind(s.success)
    .bind(s.status)
    .bind(s.root)
    .bind(s.group)
    .bind(s.outcome_id)
    .bind(s.run_end)
    .bind(s.guardrail_blocked)
    .bind(s.server_id)
    .bind(s.current)
    .bind(s.parent)
    .bind(s.task_state)
    .execute(pool)
    .await
    .expect("insert step");
}

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
    .expect("seed system");
}

/// A contract whose closure carries `outcomes` (or none when `None`).
async fn seed_contract(
    pool: &PgPool,
    tenant: &str,
    id: &str,
    system: &str,
    version: i32,
    status: &str,
    outcomes: Option<Value>,
) {
    let closure = match outcomes {
        Some(o) => json!({"outcomes": o}),
        None => json!({}),
    };
    sqlx::query(
        "INSERT INTO ai_system_contracts \
         (id, tenant_id, ai_system_id, version, contract_hash, resolved_closure, status) \
         VALUES ($1, $2, $3, $4, $5, $6, $7)",
    )
    .bind(id)
    .bind(tenant)
    .bind(system)
    .bind(version)
    .bind(format!("sha256:{id}"))
    .bind(closure)
    .bind(status)
    .execute(pool)
    .await
    .expect("seed contract");
}

fn screening_catalogue() -> Value {
    json!([
        {"id": "application_processed", "label": "Processed", "counts_toward_baseline": true,
         "variants": [{"id": "loan_approved", "label": "a"}, {"id": "loan_rejected", "label": "r"}]},
        {"id": "no_applications_available", "label": "Nothing", "counts_toward_baseline": false}
    ])
}

fn fraud_catalogue() -> Value {
    json!([
        {"id": "screening_completed", "label": "Screened", "counts_toward_baseline": true,
         "variants": [{"id": "clear", "label": "c"}, {"id": "review", "label": "r"},
                      {"id": "hit", "label": "h"}]}
    ])
}

/// The five stored columns plus the terminal state, as read back.
#[derive(Debug, PartialEq)]
struct Judged {
    terminal_state: String,
    outcome_id: Option<String>,
    conformance: Option<String>,
    counts: Option<bool>,
    contract_version: Option<i32>,
    error_cause: Option<String>,
}

type JudgedRow = (
    String,
    Option<String>,
    Option<String>,
    Option<bool>,
    Option<i32>,
    Option<String>,
);

async fn judged(pool: &PgPool, tenant: &str, root: &str) -> Judged {
    let r: JudgedRow = sqlx::query_as(
        "SELECT terminal_state, outcome_id, outcome_conformance, \
                outcome_counts_toward_baseline, outcome_contract_version, error_cause \
           FROM ai_system_runs WHERE tenant_id = $1 AND root_task_id = $2",
    )
    .bind(tenant)
    .bind(root)
    .fetch_one(pool)
    .await
    .expect("run row");
    Judged {
        terminal_state: r.0,
        outcome_id: r.1,
        conformance: r.2,
        counts: r.3,
        contract_version: r.4,
        error_cause: r.5,
    }
}

const T: &str = "t-outcomes";
const SYS: &str = "sys-screening";
const FRAUD: &str = "sys-fraud";

async fn screening(pool: &PgPool) {
    common::seed_tenant(pool, T).await;
    seed_system(pool, T, SYS).await;
    seed_contract(
        pool,
        T,
        "ctr-s3",
        SYS,
        3,
        "active",
        Some(screening_catalogue()),
    )
    .await;
}

// ---------------------------------------------------------------------------
// Finalize stores the judgement
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn finalize_stores_the_five_columns_for_a_declared_outcome(pool: PgPool) {
    screening(&pool).await;
    let mut s = Step::new("s1", "r", "mcp", 30);
    s.group = Some(SYS);
    insert(&pool, T, s).await;
    let mut s = Step::new("s2", "r", "mcp", 20);
    s.group = Some(SYS);
    s.outcome_id = Some("application_processed.loan_approved");
    s.run_end = Some("completed");
    insert(&pool, T, s).await;

    let run = finalize_run(&pool, T, "r", finalized_by::EXPLICIT, None, None)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(run.run_class, "success");
    assert_eq!(
        judged(&pool, T, "r").await,
        Judged {
            terminal_state: "completed".into(),
            outcome_id: Some("application_processed.loan_approved".into()),
            conformance: Some("declared".into()),
            counts: Some(true),
            contract_version: Some(3),
            error_cause: None,
        }
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_no_work_outcome_is_declared_and_does_not_count(pool: PgPool) {
    screening(&pool).await;
    let mut s = Step::new("s1", "r", "mcp", 20);
    s.group = Some(SYS);
    s.outcome_id = Some("no_applications_available");
    insert(&pool, T, s).await;
    let run = finalize_run(
        &pool,
        T,
        "r",
        finalized_by::EXPLICIT,
        Some("completed"),
        None,
    )
    .await
    .unwrap()
    .unwrap();
    assert_eq!(run.run_class, "no_work");
    let j = judged(&pool, T, "r").await;
    assert_eq!(j.conformance.as_deref(), Some("declared"));
    assert_eq!(j.counts, Some(false));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn undeclared_unreported_and_contractless_runs_are_deviations(pool: PgPool) {
    screening(&pool).await;
    // An outcome with variants reported bare; an unknown variant; a
    // malformed value (stored verbatim, judged undeclared); nothing.
    for (root, outcome, want) in [
        ("r-bare", Some("application_processed"), "undeclared"),
        (
            "r-unknown",
            Some("application_processed.loan_maybe"),
            "undeclared",
        ),
        ("r-malformed", Some("Loan Approved!"), "undeclared"),
        ("r-none", None, "unreported"),
    ] {
        let mut s = Step::new(root, root, "mcp", 20);
        s.group = Some(SYS);
        s.outcome_id = outcome;
        insert(&pool, T, s).await;
        let run = finalize_run(
            &pool,
            T,
            root,
            finalized_by::EXPLICIT,
            Some("completed"),
            None,
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(run.run_class, "deviation", "{root}");
        let j = judged(&pool, T, root).await;
        assert_eq!(j.conformance.as_deref(), Some(want), "{root}");
        assert_eq!(j.outcome_id.as_deref(), outcome, "{root}: stored verbatim");
        assert_eq!(j.counts, None, "{root}");
        assert_eq!(j.contract_version, Some(3), "{root}: judged against v3");
    }

    // A system with no contract at all, and one whose contract has no
    // `outcomes` section: every reported id is undeclared.
    seed_system(&pool, T, "sys-bare").await;
    seed_system(&pool, T, "sys-old").await;
    seed_contract(&pool, T, "ctr-old", "sys-old", 1, "active", None).await;
    for (root, sys, version) in [
        ("r-bare-sys", "sys-bare", None),
        ("r-old", "sys-old", Some(1)),
    ] {
        let mut s = Step::new(root, root, "mcp", 20);
        s.group = Some(sys);
        s.outcome_id = Some("task_completed");
        insert(&pool, T, s).await;
        finalize_run(
            &pool,
            T,
            root,
            finalized_by::EXPLICIT,
            Some("completed"),
            None,
        )
        .await
        .unwrap();
        let j = judged(&pool, T, root).await;
        assert_eq!(j.conformance.as_deref(), Some("undeclared"), "{root}");
        assert_eq!(j.contract_version, version, "{root}");
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_run_stays_judged_by_the_contract_it_was_stamped_with(pool: PgPool) {
    // A later contract change never rewrites history: re-finalized after a
    // promotion that removed the outcome, the run keeps v3's judgement.
    screening(&pool).await;
    let mut s = Step::new("s1", "r", "mcp", 30);
    s.group = Some(SYS);
    s.outcome_id = Some("application_processed.loan_approved");
    insert(&pool, T, s).await;
    finalize_run(
        &pool,
        T,
        "r",
        finalized_by::EXPLICIT,
        Some("completed"),
        None,
    )
    .await
    .unwrap();
    sqlx::query("UPDATE ai_system_contracts SET status = 'superseded' WHERE id = 'ctr-s3'")
        .execute(&pool)
        .await
        .unwrap();
    seed_contract(&pool, T, "ctr-s4", SYS, 4, "active", Some(json!([]))).await;
    insert(&pool, T, {
        let mut s = Step::new("s2", "r", "mcp", 10);
        s.group = Some(SYS);
        s
    })
    .await;
    finalize_run(&pool, T, "r", finalized_by::IDLE, None, None)
        .await
        .unwrap();
    let j = judged(&pool, T, "r").await;
    assert_eq!(j.contract_version, Some(3));
    assert_eq!(j.conformance.as_deref(), Some("declared"));
}

// ---------------------------------------------------------------------------
// Durability — the sweeper backstop keeps the outcome
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_sweeper_backstop_keeps_the_outcome_the_client_reported(pool: PgPool) {
    // The client closed the run on its last call (run-end marker + outcome
    // header), but the immediate finalize never ran — writer restart, a
    // transient DB error. The sweeper finds the run later. The outcome
    // rides on the row, not beside it, so this close keeps it.
    screening(&pool).await;
    let mut s = Step::new("s1", "r-swept", "llm", 400);
    s.group = Some(SYS);
    insert(&pool, T, s).await;
    let mut s = Step::new("s2", "r-swept", "mcp", 390);
    s.group = Some(SYS);
    s.outcome_id = Some("application_processed.loan_rejected");
    s.run_end = Some("completed");
    insert(&pool, T, s).await;

    assert!(
        sweep_idle_runs(&pool, 50).await >= 1,
        "the sweeper closed it"
    );
    let j = judged(&pool, T, "r-swept").await;
    assert_eq!(j.terminal_state, "completed");
    assert_eq!(
        j.outcome_id.as_deref(),
        Some("application_processed.loan_rejected")
    );
    assert_eq!(j.conformance.as_deref(), Some("declared"));
    assert_eq!(j.counts, Some(true));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_outcome_stated_on_end_survives_idle_refinalization(pool: PgPool) {
    screening(&pool).await;
    let mut s = Step::new("s1", "r", "mcp", 30);
    s.group = Some(SYS);
    insert(&pool, T, s).await;
    finalize_run(
        &pool,
        T,
        "r",
        finalized_by::EXPLICIT,
        Some("completed"),
        Some("no_applications_available"),
    )
    .await
    .unwrap();
    // A late step reopens it; the sweeper carries no outcome. The one the
    // client stated must not be erased, and its judgement must not drift.
    let mut s = Step::new("s2", "r", "mcp", 10);
    s.group = Some(SYS);
    insert(&pool, T, s).await;
    finalize_run(&pool, T, "r", finalized_by::IDLE, None, None)
        .await
        .unwrap();
    let j = judged(&pool, T, "r").await;
    assert_eq!(j.outcome_id.as_deref(), Some("no_applications_available"));
    assert_eq!(j.conformance.as_deref(), Some("declared"));
    assert_eq!(j.counts, Some(false));
}

// ---------------------------------------------------------------------------
// Error causes
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_guardrail_block_ends_the_run_errored_blocked_by_policy(pool: PgPool) {
    screening(&pool).await;
    let mut s = Step::new("s1", "r", "mcp", 30);
    s.group = Some(SYS);
    insert(&pool, T, s).await;
    let mut s = Step::new("s2", "r", "llm", 20);
    s.group = Some(SYS);
    s.success = false;
    s.status = Some(403);
    s.guardrail_blocked = true;
    insert(&pool, T, s).await;
    let run = finalize_run(&pool, T, "r", finalized_by::IDLE, None, None)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(run.terminal_state, terminal::ERRORED, "not blocked_policy");
    assert_eq!(run.run_class, "failed", "a block counts against success");
    let j = judged(&pool, T, "r").await;
    assert_eq!(j.error_cause.as_deref(), Some("blocked_by_policy"));
    // The guardrail emitter's item is the sharper one: no run-failure item.
    let (n,): (i64,) = sqlx::query_as(
        "SELECT COUNT(*) FROM assurance_inbox_items WHERE tenant_id = $1 \
           AND fingerprint LIKE 'run_failure:%'",
    )
    .bind(T)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(n, 0);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn error_causes_are_derived_from_the_failing_call(pool: PgPool) {
    screening(&pool).await;
    // (root, last step (type, subtype, status), explicit state, cause)
    type Case<'a> = (
        &'a str,
        &'a str,
        Option<&'a str>,
        i32,
        Option<&'a str>,
        &'a str,
    );
    let cases: &[Case<'_>] = &[
        ("r-502", "llm", None, 502, None, "model_unavailable"),
        ("r-400", "llm", None, 400, None, "model_rejected"),
        ("r-429", "mcp", None, 429, None, "rate_limited"),
        ("r-tool", "mcp", None, 500, None, "tool_failed"),
        (
            "r-skill",
            "mcp",
            Some("skill_run"),
            500,
            None,
            "skill_failed",
        ),
        (
            "r-acl",
            "mcp",
            Some("access_denied"),
            403,
            None,
            "blocked_by_policy",
        ),
        (
            "r-a2a",
            "a2a",
            Some("outbound"),
            502,
            None,
            "agent_unreachable",
        ),
        (
            "r-exh",
            "llm",
            None,
            200,
            Some("exhausted"),
            "iteration_limit",
        ),
        (
            "r-client",
            "llm",
            None,
            200,
            Some("errored"),
            "client_reported",
        ),
    ];
    for (root, pt, sub, status, explicit, cause) in cases {
        let mut s = Step::new(root, root, pt, 20);
        s.group = Some(SYS);
        s.subtype = *sub;
        s.status = Some(*status);
        s.success = *status < 400;
        insert(&pool, T, s).await;
        finalize_run(&pool, T, root, finalized_by::EXPLICIT, *explicit, None)
            .await
            .unwrap();
        let j = judged(&pool, T, root).await;
        assert_eq!(j.error_cause.as_deref(), Some(*cause), "{root}: {j:?}");
    }
    // A completed run carries no cause, even after a recovered failure.
    let mut s = Step::new("c1", "r-ok", "llm", 30);
    s.group = Some(SYS);
    s.success = false;
    s.status = Some(502);
    insert(&pool, T, s).await;
    let mut s = Step::new("c2", "r-ok", "llm", 20);
    s.group = Some(SYS);
    insert(&pool, T, s).await;
    finalize_run(
        &pool,
        T,
        "r-ok",
        finalized_by::EXPLICIT,
        Some("completed"),
        None,
    )
    .await
    .unwrap();
    assert_eq!(judged(&pool, T, "r-ok").await.error_cause, None);
}

// ---------------------------------------------------------------------------
// Delegated outcomes on segments
// ---------------------------------------------------------------------------

/// The caller (screening) delegates to the fraud screener: its outbound A2A
/// row carries the delegate's reported outcome; the screener's own step is
/// in its group under the forked chain.
async fn delegation(pool: &PgPool, root: &str, delegate_outcome: Option<&str>) {
    let (first_id, call_id, child_id, task) = (
        format!("{root}-first"),
        format!("{root}-call"),
        format!("{root}-child"),
        format!("{root}-c"),
    );
    // The caller's first step: the run starts (and is attributed) here.
    let mut s = Step::new(&first_id, root, "mcp", 40);
    s.group = Some(SYS);
    insert(pool, T, s).await;
    let mut s = Step::new(&call_id, root, "a2a", 30);
    s.subtype = Some("outbound");
    s.group = Some(SYS);
    s.endpoint = "/v1/proxy/a2a/outbound";
    s.server_id = Some("card-fraud");
    s.current = Some(&task);
    s.task_state = Some("completed");
    s.outcome_id = delegate_outcome;
    insert(pool, T, s).await;
    let mut s = Step::new(&child_id, root, "llm", 35);
    s.group = Some(FRAUD);
    s.parent = Some(&task);
    insert(pool, T, s).await;
}

async fn segment(
    pool: &PgPool,
    root: &str,
) -> (String, Option<String>, Option<String>, Option<String>) {
    sqlx::query_as(
        "SELECT outcome, outcome_id, outcome_conformance, error_cause \
           FROM ai_system_run_segments WHERE tenant_id = $1 AND root_task_id = $2",
    )
    .bind(T)
    .bind(root)
    .fetch_one(pool)
    .await
    .expect("one segment")
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_declared_delegate_outcome_is_stored_on_its_segment_never_on_the_run(pool: PgPool) {
    screening(&pool).await;
    seed_system(&pool, T, FRAUD).await;
    seed_contract(
        &pool,
        T,
        "ctr-f1",
        FRAUD,
        1,
        "active",
        Some(fraud_catalogue()),
    )
    .await;
    delegation(&pool, "r", Some("screening_completed.hit")).await;
    // The caller's own outcome, on its own later step.
    let mut s = Step::new("own", "r", "mcp", 10);
    s.group = Some(SYS);
    s.outcome_id = Some("application_processed.loan_rejected");
    insert(&pool, T, s).await;

    finalize_run(
        &pool,
        T,
        "r",
        finalized_by::EXPLICIT,
        Some("completed"),
        None,
    )
    .await
    .unwrap();
    let (outcome, oid, conf, cause) = segment(&pool, "r").await;
    assert_eq!(outcome, "answered");
    assert_eq!(oid.as_deref(), Some("screening_completed.hit"));
    assert_eq!(
        conf.as_deref(),
        Some("declared"),
        "judged on the FRAUD contract"
    );
    assert_eq!(cause, None);
    let j = judged(&pool, T, "r").await;
    assert_eq!(
        j.outcome_id.as_deref(),
        Some("application_processed.loan_rejected"),
        "the delegate's outcome is never the caller's"
    );
    assert_eq!(j.conformance.as_deref(), Some("declared"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_undeclared_delegate_outcome_is_delegate_deviated(pool: PgPool) {
    screening(&pool).await;
    seed_system(&pool, T, FRAUD).await;
    seed_contract(
        &pool,
        T,
        "ctr-f1",
        FRAUD,
        1,
        "active",
        Some(fraud_catalogue()),
    )
    .await;
    for (root, reported, conf) in [
        ("r-maybe", Some("screening_completed.maybe"), "undeclared"),
        ("r-silent", None, "unreported"),
    ] {
        delegation(&pool, root, reported).await;
        // The caller treats it as a failed fraud check and errors the run.
        finalize_run(
            &pool,
            T,
            root,
            finalized_by::EXPLICIT,
            Some("errored"),
            None,
        )
        .await
        .unwrap();
        let (outcome, oid, c, cause) = segment(&pool, root).await;
        assert_eq!(outcome, "answered", "it did answer (liveness)");
        assert_eq!(oid.as_deref(), reported);
        assert_eq!(c.as_deref(), Some(conf), "{root}");
        assert_eq!(cause.as_deref(), Some("delegate_deviated"), "{root}");
        assert_eq!(
            judged(&pool, T, root).await.error_cause.as_deref(),
            Some("delegate_deviated"),
            "{root}: the caller's run names the delegate"
        );
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_refused_delegation_is_a_failed_segment_blocked_by_policy(pool: PgPool) {
    screening(&pool).await;
    seed_system(&pool, T, FRAUD).await;
    common::seed_agent_card(&pool, "card-fraud", T, "fraud-agent").await;
    sqlx::query(
        "INSERT INTO ai_system_dependencies \
         (id, tenant_id, ai_system_id, role, depends_on_system_id, agent_card_id) \
         VALUES ('dep-1', $1, $2, 'shared_service', $3, 'card-fraud')",
    )
    .bind(T)
    .bind(SYS)
    .bind(FRAUD)
    .execute(&pool)
    .await
    .unwrap();
    let mut s = Step::new("refused", "r", "a2a", 20);
    s.subtype = Some("unaccepted_caller");
    s.group = Some(SYS);
    s.endpoint = "/v1/proxy/a2a/outbound";
    s.server_id = Some("card-fraud");
    s.current = Some("c-refused");
    s.success = false;
    s.status = Some(403);
    insert(&pool, T, s).await;
    let run = finalize_run(&pool, T, "r", finalized_by::IDLE, None, None)
        .await
        .unwrap()
        .unwrap();
    let (outcome, _, _, cause) = segment(&pool, "r").await;
    assert_eq!(outcome, "failed", "`blocked` is gone");
    assert_eq!(cause.as_deref(), Some("blocked_by_policy"));
    assert_eq!(run.terminal_state, "errored");
    assert_eq!(run.error_cause.as_deref(), Some("blocked_by_policy"));
}

// ---------------------------------------------------------------------------
// The wire: /end 400s, the MCP header path, the A2A round trip
// ---------------------------------------------------------------------------

fn runs_app(state: &SharedState) -> axum::Router {
    axum::Router::new()
        .nest("/v1/runs", routes::runs::router())
        .layer(middleware::tenant::TenantLayer::new(state.clone()))
        .layer(middleware::run_end::RunEndLayer::new(
            state.mcp_sessions.clone(),
        ))
        .with_state(state.clone())
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn end_refuses_a_malformed_outcome_and_a_removed_state(pool: PgPool) {
    screening(&pool).await;
    let key = common::seed_api_key(&pool, T, SYS, None, "k-end").await;
    let mut s = Step::new("s1", "r-end", "mcp", 20);
    s.group = Some(SYS);
    insert(&pool, T, s).await;
    let state = common::build_test_state(pool.clone());

    for body in [
        json!({"state": "completed", "outcome_id": "Application_Processed"}),
        json!({"state": "completed", "outcome_id": "a.b.c"}),
        json!({"state": "completed", "outcome_id": "x".repeat(49)}),
        json!({"state": "blocked_policy"}),
    ] {
        let resp = runs_app(&state)
            .oneshot(common::build_post_request(
                "/v1/runs/r-end/end",
                body.clone(),
                Some(&key),
                Some(T),
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST, "{body}");
    }
    let (n,): (i64,) = sqlx::query_as("SELECT COUNT(*) FROM ai_system_runs WHERE tenant_id = $1")
        .bind(T)
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(n, 0, "a refused /end finalizes nothing");

    // A malformed header is refused the same way.
    let mut req = common::build_post_request(
        "/v1/runs/r-end/end",
        json!({"state": "completed"}),
        Some(&key),
        Some(T),
    );
    req.headers_mut()
        .insert("X-Brutor-Outcome-Id", "bad id".parse().unwrap());
    let resp = runs_app(&state).oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);

    // A well-formed one finalizes and answers with the judgement.
    let resp = runs_app(&state)
        .oneshot(common::build_post_request(
            "/v1/runs/r-end/end",
            json!({"state": "completed", "outcome_id": "application_processed.loan_approved"}),
            Some(&key),
            Some(T),
        ))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body: Value = serde_json::from_slice(
        &axum::body::to_bytes(resp.into_body(), 1 << 20)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(body["outcome_conformance"], "declared");
    assert_eq!(body["run_class"], "success");
    assert_eq!(body["outcome_id"], "application_processed.loan_approved");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_mcp_call_carrying_run_end_and_outcome_closes_and_judges_the_run(pool: PgPool) {
    // The demo scheduler's tick closes its run on one more MCP call carrying
    // `X-Brutor-Run-End` + `X-Brutor-Outcome-Id` (MCP replies carry no root
    // usable for `/end`). The header must reach the step row and the run.
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "jsonrpc": "2.0", "id": 1, "result": {"tools": []}
        })))
        .mount(&mock)
        .await;
    common::seed_tenant(&pool, T).await;
    seed_system(&pool, T, SYS).await;
    seed_contract(
        &pool,
        T,
        "ctr-s3",
        SYS,
        3,
        "active",
        Some(screening_catalogue()),
    )
    .await;
    common::seed_end_user(&pool, 9101, T).await;
    common::add_direct_membership(&pool, 9101, SYS, T).await;
    common::seed_mcp_server_with_base_url(&pool, "ms-apps", T, "apps", &mock.uri()).await;
    common::seed_server_config(&pool, "sc-apps", T, "ms-apps").await;
    common::bind_server_config_to_rg(&pool, "sc-apps", SYS).await;
    let key = common::seed_api_key(&pool, T, SYS, Some(9101), "k-mcp").await;
    let state = common::build_test_state(pool.clone());
    let app = common::build_mcp_proxy_app(state);

    let mut req = common::build_post_request(
        "/v1/proxy/mcp/ms-apps",
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
        Some(&key),
        Some(T),
    );
    for (k, v) in [
        ("x-brutor-run-id", "bds-tick-1"),
        ("X-Brutor-Run-End", "completed"),
        ("X-Brutor-Outcome-Id", "no_applications_available"),
    ] {
        req.headers_mut().insert(k, v.parse().unwrap());
    }
    let resp = app.oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    // The writer flushes, then finalizes the marked run.
    let mut found = None;
    for _ in 0..100 {
        found = sqlx::query_as::<_, (String, Option<String>, Option<String>, Option<bool>)>(
            "SELECT finalized_by, outcome_id, outcome_conformance, outcome_counts_toward_baseline \
               FROM ai_system_runs WHERE tenant_id = $1",
        )
        .bind(T)
        .fetch_optional(&pool)
        .await
        .unwrap();
        if found.is_some() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    let (by, oid, conf, counts) = found.expect("the marked run was finalized");
    assert_eq!(by, "explicit");
    assert_eq!(oid.as_deref(), Some("no_applications_available"));
    assert_eq!(conf.as_deref(), Some("declared"));
    assert_eq!(counts, Some(false));
    let (row_outcome,): (Option<String>,) = sqlx::query_as(
        "SELECT outcome_id FROM proxy_logs WHERE tenant_id = $1 AND run_end_state IS NOT NULL",
    )
    .bind(T)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(
        row_outcome.as_deref(),
        Some("no_applications_available"),
        "durable on the step"
    );
}

// -- A2A round trip -------------------------------------------------------------

const CARD: &str = "card-fraud";

async fn a2a_topology(pool: &PgPool, upstream: &str) -> String {
    common::seed_tenant(pool, T).await;
    seed_system(pool, T, SYS).await;
    seed_system(pool, T, FRAUD).await;
    seed_contract(
        pool,
        T,
        "ctr-f1",
        FRAUD,
        1,
        "active",
        Some(fraud_catalogue()),
    )
    .await;
    common::seed_agent_card(pool, CARD, T, "fraud-agent").await;
    sqlx::query("UPDATE agent_cards SET card_json = $2 WHERE id = $1")
        .bind(CARD)
        .bind(json!({"name": "fraud-agent", "url": upstream, "capabilities": [{"name": "screen"}]}))
        .execute(pool)
        .await
        .unwrap();
    sqlx::query(
        "INSERT INTO agent_card_capabilities (id, agent_card_id, capability_name, handler_type, enabled) \
         VALUES ('cap-screen', $1, 'screen', 'noop', true)",
    )
    .bind(CARD)
    .execute(pool)
    .await
    .unwrap();
    for g in [SYS, FRAUD] {
        common::bind_agent_card_to_rg(pool, CARD, g).await;
    }
    sqlx::query(
        "INSERT INTO ai_system_dependencies \
         (id, tenant_id, ai_system_id, role, depends_on_system_id, agent_card_id) \
         VALUES ('dep-fraud', $1, $2, 'shared_service', $3, $4)",
    )
    .bind(T)
    .bind(SYS)
    .bind(FRAUD)
    .bind(CARD)
    .execute(pool)
    .await
    .unwrap();
    common::seed_api_key(pool, T, SYS, None, "k-a2a").await
}

fn a2a_app(state: &SharedState) -> axum::Router {
    axum::Router::new()
        .nest("/v1/proxy/a2a", routes::a2a::router())
        .layer(middleware::tenant::TenantLayer::new(state.clone()))
        .layer(middleware::run_end::RunEndLayer::new(
            state.mcp_sessions.clone(),
        ))
        .with_state(state.clone())
}

fn outbound(key: &str) -> Request<Body> {
    let mut req = common::build_post_request(
        "/v1/proxy/a2a/outbound",
        json!({"target_card_id": CARD, "capability": "screen", "message": {"text": "x"}}),
        Some(key),
        Some(T),
    );
    // The CALLER's own outcome header on the outbound route is not read: the
    // row carries the delegate's.
    req.headers_mut().insert(
        "X-Brutor-Outcome-Id",
        "application_processed.loan_approved".parse().unwrap(),
    );
    req
}

async fn outbound_row_outcome(pool: &PgPool) -> Option<String> {
    for _ in 0..100 {
        let r: Option<(Option<String>,)> = sqlx::query_as(
            "SELECT outcome_id FROM proxy_logs WHERE tenant_id = $1 AND proxy_subtype = 'outbound' \
             ORDER BY timestamp DESC LIMIT 1",
        )
        .bind(T)
        .fetch_optional(pool)
        .await
        .unwrap();
        if let Some((o,)) = r {
            return o;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    panic!("no outbound row")
}

async fn round_trip(
    pool: PgPool,
    reply: ResponseTemplate,
) -> (axum::http::HeaderMap, Vec<u8>, Option<String>) {
    let upstream = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(reply)
        .mount(&upstream)
        .await;
    let key = a2a_topology(&pool, &upstream.uri()).await;
    let state = common::build_test_state(pool.clone());
    let resp = a2a_app(&state).oneshot(outbound(&key)).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let headers = resp.headers().clone();
    let body = axum::body::to_bytes(resp.into_body(), 1 << 20)
        .await
        .unwrap()
        .to_vec();
    let row = outbound_row_outcome(&pool).await;
    (headers, body, row)
}

fn header(h: &axum::http::HeaderMap, k: &str) -> Option<String> {
    h.get(k).and_then(|v| v.to_str().ok()).map(str::to_string)
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_sync_delegate_header_is_checked_and_returned_to_the_caller(pool: PgPool) {
    let reply = ResponseTemplate::new(200)
        .insert_header("X-Brutor-Outcome-Id", "screening_completed.hit")
        .set_body_json(json!({"task": {"id": "t1", "status": {"state": "TASK_STATE_COMPLETED"}}}));
    let (h, body, row) = round_trip(pool, reply).await;
    assert_eq!(
        header(&h, "X-Brutor-Outcome-Id").as_deref(),
        Some("screening_completed.hit")
    );
    assert_eq!(
        header(&h, "X-Brutor-Outcome-Conformance").as_deref(),
        Some("declared")
    );
    let v: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(
        v["task"]["metadata"]["brutor.outcome.conformance"],
        "declared"
    );
    assert_eq!(
        v["task"]["metadata"]["brutor.outcome.id"],
        "screening_completed.hit"
    );
    assert_eq!(
        row.as_deref(),
        Some("screening_completed.hit"),
        "the DELEGATE's, not the caller's"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_undeclared_delegate_answer_is_flagged_to_the_caller(pool: PgPool) {
    let reply = ResponseTemplate::new(200)
        .insert_header("X-Brutor-Outcome-Id", "screening_completed.maybe")
        .set_body_json(json!({"task": {"id": "t1", "status": {"state": "TASK_STATE_COMPLETED"}}}));
    let (h, body, _) = round_trip(pool, reply).await;
    assert_eq!(
        header(&h, "X-Brutor-Outcome-Conformance").as_deref(),
        Some("undeclared")
    );
    let v: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(
        v["task"]["metadata"]["brutor.outcome.conformance"],
        "undeclared"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_delegate_that_reports_nothing_is_unreported(pool: PgPool) {
    let reply = ResponseTemplate::new(200)
        .set_body_json(json!({"task": {"id": "t1", "status": {"state": "TASK_STATE_COMPLETED"}}}));
    let (h, _, row) = round_trip(pool, reply).await;
    assert_eq!(
        header(&h, "X-Brutor-Outcome-Conformance").as_deref(),
        Some("unreported")
    );
    assert_eq!(header(&h, "X-Brutor-Outcome-Id"), None);
    assert_eq!(row, None, "the caller's own header never lands on this row");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_streamed_task_carrying_the_outcome_in_final_metadata_is_checked_the_same_way(
    pool: PgPool,
) {
    let sse = "data: {\"task\":{\"id\":\"t1\",\"status\":{\"state\":\"TASK_STATE_WORKING\"}}}\n\n\
               data: {\"statusUpdate\":{\"taskId\":\"t1\",\"status\":{\"state\":\"TASK_STATE_COMPLETED\"},\"final\":true,\
               \"metadata\":{\"brutor.outcome.id\":\"screening_completed.clear\"}}}\n\n";
    let reply = ResponseTemplate::new(200)
        .insert_header("content-type", "text/event-stream")
        .set_body_string(sse);
    let (h, body, row) = round_trip(pool, reply).await;
    assert_eq!(
        header(&h, "X-Brutor-Outcome-Id").as_deref(),
        Some("screening_completed.clear")
    );
    assert_eq!(
        header(&h, "X-Brutor-Outcome-Conformance").as_deref(),
        Some("declared")
    );
    let text = String::from_utf8(body).unwrap();
    assert!(
        text.contains("\"brutor.outcome.conformance\":\"declared\""),
        "the final frame carries the checked pair: {text}"
    );
    assert_eq!(row.as_deref(), Some("screening_completed.clear"));
}
