// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The core decides tool approvals, also when an administrator decides in
//! the Admin Console.
//!
//! Why it matters: the decision row the core writes is what mints the
//! `resolved` evidence record with a human disposition, what the run ledger
//! reads, and what the compliance resolvers count. The control plane used to
//! update the request itself and write only its own admin audit log, so an
//! approval decided there had an `awaiting_human` record no human verdict
//! ever answered. `POST /v1/internal/approvals/decide` runs the same
//! `decide` the portal handlers run; these tests drive it through the real
//! assembly, as the control plane does, with the platform service key.

use std::net::SocketAddr;
use std::time::Duration;

use axum::Router;
use axum::body::Body;
use axum::extract::ConnectInfo;
use axum::http::{Request, StatusCode};
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use ulid::Ulid;

use brutor_gateway_core::builder::GatewayBuilder;
use brutor_gateway_core::config::AppConfig;
use brutor_gateway_core::services::AppState;
use brutor_gateway_core::services::telemetry::TelemetryState;

use crate::common;

const PATH: &str = "/v1/internal/approvals/decide";
const GROUP: &str = "rg-decide";

struct Env {
    app: Router,
    tenant: String,
    platform_key: String,
    tenant_key: String,
}

async fn setup(pool: &PgPool) -> Env {
    unsafe {
        std::env::set_var("BRUTOR_EVIDENCE_SIGNING_KEY", hex::encode([0x42u8; 32]));
        std::env::set_var("BRUTOR_EVIDENCE_WITNESS_ENABLED", "off");
    }
    brutor_gateway_core::env_vars::reload_for_tests();
    let tenant = format!("tenant-decide-{}", Ulid::new()).to_lowercase();
    common::seed_tenant(pool, &tenant).await;
    common::seed_resource_group(pool, GROUP, &tenant).await;
    let platform_key = common::seed_key(pool, &tenant, None, true, "system-health-check").await;
    let tenant_key = common::seed_api_key(pool, &tenant, GROUP, None, "an-agent").await;
    let state = AppState::new(
        AppConfig::for_test("postgresql://placeholder/replaced".to_string()),
        pool.clone(),
        None,
        reqwest::Client::new(),
        TelemetryState {
            prometheus_handle: None,
        },
    );
    let gateway = GatewayBuilder::assemble(state, vec![]).expect("assemble");
    Env {
        app: gateway.app,
        tenant,
        platform_key,
        tenant_key,
    }
}

async fn post(env: &Env, bearer: Option<&str>, body: Value) -> (StatusCode, Value) {
    let mut b = Request::builder()
        .method("POST")
        .uri(PATH)
        .header("content-type", "application/json");
    if let Some(k) = bearer {
        b = b.header("authorization", format!("Bearer {k}"));
    }
    let mut req = b.body(Body::from(body.to_string())).expect("request");
    let peer: SocketAddr = "127.0.0.1:55555".parse().expect("peer");
    req.extensions_mut().insert(ConnectInfo(peer));
    let resp = env.app.clone().oneshot(req).await.expect("router");
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20)
        .await
        .expect("body");
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

async fn seed_approval(pool: &PgPool, tenant: &str, lapsed: bool) -> String {
    let id = format!("apr-{}", Ulid::new()).to_lowercase();
    let expires = if lapsed {
        "NOW() - INTERVAL '5 minutes'"
    } else {
        "NOW() + INTERVAL '5 minutes'"
    };
    sqlx::query(&format!(
        "INSERT INTO tool_approval_requests \
         (id, tenant_id, server_config_id, server_name, capability_type, capability_name, \
          requester_type, requester_group_id, status, timeout_seconds, expires_at, created_at) \
         VALUES ($1, $2, 'srv-1', 'payments', 'tool', 'send_payment', 'agent', $3, \
                 'pending', 60, {expires}, NOW() - INTERVAL '6 minutes')"
    ))
    .bind(&id)
    .bind(tenant)
    .bind(GROUP)
    .execute(pool)
    .await
    .expect("seed approval");
    id
}

fn decide_body(env: &Env, id: &str, decision: &str) -> Value {
    json!({
        "tenant_id": env.tenant,
        "approval_request_id": id,
        "decision": decision,
        "note": "checked the amount",
        "approver": {"id": "17", "username": "olivia.admin"},
    })
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

/// The decision row, once the background writer has written it:
/// `(proxy_subtype, gateway_user_id, gateway_username)`.
async fn decision_row(pool: &PgPool, tenant: &str) -> (String, Option<String>, Option<String>) {
    for _ in 0..120 {
        let row: Option<(String, Option<String>, Option<String>)> = sqlx::query_as(
            "SELECT proxy_subtype, gateway_user_id, gateway_user_username FROM proxy_logs \
             WHERE tenant_id = $1 AND proxy_subtype LIKE 'approval\\_%' ESCAPE '\\' \
               AND proxy_subtype <> 'approval_required'",
        )
        .bind(tenant)
        .fetch_optional(pool)
        .await
        .expect("proxy logs");
        if let Some(r) = row {
            return r;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("no decision row was written for {tenant}");
}

async fn decision_rows(pool: &PgPool, tenant: &str) -> i64 {
    // let the writer drain what it has
    tokio::time::sleep(Duration::from_millis(400)).await;
    sqlx::query_scalar(
        "SELECT COUNT(*) FROM proxy_logs WHERE tenant_id = $1 \
           AND proxy_subtype IN ('approval_approved', 'approval_rejected')",
    )
    .bind(tenant)
    .fetch_one(pool)
    .await
    .expect("count")
}

async fn verdicts(pool: &PgPool, tenant: &str, verdict: &str) -> i64 {
    for _ in 0..60 {
        let n: i64 = sqlx::query_scalar(
            "SELECT COUNT(*) FROM action_records WHERE tenant_id = $1 AND verdict = $2",
        )
        .bind(tenant)
        .bind(verdict)
        .fetch_one(pool)
        .await
        .expect("records");
        if n > 0 {
            return n;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    0
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_administrators_approval_is_recorded_as_the_cores_own(pool: PgPool) {
    let env = setup(&pool).await;
    let id = seed_approval(&pool, &env.tenant, false).await;

    let (status, v) = post(
        &env,
        Some(&env.platform_key),
        decide_body(&env, &id, "approve"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{v}");
    assert_eq!(v["outcome"], "approved");
    assert_eq!(v["status"], "approved");
    assert_eq!(v["resolved_by"], "admin:17");
    let token = v["approval_token"]
        .as_str()
        .expect("a token for the agent to poll");

    let (stored_status, stored_token, resolved_by) = stored(&pool, &id).await;
    assert_eq!(stored_status, "approved");
    assert_eq!(stored_token.as_deref(), Some(token));
    // An administrator's id never reads as an end user's.
    assert_eq!(resolved_by.as_deref(), Some("admin:17"));

    // The decision row: what the run ledger, the evidence mint and the
    // compliance resolvers read.
    let (subtype, user_id, username) = decision_row(&pool, &env.tenant).await;
    assert_eq!(subtype, "approval_approved");
    assert_eq!(user_id.as_deref(), Some("admin:17"));
    assert_eq!(username.as_deref(), Some("olivia.admin"));
    assert_eq!(
        verdicts(&pool, &env.tenant, "resolved").await,
        1,
        "the human verdict is sealed"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_administrators_rejection_is_recorded_too(pool: PgPool) {
    let env = setup(&pool).await;
    let id = seed_approval(&pool, &env.tenant, false).await;

    let (status, v) = post(
        &env,
        Some(&env.platform_key),
        decide_body(&env, &id, "reject"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{v}");
    assert_eq!(v["outcome"], "rejected");
    assert_eq!(v["status"], "rejected");
    assert!(v.get("approval_token").is_none_or(Value::is_null));

    let (stored_status, stored_token, resolved_by) = stored(&pool, &id).await;
    assert_eq!(stored_status, "rejected");
    assert_eq!(stored_token, None);
    assert_eq!(resolved_by.as_deref(), Some("admin:17"));
    let (subtype, _, username) = decision_row(&pool, &env.tenant).await;
    assert_eq!(subtype, "approval_rejected");
    assert_eq!(username.as_deref(), Some("olivia.admin"));
}

/// Approving past the deadline is refused, and the core expires the
/// request and seals `expired` — once.
#[sqlx::test(migrations = "./tests/migrations")]
async fn approving_a_lapsed_request_expires_it_and_seals_once(pool: PgPool) {
    let env = setup(&pool).await;
    let id = seed_approval(&pool, &env.tenant, true).await;

    let (status, v) = post(
        &env,
        Some(&env.platform_key),
        decide_body(&env, &id, "approve"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{v}");
    assert_eq!(v["outcome"], "expired", "refused: nothing was approved");
    assert!(v.get("approval_token").is_none_or(Value::is_null));
    assert_eq!(stored(&pool, &id).await.0, "expired");
    assert_eq!(verdicts(&pool, &env.tenant, "expired").await, 1);

    // Asked again: the request is resolved; nothing more is sealed or logged.
    let (_, v) = post(
        &env,
        Some(&env.platform_key),
        decide_body(&env, &id, "approve"),
    )
    .await;
    assert_eq!(v["outcome"], "already_resolved", "{v}");
    assert_eq!(v["status"], "expired");
    assert_eq!(verdicts(&pool, &env.tenant, "expired").await, 1);
    assert_eq!(decision_rows(&pool, &env.tenant).await, 0);
}

/// A decision is made once. A second decision — the opposite one
/// included — is refused and changes nothing.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_decided_request_cannot_be_decided_again(pool: PgPool) {
    let env = setup(&pool).await;
    let id = seed_approval(&pool, &env.tenant, false).await;

    let (status, v) = post(
        &env,
        Some(&env.platform_key),
        decide_body(&env, &id, "approve"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{v}");
    let before = stored(&pool, &id).await;

    for decision in ["reject", "approve"] {
        let (status, v) = post(
            &env,
            Some(&env.platform_key),
            decide_body(&env, &id, decision),
        )
        .await;
        assert_eq!(status, StatusCode::OK, "{decision}: {v}");
        assert_eq!(v["outcome"], "already_resolved", "{decision}: {v}");
        assert_eq!(v["status"], "approved");
    }
    assert_eq!(
        stored(&pool, &id).await,
        before,
        "the first decision stands"
    );
    assert_eq!(decision_rows(&pool, &env.tenant).await, 1);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn only_the_platform_key_may_decide_and_only_for_a_request_that_exists(pool: PgPool) {
    let env = setup(&pool).await;
    let id = seed_approval(&pool, &env.tenant, false).await;

    // No credential; a tenant's own key (an agent could otherwise approve
    // its own call).
    let (status, _) = post(&env, None, decide_body(&env, &id, "approve")).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    let (status, _) = post(
        &env,
        Some(&env.tenant_key),
        decide_body(&env, &id, "approve"),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN);

    // Unknown id; the right id under another tenant.
    let (_, v) = post(
        &env,
        Some(&env.platform_key),
        decide_body(&env, "apr-nope", "approve"),
    )
    .await;
    assert_eq!(v["outcome"], "not_found", "{v}");
    let mut other = decide_body(&env, &id, "approve");
    other["tenant_id"] = json!("some-other-tenant");
    let (_, v) = post(&env, Some(&env.platform_key), other).await;
    assert_eq!(v["outcome"], "not_found", "{v}");

    // A decision that is not one, and a decision nobody made.
    let (status, _) = post(
        &env,
        Some(&env.platform_key),
        decide_body(&env, &id, "maybe"),
    )
    .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    let mut anonymous = decide_body(&env, &id, "approve");
    anonymous["approver"] = json!({"id": "", "username": ""});
    let (status, _) = post(&env, Some(&env.platform_key), anonymous).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);

    assert_eq!(
        stored(&pool, &id).await.0,
        "pending",
        "none of that decided anything"
    );
    assert_eq!(decision_rows(&pool, &env.tenant).await, 0);
}

/// The race itself: an approval and a rejection of the same request
/// arrive together. One is made; the other is told so; one decision row.
#[sqlx::test(migrations = "./tests/migrations")]
async fn two_decisions_at_once_make_one(pool: PgPool) {
    let env = setup(&pool).await;
    let mut made = 0;
    for _ in 0..4 {
        let id = seed_approval(&pool, &env.tenant, false).await;
        let (a, b) = tokio::join!(
            post(
                &env,
                Some(&env.platform_key),
                decide_body(&env, &id, "approve")
            ),
            post(
                &env,
                Some(&env.platform_key),
                decide_body(&env, &id, "reject")
            ),
        );
        let outcomes = [
            a.1["outcome"].as_str().unwrap_or("?").to_string(),
            b.1["outcome"].as_str().unwrap_or("?").to_string(),
        ];
        let decided: Vec<&String> = outcomes
            .iter()
            .filter(|o| *o == "approved" || *o == "rejected")
            .collect();
        assert_eq!(
            decided.len(),
            1,
            "exactly one decision is made: {outcomes:?}"
        );
        assert!(
            outcomes
                .iter()
                .any(|o| o == "no_longer_pending" || o == "already_resolved"),
            "the other is refused: {outcomes:?}"
        );
        assert_eq!(
            &stored(&pool, &id).await.0,
            decided[0],
            "the row is the decision made"
        );
        made += 1;
    }
    assert_eq!(decision_rows(&pool, &env.tenant).await, made);
}
