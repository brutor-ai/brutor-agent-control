// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Tenant-wide per-model quotas and ancestor (subtree) budgets must hold
//! when the calls arrive TOGETHER — from several groups, on several replicas.
//!
//! Both used to be a `SUM(usage_logs)` followed by a comparison. The ledger
//! row of a call is written after the call completes (and batched), so every
//! call admitted before the first one finished read the same "under the cap"
//! sum. The per-GROUP quota was already an atomic reservation; these scopes
//! span groups, so no group's reservation could see the others' traffic.
//!
//! Two replicas sharing one Redis, calls split between them and between two
//! sibling groups, an upstream slow enough that none completes before the
//! rest are admitted.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::services::SharedState;

use crate::common;

fn chat_fixture() -> Value {
    json!({
        "id": "chatcmpl-budget",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "test-gpt",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}
    })
}

fn chat_request(api_key: &str, tenant: &str) -> Request<Body> {
    let body = json!({"model_id": "m-test", "messages": [{"role": "user", "content": "hi"}]});
    let mut r = Request::builder()
        .method("POST")
        .uri("/v1/proxy/llm/chat/completions")
        .header("content-type", "application/json")
        .header("X-Tenant-ID", tenant)
        .header("authorization", format!("Bearer {api_key}"))
        .body(Body::from(serde_json::to_vec(&body).unwrap()))
        .unwrap();
    let peer: std::net::SocketAddr = "127.0.0.1:55555".parse().unwrap();
    r.extensions_mut().insert(axum::extract::ConnectInfo(peer));
    r
}

async fn slow_upstream() -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(chat_fixture())
                .set_delay(std::time::Duration::from_millis(400)),
        )
        .mount(&mock)
        .await;
    mock
}

/// A replica whose usage counters live in a shared, REAL Redis.
async fn replica(pool: &PgPool) -> SharedState {
    let url =
        std::env::var("TEST_REDIS_URL").unwrap_or_else(|_| "redis://127.0.0.1:6379/15".to_string());
    let client = redis::Client::open(url.as_str()).expect("redis url");
    let conn = redis::aio::ConnectionManager::new(client)
        .await
        .unwrap_or_else(|e| panic!("connect to the test Redis at {url} (set TEST_REDIS_URL): {e}"));
    let config = brutor_gateway_core::config::AppConfig::for_test(
        "postgresql://placeholder/replaced".to_string(),
    );
    std::sync::Arc::new(brutor_gateway_core::services::AppState::new(
        config,
        pool.clone(),
        Some(conn),
        reqwest::Client::new(),
        brutor_gateway_core::services::telemetry::TelemetryState {
            prometheus_handle: None,
        },
    ))
}

/// Model + two sibling groups, each with its own user and key. Group ids are
/// prefixed by the tenant: the usage counters are keyed by group id in a Redis
/// shared by every test running in parallel.
async fn topology(pool: &PgPool, tenant: &str, mock: &MockServer) -> (String, String) {
    common::seed_tenant(pool, tenant).await;
    common::seed_llm_provider(pool, "prov-openai", tenant, "openai").await;
    common::seed_llm_model_with_api_url(
        pool,
        "m-test",
        tenant,
        "prov-openai",
        "test-gpt",
        &mock.uri(),
    )
    .await;
    let mut keys = Vec::new();
    for (i, suffix) in ["a", "b"].iter().enumerate() {
        let rg = format!("{tenant}-rg-{suffix}");
        let uid = 8801 + i as i32;
        common::seed_resource_group(pool, &rg, tenant).await;
        common::seed_end_user(pool, uid, tenant).await;
        common::add_direct_membership(pool, uid, &rg, tenant).await;
        common::bind_llm_model_to_rg(pool, "m-test", &rg).await;
        keys.push(
            common::seed_api_key(pool, tenant, &rg, Some(uid), &format!("bc-key-{rg}")).await,
        );
    }
    (keys[0].clone(), keys[1].clone())
}

/// `per` calls through each replica, alternating the two groups' keys, all at
/// once. Returns how many reached the upstream.
async fn burst(
    pool: &PgPool,
    mock: &MockServer,
    tenant: &str,
    ka: &str,
    kb: &str,
    per: usize,
) -> usize {
    let (ra, rb) = (replica(pool).await, replica(pool).await);
    let mut tasks = Vec::new();
    for i in 0..per * 2 {
        let state = if i % 2 == 0 { ra.clone() } else { rb.clone() };
        let key = if i % 4 < 2 {
            ka.to_string()
        } else {
            kb.to_string()
        };
        let tenant = tenant.to_string();
        tasks.push(tokio::spawn(async move {
            let resp = common::build_llm_app(state)
                .oneshot(chat_request(&key, &tenant))
                .await
                .unwrap();
            let s = resp.status();
            if s != StatusCode::OK && std::env::var("BC_DEBUG").is_ok() {
                use http_body_util::BodyExt;
                let b = resp.into_body().collect().await.unwrap().to_bytes();
                eprintln!("DEBUG {s} {}", String::from_utf8_lossy(&b));
            }
            s
        }));
    }
    for t in tasks {
        let s = t.await.unwrap();
        assert!(
            s == StatusCode::OK || s == StatusCode::TOO_MANY_REQUESTS,
            "only served or quota-refused is expected, got {s}"
        );
    }
    mock.received_requests().await.unwrap().len()
}

/// A tenant id unique to this run. Postgres is a fresh database per test,
/// but the Redis counters outlive the run, keyed by tenant (and group ids,
/// which start with it) per day: a fixed id makes a second run on the same
/// day start with the quota already spent.
fn run_tenant(base: &str) -> String {
    format!("{base}-{}", &uuid::Uuid::new_v4().simple().to_string()[..8])
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_tenant_wide_model_quota_holds_across_groups_and_replicas(pool: PgPool) {
    let mock = slow_upstream().await;
    let t = run_tenant("t-bc-tenant");
    let t = t.as_str();
    let (ka, kb) = topology(&pool, t, &mock).await;
    common::set_llm_tenant_limits(&pool, "m-test", json!({"requests": {"daily_limit": 3}})).await;

    let upstream = burst(&pool, &mock, t, &ka, &kb, 8).await;
    assert_eq!(
        upstream, 3,
        "tenant-wide requests.daily_limit = 3 for the model, 16 calls across two groups and \
         two replicas: {upstream} reached the upstream"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_ancestor_budget_holds_across_its_subtree_and_replicas(pool: PgPool) {
    let mock = slow_upstream().await;
    let t = run_tenant("t-bc-ancestor");
    let t = t.as_str();
    let (ka, kb) = topology(&pool, t, &mock).await;
    // org caps its WHOLE subtree at 3 requests a day. Each team inherits that
    // limit locally too, which the per-group reservation already enforces —
    // so without a subtree reservation the total reaches 3 PER TEAM.
    let org = format!("{t}-org");
    common::seed_resource_group(&pool, &org, t).await;
    common::set_llm_global_limits(&pool, &org, json!({"requests": {"daily_limit": 3}})).await;
    for suffix in ["a", "b"] {
        let rg = format!("{t}-rg-{suffix}");
        common::set_group_parent(&pool, &rg, &org, &format!("/{org}/{rg}"), 1).await;
    }
    sqlx::query(
        "UPDATE resource_groups SET hierarchy_path = $2, hierarchy_level = 0 WHERE id = $1",
    )
    .bind(&org)
    .bind(format!("/{org}"))
    .execute(&pool)
    .await
    .unwrap();
    // Guard against a vacuous pass: the topology really is a subtree.
    let parents: i64 =
        sqlx::query_scalar("SELECT COUNT(*) FROM resource_groups WHERE parent_group_id = $1")
            .bind(&org)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(parents, 2, "both teams must sit under org");

    let upstream = burst(&pool, &mock, t, &ka, &kb, 8).await;
    assert_eq!(
        upstream, 3,
        "org's subtree requests.daily_limit = 3, 16 calls from its two teams across two \
         replicas: {upstream} reached the upstream"
    );
}

// ---------------------------------------------------------------------------
// Skill executions and MCP ancestor call budgets, at the enforcement function
// ---------------------------------------------------------------------------
//
// These scopes count one per call (no estimate to settle), so the gate itself
// is the thing to race: N concurrent admissions against a real shared Redis.

/// N concurrent calls to the real enforcement function; returns how many were
/// admitted.
async fn admit_concurrently<F, Fut>(n: usize, f: F) -> usize
where
    F: Fn() -> Fut,
    Fut: std::future::Future<Output = bool> + Send + 'static,
{
    let tasks: Vec<_> = (0..n).map(|_| tokio::spawn(f())).collect();
    let mut admitted = 0;
    for t in tasks {
        if t.await.unwrap() {
            admitted += 1;
        }
    }
    admitted
}

async fn counters(
    pool: &PgPool,
) -> std::sync::Arc<brutor_gateway_core::services::cache::usage_state::UsageState> {
    replica(pool).await.usage_state.clone()
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_group_skill_execution_quota_holds_under_concurrency(pool: PgPool) {
    let t = run_tenant("t-bc-skill");
    let t = t.as_str();
    let rg = format!("{t}-rg");
    common::seed_tenant(&pool, t).await;
    common::seed_resource_group(&pool, &rg, t).await;
    common::set_skill_global_limits(&pool, &rg, json!({"execution": {"daily_limit": 3}})).await;
    let g = common::load_resource_group(&pool, &rg).await;
    let us = counters(&pool).await;

    let admitted = admit_concurrently(16, || {
        let (pool, g, us, t) = (pool.clone(), g.clone(), us.clone(), t.to_string());
        async move {
            !brutor_gateway_core::services::usage::enforce_skill_quota_counted(
                &pool,
                &t,
                Some(&g),
                Some("s1"),
                Some(us.as_ref()),
                None,
            )
            .await
            .blocked
        }
    })
    .await;
    assert_eq!(
        admitted, 3,
        "execution.daily_limit = 3, 16 concurrent admissions: {admitted} admitted"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_mcp_ancestor_call_budget_holds_under_concurrency(pool: PgPool) {
    let t = run_tenant("t-bc-mcp");
    let t = t.as_str();
    let org = format!("{t}-org");
    common::seed_tenant(&pool, t).await;
    common::seed_resource_group(&pool, &org, t).await;
    sqlx::query(
        "UPDATE resource_groups SET hierarchy_path = $2, hierarchy_level = 0 WHERE id = $1",
    )
    .bind(&org)
    .bind(format!("/{org}"))
    .execute(&pool)
    .await
    .unwrap();
    common::set_mcp_global_limits(&pool, &org, json!({"frequency": {"max_calls_per_day": 3}}))
        .await;
    let mut teams = Vec::new();
    for suffix in ["a", "b"] {
        let rg = format!("{t}-rg-{suffix}");
        common::seed_resource_group(&pool, &rg, t).await;
        common::set_group_parent(&pool, &rg, &org, &format!("/{org}/{rg}"), 1).await;
        teams.push(common::load_resource_group(&pool, &rg).await);
    }
    let us = counters(&pool).await;

    let i = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let admitted = admit_concurrently(16, || {
        let (pool, us, t) = (pool.clone(), us.clone(), t.to_string());
        let g = teams[i.fetch_add(1, std::sync::atomic::Ordering::Relaxed) % 2].clone();
        async move {
            !brutor_gateway_core::services::usage::enforce_mcp_quota_counted(
                &pool,
                &t,
                Some(&g),
                Some(us.as_ref()),
                None,
            )
            .await
            .blocked
        }
    })
    .await;
    assert_eq!(
        admitted, 3,
        "org's subtree max_calls_per_day = 3, 16 concurrent calls from two teams: {admitted} admitted"
    );
}
