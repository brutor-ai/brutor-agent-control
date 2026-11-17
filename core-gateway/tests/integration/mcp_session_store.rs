// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The session carrier across replicas: an MCP session's `initialize`
//! `clientInfo` lives in the shared store (Redis), not in one replica's
//! memory.
//!
//! * a session bound through one `AppState` (replica A) is resolved by a
//!   second, independent `AppState` (replica B) — its own connection, its
//!   own middleware;
//! * the key is namespaced and carries the tenant, a hash of the credential
//!   and the session id — never the credential;
//! * a read refreshes the 24 h idle TTL; an expired or DELETEd session
//!   resolves as absent;
//! * with the store unreachable the request still succeeds on its
//!   per-request carriers, the session carrier is absent, and the failure is
//!   counted.
//!
//! Needs a real Redis: `TEST_REDIS_URL`, defaulting to the local Redis on
//! logical DB 15 (CI provides a service container). Unreachable is a
//! failure, not a skip — a skipped test proves nothing.

use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use axum::http::{HeaderValue, Request, StatusCode};
use serde_json::json;
use sqlx::PgPool;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::config::AppConfig;
use brutor_gateway_core::services::agent_impl::{
    Release, SESSION_IDLE, SESSION_KEY_PREFIX, SessionScope, SessionStore,
};
use brutor_gateway_core::services::telemetry::TelemetryState;
use brutor_gateway_core::services::{AppState, SharedState};

use crate::common;

/// Where the tests' Redis is: `TEST_REDIS_URL`, else the conventional local
/// Redis on logical DB 15 (never the gateway's DB 0). Every key a test writes
/// is namespaced by a per-test random tenant and is deleted or expires by the
/// end of the test. An unreachable Redis FAILS the test — a skipped
/// cross-replica test proves nothing.
fn redis_url() -> String {
    std::env::var("TEST_REDIS_URL").unwrap_or_else(|_| DEFAULT_TEST_REDIS_URL.to_string())
}

const DEFAULT_TEST_REDIS_URL: &str = "redis://127.0.0.1:6379/15";

async fn connect(url: &str) -> redis::aio::ConnectionManager {
    let client = redis::Client::open(url).expect("redis url");
    redis::aio::ConnectionManager::new(client)
        .await
        .unwrap_or_else(|e| panic!("connect to the test Redis at {url} (set TEST_REDIS_URL): {e}"))
}

/// One replica: its own `AppState` over its own Redis connection.
async fn replica(pool: &PgPool, redis: redis::aio::ConnectionManager) -> SharedState {
    let config = AppConfig::for_test("postgresql://placeholder/replaced".to_string());
    let telemetry = TelemetryState {
        prometheus_handle: None,
    };
    Arc::new(AppState::new(
        config,
        pool.clone(),
        Some(redis),
        reqwest::Client::new(),
        telemetry,
    ))
}

async fn topology(pool: &PgPool, tenant: &str, upstream: &str, user: i32) -> String {
    common::seed_tenant(pool, tenant).await;
    common::seed_resource_group(pool, "rg-sess", tenant).await;
    common::seed_end_user(pool, user, tenant).await;
    common::add_direct_membership(pool, user, "rg-sess", tenant).await;
    common::seed_mcp_server_with_base_url(pool, "ms-sess", tenant, "sess", upstream).await;
    common::seed_server_config(pool, "sc-sess", tenant, "ms-sess").await;
    common::bind_server_config_to_rg(pool, "sc-sess", "rg-sess").await;
    common::seed_api_key(pool, tenant, "rg-sess", Some(user), "sess-key").await
}

async fn upstream(session_id: &str) -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}))
                .insert_header("mcp-session-id", session_id),
        )
        .mount(&mock)
        .await;
    Mock::given(method("DELETE"))
        .respond_with(ResponseTemplate::new(200))
        .mount(&mock)
        .await;
    mock
}

fn mcp(key: &str, tenant: &str, body: serde_json::Value, extra: &[(&str, &str)]) -> Request<Body> {
    let mut req =
        common::build_post_request("/v1/proxy/mcp/ms-sess", body, Some(key), Some(tenant));
    for (k, v) in extra {
        req.headers_mut().insert(
            axum::http::HeaderName::from_bytes(k.as_bytes()).unwrap(),
            HeaderValue::from_str(v).unwrap(),
        );
    }
    req
}

/// `(agent_impl_name, agent_impl_source)` of the row with `correlation`.
async fn impl_of(
    pool: &PgPool,
    tenant: &str,
    correlation: &str,
) -> (Option<String>, Option<String>) {
    for _ in 0..120 {
        if let Some(r) = sqlx::query_as::<_, (Option<String>, Option<String>)>(
            "SELECT agent_impl_name, agent_impl_source FROM proxy_logs \
              WHERE tenant_id = $1 AND correlation_id = $2 LIMIT 1",
        )
        .bind(tenant)
        .bind(correlation)
        .fetch_optional(pool)
        .await
        .unwrap()
        {
            return r;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("no row {correlation}");
}

async fn keys(redis: &mut redis::aio::ConnectionManager, pattern: &str) -> Vec<String> {
    redis::cmd("KEYS")
        .arg(pattern)
        .query_async(redis)
        .await
        .unwrap()
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_session_bound_on_one_replica_names_its_calls_on_another(pool: PgPool) {
    let url = redis_url();
    let t = format!("t-sess-{}", uuid::Uuid::new_v4().simple());
    let session = format!("sess-{}", uuid::Uuid::new_v4().simple());
    let mock = upstream(&session).await;
    let key = topology(&pool, &t, &mock.uri(), 9901).await;

    let a = replica(&pool, connect(&url).await).await;
    let b = replica(&pool, connect(&url).await).await;
    assert!(a.mcp_sessions.is_shared() && b.mcp_sessions.is_shared());
    let app_a = common::build_mcp_proxy_app(a.clone());
    let app_b = common::build_mcp_proxy_app(b.clone());

    // initialize on replica A.
    let resp = app_a
        .clone()
        .oneshot(mcp(
            &key,
            &t,
            json!({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "session-agent", "version": "5.0.1"}}}),
            &[],
        ))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    // The key: namespaced, tenant + credential hash + session — no credential.
    let mut redis = connect(&url).await;
    let stored = keys(&mut redis, &format!("{SESSION_KEY_PREFIX}:{t}:*")).await;
    assert_eq!(stored.len(), 1, "{stored:?}");
    assert!(stored[0].ends_with(&format!(":{session}")), "{}", stored[0]);
    assert!(
        !stored[0].contains(&key),
        "the raw credential is never in the key"
    );

    // A later call of the session lands on replica B.
    //
    // The store read is bounded (500 ms) and a timeout reads as "absent" —
    // the designed fail-soft. On an overloaded host that made this test
    // flaky: B timed out, so it named nothing, which says nothing
    // about cross-replica resolution. An attempt during which B's own
    // unavailability counter moved is inconclusive and is repeated; an
    // attempt where the store answered is judged as strictly as ever.
    let mut b_timeouts = 0;
    let mut on_b = None;
    for attempt in 0..5 {
        let correlation = format!("on-b-{attempt}");
        let before = b.mcp_sessions.unavailable_count();
        let resp = app_b
            .clone()
            .oneshot(mcp(
                &key,
                &t,
                json!({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                &[
                    ("mcp-session-id", &session),
                    ("x-correlation-id", &correlation),
                ],
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        if b.mcp_sessions.unavailable_count() == before {
            on_b = Some(correlation);
            break;
        }
        b_timeouts += 1;
    }
    let on_b = on_b.expect("the session store timed out on every attempt: the host is overloaded");
    let (name, source) = impl_of(&pool, &t, &on_b).await;
    assert_eq!(
        name.as_deref(),
        Some("session-agent"),
        "replica B resolved A's binding"
    );
    assert_eq!(source.as_deref(), Some("mcp_client_info"));

    // DELETE on replica B ends the session everywhere.
    let mut del = mcp(&key, &t, json!({}), &[("mcp-session-id", &session)]);
    *del.method_mut() = axum::http::Method::DELETE;
    *del.body_mut() = Body::empty();
    let _ = app_b.oneshot(del).await.unwrap();
    assert!(
        keys(&mut redis, &format!("{SESSION_KEY_PREFIX}:{t}:*"))
            .await
            .is_empty()
    );

    let resp = app_a
        .oneshot(mcp(
            &key,
            &t,
            json!({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}),
            &[
                ("mcp-session-id", &session),
                ("x-correlation-id", "after-delete"),
            ],
        ))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let (name, source) = impl_of(&pool, &t, "after-delete").await;
    assert_eq!(
        (name, source),
        (None, None),
        "a deleted session names nothing"
    );
    assert_eq!(a.mcp_sessions.unavailable_count(), 0);
    assert_eq!(b.mcp_sessions.unavailable_count(), b_timeouts);
}

#[tokio::test]
async fn a_read_refreshes_the_idle_ttl_and_an_expired_session_is_absent() {
    let url = redis_url();
    let a = SessionStore::new(Some(connect(&url).await));
    let b = SessionStore::new(Some(connect(&url).await));
    let mut h = axum::http::HeaderMap::new();
    let tenant = format!("t-ttl-{}", uuid::Uuid::new_v4().simple());
    h.insert("x-tenant-id", HeaderValue::from_str(&tenant).unwrap());
    h.insert("x-api-key", HeaderValue::from_static("k-ttl"));
    let key = SessionScope::of(&h).key("s-ttl");
    let release = Release {
        name: Some("ttl-agent".into()),
        version: Some("1".into()),
        build: None,
    };
    a.bind(&key, &release).await;

    let mut redis = connect(&url).await;
    let ttl: i64 = redis::cmd("TTL")
        .arg(key.as_str())
        .query_async(&mut redis)
        .await
        .unwrap();
    let idle = SESSION_IDLE.as_secs() as i64;
    assert!(
        ttl > idle - 5 && ttl <= idle,
        "bound for 24 h idle, got {ttl}"
    );

    // Nearly idle out, then read on the other replica: the TTL is refreshed.
    let _: () = redis::cmd("EXPIRE")
        .arg(key.as_str())
        .arg(5)
        .query_async(&mut redis)
        .await
        .unwrap();
    assert_eq!(b.resolve(&key).await, Some(release.clone()));
    let ttl: i64 = redis::cmd("TTL")
        .arg(key.as_str())
        .query_async(&mut redis)
        .await
        .unwrap();
    assert!(ttl > idle - 5, "a read refreshes the idle TTL, got {ttl}");

    // Idle past the TTL: absent.
    let _: () = redis::cmd("PEXPIRE")
        .arg(key.as_str())
        .arg(1)
        .query_async(&mut redis)
        .await
        .unwrap();
    tokio::time::sleep(Duration::from_millis(30)).await;
    assert_eq!(b.resolve(&key).await, None);
    assert_eq!(a.unavailable_count() + b.unavailable_count(), 0);
}

/// A Redis that accepts connections and answers every command with an
/// error — the store is "configured but failing".
async fn failing_redis() -> String {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        loop {
            let Ok((mut sock, _)) = listener.accept().await else {
                return;
            };
            tokio::spawn(async move {
                let mut buf = Vec::new();
                let mut chunk = [0u8; 4096];
                loop {
                    let n = match sock.read(&mut chunk).await {
                        Ok(0) | Err(_) => return,
                        Ok(n) => n,
                    };
                    buf.extend_from_slice(&chunk[..n]);
                    while let Some(used) = resp_command_len(&buf) {
                        buf.drain(..used);
                        if sock.write_all(b"-ERR store down\r\n").await.is_err() {
                            return;
                        }
                    }
                }
            });
        }
    });
    format!("redis://{addr}")
}

/// Length of one complete RESP command (`*N` array of bulk strings) at the
/// start of `buf`, if it is all there.
fn resp_command_len(buf: &[u8]) -> Option<usize> {
    fn line(buf: &[u8], from: usize) -> Option<(&[u8], usize)> {
        let end = buf[from..].windows(2).position(|w| w == b"\r\n")? + from;
        Some((&buf[from..end], end + 2))
    }
    let (head, mut at) = line(buf, 0)?;
    let n: usize = std::str::from_utf8(head.strip_prefix(b"*")?)
        .ok()?
        .parse()
        .ok()?;
    for _ in 0..n {
        let (len, next) = line(buf, at)?;
        let len: usize = std::str::from_utf8(len.strip_prefix(b"$")?)
            .ok()?
            .parse()
            .ok()?;
        at = next + len + 2;
        if at > buf.len() {
            return None;
        }
    }
    Some(at)
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn with_the_store_down_the_call_succeeds_on_its_own_carriers_and_is_counted(pool: PgPool) {
    let t = format!("t-down-{}", uuid::Uuid::new_v4().simple());
    let session = format!("sess-{}", uuid::Uuid::new_v4().simple());
    let mock = upstream(&session).await;
    let key = topology(&pool, &t, &mock.uri(), 9902).await;
    let down = replica(&pool, connect(&failing_redis().await).await).await;
    assert!(down.mcp_sessions.is_shared());
    let app = common::build_mcp_proxy_app(down.clone());

    let resp = app
        .oneshot(mcp(
            &key,
            &t,
            json!({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            &[
                ("mcp-session-id", &session),
                ("x-correlation-id", "store-down"),
                ("x-brutor-agent-name", "header-agent"),
                ("x-brutor-agent-version", "2.0"),
            ],
        ))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK, "never fail the request");
    let (name, source) = impl_of(&pool, &t, "store-down").await;
    assert_eq!(
        name.as_deref(),
        Some("header-agent"),
        "per-request carriers only"
    );
    assert_eq!(
        source.as_deref(),
        Some("header"),
        "the session carrier is absent"
    );
    assert!(
        down.mcp_sessions.unavailable_count() >= 1,
        "the outage is counted"
    );
}
