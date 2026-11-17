// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A skill execution is read and cancelled by the caller that started it.
//!
//! `GET /v1/proxy/skills/executions/{id}` and its `/cancel` authorised by
//! tenant alone: any credential of the tenant that knew an execution's id
//! read its status and cancelled it. The rule is the one a file has
//! (`media_api_key.rs`): the end user when the caller has one, otherwise
//! the resource group the caller acts in. For anyone else the execution
//! does not exist, on both routes.

use axum::Router;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

/// The runner's address is a process environment variable these tests set.
static SERIAL: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

const T: &str = "t-exec-owner";
const RG: &str = "rg-exec-owner";
const RG_OTHER: &str = "rg-exec-owner-other";
const SKILL_ID: &str = "skill-exec-owner";
const SKILL_NAME: &str = "summarise";

async fn runner() -> MockServer {
    let runner = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "success": true,
            "output": "ran",
            "error": null,
            "script_results": {},
            "loaded_assets": {},
            "loaded_resources": {},
            "unresolved_refs": [],
            "duration_ms": 1.0
        })))
        .mount(&runner)
        .await;
    unsafe {
        std::env::set_var("BRUTOR_SKILL_RUNNER_URL", runner.uri());
        std::env::set_var("BRUTOR_SKILL_RUNNER_JWT_SECRET", "exec-owner-runner-secret");
    }
    brutor_gateway_core::env_vars::reload_for_tests();
    runner
}

async fn topology(pool: &PgPool) {
    common::seed_tenant(pool, T).await;
    for group in [RG, RG_OTHER] {
        common::seed_resource_group(pool, group, T).await;
    }
    for user in [9741, 9742] {
        common::seed_end_user(pool, user, T).await;
        common::add_direct_membership(pool, user, RG, T).await;
    }
    common::seed_agent_skill(pool, SKILL_ID, T, SKILL_NAME, "# Summarise", "Summarises").await;
    for group in [RG, RG_OTHER] {
        common::bind_skill_to_rg(pool, SKILL_ID, group).await;
    }
}

fn app(pool: &PgPool) -> Router {
    common::build_multimodal_app(common::build_test_state(pool.clone()))
}

async fn send(app: Router, req: Request<Body>) -> (StatusCode, Value) {
    let resp = app.oneshot(req).await.expect("router");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.expect("body").to_bytes();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

/// Runs the skill as `key` and returns the execution's id, put back to
/// `RUNNING` so a cancel has something to cancel.
async fn start_as(pool: &PgPool, key: &str) -> String {
    let (status, body) = send(
        app(pool),
        common::build_post_request(
            "/v1/proxy/skills/execute",
            json!({"skill_name": SKILL_NAME, "input_params": {"text": "x"}}),
            Some(key),
            Some(T),
        ),
    )
    .await;
    assert!(status.is_success(), "{status}: {body}");
    let id = body["execution_id"]
        .as_str()
        .unwrap_or_else(|| panic!("no execution id in {body}"))
        .to_string();
    sqlx::query(
        "UPDATE agent_skill_executions SET execution_status = 'RUNNING'::executionstatus, \
         completed_at = NULL WHERE id = $1",
    )
    .bind(&id)
    .execute(pool)
    .await
    .expect("back to running");
    id
}

async fn read(pool: &PgPool, id: &str, key: &str) -> (StatusCode, Value) {
    send(
        app(pool),
        common::build_get_request(
            &format!("/v1/proxy/skills/executions/{id}"),
            Some(key),
            Some(T),
        ),
    )
    .await
}

async fn cancel(pool: &PgPool, id: &str, key: &str) -> (StatusCode, Value) {
    send(
        app(pool),
        common::build_post_request(
            &format!("/v1/proxy/skills/executions/{id}/cancel"),
            json!({}),
            Some(key),
            Some(T),
        ),
    )
    .await
}

async fn status_of(pool: &PgPool, id: &str) -> String {
    sqlx::query_scalar("SELECT execution_status::text FROM agent_skill_executions WHERE id = $1")
        .bind(id)
        .fetch_one(pool)
        .await
        .expect("execution row")
}

/// What `key` is answered for an execution that is not there, on both
/// routes: the answer another caller's execution must get.
async fn refused_as_missing(pool: &PgPool, id: &str, key: &str, who: &str) {
    const MISSING: &str = "01HZZZZZZZZZZZZZZZZZZZZZZZ";
    let (missing_read, missing_read_body) = read(pool, MISSING, key).await;
    let (missing_cancel, missing_cancel_body) = cancel(pool, MISSING, key).await;
    assert_eq!(missing_read, StatusCode::NOT_FOUND);
    assert_eq!(missing_cancel, StatusCode::NOT_FOUND);
    let as_missing = |body: &Value| body.to_string().replace(id, MISSING);
    let (status, body) = read(pool, id, key).await;
    assert_eq!(status, missing_read, "{who} reads it: {body}");
    assert_eq!(as_missing(&body), missing_read_body.to_string(), "{who}");
    let (status, body) = cancel(pool, id, key).await;
    assert_eq!(status, missing_cancel, "{who} cancels it: {body}");
    assert_eq!(as_missing(&body), missing_cancel_body.to_string(), "{who}");
    assert_eq!(status_of(pool, id).await, "RUNNING", "{who} cancelled it");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn one_users_execution_is_not_another_users_of_the_same_tenant(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let _runner = runner().await;
    topology(&pool).await;
    let alice = common::seed_api_key(&pool, T, RG, Some(9741), "exec-alice").await;
    let alice_again = common::seed_api_key(&pool, T, RG, Some(9741), "exec-alice-2").await;
    let bob = common::seed_api_key(&pool, T, RG, Some(9742), "exec-bob").await;
    let group_key = common::seed_api_key(&pool, T, RG, None, "exec-group").await;

    let id = start_as(&pool, &alice).await;
    refused_as_missing(&pool, &id, &bob, "another user").await;
    refused_as_missing(&pool, &id, &group_key, "a key of the user's group").await;

    // The user follows it through any key of theirs, and cancels it.
    let (status, body) = read(&pool, &id, &alice_again).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["execution_id"], json!(id));
    let (status, body) = cancel(&pool, &id, &alice).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(status_of(&pool, &id).await, "CANCELLED");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_groups_execution_is_its_groups_and_no_other_groups(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let _runner = runner().await;
    topology(&pool).await;
    let starter = common::seed_api_key(&pool, T, RG, None, "exec-grp-starter").await;
    let peer = common::seed_api_key(&pool, T, RG, None, "exec-grp-peer").await;
    let other_group = common::seed_api_key(&pool, T, RG_OTHER, None, "exec-grp-other").await;
    let member = common::seed_api_key(&pool, T, RG, Some(9741), "exec-grp-member").await;

    let id = start_as(&pool, &starter).await;
    refused_as_missing(&pool, &id, &other_group, "another group").await;
    refused_as_missing(&pool, &id, &member, "an end user of the group").await;

    // Another key that acts as the same group follows and cancels it.
    let (status, body) = read(&pool, &id, &peer).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let (status, body) = cancel(&pool, &id, &peer).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(status_of(&pool, &id).await, "CANCELLED");
}

/// An execution that records neither a user nor a group is nobody's.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_execution_with_no_recorded_starter_is_read_by_nobody(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let _runner = runner().await;
    topology(&pool).await;
    let key = common::seed_api_key(&pool, T, RG, None, "exec-nobody").await;
    let id = start_as(&pool, &key).await;
    sqlx::query("UPDATE agent_skill_executions SET group_id = NULL, user_id = NULL WHERE id = $1")
        .bind(&id)
        .execute(&pool)
        .await
        .expect("forget the starter");
    refused_as_missing(&pool, &id, &key, "the caller").await;
}
