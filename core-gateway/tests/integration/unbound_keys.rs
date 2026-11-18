// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Every call is bound to a resource group; the platform service key
//! is the one exception, billed to the tenant's Unbound group.
//!
//! The defects this pins (all present at v0.11.21):
//!
//! * A key bound to NO group was accepted. On the LLM path it was billed to
//!   whichever group held the model (`LIMIT 1`, no order); on the MCP, A2A
//!   and skill paths it fell back to the Default group's access and limits.
//!   A call no group governs escapes every group's limits and policies.
//! * A key whose group was deactivated or deleted kept working, through
//!   Default.
//! * The health-probe exemption (skip "is this model granted to you") went
//!   to any unbound `global` key plus a header the caller sets; the
//!   control plane's own probe key was bound to Default, so a model outside
//!   Default could not be health-checked at all.
//! * The probe header alone made a call skip metric and usage-ledger writes,
//!   and any key could write probe results for a model.
//! * The routes the control plane calls (audit, evidence, assurance)
//!   accepted ANY `global` key, and a tenant admin mints those.
//!
//! Each refusal is checked for its status, its reason, and that the
//! upstream provider was never called.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

const T: &str = "t-unbound";
const TEAM: &str = "rg-team";
const UNBOUND: &str = "rg-unbound-sys";
const NOT_BOUND_MSG: &str = "not bound to a resource group";
const INACTIVE_MSG: &str = "inactive or no longer exists";

fn chat_fixture() -> Value {
    json!({
        "id": "chatcmpl-unbound", "object": "chat.completion", "created": 1_700_000_000,
        "model": "test-gpt",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    })
}

fn embeddings_fixture() -> Value {
    json!({
        "object": "list", "model": "test-embed",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
        "usage": {"prompt_tokens": 2, "total_tokens": 2}
    })
}

/// An upstream that answers chat and embeddings; tests assert how many
/// requests it received.
async fn upstream() -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(chat_fixture()))
        .mount(&mock)
        .await;
    Mock::given(method("POST"))
        .and(wm_path("/embeddings"))
        .respond_with(ResponseTemplate::new(200).set_body_json(embeddings_fixture()))
        .mount(&mock)
        .await;
    mock
}

struct Keys {
    bound: String,
    unbound: String,
    inactive: String,
    platform: String,
}

/// Tenant; a team group holding `m-team`; the Unbound system group; a model
/// `m-orphan` granted to no group; and one key of every kind.
async fn topology(pool: &PgPool, mock: &MockServer) -> Keys {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, TEAM, T).await;
    common::seed_resource_group(pool, "rg-gone", T).await;
    common::seed_unbound_system_group(pool, UNBOUND, T).await;
    common::seed_llm_provider(pool, "prov-u", T, "openai").await;
    common::seed_llm_model_with_api_url(pool, "m-team", T, "prov-u", "test-gpt", &mock.uri()).await;
    common::seed_llm_model_with_api_url(pool, "m-orphan", T, "prov-u", "test-gpt", &mock.uri())
        .await;
    common::bind_llm_model_to_rg(pool, "m-team", TEAM).await;
    common::bind_llm_model_to_rg(pool, "m-team", "rg-gone").await;
    let keys = Keys {
        bound: common::seed_key(pool, T, Some(TEAM), false, "bound").await,
        unbound: common::seed_key(pool, T, None, false, "unbound").await,
        inactive: common::seed_key(pool, T, Some("rg-gone"), false, "inactive").await,
        platform: common::seed_key(pool, T, None, true, "system-health-check").await,
    };
    sqlx::query("UPDATE resource_groups SET is_active = false WHERE id = 'rg-gone'")
        .execute(pool)
        .await
        .unwrap();
    keys
}

fn post(path: &str, body: Value, key: &str, probe: bool) -> Request<Body> {
    let mut req = common::build_post_request(path, body, Some(key), Some(T));
    if probe {
        req.headers_mut()
            .insert("x-brutor-internal-health-check", "1".parse().unwrap());
    }
    req
}

async fn send(app: axum::Router, req: Request<Body>) -> (StatusCode, String) {
    let resp = app.oneshot(req).await.expect("router");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (status, String::from_utf8_lossy(&bytes).into_owned())
}

/// The group the call's audit row was billed to (the background writer is
/// asynchronous).
async fn billed_group(pool: &PgPool, endpoint_like: &str) -> Option<String> {
    for _ in 0..50 {
        let row: Option<(Option<String>,)> = sqlx::query_as(
            "SELECT resource_group_id FROM proxy_logs \
             WHERE tenant_id = $1 AND endpoint LIKE $2 AND http_status_code = 200 \
             ORDER BY timestamp DESC LIMIT 1",
        )
        .bind(T)
        .bind(endpoint_like)
        .fetch_optional(pool)
        .await
        .unwrap();
        if let Some((g,)) = row {
            return g;
        }
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    }
    panic!("no successful proxy_logs row for {endpoint_like}");
}

// ---------------------------------------------------------------------------
// Every surface refuses a key no group governs, before anything runs
// ---------------------------------------------------------------------------

async fn assert_refused_everywhere(pool: &PgPool, mock: &MockServer, key: &str, reason: &str) {
    let mut req_anthropic = common::build_post_request(
        "/v1/messages",
        json!({"model": "test-gpt", "max_tokens": 10,
               "messages": [{"role": "user", "content": "hi"}]}),
        None,
        Some(T),
    );
    req_anthropic
        .headers_mut()
        .insert("x-api-key", key.parse().unwrap());
    let state = || common::build_test_state(pool.clone());
    let cases: Vec<(&str, axum::Router, Request<Body>)> = vec![
        (
            "llm chat",
            common::build_llm_app(state()),
            post(
                "/v1/proxy/llm/chat/completions",
                json!({"model_id": "m-team", "messages": [{"role": "user", "content": "hi"}]}),
                key,
                false,
            ),
        ),
        (
            "llm responses",
            common::build_llm_app(state()),
            post(
                "/v1/proxy/llm/responses",
                json!({"model_id": "m-team", "input": "hi"}),
                key,
                false,
            ),
        ),
        (
            "llm embeddings",
            common::build_llm_app(state()),
            post(
                "/v1/proxy/llm/embeddings",
                json!({"model_id": "m-team", "input": "hi"}),
                key,
                false,
            ),
        ),
        (
            "anthropic messages",
            common::build_anthropic_app(state()),
            req_anthropic,
        ),
        (
            "gemini",
            common::build_gemini_app(state()),
            post(
                "/v1beta/models/test-gpt:generateContent",
                json!({"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}),
                key,
                false,
            ),
        ),
        (
            "images",
            common::build_multimodal_app(state()),
            post(
                "/v1/proxy/llm/images/generations",
                json!({"model": "test-gpt", "prompt": "a cat"}),
                key,
                false,
            ),
        ),
        (
            "skills",
            common::build_multimodal_app(state()),
            post(
                "/v1/proxy/skills/execute",
                json!({"skill_id": "s-1", "input": {}}),
                key,
                false,
            ),
        ),
        (
            "mcp",
            common::build_mcp_proxy_app(state()),
            post(
                "/v1/proxy/mcp/srv-1",
                json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                key,
                false,
            ),
        ),
        (
            "a2a v1",
            common::build_a2a_v1_app(state()),
            post(
                &format!("/a2a/{T}/card-1/message:send"),
                json!({"message": {"role": "user", "messageId": "m1",
                                    "parts": [{"kind": "text", "text": "hi"}]}}),
                key,
                false,
            ),
        ),
    ];
    for (name, app, req) in cases {
        let (status, body) = send(app, req).await;
        assert_eq!(status, StatusCode::FORBIDDEN, "{name}: {body}");
        assert!(
            body.contains(reason),
            "{name}: refusal names the reason: {body}"
        );
    }
    assert_eq!(
        mock.received_requests().await.unwrap().len(),
        0,
        "no refused call reached the provider"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_key_no_group_governs_is_refused_on_every_surface(pool: PgPool) {
    let mock = upstream().await;
    let keys = topology(&pool, &mock).await;
    assert_refused_everywhere(&pool, &mock, &keys.unbound, NOT_BOUND_MSG).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_key_whose_group_was_deactivated_is_refused_on_every_surface(pool: PgPool) {
    let mock = upstream().await;
    let keys = topology(&pool, &mock).await;
    assert_refused_everywhere(&pool, &mock, &keys.inactive, INACTIVE_MSG).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_bound_key_still_works_and_is_billed_to_its_own_group(pool: PgPool) {
    let mock = upstream().await;
    let keys = topology(&pool, &mock).await;
    let (status, body) = send(
        common::build_llm_app(common::build_test_state(pool.clone())),
        post(
            "/v1/proxy/llm/chat/completions",
            json!({"model_id": "m-team", "messages": [{"role": "user", "content": "hi"}]}),
            &keys.bound,
            false,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(
        billed_group(&pool, "%chat/completions%").await.as_deref(),
        Some(TEAM)
    );
}

// ---------------------------------------------------------------------------
// The platform service key: billed to Unbound, probe exemption only for it
// ---------------------------------------------------------------------------

/// Was: billed to whichever group held the model. Judge and embedding spend
/// landed on a team's budget.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_platform_key_is_billed_to_the_unbound_group(pool: PgPool) {
    let mock = upstream().await;
    let keys = topology(&pool, &mock).await;
    let (status, body) = send(
        common::build_llm_app(common::build_test_state(pool.clone())),
        post(
            "/v1/proxy/llm/chat/completions",
            json!({"model_id": "m-team", "messages": [{"role": "user", "content": "judge"}]}),
            &keys.platform,
            false,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(
        billed_group(&pool, "%chat/completions%").await.as_deref(),
        Some(UNBOUND)
    );

    let (status, body) = send(
        common::build_llm_app(common::build_test_state(pool.clone())),
        post(
            "/v1/proxy/llm/embeddings",
            json!({"model_id": "m-team", "input": "kb chunk"}),
            &keys.platform,
            false,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(
        billed_group(&pool, "%embeddings%").await.as_deref(),
        Some(UNBOUND)
    );
}

/// The health check exists for a model in no group yet. Was: refused,
/// because the probe key was bound to Default.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_platform_probe_tests_a_model_granted_to_no_group(pool: PgPool) {
    let mock = upstream().await;
    let keys = topology(&pool, &mock).await;
    let (status, body) = send(
        common::build_llm_app(common::build_test_state(pool.clone())),
        post(
            "/v1/proxy/llm/chat/completions",
            json!({"model_id": "m-orphan", "messages": [{"role": "user", "content": "ping"}]}),
            &keys.platform,
            true,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(
        billed_group(&pool, "%chat/completions%").await.as_deref(),
        Some(UNBOUND)
    );
}

/// Without the probe header the platform key gets no exemption: a model
/// granted to no group is refused like anyone else's call.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_platform_key_without_the_probe_header_needs_a_granted_model(pool: PgPool) {
    let mock = upstream().await;
    let keys = topology(&pool, &mock).await;
    let (status, body) = send(
        common::build_llm_app(common::build_test_state(pool.clone())),
        post(
            "/v1/proxy/llm/chat/completions",
            json!({"model_id": "m-orphan", "messages": [{"role": "user", "content": "hi"}]}),
            &keys.platform,
            false,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert_eq!(mock.received_requests().await.unwrap().len(), 0);
}

/// The header proves nothing on a tenant's key: no access exemption.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_probe_header_on_a_tenant_key_grants_nothing(pool: PgPool) {
    let mock = upstream().await;
    let keys = topology(&pool, &mock).await;
    let (status, body) = send(
        common::build_llm_app(common::build_test_state(pool.clone())),
        post(
            "/v1/proxy/llm/chat/completions",
            json!({"model_id": "m-orphan", "messages": [{"role": "user", "content": "hi"}]}),
            &keys.bound,
            true,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert!(body.contains("does not grant access to model"), "{body}");
    assert_eq!(mock.received_requests().await.unwrap().len(), 0);
}

/// A tenant's own group that is merely called "Unbound" is not the system
/// group: the platform key is never billed to it.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_tenant_group_named_unbound_is_not_taken_for_the_system_group(pool: PgPool) {
    let mock = upstream().await;
    let keys = topology(&pool, &mock).await;
    sqlx::query(
        "INSERT INTO resource_groups (id, tenant_id, hierarchy_level, inherit_from_parent, \
         name, is_active, is_system) VALUES ('rg-impostor', $1, 0, false, 'Unbound', true, false)",
    )
    .bind(T)
    .execute(&pool)
    .await
    .unwrap();
    let (status, body) = send(
        common::build_llm_app(common::build_test_state(pool.clone())),
        post(
            "/v1/proxy/llm/chat/completions",
            json!({"model_id": "m-team", "messages": [{"role": "user", "content": "hi"}]}),
            &keys.platform,
            false,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(
        billed_group(&pool, "%chat/completions%").await.as_deref(),
        Some(UNBOUND)
    );
}

/// With no Unbound group (its seed failed) the platform key's call is
/// refused as an engine failure, never billed to some other group.
#[sqlx::test(migrations = "./tests/migrations")]
async fn without_an_unbound_group_the_platform_call_is_refused_not_misbilled(pool: PgPool) {
    let mock = upstream().await;
    let keys = topology(&pool, &mock).await;
    sqlx::query("DELETE FROM resource_groups WHERE id = $1")
        .bind(UNBOUND)
        .execute(&pool)
        .await
        .unwrap();
    let (status, body) = send(
        common::build_llm_app(common::build_test_state(pool.clone())),
        post(
            "/v1/proxy/llm/chat/completions",
            json!({"model_id": "m-team", "messages": [{"role": "user", "content": "hi"}]}),
            &keys.platform,
            false,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    assert_eq!(mock.received_requests().await.unwrap().len(), 0);
}

// ---------------------------------------------------------------------------
// Probe results and control-plane routes: the platform key only
// ---------------------------------------------------------------------------

/// Was: any key in the tenant could write a model's probe results (its
/// health counters and usage rows).
#[sqlx::test(migrations = "./tests/migrations")]
async fn only_the_platform_key_records_probe_results(pool: PgPool) {
    let mock = upstream().await;
    let keys = topology(&pool, &mock).await;
    let body =
        json!({"success": true, "status_code": 200, "latency_ms": 12.0, "probe_type": "endpoint"});
    let path = "/v1/proxy/llm/admin/m-team/record-probe-result";
    let (status, text) = send(
        common::build_llm_app(common::build_test_state(pool.clone())),
        post(path, body.clone(), &keys.bound, false),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{text}");
    let (status, text) = send(
        common::build_llm_app(common::build_test_state(pool.clone())),
        post(path, body, &keys.platform, false),
    )
    .await;
    assert!(status.is_success(), "{status}: {text}");
}

#[test]
fn the_probe_header_counts_only_on_the_platform_key() {
    use brutor_gateway_core::services::catalog_probe::is_health_probe_request;
    let mut headers = axum::http::HeaderMap::new();
    headers.insert("x-brutor-internal-health-check", "1".parse().unwrap());
    let mut auth = brutor_gateway_core::models::auth::AuthContext {
        credential_kind: brutor_gateway_core::models::auth::CredentialKind::ApiKey,
        credential_assurance: Some(brutor_gateway_core::models::auth::CredentialAssurance::Key),
        credential_ref: None,
        credential_detail: Default::default(),
        identity_verdict: None,
        auth_type: brutor_gateway_core::models::auth::AuthType::ApiKey,
        tenant_id: T.to_string(),
        gateway_user_id: None,
        gateway_username: None,
        gateway_tenant_id: None,
        idp_user_id: None,
        idp_username: None,
        idp_email: None,
        api_key_id: Some("k".into()),
        api_key_name: None,
        api_key_scope: Some("global".into()),
        api_key_group_id: Some(TEAM.into()),
        api_key_is_platform: false,
        api_key_user_id: None,
        allowed_skill_ids: None,
        session_key: None,
        client_ip: None,
        resolved_group_id: None,
        actor_agent_id: None,
        session_token_type: None,
        agent_impl: Default::default(),
        admission_hold: None,
    };
    assert!(
        !is_health_probe_request(&headers, &auth),
        "a tenant key with the header is not a probe: its metrics and usage are written"
    );
    auth.api_key_group_id = None;
    auth.api_key_is_platform = true;
    auth.credential_kind = brutor_gateway_core::models::auth::CredentialKind::PlatformService;
    auth.credential_assurance = None;
    assert!(is_health_probe_request(&headers, &auth));
    assert!(!is_health_probe_request(
        &axum::http::HeaderMap::new(),
        &auth
    ));
}

// ---------------------------------------------------------------------------
// The semantic-policy judge is a platform call
// ---------------------------------------------------------------------------

/// The judge's self-call, as `semantic_policy::SelfCallJudge` sends it: the
/// caller's own credentials, the process's internal token, the model by id.
fn judge_self_call(key: &str, model_id: &str) -> Request<Body> {
    let mut req = post(
        "/v1/proxy/llm/chat/completions",
        json!({"model_id": model_id, "temperature": 0, "max_tokens": 50,
               "messages": [{"role": "user", "content": "Rule on this content."}]}),
        key,
        false,
    );
    req.headers_mut().insert(
        "x-brutor-semantic-policy",
        brutor_gateway_core::services::internal_call::token()
            .parse()
            .unwrap(),
    );
    req
}

/// Was: the judge ran under the CALLER's access. A tenant-wide policy whose
/// judge model is assigned only to another group 403'd for every caller
/// outside it, so the policy could not evaluate (refusing every call when
/// enforcing and fail-closed). The judge now runs as a platform call, billed
/// to Unbound, needing its model granted to some group, not to the caller's.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_policy_judge_runs_as_the_platform_not_as_the_caller(pool: PgPool) {
    let mock = upstream().await;
    let keys = topology(&pool, &mock).await;
    // The judge model lives only in another team's group.
    common::seed_resource_group(&pool, "rg-other", T).await;
    common::seed_llm_model_with_api_url(&pool, "m-judge", T, "prov-u", "test-gpt", &mock.uri())
        .await;
    common::bind_llm_model_to_rg(&pool, "m-judge", "rg-other").await;

    let (status, body) = send(
        common::build_llm_app(common::build_test_state(pool.clone())),
        judge_self_call(&keys.bound, "m-judge"),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::OK,
        "the judge reaches its model: {body}"
    );
    assert_eq!(
        billed_group(&pool, "%chat/completions%").await.as_deref(),
        Some(UNBOUND)
    );
}

/// The internal token is what makes it the judge: the same request without
/// it is the caller's own call, refused for a model its group lacks.
#[sqlx::test(migrations = "./tests/migrations")]
async fn without_the_internal_token_the_caller_gets_no_judge_access(pool: PgPool) {
    let mock = upstream().await;
    let keys = topology(&pool, &mock).await;
    common::seed_resource_group(&pool, "rg-other", T).await;
    common::seed_llm_model_with_api_url(&pool, "m-judge", T, "prov-u", "test-gpt", &mock.uri())
        .await;
    common::bind_llm_model_to_rg(&pool, "m-judge", "rg-other").await;
    let mut req = judge_self_call(&keys.bound, "m-judge");
    req.headers_mut()
        .insert("x-brutor-semantic-policy", "guessed".parse().unwrap());
    let (status, body) = send(
        common::build_llm_app(common::build_test_state(pool.clone())),
        req,
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert_eq!(mock.received_requests().await.unwrap().len(), 0);
}

// ---------------------------------------------------------------------------
// A health probe's real cost reaches the usage ledger, once
// ---------------------------------------------------------------------------

/// Usage rows for the probe call, waited for (the writer is asynchronous).
async fn usage_rows(pool: &PgPool, want: usize) -> Vec<(Option<String>, Option<i32>, Option<f64>)> {
    let mut rows = Vec::new();
    for _ in 0..50 {
        rows = sqlx::query_as(
            "SELECT group_id, total_tokens, estimated_cost FROM usage_logs \
              WHERE tenant_id = $1 ORDER BY created_at",
        )
        .bind(T)
        .fetch_all(pool)
        .await
        .unwrap();
        if rows.len() >= want {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    }
    rows
}

/// Was: an endpoint probe (a real generation) skipped the route's usage
/// write, and the control plane's `record-probe-result` wrote a row with
/// zero tokens and zero cost, so the Unbound group's Usage tab showed $0
/// while the provider billed every probe (~$3.70 overnight on the dev
/// stack). The route now records the probe's real tokens and cost, billed
/// to Unbound, and the central recorder adds no second row for it.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_endpoint_probe_records_its_real_cost_once(pool: PgPool) {
    let mock = upstream().await;
    let keys = topology(&pool, &mock).await;
    sqlx::query(
        // Prices are per million tokens: $1 in, $2 out.
        "UPDATE llm_model SET cost_per_input_token = 1.0, cost_per_output_token = 2.0 \
          WHERE id = 'm-team'",
    )
    .execute(&pool)
    .await
    .unwrap();

    let (status, body) = send(
        common::build_llm_app(common::build_test_state(pool.clone())),
        post(
            "/v1/proxy/llm/chat/completions",
            json!({"model_id": "m-team", "max_tokens": 5,
                   "messages": [{"role": "user", "content": "test"}]}),
            &keys.platform,
            true,
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    // What the control plane sends after every probe.
    let (status, body) = send(
        common::build_llm_app(common::build_test_state(pool.clone())),
        post(
            "/v1/proxy/llm/admin/m-team/record-probe-result",
            json!({"success": true, "status_code": 200, "latency_ms": 12.0, "probe_type": "endpoint"}),
            &keys.platform,
            false,
        ),
    )
    .await;
    assert!(status.is_success(), "{status}: {body}");

    let rows = usage_rows(&pool, 1).await;
    tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    let rows_after = usage_rows(&pool, 1).await;
    assert_eq!(rows_after.len(), 1, "one usage row per probe: {rows:?}");
    let (group, tokens, cost) = &rows_after[0];
    assert_eq!(group.as_deref(), Some(UNBOUND));
    assert_eq!(*tokens, Some(15));
    // Prices are per million tokens: (10 × $1 + 5 × $2) / 1M
    assert!(
        (cost.unwrap_or(0.0) - 2e-5).abs() < 1e-12,
        "real cost: {cost:?}"
    );
}

/// A catalog probe (the provider's /models listing) costs nothing and still
/// leaves its one zero-cost row, as before.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_catalog_probe_still_records_one_free_row(pool: PgPool) {
    let mock = upstream().await;
    let keys = topology(&pool, &mock).await;
    let (status, body) = send(
        common::build_llm_app(common::build_test_state(pool.clone())),
        post(
            "/v1/proxy/llm/admin/m-team/record-probe-result",
            json!({"success": true, "status_code": 200, "latency_ms": 9.0, "probe_type": "catalog"}),
            &keys.platform,
            false,
        ),
    )
    .await;
    assert!(status.is_success(), "{status}: {body}");
    let rows = usage_rows(&pool, 1).await;
    assert_eq!(rows.len(), 1, "{rows:?}");
    assert_eq!(rows[0].2.unwrap_or(0.0), 0.0);
}
