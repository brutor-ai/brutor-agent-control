// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A tenant whose feature switch is off is refused on the surface that
//! feature carries: models, MCP servers, virtual MCP servers, agent skills,
//! agents (A2A). Only the `a2a` switch was read; a tenant with "Models" off
//! could still call every model. The switches come with the tenant status
//! the tenant layer loads (`services::tenant_features`).

use axum::Router;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;

use brutor_gateway_core::{middleware, routes};

use crate::common;

fn app(pool: &PgPool) -> Router {
    let state = common::build_test_state(pool.clone());
    Router::new()
        .nest("/v1/proxy/llm", routes::llm::router())
        .nest("/v1/proxy/mcp", routes::mcp::router())
        .nest("/v1/proxy/vmcp", routes::vmcp::router())
        .nest("/v1/proxy/skills", routes::skills::router())
        .nest("/v1/proxy/a2a", routes::a2a::router())
        .layer(middleware::tenant::TenantLayer::new(state.clone()))
        .with_state(state)
}

async fn send(
    pool: &PgPool,
    tenant: &str,
    key: &str,
    path: &str,
    body: Value,
) -> (StatusCode, String) {
    let req: Request<Body> = common::build_post_request(path, body, Some(key), Some(tenant));
    let resp = app(pool).oneshot(req).await.expect("router");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.expect("body").to_bytes();
    (status, String::from_utf8_lossy(&bytes).into_owned())
}

/// A tenant `t` with these switches, a group, the MCP server
/// `srv-features-{t}` bound to it, and an API key of the group.
async fn world(pool: &PgPool, t: &str, features: Value) -> String {
    let rg = format!("rg-{t}");
    common::seed_tenant(pool, t).await;
    common::seed_resource_group(pool, &rg, t).await;
    sqlx::query("UPDATE tenants SET features = $2 WHERE id = $1")
        .bind(t)
        .bind(features)
        .execute(pool)
        .await
        .expect("set the tenant's features");
    let server = format!("srv-features-{t}");
    common::seed_mcp_server(pool, &server, t, &format!("server-{t}")).await;
    common::seed_server_config(pool, &format!("sc-{t}"), t, &server).await;
    common::bind_server_config_to_rg(pool, &format!("sc-{t}"), &rg).await;
    common::seed_api_key(pool, t, &rg, None, &format!("key-{t}")).await
}

/// Each surface: refused with its feature's sentence while the switch is
/// off; not refused for that reason while it is on.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_switched_off_feature_refuses_its_surface(pool: PgPool) {
    let surfaces: [(&str, &str, Value, &str); 5] = [
        (
            "models",
            "/v1/proxy/llm/chat/completions",
            json!({"model": "m-features", "messages": [{"role": "user", "content": "hi"}]}),
            "Models feature is disabled for this tenant",
        ),
        (
            "mcp_servers",
            "/v1/proxy/mcp/srv-features-{t}",
            json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
            "MCP servers feature is disabled for this tenant",
        ),
        (
            "virtual_mcp_servers",
            "/v1/proxy/vmcp/vs-features",
            json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
            "Virtual MCP servers feature is disabled for this tenant",
        ),
        (
            "agent_skills",
            "/v1/proxy/skills/execute",
            json!({"skill_name": "nothing", "input_params": {}}),
            "Agent skills feature is disabled for this tenant",
        ),
        (
            "a2a",
            "/v1/proxy/a2a/outbound",
            json!({"target_card_id": "card-none", "capability": "x.y", "message": {}}),
            "A2A feature is disabled for this tenant",
        ),
    ];
    for (key, path, body, sentence) in surfaces {
        for on in [false, true] {
            let t = format!("t-feat-{key}-{on}");
            let path = path.replace("{t}", &t);
            let api_key = world(&pool, &t, json!({ key: on })).await;
            let (status, answer) = send(&pool, &t, &api_key, &path, body.clone()).await;
            if on {
                assert!(!answer.contains(sentence), "{key} on: {status} {answer}");
            } else {
                assert_eq!(status, StatusCode::FORBIDDEN, "{key}: {answer}");
                assert!(answer.contains(sentence), "{key}: {answer}");
            }
        }
    }
}
