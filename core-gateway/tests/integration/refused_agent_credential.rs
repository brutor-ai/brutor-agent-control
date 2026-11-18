// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

// A credential of an agent identity that does not
// stand (disabled, revoked, expired, not active) is refused at
// authentication with 401. The refusal must leave an audit record,
// attributed to the agent and to the tenant that owns the credential, and
// replaying the dead credential must not multiply records. Both credentials
// an agent has: its API key and its workload token. Through the real routes.

use std::time::{Duration, Instant};

use axum::body::Body;
use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::Value;
use sqlx::PgPool;
use tower::ServiceExt;

use brutor_gateway_core::services::refused_agent_credential as refused;

use crate::common;

const T: &str = "tenant-f497";
const RG: &str = "rg-f497";

async fn ask(
    state: &brutor_gateway_core::services::SharedState,
    bearer: &str,
) -> (StatusCode, Value) {
    let req = common::build_get_request("/v1/proxy/llm/models", Some(bearer), Some(T));
    let response = common::build_llm_app(state.clone())
        .oneshot(req)
        .await
        .expect("router call");
    let status = response.status();
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

type Row = (
    String,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<i32>,
    Option<String>,
    bool,
    Option<String>,
    Option<String>,
);

/// The audit rows of `agent`, oldest first, once `n` of them are there.
async fn rows_of(pool: &PgPool, agent: &str, n: usize) -> Vec<Row> {
    let mut rows = Vec::new();
    for _ in 0..60 {
        rows = sqlx::query_as(
            "SELECT tenant_id, proxy_subtype, agent_authz_decision, api_key_id, \
                    http_status_code, error_message, is_auth_event, resource_group_id, \
                    proxy_type::text \
               FROM proxy_logs WHERE actor_agent_id = $1 ORDER BY timestamp",
        )
        .bind(agent)
        .fetch_all(pool)
        .await
        .expect("read proxy_logs");
        if rows.len() >= n {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    rows
}

async fn seed_agent(
    pool: &PgPool,
    agent: &str,
    enabled: bool,
    expires: Option<&str>,
    revoked: bool,
) {
    sqlx::query(
        "INSERT INTO agent_identities \
         (id, tenant_id, name, source, enabled, status, default_deny, expires_at, revoked_at) \
         VALUES ($1, $2, $1, 'manual', $3, 'active', false, now() + $4::interval, \
                 CASE WHEN $5 THEN now() END)",
    )
    .bind(agent)
    .bind(T)
    .bind(enabled)
    .bind(expires)
    .bind(revoked)
    .execute(pool)
    .await
    .expect("seed agent");
}

async fn key_of(pool: &PgPool, agent: &str) -> (String, String) {
    let name = format!("key-{agent}");
    let key = common::seed_api_key(pool, T, RG, None, &name).await;
    let (id,): (String,) =
        sqlx::query_as("UPDATE api_keys SET agent_id = $1 WHERE name = $2 RETURNING id")
            .bind(agent)
            .bind(&name)
            .fetch_one(pool)
            .await
            .expect("bind key");
    (key, id)
}

/// The key of a disabled, an expired and a revoked agent: each answers 401
/// with the body a revoked key gets, and each leaves one `agent_authz_block`
/// row naming the agent, the key and the key's tenant. Replaying a dead key
/// 200 times adds no row inside the interval and one row, carrying the
/// count, after it. A live agent's key leaves none.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_refused_agent_key_leaves_one_bounded_audit_record(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, RG, T).await;
    seed_agent(
        &pool,
        "agent-f497-live-611204977",
        true,
        Some("1 hour"),
        false,
    )
    .await;
    seed_agent(&pool, "agent-f497-off-611204978", false, None, false).await;
    seed_agent(
        &pool,
        "agent-f497-expired-611204979",
        true,
        Some("-1 second"),
        false,
    )
    .await;
    seed_agent(&pool, "agent-f497-revoked-611204980", true, None, true).await;
    let revoked_key = common::seed_api_key(&pool, T, RG, None, "key-plain-revoked").await;
    sqlx::query("UPDATE api_keys SET revoked = true WHERE name = 'key-plain-revoked'")
        .execute(&pool)
        .await
        .unwrap();

    let state = common::build_test_state(pool.clone());
    let revoked_answer = ask(&state, &revoked_key).await;
    assert_eq!(revoked_answer.0, StatusCode::UNAUTHORIZED);

    let (live_key, _) = key_of(&pool, "agent-f497-live-611204977").await;
    assert_eq!(ask(&state, &live_key).await.0, StatusCode::OK);

    let t0 = Instant::now();
    let interval = Duration::from_secs(refused::RECORD_INTERVAL_SECS);
    for agent in [
        "agent-f497-off-611204978",
        "agent-f497-expired-611204979",
        "agent-f497-revoked-611204980",
    ] {
        let (key, key_id) = key_of(&pool, agent).await;
        let answer = ask(&state, &key).await;
        assert_eq!(
            answer, revoked_answer,
            "{agent}: the caller is told what a revoked key is told, nothing more"
        );
        assert_eq!(refused::flush_due(&state, t0, interval).await, 1, "{agent}");
        let rows = rows_of(&pool, agent, 1).await;
        assert_eq!(rows.len(), 1, "{agent}: one audit row");
        let (tenant, subtype, decision, api_key_id, http, error, auth_event, group, proxy_type) =
            rows[0].clone();
        assert_eq!(tenant, T, "{agent}");
        assert_eq!(subtype.as_deref(), Some("agent_authz_block"), "{agent}");
        assert_eq!(decision.as_deref(), Some("deny"), "{agent}");
        assert_eq!(api_key_id.as_deref(), Some(key_id.as_str()), "{agent}");
        assert_eq!(http, Some(401), "{agent}");
        assert!(auth_event, "{agent}");
        assert_eq!(group.as_deref(), Some(RG), "{agent}");
        // The row is a row of the surface it was refused on (it was
        // `oauth`, which no evidence record is made of).
        assert_eq!(proxy_type.as_deref(), Some("llm"), "{agent}");
        let error = error.unwrap_or_default();
        assert!(
            error.contains("agent_credential_refused") && error.contains("1 refusal since"),
            "{agent}: {error}"
        );
        assert!(!error.contains(&key), "{agent}: the key is never written");

        if agent == "agent-f497-off-611204978" {
            // Replay: no row inside the interval, one after it.
            for _ in 0..200 {
                assert_eq!(ask(&state, &key).await.0, StatusCode::UNAUTHORIZED);
            }
            assert_eq!(
                refused::flush_due(&state, t0 + interval - Duration::from_secs(1), interval).await,
                0,
                "nothing is written inside the interval"
            );
            assert_eq!(refused::flush_due(&state, t0 + interval, interval).await, 1);
            let rows = rows_of(&pool, agent, 2).await;
            assert_eq!(rows.len(), 2, "200 replays are one more row");
            assert!(
                rows[1]
                    .5
                    .as_deref()
                    .unwrap_or_default()
                    .contains("200 refusals since"),
                "{:?}",
                rows[1].5
            );
        }
    }

    let (live_rows,): (i64,) = sqlx::query_as(
        "SELECT COUNT(*) FROM proxy_logs \
          WHERE actor_agent_id = 'agent-f497-live-611204977' \
            AND proxy_subtype = 'agent_authz_block'",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(live_rows, 0, "a served key leaves no refusal");
}

/// A key that acts for another tenant (a shared key): the record is the
/// key's tenant's, not the tenant the request named.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_record_belongs_to_the_tenant_that_owns_the_key(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_tenant(&pool, "tenant-f497-other").await;
    common::seed_resource_group(&pool, RG, T).await;
    seed_agent(&pool, "agent-f497-shared-611204981", false, None, false).await;
    let (key, _) = key_of(&pool, "agent-f497-shared-611204981").await;
    // No tenant on the request: the key names it.
    let state = common::build_test_state(pool.clone());
    let req = common::build_get_request("/v1/proxy/llm/models", Some(&key), None);
    let response = common::build_llm_app(state.clone())
        .oneshot(req)
        .await
        .expect("router call");
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
    let _ = Body::from(response.into_body().collect().await.unwrap().to_bytes());
    assert_eq!(refused::flush_once(&state).await, 1);
    let rows = rows_of(&pool, "agent-f497-shared-611204981", 1).await;
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].0, T);
}

/// The workload token of a disabled and of an expired agent: 401, and the
/// same record, attributed to the agent, with no key.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_refused_workload_token_leaves_the_same_record(pool: PgPool) {
    use super::workload_identity::{mint_token, mock_jwks, seed_agent_with_idp, seed_idp};

    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, RG, T).await;
    let jwks = mock_jwks("kid-f497").await;
    let issuer = "https://idp.f497.example/";
    seed_idp(
        &pool,
        "idp-f497",
        T,
        issuer,
        &format!("{}/jwks", jwks.uri()),
    )
    .await;
    let exp = (common::db_now().timestamp() + 3600) as usize;
    let state = common::build_test_state(pool.clone());
    let t0 = Instant::now();
    let interval = Duration::from_secs(refused::RECORD_INTERVAL_SECS);

    for (agent, fall) in [
        (
            "agent-f497-wl-off-611204982",
            "UPDATE agent_identities SET enabled = false WHERE id = $1",
        ),
        (
            "agent-f497-wl-expired-611204983",
            "UPDATE agent_identities SET expires_at = now() - interval '1 second' WHERE id = $1",
        ),
    ] {
        let sub = format!("spiffe://acme/{agent}");
        seed_agent_with_idp(&pool, agent, T, "idp-f497", &sub).await;
        sqlx::query(
            "INSERT INTO resource_group_agent_members (tenant_id, agent_id, group_id, joined_at) \
             VALUES ($1, $2, $3, clock_timestamp())",
        )
        .bind(T)
        .bind(agent)
        .bind(RG)
        .execute(&pool)
        .await
        .expect("membership");
        let token = mint_token(issuer, &sub, "kid-f497", exp);
        assert_eq!(
            ask(&state, &token).await.0,
            StatusCode::OK,
            "{agent}: stands"
        );
        assert_eq!(refused::flush_due(&state, t0, interval).await, 0, "{agent}");

        sqlx::query(fall)
            .bind(agent)
            .execute(&pool)
            .await
            .expect("fall");
        let unknown = mint_token(issuer, "spiffe://acme/nobody-611204999", "kid-f497", exp);
        let unknown_answer = ask(&state, &unknown).await;
        for _ in 0..5 {
            let answer = ask(&state, &token).await;
            assert_eq!(answer.0, StatusCode::UNAUTHORIZED, "{agent}");
            assert_eq!(
                answer, unknown_answer,
                "{agent}: answered as a token of no agent is"
            );
        }
        assert_eq!(refused::flush_due(&state, t0, interval).await, 1, "{agent}");
        let rows = rows_of(&pool, agent, 1).await;
        let refusals: Vec<&Row> = rows
            .iter()
            .filter(|r| r.1.as_deref() == Some("agent_authz_block"))
            .collect();
        assert_eq!(refusals.len(), 1, "{agent}: five refusals, one row");
        let r = refusals[0];
        assert_eq!(r.0, T);
        assert_eq!(r.2.as_deref(), Some("deny"));
        assert_eq!(r.3, None, "no key");
        assert_eq!(r.4, Some(401));
        assert!(r.6);
        let error = r.5.clone().unwrap_or_default();
        assert!(
            error.contains("workload_token") && error.contains("5 refusals since"),
            "{error}"
        );
        assert!(!error.contains(&token));
    }
    // A token that verifies against nothing, or names no agent, is not an
    // agent's credential: nothing is tracked for it.
    assert_eq!(
        refused::flush_due(&state, t0 + interval * 3, interval).await,
        0
    );
}

/// A verified token of an agent that is a member of no active group: 403
/// `AGENT_NOT_BOUND`, and a record of the refusal, bounded like the others.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unbound_agents_token_is_refused_and_recorded(pool: PgPool) {
    use super::workload_identity::{mint_token, mock_jwks, seed_agent_with_idp, seed_idp};

    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, RG, T).await;
    let jwks = mock_jwks("kid-unbound").await;
    let issuer = "https://idp.unbound.example/";
    seed_idp(
        &pool,
        "idp-unbound",
        T,
        issuer,
        &format!("{}/jwks", jwks.uri()),
    )
    .await;
    let agent = "agent-unbound-611205001";
    let sub = format!("spiffe://acme/{agent}");
    seed_agent_with_idp(&pool, agent, T, "idp-unbound", &sub).await;
    let exp = (common::db_now().timestamp() + 3600) as usize;
    let token = mint_token(issuer, &sub, "kid-unbound", exp);
    let state = common::build_test_state(pool.clone());
    let t0 = Instant::now();
    let interval = Duration::from_secs(refused::RECORD_INTERVAL_SECS);

    for _ in 0..3 {
        assert_eq!(ask(&state, &token).await.0, StatusCode::FORBIDDEN);
    }
    assert_eq!(refused::flush_due(&state, t0, interval).await, 1);
    let rows = rows_of(&pool, agent, 1).await;
    let refusals: Vec<&Row> = rows
        .iter()
        .filter(|r| r.1.as_deref() == Some("agent_authz_block"))
        .collect();
    assert_eq!(refusals.len(), 1, "three refusals, one row");
    let r = refusals[0];
    assert_eq!(r.4, Some(403));
    let error = r.5.clone().unwrap_or_default();
    assert!(
        error.starts_with("AGENT_NOT_BOUND: ") && error.contains("3 refusals since"),
        "{error}"
    );
    assert!(!error.contains(&token));

    // Bound to a group, the same token is served.
    sqlx::query(
        "INSERT INTO resource_group_agent_members (tenant_id, agent_id, group_id, joined_at) \
         VALUES ($1, $2, $3, clock_timestamp())",
    )
    .bind(T)
    .bind(agent)
    .bind(RG)
    .execute(&pool)
    .await
    .expect("membership");
    assert_eq!(ask(&state, &token).await.0, StatusCode::OK);
}

// ---------------------------------------------------------------------------
// The record is sealed, with the route
// ---------------------------------------------------------------------------

use crate::evidence_runtime::{assert_verifies, enable_evidence, records};

/// The user the dead keys are bound to. Asserted absent from the sealed
/// records by substring: nine digits do not turn up in a hash by chance.
const KEY_USER: i32 = 611_204_990;

/// What the caller is told: status, headers and body.
async fn told(
    app: axum::Router,
    req: axum::http::Request<Body>,
) -> (StatusCode, Vec<(String, String)>, Vec<u8>) {
    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let mut headers: Vec<(String, String)> = response
        .headers()
        .iter()
        // Per-request ids differ between any two requests.
        .filter(|(k, _)| {
            !matches!(
                k.as_str(),
                "x-brutor-run-id" | "x-request-id" | "x-correlation-id" | "date" | "traceparent"
            )
        })
        .map(|(k, v)| (k.to_string(), v.to_str().unwrap_or("").to_string()))
        .collect();
    headers.sort();
    let body = response.into_body().collect().await.unwrap().to_bytes();
    (status, headers, body.to_vec())
}

#[derive(Clone, Copy, Debug)]
enum Family {
    Llm,
    Mcp,
    A2a,
}

impl Family {
    fn request(self, key: &str) -> axum::http::Request<Body> {
        match self {
            Family::Llm => common::build_post_request(
                "/v1/proxy/llm/chat/completions",
                serde_json::json!({"model_id": "m-f497",
                    "messages": [{"role": "user", "content": "hi"}]}),
                Some(key),
                Some(T),
            ),
            Family::Mcp => common::build_post_request(
                "/v1/proxy/mcp/ms-f497-611204991",
                serde_json::json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                Some(key),
                Some(T),
            ),
            Family::A2a => common::build_post_request(
                &format!("/a2a/{T}/card-f497-611204992/message:send"),
                serde_json::json!({"message": {"messageId": "01msg", "role": "ROLE_USER",
                    "parts": [{"text": "hi"}]}}),
                Some(key),
                Some(T),
            ),
        }
    }
    fn app(self, state: &brutor_gateway_core::services::SharedState) -> axum::Router {
        match self {
            Family::Llm => common::build_llm_app(state.clone()),
            Family::Mcp => common::build_mcp_proxy_app(state.clone()),
            Family::A2a => common::build_a2a_v1_app(state.clone()),
        }
    }
    fn surface(self) -> &'static str {
        match self {
            Family::Llm => "llm",
            Family::Mcp => "mcp",
            Family::A2a => "a2a",
        }
    }
    fn route(self) -> &'static str {
        match self {
            Family::Llm => "/v1/proxy/llm/chat/completions",
            Family::Mcp => "/v1/proxy/mcp/{}",
            Family::A2a => "/a2a/{}/{}/message:send",
        }
    }
}

/// The key of a fallen agent, refused on a route of each family: the caller
/// is told exactly what a revoked key is told on that route, and the
/// refusal is one sealed `denied` record of that surface, which states the
/// route (no name from the path), how many refusals it stands for and the
/// kind of credential, verifies, and carries no key, key id or user id.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_refused_agent_key_is_a_sealed_denied_record_on_every_family(pool: PgPool) {
    enable_evidence();
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, RG, T).await;
    common::seed_end_user(&pool, KEY_USER, T).await;
    common::add_direct_membership(&pool, KEY_USER, RG, T).await;
    common::seed_mcp_server(&pool, "ms-f497-611204991", T, "ms-f497-611204991").await;
    common::seed_agent_card(&pool, "card-f497", T, "card-f497-611204992").await;
    common::bind_agent_card_to_rg(&pool, "card-f497", RG).await;
    let revoked_key =
        common::seed_api_key(&pool, T, RG, Some(KEY_USER), "key-sealed-revoked").await;
    sqlx::query("UPDATE api_keys SET revoked = true WHERE name = 'key-sealed-revoked'")
        .execute(&pool)
        .await
        .unwrap();
    let state = common::build_test_state(pool.clone());
    let t0 = Instant::now();
    let interval = Duration::from_secs(refused::RECORD_INTERVAL_SECS);

    for (family, agent, replays) in [
        (Family::Llm, "agent-f497-sealed-llm-611204993", 3u64),
        (Family::Mcp, "agent-f497-sealed-mcp-611204994", 1),
        (Family::A2a, "agent-f497-sealed-a2a-611204995", 2),
    ] {
        seed_agent(&pool, agent, false, None, false).await;
        let (key, key_id) = key_of(&pool, agent).await;
        sqlx::query("UPDATE api_keys SET user_id = $1 WHERE id = $2")
            .bind(KEY_USER)
            .bind(&key_id)
            .execute(&pool)
            .await
            .expect("bind the key to the user");
        let revoked = told(family.app(&state), family.request(&revoked_key)).await;
        // What a revoked key is told on this route: 401, on every family
        // (the MCP route let a request with a key that is not valid
        // go on as one with no credential and answered 403, while the
        // sealed record of the same refusal said 401).
        assert_eq!(revoked.0, StatusCode::UNAUTHORIZED, "{family:?}");
        for _ in 0..replays {
            let answer = told(family.app(&state), family.request(&key)).await;
            assert_eq!(
                answer, revoked,
                "{family:?}: the caller is told what a revoked key is told, byte for byte"
            );
        }
        assert_eq!(
            refused::flush_due(&state, t0, interval).await,
            1,
            "{family:?}: {replays} refusals are one record"
        );

        let (proxy_type, endpoint, record_id): (String, String, Option<String>) = {
            let mut row = None;
            for _ in 0..120 {
                row = sqlx::query_as(
                    "SELECT proxy_type::text, endpoint, record_id FROM proxy_logs \
                      WHERE actor_agent_id = $1 AND proxy_subtype = 'agent_authz_block' \
                        AND record_id IS NOT NULL",
                )
                .bind(agent)
                .fetch_optional(&pool)
                .await
                .expect("read proxy_logs");
                if row.is_some() {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
            row.unwrap_or_else(|| panic!("{family:?}: the refusal row carries a sealed record"))
        };
        assert_eq!(proxy_type, family.surface(), "{family:?}");
        assert_eq!(endpoint, family.route(), "{family:?}");
        let recs = records(&pool, T).await;
        let (_, _, verdict, record) = recs
            .iter()
            .find(|r| Some(&r.0) == record_id.as_ref())
            .unwrap_or_else(|| panic!("{family:?}: the row's record is in the log"));
        assert_eq!(verdict, "denied", "{family:?}");
        assert_eq!(record["verdict"], "denied", "{family:?}");
        assert_eq!(record["disposition"]["approver"], "policy", "{family:?}");
        assert!(
            record.get("effect").is_none(),
            "{family:?}: nothing was done"
        );
        let ctx = &record["context"];
        assert_eq!(ctx["surface"], family.surface(), "{family:?}");
        assert_eq!(ctx["proxy_subtype"], "agent_authz_block", "{family:?}");
        assert_eq!(ctx["refused_route"], family.route(), "{family:?}");
        assert_eq!(ctx["refusals"], replays, "{family:?}");
        assert_eq!(ctx["refused_credential"], "api_key", "{family:?}");
        assert_eq!(ctx["http_status"], 401, "{family:?}");
        assert_verifies(record);
        let text = record.to_string();
        for (what, secret) in [
            ("the key", key.as_str()),
            ("the key's id", key_id.as_str()),
            ("the user id", &KEY_USER.to_string()),
            ("the MCP server named in the path", "ms-f497-611204991"),
            ("the agent card named in the path", "card-f497-611204992"),
        ] {
            assert!(
                !text.contains(secret),
                "{family:?}: {what} is in the record"
            );
        }
    }

    // One record each: the chain did not grow by a record per replay.
    let denied = records(&pool, T)
        .await
        .iter()
        .filter(|r| r.2 == "denied")
        .count();
    assert_eq!(denied, 3);
    // Replaying inside the interval seals nothing more.
    assert_eq!(
        refused::flush_due(&state, t0 + interval - Duration::from_secs(1), interval).await,
        0
    );
}

/// The MCP proxy route lets a request with no
/// credential in (it goes on as anonymous and is answered by the server's
/// access rule). A request that PRESENTS an API key which is not valid
/// (unknown, revoked, or the key of an agent identity that does not stand)
/// is not such a request: the credential is invalid, and the answer is 401,
/// what every other surface answers and what the MCP authorization
/// specification requires ("Invalid or expired tokens MUST receive a HTTP
/// 401 response", revision 2025-11-25, Token Handling). It was 403, the
/// answer of the access rule to an anonymous caller, and where a server is
/// open to anonymous callers the dead key was served. A request with no
/// credential is answered as before.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_api_key_that_is_not_valid_is_401_on_the_mcp_route_and_no_key_is_anonymous(
    pool: PgPool,
) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, RG, T).await;
    common::seed_end_user(&pool, KEY_USER, T).await;
    common::add_direct_membership(&pool, KEY_USER, RG, T).await;
    common::seed_mcp_server(&pool, "ms-f497-611204991", T, "ms-f497-611204991").await;
    let revoked_key = common::seed_api_key(&pool, T, RG, Some(KEY_USER), "key-f566-revoked").await;
    sqlx::query("UPDATE api_keys SET revoked = true WHERE name = 'key-f566-revoked'")
        .execute(&pool)
        .await
        .unwrap();
    seed_agent(&pool, "agent-f566-611204996", false, None, false).await;
    let (dead_agent_key, _) = key_of(&pool, "agent-f566-611204996").await;
    let state = common::build_test_state(pool.clone());

    let mut answers = Vec::new();
    for key in [
        revoked_key.as_str(),
        dead_agent_key.as_str(),
        "sk_brutor_api_test_f566_never_existed",
    ] {
        // As `X-API-Key` and as a bearer token: the same credential.
        let as_header = told(Family::Mcp.app(&state), Family::Mcp.request(key)).await;
        let mut bearer = common::build_post_request(
            "/v1/proxy/mcp/ms-f497-611204991",
            serde_json::json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
            None,
            Some(T),
        );
        bearer.headers_mut().insert(
            "authorization",
            format!("Bearer {key}").parse().expect("header"),
        );
        let as_bearer = told(Family::Mcp.app(&state), bearer).await;
        answers.push(as_header);
        answers.push(as_bearer);
    }
    for answer in &answers {
        assert_eq!(
            answer.0,
            StatusCode::UNAUTHORIZED,
            "an API key that is not valid: {}",
            String::from_utf8_lossy(&answer.2)
        );
        assert_eq!(
            answer, &answers[0],
            "one answer for every key that is not valid, byte for byte"
        );
    }
    // The 401 carries the RFC 6750 challenge and no
    // `resource_metadata`. The parameter has nothing correct to point at:
    // the protected-resource document the gateway serves for a server and
    // its identity provider declares the upstream's URL as `resource`, and
    // RFC 9728 section 3.3 has a client discard a document reached through
    // the header whose `resource` is not the URL it called; and the token
    // that flow ends in is not a credential of this route (a gateway
    // credential is), so the flow does not cure this 401. Change this
    // assertion together with those two facts, not before.
    let challenge: Vec<&str> = answers[0]
        .1
        .iter()
        .filter(|(name, _)| name == "www-authenticate")
        .map(|(_, value)| value.as_str())
        .collect();
    assert_eq!(
        challenge,
        [brutor_gateway_core::services::error_envelope::WWW_AUTHENTICATE_BEARER]
    );
    assert!(!challenge[0].contains("resource_metadata"));

    // The same answer as a route that requires a credential gives.
    let llm = told(Family::Llm.app(&state), Family::Llm.request(&revoked_key)).await;
    assert_eq!(llm.0, StatusCode::UNAUTHORIZED);

    // No credential at all: anonymous, answered by the access rule.
    let anonymous = common::build_post_request(
        "/v1/proxy/mcp/ms-f497-611204991",
        serde_json::json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
        None,
        Some(T),
    );
    let anonymous = told(Family::Mcp.app(&state), anonymous).await;
    assert_eq!(
        anonymous.0,
        StatusCode::FORBIDDEN,
        "no credential is not an invalid credential: {}",
        String::from_utf8_lossy(&anonymous.2)
    );
}

/// A verified workload token of a fallen agent, refused on the chat route:
/// the same sealed record, credential `workload_token`, and the token is
/// not in it.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_refused_workload_token_is_a_sealed_denied_record(pool: PgPool) {
    use super::workload_identity::{mint_token, mock_jwks, seed_agent_with_idp, seed_idp};
    enable_evidence();
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, RG, T).await;
    let jwks = mock_jwks("kid-f497s").await;
    let issuer = "https://idp.f497s.example/";
    seed_idp(
        &pool,
        "idp-f497s",
        T,
        issuer,
        &format!("{}/jwks", jwks.uri()),
    )
    .await;
    let exp = (common::db_now().timestamp() + 3600) as usize;
    let agent = "agent-f497-wl-sealed-611204996";
    let sub = format!("spiffe://acme/{agent}");
    seed_agent_with_idp(&pool, agent, T, "idp-f497s", &sub).await;
    sqlx::query("UPDATE agent_identities SET enabled = false WHERE id = $1")
        .bind(agent)
        .execute(&pool)
        .await
        .expect("fall");
    let token = mint_token(issuer, &sub, "kid-f497s", exp);
    let state = common::build_test_state(pool.clone());
    for _ in 0..4 {
        let answer = told(Family::Llm.app(&state), Family::Llm.request(&token)).await;
        assert_eq!(answer.0, StatusCode::UNAUTHORIZED);
    }
    assert_eq!(refused::flush_once(&state).await, 1);
    let mut record = None;
    for _ in 0..120 {
        record = records(&pool, T)
            .await
            .into_iter()
            .find(|r| r.2 == "denied");
        if record.is_some() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let (_, _, _, record) = record.expect("a sealed denied record");
    let ctx = &record["context"];
    assert_eq!(ctx["refused_route"], "/v1/proxy/llm/chat/completions");
    assert_eq!(ctx["refusals"], 4);
    assert_eq!(ctx["refused_credential"], "workload_token");
    assert_verifies(&record);
    let text = record.to_string();
    assert!(!text.contains(&token), "the token is in the record");
    for part in token.split('.') {
        assert!(!text.contains(part), "a part of the token is in the record");
    }
}

/// A media route authenticates more than once per request. One
/// refused request is one refusal.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_refused_media_request_counts_once(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, RG, T).await;
    let agent = "agent-f527-media-611204997";
    seed_agent(&pool, agent, false, None, false).await;
    let (key, _) = key_of(&pool, agent).await;
    let state = common::build_test_state(pool.clone());
    for (path, body) in [
        (
            "/v1/proxy/llm/audio/speech",
            serde_json::json!({"model": "m", "voice": "alloy", "input": "hello"}),
        ),
        (
            "/v1/proxy/llm/images/generations",
            serde_json::json!({"model": "m", "prompt": "a red apple"}),
        ),
        (
            "/v1/proxy/llm/video/generations",
            serde_json::json!({"model": "m", "prompt": "a red apple"}),
        ),
    ] {
        let req = common::build_post_request(path, body, Some(&key), Some(T));
        let (status, _, _) = told(common::build_multimodal_app(state.clone()), req).await;
        assert_eq!(status, StatusCode::UNAUTHORIZED, "{path}");
    }
    // Transcription: a multipart request.
    let boundary = "----brutorf527";
    let body = format!(
        "--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.mp3\"\r\n\
         Content-Type: application/octet-stream\r\n\r\n\x00\r\n\
         --{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nm\r\n\
         --{boundary}--\r\n"
    );
    let mut req = axum::http::Request::builder()
        .method("POST")
        .uri("/v1/proxy/llm/audio/transcriptions")
        .header(
            "content-type",
            format!("multipart/form-data; boundary={boundary}"),
        )
        .header("X-Tenant-ID", T)
        .header("authorization", format!("Bearer {key}"))
        .body(Body::from(body))
        .expect("multipart request");
    req.extensions_mut().insert(axum::extract::ConnectInfo(
        "127.0.0.1:55555".parse::<std::net::SocketAddr>().unwrap(),
    ));
    let (status, _, _) = told(common::build_multimodal_app(state.clone()), req).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED, "transcription");
    assert_eq!(refused::flush_once(&state).await, 1);
    let rows = rows_of(&pool, agent, 1).await;
    assert_eq!(rows.len(), 1);
    let error = rows[0].5.clone().unwrap_or_default();
    assert!(
        error.contains("4 refusals since"),
        "four refused requests are four refusals: {error}"
    );
}

/// Redis that cannot be asked: the instance writes its record all the same,
/// on its own interval. A server on loopback that answers every command
/// with an error stands in for it.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_redis_that_cannot_answer_leaves_the_bound_per_instance(pool: PgPool) {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let asked = std::sync::Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
    let log = asked.clone();
    tokio::spawn(async move {
        while let Ok((mut socket, _)) = listener.accept().await {
            let log = log.clone();
            tokio::spawn(async move {
                let mut chunk = [0u8; 4096];
                loop {
                    let n = match socket.read(&mut chunk).await {
                        Ok(0) | Err(_) => return,
                        Ok(n) => n,
                    };
                    let text = String::from_utf8_lossy(&chunk[..n]).into_owned();
                    // One error per command (a RESP array per command).
                    let commands = text.lines().filter(|l| l.starts_with('*')).count();
                    log.lock().unwrap().push(text);
                    for _ in 0..commands {
                        if socket.write_all(b"-ERR down\r\n").await.is_err() {
                            return;
                        }
                    }
                }
            });
        }
    });
    let client = redis::Client::open(format!("redis://{addr}/")).expect("redis url");
    let redis = redis::aio::ConnectionManager::new(client)
        .await
        .expect("a connection to the stand-in");
    let state: brutor_gateway_core::services::SharedState =
        std::sync::Arc::new(brutor_gateway_core::services::AppState::new(
            brutor_gateway_core::config::AppConfig::for_test(
                "postgresql://placeholder/replaced".to_string(),
            ),
            pool.clone(),
            Some(redis),
            reqwest::Client::new(),
            brutor_gateway_core::services::telemetry::TelemetryState {
                prometheus_handle: None,
            },
        ));
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, RG, T).await;
    let agent = "agent-f497-noredis-611204998";
    seed_agent(&pool, agent, false, None, false).await;
    let (key, key_id) = key_of(&pool, agent).await;
    assert_eq!(ask(&state, &key).await.0, StatusCode::UNAUTHORIZED);
    let t0 = Instant::now();
    let interval = Duration::from_secs(refused::RECORD_INTERVAL_SECS);
    assert_eq!(refused::flush_due(&state, t0, interval).await, 1);
    assert_eq!(rows_of(&pool, agent, 1).await.len(), 1);
    let asked = asked.lock().unwrap().join("");
    assert!(
        asked.contains(&refused::interval_key(&format!("key:{key_id}"))),
        "Redis was asked for the interval"
    );
    assert!(!asked.contains(&key) && !asked.contains(&key_id));
    // Its own interval still holds.
    assert_eq!(ask(&state, &key).await.0, StatusCode::UNAUTHORIZED);
    assert_eq!(
        refused::flush_due(&state, t0 + Duration::from_secs(1), interval).await,
        0
    );
}
