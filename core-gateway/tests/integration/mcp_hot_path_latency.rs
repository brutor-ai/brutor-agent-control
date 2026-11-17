// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The latency of an MCP `tools/call`, measured (the MCP twin of
//! `llm_hot_path_latency`; protocol in `benches/README.md`, "Hot-path
//! latency measurement").
//!
//! Two calls through the *assembled* gateway, real Postgres and Redis
//! behind it:
//!
//! * `proxy`: a `tools/call` to an external MCP server (a wiremock upstream
//!   that answers at once) whose tool a JSON argument policy targets: a
//!   presence guard and a two-step band, none of which fires. The body
//!   reader, the argument-policy analysis and evaluation, the run controls
//!   and the capability gate are all on this path.
//! * `system`: a `tools/call` of `skills__list` on the Agent Skills system
//!   server: the system-server branch (one reading of the body, the run
//!   controls, the per-tool rate limits and argument policy with nothing
//!   bound, the tool).
//! * `list`: a `tools/list` to the external server: protocol traffic, which
//!   no frequency or concurrency allowance counts, but which reads the
//!   call's execution limits (timeout and payload ceiling).
//!
//! A measurement, not a gate: `#[ignore]`d, asserts nothing about speed,
//! prints one JSON line per call kind and appends it to `BRUTOR_BENCH_OUT`.
//!
//! Run:
//!   cargo nextest run --locked --features db-tests --test integration \
//!     --run-ignored only -E 'test(mcp_hot_path_latency)' --no-capture

use std::net::SocketAddr;
use std::time::{Duration, Instant};

use axum::Router;
use axum::body::Body;
use axum::extract::ConnectInfo;
use axum::http::{Request, StatusCode};
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::builder::GatewayBuilder;
use brutor_gateway_core::config::AppConfig;
use brutor_gateway_core::constants::system_server::agent_skill;
use brutor_gateway_core::services::AppState;
use brutor_gateway_core::services::telemetry::TelemetryState;

use crate::common;

const TENANT: &str = "tenant-mcpbench";
const GROUP: &str = "rg-mcpbench";
const SERVER: &str = "ms-mcpbench";
const SKILLS: &str = "ms-mcpbench-skills";

fn env_usize(name: &str, default: usize) -> usize {
    std::env::var(name)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

async fn redis_conn() -> redis::aio::ConnectionManager {
    let url =
        std::env::var("TEST_REDIS_URL").unwrap_or_else(|_| "redis://127.0.0.1:6379/15".to_string());
    let client = redis::Client::open(url.as_str()).expect("redis url");
    redis::aio::ConnectionManager::new(client)
        .await
        .unwrap_or_else(|e| panic!("connect to the test Redis at {url} (set TEST_REDIS_URL): {e}"))
}

fn request(api_key: &str, server: &str, body: &Value) -> Request<Body> {
    let mut req = Request::builder()
        .method("POST")
        .uri(format!("/v1/proxy/mcp/{server}"))
        .header("authorization", format!("Bearer {api_key}"))
        .header("x-tenant-id", TENANT)
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .expect("request");
    let peer: SocketAddr = "127.0.0.1:55555".parse().expect("peer");
    req.extensions_mut().insert(ConnectInfo(peer));
    req
}

async fn one(app: &Router, api_key: &str, server: &str, body: &Value) -> Duration {
    let started = Instant::now();
    let response = app
        .clone()
        .oneshot(request(api_key, server, body))
        .await
        .expect("router");
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 1 << 20)
        .await
        .expect("body");
    let elapsed = started.elapsed();
    assert_eq!(
        status,
        StatusCode::OK,
        "the benchmark request must succeed: {}",
        String::from_utf8_lossy(&bytes)
    );
    assert!(
        !String::from_utf8_lossy(&bytes).contains("\"isError\":true"),
        "the benchmark tool call must succeed: {}",
        String::from_utf8_lossy(&bytes)
    );
    elapsed
}

fn percentile(sorted: &[Duration], p: f64) -> f64 {
    let idx = ((sorted.len() as f64 - 1.0) * p).round() as usize;
    sorted[idx].as_secs_f64() * 1_000_000.0
}

async fn measure(app: &Router, api_key: &str, kind: &str, server: &str, body: &Value) {
    let warmup = env_usize("BRUTOR_BENCH_WARMUP", 200);
    let requests = env_usize("BRUTOR_BENCH_REQUESTS", 2000);
    for _ in 0..warmup {
        one(app, api_key, server, body).await;
    }
    let mut samples = Vec::with_capacity(requests);
    let wall = Instant::now();
    for _ in 0..requests {
        samples.push(one(app, api_key, server, body).await);
    }
    let wall = wall.elapsed();
    samples.sort();
    let mean =
        samples.iter().map(Duration::as_secs_f64).sum::<f64>() / samples.len() as f64 * 1_000_000.0;
    let line = json!({
        "benchmark": "mcp_hot_path_latency",
        "call": kind,
        "requests": requests,
        "warmup": warmup,
        "unit": "microseconds",
        "p50": percentile(&samples, 0.50).round(),
        "p90": percentile(&samples, 0.90).round(),
        "p99": percentile(&samples, 0.99).round(),
        "mean": mean.round(),
        "min": percentile(&samples, 0.0).round(),
        "max": percentile(&samples, 1.0).round(),
        "requests_per_second": (requests as f64 / wall.as_secs_f64()).round(),
        "label": std::env::var("BRUTOR_BENCH_LABEL").unwrap_or_default(),
    });
    println!("{line}");
    if let Ok(out) = std::env::var("BRUTOR_BENCH_OUT") {
        use std::io::Write;
        let mut f = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&out)
            .unwrap_or_else(|e| panic!("open {out}: {e}"));
        writeln!(f, "{line}").expect("write benchmark line");
    }
}

#[ignore = "a measurement, not a test: run explicitly (see the module docs)"]
#[sqlx::test(migrations = "./tests/migrations")]
async fn mcp_hot_path_latency(pool: PgPool) {
    let upstream = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "jsonrpc": "2.0", "id": 7,
            "result": {"content": [{"type": "text", "text": "done"}], "isError": false}
        })))
        .mount(&upstream)
        .await;

    common::seed_tenant(&pool, TENANT).await;
    common::seed_resource_group(&pool, GROUP, TENANT).await;
    common::seed_mcp_server_with_base_url(&pool, SERVER, TENANT, "bench", &upstream.uri()).await;
    common::seed_server_config(&pool, "sc-mcpbench", TENANT, SERVER).await;
    common::bind_server_config_to_rg(&pool, "sc-mcpbench", GROUP).await;
    common::seed_mcp_server_with_base_url(&pool, SKILLS, TENANT, "skills", agent_skill::BASE_URL)
        .await;
    common::seed_server_config(&pool, "sc-mcpbench-skills", TENANT, SKILLS).await;
    common::bind_server_config_to_rg(&pool, "sc-mcpbench-skills", GROUP).await;
    let api_key = common::seed_api_key(&pool, TENANT, GROUP, None, "mcpbench").await;

    // `BRUTOR_BENCH_ENVELOPE` (a JSON object) declares the group's operating
    // envelope, to measure the request path with run ceilings and the limit
    // on new runs armed. Unset: no envelope, the shape the protocol compares.
    if let Ok(envelope) = std::env::var("BRUTOR_BENCH_ENVELOPE") {
        let envelope: Value = serde_json::from_str(&envelope).expect("BRUTOR_BENCH_ENVELOPE");
        common::set_rg_operating_envelope(&pool, GROUP, envelope).await;
    }
    // `BRUTOR_BENCH_MCP_LIMITS` (a JSON object) is written to the group's
    // `mcp_global_limits`, to measure the path with MCP limits armed (for
    // example concurrency allowances: the held path). Unset: no limits, the
    // shape the protocol compares.
    if let Ok(limits) = std::env::var("BRUTOR_BENCH_MCP_LIMITS") {
        let limits: Value = serde_json::from_str(&limits).expect("BRUTOR_BENCH_MCP_LIMITS");
        common::set_mcp_global_limits(&pool, GROUP, limits).await;
    }

    // A band on the proxied tool: guard, deny over 10,000, hold over 1,000.
    let rules = json!([
        {"allow_only_if": "field_present", "values": ["amount"], "severity": "deny"},
        {"deny_if": "field_gt", "field": "amount", "value": 10000, "severity": "deny"},
        {"deny_if": "field_gt", "field": "amount", "value": 1000,
         "severity": "approval_required"}
    ]);
    sqlx::query(
        "INSERT INTO argument_policies \
         (id, tenant_id, name, surface, target, argument_key, analyzer, \
          analyzer_config, rules, enabled, severity, created_at, updated_at) \
         VALUES ('ap-mcpbench', $1, 'bench-band', 'mcp_input', 'transfer_funds', '*', 'json', \
                 NULL, $2::jsonb, true, 'deny', now(), now())",
    )
    .bind(TENANT)
    .bind(rules.to_string())
    .execute(&pool)
    .await
    .expect("seed policy");
    sqlx::query(
        "INSERT INTO resource_group_argument_policies (resource_group_id, argument_policy_id) \
         VALUES ($1, 'ap-mcpbench')",
    )
    .bind(GROUP)
    .execute(&pool)
    .await
    .expect("bind policy");

    let state = AppState::new(
        AppConfig::for_test("postgresql://placeholder/replaced".to_string()),
        pool.clone(),
        Some(redis_conn().await),
        reqwest::Client::new(),
        TelemetryState {
            prometheus_handle: None,
        },
    );
    let app = GatewayBuilder::assemble(state, Vec::new())
        .expect("assemble")
        .app;

    let proxied = json!({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "transfer_funds", "arguments": {
            "amount": 500, "currency": "GBP", "to": "acct-10293", "confirm": true,
            "memo": "invoice 2026-1113", "tags": ["ops", "monthly"],
            "meta": {"channel": "agent", "attempt": 1}
        }}
    });
    measure(&app, &api_key, "proxy", SERVER, &proxied).await;

    let system = json!({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": agent_skill::TOOL_LIST, "arguments": {}}
    });
    measure(&app, &api_key, "system", SKILLS, &system).await;

    let list = json!({"jsonrpc": "2.0", "id": 7, "method": "tools/list"});
    measure(&app, &api_key, "list", SERVER, &list).await;
}
