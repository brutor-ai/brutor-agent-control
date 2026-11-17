// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! An admission hold belongs to ONE request, whatever correlation id the
//! request carries.
//!
//! A client may send its own `X-Correlation-ID`, and nothing stops two of
//! its requests — or two tenants' requests — from carrying the same one. A
//! hold named from the correlation id is then one record shared by all of
//! them: the second admission replaces the first one's record, a request the
//! limits refuse gives back the units of another request that is running,
//! and a call settles what another call reserved.
//!
//! Each test here sends concurrent requests under ONE correlation id, on two
//! replicas that share a REAL Redis, and reads the counters afterwards: they
//! must be exact, per tenant, on the LLM, MCP and skill surfaces.
//!
//! Needs `TEST_REDIS_URL` (CI provides a service container).

use axum::body::Body;
use axum::http::{HeaderValue, Request, StatusCode};
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::services::SharedState;
use brutor_gateway_core::services::cache::usage_state::QuotaWindow;

use crate::common;

fn redis_url() -> String {
    std::env::var("TEST_REDIS_URL").unwrap_or_else(|_| "redis://127.0.0.1:6379/15".to_string())
}

async fn redis_conn() -> redis::aio::ConnectionManager {
    let url = redis_url();
    let client = redis::Client::open(url.as_str()).expect("redis url");
    redis::aio::ConnectionManager::new(client)
        .await
        .unwrap_or_else(|e| panic!("connect to the test Redis at {url} (set TEST_REDIS_URL): {e}"))
}

/// A replica whose limits live in the shared, REAL Redis.
async fn replica(pool: &PgPool) -> SharedState {
    let config = brutor_gateway_core::config::AppConfig::for_test(
        "postgresql://placeholder/replaced".to_string(),
    );
    std::sync::Arc::new(brutor_gateway_core::services::AppState::new(
        config,
        pool.clone(),
        Some(redis_conn().await),
        reqwest::Client::new(),
        brutor_gateway_core::services::telemetry::TelemetryState {
            prometheus_handle: None,
        },
    ))
}

/// Drop every Redis key naming `needle` (a tenant, or the shared correlation
/// id): the test Redis outlives a run.
async fn forget_redis_state(needle: &str) {
    let mut c = redis_conn().await;
    let keys: Vec<String> = redis::cmd("KEYS")
        .arg(format!("*{needle}*"))
        .query_async(&mut c)
        .await
        .unwrap();
    if !keys.is_empty() {
        let _: () = redis::cmd("DEL")
            .arg(keys)
            .query_async(&mut c)
            .await
            .unwrap();
    }
}

fn with_correlation_id(mut req: Request<Body>, correlation_id: &str) -> Request<Body> {
    req.headers_mut().insert(
        "x-correlation-id",
        HeaderValue::from_str(correlation_id).unwrap(),
    );
    req
}

/// Every request at once, alternating between two replicas. Returns the
/// statuses in request order.
async fn burst(
    pool: &PgPool,
    app: fn(SharedState) -> axum::Router,
    requests: Vec<Request<Body>>,
) -> Vec<StatusCode> {
    let (ra, rb) = (replica(pool).await, replica(pool).await);
    let mut tasks = Vec::new();
    for (i, req) in requests.into_iter().enumerate() {
        let state = if i % 2 == 0 { ra.clone() } else { rb.clone() };
        tasks.push(tokio::spawn(async move {
            app(state).oneshot(req).await.unwrap().status()
        }));
    }
    let mut out = Vec::new();
    for t in tasks {
        out.push(t.await.unwrap());
    }
    out
}

/// Settles and releases are spawned when a response completes.
async fn settled() {
    tokio::time::sleep(std::time::Duration::from_millis(400)).await;
}

fn count(statuses: &[StatusCode], status: StatusCode) -> usize {
    statuses.iter().filter(|s| **s == status).count()
}

// ---------------------------------------------------------------------------
// LLM
// ---------------------------------------------------------------------------

/// An upstream that answers after 400 ms with 20 prompt and 5 completion
/// tokens: slow enough that two calls sent together are both in flight.
async fn slow_llm_upstream() -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({
                    "id": "chatcmpl-hold",
                    "object": "chat.completion",
                    "created": 1_700_000_000,
                    "model": "test-gpt",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}
                }))
                .set_delay(std::time::Duration::from_millis(400)),
        )
        .mount(&mock)
        .await;
    mock
}

/// A tenant with one group, one model bound to it and a member's key.
/// Returns (group, key, model).
async fn llm_topology(
    pool: &PgPool,
    tenant: &str,
    user: i32,
    mock: &MockServer,
) -> (String, String, String) {
    forget_redis_state(tenant).await;
    common::seed_tenant(pool, tenant).await;
    let provider = format!("{tenant}-prov");
    let model = format!("{tenant}-m");
    common::seed_llm_provider(pool, &provider, tenant, "openai").await;
    common::seed_llm_model_with_api_url(pool, &model, tenant, &provider, "test-gpt", &mock.uri())
        .await;
    let rg = format!("{tenant}-rg");
    common::seed_resource_group(pool, &rg, tenant).await;
    common::seed_end_user(pool, user, tenant).await;
    common::add_direct_membership(pool, user, &rg, tenant).await;
    common::bind_llm_model_to_rg(pool, &model, &rg).await;
    let key = common::seed_api_key(pool, tenant, &rg, Some(user), &format!("{tenant}-key")).await;
    (rg, key, model)
}

fn chat_request(key: &str, tenant: &str, model: &str, correlation_id: &str) -> Request<Body> {
    with_correlation_id(
        common::build_post_request(
            "/v1/proxy/llm/chat/completions",
            json!({"model_id": model, "max_tokens": 100,
                   "messages": [{"role": "user", "content": "hi"}]}),
            Some(key),
            Some(tenant),
        ),
        correlation_id,
    )
}

async fn tokens_today(pool: &PgPool, rg: &str) -> i64 {
    replica(pool)
        .await
        .usage_state
        .read_usage(&format!("tok:grp:{rg}"), QuotaWindow::Day)
        .await
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn two_llm_calls_of_one_tenant_under_one_correlation_id_each_settle_their_own(pool: PgPool) {
    let mock = slow_llm_upstream().await;
    let t = "t-hold-llm";
    let (rg, key, model) = llm_topology(&pool, t, 8811, &mock).await;
    let cid = "corr-t-hold-llm";

    let statuses = burst(
        &pool,
        common::build_llm_app,
        (0..2).map(|_| chat_request(&key, t, &model, cid)).collect(),
    )
    .await;
    assert_eq!(count(&statuses, StatusCode::OK), 2, "{statuses:?}");
    settled().await;
    assert_eq!(
        tokens_today(&pool, &rg).await,
        50,
        "each call settles the worst case it reserved to the 25 tokens it used; a reservation \
         left behind, or a call that settled nothing, shows here"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn two_tenants_llm_calls_under_one_correlation_id_each_settle_their_own(pool: PgPool) {
    let mock = slow_llm_upstream().await;
    let (ta, tb) = ("t-hold-llm-a", "t-hold-llm-b");
    let (rg_a, key_a, model_a) = llm_topology(&pool, ta, 8812, &mock).await;
    let (rg_b, key_b, model_b) = llm_topology(&pool, tb, 8813, &mock).await;
    let cid = "corr-t-hold-llm-a";

    let statuses = burst(
        &pool,
        common::build_llm_app,
        vec![
            chat_request(&key_a, ta, &model_a, cid),
            chat_request(&key_b, tb, &model_b, cid),
        ],
    )
    .await;
    assert_eq!(count(&statuses, StatusCode::OK), 2, "{statuses:?}");
    settled().await;
    assert_eq!(
        (
            tokens_today(&pool, &rg_a).await,
            tokens_today(&pool, &rg_b).await
        ),
        (25, 25),
        "a call's settle reaches its own tenant's counters, never the other tenant's"
    );
}

// ---------------------------------------------------------------------------
// MCP
// ---------------------------------------------------------------------------

const TOOL: &str = "lookup";

async fn mcp_upstream() -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({"jsonrpc": "2.0", "id": 1, "result": {}})),
        )
        .mount(&mock)
        .await;
    mock
}

/// A tenant with one group, one MCP server and `limit` calls an hour of
/// [`TOOL`] in the group. Returns (group, key, server).
async fn mcp_topology(
    pool: &PgPool,
    tenant: &str,
    user: i32,
    mock_uri: &str,
    limit: i64,
) -> (String, String, String) {
    forget_redis_state(tenant).await;
    common::seed_tenant(pool, tenant).await;
    let rg = format!("{tenant}-rg");
    let server = format!("{tenant}-ms");
    let config = format!("{tenant}-sc");
    common::seed_resource_group(pool, &rg, tenant).await;
    common::seed_end_user(pool, user, tenant).await;
    common::add_direct_membership(pool, user, &rg, tenant).await;
    common::seed_mcp_server_with_base_url(
        pool,
        &server,
        tenant,
        &format!("{tenant}-crm"),
        mock_uri,
    )
    .await;
    common::seed_server_config(pool, &config, tenant, &server).await;
    common::bind_server_config_to_rg(pool, &config, &rg).await;
    common::set_mcp_global_limits(
        pool,
        &rg,
        json!({"per_tool": {TOOL: {"max_calls_per_hour": limit}}}),
    )
    .await;
    let key = common::seed_api_key(pool, tenant, &rg, Some(user), &format!("{tenant}-key")).await;
    (rg, key, server)
}

fn tool_call(key: &str, tenant: &str, server: &str, correlation_id: &str) -> Request<Body> {
    with_correlation_id(
        common::build_post_request(
            &format!("/v1/proxy/mcp/{server}"),
            json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": TOOL, "arguments": {}}}),
            Some(key),
            Some(tenant),
        ),
        correlation_id,
    )
}

async fn tool_calls_this_hour(pool: &PgPool, rg: &str) -> i64 {
    let key = brutor_gateway_core::services::limits::freq_window_key(
        &format!("g:{rg}:t:{TOOL}"),
        QuotaWindow::Hour,
    );
    replica(pool)
        .await
        .usage_state
        .counter_value(&key)
        .await
        .unwrap()
}

/// Requests of the upstream that are tool calls.
async fn tool_calls_received(mock: &MockServer) -> usize {
    mock.received_requests()
        .await
        .unwrap_or_default()
        .iter()
        .filter(|r| {
            serde_json::from_slice::<Value>(&r.body)
                .ok()
                .is_some_and(|b| b["method"] == "tools/call")
        })
        .count()
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn mcp_calls_of_one_tenant_under_one_correlation_id_are_counted_exactly(pool: PgPool) {
    let mock = mcp_upstream().await;
    let t = "t-hold-mcp";
    let (rg, key, server) = mcp_topology(&pool, t, 8821, &mock.uri(), 3).await;
    let cid = "corr-t-hold-mcp";

    let statuses = burst(
        &pool,
        common::build_mcp_proxy_app,
        (0..16).map(|_| tool_call(&key, t, &server, cid)).collect(),
    )
    .await;
    settled().await;
    assert_eq!(
        (
            count(&statuses, StatusCode::OK),
            count(&statuses, StatusCode::TOO_MANY_REQUESTS),
            tool_calls_received(&mock).await,
            tool_calls_this_hour(&pool, &rg).await,
        ),
        (3, 13, 3, 3),
        "three calls an hour: three run and are counted, whatever the refused ones do with \
         their holds: {statuses:?}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn two_tenants_mcp_calls_under_one_correlation_id_are_counted_exactly(pool: PgPool) {
    let (mock_a, mock_b) = (mcp_upstream().await, mcp_upstream().await);
    let (ta, tb) = ("t-hold-mcp-a", "t-hold-mcp-b");
    let (rg_a, key_a, server_a) = mcp_topology(&pool, ta, 8822, &mock_a.uri(), 2).await;
    let (rg_b, key_b, server_b) = mcp_topology(&pool, tb, 8823, &mock_b.uri(), 5).await;
    let cid = "corr-t-hold-mcp-a";

    let mut requests = Vec::new();
    for _ in 0..10 {
        requests.push(tool_call(&key_a, ta, &server_a, cid));
        requests.push(tool_call(&key_b, tb, &server_b, cid));
        // Each tenant on both replicas.
        requests.push(tool_call(&key_b, tb, &server_b, cid));
        requests.push(tool_call(&key_a, ta, &server_a, cid));
    }
    let statuses = burst(&pool, common::build_mcp_proxy_app, requests).await;
    settled().await;
    assert_eq!(
        (
            count(&statuses, StatusCode::OK),
            tool_calls_received(&mock_a).await,
            tool_calls_this_hour(&pool, &rg_a).await,
            tool_calls_received(&mock_b).await,
            tool_calls_this_hour(&pool, &rg_b).await,
        ),
        (7, 2, 2, 5, 5),
        "each tenant's limit admits its own calls and counts them: {statuses:?}"
    );
}

// ---------------------------------------------------------------------------
// Skills
// ---------------------------------------------------------------------------

/// A tenant with one group, one published skill bound to it and `limit`
/// executions a day in the group. Returns (group, key, skill name).
async fn skill_topology(
    pool: &PgPool,
    tenant: &str,
    user: i32,
    limit: i64,
) -> (String, String, String) {
    forget_redis_state(tenant).await;
    common::seed_tenant(pool, tenant).await;
    let rg = format!("{tenant}-rg");
    let skill = format!("{tenant}-skill");
    common::seed_resource_group(pool, &rg, tenant).await;
    common::seed_end_user(pool, user, tenant).await;
    common::add_direct_membership(pool, user, &rg, tenant).await;
    common::seed_agent_skill(pool, &skill, tenant, &skill, "# Skill\nDo it.", "Does it").await;
    common::bind_skill_to_rg(pool, &skill, &rg).await;
    common::set_skill_global_limits(pool, &rg, json!({"execution": {"daily_limit": limit}})).await;
    let key = common::seed_api_key(pool, tenant, &rg, Some(user), &format!("{tenant}-key")).await;
    (rg, key, skill)
}

fn skill_run(key: &str, tenant: &str, skill: &str, correlation_id: &str) -> Request<Body> {
    with_correlation_id(
        common::build_post_request(
            "/v1/proxy/skills/execute",
            json!({"skill_name": skill, "input_params": {}}),
            Some(key),
            Some(tenant),
        ),
        correlation_id,
    )
}

async fn skill_runs_today(pool: &PgPool, rg: &str) -> i64 {
    replica(pool)
        .await
        .usage_state
        .read_usage(&format!("skx:grp:{rg}"), QuotaWindow::Day)
        .await
}

async fn executions(pool: &PgPool, tenant: &str) -> i64 {
    sqlx::query_scalar("SELECT COUNT(*) FROM agent_skill_executions WHERE tenant_id = $1")
        .bind(tenant)
        .fetch_one(pool)
        .await
        .unwrap()
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn skill_runs_of_one_tenant_under_one_correlation_id_are_counted_exactly(pool: PgPool) {
    let t = "t-hold-skl";
    let (rg, key, skill) = skill_topology(&pool, t, 8831, 3).await;
    let cid = "corr-t-hold-skl";

    let statuses = burst(
        &pool,
        common::build_multimodal_app,
        (0..16).map(|_| skill_run(&key, t, &skill, cid)).collect(),
    )
    .await;
    settled().await;
    assert_eq!(
        (
            count(&statuses, StatusCode::OK),
            executions(&pool, t).await,
            skill_runs_today(&pool, &rg).await,
        ),
        (3, 3, 3),
        "three executions a day: three start and are counted: {statuses:?}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn two_tenants_skill_runs_under_one_correlation_id_are_counted_exactly(pool: PgPool) {
    let (ta, tb) = ("t-hold-skl-a", "t-hold-skl-b");
    let (rg_a, key_a, skill_a) = skill_topology(&pool, ta, 8832, 2).await;
    let (rg_b, key_b, skill_b) = skill_topology(&pool, tb, 8833, 5).await;
    let cid = "corr-t-hold-skl-a";

    let mut requests = Vec::new();
    for _ in 0..10 {
        requests.push(skill_run(&key_a, ta, &skill_a, cid));
        requests.push(skill_run(&key_b, tb, &skill_b, cid));
        requests.push(skill_run(&key_b, tb, &skill_b, cid));
        requests.push(skill_run(&key_a, ta, &skill_a, cid));
    }
    let statuses = burst(&pool, common::build_multimodal_app, requests).await;
    settled().await;
    assert_eq!(
        (
            count(&statuses, StatusCode::OK),
            executions(&pool, ta).await,
            skill_runs_today(&pool, &rg_a).await,
            executions(&pool, tb).await,
            skill_runs_today(&pool, &rg_b).await,
        ),
        (7, 2, 2, 5, 5),
        "each tenant's limit admits its own runs and counts them: {statuses:?}"
    );
}
