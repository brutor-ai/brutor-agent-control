// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The platform's own embedding calls.
//!
//! The semantic cache and the Knowledge Base embed text through the gateway's
//! own `/v1/proxy/llm/embeddings`, with the caller's credential. Since that
//! route runs the actor planes, a default-deny agent with no grant for the
//! embedding model was refused on every lookup. The decision: such a call is
//! exempt from the agent's GRANT check and from nothing else, and it is
//! attributed and logged as the caller's.
//!
//! The tests ask the route as the platform's code does
//! (`internal_call::embedding_self_call` builds the same request) and as a
//! caller who tries to claim the exemption.

use axum::Router;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use ulid::Ulid;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::constants::headers::X_BRUTOR_INTERNAL_EMBEDDING;
use brutor_gateway_core::services::a2a_delegation::{CallChain, derive_tenant_key};
use brutor_gateway_core::services::internal_call::{InternalEmbedding, embedding_assertion};

use crate::common;

const T: &str = "tenant-ie";
const RG: &str = "rg-ie";
const EMBED: &str = "m-ie-embed";
const OTHER: &str = "m-ie-other";
const AGENT: &str = "agent-ie";
const KEY: &str = "ie-agent-key";

async fn provider() -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/embeddings"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "object": "list", "model": "text-embed",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
            "usage": {"prompt_tokens": 7, "total_tokens": 7}
        })))
        .mount(&mock)
        .await;
    mock
}

/// A tenant whose embedding model is `EMBED`, a second model bound to the
/// same group, and a default-deny agent with no grant whose key is returned.
async fn seed(pool: &PgPool) -> (MockServer, String) {
    let mock = provider().await;
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_llm_provider(pool, "prov-ie", T, "openai").await;
    for (id, name) in [(EMBED, "text-embed"), (OTHER, "text-embed-other")] {
        common::seed_llm_model_with_api_url(pool, id, T, "prov-ie", name, &mock.uri()).await;
        common::bind_llm_model_to_rg(pool, id, RG).await;
        sqlx::query("UPDATE llm_model SET cost_per_input_token = 2.0 WHERE id = $1")
            .bind(id)
            .execute(pool)
            .await
            .expect("price the model");
    }
    sqlx::query("UPDATE tenants SET default_embedding_model_id = $1 WHERE id = $2")
        .bind(EMBED)
        .bind(T)
        .execute(pool)
        .await
        .expect("the tenant's embedding model");
    let key = common::seed_api_key(pool, T, RG, None, KEY).await;
    sqlx::query(
        "INSERT INTO agent_identities (id, tenant_id, name, source, enabled, status, default_deny) \
         VALUES ($1, $2, $1, 'manual', true, 'active', true)",
    )
    .bind(AGENT)
    .bind(T)
    .execute(pool)
    .await
    .expect("agent identity");
    sqlx::query("UPDATE api_keys SET agent_id = $1 WHERE name = $2")
        .bind(AGENT)
        .bind(KEY)
        .execute(pool)
        .await
        .expect("bind key to agent");
    (mock, key)
}

struct Gateway {
    app: Router,
    tenant_key: Vec<u8>,
}

fn gateway(pool: &PgPool) -> Gateway {
    let state = common::build_test_state(pool.clone());
    let tenant_key = derive_tenant_key(state.config.encryption_key.as_bytes(), T);
    Gateway {
        app: common::build_llm_app(state),
        tenant_key,
    }
}

/// An embeddings request for `model` with `key` and extra headers.
fn embed(model: &str, key: &str, extra: &[(&str, String)]) -> Request<Body> {
    let mut req = common::build_post_request(
        "/v1/proxy/llm/embeddings",
        json!({"model_id": model, "input": "what is in the handbook"}),
        Some(key),
        Some(T),
    );
    for (name, value) in extra {
        req.headers_mut().insert(
            axum::http::HeaderName::from_bytes(name.as_bytes()).expect("name"),
            value.parse().expect("value"),
        );
    }
    req
}

fn asserted(purpose: InternalEmbedding) -> Vec<(&'static str, String)> {
    vec![(X_BRUTOR_INTERNAL_EMBEDDING, embedding_assertion(purpose))]
}

async fn send(app: &Router, req: Request<Body>) -> (StatusCode, String) {
    let resp = app.clone().oneshot(req).await.expect("router");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.expect("body").to_bytes();
    (status, String::from_utf8_lossy(&bytes).into_owned())
}

async fn provider_calls(mock: &MockServer) -> usize {
    mock.received_requests().await.unwrap_or_default().len()
}

/// The newest `proxy_logs` row of the tenant with `status`.
async fn row(pool: &PgPool, status: i32) -> Value {
    for _ in 0..120 {
        let found = sqlx::query_as::<_, (sqlx::types::Json<Value>,)>(
            "SELECT to_jsonb(p.*) FROM proxy_logs p WHERE tenant_id = $1 \
                AND http_status_code = $2 ORDER BY timestamp DESC LIMIT 1",
        )
        .bind(T)
        .bind(status)
        .fetch_optional(pool)
        .await
        .expect("proxy_logs");
        if let Some((j,)) = found {
            return j.0;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    panic!("no {status} row of {T}");
}

async fn key_id(pool: &PgPool) -> String {
    sqlx::query_scalar("SELECT id FROM api_keys WHERE name = $1")
        .bind(KEY)
        .fetch_one(pool)
        .await
        .expect("key id")
}

// ---------------------------------------------------------------------------
// The exemption, and what the call is recorded as
// ---------------------------------------------------------------------------

async fn the_platforms_call_is_served_without_a_grant_and_is_the_callers(
    pool: PgPool,
    purpose: InternalEmbedding,
    feature: &str,
    cache_op: Option<&str>,
) {
    let (mock, key) = seed(&pool).await;
    let gw = gateway(&pool);

    // The agent's own call of the embedding model: no grant, refused.
    let (status, body) = send(&gw.app, embed(EMBED, &key, &[])).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert!(body.contains("agent_not_authorized"), "{body}");
    assert_eq!(provider_calls(&mock).await, 0);

    // The platform's call for that agent, as a step of the agent's run.
    let run = Ulid::new();
    let step = Ulid::new();
    let chain = CallChain::root(run).fork_child(step).expect("child");
    let mut headers = asserted(purpose);
    for (name, value) in chain.to_header_pairs(&gw.tenant_key) {
        headers.push((name, value));
    }
    let (status, body) = send(&gw.app, embed(EMBED, &key, &headers)).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(provider_calls(&mock).await, 1);

    // Its audit row (`proxy_logs`): the caller's, with tokens and cost, and
    // marked as the platform's call for the feature.
    let log = row(&pool, 200).await;
    assert_eq!(log["proxy_type"], "llm");
    assert_eq!(log["proxy_subtype"], "embeddings");
    assert_eq!(log["tenant_id"], T);
    assert_eq!(log["resource_group_id"], RG);
    assert_eq!(log["api_key_id"], json!(key_id(&pool).await));
    assert_eq!(log["actor_agent_id"], AGENT);
    assert_eq!(
        log["gateway_user_id"], RG,
        "a group key's identity is its group"
    );
    assert_eq!(
        log["root_task_id"],
        json!(run.to_string()),
        "the caller's run"
    );
    assert_eq!(log["parent_task_id"], json!(step.to_string()));
    assert_eq!(log["delegation_depth"], 1);
    assert_eq!(log["llm_model_used"], "text-embed");
    assert_eq!(log["llm_prompt_tokens"], 7);
    assert_eq!(log["llm_total_tokens"], 7);
    let cost = log["llm_estimated_cost_usd"].as_f64().expect("cost");
    assert!(cost > 0.0, "the call is priced: {cost}");
    assert_eq!(log["tags"], json!({"internal_embedding": feature}));
    assert_eq!(log["llm_cache_layer"], json!(cache_op));

    // Its usage row (`usage_logs`): billed to the caller's tenant and group.
    let mut usage = None;
    for _ in 0..120 {
        usage = sqlx::query_as::<_, (Option<String>, Option<i32>, Option<f64>, Option<String>)>(
            "SELECT group_id, prompt_tokens, estimated_cost, model_name FROM usage_logs \
              WHERE tenant_id = $1 AND resource_type = 'llm' ORDER BY id DESC LIMIT 1",
        )
        .bind(T)
        .fetch_optional(&pool)
        .await
        .expect("usage_logs");
        if usage.is_some() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    let (group, tokens, usage_cost, model) = usage.expect("a usage row");
    assert_eq!(group.as_deref(), Some(RG));
    assert_eq!(tokens, Some(7));
    assert_eq!(model.as_deref(), Some("text-embed"));
    assert!(usage_cost.unwrap_or(0.0) > 0.0, "{usage_cost:?}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_semantic_caches_lookup_is_served_without_a_grant_and_is_the_callers(pool: PgPool) {
    the_platforms_call_is_served_without_a_grant_and_is_the_callers(
        pool,
        InternalEmbedding::SemanticCacheLookup,
        "semantic_cache",
        Some("lookup"),
    )
    .await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_semantic_caches_store_is_served_without_a_grant_and_is_the_callers(pool: PgPool) {
    the_platforms_call_is_served_without_a_grant_and_is_the_callers(
        pool,
        InternalEmbedding::SemanticCacheStore,
        "semantic_cache",
        Some("store"),
    )
    .await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_knowledge_bases_call_is_served_without_a_grant_and_is_the_callers(pool: PgPool) {
    the_platforms_call_is_served_without_a_grant_and_is_the_callers(
        pool,
        InternalEmbedding::KnowledgeBase,
        "knowledge_base",
        None,
    )
    .await;
}

// ---------------------------------------------------------------------------
// Only the platform's code can assert it
// ---------------------------------------------------------------------------

/// A caller who writes the header, in every form a caller can write it, is
/// refused as before, and the refusal is not marked as the platform's call.
/// The old marker (`X-Brutor-Cache-Op`) is no longer read at all.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_caller_cannot_claim_the_exemption(pool: PgPool) {
    let (mock, key) = seed(&pool).await;
    let gw = gateway(&pool);
    let real = embedding_assertion(InternalEmbedding::KnowledgeBase);
    let forged = [
        "1".to_string(),
        "true".to_string(),
        "knowledge_base".to_string(),
        "semantic_cache.lookup".to_string(),
        "knowledge_base:".to_string(),
        "knowledge_base:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA".to_string(),
        // The right purpose with a token one character short, and one long.
        real[..real.len() - 1].to_string(),
        format!("{real}x"),
    ];
    for value in forged {
        let headers = [
            (X_BRUTOR_INTERNAL_EMBEDDING, value.clone()),
            ("x-brutor-cache-op", "lookup".to_string()),
            ("x-brutor-semantic-policy", "1".to_string()),
        ];
        let (status, body) = send(&gw.app, embed(EMBED, &key, &headers)).await;
        assert_eq!(status, StatusCode::FORBIDDEN, "{value:?}: {body}");
        assert!(body.contains("agent_not_authorized"), "{value:?}: {body}");
    }
    assert_eq!(provider_calls(&mock).await, 0);
    let log = row(&pool, 403).await;
    assert_eq!(log["proxy_subtype"], "agent_authz_block");
    assert_eq!(log["actor_agent_id"], AGENT);
    assert!(log["tags"].is_null(), "{}", log["tags"]);
    assert!(log["llm_cache_layer"].is_null());
}

/// A caller's embedding that is served (an agent that may call) is not
/// marked as the platform's because it carries the old marker.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_callers_own_embedding_is_not_recorded_as_the_platforms(pool: PgPool) {
    let (_mock, key) = seed(&pool).await;
    sqlx::query("UPDATE agent_identities SET default_deny = false WHERE id = $1")
        .bind(AGENT)
        .execute(&pool)
        .await
        .expect("allow the agent");
    let gw = gateway(&pool);
    let headers = [
        (
            X_BRUTOR_INTERNAL_EMBEDDING,
            "semantic_cache.lookup".to_string(),
        ),
        ("x-brutor-cache-op", "lookup".to_string()),
    ];
    let (status, body) = send(&gw.app, embed(EMBED, &key, &headers)).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let log = row(&pool, 200).await;
    assert!(log["tags"].is_null(), "{}", log["tags"]);
    assert!(
        log["llm_cache_layer"].is_null(),
        "{}",
        log["llm_cache_layer"]
    );
}

// ---------------------------------------------------------------------------
// Everything else still applies to the platform's call
// ---------------------------------------------------------------------------

/// The exemption is for the tenant's configured embedding model. The
/// platform's assertion on a call of any other model changes nothing.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_exemption_is_for_the_tenants_embedding_model_only(pool: PgPool) {
    let (mock, key) = seed(&pool).await;
    let gw = gateway(&pool);
    let (status, body) = send(
        &gw.app,
        embed(OTHER, &key, &asserted(InternalEmbedding::KnowledgeBase)),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert!(body.contains("agent_not_authorized"), "{body}");
    assert_eq!(provider_calls(&mock).await, 0);
}

/// An explicit `deny` grant is the operator's statement about this agent and
/// this model; "no grant is required" does not override it.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_explicit_deny_grant_still_refuses_the_platforms_call(pool: PgPool) {
    let (mock, key) = seed(&pool).await;
    sqlx::query(
        "INSERT INTO agent_grants (id, tenant_id, agent_id, action_type, target, effect, enabled) \
         VALUES ('g-ie-deny', $1, $2, 'llm_call', 'text-embed', 'deny', true)",
    )
    .bind(T)
    .bind(AGENT)
    .execute(&pool)
    .await
    .expect("deny grant");
    let gw = gateway(&pool);
    let (status, body) = send(
        &gw.app,
        embed(
            EMBED,
            &key,
            &asserted(InternalEmbedding::SemanticCacheLookup),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert_eq!(provider_calls(&mock).await, 0);
    assert_eq!(row(&pool, 403).await["proxy_subtype"], "agent_authz_block");
}

/// A revoked agent is no principal, with or without the assertion.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_revoked_agents_platform_call_is_refused(pool: PgPool) {
    let (mock, key) = seed(&pool).await;
    sqlx::query("UPDATE agent_identities SET revoked_at = now() WHERE id = $1")
        .bind(AGENT)
        .execute(&pool)
        .await
        .expect("revoke the agent");
    let gw = gateway(&pool);
    let (status, body) = send(
        &gw.app,
        embed(EMBED, &key, &asserted(InternalEmbedding::KnowledgeBase)),
    )
    .await;
    // Its key is no credential: refused at
    // authentication, as a revoked key is.
    assert_eq!(status, StatusCode::UNAUTHORIZED, "{body}");
    assert_eq!(provider_calls(&mock).await, 0);
}

/// The kill switch: a suspended agent's platform call is refused.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_suspended_agents_platform_call_is_refused(pool: PgPool) {
    let (mock, key) = seed(&pool).await;
    sqlx::query("UPDATE agent_identities SET autonomy_level = 'suspended' WHERE id = $1")
        .bind(AGENT)
        .execute(&pool)
        .await
        .expect("suspend the agent");
    let gw = gateway(&pool);
    let (status, body) = send(
        &gw.app,
        embed(EMBED, &key, &asserted(InternalEmbedding::KnowledgeBase)),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert_eq!(provider_calls(&mock).await, 0);
}

/// The kill switch for a caller that is no agent: a key of a suspended AI
/// System is refused with the assertion as without it.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_suspended_systems_platform_call_is_refused(pool: PgPool) {
    let (mock, _agent_key) = seed(&pool).await;
    let key = common::seed_api_key(&pool, T, RG, None, "ie-plain-key").await;
    sqlx::query("UPDATE resource_groups SET autonomy_level = 'suspended' WHERE id = $1")
        .bind(RG)
        .execute(&pool)
        .await
        .expect("suspend the system");
    let gw = gateway(&pool);
    let (status, body) = send(
        &gw.app,
        embed(
            EMBED,
            &key,
            &asserted(InternalEmbedding::SemanticCacheLookup),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert!(body.contains("autonomy_denied"), "{body}");
    assert_eq!(provider_calls(&mock).await, 0);
    assert_eq!(row(&pool, 403).await["proxy_subtype"], "autonomy_block");
}

/// Model access through the caller's group: an embedding model that is not
/// bound to the group is not reachable through the platform's call either.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_platforms_call_needs_the_model_in_the_callers_group(pool: PgPool) {
    let (mock, key) = seed(&pool).await;
    sqlx::query("DELETE FROM resource_group_llm_models WHERE llm_model_id = $1")
        .bind(EMBED)
        .execute(&pool)
        .await
        .expect("unbind the model");
    let gw = gateway(&pool);
    let (status, body) = send(
        &gw.app,
        embed(EMBED, &key, &asserted(InternalEmbedding::KnowledgeBase)),
    )
    .await;
    assert!(
        status == StatusCode::FORBIDDEN || status == StatusCode::NOT_FOUND,
        "{status}: {body}"
    );
    assert_eq!(provider_calls(&mock).await, 0);
}

/// The tenant: the platform's assertion does not move a key to another
/// tenant. A key of this tenant naming another tenant's embedding model is
/// refused as any call is.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_platforms_call_stays_in_the_callers_tenant(pool: PgPool) {
    let (mock, key) = seed(&pool).await;
    common::seed_tenant(&pool, "tenant-ie-other").await;
    let gw = gateway(&pool);
    let mut req = embed(EMBED, &key, &asserted(InternalEmbedding::KnowledgeBase));
    req.headers_mut()
        .insert("x-tenant-id", "tenant-ie-other".parse().expect("tenant"));
    let (status, body) = send(&gw.app, req).await;
    assert!(
        matches!(status.as_u16(), 401 | 403 | 404),
        "{status}: {body}"
    );
    assert_eq!(provider_calls(&mock).await, 0);
}

/// A request quota of the group counts the platform's call and stops it.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_platforms_call_is_held_to_the_groups_limits(pool: PgPool) {
    let (mock, key) = seed(&pool).await;
    common::set_llm_global_limits(&pool, RG, json!({"requests": {"daily_limit": 1}})).await;
    let gw = gateway(&pool);
    let headers = asserted(InternalEmbedding::SemanticCacheLookup);
    let (first, body) = send(&gw.app, embed(EMBED, &key, &headers)).await;
    assert_eq!(first, StatusCode::OK, "{body}");
    let (second, body) = send(&gw.app, embed(EMBED, &key, &headers)).await;
    assert_eq!(second, StatusCode::TOO_MANY_REQUESTS, "{body}");
    assert_eq!(provider_calls(&mock).await, 1);
}
