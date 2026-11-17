// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Run segments against a real Postgres.
//!
//! The partition itself is pure and property-tested in
//! `services::run_segments`. What only a database can show is that the
//! pieces meet: the per-group SQL, the entering-call SQL, the single write
//! transaction, delete-and-rewrite on reopen, and that a replay run keeps
//! none. Expectations are hand-computed.

use chrono::{DateTime, Duration, Utc};
use serde_json::json;
use sqlx::PgPool;

use brutor_gateway_core::services::run_ledger::{finalize_run, finalized_by};

use super::common;

const ROOT_SYS: &str = "sys-screening";
const FRAUD_SYS: &str = "sys-fraud";

#[derive(Clone)]
struct Step<'a> {
    id: &'a str,
    at: DateTime<Utc>,
    proxy_type: &'a str,
    subtype: Option<&'a str>,
    group: Option<&'a str>,
    depth: i32,
    current: Option<&'a str>,
    parent: Option<&'a str>,
    actor: Option<&'a str>,
    card: Option<&'a str>,
    success: bool,
    status: i32,
    error: Option<&'a str>,
    body: Option<&'a str>,
    /// `proxy_logs.a2a_task_state` — what the gateway decided the caller
    /// received; the ledger reads this, never `body`.
    task_state: Option<&'a str>,
    model: Option<&'a str>,
    tool: Option<&'a str>,
    tokens: i32,
    cost: Option<f64>,
    /// `proxy_logs.duration_ms`. A row is stamped when its call FINISHES,
    /// so the step ran over `[at - duration, at]`.
    duration_ms: Option<f64>,
    /// `custom_metadata.approval_request_id` — the request a held row waits on.
    approval: Option<&'a str>,
    /// `proxy_logs.endpoint` when it is not the default for the type (an
    /// `a2a` row is the OUTBOUND route's unless this says otherwise).
    endpoint: Option<&'a str>,
}

fn step<'a>(
    id: &'a str,
    at: DateTime<Utc>,
    proxy_type: &'a str,
    group: Option<&'a str>,
) -> Step<'a> {
    Step {
        id,
        at,
        proxy_type,
        subtype: None,
        group,
        depth: 0,
        current: None,
        parent: None,
        actor: None,
        card: None,
        success: true,
        status: 200,
        error: None,
        body: None,
        task_state: None,
        model: None,
        tool: None,
        tokens: 0,
        cost: None,
        duration_ms: None,
        approval: None,
        endpoint: None,
    }
}

async fn insert(pool: &PgPool, tenant: &str, root: &str, s: Step<'_>) {
    sqlx::query(
        "INSERT INTO proxy_logs (
            id, tenant_id, timestamp, proxy_type, proxy_subtype, end_user_session_id,
            http_method, endpoint, success, http_status_code, error_message,
            root_task_id, delegation_depth, current_task_id, parent_task_id,
            resource_group_id, actor_agent_id, server_id, response_body_preview,
            llm_model_used, mcp_capability_name, llm_total_tokens, llm_estimated_cost_usd,
            trace_continuity, is_auth_event, a2a_task_state, duration_ms, custom_metadata
         ) VALUES (
            $1, $2, $3, $4, $5, 'sess', 'POST', $22, $6, $7, $8,
            $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20,
            'verified', false, $21, $23,
            CASE WHEN $24::text IS NULL THEN NULL
                 ELSE json_build_object('approval_request_id', $24::text) END
         )",
    )
    .bind(s.id)
    .bind(tenant)
    .bind(s.at)
    .bind(s.proxy_type)
    .bind(s.subtype)
    .bind(s.success)
    .bind(s.status)
    .bind(s.error)
    .bind(root)
    .bind(s.depth)
    .bind(s.current)
    .bind(s.parent)
    .bind(s.group)
    .bind(s.actor)
    .bind(s.card)
    .bind(s.body)
    .bind(s.model)
    .bind(s.tool)
    .bind(s.tokens)
    .bind(s.cost)
    .bind(s.task_state)
    // Every a2a row in this file is one the OUTBOUND route wrote.
    .bind(s.endpoint.unwrap_or(if s.proxy_type == "a2a" {
        brutor_gateway_core::constants::routes::A2A_OUTBOUND
    } else {
        "/x"
    }))
    .bind(s.duration_ms)
    .bind(s.approval)
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
    .expect("seed_system");
}

async fn seed_contract(pool: &PgPool, tenant: &str, id: &str, system: &str, version: i32) {
    sqlx::query(
        "INSERT INTO ai_system_contracts \
         (id, tenant_id, ai_system_id, version, contract_hash, resolved_closure, status) \
         VALUES ($1, $2, $3, $4, $5, '{}'::jsonb, 'active')",
    )
    .bind(id)
    .bind(tenant)
    .bind(system)
    .bind(version)
    .bind(format!("sha256:{id}"))
    .execute(pool)
    .await
    .expect("seed_contract");
}

async fn retire_contracts(pool: &PgPool, system: &str) {
    sqlx::query("UPDATE ai_system_contracts SET status = 'superseded' WHERE ai_system_id = $1")
        .bind(system)
        .execute(pool)
        .await
        .unwrap();
}

async fn setup(pool: &PgPool, tenant: &str) {
    common::seed_tenant(pool, tenant).await;
    seed_system(pool, tenant, ROOT_SYS).await;
    seed_system(pool, tenant, FRAUD_SYS).await;
    // The screening agent's identity: a run's agent_id is an FK to it.
    sqlx::query(
        "INSERT INTO agent_identities (id, tenant_id, name) VALUES ('agent-screener', $1, 'screener') \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(tenant)
    .execute(pool)
    .await
    .expect("seed agent identity");
}

/// The shape of the demo system: the screening agent's own steps, one A2A
/// outbound to the fraud screener, and the screener's steps at depth 1 under
/// the chain that call forked.
async fn seed_delegation_run(pool: &PgPool, tenant: &str, root: &str, t: DateTime<Utc>) {
    let s = |sec: i64| t + Duration::seconds(sec);

    let mut a = step("s-1", s(0), "llm", Some(ROOT_SYS));
    a.current = Some("task-1");
    a.model = Some("gpt-5.2");
    a.tokens = 1000;
    a.cost = Some(0.012_345_678_9);
    insert(pool, tenant, root, a).await;

    let mut b = step("s-2", s(5), "mcp", Some(ROOT_SYS));
    b.current = Some("task-2");
    b.tool = Some("credit_report");
    insert(pool, tenant, root, b).await;

    let mut call = step("s-3", s(10), "a2a", Some(ROOT_SYS));
    call.subtype = Some("outbound");
    call.current = Some("task-call");
    call.actor = Some("agent-screener");
    call.card = Some("card-fraud");
    call.tool = Some("fraud_screen");
    call.body = Some(r#"{"task":{"id":"x","status":{"state":"TASK_STATE_COMPLETED"}}}"#);
    call.task_state = Some("completed");
    insert(pool, tenant, root, call).await;

    // The delegate: two model calls and a tool, all under the forked chain.
    let mut f1 = step("f-1", s(11), "llm", Some(FRAUD_SYS));
    f1.depth = 1;
    f1.current = Some("task-f1");
    f1.parent = Some("task-call");
    f1.model = Some("gpt-5.1-mini");
    f1.tokens = 300;
    f1.cost = Some(0.000_123_456_7);
    insert(pool, tenant, root, f1).await;

    let mut f2 = step("f-2", s(20), "mcp", Some(FRAUD_SYS));
    f2.depth = 1;
    f2.current = Some("task-f2");
    f2.parent = Some("task-call");
    f2.tool = Some("watchlist_lookup");
    insert(pool, tenant, root, f2).await;

    let mut f3 = step("f-3", s(31), "llm", Some(FRAUD_SYS));
    f3.depth = 1;
    f3.current = Some("task-f3");
    f3.parent = Some("task-call");
    f3.model = Some("gpt-5.1-mini");
    f3.tokens = 200;
    f3.cost = Some(0.000_076_543_2);
    insert(pool, tenant, root, f3).await;

    let mut d = step("s-4", s(40), "llm", Some(ROOT_SYS));
    d.current = Some("task-4");
    d.model = Some("gpt-5.5");
    d.tokens = 2000;
    d.cost = Some(0.03);
    insert(pool, tenant, root, d).await;
}

#[derive(Debug, sqlx::FromRow)]
struct SegRow {
    id: String,
    run_id: String,
    root_system_id: Option<String>,
    segment_system_id: String,
    min_depth: i16,
    first_action_at: DateTime<Utc>,
    last_action_at: DateTime<Utc>,
    active_ms: i32,
    action_count: i32,
    llm_call_count: i32,
    tool_call_count: i32,
    error_count: i32,
    total_tokens: i64,
    cost_usd: String,
    models_used: serde_json::Value,
    tool_set_signature: Option<String>,
    caller_agent_id: Option<String>,
    caller_system_id: Option<String>,
    a2a_task_state: Option<String>,
    outcome: String,
    contract_id: Option<String>,
}

async fn segments(pool: &PgPool, tenant: &str, root: &str) -> Vec<SegRow> {
    sqlx::query_as(
        "SELECT s.id, s.run_id, s.root_system_id, s.segment_system_id, s.min_depth,
                s.first_action_at, s.last_action_at, s.active_ms, s.action_count,
                s.llm_call_count, s.tool_call_count, s.error_count, s.total_tokens,
                CAST(s.cost_usd AS TEXT) AS cost_usd, s.models_used, s.tool_set_signature,
                s.caller_agent_id, s.caller_system_id, s.a2a_task_state, s.outcome,
                s.contract_id
           FROM ai_system_run_segments s
           JOIN ai_system_runs r ON r.id = s.run_id
          WHERE r.tenant_id = $1 AND r.root_task_id = $2
          ORDER BY s.first_action_at, s.segment_system_id",
    )
    .bind(tenant)
    .bind(root)
    .fetch_all(pool)
    .await
    .expect("load segments")
}

/// Conservation checked in SQL, independently of the Rust partition: the root's own
/// share is recomputed from `proxy_logs` (every step NOT in a segment
/// system's groups), rounded per group exactly as the column is, and must
/// add up with the stored segments to the stored run — exactly.
async fn assert_conserved(pool: &PgPool, tenant: &str, root: &str) {
    let (ok, detail): (bool, String) = sqlx::query_as(
        "WITH run AS (
             SELECT id, action_count, total_tokens, total_cost_usd
               FROM ai_system_runs WHERE tenant_id = $1 AND root_task_id = $2),
         seg AS (
             SELECT COALESCE(SUM(action_count), 0) AS a, COALESCE(SUM(total_tokens), 0) AS t,
                    COALESCE(SUM(cost_usd), 0) AS c
               FROM ai_system_run_segments WHERE run_id = (SELECT id FROM run)),
         own_groups AS (
             SELECT pl.resource_group_id, COUNT(*) AS a,
                    COALESCE(SUM(pl.llm_total_tokens), 0) AS t,
                    ROUND(COALESCE(SUM(CAST(pl.llm_estimated_cost_usd AS NUMERIC)), 0), 8) AS c
               FROM proxy_logs pl
              WHERE pl.tenant_id = $1 AND pl.root_task_id = $2
                AND NOT EXISTS (
                    SELECT 1 FROM ai_system_run_segments s
                     WHERE s.run_id = (SELECT id FROM run)
                       AND s.segment_system_id = pl.resource_group_id)
              GROUP BY pl.resource_group_id),
         own AS (SELECT COALESCE(SUM(a), 0) AS a, COALESCE(SUM(t), 0) AS t,
                        COALESCE(SUM(c), 0) AS c FROM own_groups),
         rows AS (SELECT COUNT(*) AS a FROM proxy_logs WHERE tenant_id = $1 AND root_task_id = $2)
         SELECT run.action_count = own.a + seg.a
            AND run.total_tokens = own.t + seg.t
            AND run.total_cost_usd = own.c + seg.c
            AND run.action_count = rows.a,
                format('run=(%s,%s,%s) own=(%s,%s,%s) seg=(%s,%s,%s) rows=%s',
                       run.action_count, run.total_tokens, run.total_cost_usd,
                       own.a, own.t, own.c, seg.a, seg.t, seg.c, rows.a)
           FROM run, seg, own, rows",
    )
    .bind(tenant)
    .bind(root)
    .fetch_one(pool)
    .await
    .expect("conservation query");
    assert!(ok, "root-own + Σ segments != run: {detail}");
}

async fn via_of(pool: &PgPool, tenant: &str, root: &str) -> serde_json::Value {
    let (via,): (serde_json::Value,) = sqlx::query_as(
        "SELECT via_system_ids FROM ai_system_runs WHERE tenant_id = $1 AND root_task_id = $2",
    )
    .bind(tenant)
    .bind(root)
    .fetch_one(pool)
    .await
    .unwrap();
    via
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_two_system_delegation_writes_exactly_one_segment(pool: PgPool) {
    let tenant = "t-seg-two";
    setup(&pool, tenant).await;
    seed_contract(&pool, tenant, "ctr-fraud-v3", FRAUD_SYS, 3).await;
    let t = crate::common::db_now() - Duration::minutes(10);
    seed_delegation_run(&pool, tenant, "root-two", t).await;

    let run = finalize_run(
        &pool,
        tenant,
        "root-two",
        finalized_by::EXPLICIT,
        None,
        None,
    )
    .await
    .unwrap()
    .unwrap();
    assert_eq!(run.ai_system_id.as_deref(), Some(ROOT_SYS));
    assert_eq!(run.action_count, 7);

    let segs = segments(&pool, tenant, "root-two").await;
    assert_eq!(segs.len(), 1, "exactly one segment: {segs:?}");
    let s = &segs[0];
    assert_eq!(s.run_id, run.run_id);
    assert!(s.id.starts_with("seg-"));
    assert_eq!(s.root_system_id.as_deref(), Some(ROOT_SYS));
    assert_eq!(s.segment_system_id, FRAUD_SYS);
    assert_eq!(s.min_depth, 1);
    assert_eq!(s.first_action_at, t + Duration::seconds(11));
    assert_eq!(s.last_action_at, t + Duration::seconds(31));
    assert_eq!(s.active_ms, 20_000);
    assert_eq!(
        (s.action_count, s.llm_call_count, s.tool_call_count),
        (3, 2, 1)
    );
    assert_eq!(s.error_count, 0);
    assert_eq!(s.total_tokens, 500);
    // 0.0001234567 + 0.0000765432, rounded once to the column scale.
    assert_eq!(s.cost_usd, "0.00020000");
    assert_eq!(s.models_used, json!({"gpt-5.1-mini": 2}));
    // The run's formula over the segment's own tools only.
    let (want_sig,): (String,) = sqlx::query_as("SELECT MD5('watchlist_lookup')")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(s.tool_set_signature.as_deref(), Some(want_sig.as_str()));
    assert_eq!(s.caller_agent_id.as_deref(), Some("agent-screener"));
    assert_eq!(s.a2a_task_state.as_deref(), Some("completed"));
    assert_eq!(s.outcome, "answered");
    assert_eq!(s.contract_id.as_deref(), Some("ctr-fraud-v3"));

    assert_eq!(via_of(&pool, tenant, "root-two").await, json!([FRAUD_SYS]));
    assert_conserved(&pool, tenant, "root-two").await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_segment_excludes_only_its_own_human_holds(pool: PgPool) {
    let tenant = "t-seg-hold";
    setup(&pool, tenant).await;
    let t = crate::common::db_now() - Duration::minutes(10);
    seed_delegation_run(&pool, tenant, "root-hold", t).await;
    // The screener parked a 6s decision on a person inside its span; the
    // ROOT's hold overlapping the same span is not the screener's wait.
    for (id, group, from, to) in [
        ("hold-fraud", FRAUD_SYS, 12, 18),
        ("hold-root", ROOT_SYS, 20, 30),
    ] {
        sqlx::query(
            "INSERT INTO tool_approval_requests (
                id, tenant_id, server_name, capability_type, capability_name,
                requester_type, requester_group_id, status, timeout_seconds,
                expires_at, resolved_at, originating_trace_root_id, created_at
             ) VALUES ($1, $2, 'srv', 'tool', 'x', 'agent', $3, 'approved', 3600,
                       $4, $5, 'root-hold', $6)",
        )
        .bind(id)
        .bind(tenant)
        .bind(group)
        .bind(t + Duration::hours(1))
        .bind(t + Duration::seconds(to))
        .bind(t + Duration::seconds(from))
        .execute(&pool)
        .await
        .unwrap();
    }
    finalize_run(
        &pool,
        tenant,
        "root-hold",
        finalized_by::EXPLICIT,
        None,
        None,
    )
    .await
    .unwrap();
    let segs = segments(&pool, tenant, "root-hold").await;
    assert_eq!(segs[0].active_ms, 20_000 - 6_000);
}

// ---------------------------------------------------------------------------
// Active time starts when the first call STARTED
//
// A proxy-log row is stamped when its call finishes, so a step ran over
// `[timestamp - duration_ms, timestamp]`. Measuring the span between stamps
// made every one-call segment 0 ms (live: all 105 Fraud Screener segments)
// and cut the first call out of every longer one — and a shared service's
// duration baseline is learned from this column.
// ---------------------------------------------------------------------------

async fn set_duration(pool: &PgPool, tenant: &str, id: &str, ms: &str) {
    sqlx::query(
        "UPDATE proxy_logs SET duration_ms = CAST($3 AS DOUBLE PRECISION) \
         WHERE tenant_id = $1 AND id = $2",
    )
    .bind(tenant)
    .bind(id)
    .bind(ms)
    .execute(pool)
    .await
    .expect("set duration");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_one_call_delegate_is_active_for_its_call(pool: PgPool) {
    let tenant = "t-seg-onecall";
    setup(&pool, tenant).await;
    let t = crate::common::db_now() - Duration::minutes(10);
    let root = "root-onecall";

    let mut a = step("s-1", t, "llm", Some(ROOT_SYS));
    a.current = Some("task-1");
    a.duration_ms = Some(900.0);
    insert(&pool, tenant, root, a).await;

    // The caller's outbound row finishes just after the delegate's only call.
    let mut call = step(
        "s-2",
        t + Duration::milliseconds(5_016),
        "a2a",
        Some(ROOT_SYS),
    );
    call.subtype = Some("outbound");
    call.current = Some("task-call");
    call.actor = Some("agent-screener");
    call.card = Some("card-fraud");
    call.task_state = Some("completed");
    call.duration_ms = Some(1_304.0);
    insert(&pool, tenant, root, call).await;

    let mut f = step("f-1", t + Duration::seconds(5), "llm", Some(FRAUD_SYS));
    f.depth = 1;
    f.current = Some("task-f1");
    f.parent = Some("task-call");
    f.model = Some("gpt-5.1-mini");
    f.duration_ms = Some(1_243.0);
    insert(&pool, tenant, root, f).await;

    finalize_run(&pool, tenant, root, finalized_by::EXPLICIT, None, None)
        .await
        .unwrap()
        .expect("finalized");
    let segs = segments(&pool, tenant, root).await;
    assert_eq!(segs.len(), 1, "{segs:?}");
    assert_eq!(segs[0].active_ms, 1_243, "the delegate worked for its call");
    // `first_action_at` keeps meaning "the first step's stamp".
    assert_eq!(segs[0].first_action_at, t + Duration::seconds(5));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_segments_active_time_includes_its_first_call(pool: PgPool) {
    let tenant = "t-seg-firstcall";
    setup(&pool, tenant).await;
    let t = crate::common::db_now() - Duration::minutes(10);
    seed_delegation_run(&pool, tenant, "root-first", t).await;
    // f-1 (stamped at 11 s) ran for 2 s; f-3 (31 s) for 4 s, inside the span.
    set_duration(&pool, tenant, "f-1", "2000").await;
    set_duration(&pool, tenant, "f-3", "4000").await;
    finalize_run(
        &pool,
        tenant,
        "root-first",
        finalized_by::EXPLICIT,
        None,
        None,
    )
    .await
    .unwrap();
    let segs = segments(&pool, tenant, "root-first").await;
    assert_eq!(segs[0].active_ms, 31_000 - 9_000);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_hold_inside_the_first_call_is_excluded_once(pool: PgPool) {
    let tenant = "t-seg-earlyhold";
    setup(&pool, tenant).await;
    let t = crate::common::db_now() - Duration::minutes(10);
    seed_delegation_run(&pool, tenant, "root-early", t).await;
    set_duration(&pool, tenant, "f-1", "2000").await;
    // The screener's 1 s hold sits inside its first call (9 s..11 s), before
    // that call's stamp: clipped to the old span it was never subtracted.
    sqlx::query(
        "INSERT INTO tool_approval_requests (
            id, tenant_id, server_name, capability_type, capability_name,
            requester_type, requester_group_id, status, timeout_seconds,
            expires_at, resolved_at, originating_trace_root_id, created_at
         ) VALUES ('hold-early', $1, 'srv', 'tool', 'x', 'agent', $2, 'approved', 3600,
                   $3, $4, 'root-early', $5)",
    )
    .bind(tenant)
    .bind(FRAUD_SYS)
    .bind(t + Duration::hours(1))
    .bind(t + Duration::milliseconds(10_500))
    .bind(t + Duration::milliseconds(9_500))
    .execute(&pool)
    .await
    .unwrap();
    finalize_run(
        &pool,
        tenant,
        "root-early",
        finalized_by::EXPLICIT,
        None,
        None,
    )
    .await
    .unwrap();
    let segs = segments(&pool, tenant, "root-early").await;
    assert_eq!(segs[0].active_ms, 22_000 - 1_000);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unusable_duration_never_moves_the_start(pool: PgPool) {
    let tenant = "t-seg-baddur";
    setup(&pool, tenant).await;
    let t = crate::common::db_now() - Duration::minutes(10);
    seed_delegation_run(&pool, tenant, "root-bad", t).await;
    // NaN sorts above every number in Postgres and Infinity would overflow
    // the timestamp; a negative duration is nonsense. Each counts as 0, i.e.
    // exactly the old measure — never an error that leaves the run open.
    set_duration(&pool, tenant, "f-1", "NaN").await;
    set_duration(&pool, tenant, "f-2", "Infinity").await;
    set_duration(&pool, tenant, "f-3", "-5000").await;
    finalize_run(
        &pool,
        tenant,
        "root-bad",
        finalized_by::EXPLICIT,
        None,
        None,
    )
    .await
    .unwrap()
    .expect("finalized");
    let segs = segments(&pool, tenant, "root-bad").await;
    assert_eq!(segs[0].active_ms, 20_000);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_failed_delegate_is_outcome_failed(pool: PgPool) {
    let tenant = "t-seg-fail";
    setup(&pool, tenant).await;
    let t = crate::common::db_now() - Duration::minutes(10);
    let s = |sec: i64| t + Duration::seconds(sec);

    let mut a = step("s-1", s(0), "llm", Some(ROOT_SYS));
    a.current = Some("task-1");
    insert(&pool, tenant, "root-fail", a).await;
    // The screener ran, its model call failed, and it handed back a failed
    // task. The transport succeeded — the ANSWER was a failure.
    let mut call = step("s-2", s(1), "a2a", Some(ROOT_SYS));
    call.subtype = Some("outbound");
    call.current = Some("task-call");
    call.actor = Some("agent-screener");
    call.card = Some("card-fraud");
    call.body = Some(r#"{"jsonrpc":"2.0","result":{"status":{"state":"failed"}}}"#);
    call.task_state = Some("failed");
    insert(&pool, tenant, "root-fail", call).await;
    let mut f = step("f-1", s(2), "llm", Some(FRAUD_SYS));
    f.depth = 1;
    f.parent = Some("task-call");
    f.current = Some("task-f1");
    f.success = false;
    f.status = 500;
    insert(&pool, tenant, "root-fail", f).await;

    finalize_run(
        &pool,
        tenant,
        "root-fail",
        finalized_by::EXPLICIT,
        None,
        None,
    )
    .await
    .unwrap();
    let segs = segments(&pool, tenant, "root-fail").await;
    assert_eq!(segs.len(), 1);
    assert_eq!(segs[0].outcome, "failed");
    assert_eq!(segs[0].a2a_task_state.as_deref(), Some("failed"));
    assert_eq!(segs[0].error_count, 1);
    assert_conserved(&pool, tenant, "root-fail").await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_delegate_that_never_answered_is_a_failed_segment_via_its_declared_edge(pool: PgPool) {
    let tenant = "t-seg-down";
    setup(&pool, tenant).await;
    sqlx::query(
        "INSERT INTO agent_cards (id, tenant_id, name, card_json) \
         VALUES ('card-fraud', $1, 'fraud', '{}')",
    )
    .bind(tenant)
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO ai_system_dependencies \
         (id, tenant_id, ai_system_id, role, depends_on_system_id, agent_card_id) \
         VALUES ('dep-1', $1, $2, 'shared_service', $3, 'card-fraud')",
    )
    .bind(tenant)
    .bind(ROOT_SYS)
    .bind(FRAUD_SYS)
    .execute(&pool)
    .await
    .unwrap();

    let t = crate::common::db_now() - Duration::minutes(10);
    let mut a = step("s-1", t, "llm", Some(ROOT_SYS));
    a.current = Some("task-1");
    insert(&pool, tenant, "root-down", a).await;
    // The screener is down: connection refused, no step of its own.
    let mut call = step("s-2", t + Duration::seconds(1), "a2a", Some(ROOT_SYS));
    call.subtype = Some("outbound");
    call.current = Some("task-call");
    call.actor = Some("agent-screener");
    call.card = Some("card-fraud");
    call.success = false;
    call.status = 502;
    call.error = Some("Remote agent request failed: connection refused");
    insert(&pool, tenant, "root-down", call).await;

    finalize_run(
        &pool,
        tenant,
        "root-down",
        finalized_by::EXPLICIT,
        None,
        None,
    )
    .await
    .unwrap();
    let segs = segments(&pool, tenant, "root-down").await;
    assert_eq!(
        segs.len(),
        1,
        "the unanswered call is the screener's segment"
    );
    let s = &segs[0];
    assert_eq!(s.segment_system_id, FRAUD_SYS);
    assert_eq!(s.outcome, "failed");
    assert_eq!((s.action_count, s.total_tokens), (0, 0));
    assert_eq!(s.cost_usd, "0.00000000");
    assert_eq!(s.min_depth, 1);
    assert!(s.tool_set_signature.is_none(), "no steps, no tool set");
    assert_eq!(via_of(&pool, tenant, "root-down").await, json!([FRAUD_SYS]));
    assert_conserved(&pool, tenant, "root-down").await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_reopen_rewrites_the_segments(pool: PgPool) {
    let tenant = "t-seg-reopen";
    setup(&pool, tenant).await;
    seed_system(&pool, tenant, "sys-kyc").await;
    seed_contract(&pool, tenant, "ctr-fraud-v1", FRAUD_SYS, 1).await;
    let t = crate::common::db_now() - Duration::minutes(10);
    seed_delegation_run(&pool, tenant, "root-re", t).await;

    finalize_run(&pool, tenant, "root-re", finalized_by::IDLE, None, None)
        .await
        .unwrap();
    let before = segments(&pool, tenant, "root-re").await;
    assert_eq!(before.len(), 1);

    // The screener promotes v2, then late steps land: one more screener step,
    // and a call into a THIRD system that was not in the run before.
    retire_contracts(&pool, FRAUD_SYS).await;
    seed_contract(&pool, tenant, "ctr-fraud-v2", FRAUD_SYS, 2).await;
    seed_contract(&pool, tenant, "ctr-kyc-v1", "sys-kyc", 1).await;
    let mut late = step("f-4", t + Duration::seconds(45), "llm", Some(FRAUD_SYS));
    late.depth = 1;
    late.parent = Some("task-call");
    late.current = Some("task-f4");
    late.tokens = 50;
    late.cost = Some(0.001);
    insert(&pool, tenant, "root-re", late).await;
    let mut call = step("s-5", t + Duration::seconds(50), "a2a", Some(ROOT_SYS));
    call.subtype = Some("outbound");
    call.current = Some("task-kyc-call");
    call.actor = Some("agent-screener");
    insert(&pool, tenant, "root-re", call).await;
    let mut k = step("k-1", t + Duration::seconds(51), "llm", Some("sys-kyc"));
    k.depth = 1;
    k.parent = Some("task-kyc-call");
    k.current = Some("task-k1");
    insert(&pool, tenant, "root-re", k).await;

    let run = finalize_run(&pool, tenant, "root-re", finalized_by::IDLE, None, None)
        .await
        .unwrap()
        .unwrap();
    assert!(run.reopened);
    let after = segments(&pool, tenant, "root-re").await;
    assert_eq!(after.len(), 2, "rewritten, not appended: {after:?}");
    assert!(
        after.iter().all(|s| s.id != before[0].id),
        "the old rows are deleted, not updated in place"
    );
    let fraud = after
        .iter()
        .find(|s| s.segment_system_id == FRAUD_SYS)
        .unwrap();
    assert_eq!(fraud.action_count, 4);
    assert_eq!(fraud.total_tokens, 550);
    assert_eq!(fraud.last_action_at, t + Duration::seconds(45));
    // First finalisation wins the stamp, as it does for the run itself.
    assert_eq!(fraud.contract_id.as_deref(), Some("ctr-fraud-v1"));
    let kyc = after
        .iter()
        .find(|s| s.segment_system_id == "sys-kyc")
        .unwrap();
    assert_eq!(kyc.action_count, 1);
    assert_eq!(kyc.contract_id.as_deref(), Some("ctr-kyc-v1"));
    assert_eq!(
        via_of(&pool, tenant, "root-re").await,
        json!([FRAUD_SYS, "sys-kyc"])
    );
    assert_conserved(&pool, tenant, "root-re").await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_replay_run_writes_no_segments(pool: PgPool) {
    let tenant = "t-seg-replay";
    setup(&pool, tenant).await;
    let t = crate::common::db_now() - Duration::minutes(10);
    seed_delegation_run(&pool, tenant, "root-rp", t).await;
    finalize_run(&pool, tenant, "root-rp", finalized_by::EXPLICIT, None, None)
        .await
        .unwrap();
    assert_eq!(segments(&pool, tenant, "root-rp").await.len(), 1);

    sqlx::query("UPDATE ai_system_runs SET is_replay = true WHERE root_task_id = 'root-rp'")
        .execute(&pool)
        .await
        .unwrap();
    finalize_run(&pool, tenant, "root-rp", finalized_by::EXPLICIT, None, None)
        .await
        .unwrap();
    let (n,): (i64,) =
        sqlx::query_as("SELECT COUNT(*) FROM ai_system_run_segments WHERE tenant_id = $1")
            .bind(tenant)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(
        n, 0,
        "a replay run never feeds a baseline, so it has no segments"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn every_step_has_one_home_in_a_messy_run(pool: PgPool) {
    // Two delegates (one nested under the other), a step with no group, a
    // group under no AI System, and costs that do not round cleanly.
    let tenant = "t-seg-messy";
    setup(&pool, tenant).await;
    seed_system(&pool, tenant, "sys-kyc").await;
    common::seed_resource_group(&pool, "rg-orphan", tenant).await;
    let t = crate::common::db_now() - Duration::minutes(10);
    let s = |sec: i64| t + Duration::seconds(sec);
    let costs = [
        0.1234567891,
        0.000000004,
        0.3333333333,
        0.0000000051,
        0.7777777777,
    ];
    let groups: [Option<&str>; 7] = [
        Some(ROOT_SYS),
        None,
        Some("rg-orphan"),
        Some(FRAUD_SYS),
        Some("sys-kyc"),
        Some(FRAUD_SYS),
        Some(ROOT_SYS),
    ];
    let ids = ["m0", "m1", "m2", "m3", "m4", "m5", "m6"];
    let currents = ["c0", "c1", "c2", "c3", "c4", "c5", "c6"];
    for (i, g) in groups.iter().enumerate() {
        let mut st = step(ids[i], s(i as i64), "llm", *g);
        st.current = Some(currents[i]);
        st.tokens = 10 * (i as i32 + 1);
        st.cost = Some(costs[i % costs.len()]);
        match i {
            3 | 5 => {
                st.depth = 1;
                st.parent = Some("c0");
            }
            4 => {
                st.depth = 2;
                st.parent = Some("c3");
            }
            _ => {}
        }
        insert(&pool, tenant, "root-messy", st).await;
    }
    let run = finalize_run(
        &pool,
        tenant,
        "root-messy",
        finalized_by::EXPLICIT,
        None,
        None,
    )
    .await
    .unwrap()
    .unwrap();
    assert_eq!(run.action_count, 7);
    let segs = segments(&pool, tenant, "root-messy").await;
    let by: Vec<(&str, i32, i16)> = segs
        .iter()
        .map(|s| (s.segment_system_id.as_str(), s.action_count, s.min_depth))
        .collect();
    assert_eq!(by, vec![(FRAUD_SYS, 2, 1), ("sys-kyc", 1, 2)]);
    // The nested delegate was entered by the fraud screener, not the root.
    assert_eq!(segs[1].outcome, "answered");
    assert_conserved(&pool, tenant, "root-messy").await;
}

/// The segment's outcome and state come from
/// `proxy_logs.a2a_task_state`, the gateway's one decision, not from a
/// re-parse of the stored body (which retention may have pruned or which a
/// redaction may have rewritten).
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_segment_reads_the_stored_task_state_not_the_body(pool: PgPool) {
    let tenant = "t-seg-state";
    setup(&pool, tenant).await;
    let t = crate::common::db_now() - Duration::minutes(10);
    let s = |sec: i64| t + Duration::seconds(sec);

    let mut a = step("s-1", s(0), "llm", Some(ROOT_SYS));
    a.current = Some("task-1");
    insert(&pool, tenant, "root-state", a).await;
    let mut call = step("s-2", s(1), "a2a", Some(ROOT_SYS));
    call.subtype = Some("outbound");
    call.current = Some("task-call");
    call.card = Some("card-fraud");
    // The stored body disagrees with the column on purpose.
    call.body = Some(r#"{"task":{"status":{"state":"TASK_STATE_COMPLETED"}}}"#);
    call.task_state = Some("rejected");
    insert(&pool, tenant, "root-state", call).await;
    let mut f = step("f-1", s(2), "llm", Some(FRAUD_SYS));
    f.depth = 1;
    f.parent = Some("task-call");
    f.current = Some("task-f1");
    insert(&pool, tenant, "root-state", f).await;

    finalize_run(
        &pool,
        tenant,
        "root-state",
        finalized_by::EXPLICIT,
        None,
        None,
    )
    .await
    .unwrap();
    let segs = segments(&pool, tenant, "root-state").await;
    assert_eq!(segs.len(), 1);
    assert_eq!(segs[0].a2a_task_state.as_deref(), Some("rejected"));
    assert_eq!(segs[0].outcome, "rejected");
    assert_conserved(&pool, tenant, "root-state").await;
}

/// A call the accepted-caller gate refused never reached
/// the service: no child steps, its own subtype on the outbound route. The
/// ledger still counts it as the delegation the caller attempted — a
/// `blocked` segment of the service through the caller's declared edge —
/// and the run counts the refusal like every other gateway refusal.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unaccepted_caller_refusal_is_a_blocked_segment(pool: PgPool) {
    let tenant = "t-seg-unaccepted";
    setup(&pool, tenant).await;
    sqlx::query(
        "INSERT INTO agent_cards (id, tenant_id, name, card_json) \
         VALUES ('card-fraud', $1, 'fraud', '{}')",
    )
    .bind(tenant)
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO ai_system_dependencies \
         (id, tenant_id, ai_system_id, role, depends_on_system_id, agent_card_id) \
         VALUES ('dep-1', $1, $2, 'shared_service', $3, 'card-fraud')",
    )
    .bind(tenant)
    .bind(ROOT_SYS)
    .bind(FRAUD_SYS)
    .execute(&pool)
    .await
    .unwrap();

    let t = crate::common::db_now() - Duration::minutes(10);
    let mut a = step("s-1", t, "llm", Some(ROOT_SYS));
    a.current = Some("task-1");
    insert(&pool, tenant, "root-unacc", a).await;
    let mut refused = step("s-2", t + Duration::seconds(1), "a2a", Some(ROOT_SYS));
    refused.subtype = Some("unaccepted_caller");
    refused.current = Some("task-call");
    refused.actor = Some("agent-screener");
    refused.card = Some("card-fraud");
    refused.success = false;
    refused.status = 403;
    refused.error = Some("unaccepted_caller: AI System 'sys-screening' is not an accepted caller");
    insert(&pool, tenant, "root-unacc", refused).await;

    finalize_run(
        &pool,
        tenant,
        "root-unacc",
        finalized_by::EXPLICIT,
        None,
        None,
    )
    .await
    .unwrap();
    let segs = segments(&pool, tenant, "root-unacc").await;
    assert_eq!(
        segs.len(),
        1,
        "the refused delegation is the service's segment"
    );
    let seg = &segs[0];
    assert_eq!(seg.segment_system_id, FRAUD_SYS);
    // `blocked` is gone: a refusal is `failed` with the segment
    // error cause `blocked_by_policy` — the gateway's, not the service's.
    assert_eq!(seg.outcome, "failed");
    let (cause,): (Option<String>,) = sqlx::query_as(
        "SELECT error_cause FROM ai_system_run_segments WHERE tenant_id = $1 AND root_task_id = 'root-unacc'",
    )
    .bind(tenant)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(
        cause.as_deref(),
        Some("blocked_by_policy"),
        "a gateway refusal, not a service failure"
    );
    assert_eq!((seg.action_count, seg.total_tokens), (0, 0));
    assert_eq!(seg.caller_agent_id.as_deref(), Some("agent-screener"));
    assert!(seg.a2a_task_state.is_none());
    // The run counts the refusal exactly as it counts a run-cap refusal:
    // a step that did not succeed, held by the caller's own share.
    let (errors,): (i32,) = sqlx::query_as(
        "SELECT error_count FROM ai_system_runs WHERE tenant_id = $1 AND root_task_id = 'root-unacc'",
    )
    .bind(tenant)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(errors, 1);
    assert_conserved(&pool, tenant, "root-unacc").await;
}

/// The caller's seeded dependency on the fraud screener's card, so a call no
/// step answered still resolves to the screener's segment.
async fn seed_fraud_edge(pool: &PgPool, tenant: &str) {
    sqlx::query(
        "INSERT INTO agent_cards (id, tenant_id, name, card_json) \
         VALUES ('card-fraud', $1, 'fraud', '{}')",
    )
    .bind(tenant)
    .execute(pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO ai_system_dependencies \
         (id, tenant_id, ai_system_id, role, depends_on_system_id, agent_card_id) \
         VALUES ('dep-1', $1, $2, 'shared_service', $3, 'card-fraud')",
    )
    .bind(tenant)
    .bind(ROOT_SYS)
    .bind(FRAUD_SYS)
    .execute(pool)
    .await
    .unwrap();
}

async fn held_and_cause(
    pool: &PgPool,
    tenant: &str,
    root: &str,
) -> (Option<String>, Option<String>) {
    sqlx::query_as(
        "SELECT held_approval_request_id, error_cause FROM ai_system_run_segments \
          WHERE tenant_id = $1 AND root_task_id = $2",
    )
    .bind(tenant)
    .bind(root)
    .fetch_one(pool)
    .await
    .unwrap()
}

/// The caller's delegation was held by the gateway
/// for a person and nobody has answered yet. It is the service's segment,
/// stored as the gateway stop it is (`failed` + `blocked_by_policy`, never
/// the service's failure), and it names the approval request, so the
/// connection line counts it as a hold and judges it by that request.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_held_delegation_is_the_services_segment_and_names_its_hold(pool: PgPool) {
    let tenant = "t-seg-held";
    setup(&pool, tenant).await;
    seed_fraud_edge(&pool, tenant).await;

    let t = crate::common::db_now() - Duration::minutes(10);
    let mut a = step("s-1", t, "llm", Some(ROOT_SYS));
    a.current = Some("task-1");
    insert(&pool, tenant, "root-held", a).await;
    let mut held = step("s-2", t + Duration::seconds(1), "a2a", Some(ROOT_SYS));
    held.subtype = Some("approval_required");
    held.current = Some("task-call");
    held.actor = Some("agent-screener");
    held.card = Some("card-fraud");
    held.success = false;
    held.status = 202;
    held.error = Some("approval_required");
    held.approval = Some("apr-held");
    insert(&pool, tenant, "root-held", held).await;

    finalize_run(
        &pool,
        tenant,
        "root-held",
        finalized_by::EXPLICIT,
        None,
        None,
    )
    .await
    .unwrap();
    let segs = segments(&pool, tenant, "root-held").await;
    assert_eq!(
        segs.len(),
        1,
        "the held delegation is the service's segment"
    );
    assert_eq!(segs[0].segment_system_id, FRAUD_SYS);
    assert_eq!(segs[0].outcome, "failed");
    let (held_id, cause) = held_and_cause(&pool, tenant, "root-held").await;
    assert_eq!(held_id.as_deref(), Some("apr-held"));
    assert_eq!(cause.as_deref(), Some("blocked_by_policy"));
    assert_conserved(&pool, tenant, "root-held").await;
}

/// The service's gateway held the caller's call on its INBOUND side (the
/// caller agent's `a2a_call` grant needs a person) and answered the caller
/// with a refusal. That is a hold, not the service failing to answer.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_hold_on_the_services_inbound_side_is_a_hold_not_a_failure(pool: PgPool) {
    let tenant = "t-seg-inbound-held";
    setup(&pool, tenant).await;
    seed_fraud_edge(&pool, tenant).await;

    let t = crate::common::db_now() - Duration::minutes(10);
    let mut a = step("s-1", t, "llm", Some(ROOT_SYS));
    a.current = Some("task-1");
    insert(&pool, tenant, "root-in-held", a).await;
    // The service side: the inbound call, held, a step of the caller's run.
    // As the A2A v1 route writes it: an `a2a` row on the inbound endpoint.
    let mut inbound = step("s-2", t + Duration::seconds(1), "a2a", Some(FRAUD_SYS));
    inbound.endpoint = Some("/a2a/v1");
    inbound.subtype = Some("approval_required");
    inbound.parent = Some("task-call");
    inbound.current = Some("task-svc");
    inbound.depth = 1;
    inbound.success = false;
    inbound.status = 403;
    inbound.approval = Some("apr-inbound");
    insert(&pool, tenant, "root-in-held", inbound).await;
    // What the caller received: the refusal the service's gateway returned.
    let mut call = step("s-3", t + Duration::seconds(2), "a2a", Some(ROOT_SYS));
    call.subtype = Some("outbound");
    call.current = Some("task-call");
    call.actor = Some("agent-screener");
    call.card = Some("card-fraud");
    call.success = false;
    call.status = 403;
    call.error = Some("PolicyBlocked: approval_required");
    insert(&pool, tenant, "root-in-held", call).await;

    finalize_run(
        &pool,
        tenant,
        "root-in-held",
        finalized_by::EXPLICIT,
        None,
        None,
    )
    .await
    .unwrap();
    let segs = segments(&pool, tenant, "root-in-held").await;
    assert_eq!(segs.len(), 1);
    assert_eq!(
        (segs[0].segment_system_id.as_str(), segs[0].outcome.as_str()),
        (FRAUD_SYS, "failed")
    );
    let (held_id, cause) = held_and_cause(&pool, tenant, "root-in-held").await;
    assert_eq!(held_id.as_deref(), Some("apr-inbound"));
    assert_eq!(
        cause.as_deref(),
        Some("blocked_by_policy"),
        "a hold, not agent_unreachable"
    );
    assert_conserved(&pool, tenant, "root-in-held").await;
}

/// A hold that a person approved, followed by the retry that answered: the
/// answer decides, and no hold is recorded.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_approved_and_answered_delegation_records_no_hold(pool: PgPool) {
    let tenant = "t-seg-held-ok";
    setup(&pool, tenant).await;
    seed_fraud_edge(&pool, tenant).await;

    let t = crate::common::db_now() - Duration::minutes(10);
    let mut held = step("s-1", t, "a2a", Some(ROOT_SYS));
    held.subtype = Some("approval_required");
    held.current = Some("task-held");
    held.card = Some("card-fraud");
    held.success = false;
    held.status = 202;
    held.approval = Some("apr-ok");
    insert(&pool, tenant, "root-held-ok", held).await;
    let mut retry = step("s-2", t + Duration::seconds(5), "a2a", Some(ROOT_SYS));
    retry.subtype = Some("outbound");
    retry.current = Some("task-retry");
    retry.card = Some("card-fraud");
    retry.task_state = Some("completed");
    insert(&pool, tenant, "root-held-ok", retry).await;
    let mut answer = step("s-3", t + Duration::seconds(4), "llm", Some(FRAUD_SYS));
    answer.parent = Some("task-retry");
    answer.depth = 1;
    insert(&pool, tenant, "root-held-ok", answer).await;

    finalize_run(
        &pool,
        tenant,
        "root-held-ok",
        finalized_by::EXPLICIT,
        None,
        None,
    )
    .await
    .unwrap();
    let segs = segments(&pool, tenant, "root-held-ok").await;
    assert_eq!(segs.len(), 1);
    assert_eq!(segs[0].outcome, "answered");
    // (Its cause is `delegate_deviated`: the answer declared no
    // outcome. Not a hold either way.)
    assert_eq!(held_and_cause(&pool, tenant, "root-held-ok").await.0, None);
    assert_conserved(&pool, tenant, "root-held-ok").await;
}

/// `caller_system_id` is the IMMEDIATE calling AI System. In a run
/// A→B→C rooted at A, C's segment names B as its caller while
/// `root_system_id` stays A.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_segments_caller_is_the_immediate_calling_system(pool: PgPool) {
    let tenant = "t-seg-caller";
    setup(&pool, tenant).await;
    seed_system(&pool, tenant, "sys-kyc").await;
    let t = crate::common::db_now() - Duration::minutes(10);
    let s = |sec: i64| t + Duration::seconds(sec);

    let mut a = step("s-1", s(0), "llm", Some(ROOT_SYS));
    a.current = Some("task-1");
    insert(&pool, tenant, "root-abc", a).await;
    let mut ab = step("s-2", s(1), "a2a", Some(ROOT_SYS));
    ab.subtype = Some("outbound");
    ab.current = Some("task-ab");
    insert(&pool, tenant, "root-abc", ab).await;
    let mut bc = step("f-1", s(2), "a2a", Some(FRAUD_SYS));
    bc.subtype = Some("outbound");
    bc.depth = 1;
    bc.parent = Some("task-ab");
    bc.current = Some("task-bc");
    insert(&pool, tenant, "root-abc", bc).await;
    let mut c = step("k-1", s(3), "llm", Some("sys-kyc"));
    c.depth = 2;
    c.parent = Some("task-bc");
    c.current = Some("task-k1");
    insert(&pool, tenant, "root-abc", c).await;

    finalize_run(
        &pool,
        tenant,
        "root-abc",
        finalized_by::EXPLICIT,
        None,
        None,
    )
    .await
    .unwrap();
    let segs = segments(&pool, tenant, "root-abc").await;
    let kyc = segs
        .iter()
        .find(|x| x.segment_system_id == "sys-kyc")
        .expect("C's segment");
    let fraud = segs
        .iter()
        .find(|x| x.segment_system_id == FRAUD_SYS)
        .expect("B's segment");
    assert_eq!(kyc.caller_system_id.as_deref(), Some(FRAUD_SYS));
    assert_eq!(kyc.root_system_id.as_deref(), Some(ROOT_SYS));
    assert_eq!(fraud.caller_system_id.as_deref(), Some(ROOT_SYS));
    assert_conserved(&pool, tenant, "root-abc").await;
}
