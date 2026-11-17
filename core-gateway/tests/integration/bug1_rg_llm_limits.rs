// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Repro for Bug 1 — RG-level LLM throughput (RPM) + daily-token quota not
//! enforced when a concrete `model_id` is passed (the live preflight path).
//!
//! Uses the EXACT Sales `llm_global_limits` JSON from the field report.

use serde_json::json;
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::services::usage;

const T: &str = "t-bug1";

fn openai_fixture() -> serde_json::Value {
    json!({
        "id": "chatcmpl-bug1",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "test-gpt",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop"
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    })
}

async fn body_json(body: axum::body::Body) -> serde_json::Value {
    let bytes = axum::body::to_bytes(body, usize::MAX).await.expect("body");
    serde_json::from_slice(&bytes).unwrap_or(serde_json::Value::Null)
}

fn sales_limits() -> serde_json::Value {
    json!({
        "throughput": {
            "max_requests_per_minute": 5,
            "cooldown_seconds": 30,
            "max_usd_per_minute": 0.05
        },
        "tokens": {"daily_limit": 100000},
        "concurrency": {"max_concurrent": 2}
    })
}

/// The live preflight calls `enforce_llm_rate_limits(.., group, &model.id)`.
/// The 5-rpm group cap must block after 5 of 10 back-to-back calls.
#[sqlx::test(migrations = "./tests/migrations")]
async fn rg_throughput_rpm_blocks_with_model_id(pool: PgPool) {
    crate::common::seed_tenant(&pool, T).await;
    crate::common::seed_resource_group(&pool, "g1", T).await;
    crate::common::seed_llm_provider(&pool, "p1", T, "openai").await;
    crate::common::seed_llm_model(&pool, "m1", T, "p1", "gpt").await;
    crate::common::set_llm_global_limits(&pool, "g1", sales_limits()).await;

    let g1 = crate::common::load_resource_group(&pool, "g1").await;
    let state = crate::common::build_test_state(pool);

    let mut blocked = 0;
    for _ in 0..10 {
        if usage::enforce_llm_rate_limits(&state.usage_state, &state.db, &g1, "m1")
            .await
            .is_some()
        {
            blocked += 1;
        }
    }
    assert!(
        blocked >= 5,
        "5-rpm group cap must block at least 5 of 10 calls; blocked={blocked}"
    );
}

/// The live preflight calls `enforce_llm_quota(.., Some(group), Some(&model.id), ..)`.
/// A 800-token daily cap with 1000 tokens already logged must block.
#[sqlx::test(migrations = "./tests/migrations")]
async fn rg_daily_tokens_blocks_with_model_id(pool: PgPool) {
    crate::common::seed_tenant(&pool, T).await;
    crate::common::seed_resource_group(&pool, "g1", T).await;
    crate::common::seed_llm_provider(&pool, "p1", T, "openai").await;
    crate::common::seed_llm_model(&pool, "m1", T, "p1", "gpt").await;
    crate::common::set_llm_global_limits(&pool, "g1", json!({"tokens": {"daily_limit": 800}}))
        .await;
    crate::common::insert_usage_log(
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

    let g = crate::common::load_resource_group(&pool, "g1").await;
    let r = usage::enforce_llm_quota(&pool, T, Some(&g), Some("m1"), None, None).await;
    assert!(
        r.blocked,
        "1000 logged tokens over an 800 daily cap must block (model_id path)"
    );
}

// ===========================================================================
// END-TO-END through the real /v1/proxy/llm/chat/completions route, routing
// through a ROUTING GROUP (model_id = "rg-...") that resolves to a member —
// the EXACT production path. The NEW throughput shape + routing indirection
// is what the only pre-existing e2e 429 test (proxy_pipeline::s3) did NOT
// cover (it used the legacy `frequency` shape + a directly-bound model).
// ===========================================================================

/// Seed a routing group whose member models point at `mock_uri`, bind both
/// the members AND the virtual routing model to the RG (explicit-grant rule),
/// and return the raw group-scoped API key. RG id "g1" must already exist.
async fn seed_routing_topology(pool: &PgPool, rgroup: &str, mock_uri: &str) {
    crate::common::seed_llm_provider(pool, "pe", T, "openai").await;
    // Routing group + its virtual llm_model (id = rg-<rgroup>) FIRST, so the
    // members' `routing_group_id` FK is satisfiable.
    crate::common::seed_routing_group(pool, rgroup, T, "pe", "simple").await;
    crate::common::bind_llm_model_to_rg(pool, &format!("rg-{rgroup}"), "g1").await;
    // Two chat members reachable at the mock upstream.
    for m in ["mem-a", "mem-b"] {
        crate::common::seed_llm_model_with_api_url(pool, m, T, "pe", "test-gpt", mock_uri).await;
        // Wire the member into the routing group (get_group_models keys on this).
        sqlx::query("UPDATE llm_model SET routing_group_id = $1 WHERE id = $2")
            .bind(rgroup)
            .bind(m)
            .execute(pool)
            .await
            .expect("set member routing_group_id");
        crate::common::bind_llm_model_to_rg(pool, m, "g1").await;
    }
}

async fn seed_routing_e2e(pool: &PgPool, rgroup: &str, mock_uri: &str) -> String {
    seed_routing_topology(pool, rgroup, mock_uri).await;
    crate::common::seed_api_key(pool, T, "g1", None, "k-bug1").await
}

/// NEW throughput shape (`throughput.max_requests_per_minute`) + routing group:
/// second call in the same minute must 429.
#[sqlx::test(migrations = "./tests/migrations")]
async fn e2e_routing_group_throughput_rpm_429(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(openai_fixture()))
        .mount(&mock)
        .await;

    crate::common::seed_tenant(&pool, T).await;
    crate::common::seed_resource_group(&pool, "g1", T).await;
    crate::common::set_llm_global_limits(
        &pool,
        "g1",
        json!({"throughput": {"max_requests_per_minute": 1}}),
    )
    .await;
    let key = seed_routing_e2e(&pool, "rgrp", &mock.uri()).await;
    let state = crate::common::build_test_state(pool);

    // Dispatch to the routing group by its virtual model id — production path.
    let req = |n: &str| {
        crate::common::build_post_request(
            "/v1/proxy/llm/chat/completions",
            json!({"model_id": "rg-rgrp", "messages": [{"role": "user", "content": n}]}),
            Some(&key),
            Some(T),
        )
    };

    let r1 = crate::common::build_llm_app(state.clone())
        .oneshot(req("1"))
        .await
        .expect("first call");
    assert_eq!(
        r1.status(),
        axum::http::StatusCode::OK,
        "first routing-group call must pass; body={:?}",
        body_json(r1.into_body()).await
    );

    let r2 = crate::common::build_llm_app(state)
        .oneshot(req("2"))
        .await
        .expect("second call");
    let status = r2.status();
    assert_eq!(
        status,
        axum::http::StatusCode::TOO_MANY_REQUESTS,
        "second routing-group call must 429 (RG throughput RPM=1); body={:?}",
        body_json(r2.into_body()).await
    );
}

/// NEW `tokens.daily_limit` + routing group: a single call with the daily
/// token budget already exhausted must 429 (quota), through the real route.
#[sqlx::test(migrations = "./tests/migrations")]
async fn e2e_routing_group_daily_tokens_429(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(openai_fixture()))
        .mount(&mock)
        .await;

    crate::common::seed_tenant(&pool, T).await;
    crate::common::seed_resource_group(&pool, "g1", T).await;
    crate::common::set_llm_global_limits(&pool, "g1", json!({"tokens": {"daily_limit": 5}})).await;
    let key = seed_routing_e2e(&pool, "rgrp", &mock.uri()).await;
    // Pre-exhaust the RG's daily token budget so ONE call is deterministically
    // over-limit (no dependency on the async usage-log write of the request).
    crate::common::insert_usage_log(
        &pool,
        T,
        "g1",
        None,
        "llm",
        "mem-a",
        50,
        0.0,
        crate::common::db_now(),
    )
    .await;
    let state = crate::common::build_test_state(pool);

    let r = crate::common::build_llm_app(state)
        .oneshot(crate::common::build_post_request(
            "/v1/proxy/llm/chat/completions",
            json!({"model_id": "rg-rgrp", "messages": [{"role": "user", "content": "hi"}]}),
            Some(&key),
            Some(T),
        ))
        .await
        .expect("call");
    let status = r.status();
    assert_eq!(
        status,
        axum::http::StatusCode::TOO_MANY_REQUESTS,
        "over-daily-token-budget routing-group call must 429; body={:?}",
        body_json(r.into_body()).await
    );
}

/// Same RPM repro but with a SHARED `scope='global'` group-bound key — the
/// EXACT key type the control plane emits for an "SDK key" (the Sales key in
/// the field report). This is the last untested variable vs the field repro.
#[sqlx::test(migrations = "./tests/migrations")]
async fn e2e_routing_group_throughput_rpm_429_shared_global_key(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(openai_fixture()))
        .mount(&mock)
        .await;

    crate::common::seed_tenant(&pool, T).await;
    crate::common::seed_resource_group(&pool, "g1", T).await;
    crate::common::set_llm_global_limits(
        &pool,
        "g1",
        json!({"throughput": {"max_requests_per_minute": 1}}),
    )
    .await;
    seed_routing_topology(&pool, "rgrp", &mock.uri()).await;
    let key = crate::common::seed_shared_group_api_key(&pool, T, "g1", "sales-sdk").await;
    let state = crate::common::build_test_state(pool);

    let req = |n: &str| {
        crate::common::build_post_request(
            "/v1/proxy/llm/chat/completions",
            json!({"model_id": "rg-rgrp", "messages": [{"role": "user", "content": n}]}),
            Some(&key),
            Some(T),
        )
    };

    let r1 = crate::common::build_llm_app(state.clone())
        .oneshot(req("1"))
        .await
        .expect("first call");
    assert_eq!(
        r1.status(),
        axum::http::StatusCode::OK,
        "first shared-key routing call must pass; body={:?}",
        body_json(r1.into_body()).await
    );

    let r2 = crate::common::build_llm_app(state)
        .oneshot(req("2"))
        .await
        .expect("second call");
    let status = r2.status();
    assert_eq!(
        status,
        axum::http::StatusCode::TOO_MANY_REQUESTS,
        "second shared-key routing call must 429 (RG throughput RPM=1); body={:?}",
        body_json(r2.into_body()).await
    );
}

/// RG concurrency (`concurrency.max_concurrent`) on the NON-STREAMING chat
/// path. The upstream is delayed so overlapping requests genuinely contend for
/// the single slot. Before the fix the non-streaming path never acquired the
/// concurrency guard, so all N returned 200; after, only 1 in-flight is
/// admitted and the rest 429.
#[sqlx::test(migrations = "./tests/migrations")]
async fn e2e_routing_group_non_streaming_concurrency_429(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(openai_fixture())
                .set_delay(std::time::Duration::from_millis(800)),
        )
        .mount(&mock)
        .await;

    crate::common::seed_tenant(&pool, T).await;
    crate::common::seed_resource_group(&pool, "g1", T).await;
    crate::common::set_llm_global_limits(
        &pool,
        "g1",
        json!({"concurrency": {"max_concurrent": 1}}),
    )
    .await;
    seed_routing_topology(&pool, "rgrp", &mock.uri()).await;
    let key = crate::common::seed_shared_group_api_key(&pool, T, "g1", "sales-sdk").await;
    let state = crate::common::build_test_state(pool);

    let fire = |n: &'static str| {
        let app = crate::common::build_llm_app(state.clone());
        let key = key.clone();
        async move {
            app.oneshot(crate::common::build_post_request(
                "/v1/proxy/llm/chat/completions",
                json!({"model_id": "rg-rgrp", "messages": [{"role": "user", "content": n}]}),
                Some(&key),
                Some(T),
            ))
            .await
            .expect("call")
            .status()
        }
    };

    // Three genuinely-overlapping requests; max_concurrent=1 admits one.
    let (s1, s2, s3) = tokio::join!(fire("a"), fire("b"), fire("c"));
    let blocked = [s1, s2, s3]
        .iter()
        .filter(|s| **s == axum::http::StatusCode::TOO_MANY_REQUESTS)
        .count();
    assert!(
        blocked >= 1,
        "max_concurrent=1 with 3 overlapping non-streaming calls must 429 at least one; \
         statuses = [{s1}, {s2}, {s3}]"
    );
}

// ===========================================================================
// Bug 2 — parameter-governance response envelope. When a clamp / mandatory
// fragment actually applies, the non-streaming chat response must carry an
// additive top-level `brutor_governance` block.
// ===========================================================================

#[sqlx::test(migrations = "./tests/migrations")]
async fn e2e_governance_envelope_present_when_clamped(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(openai_fixture()))
        .mount(&mock)
        .await;

    crate::common::seed_tenant(&pool, T).await;
    crate::common::seed_resource_group(&pool, "g1", T).await;
    crate::common::seed_llm_provider(&pool, "pe", T, "openai").await;
    crate::common::seed_llm_model_with_api_url(&pool, "mdl", T, "pe", "test-gpt", &mock.uri())
        .await;
    crate::common::bind_llm_model_to_rg(&pool, "mdl", "g1").await;
    // RG governance: cap output tokens + temperature, inject a mandatory prefix.
    crate::common::set_rg_llm_governance(
        &pool,
        "g1",
        json!({
            "temperature": {"max": 0.7, "enforce": "clamp"},
            "context_window": {"max_output_tokens": 2000, "enforce": "clamp"},
            "system_prompt": {"mandatory_fragments": [{"position": "prepend", "text": "Be concise."}]}
        }),
    )
    .await;
    let key = crate::common::seed_shared_group_api_key(&pool, T, "g1", "sdk").await;
    let state = crate::common::build_test_state(pool);

    let r = crate::common::build_llm_app(state)
        .oneshot(crate::common::build_post_request(
            "/v1/proxy/llm/chat/completions",
            json!({
                "model_id": "mdl",
                "max_tokens": 50000,
                "temperature": 1.8,
                "messages": [{"role": "user", "content": "hi"}]
            }),
            Some(&key),
            Some(T),
        ))
        .await
        .expect("call");
    assert_eq!(r.status(), axum::http::StatusCode::OK);
    let body = body_json(r.into_body()).await;
    let env = body
        .get("brutor_governance")
        .unwrap_or_else(|| panic!("response must carry brutor_governance; body={body}"));
    assert_eq!(env["max_tokens"]["requested"], 50000);
    assert_eq!(env["max_tokens"]["applied"], 2000);
    assert_eq!(env["temperature"]["requested"], 1.8);
    assert_eq!(env["temperature"]["applied"], 0.7);
    assert_eq!(env["system_prompt_fragment"]["applied"], true);
    // Base OpenAI fields untouched (additive contract).
    assert_eq!(body["object"], "chat.completion");
}

/// Negative contract: an in-limits request gets NO envelope (byte-for-byte
/// OpenAI-compatible).
#[sqlx::test(migrations = "./tests/migrations")]
async fn e2e_governance_envelope_absent_when_unchanged(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(openai_fixture()))
        .mount(&mock)
        .await;

    crate::common::seed_tenant(&pool, T).await;
    crate::common::seed_resource_group(&pool, "g1", T).await;
    crate::common::seed_llm_provider(&pool, "pe", T, "openai").await;
    crate::common::seed_llm_model_with_api_url(&pool, "mdl", T, "pe", "test-gpt", &mock.uri())
        .await;
    crate::common::bind_llm_model_to_rg(&pool, "mdl", "g1").await;
    crate::common::set_rg_llm_governance(
        &pool,
        "g1",
        json!({"context_window": {"max_output_tokens": 2000, "enforce": "clamp"}}),
    )
    .await;
    let key = crate::common::seed_shared_group_api_key(&pool, T, "g1", "sdk").await;
    let state = crate::common::build_test_state(pool);

    let r = crate::common::build_llm_app(state)
        .oneshot(crate::common::build_post_request(
            "/v1/proxy/llm/chat/completions",
            json!({"model_id": "mdl", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]}),
            Some(&key),
            Some(T),
        ))
        .await
        .expect("call");
    assert_eq!(r.status(), axum::http::StatusCode::OK);
    let body = body_json(r.into_body()).await;
    assert!(
        body.get("brutor_governance").is_none(),
        "no clamp/injection → no envelope; body={body}"
    );
}

// ---------------------------------------------------------------------------
// Regression: JSON-`null` ancestor limits must not collapse the child's limits.
// resource-group creation stores `llm_global_limits = 'null'::json` (a JSON null,
// NOT a SQL NULL) for a group with no limits. In `collect_effective_limits` that
// value survived `flatten`, became the merge base, and `restrictive_merge(Null,
// child)` no-op'd — silently DROPPING the child group's own RPM/token/budget
// caps. That is why RG-level LLM limits never enforced in production while the
// db-tests (which seed SQL-NULL ancestors) stayed green.
// ---------------------------------------------------------------------------
#[sqlx::test(migrations = "./tests/migrations")]
async fn json_null_ancestor_does_not_drop_child_limits(pool: PgPool) {
    crate::common::seed_tenant(&pool, T).await;
    // Parent carries a JSON-null limits column (the exact production condition).
    crate::common::seed_resource_group(&pool, "acme", T).await;
    crate::common::set_llm_global_limits(&pool, "acme", serde_json::Value::Null).await;
    // Child has real throughput + token limits, parented under acme.
    crate::common::seed_resource_group(&pool, "sales", T).await;
    crate::common::set_group_parent(&pool, "sales", "acme", "acme/sales", 1).await;
    crate::common::set_llm_global_limits(
        &pool,
        "sales",
        json!({"throughput": {"max_requests_per_minute": 5}, "tokens": {"daily_limit": 100000}}),
    )
    .await;

    let child = crate::common::load_resource_group(&pool, "sales").await;
    let eff = usage::collect_effective_llm_limits(&pool, &child, "no-such-model")
        .await
        .expect("the limits are readable")
        .expect("effective limits must be present, not collapsed to null");
    assert_eq!(
        eff.get("throughput")
            .and_then(|t| t.get("max_requests_per_minute")),
        Some(&json!(5)),
        "child RPM must survive a JSON-null ancestor; got {eff}"
    );
    assert_eq!(
        eff.get("tokens").and_then(|t| t.get("daily_limit")),
        Some(&json!(100000)),
        "child daily token limit must survive a JSON-null ancestor; got {eff}"
    );
}
