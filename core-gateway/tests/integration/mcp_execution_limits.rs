// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! MCP execution limits, end to end: the server's per-instance concurrency
//! permit, the cluster-wide concurrency allowances per group and per user
//! (Redis slots), the payload ceiling and the call timeout.
//!
//! Every test drives the MCP proxy route against an upstream started here
//! (`Upstream`), whose answers a test holds open on a gate, streams, cuts
//! short or slows down, and a REAL Redis (`TEST_REDIS_URL`): the slots are
//! cluster-wide state and an in-memory twin is not evidence of them.
//!
//! The upstream is an axum server rather than wiremock: wiremock's
//! responder is synchronous, so it can neither hold an answer open until a
//! test opens a gate nor stream an SSE body chunk by chunk, and both are
//! what the exit paths below need.
//!
//! The invariant every exit path is held to: once the call is over (an
//! answer, a refusal, an upstream error, a timeout, a cut, a hang-up), its
//! server permit is back and its slots are gone.

use std::sync::Arc;
use std::sync::atomic::{AtomicI64, AtomicUsize, Ordering};
use std::time::Duration;

use axum::Router;
use axum::body::{Body, Bytes};
use axum::extract::State;
use axum::http::{HeaderMap, Request, StatusCode};
use axum::response::{IntoResponse, Response};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;

use brutor_gateway_core::services::SharedState;

use brutor_gateway_core::services::mcp_limits;

use crate::common;

// ---------------------------------------------------------------------------
// The upstream
// ---------------------------------------------------------------------------

#[derive(Clone)]
struct UpstreamState {
    gate: tokio::sync::watch::Receiver<bool>,
    /// tools/call requests that reached the upstream.
    calls: Arc<AtomicUsize>,
    /// Response bodies the upstream is still writing (a stream the gateway
    /// stopped reading is dropped and counted out).
    open_streams: Arc<AtomicI64>,
}

struct Upstream {
    uri: String,
    gate: tokio::sync::watch::Sender<bool>,
    calls: Arc<AtomicUsize>,
    open_streams: Arc<AtomicI64>,
}

impl Upstream {
    fn open_gate(&self) {
        let _ = self.gate.send(true);
    }

    fn close_gate(&self) {
        let _ = self.gate.send(false);
    }
}

/// Counts a body out when hyper drops it (the stream ended or the gateway
/// hung up).
struct StreamGuard(Arc<AtomicI64>);

impl StreamGuard {
    fn new(n: &Arc<AtomicI64>) -> Self {
        n.fetch_add(1, Ordering::SeqCst);
        Self(n.clone())
    }
}

impl Drop for StreamGuard {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::SeqCst);
    }
}

fn sse_event(v: &Value) -> Bytes {
    Bytes::from(format!("event: message\ndata: {v}\n\n"))
}

fn tool_result(id: &Value, text: &str) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "result": {
        "content": [{"type": "text", "text": text}], "isError": false}})
}

async fn wait_gate(mut gate: tokio::sync::watch::Receiver<bool>) {
    let _ = gate.wait_for(|open| *open).await;
}

/// One body: `chunks` then, if `fail`, an error that aborts the response.
fn streamed(
    open: &Arc<AtomicI64>,
    chunks: Vec<(Duration, Bytes)>,
    fail: bool,
    forever: Option<Duration>,
    gate: Option<tokio::sync::watch::Receiver<bool>>,
) -> Body {
    let guard = StreamGuard::new(open);
    let s = futures::stream::unfold(
        (chunks.into_iter(), gate, guard, 0u64, false),
        move |(mut it, gate, guard, n, done)| async move {
            if done {
                return None;
            }
            if let Some(g) = gate {
                wait_gate(g).await;
            }
            match it.next() {
                Some((delay, chunk)) => {
                    tokio::time::sleep(delay).await;
                    Some((Ok(chunk), (it, None, guard, n, false)))
                }
                None => {
                    if fail {
                        return Some((
                            Err(std::io::Error::other("upstream broke off")),
                            (it, None, guard, n, true),
                        ));
                    }
                    let every = forever?;
                    tokio::time::sleep(every).await;
                    let ping = sse_event(&json!({"jsonrpc": "2.0",
                        "method": "notifications/progress", "params": {"progress": n}}));
                    Some((Ok(ping), (it, None, guard, n + 1, false)))
                }
            }
        },
    );
    Body::from_stream(s)
}

/// What a `tools/call` asks the upstream to do: its `arguments`.
///
/// * `reply`: `json` (default) | `sse` (one result event) | `sse_forever`
///   (pings every 100 ms, never ends) | `status` | `big` | `sse_big` |
///   `broken` (a body that breaks off) | `slow_body` (headers at once, the
///   body after `delay_ms`);
/// * `gate`: hold the answer (the result event, for `sse`) until the test
///   opens the gate;
/// * `delay_ms`: wait before the headers (`slow_body`: before the body);
/// * `status`, `bytes`, `chunked`.
async fn upstream_post(State(s): State<UpstreamState>, body: Bytes) -> Response {
    let req: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
    let id = req.get("id").cloned().unwrap_or(Value::Null);
    let method = req.get("method").and_then(Value::as_str).unwrap_or("");
    if method != "tools/call" {
        if let Some(ms) = req.pointer("/params/delay_ms").and_then(Value::as_u64) {
            tokio::time::sleep(Duration::from_millis(ms)).await;
        }
        return axum::Json(json!({"jsonrpc": "2.0", "id": id, "result": {
            "tools": [{"name": "work", "description": "does work"}]}}))
        .into_response();
    }
    s.calls.fetch_add(1, Ordering::SeqCst);
    let args = req
        .pointer("/params/arguments")
        .cloned()
        .unwrap_or(json!({}));
    let reply = args.get("reply").and_then(Value::as_str).unwrap_or("json");
    let gate = args.get("gate").and_then(Value::as_bool).unwrap_or(false);
    let delay = Duration::from_millis(args.get("delay_ms").and_then(Value::as_u64).unwrap_or(0));
    let bytes = args.get("bytes").and_then(Value::as_u64).unwrap_or(0) as usize;
    let chunked = args
        .get("chunked")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if reply != "slow_body" && !delay.is_zero() {
        tokio::time::sleep(delay).await;
    }
    if gate && reply != "sse" {
        wait_gate(s.gate.clone()).await;
    }
    let sse_headers = [("content-type", "text/event-stream")];
    let json_headers = [("content-type", "application/json")];
    match reply {
        "status" => {
            let status = args.get("status").and_then(Value::as_u64).unwrap_or(500) as u16;
            (
                StatusCode::from_u16(status).unwrap(),
                axum::Json(json!({"jsonrpc": "2.0", "id": id,
                    "error": {"code": -32603, "message": "upstream failed"}})),
            )
                .into_response()
        }
        "sse" => {
            let event = sse_event(&tool_result(&id, "done"));
            let g = gate.then(|| s.gate.clone());
            (
                sse_headers,
                streamed(
                    &s.open_streams,
                    vec![(Duration::ZERO, event)],
                    false,
                    None,
                    g,
                ),
            )
                .into_response()
        }
        "sse_forever" => (
            sse_headers,
            streamed(
                &s.open_streams,
                Vec::new(),
                false,
                Some(Duration::from_millis(100)),
                None,
            ),
        )
            .into_response(),
        "sse_big" => {
            let text = "x".repeat(bytes);
            let event = sse_event(&tool_result(&id, &text));
            let chunks = event
                .chunks(16 * 1024)
                .map(|c| (Duration::from_millis(5), Bytes::copy_from_slice(c)))
                .collect();
            (
                sse_headers,
                streamed(&s.open_streams, chunks, false, None, None),
            )
                .into_response()
        }
        "big" => {
            let body = serde_json::to_vec(&tool_result(&id, &"x".repeat(bytes))).unwrap();
            if chunked {
                let chunks = body
                    .chunks(16 * 1024)
                    .map(|c| (Duration::ZERO, Bytes::copy_from_slice(c)))
                    .collect();
                (
                    json_headers,
                    streamed(&s.open_streams, chunks, false, None, None),
                )
                    .into_response()
            } else {
                (json_headers, body).into_response()
            }
        }
        "broken" => {
            let head = Bytes::from_static(b"{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":");
            (
                json_headers,
                streamed(
                    &s.open_streams,
                    vec![(Duration::ZERO, head)],
                    true,
                    None,
                    None,
                ),
            )
                .into_response()
        }
        "slow_body" => {
            let body = Bytes::from(serde_json::to_vec(&tool_result(&id, "late")).unwrap());
            (
                json_headers,
                streamed(&s.open_streams, vec![(delay, body)], false, None, None),
            )
                .into_response()
        }
        _ => axum::Json(tool_result(&id, "done")).into_response(),
    }
}

/// The Streamable HTTP listener: an SSE stream that never ends.
async fn upstream_get(State(s): State<UpstreamState>, _h: HeaderMap) -> Response {
    (
        [("content-type", "text/event-stream")],
        streamed(
            &s.open_streams,
            Vec::new(),
            false,
            Some(Duration::from_millis(100)),
            None,
        ),
    )
        .into_response()
}

async fn start_upstream() -> Upstream {
    let (gate_tx, gate_rx) = tokio::sync::watch::channel(false);
    let calls = Arc::new(AtomicUsize::new(0));
    let open_streams = Arc::new(AtomicI64::new(0));
    let state = UpstreamState {
        gate: gate_rx,
        calls: calls.clone(),
        open_streams: open_streams.clone(),
    };
    let app = Router::new()
        .route("/", axum::routing::post(upstream_post).get(upstream_get))
        .with_state(state);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind upstream");
    let addr = listener.local_addr().expect("addr");
    tokio::spawn(async move {
        let _ = axum::serve(listener, app).await;
    });
    Upstream {
        uri: format!("http://{addr}"),
        gate: gate_tx,
        calls,
        open_streams,
    }
}

// ---------------------------------------------------------------------------
// The gateway
// ---------------------------------------------------------------------------

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

/// A gateway instance whose limits live in the shared, REAL Redis.
async fn replica(pool: &PgPool) -> SharedState {
    Arc::new(brutor_gateway_core::services::AppState::new(
        brutor_gateway_core::config::AppConfig::for_test(
            "postgresql://placeholder/replaced".to_string(),
        ),
        pool.clone(),
        Some(redis_conn().await),
        reqwest::Client::new(),
        brutor_gateway_core::services::telemetry::TelemetryState {
            prometheus_handle: None,
        },
    ))
}

const SERVER: &str = "ms-exec";

/// One installation: a tenant of its own (the Redis outlives a run and is
/// shared by every test), a group, the upstream bound to it, two users with
/// a key each and a second key of the first user.
struct Fixture {
    pool: PgPool,
    tenant: String,
    /// A group of its own: the frequency buckets are keyed by group id
    /// alone, and the test Redis outlives a run.
    group: String,
    upstream: Upstream,
    state: SharedState,
    app: Router,
    key_a: String,
    key_a2: String,
    key_b: String,
}

async fn fixture(pool: PgPool) -> Fixture {
    let tenant = format!("t-mel-{}", &uuid::Uuid::new_v4().simple().to_string()[..10]);
    let group = format!("rg-exec-{}", &tenant[6..]);
    let upstream = start_upstream().await;
    common::seed_tenant(&pool, &tenant).await;
    common::seed_resource_group(&pool, &group, &tenant).await;
    common::seed_end_user(&pool, 9101, &tenant).await;
    common::seed_end_user(&pool, 9102, &tenant).await;
    common::add_direct_membership(&pool, 9101, &group, &tenant).await;
    common::add_direct_membership(&pool, 9102, &group, &tenant).await;
    common::seed_mcp_server_with_base_url(&pool, SERVER, &tenant, "exec", &upstream.uri).await;
    common::seed_server_config(&pool, "sc-exec", &tenant, SERVER).await;
    common::bind_server_config_to_rg(&pool, "sc-exec", &group).await;
    let key_a = common::seed_api_key(&pool, &tenant, &group, Some(9101), "a").await;
    let key_a2 = common::seed_api_key(&pool, &tenant, &group, Some(9101), "a2").await;
    let key_b = common::seed_api_key(&pool, &tenant, &group, Some(9102), "b").await;
    let state = replica(&pool).await;
    let app = common::build_mcp_proxy_app(state.clone());
    Fixture {
        pool,
        tenant,
        group,
        upstream,
        state,
        app,
        key_a,
        key_a2,
        key_b,
    }
}

impl Fixture {
    async fn set_group_limits(&self, limits: Value) {
        common::set_mcp_global_limits(&self.pool, &self.group, limits).await;
    }

    async fn set_server(&self, column: &str, value: i32) {
        sqlx::query(&format!(
            "UPDATE mcp_servers SET {column} = $1 WHERE id = $2"
        ))
        .bind(value)
        .bind(SERVER)
        .execute(&self.pool)
        .await
        .expect("update mcp_servers");
    }

    fn post(&self, key: &str, body: &Value) -> Request<Body> {
        let mut r = common::build_post_request(
            &format!("/v1/proxy/mcp/{SERVER}"),
            body.clone(),
            Some(key),
            Some(&self.tenant),
        );
        r.headers_mut().insert(
            "accept",
            "application/json, text/event-stream".parse().unwrap(),
        );
        r
    }

    fn listener(&self, key: &str) -> Request<Body> {
        let mut r = Request::builder()
            .method("GET")
            .uri(format!("/v1/proxy/mcp/{SERVER}"))
            .header("accept", "text/event-stream")
            .header("X-Tenant-ID", &self.tenant)
            .header("authorization", format!("Bearer {key}"))
            .body(Body::empty())
            .unwrap();
        let peer: std::net::SocketAddr = "127.0.0.1:55557".parse().unwrap();
        r.extensions_mut().insert(axum::extract::ConnectInfo(peer));
        r
    }

    async fn send(&self, req: Request<Body>) -> Response {
        self.app.clone().oneshot(req).await.expect("router")
    }

    async fn call(&self, key: &str, args: Value) -> Response {
        self.send(self.post(key, &call_body(args))).await
    }

    /// Permits of this server's semaphore free right now (`None` before
    /// the first call created it).
    fn free_permits(&self) -> Option<usize> {
        self.state
            .mcp_concurrency
            .get(&format!("{}:{SERVER}", self.tenant))
            .map(|s| s.available_permits())
    }

    async fn wait_permits(&self, want: usize) {
        let ok = eventually(|| async { self.free_permits() == Some(want) }).await;
        assert!(
            ok,
            "the server permits must come back to {want}; free: {:?}",
            self.free_permits()
        );
    }

    async fn rows(&self, subtype: &str) -> Vec<(Option<i32>, Option<String>)> {
        self.rows_n(subtype, 1).await
    }

    /// The rows of `subtype` once at least `want` are written (the
    /// background writer flushes off the request path).
    async fn rows_n(&self, subtype: &str, want: usize) -> Vec<(Option<i32>, Option<String>)> {
        let mut rows = Vec::new();
        for _ in 0..50 {
            rows = sqlx::query_as(
                "SELECT http_status_code, error_message FROM proxy_logs \
                 WHERE tenant_id = $1 AND proxy_subtype = $2",
            )
            .bind(&self.tenant)
            .bind(subtype)
            .fetch_all(&self.pool)
            .await
            .expect("proxy_logs");
            if rows.len() >= want {
                return rows;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        rows
    }
}

fn call_body(args: Value) -> Value {
    json!({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
           "params": {"name": "work", "arguments": args}})
}

async fn eventually<F, Fut>(mut check: F) -> bool
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = bool>,
{
    for _ in 0..100 {
        if check().await {
            return true;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    false
}

async fn body_json(resp: Response) -> Value {
    let bytes = resp.into_body().collect().await.expect("body").to_bytes();
    serde_json::from_slice(&bytes)
        .unwrap_or_else(|_| json!({"raw": String::from_utf8_lossy(&bytes).to_string()}))
}

async fn body_text(resp: Response) -> String {
    let bytes = resp.into_body().collect().await.expect("body").to_bytes();
    String::from_utf8_lossy(&bytes).to_string()
}

/// The next data frame of a streamed body.
async fn next_frame(body: &mut Body) -> Option<Bytes> {
    loop {
        match tokio::time::timeout(Duration::from_secs(5), body.frame()).await {
            Ok(Some(Ok(f))) => {
                if let Ok(d) = f.into_data() {
                    return Some(d);
                }
            }
            _ => return None,
        }
    }
}

// ---------------------------------------------------------------------------
// The server's concurrency permit (per gateway instance)
// ---------------------------------------------------------------------------

/// A POST whose answer is an SSE stream holds its permit until the stream
/// ends; a call refused for want of one is a JSON-RPC 429 on the record.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_server_permit_is_held_for_a_streamed_answer_and_its_refusal_is_on_the_record(
    pool: PgPool,
) {
    let f = fixture(pool).await;
    f.set_server("max_concurrent_requests", 2).await;

    let a = f
        .call(&f.key_a, json!({"reply": "sse", "gate": true}))
        .await;
    assert_eq!(a.status(), StatusCode::OK);
    let b = f
        .call(&f.key_b, json!({"reply": "sse", "gate": true}))
        .await;
    assert_eq!(b.status(), StatusCode::OK);
    assert_eq!(
        f.free_permits(),
        Some(0),
        "two open streams hold both permits"
    );

    let refused = f.call(&f.key_a, json!({})).await;
    assert_eq!(refused.status(), StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(
        refused
            .headers()
            .get("retry-after")
            .map(|v| v.to_str().unwrap()),
        Some("1")
    );
    let body = body_json(refused).await;
    assert_eq!(body["jsonrpc"], "2.0");
    assert_eq!(body["id"], 7);
    assert_eq!(body["error"]["code"], -32000);
    assert_eq!(body["error"]["data"]["code"], "rate_limit_exceeded");
    assert_eq!(body["error"]["data"]["scope"], "server-instance");
    assert_eq!(body["error"]["data"]["limit_type"], "concurrency");
    let rows = f.rows("rate_limit_server-instance").await;
    assert_eq!(rows.len(), 1, "the refusal writes one row: {rows:?}");
    assert_eq!(rows[0].0, Some(429));

    f.upstream.open_gate();
    assert!(body_text(a).await.contains("done"));
    assert!(body_text(b).await.contains("done"));
    f.wait_permits(2).await;
}

/// A GET listener never ends; it gives its permit back at headers, so open
/// listeners do not starve the server's calls.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_get_listener_gives_its_permit_back_at_headers(pool: PgPool) {
    let f = fixture(pool).await;
    f.set_server("max_concurrent_requests", 1).await;
    let listener = f.send(f.listener(&f.key_a)).await;
    assert_eq!(listener.status(), StatusCode::OK);
    let mut stream = listener.into_body();
    assert!(
        next_frame(&mut stream).await.is_some(),
        "the listener streams"
    );
    f.wait_permits(1).await;
    let call = f.call(&f.key_a, json!({})).await;
    assert_eq!(
        call.status(),
        StatusCode::OK,
        "a call is admitted while the listener is open"
    );
    drop(stream);
}

/// Every way a call ends gives its permit back.
#[sqlx::test(migrations = "./tests/migrations")]
async fn every_exit_path_gives_the_permit_back(pool: PgPool) {
    let f = fixture(pool).await;
    f.set_server("max_concurrent_requests", 3).await;
    f.set_server("timeout", 1).await;

    // A buffered answer.
    assert_eq!(f.call(&f.key_a, json!({})).await.status(), StatusCode::OK);
    f.wait_permits(3).await;

    // An upstream error passed through.
    let r = f
        .call(&f.key_a, json!({"reply": "status", "status": 500}))
        .await;
    assert_eq!(r.status(), StatusCode::INTERNAL_SERVER_ERROR);
    f.wait_permits(3).await;

    // No answer within the server's timeout.
    let r = f.call(&f.key_a, json!({"delay_ms": 2500})).await;
    assert!(r.status().is_server_error(), "{}", r.status());
    f.wait_permits(3).await;

    // A stream that ends.
    let r = f.call(&f.key_a, json!({"reply": "sse"})).await;
    assert!(body_text(r).await.contains("done"));
    f.wait_permits(3).await;

    // A client that hangs up on a stream passed through.
    let r = f.call(&f.key_a, json!({"reply": "sse_forever"})).await;
    let mut stream = r.into_body();
    assert!(next_frame(&mut stream).await.is_some());
    assert_eq!(
        f.free_permits(),
        Some(2),
        "the open stream holds its permit"
    );
    drop(stream);
    f.wait_permits(3).await;
    assert!(
        eventually(|| async { f.upstream.open_streams.load(Ordering::SeqCst) == 0 }).await,
        "the gateway stops reading the upstream when the client hangs up"
    );

    // A client that hangs up before any answer (the handler is dropped
    // while it waits for the upstream).
    let pending = f.call(&f.key_a, json!({"gate": true}));
    assert!(
        tokio::time::timeout(Duration::from_millis(500), pending)
            .await
            .is_err()
    );
    f.wait_permits(3).await;

    // The circuit opens on an upstream 503; the next call is refused
    // without being sent.
    f.set_server("failure_threshold", 1).await;
    let r = f
        .call(&f.key_a, json!({"reply": "status", "status": 503}))
        .await;
    assert_eq!(r.status(), StatusCode::SERVICE_UNAVAILABLE);
    let before = f.upstream.calls.load(Ordering::SeqCst);
    let r = f.call(&f.key_a, json!({})).await;
    assert_eq!(r.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(f.upstream.calls.load(Ordering::SeqCst), before);
    f.wait_permits(3).await;
}

/// A hang-up while the relay buffers a streamed result (here, for an
/// output guardrail) is seen at once: the permit comes back and the
/// upstream is dropped, although the upstream never stops sending.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_hang_up_while_buffering_is_seen(pool: PgPool) {
    let f = fixture(pool).await;
    f.set_server("max_concurrent_requests", 2).await;
    common::seed_banned_words_guardrail_surfaces(
        &f.pool,
        "gr-exec",
        &f.tenant,
        &["forbidden-word"],
        &["mcp_output"],
    )
    .await;
    common::bind_guardrail_to_rg(&f.pool, "gr-exec", &f.group).await;

    let r = f.call(&f.key_a, json!({"reply": "sse_forever"})).await;
    assert_eq!(r.status(), StatusCode::OK);
    assert!(eventually(|| async { f.upstream.open_streams.load(Ordering::SeqCst) == 1 }).await);
    assert_eq!(f.free_permits(), Some(1));
    drop(r);
    f.wait_permits(2).await;
    assert!(
        eventually(|| async { f.upstream.open_streams.load(Ordering::SeqCst) == 0 }).await,
        "the upstream stream is dropped"
    );
}

/// A changed `max_concurrent_requests` takes effect without a
/// restart. A call that holds a permit of the old size finishes normally and
/// gives it back; the next call is admitted against the new size.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_changed_server_concurrency_takes_effect_without_a_restart(pool: PgPool) {
    let f = fixture(pool).await;
    f.set_server("max_concurrent_requests", 1).await;
    assert_eq!(f.call(&f.key_a, json!({})).await.status(), StatusCode::OK);
    f.wait_permits(1).await;

    // Raised while a call holds the only permit of the old size.
    let held = f
        .call(&f.key_a, json!({"reply": "sse", "gate": true}))
        .await;
    assert_eq!(held.status(), StatusCode::OK);
    assert_eq!(f.free_permits(), Some(0));
    f.set_server("max_concurrent_requests", 3).await;
    let r = f.call(&f.key_b, json!({})).await;
    assert_eq!(
        r.status(),
        StatusCode::OK,
        "the raised size admits a call beside the one in flight"
    );
    f.wait_permits(3).await;
    f.upstream.open_gate();
    assert!(
        body_text(held).await.contains("done"),
        "the call holding an old permit finishes normally"
    );
    f.wait_permits(3).await;

    // Lowered: two calls in flight fill a size of two; a third is refused.
    f.set_server("max_concurrent_requests", 2).await;
    f.upstream.close_gate();
    let a = f
        .call(&f.key_a, json!({"reply": "sse", "gate": true}))
        .await;
    let b = f
        .call(&f.key_b, json!({"reply": "sse", "gate": true}))
        .await;
    assert_eq!((a.status(), b.status()), (StatusCode::OK, StatusCode::OK));
    let refused = f.call(&f.key_a, json!({})).await;
    assert_eq!(refused.status(), StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(
        body_json(refused).await["error"]["data"]["scope"],
        "server-instance"
    );
    f.upstream.open_gate();
    assert!(body_text(a).await.contains("done"));
    assert!(body_text(b).await.contains("done"));
    f.wait_permits(2).await;
}

/// `mcp_servers.max_concurrent_requests` has no default: a server that does
/// not state it holds NULL, which the gateway reads as ten permits on this
/// instance. The database refuses a value that is not positive.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unset_server_concurrency_is_ten_permits_per_instance(pool: PgPool) {
    let f = fixture(pool).await;
    let stated: Option<i32> =
        sqlx::query_scalar("SELECT max_concurrent_requests FROM mcp_servers WHERE id = $1")
            .bind(SERVER)
            .fetch_one(&f.pool)
            .await
            .expect("read mcp_servers");
    assert_eq!(
        stated, None,
        "a server that states no concurrency holds NULL"
    );

    assert_eq!(f.call(&f.key_a, json!({})).await.status(), StatusCode::OK);
    f.wait_permits(10).await;
    let size = f
        .state
        .mcp_concurrency
        .get(&format!("{}:{SERVER}", f.tenant))
        .map(|s| s.size());
    assert_eq!(size, Some(10), "NULL is read as ten permits");

    for refused in [0, -1] {
        let r = sqlx::query("UPDATE mcp_servers SET max_concurrent_requests = $1 WHERE id = $2")
            .bind(refused)
            .bind(SERVER)
            .execute(&f.pool)
            .await;
        assert!(
            r.is_err(),
            "the database refuses max_concurrent_requests = {refused}"
        );
    }
}

// ---------------------------------------------------------------------------
// Payload
// ---------------------------------------------------------------------------

/// A `tools/call` body of exactly `len` bytes.
fn call_of_len(len: usize) -> Value {
    let base = serde_json::to_vec(&call_body(json!({"pad": ""})))
        .unwrap()
        .len();
    assert!(len >= base);
    call_body(json!({"pad": "x".repeat(len - base)}))
}

async fn assert_payload_error(resp: Response, limit: usize) {
    assert_eq!(resp.status(), StatusCode::BAD_GATEWAY);
    let body = body_json(resp).await;
    assert_eq!(body["jsonrpc"], "2.0", "{body}");
    assert_eq!(body["error"]["code"], -32000, "{body}");
    assert_eq!(body["error"]["data"]["code"], "payload_too_large", "{body}");
    assert_eq!(body["error"]["data"]["direction"], "response", "{body}");
    assert_eq!(body["error"]["data"]["limit_bytes"], limit, "{body}");
}

/// The request ceiling is the configured limit: a body one byte over it is
/// refused (413, on the record), a body at it passes.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_request_over_the_payload_limit_is_refused(pool: PgPool) {
    let f = fixture(pool).await;
    // 0.002 MB = 2097 bytes.
    f.set_group_limits(json!({"data": {"max_payload_size_mb": 0.002}}))
        .await;
    let limit = (0.002f64 * 1_048_576.0).floor() as usize;
    let at = f.send(f.post(&f.key_a, &call_of_len(limit))).await;
    assert_eq!(at.status(), StatusCode::OK);
    let over = f.send(f.post(&f.key_a, &call_of_len(limit + 1))).await;
    assert_eq!(over.status(), StatusCode::PAYLOAD_TOO_LARGE);
    let body = body_json(over).await;
    assert!(
        body.to_string().contains(&limit.to_string()),
        "the refusal names the limit: {body}"
    );
    let rows = f.rows("payload_limit").await;
    assert_eq!(rows.len(), 1, "{rows:?}");
    assert_eq!(rows[0].0, Some(413));
    assert_eq!(
        f.upstream.calls.load(Ordering::SeqCst),
        1,
        "only the call at the limit was sent"
    );
}

/// A configured limit above 10 MB does not raise the request ceiling.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_limit_above_ten_megabytes_keeps_the_ceiling(pool: PgPool) {
    let f = fixture(pool).await;
    f.set_group_limits(json!({"data": {"max_payload_size_mb": 50}}))
        .await;
    let over = f
        .send(f.post(&f.key_a, &call_of_len(10 * 1024 * 1024 + 1)))
        .await;
    assert_eq!(over.status(), StatusCode::PAYLOAD_TOO_LARGE);
}

/// A buffered answer over the limit is refused (502, JSON-RPC
/// `payload_too_large`), whether its length is announced or not; with no
/// limit configured, a large answer passes.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_buffered_answer_over_the_limit_is_refused(pool: PgPool) {
    let f = fixture(pool).await;
    let big = f
        .call(
            &f.key_a,
            json!({"reply": "big", "bytes": 2_000_000, "chunked": true}),
        )
        .await;
    assert_eq!(big.status(), StatusCode::OK, "no limit: no cap");
    assert!(body_text(big).await.len() > 2_000_000);

    f.set_group_limits(json!({"data": {"max_payload_size_mb": 0.5}}))
        .await;
    let limit = 524_288;
    assert_payload_error(
        f.call(
            &f.key_a,
            json!({"reply": "big", "bytes": 600_000, "chunked": true}),
        )
        .await,
        limit,
    )
    .await;
    assert_payload_error(
        f.call(&f.key_a, json!({"reply": "big", "bytes": 600_000}))
            .await,
        limit,
    )
    .await;
    let rows = f.rows_n("payload_limit", 2).await;
    assert_eq!(rows.len(), 2, "{rows:?}");
    assert!(rows.iter().all(|r| r.0 == Some(502)), "{rows:?}");
}

/// An answer the upstream breaks off is a 502, never an empty 200.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_answer_that_breaks_off_is_a_bad_gateway(pool: PgPool) {
    let f = fixture(pool).await;
    let r = f.call(&f.key_a, json!({"reply": "broken"})).await;
    assert_eq!(r.status(), StatusCode::BAD_GATEWAY);
}

async fn assert_cut_stream(resp: Response, code: &str) {
    assert_eq!(resp.status(), StatusCode::OK, "the stream had started");
    let text = tokio::time::timeout(Duration::from_secs(10), body_text(resp))
        .await
        .expect("the stream is closed");
    // The last data line: the error event the cut stream ends with.
    let last = text
        .lines()
        .rev()
        .find_map(|l| l.strip_prefix("data: "))
        .unwrap_or("null")
        .to_string();
    let data: Value = serde_json::from_str(&last).unwrap_or(Value::Null);
    assert_eq!(data["error"]["data"]["code"], code, "last event: {last}");
    assert_eq!(data["id"], 7, "the error answers the call: {last}");
}

/// A streamed answer over the limit is cut with an error event, passed
/// through or buffered; the upstream is dropped.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_streamed_answer_over_the_limit_is_cut(pool: PgPool) {
    let f = fixture(pool).await;
    f.set_group_limits(json!({"data": {"max_payload_size_mb": 0.1}}))
        .await;
    let r = f
        .call(&f.key_a, json!({"reply": "sse_big", "bytes": 400_000}))
        .await;
    assert_cut_stream(r, "payload_too_large").await;
    assert!(eventually(|| async { f.upstream.open_streams.load(Ordering::SeqCst) == 0 }).await);

    common::seed_banned_words_guardrail_surfaces(
        &f.pool,
        "gr-exec",
        &f.tenant,
        &["forbidden-word"],
        &["mcp_output"],
    )
    .await;
    common::bind_guardrail_to_rg(&f.pool, "gr-exec", &f.group).await;
    let r = f
        .call(&f.key_a, json!({"reply": "sse_big", "bytes": 400_000}))
        .await;
    assert_cut_stream(r, "payload_too_large").await;
    let rows: Vec<(Option<i32>,)> = sqlx::query_as(
        "SELECT http_status_code FROM proxy_logs WHERE tenant_id = $1 AND http_status_code = 502",
    )
    .bind(&f.tenant)
    .fetch_all(&f.pool)
    .await
    .unwrap();
    assert!(
        eventually(|| async {
            let n: (i64,) = sqlx::query_as(
                "SELECT count(*) FROM proxy_logs WHERE tenant_id = $1 AND http_status_code = 502",
            )
            .bind(&f.tenant)
            .fetch_one(&f.pool)
            .await
            .unwrap();
            n.0 == 2
        })
        .await,
        "both cuts are on the record as 502: {rows:?}"
    );
}

/// The GET listener is not cut by the payload limit nor by the timeout.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_listener_is_exempt_from_the_cumulative_limits(pool: PgPool) {
    let f = fixture(pool).await;
    f.set_group_limits(json!({
        "data": {"max_payload_size_mb": 0.0005},
        "execution": {"timeout_seconds": 1}
    }))
    .await;
    let listener = f.send(f.listener(&f.key_a)).await;
    assert_eq!(listener.status(), StatusCode::OK);
    let mut body = listener.into_body();
    let mut seen = 0usize;
    let started = std::time::Instant::now();
    while started.elapsed() < Duration::from_millis(2_000) {
        let frame = next_frame(&mut body)
            .await
            .expect("the listener keeps streaming");
        seen += frame.len();
    }
    assert!(seen > 524, "more than the limit went through: {seen}");
}

// ---------------------------------------------------------------------------
// Timeout
// ---------------------------------------------------------------------------

/// `execution.timeout_seconds` bounds the headers, a slow body and a
/// stream; the server's own timeout still bounds the headers when lower.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_call_timeout_is_a_total(pool: PgPool) {
    let f = fixture(pool).await;
    f.set_group_limits(json!({"execution": {"timeout_seconds": 1}}))
        .await;

    let t = std::time::Instant::now();
    let r = f.call(&f.key_a, json!({"delay_ms": 3000})).await;
    assert_eq!(r.status(), StatusCode::GATEWAY_TIMEOUT, "slow headers");
    assert!(
        t.elapsed() < Duration::from_millis(2_000),
        "{:?}",
        t.elapsed()
    );

    let t = std::time::Instant::now();
    let r = f
        .call(&f.key_a, json!({"reply": "slow_body", "delay_ms": 3000}))
        .await;
    assert_eq!(r.status(), StatusCode::GATEWAY_TIMEOUT, "slow body");
    assert!(
        t.elapsed() < Duration::from_millis(2_000),
        "{:?}",
        t.elapsed()
    );

    let t = std::time::Instant::now();
    let r = f.call(&f.key_a, json!({"reply": "sse_forever"})).await;
    assert_cut_stream(r, "timeout").await;
    assert!(
        t.elapsed() < Duration::from_millis(2_500),
        "{:?}",
        t.elapsed()
    );
    assert!(eventually(|| async { f.upstream.open_streams.load(Ordering::SeqCst) == 0 }).await);
    assert!(
        eventually(|| async {
            let n: (i64,) = sqlx::query_as(
                "SELECT count(*) FROM proxy_logs WHERE tenant_id = $1 AND http_status_code = 504",
            )
            .bind(&f.tenant)
            .fetch_one(&f.pool)
            .await
            .unwrap();
            n.0 == 3
        })
        .await,
        "each timeout is on the record as 504"
    );

    // The server's own (lower) timeout still bounds the headers.
    f.set_group_limits(json!({"execution": {"timeout_seconds": 30}}))
        .await;
    f.set_server("timeout", 1).await;
    let t = std::time::Instant::now();
    let r = f.call(&f.key_a, json!({"delay_ms": 3000})).await;
    assert_eq!(r.status(), StatusCode::GATEWAY_TIMEOUT);
    assert!(
        t.elapsed() < Duration::from_millis(2_000),
        "{:?}",
        t.elapsed()
    );
}

/// The retries of an idempotent request share the call's one deadline.
#[sqlx::test(migrations = "./tests/migrations")]
async fn retries_share_one_deadline(pool: PgPool) {
    let f = fixture(pool).await;
    f.set_group_limits(json!({"execution": {"timeout_seconds": 1}}))
        .await;
    f.set_server("retry_attempts", 3).await;
    let t = std::time::Instant::now();
    let r = f
        .send(f.post(
            &f.key_a,
            &json!({"jsonrpc": "2.0", "id": 2, "method": "resources/list",
                    "params": {"delay_ms": 3000}}),
        ))
        .await;
    assert_eq!(r.status(), StatusCode::GATEWAY_TIMEOUT);
    assert!(
        t.elapsed() < Duration::from_millis(1_800),
        "four attempts fit in one second: {:?}",
        t.elapsed()
    );

    // Attempts that fit before the deadline: the server's own timeout (1 s)
    // bounds each, and the last gets only what is left of the call's 2.5 s.
    f.set_group_limits(json!({"execution": {"timeout_seconds": 2.5}}))
        .await;
    f.set_server("timeout", 1).await;
    f.set_server("retry_after_ms", 1).await;
    let t = std::time::Instant::now();
    let r = f
        .send(f.post(
            &f.key_a,
            &json!({"jsonrpc": "2.0", "id": 3, "method": "resources/list",
                    "params": {"delay_ms": 5000}}),
        ))
        .await;
    assert_eq!(r.status(), StatusCode::GATEWAY_TIMEOUT);
    let took = t.elapsed();
    assert!(
        took >= Duration::from_millis(2_300) && took < Duration::from_millis(2_850),
        "three attempts, the last cut at the call's deadline: {took:?}"
    );
}

// ---------------------------------------------------------------------------
// Concurrency allowances per group and per user (cluster-wide, Redis slots)
// ---------------------------------------------------------------------------

impl Fixture {
    fn slot_key(&self, scope: &str, principal: &str) -> String {
        mcp_limits::slot_key(scope, &self.tenant, &self.group, SERVER, principal)
    }

    async fn holders(&self, key: &str) -> i64 {
        let mut c = redis_conn().await;
        redis::cmd("ZCARD")
            .arg(key)
            .query_async(&mut c)
            .await
            .expect("ZCARD")
    }

    /// Every slot set of this installation: none may hold anyone.
    async fn slot_sets(&self) -> Vec<(String, i64)> {
        let mut c = redis_conn().await;
        let keys: Vec<String> = redis::cmd("KEYS")
            .arg(format!("brutor:mcp_slot:t:{}:*", self.tenant))
            .query_async(&mut c)
            .await
            .expect("KEYS");
        let mut out = Vec::new();
        for k in keys {
            let n: i64 = redis::cmd("ZCARD")
                .arg(&k)
                .query_async(&mut c)
                .await
                .expect("ZCARD");
            out.push((k, n));
        }
        out
    }

    async fn wait_slots_free(&self) {
        let ok = eventually(|| async { self.slot_sets().await.iter().all(|(_, n)| *n == 0) }).await;
        assert!(ok, "every slot must be freed: {:?}", self.slot_sets().await);
    }

    /// Start a call that the upstream holds open on the gate; returns once
    /// the upstream has it.
    async fn start_held(&self, key: &str) -> tokio::task::JoinHandle<Response> {
        let before = self.upstream.calls.load(Ordering::SeqCst);
        let app = self.app.clone();
        let req = self.post(key, &call_body(json!({"gate": true})));
        let h = tokio::spawn(async move { app.oneshot(req).await.expect("router") });
        assert!(
            eventually(|| async { self.upstream.calls.load(Ordering::SeqCst) > before }).await,
            "the held call reaches the upstream"
        );
        h
    }
}

async fn assert_concurrency_refusal(resp: Response, scope: &str) {
    assert_eq!(resp.status(), StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(
        resp.headers()
            .get("retry-after")
            .map(|v| v.to_str().unwrap()),
        Some("1")
    );
    let body = body_json(resp).await;
    assert_eq!(body["error"]["code"], -32000, "{body}");
    assert_eq!(
        body["error"]["data"]["code"], "rate_limit_exceeded",
        "{body}"
    );
    assert_eq!(body["error"]["data"]["scope"], scope, "{body}");
    assert_eq!(body["error"]["data"]["limit_type"], "concurrency", "{body}");
}

/// `max_concurrency_per_group: 2`: a third call in flight at once is
/// refused at group scope, on the record; the slots go when the calls end.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_group_allowance_refuses_the_call_over_it(pool: PgPool) {
    let f = fixture(pool).await;
    f.set_group_limits(json!({"execution": {"max_concurrency_per_group": 2}}))
        .await;
    let a = f.start_held(&f.key_a).await;
    let b = f.start_held(&f.key_b).await;
    assert_eq!(f.holders(&f.slot_key("group", "")).await, 2);
    assert_eq!(mcp_limits::live_slot_holds(), 2, "the gauge counts both");
    let before = f.upstream.calls.load(Ordering::SeqCst);
    assert_concurrency_refusal(f.call(&f.key_a, json!({})).await, "group").await;
    assert_eq!(
        f.upstream.calls.load(Ordering::SeqCst),
        before,
        "nothing sent"
    );
    let rows = f.rows("rate_limit_group").await;
    assert_eq!(rows.len(), 1, "{rows:?}");
    assert_eq!(rows[0].0, Some(429));
    f.upstream.open_gate();
    assert_eq!(a.await.unwrap().status(), StatusCode::OK);
    assert_eq!(b.await.unwrap().status(), StatusCode::OK);
    f.wait_slots_free().await;
    assert!(eventually(|| async { mcp_limits::live_slot_holds() == 0 }).await);
}

/// `max_concurrency_per_user: 1`: one user's second call is refused while
/// another user's is admitted; the two keys of one user share the
/// allowance; two keys of the group that name no user do not.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_user_allowance_is_the_users_whatever_key_they_use(pool: PgPool) {
    let f = fixture(pool).await;
    f.set_group_limits(json!({"execution": {"max_concurrency_per_user": 1}}))
        .await;
    let a = f.start_held(&f.key_a).await;
    assert_eq!(f.holders(&f.slot_key("group-user", "user:9101")).await, 1);
    assert_concurrency_refusal(f.call(&f.key_a2, json!({})).await, "group-user").await;
    let b = f.start_held(&f.key_b).await;

    let shared_1 = common::seed_shared_group_api_key(&f.pool, &f.tenant, &f.group, "s1").await;
    let shared_2 = common::seed_shared_group_api_key(&f.pool, &f.tenant, &f.group, "s2").await;
    let s1 = f.start_held(&shared_1).await;
    let s2 = f.start_held(&shared_2).await;

    f.upstream.open_gate();
    for h in [a, b, s1, s2] {
        assert_eq!(h.await.unwrap().status(), StatusCode::OK);
    }
    f.wait_slots_free().await;
}

/// The group's binding of a server states a tighter allowance: it wins for
/// that server, and another server of the group is not touched by it.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_binding_tighter_than_the_group_wins_for_its_server_only(pool: PgPool) {
    let f = fixture(pool).await;
    f.set_group_limits(json!({"execution": {"max_concurrency_per_group": 5}}))
        .await;
    common::set_rg_server_limits(
        &f.pool,
        "sc-exec",
        &f.group,
        json!({"execution": {"max_concurrency_per_group": 1}}),
    )
    .await;
    common::seed_mcp_server_with_base_url(
        &f.pool,
        "ms-exec-2",
        &f.tenant,
        "exec2",
        &f.upstream.uri,
    )
    .await;
    common::seed_server_config(&f.pool, "sc-exec-2", &f.tenant, "ms-exec-2").await;
    common::bind_server_config_to_rg(&f.pool, "sc-exec-2", &f.group).await;

    let a = f.start_held(&f.key_a).await;
    assert_concurrency_refusal(f.call(&f.key_b, json!({})).await, "server").await;
    let mut other = common::build_post_request(
        "/v1/proxy/mcp/ms-exec-2",
        call_body(json!({})),
        Some(&f.key_b),
        Some(&f.tenant),
    );
    other
        .headers_mut()
        .insert("accept", "application/json".parse().unwrap());
    assert_eq!(f.send(other).await.status(), StatusCode::OK);
    f.upstream.open_gate();
    assert_eq!(a.await.unwrap().status(), StatusCode::OK);
    f.wait_slots_free().await;
}

/// An ancestor's allowance applies to each descendant group on its own.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_ancestors_allowance_is_merged_into_the_calling_group(pool: PgPool) {
    let f = fixture(pool).await;
    common::seed_resource_group(&f.pool, "rg-exec-parent", &f.tenant).await;
    common::set_rg_parent(&f.pool, &f.group, "rg-exec-parent").await;
    common::set_mcp_global_limits(
        &f.pool,
        "rg-exec-parent",
        json!({"execution": {"max_concurrency_per_group": 1}}),
    )
    .await;
    let a = f.start_held(&f.key_a).await;
    assert_concurrency_refusal(f.call(&f.key_b, json!({})).await, "group").await;
    assert_eq!(f.holders(&f.slot_key("group", "")).await, 1);
    f.upstream.open_gate();
    assert_eq!(a.await.unwrap().status(), StatusCode::OK);
    f.wait_slots_free().await;
}

/// A hang-up after dispatch keeps the frequency unit: the call ran.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_hang_up_keeps_the_frequency_unit(pool: PgPool) {
    let f = fixture(pool).await;
    f.set_group_limits(json!({
        "execution": {"max_concurrency_per_group": 5},
        "frequency": {"max_calls_per_minute": 1}
    }))
    .await;
    let r = f.call(&f.key_a, json!({"reply": "sse_forever"})).await;
    let mut body = r.into_body();
    assert!(next_frame(&mut body).await.is_some());
    drop(body);
    f.wait_slots_free().await;
    let again = f.call(&f.key_a, json!({})).await;
    assert_eq!(
        again.status(),
        StatusCode::TOO_MANY_REQUESTS,
        "the unit of the call that ran is not given back"
    );
    let body = body_json(again).await;
    assert_eq!(body["error"]["data"]["limit_type"], "rate", "{body}");
}

/// A hang-up while the upstream is still working on the call (the handler
/// is dropped, and with it what the call holds): the slot is freed, the
/// unit stays spent — the hold is abandoned, not released.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_hang_up_before_the_answer_keeps_the_frequency_unit(pool: PgPool) {
    let f = fixture(pool).await;
    f.set_group_limits(json!({
        "execution": {"max_concurrency_per_group": 5},
        "frequency": {"max_calls_per_minute": 1}
    }))
    .await;
    let held = f.start_held(&f.key_a).await;
    assert_eq!(f.holders(&f.slot_key("group", "")).await, 1);
    held.abort();
    let _ = held.await;
    f.wait_slots_free().await;
    let again = f.call(&f.key_a, json!({})).await;
    assert_eq!(
        again.status(),
        StatusCode::TOO_MANY_REQUESTS,
        "the call was dispatched: its unit is not given back"
    );
    let body = body_json(again).await;
    assert_eq!(body["error"]["data"]["limit_type"], "rate", "{body}");
}

/// A call a later gate refuses (here an argument policy) took a slot and a
/// frequency unit; it gives both back.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_later_refusal_gives_the_slot_and_the_unit_back(pool: PgPool) {
    let f = fixture(pool).await;
    f.set_group_limits(json!({
        "execution": {"max_concurrency_per_group": 1},
        "frequency": {"max_calls_per_minute": 1}
    }))
    .await;
    sqlx::query(
        "INSERT INTO argument_policies \
         (id, tenant_id, name, surface, target, argument_key, analyzer, \
          analyzer_config, rules, enabled, severity, created_at, updated_at) \
         VALUES ('ap-exec', $1, 'no-big', 'mcp_input', 'work', '*', 'json', \
                 NULL, $2::jsonb, true, 'deny', now(), now())",
    )
    .bind(&f.tenant)
    .bind(
        json!([{"deny_if": "field_gt", "field": "amount", "value": 10, "severity": "deny"}])
            .to_string(),
    )
    .execute(&f.pool)
    .await
    .expect("seed policy");
    sqlx::query(
        "INSERT INTO resource_group_argument_policies (resource_group_id, argument_policy_id) \
         VALUES ($1, 'ap-exec')",
    )
    .bind(&f.group)
    .execute(&f.pool)
    .await
    .expect("bind policy");

    let refused = f.call(&f.key_a, json!({"amount": 50})).await;
    let status = refused.status();
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "{}",
        body_text(refused).await
    );
    f.wait_slots_free().await;
    assert_eq!(
        f.call(&f.key_a, json!({"amount": 1})).await.status(),
        StatusCode::OK,
        "the refused call's slot and minute unit were given back"
    );
}

/// Two gateway instances on one Redis share one allowance.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_allowance_holds_across_instances(pool: PgPool) {
    let f = fixture(pool).await;
    f.set_group_limits(json!({"execution": {"max_concurrency_per_group": 1}}))
        .await;
    let other = common::build_mcp_proxy_app(replica(&f.pool).await);
    let a = f.start_held(&f.key_a).await;
    let resp = other
        .oneshot(f.post(&f.key_b, &call_body(json!({}))))
        .await
        .unwrap();
    assert_concurrency_refusal(resp, "group").await;
    f.upstream.open_gate();
    assert_eq!(a.await.unwrap().status(), StatusCode::OK);
    f.wait_slots_free().await;
}

/// A holder that died without ending its hold (and was never reaped)
/// frees its slot once its lease runs out: the admission purges an
/// expired member before it counts.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_crashed_holders_slot_expires_without_the_reaper(pool: PgPool) {
    use brutor_gateway_core::services::cache::usage_state::Shape;
    use brutor_gateway_core::services::limits::{HoldId, admit};

    let f = fixture(pool).await;
    f.set_group_limits(json!({"execution": {"max_concurrency_per_group": 1}}))
        .await;
    let key = f.slot_key("group", "");
    let hold = HoldId::new(&f.tenant, "mcp-rate");
    {
        let crashed = replica(&f.pool).await;
        let limits = mcp_limits::McpExecLimits {
            per_group: [Some(1), None],
            ..Default::default()
        };
        let dims = limits.slot_dims(&f.tenant, &f.group, SERVER, "anon");
        let admitted = admit(&crashed.usage_state, Some(&hold), dims, Shape::count())
            .await
            .expect("redis")
            .expect("admitted");
        // The process dies: nothing ends the hold, nothing renews it.
        std::mem::forget(admitted);
        std::mem::forget(crashed);
    }
    assert_concurrency_refusal(f.call(&f.key_a, json!({})).await, "group").await;

    // Its lease runs out (scored in the past rather than waited for).
    let mut c = redis_conn().await;
    let members: Vec<String> = redis::cmd("ZRANGE")
        .arg(&key)
        .arg(0)
        .arg(-1)
        .query_async(&mut c)
        .await
        .unwrap();
    assert_eq!(members.len(), 1);
    let past = common::db_now().timestamp_millis() - 1_000;
    let _: () = redis::cmd("ZADD")
        .arg(&key)
        .arg("XX")
        .arg(past)
        .arg(&members[0])
        .query_async(&mut c)
        .await
        .unwrap();
    assert_eq!(
        f.call(&f.key_a, json!({})).await.status(),
        StatusCode::OK,
        "the expired slot is purged by the next admission, no reaper ran"
    );
    f.wait_slots_free().await;
    // Tidy the shared Redis: the dead hold's record is ended (as the reaper
    // would), so no other test meets it in the holds set.
    f.state
        .usage_state
        .finish_hold(
            hold.as_str(),
            brutor_gateway_core::services::cache::usage_state::Finish::Abandon,
            &[],
        )
        .await
        .expect("end the dead hold");
}

/// `tools/list` and the GET listener take no slot: with the allowance
/// full, both still answer.
#[sqlx::test(migrations = "./tests/migrations")]
async fn protocol_traffic_takes_no_slot(pool: PgPool) {
    let f = fixture(pool).await;
    f.set_group_limits(json!({"execution": {
        "max_concurrency_per_group": 1, "max_concurrency_per_user": 1}}))
        .await;
    let a = f.start_held(&f.key_a).await;
    let list = f
        .send(f.post(
            &f.key_a,
            &json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
        ))
        .await;
    assert_eq!(list.status(), StatusCode::OK);
    let listener = f.send(f.listener(&f.key_a)).await;
    assert_eq!(listener.status(), StatusCode::OK);
    assert_eq!(f.holders(&f.slot_key("group", "")).await, 1);
    assert_eq!(f.holders(&f.slot_key("group-user", "user:9101")).await, 1);
    drop(listener);
    f.upstream.open_gate();
    assert_eq!(a.await.unwrap().status(), StatusCode::OK);
    f.wait_slots_free().await;
}

/// The system server's tool calls are admitted under the same allowance.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_system_server_tool_call_takes_a_slot(pool: PgPool) {
    use brutor_gateway_core::constants::system_server::agent_skill;
    let f = fixture(pool).await;
    common::seed_mcp_server_with_base_url(
        &f.pool,
        "ms-exec-skills",
        &f.tenant,
        "skills",
        agent_skill::BASE_URL,
    )
    .await;
    common::seed_server_config(&f.pool, "sc-exec-skills", &f.tenant, "ms-exec-skills").await;
    common::bind_server_config_to_rg(&f.pool, "sc-exec-skills", &f.group).await;
    f.set_group_limits(json!({"execution": {"max_concurrency_per_group": 1}}))
        .await;
    let list = || {
        let mut r = common::build_post_request(
            "/v1/proxy/mcp/ms-exec-skills",
            json!({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                   "params": {"name": agent_skill::TOOL_LIST, "arguments": {}}}),
            Some(&f.key_b),
            Some(&f.tenant),
        );
        r.headers_mut()
            .insert("accept", "application/json".parse().unwrap());
        r
    };
    assert_eq!(f.send(list()).await.status(), StatusCode::OK);
    f.wait_slots_free().await;

    let a = f.start_held(&f.key_a).await;
    let refused = f.send(list()).await;
    assert_eq!(refused.status(), StatusCode::TOO_MANY_REQUESTS);
    let body = body_json(refused).await;
    assert_eq!(
        body["error"]["data"]["rate_limit"]["limit_type"], "concurrency",
        "{body}"
    );
    assert_eq!(
        body["error"]["data"]["rate_limit"]["scope"], "group",
        "{body}"
    );
    f.upstream.open_gate();
    assert_eq!(a.await.unwrap().status(), StatusCode::OK);
    f.wait_slots_free().await;
}

/// The slots cannot be held (here: the key is not a set): the call is
/// refused as `limits_unavailable` and nothing is sent.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_allowance_that_cannot_be_held_refuses(pool: PgPool) {
    let f = fixture(pool).await;
    f.set_group_limits(json!({"execution": {"max_concurrency_per_group": 3}}))
        .await;
    let mut c = redis_conn().await;
    let _: () = redis::cmd("SET")
        .arg(f.slot_key("group", ""))
        .arg("not a set")
        .arg("EX")
        .arg(60)
        .query_async(&mut c)
        .await
        .unwrap();
    let r = f.call(&f.key_a, json!({})).await;
    assert_eq!(r.status(), StatusCode::TOO_MANY_REQUESTS);
    let body = body_json(r).await;
    assert_eq!(
        body["error"]["data"]["limit_type"], "limits_unavailable",
        "{body}"
    );
    assert_eq!(f.upstream.calls.load(Ordering::SeqCst), 0, "nothing sent");
}

/// A stated limit the gateway cannot read refuses the call.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_malformed_limit_refuses_the_call(pool: PgPool) {
    let f = fixture(pool).await;
    f.set_group_limits(json!({"execution": {"max_concurrency_per_user": "two"}}))
        .await;
    let r = f.call(&f.key_a, json!({})).await;
    assert_eq!(r.status(), StatusCode::TOO_MANY_REQUESTS);
    let body = body_json(r).await;
    assert_eq!(
        body["error"]["data"]["limit_type"], "limits_unavailable",
        "{body}"
    );
    assert_eq!(f.upstream.calls.load(Ordering::SeqCst), 0, "nothing sent");
    let rows = f.rows("rate_limit_limits").await;
    assert_eq!(rows.len(), 1, "{rows:?}");
}

/// 500 calls, at most a few admitted at a time, each hung up at a random
/// point (before an answer, mid-stream, or never): afterwards no slot is
/// held anywhere and every server permit is back.
#[sqlx::test(migrations = "./tests/migrations")]
async fn five_hundred_calls_with_random_hang_ups_leave_nothing_held(pool: PgPool) {
    use rand::Rng;
    let f = fixture(pool).await;
    f.set_server("max_concurrent_requests", 8).await;
    f.set_group_limits(json!({"execution": {
        "max_concurrency_per_group": 6, "max_concurrency_per_user": 4}}))
        .await;
    common::set_rg_server_limits(
        &f.pool,
        "sc-exec",
        &f.group,
        json!({"execution": {"max_concurrency_per_user": 3}}),
    )
    .await;
    f.upstream.open_gate();
    let keys = [f.key_a.clone(), f.key_a2.clone(), f.key_b.clone()];
    // 24 calls at a time: more than every allowance, few enough that the
    // test database does not make every call time out before dispatch.
    let at_once = Arc::new(tokio::sync::Semaphore::new(24));
    let mut tasks = Vec::new();
    for i in 0..500usize {
        let mut rng = rand::rng();
        let reply = ["json", "sse", "sse_forever", "slow_body"][rng.random_range(0..4)];
        let delay = rng.random_range(0..40u64);
        let cut_after = Duration::from_millis(rng.random_range(1..800));
        let req = f.post(
            &keys[i % 3],
            &call_body(json!({"reply": reply, "delay_ms": delay})),
        );
        let app = f.app.clone();
        let at_once = at_once.clone();
        tasks.push(tokio::spawn(async move {
            let _turn = at_once.acquire_owned().await.expect("semaphore");
            let _ = tokio::time::timeout(cut_after, async move {
                let resp = app.oneshot(req).await.expect("router");
                let mut body = resp.into_body();
                while let Some(Ok(_)) = body.frame().await {}
            })
            .await;
        }));
    }
    for t in tasks {
        t.await.expect("task");
    }
    let dispatched = f.upstream.calls.load(Ordering::SeqCst);
    assert!(
        dispatched >= 20,
        "the run must exercise calls in flight, not only refusals: {dispatched} dispatched"
    );
    f.wait_permits(8).await;
    f.wait_slots_free().await;
    assert!(
        eventually(|| async { mcp_limits::live_slot_holds() == 0 }).await,
        "no slot hold is left live in the process: {}",
        mcp_limits::live_slot_holds()
    );
}

/// A caller that hangs up while its admission is on its way to Redis: the
/// script runs, but nothing in the process owns the hold it made. The slot
/// must not stay taken until its lease runs out.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_hang_up_during_the_admission_frees_the_slot(pool: PgPool) {
    use brutor_gateway_core::services::cache::usage_state::Shape;
    use brutor_gateway_core::services::limits::{HoldId, admit};
    let f = fixture(pool).await;
    let key = f.slot_key("group", "");
    let limits = mcp_limits::McpExecLimits {
        per_group: [Some(1_000), None],
        ..Default::default()
    };
    // Each admission is dropped after 0 to 2 ms: some before the script
    // was sent, some after Redis ran it and before its answer was read.
    for i in 0..300u64 {
        let dims = limits.slot_dims(&f.tenant, &f.group, SERVER, "anon");
        let hold = HoldId::new(&f.tenant, "mcp-rate");
        let done = tokio::time::timeout(
            Duration::from_micros(i % 2_000),
            admit(&f.state.usage_state, Some(&hold), dims, Shape::count()),
        )
        .await;
        // An admission that completed is owned and ended, as the route does.
        if let Ok(Ok(Ok(admitted))) = done {
            drop(admitted.hold.map(|h| f.state.usage_state.adopt_hold(h)));
        }
    }
    let ok = eventually(|| async { f.holders(&key).await == 0 }).await;
    assert!(
        ok,
        "slots of admissions nobody owns are left taken: {}",
        f.holders(&key).await
    );
}
