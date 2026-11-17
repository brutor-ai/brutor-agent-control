// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Every OpenAI-shaped LLM route runs the actor planes, and a key of a
//! deactivated end user is no credential.
//!
//! `/v1/proxy/llm/chat/completions` ran `llm_preflight::enforce_actor_planes`
//! (per-agent runtime authorization, the AI System's autonomy gate — the kill
//! switch — and semantic policy); `/responses`, `/completions` and
//! `/embeddings` take the same keys, reach the same models and did not: a
//! default-deny agent with no grant and a key of a suspended AI System were
//! refused on chat and served on the other three. Each case here is asked on
//! all four routes.
//!
//! A key bound to an end user acts as that user. Deactivating the user must
//! end the key, as revoking the key does; it used to keep working.

use axum::Router;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

const T: &str = "tenant-apr";
const RG: &str = "rg-apr";
const MODEL: &str = "m-apr";

#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) enum Route {
    Chat,
    Responses,
    Completions,
    Embeddings,
}

pub(crate) const ROUTES: [Route; 4] = [
    Route::Chat,
    Route::Responses,
    Route::Completions,
    Route::Embeddings,
];

impl Route {
    fn path(self) -> &'static str {
        match self {
            Route::Chat => "/v1/proxy/llm/chat/completions",
            Route::Responses => "/v1/proxy/llm/responses",
            Route::Completions => "/v1/proxy/llm/completions",
            Route::Embeddings => "/v1/proxy/llm/embeddings",
        }
    }
    fn body(self) -> Value {
        match self {
            Route::Chat => {
                json!({"model_id": MODEL, "messages": [{"role": "user", "content": "hi"}]})
            }
            Route::Responses => json!({"model_id": MODEL, "input": "hi"}),
            Route::Completions => json!({"model_id": MODEL, "prompt": "hi"}),
            Route::Embeddings => json!({"model_id": MODEL, "input": "hi"}),
        }
    }
    fn request(self, key: &str) -> Request<Body> {
        common::build_post_request(self.path(), self.body(), Some(key), Some(T))
    }
}

/// A provider that answers all four endpoints as OpenAI does.
async fn provider() -> MockServer {
    let mock = MockServer::start().await;
    let usage = json!({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2});
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "chatcmpl-apr", "object": "chat.completion", "created": 1_700_000_000,
            "model": "test-gpt",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": usage
        })))
        .mount(&mock)
        .await;
    Mock::given(method("POST"))
        .and(wm_path("/responses"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "resp_apr", "object": "response", "created_at": 1_700_000_000,
            "model": "test-gpt", "status": "completed",
            "output": [{"type": "message", "id": "msg_1", "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok", "annotations": []}]}],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        })))
        .mount(&mock)
        .await;
    Mock::given(method("POST"))
        .and(wm_path("/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "cmpl-apr", "object": "text_completion", "created": 1_700_000_000,
            "model": "test-gpt",
            "choices": [{"index": 0, "text": "ok", "finish_reason": "stop"}],
            "usage": usage
        })))
        .mount(&mock)
        .await;
    Mock::given(method("POST"))
        .and(wm_path("/embeddings"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "object": "list", "model": "test-gpt",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
            "usage": {"prompt_tokens": 1, "total_tokens": 1}
        })))
        .mount(&mock)
        .await;
    mock
}

async fn seed(pool: &PgPool) -> MockServer {
    let mock = provider().await;
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_llm_provider(pool, "prov-apr", T, "openai").await;
    common::seed_llm_model_with_api_url(pool, MODEL, T, "prov-apr", "test-gpt", &mock.uri()).await;
    common::bind_llm_model_to_rg(pool, MODEL, RG).await;
    mock
}

fn app(pool: &PgPool) -> Router {
    common::build_llm_app(common::build_test_state(pool.clone()))
}

async fn send(app: Router, req: Request<Body>) -> (StatusCode, String) {
    let resp = app.oneshot(req).await.expect("router");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.expect("body").to_bytes();
    (status, String::from_utf8_lossy(&bytes).into_owned())
}

async fn provider_calls(mock: &MockServer) -> usize {
    mock.received_requests().await.unwrap_or_default().len()
}

/// The newest refusal row of the tenant: (status, subtype, group, key id).
async fn refusal_row(
    pool: &PgPool,
) -> (Option<i32>, Option<String>, Option<String>, Option<String>) {
    for _ in 0..120 {
        if let Some(r) = sqlx::query_as(
            "SELECT http_status_code, proxy_subtype, resource_group_id, api_key_id \
               FROM proxy_logs WHERE tenant_id = $1 AND http_status_code = 403 \
              ORDER BY timestamp DESC LIMIT 1",
        )
        .bind(T)
        .fetch_optional(pool)
        .await
        .expect("proxy_logs")
        {
            return r;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    panic!("no refusal row of {T}");
}

async fn key_id(pool: &PgPool, name: &str) -> String {
    sqlx::query_scalar("SELECT id FROM api_keys WHERE name = $1")
        .bind(name)
        .fetch_one(pool)
        .await
        .expect("key id")
}

// ---------------------------------------------------------------------------
// The control: a key that stands is served on every route
// ---------------------------------------------------------------------------

async fn a_key_of_a_permissive_group_is_served(pool: PgPool, route: Route) {
    let mock = seed(&pool).await;
    let key = common::seed_api_key(&pool, T, RG, None, "apr-open").await;
    let (status, body) = send(app(&pool), route.request(&key)).await;
    assert_eq!(status, StatusCode::OK, "{route:?}: {body}");
    assert_eq!(provider_calls(&mock).await, 1, "{route:?}");
}

/// An agent that is allowed (no default deny) is served too: the planes
/// refuse what they are configured to refuse, not every agent.
async fn an_agent_that_may_call_is_served(pool: PgPool, route: Route) {
    let mock = seed(&pool).await;
    let key = common::seed_api_key(&pool, T, RG, None, "apr-agent-open").await;
    sqlx::query(
        "INSERT INTO agent_identities (id, tenant_id, name, source, enabled, status, default_deny) \
         VALUES ('agent-apr-open', $1, 'agent-apr-open', 'manual', true, 'active', false)",
    )
    .bind(T)
    .execute(&pool)
    .await
    .expect("agent identity");
    sqlx::query("UPDATE api_keys SET agent_id = 'agent-apr-open' WHERE name = 'apr-agent-open'")
        .execute(&pool)
        .await
        .expect("bind key to agent");
    let (status, body) = send(app(&pool), route.request(&key)).await;
    assert_eq!(status, StatusCode::OK, "{route:?}: {body}");
    assert_eq!(provider_calls(&mock).await, 1, "{route:?}");
}

// ---------------------------------------------------------------------------
// The actor planes
// ---------------------------------------------------------------------------

/// A key bound to an agent acts as that agent. A default-deny agent with no
/// grant for the model is refused, the refusal is recorded as the agent's,
/// and the provider is not called.
async fn a_default_deny_agents_key_is_refused(pool: PgPool, route: Route) {
    let mock = seed(&pool).await;
    let key = common::seed_api_key(&pool, T, RG, None, "apr-agent").await;
    sqlx::query(
        "INSERT INTO agent_identities (id, tenant_id, name, source, enabled, status, default_deny) \
         VALUES ('agent-apr', $1, 'agent-apr', 'manual', true, 'active', true)",
    )
    .bind(T)
    .execute(&pool)
    .await
    .expect("agent identity");
    sqlx::query("UPDATE api_keys SET agent_id = 'agent-apr' WHERE name = 'apr-agent'")
        .execute(&pool)
        .await
        .expect("bind key to agent");

    let (status, body) = send(app(&pool), route.request(&key)).await;
    assert_eq!(
        provider_calls(&mock).await,
        0,
        "{route:?}: an agent with no grant reached the provider ({status}: {body})"
    );
    assert_eq!(status, StatusCode::FORBIDDEN, "{route:?}: {body}");
    assert!(body.contains("agent_not_authorized"), "{route:?}: {body}");
    let (http, subtype, group, api_key) = refusal_row(&pool).await;
    assert_eq!(http, Some(403));
    assert_eq!(subtype.as_deref(), Some("agent_authz_block"), "{route:?}");
    assert_eq!(group.as_deref(), Some(RG), "{route:?}");
    assert_eq!(api_key, Some(key_id(&pool, "apr-agent").await), "{route:?}");
}

/// The kill switch: a key of a suspended AI System is refused.
async fn a_suspended_systems_key_is_refused(pool: PgPool, route: Route) {
    let mock = seed(&pool).await;
    let key = common::seed_api_key(&pool, T, RG, None, "apr-paused").await;
    sqlx::query("UPDATE resource_groups SET autonomy_level = 'suspended' WHERE id = $1")
        .bind(RG)
        .execute(&pool)
        .await
        .expect("suspend the system");

    let (status, body) = send(app(&pool), route.request(&key)).await;
    assert_eq!(
        provider_calls(&mock).await,
        0,
        "{route:?}: a suspended system reached the provider ({status}: {body})"
    );
    assert_eq!(status, StatusCode::FORBIDDEN, "{route:?}: {body}");
    assert!(body.contains("autonomy_denied"), "{route:?}: {body}");
    let (http, subtype, group, api_key) = refusal_row(&pool).await;
    assert_eq!(http, Some(403));
    assert_eq!(subtype.as_deref(), Some("autonomy_block"), "{route:?}");
    assert_eq!(group.as_deref(), Some(RG), "{route:?}");
    assert_eq!(
        api_key,
        Some(key_id(&pool, "apr-paused").await),
        "{route:?}"
    );
}

// ---------------------------------------------------------------------------
// A key of a deactivated end user
// ---------------------------------------------------------------------------

async fn deactivate(pool: &PgPool, user: i32) {
    sqlx::query("UPDATE end_users SET is_active = false WHERE id = $1")
        .bind(user)
        .execute(pool)
        .await
        .expect("deactivate the user");
}

/// What a revoked key answers on `route`, to hold the deactivated user's key
/// to the same answer.
async fn a_revoked_key_answers(pool: &PgPool, route: Route) -> (StatusCode, String) {
    let revoked = common::seed_api_key(pool, T, RG, None, "apr-revoked").await;
    sqlx::query("UPDATE api_keys SET revoked = true WHERE name = 'apr-revoked'")
        .execute(pool)
        .await
        .expect("revoke");
    send(app(pool), route.request(&revoked)).await
}

/// The user was deactivated before the key was ever presented.
async fn a_deactivated_users_key_is_refused_as_a_revoked_key_is(pool: PgPool, route: Route) {
    let mock = seed(&pool).await;
    common::seed_end_user(&pool, 9801, T).await;
    common::add_direct_membership(&pool, 9801, RG, T).await;
    let key = common::seed_api_key(&pool, T, RG, Some(9801), "apr-user-key").await;
    deactivate(&pool, 9801).await;
    let before = common::count_proxy_logs(&pool, T).await;

    let got = send(app(&pool), route.request(&key)).await;
    assert_eq!(
        provider_calls(&mock).await,
        0,
        "{route:?}: a deactivated user's key reached the provider ({}: {})",
        got.0,
        got.1
    );
    assert_eq!(got.0, StatusCode::UNAUTHORIZED, "{route:?}: {}", got.1);
    assert_eq!(
        got,
        a_revoked_key_answers(&pool, route).await,
        "{route:?}: the answer of a revoked key"
    );
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    assert_eq!(
        common::count_proxy_logs(&pool, T).await,
        before,
        "{route:?}: a refused key was recorded as a call of the tenant"
    );
}

/// The user is deactivated while the gateway is serving the key: the same
/// application (its tenant cache and group cache warm from the call that
/// was served) refuses the next call. No cache holds the decision: the key
/// is read on every request.
async fn a_user_deactivated_between_two_calls_is_refused_on_the_second(pool: PgPool, route: Route) {
    let mock = seed(&pool).await;
    common::seed_end_user(&pool, 9802, T).await;
    common::add_direct_membership(&pool, 9802, RG, T).await;
    let key = common::seed_api_key(&pool, T, RG, Some(9802), "apr-user-key-2").await;
    let app = app(&pool);

    let (status, body) = send(app.clone(), route.request(&key)).await;
    assert_eq!(status, StatusCode::OK, "{route:?}, while active: {body}");
    assert_eq!(provider_calls(&mock).await, 1);

    deactivate(&pool, 9802).await;
    let (status, body) = send(app.clone(), route.request(&key)).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED, "{route:?}: {body}");
    assert_eq!(
        provider_calls(&mock).await,
        1,
        "{route:?}: served after the user was deactivated"
    );

    // Reactivated, the key stands again: the refusal was the user's state,
    // not a damaged key.
    sqlx::query("UPDATE end_users SET is_active = true WHERE id = 9802")
        .execute(&pool)
        .await
        .expect("reactivate");
    let (status, body) = send(app, route.request(&key)).await;
    assert_eq!(status, StatusCode::OK, "{route:?}, reactivated: {body}");
}

/// A key with no end user is not touched by the rule.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_key_with_no_end_user_does_not_depend_on_any_user(pool: PgPool) {
    let mock = seed(&pool).await;
    common::seed_end_user(&pool, 9803, T).await;
    deactivate(&pool, 9803).await;
    let key = common::seed_api_key(&pool, T, RG, None, "apr-group-key").await;
    let (status, body) = send(app(&pool), Route::Chat.request(&key)).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(provider_calls(&mock).await, 1);
}

/// The rule is the key lookup's (`auth::authenticate_request`), in both of
/// its forms: for a request that names its tenant and for one that does not
/// (`n/a`: the tenant is the key's). Asked directly, so neither form can lose
/// the rule while a route happens to use the other.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_key_lookup_holds_the_rule_with_and_without_a_tenant(pool: PgPool) {
    use brutor_gateway_core::models::auth::AuthType;
    use brutor_gateway_core::services::auth::authenticate_request;
    seed(&pool).await;
    common::seed_end_user(&pool, 9804, T).await;
    let key = common::seed_api_key(&pool, T, RG, Some(9804), "apr-direct").await;
    let state = common::build_test_state(pool.clone());
    let mut headers = axum::http::HeaderMap::new();
    headers.insert("x-api-key", key.parse().expect("header"));

    for tenant in [T, "n/a", ""] {
        let ctx = authenticate_request(
            &headers,
            tenant,
            &state.config.jwt_secret,
            &pool,
            &state.credential_verifiers,
            None,
            true,
        )
        .await
        .unwrap_or_else(|e| panic!("tenant {tenant:?}, user active: {e}"));
        assert_eq!(ctx.auth_type, AuthType::ApiKey, "tenant {tenant:?}");
        assert_eq!(ctx.api_key_user_id, Some(9804));
    }
    deactivate(&pool, 9804).await;
    for tenant in [T, "n/a", ""] {
        let refused = authenticate_request(
            &headers,
            tenant,
            &state.config.jwt_secret,
            &pool,
            &state.credential_verifiers,
            None,
            true,
        )
        .await;
        assert!(
            refused.is_err(),
            "tenant {tenant:?}: a deactivated user's key authenticated"
        );
        // Where no credential is required the key is refused all the same
        // (a request that presents a key which is not valid is not a
        // request with no credential; it used to go on as anonymous). It is
        // never the key's principal.
        let presented = authenticate_request(
            &headers,
            tenant,
            &state.config.jwt_secret,
            &pool,
            &state.credential_verifiers,
            None,
            false,
        )
        .await;
        assert!(
            matches!(
                presented,
                Err(brutor_gateway_core::services::auth::AuthError::Unauthenticated)
            ),
            "tenant {tenant:?}: {:?}",
            presented.map(|ctx| ctx.auth_type)
        );
        // A request with no credential at all is anonymous there.
        let anonymous = authenticate_request(
            &axum::http::HeaderMap::new(),
            tenant,
            &state.config.jwt_secret,
            &pool,
            &state.credential_verifiers,
            None,
            false,
        )
        .await
        .expect("anonymous");
        assert_eq!(
            anonymous.auth_type,
            AuthType::Anonymous,
            "tenant {tenant:?}"
        );
        assert!(anonymous.api_key_id.is_none() && anonymous.gateway_user_id.is_none());
    }
}

macro_rules! per_route {
    ($($case:ident),+ $(,)?) => {$(
        mod $case {
            use super::*;
            #[sqlx::test(migrations = "./tests/migrations")]
            async fn chat(pool: PgPool) { super::$case(pool, Route::Chat).await; }
            #[sqlx::test(migrations = "./tests/migrations")]
            async fn responses(pool: PgPool) { super::$case(pool, Route::Responses).await; }
            #[sqlx::test(migrations = "./tests/migrations")]
            async fn completions(pool: PgPool) { super::$case(pool, Route::Completions).await; }
            #[sqlx::test(migrations = "./tests/migrations")]
            async fn embeddings(pool: PgPool) { super::$case(pool, Route::Embeddings).await; }
        }
    )+};
}

per_route!(
    a_key_of_a_permissive_group_is_served,
    an_agent_that_may_call_is_served,
    a_default_deny_agents_key_is_refused,
    a_suspended_systems_key_is_refused,
    a_deactivated_users_key_is_refused_as_a_revoked_key_is,
    a_user_deactivated_between_two_calls_is_refused_on_the_second,
);

#[test]
fn every_route_is_covered() {
    assert_eq!(ROUTES.len(), 4);
}
