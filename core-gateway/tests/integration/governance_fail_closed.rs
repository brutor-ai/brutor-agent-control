// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A governance policy that cannot be read refuses the call; it is
//! never read as "no policy".
//!
//! The resolvers used to turn a database error into "no governance" (and to
//! skip an LLM governance blob that did not parse), so temperature caps, tool
//! rules and mandatory system-prompt fragments silently stopped applying for
//! that request. Each shape is pinned here; `tests/limits_guard.rs` (rule 3) pins the source shape for every
//! governance and limit read.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::services::{skill_orchestrator, usage};

use crate::common;

const TENANT: &str = "t-gov-fc";
const RG: &str = "rg-gov-fc";

async fn upstream() -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "chatcmpl-gov",
            "object": "chat.completion",
            "created": 1_700_000_000,
            "model": "test-gpt",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
        })))
        .mount(&mock)
        .await;
    mock
}

/// Tenant, model at the mock, one group bound to it, its user and key.
async fn topology(pool: &PgPool, mock: &MockServer) -> String {
    common::seed_tenant(pool, TENANT).await;
    common::seed_llm_provider(pool, "prov-openai", TENANT, "openai").await;
    common::seed_llm_model_with_api_url(
        pool,
        "m-gov",
        TENANT,
        "prov-openai",
        "test-gpt",
        &mock.uri(),
    )
    .await;
    common::seed_resource_group(pool, RG, TENANT).await;
    common::seed_end_user(pool, 8911, TENANT).await;
    common::add_direct_membership(pool, 8911, RG, TENANT).await;
    common::bind_llm_model_to_rg(pool, "m-gov", RG).await;
    common::seed_api_key(pool, TENANT, RG, Some(8911), "gov-fc-key").await
}

fn chat(key: &str, extra: Value) -> Request<Body> {
    let mut body = json!({"model_id": "m-gov", "messages": [{"role": "user", "content": "hi"}]});
    if let (Some(b), Some(e)) = (body.as_object_mut(), extra.as_object()) {
        for (k, v) in e {
            b.insert(k.clone(), v.clone());
        }
    }
    let mut r = Request::builder()
        .method("POST")
        .uri("/v1/proxy/llm/chat/completions")
        .header("content-type", "application/json")
        .header("X-Tenant-ID", TENANT)
        .header("authorization", format!("Bearer {key}"))
        .body(Body::from(serde_json::to_vec(&body).unwrap()))
        .unwrap();
    let peer: std::net::SocketAddr = "127.0.0.1:55557".parse().unwrap();
    r.extensions_mut().insert(axum::extract::ConnectInfo(peer));
    r
}

async fn send(pool: &PgPool, req: Request<Body>) -> (StatusCode, String) {
    let resp = common::build_llm_app(common::build_test_state(pool.clone()))
        .oneshot(req)
        .await
        .unwrap();
    let status = resp.status();
    let body = resp.into_body().collect().await.unwrap().to_bytes();
    (status, String::from_utf8_lossy(&body).into_owned())
}

async fn upstream_calls(mock: &MockServer) -> Vec<Value> {
    mock.received_requests()
        .await
        .unwrap_or_default()
        .iter()
        .map(|r| serde_json::from_slice(&r.body).unwrap_or(Value::Null))
        .collect()
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_governance_policy_that_does_not_parse_refuses_the_call(pool: PgPool) {
    let mock = upstream().await;
    let key = topology(&pool, &mock).await;
    // A temperature cap the proxy cannot read. It used to be skipped — the
    // call went through with no temperature cap at all.
    common::set_rg_llm_governance(&pool, RG, json!({"temperature": {"max": "cool"}})).await;

    let (status, body) = send(&pool, chat(&key, json!({"temperature": 1.9}))).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    assert!(body.contains("governance"), "{body}");
    assert!(
        upstream_calls(&mock).await.is_empty(),
        "a call whose policy cannot be read never reaches the provider"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_governance_policy_that_cannot_be_read_refuses_the_call(pool: PgPool) {
    let mock = upstream().await;
    let key = topology(&pool, &mock).await;
    // The per-model governance layer becomes unreadable (the database
    // errors on it). It used to read as "no governance".
    sqlx::query("ALTER TABLE resource_group_llm_governance RENAME TO rglg_unreadable")
        .execute(&pool)
        .await
        .unwrap();

    let (status, body) = send(&pool, chat(&key, json!({}))).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    assert!(upstream_calls(&mock).await.is_empty());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn every_governance_resolver_returns_an_error_not_absence(pool: PgPool) {
    common::seed_tenant(&pool, TENANT).await;
    common::seed_resource_group(&pool, RG, TENANT).await;
    // A parent, so every chain resolver has an ancestor hop to read.
    common::seed_resource_group(&pool, "rg-gov-fc-parent", TENANT).await;
    common::set_rg_parent(&pool, RG, "rg-gov-fc-parent").await;
    let group = common::load_resource_group(&pool, RG).await;
    // Every read now fails.
    pool.close().await;

    assert!(
        usage::collect_effective_llm_governance(&pool, &group, "m-gov")
            .await
            .is_err(),
        "LLM governance"
    );
    assert!(
        usage::collect_effective_media_governance(&pool, &group)
            .await
            .is_err(),
        "media governance (group chain)"
    );
    assert!(
        usage::collect_effective_media_governance_for_model(&pool, &group, "embedding", "m-gov")
            .await
            .is_err(),
        "media governance (model layer)"
    );
    let mode = skill_orchestrator::fetch_assignment_governance(&pool, RG, "sk-gov").await;
    match mode {
        Err(e) => assert_eq!(e.status_code, 503, "{}", e.message),
        Ok(m) => panic!("an unreadable execution mode read as {m:?}"),
    }
}
