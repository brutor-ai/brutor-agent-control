// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # `collect_effective_llm_governance` — end-to-end min-wins composition.
//!
//! Proves the three governance layers the function folds together compose
//! MOST-RESTRICTIVE (min-wins) across a real DB:
//!   1. group-wide `resource_groups.llm_global_governance` column,
//!   2. per-(group, model) `resource_group_llm_governance.governance` row,
//!   3. tenant-wide `llm_model.tenant_governance`.
//!
//! This is the exact regression the code comment at
//! `collect_effective_llm_governance` guards: a group-wide cap set in the admin
//! UI used to be silently ignored because only layer (2) was read. The test
//! sets a LOOSE cap on one layer, a STRICTER cap on another, and a DIFFERENT
//! cap on the third, then asserts the strictest wins — and that the clamp /
//! reject enforcement path uses the merged value.

use serde_json::json;
use sqlx::PgPool;

use brutor_gateway_core::models::proxy::{ChatCompletionRequest, ChatMessage};
use brutor_gateway_core::services::{llm_governance, usage};

use crate::common;

const T: &str = "gov-merge";

fn req_with(
    temperature: Option<f64>,
    max_tokens: Option<i32>,
    user_text: &str,
) -> ChatCompletionRequest {
    ChatCompletionRequest {
        model: Some("gpt".to_string()),
        model_id: None,
        messages: vec![ChatMessage {
            role: "user".to_string(),
            content: Some(json!(user_text)),
            name: None,
            tool_calls: None,
            tool_call_id: None,
        }],
        temperature,
        max_tokens,
        stream: None,
        top_p: None,
        n: None,
        stop: None,
        presence_penalty: None,
        frequency_penalty: None,
        tools: None,
        tool_choice: None,
        extra: serde_json::Map::new(),
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn effective_governance_is_min_across_three_layers(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    common::seed_llm_provider(&pool, "p1", T, "openai").await;
    common::seed_llm_model(&pool, "m1", T, "p1", "gpt").await;
    common::bind_llm_model_to_rg(&pool, "m1", "g1").await;

    // Layer 1: group-wide — LOOSE temperature cap.
    common::set_rg_llm_governance(
        &pool,
        "g1",
        json!({"temperature": {"max": 1.5, "enforce": "clamp"}}),
    )
    .await;
    // Layer 2: per-(group, model) — STRICTER temperature + a context cap.
    common::set_rg_model_governance(
        &pool,
        T,
        "g1",
        "m1",
        json!({
            "temperature": {"max": 0.8, "enforce": "clamp"},
            "context_window": {"max_input_tokens": 8000, "enforce": "clamp"}
        }),
    )
    .await;
    // Layer 3: tenant-wide model default — STRICTEST temperature + tighter context.
    common::set_llm_tenant_governance(
        &pool,
        "m1",
        json!({
            "temperature": {"max": 0.5, "enforce": "clamp"},
            "context_window": {"max_input_tokens": 4000, "enforce": "clamp"}
        }),
    )
    .await;

    let g1 = common::load_resource_group(&pool, "g1").await;
    let eff = usage::collect_effective_llm_governance(&pool, &g1, "m1")
        .await
        .expect("governance readable")
        .expect("effective governance must be present");

    // temperature.max = min(1.5, 0.8, 0.5) = 0.5 — the tenant layer wins.
    assert_eq!(
        eff.temperature.as_ref().and_then(|t| t.max),
        Some(0.5),
        "strictest temperature.max across all three layers must win"
    );
    // context_window.max_input_tokens = min(8000, 4000) = 4000.
    assert_eq!(
        eff.context_window.as_ref().and_then(|c| c.max_input_tokens),
        Some(4000),
        "strictest context_window.max_input_tokens must win"
    );

    // Enforcement uses the MERGED value: a temperature of 1.2 clamps to 0.5,
    // not to the looser 1.5 group-wide cap.
    let mut request = req_with(Some(1.2), None, "hello");
    let result = llm_governance::enforce_request_governance(&mut request, &eff);
    assert!(!result.blocked, "clamp mode must not block");
    assert_eq!(
        request.temperature,
        Some(0.5),
        "temperature must be clamped to the merged (strictest) cap"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn strictest_enforce_mode_wins_reject_over_clamp(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "g1", T).await;
    common::seed_llm_provider(&pool, "p1", T, "openai").await;
    common::seed_llm_model(&pool, "m1", T, "p1", "gpt").await;
    common::bind_llm_model_to_rg(&pool, "m1", "g1").await;

    // Group-wide is clamp; the per-model layer is the stricter `reject` AND a
    // tighter cap. merge → max 0.7, enforce = reject (reject > clamp).
    common::set_rg_llm_governance(
        &pool,
        "g1",
        json!({"temperature": {"max": 1.0, "enforce": "clamp"}}),
    )
    .await;
    common::set_rg_model_governance(
        &pool,
        T,
        "g1",
        "m1",
        json!({"temperature": {"max": 0.7, "enforce": "reject"}}),
    )
    .await;

    let g1 = common::load_resource_group(&pool, "g1").await;
    let eff = usage::collect_effective_llm_governance(&pool, &g1, "m1")
        .await
        .expect("governance readable")
        .expect("effective governance present");
    assert_eq!(eff.temperature.as_ref().and_then(|t| t.max), Some(0.7));
    assert_eq!(
        eff.temperature.as_ref().and_then(|t| t.enforce.clone()),
        Some("reject".to_string()),
        "reject must win over clamp when merging enforce mode"
    );

    // 0.9 > merged max 0.7 with enforce=reject → the request is BLOCKED.
    let mut request = req_with(Some(0.9), None, "hello");
    let result = llm_governance::enforce_request_governance(&mut request, &eff);
    assert!(
        result.blocked,
        "temperature over the merged cap with enforce=reject must block"
    );
}
