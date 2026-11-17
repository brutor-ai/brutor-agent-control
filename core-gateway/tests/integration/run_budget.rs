// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Run-budget COMMIT wiring, end to end.
//!
//! `run_budget::evaluate` is a pure function with thirteen unit tests and it
//! has never been wrong. What was wrong, twice, was everything that feeds it:
//! the non-streaming path passed `run_id = None` so no call was ever counted,
//! and the cost was derived from a whole-cents value that rounds a
//! typical chat call to ZERO. Both defects are invisible to a test
//! of the decision function, because the decision function was correct in
//! both cases — it was simply being asked about a run that, as far as the
//! counters were concerned, had never made a call.
//!
//! So these tests deliberately do not test `evaluate`. They drive real
//! requests through the proxy and assert that the counters MOVED — that the
//! N+1th call under a cap of N is refused. A budget that is enforced
//! perfectly against a counter nobody increments is not a budget.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use ulid::Ulid;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::services::SharedState;

use crate::common;

/// An upstream reply whose usage block is realistic: a few thousand tokens
/// of prompt and a short completion. Priced at the catalog rates the fixture
/// seeds, one of these costs well under a cent — which is exactly the case
/// that whole cents would round to zero.
fn chat_fixture() -> Value {
    json!({
        "id": "chatcmpl-run-budget",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "test-gpt",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop"
        }],
        "usage": {"prompt_tokens": 2000, "completion_tokens": 40, "total_tokens": 2040}
    })
}

/// One chat request carrying an asserted run id, so every call in a test
/// lands on the same run rather than minting a fresh root each time.
fn chat_request(api_key: &str, tenant: &str, run_id: &str, stream: bool) -> Request<Body> {
    let body = json!({
        "model_id": "m-test",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream,
    });
    Request::builder()
        .method("POST")
        .uri("/v1/proxy/llm/chat/completions")
        .header("content-type", "application/json")
        .header("X-Tenant-ID", tenant)
        .header("authorization", format!("Bearer {api_key}"))
        // Client-asserted run correlation. Without a delegation
        // chain this is what makes N separate HTTP calls one run.
        .header("x-brutor-run-id", run_id)
        .body(Body::from(serde_json::to_vec(&body).unwrap()))
        .map(|mut r| {
            let peer: std::net::SocketAddr = "127.0.0.1:55555".parse().unwrap();
            r.extensions_mut().insert(axum::extract::ConnectInfo(peer));
            r
        })
        .expect("build request")
}

/// Seed provider + model + group + key, pointed at `mock`.
async fn topology(pool: &PgPool, tenant: &str, rg: &str, mock: &MockServer) -> String {
    common::seed_tenant(pool, tenant).await;
    common::seed_resource_group(pool, rg, tenant).await;
    common::seed_end_user(pool, 7301, tenant).await;
    common::add_direct_membership(pool, 7301, rg, tenant).await;
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
    common::bind_llm_model_to_rg(pool, "m-test", rg).await;
    common::seed_api_key(pool, tenant, rg, Some(7301), "rb-key").await
}

/// Drive `n` calls on one run and return the status of each.
async fn drive(
    state: &SharedState,
    key: &str,
    tenant: &str,
    run: &str,
    n: usize,
    stream: bool,
) -> Vec<StatusCode> {
    let mut out = Vec::new();
    for _ in 0..n {
        let app = common::build_llm_app(state.clone());
        let resp = app
            .oneshot(chat_request(key, tenant, run, stream))
            .await
            .expect("router call");
        out.push(resp.status());
        // Post-response booking is what increments the run counters, and on
        // some paths it is spawned. Give it a beat before the next call asks
        // what the run has spent.
        tokio::time::sleep(std::time::Duration::from_millis(150)).await;
    }
    out
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_call_after_the_last_permitted_one_is_refused(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(chat_fixture()))
        .mount(&mock)
        .await;

    let t = "t-rb-calls";
    let key = topology(&pool, t, "rg-rb-calls", &mock).await;
    common::set_rg_operating_envelope(&pool, "rg-rb-calls", json!({"max_llm_calls_per_run": 2}))
        .await;

    let state = common::build_test_state(pool.clone());
    let run = Ulid::new().to_string();
    let statuses = drive(&state, &key, t, &run, 3, false).await;

    assert_eq!(
        &statuses[..2],
        &[StatusCode::OK, StatusCode::OK],
        "the first two calls are within the cap and must be served; got {statuses:?}"
    );
    assert_eq!(
        statuses[2],
        StatusCode::TOO_MANY_REQUESTS,
        "the third call exceeds max_llm_calls_per_run=2. A 200 here means the \
         counter never moved, not a passing budget"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_cap_holds_on_the_streaming_path_too(pool: PgPool) {
    // Streaming and non-streaming book their usage from different places, so
    // one working proves nothing about the other.
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_raw(
            "data: {\"id\":\"c\",\"object\":\"chat.completion.chunk\",\"choices\":\
             [{\"index\":0,\"delta\":{\"content\":\"ok\"},\"finish_reason\":\"stop\"}],\
             \"usage\":{\"prompt_tokens\":2000,\"completion_tokens\":40,\"total_tokens\":2040}}\n\n\
             data: [DONE]\n\n",
            "text/event-stream",
        ))
        .mount(&mock)
        .await;

    let t = "t-rb-stream";
    let key = topology(&pool, t, "rg-rb-stream", &mock).await;
    common::set_rg_operating_envelope(&pool, "rg-rb-stream", json!({"max_llm_calls_per_run": 2}))
        .await;

    let state = common::build_test_state(pool.clone());
    let run = Ulid::new().to_string();
    let statuses = drive(&state, &key, t, &run, 3, true).await;

    assert_eq!(
        statuses[2],
        StatusCode::TOO_MANY_REQUESTS,
        "the streaming path must count its calls against the run too; got {statuses:?}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_sub_cent_cost_still_accumulates_against_a_dollar_cap(pool: PgPool) {
    // A typical chat call costs $0.0002-$0.004, which
    // rounds to ZERO whole cents; deriving the run's cost from the cents
    // value therefore committed $0.00 per hop and `max_cost_per_run_usd`
    // never fired however long the loop ran. The cap here is small enough
    // that a handful of honestly-priced calls must cross it, and
    // unreachable if any hop is allowed to round to nothing.
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(chat_fixture()))
        .mount(&mock)
        .await;

    let t = "t-rb-cost";
    let key = topology(&pool, t, "rg-rb-cost", &mock).await;
    // $1 per 1M input tokens, $3 per 1M output. 2000 prompt + 40 completion
    // tokens is $0.00212 a call — three-quarters of a cent, i.e. zero when
    // rounded to whole cents.
    common::set_llm_model_costs(&pool, "m-test", 1.0, 3.0).await;
    // 2000 prompt tokens at the seeded input rate; three calls must exceed
    // this, one must not.
    common::set_rg_operating_envelope(&pool, "rg-rb-cost", json!({"max_cost_per_run_usd": 0.005}))
        .await;

    let state = common::build_test_state(pool.clone());
    let run = Ulid::new().to_string();
    let statuses = drive(&state, &key, t, &run, 6, false).await;

    assert_eq!(
        statuses[0],
        StatusCode::OK,
        "the first call cannot be over budget"
    );
    assert!(
        statuses.contains(&StatusCode::TOO_MANY_REQUESTS),
        "six priced calls must cross a $0.005 ceiling. If none did, the \
         per-hop cost is being quantised away before it reaches the counter; \
         got {statuses:?}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn separate_runs_have_separate_budgets(pool: PgPool) {
    // The control. A cap that refuses the third call regardless of which run
    // it belongs to would pass every test above while being a per-GROUP cap
    // wearing a run cap's name.
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(chat_fixture()))
        .mount(&mock)
        .await;

    let t = "t-rb-isolated";
    let key = topology(&pool, t, "rg-rb-isolated", &mock).await;
    common::set_rg_operating_envelope(&pool, "rg-rb-isolated", json!({"max_llm_calls_per_run": 2}))
        .await;

    let state = common::build_test_state(pool.clone());
    let first = drive(&state, &key, t, &Ulid::new().to_string(), 3, false).await;
    assert_eq!(first[2], StatusCode::TOO_MANY_REQUESTS);

    // A fresh run id starts from zero.
    let second = drive(&state, &key, t, &Ulid::new().to_string(), 1, false).await;
    assert_eq!(
        second[0],
        StatusCode::OK,
        "one run exhausting its budget must not refuse a different run"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_system_with_no_envelope_is_never_gated(pool: PgPool) {
    // Fail-open is a stated property of this module, not an accident, so it
    // gets a test: adding run budgets must not add refusals for operators
    // who never asked for them.
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(chat_fixture()))
        .mount(&mock)
        .await;

    let t = "t-rb-open";
    let key = topology(&pool, t, "rg-rb-open", &mock).await;

    let state = common::build_test_state(pool.clone());
    let run = Ulid::new().to_string();
    let statuses = drive(&state, &key, t, &run, 4, false).await;

    assert!(
        statuses.iter().all(|s| *s == StatusCode::OK),
        "no envelope means no cap; got {statuses:?}"
    );
}

// ===========================================================================
// Contention: the cap must hold when the calls arrive TOGETHER
// ===========================================================================
//
// Every test above drives calls one after another with a pause, which is the
// one schedule in which check-then-act is correct. An agent that fans out a
// step does not wait: N hops of one run arrive together, each reads the run's
// counters before any of them has finished, and every one of them sees "under
// the cap". The counters only move after a call COMPLETES, so the overshoot
// grows with the parallelism — and across a cluster of replicas, without
// bound. These tests send the calls at once, against an upstream slow enough
// that none has completed when the others are admitted.

/// Same topology and run; `parallel` calls released together; returns how many
/// reached the upstream (the number that matters: those are executed calls).
async fn drive_parallel(
    state: &SharedState,
    key: &str,
    tenant: &str,
    run: &str,
    parallel: usize,
) -> usize {
    let mut tasks = Vec::new();
    for _ in 0..parallel {
        let app = common::build_llm_app(state.clone());
        let req = chat_request(key, tenant, run, false);
        tasks.push(tokio::spawn(async move {
            app.oneshot(req).await.expect("router call").status()
        }));
    }
    let mut served = 0;
    for t in tasks {
        if t.await.unwrap() == StatusCode::OK {
            served += 1;
        }
    }
    served
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

/// A state whose usage counters live in a REAL Redis, as in a cluster: every
/// replica shares them, so this is the configuration where an overshoot is
/// unbounded.
async fn redis_state(pool: &PgPool) -> SharedState {
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

#[sqlx::test(migrations = "./tests/migrations")]
async fn parallel_calls_on_one_run_cannot_exceed_the_call_cap(pool: PgPool) {
    let mock = slow_upstream().await;
    let t = "t-rb-par-mem";
    let key = topology(&pool, t, "rg-rb-par-mem", &mock).await;
    common::set_rg_operating_envelope(&pool, "rg-rb-par-mem", json!({"max_llm_calls_per_run": 3}))
        .await;

    let state = common::build_test_state(pool.clone());
    let run = Ulid::new().to_string();
    let served = drive_parallel(&state, &key, t, &run, 15).await;
    let upstream_calls = mock.received_requests().await.unwrap().len();
    assert!(
        upstream_calls <= 3,
        "max_llm_calls_per_run=3, 15 calls in parallel: {upstream_calls} reached the upstream \
         ({served} served). Every call read the counter before any had completed."
    );
    assert_eq!(
        upstream_calls, 3,
        "the cap admits exactly its limit, not fewer"
    );

    // The books balance: once the settles and releases have run, the run's
    // call counter equals the calls that ran — the refused ones gave their
    // reservation back rather than holding it until the reaper.
    // The asserted run id is namespaced per credential, so read the run's own
    // id from its rows rather than assuming the header value.
    let mut root: Option<String> = None;
    for _ in 0..60 {
        root = sqlx::query_scalar(
            "SELECT root_task_id FROM proxy_logs WHERE tenant_id = $1 AND root_task_id IS NOT NULL LIMIT 1",
        )
        .bind(t)
        .fetch_optional(&pool)
        .await
        .unwrap();
        if root.is_some() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    let root = root.expect("the run's rows were written");
    let mut calls = -1;
    for _ in 0..40 {
        calls = state.usage_state.run_usage(t, &root).await.unwrap().calls;
        if calls == 3 {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    assert_eq!(
        calls, 3,
        "run counter = executed calls; no leaked reservations"
    );
    let _ = run;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn parallel_calls_on_one_run_cannot_exceed_the_call_cap_with_shared_redis(pool: PgPool) {
    let mock = slow_upstream().await;
    let t = "t-rb-par-redis";
    let key = topology(&pool, t, "rg-rb-par-redis", &mock).await;
    common::set_rg_operating_envelope(
        &pool,
        "rg-rb-par-redis",
        json!({"max_llm_calls_per_run": 3}),
    )
    .await;

    // Two "replicas" sharing one Redis, the calls split between them.
    let a = redis_state(&pool).await;
    let b = redis_state(&pool).await;
    let run = Ulid::new().to_string();
    let (sa, sb) = tokio::join!(
        drive_parallel(&a, &key, t, &run, 8),
        drive_parallel(&b, &key, t, &run, 8),
    );
    let upstream_calls = mock.received_requests().await.unwrap().len();
    assert!(
        upstream_calls <= 3,
        "max_llm_calls_per_run=3, 16 calls across two replicas: {upstream_calls} reached the \
         upstream ({} served)",
        sa + sb
    );
}
