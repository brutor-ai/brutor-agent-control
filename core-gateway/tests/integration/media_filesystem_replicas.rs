// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Against a real Redis: the instances that use a filesystem media store
//! find each other (`services::media_storage::replicas`), so that a second
//! one is logged, never silently ignored; one that stopped beating stops
//! counting.

use std::time::Duration;

use brutor_gateway_core::services::media_storage::replicas::{Presence, warning};

fn redis_url() -> String {
    std::env::var("TEST_REDIS_URL").unwrap_or_else(|_| "redis://127.0.0.1:6379/15".to_string())
}

async fn connection() -> redis::aio::ConnectionManager {
    let client = redis::Client::open(redis_url().as_str()).expect("redis url");
    redis::aio::ConnectionManager::new(client)
        .await
        .expect("a Redis at TEST_REDIS_URL")
}

#[tokio::test]
async fn a_second_instance_is_seen_and_a_silent_one_stops_counting() {
    let mut redis = connection().await;
    let key = format!("brutor:test:media-presence:{}", ulid::Ulid::new());
    let presence = Presence::new(key.clone(), Duration::from_secs(90));
    let t0 = 1_000_000_000_000u64;

    assert_eq!(presence.beat(&mut redis, "gw-a", t0).await.unwrap(), 1);
    assert_eq!(warning(1), None);
    // A second instance starts: both see two.
    assert_eq!(
        presence.beat(&mut redis, "gw-b", t0 + 1_000).await.unwrap(),
        2
    );
    assert_eq!(
        presence
            .beat(&mut redis, "gw-a", t0 + 30_000)
            .await
            .unwrap(),
        2
    );
    let text = warning(2).expect("a warning for two");
    assert!(
        text.contains("2 gateway instances") && text.contains("s3"),
        "{text}"
    );
    // A beat again is the same instance, not a third.
    assert_eq!(
        presence
            .beat(&mut redis, "gw-a", t0 + 31_000)
            .await
            .unwrap(),
        2
    );
    // gw-b stops; past the stale window only gw-a counts.
    assert_eq!(
        presence
            .beat(&mut redis, "gw-a", t0 + 92_000)
            .await
            .unwrap(),
        1
    );

    let _: () = redis::cmd("DEL")
        .arg(&key)
        .query_async(&mut redis)
        .await
        .unwrap();
}
