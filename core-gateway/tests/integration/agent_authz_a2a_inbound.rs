// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

// A2A INBOUND gating by per-agent authz. Before this work the inbound v1
// `message:send` path was not gated on agent grants at all (only the RG
// card-binding applied). Now a default-deny agent with no `a2a_call` grant for
// the target card is refused with a spec-shaped PolicyBlocked (HTTP 403)
// carrying `agent_not_authorized`, even though the card IS bound to its RG.

use axum::body::Body;
use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;

use crate::common;

async fn body_to_json(body: Body) -> Value {
    let bytes = body.collect().await.expect("collect body").to_bytes();
    serde_json::from_slice(&bytes).unwrap_or(Value::Null)
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a2a_inbound_denied_for_agent_without_grant(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-a2a").await;
    common::seed_resource_group(&pool, "rg-a2a", "tenant-a2a").await;
    common::seed_end_user(&pool, 7401, "tenant-a2a").await;
    common::add_direct_membership(&pool, 7401, "rg-a2a", "tenant-a2a").await;

    // A card the caller's RG IS bound to — so the RG card-binding check passes
    // and any refusal must come from the per-agent authz layer.
    common::seed_agent_card(&pool, "card-bill", "tenant-a2a", "billing-agent").await;
    common::bind_agent_card_to_rg(&pool, "card-bill", "rg-a2a").await;

    let api_key =
        common::seed_api_key(&pool, "tenant-a2a", "rg-a2a", Some(7401), "agent-key").await;

    // Active, default-deny agent with NO a2a_call grant → inbound call denied.
    sqlx::query(
        "INSERT INTO agent_identities (id, tenant_id, name, source, enabled, status, default_deny) \
         VALUES ('agent-a2a', 'tenant-a2a', 'agent-a2a', 'manual', true, 'active', true)",
    )
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query("UPDATE api_keys SET agent_id = 'agent-a2a' WHERE tenant_id = 'tenant-a2a' AND name = 'agent-key'")
        .execute(&pool)
        .await
        .unwrap();

    let state = common::build_test_state(pool.clone());
    let app = common::build_a2a_v1_app(state);
    let req = common::build_post_request(
        "/a2a/tenant-a2a/billing-agent/message:send",
        json!({
            "message": {
                "messageId": "01msg",
                "role": "ROLE_USER",
                "parts": [{ "text": "refund order #1" }]
            }
        }),
        Some(&api_key),
        Some("tenant-a2a"),
    );

    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let body = body_to_json(response.into_body()).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "default-deny agent must be refused on inbound A2A; body={body}"
    );
    let msg = serde_json::to_string(&body).unwrap_or_default();
    assert!(
        msg.contains("agent_not_authorized") || msg.contains("PERMISSION_DENIED"),
        "refusal must be the agent-authz PolicyBlocked; body={body}"
    );
}
