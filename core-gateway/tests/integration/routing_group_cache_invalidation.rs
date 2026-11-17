// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! `ModelCache` holds routing groups in a sub-cache
//! keyed by `{tenant}:{name}`. The control plane announces an edited
//! routing group by its id (`routing_group`, `llm_routing_groups.py`), and
//! `ModelCache::invalidate` evicted models and member snapshots but never
//! that sub-cache: an edited, or disabled, routing group was served as it
//! was for up to the cache's lifetime (300 s).

use brutor_gateway_core::services::cache::ResourceCache;
use sqlx::PgPool;

use crate::common;

const T: &str = "tenant-f492";

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_edited_routing_group_is_not_served_stale_after_its_invalidation(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_tenant(&pool, "tenant-f492-other").await;
    // The name is not the id, as in a real installation.
    for (id, tenant) in [
        ("rgid-f492-840217365", T),
        ("rgid-f492-other-840217366", "tenant-f492-other"),
    ] {
        sqlx::query(
            "INSERT INTO llm_routing_group (id, tenant_id, name, mode, enabled) \
             VALUES ($1, $2, 'fast-pool', 'chat', true)",
        )
        .bind(id)
        .bind(tenant)
        .execute(&pool)
        .await
        .expect("routing group");
    }
    let state = common::build_test_state(pool.clone());
    let cache = &state.model_cache;
    let read = |tenant: &'static str| async move {
        cache
            .get_routing_group(tenant, "fast-pool")
            .await
            .expect("read")
            .map(|g| g.description.clone())
    };
    assert_eq!(read(T).await, Some(None), "warmed");
    assert_eq!(read("tenant-f492-other").await, Some(None), "warmed");

    // The operator edits the group; the control plane announces its id.
    sqlx::query("UPDATE llm_routing_group SET description = 'edited' WHERE tenant_id = $1")
        .bind(T)
        .execute(&pool)
        .await
        .unwrap();
    assert_eq!(
        read(T).await,
        Some(None),
        "still cached before the announcement"
    );
    cache.invalidate(T, "rgid-f492-840217365").await;
    assert_eq!(
        read(T).await,
        Some(Some("edited".to_string())),
        "the edited group is read again after its invalidation"
    );

    // Disabled: no longer a routing group, on the next call.
    sqlx::query("UPDATE llm_routing_group SET enabled = false WHERE tenant_id = $1")
        .bind(T)
        .execute(&pool)
        .await
        .unwrap();
    cache.invalidate(T, "rgid-f492-840217365").await;
    assert_eq!(read(T).await, None, "a disabled group is not served");

    // Another tenant's group of the same name stays warm: edited behind
    // the cache's back, with no announcement for it, it is still the
    // cached row.
    sqlx::query("UPDATE llm_routing_group SET description = 'other' WHERE tenant_id = $1")
        .bind("tenant-f492-other")
        .execute(&pool)
        .await
        .unwrap();
    assert_eq!(read("tenant-f492-other").await, Some(None));
}
