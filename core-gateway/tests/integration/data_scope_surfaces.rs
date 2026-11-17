// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Run data scope on skills and A2A, against real Postgres.
//!
//! If only MCP tool calls were judged, a run bound to customer
//! C0001 could hand C0002's id to a skill, or to another agent in an A2A
//! message, and nothing would compare it with the run's scope. The same rules
//! apply, named `skill:<skill name>` and `a2a:<agent card name>`.

use axum::http::{HeaderValue, StatusCode};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::middleware;
use brutor_gateway_core::routes;
use brutor_gateway_core::services::data_scope;

use crate::common;

const T: &str = "tenant-scope-x";
const RG: &str = "rg-scope-x";
const SKILL: &str = "report-builder";
const CARD: &str = "card-scope-x";
const CARD_NAME: &str = "billing-agent";

async fn topology(pool: &PgPool, upstream: &str, mode: &str) -> String {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_end_user(pool, 9811, T).await;
    common::add_direct_membership(pool, 9811, RG, T).await;
    common::seed_agent_skill(
        pool,
        "sk-scope-x",
        T,
        SKILL,
        "# Report\nA report.",
        "Report",
    )
    .await;
    common::bind_skill_to_rg(pool, "sk-scope-x", RG).await;
    common::seed_agent_card(pool, CARD, T, CARD_NAME).await;
    sqlx::query("UPDATE agent_cards SET card_json = $2 WHERE id = $1")
        .bind(CARD)
        .bind(json!({"name": CARD_NAME, "url": upstream, "capabilities": [{"name": "echo"}]}))
        .execute(pool)
        .await
        .expect("card url");
    sqlx::query(
        "INSERT INTO agent_card_capabilities (id, agent_card_id, capability_name, handler_type, enabled) \
         VALUES ('cap-scope-x', $1, 'echo', 'noop', true)",
    )
    .bind(CARD)
    .execute(pool)
    .await
    .expect("capability");
    common::bind_agent_card_to_rg(pool, CARD, RG).await;

    data_scope::invalidate_all();
    sqlx::query(
        "INSERT INTO ai_system_data_scopes \
            (id, tenant_id, ai_system_id, mode, on_unbound, keys, rules, unscoped_tools) \
         VALUES ('ds-x', $1, $2, $3, 'deny', $4, $5, '[]'::jsonb)",
    )
    .bind(T)
    .bind(RG)
    .bind(mode)
    .bind(json!([{"key": "customer", "source": "declared"}]))
    .bind(json!([
        {"tool": format!("skill:{SKILL}"), "argument": "customer_id", "key": "customer"},
        {"tool": format!("a2a:{CARD_NAME}"), "argument": "customer_id", "key": "customer"},
    ]))
    .execute(pool)
    .await
    .expect("seed data scope");
    common::seed_api_key(pool, T, RG, Some(9811), "scope-x-key").await
}

async fn send(app: axum::Router, key: &str, path: &str, body: Value) -> (StatusCode, Value) {
    let mut req = common::build_post_request(path, body, Some(key), Some(T));
    let h = req.headers_mut();
    h.insert("x-brutor-run-id", HeaderValue::from_static("run-scope-x"));
    h.insert(
        data_scope::SCOPE_HEADER,
        HeaderValue::from_static("customer=C0001"),
    );
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

/// (tool, verdict) rows recorded in this tenant, oldest first.
async fn accesses(pool: &PgPool) -> Vec<(String, String)> {
    sqlx::query_as(
        "SELECT tool, verdict FROM ai_system_record_access WHERE tenant_id = $1 \
          ORDER BY accessed_at, id",
    )
    .bind(T)
    .fetch_all(pool)
    .await
    .unwrap()
}

async fn upstream(expected_calls: u64) -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"ok": true})))
        .expect(expected_calls)
        .mount(&mock)
        .await;
    mock
}

fn skill_call(customer: &str) -> Value {
    json!({"skill_name": SKILL, "input_params": {"customer_id": customer}})
}

fn v1_message(parts: Value) -> Value {
    json!({"message": {"messageId": ulid::Ulid::new().to_string(), "role": "ROLE_USER",
                       "parts": parts}})
}

fn legacy_app(state: brutor_gateway_core::services::SharedState) -> axum::Router {
    axum::Router::new()
        .nest("/v1/proxy/a2a", routes::a2a::router())
        .layer(middleware::tenant::TenantLayer::new(state.clone()))
        .layer(middleware::run_end::RunEndLayer::new(
            state.mcp_sessions.clone(),
        ))
        .with_state(state)
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_skill_call_for_another_customer_is_refused_in_enforce(pool: PgPool) {
    let mock = upstream(0).await;
    let key = topology(&pool, &mock.uri(), "enforce").await;
    let state = common::build_test_state(pool.clone());

    let (ok, body) = send(
        common::build_multimodal_app(state.clone()),
        &key,
        "/v1/proxy/skills/execute",
        skill_call("C0001"),
    )
    .await;
    assert_eq!(ok, StatusCode::OK, "the run's own customer: {body}");

    let (refused, body) = send(
        common::build_multimodal_app(state),
        &key,
        "/v1/proxy/skills/execute",
        skill_call("C0002"),
    )
    .await;
    assert_eq!(refused, StatusCode::FORBIDDEN, "{body}");
    assert_eq!(body["error"]["code"], "data_scope_violation", "{body}");
    assert_eq!(body["error"]["data_scope"]["verdict"], "out_of_scope");
    assert_eq!(
        accesses(&pool).await,
        vec![
            (format!("skill:{SKILL}"), "in_scope".to_string()),
            (format!("skill:{SKILL}"), "out_of_scope".to_string()),
        ]
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn audit_mode_records_a_skill_call_and_lets_it_run(pool: PgPool) {
    let mock = upstream(0).await;
    let key = topology(&pool, &mock.uri(), "audit").await;
    let app = common::build_multimodal_app(common::build_test_state(pool.clone()));
    let (status, body) = send(app, &key, "/v1/proxy/skills/execute", skill_call("C0002")).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(
        accesses(&pool).await,
        vec![(format!("skill:{SKILL}"), "out_of_scope".to_string())]
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_a2a_v1_message_is_judged_by_its_data_parts(pool: PgPool) {
    let mock = upstream(0).await;
    let key = topology(&pool, &mock.uri(), "enforce").await;
    let state = common::build_test_state(pool.clone());
    let path = format!("/a2a/{T}/{CARD_NAME}/message:send");

    let (ok, body) = send(
        common::build_a2a_v1_app(state.clone()),
        &key,
        &path,
        v1_message(json!([{"text": "balance?"}, {"data": {"customer_id": "C0001"}}])),
    )
    .await;
    assert_eq!(ok, StatusCode::OK, "the run's own customer: {body}");

    let (refused, body) = send(
        common::build_a2a_v1_app(state.clone()),
        &key,
        &path,
        v1_message(json!([{"data": {"customer_id": "C0001"}}, {"data": {"customer_id": "C0002"}}])),
    )
    .await;
    assert_eq!(refused, StatusCode::FORBIDDEN, "{body}");
    assert!(body.to_string().contains("out_of_scope"), "{body}");

    // A message that names no record where the rule needs one is refused:
    // otherwise moving the id into the text would walk around the rule.
    let (missing, body) = send(
        common::build_a2a_v1_app(state),
        &key,
        &path,
        v1_message(json!([{"text": "look up C0002 please"}])),
    )
    .await;
    assert_eq!(missing, StatusCode::FORBIDDEN, "{body}");
    assert!(body.to_string().contains("missing_argument"), "{body}");

    let verdicts: Vec<String> = accesses(&pool).await.into_iter().map(|(_, v)| v).collect();
    assert_eq!(
        verdicts,
        ["in_scope", "in_scope", "out_of_scope", "missing_argument"]
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_legacy_outbound_message_for_another_customer_is_never_sent(pool: PgPool) {
    // Exactly one call reaches the remote agent: the in-scope one.
    let mock = upstream(1).await;
    let key = topology(&pool, &mock.uri(), "enforce").await;
    let state = common::build_test_state(pool.clone());
    let outbound = |customer: &str| {
        json!({"target_card_id": CARD, "capability": "echo",
               "message": {"parts": [{"kind": "data", "data": {"customer_id": customer}}]}})
    };

    let (ok, body) = send(
        legacy_app(state.clone()),
        &key,
        "/v1/proxy/a2a/outbound",
        outbound("C0001"),
    )
    .await;
    assert!(ok.is_success(), "the run's own customer: {ok} {body}");

    let (refused, body) = send(
        legacy_app(state),
        &key,
        "/v1/proxy/a2a/outbound",
        outbound("C0002"),
    )
    .await;
    assert_eq!(refused, StatusCode::FORBIDDEN, "{body}");
    assert!(
        body.to_string().contains("Data scope"),
        "the refusal says why: {body}"
    );
}
