// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Every LLM block lands on its run, with its step, and is sealed.
//!
//! Guardrail blocks (input and output), post-response governance blocks and
//! streamed output blocks wrote their rows outside `llm_refusal::refuse`,
//! without the call chain: the row named no run and no step, so a run's
//! timeline could not show why it ended and the run ledger did not count the
//! block against it. A streamed output block wrote no block row at all — the
//! stream's outcome row read as a plain response. Each test drives the real
//! route inside an asserted run and proves the block row names that run and
//! a step and was sealed as the verdict it is. The batch residency refusal
//! (written with `proxy_type = batch`) was never sealed; it is now, as a
//! denial on its run.

use std::time::Duration;

use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::services::SharedState;
use brutor_gateway_core::services::call_chain::{asserted_root, derive_tenant_key};

use crate::common;

pub(crate) const RUN: &str = "run-block-1";
pub(crate) const SEED: [u8; 32] = [0x42; 32];
pub(crate) const BANNED: &str = "forbidden-phrase";

/// The same process-global values the other evidence suites set, so they
/// agree whatever order the harness runs them in.
pub(crate) fn enable_evidence() {
    unsafe {
        std::env::set_var("BRUTOR_EVIDENCE_SIGNING_KEY", hex::encode(SEED));
        std::env::set_var("BRUTOR_EVIDENCE_HEAD_CADENCE_ENTRIES", "2");
        std::env::set_var("BRUTOR_EVIDENCE_WITNESS_ENABLED", "off");
    }
    brutor_gateway_core::env_vars::reload_for_tests();
}

/// Tenant, group, user, model (upstream `mock`) and an API key.
pub(crate) async fn topology(
    pool: &PgPool,
    t: &str,
    rg: &str,
    user: i32,
    upstream: &str,
) -> String {
    common::seed_tenant(pool, t).await;
    common::seed_resource_group(pool, rg, t).await;
    common::seed_end_user(pool, user, t).await;
    common::add_direct_membership(pool, user, rg, t).await;
    let prov = format!("prov-{t}");
    let model = format!("m-{t}");
    common::seed_llm_provider(pool, &prov, t, "openai").await;
    common::seed_llm_model_with_api_url(pool, &model, t, &prov, "test-gpt", upstream).await;
    common::set_model_api_key(pool, &model, "sk-test").await;
    common::bind_llm_model_to_rg(pool, &model, rg).await;
    common::seed_api_key(pool, t, rg, Some(user), &format!("key-{t}")).await
}

pub(crate) async fn banned_words_on(pool: &PgPool, t: &str, rg: &str, input: bool, output: bool) {
    let id = format!("gr-{t}");
    common::seed_llm_banned_words_guardrail(pool, &id, t, &[BANNED], input, output).await;
    common::bind_guardrail_to_rg(pool, &id, rg).await;
}

pub(crate) async fn banned_tool_args(pool: &PgPool, t: &str, rg: &str) {
    sqlx::query(
        "INSERT INTO resource_group_llm_governance \
         (id, tenant_id, resource_group_id, llm_model_id, governance, created_at, updated_at) \
         VALUES ($1, $2, $3, $4, $5, NOW(), NOW())",
    )
    .bind(format!("rglg-{t}"))
    .bind(t)
    .bind(rg)
    .bind(format!("m-{t}"))
    .bind(json!({"tool_use": {"banned_argument_patterns": ["DROP TABLE"]}}))
    .execute(pool)
    .await
    .expect("seed llm governance");
}

/// The `root_task_id` the gateway derives for this key's asserted run.
pub(crate) fn run_root(state: &SharedState, t: &str, key: &str) -> String {
    let tenant_key = derive_tenant_key(state.config.encryption_key.as_bytes(), t);
    let auth = format!("Bearer {key}");
    let get = |n: &str| match n {
        "authorization" => Some(auth.clone()),
        "x-brutor-run-id" => Some(RUN.to_string()),
        _ => None,
    };
    asserted_root(&get, &tenant_key)
        .expect("an authenticated caller may assert its run")
        .to_string()
}

/// POST `body` to the chat route inside the asserted run; the response body.
pub(crate) async fn chat(
    state: &SharedState,
    t: &str,
    key: &str,
    body: Value,
) -> (StatusCode, String) {
    let mut req =
        common::build_post_request("/v1/proxy/llm/chat/completions", body, Some(key), Some(t));
    req.headers_mut()
        .insert("x-brutor-run-id", RUN.parse().expect("run header"));
    let resp = common::build_llm_app(state.clone())
        .oneshot(req)
        .await
        .expect("router call");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.expect("body").to_bytes();
    (status, String::from_utf8_lossy(&bytes).into_owned())
}

/// `(root_task_id, current_task_id, record_id)` of the tenant's newest row
/// matching `filter`, once it has been sealed.
pub(crate) async fn sealed_block_row(
    pool: &PgPool,
    t: &str,
    filter: &str,
) -> (Option<String>, Option<String>, String) {
    let sql = format!(
        "SELECT root_task_id, current_task_id, record_id FROM proxy_logs \
          WHERE tenant_id = $1 AND {filter} ORDER BY timestamp DESC LIMIT 1"
    );
    for _ in 0..160 {
        if let Some((root, current, Some(record))) =
            sqlx::query_as::<_, (Option<String>, Option<String>, Option<String>)>(&sql)
                .bind(t)
                .fetch_optional(pool)
                .await
                .expect("proxy_logs query")
        {
            return (root, current, record);
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("no sealed proxy_logs row for {t} matching {filter}");
}

pub(crate) async fn verdict(pool: &PgPool, record_id: &str) -> String {
    for _ in 0..120 {
        if let Some((v,)) = sqlx::query_as::<_, (String,)>(
            "SELECT verdict FROM action_records WHERE record_id = $1",
        )
        .bind(record_id)
        .fetch_optional(pool)
        .await
        .expect("action_records query")
        {
            return v;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("no action record {record_id}");
}

/// The row landed on `root`, named a step, and was sealed as `expected`.
pub(crate) async fn assert_on_run(
    pool: &PgPool,
    t: &str,
    filter: &str,
    root: &str,
    expected: &str,
) {
    let (row_root, current, record) = sealed_block_row(pool, t, filter).await;
    assert_eq!(
        row_root.as_deref(),
        Some(root),
        "{t}: the block row must land on the run it stopped"
    );
    assert!(current.is_some(), "{t}: the block row must name its step");
    assert_eq!(verdict(pool, &record).await, expected, "{t}");
}

pub(crate) fn chat_completion(content: &str) -> Value {
    json!({
        "id": "chatcmpl-block", "object": "chat.completion", "created": 1, "model": "test-gpt",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 3, "total_tokens": 6}
    })
}

pub(crate) fn sse(v: &Value) -> String {
    format!("data: {v}\n\n")
}

pub(crate) async fn upstream_json(body: Value) -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(body))
        .mount(&mock)
        .await;
    mock
}

pub(crate) async fn upstream_sse(body: String) -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(
            ResponseTemplate::new(200)
                .insert_header("content-type", "text/event-stream")
                .set_body_string(body),
        )
        .mount(&mock)
        .await;
    mock
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_input_guardrail_block_lands_on_its_run(pool: PgPool) {
    enable_evidence();
    let t = "t-blk-in";
    let mock = upstream_json(chat_completion("ok")).await;
    let key = topology(&pool, t, "rg-blk-in", 9871, &mock.uri()).await;
    banned_words_on(&pool, t, "rg-blk-in", true, false).await;
    let state = common::build_test_state(pool.clone());
    let root = run_root(&state, t, &key);

    let (status, _) = chat(
        &state,
        t,
        &key,
        json!({"model_id": format!("m-{t}"),
               "messages": [{"role": "user", "content": format!("say {BANNED}")}]}),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    assert_on_run(&pool, t, "guardrail_blocked", &root, "blocked").await;
    assert_eq!(
        mock.received_requests().await.map_or(0, |r| r.len()),
        0,
        "the provider was never called"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_output_guardrail_block_lands_on_its_run(pool: PgPool) {
    enable_evidence();
    let t = "t-blk-out";
    let mock = upstream_json(chat_completion(&format!("here is {BANNED}"))).await;
    let key = topology(&pool, t, "rg-blk-out", 9872, &mock.uri()).await;
    banned_words_on(&pool, t, "rg-blk-out", false, true).await;
    let state = common::build_test_state(pool.clone());
    let root = run_root(&state, t, &key);

    let (status, body) = chat(
        &state,
        t,
        &key,
        json!({"model_id": format!("m-{t}"),
               "messages": [{"role": "user", "content": "hello"}]}),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert!(
        !body.contains("here is"),
        "the model's answer must not leak: {body}"
    );
    assert_on_run(&pool, t, "guardrail_blocked", &root, "blocked").await;
    // Exactly one row: the block, no deferred success row besides it.
    tokio::time::sleep(Duration::from_millis(400)).await;
    assert_eq!(common::count_proxy_logs(&pool, t).await, 1);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_streamed_output_guardrail_block_lands_on_its_run(pool: PgPool) {
    enable_evidence();
    let t = "t-blk-stream";
    let chunk = |content: &str| {
        json!({"id": "c", "object": "chat.completion.chunk", "model": "test-gpt",
               "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": null}]})
    };
    let finish = json!({"id": "c", "object": "chat.completion.chunk", "model": "test-gpt",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]});
    let stream = format!(
        "{}{}{}data: [DONE]\n\n",
        sse(&chunk("here is ")),
        sse(&chunk(&format!("{BANNED}. "))),
        sse(&finish)
    );
    let mock = upstream_sse(stream).await;
    let key = topology(&pool, t, "rg-blk-stream", 9873, &mock.uri()).await;
    banned_words_on(&pool, t, "rg-blk-stream", false, true).await;
    let state = common::build_test_state(pool.clone());
    let root = run_root(&state, t, &key);

    let (status, body) = chat(
        &state,
        t,
        &key,
        json!({"model_id": format!("m-{t}"), "stream": true,
               "messages": [{"role": "user", "content": "hello"}]}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "a stream has started");
    assert!(body.contains("guardrail_block"), "{body}");
    assert!(!body.contains(&format!("{BANNED}. ")), "{body}");
    // The stream's outcome row is its block row: flagged, on the run, sealed.
    assert_on_run(&pool, t, "guardrail_blocked", &root, "blocked").await;
    let (check,): (Option<String>,) = sqlx::query_as(
        "SELECT guardrail_check FROM proxy_logs WHERE tenant_id = $1 AND guardrail_blocked",
    )
    .bind(t)
    .fetch_one(&pool)
    .await
    .expect("block row");
    assert!(check.is_some(), "the row names the check that fired");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_post_response_governance_block_lands_on_its_run(pool: PgPool) {
    enable_evidence();
    let t = "t-blk-gov";
    let tool_call = json!({
        "id": "chatcmpl-gov", "object": "chat.completion", "created": 1, "model": "test-gpt",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": null,
            "tool_calls": [{"id": "call_1", "type": "function", "function": {
                "name": "run_sql", "arguments": "{\"query\":\"DROP TABLE users\"}"}}]}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 3, "total_tokens": 6}
    });
    let mock = upstream_json(tool_call).await;
    let key = topology(&pool, t, "rg-blk-gov", 9874, &mock.uri()).await;
    banned_tool_args(&pool, t, "rg-blk-gov").await;
    let state = common::build_test_state(pool.clone());
    let root = run_root(&state, t, &key);

    let (status, body) = chat(
        &state,
        t,
        &key,
        json!({"model_id": format!("m-{t}"),
               "messages": [{"role": "user", "content": "clean up"}]}),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert_on_run(&pool, t, "guardrail_blocked", &root, "blocked").await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_streamed_post_response_governance_block_lands_on_its_run(pool: PgPool) {
    enable_evidence();
    let t = "t-blk-sgov";
    let tool_delta = json!({"id": "c", "object": "chat.completion.chunk", "model": "test-gpt",
        "choices": [{"index": 0, "finish_reason": null, "delta": {"role": "assistant",
            "tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {
                "name": "run_sql", "arguments": "{\"query\":\"DROP TABLE users\"}"}}]}}]});
    let finish = json!({"id": "c", "object": "chat.completion.chunk", "model": "test-gpt",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]});
    let mock = upstream_sse(format!(
        "{}{}data: [DONE]\n\n",
        sse(&tool_delta),
        sse(&finish)
    ))
    .await;
    let key = topology(&pool, t, "rg-blk-sgov", 9875, &mock.uri()).await;
    banned_tool_args(&pool, t, "rg-blk-sgov").await;
    let state = common::build_test_state(pool.clone());
    let root = run_root(&state, t, &key);

    let (status, body) = chat(
        &state,
        t,
        &key,
        json!({"model_id": format!("m-{t}"), "stream": true,
               "messages": [{"role": "user", "content": "clean up"}]}),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert!(body.contains("guardrail_block"), "{body}");
    assert_on_run(&pool, t, "guardrail_blocked", &root, "blocked").await;
}
