// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! DB-integration test for the per-tool MCP rate-limit path.
//!
//! Wires up `usage::enforce_mcp_rate_limits` against a real Postgres
//! with seeded resource-group limits, then exercises the
//! consume/block cycle. This covers:
//!
//! - `collect_effective_limits` reading `mcp_global_limits` JSON
//! - `freq_at` navigating `per_tool.{tool_name}`
//! - The four-scope iteration (group / server / group-tool / server-tool)
//! - `UsageState` token-bucket state across calls
//!
//! The in-source `#[cfg(test)]` tests in `services/usage.rs` cover
//! `enforce_rate_scope` in isolation (30+ cases). This test glues
//! the DB-read path to the enforcement decision — the gap those
//! unit tests can't reach because they have no DB.

use serde_json::json;
use sqlx::PgPool;

use brutor_gateway_core::services::usage::enforce_mcp_rate_limits;

use crate::common;

#[sqlx::test(migrations = "./tests/migrations")]
async fn per_tool_rate_limit_blocks_second_call(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-1").await;
    common::seed_resource_group(&pool, "rg-ops", "tenant-1").await;
    // Tightest possible per-tool rate: one call per minute. The first
    // call drains the token; the second hits the block.
    common::set_mcp_global_limits(
        &pool,
        "rg-ops",
        json!({
            "per_tool": {
                "generate_report": { "max_calls_per_minute": 1 }
            }
        }),
    )
    .await;

    let rg = common::load_resource_group(&pool, "rg-ops").await;
    let state = common::build_test_state(pool);

    // First call — bucket has one token. Returns None (allowed).
    let first = enforce_mcp_rate_limits(
        &state.usage_state,
        &state.db,
        &rg,
        "ms-test",
        None,
        "generate_report",
    )
    .await;
    assert!(
        first.is_none(),
        "first call must be allowed — bucket has one token; got {first:?}"
    );

    // Second call — bucket empty, no refill yet. Returns Some(block).
    let second = enforce_mcp_rate_limits(
        &state.usage_state,
        &state.db,
        &rg,
        "ms-test",
        None,
        "generate_report",
    )
    .await;
    let block = second.expect("second call must be blocked");
    assert_eq!(
        block.scope, "group-tool",
        "block must be at the group-tool scope (the most specific scope with a limit set)"
    );
    assert!(
        block.retry_after_secs >= 1,
        "Retry-After must be at least one second; got {}",
        block.retry_after_secs
    );
    assert!(
        !block.message.is_empty(),
        "block message must be populated for client display"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn per_tool_rate_limit_only_blocks_the_named_tool(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-1").await;
    common::seed_resource_group(&pool, "rg-ops", "tenant-1").await;
    common::set_mcp_global_limits(
        &pool,
        "rg-ops",
        json!({
            "per_tool": {
                "generate_report": { "max_calls_per_minute": 1 }
            }
        }),
    )
    .await;

    let rg = common::load_resource_group(&pool, "rg-ops").await;
    let state = common::build_test_state(pool);

    // Drain the bucket for generate_report.
    let _ = enforce_mcp_rate_limits(
        &state.usage_state,
        &state.db,
        &rg,
        "ms-test",
        None,
        "generate_report",
    )
    .await;
    let blocked = enforce_mcp_rate_limits(
        &state.usage_state,
        &state.db,
        &rg,
        "ms-test",
        None,
        "generate_report",
    )
    .await;
    assert!(blocked.is_some(), "named tool must be blocked");

    // A different tool with no per_tool config must not be blocked —
    // the limit is per-tool, not per-group.
    let unrelated = enforce_mcp_rate_limits(
        &state.usage_state,
        &state.db,
        &rg,
        "ms-test",
        None,
        "search",
    )
    .await;
    assert!(
        unrelated.is_none(),
        "unrelated tool 'search' must not be blocked by 'generate_report' limit; got {unrelated:?}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn no_limits_configured_allows_unlimited_calls(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-1").await;
    common::seed_resource_group(&pool, "rg-no-limits", "tenant-1").await;
    // No mcp_global_limits configured — the column is NULL.

    let rg = common::load_resource_group(&pool, "rg-no-limits").await;
    let state = common::build_test_state(pool);

    // Burst 100 calls — none should block.
    for i in 0..100 {
        let result = enforce_mcp_rate_limits(
            &state.usage_state,
            &state.db,
            &rg,
            "ms-test",
            None,
            "any_tool",
        )
        .await;
        assert!(
            result.is_none(),
            "call {i} blocked despite no limits configured: {result:?}"
        );
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn per_server_limits_override_blocks_second_call(pool: PgPool) {
    // The per-(group, server) `.limits` OVERRIDE on the binding row — NOT the
    // group-wide column — must be enforced. (This override path worked but had
    // no test, so a regression would have been silent.)
    common::seed_tenant(&pool, "tenant-1").await;
    common::seed_resource_group(&pool, "rg-ops", "tenant-1").await;
    common::seed_mcp_server_with_base_url(
        &pool,
        "ms-test",
        "tenant-1",
        "fixture",
        "http://unused.invalid",
    )
    .await;
    common::seed_server_config(&pool, "sc-test", "tenant-1", "ms-test").await;
    common::bind_server_config_to_rg(&pool, "sc-test", "rg-ops").await;
    // NO group-wide mcp_global_limits; per-server override only.
    common::set_rg_server_limits(
        &pool,
        "sc-test",
        "rg-ops",
        json!({"per_tool": {"generate_report": {"max_calls_per_minute": 1}}}),
    )
    .await;

    let rg = common::load_resource_group(&pool, "rg-ops").await;
    let state = common::build_test_state(pool);

    let first = enforce_mcp_rate_limits(
        &state.usage_state,
        &state.db,
        &rg,
        "ms-test",
        Some("sc-test"),
        "generate_report",
    )
    .await;
    assert!(first.is_none(), "first call allowed; got {first:?}");

    let second = enforce_mcp_rate_limits(
        &state.usage_state,
        &state.db,
        &rg,
        "ms-test",
        Some("sc-test"),
        "generate_report",
    )
    .await;
    assert!(
        second.is_some(),
        "per-server `.limits` override (1/min) must block the 2nd call; got {second:?}"
    );
}
