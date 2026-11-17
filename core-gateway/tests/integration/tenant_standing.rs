// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! What a tenant's standing does to its requests.
//!
//! * A suspended tenant is blocked in both editions, and the wire error says
//!   so: `tenant_suspended`. (It said `trial_expired`.)
//! * A trial is the Enterprise Edition's. A Community gateway reads no trial
//!   column: a tenant whose trial is over is a tenant like any other, and no
//!   "contact sales" message exists there. Where the `multi_tenant`
//!   extension is composed, such a tenant is blocked with `trial_expired`
//!   and that message, as before.
//!
//! Asked on a proxy route (the tenant layer) and on the A2A protocol route
//! (which checks the tenant itself). The tenant-resolution matrix asks the
//! same of every route.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;

use crate::common;

const SUSPENDED: &str = "tenant-standing-suspended";
const TRIAL_OVER: &str = "tenant-standing-trial-over";
const TRIAL_RUNNING: &str = "tenant-standing-trial";

async fn seed(pool: &PgPool) {
    for tenant in [SUSPENDED, TRIAL_OVER, TRIAL_RUNNING] {
        common::seed_tenant(pool, tenant).await;
    }
    sqlx::query(
        "UPDATE tenants SET suspended = true, suspension_reason = 'Unpaid invoice' WHERE id = $1",
    )
    .bind(SUSPENDED)
    .execute(pool)
    .await
    .expect("suspend");
    sqlx::query(
        "UPDATE tenants SET is_trial = true, trial_ends_at = NOW() - interval '1 day' WHERE id = $1",
    )
    .bind(TRIAL_OVER)
    .execute(pool)
    .await
    .expect("trial over");
    sqlx::query(
        "UPDATE tenants SET is_trial = true, trial_ends_at = NOW() + interval '7 days' WHERE id = $1",
    )
    .bind(TRIAL_RUNNING)
    .execute(pool)
    .await
    .expect("trial running");
}

/// Does the edition running the suite gate trials?
fn trials_are_gated() -> bool {
    common::edition_composes("multi_tenant")
}

async fn chat(pool: &PgPool, tenant: &str) -> (StatusCode, Value) {
    let req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({"model": "m", "messages": [{"role": "user", "content": "hi"}]}),
        None,
        Some(tenant),
    );
    let resp = common::edition_app(pool.clone())
        .oneshot(req)
        .await
        .expect("router");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.expect("body").to_bytes();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

async fn a2a(pool: &PgPool, tenant: &str) -> (StatusCode, String) {
    let mut req = Request::builder()
        .method("POST")
        .uri(format!("/a2a/{tenant}/no-such-card/message:send"))
        .header("content-type", "application/json")
        .body(Body::from(
            r#"{"message":{"messageId":"m1","role":"ROLE_USER","parts":[{"text":"hi"}]}}"#,
        ))
        .expect("request");
    req.extensions_mut().insert(axum::extract::ConnectInfo(
        "127.0.0.1:55555".parse::<std::net::SocketAddr>().unwrap(),
    ));
    let resp = common::edition_app(pool.clone())
        .oneshot(req)
        .await
        .expect("router");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.expect("body").to_bytes();
    (status, String::from_utf8_lossy(&bytes).into_owned())
}

/// Suspension is `tenant_suspended`, with the operator's reason, in
/// both editions.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_suspended_tenant_is_blocked_as_suspended(pool: PgPool) {
    seed(&pool).await;
    let (status, body) = chat(&pool, SUSPENDED).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert_eq!(
        body,
        json!({"error": "tenant_suspended", "message": "Unpaid invoice"})
    );

    let (_, body) = a2a(&pool, SUSPENDED).await;
    assert!(body.contains("Tenant is suspended"), "{body}");
}

/// The trial. Gated where the Enterprise extension is composed; not read at
/// all by a Community gateway.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_trial_that_is_over_blocks_only_where_trials_are_gated(pool: PgPool) {
    seed(&pool).await;
    let (status, body) = chat(&pool, TRIAL_OVER).await;
    let (running_status, running_body) = chat(&pool, TRIAL_RUNNING).await;
    assert_ne!(running_status, StatusCode::FORBIDDEN, "{running_body}");
    let (_, a2a_body) = a2a(&pool, TRIAL_OVER).await;
    let (_, a2a_running) = a2a(&pool, TRIAL_RUNNING).await;
    assert!(!a2a_running.contains("trial"), "{a2a_running}");

    if trials_are_gated() {
        // The edition's own message (`composition.rs`).
        let contact = crate::composition::trial_expired_message()
            .expect("an edition that gates trials states their message");
        assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
        assert_eq!(body, json!({"error": "trial_expired", "message": contact}));
        assert!(a2a_body.contains(contact), "{a2a_body}");
    } else {
        // Answered exactly as the tenant whose trial runs: the columns are
        // not read.
        assert_eq!(
            (status, &body),
            (running_status, &running_body),
            "a Community gateway told a trial that is over from one that runs"
        );
        assert!(!body.to_string().contains("trial"), "{body}");
        assert!(!body.to_string().contains("sales@"), "{body}");
        assert_eq!(
            a2a_body.replace(TRIAL_OVER, "T"),
            a2a_running.replace(TRIAL_RUNNING, "T")
        );
    }
}
