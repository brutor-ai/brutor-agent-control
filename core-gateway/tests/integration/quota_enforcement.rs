// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Quota enforcement — golden-master characterization suite
//!
//! Pins the EXACT current behavior of `usage::enforce_llm_quota` /
//! `enforce_mcp_quota` / `enforce_skill_quota` against a real Postgres, BEFORE
//! the reserve/commit refactor. These call the public enforce API (which the
//! refactor keeps), so they stay valid across it and prove the new
//! counter-based implementation preserves semantics.
//!
//! Captured invariants (from reading the current code):
//! - Block conditions: budget `util >= 100% && hard_stop`; tokens `used >= limit`;
//!   requests `(count + 1) > limit`.
//! - Warn defaults: tokens daily 85 / monthly 90; requests daily 80 / monthly 85;
//!   budget daily 80 / monthly 90.
//! - Windows: daily = since UTC-midnight-today; monthly = since 1st-of-month.
//!   Filter is `created_at >= window_start` (older rows excluded).
//! - Hierarchy: `collect_effective_limits` restrictively merges self+ancestors
//!   (numeric min wins) → a parent can only TIGHTEN a child.
//! - Ancestor rollup: `enforce_ancestor_budgets` sums descendant usage by
//!   `ancestry_path LIKE '{ancestor.hierarchy_path}%'` against each ancestor's
//!   OWN limit.
//! - MCP quota path enforces ONLY ancestor budgets
//!   (`frequency.max_calls_per_day`); per-call rate limits are a separate path.

use chrono::Duration;
use serde_json::json;
use sqlx::PgPool;

use brutor_gateway_core::services::cache::usage_state::{
    Actual, Finish, QuotaWindow, Shape, UsageState,
};
use brutor_gateway_core::services::limits::HoldId;
use brutor_gateway_core::services::usage;

use crate::common;

const T: &str = "tenant-quota";

// ===========================================================================
// LLM — group-scoped
// ===========================================================================

#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_no_limits_allows(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    let g = common::load_resource_group(&pool, "g1").await;

    let r = usage::enforce_llm_quota(&pool, T, Some(&g), None, None, None).await;
    assert!(!r.blocked, "no limits configured must allow");
    assert!(r.warning_headers.is_empty());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_daily_tokens_blocks_at_limit(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    common::set_llm_global_limits(&pool, "g1", json!({"tokens": {"daily_limit": 1000}})).await;
    // Exactly at the limit — block is `used >= limit`.
    common::insert_usage_log(
        &pool,
        T,
        "g1",
        None,
        "llm",
        "m1",
        1000,
        0.0,
        crate::common::db_now(),
    )
    .await;
    let g = common::load_resource_group(&pool, "g1").await;

    let r = usage::enforce_llm_quota(&pool, T, Some(&g), None, None, None).await;
    assert!(r.blocked, "used == limit must block (>=)");
    assert_eq!(r.limit_type.as_deref(), Some("daily_tokens"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_daily_tokens_under_allows(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    common::set_llm_global_limits(&pool, "g1", json!({"tokens": {"daily_limit": 1000}})).await;
    common::insert_usage_log(
        &pool,
        T,
        "g1",
        None,
        "llm",
        "m1",
        500,
        0.0,
        crate::common::db_now(),
    )
    .await;
    let g = common::load_resource_group(&pool, "g1").await;

    let r = usage::enforce_llm_quota(&pool, T, Some(&g), None, None, None).await;
    assert!(!r.blocked, "500 < 1000 must allow");
    // 50% < 85% warn default → no warning header.
    assert!(
        r.warning_headers.is_empty(),
        "below warn threshold → no warning"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_daily_tokens_warns_at_threshold(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    common::set_llm_global_limits(&pool, "g1", json!({"tokens": {"daily_limit": 1000}})).await;
    // 90% — over the 85% daily-token warn default, still under the limit.
    common::insert_usage_log(
        &pool,
        T,
        "g1",
        None,
        "llm",
        "m1",
        900,
        0.0,
        crate::common::db_now(),
    )
    .await;
    let g = common::load_resource_group(&pool, "g1").await;

    let r = usage::enforce_llm_quota(&pool, T, Some(&g), None, None, None).await;
    assert!(!r.blocked, "90% is under the hard limit");
    assert!(
        r.warning_headers
            .iter()
            .any(|(k, _)| k == "X-Usage-Warning"),
        "90% >= 85% warn default → warning header expected"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_daily_budget_hard_stop_blocks(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    common::set_llm_global_limits(
        &pool,
        "g1",
        json!({"budget": {"daily_limit_usd": 10.0, "hard_stop": true}}),
    )
    .await;
    common::insert_usage_log(
        &pool,
        T,
        "g1",
        None,
        "llm",
        "m1",
        0,
        12.50,
        crate::common::db_now(),
    )
    .await;
    let g = common::load_resource_group(&pool, "g1").await;

    let r = usage::enforce_llm_quota(&pool, T, Some(&g), None, None, None).await;
    assert!(r.blocked, "cost over budget with hard_stop must block");
    assert_eq!(r.limit_type.as_deref(), Some("daily_budget"));
}

/// A stored `hard_stop: false` no longer buys an exemption.
///
/// The opt-out was removed platform-wide (see the notes in
/// `services::usage`: "every configured cap now blocks at 100%"), because a
/// dollar cap that can be configured not to stop anything is not a cap. This
/// test previously asserted the old semantics and had been failing since the
/// removal shipped — a red test nobody was reading, which is worse than no
/// test, because it hides the next real regression.
///
/// What matters now is that the *stored* flag cannot resurrect the exemption:
/// tenants configured before the removal still carry `hard_stop: false` rows.
#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_daily_budget_blocks_over_even_with_a_stored_hard_stop_false(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    common::set_llm_global_limits(
        &pool,
        "g1",
        json!({"budget": {"daily_limit_usd": 10.0, "hard_stop": false}}),
    )
    .await;
    common::insert_usage_log(
        &pool,
        T,
        "g1",
        None,
        "llm",
        "m1",
        0,
        99.0,
        crate::common::db_now(),
    )
    .await;
    let g = common::load_resource_group(&pool, "g1").await;

    let r = usage::enforce_llm_quota(&pool, T, Some(&g), None, None, None).await;
    assert!(
        r.blocked,
        "a legacy `hard_stop: false` must not exempt a call from a $ cap"
    );
    assert_eq!(r.limit_type.as_deref(), Some("daily_budget"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_daily_requests_blocks_on_next(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    common::set_llm_global_limits(&pool, "g1", json!({"requests": {"daily_limit": 2}})).await;
    // 2 prior requests today; the block is `(count + 1) > limit` → (2+1)>2.
    common::insert_usage_log(
        &pool,
        T,
        "g1",
        None,
        "llm",
        "m1",
        1,
        0.0,
        crate::common::db_now(),
    )
    .await;
    common::insert_usage_log(
        &pool,
        T,
        "g1",
        None,
        "llm",
        "m1",
        1,
        0.0,
        crate::common::db_now(),
    )
    .await;
    let g = common::load_resource_group(&pool, "g1").await;

    let r = usage::enforce_llm_quota(&pool, T, Some(&g), None, None, None).await;
    assert!(r.blocked, "(2+1) > 2 must block the incoming request");
    assert_eq!(r.limit_type.as_deref(), Some("daily_requests"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_monthly_tokens_blocks(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    common::set_llm_global_limits(&pool, "g1", json!({"tokens": {"monthly_limit": 5000}})).await;
    common::insert_usage_log(
        &pool,
        T,
        "g1",
        None,
        "llm",
        "m1",
        5000,
        0.0,
        crate::common::db_now(),
    )
    .await;
    let g = common::load_resource_group(&pool, "g1").await;

    let r = usage::enforce_llm_quota(&pool, T, Some(&g), None, None, None).await;
    assert!(r.blocked);
    assert_eq!(r.limit_type.as_deref(), Some("monthly_tokens"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_yesterday_usage_excluded_from_daily(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    common::set_llm_global_limits(&pool, "g1", json!({"tokens": {"daily_limit": 1000}})).await;
    // 5000 tokens YESTERDAY — must not count toward today's daily window.
    let yesterday = crate::common::db_now() - Duration::days(1);
    common::insert_usage_log(&pool, T, "g1", None, "llm", "m1", 5000, 0.0, yesterday).await;
    let g = common::load_resource_group(&pool, "g1").await;

    let r = usage::enforce_llm_quota(&pool, T, Some(&g), None, None, None).await;
    assert!(
        !r.blocked,
        "yesterday's usage is outside today's daily window"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_hierarchy_parent_tightens_child(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "parent", T).await;
    common::seed_resource_group(&pool, "child", T).await;
    // Parent limit 500 is tighter than child's 1000 → effective = min = 500.
    common::set_llm_global_limits(&pool, "parent", json!({"tokens": {"daily_limit": 500}})).await;
    common::set_llm_global_limits(&pool, "child", json!({"tokens": {"daily_limit": 1000}})).await;
    common::set_group_parent(&pool, "child", "parent", "/parent/child", 1).await;
    // 600 on the child: under its own 1000 but over the inherited 500.
    common::insert_usage_log(
        &pool,
        T,
        "child",
        Some("/parent/child"),
        "llm",
        "m1",
        600,
        0.0,
        crate::common::db_now(),
    )
    .await;
    let g = common::load_resource_group(&pool, "child").await;

    let r = usage::enforce_llm_quota(&pool, T, Some(&g), None, None, None).await;
    assert!(
        r.blocked,
        "parent's tighter 500 limit must apply to the child"
    );
    assert_eq!(r.limit_type.as_deref(), Some("daily_tokens"));
    assert_eq!(
        r.max_limit.as_deref(),
        Some("500"),
        "effective limit is the parent's min"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_ancestor_token_rollup_blocks(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "org", T).await;
    common::seed_resource_group(&pool, "team-a", T).await;
    common::seed_resource_group(&pool, "team-b", T).await;
    // Org caps the WHOLE subtree at 1000 daily tokens (its OWN limit). Neither
    // child has its own limit.
    common::set_llm_global_limits(&pool, "org", json!({"tokens": {"daily_limit": 1000}})).await;
    common::set_group_parent(&pool, "org", "org", "/org", 0).await;
    common::set_group_parent(&pool, "team-a", "org", "/org/team-a", 1).await;
    common::set_group_parent(&pool, "team-b", "org", "/org/team-b", 1).await;

    // CRITICAL to what this test exercises: usage is SPLIT across siblings so
    // NO single child exceeds the limit it inherits from org (600 < 1000) — the
    // local check must pass for team-a. Only the SUBTREE aggregate
    // (600 + 600 = 1200) exceeds org's 1000 cap. That makes the block come from
    // `enforce_ancestor_budgets` ("daily_tokens_ancestor"), not the inherited
    // local limit. Putting all usage on one child would instead trip the local
    // inherited-limit check ("daily_tokens") — a different code path.
    common::insert_usage_log(
        &pool,
        T,
        "team-a",
        Some("/org/team-a"),
        "llm",
        "m1",
        600,
        0.0,
        crate::common::db_now(),
    )
    .await;
    common::insert_usage_log(
        &pool,
        T,
        "team-b",
        Some("/org/team-b"),
        "llm",
        "m1",
        600,
        0.0,
        crate::common::db_now(),
    )
    .await;
    let g = common::load_resource_group(&pool, "team-a").await;

    let r = usage::enforce_llm_quota(&pool, T, Some(&g), None, None, None).await;
    assert!(
        r.blocked,
        "descendant subtree usage must count against the ancestor's cap"
    );
    assert_eq!(
        r.limit_type.as_deref(),
        Some("daily_tokens_ancestor"),
        "block must be attributed to the ancestor rollup, not the local inherited limit"
    );
}

// ===========================================================================
// Skill
// ===========================================================================

#[sqlx::test(migrations = "./tests/migrations")]
async fn skill_no_limits_allows(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    let g = common::load_resource_group(&pool, "g1").await;

    let r = usage::enforce_skill_quota(&pool, T, Some(&g), None).await;
    assert!(!r.blocked);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn skill_daily_executions_blocks(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    common::set_skill_global_limits(&pool, "g1", json!({"execution": {"daily_limit": 2}})).await;
    common::insert_usage_log(
        &pool,
        T,
        "g1",
        None,
        "skill",
        "s1",
        0,
        0.0,
        crate::common::db_now(),
    )
    .await;
    common::insert_usage_log(
        &pool,
        T,
        "g1",
        None,
        "skill",
        "s1",
        0,
        0.0,
        crate::common::db_now(),
    )
    .await;
    let g = common::load_resource_group(&pool, "g1").await;

    let r = usage::enforce_skill_quota(&pool, T, Some(&g), None).await;
    assert!(r.blocked, "(2+1) > 2 daily skill executions must block");
    assert_eq!(r.limit_type.as_deref(), Some("daily_skill_executions"));
}

// ===========================================================================
// MCP — the quota path enforces ONLY ancestor budgets
// ===========================================================================

#[sqlx::test(migrations = "./tests/migrations")]
async fn mcp_no_limits_allows(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    let g = common::load_resource_group(&pool, "g1").await;

    let r = usage::enforce_mcp_quota(&pool, T, Some(&g)).await;
    assert!(!r.blocked);
}

// ===========================================================================
// Cold-start seed — counters seeded from the usage_logs ledger
// ===========================================================================

/// Reconcile heals every counter UP to the ledger and never down.
///
/// This test used to assert the opposite of step 3 — that a live counter
/// behind the ledger is "never reseeded" — which pinned the overspend bug: a
/// counter that lost increments while Redis was erroring stayed under-counted
/// for the rest of the window.
#[sqlx::test(migrations = "./tests/migrations")]
async fn reconcile_heals_counters_up_to_the_ledger_and_never_down(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    // Ledger: 500 tokens for group g1 today.
    common::insert_usage_log(
        &pool,
        T,
        "g1",
        None,
        "llm",
        "m1",
        500,
        0.0,
        crate::common::db_now(),
    )
    .await;

    // In-memory counters (no Redis in tests) — the heal logic is identical;
    // the Redis script is exercised in `quota_counter_redis.rs`.
    let us = UsageState::new(None);

    // 1. Cold counter: seeded from the ledger.
    let changed1 = usage::reconcile_llm_shadow_counters(&pool, &us).await;
    assert!(changed1 >= 1, "cold counters are seeded from the ledger");
    assert_eq!(
        us.read_usage("tok:grp:g1", QuotaWindow::Day).await,
        500,
        "daily token counter seeded to the ledger SUM"
    );

    // 2. Counter equals the ledger: nothing to do.
    let changed2 = usage::reconcile_llm_shadow_counters(&pool, &us).await;
    assert_eq!(changed2, 0, "a counter already at the ledger is left alone");

    // 3. The ledger grows by 300 but the counter missed those increments (a
    //    settle lost while Redis was erroring): the heal raises it.
    common::insert_usage_log(
        &pool,
        T,
        "g1",
        None,
        "llm",
        "m1",
        300,
        0.0,
        crate::common::db_now(),
    )
    .await;
    let changed3 = usage::reconcile_llm_shadow_counters(&pool, &us).await;
    assert!(changed3 >= 1, "an under-counted live counter is healed");
    assert_eq!(
        us.read_usage("tok:grp:g1", QuotaWindow::Day).await,
        800,
        "the counter is raised to the ledger, not left under-counted"
    );

    // 4. The counter is AHEAD of the ledger (reservations in flight, rows not
    //    yet flushed): the heal never lowers it.
    us.set_usage("tok:grp:g1", QuotaWindow::Day, 1_000).await;
    let changed4 = usage::reconcile_llm_shadow_counters(&pool, &us).await;
    assert_eq!(
        changed4, 0,
        "a counter ahead of the ledger is never lowered"
    );
    assert_eq!(us.read_usage("tok:grp:g1", QuotaWindow::Day).await, 1_000);
}

/// CUTOVER: with a counter source supplied, enforcement reads the O(1) counter
/// instead of the usage_logs ledger. Proven by making the two disagree: ledger
/// is empty, the counter is at the limit → counter-mode blocks, ledger-mode
/// allows.
#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_cutover_enforces_from_counter_not_ledger(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    common::set_llm_global_limits(&pool, "g1", json!({"tokens": {"daily_limit": 1000}})).await;
    // Ledger stays EMPTY (no usage_logs rows) → SUM = 0.
    let g = common::load_resource_group(&pool, "g1").await;

    let us = UsageState::new(None);
    // Drive the daily token counter to the limit.
    us.set_usage("tok:grp:g1", QuotaWindow::Day, 1000).await;

    // Ledger mode (None): ledger is empty → allowed.
    let ledger = usage::enforce_llm_quota(&pool, T, Some(&g), None, None, None).await;
    assert!(!ledger.blocked, "ledger SUM is 0 → allowed");

    // Counter mode (Some): counter is at the limit → blocked, despite empty ledger.
    let counter = usage::enforce_llm_quota(&pool, T, Some(&g), None, Some(&us), None).await;
    assert!(
        counter.blocked,
        "counter at limit → blocked even though ledger is empty"
    );
    assert_eq!(counter.limit_type.as_deref(), Some("daily_tokens"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn mcp_ancestor_calls_blocks(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "org", T).await;
    common::seed_resource_group(&pool, "team", T).await;
    // Org caps daily MCP calls for the subtree.
    common::set_mcp_global_limits(&pool, "org", json!({"frequency": {"max_calls_per_day": 2}}))
        .await;
    common::set_group_parent(&pool, "org", "org", "/org", 0).await;
    common::set_group_parent(&pool, "team", "org", "/org/team", 1).await;
    // 2 prior MCP calls in the subtree; (2+1) > 2 blocks the next.
    common::insert_usage_log(
        &pool,
        T,
        "team",
        Some("/org/team"),
        "mcp",
        "srv1",
        0,
        0.0,
        crate::common::db_now(),
    )
    .await;
    common::insert_usage_log(
        &pool,
        T,
        "team",
        Some("/org/team"),
        "mcp",
        "srv1",
        0,
        0.0,
        crate::common::db_now(),
    )
    .await;
    let g = common::load_resource_group(&pool, "team").await;

    let r = usage::enforce_mcp_quota(&pool, T, Some(&g)).await;
    assert!(
        r.blocked,
        "descendant MCP calls roll up to the ancestor cap"
    );
    assert_eq!(r.limit_type.as_deref(), Some("daily_mcp_calls_ancestor"));
}

// ===========================================================================
// Per-resource `.limits` OVERRIDE backfill — the binding-row override is
// merged (most-restrictive) with the group-wide limit. These paths worked but
// were untested (no fixture set a binding-row `.limits`), so a regression
// would have been silent.
// ===========================================================================

#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_per_model_limits_override_narrows_group(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    common::seed_llm_provider(&pool, "p1", T, "openai").await;
    common::seed_llm_model(&pool, "m1", T, "p1", "gpt").await;
    common::bind_llm_model_to_rg(&pool, "m1", "g1").await;
    // Loose group-wide cap, TIGHT per-model override (Models → Limits).
    common::set_llm_global_limits(&pool, "g1", json!({"tokens": {"daily_limit": 1_000_000}})).await;
    common::set_rg_model_limits(&pool, "m1", "g1", json!({"tokens": {"daily_limit": 1000}})).await;
    common::insert_usage_log(
        &pool,
        T,
        "g1",
        None,
        "llm",
        "m1",
        1000,
        0.0,
        crate::common::db_now(),
    )
    .await;
    let g = common::load_resource_group(&pool, "g1").await;

    let r = usage::enforce_llm_quota(&pool, T, Some(&g), Some("m1"), None, None).await;
    assert!(
        r.blocked,
        "per-model daily_tokens override (1000) must narrow the group cap and block at used=1000"
    );
    assert_eq!(r.limit_type.as_deref(), Some("per_model_daily_tokens"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn skill_per_skill_limits_override_narrows_group(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    common::seed_agent_skill(&pool, "s1", T, "skill-one", "# skill", "desc").await;
    common::bind_skill_to_rg(&pool, "s1", "g1").await;
    // Loose group-wide cap, TIGHT per-skill override.
    common::set_skill_global_limits(
        &pool,
        "g1",
        json!({"execution": {"daily_limit": 1_000_000}}),
    )
    .await;
    common::set_rg_skill_limits(&pool, "s1", "g1", json!({"execution": {"daily_limit": 2}})).await;
    common::insert_usage_log(
        &pool,
        T,
        "g1",
        None,
        "skill",
        "s1",
        0,
        0.0,
        crate::common::db_now(),
    )
    .await;
    common::insert_usage_log(
        &pool,
        T,
        "g1",
        None,
        "skill",
        "s1",
        0,
        0.0,
        crate::common::db_now(),
    )
    .await;
    let g = common::load_resource_group(&pool, "g1").await;

    let r = usage::enforce_skill_quota(&pool, T, Some(&g), Some("s1")).await;
    assert!(
        r.blocked,
        "per-skill daily executions override (2) must narrow the group cap and block at (2+1)>2"
    );
    assert_eq!(r.limit_type.as_deref(), Some("per_skill_daily_executions"));
}

// ===========================================================================
// Reservation at admission, against a real database
//
// The golden-master tests above pass `None` for the reservation: they ask
// "would this be allowed", which must stay a pure read. These drive the
// admitting path, where the answer has to account for requests already in
// flight. The defect they pin: the counters only moved after the response, so
// concurrent callers at a cap all read the same pre-request snapshot and all
// passed, and the overshoot scaled with client concurrency.
// ===========================================================================

/// A call whose worst case is exactly 5c: 500 input tokens at 100 µ$ each,
/// no output.
const EST: Shape = Shape {
    in_tokens: 500,
    in_estimate: 125,
    out_tokens: 0,
    min_out: 0,
    choices: 1,
    in_micros_per_token: 100.0,
    out_micros_per_token: 0.0,
};

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_concurrent_second_request_cannot_reuse_the_first_ones_headroom(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    // $1.00/day, $0.94 already spent — room for one 5c call, not two.
    common::set_llm_global_limits(&pool, "g1", json!({"budget": {"daily_limit_usd": 1.0}})).await;
    let counters = UsageState::new(None);
    counters
        .set_usage("cost:grp:g1", QuotaWindow::Day, 940_000)
        .await;
    let g = common::load_resource_group(&pool, "g1").await;

    let first = usage::enforce_llm_quota(
        &pool,
        T,
        Some(&g),
        Some("m1"),
        Some(&counters),
        Some((EST, &HoldId::for_test("rq-a"))),
    )
    .await;
    assert!(!first.blocked, "first request fits under the cap");

    // No response has completed, so nothing has been committed. Under
    // read-then-decide this read 940_000 again and passed.
    let second = usage::enforce_llm_quota(
        &pool,
        T,
        Some(&g),
        Some("m1"),
        Some(&counters),
        Some((EST, &HoldId::for_test("rq-b"))),
    )
    .await;
    assert!(
        second.blocked,
        "the first request's in-flight spend must be visible to the second"
    );
    assert_eq!(second.limit_type.as_deref(), Some("daily_budget"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_burst_at_the_cap_admits_only_what_fits(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    common::set_llm_global_limits(&pool, "g1", json!({"budget": {"daily_limit_usd": 1.0}})).await;
    let counters = UsageState::new(None);
    counters
        .set_usage("cost:grp:g1", QuotaWindow::Day, 950_000)
        .await;
    let g = common::load_resource_group(&pool, "g1").await;

    let mut admitted = 0;
    for i in 0..20 {
        let r = usage::enforce_llm_quota(
            &pool,
            T,
            Some(&g),
            Some("m1"),
            Some(&counters),
            Some((EST, &HoldId::for_test(&format!("rq-burst-{i}")))),
        )
        .await;
        if !r.blocked {
            admitted += 1;
        }
    }
    assert_eq!(
        admitted, 1,
        "a 20-deep burst with room for exactly one 5c call admits one — this \
         was 20 before the reservation existed"
    );
    assert_eq!(
        counters.read_usage("cost:grp:g1", QuotaWindow::Day).await,
        1_000_000,
        "and the cap is reached, never passed"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_admitted_request_that_settles_leaves_only_its_real_spend(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    common::set_llm_global_limits(&pool, "g1", json!({"budget": {"daily_limit_usd": 10.0}})).await;
    let counters = UsageState::new(None);
    let g = common::load_resource_group(&pool, "g1").await;

    let r = usage::enforce_llm_quota(
        &pool,
        T,
        Some(&g),
        Some("m1"),
        Some(&counters),
        Some((EST, &HoldId::for_test("rq-settle"))),
    )
    .await;
    assert!(!r.blocked);
    assert_eq!(
        counters.read_usage("cost:grp:g1", QuotaWindow::Day).await,
        50_000,
        "the worst case is held while the call is in flight"
    );

    // The call turned out to cost 1c, not the 5c reserved.
    counters
        .finish_hold(
            "rq-settle",
            Finish::Settle(Actual {
                in_tokens: 100,
                out_tokens: 0,
                cost_micros: 10_000,
            }),
            &[],
        )
        .await
        .unwrap();
    assert_eq!(
        counters.read_usage("cost:grp:g1", QuotaWindow::Day).await,
        10_000,
        "once settled the counter carries actual spend, never the reservation"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_group_with_no_configured_limits_is_accounted_never_refused(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    let counters = UsageState::new(None);
    let g = common::load_resource_group(&pool, "g1").await;

    let r = usage::enforce_llm_quota(
        &pool,
        T,
        Some(&g),
        Some("m1"),
        Some(&counters),
        Some((EST, &HoldId::for_test("rq-unlimited"))),
    )
    .await;
    assert!(!r.blocked);
    // The group's own counters carry its usage — the dashboards and the
    // reconcile read them — and a call that ends without running gives it
    // back.
    assert_eq!(
        counters.read_usage("cost:grp:g1", QuotaWindow::Day).await,
        50_000
    );
    counters
        .finish_hold("rq-unlimited", Finish::Release, &[])
        .await
        .unwrap();
    assert_eq!(
        counters.read_usage("cost:grp:g1", QuotaWindow::Day).await,
        0
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_request_refused_by_an_earlier_gate_takes_no_reservation(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    // Token cap already exhausted, so the snapshot path refuses before the
    // admission is ever reached.
    common::set_llm_global_limits(&pool, "g1", json!({"tokens": {"daily_limit": 100}})).await;
    let counters = UsageState::new(None);
    counters
        .set_usage("tok:grp:g1", QuotaWindow::Day, 100)
        .await;
    let g = common::load_resource_group(&pool, "g1").await;

    let r = usage::enforce_llm_quota(
        &pool,
        T,
        Some(&g),
        Some("m1"),
        Some(&counters),
        Some((EST, &HoldId::for_test("rq-refused"))),
    )
    .await;
    assert!(r.blocked);
    assert!(
        !counters.holds_live("rq-refused"),
        "a refusal must not leave a hold behind — it would tighten the cap for \
         every request after it"
    );
    assert_eq!(counters.read_usage("req:grp:g1", QuotaWindow::Day).await, 0);
}

// ===========================================================================
// The tenant-wide A2A rule must bind a caller with no group
//
// The outbound A2A path reaches the tenant ceiling through the group cache's
// effective view, which merges the caller's chain up to the root. A caller
// with no resolvable group has no chain, so `group_data` was `None` and every
// governance check was skipped — while the comment at that site claimed the
// tenant-wide rule still applied "alone". It did not: only
// `card.tenant_limits` survived, so the remote allow-list and the data
// classifications did not bind such a caller at all.
//
// These cover the lookup that closes it. The refusal decision itself is
// `a2a_governance::Policy::judge`, unit-tested where it lives; what was
// missing was any route to it for a group-less caller.
// ===========================================================================

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_tenant_ceiling_is_readable_without_a_group(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "root-a", T).await;
    sqlx::query(
        "UPDATE resource_groups SET parent_group_id = NULL, \
         a2a_global_governance = $2 WHERE id = $1",
    )
    .bind("root-a")
    .bind(json!({"remote": {"allowed_hosts": ["partner.example.com"]}}))
    .execute(&pool)
    .await
    .expect("set tenant ceiling");

    let found = brutor_gateway_core::services::a2a_governance::tenant_root_policies(&pool, T)
        .await
        .expect("the tenant's root policy is readable");
    assert_eq!(
        found.len(),
        1,
        "a group-less caller must still be able to reach the tenant's rule"
    );
    assert!(found[0].get("remote").is_some());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_tenant_with_no_ceiling_configured_yields_nothing_to_enforce(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "root-b", T).await;
    sqlx::query("UPDATE resource_groups SET parent_group_id = NULL WHERE id = $1")
        .bind("root-b")
        .execute(&pool)
        .await
        .expect("clear parent");

    // No rule configured => nothing is refused. The fix must not start
    // blocking group-less traffic on tenants that never set a ceiling.
    assert!(
        brutor_gateway_core::services::a2a_governance::tenant_root_policies(&pool, T)
            .await
            .expect("the tenant's root policy is readable")
            .is_empty()
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_child_groups_rule_is_not_mistaken_for_the_tenant_ceiling(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "root-c", T).await;
    common::seed_resource_group(&pool, "child-c", T).await;
    sqlx::query("UPDATE resource_groups SET parent_group_id = NULL WHERE id = $1")
        .bind("root-c")
        .execute(&pool)
        .await
        .expect("root");
    sqlx::query(
        "UPDATE resource_groups SET parent_group_id = $2, a2a_global_governance = $3 \
         WHERE id = $1",
    )
    .bind("child-c")
    .bind("root-c")
    .bind(json!({"remote": {"allowed_hosts": ["child-only.example.com"]}}))
    .execute(&pool)
    .await
    .expect("child rule");

    // The ceiling is the ROOT's rule. A rule set on a child governs that
    // subtree, and applying it to a caller outside the subtree would refuse
    // traffic on a policy that was never meant to reach them.
    assert!(
        brutor_gateway_core::services::a2a_governance::tenant_root_policies(&pool, T)
            .await
            .expect("the tenant's root policy is readable")
            .is_empty(),
        "a child group's rule must not be read as the tenant ceiling"
    );
}

// ---------------------------------------------------------------------------
// An unreadable ledger never pins a counter at "over limit"
// ---------------------------------------------------------------------------

/// Makes the token SUM fail while the scope list (same table, other columns)
/// still reads: the partial failure a statement timeout or a dropped
/// connection produces mid-pass.
async fn make_token_sums_fail(pool: &PgPool) {
    sqlx::query("ALTER TABLE usage_logs RENAME COLUMN total_tokens TO total_tokens_unreadable")
        .execute(pool)
        .await
        .unwrap();
}

async fn seed_ledger_500(pool: &PgPool) {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, "g1", T).await;
    common::insert_usage_log(
        pool,
        T,
        "g1",
        None,
        "llm",
        "m1",
        500,
        0.0,
        crate::common::db_now(),
    )
    .await;
}

/// Was: the token SUM failed closed to `i64::MAX` and the raise-only heal
/// wrote it into the counter, so every later request of the group was
/// refused as over quota until the window rolled over, long after the
/// database recovered.
#[sqlx::test(migrations = "./tests/migrations")]
async fn reconcile_skips_an_unreadable_ledger_instead_of_pinning_the_counter(pool: PgPool) {
    seed_ledger_500(&pool).await;
    let us = UsageState::new(None);
    us.set_usage("tok:grp:g1", QuotaWindow::Day, 200).await;

    make_token_sums_fail(&pool).await;
    usage::reconcile_llm_shadow_counters(&pool, &us).await;
    assert_eq!(
        us.read_usage("tok:grp:g1", QuotaWindow::Day).await,
        200,
        "a failed ledger read must leave the counter as it was"
    );
    assert_eq!(us.read_usage("tok:grp:g1", QuotaWindow::Month).await, 0);

    // The database recovers: the next pass heals to the real ledger.
    sqlx::query("ALTER TABLE usage_logs RENAME COLUMN total_tokens_unreadable TO total_tokens")
        .execute(&pool)
        .await
        .unwrap();
    usage::reconcile_llm_shadow_counters(&pool, &us).await;
    assert_eq!(us.read_usage("tok:grp:g1", QuotaWindow::Day).await, 500);
}

/// Was: backfill OVERWRITES counters, so the same failure set the counter to
/// `i64::MAX`.
#[sqlx::test(migrations = "./tests/migrations")]
async fn backfill_never_overwrites_a_counter_from_an_unreadable_ledger(pool: PgPool) {
    seed_ledger_500(&pool).await;
    let us = UsageState::new(None);
    us.set_usage("tok:grp:g1", QuotaWindow::Day, 700).await;
    us.set_usage("cost:grp:g1", QuotaWindow::Day, 9_000).await;

    make_token_sums_fail(&pool).await;
    usage::backfill_llm_shadow_counters(&pool, &us).await;
    assert_eq!(us.read_usage("tok:grp:g1", QuotaWindow::Day).await, 700);
    assert_eq!(us.read_usage("cost:grp:g1", QuotaWindow::Day).await, 9_000);
}

/// Control: the request path keeps failing closed. A request that has to
/// read the ledger and cannot is refused (this one request only).
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_request_that_cannot_read_the_ledger_is_still_refused(pool: PgPool) {
    seed_ledger_500(&pool).await;
    common::set_llm_global_limits(&pool, "g1", json!({"tokens": {"daily_limit": 1000}})).await;
    let g = common::load_resource_group(&pool, "g1").await;

    let ok = usage::enforce_llm_quota(&pool, T, Some(&g), None, None, None).await;
    assert!(!ok.blocked, "control: 500 of 1000 is allowed");

    make_token_sums_fail(&pool).await;
    let refused = usage::enforce_llm_quota(&pool, T, Some(&g), None, None, None).await;
    assert!(
        refused.blocked,
        "an unreadable ledger must refuse the request"
    );
}
