// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The latency of the LLM hot path, measured.
//!
//! Why it exists: the response cache, the prompt-cache policy and the context
//! enricher become hooks with no-op defaults. "A no-op
//! costs nothing" is a claim about the request path, so it is measured on
//! the request path: a chat completion through the *assembled* gateway
//! (`GatewayBuilder::assemble`, every middleware layer), with real Postgres
//! and real Redis behind it and an upstream that answers at once. What is
//! left is the gateway's own time per request.
//!
//! This is a measurement, not a gate: it is `#[ignore]`d, never part of the
//! suite, and makes no assertion about speed (a threshold on a developer
//! machine fails for reasons that are not the code). It prints one JSON line
//! and, when `BRUTOR_BENCH_OUT` is set, appends it to that file. The
//! protocol for comparing two revisions is in `benches/README.md`
//! ("Hot-path latency measurement").
//!
//! Run:
//!   cargo nextest run --locked --features db-tests --test integration \
//!     --run-ignored only -E 'test(llm_hot_path_latency)' --no-capture

use std::net::SocketAddr;
use std::time::{Duration, Instant};

use axum::Router;
use axum::body::Body;
use axum::extract::ConnectInfo;
use axum::http::{Request, StatusCode};
use serde_json::json;
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::builder::GatewayBuilder;
use brutor_gateway_core::config::AppConfig;
use brutor_gateway_core::services::AppState;
use brutor_gateway_core::services::telemetry::TelemetryState;

use crate::common;

const TENANT: &str = "tenant-bench";
const GROUP: &str = "rg-bench";
const MODEL: &str = "m-bench";

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

fn request(api_key: &str) -> Request<Body> {
    let mut req = Request::builder()
        .method("POST")
        .uri("/v1/proxy/llm/chat/completions")
        .header("authorization", format!("Bearer {api_key}"))
        .header("x-tenant-id", TENANT)
        .header("content-type", "application/json")
        .body(Body::from(
            json!({
                "model_id": MODEL,
                "messages": [
                    {"role": "system", "content": "You are a careful assistant."},
                    {"role": "user", "content": "Summarise the attached paragraph in one sentence."}
                ]
            })
            .to_string(),
        ))
        .expect("request");
    let peer: SocketAddr = "127.0.0.1:55555".parse().expect("peer");
    req.extensions_mut().insert(ConnectInfo(peer));
    req
}

async fn one(app: &Router, api_key: &str) -> Duration {
    let started = Instant::now();
    let response = app.clone().oneshot(request(api_key)).await.expect("router");
    let status = response.status();
    let body = axum::body::to_bytes(response.into_body(), 1 << 20)
        .await
        .expect("body");
    let elapsed = started.elapsed();
    assert_eq!(
        status,
        StatusCode::OK,
        "the benchmark request must succeed: {}",
        String::from_utf8_lossy(&body)
    );
    elapsed
}

fn percentile(sorted: &[Duration], p: f64) -> f64 {
    let idx = ((sorted.len() as f64 - 1.0) * p).round() as usize;
    sorted[idx].as_secs_f64() * 1_000_000.0
}

#[ignore = "a measurement, not a test: run explicitly (see the module docs)"]
#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_hot_path_latency(pool: PgPool) {
    let warmup = env_usize("BRUTOR_BENCH_WARMUP", 200);
    let requests = env_usize("BRUTOR_BENCH_REQUESTS", 2000);

    let upstream = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "chatcmpl-bench",
            "object": "chat.completion",
            "created": 1_700_000_000,
            "model": "test-gpt",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "One sentence."},
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 24, "completion_tokens": 3, "total_tokens": 27}
        })))
        .mount(&upstream)
        .await;

    // The Community shape: a tenant, a resource group, one model bound to
    // it, one API key. No response cache, no prompt-cache policy, no
    // knowledge base — the paths the hooks' defaults stand in for.
    common::seed_tenant(&pool, TENANT).await;
    common::seed_resource_group(&pool, GROUP, TENANT).await;
    common::seed_llm_provider(&pool, "prov-bench", TENANT, "openai").await;
    common::seed_llm_model_with_api_url(
        &pool,
        MODEL,
        TENANT,
        "prov-bench",
        "test-gpt",
        &upstream.uri(),
    )
    .await;
    common::bind_llm_model_to_rg(&pool, MODEL, GROUP).await;
    let api_key = common::seed_api_key(&pool, TENANT, GROUP, None, "bench").await;

    // `BRUTOR_BENCH_ENVELOPE` (a JSON object) declares the group's operating
    // envelope, to measure the request path with run ceilings and the limit
    // on new runs armed. Unset: no envelope, the shape the protocol compares.
    if let Ok(envelope) = std::env::var("BRUTOR_BENCH_ENVELOPE") {
        let envelope: serde_json::Value =
            serde_json::from_str(&envelope).expect("BRUTOR_BENCH_ENVELOPE");
        common::set_rg_operating_envelope(&pool, GROUP, envelope).await;
    }

    let state = AppState::new(
        AppConfig::for_test("postgresql://placeholder/replaced".to_string()),
        pool.clone(),
        Some(redis_conn().await),
        reqwest::Client::new(),
        TelemetryState {
            prometheus_handle: None,
        },
    );
    // The protocol measures the Community shape (no extension installed),
    // whichever edition runs the suite. `BRUTOR_BENCH_EDITION=1` assembles
    // the extensions of the edition running it instead (in the Enterprise
    // workspace: the distribution's, prompt caching among them, with no
    // policy bound), to see what they cost a request that uses none. It
    // replaces `BRUTOR_BENCH_PROMPT_CACHE=1`, which installed prompt caching
    // alone.
    let extensions = if std::env::var("BRUTOR_BENCH_EDITION").is_ok_and(|v| v == "1") {
        crate::composition::extensions()
    } else {
        Vec::new()
    };
    let gateway = GatewayBuilder::assemble(state, extensions).expect("assemble");
    let app = gateway.app;

    for _ in 0..warmup {
        one(&app, &api_key).await;
    }
    let mut samples = Vec::with_capacity(requests);
    let wall = Instant::now();
    for _ in 0..requests {
        samples.push(one(&app, &api_key).await);
    }
    let wall = wall.elapsed();
    samples.sort();

    let mean =
        samples.iter().map(Duration::as_secs_f64).sum::<f64>() / samples.len() as f64 * 1_000_000.0;
    let line = json!({
        "benchmark": "llm_hot_path_latency",
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
