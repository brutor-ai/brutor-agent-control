// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Agent implementation identity against a real Postgres and
//! wiremock upstreams.
//!
//! * every surface records the release, its source and the client label on
//!   its `proxy_logs` row (LLM chat, the streaming tail written after a
//!   spawn, embeddings, MCP — per-request `_meta`, `initialize` and the
//!   session's `initialize` — and A2A outbound);
//! * the caller's `baggage` never reaches an upstream;
//! * a verified token's claims outrank every declared carrier;
//! * the run carries its actor's release, trust, fingerprint and normalised
//!   client label; a segment carries its DELEGATE's release;
//! * `agent_implementation_releases` counts runs, idempotently across
//!   reopens, and moves the count when a run's release changes;
//! * the drift cause ladder names both releases of a transition, and the
//!   undeclared change (same release, new fingerprint).

use axum::body::Body;
use axum::http::{HeaderValue, Request, StatusCode};
use chrono::{DateTime, Duration, Utc};
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::middleware;
use brutor_gateway_core::routes;
use brutor_gateway_core::services::SharedState;
use brutor_gateway_core::services::agent_impl::{self, implementation_fingerprint};
use brutor_gateway_core::services::drift::cause;
use brutor_gateway_core::services::run_ledger::{finalize_run, finalized_by};

use super::common;

const UA: &str = "python-httpx/0.28.1";
const BAGGAGE: &str = "gen_ai.agent.name=bag-agent,gen_ai.agent.version=2.0.0,other=x";

/// `(name, version, build, source, client_label, user_agent)` of a row.
type ImplRow = (
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
);

/// The newest row of a tenant matching `filter` (a fixed SQL predicate),
/// polled because the background writer is batched.
async fn impl_row(pool: &PgPool, tenant: &str, filter: &str) -> ImplRow {
    let sql = format!(
        "SELECT agent_impl_name, agent_impl_version, agent_impl_build, agent_impl_source, \
                client_label, user_agent \
           FROM proxy_logs WHERE tenant_id = $1 AND {filter} \
          ORDER BY timestamp DESC LIMIT 1"
    );
    for _ in 0..80 {
        let r: Option<ImplRow> = sqlx::query_as(&sql)
            .bind(tenant)
            .fetch_optional(pool)
            .await
            .expect("proxy_logs query");
        if let Some(r) = r {
            return r;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    panic!("no proxy_logs row for {tenant} matching {filter}");
}

fn with_headers(mut req: Request<Body>, headers: &[(&str, &str)]) -> Request<Body> {
    for (k, v) in headers {
        req.headers_mut().append(
            axum::http::HeaderName::from_bytes(k.as_bytes()).expect("header name"),
            HeaderValue::from_str(v).expect("header value"),
        );
    }
    req
}

async fn assert_no_baggage_upstream(mock: &MockServer) {
    let received = mock.received_requests().await.expect("recording on");
    assert!(!received.is_empty(), "the upstream must have been called");
    for r in received {
        assert!(
            !r.headers.contains_key("baggage"),
            "caller baggage was forwarded upstream to {}",
            r.url
        );
    }
}

fn chat_fixture() -> Value {
    json!({
        "id": "chatcmpl-agent-impl", "object": "chat.completion", "created": 1_700_000_000,
        "model": "test-gpt",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}
    })
}

async fn llm_topology(pool: &PgPool, tenant: &str, upstream: &str, user: i32) -> String {
    common::seed_tenant(pool, tenant).await;
    common::seed_resource_group(pool, "rg-impl", tenant).await;
    common::seed_end_user(pool, user, tenant).await;
    common::add_direct_membership(pool, user, "rg-impl", tenant).await;
    common::seed_llm_provider(pool, "prov-impl", tenant, "openai").await;
    common::seed_llm_model_with_api_url(pool, "m-impl", tenant, "prov-impl", "test-gpt", upstream)
        .await;
    common::bind_llm_model_to_rg(pool, "m-impl", "rg-impl").await;
    common::seed_api_key(pool, tenant, "rg-impl", Some(user), "impl-key").await
}

// ---------------------------------------------------------------------------
// Surfaces
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn llm_chat_records_the_release_and_never_forwards_baggage(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(chat_fixture()))
        .mount(&mock)
        .await;
    let key = llm_topology(&pool, "t-impl-llm", &mock.uri(), 9701).await;
    let app = common::build_llm_app(common::build_test_state(pool.clone()));
    let req = with_headers(
        common::build_post_request(
            "/v1/proxy/llm/chat/completions",
            json!({"model_id": "m-impl", "messages": [{"role": "user", "content": "hi"}]}),
            Some(&key),
            Some("t-impl-llm"),
        ),
        &[
            ("baggage", BAGGAGE),
            ("x-brutor-agent-name", "header-agent"),
            ("x-brutor-agent-version", "1.0.0"),
            ("user-agent", UA),
        ],
    );
    let resp = app.oneshot(req).await.expect("router");
    assert_eq!(resp.status(), StatusCode::OK);

    let (name, version, build, source, label, ua) =
        impl_row(&pool, "t-impl-llm", "proxy_type = 'llm'").await;
    // Baggage outranks the Brutor headers (the third carrier over the fourth).
    assert_eq!(name.as_deref(), Some("bag-agent"));
    assert_eq!(version.as_deref(), Some("2.0.0"));
    assert_eq!(build, None);
    assert_eq!(source.as_deref(), Some("baggage"));
    // The product name, not the library version.
    assert_eq!(label.as_deref(), Some("python-httpx"));
    assert_eq!(ua.as_deref(), Some(UA), "the raw User-Agent is kept");
    assert_no_baggage_upstream(&mock).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_streaming_tail_row_written_after_a_spawn_carries_the_release(pool: PgPool) {
    let mock = MockServer::start().await;
    let chunk = json!({"id": "c", "object": "chat.completion.chunk", "model": "test-gpt",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hi"},
                     "finish_reason": "stop"}]});
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(
            ResponseTemplate::new(200)
                .insert_header("content-type", "text/event-stream")
                .set_body_string(format!("data: {chunk}\n\ndata: [DONE]\n\n")),
        )
        .mount(&mock)
        .await;
    let key = llm_topology(&pool, "t-impl-stream", &mock.uri(), 9702).await;
    let app = common::build_llm_app(common::build_test_state(pool.clone()));
    let req = with_headers(
        common::build_post_request(
            "/v1/proxy/llm/chat/completions",
            json!({"model_id": "m-impl", "stream": true,
                   "messages": [{"role": "user", "content": "q"}]}),
            Some(&key),
            Some("t-impl-stream"),
        ),
        &[
            ("x-brutor-agent-name", "Stream-Agent"),
            ("x-brutor-agent-version", "3.1.4"),
            ("x-brutor-agent-build", "sha256:0badc0de"),
            ("x-brutor-client", "demo-app"),
            (
                "baggage",
                BAGGAGE.replace("gen_ai.agent.name=bag-agent,", "").as_str(),
            ),
        ],
    );
    let resp = app.oneshot(req).await.expect("router");
    assert_eq!(resp.status(), StatusCode::OK);
    let _ = axum::body::to_bytes(resp.into_body(), usize::MAX).await;

    let (name, version, build, source, label, _) = impl_row(
        &pool,
        "t-impl-stream",
        "proxy_type = 'llm' AND llm_streaming IS NOT DISTINCT FROM true",
    )
    .await;
    // Baggage with a version only is still present, so it wins over the
    // headers — and its tuple is taken whole (no header name mixed in).
    assert_eq!(source.as_deref(), Some("baggage"));
    assert_eq!(name, None);
    assert_eq!(version.as_deref(), Some("2.0.0"));
    assert_eq!(build, None);
    assert_eq!(label.as_deref(), Some("demo-app"), "X-Brutor-Client wins");
    assert_no_baggage_upstream(&mock).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_embeddings_row_records_the_release(pool: PgPool) {
    let t = "t-impl-emb";
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/embeddings"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "object": "list",
            "data": [{"object": "embedding", "embedding": [0.1, 0.2], "index": 0}],
            "model": "text-embedding-3-small",
            "usage": {"prompt_tokens": 1, "total_tokens": 1}
        })))
        .mount(&mock)
        .await;
    common::seed_tenant(&pool, t).await;
    common::seed_resource_group(&pool, "rg-emb", t).await;
    common::seed_end_user(&pool, 9703, t).await;
    common::add_direct_membership(&pool, 9703, "rg-emb", t).await;
    common::seed_llm_provider(&pool, "prov-emb", t, "openai").await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-emb",
        t,
        "prov-emb",
        "text-embedding-3-small",
        &mock.uri(),
    )
    .await;
    common::set_model_mode(&pool, "m-emb", "embedding").await;
    common::bind_llm_model_to_rg(&pool, "m-emb", "rg-emb").await;
    let jwt = common::seed_api_key(&pool, t, "rg-emb", Some(9703), "caller").await;
    let app = common::build_llm_app(common::build_test_state(pool.clone()));
    let req = with_headers(
        common::build_post_request(
            "/v1/proxy/llm/embeddings",
            json!({"model": "m-emb", "input": "hello"}),
            Some(&jwt),
            Some(t),
        ),
        &[
            ("x-brutor-agent-name", "embedder"),
            ("x-brutor-agent-version", "0.9"),
            ("baggage", "unrelated=1"),
            ("user-agent", "Mozilla/5.0 (X11; Linux x86_64)"),
        ],
    );
    let resp = app.oneshot(req).await.expect("router");
    assert_eq!(resp.status(), StatusCode::OK);
    let (name, version, _, source, label, _) = impl_row(&pool, t, "proxy_type = 'llm'").await;
    assert_eq!(name.as_deref(), Some("embedder"));
    assert_eq!(version.as_deref(), Some("0.9"));
    assert_eq!(
        source.as_deref(),
        Some("header"),
        "baggage without gen_ai members is absent"
    );
    assert_eq!(label.as_deref(), Some("browser"));
    assert_no_baggage_upstream(&mock).await;
}

async fn mcp_topology(pool: &PgPool, tenant: &str, upstream: &str, user: i32) -> String {
    common::seed_tenant(pool, tenant).await;
    common::seed_resource_group(pool, "rg-mcp-impl", tenant).await;
    common::seed_end_user(pool, user, tenant).await;
    common::add_direct_membership(pool, user, "rg-mcp-impl", tenant).await;
    common::seed_mcp_server_with_base_url(pool, "ms-impl", tenant, "impl", upstream).await;
    common::seed_server_config(pool, "sc-impl", tenant, "ms-impl").await;
    common::bind_server_config_to_rg(pool, "sc-impl", "rg-mcp-impl").await;
    common::seed_api_key(pool, tenant, "rg-mcp-impl", Some(user), "mcp-impl-key").await
}

async fn mcp_upstream(session_id: Option<&str>) -> MockServer {
    let mock = MockServer::start().await;
    let mut tmpl = ResponseTemplate::new(200).set_body_json(json!({
        "jsonrpc": "2.0", "id": 1, "result": {"tools": []}
    }));
    if let Some(s) = session_id {
        tmpl = tmpl.insert_header("mcp-session-id", s);
    }
    Mock::given(method("POST"))
        .respond_with(tmpl)
        .mount(&mock)
        .await;
    mock
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn mcp_meta_client_info_outranks_baggage_and_baggage_stays_home(pool: PgPool) {
    let t = "t-impl-mcp";
    let mock = mcp_upstream(None).await;
    let key = mcp_topology(&pool, t, &mock.uri(), 9704).await;
    let app = common::build_mcp_proxy_app(common::build_test_state(pool.clone()));
    let req = with_headers(
        common::build_post_request(
            "/v1/proxy/mcp/ms-impl",
            json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {
                "_meta": {"io.modelcontextprotocol/clientInfo":
                          {"name": "Screening-Agent", "version": "1.4.2"}}}}),
            Some(&key),
            Some(t),
        ),
        &[("baggage", BAGGAGE), ("user-agent", UA)],
    );
    let resp = app.oneshot(req).await.expect("router");
    assert_eq!(resp.status(), StatusCode::OK);
    let (name, version, _, source, label, _) = impl_row(&pool, t, "proxy_type = 'mcp'").await;
    assert_eq!(
        name.as_deref(),
        Some("screening-agent"),
        "normalised to lower case"
    );
    assert_eq!(version.as_deref(), Some("1.4.2"));
    assert_eq!(source.as_deref(), Some("mcp_client_info"));
    assert_eq!(label.as_deref(), Some("python-httpx"));
    assert_no_baggage_upstream(&mock).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_mcp_row_takes_its_build_from_headers_naming_the_same_release(pool: PgPool) {
    // clientInfo cannot carry a build; the agent's headers name the same
    // release with one. Before the fix the MCP row recorded build NULL while
    // the same agent's HTTP rows recorded the build: one release, two rows.
    let t = "t-impl-mcp-build";
    let mock = mcp_upstream(None).await;
    let key = mcp_topology(&pool, t, &mock.uri(), 9706).await;
    let app = common::build_mcp_proxy_app(common::build_test_state(pool.clone()));
    let call = |version: &'static str, correlation: &'static str| {
        with_headers(
            common::build_post_request(
                "/v1/proxy/mcp/ms-impl",
                json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {
                    "_meta": {"io.modelcontextprotocol/clientInfo":
                              {"name": "screening-agent", "version": "1.4.2"}}}}),
                Some(&key),
                Some(t),
            ),
            &[
                ("x-brutor-agent-name", "screening-agent"),
                ("x-brutor-agent-version", version),
                ("x-brutor-agent-build", "defbe1a-dirty"),
                ("x-correlation-id", correlation),
            ],
        )
    };
    let resp = app
        .clone()
        .oneshot(call("1.4.2", "same-release"))
        .await
        .expect("router");
    assert_eq!(resp.status(), StatusCode::OK);
    let (name, version, build, source, _, _) = impl_row(
        &pool,
        t,
        "proxy_type = 'mcp' AND correlation_id = 'same-release'",
    )
    .await;
    assert_eq!(name.as_deref(), Some("screening-agent"));
    assert_eq!(version.as_deref(), Some("1.4.2"));
    assert_eq!(source.as_deref(), Some("mcp_client_info"));
    assert_eq!(build.as_deref(), Some("defbe1a-dirty"));

    // Headers naming ANOTHER version never lend their build.
    let resp = app
        .oneshot(call("1.5.0", "other-release"))
        .await
        .expect("router");
    assert_eq!(resp.status(), StatusCode::OK);
    let (_, version, build, _, _, _) = impl_row(
        &pool,
        t,
        "proxy_type = 'mcp' AND correlation_id = 'other-release'",
    )
    .await;
    assert_eq!(version.as_deref(), Some("1.4.2"));
    assert_eq!(build, None);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_sessions_initialize_client_info_names_its_later_calls(pool: PgPool) {
    let t = "t-impl-mcp-sess";
    let session = format!("sess-{}", uuid::Uuid::new_v4().simple());
    let mock = mcp_upstream(Some(&session)).await;
    let key = mcp_topology(&pool, t, &mock.uri(), 9705).await;
    let app = common::build_mcp_proxy_app(common::build_test_state(pool.clone()));

    let init = common::build_post_request(
        "/v1/proxy/mcp/ms-impl",
        json!({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "session-agent", "version": "5.0.1"}}}),
        Some(&key),
        Some(t),
    );
    let resp = app.clone().oneshot(init).await.expect("router");
    assert_eq!(resp.status(), StatusCode::OK);
    assert_eq!(
        resp.headers()
            .get("mcp-session-id")
            .and_then(|v| v.to_str().ok()),
        Some(session.as_str())
    );

    let later = with_headers(
        common::build_post_request(
            "/v1/proxy/mcp/ms-impl",
            json!({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            Some(&key),
            Some(t),
        ),
        &[
            ("mcp-session-id", &session),
            ("x-correlation-id", "later-call"),
        ],
    );
    let resp = app.oneshot(later).await.expect("router");
    assert_eq!(resp.status(), StatusCode::OK);
    let (name, version, _, source, _, _) = impl_row(
        &pool,
        t,
        "proxy_type = 'mcp' AND correlation_id = 'later-call'",
    )
    .await;
    assert_eq!(name.as_deref(), Some("session-agent"));
    assert_eq!(version.as_deref(), Some("5.0.1"));
    assert_eq!(source.as_deref(), Some("mcp_client_info"));
}

fn a2a_app(state: &SharedState) -> axum::Router {
    axum::Router::new()
        .nest("/v1/proxy/a2a", routes::a2a::router())
        .layer(middleware::tenant::TenantLayer::new(state.clone()))
        .layer(middleware::run_end::RunEndLayer::new(
            state.mcp_sessions.clone(),
        ))
        .with_state(state.clone())
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a2a_outbound_records_the_release_and_never_forwards_baggage(pool: PgPool) {
    let t = "t-impl-a2a";
    let upstream = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "jsonrpc": "2.0", "id": 1,
            "result": {"kind": "message", "role": "agent", "parts": [{"kind": "text", "text": "ok"}]}
        })))
        .mount(&upstream)
        .await;
    common::seed_tenant(&pool, t).await;
    common::seed_resource_group(&pool, "rg-a2a-impl", t).await;
    common::seed_agent_card(&pool, "card-impl", t, "impl-agent").await;
    sqlx::query("UPDATE agent_cards SET card_json = $2 WHERE id = $1")
        .bind("card-impl")
        .bind(json!({"name": "impl-agent", "url": upstream.uri(),
                     "capabilities": [{"name": "echo"}]}))
        .execute(&pool)
        .await
        .expect("card url");
    sqlx::query(
        "INSERT INTO agent_card_capabilities (id, agent_card_id, capability_name, handler_type, enabled) \
         VALUES ('cap-impl', 'card-impl', 'echo', 'noop', true)",
    )
    .execute(&pool)
    .await
    .expect("capability");
    common::bind_agent_card_to_rg(&pool, "card-impl", "rg-a2a-impl").await;
    let key = common::seed_api_key(&pool, t, "rg-a2a-impl", None, "a2a-impl-key").await;

    let state = common::build_test_state(pool.clone());
    let req = with_headers(
        common::build_post_request(
            "/v1/proxy/a2a/outbound",
            json!({"target_card_id": "card-impl", "capability": "echo",
                   "message": {"text": "hi"}}),
            Some(&key),
            Some(t),
        ),
        &[
            ("baggage", BAGGAGE),
            ("x-brutor-agent-name", "caller-agent"),
            ("x-brutor-agent-version", "7.0.0"),
        ],
    );
    let resp = a2a_app(&state).oneshot(req).await.expect("router");
    assert_eq!(resp.status(), StatusCode::OK);
    let (name, version, _, source, _, _) = impl_row(
        &pool,
        t,
        "proxy_type = 'a2a' AND proxy_subtype = 'outbound'",
    )
    .await;
    assert_eq!(name.as_deref(), Some("bag-agent"));
    assert_eq!(version.as_deref(), Some("2.0.0"));
    assert_eq!(source.as_deref(), Some("baggage"));
    assert_no_baggage_upstream(&upstream).await;
}

// ---------------------------------------------------------------------------
// Verified token claims (the first carrier)
// ---------------------------------------------------------------------------

#[derive(serde::Serialize)]
struct SoftwareClaims {
    iss: String,
    sub: String,
    exp: usize,
    iat: usize,
    aud: String,
    software_id: String,
    software_version: String,
    software_build: String,
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_verified_tokens_claims_outrank_every_declared_carrier(pool: PgPool) {
    use super::workload_identity::{TEST_RSA_PEM, mock_jwks, seed_agent_with_idp, seed_idp};
    let t = "t-impl-wl";
    common::seed_tenant(&pool, t).await;
    let jwks = mock_jwks("kid-impl").await;
    let issuer = "https://idp.impl.example/";
    seed_idp(
        &pool,
        "idp-impl",
        t,
        issuer,
        &format!("{}/jwks", jwks.uri()),
    )
    .await;
    seed_agent_with_idp(&pool, "agent-impl-wl", t, "idp-impl", "spiffe://acme/impl").await;
    common::seed_resource_group(&pool, "rg-impl-wl", t).await;
    sqlx::query(
        "INSERT INTO resource_group_agent_members (tenant_id, agent_id, group_id, joined_at) \
         VALUES ($1, 'agent-impl-wl', 'rg-impl-wl', now())",
    )
    .bind(t)
    .execute(&pool)
    .await
    .expect("membership");

    let mut header = jsonwebtoken::Header::new(jsonwebtoken::Algorithm::RS256);
    header.kid = Some("kid-impl".into());
    let token = jsonwebtoken::encode(
        &header,
        &SoftwareClaims {
            iss: issuer.into(),
            sub: "spiffe://acme/impl".into(),
            exp: (crate::common::db_now().timestamp() + 3600) as usize,
            iat: crate::common::db_now().timestamp() as usize,
            aud: super::workload_identity::WL_AUDIENCE.into(),
            software_id: "Verified-Agent".into(),
            software_version: "1.4.2".into(),
            software_build: "sha256:feedface".into(),
        },
        &jsonwebtoken::EncodingKey::from_rsa_pem(TEST_RSA_PEM.as_bytes()).unwrap(),
    )
    .unwrap();

    let mut headers = axum::http::HeaderMap::new();
    headers.insert("authorization", format!("Bearer {token}").parse().unwrap());
    headers.insert("baggage", BAGGAGE.parse().unwrap());
    headers.insert("x-brutor-agent-name", "spoofed".parse().unwrap());
    headers.insert("x-brutor-agent-version", "9.9.9".parse().unwrap());
    let carriers = agent_impl::Carriers::from_headers(&headers, &mut agent_impl::Drops::default());

    let auth = agent_impl::scope(carriers, async {
        brutor_gateway_core::services::auth::authenticate_request(
            &headers,
            t,
            "unused-gateway-jwt-secret-0123456789abcdef",
            &pool,
            &brutor_gateway_core::services::credential_verifier::CredentialVerifiers::community(
                brutor_gateway_core::services::credential_verifier::RefuseUnregistered,
            ),
            None,
            true,
        )
        .await
        .expect("the workload token authenticates")
    })
    .await;
    assert_eq!(auth.actor_agent_id.as_deref(), Some("agent-impl-wl"));
    let r = auth.agent_impl.resolve().expect("resolved");
    assert_eq!(r.source, "token");
    assert_eq!(r.trust(), "verified");
    assert_eq!(r.name.as_deref(), Some("verified-agent"));
    assert_eq!(r.version.as_deref(), Some("1.4.2"));
    assert_eq!(r.build.as_deref(), Some("sha256:feedface"));

    // …and that is what the audit row records.
    let log = brutor_gateway_core::services::proxy_logger::build_proxy_log(
        "llm", &auth, "POST", "/x", None, None, 200, 1.0, true, None, "c-wl",
    );
    assert_eq!(log.agent_impl_source.as_deref(), Some("token"));
    assert_eq!(log.agent_impl_name.as_deref(), Some("verified-agent"));
}

// ---------------------------------------------------------------------------
// Run ledger: run fields, segments, releases
// ---------------------------------------------------------------------------

struct Step<'a> {
    root: &'a str,
    at: DateTime<Utc>,
    group: &'a str,
    actor: Option<&'a str>,
    name: Option<&'a str>,
    version: Option<&'a str>,
    build: Option<&'a str>,
    source: Option<&'a str>,
    label: Option<&'a str>,
    ua: Option<&'a str>,
    prompt_fp: Option<&'a str>,
}

impl<'a> Step<'a> {
    fn new(root: &'a str, at: DateTime<Utc>, group: &'a str, actor: Option<&'a str>) -> Self {
        Step {
            root,
            at,
            group,
            actor,
            name: None,
            version: None,
            build: None,
            source: None,
            label: None,
            ua: None,
            prompt_fp: None,
        }
    }

    fn release(mut self, name: &'a str, version: &'a str, source: &'a str) -> Self {
        self.name = Some(name);
        self.version = Some(version);
        self.source = Some(source);
        self
    }
}

async fn insert_step(pool: &PgPool, tenant: &str, s: Step<'_>) {
    sqlx::query(
        "INSERT INTO proxy_logs (
            id, tenant_id, timestamp, proxy_type, end_user_session_id, http_method, endpoint,
            success, root_task_id, delegation_depth, resource_group_id, actor_agent_id,
            guardrail_blocked, argument_policy_denied, pii_detected, is_auth_event,
            trace_continuity, agent_impl_name, agent_impl_version, agent_impl_build,
            agent_impl_source, client_label, user_agent, prompt_fingerprint
         ) VALUES (
            $1, $2, $3, 'llm', 'sess', 'POST', '/x', true, $4, 0, $5, $6,
            false, false, false, false, 'minted', $7, $8, $9, $10, $11, $12, $13
         )",
    )
    .bind(format!("pl-{}", ulid::Ulid::new()))
    .bind(tenant)
    .bind(s.at)
    .bind(s.root)
    .bind(s.group)
    .bind(s.actor)
    .bind(s.name)
    .bind(s.version)
    .bind(s.build)
    .bind(s.source)
    .bind(s.label)
    .bind(s.ua)
    .bind(s.prompt_fp)
    .execute(pool)
    .await
    .expect("insert step");
}

async fn seed_system(pool: &PgPool, tenant: &str, id: &str) {
    sqlx::query(
        "INSERT INTO resource_groups (id, tenant_id, hierarchy_level, inherit_from_parent, \
         name, is_active, is_system, group_type, system_kind) \
         VALUES ($1, $2, 0, true, $1, true, false, 'AI_SYSTEM', 'agent') ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant)
    .execute(pool)
    .await
    .expect("seed system");
}

async fn seed_agent(pool: &PgPool, tenant: &str, id: &str) {
    sqlx::query(
        "INSERT INTO agent_identities (id, tenant_id, name, source, enabled, status, default_deny) \
         VALUES ($1, $2, $1, 'manual', true, 'active', false) ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant)
    .execute(pool)
    .await
    .expect("seed agent");
}

type RunImpl = (
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
);

async fn run_impl(pool: &PgPool, tenant: &str, root: &str) -> RunImpl {
    sqlx::query_as(
        "SELECT agent_impl_name, agent_impl_version, agent_impl_build, agent_impl_trust, \
                implementation_fingerprint, client_label \
           FROM ai_system_runs WHERE tenant_id = $1 AND root_task_id = $2",
    )
    .bind(tenant)
    .bind(root)
    .fetch_one(pool)
    .await
    .expect("run row")
}

/// `(run_count, trust, fingerprints)` of one release row, if any.
async fn release_row(
    pool: &PgPool,
    tenant: &str,
    agent: &str,
    version: &str,
) -> Option<(i32, String, Value)> {
    sqlx::query_as(
        "SELECT run_count, trust, fingerprints FROM agent_implementation_releases \
          WHERE tenant_id = $1 AND agent_id = $2 AND version = $3",
    )
    .bind(tenant)
    .bind(agent)
    .bind(version)
    .fetch_optional(pool)
    .await
    .expect("release query")
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_run_carries_its_actors_release_fingerprint_and_label(pool: PgPool) {
    let t = "t-impl-run";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys-run").await;
    seed_agent(&pool, t, "agent-run").await;
    let t0 = crate::common::db_now() - Duration::minutes(10);
    // First step carries no release; the second is the first carrying one.
    let mut s = Step::new("root-run", t0, "sys-run", Some("agent-run"));
    s.ua = Some(UA);
    s.label = Some("python-httpx");
    insert_step(&pool, t, s).await;
    let mut s = Step::new(
        "root-run",
        t0 + Duration::seconds(1),
        "sys-run",
        Some("agent-run"),
    )
    .release("screening-agent", "1.4.2", "header");
    s.build = Some("sha256:ab");
    s.prompt_fp = Some("fp-instructions");
    insert_step(&pool, t, s).await;
    // A later, different declaration does not move the run's release.
    insert_step(
        &pool,
        t,
        Step::new(
            "root-run",
            t0 + Duration::seconds(2),
            "sys-run",
            Some("agent-run"),
        )
        .release("screening-agent", "9.9.9", "header"),
    )
    .await;

    finalize_run(&pool, t, "root-run", finalized_by::EXPLICIT, None, None)
        .await
        .expect("finalize")
        .expect("run");
    let (name, version, build, trust, fp, label) = run_impl(&pool, t, "root-run").await;
    assert_eq!(name.as_deref(), Some("screening-agent"));
    assert_eq!(version.as_deref(), Some("1.4.2"));
    assert_eq!(build.as_deref(), Some("sha256:ab"));
    assert_eq!(trust.as_deref(), Some("declared"));
    assert_eq!(
        label.as_deref(),
        Some("python-httpx"),
        "the normalised label, not the raw UA"
    );
    // No row's release came from clientInfo, so the MCP inputs are absent.
    assert_eq!(
        fp,
        implementation_fingerprint(Some(UA), None, None, Some("fp-instructions")),
        "the fingerprint is over the first UA, clientInfo and instruction fingerprint"
    );
    let (count, rel_trust, fps) = release_row(&pool, t, "agent-run", "1.4.2")
        .await
        .expect("release row");
    assert_eq!(count, 1);
    assert_eq!(rel_trust, "declared");
    assert_eq!(fps, json!([fp.unwrap()]));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn release_counts_are_idempotent_on_reopen_and_move_on_change(pool: PgPool) {
    let t = "t-impl-rel";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys-rel").await;
    seed_agent(&pool, t, "agent-rel").await;
    let t0 = crate::common::db_now() - Duration::minutes(30);
    insert_step(
        &pool,
        t,
        Step::new("root-rel", t0, "sys-rel", Some("agent-rel")).release(
            "rel-agent",
            "1.0.0",
            "baggage",
        ),
    )
    .await;
    for _ in 0..3 {
        finalize_run(&pool, t, "root-rel", finalized_by::IDLE, None, None)
            .await
            .unwrap();
    }
    // A late step reopens the run: still one run.
    insert_step(
        &pool,
        t,
        Step::new(
            "root-rel",
            t0 + Duration::minutes(5),
            "sys-rel",
            Some("agent-rel"),
        )
        .release("rel-agent", "1.0.0", "baggage"),
    )
    .await;
    finalize_run(&pool, t, "root-rel", finalized_by::IDLE, None, None)
        .await
        .unwrap();
    assert_eq!(
        release_row(&pool, t, "agent-rel", "1.0.0").await.unwrap().0,
        1
    );

    // An earlier step with a different release arrives late: the run's
    // release moves, and so does its single count.
    insert_step(
        &pool,
        t,
        Step::new(
            "root-rel",
            t0 - Duration::minutes(1),
            "sys-rel",
            Some("agent-rel"),
        )
        .release("rel-agent", "0.9.0", "token"),
    )
    .await;
    finalize_run(&pool, t, "root-rel", finalized_by::IDLE, None, None)
        .await
        .unwrap();
    assert_eq!(
        release_row(&pool, t, "agent-rel", "1.0.0").await.unwrap().0,
        0
    );
    let (count, trust, _) = release_row(&pool, t, "agent-rel", "0.9.0").await.unwrap();
    assert_eq!(count, 1);
    assert_eq!(trust, "verified");

    // A second run of the same release counts once more.
    insert_step(
        &pool,
        t,
        Step::new("root-rel-2", t0, "sys-rel", Some("agent-rel")).release(
            "rel-agent",
            "0.9.0",
            "header",
        ),
    )
    .await;
    finalize_run(&pool, t, "root-rel-2", finalized_by::IDLE, None, None)
        .await
        .unwrap();
    let (count, trust, _) = release_row(&pool, t, "agent-rel", "0.9.0").await.unwrap();
    assert_eq!(count, 2);
    assert_eq!(trust, "verified", "trust is the strongest ever seen");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_segment_carries_the_delegates_release(pool: PgPool) {
    let t = "t-impl-seg";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys-caller").await;
    seed_system(&pool, t, "sys-service").await;
    seed_agent(&pool, t, "agent-caller").await;
    seed_agent(&pool, t, "agent-svc").await;
    let t0 = crate::common::db_now() - Duration::minutes(10);
    insert_step(
        &pool,
        t,
        Step::new("root-seg", t0, "sys-caller", Some("agent-caller")).release(
            "caller-agent",
            "1.0.0",
            "header",
        ),
    )
    .await;
    // The caller's own declaration lands in the service's group first…
    insert_step(
        &pool,
        t,
        Step::new(
            "root-seg",
            t0 + Duration::seconds(1),
            "sys-service",
            Some("agent-caller"),
        )
        .release("caller-agent", "1.0.0", "header"),
    )
    .await;
    // …then the delegate's own calls.
    insert_step(
        &pool,
        t,
        Step::new(
            "root-seg",
            t0 + Duration::seconds(2),
            "sys-service",
            Some("agent-svc"),
        )
        .release("fraud-screener", "2.3.0", "mcp_client_info"),
    )
    .await;
    finalize_run(&pool, t, "root-seg", finalized_by::EXPLICIT, None, None)
        .await
        .unwrap()
        .unwrap();

    let run = run_impl(&pool, t, "root-seg").await;
    assert_eq!(
        run.0.as_deref(),
        Some("caller-agent"),
        "the run is the caller's"
    );
    let seg: (
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
    ) = sqlx::query_as(
        "SELECT agent_impl_name, agent_impl_version, agent_impl_build, agent_impl_trust \
           FROM ai_system_run_segments WHERE tenant_id = $1 AND segment_system_id = 'sys-service'",
    )
    .bind(t)
    .fetch_one(&pool)
    .await
    .expect("segment");
    assert_eq!(seg.0.as_deref(), Some("fraud-screener"));
    assert_eq!(seg.1.as_deref(), Some("2.3.0"));
    assert_eq!(seg.2, None);
    assert_eq!(seg.3.as_deref(), Some("declared"));
}

// ---------------------------------------------------------------------------
// Drift: the agent_release rung
// ---------------------------------------------------------------------------

/// One single-step run per entry, finalised through the ledger.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_delegation_run_counts_toward_both_agents_releases_idempotently(pool: PgPool) {
    // A shared service's delegate (the fraud screener) only ever acts inside
    // other systems' runs. It used to get no release row at all.
    let t = "t-impl-deleg";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys-app").await;
    seed_system(&pool, t, "sys-fraud").await;
    seed_agent(&pool, t, "agent-app").await;
    seed_agent(&pool, t, "agent-fraud").await;
    let t0 = crate::common::db_now() - Duration::minutes(20);
    let step = |root: &'static str, secs: i64, group: &'static str, actor: &'static str| {
        Step::new(root, t0 + Duration::seconds(secs), group, Some(actor))
    };
    insert_step(
        &pool,
        t,
        step("root-deleg", 0, "sys-app", "agent-app").release("loan-screener", "1.0.0", "header"),
    )
    .await;
    insert_step(
        &pool,
        t,
        step("root-deleg", 1, "sys-fraud", "agent-fraud").release(
            "fraud-screener",
            "2.3.0",
            "header",
        ),
    )
    .await;
    finalize_run(&pool, t, "root-deleg", finalized_by::EXPLICIT, None, None)
        .await
        .unwrap()
        .unwrap();

    let delegate: (Option<String>,) = sqlx::query_as(
        "SELECT delegate_agent_id FROM ai_system_run_segments \
          WHERE tenant_id = $1 AND segment_system_id = 'sys-fraud'",
    )
    .bind(t)
    .fetch_one(&pool)
    .await
    .expect("segment");
    assert_eq!(delegate.0.as_deref(), Some("agent-fraud"));
    assert_eq!(
        release_row(&pool, t, "agent-app", "1.0.0").await.unwrap().0,
        1
    );
    let (count, trust, _) = release_row(&pool, t, "agent-fraud", "2.3.0")
        .await
        .expect("the delegate has a release row");
    assert_eq!(count, 1);
    assert_eq!(trust, "declared");

    // Reopened by late steps (twice): still one run each.
    insert_step(
        &pool,
        t,
        step("root-deleg", 5, "sys-fraud", "agent-fraud").release(
            "fraud-screener",
            "2.3.0",
            "header",
        ),
    )
    .await;
    for _ in 0..2 {
        finalize_run(&pool, t, "root-deleg", finalized_by::IDLE, None, None)
            .await
            .unwrap();
    }
    assert_eq!(
        release_row(&pool, t, "agent-app", "1.0.0").await.unwrap().0,
        1
    );
    assert_eq!(
        release_row(&pool, t, "agent-fraud", "2.3.0")
            .await
            .unwrap()
            .0,
        1
    );

    // An earlier delegate step with another release arrives late: the
    // delegate's count moves, the actor's does not.
    insert_step(
        &pool,
        t,
        Step::new(
            "root-deleg",
            t0 + Duration::milliseconds(500),
            "sys-fraud",
            Some("agent-fraud"),
        )
        .release("fraud-screener", "2.2.0", "header"),
    )
    .await;
    finalize_run(&pool, t, "root-deleg", finalized_by::IDLE, None, None)
        .await
        .unwrap();
    assert_eq!(
        release_row(&pool, t, "agent-fraud", "2.3.0")
            .await
            .unwrap()
            .0,
        0
    );
    assert_eq!(
        release_row(&pool, t, "agent-fraud", "2.2.0")
            .await
            .unwrap()
            .0,
        1
    );
    assert_eq!(
        release_row(&pool, t, "agent-app", "1.0.0").await.unwrap().0,
        1
    );

    // A second delegation run counts once more for both.
    insert_step(
        &pool,
        t,
        step("root-deleg-2", 0, "sys-app", "agent-app").release("loan-screener", "1.0.0", "header"),
    )
    .await;
    insert_step(
        &pool,
        t,
        step("root-deleg-2", 1, "sys-fraud", "agent-fraud").release(
            "fraud-screener",
            "2.2.0",
            "header",
        ),
    )
    .await;
    finalize_run(&pool, t, "root-deleg-2", finalized_by::EXPLICIT, None, None)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        release_row(&pool, t, "agent-app", "1.0.0").await.unwrap().0,
        2
    );
    assert_eq!(
        release_row(&pool, t, "agent-fraud", "2.2.0")
            .await
            .unwrap()
            .0,
        2
    );
}

async fn runs(pool: &PgPool, t: &str, sys: &str, agent: &str, entries: &[(i64, &str, &str)]) {
    for (i, (minutes_ago, version, ua)) in entries.iter().enumerate() {
        let root = format!("root-{sys}-{i}");
        let mut s = Step::new(
            &root,
            crate::common::db_now() - Duration::minutes(*minutes_ago),
            sys,
            Some(agent),
        )
        .release("screening-agent", version, "header");
        s.ua = Some(ua);
        insert_step(pool, t, s).await;
        finalize_run(pool, t, &root, finalized_by::EXPLICIT, None, None)
            .await
            .unwrap()
            .unwrap();
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn drift_at_a_release_transition_is_attributed_to_the_release(pool: PgPool) {
    let (t, sys) = ("t-impl-drift", "sys-drift");
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, sys).await;
    seed_agent(&pool, t, "agent-drift").await;
    runs(
        &pool,
        t,
        sys,
        "agent-drift",
        &[
            (200, "1.4.2", UA),
            (150, "1.4.2", UA),
            (100, "1.4.2", UA),
            (50, "1.5.0", UA),
            (40, "1.5.0", UA),
        ],
    )
    .await;
    let cp = crate::common::db_now() - Duration::minutes(45);
    let rc = cause::find_release_change(&pool, t, sys, cp, false)
        .await
        .unwrap()
        .expect("a release transition near the change point");
    assert!(rc.version_changed);
    assert_eq!(rc.from.version.as_deref(), Some("1.4.2"));
    assert_eq!(rc.to.version.as_deref(), Some("1.5.0"));
    let a = cause::attribute(
        &cause::CauseFacts {
            release_change: Some(rc),
            ..Default::default()
        },
        Some(cp),
    );
    assert_eq!(a.cause, cause::kind::AGENT_RELEASE);
    assert!(
        a.summary.contains("screening-agent@1.4.2") && a.summary.contains("screening-agent@1.5.0")
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_new_fingerprint_on_an_unchanged_release_is_the_undeclared_change(pool: PgPool) {
    let (t, sys) = ("t-impl-undeclared", "sys-undeclared");
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, sys).await;
    seed_agent(&pool, t, "agent-und").await;
    runs(
        &pool,
        t,
        sys,
        "agent-und",
        &[
            (200, "1.4.2", UA),
            (150, "1.4.2", UA),
            (100, "1.4.2", UA),
            (50, "1.4.2", "python-requests/2.32"),
        ],
    )
    .await;
    let cp = crate::common::db_now() - Duration::minutes(45);
    let rc = cause::find_release_change(&pool, t, sys, cp, false)
        .await
        .unwrap()
        .expect("the undeclared change is found");
    assert!(!rc.version_changed, "no version change");
    assert_eq!(rc.to.version.as_deref(), Some("1.4.2"));
    assert_ne!(rc.from_fingerprint, rc.to_fingerprint);
    let a = cause::attribute(
        &cause::CauseFacts {
            release_change: Some(rc),
            ..Default::default()
        },
        Some(cp),
    );
    assert_eq!(a.cause, cause::kind::AGENT_RELEASE);
    assert!(a.summary.starts_with("No version change"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_steady_release_explains_nothing(pool: PgPool) {
    let (t, sys) = ("t-impl-steady", "sys-steady");
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, sys).await;
    seed_agent(&pool, t, "agent-steady").await;
    runs(
        &pool,
        t,
        sys,
        "agent-steady",
        &[(200, "1.0", UA), (100, "1.0", UA), (50, "1.0", UA)],
    )
    .await;
    let cp = crate::common::db_now() - Duration::minutes(45);
    assert!(
        cause::find_release_change(&pool, t, sys, cp, false)
            .await
            .unwrap()
            .is_none()
    );
}

// ---------------------------------------------------------------------------
// Tier-A checks over the release facts
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_tier_a_check_on_agent_impl_version_works_end_to_end(pool: PgPool) {
    // RUN_FACT_KEYS carried client_label but not the release, so a check
    // like "flag runs of an agent release other than the approved one"
    // could not be written. Now: finalize a run whose actor declared a
    // release, and both the on-finalize evaluation (finalize_run_facts) and
    // the backtest (RunFactsRow::to_facts) see it.
    use super::assurance_checks::{breach_threshold, inbox_count, insert_check, result_rows};
    use brutor_gateway_core::services::assurance_checks::backtest;

    let t = "t-impl-check";
    common::seed_tenant(&pool, t).await;
    seed_system(&pool, t, "sys-chk").await;
    seed_agent(&pool, t, "agent-chk").await;
    let expression = "run.agent_impl_version == \"1.4.2\" \
         && run.agent_impl_name == \"screening-agent\" \
         && run.agent_impl_build == \"sha256:ab\" \
         && run.agent_impl_trust == \"declared\" \
         && run.implementation_fingerprint.startsWith(\"sha256:\") \
         && run.client_label == \"python-httpx\"";
    insert_check(
        &pool,
        t,
        "chk-release",
        expression,
        json!({}),
        breach_threshold(),
    )
    .await;
    insert_check(
        &pool,
        t,
        "chk-other-release",
        "run.agent_impl_version == \"9.9.9\"",
        json!({}),
        breach_threshold(),
    )
    .await;

    let t0 = crate::common::db_now() - Duration::minutes(10);
    let mut s = Step::new("root-chk", t0, "sys-chk", Some("agent-chk")).release(
        "screening-agent",
        "1.4.2",
        "header",
    );
    s.build = Some("sha256:ab");
    s.ua = Some(UA);
    s.label = Some("python-httpx");
    insert_step(&pool, t, s).await;
    finalize_run(&pool, t, "root-chk", finalized_by::EXPLICIT, None, None)
        .await
        .expect("finalize")
        .expect("run");

    let hit = result_rows(&pool, "chk-release").await;
    assert_eq!(hit.len(), 1, "the check evaluated once on finalize");
    assert_eq!(
        hit[0].1["value"],
        json!(true),
        "run.agent_impl_* reached the check"
    );
    let miss = result_rows(&pool, "chk-other-release").await;
    assert_eq!(miss.len(), 1);
    assert_eq!(miss[0].1["value"], json!(false));
    assert_eq!(inbox_count(&pool, t, "check_breach").await, 1);

    // The backtest reloads finalized runs through RunFactsRow::to_facts.
    let summary = backtest(&pool, t, "chk-release", 1, 7)
        .await
        .expect("backtest");
    assert_eq!(summary.evaluated, 1);
    assert_eq!(
        summary.errors, 0,
        "a release fact is missing from the reloaded run"
    );
    assert_eq!(summary.breaches, 1);
}
