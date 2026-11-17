// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Model access understands workload-identity agents.
//!
//! An agent that authenticates with its own IdP-issued token
//! (`services::workload_identity`) reached no model on any LLM route: model
//! access (`usage::resolve_model_access_with_preferred`) knew a key's group
//! and an end user's memberships, the agent has neither, and the call ended
//! in 403 `access_denied` although the agent is a member of a group that
//! holds the model and holds an `allow` grant for it.
//!
//! The rule: a workload agent acts in ONE resource group per request — the
//! group it names in `X-Resource-Group` when it is a member of it, otherwise
//! its oldest membership — and is governed there exactly as a key of that
//! group is: the group must hold the model, the group's limits, quotas and
//! guardrails apply, the agent's own grants and autonomy decide (default-deny
//! still needs a grant), and every row names the agent and the group. Naming
//! a group it is not a member of is a refusal, on every route; it is never
//! moved to another group.

use axum::Router;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use super::workload_identity as wl;
use crate::common;

const T: &str = "tenant-wma";
const OTHER: &str = "tenant-wma-other";
/// Holds [`MODEL`]. The agent's oldest membership.
const RG: &str = "rg-wma";
/// Holds [`MODEL_B`] only.
const RG_B: &str = "rg-wma-b";
/// Holds no model.
const RG_EMPTY: &str = "rg-wma-empty";
const MODEL: &str = "m-wma";
const MODEL_B: &str = "m-wma-b";
const GEMINI_MODEL: &str = "m-wma-gem";
const AGENT: &str = "agent-wma";
const ISSUER: &str = "https://idp.wma.example/";
const SUB: &str = "spiffe://wma/agent";
const KID: &str = "kid-wma";

#[derive(Clone, Copy, Debug, PartialEq)]
enum Route {
    Chat,
    Responses,
    Completions,
    Embeddings,
    Anthropic,
    Gemini,
}

const ROUTES: [Route; 6] = [
    Route::Chat,
    Route::Responses,
    Route::Completions,
    Route::Embeddings,
    Route::Anthropic,
    Route::Gemini,
];

impl Route {
    fn path(self) -> String {
        match self {
            Route::Chat => "/v1/proxy/llm/chat/completions".into(),
            Route::Responses => "/v1/proxy/llm/responses".into(),
            Route::Completions => "/v1/proxy/llm/completions".into(),
            Route::Embeddings => "/v1/proxy/llm/embeddings".into(),
            Route::Anthropic => "/v1/messages".into(),
            Route::Gemini => format!("/v1beta/models/{GEMINI_MODEL}:generateContent"),
        }
    }

    /// The model the route's request names.
    fn model(self) -> &'static str {
        match self {
            Route::Gemini => GEMINI_MODEL,
            _ => MODEL,
        }
    }

    fn body(self, model: &str, text: &str) -> Value {
        match self {
            Route::Chat => {
                json!({"model_id": model, "messages": [{"role": "user", "content": text}]})
            }
            Route::Responses => json!({"model_id": model, "input": text}),
            Route::Completions => json!({"model_id": model, "prompt": text}),
            Route::Embeddings => json!({"model_id": model, "input": text}),
            Route::Anthropic => json!({
                "model": model, "max_tokens": 16,
                "messages": [{"role": "user", "content": text}]
            }),
            Route::Gemini => json!({"contents": [{"role": "user", "parts": [{"text": text}]}]}),
        }
    }

    fn request(self, token: &str) -> Request<Body> {
        self.request_for(token, T, None)
    }

    fn request_for(self, token: &str, tenant: &str, group: Option<&str>) -> Request<Body> {
        let mut req = common::build_post_request(
            &self.path(),
            self.body(self.model(), "hello"),
            Some(token),
            Some(tenant),
        );
        if let Some(group) = group {
            req.headers_mut()
                .insert("x-resource-group", group.parse().expect("group header"));
        }
        req
    }
}

fn chat(token: &str, model: &str, group: Option<&str>) -> Request<Body> {
    let mut req = common::build_post_request(
        &Route::Chat.path(),
        Route::Chat.body(model, "hello"),
        Some(token),
        Some(T),
    );
    if let Some(group) = group {
        req.headers_mut()
            .insert("x-resource-group", group.parse().expect("group header"));
    }
    req
}

/// A provider that answers every endpoint the six routes call.
async fn provider() -> MockServer {
    let mock = MockServer::start().await;
    let usage = json!({"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5});
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "chatcmpl-wma", "object": "chat.completion", "created": 1_700_000_000,
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
            "id": "resp_wma", "object": "response", "created_at": 1_700_000_000,
            "model": "test-gpt", "status": "completed",
            "output": [{"type": "message", "id": "msg_1", "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok", "annotations": []}]}],
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}
        })))
        .mount(&mock)
        .await;
    Mock::given(method("POST"))
        .and(wm_path("/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "cmpl-wma", "object": "text_completion", "created": 1_700_000_000,
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
            "usage": {"prompt_tokens": 3, "total_tokens": 3}
        })))
        .mount(&mock)
        .await;
    Mock::given(method("POST"))
        .and(wm_path("/v1beta/models/test-gem:generateContent"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "candidates": [{
                "content": {"role": "model", "parts": [{"text": "ok"}]},
                "finishReason": "STOP"
            }],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2,
                              "totalTokenCount": 5}
        })))
        .mount(&mock)
        .await;
    mock
}

struct World {
    mock: MockServer,
    /// Kept alive: the IdP's key set is fetched from it.
    _jwks: MockServer,
}

impl World {
    fn token(&self) -> String {
        token_of(SUB, 3600)
    }

    async fn provider_calls(&self) -> usize {
        self.mock
            .received_requests()
            .await
            .unwrap_or_default()
            .len()
    }
}

fn token_of(sub: &str, expires_in: i64) -> String {
    let exp = (common::db_now().timestamp() + expires_in) as usize;
    wl::mint_token(ISSUER, sub, KID, exp)
}

async fn join(pool: &PgPool, agent: &str, group: &str, tenant: &str) {
    sqlx::query(
        "INSERT INTO resource_group_agent_members (tenant_id, agent_id, group_id, joined_at) \
         VALUES ($1, $2, $3, clock_timestamp())",
    )
    .bind(tenant)
    .bind(agent)
    .bind(group)
    .execute(pool)
    .await
    .expect("membership");
}

async fn grant(pool: &PgPool, agent: &str, model_name: &str) {
    sqlx::query(
        "INSERT INTO agent_grants (id, tenant_id, agent_id, action_type, target, effect, enabled) \
         VALUES ($1, $2, $3, 'llm_call', $4, 'allow', true)",
    )
    .bind(format!("g-{agent}-{model_name}"))
    .bind(T)
    .bind(agent)
    .bind(model_name)
    .execute(pool)
    .await
    .expect("grant");
}

/// The tenant, three groups ([`RG`] holds the models the six routes name,
/// [`RG_B`] holds [`MODEL_B`], [`RG_EMPTY`] none), the IdP, and a
/// default-deny agent of that IdP that is a member of no group and holds no
/// grant: each test gives it what its case needs.
async fn seed(pool: &PgPool) -> World {
    let mock = provider().await;
    common::seed_tenant(pool, T).await;
    common::seed_tenant(pool, OTHER).await;
    for group in [RG, RG_B, RG_EMPTY] {
        common::seed_resource_group(pool, group, T).await;
    }
    common::seed_llm_provider(pool, "prov-wma", T, "openai").await;
    common::seed_llm_model_with_api_url(pool, MODEL, T, "prov-wma", "test-gpt", &mock.uri()).await;
    common::seed_llm_model_with_api_url(pool, MODEL_B, T, "prov-wma", "test-gpt-b", &mock.uri())
        .await;
    common::seed_llm_provider(pool, "prov-wma-gem", T, "gemini").await;
    common::seed_llm_model_with_api_url(
        pool,
        GEMINI_MODEL,
        T,
        "prov-wma-gem",
        "test-gem",
        &mock.uri(),
    )
    .await;
    common::set_model_api_key(pool, GEMINI_MODEL, "dummy-key").await;
    common::bind_llm_model_to_rg(pool, MODEL, RG).await;
    common::bind_llm_model_to_rg(pool, GEMINI_MODEL, RG).await;
    common::bind_llm_model_to_rg(pool, MODEL_B, RG_B).await;

    let jwks = wl::mock_jwks(KID).await;
    wl::seed_idp(pool, "idp-wma", T, ISSUER, &format!("{}/jwks", jwks.uri())).await;
    wl::seed_agent_with_idp(pool, AGENT, T, "idp-wma", SUB).await;
    World { mock, _jwks: jwks }
}

/// The world with the agent a member of [`RG`] and holding an `allow` grant
/// for every model the routes name.
async fn seed_served(pool: &PgPool) -> World {
    let world = seed(pool).await;
    join(pool, AGENT, RG, T).await;
    for model in ["test-gpt", "test-gpt-b", "test-gem"] {
        grant(pool, AGENT, model).await;
    }
    world
}

fn app(pool: &PgPool) -> Router {
    common::edition_app(pool.clone())
}

async fn send(app: Router, req: Request<Body>) -> (StatusCode, String) {
    let resp = app.oneshot(req).await.expect("router");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.expect("body").to_bytes();
    (status, String::from_utf8_lossy(&bytes).into_owned())
}

/// (status, subtype, group, agent, key id) of the tenant's newest row that
/// matches `filter`.
type Row = (
    Option<i32>,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
);

async fn row(pool: &PgPool, filter: &str) -> Row {
    for _ in 0..160 {
        if let Some(r) = sqlx::query_as(&format!(
            "SELECT http_status_code, proxy_subtype, resource_group_id, actor_agent_id, api_key_id \
               FROM proxy_logs WHERE tenant_id = $1 AND {filter} \
              ORDER BY timestamp DESC LIMIT 1"
        ))
        .bind(T)
        .fetch_optional(pool)
        .await
        .expect("proxy_logs")
        {
            return r;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    panic!("no row of {T} where {filter}");
}

// ---------------------------------------------------------------------------
// Served
// ---------------------------------------------------------------------------

/// An agent in a group that holds the model, with an `allow` grant, is
/// served; the row names the agent and the group, and the usage is the
/// group's.
async fn an_agent_of_a_group_that_holds_the_model_is_served(pool: PgPool, route: Route) {
    let world = seed_served(&pool).await;
    let (status, body) = send(app(&pool), route.request(&world.token())).await;
    assert_eq!(status, StatusCode::OK, "{route:?}: {body}");
    assert_eq!(world.provider_calls().await, 1, "{route:?}");

    // `/completions` writes a usage row and no `proxy_logs` row for a call
    // it serves, whoever the caller: its refusals
    // are rows, and are asked for in the cases below.
    if route != Route::Completions {
        let (http, _, group, agent, key) = row(&pool, "http_status_code = 200").await;
        assert_eq!(http, Some(200));
        assert_eq!(
            group.as_deref(),
            Some(RG),
            "{route:?}: the row names the group"
        );
        assert_eq!(
            agent.as_deref(),
            Some(AGENT),
            "{route:?}: the row names the agent"
        );
        assert_eq!(key, None, "{route:?}: no key was presented");
    }

    // The usage is counted against the group, as a group key's is.
    let mut usage: Vec<(Option<String>, Option<i32>)> = Vec::new();
    for _ in 0..160 {
        usage = sqlx::query_as("SELECT group_id, user_id FROM usage_logs WHERE tenant_id = $1")
            .bind(T)
            .fetch_all(&pool)
            .await
            .expect("usage_logs");
        if !usage.is_empty() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    assert_eq!(
        usage,
        vec![(Some(RG.to_string()), None)],
        "{route:?}: one usage row, the group's"
    );
}

/// Default-deny still needs a grant: without one the agent is refused by the
/// actor planes (`agent_authz_block`), not by model access.
async fn without_a_grant_the_agent_is_refused_by_the_actor_planes(pool: PgPool, route: Route) {
    let world = seed(&pool).await;
    join(&pool, AGENT, RG, T).await;
    let (status, body) = send(app(&pool), route.request(&world.token())).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{route:?}: {body}");
    assert!(
        body.contains("agent_not_authorized"),
        "{route:?}: refused by something other than the agent's grants: {body}"
    );
    let (_, subtype, group, agent, _) = row(&pool, "http_status_code = 403").await;
    assert_eq!(subtype.as_deref(), Some("agent_authz_block"), "{route:?}");
    assert_eq!(group.as_deref(), Some(RG), "{route:?}");
    assert_eq!(agent.as_deref(), Some(AGENT), "{route:?}");
    assert_eq!(world.provider_calls().await, 0, "{route:?}");
}

/// An agent whose group does not hold the model is refused at model access.
async fn a_model_the_agents_group_does_not_hold_is_refused(pool: PgPool, route: Route) {
    let world = seed(&pool).await;
    join(&pool, AGENT, RG_EMPTY, T).await;
    for model in ["test-gpt", "test-gem"] {
        grant(&pool, AGENT, model).await;
    }
    let (status, body) = send(app(&pool), route.request(&world.token())).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{route:?}: {body}");
    assert!(
        body.contains("does not grant access to model"),
        "{route:?}: {body}"
    );
    let (_, subtype, _, agent, _) = row(&pool, "http_status_code = 403").await;
    assert_eq!(subtype.as_deref(), Some("access_denied"), "{route:?}");
    assert_eq!(agent.as_deref(), Some(AGENT), "{route:?}");
    assert_eq!(world.provider_calls().await, 0, "{route:?}");
}

/// An agent that is a member of no group reaches no model — also when it
/// names a group that holds the model.
async fn an_agent_in_no_group_is_refused(pool: PgPool, route: Route) {
    let world = seed(&pool).await;
    for model in ["test-gpt", "test-gem"] {
        grant(&pool, AGENT, model).await;
    }
    let (status, body) = send(app(&pool), route.request(&world.token())).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{route:?}: {body}");
    assert!(
        body.contains("does not grant access to model"),
        "{route:?}: {body}"
    );

    let (status, body) = send(app(&pool), route.request_for(&world.token(), T, Some(RG))).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "{route:?}: an agent in no group names one: {body}"
    );
    assert!(
        body.contains("resource_group_not_member"),
        "{route:?}: {body}"
    );
    assert_eq!(world.provider_calls().await, 0, "{route:?}");
}

/// An agent that names a group it is not a member of is refused — it is not
/// served there, and it is not moved to its own group either.
async fn an_agent_that_names_another_group_is_refused(pool: PgPool, route: Route) {
    let world = seed_served(&pool).await;
    // RG_B exists, is active, and is not the agent's.
    let (status, body) = send(app(&pool), route.request_for(&world.token(), T, Some(RG_B))).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{route:?}: {body}");
    assert!(
        body.contains("resource_group_not_member"),
        "{route:?}: {body}"
    );
    // A group that does not exist at all.
    let (status, body) = send(
        app(&pool),
        route.request_for(&world.token(), T, Some("rg-wma-nowhere")),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{route:?}: {body}");
    assert_eq!(world.provider_calls().await, 0, "{route:?}");
}

/// Tokens that do not stand never reach a model: expired, tampered, of an
/// unknown subject, of a disabled agent, and a valid one presented for
/// another tenant.
async fn tokens_that_do_not_stand_are_refused(pool: PgPool, route: Route) {
    let world = seed_served(&pool).await;
    let valid = world.token();
    let mut tampered = valid.clone();
    tampered.push('x');
    for (what, token, tenant) in [
        ("expired", token_of(SUB, -3600), T),
        ("tampered", tampered, T),
        (
            "an unknown subject",
            token_of("spiffe://wma/nobody", 3600),
            T,
        ),
        ("another tenant", valid.clone(), OTHER),
    ] {
        let (status, body) = send(app(&pool), route.request_for(&token, tenant, None)).await;
        assert_eq!(
            status,
            StatusCode::UNAUTHORIZED,
            "{route:?}, {what}: {body}"
        );
    }
    sqlx::query("UPDATE agent_identities SET enabled = false WHERE id = $1")
        .bind(AGENT)
        .execute(&pool)
        .await
        .expect("disable");
    let (status, body) = send(app(&pool), route.request(&valid)).await;
    assert_eq!(
        status,
        StatusCode::UNAUTHORIZED,
        "{route:?}, disabled: {body}"
    );
    assert_eq!(world.provider_calls().await, 0, "{route:?}");
}

/// An agent identity past its `expires_at` is no credential:
/// its token, valid at the identity provider, is refused as a
/// disabled agent's is, and no model is called. An expiry still ahead, and
/// none at all, serve.
async fn an_agent_identity_past_its_expiry_is_refused(pool: PgPool, route: Route) {
    let world = seed_served(&pool).await;
    let token = world.token();
    let set_expiry = |interval: Option<&'static str>| {
        let pool = pool.clone();
        async move {
            let sql = match interval {
                Some(i) => format!(
                    "UPDATE agent_identities SET expires_at = now() + interval '{i}' WHERE id = $1"
                ),
                None => "UPDATE agent_identities SET expires_at = NULL WHERE id = $1".to_string(),
            };
            sqlx::query(&sql)
                .bind(AGENT)
                .execute(&pool)
                .await
                .expect("set expires_at");
        }
    };

    set_expiry(Some("-1 second")).await;
    let (status, body) = send(app(&pool), route.request(&token)).await;
    assert_eq!(
        status,
        StatusCode::UNAUTHORIZED,
        "{route:?}, identity expired: {body}"
    );
    assert_eq!(world.provider_calls().await, 0, "{route:?}");

    // The refusal is the disabled agent's, word for word.
    set_expiry(None).await;
    sqlx::query("UPDATE agent_identities SET enabled = false WHERE id = $1")
        .bind(AGENT)
        .execute(&pool)
        .await
        .expect("disable");
    let (disabled_status, disabled_body) = send(app(&pool), route.request(&token)).await;
    assert_eq!(
        (status, &body),
        (disabled_status, &disabled_body),
        "{route:?}"
    );
    sqlx::query("UPDATE agent_identities SET enabled = true WHERE id = $1")
        .bind(AGENT)
        .execute(&pool)
        .await
        .expect("enable");

    // Not yet expired: served.
    set_expiry(Some("1 hour")).await;
    let (status, body) = send(app(&pool), route.request(&token)).await;
    assert_eq!(status, StatusCode::OK, "{route:?}, expiry ahead: {body}");
    assert_eq!(world.provider_calls().await, 1, "{route:?}");
}

/// The group's quota is the agent's: over it, the call is refused (429) and
/// the row names the group and the agent.
async fn the_groups_quota_applies(pool: PgPool, route: Route) {
    let world = seed_served(&pool).await;
    common::set_llm_global_limits(&pool, RG, json!({"tokens": {"daily_limit": 5}})).await;
    common::insert_usage_log(
        &pool,
        T,
        RG,
        None,
        "llm",
        route.model(),
        50,
        0.0,
        common::db_now(),
    )
    .await;
    let (status, body) = send(app(&pool), route.request(&world.token())).await;
    assert_eq!(status, StatusCode::TOO_MANY_REQUESTS, "{route:?}: {body}");
    let (http, _, group, agent, _) = row(&pool, "proxy_subtype LIKE 'rate_limit%'").await;
    assert_eq!(http, Some(429));
    assert_eq!(group.as_deref(), Some(RG), "{route:?}");
    assert_eq!(agent.as_deref(), Some(AGENT), "{route:?}");
    assert_eq!(world.provider_calls().await, 0, "{route:?}");
}

/// The AI System's kill switch stops its agent.
async fn a_suspended_system_refuses_its_agent(pool: PgPool, route: Route) {
    let world = seed_served(&pool).await;
    sqlx::query("UPDATE resource_groups SET autonomy_level = 'suspended' WHERE id = $1")
        .bind(RG)
        .execute(&pool)
        .await
        .expect("suspend the system");
    let (status, body) = send(app(&pool), route.request(&world.token())).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{route:?}: {body}");
    assert!(
        !body.contains("does not grant access to model"),
        "{route:?}: refused at model access, not by the kill switch: {body}"
    );
    let (_, _, group, agent, _) = row(&pool, "http_status_code = 403").await;
    assert_eq!(group.as_deref(), Some(RG), "{route:?}");
    assert_eq!(agent.as_deref(), Some(AGENT), "{route:?}");
    assert_eq!(world.provider_calls().await, 0, "{route:?}");
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
            #[sqlx::test(migrations = "./tests/migrations")]
            async fn anthropic(pool: PgPool) { super::$case(pool, Route::Anthropic).await; }
            #[sqlx::test(migrations = "./tests/migrations")]
            async fn gemini(pool: PgPool) { super::$case(pool, Route::Gemini).await; }
        }
    )+};
}

per_route!(
    an_agent_of_a_group_that_holds_the_model_is_served,
    without_a_grant_the_agent_is_refused_by_the_actor_planes,
    a_model_the_agents_group_does_not_hold_is_refused,
    an_agent_in_no_group_is_refused,
    an_agent_that_names_another_group_is_refused,
    tokens_that_do_not_stand_are_refused,
    an_agent_identity_past_its_expiry_is_refused,
    the_groups_quota_applies,
    a_suspended_system_refuses_its_agent,
);

#[test]
fn every_route_is_covered() {
    assert_eq!(ROUTES.len(), 6);
}

// ---------------------------------------------------------------------------
// The acting group
// ---------------------------------------------------------------------------

/// An agent of several groups acts in ONE per request: the group it names
/// when it is a member of it, otherwise its oldest membership. The group it
/// acts in must itself hold the model; another of its groups holding it does
/// not serve the call (a key of the acting group would not be served
/// either).
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_agent_of_several_groups_acts_in_the_one_it_names_or_its_oldest(pool: PgPool) {
    let world = seed_served(&pool).await; // RG first
    join(&pool, AGENT, RG_B, T).await;
    let token = world.token();

    // No group named: the oldest membership.
    let (status, body) = send(app(&pool), chat(&token, MODEL, None)).await;
    assert_eq!(status, StatusCode::OK, "oldest membership: {body}");
    let (_, _, group, agent, _) = row(&pool, "http_status_code = 200").await;
    assert_eq!(group.as_deref(), Some(RG));
    assert_eq!(agent.as_deref(), Some(AGENT));
    // ... which does not hold MODEL_B.
    let (status, body) = send(app(&pool), chat(&token, MODEL_B, None)).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert!(body.contains("does not grant access to model"), "{body}");

    // RG_B named: the call runs there.
    let (status, body) = send(app(&pool), chat(&token, MODEL_B, Some(RG_B))).await;
    assert_eq!(status, StatusCode::OK, "the named group: {body}");
    let (_, _, group, agent, _) = row(
        &pool,
        "http_status_code = 200 AND llm_model_used = 'test-gpt-b'",
    )
    .await;
    assert_eq!(
        group.as_deref(),
        Some(RG_B),
        "the row names the acting group"
    );
    assert_eq!(agent.as_deref(), Some(AGENT));
    // ... and RG_B does not hold MODEL.
    let (status, body) = send(app(&pool), chat(&token, MODEL, Some(RG_B))).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert!(body.contains("does not grant access to model"), "{body}");

    assert_eq!(world.provider_calls().await, 2);
}

/// A membership of a group that is no longer active is no membership: the
/// agent is governed by no group, as a key of an inactive group is refused.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_membership_of_an_inactive_group_reaches_no_model(pool: PgPool) {
    let world = seed_served(&pool).await;
    sqlx::query("UPDATE resource_groups SET is_active = false WHERE id = $1")
        .bind(RG)
        .execute(&pool)
        .await
        .expect("deactivate");
    let (status, body) = send(app(&pool), chat(&world.token(), MODEL, None)).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    // Refused as an agent of no group, not as one acting in the inactive
    // group (which model access would refuse too).
    assert!(
        body.contains("an agent that is a member of no resource group"),
        "{body}"
    );
    let (status, body) = send(app(&pool), chat(&world.token(), MODEL, Some(RG))).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "named: {body}");
    assert!(body.contains("resource_group_not_member"), "{body}");
    assert_eq!(world.provider_calls().await, 0);
}

/// A membership row that names another tenant's group is no membership.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_membership_of_another_tenants_group_reaches_no_model(pool: PgPool) {
    let world = seed(&pool).await;
    grant(&pool, AGENT, "test-gpt").await;
    common::seed_resource_group(&pool, "rg-wma-foreign", OTHER).await;
    // The row carries this tenant's id and the other tenant's group.
    join(&pool, AGENT, "rg-wma-foreign", T).await;
    let (status, body) = send(app(&pool), chat(&world.token(), MODEL, None)).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert_eq!(world.provider_calls().await, 0);
}

// ---------------------------------------------------------------------------
// The group's guardrails
// ---------------------------------------------------------------------------

/// A guardrail bound to the agent's group blocks the agent's call, as it
/// blocks a call made with a key of that group.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_groups_guardrail_blocks_the_agents_call(pool: PgPool) {
    let world = seed_served(&pool).await;
    common::seed_llm_banned_words_guardrail(
        &pool,
        "gr-wma",
        T,
        &["forbidden-wma-word"],
        true,
        false,
    )
    .await;
    common::bind_guardrail_to_rg(&pool, "gr-wma", RG).await;

    // The control: a key of the group is blocked.
    let key = common::seed_api_key(&pool, T, RG, None, "wma-key").await;
    let blocked = |credential: String| {
        common::build_post_request(
            &Route::Chat.path(),
            Route::Chat.body(MODEL, "say forbidden-wma-word now"),
            Some(&credential),
            Some(T),
        )
    };
    let (status, body) = send(app(&pool), blocked(key)).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "the control, a key: {body}");
    assert!(body.contains("guardrail"), "{body}");

    let (status, body) = send(app(&pool), blocked(world.token())).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "the agent: {body}");
    assert!(body.contains("guardrail"), "{body}");
    assert_eq!(world.provider_calls().await, 0);
}
