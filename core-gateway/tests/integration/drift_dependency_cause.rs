// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! `find_dependency_change` blames only snapshots that are a
//! change against their predecessor (`is_change`).
//!
//! If every capability discovery appended a snapshot and the
//! cause query took ANY snapshot inside the window, an operator's
//! routine Refresh of an unchanged server would be reported as the dependency
//! change behind a drift finding. The control plane appends only when
//! the contract hash moved and flags semantic changes; this pins the core
//! side of that contract against the real schema.

use chrono::{Duration, Utc};
use serde_json::json;
use sqlx::PgPool;
use ulid::Ulid;

use brutor_gateway_core::services::drift::cause::find_dependency_change;

use super::common;

async fn snapshot(
    pool: &PgPool,
    tenant: &str,
    server: &str,
    at: chrono::DateTime<Utc>,
    is_change: bool,
) {
    sqlx::query(
        "INSERT INTO mcp_capability_snapshots \
         (id, tenant_id, mcp_server_id, snapshot_at, capabilities, trigger, content_hash, is_change) \
         VALUES ($1, $2, $3, $4, $5::json, 'discovery', $6, $7)",
    )
    .bind(format!("snap-{}", Ulid::new()))
    .bind(tenant)
    .bind(server)
    .bind(at)
    .bind(json!({"tools": [{"name": "get_order", "inputSchema": {}}]}).to_string())
    .bind(format!("{:064x}", at.timestamp()))
    .bind(is_change)
    .execute(pool)
    .await
    .expect("snapshot");
}

async fn seed_bound_server(pool: &PgPool, tenant: &str, rg: &str) -> String {
    common::seed_tenant(pool, tenant).await;
    common::seed_resource_group(pool, rg, tenant).await;
    let srv = format!("srv-{}", Ulid::new());
    common::seed_mcp_server(pool, &srv, tenant, "crm").await;
    let cfg = format!("cfg-{}", Ulid::new());
    common::seed_server_config(pool, &cfg, tenant, &srv).await;
    common::bind_server_config_to_rg(pool, &cfg, rg).await;
    srv
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_baseline_or_refresh_snapshot_is_not_a_dependency_change(pool: PgPool) {
    let (t, rg) = ("t-dep-cause", "rg-dep-cause");
    let srv = seed_bound_server(&pool, t, rg).await;
    let change_point = crate::common::db_now() - Duration::hours(1);

    // First-ever snapshot (baseline) and a representation-only row, both
    // inside the window, neither a semantic change.
    snapshot(&pool, t, &srv, change_point - Duration::minutes(30), false).await;
    snapshot(&pool, t, &srv, change_point + Duration::minutes(5), false).await;

    assert!(
        find_dependency_change(&pool, t, rg, change_point)
            .await
            .unwrap()
            .is_none(),
        "non-change snapshots must not be blamed as a dependency change"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_real_contract_change_is_still_attributed(pool: PgPool) {
    let (t, rg) = ("t-dep-cause-2", "rg-dep-cause-2");
    let srv = seed_bound_server(&pool, t, rg).await;
    let change_point = crate::common::db_now() - Duration::hours(1);

    snapshot(&pool, t, &srv, change_point - Duration::minutes(30), false).await;
    let changed_at = change_point + Duration::minutes(2);
    snapshot(&pool, t, &srv, changed_at, true).await;
    // A later non-change row must not shadow the change.
    snapshot(&pool, t, &srv, change_point + Duration::minutes(20), false).await;

    let cause = find_dependency_change(&pool, t, rg, change_point)
        .await
        .unwrap()
        .expect("the flagged change is the cause");
    assert_eq!(cause.mcp_server_id, srv);
    assert_eq!(
        cause.changed_at.timestamp_micros(),
        changed_at.timestamp_micros()
    );
}
