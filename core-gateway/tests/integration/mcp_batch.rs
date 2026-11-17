// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A request the proxy does
//! not recognise as MCP must never reach an MCP upstream ungoverned.
//!
//! The capability ACL, approval holds, agent authorisation, argument policy
//! and the run-abort check all sit behind "is this an MCP request?". That
//! classifier used to answer from the `Content-Type` header and from the
//! top-level JSON value being an object, so two wrappers walked a
//! `tools/call` past every one of those controls to the upstream:
//!
//! * a JSON-RPC **batch** — the same call inside `[ … ]`, which MCP
//!   2025-03-26 servers still execute;
//! * a JSON-RPC message sent as **`text/plain`**.
//!
//! What these pin: a denied tool stays denied, and an approval-required tool
//! stays held, whatever the envelope — and the upstream sees nothing.

use axum::http::{HeaderValue, StatusCode};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

const T: &str = "tenant-batch";

async fn seed_topology(pool: &PgPool, mock_uri: &str, user_id: i32) -> String {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, "rg-batch", T).await;
    common::seed_end_user(pool, user_id, T).await;
    common::add_direct_membership(pool, user_id, "rg-batch", T).await;
    common::seed_mcp_server_with_base_url(pool, "ms-batch", T, "crm", mock_uri).await;
    common::seed_server_config(pool, "sc-batch", T, "ms-batch").await;
    common::bind_server_config_to_rg(pool, "sc-batch", "rg-batch").await;
    common::set_mcp_capability_filter(
        pool,
        T,
        "rg-batch",
        "sc-batch",
        "tool",
        "delete_customer",
        "disabled",
    )
    .await;
    common::set_mcp_capability_filter(
        pool,
        T,
        "rg-batch",
        "sc-batch",
        "tool",
        "refund_order",
        "approval_required",
    )
    .await;
    common::seed_api_key(pool, T, "rg-batch", Some(user_id), "batch-key").await
}

/// An upstream that must never be reached; `expect(0)` is verified on drop.
async fn untouchable_upstream() -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({"jsonrpc": "2.0", "id": 1, "result": {"executed": true}})),
        )
        .expect(0)
        .mount(&mock)
        .await;
    mock
}

fn tool_call(id: i64, tool: &str) -> Value {
    json!({
        "jsonrpc": "2.0", "id": id, "method": "tools/call",
        "params": {"name": tool, "arguments": {"customer_id": "c-1"}}
    })
}

async fn send(pool: &PgPool, key: &str, body: Value, content_type: &str) -> (StatusCode, Value) {
    let app = common::build_mcp_proxy_app(common::build_test_state(pool.clone()));
    let mut req = common::build_post_request("/v1/proxy/mcp/ms-batch", body, Some(key), Some(T));
    req.headers_mut()
        .insert("content-type", HeaderValue::from_str(content_type).unwrap());
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

/// Control: the single, correctly-typed call is refused today.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_disabled_tool_is_refused(pool: PgPool) {
    let mock = untouchable_upstream().await;
    let key = seed_topology(&pool, &mock.uri(), 9801).await;

    let (status, body) = send(
        &pool,
        &key,
        tool_call(1, "delete_customer"),
        "application/json",
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_batch_cannot_carry_a_disabled_tool_past_the_acl(pool: PgPool) {
    let mock = untouchable_upstream().await;
    let key = seed_topology(&pool, &mock.uri(), 9802).await;

    let batch = json!([tool_call(1, "delete_customer")]);
    let (status, body) = send(&pool, &key, batch, "application/json").await;
    assert_eq!(
        status,
        StatusCode::BAD_REQUEST,
        "a JSON-RPC batch must be refused, not forwarded ungoverned: {body}"
    );
    assert_eq!(body["jsonrpc"], json!("2.0"), "{body}");
    assert_eq!(body["error"]["code"], json!(-32600), "{body}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_batch_cannot_skip_an_approval_hold(pool: PgPool) {
    let mock = untouchable_upstream().await;
    let key = seed_topology(&pool, &mock.uri(), 9803).await;

    // Mixed with a harmless call, as an agent would send it.
    let batch = json!([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        tool_call(2, "refund_order"),
    ]);
    let (status, body) = send(&pool, &key, batch, "application/json").await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    let held: (i64,) =
        sqlx::query_as("SELECT COUNT(*) FROM tool_approval_requests WHERE tenant_id = $1")
            .bind(T)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(held.0, 0, "a refused batch queues nothing for approval");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_jsonrpc_message_sent_as_text_plain_is_still_governed(pool: PgPool) {
    let mock = untouchable_upstream().await;
    let key = seed_topology(&pool, &mock.uri(), 9804).await;

    let (status, body) = send(&pool, &key, tool_call(1, "delete_customer"), "text/plain").await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "the content type must not decide whether the ACL applies: {body}"
    );
}

// ---------------------------------------------------------------------------
// One reading of the body. The classifier skipped a
// UTF-8 BOM and called the body a message; the handler parsed the unstripped
// bytes, got nothing, ran no control, and forwarded the bytes.
// ---------------------------------------------------------------------------

async fn send_bytes(pool: &PgPool, key: &str, body: Vec<u8>) -> (StatusCode, Value) {
    let app = common::build_mcp_proxy_app(common::build_test_state(pool.clone()));
    let mut req =
        common::build_post_request("/v1/proxy/mcp/ms-batch", json!({}), Some(key), Some(T));
    *req.body_mut() = axum::body::Body::from(body);
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

fn with_bom(body: &Value) -> Vec<u8> {
    [&b"\xEF\xBB\xBF"[..], body.to_string().as_bytes()].concat()
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_bom_cannot_carry_a_disabled_tool_past_the_acl(pool: PgPool) {
    let mock = untouchable_upstream().await;
    let key = seed_topology(&pool, &mock.uri(), 9805).await;

    let (status, body) = send_bytes(&pool, &key, with_bom(&tool_call(1, "delete_customer"))).await;
    assert_eq!(
        mock.received_requests().await.unwrap().len(),
        0,
        "the upstream received the call"
    );
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_bom_cannot_skip_an_approval_hold(pool: PgPool) {
    let mock = untouchable_upstream().await;
    let key = seed_topology(&pool, &mock.uri(), 9806).await;

    let (status, body) = send_bytes(&pool, &key, with_bom(&tool_call(1, "refund_order"))).await;
    assert_eq!(mock.received_requests().await.unwrap().len(), 0);
    assert_ne!(status, StatusCode::OK, "{body}");
    let held: (i64,) =
        sqlx::query_as("SELECT COUNT(*) FROM tool_approval_requests WHERE tenant_id = $1")
            .bind(T)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(held.0, 1, "the call is held for approval like any other");
}

/// The control for the two above: an allowed tool sent with a BOM is
/// forwarded, and what the upstream receives is the message the gateway
/// judged (no BOM).
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_allowed_call_with_a_bom_is_forwarded_as_the_message_that_was_judged(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({"jsonrpc": "2.0", "id": 1, "result": {"executed": true}})),
        )
        .mount(&mock)
        .await;
    let key = seed_topology(&pool, &mock.uri(), 9807).await;
    common::set_mcp_capability_filter(
        &pool,
        T,
        "rg-batch",
        "sc-batch",
        "tool",
        "list_customers",
        "enabled",
    )
    .await;

    let call = tool_call(1, "list_customers");
    let (status, body) = send_bytes(&pool, &key, with_bom(&call)).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let received = mock.received_requests().await.unwrap();
    assert_eq!(received.len(), 1);
    assert_eq!(
        serde_json::from_slice::<Value>(&received[0].body).expect("no BOM reaches the upstream"),
        call
    );
}

/// A body a JSON-RPC server might execute and the gateway cannot read as
/// one message is refused, never forwarded: a key written twice (this
/// parser judges the last, a first-wins upstream acts on the first), text
/// that is not one JSON value, JSON in UTF-16.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_message_the_gateway_cannot_read_is_refused_not_forwarded(pool: PgPool) {
    let mock = untouchable_upstream().await;
    let key = seed_topology(&pool, &mock.uri(), 9808).await;

    let utf16: Vec<u8> = tool_call(1, "delete_customer")
        .to_string()
        .encode_utf16()
        .flat_map(u16::to_le_bytes)
        .collect();
    let bodies: Vec<Vec<u8>> = vec![
        // The ACL would judge `list_customers`; a first-wins parser runs
        // `delete_customer`.
        br#"{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"delete_customer","name":"list_customers","arguments":{}}}"#.to_vec(),
        br#"{"jsonrpc":"2.0","id":1,"method":"tools/call","method":"tools/list","params":{"name":"delete_customer"}}"#.to_vec(),
        br#"{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_customers","arguments":{"amount":20000,"amount":1}}}"#.to_vec(),
        br#"{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"delete_customer"}} trailing"#.to_vec(),
        br#"{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"delete_customer"},}"#.to_vec(),
        utf16,
    ];
    for bytes in bodies {
        let shown = String::from_utf8_lossy(&bytes).into_owned();
        let (status, body) = send_bytes(&pool, &key, bytes).await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{shown}: {body}");
        assert_eq!(body["error"]["code"], json!(-32700), "{shown}: {body}");
    }
}
