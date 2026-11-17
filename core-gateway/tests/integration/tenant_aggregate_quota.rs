// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # TENANT-WIDE aggregate enforcement — the headline coverage gap.
//!
//! Every per-resource control also has a TENANT-WIDE variant: one aggregate
//! across ALL resource groups for a given resource (`llm_model.tenant_limits`,
//! `mcp_servers.tenant_limits`, `agent_skills.tenant_limits`). It is enforced
//! IN ADDITION to each RG's own limits — an operator's tenant cap must hold no
//! matter how many RGs use the resource. This scope was enforced everywhere but
//! tested NOWHERE.
//!
//! The shared trick in every test below: seed TWO resource groups, split usage
//! across BOTH so that NEITHER single group exceeds the (folded) per-group cap,
//! and assert the request is still blocked by the TENANT aggregate.
//!
//! Notes on how the folding interacts:
//!   * `collect_effective_*_limits` folds the resource's `tenant_limits` into
//!     each group's effective limits (min-wins). So each group's OWN scope
//!     inherits the tenant cap too — the tests keep each single group *within*
//!     that cap (called once, or usage kept under it) so the block is
//!     unambiguously the tenant aggregate, not the group scope.
//!   * Ledger-sourced quota (tokens/requests/budget) sums `usage_logs` by
//!     `resource_id = model/skill`, so the usage-log `resource_id` must match.
//!   * Rate/throughput/concurrency use the shared in-memory `UsageState`
//!     counters keyed `…:t:{tenant}:…` so they aggregate regardless of RG.
//!
//! A2A quota (`enforce_a2a_quota` / `enforce_a2a_tenant_quota`) is DB-free —
//! it takes the limits JSON as a parameter — so its RG-scope + tenant-scope
//! coverage lives in the library unit tests (`services::usage::tests`), not
//! here.

use serde_json::json;
use sqlx::PgPool;

use brutor_gateway_core::services::usage;

use crate::common;

const T: &str = "tenant-agg";

// ===========================================================================
// LLM — tenant-wide token / request / budget quota (ledger-sourced)
// ===========================================================================

#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_tenant_token_quota_blocks_on_cross_group_aggregate(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    common::seed_resource_group(&pool, "g2", T).await;
    common::seed_llm_provider(&pool, "p1", T, "openai").await;
    common::seed_llm_model(&pool, "m1", T, "p1", "gpt").await;
    // Tenant-wide cap only (no group limits). Folded in, each group's own cap
    // becomes 1000 too — but 600 per group stays under it.
    common::set_llm_tenant_limits(&pool, "m1", json!({"tokens": {"daily_limit": 1000}})).await;

    // Only 600 on g1 so far — tenant aggregate 600 < 1000 → allowed.
    common::insert_usage_log(
        &pool,
        T,
        "g1",
        None,
        "llm",
        "m1",
        600,
        0.0,
        crate::common::db_now(),
    )
    .await;
    let g1 = common::load_resource_group(&pool, "g1").await;
    let before = usage::enforce_llm_quota(&pool, T, Some(&g1), Some("m1"), None, None).await;
    assert!(
        !before.blocked,
        "single group at 600 < 1000 tenant cap must be allowed"
    );

    // Add 600 on g2 — neither group exceeds its own 1000 cap, but the tenant
    // aggregate is now 1200 > 1000.
    common::insert_usage_log(
        &pool,
        T,
        "g2",
        None,
        "llm",
        "m1",
        600,
        0.0,
        crate::common::db_now(),
    )
    .await;
    let r = usage::enforce_llm_quota(&pool, T, Some(&g1), Some("m1"), None, None).await;
    assert!(
        r.blocked,
        "tenant aggregate (1200) over the tenant cap (1000) must block even though neither group's own usage does"
    );
    assert_eq!(r.limit_type.as_deref(), Some("daily_tokens"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_tenant_request_quota_blocks_on_cross_group_aggregate(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    common::seed_resource_group(&pool, "g2", T).await;
    common::seed_llm_provider(&pool, "p1", T, "openai").await;
    common::seed_llm_model(&pool, "m1", T, "p1", "gpt").await;
    common::set_llm_tenant_limits(&pool, "m1", json!({"requests": {"daily_limit": 3}})).await;

    // 2 requests on g1, 2 on g2 → tenant count 4. Each group alone is at 2,
    // and (2+1) > 3 is false → each group passes; (4+1) > 3 → tenant blocks.
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
    common::insert_usage_log(
        &pool,
        T,
        "g2",
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
        "g2",
        None,
        "llm",
        "m1",
        1,
        0.0,
        crate::common::db_now(),
    )
    .await;
    let g1 = common::load_resource_group(&pool, "g1").await;

    let r = usage::enforce_llm_quota(&pool, T, Some(&g1), Some("m1"), None, None).await;
    assert!(
        r.blocked,
        "tenant request aggregate (4) → (4+1)>3 must block the incoming request"
    );
    assert_eq!(r.limit_type.as_deref(), Some("daily_requests"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_tenant_budget_hard_stop_blocks_on_cross_group_aggregate(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    common::seed_resource_group(&pool, "g2", T).await;
    common::seed_llm_provider(&pool, "p1", T, "openai").await;
    common::seed_llm_model(&pool, "m1", T, "p1", "gpt").await;
    common::set_llm_tenant_limits(
        &pool,
        "m1",
        json!({"budget": {"daily_limit_usd": 10.0, "hard_stop": true}}),
    )
    .await;

    // $6 on each group → each is at 60% (allowed), tenant is at 120% with
    // hard_stop → block.
    common::insert_usage_log(
        &pool,
        T,
        "g1",
        None,
        "llm",
        "m1",
        0,
        6.0,
        crate::common::db_now(),
    )
    .await;
    common::insert_usage_log(
        &pool,
        T,
        "g2",
        None,
        "llm",
        "m1",
        0,
        6.0,
        crate::common::db_now(),
    )
    .await;
    let g1 = common::load_resource_group(&pool, "g1").await;

    let r = usage::enforce_llm_quota(&pool, T, Some(&g1), Some("m1"), None, None).await;
    assert!(
        r.blocked,
        "tenant $-aggregate (12) over budget (10) with hard_stop must block"
    );
    assert_eq!(r.limit_type.as_deref(), Some("daily_budget"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_tenant_budget_blocks_over_even_with_a_stored_hard_stop_false(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    common::seed_resource_group(&pool, "g2", T).await;
    common::seed_llm_provider(&pool, "p1", T, "openai").await;
    common::seed_llm_model(&pool, "m1", T, "p1", "gpt").await;
    // A stored `hard_stop: false` no longer buys an exemption.
    //
    // The opt-out was removed platform-wide ("every configured cap now blocks
    // at 100%", services::usage), because a dollar cap that can be configured
    // not to stop anything is not a cap. This test asserted the old semantics
    // and had been failing since the removal shipped — the tenant-level twin
    // of the group-level test fixed alongside it. Rows written before the
    // removal still carry the flag, so what matters now is that it cannot
    // resurrect the exemption.
    common::set_llm_tenant_limits(
        &pool,
        "m1",
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
        30.0,
        crate::common::db_now(),
    )
    .await;
    common::insert_usage_log(
        &pool,
        T,
        "g2",
        None,
        "llm",
        "m1",
        0,
        30.0,
        crate::common::db_now(),
    )
    .await;
    let g1 = common::load_resource_group(&pool, "g1").await;

    let r = usage::enforce_llm_quota(&pool, T, Some(&g1), Some("m1"), None, None).await;
    assert!(
        r.blocked,
        "a legacy `hard_stop: false` must not exempt a call from a tenant $ cap"
    );
}

// ===========================================================================
// LLM — tenant-wide rate (RPM) + concurrency (in-memory counters)
// ===========================================================================

#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_tenant_rate_blocks_across_two_groups(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    common::seed_resource_group(&pool, "g2", T).await;
    common::seed_llm_provider(&pool, "p1", T, "openai").await;
    common::seed_llm_model(&pool, "m1", T, "p1", "gpt").await;
    // One call/min tenant-wide for this model, shared across all RGs.
    common::set_llm_tenant_limits(
        &pool,
        "m1",
        json!({"frequency": {"max_calls_per_minute": 1}}),
    )
    .await;

    let g1 = common::load_resource_group(&pool, "g1").await;
    let g2 = common::load_resource_group(&pool, "g2").await;
    let state = common::build_test_state(pool);

    // First call via g1 — its own group bucket + the shared tenant bucket each
    // spend their single token. Allowed.
    let first = usage::enforce_llm_rate_limits(&state.usage_state, &state.db, &g1, "m1").await;
    assert!(first.is_none(), "first call via g1 allowed; got {first:?}");

    // Second call via g2 — g2's OWN group bucket is fresh, but the shared
    // tenant bucket (llm:t:{tenant}:m:m1) is exhausted → block at tenant scope.
    let second = usage::enforce_llm_rate_limits(&state.usage_state, &state.db, &g2, "m1").await;
    let block = second.expect("second call via a different group must hit the tenant rate cap");
    assert_eq!(
        block.scope, "tenant",
        "the block must come from the tenant aggregate scope, not g2's own bucket"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_tenant_concurrency_blocks_and_releases_across_two_groups(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    common::seed_resource_group(&pool, "g2", T).await;
    common::seed_llm_provider(&pool, "p1", T, "openai").await;
    common::seed_llm_model(&pool, "m1", T, "p1", "gpt").await;
    // One simultaneous request tenant-wide for this model.
    common::set_llm_tenant_limits(&pool, "m1", json!({"concurrency": {"max_concurrent": 1}})).await;

    let g1 = common::load_resource_group(&pool, "g1").await;
    let g2 = common::load_resource_group(&pool, "g2").await;
    let state = common::build_test_state(pool);

    // Acquire via g1 — takes g1's own group slot + the shared tenant slot.
    let guard1 = usage::acquire_llm_concurrency(&state.usage_state, &state.db, &g1, "m1", None)
        .await
        .expect("first acquire (via g1) must succeed");

    // Acquire via g2 — g2's own group slot is free, but the shared tenant slot
    // (llm:t:{tenant}:m:m1) is taken → Err at tenant scope. (LlmConcurrencyGuard
    // is not Debug, so match rather than unwrap_err.)
    match usage::acquire_llm_concurrency(&state.usage_state, &state.db, &g2, "m1", None).await {
        Ok(_) => panic!("second acquire (via g2) must be rejected by the tenant cap"),
        Err(err) => {
            assert_eq!(
                err.scope, "tenant",
                "rejection must be at the tenant concurrency scope"
            );
            assert_eq!(err.limit_type, "concurrency");
        }
    }

    // Dropping guard1 releases the tenant slot (fire-and-forget on Drop).
    drop(guard1);

    // Poll until a fresh acquire via g2 succeeds — proving the tenant_key slot
    // was released on drop.
    let mut reacquired = None;
    for _ in 0..20 {
        tokio::time::sleep(tokio::time::Duration::from_millis(10)).await;
        if let Ok(g) =
            usage::acquire_llm_concurrency(&state.usage_state, &state.db, &g2, "m1", None).await
        {
            reacquired = Some(g);
            break;
        }
    }
    assert!(
        reacquired.is_some(),
        "dropping the first guard must release the tenant slot so g2 can acquire"
    );
}

// ===========================================================================
// MCP — tenant-wide + tenant-tool rate (mcp_servers.tenant_limits)
// ===========================================================================

#[sqlx::test(migrations = "./tests/migrations")]
async fn mcp_tenant_rate_blocks_across_two_groups(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    common::seed_resource_group(&pool, "g2", T).await;
    common::seed_mcp_server(&pool, "ms1", T, "server").await;
    // Server-wide tenant cap: one call/min across all RGs.
    common::set_mcp_tenant_limits(
        &pool,
        "ms1",
        json!({"frequency": {"max_calls_per_minute": 1}}),
    )
    .await;

    let g1 = common::load_resource_group(&pool, "g1").await;
    let g2 = common::load_resource_group(&pool, "g2").await;
    let state = common::build_test_state(pool);

    let first =
        usage::enforce_mcp_rate_limits(&state.usage_state, &state.db, &g1, "ms1", None, "any_tool")
            .await;
    assert!(
        first.is_none(),
        "first MCP call via g1 allowed; got {first:?}"
    );

    let second =
        usage::enforce_mcp_rate_limits(&state.usage_state, &state.db, &g2, "ms1", None, "any_tool")
            .await;
    let block = second.expect("second MCP call via a different group must hit the tenant cap");
    assert_eq!(
        block.scope, "tenant",
        "block must be at the server-wide tenant scope"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn mcp_tenant_tool_rate_blocks_across_two_groups(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    common::seed_resource_group(&pool, "g2", T).await;
    common::seed_mcp_server(&pool, "ms1", T, "server").await;
    // Per-tool tenant cap: one `report` call/min across all RGs.
    common::set_mcp_tenant_limits(
        &pool,
        "ms1",
        json!({"per_tool": {"report": {"max_calls_per_minute": 1}}}),
    )
    .await;

    let g1 = common::load_resource_group(&pool, "g1").await;
    let g2 = common::load_resource_group(&pool, "g2").await;
    let state = common::build_test_state(pool);

    let first =
        usage::enforce_mcp_rate_limits(&state.usage_state, &state.db, &g1, "ms1", None, "report")
            .await;
    assert!(
        first.is_none(),
        "first `report` call via g1 allowed; got {first:?}"
    );

    let second =
        usage::enforce_mcp_rate_limits(&state.usage_state, &state.db, &g2, "ms1", None, "report")
            .await;
    let block =
        second.expect("second `report` call via a different group must hit the tenant-tool cap");
    assert_eq!(
        block.scope, "tenant-tool",
        "block must be at the per-tool tenant scope"
    );

    // A different tool is unconstrained — the tenant cap is per-tool.
    let other =
        usage::enforce_mcp_rate_limits(&state.usage_state, &state.db, &g1, "ms1", None, "search")
            .await;
    assert!(
        other.is_none(),
        "unrelated tool must not be blocked by the `report` tenant cap; got {other:?}"
    );
}

// ===========================================================================
// Skill — tenant-wide execution quota (agent_skills.tenant_limits)
// ===========================================================================

#[sqlx::test(migrations = "./tests/migrations")]
async fn skill_tenant_execution_quota_blocks_across_two_groups(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    common::seed_resource_group(&pool, "g2", T).await;
    common::seed_agent_skill(&pool, "s1", T, "skill-one", "# skill", "desc").await;
    common::set_skill_tenant_limits(&pool, "s1", json!({"execution": {"daily_limit": 3}})).await;

    // 2 execs on g1, 2 on g2 → tenant count 4. Each group alone at 2:
    // (2+1)>3 false → allowed; (4+1)>3 → tenant blocks.
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
    common::insert_usage_log(
        &pool,
        T,
        "g2",
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
        "g2",
        None,
        "skill",
        "s1",
        0,
        0.0,
        crate::common::db_now(),
    )
    .await;
    let g1 = common::load_resource_group(&pool, "g1").await;

    let r = usage::enforce_skill_quota(&pool, T, Some(&g1), Some("s1")).await;
    assert!(
        r.blocked,
        "tenant skill-execution aggregate (4) → (4+1)>3 must block even though neither group exceeds"
    );
    assert_eq!(r.limit_type.as_deref(), Some("daily_skill_executions"));
}
