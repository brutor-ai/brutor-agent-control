// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Every limit, end to end: through the HTTP surfaces, on two replicas that
//! share one REAL Redis, against an upstream slow enough that a burst is
//! admitted before any call completes.
//!
//! The residuals of the first reservation fix, each pinned here:
//!
//! * token and spend caps were reserved against an ESTIMATE, so a call could
//!   overshoot by what it used beyond it — now the worst case is reserved and
//!   the output the upstream may produce is bounded to what is left;
//! * the conversation ceiling was read from a cache and compared — a burst in
//!   one conversation all read "under";
//! * a call refused by a later gate kept the units an earlier gate took;
//! * a Redis that errors fell back to a per-pod counter — now it refuses.
//!
//! Needs `TEST_REDIS_URL` (CI provides a service container).

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::services::SharedState;
use brutor_gateway_core::services::cache::usage_state::{QuotaWindow, UsageState};

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

fn chat_fixture(prompt: i64, completion: i64) -> Value {
    json!({
        "id": "chatcmpl-limits",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "test-gpt",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                  "total_tokens": prompt + completion}
    })
}

async fn slow_upstream(prompt: i64, completion: i64) -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(chat_fixture(prompt, completion))
                .set_delay(std::time::Duration::from_millis(400)),
        )
        .mount(&mock)
        .await;
    mock
}

fn chat_request(key: &str, tenant: &str, extra: Value, session: Option<&str>) -> Request<Body> {
    let mut body = json!({"model_id": "m-lim", "messages": [{"role": "user", "content": "hi"}]});
    if let (Some(b), Some(e)) = (body.as_object_mut(), extra.as_object()) {
        for (k, v) in e {
            b.insert(k.clone(), v.clone());
        }
    }
    let mut builder = Request::builder()
        .method("POST")
        .uri("/v1/proxy/llm/chat/completions")
        .header("content-type", "application/json")
        .header("X-Tenant-ID", tenant)
        .header("authorization", format!("Bearer {key}"));
    if let Some(s) = session {
        builder = builder.header("X-Brutor-Session-Id", s);
    }
    let mut r = builder
        .body(Body::from(serde_json::to_vec(&body).unwrap()))
        .unwrap();
    let peer: std::net::SocketAddr = "127.0.0.1:55556".parse().unwrap();
    r.extensions_mut().insert(axum::extract::ConnectInfo(peer));
    r
}

/// Drop every Redis key naming `tenant`. The test Redis outlives a run, and a
/// daily counter left by an earlier run on the same day would otherwise be
/// read as this run's usage.
async fn forget_redis_state(tenant: &str) {
    let mut c = redis_conn().await;
    let keys: Vec<String> = redis::cmd("KEYS")
        .arg(format!("*{tenant}*"))
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

/// Tenant, model (priced: 1,000 µ$ an input token, 2,000 µ$ an output
/// token), one group bound to it, its user and key. Ids are prefixed by the
/// tenant — the shared Redis is shared by every test running in parallel —
/// and the tenant starts with no Redis state.
async fn topology(pool: &PgPool, tenant: &str, mock: &MockServer) -> (String, String) {
    forget_redis_state(tenant).await;
    common::seed_tenant(pool, tenant).await;
    common::seed_llm_provider(pool, "prov-openai", tenant, "openai").await;
    common::seed_llm_model_with_api_url(
        pool,
        "m-lim",
        tenant,
        "prov-openai",
        "test-gpt",
        &mock.uri(),
    )
    .await;
    sqlx::query(
        "UPDATE llm_model SET cost_per_input_token = 1000.0, cost_per_output_token = 2000.0 WHERE id = 'm-lim'",
    )
    .execute(pool)
    .await
    .unwrap();
    let rg = format!("{tenant}-rg");
    common::seed_resource_group(pool, &rg, tenant).await;
    common::seed_end_user(pool, 8901, tenant).await;
    common::add_direct_membership(pool, 8901, &rg, tenant).await;
    common::bind_llm_model_to_rg(pool, "m-lim", &rg).await;
    let key = common::seed_api_key(pool, tenant, &rg, Some(8901), &format!("lim-key-{rg}")).await;
    (rg, key)
}

/// `n` calls at once, split across two replicas. Returns the statuses.
async fn burst(pool: &PgPool, n: usize, make: impl Fn() -> Request<Body>) -> Vec<StatusCode> {
    let (ra, rb) = (replica(pool).await, replica(pool).await);
    let mut tasks = Vec::new();
    for i in 0..n {
        let state = if i % 2 == 0 { ra.clone() } else { rb.clone() };
        let req = make();
        tasks.push(tokio::spawn(async move {
            common::build_llm_app(state)
                .oneshot(req)
                .await
                .unwrap()
                .status()
        }));
    }
    let mut out = Vec::new();
    for t in tasks {
        out.push(t.await.unwrap());
    }
    out
}

/// Settles are spawned when a response completes: give them a moment.
async fn settled() {
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_daily_token_cap_holds_against_worst_cases_under_a_burst(pool: PgPool) {
    let mock = slow_upstream(20, 5).await;
    let t = "t-lim-tok";
    let (rg, key) = topology(&pool, t, &mock).await;
    // A call's worst case is ~1.1k tokens (its input bound — every text byte
    // plus a fixed allowance — and the 100 output tokens it asks for): two
    // fit at once under 2,500, never three.
    common::set_llm_global_limits(&pool, &rg, json!({"tokens": {"daily_limit": 2_500}})).await;

    let statuses = burst(&pool, 12, || {
        chat_request(&key, t, json!({"max_tokens": 100}), None)
    })
    .await;
    let served = statuses.iter().filter(|s| **s == StatusCode::OK).count();
    assert!(
        statuses
            .iter()
            .all(|s| *s == StatusCode::OK || *s == StatusCode::TOO_MANY_REQUESTS),
        "{statuses:?}"
    );
    assert_eq!(
        served, 2,
        "only calls whose worst case fits are admitted at once: {statuses:?}"
    );
    settled().await;
    let us = replica(&pool).await.usage_state.clone();
    assert_eq!(
        us.read_usage(&format!("tok:grp:{rg}"), QuotaWindow::Day)
            .await,
        50,
        "settled, the counter carries the actual usage of the two calls"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_upstream_is_told_the_output_the_caps_have_room_for(pool: PgPool) {
    let mock = slow_upstream(20, 5).await;
    let t = "t-lim-ceil";
    let (rg, key) = topology(&pool, t, &mock).await;
    common::set_llm_global_limits(&pool, &rg, json!({"tokens": {"daily_limit": 2_000}})).await;

    let resp = common::build_llm_app(replica(&pool).await)
        .oneshot(chat_request(&key, t, json!({"max_tokens": 100_000}), None))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let sent = mock.received_requests().await.unwrap();
    let body: Value = serde_json::from_slice(&sent[0].body).unwrap();
    let max = body["max_tokens"]
        .as_i64()
        .expect("an output ceiling is sent");
    assert!(
        (256..2_000).contains(&max),
        "asked for 100,000, the cap leaves < 2,000 after the input bound: sent {max}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_conversation_ceiling_holds_under_a_burst_in_one_conversation(pool: PgPool) {
    let mock = slow_upstream(20, 5).await;
    let t = "t-lim-sess";
    let (rg, key) = topology(&pool, t, &mock).await;
    // Worst case of one call: ~1.05k input tokens at 1,000 µ$ + 50 output at
    // 2,000 µ$ ≈ $1.15. A $2.50 conversation fits two at once.
    common::set_rg_operating_envelope(&pool, &rg, json!({"max_cost_per_session_usd": 2.5})).await;

    let statuses = burst(&pool, 10, || {
        chat_request(&key, t, json!({"max_tokens": 50}), Some("chat-limits-1"))
    })
    .await;
    let served = statuses.iter().filter(|s| **s == StatusCode::OK).count();
    assert_eq!(
        served, 2,
        "a conversation's burst cannot share its headroom: {statuses:?}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_redis_that_cannot_hold_a_limit_refuses_the_call(pool: PgPool) {
    let mock = slow_upstream(20, 5).await;
    let t = "t-lim-fault";
    let (rg, key) = topology(&pool, t, &mock).await;
    common::set_llm_global_limits(&pool, &rg, json!({"budget": {"daily_limit_usd": 100.0}})).await;
    // Fault injection: the group's cost counter holds the wrong type, so the
    // admission script errors on it.
    let day = crate::common::db_now().timestamp() / 86_400;
    let mut c = redis_conn().await;
    let _: () = redis::cmd("HSET")
        .arg(format!("brutor:uq:d:cost:grp:{rg}:{day}"))
        .arg("x")
        .arg("1")
        .query_async(&mut c)
        .await
        .unwrap();

    let resp = common::build_llm_app(replica(&pool).await)
        .oneshot(chat_request(&key, t, json!({"max_tokens": 10}), None))
        .await
        .unwrap();
    let status = resp.status();
    let body = resp.into_body().collect().await.unwrap().to_bytes();
    assert_eq!(
        status,
        StatusCode::TOO_MANY_REQUESTS,
        "{}",
        String::from_utf8_lossy(&body)
    );
    assert!(
        String::from_utf8_lossy(&body).contains("limits_unavailable"),
        "{}",
        String::from_utf8_lossy(&body)
    );
    assert!(
        mock.received_requests().await.unwrap().is_empty(),
        "a limit nobody can check is not a limit nobody set: the upstream is never reached"
    );
}

// ---------------------------------------------------------------------------
// One request is one call in flight
// ---------------------------------------------------------------------------

/// The members of one slot set right now (expired leases excluded by the
/// reaper's own rule are still counted: this reads the raw zset).
async fn slots_in_use(key: &str) -> i64 {
    let mut c = redis_conn().await;
    redis::cmd("ZCARD")
        .arg(key)
        .query_async(&mut c)
        .await
        .unwrap()
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_group_concurrency_cap_admits_as_many_chat_calls_as_configured(pool: PgPool) {
    let mock = slow_upstream(20, 5).await;
    let t = "t-lim-conc";
    let (rg, key) = topology(&pool, t, &mock).await;
    common::set_llm_global_limits(&pool, &rg, json!({"concurrency": {"max_concurrent": 2}})).await;

    // The route took the group's slot and the dispatch gate below it took
    // the same slot again, so a cap of 2 admitted ONE call at a time (or
    // none, when two routes each took one and both gates then found it full).
    let statuses = burst(&pool, 6, || {
        chat_request(&key, t, json!({"max_tokens": 10}), None)
    })
    .await;
    let served = statuses.iter().filter(|s| **s == StatusCode::OK).count();
    assert!(
        statuses
            .iter()
            .all(|s| *s == StatusCode::OK || *s == StatusCode::TOO_MANY_REQUESTS),
        "{statuses:?}"
    );
    assert_eq!(
        served, 2,
        "max_concurrent 2 admits exactly two calls at once: {statuses:?}"
    );
    settled().await;
    assert_eq!(
        slots_in_use(&format!("brutor:llm_slot:llm:g:{rg}")).await,
        0,
        "every slot is released when its call completes"
    );
}

async fn acquire_in(
    slots: brutor_gateway_core::middleware::run_end::HeldSlots,
    state: &SharedState,
    pool: &PgPool,
    group: &brutor_gateway_core::models::db::ResourceGroup,
) -> Result<
    brutor_gateway_core::services::usage::LlmConcurrencyGuard,
    brutor_gateway_core::services::usage::McpRateLimitBlock,
> {
    brutor_gateway_core::middleware::run_end::REQUEST_SLOTS
        .scope(
            slots,
            brutor_gateway_core::services::usage::acquire_llm_concurrency(
                &state.usage_state,
                pool,
                group,
                "m-lim",
                None,
            ),
        )
        .await
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_layers_of_one_request_hold_each_slot_once(pool: PgPool) {
    use brutor_gateway_core::middleware::run_end::HeldSlots;
    let mock = slow_upstream(20, 5).await;
    let t = "t-lim-nest";
    let (rg, _key) = topology(&pool, t, &mock).await;
    common::set_llm_global_limits(&pool, &rg, json!({"concurrency": {"max_concurrent": 1}})).await;
    let group = common::load_resource_group(&pool, &rg).await;
    let state = replica(&pool).await;
    let slot = format!("brutor:llm_slot:llm:g:{rg}");

    let a = HeldSlots::default();
    let outer = acquire_in(a.clone(), &state, &pool, &group)
        .await
        .expect("the route layer takes the one slot");
    let inner = acquire_in(a.clone(), &state, &pool, &group)
        .await
        .expect("the dispatch layer of the SAME request is not refused by its own slot");
    assert_eq!(slots_in_use(&slot).await, 1, "one request, one slot");

    // Another request is refused: one call is in flight, at either layer.
    assert!(
        acquire_in(HeldSlots::default(), &state, &pool, &group)
            .await
            .is_err()
    );
    // The inner layer ending does not free the slot the outer still holds.
    drop(inner);
    assert!(
        acquire_in(HeldSlots::default(), &state, &pool, &group)
            .await
            .is_err(),
        "the outer layer still holds the slot"
    );
    // The outer ending frees it.
    drop(outer);
    let mut freed = false;
    for _ in 0..40 {
        if let Ok(g) = acquire_in(HeldSlots::default(), &state, &pool, &group).await {
            drop(g);
            freed = true;
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    assert!(
        freed,
        "the slot is free once the request's outermost layer ends"
    );
    assert!(
        a.lock().unwrap().is_empty(),
        "the request's claims are gone with its layers"
    );
}

// ---------------------------------------------------------------------------
// MCP: a call refused by a later gate spends nothing
// ---------------------------------------------------------------------------

const MT: &str = "t-lim-mcp";

async fn mcp_topology(pool: &PgPool, mock_uri: &str) -> String {
    common::seed_tenant(pool, MT).await;
    common::seed_resource_group(pool, "rg-lim-mcp", MT).await;
    common::seed_end_user(pool, 9811, MT).await;
    common::add_direct_membership(pool, 9811, "rg-lim-mcp", MT).await;
    common::seed_mcp_server_with_base_url(pool, "ms-lim", MT, "crm", mock_uri).await;
    common::seed_server_config(pool, "sc-lim", MT, "ms-lim").await;
    common::bind_server_config_to_rg(pool, "sc-lim", "rg-lim-mcp").await;
    common::set_mcp_capability_filter(
        pool,
        MT,
        "rg-lim-mcp",
        "sc-lim",
        "tool",
        "delete_customer",
        "disabled",
    )
    .await;
    // One call an hour of that tool, at group-tool scope.
    common::set_mcp_global_limits(
        pool,
        "rg-lim-mcp",
        json!({"per_tool": {"delete_customer": {"max_calls_per_hour": 1}}}),
    )
    .await;
    common::seed_api_key(pool, MT, "rg-lim-mcp", Some(9811), "lim-mcp-key").await
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_mcp_call_the_acl_refuses_spends_no_rate_allowance(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({"jsonrpc": "2.0", "id": 1, "result": {}})),
        )
        .expect(0)
        .mount(&mock)
        .await;
    let key = mcp_topology(&pool, &mock.uri()).await;
    let state = common::build_test_state(pool.clone());
    let call = || {
        common::build_post_request(
            "/v1/proxy/mcp/ms-lim",
            json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "delete_customer", "arguments": {}}}),
            Some(&key),
            Some(MT),
        )
    };
    for attempt in 1..=3 {
        let resp = common::build_mcp_proxy_app(state.clone())
            .oneshot(call())
            .await
            .unwrap();
        assert_eq!(
            resp.status(),
            StatusCode::FORBIDDEN,
            "attempt {attempt}: refused by the ACL, never by a rate limit the refused calls \
             used up"
        );
        // Let the refused call's guard hand its units back.
        for _ in 0..20 {
            tokio::task::yield_now().await;
        }
    }
    let us: &UsageState = &state.usage_state;
    let key = brutor_gateway_core::services::limits::freq_window_key(
        "g:rg-lim-mcp:t:delete_customer",
        QuotaWindow::Hour,
    );
    assert_eq!(us.counter_value(&key).await.unwrap(), 0);
}

/// An OpenAI Responses API stand-in that holds the request to the API's own
/// contract: `max_output_tokens` is its only output limit, and a body naming
/// a chat spelling is refused exactly as OpenAI refuses it (400
/// `unsupported_parameter`). A mock that accepts any body cannot catch a
/// request the real API rejects.
async fn strict_responses_upstream() -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/responses"))
        .respond_with(|req: &wiremock::Request| {
            let body: Value = serde_json::from_slice(&req.body).unwrap_or_default();
            for rejected in ["max_tokens", "max_completion_tokens"] {
                if body.get(rejected).is_some() {
                    return ResponseTemplate::new(400).set_body_json(json!({"error": {
                        "code": "unsupported_parameter",
                        "message": format!("Unsupported parameter: '{rejected}'. In the Responses API, this parameter has moved to 'max_output_tokens'."),
                        "param": null, "type": "invalid_request_error"}}));
                }
            }
            ResponseTemplate::new(200).set_body_json(json!({
                "id": "resp_limits", "object": "response", "status": "completed",
                "model": body["model"],
                "output": [{"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "ok"}]}],
                "usage": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25}
            }))
        })
        .mount(&mock)
        .await;
    mock
}

/// Was: a Responses call that named no output limit got the admitted
/// ceiling in the CHAT spelling (`max_completion_tokens` for GPT-5 and
/// o-series, `max_tokens` otherwise), which the Responses API refuses — every
/// such call failed with a 400 while any token or spend cap was in force.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_responses_call_is_told_its_ceiling_in_the_responses_spelling(pool: PgPool) {
    let mock = strict_responses_upstream().await;
    let t = "t-lim-resp";
    let (rg, key) = topology(&pool, t, &mock).await;
    common::set_llm_global_limits(&pool, &rg, json!({"tokens": {"daily_limit": 2_000}})).await;

    for model_name in ["test-gpt", "gpt-5.5", "o3"] {
        sqlx::query("UPDATE llm_model SET model_name = $1 WHERE id = 'm-lim'")
            .bind(model_name)
            .execute(&pool)
            .await
            .unwrap();
        let mut req = Request::builder()
            .method("POST")
            .uri("/v1/proxy/llm/responses")
            .header("content-type", "application/json")
            .header("X-Tenant-ID", t)
            .header("authorization", format!("Bearer {key}"))
            .body(Body::from(
                serde_json::to_vec(&json!({"model_id": "m-lim", "input": "hi"})).unwrap(),
            ))
            .unwrap();
        let peer: std::net::SocketAddr = "127.0.0.1:55557".parse().unwrap();
        req.extensions_mut()
            .insert(axum::extract::ConnectInfo(peer));

        let resp = common::build_llm_app(replica(&pool).await)
            .oneshot(req)
            .await
            .unwrap();
        let status = resp.status();
        let body = resp.into_body().collect().await.unwrap().to_bytes();
        assert_eq!(
            status,
            StatusCode::OK,
            "{model_name}: {}",
            String::from_utf8_lossy(&body)
        );
        let sent = mock.received_requests().await.unwrap();
        let sent: Value = serde_json::from_slice(&sent.last().unwrap().body).unwrap();
        let max = sent["max_output_tokens"]
            .as_i64()
            .unwrap_or_else(|| panic!("{model_name}: no max_output_tokens sent: {sent}"));
        assert!(
            (1..2_000).contains(&max),
            "{model_name}: the cap leaves < 2,000: sent {max}"
        );
    }
}
