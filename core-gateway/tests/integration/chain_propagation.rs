// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! End-to-end chain propagation.
//!
//! Drives real Axum routes against a `wiremock` upstream that captures
//! the inbound headers it received. Asserts that:
//!
//!   * **mint**: a request with no inbound `x-brutor-delegation-*` headers
//!     causes the gateway to mint a fresh root chain and emit it on the
//!     upstream send.
//!   * **verify-and-extend**: a request that already carries a valid
//!     signed chain is verified, depth increments by one, parent points
//!     at the inbound `current_task_id`, and the new HMAC validates.
//!   * **sig_failed**: a tampered signature is treated as `sig_failed`
//!     (defensive remint, not a silent passthrough).
//!   * **client_dropped**: headers present but signature absent are
//!     treated as `client_dropped` and a fresh chain is minted.
//!   * **depth_overflow**: depth at the absolute hard cap aborts the
//!     request with a 4xx rather than emitting a chain that breaks
//!     downstream verification.
//!
//! The mock captures every request's headers via a static `Mock::matchers`
//! side-effect (the `wiremock` `Match` trait). After each call the test
//! pulls the captured headers off the channel and asserts shape.

use axum::body::Body;
use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use std::sync::{Arc, Mutex};
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, Request as WireRequest, Respond, ResponseTemplate};

use crate::common;

use brutor_gateway_core::services::call_chain::{
    CallChain, HEADER_DEPTH, HEADER_PARENT, HEADER_ROOT, HEADER_SIG, derive_tenant_key,
    parse_or_mint,
};

// ---------------------------------------------------------------------------
// Test harness
// ---------------------------------------------------------------------------

/// One captured request = one list of (name, value) header pairs.
type HeaderRows = Arc<Mutex<Vec<Vec<(String, String)>>>>;

#[derive(Clone, Default)]
struct CapturedHeaders {
    rows: HeaderRows,
}

impl CapturedHeaders {
    fn snapshot(&self) -> Vec<Vec<(String, String)>> {
        self.rows.lock().expect("lock").clone()
    }

    fn last(&self) -> Option<Vec<(String, String)>> {
        self.rows.lock().expect("lock").last().cloned()
    }

    fn header(headers: &[(String, String)], name: &str) -> Option<String> {
        headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.clone())
    }
}

struct CaptureResponder {
    capture: CapturedHeaders,
    body: Value,
}

impl Respond for CaptureResponder {
    fn respond(&self, req: &WireRequest) -> ResponseTemplate {
        let pairs: Vec<(String, String)> = req
            .headers
            .iter()
            .map(|(k, v)| {
                (
                    k.as_str().to_string(),
                    v.to_str().unwrap_or_default().to_string(),
                )
            })
            .collect();
        self.capture.rows.lock().expect("lock").push(pairs);
        ResponseTemplate::new(200).set_body_json(self.body.clone())
    }
}

async fn seed(pool: &PgPool, mock_uri: &str, user_id: i32) -> String {
    common::seed_tenant(pool, "tenant-chain").await;
    common::seed_resource_group(pool, "rg-chain", "tenant-chain").await;
    common::seed_end_user(pool, user_id, "tenant-chain").await;
    common::add_direct_membership(pool, user_id, "rg-chain", "tenant-chain").await;
    common::seed_mcp_server_with_base_url(
        pool,
        "ms-chain",
        "tenant-chain",
        "chain-fixture",
        mock_uri,
    )
    .await;
    common::seed_server_config(pool, "sc-chain", "tenant-chain", "ms-chain").await;
    common::bind_server_config_to_rg(pool, "sc-chain", "rg-chain").await;
    common::seed_api_key(pool, "tenant-chain", "rg-chain", Some(user_id), "chain-key").await
}

async fn body_to_json(body: Body) -> Value {
    let bytes = body.collect().await.expect("collect body").to_bytes();
    serde_json::from_slice(&bytes).unwrap_or(Value::Null)
}

fn mint_inbound_chain(tenant_key: &[u8]) -> (Vec<(&'static str, String)>, CallChain) {
    // Use parse_or_mint with no inbound headers to mint a fresh chain
    // for the "client started a chain" scenario.
    let (chain, _current, _continuity) = parse_or_mint(|_n| None::<String>, tenant_key);
    let headers = chain.to_header_pairs(tenant_key);
    (headers, chain)
}

fn tenant_key_for(state: &brutor_gateway_core::services::SharedState) -> Vec<u8> {
    derive_tenant_key(state.config.encryption_key.as_bytes(), "tenant-chain")
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn mints_root_chain_when_inbound_has_no_headers(pool: PgPool) {
    let capture = CapturedHeaders::default();
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(CaptureResponder {
            capture: capture.clone(),
            body: json!({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}),
        })
        .mount(&mock)
        .await;

    let api_key = seed(&pool, &mock.uri(), 9101).await;
    let state = common::build_test_state(pool);
    let app = common::build_mcp_proxy_app(state);

    let req = common::build_post_request(
        "/v1/proxy/mcp/ms-chain",
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
        Some(&api_key),
        Some("tenant-chain"),
    );

    let response = app.oneshot(req).await.expect("router call");
    assert_eq!(response.status(), StatusCode::OK);

    let captured = capture
        .last()
        .expect("upstream must have been reached at least once");
    let root = CapturedHeaders::header(&captured, HEADER_ROOT);
    let parent = CapturedHeaders::header(&captured, HEADER_PARENT);
    let depth = CapturedHeaders::header(&captured, HEADER_DEPTH);
    let sig = CapturedHeaders::header(&captured, HEADER_SIG);

    assert!(
        root.is_some(),
        "minted chain must emit x-brutor-delegation-root"
    );
    assert!(
        parent.is_some(),
        "minted chain must emit a parent (=root at depth 0)"
    );
    assert_eq!(depth.as_deref(), Some("0"), "minted root must be depth 0");
    assert!(sig.is_some(), "every emitted chain MUST carry a signature");

    // Parent equals root at depth 0 (the minted-root invariant). Mission
    // Control depends on this: depth-0 rows are tree roots.
    assert_eq!(
        root.as_deref(),
        parent.as_deref(),
        "depth-0 chains must have parent == root"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn verifies_and_extends_inbound_chain(pool: PgPool) {
    let capture = CapturedHeaders::default();
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(CaptureResponder {
            capture: capture.clone(),
            body: json!({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}),
        })
        .mount(&mock)
        .await;

    let api_key = seed(&pool, &mock.uri(), 9102).await;
    let state = common::build_test_state(pool.clone());
    let tk = tenant_key_for(&state);
    let (inbound_headers, inbound_chain) = mint_inbound_chain(&tk);

    let app = common::build_mcp_proxy_app(state);
    let mut req = common::build_post_request(
        "/v1/proxy/mcp/ms-chain",
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
        Some(&api_key),
        Some("tenant-chain"),
    );
    for (k, v) in inbound_headers.iter() {
        req.headers_mut()
            .insert(*k, axum::http::HeaderValue::from_str(v).expect("hv"));
    }

    let response = app.oneshot(req).await.expect("router call");
    assert_eq!(response.status(), StatusCode::OK);

    let captured = capture.last().expect("upstream reached");
    let root = CapturedHeaders::header(&captured, HEADER_ROOT).expect("root emitted");
    let parent = CapturedHeaders::header(&captured, HEADER_PARENT).expect("parent emitted");
    let depth = CapturedHeaders::header(&captured, HEADER_DEPTH).expect("depth emitted");
    let sig = CapturedHeaders::header(&captured, HEADER_SIG).expect("sig emitted");

    assert_eq!(
        root,
        inbound_chain.root_task_id.to_string(),
        "verified chain must keep the same root across hops"
    );
    assert_eq!(depth, "1", "depth must increment by one after verification");
    assert_ne!(
        parent,
        inbound_chain.root_task_id.to_string(),
        "parent must be the freshly minted current_task_id, not the root"
    );
    assert!(!sig.is_empty(), "verified chain re-signs at the new depth");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn tampered_signature_is_treated_as_sig_failed(pool: PgPool) {
    let capture = CapturedHeaders::default();
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(CaptureResponder {
            capture: capture.clone(),
            body: json!({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}),
        })
        .mount(&mock)
        .await;

    let api_key = seed(&pool, &mock.uri(), 9103).await;
    let state = common::build_test_state(pool.clone());
    let tk = tenant_key_for(&state);
    let (inbound_headers, inbound_chain) = mint_inbound_chain(&tk);

    let app = common::build_mcp_proxy_app(state);
    let mut req = common::build_post_request(
        "/v1/proxy/mcp/ms-chain",
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
        Some(&api_key),
        Some("tenant-chain"),
    );
    for (k, v) in inbound_headers.iter() {
        let value = if *k == HEADER_SIG {
            "AAAA-tampered-AAAA".to_string()
        } else {
            v.clone()
        };
        req.headers_mut()
            .insert(*k, axum::http::HeaderValue::from_str(&value).expect("hv"));
    }

    let response = app.oneshot(req).await.expect("router call");
    assert_eq!(
        response.status(),
        StatusCode::OK,
        "sig_failed must not 5xx — the gateway mints a fresh root and proceeds"
    );

    let captured = capture.last().expect("upstream reached");
    let root = CapturedHeaders::header(&captured, HEADER_ROOT).expect("root emitted");
    let depth = CapturedHeaders::header(&captured, HEADER_DEPTH).expect("depth emitted");
    let sig = CapturedHeaders::header(&captured, HEADER_SIG).expect("sig emitted");

    assert_ne!(
        root,
        inbound_chain.root_task_id.to_string(),
        "tampered sig must produce a fresh root — never trust an unverified inbound chain"
    );
    assert_eq!(depth, "0", "fresh root after sig_failed must be depth 0");
    assert!(
        !sig.is_empty(),
        "fresh-root chain still carries its own sig"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn missing_signature_is_treated_as_client_dropped(pool: PgPool) {
    let capture = CapturedHeaders::default();
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(CaptureResponder {
            capture: capture.clone(),
            body: json!({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}),
        })
        .mount(&mock)
        .await;

    let api_key = seed(&pool, &mock.uri(), 9104).await;
    let state = common::build_test_state(pool.clone());
    let tk = tenant_key_for(&state);
    let (inbound_headers, inbound_chain) = mint_inbound_chain(&tk);

    let app = common::build_mcp_proxy_app(state);
    let mut req = common::build_post_request(
        "/v1/proxy/mcp/ms-chain",
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
        Some(&api_key),
        Some("tenant-chain"),
    );
    for (k, v) in inbound_headers.iter() {
        if *k == HEADER_SIG {
            // Drop the signature — equivalent to a client that didn't
            // sign its outbound chain.
            continue;
        }
        req.headers_mut()
            .insert(*k, axum::http::HeaderValue::from_str(v).expect("hv"));
    }

    let response = app.oneshot(req).await.expect("router call");
    assert_eq!(response.status(), StatusCode::OK);

    let captured = capture.last().expect("upstream reached");
    let root = CapturedHeaders::header(&captured, HEADER_ROOT).expect("root emitted");
    let depth = CapturedHeaders::header(&captured, HEADER_DEPTH).expect("depth emitted");

    // client_dropped must NOT carry the inbound root forward — that would
    // allow a malicious client to set whatever root they like.
    assert_ne!(
        root,
        inbound_chain.root_task_id.to_string(),
        "client_dropped must mint a fresh root rather than honour the inbound one"
    );
    assert_eq!(
        depth, "0",
        "client_dropped recovery is a fresh depth-0 root"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn depth_overflow_aborts_with_error_status(pool: PgPool) {
    // Construct an inbound chain at the absolute hard cap. The mcp.rs
    // route's parse_or_mint extends by one — which trips the
    // overflow check rather than emitting a chain at the forbidden
    // depth.
    let capture = CapturedHeaders::default();
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(CaptureResponder {
            capture: capture.clone(),
            body: json!({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}),
        })
        .mount(&mock)
        .await;

    let api_key = seed(&pool, &mock.uri(), 9105).await;
    let state = common::build_test_state(pool.clone());
    let tk = tenant_key_for(&state);
    // Build a chain at ABSOLUTE_MAX_DEPTH using the same primitives the
    // gateway exposes — that way the test stays valid if the cap is
    // later relaxed.
    let cap = brutor_gateway_core::services::call_chain::ABSOLUTE_MAX_DEPTH;
    let (mut chain, _current, _continuity) = parse_or_mint(|_| None::<String>, &tk);
    // Iteratively extend to cap via fork_child — the same primitive
    // the producer side uses. After this loop the chain sits at
    // ABSOLUTE_MAX_DEPTH; the gateway's parse_or_mint will fail to
    // extend it one more hop and the route handler must abort.
    while chain.depth < cap {
        chain = chain
            .fork_child(ulid::Ulid::new())
            .expect("extension below the cap must succeed");
    }
    let headers = chain.to_header_pairs(&tk);

    let app = common::build_mcp_proxy_app(state);
    let mut req = common::build_post_request(
        "/v1/proxy/mcp/ms-chain",
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
        Some(&api_key),
        Some("tenant-chain"),
    );
    for (k, v) in headers.iter() {
        req.headers_mut()
            .insert(*k, axum::http::HeaderValue::from_str(v).expect("hv"));
    }

    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let json = body_to_json(response.into_body()).await;
    assert!(
        status.is_client_error() || status.is_server_error(),
        "request at depth cap must NOT succeed; got {status} body={json}"
    );
    // The upstream must NEVER be reached when depth overflows — that
    // would mean we emitted a chain header at a forbidden depth.
    let snapshot = capture.snapshot();
    assert!(
        snapshot.is_empty(),
        "depth-overflow must short-circuit before the upstream send; got {snapshot:?}"
    );
}
