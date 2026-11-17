// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A routing group with no member available right now.
//!
//! When every member of a routing group is cooling down after failures, or
//! has used up its own per-minute rate, the group exists, the caller may use
//! it, and nothing can take the call at this moment. The preflight mapped
//! every model-resolution error to 404, so the caller was told the model
//! does not exist. It is now 429 with `Retry-After`: the shortest time after
//! which a member is available again. A model that does not exist stays 404,
//! and a group the caller's resource group does not hold answers as before
//! (403 while a member is available, 404 while none is).

use std::time::Duration;

use axum::Router;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::json;
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::services::SharedState;

use crate::common;

const T: &str = "tenant-busy";
const RG: &str = "rg-busy";
/// The virtual model of the routing group `grp-busy`.
const GROUP_MODEL: &str = "rg-grp-busy";
const MEMBERS: [&str; 2] = ["busy-a", "busy-b"];

const ROUTES: [(&str, &str); 4] = [
    ("/v1/proxy/llm/chat/completions", "messages"),
    ("/v1/proxy/llm/responses", "input"),
    ("/v1/proxy/llm/completions", "prompt"),
    ("/v1/proxy/llm/embeddings", "input"),
];

fn request(route: usize, model: &str, key: &str) -> Request<Body> {
    let (path, field) = ROUTES[route];
    let mut body = json!({"model_id": model});
    body[field] = match field {
        "messages" => json!([{"role": "user", "content": "hi"}]),
        _ => json!("hi"),
    };
    common::build_post_request(path, body, Some(key), Some(T))
}

/// A provider that answers anything with a body every route can read.
async fn provider() -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "x", "object": "chat.completion", "created": 1_700_000_000, "model": "test-gpt",
            "status": "completed",
            "choices": [{"index": 0, "text": "ok",
                         "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "output": [{"type": "message", "id": "msg_1", "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok", "annotations": []}]}],
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2,
                      "input_tokens": 1, "output_tokens": 1}
        })))
        .mount(&mock)
        .await;
    mock
}

struct World {
    mock: MockServer,
    state: SharedState,
    app: Router,
    key: String,
}

/// A routing group with two members, the group and its members bound to the
/// caller's resource group.
async fn world(pool: &PgPool) -> World {
    let mock = provider().await;
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_llm_provider(pool, "prov-busy", T, "openai").await;
    common::seed_routing_group(pool, "grp-busy", T, "prov-busy", "simple").await;
    common::bind_llm_model_to_rg(pool, GROUP_MODEL, RG).await;
    for member in MEMBERS {
        common::seed_llm_model_with_api_url(pool, member, T, "prov-busy", "test-gpt", &mock.uri())
            .await;
        sqlx::query("UPDATE llm_model SET routing_group_id = 'grp-busy' WHERE id = $1")
            .bind(member)
            .execute(pool)
            .await
            .expect("member of the routing group");
        common::bind_llm_model_to_rg(pool, member, RG).await;
    }
    let key = common::seed_api_key(pool, T, RG, None, "busy-key").await;
    let state = common::build_test_state(pool.clone());
    World {
        mock,
        app: common::build_llm_app(state.clone()),
        state,
        key,
    }
}

async fn send(app: &Router, req: Request<Body>) -> (StatusCode, Option<u64>, String) {
    let resp = app.clone().oneshot(req).await.expect("router");
    let status = resp.status();
    let retry_after = resp
        .headers()
        .get("retry-after")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse().ok());
    let bytes = resp.into_body().collect().await.expect("body").to_bytes();
    (
        status,
        retry_after,
        String::from_utf8_lossy(&bytes).into_owned(),
    )
}

async fn provider_calls(w: &World) -> usize {
    w.mock.received_requests().await.unwrap_or_default().len()
}

/// Every member cooling down: 429, `Retry-After` is the shortest remaining
/// cooldown, the provider is not called. When the first member is back, the
/// group serves again.
async fn every_member_cooling_down_is_a_429_with_the_shortest_cooldown(pool: PgPool, route: usize) {
    let w = world(&pool).await;
    let path = ROUTES[route].0;

    // The control: the group serves while a member is available.
    let (status, _, body) = send(&w.app, request(route, GROUP_MODEL, &w.key)).await;
    assert_eq!(status, StatusCode::OK, "{path}, members available: {body}");
    assert_eq!(provider_calls(&w).await, 1);

    w.state
        .router_state
        .set_cooldown(MEMBERS[0], Duration::from_secs(90), 500, "test");
    w.state
        .router_state
        .set_cooldown(MEMBERS[1], Duration::from_secs(40), 500, "test");
    let (status, retry_after, body) = send(&w.app, request(route, GROUP_MODEL, &w.key)).await;
    assert_eq!(
        status,
        StatusCode::TOO_MANY_REQUESTS,
        "{path}: a group that exists and is the caller's, with no member available: {body}"
    );
    assert!(body.contains("busy or in cooldown"), "{path}: {body}");
    let retry_after = retry_after.unwrap_or_else(|| panic!("{path}: no Retry-After ({body})"));
    assert!(
        (30..=40).contains(&retry_after),
        "{path}: Retry-After {retry_after}; the shortest remaining cooldown is 40 s"
    );
    assert_eq!(provider_calls(&w).await, 1, "{path}: nothing was sent");
    // The refusal is on the record: a capacity refusal, on the caller's key.
    let mut row: Option<(Option<String>, Option<String>, Option<String>)> = None;
    for _ in 0..120 {
        row = sqlx::query_as(
            "SELECT proxy_subtype, error_type, api_key_name FROM proxy_logs \
              WHERE tenant_id = $1 AND http_status_code = 429 LIMIT 1",
        )
        .bind(T)
        .fetch_optional(&pool)
        .await
        .expect("proxy_logs");
        if row.is_some() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let (subtype, error_type, key_name) = row.unwrap_or_else(|| panic!("{path}: no 429 row"));
    assert_eq!(subtype.as_deref(), Some("rate_limit_routing"), "{path}");
    assert_eq!(error_type.as_deref(), Some("all_models_busy"), "{path}");
    assert_eq!(key_name.as_deref(), Some("busy-key"), "{path}");

    // One member back: served.
    w.state.router_state.record_success(MEMBERS[1]);
    let (status, _, body) = send(&w.app, request(route, GROUP_MODEL, &w.key)).await;
    assert_eq!(status, StatusCode::OK, "{path}, a member is back: {body}");
}

/// Every member at its own per-minute rate: 429, and the hint is the rest of
/// the minute the rate is counted in.
async fn every_member_at_its_rate_is_a_429_until_the_minute_ends(pool: PgPool, route: usize) {
    let w = world(&pool).await;
    let path = ROUTES[route].0;
    sqlx::query("UPDATE llm_model SET rate_limit_rpm = 1 WHERE routing_group_id = 'grp-busy'")
        .execute(&pool)
        .await
        .expect("a rate per member");
    for member in MEMBERS {
        w.state.router_state.increment_rpm(member);
    }
    let (status, retry_after, body) = send(&w.app, request(route, GROUP_MODEL, &w.key)).await;
    assert_eq!(status, StatusCode::TOO_MANY_REQUESTS, "{path}: {body}");
    let retry_after = retry_after.unwrap_or_else(|| panic!("{path}: no Retry-After ({body})"));
    assert!(
        (1..=60).contains(&retry_after),
        "{path}: Retry-After {retry_after} is not within the minute"
    );
    assert_eq!(provider_calls(&w).await, 0, "{path}");
}

/// What is not "none available now" answers as before: a model that does
/// not exist is 404, a routing group the caller's resource group does not
/// hold is 403, also while its members cool down.
async fn a_missing_model_stays_404_and_an_unbound_group_stays_403(pool: PgPool, route: usize) {
    let w = world(&pool).await;
    let path = ROUTES[route].0;

    let (status, retry_after, body) = send(&w.app, request(route, "no-such-model", &w.key)).await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{path}: {body}");
    assert_eq!(retry_after, None, "{path}: a missing model is not retried");

    // A group with no members is not "busy": there is nothing to wait for.
    common::seed_routing_group(&pool, "grp-empty", T, "prov-busy", "simple").await;
    common::bind_llm_model_to_rg(&pool, "rg-grp-empty", RG).await;
    let (status, retry_after, body) = send(&w.app, request(route, "rg-grp-empty", &w.key)).await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{path}, no members: {body}");
    assert_eq!(retry_after, None, "{path}");

    // Another resource group's key: the routing group is not bound to it.
    common::seed_resource_group(&pool, "rg-busy-other", T).await;
    let outsider = common::seed_api_key(&pool, T, "rg-busy-other", None, "busy-outsider").await;
    let (status, _, body) = send(&w.app, request(route, GROUP_MODEL, &outsider)).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "{path}, members available: {body}"
    );
    for member in MEMBERS {
        w.state
            .router_state
            .set_cooldown(member, Duration::from_secs(60), 500, "test");
    }
    // With every member cooling down it is told what it was told before:
    // 404. Not 429 — how a group's members are doing is not for a caller
    // who may not use the group.
    let (status, retry_after, body) = send(&w.app, request(route, GROUP_MODEL, &outsider)).await;
    assert_eq!(
        status,
        StatusCode::NOT_FOUND,
        "{path}, members cooling down: {body}"
    );
    assert_eq!(retry_after, None, "{path}");
    assert_eq!(provider_calls(&w).await, 0, "{path}");
    // The group's own caller, at the same moment: 429.
    let (status, retry_after, _) = send(&w.app, request(route, GROUP_MODEL, &w.key)).await;
    assert_eq!(status, StatusCode::TOO_MANY_REQUESTS, "{path}");
    assert!(retry_after.is_some(), "{path}");
}

macro_rules! per_route {
    ($($case:ident),+ $(,)?) => {$(
        mod $case {
            use super::*;
            #[sqlx::test(migrations = "./tests/migrations")]
            async fn chat(pool: PgPool) { super::$case(pool, 0).await; }
            #[sqlx::test(migrations = "./tests/migrations")]
            async fn responses(pool: PgPool) { super::$case(pool, 1).await; }
            #[sqlx::test(migrations = "./tests/migrations")]
            async fn completions(pool: PgPool) { super::$case(pool, 2).await; }
            #[sqlx::test(migrations = "./tests/migrations")]
            async fn embeddings(pool: PgPool) { super::$case(pool, 3).await; }
        }
    )+};
}

per_route!(
    every_member_cooling_down_is_a_429_with_the_shortest_cooldown,
    every_member_at_its_rate_is_a_429_until_the_minute_ends,
    a_missing_model_stays_404_and_an_unbound_group_stays_403,
);
