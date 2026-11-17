// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Tool provenance falls back to the control plane's latest
//! capability snapshot when no `tools/list` has passed through this instance.

use serde_json::json;
use sqlx::PgPool;
use ulid::Ulid;

use brutor_gateway_core::services::tool_provenance::{definition_hash, lookup};

use super::common;

async fn snapshot(
    pool: &PgPool,
    tenant: &str,
    server: &str,
    offset_secs: i64,
    caps: &serde_json::Value,
) {
    sqlx::query(
        "INSERT INTO mcp_capability_snapshots (id, tenant_id, mcp_server_id, snapshot_at, capabilities, trigger) \
         VALUES ($1, $2, $3, now() + make_interval(secs => $4), $5::json, 'discovery')",
    )
    .bind(format!("snap-{}", Ulid::new()))
    .bind(tenant)
    .bind(server)
    .bind(offset_secs as f64)
    .bind(caps.to_string())
    .execute(pool)
    .await
    .expect("snapshot");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_tool_never_listed_through_this_instance_is_hashed_from_the_latest_snapshot(
    pool: PgPool,
) {
    let t = "t-prov";
    common::seed_tenant(&pool, t).await;
    let srv = format!("srv-{}", Ulid::new());
    common::seed_mcp_server(&pool, &srv, t, "crm").await;

    let old = json!({"serverInfo": {"version": "1.0.0"},
        "tools": [{"name": "get_order", "description": "old"}]});
    let new = json!({"serverInfo": {"version": "2.4.1"},
        "tools": [{"name": "get_order", "description": "new", "inputSchema": {"type": "object"}}]});
    snapshot(&pool, t, &srv, -60, &old).await;
    snapshot(&pool, t, &srv, 0, &new).await;

    let (version, hash) = lookup(&pool, t, &srv, "get_order").await;
    assert_eq!(
        version.as_deref(),
        Some("2.4.1"),
        "the latest snapshot, not the first"
    );
    assert_eq!(hash, Some(definition_hash(&new["tools"][0])));
    assert_ne!(hash, Some(definition_hash(&old["tools"][0])));

    // Listed, but no such tool: no hash rather than a guess.
    assert_eq!(lookup(&pool, t, &srv, "nope").await.1, None);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_server_with_no_snapshot_yields_nothing(pool: PgPool) {
    let t = "t-prov-none";
    common::seed_tenant(&pool, t).await;
    let srv = format!("srv-{}", Ulid::new());
    common::seed_mcp_server(&pool, &srv, t, "crm").await;
    assert_eq!(lookup(&pool, t, &srv, "get_order").await, (None, None));
}
