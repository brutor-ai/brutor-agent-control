// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A Virtual MCP server in a group must not make the group unreadable.
//!
//! A group reaches a Virtual MCP server through a `server_configs` row whose
//! `virtual_server_id` is set and whose `mcp_server_id` is NULL. The group
//! loader read `SELECT DISTINCT sc.mcp_server_id` into a non-optional
//! `String`, so the NULL failed the whole load, and every enforcement path
//! that needs the group failed closed: the live E2ETests group refused all of
//! its LLM traffic with 429 "the usage limits for this request cannot be read
//! right now" once a VMCP was bound to it.

use brutor_gateway_core::services::cache::ResourceGroupCache;
use sqlx::PgPool;

use crate::common;

const T: &str = "tenant-vmcp-binding";

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_group_with_a_virtual_mcp_server_still_loads(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "rg-vmcp", T).await;
    // An ordinary MCP server next to the virtual one, so the set is non-empty.
    common::seed_mcp_server(&pool, "srv-real", T, "srv-real").await;
    common::seed_server_config(&pool, "cfg-real", T, "srv-real").await;
    common::bind_server_config_to_rg(&pool, "cfg-real", "rg-vmcp").await;
    sqlx::query(
        "INSERT INTO virtual_servers (id, tenant_id, name, capabilities, owner, enabled) \
         VALUES ('vs-1', $1, 'vs-1', '{}'::json, 'tenant', true)",
    )
    .bind(T)
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO server_configs \
         (id, tenant_id, name, priority, is_default, enabled, usage_count, virtual_server_id) \
         VALUES ('cfg-vmcp', $1, 'cfg-vmcp', 0, false, true, 0, 'vs-1')",
    )
    .bind(T)
    .execute(&pool)
    .await
    .unwrap();
    common::bind_server_config_to_rg(&pool, "cfg-vmcp", "rg-vmcp").await;

    let cache = ResourceGroupCache::new(pool.clone());
    let group = cache
        .try_get_effective_group(T, "rg-vmcp")
        .await
        .expect("a VMCP binding must not make the group unreadable")
        .expect("the group exists");
    assert_eq!(
        group.effective_mcp_ids.iter().collect::<Vec<_>>(),
        vec!["srv-real"],
        "MCP servers are the real ones; a virtual server has none of its own"
    );
}
