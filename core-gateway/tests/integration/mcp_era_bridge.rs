// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Legacy-client → stateless-upstream era bridge.
//!
//! A legacy client (`initialize`, no `_meta`, no routing headers) talks to
//! a remote MCP server that only speaks the stateless 2026-07-28 dialect
//! (`mcp_servers.dialect = 'stateless'`). The gateway must:
//!   * answer the `initialize` handshake itself (the upstream has none),
//!   * 202 `notifications/initialized`,
//!   * upgrade every forwarded call with the required `_meta`
//!     (protocolVersion + clientCapabilities) and the
//!     `MCP-Protocol-Version` header.
//!
//! This was previously validated only by a manual E2E against
//! `brutor-mcp-stateless`; these tests pin it as automated regression.

use axum::body::Body;
use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{body_partial_json, header, method};
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

async fn body_to_json(body: Body) -> Value {
    let bytes = body.collect().await.expect("collect body").to_bytes();
    serde_json::from_slice(&bytes).unwrap_or(Value::Null)
}

/// Seed the topology and mark the upstream as stateless-only.
async fn seed_stateless_upstream(pool: &PgPool, mock_uri: &str, user_id: i32) -> String {
    common::seed_tenant(pool, "tenant-1").await;
    common::seed_resource_group(pool, "rg-mcp", "tenant-1").await;
    common::seed_end_user(pool, user_id, "tenant-1").await;
    common::add_direct_membership(pool, user_id, "rg-mcp", "tenant-1").await;
    common::seed_mcp_server_with_base_url(pool, "ms-stateless", "tenant-1", "fixture", mock_uri)
        .await;
    sqlx::query("UPDATE mcp_servers SET dialect = 'stateless', protocol_version = '2026-07-28' WHERE id = 'ms-stateless'")
        .execute(pool)
        .await
        .expect("mark upstream stateless");
    common::seed_server_config(pool, "sc-stateless", "tenant-1", "ms-stateless").await;
    common::bind_server_config_to_rg(pool, "sc-stateless", "rg-mcp").await;
    common::seed_api_key(pool, "tenant-1", "rg-mcp", Some(user_id), "test-key").await
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn legacy_initialize_is_synthesized_not_forwarded(pool: PgPool) {
    // The upstream is stateless — it has no `initialize`. If the gateway
    // forwarded it, this mock would need to answer; instead it must never
    // be hit.
    let mock = MockServer::start().await;
    let api_key = seed_stateless_upstream(&pool, &mock.uri(), 9101).await;
    let app = common::build_mcp_proxy_app(common::build_test_state(pool));

    let req = common::build_post_request(
        "/v1/proxy/mcp/ms-stateless",
        json!({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "legacy-client", "version": "1.0"}
            }
        }),
        Some(&api_key),
        Some("tenant-1"),
    );

    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    let json = body_to_json(resp.into_body()).await;

    assert_eq!(
        status,
        StatusCode::OK,
        "synthesized initialize; body={json}"
    );
    // A legacy-shaped result the client understands.
    assert!(
        json["result"]["protocolVersion"].is_string(),
        "must carry protocolVersion; got {json}"
    );
    assert!(json["result"]["capabilities"]["tools"].is_object());
    assert!(json["result"]["serverInfo"]["name"].is_string());
    // The stateless upstream was never contacted for the handshake.
    assert!(
        mock.received_requests().await.unwrap().is_empty(),
        "gateway must synthesize initialize locally, not forward it"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn notifications_initialized_is_accepted_locally(pool: PgPool) {
    let mock = MockServer::start().await;
    let api_key = seed_stateless_upstream(&pool, &mock.uri(), 9102).await;
    let app = common::build_mcp_proxy_app(common::build_test_state(pool));

    let req = common::build_post_request(
        "/v1/proxy/mcp/ms-stateless",
        json!({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        Some(&api_key),
        Some("tenant-1"),
    );

    let resp = app.oneshot(req).await.expect("router call");
    assert_eq!(resp.status(), StatusCode::ACCEPTED);
    assert!(
        mock.received_requests().await.unwrap().is_empty(),
        "there is no upstream session to notify"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn tools_call_is_upgraded_with_meta_and_header(pool: PgPool) {
    // The mock ONLY answers when the forwarded request carries the modern
    // `_meta.protocolVersion` AND the `MCP-Protocol-Version` header — so a
    // 200 here proves the gateway upgraded a legacy `tools/call`.
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(header("mcp-protocol-version", "2026-07-28"))
        .and(body_partial_json(json!({
            "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}
        })))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "jsonrpc": "2.0", "id": 5,
            "result": {"resultType": "complete", "content": [{"type": "text", "text": "ok"}]}
        })))
        .mount(&mock)
        .await;

    let api_key = seed_stateless_upstream(&pool, &mock.uri(), 9103).await;
    let app = common::build_mcp_proxy_app(common::build_test_state(pool));

    // A legacy client sends NO _meta and NO routing headers.
    let req = common::build_post_request(
        "/v1/proxy/mcp/ms-stateless",
        json!({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "echo", "arguments": {"text": "hi"}}
        }),
        Some(&api_key),
        Some("tenant-1"),
    );

    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    let json = body_to_json(resp.into_body()).await;
    assert_eq!(
        status,
        StatusCode::OK,
        "upgraded tools/call must reach the strict upstream; body={json}"
    );
    assert_eq!(json["result"]["content"][0]["text"], "ok");
}
