// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! End-to-end tests for the MCP proxy hot path.
//!
//! Sets up a `wiremock` MCP-style upstream, seeds an `mcp_servers` row
//! pointing `base_url` at the mock, binds it to a resource group via
//! `server_configs` → `resource_group_server_configs`, then drives
//! JSON-RPC requests through `POST /v1/proxy/mcp/{server_id}` and
//! asserts the proxy forwards correctly and propagates the response.

use axum::body::Body;
use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

/// (trace_id, span_id, server_version, tool_hash, content_hash) read back
/// from proxy_logs.
type AuditRow = (
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
);

async fn body_to_json(body: Body) -> Value {
    let bytes = body.collect().await.expect("collect body").to_bytes();
    serde_json::from_slice(&bytes).unwrap_or(Value::Null)
}

/// Seed the full topology: tenant + rg + user (direct member) + mcp_server
/// + server_config + binding + api_key. Returns the raw API key.
async fn seed_full_topology(pool: &PgPool, mock_uri: &str, user_id: i32) -> String {
    common::seed_tenant(pool, "tenant-1").await;
    common::seed_resource_group(pool, "rg-mcp", "tenant-1").await;
    common::seed_end_user(pool, user_id, "tenant-1").await;
    common::add_direct_membership(pool, user_id, "rg-mcp", "tenant-1").await;
    common::seed_mcp_server_with_base_url(pool, "ms-fixture", "tenant-1", "fixture", mock_uri)
        .await;
    common::seed_server_config(pool, "sc-fixture", "tenant-1", "ms-fixture").await;
    common::bind_server_config_to_rg(pool, "sc-fixture", "rg-mcp").await;
    common::seed_api_key(pool, "tenant-1", "rg-mcp", Some(user_id), "test-key").await
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn tools_list_round_trips_through_proxy(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "tools": [
                    {"name": "echo", "description": "Echo input back"}
                ]
            }
        })))
        .mount(&mock)
        .await;

    let api_key = seed_full_topology(&pool, &mock.uri(), 8001).await;
    let state = common::build_test_state(pool);
    let app = common::build_mcp_proxy_app(state);

    let req = common::build_post_request(
        "/v1/proxy/mcp/ms-fixture",
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
        Some(&api_key),
        Some("tenant-1"),
    );

    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let json = body_to_json(response.into_body()).await;

    assert_eq!(
        status,
        StatusCode::OK,
        "tools/list happy path must return 200; got {status} body={json}"
    );
    assert_eq!(json["jsonrpc"], "2.0");
    assert_eq!(json["id"], 1, "JSON-RPC id must round-trip");
    let names: Vec<String> = json["result"]["tools"]
        .as_array()
        .map(|a| {
            a.iter()
                .map(|t| t["name"].as_str().unwrap_or_default().to_string())
                .collect()
        })
        .unwrap_or_default();
    assert!(
        names.contains(&"echo".to_string()),
        "echo tool must be in response; got {names:?}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn tools_call_forwards_arguments_to_upstream(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "jsonrpc": "2.0",
            "id": 42,
            "result": {
                "content": [{"type": "text", "text": "echoed: hi mom"}],
                "isError": false
            }
        })))
        .expect(1)
        .mount(&mock)
        .await;

    let api_key = seed_full_topology(&pool, &mock.uri(), 8002).await;
    let state = common::build_test_state(pool);
    let app = common::build_mcp_proxy_app(state);

    let req = common::build_post_request(
        "/v1/proxy/mcp/ms-fixture",
        json!({
            "jsonrpc": "2.0",
            "id": 42,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"text": "hi mom"}}
        }),
        Some(&api_key),
        Some("tenant-1"),
    );

    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let json = body_to_json(response.into_body()).await;

    assert_eq!(
        status,
        StatusCode::OK,
        "tools/call happy path; got {status} body={json}"
    );
    assert_eq!(json["id"], 42, "request id must round-trip");
    assert_eq!(json["result"]["content"][0]["text"], "echoed: hi mom");
    // `.expect(1)` on the mock asserts the upstream was reached.
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn upstream_5xx_surfaces_as_proxy_error(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(503).set_body_string("upstream MCP is down"))
        .mount(&mock)
        .await;

    let api_key = seed_full_topology(&pool, &mock.uri(), 8003).await;
    let state = common::build_test_state(pool);
    let app = common::build_mcp_proxy_app(state);

    let req = common::build_post_request(
        "/v1/proxy/mcp/ms-fixture",
        json!({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}),
        Some(&api_key),
        Some("tenant-1"),
    );

    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let json = body_to_json(response.into_body()).await;

    // Upstream 503 must produce a 5xx to the client. The body may be
    // either a JSON-RPC envelope (with the request id echoed for
    // correlation) or a plain error JSON — either is acceptable for
    // this gate; the contract is "client sees a real failure status",
    // not a 200 with a hidden error.
    assert!(
        status.is_server_error(),
        "upstream 5xx must produce a 5xx client response; got {status} body={json}"
    );
}

/// A `tools/call` row records the trace context the caller asserted,
/// the server version the upstream announced, the hash of the tool
/// definition it listed, and a digest of the request content. End to end,
/// through the real route, middleware and writer.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_tools_call_row_records_trace_context_and_tool_provenance(pool: PgPool) {
    use brutor_gateway_core::middleware;
    use brutor_gateway_core::services::tool_provenance::definition_hash;
    use wiremock::matchers::body_partial_json;

    let mock = MockServer::start().await;
    let echo_def = json!({"name": "echo", "description": "Echo input back",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}});
    Mock::given(method("POST"))
        .and(body_partial_json(json!({"method": "initialize"})))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "jsonrpc": "2.0", "id": 1,
            "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                       "serverInfo": {"name": "fixture", "version": "3.1.4"}}
        })))
        .mount(&mock)
        .await;
    Mock::given(method("POST"))
        .and(body_partial_json(json!({"method": "tools/list"})))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "jsonrpc": "2.0", "id": 2, "result": {"tools": [echo_def.clone()]}
        })))
        .mount(&mock)
        .await;
    Mock::given(method("POST"))
        .and(body_partial_json(json!({"method": "tools/call"})))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "jsonrpc": "2.0", "id": 3,
            "result": {"content": [{"type": "text", "text": "echoed"}], "isError": false}
        })))
        .mount(&mock)
        .await;

    let api_key = seed_full_topology(&pool, &mock.uri(), 8004).await;
    let state = common::build_test_state(pool.clone());
    // The production stack, not the bare router: the run-end layer is what
    // scopes the per-request task-locals the row is built from.
    let app = axum::Router::new()
        .nest("/v1/proxy/mcp", brutor_gateway_core::routes::mcp::router())
        .layer(middleware::run_end::RunEndLayer::new(
            state.mcp_sessions.clone(),
        ))
        .layer(middleware::tenant::TenantLayer::new(state.clone()))
        .with_state(state);

    let traceparent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01";
    for (id, body) in [
        (
            1,
            json!({"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}),
        ),
        (
            2,
            json!({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ),
        (
            3,
            json!({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "echo", "arguments": {"text": "hi"}}}),
        ),
    ] {
        let mut req = common::build_post_request(
            "/v1/proxy/mcp/ms-fixture",
            body,
            Some(&api_key),
            Some("tenant-1"),
        );
        req.headers_mut()
            .insert("traceparent", traceparent.parse().unwrap());
        let response = app.clone().oneshot(req).await.expect("router call");
        let status = response.status();
        let json = body_to_json(response.into_body()).await;
        assert_eq!(status, StatusCode::OK, "call {id}: {json}");
    }

    let mut row: Option<AuditRow> = None;
    for _ in 0..80 {
        row = sqlx::query_as(
            "SELECT trace_id, span_id, server_version, tool_definition_hash, request_content_hash \
             FROM proxy_logs WHERE tenant_id = 'tenant-1' AND mcp_method = 'tools/call' \
             ORDER BY timestamp DESC LIMIT 1",
        )
        .fetch_optional(&pool)
        .await
        .expect("query");
        if row.is_some() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    let (trace_id, span_id, server_version, tool_hash, content_hash) =
        row.expect("the tools/call audit row never landed");
    assert_eq!(
        trace_id.as_deref(),
        Some("0af7651916cd43dd8448eb211c80319c")
    );
    assert_eq!(span_id.as_deref(), Some("b7ad6b7169203331"));
    assert_eq!(
        server_version.as_deref(),
        Some("3.1.4"),
        "as announced at initialize"
    );
    assert_eq!(
        tool_hash,
        Some(definition_hash(&echo_def)),
        "the definition the upstream listed"
    );
    assert_eq!(
        content_hash.as_deref().map(str::len),
        Some(64),
        "request digest sealed"
    );
}
