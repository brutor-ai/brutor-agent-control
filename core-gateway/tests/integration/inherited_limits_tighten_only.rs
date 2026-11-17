// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! An inherited limit can only be tightened, never removed.
//!
//! A group's limit documents are merged up its ancestor chain (and folded
//! with the resource's tenant-wide limits). A descendant that leaves a limit
//! `null`, or states a value its family cannot read, used to REPLACE the
//! ancestor's number: a child's `timeout_seconds: null` erased the parent's
//! timeout. The rule, for every family that merges this way:
//!
//! * the merged value of a numeric limit is the minimum of the stated
//!   positive numbers along the chain;
//! * a descendant's `null` (or absent field) leaves the ancestor's value;
//! * a descendant's malformed value does what the family does with a
//!   malformed value at that level: the MCP execution limits refuse the
//!   call; families that read such a value as unset keep the ancestor's;
//!   families that read `0` as a cap that admits nothing (media quotas,
//!   media governance ceilings) keep the `0`.
//!
//! These run the real collectors against a real two-level hierarchy.

use serde_json::{Value, json};
use sqlx::PgPool;
use std::time::Duration;

use brutor_gateway_core::services::limits::FreqLimits;
use brutor_gateway_core::services::mcp_limits;
use brutor_gateway_core::services::usage;

use crate::common;

const T: &str = "t-f2544";
const PARENT: &str = "rg-f2544-parent";
const CHILD: &str = "rg-f2544-child";

async fn hierarchy(pool: &PgPool) {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, PARENT, T).await;
    common::seed_resource_group(pool, CHILD, T).await;
    common::set_rg_parent(pool, CHILD, PARENT).await;
}

async fn child(pool: &PgPool) -> brutor_gateway_core::models::db::ResourceGroup {
    common::load_resource_group(pool, CHILD).await
}

fn at<'a>(doc: &'a Value, path: &[&str]) -> &'a Value {
    path.iter().fold(doc, |v, k| &v[*k])
}

// ---------------------------------------------------------------------------
// LLM limits
// ---------------------------------------------------------------------------

fn llm_parent() -> Value {
    json!({
        "throughput": {"max_requests_per_minute": 5},
        "tokens": {"daily_limit": 1000},
        "budget": {"daily_limit_usd": 10.5},
        "concurrency": {"max_concurrent": 2}
    })
}

async fn effective_llm(pool: &PgPool, child_limits: Value) -> Value {
    hierarchy(pool).await;
    common::set_llm_global_limits(pool, PARENT, llm_parent()).await;
    common::set_llm_global_limits(pool, CHILD, child_limits).await;
    usage::collect_effective_llm_limits(pool, &child(pool).await, "no-such-model")
        .await
        .expect("readable")
        .expect("the parent's limits are inherited")
}

fn assert_llm_parent_kept(eff: &Value) {
    assert_eq!(
        at(eff, &["throughput", "max_requests_per_minute"]),
        &json!(5),
        "{eff}"
    );
    assert_eq!(at(eff, &["tokens", "daily_limit"]), &json!(1000), "{eff}");
    assert_eq!(
        at(eff, &["budget", "daily_limit_usd"]),
        &json!(10.5),
        "{eff}"
    );
    assert_eq!(
        at(eff, &["concurrency", "max_concurrent"]),
        &json!(2),
        "{eff}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_a_childs_null_keeps_the_parents_limits(pool: PgPool) {
    let eff = effective_llm(
        &pool,
        json!({
            "throughput": {"max_requests_per_minute": null},
            "tokens": {"daily_limit": null},
            "budget": {"daily_limit_usd": null},
            "concurrency": null
        }),
    )
    .await;
    assert_llm_parent_kept(&eff);
}

/// The LLM family reads a value that is not a positive number as unset, so
/// at the child's level there is no limit and the parent's applies.
#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_a_childs_malformed_value_keeps_the_parents_limits(pool: PgPool) {
    let eff = effective_llm(
        &pool,
        json!({
            "throughput": {"max_requests_per_minute": 0},
            "tokens": {"daily_limit": "unlimited"},
            "budget": {"daily_limit_usd": -1},
            "concurrency": {"max_concurrent": true}
        }),
    )
    .await;
    assert_llm_parent_kept(&eff);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_a_child_still_tightens(pool: PgPool) {
    let eff = effective_llm(
        &pool,
        json!({"throughput": {"max_requests_per_minute": 3}, "tokens": {"daily_limit": 5000}}),
    )
    .await;
    assert_eq!(
        at(&eff, &["throughput", "max_requests_per_minute"]),
        &json!(3)
    );
    assert_eq!(at(&eff, &["tokens", "daily_limit"]), &json!(1000));
}

/// The model's tenant-wide limits are folded the same way: their `null`
/// does not erase the group's limit.
#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_a_tenant_wide_null_keeps_the_groups_limits(pool: PgPool) {
    hierarchy(&pool).await;
    common::seed_llm_provider(&pool, "p-f2544", T, "openai").await;
    common::seed_llm_model(&pool, "m-f2544", T, "p-f2544", "gpt").await;
    common::set_llm_global_limits(&pool, PARENT, llm_parent()).await;
    common::set_llm_tenant_limits(
        &pool,
        "m-f2544",
        json!({"throughput": {"max_requests_per_minute": null}, "tokens": {"daily_limit": "x"}}),
    )
    .await;
    let eff = usage::collect_effective_llm_limits(&pool, &child(&pool).await, "m-f2544")
        .await
        .expect("readable")
        .expect("present");
    assert_llm_parent_kept(&eff);
}

// ---------------------------------------------------------------------------
// MCP limits
// ---------------------------------------------------------------------------

const MCP_SERVER: &str = "mcp-f2544";

fn mcp_parent() -> Value {
    json!({
        "execution": {"timeout_seconds": 30, "max_concurrency_per_group": 4,
                      "max_concurrency_per_user": 2},
        "data": {"max_payload_size_mb": 2},
        "frequency": {"max_calls_per_minute": 10}
    })
}

async fn mcp_sources(
    pool: &PgPool,
    parent: Value,
    child_limits: Value,
) -> mcp_limits::McpLimitSources {
    hierarchy(pool).await;
    common::seed_mcp_server(pool, MCP_SERVER, T, "f2544").await;
    common::set_mcp_global_limits(pool, PARENT, parent).await;
    common::set_mcp_global_limits(pool, CHILD, child_limits).await;
    usage::load_mcp_limit_sources(pool, &child(pool).await, MCP_SERVER, None)
        .await
        .expect("readable")
}

fn assert_mcp_parent_kept(src: &mcp_limits::McpLimitSources) {
    let l = mcp_limits::exec_limits(src).expect("the parent's limits are readable");
    assert_eq!(l.timeout, Some(Duration::from_secs(30)), "{:?}", src.group);
    assert_eq!(l.max_payload_bytes, Some(2 * 1_048_576), "{:?}", src.group);
    assert_eq!(l.per_group, [Some(4), None], "{:?}", src.group);
    assert_eq!(l.per_user, [Some(2), None], "{:?}", src.group);
    assert_eq!(
        FreqLimits::at(src.group.as_ref(), &["frequency"]).per_minute,
        Some(10),
        "{:?}",
        src.group
    );
}

/// The reported case: a child's `timeout_seconds: null` erased the parent's
/// timeout.
#[sqlx::test(migrations = "./tests/migrations")]
async fn mcp_a_childs_null_keeps_the_parents_limits(pool: PgPool) {
    let src = mcp_sources(
        &pool,
        mcp_parent(),
        json!({
            "execution": {"timeout_seconds": null, "max_concurrency_per_group": null,
                          "max_concurrency_per_user": null},
            "data": null,
            "frequency": {"max_calls_per_minute": null}
        }),
    )
    .await;
    assert_mcp_parent_kept(&src);
}

/// The frequency limits read a value that is not a positive number as
/// unset: the parent's rate applies.
#[sqlx::test(migrations = "./tests/migrations")]
async fn mcp_a_childs_malformed_rate_keeps_the_parents_rate(pool: PgPool) {
    mcp_sources(&pool, mcp_parent(), json!({})).await;
    for bad in [json!(0), json!("ten"), json!(-3)] {
        common::set_mcp_global_limits(
            &pool,
            CHILD,
            json!({"frequency": {"max_calls_per_minute": bad.clone()}}),
        )
        .await;
        let src = usage::load_mcp_limit_sources(&pool, &child(&pool).await, MCP_SERVER, None)
            .await
            .unwrap();
        assert_mcp_parent_kept(&src);
    }
}

/// The execution limits refuse a value that is not a positive number, at
/// whatever level it is stated: a child's, a parent's under a child that
/// states a valid one, or the server's tenant-wide default.
#[sqlx::test(migrations = "./tests/migrations")]
async fn mcp_a_malformed_execution_limit_at_any_level_refuses(pool: PgPool) {
    let src = mcp_sources(
        &pool,
        mcp_parent(),
        json!({"execution": {"timeout_seconds": "30"}}),
    )
    .await;
    let e = mcp_limits::exec_limits(&src).expect_err("a child's malformed timeout refuses");
    assert_eq!(e.field, "execution.timeout_seconds");

    common::set_mcp_global_limits(
        &pool,
        CHILD,
        json!({"execution": {"max_concurrency_per_user": 0}}),
    )
    .await;
    let src = usage::load_mcp_limit_sources(&pool, &child(&pool).await, MCP_SERVER, None)
        .await
        .unwrap();
    assert!(
        mcp_limits::exec_limits(&src).is_err(),
        "a child's 0 refuses: {:?}",
        src.group
    );

    // The parent's value is malformed; the child states a valid one.
    common::set_mcp_global_limits(
        &pool,
        PARENT,
        json!({"data": {"max_payload_size_mb": "big"}}),
    )
    .await;
    common::set_mcp_global_limits(&pool, CHILD, json!({"data": {"max_payload_size_mb": 1}})).await;
    let src = usage::load_mcp_limit_sources(&pool, &child(&pool).await, MCP_SERVER, None)
        .await
        .unwrap();
    assert!(
        mcp_limits::exec_limits(&src).is_err(),
        "a parent's malformed value refuses too: {:?}",
        src.group
    );

    // The server's tenant-wide default is malformed.
    common::set_mcp_global_limits(&pool, PARENT, mcp_parent()).await;
    common::set_mcp_global_limits(&pool, CHILD, json!({})).await;
    common::set_mcp_tenant_limits(
        &pool,
        MCP_SERVER,
        json!({"execution": {"timeout_seconds": false}}),
    )
    .await;
    let src = usage::load_mcp_limit_sources(&pool, &child(&pool).await, MCP_SERVER, None)
        .await
        .unwrap();
    assert!(mcp_limits::exec_limits(&src).is_err(), "{:?}", src.group);
}

/// The server's tenant-wide `null` does not erase the group's limits.
#[sqlx::test(migrations = "./tests/migrations")]
async fn mcp_a_tenant_wide_null_keeps_the_groups_limits(pool: PgPool) {
    hierarchy(&pool).await;
    common::seed_mcp_server(&pool, MCP_SERVER, T, "f2544").await;
    common::set_mcp_global_limits(&pool, PARENT, mcp_parent()).await;
    common::set_mcp_tenant_limits(
        &pool,
        MCP_SERVER,
        json!({"execution": {"timeout_seconds": null}, "frequency": {"max_calls_per_minute": null}}),
    )
    .await;
    let src = usage::load_mcp_limit_sources(&pool, &child(&pool).await, MCP_SERVER, None)
        .await
        .unwrap();
    assert_mcp_parent_kept(&src);
}

// ---------------------------------------------------------------------------
// Skill limits
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn skill_a_childs_null_or_malformed_value_keeps_the_parents_limit(pool: PgPool) {
    hierarchy(&pool).await;
    common::set_skill_global_limits(
        &pool,
        PARENT,
        json!({"execution": {"daily_limit": 5, "monthly_limit": 50}}),
    )
    .await;
    for bad in [json!(null), json!(0), json!("none")] {
        common::set_skill_global_limits(
            &pool,
            CHILD,
            json!({"execution": {"daily_limit": bad.clone(), "monthly_limit": null}}),
        )
        .await;
        let eff =
            usage::collect_effective_skill_limits(&pool, &child(&pool).await, "no-such-skill")
                .await
                .unwrap()
                .expect("present");
        assert_eq!(
            at(&eff, &["execution", "daily_limit"]),
            &json!(5),
            "{bad}: {eff}"
        );
        assert_eq!(
            at(&eff, &["execution", "monthly_limit"]),
            &json!(50),
            "{bad}: {eff}"
        );
    }
}

// ---------------------------------------------------------------------------
// Media limits (per mode) and media governance
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn media_a_childs_null_keeps_the_parents_limits(pool: PgPool) {
    hierarchy(&pool).await;
    common::set_rg_media_limits(
        &pool,
        PARENT,
        json!({"image": {"daily_requests": 5, "daily_budget_usd": 1.5}}),
    )
    .await;
    for child_limits in [
        json!({"image": {"daily_requests": null, "daily_budget_usd": null}}),
        json!({"image": {"daily_requests": "many", "daily_budget_usd": false}}),
        json!({"image": null}),
    ] {
        common::set_rg_media_limits(&pool, CHILD, child_limits.clone()).await;
        let eff = usage::collect_effective_media_limits(&pool, &child(&pool).await, "image")
            .await
            .unwrap()
            .unwrap_or(Value::Null);
        assert_eq!(eff["daily_requests"], json!(5), "{child_limits}: {eff}");
        assert_eq!(eff["daily_budget_usd"], json!(1.5), "{child_limits}: {eff}");
    }
}

/// A media quota of `0` admits nothing at its own level, so a child's `0`
/// stays (the strictest), and the model's tenant-wide `null` erases nothing.
#[sqlx::test(migrations = "./tests/migrations")]
async fn media_a_childs_zero_is_kept_and_a_tenant_wide_null_erases_nothing(pool: PgPool) {
    hierarchy(&pool).await;
    common::seed_llm_provider(&pool, "p-f2544", T, "openai").await;
    common::seed_llm_model(&pool, "m-f2544", T, "p-f2544", "img").await;
    common::set_rg_media_limits(&pool, PARENT, json!({"image": {"daily_requests": 5}})).await;
    common::set_rg_media_limits(&pool, CHILD, json!({"image": {"daily_requests": 0}})).await;
    let eff = usage::collect_effective_media_limits(&pool, &child(&pool).await, "image")
        .await
        .unwrap()
        .unwrap();
    assert_eq!(eff["daily_requests"], json!(0), "{eff}");

    common::set_rg_media_limits(&pool, CHILD, json!({})).await;
    common::set_llm_tenant_limits(&pool, "m-f2544", json!({"daily_requests": null})).await;
    let eff = usage::collect_effective_media_limits_for_model(
        &pool,
        &child(&pool).await,
        "image",
        "m-f2544",
    )
    .await
    .unwrap()
    .unwrap();
    assert_eq!(eff["daily_requests"], json!(5), "{eff}");
}

/// Media governance ceilings are read as unsigned integers: a merge that
/// turned the parent's `4` into `4.0` removed the ceiling.
#[sqlx::test(migrations = "./tests/migrations")]
async fn media_governance_a_childs_null_keeps_the_parents_ceiling(pool: PgPool) {
    hierarchy(&pool).await;
    common::set_rg_media_governance(
        &pool,
        PARENT,
        json!({"image": {"max_n": 4, "max_prompt_characters": 100,
                         "size_whitelist": ["512x512"]}}),
    )
    .await;
    for child_gov in [
        json!({"image": {"max_n": null, "max_prompt_characters": null}}),
        json!({"image": {"max_n": "x", "max_prompt_characters": -1}}),
        json!({"image": null}),
        json!(null),
    ] {
        common::set_rg_media_governance(&pool, CHILD, child_gov.clone()).await;
        let eff = usage::collect_effective_media_governance(&pool, &child(&pool).await)
            .await
            .unwrap()
            .unwrap_or(Value::Null);
        assert_eq!(
            eff["image"]["max_n"].as_u64(),
            Some(4),
            "{child_gov}: {eff}"
        );
        assert_eq!(
            eff["image"]["max_prompt_characters"].as_u64(),
            Some(100),
            "{child_gov}: {eff}"
        );
        assert_eq!(
            eff["image"]["size_whitelist"],
            json!(["512x512"]),
            "{child_gov}: {eff}"
        );
    }
}

/// An ancestor's `null` mode does not erase the child's own governance.
#[sqlx::test(migrations = "./tests/migrations")]
async fn media_governance_a_parents_null_mode_keeps_the_childs(pool: PgPool) {
    hierarchy(&pool).await;
    common::set_rg_media_governance(&pool, PARENT, json!({"image": null})).await;
    common::set_rg_media_governance(&pool, CHILD, json!({"image": {"max_n": 2}})).await;
    let eff = usage::collect_effective_media_governance(&pool, &child(&pool).await)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(eff["image"]["max_n"].as_u64(), Some(2), "{eff}");
}
