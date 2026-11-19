// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The run ledger against a real Postgres.
//!
//! The unit tests in `services::run_ledger` cover the terminal-state and
//! chain-integrity decisions, which are pure. Everything else — the
//! aggregation SQL, the upsert, attribution, the idle sweeper's candidate
//! query — is SQL, and SQL is only really tested against a database.
//!
//! The aggregation is the single largest statement in the codebase, and a
//! silent mistake in it does not crash anything: it produces a number that
//! is merely *wrong*, on a dashboard someone is about to make a decision
//! from. So the assertions here are hand-computed rather than
//! self-referential.

use chrono::Duration;
use sqlx::PgPool;

use brutor_gateway_core::services::run_ledger::{
    aggregate_run, chain, finalize_run, finalized_by, held_ms_between, sweep_idle_runs, terminal,
};

use super::common;

#[allow(clippy::too_many_arguments)]
struct Hop<'a> {
    root: &'a str,
    proxy_type: &'a str,
    seconds_ago: i64,
    success: bool,
    group_id: Option<&'a str>,
    tool: Option<&'a str>,
    prompt_tokens: Option<i32>,
    completion_tokens: Option<i32>,
    cost: Option<f64>,
    guardrail_blocked: bool,
    policy_denied: bool,
    fallback: bool,
    continuity: Option<&'a str>,
    error_type: Option<&'a str>,
    depth: i32,
    model: Option<&'a str>,
    /// Client-asserted turn id (X-Brutor-Turn-Id).
    turn_id: Option<&'a str>,
    /// proxy_subtype — 'skill_run' marks a skill executed through the skills
    /// MCP system server (the documented path), which the ledger must count
    /// as a skill call, not a tool call.
    subtype: Option<&'a str>,
    /// `proxy_logs.duration_ms` (stored as text so 'NaN' can be written).
    /// A row is stamped when its call finishes: the step ran over
    /// `[timestamp - duration, timestamp]`.
    duration_ms: Option<&'a str>,
}

impl<'a> Hop<'a> {
    fn new(root: &'a str, proxy_type: &'a str, seconds_ago: i64) -> Self {
        Self {
            root,
            proxy_type,
            seconds_ago,
            success: true,
            group_id: None,
            tool: None,
            prompt_tokens: None,
            completion_tokens: None,
            cost: None,
            guardrail_blocked: false,
            policy_denied: false,
            fallback: false,
            continuity: Some("minted"),
            error_type: None,
            depth: 0,
            model: None,
            turn_id: None,
            subtype: None,
            duration_ms: None,
        }
    }
}

async fn insert_hop(pool: &PgPool, tenant: &str, id: &str, h: Hop<'_>) {
    let ts = crate::common::db_now() - Duration::seconds(h.seconds_ago);
    sqlx::query(
        "INSERT INTO proxy_logs (
            id, tenant_id, timestamp, proxy_type, end_user_session_id,
            http_method, endpoint, success, root_task_id, delegation_depth,
            resource_group_id, mcp_capability_name, llm_prompt_tokens,
            llm_completion_tokens, llm_total_tokens, llm_estimated_cost_usd,
            guardrail_blocked, argument_policy_denied, pii_detected,
            llm_used_fallback, trace_continuity, error_type, llm_model_used,
            request_size_bytes, response_size_bytes, is_auth_event,
            turn_id, proxy_subtype, duration_ms, llm_context_tokens
         ) VALUES (
            $1, $2, $3, $4, 'sess', 'POST', '/x', $5, $6, $7,
            $8, $9, $10, $11, $12, $13, $14, $15, false, $16, $17, $18, $19,
            100, 200, false, $20, $21, CAST($22 AS DOUBLE PRECISION), $10
         )",
    )
    .bind(id)
    .bind(tenant)
    .bind(ts)
    .bind(h.proxy_type)
    .bind(h.success)
    .bind(h.root)
    .bind(h.depth)
    .bind(h.group_id)
    .bind(h.tool)
    .bind(h.prompt_tokens)
    .bind(h.completion_tokens)
    .bind(h.prompt_tokens.unwrap_or(0) + h.completion_tokens.unwrap_or(0))
    .bind(h.cost)
    .bind(h.guardrail_blocked)
    .bind(h.policy_denied)
    .bind(h.fallback)
    .bind(h.continuity)
    .bind(h.error_type)
    .bind(h.model)
    .bind(h.turn_id)
    .bind(h.subtype)
    .bind(h.duration_ms)
    .execute(pool)
    .await
    .expect("insert_hop");
}

async fn seed_system(pool: &PgPool, tenant: &str, id: &str, kind: &str, idle: Option<i32>) {
    sqlx::query(
        "INSERT INTO resource_groups (id, tenant_id, hierarchy_level, inherit_from_parent, \
         name, is_active, is_system, group_type, system_kind, run_idle_timeout_seconds) \
         VALUES ($1, $2, 0, true, $1, true, false, 'AI_SYSTEM', $3, $4) \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant)
    .bind(kind)
    .bind(idle)
    .execute(pool)
    .await
    .expect("seed_system");
}

// ---------------------------------------------------------------------------
// Aggregation
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn aggregates_a_multi_hop_run(pool: PgPool) {
    let t = "t-rl-agg";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent", None).await;

    // A realistic agent trajectory: think, call two tools, think, answer.
    let mut h = Hop::new("root-1", "llm", 60);
    h.group_id = Some("sys");
    h.prompt_tokens = Some(100);
    h.completion_tokens = Some(20);
    h.cost = Some(0.001);
    h.model = Some("gpt-x");
    insert_hop(&pool, t, "h1", h).await;

    let mut h = Hop::new("root-1", "mcp", 50);
    h.group_id = Some("sys");
    h.tool = Some("search");
    insert_hop(&pool, t, "h2", h).await;

    let mut h = Hop::new("root-1", "mcp", 40);
    h.group_id = Some("sys");
    h.tool = Some("fetch");
    h.depth = 1;
    insert_hop(&pool, t, "h3", h).await;

    let mut h = Hop::new("root-1", "llm", 30);
    h.group_id = Some("sys");
    h.prompt_tokens = Some(400);
    h.completion_tokens = Some(80);
    h.cost = Some(0.004);
    h.model = Some("gpt-x");
    insert_hop(&pool, t, "h4", h).await;

    let agg = aggregate_run(&pool, t, "root-1")
        .await
        .expect("aggregate")
        .expect("run exists");

    assert_eq!(agg.action_count, 4);
    assert_eq!(agg.llm_call_count, 2);
    assert_eq!(agg.tool_call_count, 2);
    assert_eq!(agg.skill_call_count, 0);
    assert_eq!(agg.distinct_tools, 2);
    assert_eq!(agg.max_delegation_depth, 1);
    assert_eq!(agg.prompt_tokens, 500);
    assert_eq!(agg.completion_tokens, 100);
    assert_eq!(agg.total_tokens, 600);
    assert!((agg.total_cost_usd - 0.005).abs() < 1e-9);
    // The *first* LLM hop's prompt — the task as posed, not the
    // accumulated context of the second call.
    assert_eq!(agg.first_prompt_tokens, Some(100));
    assert_eq!(agg.input_bytes, 400); // 4 hops × 100
    assert_eq!(agg.output_bytes, 800);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn skill_runs_on_the_mcp_path_count_as_skill_calls(pool: PgPool) {
    // A skill reaches the runner on two surfaces: the deprecated REST route
    // writes proxy_type 'skill'; the progressive-disclosure path agents are
    // told to use writes an 'mcp' row on the skills system server with
    // proxy_subtype 'skill_run'. Both are skill calls and neither is a tool
    // call. Otherwise a system built the documented way reads
    // skill_call_count = 0 and tool_call_count one too high on every run.
    let t = "t-rl-skill";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent", None).await;

    let mut h = Hop::new("root-s", "mcp", 50);
    h.group_id = Some("sys");
    h.tool = Some("crm_lookup");
    insert_hop(&pool, t, "s1", h).await;

    let mut h = Hop::new("root-s", "mcp", 40);
    h.group_id = Some("sys");
    h.tool = Some("skills__run_script");
    h.subtype = Some("skill_run");
    insert_hop(&pool, t, "s2", h).await;

    let mut h = Hop::new("root-s", "skill", 30);
    h.group_id = Some("sys");
    h.tool = Some("legacy-skill");
    insert_hop(&pool, t, "s3", h).await;

    let agg = aggregate_run(&pool, t, "root-s")
        .await
        .expect("aggregate")
        .expect("run exists");

    assert_eq!(agg.action_count, 3);
    assert_eq!(agg.tool_call_count, 1, "only the real tool call");
    assert_eq!(agg.skill_call_count, 2, "REST skill + MCP-path skill_run");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_approval_hold_is_a_request_not_an_error(pool: PgPool) {
    // A call held for a human answers 202 and is logged success = false with
    // proxy_subtype 'approval_required'. It is governance working. Counted
    // as a tool error, every held decision would make
    // reliability read 50% and a critical tool_error_rate drift
    // finding downgrade the system's autonomy, on a system whose tools never
    // failed. The hold counts once, as an approval request; a real failure in
    // the same run still counts as an error.
    let t = "t-rl-hold";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent", None).await;

    let mut h = Hop::new("root-h", "mcp", 50);
    h.group_id = Some("sys");
    h.tool = Some("applications_get");
    insert_hop(&pool, t, "h1", h).await;

    let mut h = Hop::new("root-h", "mcp", 40);
    h.group_id = Some("sys");
    h.tool = Some("applications_set_recommendation");
    h.success = false;
    h.error_type = Some("proxy_error");
    h.subtype = Some("approval_required");
    insert_hop(&pool, t, "h2", h).await;

    let mut h = Hop::new("root-h", "mcp", 30);
    h.group_id = Some("sys");
    h.tool = Some("bureau_get_report");
    h.success = false;
    h.error_type = Some("upstream_error");
    insert_hop(&pool, t, "h3", h).await;

    let agg = aggregate_run(&pool, t, "root-h")
        .await
        .expect("aggregate")
        .expect("run exists");

    assert_eq!(
        agg.approval_request_count, 1,
        "the hold is an approval request"
    );
    assert_eq!(agg.tool_error_count, 1, "only the real upstream failure");
    assert_eq!(agg.error_count, 1, "a hold is not an error");
    let types = agg.tool_error_types.expect("one real error type");
    assert_eq!(
        types.get("upstream_error").and_then(|v| v.as_i64()),
        Some(1)
    );
    assert!(
        types.get("proxy_error").is_none(),
        "the hold is not an error type"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn tool_signatures_separate_order_from_membership(pool: PgPool) {
    let t = "t-rl-sig";
    common::seed_tenant(&pool, t).await;

    // Same two tools, opposite order.
    for (root, first, second) in [("r-a", "alpha", "beta"), ("r-b", "beta", "alpha")] {
        let mut h = Hop::new(root, "mcp", 60);
        h.tool = Some(first);
        insert_hop(&pool, t, &format!("{root}-1"), h).await;
        let mut h = Hop::new(root, "mcp", 50);
        h.tool = Some(second);
        insert_hop(&pool, t, &format!("{root}-2"), h).await;
    }

    let a = aggregate_run(&pool, t, "r-a").await.unwrap().unwrap();
    let b = aggregate_run(&pool, t, "r-b").await.unwrap().unwrap();

    assert_ne!(
        a.tool_signature, b.tool_signature,
        "ordered signature must distinguish trajectories"
    );
    assert_eq!(
        a.tool_set_signature, b.tool_set_signature,
        "unordered signature must see the same tool set — this is what \
         separates 'uses different tools' from 'uses them in another order'"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn counts_governance_outcomes_and_tool_errors(pool: PgPool) {
    let t = "t-rl-gov";
    common::seed_tenant(&pool, t).await;

    let mut h = Hop::new("r", "mcp", 60);
    h.success = false;
    h.error_type = Some("timeout");
    insert_hop(&pool, t, "g1", h).await;

    let mut h = Hop::new("r", "mcp", 55);
    h.success = false;
    h.error_type = Some("timeout");
    insert_hop(&pool, t, "g2", h).await;

    let mut h = Hop::new("r", "mcp", 50);
    h.success = false;
    h.error_type = Some("upstream_500");
    insert_hop(&pool, t, "g3", h).await;

    let mut h = Hop::new("r", "llm", 45);
    h.guardrail_blocked = true;
    insert_hop(&pool, t, "g4", h).await;

    let mut h = Hop::new("r", "llm", 40);
    h.policy_denied = true;
    insert_hop(&pool, t, "g5", h).await;

    let agg = aggregate_run(&pool, t, "r").await.unwrap().unwrap();
    assert_eq!(agg.tool_error_count, 3);
    assert_eq!(agg.error_count, 3);
    assert_eq!(agg.guardrail_block_count, 1);
    assert_eq!(agg.policy_denial_count, 1);

    let types = agg.tool_error_types.expect("tool_error_types");
    assert_eq!(types["timeout"], 2);
    assert_eq!(types["upstream_500"], 1);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn models_used_is_rolled_up_per_model(pool: PgPool) {
    let t = "t-rl-models";
    common::seed_tenant(&pool, t).await;

    for (i, m) in ["gpt-x", "gpt-x", "claude-y"].iter().enumerate() {
        let mut h = Hop::new("r", "llm", 60 - i as i64);
        h.model = Some(m);
        insert_hop(&pool, t, &format!("m{i}"), h).await;
    }

    let agg = aggregate_run(&pool, t, "r").await.unwrap().unwrap();
    let models = agg.models_used.expect("models_used");
    let arr = models.as_array().expect("array");
    assert_eq!(arr.len(), 2);
    let gpt = arr.iter().find(|m| m["model"] == "gpt-x").unwrap();
    assert_eq!(gpt["calls"], 2);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn missing_run_aggregates_to_none(pool: PgPool) {
    let t = "t-rl-missing";
    common::seed_tenant(&pool, t).await;
    assert!(aggregate_run(&pool, t, "nope").await.unwrap().is_none());
}

// ---------------------------------------------------------------------------
// Finalization
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn finalizes_and_attributes_to_the_ai_system(pool: PgPool) {
    let t = "t-rl-fin";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent", None).await;

    let mut h = Hop::new("r", "llm", 30);
    h.group_id = Some("sys");
    h.cost = Some(0.02);
    insert_hop(&pool, t, "f1", h).await;

    let run = finalize_run(&pool, t, "r", finalized_by::EXPLICIT, None, None)
        .await
        .unwrap()
        .expect("finalized");

    assert_eq!(run.ai_system_id.as_deref(), Some("sys"));
    assert_eq!(run.terminal_state, terminal::COMPLETED);
    assert_eq!(run.chain_integrity, chain::INTACT);
    assert!(!run.reopened);

    let stored: (String, String, i32, f64) = sqlx::query_as(
        "SELECT terminal_state, finalized_by, action_count, \
                CAST(total_cost_usd AS DOUBLE PRECISION) \
           FROM ai_system_runs WHERE tenant_id = $1 AND root_task_id = $2",
    )
    .bind(t)
    .bind("r")
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(stored.0, terminal::COMPLETED);
    assert_eq!(stored.1, finalized_by::EXPLICIT);
    assert_eq!(stored.2, 1);
    assert!((stored.3 - 0.02).abs() < 1e-9);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn late_hop_reopens_and_recomputes(pool: PgPool) {
    let t = "t-rl-reopen";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent", None).await;

    let mut h = Hop::new("r", "llm", 60);
    h.group_id = Some("sys");
    h.cost = Some(0.01);
    insert_hop(&pool, t, "l1", h).await;

    let first = finalize_run(&pool, t, "r", finalized_by::IDLE, None, None)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(first.action_count, 1);
    assert!(!first.reopened);

    // A hop arrives after we thought the run was over.
    let mut h = Hop::new("r", "mcp", 5);
    h.group_id = Some("sys");
    h.tool = Some("search");
    insert_hop(&pool, t, "l2", h).await;

    let second = finalize_run(&pool, t, "r", finalized_by::EXPLICIT, None, None)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(second.action_count, 2, "must recompute from all hops");
    assert!(second.reopened);

    // Same run, not a duplicate — the unique key holds and identity is kept.
    let (count, run_id, reopened): (i64, String, i32) = sqlx::query_as(
        "SELECT COUNT(*) OVER (), id, reopened_count FROM ai_system_runs \
         WHERE tenant_id = $1 AND root_task_id = $2 LIMIT 1",
    )
    .bind(t)
    .bind("r")
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(count, 1);
    assert_eq!(
        run_id, first.run_id,
        "run keeps its identity across reopens"
    );
    assert_eq!(reopened, 1);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn terminal_state_reflects_a_blocked_last_hop(pool: PgPool) {
    let t = "t-rl-blocked";
    common::seed_tenant(&pool, t).await;

    let h = Hop::new("r", "llm", 60);
    insert_hop(&pool, t, "b1", h).await;
    let mut h = Hop::new("r", "llm", 30);
    h.guardrail_blocked = true;
    h.success = false;
    insert_hop(&pool, t, "b2", h).await;

    let run = finalize_run(&pool, t, "r", finalized_by::EXPLICIT, None, None)
        .await
        .unwrap()
        .unwrap();
    // A run the gateway stopped is `errored`, and the governance
    // explanation is its error cause, not a terminal state of its own.
    assert_eq!(run.terminal_state, terminal::ERRORED);
    assert_eq!(
        run.error_cause.as_deref(),
        Some("blocked_by_policy"),
        "governance is a more useful explanation than a bare error"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn chain_integrity_flags_a_dropped_link(pool: PgPool) {
    let t = "t-rl-chain";
    common::seed_tenant(&pool, t).await;

    let h = Hop::new("r", "llm", 60);
    insert_hop(&pool, t, "c1", h).await;
    let mut h = Hop::new("r", "mcp", 50);
    h.continuity = Some("client_dropped");
    insert_hop(&pool, t, "c2", h).await;

    let run = finalize_run(&pool, t, "r", finalized_by::EXPLICIT, None, None)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(run.chain_integrity, chain::PARTIAL);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn explicit_exhausted_survives_to_storage(pool: PgPool) {
    // The one state the gateway can never infer. If this round trip
    // breaks, the exhaustion-rate signal silently reads zero forever.
    let t = "t-rl-exhausted";
    common::seed_tenant(&pool, t).await;
    insert_hop(&pool, t, "e1", Hop::new("r", "llm", 30)).await;

    let run = finalize_run(
        &pool,
        t,
        "r",
        finalized_by::EXPLICIT,
        Some(terminal::EXHAUSTED),
        None,
    )
    .await
    .unwrap()
    .unwrap();
    assert_eq!(run.terminal_state, terminal::EXHAUSTED);

    let stored: (String,) = sqlx::query_as(
        "SELECT terminal_state FROM ai_system_runs WHERE tenant_id = $1 AND root_task_id = $2",
    )
    .bind(t)
    .bind("r")
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(stored.0, terminal::EXHAUSTED);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn unattributed_run_is_stored_not_dropped(pool: PgPool) {
    let t = "t-rl-unattr";
    common::seed_tenant(&pool, t).await;
    // No resource group at all — traffic nobody has organised yet.
    insert_hop(&pool, t, "u1", Hop::new("r", "llm", 30)).await;

    let run = finalize_run(&pool, t, "r", finalized_by::IDLE, None, None)
        .await
        .unwrap()
        .unwrap();
    assert!(run.ai_system_id.is_none());

    let (count,): (i64,) = sqlx::query_as(
        "SELECT COUNT(*) FROM ai_system_runs WHERE tenant_id = $1 AND ai_system_id IS NULL",
    )
    .bind(t)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(count, 1, "unattributed runs feed the adoption funnel");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn cross_system_run_records_via_without_double_counting(pool: PgPool) {
    let t = "t-rl-via";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "assistant-sys", "assistant", None).await;
    seed_system(&pool, t, "shared-svc", "service", None).await;

    let mut h = Hop::new("r", "llm", 60);
    h.group_id = Some("assistant-sys");
    h.cost = Some(0.01);
    insert_hop(&pool, t, "v1", h).await;

    let mut h = Hop::new("r", "mcp", 50);
    h.group_id = Some("shared-svc");
    h.tool = Some("lookup");
    insert_hop(&pool, t, "v2", h).await;

    let run = finalize_run(&pool, t, "r", finalized_by::EXPLICIT, None, None)
        .await
        .unwrap()
        .unwrap();

    // Attributed to the root — the owner who gets paged.
    assert_eq!(run.ai_system_id.as_deref(), Some("assistant-sys"));

    let (via,): (serde_json::Value,) = sqlx::query_as(
        "SELECT via_system_ids FROM ai_system_runs WHERE tenant_id = $1 AND root_task_id = $2",
    )
    .bind(t)
    .bind("r")
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(
        via.as_array().unwrap(),
        &vec![serde_json::json!("shared-svc")],
        "the shared service is recorded, not billed a second time"
    );

    // One row, one cost. Splitting into linked runs would have made
    // cost-per-completed-task ambiguous.
    let (rows,): (i64,) =
        sqlx::query_as("SELECT COUNT(*) FROM ai_system_runs WHERE tenant_id = $1")
            .bind(t)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(rows, 1);
}

// ---------------------------------------------------------------------------
// Idle sweeper
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn sweeper_closes_an_idle_run_as_abandoned(pool: PgPool) {
    let t = "t-rl-sweep";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent", None).await;

    // Last hop well past the 120s default.
    let mut h = Hop::new("r", "llm", 600);
    h.group_id = Some("sys");
    insert_hop(&pool, t, "s1", h).await;

    let closed = sweep_idle_runs(&pool, 100).await;
    assert_eq!(closed, 1);

    let (state, by): (String, String) = sqlx::query_as(
        "SELECT terminal_state, finalized_by FROM ai_system_runs \
         WHERE tenant_id = $1 AND root_task_id = $2",
    )
    .bind(t)
    .bind("r")
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(state, terminal::ABANDONED);
    assert_eq!(by, finalized_by::IDLE);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn sweeper_leaves_a_still_active_run_alone(pool: PgPool) {
    let t = "t-rl-sweep-active";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent", None).await;

    let mut h = Hop::new("r", "llm", 5); // well inside the window
    h.group_id = Some("sys");
    insert_hop(&pool, t, "a1", h).await;

    assert_eq!(sweep_idle_runs(&pool, 100).await, 0);
    let (count,): (i64,) =
        sqlx::query_as("SELECT COUNT(*) FROM ai_system_runs WHERE tenant_id = $1")
            .bind(t)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(count, 0, "closing a live run would corrupt its rollup");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn sweeper_honours_a_longer_per_system_window(pool: PgPool) {
    let t = "t-rl-sweep-window";
    common::seed_tenant(&pool, t).await;
    // An integration that legitimately pauses for an hour between hops.
    seed_system(&pool, t, "slow", "integration", Some(3600)).await;

    let mut h = Hop::new("r", "llm", 600); // idle past the default, not past 3600
    h.group_id = Some("slow");
    insert_hop(&pool, t, "w1", h).await;

    assert_eq!(
        sweep_idle_runs(&pool, 100).await,
        0,
        "a system allowed to pause must not be closed on the global default"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn sweeper_reopens_a_run_when_a_late_hop_lands(pool: PgPool) {
    let t = "t-rl-sweep-late";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent", None).await;

    let mut h = Hop::new("r", "llm", 900);
    h.group_id = Some("sys");
    insert_hop(&pool, t, "r1", h).await;
    assert_eq!(sweep_idle_runs(&pool, 100).await, 1);

    // Late hop, itself already idle.
    let mut h = Hop::new("r", "mcp", 300);
    h.group_id = Some("sys");
    h.tool = Some("search");
    insert_hop(&pool, t, "r2", h).await;

    assert_eq!(sweep_idle_runs(&pool, 100).await, 1, "must re-close it");
    let (steps, reopened): (i32, i32) = sqlx::query_as(
        "SELECT action_count, reopened_count FROM ai_system_runs \
         WHERE tenant_id = $1 AND root_task_id = $2",
    )
    .bind(t)
    .bind("r")
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(steps, 2);
    assert_eq!(reopened, 1);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn sweeper_does_not_re_close_a_settled_run(pool: PgPool) {
    let t = "t-rl-sweep-settled";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent", None).await;

    let mut h = Hop::new("r", "llm", 900);
    h.group_id = Some("sys");
    insert_hop(&pool, t, "q1", h).await;

    assert_eq!(sweep_idle_runs(&pool, 100).await, 1);
    assert_eq!(
        sweep_idle_runs(&pool, 100).await,
        0,
        "a settled run must not churn on every tick"
    );

    let (reopened,): (i32,) = sqlx::query_as(
        "SELECT reopened_count FROM ai_system_runs WHERE tenant_id = $1 AND root_task_id = $2",
    )
    .bind(t)
    .bind("r")
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(reopened, 0);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn sweeper_is_tenant_scoped(pool: PgPool) {
    let t1 = "t-rl-sweep-iso1";
    let t2 = "t-rl-sweep-iso2";
    common::seed_tenant(&pool, t1).await;
    common::seed_tenant(&pool, t2).await;

    insert_hop(&pool, t1, "x1", Hop::new("shared-root", "llm", 900)).await;
    insert_hop(&pool, t2, "x2", Hop::new("shared-root", "llm", 900)).await;

    sweep_idle_runs(&pool, 100).await;

    // The same root_task_id in two tenants must produce two independent
    // runs, not one merged rollup.
    let (c1,): (i64,) = sqlx::query_as(
        "SELECT COUNT(*) FROM ai_system_runs WHERE tenant_id = $1 AND root_task_id = 'shared-root'",
    )
    .bind(t1)
    .fetch_one(&pool)
    .await
    .unwrap();
    let (c2,): (i64,) = sqlx::query_as(
        "SELECT COUNT(*) FROM ai_system_runs WHERE tenant_id = $1 AND root_task_id = 'shared-root'",
    )
    .bind(t2)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!((c1, c2), (1, 1));
}

// ---------------------------------------------------------------------------
// Contract stamping
// ---------------------------------------------------------------------------
//
// The whole point of `contract_id` / `contract_hash` on a run is one sentence:
// *"every one of the 14,203 runs this AI System executed in Q3 was governed by
// contract v4 / sha256:9f2c…"*. That sentence is only true if the stamp
// records the contract that was active **when the run happened** — which makes
// the reopen case, not the happy path, the one worth defending.

async fn seed_contract(
    pool: &PgPool,
    tenant: &str,
    id: &str,
    system: &str,
    version: i32,
    hash: &str,
    status: &str,
) {
    sqlx::query(
        "INSERT INTO ai_system_contracts \
         (id, tenant_id, ai_system_id, version, contract_hash, resolved_closure, status) \
         VALUES ($1, $2, $3, $4, $5, '{}'::jsonb, $6)",
    )
    .bind(id)
    .bind(tenant)
    .bind(system)
    .bind(version)
    .bind(hash)
    .bind(status)
    .execute(pool)
    .await
    .expect("seed_contract");
}

async fn stamp_of(pool: &PgPool, tenant: &str, root: &str) -> (Option<String>, Option<String>) {
    sqlx::query_as(
        "SELECT contract_id, contract_hash FROM ai_system_runs \
                    WHERE tenant_id = $1 AND root_task_id = $2",
    )
    .bind(tenant)
    .bind(root)
    .fetch_one(pool)
    .await
    .unwrap()
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_run_is_stamped_with_the_active_contract(pool: PgPool) {
    let t = "t-rl-ctr";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent", None).await;
    seed_contract(&pool, t, "ctr-1", "sys", 1, "sha256:aaa", "active").await;

    let mut h = Hop::new("r", "llm", 30);
    h.group_id = Some("sys");
    insert_hop(&pool, t, "f1", h).await;
    finalize_run(&pool, t, "r", finalized_by::EXPLICIT, None, None)
        .await
        .unwrap()
        .expect("finalized");

    let (id, hash) = stamp_of(&pool, t, "r").await;
    assert_eq!(id.as_deref(), Some("ctr-1"));
    assert_eq!(hash.as_deref(), Some("sha256:aaa"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_draft_contract_does_not_govern_anything(pool: PgPool) {
    // Only a promoted contract describes what actually applied. Stamping a
    // draft would let an unapproved version appear in an audit as though it
    // had been in force.
    let t = "t-rl-ctr-draft";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent", None).await;
    seed_contract(&pool, t, "ctr-1", "sys", 1, "sha256:aaa", "draft").await;

    let mut h = Hop::new("r", "llm", 30);
    h.group_id = Some("sys");
    insert_hop(&pool, t, "f1", h).await;
    finalize_run(&pool, t, "r", finalized_by::EXPLICIT, None, None)
        .await
        .unwrap()
        .unwrap();

    assert_eq!(stamp_of(&pool, t, "r").await, (None, None));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_reopened_run_keeps_the_contract_it_ran_under(pool: PgPool) {
    // The case that makes the audit sentence true or false. A late hop
    // arriving after a promotion must not retro-stamp the run with the new
    // contract, or exactly the runs that straddled the change — the ones an
    // auditor is most interested in — would be attributed to rules that were
    // not yet in force when they executed.
    let t = "t-rl-ctr-reopen";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent", None).await;
    seed_contract(&pool, t, "ctr-1", "sys", 1, "sha256:old", "active").await;

    let mut h = Hop::new("r", "llm", 60);
    h.group_id = Some("sys");
    insert_hop(&pool, t, "l1", h).await;
    finalize_run(&pool, t, "r", finalized_by::IDLE, None, None)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        stamp_of(&pool, t, "r").await.1.as_deref(),
        Some("sha256:old")
    );

    // v2 is promoted while the run is, as far as anyone knew, over.
    sqlx::query("UPDATE ai_system_contracts SET status = 'superseded' WHERE id = 'ctr-1'")
        .execute(&pool)
        .await
        .unwrap();
    seed_contract(&pool, t, "ctr-2", "sys", 2, "sha256:new", "active").await;

    let mut h = Hop::new("r", "mcp", 5);
    h.group_id = Some("sys");
    h.tool = Some("search");
    insert_hop(&pool, t, "l2", h).await;
    let reopened = finalize_run(&pool, t, "r", finalized_by::IDLE, None, None)
        .await
        .unwrap()
        .unwrap();
    assert!(reopened.reopened, "the late hop must reopen the run");

    let (id, hash) = stamp_of(&pool, t, "r").await;
    assert_eq!(
        hash.as_deref(),
        Some("sha256:old"),
        "a run keeps the contract that was in force when it ran"
    );
    assert_eq!(id.as_deref(), Some("ctr-1"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unattributed_run_is_not_stamped(pool: PgPool) {
    // No AI System means no contract; inventing one would attribute traffic to
    // rules that never applied to it.
    let t = "t-rl-ctr-unattr";
    common::seed_tenant(&pool, t).await;

    let h = Hop::new("r", "llm", 30);
    insert_hop(&pool, t, "f1", h).await;
    finalize_run(&pool, t, "r", finalized_by::EXPLICIT, None, None)
        .await
        .unwrap()
        .unwrap();

    assert_eq!(stamp_of(&pool, t, "r").await, (None, None));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn another_tenants_contract_is_never_used(pool: PgPool) {
    let t = "t-rl-ctr-iso";
    let other = "t-rl-ctr-iso2";
    common::seed_tenant(&pool, t).await;
    common::seed_tenant(&pool, other).await;
    seed_system(&pool, t, "sys-a", "agent", None).await;
    seed_system(&pool, other, "sys-b", "agent", None).await;
    seed_contract(
        &pool,
        other,
        "ctr-other",
        "sys-b",
        1,
        "sha256:theirs",
        "active",
    )
    .await;

    let mut h = Hop::new("r", "llm", 30);
    h.group_id = Some("sys-a");
    insert_hop(&pool, t, "f1", h).await;
    finalize_run(&pool, t, "r", finalized_by::EXPLICIT, None, None)
        .await
        .unwrap()
        .unwrap();

    assert_eq!(stamp_of(&pool, t, "r").await, (None, None));
}

// ---------------------------------------------------------------------------
// Turns
//
// Run > Turn > Step. A step is one governed call; a turn is one pass of the
// agent loop; a run is the turns it took to finish the task.
//
// The rule that matters is provenance. A turn boundary is inferable, but only
// the client knows its own loop for certain — so we infer by default, accept
// an assertion when offered, and record which one we got. Presenting an
// inferred count as though it were reported is the same class of error as
// scoring absent evidence green.
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn turns_are_inferred_from_model_calls_when_the_client_says_nothing(pool: PgPool) {
    let t = "t-rl-turn-inferred";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent", None).await;

    // Two loop passes: model -> tool -> tool, then model -> tool.
    // Five steps, two turns.
    for (i, (kind, tool)) in [
        ("llm", None),
        ("mcp", Some("search")),
        ("mcp", Some("fetch")),
        ("llm", None),
        ("mcp", Some("write")),
    ]
    .iter()
    .enumerate()
    {
        let mut h = Hop::new("r-inf", kind, 60 - i as i64);
        h.group_id = Some("sys");
        h.tool = *tool;
        insert_hop(&pool, t, &format!("ti{i}"), h).await;
    }

    finalize_run(&pool, t, "r-inf", finalized_by::EXPLICIT, None, None)
        .await
        .unwrap()
        .expect("finalized");

    let (steps, turns, attribution): (i32, i32, String) = sqlx::query_as(
        "SELECT action_count, turn_count, turn_attribution FROM ai_system_runs \
         WHERE tenant_id = $1 AND root_task_id = $2",
    )
    .bind(t)
    .bind("r-inf")
    .fetch_one(&pool)
    .await
    .expect("run row");

    assert_eq!(steps, 5, "five governed calls is five steps");
    assert_eq!(turns, 2, "two model calls is two passes of the loop");
    assert_eq!(
        attribution, "inferred",
        "nothing was asserted, so the count must not claim to be reported",
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_asserted_turn_id_wins_over_inference(pool: PgPool) {
    // The client ran three loop passes but only two reached a model call the
    // gateway saw (one was served from cache upstream, say). Inference would
    // say 2; the client says 3, and the client is the authority on its own
    // loop.
    let t = "t-rl-turn-asserted";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent", None).await;

    for (i, (kind, turn)) in [
        ("llm", "turn-1"),
        ("mcp", "turn-1"),
        ("llm", "turn-2"),
        ("mcp", "turn-3"),
    ]
    .iter()
    .enumerate()
    {
        let mut h = Hop::new("r-ast", kind, 60 - i as i64);
        h.group_id = Some("sys");
        h.turn_id = Some(turn);
        insert_hop(&pool, t, &format!("ta{i}"), h).await;
    }

    finalize_run(&pool, t, "r-ast", finalized_by::EXPLICIT, None, None)
        .await
        .unwrap()
        .expect("finalized");

    let (turns, attribution): (i32, String) = sqlx::query_as(
        "SELECT turn_count, turn_attribution FROM ai_system_runs \
         WHERE tenant_id = $1 AND root_task_id = $2",
    )
    .bind(t)
    .bind("r-ast")
    .fetch_one(&pool)
    .await
    .expect("run row");

    assert_eq!(turns, 3, "three distinct asserted turn ids");
    assert_eq!(attribution, "client_asserted");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_embedding_is_work_inside_a_turn_not_a_turn(pool: PgPool) {
    // A retrieval happens *during* a loop pass. Counting it as its own turn
    // would inflate every RAG-backed system's turn count against systems
    // that retrieve some other way, which is exactly the kind of artefact a
    // drift baseline would then chase.
    let t = "t-rl-turn-embed";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent", None).await;

    let mut h = Hop::new("r-emb", "llm", 60);
    h.group_id = Some("sys");
    insert_hop(&pool, t, "te0", h).await;

    sqlx::query("UPDATE proxy_logs SET proxy_subtype = 'embeddings' WHERE id = 'te0'")
        .execute(&pool)
        .await
        .expect("mark embedding");

    let mut h = Hop::new("r-emb", "llm", 59);
    h.group_id = Some("sys");
    insert_hop(&pool, t, "te1", h).await;

    finalize_run(&pool, t, "r-emb", finalized_by::EXPLICIT, None, None)
        .await
        .unwrap()
        .expect("finalized");

    let (steps, turns): (i32, i32) = sqlx::query_as(
        "SELECT action_count, turn_count FROM ai_system_runs \
         WHERE tenant_id = $1 AND root_task_id = $2",
    )
    .bind(t)
    .bind("r-emb")
    .fetch_one(&pool)
    .await
    .expect("run row");

    assert_eq!(steps, 2, "the embedding is still a governed call");
    assert_eq!(turns, 1, "but it is not a pass of the agent loop");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a2a_calls_are_counted_as_calls_not_hops(pool: PgPool) {
    // The column was a2a_hop_count while its three siblings said "call",
    // which left "hop" reading as a fifth concept rather than a synonym.
    let t = "t-rl-a2a-rename";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent", None).await;

    for (i, kind) in ["llm", "a2a", "a2a"].iter().enumerate() {
        let mut h = Hop::new("r-a2a", kind, 60 - i as i64);
        h.group_id = Some("sys");
        insert_hop(&pool, t, &format!("tc{i}"), h).await;
    }

    finalize_run(&pool, t, "r-a2a", finalized_by::EXPLICIT, None, None)
        .await
        .unwrap()
        .expect("finalized");

    let (a2a,): (i32,) = sqlx::query_as(
        "SELECT a2a_call_count FROM ai_system_runs \
         WHERE tenant_id = $1 AND root_task_id = $2",
    )
    .bind(t)
    .bind("r-a2a")
    .fetch_one(&pool)
    .await
    .expect("run row");

    assert_eq!(a2a, 2);
}

// There is no client-reported outcome (`X-Brutor-Run-Outcome`,
// `client_outcome`); outcomes are declared: see `declared_outcomes.rs`.

#[sqlx::test(migrations = "./tests/migrations")]
async fn run_facts_carry_the_a167_a168_fields(pool: PgPool) {
    // subject, prompt fingerprint, collections and client outcome are all
    // Tier-A vocabulary now; the pinned key list and the facts must agree.
    use brutor_gateway_core::services::run_ledger::{RUN_FACT_KEYS, sample_run_facts};
    let sample = sample_run_facts();
    for key in [
        "subject_id",
        "subject_kind",
        "prompt_fingerprint",
        "rag_collections",
        // `client_outcome` is gone; the judgement replaced it.
        "outcome_id",
        "outcome_conformance",
        "error_cause",
        "run_class",
    ] {
        assert!(
            RUN_FACT_KEYS.contains(&key),
            "{key} missing from RUN_FACT_KEYS"
        );
        assert!(
            sample.get(key).is_some(),
            "{key} missing from sample_run_facts"
        );
    }
    let _ = pool; // the pool is unused; the harness signature is fixed
}

// ---------------------------------------------------------------------------
// Time awaiting a human is not runtime
//
// A call held for approval is retried on the SAME run once a person decides,
// possibly hours later, so the elapsed clock includes the person's wait. The
// run now records that wait (`awaiting_human_ms`, merged across concurrent
// holds) and the system's own time (`active_duration_ms`). The case:
// a decision approved 13 h later breached a 300 s ceiling on elapsed time.
// ---------------------------------------------------------------------------

/// One `tool_approval_requests` row for `root`. Times are seconds relative to
/// now (positive = in the past), matching `Hop::seconds_ago`.
struct Hold<'a> {
    id: &'a str,
    tenant: &'a str,
    root: &'a str,
    status: &'a str,
    created_ago: i64,
    resolved_ago: Option<i64>,
    /// Negative = in the future.
    expires_ago: i64,
}

async fn insert_hold(pool: &PgPool, h: Hold<'_>) {
    let now = crate::common::db_now();
    sqlx::query(
        "INSERT INTO tool_approval_requests (
            id, tenant_id, server_name, capability_type, capability_name,
            requester_type, requester_group_id, status, timeout_seconds,
            expires_at, resolved_at, originating_trace_root_id, created_at
         ) VALUES ($1, $2, 'srv', 'tool', 'send_email', 'agent', 'rg', $3, 3600,
                   $4, $5, $6, $7)",
    )
    .bind(h.id)
    .bind(h.tenant)
    .bind(h.status)
    .bind(now - Duration::seconds(h.expires_ago))
    .bind(h.resolved_ago.map(|s| now - Duration::seconds(s)))
    .bind(h.root)
    .bind(now - Duration::seconds(h.created_ago))
    .execute(pool)
    .await
    .expect("insert_hold");
}

/// Each hop and hold is stamped `crate::common::db_now() − offset` at its own insert, so
/// the rows drift apart by however long the inserts take on a loaded machine.
/// A second of slack is measurement, not error: every figure asserted here
/// differs from the wrong answer by tens of seconds or hours.
fn assert_ms(actual: i64, expected: i64, what: &str) {
    assert!(
        (actual - expected).abs() <= 1_000,
        "{what}: expected ~{expected} ms, got {actual} ms"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_thirteen_hour_hold_is_awaiting_a_human_not_runtime(pool: PgPool) {
    let t = "t-hold";
    common::seed_tenant(&pool, t).await;
    let thirteen_h = 13 * 3600;
    // The run: a model call, a held tool call, the approved retry 13 h later,
    // and a final model call. 40 s of the system's own time.
    insert_hop(
        &pool,
        t,
        "h1",
        Hop::new("root-held", "llm", thirteen_h + 50),
    )
    .await;
    let mut held = Hop::new("root-held", "mcp", thirteen_h + 45);
    held.subtype = Some("approval_required");
    held.success = false;
    insert_hop(&pool, t, "h2", held).await;
    insert_hop(&pool, t, "h3", Hop::new("root-held", "mcp", 44)).await;
    insert_hop(&pool, t, "h4", Hop::new("root-held", "llm", 10)).await;
    insert_hold(
        &pool,
        Hold {
            id: "tar-held",
            tenant: t,
            root: "root-held",
            status: "consumed",
            created_ago: thirteen_h + 45,
            resolved_ago: Some(45),
            expires_ago: -3600,
        },
    )
    .await;

    let agg = aggregate_run(&pool, t, "root-held")
        .await
        .expect("aggregate")
        .expect("run");
    assert_ms(
        agg.duration_ms,
        (thirteen_h + 40) * 1000,
        "elapsed keeps the wait",
    );
    assert_ms(
        agg.awaiting_human_ms,
        thirteen_h * 1000,
        "the wait is measured",
    );
    assert_ms(
        agg.active_duration_ms,
        40_000,
        "active time is the system's own",
    );

    // And it reaches the stored run, which is what the envelope grades.
    finalize_run(&pool, t, "root-held", finalized_by::EXPLICIT, None, None)
        .await
        .expect("finalize")
        .expect("finalized");
    let (elapsed, awaiting, active): (Option<i32>, i32, i32) = sqlx::query_as(
        "SELECT duration_ms, awaiting_human_ms, active_duration_ms FROM ai_system_runs \
         WHERE tenant_id = $1 AND root_task_id = $2",
    )
    .bind(t)
    .bind("root-held")
    .fetch_one(&pool)
    .await
    .expect("stored run");
    assert_ms(
        i64::from(elapsed.unwrap()),
        (thirteen_h + 40) * 1000,
        "stored elapsed",
    );
    assert_ms(i64::from(awaiting), thirteen_h * 1000, "stored awaiting");
    assert_ms(i64::from(active), 40_000, "stored active");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn overlapping_holds_are_one_wait_not_two(pool: PgPool) {
    let t = "t-overlap";
    common::seed_tenant(&pool, t).await;
    common::seed_tenant(&pool, "t-other").await;
    insert_hop(&pool, t, "o1", Hop::new("root-ov", "llm", 120)).await;
    insert_hop(&pool, t, "o2", Hop::new("root-ov", "llm", 10)).await;
    // [100 → 40] and [80 → 20] overlap: together they cover [100 → 20] = 80 s.
    // [90 → 85] sits entirely inside the first and adds nothing. [15 → 12] is
    // a separate island: 3 s more.
    for (id, c, r) in [("a", 100, 40), ("b", 80, 20), ("c", 90, 85), ("d", 15, 12)] {
        insert_hold(
            &pool,
            Hold {
                id,
                tenant: t,
                root: "root-ov",
                status: "approved",
                created_ago: c,
                resolved_ago: Some(r),
                expires_ago: -3600,
            },
        )
        .await;
    }
    // Another tenant's hold on the same root id is not this run's wait.
    insert_hold(
        &pool,
        Hold {
            id: "foreign",
            tenant: "t-other",
            root: "root-ov",
            status: "approved",
            created_ago: 119,
            resolved_ago: Some(11),
            expires_ago: -3600,
        },
    )
    .await;

    let agg = aggregate_run(&pool, t, "root-ov")
        .await
        .expect("aggregate")
        .expect("run");
    assert_ms(
        agg.awaiting_human_ms,
        83_000,
        "merged, not summed (summed would be 128 s)",
    );
    assert_ms(
        agg.active_duration_ms,
        110_000 - 83_000,
        "active = elapsed − merged wait",
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_pending_hold_counts_to_now_and_an_expired_one_to_its_expiry(pool: PgPool) {
    let t = "t-pending";
    common::seed_tenant(&pool, t).await;
    insert_hop(&pool, t, "p1", Hop::new("root-p", "llm", 300)).await;
    insert_hop(&pool, t, "p2", Hop::new("root-p", "llm", 5)).await;
    // Pending since 60 s ago, not yet lapsed.
    insert_hold(
        &pool,
        Hold {
            id: "pend",
            tenant: t,
            root: "root-p",
            status: "pending",
            created_ago: 60,
            resolved_ago: None,
            expires_ago: -3600,
        },
    )
    .await;
    // Expired: raised 250 s ago, lapsed 200 s ago; the sweeper marked it later.
    insert_hold(
        &pool,
        Hold {
            id: "exp",
            tenant: t,
            root: "root-p",
            status: "expired",
            created_ago: 250,
            resolved_ago: Some(150),
            expires_ago: 200,
        },
    )
    .await;

    // The finalized run stops at its last step (5 s ago), and so does the
    // pending hold's contribution: 55 s + the expired hold's 50 s.
    let agg = aggregate_run(&pool, t, "root-p")
        .await
        .expect("aggregate")
        .expect("run");
    assert_ms(
        agg.awaiting_human_ms,
        105_000,
        "pending clipped to the run's end + expired to expiry",
    );

    // The live run clock measures to NOW: the pending hold is still a wait.
    let now = crate::common::db_now();
    let held = held_ms_between(&pool, t, "root-p", now - Duration::seconds(300), now)
        .await
        .expect("held");
    assert_ms(held, 110_000, "pending counts to now for the live clock");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_run_with_no_holds_is_all_active(pool: PgPool) {
    let t = "t-noholds";
    common::seed_tenant(&pool, t).await;
    insert_hop(&pool, t, "n1", Hop::new("root-n", "llm", 90)).await;
    insert_hop(&pool, t, "n2", Hop::new("root-n", "mcp", 30)).await;
    let agg = aggregate_run(&pool, t, "root-n")
        .await
        .expect("aggregate")
        .expect("run");
    assert_eq!(agg.awaiting_human_ms, 0);
    assert_eq!(agg.active_duration_ms, agg.duration_ms);
    assert_ms(agg.duration_ms, 60_000, "elapsed");
}

/// A run records the group it started in, so a run no AI System
/// claims can still say whose traffic it is (Mission Control's Unattributed
/// card), and platform calls can be told apart from it.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unattributed_run_records_the_group_it_started_in(pool: PgPool) {
    let t = "t-rl-group";
    common::seed_tenant(&pool, t).await;
    common::seed_resource_group(&pool, "rg-start", t).await;
    common::seed_resource_group(&pool, "rg-later", t).await;

    let mut first = Hop::new("r", "llm", 30);
    first.group_id = Some("rg-start");
    insert_hop(&pool, t, "g1", first).await;
    let mut second = Hop::new("r", "mcp", 20);
    second.group_id = Some("rg-later");
    second.tool = Some("lookup");
    insert_hop(&pool, t, "g2", second).await;

    let run = finalize_run(&pool, t, "r", finalized_by::EXPLICIT, None, None)
        .await
        .unwrap()
        .expect("finalized");
    assert_eq!(run.ai_system_id, None, "no AI System claims these groups");

    let stored: (Option<String>,) = sqlx::query_as(
        "SELECT resource_group_id FROM ai_system_runs WHERE tenant_id = $1 AND root_task_id = $2",
    )
    .bind(t)
    .bind("r")
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(stored.0.as_deref(), Some("rg-start"));
}

// ---------------------------------------------------------------------------
// A run starts when its first call STARTED
//
// A proxy-log row is stamped when its call finishes, so the elapsed clock
// measured stamp to stamp read 0 ms for every one-call run and cut the first
// call out of every longer one. The step ran over
// `[timestamp - duration_ms, timestamp]`.
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_one_call_run_lasts_as_long_as_its_call(pool: PgPool) {
    let t = "t-onecall";
    common::seed_tenant(&pool, t).await;
    let mut only = Hop::new("root-one", "llm", 30);
    only.duration_ms = Some("1500");
    insert_hop(&pool, t, "o1", only).await;
    let (stamp,): (chrono::DateTime<chrono::Utc>,) =
        sqlx::query_as("SELECT timestamp FROM proxy_logs WHERE tenant_id = $1 AND id = 'o1'")
            .bind(t)
            .fetch_one(&pool)
            .await
            .unwrap();
    let agg = aggregate_run(&pool, t, "root-one")
        .await
        .expect("aggregate")
        .expect("run");
    assert_eq!(agg.duration_ms, 1_500);
    assert_eq!(agg.active_duration_ms, 1_500);
    assert_eq!(agg.started_at, stamp - Duration::milliseconds(1_500));
    assert_eq!(agg.ended_at, stamp, "the run ends at its last stamp");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_runs_elapsed_time_includes_its_first_call(pool: PgPool) {
    let t = "t-firstcall";
    common::seed_tenant(&pool, t).await;
    let mut first = Hop::new("root-f", "llm", 90);
    first.duration_ms = Some("5000");
    insert_hop(&pool, t, "f1", first).await;
    // A long call that started before the first one finished: the run began
    // at the EARLIEST start, whichever row carries it.
    let mut long = Hop::new("root-f", "mcp", 60);
    long.duration_ms = Some("40000");
    insert_hop(&pool, t, "f2", long).await;
    insert_hop(&pool, t, "f3", Hop::new("root-f", "llm", 30)).await;
    let agg = aggregate_run(&pool, t, "root-f")
        .await
        .expect("aggregate")
        .expect("run");
    assert_ms(
        agg.duration_ms,
        70_000,
        "first start (100 s ago) to last stamp",
    );
    assert_eq!(agg.active_duration_ms, agg.duration_ms);

    finalize_run(&pool, t, "root-f", finalized_by::EXPLICIT, None, None)
        .await
        .expect("finalize")
        .expect("finalized");
    let (elapsed, active): (Option<i32>, i32) = sqlx::query_as(
        "SELECT duration_ms, active_duration_ms FROM ai_system_runs \
         WHERE tenant_id = $1 AND root_task_id = $2",
    )
    .bind(t)
    .bind("root-f")
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_ms(i64::from(elapsed.unwrap()), 70_000, "stored elapsed");
    assert_ms(i64::from(active), 70_000, "stored active");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_hold_inside_the_first_call_is_awaiting_a_human(pool: PgPool) {
    let t = "t-earlyhold";
    common::seed_tenant(&pool, t).await;
    let mut first = Hop::new("root-e", "mcp", 60);
    first.duration_ms = Some("20000");
    insert_hop(&pool, t, "e1", first).await;
    insert_hop(&pool, t, "e2", Hop::new("root-e", "llm", 30)).await;
    // Held 75 s..65 s ago: inside the first call, before any stamp.
    insert_hold(
        &pool,
        Hold {
            id: "tar-early",
            tenant: t,
            root: "root-e",
            status: "approved",
            created_ago: 75,
            resolved_ago: Some(65),
            expires_ago: -3600,
        },
    )
    .await;
    let agg = aggregate_run(&pool, t, "root-e")
        .await
        .expect("aggregate")
        .expect("run");
    assert_ms(agg.duration_ms, 50_000, "elapsed");
    assert_ms(agg.awaiting_human_ms, 10_000, "the early hold is measured");
    assert_ms(agg.active_duration_ms, 40_000, "active");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unusable_duration_never_moves_the_run_start(pool: PgPool) {
    let t = "t-baddur";
    common::seed_tenant(&pool, t).await;
    for (id, ago, dur) in [
        ("b1", 90, "NaN"),
        ("b2", 60, "Infinity"),
        ("b3", 30, "-5000"),
    ] {
        let mut h = Hop::new("root-b", "llm", ago);
        h.duration_ms = Some(dur);
        insert_hop(&pool, t, id, h).await;
    }
    let agg = aggregate_run(&pool, t, "root-b")
        .await
        .expect("an unusable duration is not an error")
        .expect("run");
    assert_ms(agg.duration_ms, 60_000, "stamp to stamp, as before");
}

// ---------------------------------------------------------------------------
// Context size
// ---------------------------------------------------------------------------

/// One LLM call of the context `tokens`, as the writer stores it.
async fn context_call(
    pool: &PgPool,
    (tenant, id, root): (&str, &str, &str),
    seconds_ago: i64,
    agent: Option<&str>,
    model: &str,
    tokens: Option<i32>,
) {
    let mut h = Hop::new(root, "llm", seconds_ago);
    h.group_id = Some("sys");
    h.model = Some(model);
    insert_hop(pool, tenant, id, h).await;
    sqlx::query(
        "UPDATE proxy_logs SET llm_context_tokens = $1, actor_agent_id = $2, llm_provider = 'openai' \
          WHERE id = $3",
    )
    .bind(tokens)
    .bind(agent)
    .bind(id)
    .execute(pool)
    .await
    .unwrap();
}

async fn catalog_window(pool: &PgPool, model: &str, window: i32) {
    sqlx::query(
        "INSERT INTO llm_catalog (id, provider_key, model_name, display_name, mode, context_window) \
         VALUES ($1, 'openai', $2, $2, 'chat', $3)",
    )
    .bind(format!("llmcat-openai-{model}"))
    .bind(model)
    .bind(window)
    .execute(pool)
    .await
    .unwrap();
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_peak_survives_an_agent_compacting_before_it_finishes(pool: PgPool) {
    let t = "t-ctx-compact";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent", None).await;
    for (i, tokens) in [100, 900, 200].iter().enumerate() {
        let id = format!("c{i}");
        context_call(
            &pool,
            (t, &id, "r"),
            60 - i as i64 * 10,
            Some("a"),
            "m",
            Some(*tokens),
        )
        .await;
    }

    let agg = aggregate_run(&pool, t, "r").await.unwrap().unwrap();
    assert_eq!(
        agg.peak_context_tokens,
        Some(900),
        "not the last call's 200"
    );
    assert_eq!(agg.context_growth, Some(9.0));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn two_agents_contexts_are_never_added_and_growth_is_the_peak_agents_own(pool: PgPool) {
    let t = "t-ctx-agents";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent", None).await;
    // The run's FIRST call is a small sub-agent's. Measuring growth against it
    // would read 300x; the agent that holds the peak grew 3x.
    context_call(&pool, (t, "c1", "r"), 60, Some("small"), "m", Some(10)).await;
    context_call(&pool, (t, "c2", "r"), 50, Some("big"), "m", Some(1_000)).await;
    context_call(&pool, (t, "c3", "r"), 40, Some("small"), "m", Some(50)).await;
    context_call(&pool, (t, "c4", "r"), 30, Some("big"), "m", Some(3_000)).await;

    let agg = aggregate_run(&pool, t, "r").await.unwrap().unwrap();
    assert_eq!(agg.peak_context_tokens, Some(3_000), "not 3,050");
    assert_eq!(agg.context_growth, Some(3.0));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_run_without_reported_usage_is_not_measured_rather_than_zero(pool: PgPool) {
    let t = "t-ctx-none";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent", None).await;
    catalog_window(&pool, "m", 1_000).await;
    context_call(&pool, (t, "c1", "r"), 60, Some("a"), "m", None).await;

    let agg = aggregate_run(&pool, t, "r").await.unwrap().unwrap();
    assert_eq!(agg.peak_context_tokens, None);
    assert_eq!(agg.peak_context_utilization, None);
    assert_eq!(agg.context_growth, None);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn utilization_is_per_call_against_that_calls_own_model(pool: PgPool) {
    let t = "t-ctx-window";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent", None).await;
    catalog_window(&pool, "wide", 1_000_000).await;
    catalog_window(&pool, "narrow", 1_000).await;
    // The token peak is on the wide model (0.1%); the narrow one is the fuller
    // context (50%). A window with no catalog entry contributes nothing.
    context_call(&pool, (t, "c1", "r"), 60, Some("a"), "wide", Some(1_000)).await;
    context_call(&pool, (t, "c2", "r"), 50, Some("a"), "narrow", Some(500)).await;
    context_call(&pool, (t, "c3", "r"), 40, Some("a"), "unknown", Some(900)).await;

    let agg = aggregate_run(&pool, t, "r").await.unwrap().unwrap();
    assert_eq!(agg.peak_context_tokens, Some(1_000));
    assert_eq!(agg.peak_context_utilization, Some(0.5));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_model_with_no_known_window_has_no_utilization_but_a_peak(pool: PgPool) {
    let t = "t-ctx-nowindow";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent", None).await;
    context_call(&pool, (t, "c1", "r"), 60, Some("a"), "unknown", Some(700)).await;

    let agg = aggregate_run(&pool, t, "r").await.unwrap().unwrap();
    assert_eq!(agg.peak_context_tokens, Some(700));
    assert_eq!(agg.peak_context_utilization, None, "unknown, never 0");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn context_reaches_the_run_row_and_a_late_call_recomputes_it(pool: PgPool) {
    let t = "t-ctx-store";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent", None).await;
    catalog_window(&pool, "m", 1_000).await;
    context_call(&pool, (t, "c1", "r"), 60, None, "m", Some(100)).await;
    finalize_run(&pool, t, "r", finalized_by::EXPLICIT, None, None)
        .await
        .unwrap()
        .expect("finalized");
    context_call(&pool, (t, "c2", "r"), 30, None, "m", Some(400)).await;
    finalize_run(&pool, t, "r", finalized_by::EXPLICIT, None, None)
        .await
        .unwrap()
        .expect("finalized again");

    let stored: (Option<i32>, Option<f64>, Option<f64>) = sqlx::query_as(
        "SELECT peak_context_tokens, peak_context_utilization, context_growth \
           FROM ai_system_runs WHERE tenant_id = $1 AND root_task_id = 'r'",
    )
    .bind(t)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(stored, (Some(400), Some(0.4), Some(4.0)));
}

async fn set_prompt_fields(pool: &PgPool, id: &str, context: Option<i32>, subtype: Option<&str>) {
    sqlx::query("UPDATE proxy_logs SET llm_context_tokens = $1, proxy_subtype = $2 WHERE id = $3")
        .bind(context)
        .bind(subtype)
        .bind(id)
        .execute(pool)
        .await
        .unwrap();
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_task_as_posed_counts_a_cached_prompt_and_skips_what_read_no_context(pool: PgPool) {
    let t = "t-first-prompt";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent", None).await;

    // An embedding and a response-cache hit come first: neither put the task
    // in front of a model. The first model call reports 10 uncached tokens and
    // 5,000 cached ones, which the writer stored as a context of 5,010.
    let mut h = Hop::new("r", "llm", 60);
    h.group_id = Some("sys");
    h.prompt_tokens = Some(7);
    insert_hop(&pool, t, "embedding", h).await;
    set_prompt_fields(&pool, "embedding", None, Some("embeddings")).await;

    let mut h = Hop::new("r", "llm", 50);
    h.group_id = Some("sys");
    h.prompt_tokens = Some(9);
    insert_hop(&pool, t, "hit", h).await;
    set_prompt_fields(&pool, "hit", None, None).await;

    let mut h = Hop::new("r", "llm", 40);
    h.group_id = Some("sys");
    h.prompt_tokens = Some(10);
    insert_hop(&pool, t, "first", h).await;
    set_prompt_fields(&pool, "first", Some(5_010), None).await;

    let agg = aggregate_run(&pool, t, "r").await.unwrap().unwrap();
    assert_eq!(agg.first_prompt_tokens, Some(5_010));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn no_measured_call_leaves_the_task_size_unknown(pool: PgPool) {
    let t = "t-first-none";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent", None).await;
    let mut h = Hop::new("r", "llm", 40);
    h.group_id = Some("sys");
    insert_hop(&pool, t, "c", h).await;

    let agg = aggregate_run(&pool, t, "r").await.unwrap().unwrap();
    assert_eq!(agg.first_prompt_tokens, None);
}

// ---------------------------------------------------------------------------
// The credentials of a run
// ---------------------------------------------------------------------------

async fn set_credential(
    pool: &PgPool,
    id: &str,
    kind: &str,
    level: Option<&str>,
    reference: Option<&str>,
) {
    sqlx::query(
        "UPDATE proxy_logs SET credential_kind = $2, credential_assurance = $3, credential_ref = $4 \
         WHERE id = $1",
    )
    .bind(id)
    .bind(kind)
    .bind(level)
    .bind(reference)
    .execute(pool)
    .await
    .expect("set_credential");
}

async fn stored_credentials(
    pool: &PgPool,
    tenant: &str,
) -> (Option<String>, Option<bool>, Option<serde_json::Value>) {
    sqlx::query_as(
        "SELECT credential_assurance_min, credential_mixed, credential_refs \
           FROM ai_system_runs WHERE tenant_id = $1 AND root_task_id = 'r'",
    )
    .bind(tenant)
    .fetch_one(pool)
    .await
    .expect("run row")
}

/// A run of a workload step and a key step, the key step delegated: the run
/// is mixed, at the key's level, and names both credentials in the order
/// they appeared.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_run_of_key_and_workload_steps_is_stored_mixed_at_its_lowest_level(pool: PgPool) {
    let t = "t-rl-cred";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent", None).await;

    let mut h = Hop::new("r", "llm", 30);
    h.group_id = Some("sys");
    insert_hop(&pool, t, "c1", h).await;
    set_credential(
        &pool,
        "c1",
        "workload_token",
        Some("workload"),
        Some("token:idp-1:spiffe://a"),
    )
    .await;
    let mut h = Hop::new("r", "mcp", 20);
    h.group_id = Some("sys");
    h.tool = Some("search");
    h.depth = 1;
    insert_hop(&pool, t, "c2", h).await;
    set_credential(&pool, "c2", "api_key", Some("key"), Some("api_key:k-1")).await;

    finalize_run(&pool, t, "r", finalized_by::EXPLICIT, None, None)
        .await
        .unwrap()
        .expect("finalized");
    let (min, mixed, refs) = stored_credentials(&pool, t).await;
    assert_eq!(min.as_deref(), Some("key"));
    assert_eq!(mixed, Some(true));
    assert_eq!(
        refs,
        Some(serde_json::json!(["token:idp-1:spiffe://a", "api_key:k-1"]))
    );

    // A late anonymous step reopens the run: no level, still mixed.
    let mut h = Hop::new("r", "mcp", 5);
    h.group_id = Some("sys");
    insert_hop(&pool, t, "c3", h).await;
    set_credential(&pool, "c3", "anonymous", None, None).await;
    finalize_run(&pool, t, "r", finalized_by::EXPLICIT, None, None)
        .await
        .unwrap()
        .expect("reopened");
    let (min, mixed, _) = stored_credentials(&pool, t).await;
    assert_eq!(min, None, "an anonymous step has no level");
    assert_eq!(mixed, Some(true));
}

/// One workload credential throughout: not mixed, at `workload`.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_run_of_one_credential_is_not_mixed(pool: PgPool) {
    let t = "t-rl-cred-one";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys", "agent", None).await;
    for (id, ago) in [("o1", 30), ("o2", 20)] {
        let mut h = Hop::new("r", "llm", ago);
        h.group_id = Some("sys");
        insert_hop(&pool, t, id, h).await;
        set_credential(
            &pool,
            id,
            "workload_token",
            Some("workload"),
            Some("token:i:s"),
        )
        .await;
    }
    finalize_run(&pool, t, "r", finalized_by::EXPLICIT, None, None)
        .await
        .unwrap()
        .expect("finalized");
    let (min, mixed, refs) = stored_credentials(&pool, t).await;
    assert_eq!(min.as_deref(), Some("workload"));
    assert_eq!(mixed, Some(false));
    assert_eq!(refs, Some(serde_json::json!(["token:i:s"])));
}
