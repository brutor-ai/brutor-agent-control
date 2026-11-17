// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Accepted-caller enforcement, and the outbound A2A task
//! state, against a real Postgres and a wiremock service.
//!
//! The topology: a shared service `sys-svc` whose agent card `card-svc`
//! resolves to it through the root's declared `shared_service` edge; it
//! lists `sys-root` as its only accepted client. Callers hold keys bound to
//! `sys-root` (listed), `sys-stranger` (an AI System it did not list) and
//! `grp-plain` (a group under no AI System).
//!
//! Every refusal asserts three things: the 403, the audit row with subtype
//! `unaccepted_caller`, and ZERO requests at the service — the gate decides
//! before anything leaves.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::middleware;
use brutor_gateway_core::routes;
use brutor_gateway_core::services::SharedState;
use brutor_gateway_core::services::cache::ResourceCache;

use super::common;

const T: &str = "t-accept";
const SVC: &str = "sys-svc";
const ROOT: &str = "sys-root";
const STRANGER: &str = "sys-stranger";
const PLAIN: &str = "grp-plain";
const CARD: &str = "card-svc";
const CARD_NAME: &str = "svc-agent";

async fn seed_system(pool: &PgPool, id: &str) {
    sqlx::query(
        "INSERT INTO resource_groups (id, tenant_id, hierarchy_level, inherit_from_parent, \
         name, is_active, is_system, group_type, system_kind) \
         VALUES ($1, $2, 0, true, $1, true, false, 'AI_SYSTEM', 'agent')",
    )
    .bind(id)
    .bind(T)
    .execute(pool)
    .await
    .expect("seed system");
}

struct Keys {
    root: String,
    stranger: String,
    plain: String,
}

/// The service enforces (or not) and lists only `sys-root`.
async fn topology(pool: &PgPool, upstream: &str, enforce: bool) -> Keys {
    common::seed_tenant(pool, T).await;
    for sys in [SVC, ROOT, STRANGER] {
        seed_system(pool, sys).await;
    }
    common::seed_resource_group(pool, PLAIN, T).await;
    sqlx::query(
        "UPDATE resource_groups SET intended_client_systems = $2, enforce_accepted_callers = $3 \
         WHERE id = $1",
    )
    .bind(SVC)
    .bind(json!([ROOT]))
    .bind(enforce)
    .execute(pool)
    .await
    .expect("service config");

    common::seed_agent_card(pool, CARD, T, CARD_NAME).await;
    sqlx::query("UPDATE agent_cards SET card_json = $2 WHERE id = $1")
        .bind(CARD)
        .bind(json!({"name": CARD_NAME, "url": upstream, "capabilities": [{"name": "echo"}]}))
        .execute(pool)
        .await
        .expect("card url");
    sqlx::query(
        "INSERT INTO agent_card_capabilities (id, agent_card_id, capability_name, handler_type, enabled) \
         VALUES ('cap-accept', $1, 'echo', 'noop', true)",
    )
    .bind(CARD)
    .execute(pool)
    .await
    .expect("capability");
    for g in [ROOT, STRANGER, PLAIN, SVC] {
        common::bind_agent_card_to_rg(pool, CARD, g).await;
    }
    // The root's declared edge is what makes `card-svc` the service.
    sqlx::query(
        "INSERT INTO ai_system_dependencies \
         (id, tenant_id, ai_system_id, role, depends_on_system_id, agent_card_id) \
         VALUES ('dep-accept', $1, $2, 'shared_service', $3, $4)",
    )
    .bind(T)
    .bind(ROOT)
    .bind(SVC)
    .bind(CARD)
    .execute(pool)
    .await
    .expect("edge");

    Keys {
        root: common::seed_api_key(pool, T, ROOT, None, "root-key").await,
        stranger: common::seed_api_key(pool, T, STRANGER, None, "stranger-key").await,
        plain: common::seed_api_key(pool, T, PLAIN, None, "plain-key").await,
    }
}

async fn service(reply: Value) -> MockServer {
    let upstream = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(reply))
        .mount(&upstream)
        .await;
    upstream
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

fn outbound(key: &str) -> Request<Body> {
    common::build_post_request(
        "/v1/proxy/a2a/outbound",
        json!({"target_card_id": CARD, "capability": "echo", "message": {"text": "hi"}}),
        Some(key),
        Some(T),
    )
}

/// The newest row the writer flushed with this subtype (batched writer).
async fn row(pool: &PgPool, subtype: &str) -> Option<(i32, Option<String>, Option<String>)> {
    for _ in 0..60 {
        let r: Option<(Option<i32>, Option<String>, Option<String>)> = sqlx::query_as(
            "SELECT http_status_code, root_task_id, a2a_task_state FROM proxy_logs \
             WHERE tenant_id = $1 AND proxy_subtype = $2 ORDER BY timestamp DESC LIMIT 1",
        )
        .bind(T)
        .bind(subtype)
        .fetch_optional(pool)
        .await
        .expect("proxy_logs query");
        if let Some((status, root, state)) = r {
            return Some((status.unwrap_or(0), root, state));
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    None
}

/// Every `unaccepted_caller` row names the agent card in `server_id`, like
/// every other A2A row — the control plane counts enforced refusals by it.
async fn assert_refusals_name_the_card(pool: &PgPool) {
    let rows: Vec<(Option<String>,)> = sqlx::query_as(
        "SELECT server_id FROM proxy_logs WHERE tenant_id = $1 AND proxy_subtype = 'unaccepted_caller'",
    )
    .bind(T)
    .fetch_all(pool)
    .await
    .expect("server_id query");
    assert!(!rows.is_empty());
    for (server_id,) in rows {
        assert_eq!(
            server_id.as_deref(),
            Some(CARD),
            "refusal row must carry server_id = card id"
        );
    }
}

async fn hits(upstream: &MockServer) -> usize {
    upstream.received_requests().await.map_or(0, |r| r.len())
}

async fn body_json(resp: axum::response::Response) -> Value {
    let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20)
        .await
        .expect("body");
    serde_json::from_slice(&bytes).unwrap_or(Value::Null)
}

async fn assert_refused_outbound(
    pool: &PgPool,
    state: &SharedState,
    upstream: &MockServer,
    key: &str,
) {
    let resp = a2a_app(state).oneshot(outbound(key)).await.expect("router");
    assert_eq!(resp.status(), StatusCode::FORBIDDEN);
    let body = body_json(resp).await;
    let msg = body["error"]["message"].as_str().unwrap_or_default();
    assert!(
        msg.starts_with("unaccepted_caller:"),
        "canonical envelope: {body}"
    );
    let (status, root, task_state) = row(pool, "unaccepted_caller")
        .await
        .expect("the refusal is audited with subtype unaccepted_caller");
    assert_eq!(status, 403);
    assert!(root.is_some(), "the refusal lands on the run it stopped");
    assert!(task_state.is_none(), "nothing was answered");
    assert_eq!(
        hits(upstream).await,
        0,
        "a refused call must never reach the service"
    );
    assert_refusals_name_the_card(pool).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_listed_caller_reaches_the_service_and_its_task_state_is_recorded(pool: PgPool) {
    let upstream =
        service(json!({"task": {"id": "t1", "status": {"state": "TASK_STATE_COMPLETED"}}})).await;
    let keys = topology(&pool, &upstream.uri(), true).await;
    let state = common::build_test_state(pool.clone());

    let resp = a2a_app(&state)
        .oneshot(outbound(&keys.root))
        .await
        .expect("router");
    assert_eq!(resp.status(), StatusCode::OK);
    assert_eq!(hits(&upstream).await, 1);
    let (status, _, task_state) = row(&pool, "outbound").await.expect("outbound row");
    assert_eq!(status, 200);
    assert_eq!(task_state.as_deref(), Some("completed"));
    assert!(row(&pool, "unaccepted_caller").await.is_none());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unlisted_caller_system_is_refused_before_the_service_is_contacted(pool: PgPool) {
    let upstream = service(json!({"task": {"status": {"state": "completed"}}})).await;
    let keys = topology(&pool, &upstream.uri(), true).await;
    let state = common::build_test_state(pool.clone());
    assert_refused_outbound(&pool, &state, &upstream, &keys.stranger).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_caller_in_no_ai_system_is_refused(pool: PgPool) {
    let upstream = service(json!({"task": {"status": {"state": "completed"}}})).await;
    let keys = topology(&pool, &upstream.uri(), true).await;
    let state = common::build_test_state(pool.clone());
    assert_refused_outbound(&pool, &state, &upstream, &keys.plain).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn with_enforcement_off_an_unlisted_caller_is_advisory_only(pool: PgPool) {
    let upstream = service(json!({"task": {"status": {"state": "completed"}}})).await;
    let keys = topology(&pool, &upstream.uri(), false).await;
    let state = common::build_test_state(pool.clone());
    for key in [&keys.stranger, &keys.plain] {
        let resp = a2a_app(&state)
            .oneshot(outbound(key))
            .await
            .expect("router");
        assert_eq!(resp.status(), StatusCode::OK);
    }
    assert_eq!(hits(&upstream).await, 2);
    assert!(row(&pool, "unaccepted_caller").await.is_none());
}

/// The gate reads cached configuration. A change is invisible until the
/// cache is told (the TTL is the only other way out) — and visible at once
/// after the tenant's group entries drop, which is what a `resource_group`
/// or `ai_system_dependency` invalidation does.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_config_change_takes_effect_on_invalidation(pool: PgPool) {
    let upstream = service(json!({"task": {"status": {"state": "completed"}}})).await;
    let keys = topology(&pool, &upstream.uri(), true).await;
    let state = common::build_test_state(pool.clone());
    let call = |key: &str| a2a_app(&state).oneshot(outbound(key));

    assert_eq!(
        call(&keys.stranger).await.unwrap().status(),
        StatusCode::FORBIDDEN
    );

    // The service's owner accepts the stranger.
    sqlx::query("UPDATE resource_groups SET intended_client_systems = $2 WHERE id = $1")
        .bind(SVC)
        .bind(json!([ROOT, STRANGER]))
        .execute(&pool)
        .await
        .unwrap();
    assert_eq!(
        call(&keys.stranger).await.unwrap().status(),
        StatusCode::FORBIDDEN,
        "still the cached config — proves the gate reads the cache"
    );
    state.group_cache.invalidate_tenant(T).await;
    assert_eq!(call(&keys.stranger).await.unwrap().status(), StatusCode::OK);
    assert_eq!(hits(&upstream).await, 1);

    // The edge that identified the card as the service goes away: the card
    // is no longer a Brutor service, so there is nothing to enforce.
    sqlx::query("UPDATE resource_groups SET intended_client_systems = '[]' WHERE id = $1")
        .bind(SVC)
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("DELETE FROM ai_system_dependencies WHERE id = 'dep-accept'")
        .execute(&pool)
        .await
        .unwrap();
    state.group_cache.invalidate_tenant(T).await;
    assert_eq!(call(&keys.plain).await.unwrap().status(), StatusCode::OK);
    assert_eq!(hits(&upstream).await, 2);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_brutor_hosted_service_refuses_an_unaccepted_caller_inbound(pool: PgPool) {
    let upstream = service(json!({})).await;
    let keys = topology(&pool, &upstream.uri(), true).await;
    let state = common::build_test_state(pool.clone());

    // v0.3 inbound.
    let req = common::build_post_request(
        "/v1/proxy/a2a/inbound",
        json!({"target_card_id": CARD, "capability": "echo", "message": {"text": "hi"}}),
        Some(&keys.stranger),
        Some(T),
    );
    let resp = a2a_app(&state).oneshot(req).await.expect("router");
    assert_eq!(resp.status(), StatusCode::FORBIDDEN);
    let (status, _, _) = row(&pool, "unaccepted_caller")
        .await
        .expect("inbound refusal row");
    assert_eq!(status, 403);
    assert_refusals_name_the_card(&pool).await;

    // v1.0 message:send — spec-shaped PolicyBlocked (HTTP 403).
    sqlx::query("DELETE FROM proxy_logs WHERE tenant_id = $1")
        .bind(T)
        .execute(&pool)
        .await
        .unwrap();
    let req = common::build_post_request(
        &format!("/a2a/{T}/{CARD_NAME}/message:send"),
        json!({"message": {"messageId": "m-1", "role": "ROLE_USER", "parts": [{"text": "hi"}]}}),
        Some(&keys.stranger),
        Some(T),
    );
    let resp = common::build_a2a_v1_app(state.clone())
        .oneshot(req)
        .await
        .expect("router");
    assert_eq!(resp.status(), StatusCode::FORBIDDEN);
    let (status, _, _) = row(&pool, "unaccepted_caller")
        .await
        .expect("v1 refusal row");
    assert_eq!(status, 403);
    assert_refusals_name_the_card(&pool).await;

    // The listed caller is served on both.
    let req = common::build_post_request(
        "/v1/proxy/a2a/inbound",
        json!({"target_card_id": CARD, "capability": "echo", "message": {"text": "hi"}}),
        Some(&keys.root),
        Some(T),
    );
    let resp = a2a_app(&state).oneshot(req).await.expect("router");
    assert!(
        resp.status().is_success(),
        "listed caller inbound: {}",
        resp.status()
    );
    assert_eq!(hits(&upstream).await, 0, "inbound never calls out");
}

/// Task-state capture per reply shape, end to end through the outbound
/// route: what lands in `proxy_logs.a2a_task_state` for each dialect.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_task_state_is_captured_for_every_reply_shape(pool: PgPool) {
    let cases: Vec<(ResponseTemplate, Option<&str>)> = vec![
        (
            ResponseTemplate::new(200).set_body_json(
                json!({"jsonrpc": "2.0", "id": 1, "result": {"status": {"state": "input-required"}}}),
            ),
            Some("input_required"),
        ),
        (
            ResponseTemplate::new(200).set_body_json(
                json!({"jsonrpc": "2.0", "id": 1, "result": {"task": {"status": {"state": "TASK_STATE_FAILED"}}}}),
            ),
            Some("failed"),
        ),
        (
            ResponseTemplate::new(200)
                .set_body_json(json!({"task": {"id": "t", "status": {"state": "TASK_STATE_REJECTED"}}})),
            Some("rejected"),
        ),
        (
            ResponseTemplate::new(200).set_body_raw(
                "data: {\"statusUpdate\":{\"status\":{\"state\":\"TASK_STATE_WORKING\"}}}\n\n\
                 data: {\"statusUpdate\":{\"status\":{\"state\":\"TASK_STATE_COMPLETED\"},\"final\":true}}\n\n",
                "text/event-stream",
            ),
            Some("completed"),
        ),
        (
            ResponseTemplate::new(200)
                .set_body_json(json!({"message": {"role": "ROLE_AGENT", "parts": [{"text": "ok"}]}})),
            None,
        ),
    ];
    let mut keys = None;
    for (i, (reply, expected)) in cases.into_iter().enumerate() {
        let upstream = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(reply)
            .mount(&upstream)
            .await;
        if keys.is_none() {
            keys = Some(topology(&pool, &upstream.uri(), false).await);
        } else {
            sqlx::query("UPDATE agent_cards SET card_json = $2 WHERE id = $1")
                .bind(CARD)
                .bind(json!({"name": CARD_NAME, "url": upstream.uri(), "capabilities": [{"name": "echo"}]}))
                .execute(&pool)
                .await
                .unwrap();
        }
        sqlx::query("DELETE FROM proxy_logs WHERE tenant_id = $1")
            .bind(T)
            .execute(&pool)
            .await
            .unwrap();
        // Fresh state per case: the card (and its URL) is cached.
        let state = common::build_test_state(pool.clone());
        let key = &keys.as_ref().unwrap().root;
        let resp = a2a_app(&state)
            .oneshot(outbound(key))
            .await
            .expect("router");
        assert_eq!(resp.status(), StatusCode::OK, "case {i}");
        let (_, _, state_col) = row(&pool, "outbound").await.expect("outbound row");
        assert_eq!(state_col.as_deref(), expected, "case {i}");
    }
}
