// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Cache invalidations are never lost.
//!
//! Two ways a cached configuration outlived the change that should have
//! dropped it, each for the whole TTL (5 to 10 minutes):
//!
//! * a load that read the database BEFORE an admin edit inserted its
//!   result AFTER the edit's invalidation, re-caching the old value;
//! * an invalidation published while the Redis pub/sub connection was
//!   down was never delivered (pub/sub does not replay), and nothing was
//!   flushed on reconnect.

use std::time::{Duration, Instant};

use brutor_gateway_core::services::cache::{CacheInvalidator, ResourceCache, ResourceGroupCache};
use sqlx::PgPool;

use crate::common;

const T: &str = "t-cache-race";

async fn group_name(cache: &ResourceGroupCache, group: &str) -> String {
    cache
        .try_get_effective_group(T, group)
        .await
        .expect("load")
        .expect("group exists")
        .group
        .name
        .clone()
}

async fn rename(pool: &PgPool, group: &str, name: &str) {
    sqlx::query("UPDATE resource_groups SET name = $2 WHERE id = $1")
        .bind(group)
        .bind(name)
        .execute(pool)
        .await
        .expect("rename");
}

/// The loader read the group row, then blocked on a table it reads later;
/// the edit and its invalidation land while it waits.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_load_that_raced_an_invalidation_is_not_cached(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "rg-race", T).await;
    rename(&pool, "rg-race", "before").await;
    let cache = ResourceGroupCache::new(pool.clone());

    let mut lock = pool.begin().await.unwrap();
    sqlx::query("LOCK TABLE resource_group_memberships IN ACCESS EXCLUSIVE MODE")
        .execute(&mut *lock)
        .await
        .unwrap();
    let loader = {
        let cache = cache.clone();
        tokio::spawn(async move { group_name(&cache, "rg-race").await })
    };
    // Wait until the loader is queued behind the lock (it has read the
    // group row by then).
    let deadline = Instant::now() + Duration::from_secs(20);
    loop {
        let (waiting,): (i64,) = sqlx::query_as(
            "SELECT count(*) FROM pg_locks \
              WHERE NOT granted AND relation = 'resource_group_memberships'::regclass",
        )
        .fetch_one(&pool)
        .await
        .unwrap();
        if waiting > 0 {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "the loader never reached the lock"
        );
        tokio::time::sleep(Duration::from_millis(20)).await;
    }

    rename(&pool, "rg-race", "after").await;
    cache.invalidate_tenant(T).await;
    lock.rollback().await.unwrap();

    // Control: the load really raced, so it answered with what it read.
    assert_eq!(loader.await.unwrap(), "before", "the race did not happen");
    // It must not have cached that answer past the invalidation.
    assert_eq!(
        group_name(&cache, "rg-race").await,
        "after",
        "a load that began before an invalidation re-cached the old group"
    );
}

/// An edit whose invalidation was published while the listener was
/// disconnected is picked up when the listener reconnects.
#[sqlx::test(migrations = "./tests/migrations")]
async fn caches_are_flushed_when_the_invalidation_listener_reconnects(pool: PgPool) {
    let redis_url =
        std::env::var("TEST_REDIS_URL").unwrap_or_else(|_| "redis://127.0.0.1:6379/15".into());
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "rg-reconnect", T).await;
    rename(&pool, "rg-reconnect", "before").await;

    let state = common::build_test_state(pool.clone());
    let shutdown = tokio_util::sync::CancellationToken::new();
    CacheInvalidator {
        model_cache: state.model_cache.clone(),
        group_cache: state.group_cache.clone(),
        mcp_cache: state.mcp_cache.clone(),
        skill_cache: state.skill_cache.clone(),
        provider_cache: state.provider_cache.clone(),
        guardrails_cache: state.guardrails_cache.clone(),
        argument_policy_cache: state.argument_policy_cache.clone(),
        agent_card_cache: state.agent_card_cache.clone(),
        state: Some(state.clone()),
        redis: None,
        // As the listener task takes them: the composed registry.
        handlers: state.invalidation.clone(),
    }
    .spawn(redis_url.clone(), shutdown.clone());

    let client = redis::Client::open(redis_url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    let subscribed = || async {
        let mut c = client.get_multiplexed_async_connection().await.unwrap();
        let (_, n): (String, i64) = redis::cmd("PUBSUB")
            .arg("NUMSUB")
            .arg(brutor_gateway_core::services::cache::INVALIDATION_CHANNEL)
            .query_async(&mut c)
            .await
            .unwrap();
        n > 0
    };
    let deadline = Instant::now() + Duration::from_secs(20);
    while !subscribed().await {
        assert!(Instant::now() < deadline, "listener never subscribed");
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    // Let the first-subscribe flush pass before caching.
    tokio::time::sleep(Duration::from_millis(300)).await;

    let cache = &state.group_cache;
    assert_eq!(group_name(cache, "rg-reconnect").await, "before");
    // The edit lands while nothing is listening: its message is lost.
    rename(&pool, "rg-reconnect", "after").await;
    let _: redis::Value = redis::cmd("CLIENT")
        .arg("KILL")
        .arg("TYPE")
        .arg("pubsub")
        .query_async(&mut conn)
        .await
        .unwrap();
    // Control: with no message, the cache still serves the old value.
    assert_eq!(group_name(cache, "rg-reconnect").await, "before");

    // The listener reconnects (2 s backoff) and flushes.
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        if group_name(cache, "rg-reconnect").await == "after" {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "the edit made while the listener was disconnected was still not visible \
             15 s after it reconnected"
        );
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
    shutdown.cancel();
}
