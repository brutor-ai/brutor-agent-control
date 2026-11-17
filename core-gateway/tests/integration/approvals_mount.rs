// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Tool approvals are mounted at `/v1/approvals`,
//! outside the portal prefix, and nothing else about them changed.
//!
//! Why it matters: an agent whose call is parked is handed a poll URL. If the
//! route is not where the URL says, the agent never learns the decision; if
//! the tenant layer stopped treating the new prefix as runtime traffic, a
//! suspended tenant's agents would still learn it. Both are checked through
//! the real assembly (`GatewayBuilder::assemble`) with the real middleware.

use std::net::SocketAddr;

use axum::Router;
use axum::body::Body;
use axum::extract::ConnectInfo;
use axum::http::{Request, StatusCode};
use sqlx::PgPool;
use tower::ServiceExt;

use brutor_gateway_core::builder::GatewayBuilder;
use brutor_gateway_core::config::AppConfig;
use brutor_gateway_core::services::telemetry::TelemetryState;
use brutor_gateway_core::services::{AppState, SharedState};

use crate::common;

const TENANT: &str = "tenant-approvals-mount";
const GROUP: &str = "rg-approvals-mount";

fn test_state(pool: PgPool) -> AppState {
    AppState::new(
        AppConfig::for_test("postgresql://placeholder/replaced".to_string()),
        pool,
        None,
        reqwest::Client::new(),
        TelemetryState {
            prometheus_handle: None,
        },
    )
}

async fn get_with_key(app: &Router, path: &str, api_key: &str) -> (StatusCode, String) {
    let mut request = Request::builder()
        .method("GET")
        .uri(path)
        .header("authorization", format!("Bearer {api_key}"))
        .header("x-tenant-id", TENANT)
        .body(Body::empty())
        .expect("request");
    let peer: SocketAddr = "127.0.0.1:55555".parse().expect("peer");
    request.extensions_mut().insert(ConnectInfo(peer));
    let response = app.clone().oneshot(request).await.expect("router");
    let status = response.status();
    let body = axum::body::to_bytes(response.into_body(), 1 << 16)
        .await
        .expect("body");
    (status, String::from_utf8_lossy(&body).into_owned())
}

async fn assembled(pool: &PgPool) -> (Router, SharedState, String) {
    common::seed_tenant(pool, TENANT).await;
    common::seed_resource_group(pool, GROUP, TENANT).await;
    let key = common::seed_api_key(pool, TENANT, GROUP, None, "approvals-mount").await;
    let gateway = GatewayBuilder::assemble(test_state(pool.clone()), vec![]).expect("assemble");
    (gateway.app, gateway.state, key)
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_poll_route_answers_under_v1_approvals_and_the_old_prefix_is_gone(pool: PgPool) {
    let (app, _state, key) = assembled(&pool).await;

    // An agent with its key asks about a request that does not exist: the
    // route is there and answers for itself (404 from the handler, with the
    // handler's own message), not a bare 404 from the router.
    let (status, body) = get_with_key(&app, "/v1/approvals/01NOPE/poll", &key).await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{body}");
    assert!(
        body.contains("Approval request not found"),
        "the approvals handler answered, not the router: {body}"
    );

    // The badge count answers too (the key is not a portal session, so the
    // decision routes refuse it — that contract is unchanged by the move).
    let (status, body) = get_with_key(&app, "/v1/approvals/pending-count", &key).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED, "{body}");

    // Nothing answers under the portal prefix any more.
    let (status, body) = get_with_key(&app, "/v1/portal/approvals/01NOPE/poll", &key).await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{body}");
    assert!(
        !body.contains("Approval request not found"),
        "the old path still reaches the approvals handler: {body}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_suspended_tenants_agent_cannot_poll_a_decision(pool: PgPool) {
    let (app, state, key) = assembled(&pool).await;

    sqlx::query("UPDATE tenants SET suspended = TRUE, suspension_reason = 'test' WHERE id = $1")
        .bind(TENANT)
        .execute(&pool)
        .await
        .expect("suspend");
    state.tenant_cache.invalidate_tenant(TENANT);

    let (status, body) = get_with_key(&app, "/v1/approvals/01NOPE/poll", &key).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "a suspended tenant's agent must be blocked by the tenant layer, not answered: {body}"
    );
    assert!(
        !body.contains("Approval request not found"),
        "the approvals handler ran for a suspended tenant: {body}"
    );
}
