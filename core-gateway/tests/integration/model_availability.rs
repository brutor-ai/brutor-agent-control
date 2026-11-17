// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A model is called only while it is available, and a direct call to it is
//! bound by the model's own per-minute limits and by the group's input and
//! output token quotas.
//!
//! * A model past its `retire_date` and a model whose provider record is
//!   switched off are not available: a call to one is answered exactly as a
//!   call to a disabled model is (404, "model not found"), and neither is
//!   tried as a fallback or picked as a routing-group member.
//! * `llm_model.rate_limit_rpm` / `rate_limit_tpm` are dimensions of every
//!   call's admission, not only of one routed through a routing group.
//! * `llm_global_limits.tokens.{daily,monthly}_{input,output}_limit` are
//!   dimensions of the same admission.

use axum::http::StatusCode;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;
use crate::model_fallback::{body_to_json, chat_fixture, seed_topology};

async fn upstream(status: u16, content: &str) -> MockServer {
    let server = MockServer::start().await;
    let template = if status == 200 {
        ResponseTemplate::new(200).set_body_json(chat_fixture(content))
    } else {
        ResponseTemplate::new(status).set_body_json(json!({"error": "boom"}))
    };
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(template)
        .mount(&server)
        .await;
    server
}

async fn hits(server: &MockServer) -> usize {
    server.received_requests().await.map_or(0, |r| r.len())
}

async fn model(pool: &PgPool, id: &str, name: &str, upstream: &MockServer) {
    common::seed_llm_model_with_api_url(pool, id, "t1", "prov-openai", name, &upstream.uri()).await;
    common::bind_llm_model_to_rg(pool, id, "rg1").await;
}

async fn retire(pool: &PgPool, id: &str, days_from_today: i32) {
    sqlx::query(
        "UPDATE llm_model SET retire_date = (now() AT TIME ZONE 'UTC')::date + $2 WHERE id = $1",
    )
    .bind(id)
    .bind(days_from_today)
    .execute(pool)
    .await
    .expect("retire_date");
}

async fn chat(pool: &PgPool, key: &str, body: Value) -> (StatusCode, Value) {
    chat_on(&common::build_test_state(pool.clone()), key, body).await
}

async fn chat_on(
    state: &brutor_gateway_core::services::SharedState,
    key: &str,
    body: Value,
) -> (StatusCode, Value) {
    let app = common::build_llm_app(state.clone());
    let req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        body,
        Some(key),
        Some("t1"),
    );
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    (status, body_to_json(resp.into_body()).await)
}

fn by_id(id: &str) -> Value {
    json!({"model_id": id, "max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]})
}

fn by_name(name: &str) -> Value {
    json!({"model": name, "max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]})
}

/// What a call to a model that is switched off is answered with: the answer
/// a retired model and a model of a disabled provider must give too.
async fn disabled_answer(pool: &PgPool, key: &str) -> (StatusCode, Value) {
    let up = upstream(200, "never").await;
    model(pool, "m-off", "gpt-off", &up).await;
    sqlx::query("UPDATE llm_model SET enabled = false WHERE id = 'm-off'")
        .execute(pool)
        .await
        .expect("disable");
    let answer = chat(pool, key, by_id("m-off")).await;
    assert_eq!(hits(&up).await, 0);
    answer
}

fn same_answer(got: &(StatusCode, Value), disabled: &(StatusCode, Value), id: &str) {
    assert_eq!(got.0, disabled.0, "{}", got.1);
    assert_eq!(
        got.1.to_string().replace(id, "m-off"),
        disabled.1.to_string(),
        "a model that is not available is answered as a disabled one is"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_retired_model_is_not_called(pool: PgPool) {
    let key = seed_topology(&pool).await;
    let disabled = disabled_answer(&pool, &key).await;
    assert_eq!(disabled.0, StatusCode::NOT_FOUND, "{}", disabled.1);

    let up = upstream(200, "from a retired model").await;
    model(&pool, "m-old", "gpt-old", &up).await;

    // The day before it retires it is served.
    retire(&pool, "m-old", 1).await;
    let (status, body) = chat(&pool, &key, by_id("m-old")).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(hits(&up).await, 1);

    // On its retire date, and after it, it is not: by id and by name.
    for days in [0, -1, -400] {
        retire(&pool, "m-old", days).await;
        let got = chat(&pool, &key, by_id("m-old")).await;
        same_answer(&got, &disabled, "m-old");
        let (status, body) = chat(&pool, &key, by_name("gpt-old")).await;
        assert_eq!(status, StatusCode::NOT_FOUND, "{body}");
    }
    assert_eq!(hits(&up).await, 1, "a retired model is never called");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_model_of_a_disabled_provider_is_not_called(pool: PgPool) {
    let key = seed_topology(&pool).await;
    let disabled = disabled_answer(&pool, &key).await;
    common::seed_llm_provider(&pool, "prov-other", "t1", "deepseek").await;
    let up = upstream(200, "from a switched-off provider").await;
    common::seed_llm_model_with_api_url(&pool, "m-prov", "t1", "prov-other", "gpt-prov", &up.uri())
        .await;
    common::bind_llm_model_to_rg(&pool, "m-prov", "rg1").await;

    let (status, body) = chat(&pool, &key, by_id("m-prov")).await;
    assert_eq!(status, StatusCode::OK, "{body}");

    sqlx::query("UPDATE llm_providers SET enabled = false WHERE id = 'prov-other'")
        .execute(&pool)
        .await
        .expect("disable provider");
    let got = chat(&pool, &key, by_id("m-prov")).await;
    same_answer(&got, &disabled, "m-prov");
    let (status, body) = chat(&pool, &key, by_name("gpt-prov")).await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{body}");
    assert_eq!(hits(&up).await, 1, "the provider's model is not called");

    // Another provider's model is untouched.
    let other = upstream(200, "fine").await;
    model(&pool, "m-fine", "gpt-fine", &other).await;
    let (status, body) = chat(&pool, &key, by_id("m-fine")).await;
    assert_eq!(status, StatusCode::OK, "{body}");
}

/// A fallback that is retired, or whose provider is off, is not tried.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unavailable_model_is_skipped_as_a_fallback(pool: PgPool) {
    let key = seed_topology(&pool).await;
    common::seed_llm_provider(&pool, "prov-off", "t1", "deepseek").await;
    let primary = upstream(500, "").await;
    let retired = upstream(200, "from the retired fallback").await;
    let provider_off = upstream(200, "from the switched-off provider").await;
    let good = upstream(200, "from the available fallback").await;
    model(&pool, "m-primary", "gpt-primary", &primary).await;
    model(&pool, "m-retired", "gpt-retired", &retired).await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-prov-off",
        "t1",
        "prov-off",
        "gpt-prov-off",
        &provider_off.uri(),
    )
    .await;
    model(&pool, "m-good", "gpt-good", &good).await;
    retire(&pool, "m-retired", -1).await;
    sqlx::query("UPDATE llm_providers SET enabled = false WHERE id = 'prov-off'")
        .execute(&pool)
        .await
        .expect("disable provider");
    common::set_model_fallback_models(&pool, "m-primary", &["m-retired", "m-prov-off", "m-good"])
        .await;

    let (status, body) = chat(&pool, &key, by_id("m-primary")).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(
        body["choices"][0]["message"]["content"],
        "from the available fallback"
    );
    assert_eq!(hits(&retired).await, 0, "a retired fallback is not tried");
    assert_eq!(
        hits(&provider_off).await,
        0,
        "a fallback of a disabled provider is not tried"
    );
}

/// A routing group does not pick a member that is not available.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unavailable_member_of_a_routing_group_is_not_picked(pool: PgPool) {
    let key = seed_topology(&pool).await;
    common::seed_routing_group(&pool, "grp-avail", "t1", "prov-openai", "simple-shuffle").await;
    common::bind_llm_model_to_rg(&pool, "rg-grp-avail", "rg1").await;
    let retired = upstream(200, "from the retired member").await;
    let good = upstream(200, "from the available member").await;
    model(&pool, "m-member-old", "gpt-member-old", &retired).await;
    model(&pool, "m-member-new", "gpt-member-new", &good).await;
    // The retired member is the one every strategy would prefer: it has
    // nearly all the weight.
    sqlx::query(
        "UPDATE llm_model SET routing_group_id = 'grp-avail', \
         routing_weight = CASE WHEN id = 'm-member-old' THEN 1000 ELSE 1 END \
         WHERE id IN ('m-member-old', 'm-member-new')",
    )
    .execute(&pool)
    .await
    .expect("members");
    retire(&pool, "m-member-old", 0).await;

    for _ in 0..8 {
        let (status, body) = chat(&pool, &key, by_name("grp-avail")).await;
        assert_eq!(status, StatusCode::OK, "{body}");
    }
    assert_eq!(hits(&retired).await, 0, "a retired member is never picked");
    assert_eq!(hits(&good).await, 8);
}

// ---------------------------------------------------------------------------
// A model's own per-minute limits on a direct call
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_models_rpm_limit_binds_a_direct_call(pool: PgPool) {
    let key = seed_topology(&pool).await;
    let up = upstream(200, "ok").await;
    model(&pool, "m-rpm", "gpt-rpm", &up).await;
    sqlx::query("UPDATE llm_model SET rate_limit_rpm = 2 WHERE id = 'm-rpm'")
        .execute(&pool)
        .await
        .expect("rpm");
    let state = common::build_test_state(pool.clone());
    for _ in 0..2 {
        let (status, body) = chat_on(&state, &key, by_id("m-rpm")).await;
        assert_eq!(status, StatusCode::OK, "{body}");
    }
    // (An edition with a response cache answers the repeated call itself;
    // the call is admitted against the limit either way.)
    let before = hits(&up).await;
    let (status, body) = chat_on(&state, &key, by_id("m-rpm")).await;
    assert_eq!(status, StatusCode::TOO_MANY_REQUESTS, "{body}");
    assert!(body.to_string().contains("RPM limit"), "{body}");
    assert_eq!(
        hits(&up).await,
        before,
        "a refused call reaches no provider"
    );
}

/// The per-minute token bucket is charged what a call is expected to use
/// and trued up to what it used: a call is admitted while the minute has
/// tokens left, and the next one is refused once they are spent.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_models_tpm_limit_binds_a_direct_call(pool: PgPool) {
    let key = seed_topology(&pool).await;
    let up = upstream(200, "ok").await;
    model(&pool, "m-tpm", "gpt-tpm", &up).await;
    // One answer of the upstream is 15 tokens: the minute holds one call.
    sqlx::query("UPDATE llm_model SET rate_limit_tpm = 16 WHERE id = 'm-tpm'")
        .execute(&pool)
        .await
        .expect("tpm");
    let state = common::build_test_state(pool.clone());
    let (status, body) = chat_on(&state, &key, by_id("m-tpm")).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    // The call settles after its answer is sent: until it has, a second
    // call may still be admitted. It is refused within a few tries.
    let mut refused = None;
    for _ in 0..5 {
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
        let (status, body) = chat_on(&state, &key, by_id("m-tpm")).await;
        if status != StatusCode::OK {
            refused = Some((status, body));
            break;
        }
    }
    let (status, body) = refused.expect("the model's TPM limit never refused a direct call");
    assert_eq!(status, StatusCode::TOO_MANY_REQUESTS, "{body}");
    assert!(body.to_string().contains("TPM limit"), "{body}");
}

// ---------------------------------------------------------------------------
// The group's input and output token quotas
// ---------------------------------------------------------------------------

async fn token_quota_refuses(pool: PgPool, field: &str, names: &str) {
    let key = seed_topology(&pool).await;
    let up = upstream(200, "ok").await;
    model(&pool, "m-quota", "gpt-quota", &up).await;

    // A quota far above the call's worst case admits it.
    common::set_llm_global_limits(&pool, "rg1", json!({"tokens": {field: 1_000_000}})).await;
    let (status, body) = chat(&pool, &key, by_id("m-quota")).await;
    assert_eq!(status, StatusCode::OK, "{body}");

    // One below it refuses the call before the provider is contacted.
    common::set_llm_global_limits(&pool, "rg1", json!({"tokens": {field: 1}})).await;
    let (status, body) = chat(&pool, &key, by_id("m-quota")).await;
    assert_ne!(status, StatusCode::OK, "{field} is not enforced: {body}");
    assert!(
        body.to_string().contains(names),
        "the refusal names the quota ({names}): {body}"
    );
    assert_eq!(hits(&up).await, 1);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_daily_input_token_quota_is_enforced(pool: PgPool) {
    token_quota_refuses(pool, "daily_input_limit", "daily_input_tokens").await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_monthly_input_token_quota_is_enforced(pool: PgPool) {
    token_quota_refuses(pool, "monthly_input_limit", "monthly_input_tokens").await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_daily_output_token_quota_is_enforced(pool: PgPool) {
    token_quota_refuses(pool, "daily_output_limit", "daily_output_tokens").await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_monthly_output_token_quota_is_enforced(pool: PgPool) {
    token_quota_refuses(pool, "monthly_output_limit", "monthly_output_tokens").await;
}

/// The input and output counters are healed from the ledger, like the
/// totals: what a group has already used today counts against a quota from
/// the moment the quota exists (and after the counters are lost).
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_token_side_quota_counts_what_the_ledger_already_holds(pool: PgPool) {
    use brutor_gateway_core::services::usage;
    let key = seed_topology(&pool).await;
    let up = upstream(200, "ok").await;
    model(&pool, "m-ledger", "gpt-ledger", &up).await;
    common::set_llm_global_limits(
        &pool,
        "rg1",
        json!({"tokens": {"daily_input_limit": 100000}}),
    )
    .await;

    // With nothing used, a call fits.
    let state = common::build_test_state(pool.clone());
    let (status, body) = chat_on(&state, &key, by_id("m-ledger")).await;
    assert_eq!(status, StatusCode::OK, "{body}");

    // The ledger holds 99,995 input tokens of today that the counters of a
    // fresh process know nothing about.
    sqlx::query(
        "INSERT INTO usage_logs \
         (tenant_id, group_id, resource_type, resource_id, prompt_tokens, completion_tokens, \
          total_tokens, estimated_cost, currency, is_inherited, request_id, created_at) \
         VALUES ('t1', 'rg1', 'llm', 'm-ledger', 99995, 7, 100002, 0, 'USD', false, 'ledger-1', now())",
    )
    .execute(&pool)
    .await
    .expect("ledger row");
    let state = common::build_test_state(pool.clone());
    usage::reconcile_llm_shadow_counters(&pool, &state.usage_state).await;
    let (status, body) = chat_on(&state, &key, by_id("m-ledger")).await;
    assert_eq!(status, StatusCode::TOO_MANY_REQUESTS, "{body}");
    assert!(body.to_string().contains("daily_input_tokens"), "{body}");
}
