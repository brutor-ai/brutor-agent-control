// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A group's A2A policy and an agent card's own policy and limits apply on
//! every A2A route: the outbound proxy, the legacy inbound route and the
//! A2A v1.0 routes.
//!
//! One policy document shape, three places it can be set (the caller's
//! resource group, any ancestor of it, the agent card), one evaluation:
//!
//! * `capability.default_access` — `disabled` refuses every call,
//!   `approval_required` holds a call that does work for a person, anything
//!   that is not a known value refuses;
//! * `data.deny_classifications` — a call to a capability carrying one of
//!   the tags is refused;
//! * `remote.allowed_globs` — the address an outbound call leaves for must
//!   match the list of every level that states one. An inbound call has no
//!   remote address, so the list says nothing about it.
//!
//! The strictest level wins. Each test here drives a route over HTTP.

use axum::http::StatusCode;
use axum::{body::Body, http::Request};
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::services::SharedState;
use brutor_gateway_core::{middleware, routes};

use crate::common;

const T: &str = "t-a2agov";
const RG: &str = "rg-a2agov";
const PARENT: &str = "rg-a2agov-parent";
const CARD: &str = "card-a2agov";
const CARD_NAME: &str = "a2agov-agent";
const CAP: &str = "echo";

/// Tenant, group, key and a card bound to the group with one `noop`
/// capability tagged `PII`. The card's address is `upstream`.
async fn topology(pool: &PgPool, upstream: &str) -> String {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_end_user(pool, 9971, T).await;
    common::add_direct_membership(pool, 9971, RG, T).await;
    common::seed_agent_card(pool, CARD, T, CARD_NAME).await;
    sqlx::query("UPDATE agent_cards SET card_json = $2 WHERE id = $1")
        .bind(CARD)
        .bind(json!({"name": CARD_NAME, "url": upstream, "capabilities": [{"name": CAP}]}))
        .execute(pool)
        .await
        .expect("card url");
    common::bind_agent_card_to_rg(pool, CARD, RG).await;
    sqlx::query(
        "INSERT INTO agent_card_capabilities \
         (id, agent_card_id, capability_name, handler_type, data_classification, enabled) \
         VALUES ('cap-a2agov', $1, $2, 'noop', $3, true)",
    )
    .bind(CARD)
    .bind(CAP)
    .bind(json!(["PII"]))
    .execute(pool)
    .await
    .expect("capability");
    common::seed_api_key(pool, T, RG, Some(9971), "a2agov-key").await
}

async fn set_group_policy(pool: &PgPool, group: &str, policy: Value) {
    sqlx::query("UPDATE resource_groups SET a2a_global_governance = $2 WHERE id = $1")
        .bind(group)
        .bind(policy)
        .execute(pool)
        .await
        .expect("group policy");
}

async fn set_card_policy(pool: &PgPool, policy: Value) {
    sqlx::query("UPDATE agent_cards SET tenant_governance = $2 WHERE id = $1")
        .bind(CARD)
        .bind(policy)
        .execute(pool)
        .await
        .expect("card policy");
}

async fn set_card_limits(pool: &PgPool, limits: Value) {
    sqlx::query("UPDATE agent_cards SET tenant_limits = $2 WHERE id = $1")
        .bind(CARD)
        .bind(limits)
        .execute(pool)
        .await
        .expect("card limits");
}

async fn remote(reply: Value) -> MockServer {
    let upstream = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(reply))
        .mount(&upstream)
        .await;
    upstream
}

async fn completed_remote() -> MockServer {
    remote(json!({"task": {"id": "t1", "status": {"state": "TASK_STATE_COMPLETED"}}})).await
}

async fn hits(upstream: &MockServer) -> usize {
    upstream.received_requests().await.map_or(0, |r| r.len())
}

fn legacy_app(state: &SharedState) -> axum::Router {
    axum::Router::new()
        .nest("/v1/proxy/a2a", routes::a2a::router())
        .layer(middleware::tenant::TenantLayer::new(state.clone()))
        .layer(middleware::run_end::RunEndLayer::new(
            state.mcp_sessions.clone(),
        ))
        .with_state(state.clone())
}

/// The three routes that do work.
#[derive(Clone, Copy, Debug, PartialEq)]
enum Route {
    Outbound,
    LegacyInbound,
    V1Send,
}

const ROUTES: [Route; 3] = [Route::Outbound, Route::LegacyInbound, Route::V1Send];

fn request(route: Route, key: &str, token: Option<&str>) -> Request<Body> {
    let mut req = match route {
        Route::Outbound => common::build_post_request(
            "/v1/proxy/a2a/outbound",
            json!({"target_card_id": CARD, "capability": CAP, "message": {"text": "hi"}}),
            Some(key),
            Some(T),
        ),
        Route::LegacyInbound => common::build_post_request(
            "/v1/proxy/a2a/inbound",
            json!({"target_card_id": CARD, "capability": CAP, "message": {"text": "hi"}}),
            Some(key),
            Some(T),
        ),
        Route::V1Send => common::build_post_request(
            &format!("/a2a/{T}/{CARD_NAME}/message:send"),
            json!({"message": {"messageId": ulid::Ulid::new().to_string(), "role": "ROLE_USER",
                               "parts": [{"text": "hi"}]}}),
            Some(key),
            Some(T),
        ),
    };
    if let Some(t) = token {
        req.headers_mut()
            .insert("x-approval-token", t.parse().expect("token header"));
    }
    req
}

async fn call(
    state: &SharedState,
    route: Route,
    key: &str,
    token: Option<&str>,
) -> (StatusCode, Value) {
    let app = match route {
        Route::V1Send => common::build_a2a_v1_app(state.clone()),
        _ => legacy_app(state),
    };
    let resp = app
        .oneshot(request(route, key, token))
        .await
        .expect("router");
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20)
        .await
        .expect("body");
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

/// The first value of `key` anywhere in `v`.
fn find<'a>(v: &'a Value, key: &str) -> Option<&'a Value> {
    match v {
        Value::Object(m) => m.get(key).or_else(|| m.values().find_map(|x| find(x, key))),
        Value::Array(a) => a.iter().find_map(|x| find(x, key)),
        _ => None,
    }
}

fn text(v: &Value) -> String {
    v.to_string().to_lowercase()
}

/// Every route passes when no policy is set anywhere: the baseline the
/// refusals below are measured against.
#[sqlx::test(migrations = "./tests/migrations")]
async fn with_no_policy_every_route_passes(pool: PgPool) {
    let upstream = completed_remote().await;
    let key = topology(&pool, &upstream.uri()).await;
    let state = common::build_test_state(pool.clone());
    for route in ROUTES {
        let (status, body) = call(&state, route, &key, None).await;
        assert_eq!(status, StatusCode::OK, "{route:?}: {body}");
    }
    assert_eq!(hits(&upstream).await, 1);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_group_that_disables_a2a_is_refused_on_every_route(pool: PgPool) {
    let upstream = completed_remote().await;
    let key = topology(&pool, &upstream.uri()).await;
    set_group_policy(
        &pool,
        RG,
        json!({"capability": {"default_access": "disabled"}}),
    )
    .await;
    let state = common::build_test_state(pool.clone());
    for route in ROUTES {
        let (status, body) = call(&state, route, &key, None).await;
        assert_eq!(status, StatusCode::FORBIDDEN, "{route:?}: {body}");
        assert!(text(&body).contains("default_access=disabled"), "{body}");
    }
    assert_eq!(hits(&upstream).await, 0);
}

/// A reading verb of the v1.0 routes is refused too: `disabled` is an
/// access decision about the group's A2A use, not about one message.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_group_that_disables_a2a_cannot_list_tasks(pool: PgPool) {
    let key = topology(&pool, "http://127.0.0.1:9").await;
    set_group_policy(
        &pool,
        RG,
        json!({"capability": {"default_access": "disabled"}}),
    )
    .await;
    let state = common::build_test_state(pool.clone());
    let resp = common::build_a2a_v1_app(state)
        .oneshot(common::build_get_request(
            &format!("/a2a/{T}/{CARD_NAME}/tasks"),
            Some(&key),
            Some(T),
        ))
        .await
        .expect("router");
    assert_eq!(resp.status(), StatusCode::FORBIDDEN);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_denied_classification_is_refused_on_every_route(pool: PgPool) {
    let upstream = completed_remote().await;
    let key = topology(&pool, &upstream.uri()).await;
    // The tag compares without case.
    set_group_policy(
        &pool,
        RG,
        json!({"data": {"deny_classifications": ["pii"]}}),
    )
    .await;
    let state = common::build_test_state(pool.clone());
    for route in ROUTES {
        let (status, body) = call(&state, route, &key, None).await;
        assert_eq!(status, StatusCode::FORBIDDEN, "{route:?}: {body}");
        assert!(text(&body).contains("denies data classification"), "{body}");
    }
    assert_eq!(hits(&upstream).await, 0);
}

/// On the v1.0 routes the message goes to the capability the gateway
/// dispatches: its tags are the ones judged, not another capability's.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_v1_message_is_judged_by_the_capability_it_is_dispatched_to(pool: PgPool) {
    let key = topology(&pool, "http://127.0.0.1:9").await;
    // A second capability, bound to a handler and carrying no denied tag:
    // it is the one a v1.0 message is dispatched to.
    sqlx::query(
        "INSERT INTO agent_card_capabilities \
         (id, agent_card_id, capability_name, handler_type, handler_ref, data_classification, enabled) \
         VALUES ('cap-a2agov-demo', $1, 'demo', 'multi_turn_demo', 'demo', $2, true)",
    )
    .bind(CARD)
    .bind(json!(["PUBLIC"]))
    .execute(&pool)
    .await
    .expect("bound capability");
    set_group_policy(
        &pool,
        RG,
        json!({"data": {"deny_classifications": ["pii"]}}),
    )
    .await;
    let state = common::build_test_state(pool.clone());
    let (status, body) = call(&state, Route::V1Send, &key, None).await;
    assert_eq!(status, StatusCode::OK, "{body}");

    // Deny the bound capability's tag: now the message is refused.
    set_group_policy(
        &pool,
        RG,
        json!({"data": {"deny_classifications": ["public"]}}),
    )
    .await;
    let state = common::build_test_state(pool.clone());
    let (status, body) = call(&state, Route::V1Send, &key, None).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
}

/// An allow-list names the addresses an OUTBOUND call may leave for. An
/// inbound call has no remote address: the list does not refuse it.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_allow_list_refuses_no_inbound_call(pool: PgPool) {
    let upstream = completed_remote().await;
    let key = topology(&pool, &upstream.uri()).await;
    set_group_policy(
        &pool,
        RG,
        json!({"remote": {"allowed_globs": ["https://partner.example/*"]}}),
    )
    .await;
    let state = common::build_test_state(pool.clone());
    for route in [Route::LegacyInbound, Route::V1Send] {
        let (status, body) = call(&state, route, &key, None).await;
        assert_eq!(status, StatusCode::OK, "{route:?}: {body}");
    }
    // The outbound call leaves for an address the list does not name.
    let (status, body) = call(&state, Route::Outbound, &key, None).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert!(text(&body).contains("allow-list"), "{body}");
    assert_eq!(hits(&upstream).await, 0);
}

/// A parent and a child that each state an allow-list: the address must
/// match both. Two lists with no pattern in common used to merge to an
/// empty list, which restricts nothing.
#[sqlx::test(migrations = "./tests/migrations")]
async fn two_levels_with_different_allow_lists_do_not_cancel_out(pool: PgPool) {
    let upstream = completed_remote().await;
    let key = topology(&pool, &upstream.uri()).await;
    common::seed_resource_group(&pool, PARENT, T).await;
    common::set_rg_parent(&pool, RG, PARENT).await;
    set_group_policy(
        &pool,
        PARENT,
        json!({"remote": {"allowed_globs": ["https://a.example/*"]}}),
    )
    .await;
    set_group_policy(
        &pool,
        RG,
        json!({"remote": {"allowed_globs": ["https://b.example/*"]}}),
    )
    .await;
    let state = common::build_test_state(pool.clone());
    let (status, body) = call(&state, Route::Outbound, &key, None).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert_eq!(hits(&upstream).await, 0);
}

/// The address matches the child's list and the parent's: it passes.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_address_on_every_levels_allow_list_passes(pool: PgPool) {
    let upstream = completed_remote().await;
    let key = topology(&pool, &upstream.uri()).await;
    common::seed_resource_group(&pool, PARENT, T).await;
    common::set_rg_parent(&pool, RG, PARENT).await;
    set_group_policy(
        &pool,
        PARENT,
        json!({"remote": {"allowed_globs": ["http://127.0.0.1*"]}}),
    )
    .await;
    set_group_policy(
        &pool,
        RG,
        json!({"remote": {"allowed_globs": [format!("{}*", upstream.uri())]}}),
    )
    .await;
    let state = common::build_test_state(pool.clone());
    let (status, body) = call(&state, Route::Outbound, &key, None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(hits(&upstream).await, 1);
}

/// A value the gateway does not know is the most restrictive one.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unknown_access_value_refuses(pool: PgPool) {
    let upstream = completed_remote().await;
    let key = topology(&pool, &upstream.uri()).await;
    set_group_policy(
        &pool,
        RG,
        json!({"capability": {"default_access": "audit_only"}}),
    )
    .await;
    let state = common::build_test_state(pool.clone());
    for route in ROUTES {
        let (status, body) = call(&state, route, &key, None).await;
        assert_eq!(status, StatusCode::FORBIDDEN, "{route:?}: {body}");
    }
    assert_eq!(hits(&upstream).await, 0);
}

/// An allow-list whose only pattern cannot be read allows nothing. It used
/// to be dropped, which left the call unrestricted.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_allow_list_that_cannot_be_read_allows_nothing(pool: PgPool) {
    let upstream = completed_remote().await;
    let key = topology(&pool, &upstream.uri()).await;
    set_group_policy(
        &pool,
        RG,
        json!({"remote": {"allowed_globs": ["http://[bad"]}}),
    )
    .await;
    let state = common::build_test_state(pool.clone());
    let (status, body) = call(&state, Route::Outbound, &key, None).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert_eq!(hits(&upstream).await, 0);
}

// ---------------------------------------------------------------------------
// approval_required holds the call
// ---------------------------------------------------------------------------

async fn pending_hold(pool: &PgPool) -> Option<(String, i32, Option<String>)> {
    sqlx::query_as(
        "SELECT id, timeout_seconds, originating_trace_root_id FROM tool_approval_requests \
         WHERE tenant_id = $1 AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
    )
    .bind(T)
    .fetch_optional(pool)
    .await
    .expect("approval rows")
}

async fn approve(pool: &PgPool, approval_id: &str, token: &str) {
    sqlx::query(
        "UPDATE tool_approval_requests \
         SET status = 'approved', approval_token = $2, resolved_at = now() WHERE id = $1",
    )
    .bind(approval_id)
    .bind(token)
    .execute(pool)
    .await
    .expect("approve");
}

async fn holds(pool: &PgPool) -> i64 {
    sqlx::query_scalar("SELECT COUNT(*) FROM tool_approval_requests WHERE tenant_id = $1")
        .bind(T)
        .fetch_one(pool)
        .await
        .expect("count holds")
}

async fn approval_required_holds_and_the_approved_retry_proceeds(pool: PgPool, route: Route) {
    let upstream = completed_remote().await;
    let key = topology(&pool, &upstream.uri()).await;
    set_group_policy(
        &pool,
        RG,
        json!({"capability": {"default_access": "approval_required",
                              "approval_timeout_seconds": 900}}),
    )
    .await;
    let state = common::build_test_state(pool.clone());

    let (status, body) = call(&state, route, &key, None).await;
    // Held, not refused: a request is waiting for a person.
    let (hold_id, window, run) = pending_hold(&pool)
        .await
        .unwrap_or_else(|| panic!("{route:?}: no approval was requested ({status}: {body})"));
    assert_eq!(window, 900, "the policy's own window is the hold's");
    assert!(run.is_some(), "the hold names the run it holds");
    assert_eq!(hits(&upstream).await, 0, "a held call does no work");
    match route {
        // The legacy routes answer the approval envelope (202 for a key).
        Route::Outbound | Route::LegacyInbound => {
            assert_eq!(status, StatusCode::ACCEPTED, "{body}");
            assert_eq!(body["approval_request_id"], json!(hold_id), "{body}");
        }
        // The v1.0 routes answer in their own error shape, naming the hold.
        Route::V1Send => {
            assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
            assert_eq!(
                find(&body, "approvalRequestId"),
                Some(&json!(hold_id)),
                "{body}"
            );
        }
    }

    // A person approves; the caller sends the call again with the token.
    approve(&pool, &hold_id, "tok-a2agov").await;
    let (status, body) = call(&state, route, &key, Some("tok-a2agov")).await;
    assert_eq!(status, StatusCode::OK, "{route:?}: {body}");
    assert_eq!(
        holds(&pool).await,
        1,
        "the approved retry raises no new hold"
    );
    if route == Route::Outbound {
        assert_eq!(hits(&upstream).await, 1);
    }

    // The token is spent: the same call without one is held again.
    let (status, _) = call(&state, route, &key, Some("tok-a2agov")).await;
    assert_ne!(status, StatusCode::OK, "a spent token approves nothing");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn approval_required_holds_an_outbound_call(pool: PgPool) {
    approval_required_holds_and_the_approved_retry_proceeds(pool, Route::Outbound).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn approval_required_holds_a_legacy_inbound_call(pool: PgPool) {
    approval_required_holds_and_the_approved_retry_proceeds(pool, Route::LegacyInbound).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn approval_required_holds_a_v1_message(pool: PgPool) {
    approval_required_holds_and_the_approved_retry_proceeds(pool, Route::V1Send).await;
}

/// An agent whose own grant asks for an approval, in a group whose policy
/// asks for one too: the question is the same ("may this caller call this
/// card"), so it is asked once, and one approval lets the call through.
async fn one_approval_answers_the_grant_and_the_policy(pool: PgPool, route: Route) {
    let upstream = completed_remote().await;
    let key = topology(&pool, &upstream.uri()).await;
    sqlx::query(
        "INSERT INTO agent_identities (id, tenant_id, name, source, enabled, status, default_deny) \
         VALUES ('agent-a2agov', $1, 'agent-a2agov', 'manual', true, 'active', true)",
    )
    .bind(T)
    .execute(&pool)
    .await
    .expect("agent identity");
    sqlx::query(
        "INSERT INTO agent_grants (id, tenant_id, agent_id, action_type, target, effect, enabled) \
         VALUES ('g-a2agov', $1, 'agent-a2agov', 'a2a_call', $2, 'approval_required', true)",
    )
    .bind(T)
    .bind(CARD)
    .execute(&pool)
    .await
    .expect("agent grant");
    sqlx::query("UPDATE api_keys SET agent_id = 'agent-a2agov' WHERE tenant_id = $1")
        .bind(T)
        .execute(&pool)
        .await
        .expect("bind key to agent");
    set_group_policy(
        &pool,
        RG,
        json!({"capability": {"default_access": "approval_required"}}),
    )
    .await;
    let state = common::build_test_state(pool.clone());

    let (status, body) = call(&state, route, &key, None).await;
    assert_ne!(status, StatusCode::OK, "{body}");
    assert_eq!(holds(&pool).await, 1, "one question, one hold: {body}");
    let (hold_id, _, _) = pending_hold(&pool).await.expect("a hold");

    approve(&pool, &hold_id, "tok-once").await;
    let (status, body) = call(&state, route, &key, Some("tok-once")).await;
    assert_eq!(status, StatusCode::OK, "{route:?}: {body}");
    assert_eq!(
        holds(&pool).await,
        1,
        "the approved retry raises no second hold"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn one_approval_answers_the_grant_and_the_policy_outbound(pool: PgPool) {
    one_approval_answers_the_grant_and_the_policy(pool, Route::Outbound).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn one_approval_answers_the_grant_and_the_policy_on_v1(pool: PgPool) {
    one_approval_answers_the_grant_and_the_policy(pool, Route::V1Send).await;
}

/// Reading a task is no work: it is not held, so the caller of a held
/// message can still read and cancel what it has.
#[sqlx::test(migrations = "./tests/migrations")]
async fn approval_required_does_not_hold_a_read(pool: PgPool) {
    let key = topology(&pool, "http://127.0.0.1:9").await;
    set_group_policy(
        &pool,
        RG,
        json!({"capability": {"default_access": "approval_required"}}),
    )
    .await;
    let state = common::build_test_state(pool.clone());
    let resp = common::build_a2a_v1_app(state)
        .oneshot(common::build_get_request(
            &format!("/a2a/{T}/{CARD_NAME}/tasks"),
            Some(&key),
            Some(T),
        ))
        .await
        .expect("router");
    assert_eq!(resp.status(), StatusCode::OK);
    assert_eq!(holds(&pool).await, 0);
}

// ---------------------------------------------------------------------------
// The card's own policy
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_cards_own_policy_is_applied_on_every_route(pool: PgPool) {
    let upstream = completed_remote().await;
    let key = topology(&pool, &upstream.uri()).await;
    set_card_policy(&pool, json!({"data": {"deny_classifications": ["PII"]}})).await;
    let state = common::build_test_state(pool.clone());
    for route in ROUTES {
        let (status, body) = call(&state, route, &key, None).await;
        assert_eq!(status, StatusCode::FORBIDDEN, "{route:?}: {body}");
        assert!(text(&body).contains("agent card policy"), "{body}");
    }
    assert_eq!(hits(&upstream).await, 0);
}

/// The group asks for an approval, the card disables: the stricter wins,
/// and no approval is requested for a call that is refused anyway.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_stricter_of_group_and_card_wins(pool: PgPool) {
    let upstream = completed_remote().await;
    let key = topology(&pool, &upstream.uri()).await;
    set_group_policy(
        &pool,
        RG,
        json!({"capability": {"default_access": "approval_required"}}),
    )
    .await;
    set_card_policy(&pool, json!({"capability": {"default_access": "disabled"}})).await;
    let state = common::build_test_state(pool.clone());
    for route in ROUTES {
        let (status, body) = call(&state, route, &key, None).await;
        assert_eq!(status, StatusCode::FORBIDDEN, "{route:?}: {body}");
        assert!(text(&body).contains("default_access=disabled"), "{body}");
    }
    assert_eq!(holds(&pool).await, 0);
    assert_eq!(hits(&upstream).await, 0);
}

/// The card holds, the group allows: held, with the card's window.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_cards_approval_required_holds(pool: PgPool) {
    let key = topology(&pool, "http://127.0.0.1:9").await;
    set_group_policy(
        &pool,
        RG,
        json!({"capability": {"default_access": "enabled", "approval_timeout_seconds": 5000}}),
    )
    .await;
    set_card_policy(
        &pool,
        json!({"capability": {"default_access": "approval_required",
                              "approval_timeout_seconds": 1200}}),
    )
    .await;
    let state = common::build_test_state(pool.clone());
    let (status, body) = call(&state, Route::LegacyInbound, &key, None).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    let (_, window, _) = pending_hold(&pool).await.expect("a hold");
    assert_eq!(window, 1200, "the shortest window any level states");
}

// ---------------------------------------------------------------------------
// The card's limits and a capability's hourly rate
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_cards_frequency_limit_counts_inbound_calls(pool: PgPool) {
    let key = topology(&pool, "http://127.0.0.1:9").await;
    set_card_limits(&pool, json!({"frequency": {"max_calls_per_hour": 2}})).await;
    let state = common::build_test_state(pool.clone());
    // One counter for the card, whatever route reaches it.
    let (status, body) = call(&state, Route::LegacyInbound, &key, None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let (status, body) = call(&state, Route::V1Send, &key, None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    for route in [Route::LegacyInbound, Route::V1Send] {
        let (status, body) = call(&state, route, &key, None).await;
        assert_eq!(status, StatusCode::TOO_MANY_REQUESTS, "{route:?}: {body}");
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_cards_invocation_cap_is_applied(pool: PgPool) {
    let upstream = completed_remote().await;
    let key = topology(&pool, &upstream.uri()).await;
    set_card_limits(&pool, json!({"invocations": {"daily_limit": 3}})).await;
    let state = common::build_test_state(pool.clone());
    for route in ROUTES {
        let (status, body) = call(&state, route, &key, None).await;
        assert_eq!(status, StatusCode::OK, "{route:?}: {body}");
    }
    for route in ROUTES {
        let (status, body) = call(&state, route, &key, None).await;
        assert_eq!(status, StatusCode::TOO_MANY_REQUESTS, "{route:?}: {body}");
    }
    assert_eq!(hits(&upstream).await, 1);
}

/// A capability's own hourly rate, on the routes that name the capability.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_capabilitys_hourly_rate_is_applied_on_the_legacy_routes(pool: PgPool) {
    let upstream = completed_remote().await;
    let key = topology(&pool, &upstream.uri()).await;
    sqlx::query(
        "UPDATE agent_card_capabilities SET rate_limit_per_hour = 2 WHERE agent_card_id = $1",
    )
    .bind(CARD)
    .execute(&pool)
    .await
    .expect("capability rate");
    let state = common::build_test_state(pool.clone());
    for route in [Route::Outbound, Route::LegacyInbound] {
        let (status, body) = call(&state, route, &key, None).await;
        assert_eq!(status, StatusCode::OK, "{route:?}: {body}");
    }
    for route in [Route::Outbound, Route::LegacyInbound] {
        let (status, body) = call(&state, route, &key, None).await;
        assert_eq!(status, StatusCode::TOO_MANY_REQUESTS, "{route:?}: {body}");
        assert!(text(&body).contains("capability"), "{body}");
    }
    assert_eq!(hits(&upstream).await, 1);
}

/// On the v1.0 routes the rate is that of the capability the message is
/// dispatched to.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_capabilitys_hourly_rate_is_applied_to_a_v1_message(pool: PgPool) {
    let key = topology(&pool, "http://127.0.0.1:9").await;
    sqlx::query(
        "INSERT INTO agent_card_capabilities \
         (id, agent_card_id, capability_name, handler_type, handler_ref, rate_limit_per_hour, enabled) \
         VALUES ('cap-a2agov-demo', $1, 'demo', 'multi_turn_demo', 'demo', 1, true)",
    )
    .bind(CARD)
    .execute(&pool)
    .await
    .expect("bound capability");
    let state = common::build_test_state(pool.clone());
    let (status, body) = call(&state, Route::V1Send, &key, None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let (status, body) = call(&state, Route::V1Send, &key, None).await;
    assert_eq!(status, StatusCode::TOO_MANY_REQUESTS, "{body}");
    assert!(text(&body).contains("capability"), "{body}");
}

// ---------------------------------------------------------------------------
// The caller's own plane: an agent's grants, a system's autonomy
// ---------------------------------------------------------------------------

/// Make the topology's key the key of an agent: default-deny, with no grant
/// unless the test adds one.
async fn bind_key_to_agent(pool: &PgPool, enforcement_mode: &str) {
    sqlx::query(
        "INSERT INTO agent_identities \
         (id, tenant_id, name, source, enabled, status, default_deny, enforcement_mode) \
         VALUES ('agent-a2agov', $1, 'agent-a2agov', 'manual', true, 'active', true, $2)",
    )
    .bind(T)
    .bind(enforcement_mode)
    .execute(pool)
    .await
    .expect("agent identity");
    sqlx::query("UPDATE api_keys SET agent_id = 'agent-a2agov' WHERE tenant_id = $1")
        .bind(T)
        .execute(pool)
        .await
        .expect("bind key to agent");
}

async fn grant(pool: &PgPool, effect: &str) {
    sqlx::query(
        "INSERT INTO agent_grants (id, tenant_id, agent_id, action_type, target, effect, enabled) \
         VALUES ('g-a2agov', $1, 'agent-a2agov', 'a2a_call', $2, $3, true)",
    )
    .bind(T)
    .bind(CARD)
    .bind(effect)
    .execute(pool)
    .await
    .expect("agent grant");
}

/// The subtypes of the rows written for this tenant, oldest first.
async fn subtypes(pool: &PgPool) -> Vec<String> {
    for _ in 0..40 {
        tokio::time::sleep(std::time::Duration::from_millis(25)).await;
    }
    sqlx::query_scalar(
        "SELECT COALESCE(proxy_subtype, '') FROM proxy_logs WHERE tenant_id = $1 \
         ORDER BY timestamp, id",
    )
    .bind(T)
    .fetch_all(pool)
    .await
    .expect("proxy rows")
}

/// A default-deny agent with no grant for the card is refused on every
/// route that does work, the legacy inbound route included: the card being
/// bound to the agent's group is access, not permission to act.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_agent_without_a_grant_is_refused_on_every_route(pool: PgPool) {
    let upstream = completed_remote().await;
    let key = topology(&pool, &upstream.uri()).await;
    bind_key_to_agent(&pool, "enforce").await;
    let state = common::build_test_state(pool.clone());
    for route in ROUTES {
        let (status, body) = call(&state, route, &key, None).await;
        assert_eq!(status, StatusCode::FORBIDDEN, "{route:?}: {body}");
        assert!(
            text(&body).contains("agent_not_authorized"),
            "{route:?}: {body}"
        );
    }
    assert_eq!(hits(&upstream).await, 0);
    assert_eq!(holds(&pool).await, 0);
}

/// The same agent with a grant for the card passes on every route.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_agent_with_a_grant_passes_on_every_route(pool: PgPool) {
    let upstream = completed_remote().await;
    let key = topology(&pool, &upstream.uri()).await;
    bind_key_to_agent(&pool, "enforce").await;
    grant(&pool, "allow").await;
    let state = common::build_test_state(pool.clone());
    for route in ROUTES {
        let (status, body) = call(&state, route, &key, None).await;
        assert_eq!(status, StatusCode::OK, "{route:?}: {body}");
    }
}

/// A grant that asks for an approval holds the call on every route, with no
/// A2A policy set anywhere; the approved retry proceeds and raises no second
/// hold.
async fn a_grant_that_asks_for_approval_holds(pool: PgPool, route: Route) {
    let upstream = completed_remote().await;
    let key = topology(&pool, &upstream.uri()).await;
    bind_key_to_agent(&pool, "enforce").await;
    grant(&pool, "approval_required").await;
    let state = common::build_test_state(pool.clone());

    let (status, body) = call(&state, route, &key, None).await;
    assert_ne!(status, StatusCode::OK, "{route:?}: {body}");
    assert_eq!(holds(&pool).await, 1, "{route:?}: {body}");
    assert_eq!(hits(&upstream).await, 0);
    let (hold_id, _, run) = pending_hold(&pool).await.expect("a hold");
    assert!(run.is_some(), "{route:?}: the hold names the run it holds");

    approve(&pool, &hold_id, "tok-grant").await;
    let (status, body) = call(&state, route, &key, Some("tok-grant")).await;
    assert_eq!(status, StatusCode::OK, "{route:?}: {body}");
    assert_eq!(holds(&pool).await, 1, "{route:?}");
    // The token is one-time.
    let (status, body) = call(&state, route, &key, Some("tok-grant")).await;
    assert_ne!(status, StatusCode::OK, "{route:?}: {body}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_grant_that_asks_for_approval_holds_an_outbound_call(pool: PgPool) {
    a_grant_that_asks_for_approval_holds(pool, Route::Outbound).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_grant_that_asks_for_approval_holds_a_legacy_inbound_call(pool: PgPool) {
    a_grant_that_asks_for_approval_holds(pool, Route::LegacyInbound).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_grant_that_asks_for_approval_holds_a_v1_message(pool: PgPool) {
    a_grant_that_asks_for_approval_holds(pool, Route::V1Send).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn one_approval_answers_the_grant_and_the_policy_on_legacy_inbound(pool: PgPool) {
    one_approval_answers_the_grant_and_the_policy(pool, Route::LegacyInbound).await;
}

/// An agent in dry-run mode is not refused: what its grants would have
/// decided is recorded as a shadow row, on every route.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_dry_run_agent_proceeds_and_leaves_a_shadow_row_on_every_route(pool: PgPool) {
    let upstream = completed_remote().await;
    let key = topology(&pool, &upstream.uri()).await;
    bind_key_to_agent(&pool, "dry_run").await;
    let state = common::build_test_state(pool.clone());
    for route in ROUTES {
        let (status, body) = call(&state, route, &key, None).await;
        assert_eq!(status, StatusCode::OK, "{route:?}: {body}");
    }
    let shadows = subtypes(&pool)
        .await
        .into_iter()
        .filter(|s| s == "agent_authz_shadow")
        .count();
    assert_eq!(shadows, ROUTES.len(), "one shadow row a route");
}

/// A suspended AI System stops A2A work on every route for a caller that is
/// not an agent: suspension is the system's, not the credential's.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_suspended_system_refuses_a_non_agent_caller_on_every_route(pool: PgPool) {
    let upstream = completed_remote().await;
    let key = topology(&pool, &upstream.uri()).await;
    sqlx::query(
        "UPDATE resource_groups SET group_type = 'AI_SYSTEM', system_kind = 'agent', \
         autonomy_level = 'suspended' WHERE id = $1",
    )
    .bind(RG)
    .execute(&pool)
    .await
    .expect("suspend the system");
    let state = common::build_test_state(pool.clone());
    for route in ROUTES {
        let (status, body) = call(&state, route, &key, None).await;
        assert_eq!(status, StatusCode::FORBIDDEN, "{route:?}: {body}");
        assert!(text(&body).contains("autonomy_denied"), "{route:?}: {body}");
    }
    assert_eq!(hits(&upstream).await, 0);
}

// ---------------------------------------------------------------------------
// A call that is refused or held spends no rate allowance
// ---------------------------------------------------------------------------

/// The legacy inbound route judges first: authorisation, then the policy,
/// then the limits. A call the policy refuses, and one it holds, leave the
/// card's allowance as it was.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_refused_or_held_inbound_call_spends_no_rate_allowance(pool: PgPool) {
    let key = topology(&pool, "http://127.0.0.1:9").await;
    set_card_limits(&pool, json!({"frequency": {"max_calls_per_hour": 1}})).await;

    set_group_policy(
        &pool,
        RG,
        json!({"capability": {"default_access": "disabled"}}),
    )
    .await;
    let state = common::build_test_state(pool.clone());
    for _ in 0..3 {
        let (status, body) = call(&state, Route::LegacyInbound, &key, None).await;
        assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    }

    set_group_policy(
        &pool,
        RG,
        json!({"capability": {"default_access": "approval_required"}}),
    )
    .await;
    let state = common::build_test_state(pool.clone());
    for _ in 0..3 {
        let (status, body) = call(&state, Route::LegacyInbound, &key, None).await;
        assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    }

    // The one call the card allows this hour is still there.
    set_group_policy(&pool, RG, json!({})).await;
    let state = common::build_test_state(pool.clone());
    let (status, body) = call(&state, Route::LegacyInbound, &key, None).await;
    assert_eq!(
        status,
        StatusCode::OK,
        "the refused and held calls spent the allowance: {body}"
    );
    let (status, body) = call(&state, Route::LegacyInbound, &key, None).await;
    assert_eq!(status, StatusCode::TOO_MANY_REQUESTS, "{body}");
}

/// An agent the grants refuse spends none either.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unauthorised_inbound_call_spends_no_rate_allowance(pool: PgPool) {
    let key = topology(&pool, "http://127.0.0.1:9").await;
    set_card_limits(&pool, json!({"frequency": {"max_calls_per_hour": 1}})).await;
    bind_key_to_agent(&pool, "enforce").await;
    let state = common::build_test_state(pool.clone());
    for _ in 0..3 {
        let (status, body) = call(&state, Route::LegacyInbound, &key, None).await;
        assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    }
    grant(&pool, "allow").await;
    let state = common::build_test_state(pool.clone());
    let (status, body) = call(&state, Route::LegacyInbound, &key, None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
}

// ---------------------------------------------------------------------------
// A denial by the caller's own plane is recorded alike on every route
// ---------------------------------------------------------------------------

/// The newest row of this tenant: (subtype, agent decision, autonomy level).
async fn last_row(pool: &PgPool) -> (Option<String>, Option<String>, Option<String>) {
    for _ in 0..40 {
        tokio::time::sleep(std::time::Duration::from_millis(25)).await;
    }
    sqlx::query_as(
        "SELECT proxy_subtype, agent_authz_decision, autonomy_level FROM proxy_logs \
         WHERE tenant_id = $1 ORDER BY timestamp DESC, id DESC LIMIT 1",
    )
    .bind(T)
    .fetch_one(pool)
    .await
    .expect("the row")
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_agent_denial_is_recorded_as_one_on_every_route(pool: PgPool) {
    let upstream = completed_remote().await;
    let key = topology(&pool, &upstream.uri()).await;
    bind_key_to_agent(&pool, "enforce").await;
    for route in ROUTES {
        let state = common::build_test_state(pool.clone());
        let (status, body) = call(&state, route, &key, None).await;
        assert_eq!(status, StatusCode::FORBIDDEN, "{route:?}: {body}");
        let (subtype, decision, _) = last_row(&pool).await;
        assert_eq!(
            subtype.as_deref(),
            Some("agent_authz_block"),
            "{route:?}: the row does not say an agent's grants refused the call"
        );
        assert_eq!(decision.as_deref(), Some("deny"), "{route:?}");
        sqlx::query("DELETE FROM proxy_logs WHERE tenant_id = $1")
            .bind(T)
            .execute(&pool)
            .await
            .expect("clear rows");
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_autonomy_denial_is_recorded_as_one_on_every_route(pool: PgPool) {
    let upstream = completed_remote().await;
    let key = topology(&pool, &upstream.uri()).await;
    sqlx::query(
        "UPDATE resource_groups SET group_type = 'AI_SYSTEM', system_kind = 'agent', \
         autonomy_level = 'suspended' WHERE id = $1",
    )
    .bind(RG)
    .execute(&pool)
    .await
    .expect("suspend the system");
    for route in ROUTES {
        let state = common::build_test_state(pool.clone());
        let (status, body) = call(&state, route, &key, None).await;
        assert_eq!(status, StatusCode::FORBIDDEN, "{route:?}: {body}");
        let (subtype, _, level) = last_row(&pool).await;
        assert_eq!(
            subtype.as_deref(),
            Some("autonomy_block"),
            "{route:?}: the row does not say the system's autonomy refused the call"
        );
        assert_eq!(level.as_deref(), Some("suspended"), "{route:?}");
        sqlx::query("DELETE FROM proxy_logs WHERE tenant_id = $1")
            .bind(T)
            .execute(&pool)
            .await
            .expect("clear rows");
    }
}

/// A call the input guardrail blocks gives back what the limits admitted.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_inbound_call_a_guardrail_blocks_spends_no_rate_allowance(pool: PgPool) {
    let key = topology(&pool, "http://127.0.0.1:9").await;
    set_card_limits(&pool, json!({"frequency": {"max_calls_per_hour": 1}})).await;
    common::seed_banned_words_guardrail_surfaces(
        &pool,
        "gr-a2agov",
        T,
        &["forbiddenword"],
        &["a2a_inbound"],
    )
    .await;
    common::bind_guardrail_to_rg(&pool, "gr-a2agov", RG).await;
    let state = common::build_test_state(pool.clone());
    let send = |text: &'static str| {
        let state = state.clone();
        let key = key.clone();
        async move {
            let req = common::build_post_request(
                "/v1/proxy/a2a/inbound",
                json!({"target_card_id": CARD, "capability": CAP, "message": {"text": text}}),
                Some(&key),
                Some(T),
            );
            legacy_app(&state)
                .oneshot(req)
                .await
                .expect("router")
                .status()
        }
    };
    for _ in 0..3 {
        assert_eq!(send("say forbiddenword").await, StatusCode::FORBIDDEN);
    }
    // Let the released holds land.
    tokio::time::sleep(std::time::Duration::from_millis(200)).await;
    assert_eq!(
        send("hi").await,
        StatusCode::OK,
        "the blocked calls spent the allowance"
    );
    assert_eq!(send("hi").await, StatusCode::TOO_MANY_REQUESTS);
}

/// The routes a guardrail on the message can block after the limits were
/// admitted: outbound, and the two v1.0 message verbs.
#[derive(Clone, Copy, Debug)]
enum Guarded {
    Outbound,
    V1Send,
    V1Stream,
}

impl Guarded {
    fn surface(self) -> &'static str {
        match self {
            Guarded::Outbound => "a2a_outbound",
            Guarded::V1Send | Guarded::V1Stream => "a2a_inbound",
        }
    }

    fn request(self, key: &str, text: &str) -> Request<Body> {
        let v1 = |verb: &str| {
            common::build_post_request(
                &format!("/a2a/{T}/{CARD_NAME}/message:{verb}"),
                json!({"message": {"messageId": ulid::Ulid::new().to_string(),
                                   "role": "ROLE_USER", "parts": [{"text": text}]}}),
                Some(key),
                Some(T),
            )
        };
        match self {
            Guarded::Outbound => common::build_post_request(
                "/v1/proxy/a2a/outbound",
                json!({"target_card_id": CARD, "capability": CAP, "message": {"text": text}}),
                Some(key),
                Some(T),
            ),
            Guarded::V1Send => v1("send"),
            Guarded::V1Stream => v1("stream"),
        }
    }

    async fn send(self, state: &SharedState, key: &str, text: &str) -> StatusCode {
        let app = match self {
            Guarded::Outbound => legacy_app(state),
            _ => common::build_a2a_v1_app(state.clone()),
        };
        let resp = app.oneshot(self.request(key, text)).await.expect("router");
        // The status is the verdict; a stream that was admitted stays open
        // for its task and is dropped here.
        resp.status()
    }
}

/// A call the message guardrail blocks gives back what the limits admitted,
/// on every route that admits before it (as the legacy inbound route does).
async fn a_call_a_guardrail_blocks_spends_no_rate_allowance(pool: PgPool, route: Guarded) {
    let upstream = completed_remote().await;
    let key = topology(&pool, &upstream.uri()).await;
    set_card_limits(&pool, json!({"frequency": {"max_calls_per_hour": 1}})).await;
    common::seed_banned_words_guardrail_surfaces(
        &pool,
        "gr-a2agov-spend",
        T,
        &["forbiddenword"],
        &[route.surface()],
    )
    .await;
    common::bind_guardrail_to_rg(&pool, "gr-a2agov-spend", RG).await;
    let state = common::build_test_state(pool.clone());
    for _ in 0..3 {
        let status = route.send(&state, &key, "say forbiddenword").await;
        assert!(
            status.is_client_error() && status != StatusCode::TOO_MANY_REQUESTS,
            "{route:?}: a blocked message, got {status}"
        );
    }
    assert_eq!(hits(&upstream).await, 0, "{route:?}: a blocked call left");
    // Let the released holds land.
    tokio::time::sleep(std::time::Duration::from_millis(200)).await;
    let status = route.send(&state, &key, "hi").await;
    assert_eq!(
        status,
        StatusCode::OK,
        "{route:?}: the blocked calls spent the allowance"
    );
    // A dispatched call keeps what it was charged.
    tokio::time::sleep(std::time::Duration::from_millis(200)).await;
    assert_eq!(
        route.send(&state, &key, "hi").await,
        StatusCode::TOO_MANY_REQUESTS,
        "{route:?}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_outbound_call_a_guardrail_blocks_spends_no_rate_allowance(pool: PgPool) {
    a_call_a_guardrail_blocks_spends_no_rate_allowance(pool, Guarded::Outbound).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_v1_message_a_guardrail_blocks_spends_no_rate_allowance(pool: PgPool) {
    a_call_a_guardrail_blocks_spends_no_rate_allowance(pool, Guarded::V1Send).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_v1_stream_a_guardrail_blocks_spends_no_rate_allowance(pool: PgPool) {
    a_call_a_guardrail_blocks_spends_no_rate_allowance(pool, Guarded::V1Stream).await;
}
