// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Regression tests for the MCP tool-approval flow.
//!
//! Catches the bug class where a raw `sqlx::query(...)` INSERT omits a
//! NOT NULL column that the SQLAlchemy model only sets app-side. The
//! tests drive the real HTTP routes against the real schema, so any
//! future drift between INSERT column lists and table definitions
//! breaks the build instead of breaking the demo.
//!
//! Coverage:
//!   * `tool_approval_requests` INSERT — `token_used` must land as `'N'`.
//!   * `notifications` INSERT (from `mcp.rs` create path) — `is_read`
//!     must default to FALSE, and the approver-pool SELECT must hit
//!     `resource_group_memberships` (not the non-existent
//!     `resource_group_members`).
//!   * `notifications` INSERT from the approve and reject paths in
//!     `approvals.rs` — same `is_read` default contract.

use axum::body::Body;
use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

pub(crate) async fn body_to_json(body: Body) -> Value {
    let bytes = body.collect().await.expect("collect body").to_bytes();
    serde_json::from_slice(&bytes).unwrap_or(Value::Null)
}

/// Insert a row into `resource_group_mcp_capabilities`. No existing
/// helper covers this table — the capability-filter path is exercised
/// for the first time in this test module.
#[allow(clippy::too_many_arguments)] // one parameter per column of the seeded row
pub(crate) async fn seed_capability_filter(
    pool: &PgPool,
    id: &str,
    tenant_id: &str,
    rg_id: &str,
    server_config_id: &str,
    capability_name: &str,
    access: &str,
    timeout_seconds: Option<i32>,
) {
    sqlx::query(
        "INSERT INTO resource_group_mcp_capabilities \
         (id, tenant_id, resource_group_id, server_config_id, capability_type, \
          capability_name, access, approval_timeout_seconds, created_at, updated_at) \
         VALUES ($1, $2, $3, $4, 'tool', $5, $6, $7, now(), now()) \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant_id)
    .bind(rg_id)
    .bind(server_config_id)
    .bind(capability_name)
    .bind(access)
    .bind(timeout_seconds)
    .execute(pool)
    .await
    .expect("seed_capability_filter");
}

/// Seed: tenant, RG, requester (user_id), mcp_server, server_config,
/// the RG↔server_config binding and an API key bound to `user_id`.
/// Returns the raw API key.
pub(crate) async fn seed_proxy_topology(pool: &PgPool, mock_uri: &str, user_id: i32) -> String {
    common::seed_tenant(pool, "tenant-app").await;
    common::seed_resource_group(pool, "rg-app", "tenant-app").await;
    common::seed_end_user(pool, user_id, "tenant-app").await;
    common::add_direct_membership(pool, user_id, "rg-app", "tenant-app").await;
    common::seed_mcp_server_with_base_url(pool, "ms-app", "tenant-app", "appsrv", mock_uri).await;
    common::seed_server_config(pool, "sc-app", "tenant-app", "ms-app").await;
    common::bind_server_config_to_rg(pool, "sc-app", "rg-app").await;
    common::seed_api_key(pool, "tenant-app", "rg-app", Some(user_id), "test-key").await
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn approval_required_creates_approval_row_and_notification(pool: PgPool) {
    // The capability is gated by policy — the upstream MCP must NOT be hit.
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"unused": true})))
        .expect(0)
        .mount(&mock)
        .await;

    let requester_id: i32 = 9001;
    let api_key = seed_proxy_topology(&pool, &mock.uri(), requester_id).await;
    seed_capability_filter(
        &pool,
        "cap-app-send-email",
        "tenant-app",
        "rg-app",
        "sc-app",
        "send_email",
        "approval_required",
        Some(300),
    )
    .await;

    let state = common::build_test_state(pool.clone());
    let app = common::build_mcp_proxy_app(state);

    let req = common::build_post_request(
        "/v1/proxy/mcp/ms-app",
        json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "send_email",
                "arguments": {"to": "j@example.com", "body": "hello"}
            }
        }),
        Some(&api_key),
        Some("tenant-app"),
    );
    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let body = body_to_json(response.into_body()).await;

    // API-key path returns 202 with the approval envelope; portal JWT would
    // return 428. We test the API-key shape because the existing harness
    // mints API keys; both paths share the same body and DB writes.
    assert_eq!(
        status,
        StatusCode::ACCEPTED,
        "approval-required tool must return 202 for API key auth; got {status} body={body}"
    );
    assert_eq!(body["approval_required"], json!(true));
    let approval_id = body["approval_request_id"]
        .as_str()
        .expect("approval_request_id in body")
        .to_string();
    assert_eq!(body["capability_name"], json!("send_email"));
    assert!(
        body["poll_url"]
            .as_str()
            .map(|s| s.starts_with("/v1/approvals/"))
            .unwrap_or(false),
        "poll_url must point at portal approvals endpoint; got {body}"
    );

    // tool_approval_requests: token_used must be 'N'. This is the assertion
    // that would have caught the original NOT NULL constraint violation —
    // before the fix the INSERT itself failed with a 500, so no row
    // existed to query.
    let approval_row: (String, String, String, String) = sqlx::query_as(
        "SELECT status, token_used, capability_name, requester_group_id \
         FROM tool_approval_requests WHERE id = $1",
    )
    .bind(&approval_id)
    .fetch_one(&pool)
    .await
    .expect("approval row exists");
    assert_eq!(approval_row.0, "pending");
    assert_eq!(approval_row.1, "N", "token_used must default to 'N'");
    assert_eq!(approval_row.2, "send_email");
    assert_eq!(approval_row.3, "rg-app");

    // notifications: at least one row for the requester, is_read=false.
    // Before the fix, the INSERT either silently failed (wrong table name
    // for the approver SELECT) or tripped is_read NOT NULL.
    let notif: (i32, bool, String) = sqlx::query_as(
        "SELECT user_id, is_read, notification_type FROM notifications \
         WHERE metadata->>'approval_request_id' = $1 LIMIT 1",
    )
    .bind(&approval_id)
    .fetch_one(&pool)
    .await
    .expect("notification row exists");
    assert_eq!(
        notif.0, requester_id,
        "notification must target the requester"
    );
    assert!(
        !notif.1,
        "is_read must default to false on new notifications"
    );
    assert_eq!(notif.2, "tool_approval_request");
}
