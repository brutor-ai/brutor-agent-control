// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

// End-to-end: an agent-principal-keyed LLM request is blocked in the request
// path by the per-agent runtime authz engine (default-deny, no grant), returns
// 403, and writes an `agent_authz_block` proxy_log stamping the actor +
// decision. No upstream is mocked — the block happens before any provider call.

use axum::body::Body;
use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;

use crate::common;

async fn body_to_json(body: Body) -> Value {
    let bytes = body.collect().await.unwrap().to_bytes();
    serde_json::from_slice(&bytes).unwrap_or(Value::Null)
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn agent_keyed_request_denied_by_default_deny(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-1").await;
    common::seed_resource_group(&pool, "rg-llm", "tenant-1").await;
    common::seed_end_user(&pool, 7001, "tenant-1").await;
    common::add_direct_membership(&pool, 7001, "rg-llm", "tenant-1").await;
    common::seed_llm_provider(&pool, "prov-openai", "tenant-1", "openai").await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-test",
        "tenant-1",
        "prov-openai",
        "test-gpt",
        "http://127.0.0.1:1",
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "m-test", "rg-llm").await;
    let api_key = common::seed_api_key(&pool, "tenant-1", "rg-llm", Some(7001), "agent-key").await;

    // An active, default-deny agent with NO grants → any action is denied.
    sqlx::query(
        "INSERT INTO agent_identities (id, tenant_id, name, source, enabled, status, default_deny) \
         VALUES ('agent-x', 'tenant-1', 'agent-x', 'manual', true, 'active', true)",
    )
    .execute(&pool)
    .await
    .unwrap();
    // Bind the key to the agent — now requests with it are actor=agent-x.
    sqlx::query("UPDATE api_keys SET agent_id = 'agent-x' WHERE tenant_id = 'tenant-1' AND name = 'agent-key'")
        .execute(&pool)
        .await
        .unwrap();

    let state = common::build_test_state(pool.clone());
    let app = common::build_llm_app(state);
    let req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({"model_id": "m-test", "messages": [{"role": "user", "content": "hi"}]}),
        Some(&api_key),
        Some("tenant-1"),
    );

    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let body = body_to_json(response.into_body()).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "agent default-deny must 403; body={body}"
    );

    // The block must be recorded with the actor + decision for evidence.
    // The proxy_log is written by the background writer; poll briefly.
    let mut found: Option<(Option<String>, Option<String>, Option<String>)> = None;
    for _ in 0..40 {
        let row: Option<(Option<String>, Option<String>, Option<String>)> = sqlx::query_as(
            "SELECT actor_agent_id, agent_authz_decision, proxy_subtype \
             FROM proxy_logs WHERE tenant_id = 'tenant-1' AND actor_agent_id = 'agent-x' \
             ORDER BY timestamp DESC LIMIT 1",
        )
        .fetch_optional(&pool)
        .await
        .unwrap();
        if row.is_some() {
            found = row;
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    let (actor, decision, subtype) = found.expect("an agent_authz_block proxy_log row");
    assert_eq!(actor.as_deref(), Some("agent-x"));
    assert_eq!(decision.as_deref(), Some("deny"));
    assert_eq!(subtype.as_deref(), Some("agent_authz_block"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn agent_keyed_request_allowed_with_grant(pool: PgPool) {
    // With an explicit allow grant for the model, the agent passes authz and
    // the request proceeds to the upstream (which we mock to a 200).
    use wiremock::matchers::{method, path as wm_path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "cmpl-1", "object": "chat.completion", "created": 0, "model": "test-gpt",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        })))
        .mount(&mock)
        .await;

    common::seed_tenant(&pool, "tenant-1").await;
    common::seed_resource_group(&pool, "rg-llm", "tenant-1").await;
    common::seed_end_user(&pool, 7001, "tenant-1").await;
    common::add_direct_membership(&pool, 7001, "rg-llm", "tenant-1").await;
    common::seed_llm_provider(&pool, "prov-openai", "tenant-1", "openai").await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-test",
        "tenant-1",
        "prov-openai",
        "test-gpt",
        &mock.uri(),
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "m-test", "rg-llm").await;
    let api_key = common::seed_api_key(&pool, "tenant-1", "rg-llm", Some(7001), "agent-key").await;

    sqlx::query(
        "INSERT INTO agent_identities (id, tenant_id, name, source, enabled, status, default_deny) \
         VALUES ('agent-y', 'tenant-1', 'agent-y', 'manual', true, 'active', true)",
    )
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO agent_grants (id, tenant_id, agent_id, action_type, target, effect, enabled) \
         VALUES ('g-allow', 'tenant-1', 'agent-y', 'llm_call', 'test-gpt', 'allow', true)",
    )
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query("UPDATE api_keys SET agent_id = 'agent-y' WHERE tenant_id = 'tenant-1' AND name = 'agent-key'")
        .execute(&pool)
        .await
        .unwrap();

    let state = common::build_test_state(pool.clone());
    let app = common::build_llm_app(state);
    let req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({"model_id": "m-test", "messages": [{"role": "user", "content": "hi"}]}),
        Some(&api_key),
        Some("tenant-1"),
    );
    let response = app.oneshot(req).await.expect("router call");
    assert_eq!(
        response.status(),
        StatusCode::OK,
        "allow grant must pass authz and round-trip"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn dry_run_agent_proceeds_and_logs_shadow(pool: PgPool) {
    // A default-deny agent in `dry_run` mode would be blocked under enforce, but
    // dry_run records a would-block (shadow) and lets the call reach upstream.
    use wiremock::matchers::{method, path as wm_path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "cmpl-1", "object": "chat.completion", "created": 0, "model": "test-gpt",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        })))
        .mount(&mock)
        .await;

    common::seed_tenant(&pool, "tenant-1").await;
    common::seed_resource_group(&pool, "rg-llm", "tenant-1").await;
    common::seed_end_user(&pool, 7001, "tenant-1").await;
    common::add_direct_membership(&pool, 7001, "rg-llm", "tenant-1").await;
    common::seed_llm_provider(&pool, "prov-openai", "tenant-1", "openai").await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-test",
        "tenant-1",
        "prov-openai",
        "test-gpt",
        &mock.uri(),
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "m-test", "rg-llm").await;
    let api_key = common::seed_api_key(&pool, "tenant-1", "rg-llm", Some(7001), "agent-key").await;

    // default-deny + NO grant + enforcement_mode='dry_run'.
    sqlx::query(
        "INSERT INTO agent_identities (id, tenant_id, name, source, enabled, status, default_deny, enforcement_mode) \
         VALUES ('agent-dr', 'tenant-1', 'agent-dr', 'manual', true, 'active', true, 'dry_run')",
    )
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query("UPDATE api_keys SET agent_id = 'agent-dr' WHERE tenant_id = 'tenant-1' AND name = 'agent-key'")
        .execute(&pool)
        .await
        .unwrap();

    let state = common::build_test_state(pool.clone());
    let app = common::build_llm_app(state);
    let req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({"model_id": "m-test", "messages": [{"role": "user", "content": "hi"}]}),
        Some(&api_key),
        Some("tenant-1"),
    );
    let response = app.oneshot(req).await.expect("router call");
    assert_eq!(
        response.status(),
        StatusCode::OK,
        "dry_run must NOT block — proceeds to upstream"
    );

    // A shadow would-block row must be recorded (decision=deny, not a hard block).
    let mut found: Option<(Option<String>, Option<String>)> = None;
    for _ in 0..40 {
        let row: Option<(Option<String>, Option<String>)> = sqlx::query_as(
            "SELECT agent_authz_decision, proxy_subtype FROM proxy_logs \
             WHERE tenant_id = 'tenant-1' AND actor_agent_id = 'agent-dr' \
               AND proxy_subtype = 'agent_authz_shadow' ORDER BY timestamp DESC LIMIT 1",
        )
        .fetch_optional(&pool)
        .await
        .unwrap();
        if row.is_some() {
            found = row;
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    let (decision, subtype) = found.expect("a shadow would-block proxy_log row");
    assert_eq!(decision.as_deref(), Some("deny"));
    assert_eq!(subtype.as_deref(), Some("agent_authz_shadow"));
}

/// An API key bound to an agent identity is the
/// agent's credential; the operator ends the agent by disabling it, revoking
/// it or letting `expires_at` pass. The gate read `enabled`, `revoked_at` and
/// `status` and not `expires_at`, so the key of an agent whose identity had
/// expired was served as if it had not (the workload-token path refuses it
/// too). It is refused as a disabled agent's key is, before any
/// upstream call, whatever the enforcement mode. That refusal is
/// made at authentication (401, the key is no credential), not by
/// the gate (403); `agent_authz_db` holds the gate's own answer.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_key_of_an_agent_identity_past_its_expiry_is_refused_as_a_disabled_agents_is(
    pool: PgPool,
) {
    use wiremock::matchers::{method, path as wm_path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "cmpl-1", "object": "chat.completion", "created": 0, "model": "test-gpt",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        })))
        .mount(&mock)
        .await;

    common::seed_tenant(&pool, "tenant-1").await;
    common::seed_resource_group(&pool, "rg-llm", "tenant-1").await;
    common::seed_end_user(&pool, 7001, "tenant-1").await;
    common::add_direct_membership(&pool, 7001, "rg-llm", "tenant-1").await;
    common::seed_llm_provider(&pool, "prov-openai", "tenant-1", "openai").await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-test",
        "tenant-1",
        "prov-openai",
        "test-gpt",
        &mock.uri(),
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "m-test", "rg-llm").await;

    // Three agents that differ in one thing each. None is default-deny, so
    // nothing but the identity's own state can refuse them; the dry-run
    // mode on the expired one shows the refusal is not softened by it.
    let mut keys = Vec::new();
    for (agent, enabled, expires, mode) in [
        ("agent-live", true, Some("1 hour"), "enforce"),
        ("agent-off", false, None, "enforce"),
        ("agent-expired", true, Some("-1 hour"), "enforce"),
        ("agent-expired-dry", true, Some("-1 second"), "dry_run"),
    ] {
        sqlx::query(
            "INSERT INTO agent_identities \
             (id, tenant_id, name, source, enabled, status, default_deny, enforcement_mode, \
              expires_at) \
             VALUES ($1, 'tenant-1', $1, 'manual', $2, 'active', false, $3, \
                     now() + $4::interval)",
        )
        .bind(agent)
        .bind(enabled)
        .bind(mode)
        .bind(expires)
        .execute(&pool)
        .await
        .unwrap();
        let name = format!("key-{agent}");
        let key = common::seed_api_key(&pool, "tenant-1", "rg-llm", Some(7001), &name).await;
        sqlx::query("UPDATE api_keys SET agent_id = $1 WHERE name = $2")
            .bind(agent)
            .bind(&name)
            .execute(&pool)
            .await
            .unwrap();
        keys.push((agent, key));
    }

    let state = common::build_test_state(pool.clone());
    let mut answers = Vec::new();
    for (agent, key) in &keys {
        let req = common::build_post_request(
            "/v1/proxy/llm/chat/completions",
            json!({"model_id": "m-test", "messages": [{"role": "user", "content": "hi"}]}),
            Some(key),
            Some("tenant-1"),
        );
        let response = common::build_llm_app(state.clone())
            .oneshot(req)
            .await
            .expect("router call");
        let status = response.status();
        let body = body_to_json(response.into_body()).await;
        answers.push((*agent, status, body));
    }
    let answer = |agent: &str| {
        answers
            .iter()
            .find(|(a, _, _)| *a == agent)
            .map(|(_, s, b)| (*s, b.clone()))
            .expect("asked")
    };

    assert_eq!(
        answer("agent-live").0,
        StatusCode::OK,
        "an identity that expires in an hour is served: {}",
        answer("agent-live").1
    );
    let (off_status, off_body) = answer("agent-off");
    assert_eq!(off_status, StatusCode::UNAUTHORIZED, "disabled: {off_body}");
    for expired in ["agent-expired", "agent-expired-dry"] {
        let (status, body) = answer(expired);
        assert_eq!(
            (status, &body),
            (off_status, &off_body),
            "{expired}: answered as the disabled agent's key is"
        );
    }
    assert_eq!(
        mock.received_requests().await.unwrap_or_default().len(),
        1,
        "only the live agent's request reached the provider"
    );
}

/// The agent's gate runs only where a route has a
/// target to authorize (a model, a tool, a skill). A route with none (the
/// model listing here; files, job status, token counting, trace ingest)
/// served the key of an agent that was disabled, expired or revoked. Such a
/// key is no credential: it is refused at authentication, on every route,
/// as a revoked key is (401), in the statement that reads the key, so no
/// request pays a second round trip for it. Read on every request: ending
/// the agent ends its key on the next call.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_key_of_an_agent_that_does_not_stand_is_refused_where_no_route_reaches_the_gate(
    pool: PgPool,
) {
    common::seed_tenant(&pool, "tenant-1").await;
    common::seed_resource_group(&pool, "rg-llm", "tenant-1").await;

    let mut keys = Vec::new();
    for (agent, enabled, expires, revoked, status) in [
        ("agent-live", true, Some("1 hour"), false, "active"),
        ("agent-off", false, None, false, "active"),
        ("agent-expired", true, Some("-1 second"), false, "active"),
        ("agent-revoked", true, None, true, "active"),
        ("agent-suspended", true, None, false, "suspended"),
    ] {
        sqlx::query(
            "INSERT INTO agent_identities \
             (id, tenant_id, name, source, enabled, status, default_deny, expires_at, \
              revoked_at) \
             VALUES ($1, 'tenant-1', $1, 'manual', $2, $5, false, now() + $3::interval, \
                     CASE WHEN $4 THEN now() END)",
        )
        .bind(agent)
        .bind(enabled)
        .bind(expires)
        .bind(revoked)
        .bind(status)
        .execute(&pool)
        .await
        .unwrap();
        let name = format!("key-{agent}");
        let key = common::seed_api_key(&pool, "tenant-1", "rg-llm", None, &name).await;
        sqlx::query("UPDATE api_keys SET agent_id = $1 WHERE name = $2")
            .bind(agent)
            .bind(&name)
            .execute(&pool)
            .await
            .unwrap();
        keys.push((agent, key));
    }
    let revoked_key = common::seed_api_key(&pool, "tenant-1", "rg-llm", None, "key-revoked").await;
    sqlx::query("UPDATE api_keys SET revoked = true WHERE name = 'key-revoked'")
        .execute(&pool)
        .await
        .unwrap();

    let state = common::build_test_state(pool.clone());
    let ask = |key: String| {
        let state = state.clone();
        async move {
            let req =
                common::build_get_request("/v1/proxy/llm/models", Some(&key), Some("tenant-1"));
            let response = common::build_llm_app(state)
                .oneshot(req)
                .await
                .expect("router call");
            let status = response.status();
            (status, body_to_json(response.into_body()).await)
        }
    };

    let revoked_answer = ask(revoked_key).await;
    assert_eq!(revoked_answer.0, StatusCode::UNAUTHORIZED);
    for (agent, key) in &keys {
        let answer = ask(key.clone()).await;
        if *agent == "agent-live" {
            assert_eq!(answer.0, StatusCode::OK, "{agent}: {}", answer.1);
        } else {
            assert_eq!(
                answer, revoked_answer,
                "{agent}: its key answers as a revoked key does"
            );
        }
    }

    // No cache holds the decision: the operator disables the live agent and
    // its key stops on the next call; enabling it brings the key back.
    let live = keys[0].1.clone();
    for (enabled, expected) in [(false, StatusCode::UNAUTHORIZED), (true, StatusCode::OK)] {
        sqlx::query("UPDATE agent_identities SET enabled = $1 WHERE id = 'agent-live'")
            .bind(enabled)
            .execute(&pool)
            .await
            .unwrap();
        assert_eq!(ask(live.clone()).await.0, expected, "enabled = {enabled}");
    }
}

/// At the route: a shared (`global`-scope) key bound
/// to an agent identity, acting for a tenant the operator listed
/// (the installation's list of the tenants a shared key may act for), is authenticated as that agent:
/// the identity stands in the key's own tenant. In the tenant acted for the
/// agent's gate finds no identity row (`agent_authz_db::
/// an_agent_with_no_identity_in_the_acting_tenant_is_denied`). On this
/// route the call is refused in both tenants and no provider is called:
/// model access through the key's group refuses the other tenant's model
/// before the gate is asked.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_agent_key_acting_for_another_tenant_is_not_ungated_there(pool: PgPool) {
    use wiremock::matchers::{method, path as wm_path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    assert!(
        brutor_gateway_core::services::auth::configure_global_key_allowed_targets(Some(vec![
            "tenant-f481-b".to_string()
        ]))
    );
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "cmpl-1", "object": "chat.completion", "created": 0, "model": "test-gpt",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        })))
        .mount(&mock)
        .await;

    for t in ["tenant-f481-a", "tenant-f481-b"] {
        common::seed_tenant(&pool, t).await;
    }
    common::seed_resource_group(&pool, "rg-f481-a", "tenant-f481-a").await;
    for (t, prov, model) in [
        ("tenant-f481-a", "prov-f481-a", "m-f481-a"),
        ("tenant-f481-b", "prov-f481-b", "m-f481-b"),
    ] {
        common::seed_llm_provider(&pool, prov, t, "openai").await;
        common::seed_llm_model_with_api_url(&pool, model, t, prov, "test-gpt", &mock.uri()).await;
        // The key's group holds both models: the only thing left to refuse
        // the call is the agent's own gate.
        common::bind_llm_model_to_rg(&pool, model, "rg-f481-a").await;
    }
    sqlx::query(
        "INSERT INTO agent_identities (id, tenant_id, name, source, enabled, status, default_deny) \
         VALUES ('agent-f481-733190462', 'tenant-f481-a', 'agent-f481', 'manual', true, 'active', true)",
    )
    .execute(&pool)
    .await
    .unwrap();
    let key = common::seed_api_key(&pool, "tenant-f481-a", "rg-f481-a", None, "key-f481").await;
    sqlx::query(
        "UPDATE api_keys SET agent_id = 'agent-f481-733190462', scope = 'global' \
          WHERE name = 'key-f481'",
    )
    .execute(&pool)
    .await
    .unwrap();

    let state = common::build_test_state(pool.clone());
    let mut answers = Vec::new();
    for (tenant, model) in [("tenant-f481-a", "m-f481-a"), ("tenant-f481-b", "m-f481-b")] {
        let req = common::build_post_request(
            "/v1/proxy/llm/chat/completions",
            json!({"model_id": model, "messages": [{"role": "user", "content": "hi"}]}),
            Some(&key),
            Some(tenant),
        );
        let response = common::build_llm_app(state.clone())
            .oneshot(req)
            .await
            .expect("router call");
        let status = response.status();
        answers.push((tenant, status, body_to_json(response.into_body()).await));
    }
    assert_eq!(
        answers[0].1,
        StatusCode::FORBIDDEN,
        "in its own tenant the default-deny agent is refused: {}",
        answers[0].2
    );
    assert_eq!(
        answers[1].1,
        StatusCode::FORBIDDEN,
        "acting for the listed tenant it is refused too: {}",
        answers[1].2
    );
    assert_eq!(
        mock.received_requests().await.unwrap_or_default().len(),
        0,
        "no provider call"
    );
}

/// `api_keys.agent_id` is `ON DELETE CASCADE`:
/// deleting an agent identity row deletes its key. The agent was
/// default-deny with no grant (every call refused 403); after the row is
/// deleted the key is gone and the same call answers 401, the answer an
/// unknown key gets, and nothing is sent to the provider. Under `SET NULL`
/// the key stayed enabled with no agent and was served as a plain key of
/// its resource group.
#[sqlx::test(migrations = "./tests/migrations")]
async fn deleting_an_agent_identity_deletes_its_key_and_the_call_answers_401(pool: PgPool) {
    use wiremock::matchers::{method, path as wm_path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "cmpl-1", "object": "chat.completion", "created": 0, "model": "test-gpt",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        })))
        .mount(&mock)
        .await;
    common::seed_tenant(&pool, "tenant-f480").await;
    common::seed_resource_group(&pool, "rg-f480", "tenant-f480").await;
    common::seed_llm_provider(&pool, "prov-f480", "tenant-f480", "openai").await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-f480",
        "tenant-f480",
        "prov-f480",
        "test-gpt",
        &mock.uri(),
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "m-f480", "rg-f480").await;
    sqlx::query(
        "INSERT INTO agent_identities (id, tenant_id, name, source, enabled, status, default_deny) \
         VALUES ('agent-f480-733190463', 'tenant-f480', 'agent-f480', 'manual', true, 'active', true)",
    )
    .execute(&pool)
    .await
    .unwrap();
    let key = common::seed_api_key(&pool, "tenant-f480", "rg-f480", None, "key-f480").await;
    sqlx::query("UPDATE api_keys SET agent_id = 'agent-f480-733190463' WHERE name = 'key-f480'")
        .execute(&pool)
        .await
        .unwrap();

    let state = common::build_test_state(pool.clone());
    let ask = || {
        let state = state.clone();
        let key = key.clone();
        async move {
            let req = common::build_post_request(
                "/v1/proxy/llm/chat/completions",
                json!({"model_id": "m-f480", "messages": [{"role": "user", "content": "hi"}]}),
                Some(&key),
                Some("tenant-f480"),
            );
            let response = common::build_llm_app(state)
                .oneshot(req)
                .await
                .expect("router call");
            let status = response.status();
            (status, body_to_json(response.into_body()).await)
        }
    };
    let (before, body) = ask().await;
    assert_eq!(before, StatusCode::FORBIDDEN, "default-deny agent: {body}");

    // The agent identity row is deleted (the only statement a deletion
    // needs; the constraint does the rest).
    sqlx::query("DELETE FROM agent_identities WHERE id = 'agent-f480-733190463'")
        .execute(&pool)
        .await
        .expect("delete the agent identity");
    let keys: i64 = sqlx::query_scalar("SELECT count(*) FROM api_keys WHERE name = 'key-f480'")
        .fetch_one(&pool)
        .await
        .expect("count the key");
    assert_eq!(keys, 0, "the constraint deletes the agent's key with it");

    // A fresh state: the answer is the database's, not a cached key's.
    let state = common::build_test_state(pool.clone());
    let answer = |key: String| {
        let state = state.clone();
        async move {
            let req = common::build_post_request(
                "/v1/proxy/llm/chat/completions",
                json!({"model_id": "m-f480", "messages": [{"role": "user", "content": "hi"}]}),
                Some(&key),
                Some("tenant-f480"),
            );
            let response = common::build_llm_app(state)
                .oneshot(req)
                .await
                .expect("router call");
            let status = response.status();
            (status, body_to_json(response.into_body()).await)
        }
    };
    let (after, body) = answer(key.clone()).await;
    assert_eq!(
        after,
        StatusCode::UNAUTHORIZED,
        "the key of a deleted agent is gone: {body}"
    );
    let (unknown, unknown_body) = answer("sk_brutor_api_test_never_existed".to_string()).await;
    assert_eq!(unknown, StatusCode::UNAUTHORIZED, "{unknown_body}");
    assert_eq!(
        body, unknown_body,
        "the answer is the one an unknown key gets"
    );
    assert_eq!(
        mock.received_requests().await.unwrap_or_default().len(),
        0,
        "no provider call, before or after"
    );
}
