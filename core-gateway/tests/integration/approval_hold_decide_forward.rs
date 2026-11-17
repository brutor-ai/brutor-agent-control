// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Hold → administrator decision → agent forward, end to end, with no Portal.
//!
//! In the Community Edition nobody signs in to the gateway: an agent calls
//! with its API key, a tool that requires approval is held, an administrator
//! decides in the Admin Console, and the control plane hands the decision to
//! the core (`POST /v1/internal/approvals/decide`, platform service key). The
//! agent polls the URL the hold gave it and re-sends the call with the
//! one-time token. The pieces were each tested; the chain was only tested
//! with a Portal approver, which is the Enterprise Edition's.
//!
//! Driven through the application the edition's binary serves
//! (`common::edition_app`): in the Community suite that is a gateway with no
//! extension.

use axum::Router;
use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;
use crate::mcp_approval_required::seed_capability_filter;

const T: &str = "tenant-hdf";
const RG: &str = "rg-hdf";
const SERVER: &str = "ms-hdf";

struct World {
    app: Router,
    upstream: MockServer,
    agent_key: String,
    platform_key: String,
}

async fn world(pool: &PgPool) -> World {
    unsafe {
        std::env::set_var("BRUTOR_EVIDENCE_SIGNING_KEY", hex::encode([0x42u8; 32]));
        std::env::set_var("BRUTOR_EVIDENCE_WITNESS_ENABLED", "off");
    }
    brutor_gateway_core::env_vars::reload_for_tests();
    let upstream = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "jsonrpc": "2.0", "id": 7,
            "result": {"content": [{"type": "text", "text": "email queued"}]}
        })))
        .mount(&upstream)
        .await;
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_mcp_server_with_base_url(pool, SERVER, T, "hdfsrv", &upstream.uri()).await;
    common::seed_server_config(pool, "sc-hdf", T, SERVER).await;
    common::bind_server_config_to_rg(pool, "sc-hdf", RG).await;
    seed_capability_filter(
        pool,
        "cap-hdf",
        T,
        RG,
        "sc-hdf",
        "send_email",
        "approval_required",
        Some(300),
    )
    .await;
    // The agent: a key bound to the group, no end user behind it.
    let agent_key = common::seed_api_key(pool, T, RG, None, "hdf-agent").await;
    // The control plane's credential.
    let platform_key = common::seed_key(pool, T, None, true, "system-health-check").await;
    World {
        app: common::edition_app(pool.clone()),
        upstream,
        agent_key,
        platform_key,
    }
}

async fn answer(resp: axum::response::Response) -> (StatusCode, Value) {
    let status = resp.status();
    let bytes = resp.into_body().collect().await.expect("body").to_bytes();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

/// The agent's tool call, with the approval token when it has one.
async fn call_tool(w: &World, token: Option<&str>) -> (StatusCode, Value) {
    let mut req = common::build_post_request(
        &format!("/v1/proxy/mcp/{SERVER}"),
        json!({
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "send_email", "arguments": {"to": "x@example.com", "body": "hi"}}
        }),
        Some(&w.agent_key),
        Some(T),
    );
    if let Some(t) = token {
        req.headers_mut()
            .insert("X-Approval-Token", t.parse().expect("token header"));
    }
    answer(w.app.clone().oneshot(req).await.expect("router")).await
}

async fn poll(w: &World, poll_url: &str) -> (StatusCode, Value) {
    let req = common::build_get_request(poll_url, Some(&w.agent_key), Some(T));
    answer(w.app.clone().oneshot(req).await.expect("router")).await
}

/// The administrator's decision, as the control plane delivers it.
async fn decide(w: &World, bearer: &str, approval: &str, decision: &str) -> (StatusCode, Value) {
    let req = common::build_post_request(
        "/v1/internal/approvals/decide",
        json!({
            "tenant_id": T,
            "approval_request_id": approval,
            "decision": decision,
            "note": "checked the recipient",
            "approver": {"id": "17", "username": "olivia.admin"},
        }),
        Some(bearer),
        None,
    );
    answer(w.app.clone().oneshot(req).await.expect("router")).await
}

async fn upstream_calls(w: &World) -> usize {
    w.upstream
        .received_requests()
        .await
        .unwrap_or_default()
        .len()
}

async fn stored(pool: &PgPool, id: &str) -> (String, Option<String>, Option<String>) {
    sqlx::query_as(
        "SELECT status, approval_token, resolved_by FROM tool_approval_requests WHERE id = $1",
    )
    .bind(id)
    .fetch_one(pool)
    .await
    .expect("approval row")
}

/// The hold: 202, the request's id and the poll URL the agent is given.
async fn hold(w: &World) -> (String, String) {
    let (status, body) = call_tool(w, None).await;
    assert_eq!(status, StatusCode::ACCEPTED, "the call is held: {body}");
    assert_eq!(body["approval_required"], json!(true), "{body}");
    let id = body["approval_request_id"]
        .as_str()
        .expect("approval_request_id")
        .to_string();
    let poll_url = body["poll_url"].as_str().expect("poll_url").to_string();
    assert_eq!(
        poll_url,
        format!("/v1/approvals/{id}/poll"),
        "the poll URL is the Community route, not a Portal one"
    );
    (id, poll_url)
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_held_call_is_forwarded_after_the_administrator_approves(pool: PgPool) {
    let w = world(&pool).await;

    // 1. The agent calls; the tool needs approval; nothing is forwarded.
    let (id, poll_url) = hold(&w).await;
    assert_eq!(upstream_calls(&w).await, 0, "a held call reached the tool");
    let (status, token, _) = stored(&pool, &id).await;
    assert_eq!(status, "pending");
    assert!(token.is_none());

    // 2. The agent polls: pending, and no token to forward with.
    let (status, body) = poll(&w, &poll_url).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["status"], "pending", "{body}");
    assert!(body.get("approval_token").is_none(), "{body}");

    // A token the agent made up forwards nothing while the request is pending.
    let (status, body) = call_tool(&w, Some("not-a-token-anybody-issued")).await;
    assert_ne!(status, StatusCode::OK, "{body}");
    assert_eq!(upstream_calls(&w).await, 0, "a made-up token forwarded");

    // The agent's own key is not the control plane's: it decides nothing.
    let (status, body) = decide(&w, &w.agent_key, &id, "approve").await;
    assert!(
        matches!(status, StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN),
        "an agent decided its own approval: {status} {body}"
    );
    assert_eq!(stored(&pool, &id).await.0, "pending");

    // 3. The administrator approves in the Admin Console; the control plane
    //    delivers the decision.
    let (status, body) = decide(&w, &w.platform_key, &id, "approve").await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["outcome"], "approved", "{body}");
    let (status, token, resolved_by) = stored(&pool, &id).await;
    assert_eq!(status, "approved");
    assert_eq!(resolved_by.as_deref(), Some("admin:17"));
    let token = token.expect("an approval mints the one-time token");

    // 4. The agent polls again and is handed the token.
    let (status, body) = poll(&w, &poll_url).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["status"], "approved", "{body}");
    assert_eq!(body["approval_token"], json!(token), "{body}");
    assert_eq!(
        upstream_calls(&w).await,
        0,
        "nothing is forwarded by a poll"
    );

    // 5. The agent re-sends the call with the token: forwarded, once.
    let (status, body) = call_tool(&w, Some(&token)).await;
    assert_eq!(
        status,
        StatusCode::OK,
        "the approved call is forwarded: {body}"
    );
    assert!(body.to_string().contains("email queued"), "{body}");
    assert_eq!(upstream_calls(&w).await, 1);
    let (status, token_after, _) = stored(&pool, &id).await;
    assert_eq!(status, "consumed");
    assert!(token_after.is_none(), "the token is spent");

    // 6. The token is one-time: a second send with it is not forwarded.
    let (status, body) = call_tool(&w, Some(&token)).await;
    assert_ne!(status, StatusCode::OK, "{body}");
    assert_eq!(upstream_calls(&w).await, 1, "a spent token forwarded again");
    let (_, body) = poll(&w, &poll_url).await;
    assert!(body.get("approval_token").is_none(), "{body}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_held_call_the_administrator_rejects_is_never_forwarded(pool: PgPool) {
    let w = world(&pool).await;
    let (id, poll_url) = hold(&w).await;

    let (status, body) = decide(&w, &w.platform_key, &id, "reject").await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["outcome"], "rejected", "{body}");
    assert!(body.get("approval_token").is_none(), "{body}");
    let (status, token, resolved_by) = stored(&pool, &id).await;
    assert_eq!(status, "rejected");
    assert!(token.is_none(), "a rejection mints no token");
    assert_eq!(resolved_by.as_deref(), Some("admin:17"));

    // The agent learns of the rejection and is handed nothing to forward with.
    let (status, body) = poll(&w, &poll_url).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["status"], "rejected", "{body}");
    assert!(body.get("approval_token").is_none(), "{body}");

    // Sending again does not forward: without a token the call is held anew
    // (a new request, for a new decision); with a made-up one it is refused.
    let (status, body) = call_tool(&w, None).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    assert_ne!(body["approval_request_id"], json!(id), "{body}");
    let (status, body) = call_tool(&w, Some("not-a-token-anybody-issued")).await;
    assert_ne!(status, StatusCode::OK, "{body}");
    assert_eq!(
        upstream_calls(&w).await,
        0,
        "a rejected call reached the tool"
    );

    // A decision is made once: the rejection cannot be turned into an approval.
    let (status, body) = decide(&w, &w.platform_key, &id, "approve").await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["outcome"], "already_resolved", "{body}");
    assert_eq!(stored(&pool, &id).await.0, "rejected");
}

// ---------------------------------------------------------------------------
// Who may poll
// ---------------------------------------------------------------------------
//
// The poll hands over the one-time token, so it answers only the party the
// approval belongs to: the principal the hold recorded
// (`agent_approval::Requester`), presenting a credential that stands. Every
// other caller gets an answer that does not depend on whether the approval
// exists.

async fn poll_as(
    w: &World,
    url: &str,
    key: Option<&str>,
    tenant: Option<&str>,
) -> (StatusCode, Value) {
    let req = common::build_get_request(url, key, tenant);
    answer(w.app.clone().oneshot(req).await.expect("router")).await
}

/// `who` asks for the approval before the decision, after the approval, and
/// for an approval that does not exist: the same refusal each time, and
/// never the token.
async fn is_refused_alike(
    w: &World,
    pool: &PgPool,
    who: &str,
    key: Option<&str>,
    expected: StatusCode,
) {
    let (id, poll_url) = hold(w).await;
    let nothing = "/v1/approvals/01hzzzzzzzzzzzzzzzzzzzzzzz/poll";
    let absent = poll_as(w, nothing, key, Some(T)).await;
    assert_eq!(absent.0, expected, "{who}, no such approval: {}", absent.1);

    let pending = poll_as(w, &poll_url, key, Some(T)).await;
    assert_eq!(
        pending, absent,
        "{who}: a pending approval answers as one that does not exist"
    );

    let (status, body) = decide(w, &w.platform_key, &id, "approve").await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let token = stored(pool, &id).await.1.expect("token");
    let approved = poll_as(w, &poll_url, key, Some(T)).await;
    assert_eq!(
        approved, absent,
        "{who}: an approved approval answers as one that does not exist"
    );
    assert!(
        !approved.1.to_string().contains(&token),
        "{who} was handed the one-time token"
    );
    // The requester still collects it.
    let (status, body) = poll(w, &poll_url).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["approval_token"], json!(token), "{body}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn nobody_polls_without_a_credential(pool: PgPool) {
    let w = world(&pool).await;
    is_refused_alike(&w, &pool, "no credential", None, StatusCode::UNAUTHORIZED).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_key_nobody_issued_and_a_revoked_key_do_not_poll(pool: PgPool) {
    let w = world(&pool).await;
    is_refused_alike(
        &w,
        &pool,
        "a key nobody issued",
        Some("sk_brutor_api_test_nobody_has_this_key"),
        StatusCode::UNAUTHORIZED,
    )
    .await;
    let revoked = common::seed_api_key(&pool, T, RG, None, "hdf-revoked").await;
    sqlx::query("UPDATE api_keys SET revoked = true WHERE name = 'hdf-revoked'")
        .execute(&pool)
        .await
        .expect("revoke");
    is_refused_alike(
        &w,
        &pool,
        "a revoked key",
        Some(&revoked),
        StatusCode::UNAUTHORIZED,
    )
    .await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn another_tenants_key_does_not_poll(pool: PgPool) {
    let w = world(&pool).await;
    common::seed_tenant(&pool, "tenant-hdf-other").await;
    common::seed_resource_group(&pool, "rg-hdf-other", "tenant-hdf-other").await;
    let foreign = common::seed_api_key(
        &pool,
        "tenant-hdf-other",
        "rg-hdf-other",
        None,
        "hdf-foreign",
    )
    .await;
    is_refused_alike(
        &w,
        &pool,
        "another tenant's key",
        Some(&foreign),
        StatusCode::UNAUTHORIZED,
    )
    .await;
    // Asked on its own tenant, it finds nothing of this tenant's.
    let (id, _) = hold(&w).await;
    decide(&w, &w.platform_key, &id, "approve").await;
    let (status, body) = poll_as(
        &w,
        &format!("/v1/approvals/{id}/poll"),
        Some(&foreign),
        Some("tenant-hdf-other"),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{body}");
    assert!(body.get("approval_token").is_none(), "{body}");
}

/// A shared (`global`-scope) key of another tenant is no credential of the
/// tenant the request names (it does not act for that
/// tenant, and is not answered as a caller who is not the
/// requester: 404). It is refused as no credential is: 401.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_shared_key_of_another_tenant_does_not_poll(pool: PgPool) {
    let w = world(&pool).await;
    common::seed_tenant(&pool, "tenant-hdf-other").await;
    common::seed_resource_group(&pool, "rg-hdf-other", "tenant-hdf-other").await;
    let shared = common::seed_api_key(
        &pool,
        "tenant-hdf-other",
        "rg-hdf-other",
        None,
        "hdf-shared",
    )
    .await;
    sqlx::query("UPDATE api_keys SET scope = 'global' WHERE name = 'hdf-shared'")
        .execute(&pool)
        .await
        .expect("global scope");
    is_refused_alike(
        &w,
        &pool,
        "a shared key of another tenant",
        Some(&shared),
        StatusCode::UNAUTHORIZED,
    )
    .await;
}

/// Another key of the same tenant — of the same resource group, even — is
/// not the requester: it cannot collect another agent's token.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unrelated_key_of_the_same_tenant_does_not_poll(pool: PgPool) {
    let w = world(&pool).await;
    let peer = common::seed_api_key(&pool, T, RG, None, "hdf-peer").await;
    is_refused_alike(
        &w,
        &pool,
        "another key of the same group",
        Some(&peer),
        StatusCode::NOT_FOUND,
    )
    .await;
    common::seed_resource_group(&pool, "rg-hdf-2", T).await;
    let elsewhere = common::seed_api_key(&pool, T, "rg-hdf-2", None, "hdf-elsewhere").await;
    is_refused_alike(
        &w,
        &pool,
        "a key of another group of the tenant",
        Some(&elsewhere),
        StatusCode::NOT_FOUND,
    )
    .await;
    // The control plane's key decides; it is not the requester either.
    let platform = w.platform_key.clone();
    is_refused_alike(
        &w,
        &pool,
        "the platform service key",
        Some(&platform),
        StatusCode::NOT_FOUND,
    )
    .await;
}

/// A hold raised by an agent belongs to the agent: any key of that agent
/// polls it, a key of no agent or of another agent does not.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_agents_hold_is_polled_by_that_agent_only(pool: PgPool) {
    let mut w = world(&pool).await;
    for agent in ["agent-hdf", "agent-hdf-2"] {
        sqlx::query(
            "INSERT INTO agent_identities (id, tenant_id, name, source, enabled, status, default_deny) \
             VALUES ($1, $2, $1, 'manual', true, 'active', false)",
        )
        .bind(agent)
        .bind(T)
        .execute(&pool)
        .await
        .expect("agent identity");
    }
    let first = common::seed_api_key(&pool, T, RG, None, "hdf-agent-key-1").await;
    let second = common::seed_api_key(&pool, T, RG, None, "hdf-agent-key-2").await;
    let other_agent = common::seed_api_key(&pool, T, RG, None, "hdf-other-agent-key").await;
    sqlx::query("UPDATE api_keys SET agent_id = 'agent-hdf' WHERE name LIKE 'hdf-agent-key-%'")
        .execute(&pool)
        .await
        .expect("bind");
    sqlx::query("UPDATE api_keys SET agent_id = 'agent-hdf-2' WHERE name = 'hdf-other-agent-key'")
        .execute(&pool)
        .await
        .expect("bind");
    let plain = std::mem::replace(&mut w.agent_key, first);

    let (id, poll_url) = hold(&w).await;
    let (status, body) = decide(&w, &w.platform_key, &id, "approve").await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let token = stored(&pool, &id).await.1.expect("token");

    for (who, key) in [
        ("a key of no agent", &plain),
        ("another agent", &other_agent),
    ] {
        let (status, body) = poll_as(&w, &poll_url, Some(key), Some(T)).await;
        assert_eq!(status, StatusCode::NOT_FOUND, "{who}: {body}");
        assert!(!body.to_string().contains(&token), "{who} got the token");
    }
    let (status, body) = poll_as(&w, &poll_url, Some(&second), Some(T)).await;
    assert_eq!(status, StatusCode::OK, "the agent's other key: {body}");
    assert_eq!(body["approval_token"], json!(token), "{body}");
}
