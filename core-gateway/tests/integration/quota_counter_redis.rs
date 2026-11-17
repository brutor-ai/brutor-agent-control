// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The quota counters' Lua scripts against a REAL Redis.
//!
//! Every limiter unit test runs on `UsageState::new(None)` — the in-memory
//! twin — so before this file no test had executed a single one of the Lua
//! scripts production enforces with. In-memory
//! mocks are not proof of distributed atomicity.
//!
//! * The reconcile heal raises a warm, under-counted counter to the
//!   ledger, never lowers one that is ahead, and keeps the window TTL.
//! * The admission script admits exactly `limit` units under real
//!   concurrency (the engine's own suite is `admission_redis.rs`).
//!
//! Needs `TEST_REDIS_URL` (CI provides a service container). Keys are
//! namespaced per test run and expire with their window.

use brutor_gateway_core::services::cache::usage_state::{QuotaWindow, UsageState};

fn redis_url() -> String {
    std::env::var("TEST_REDIS_URL").unwrap_or_else(|_| "redis://127.0.0.1:6379/15".to_string())
}

async fn state() -> UsageState {
    let url = redis_url();
    let client = redis::Client::open(url.as_str()).expect("redis url");
    let conn = redis::aio::ConnectionManager::new(client)
        .await
        .unwrap_or_else(|e| panic!("connect to the test Redis at {url} (set TEST_REDIS_URL): {e}"));
    UsageState::new(Some(conn))
}

fn key(name: &str) -> String {
    format!("tok:test-{}-{name}", uuid::Uuid::new_v4().simple())
}

#[tokio::test]
async fn the_heal_raises_a_warm_counter_and_never_lowers_one() {
    let s = state().await;
    let k = key("heal");

    s.set_usage(&k, QuotaWindow::Day, 100).await;
    assert!(
        s.raise_usage_to(&k, QuotaWindow::Day, 250).await,
        "behind the ledger: raised"
    );
    assert_eq!(s.read_usage(&k, QuotaWindow::Day).await, 250);

    assert!(
        !s.raise_usage_to(&k, QuotaWindow::Day, 200).await,
        "ahead of the ledger: untouched"
    );
    assert_eq!(s.read_usage(&k, QuotaWindow::Day).await, 250);

    let cold = key("cold");
    assert!(
        s.raise_usage_to(&cold, QuotaWindow::Day, 40).await,
        "cold: seeded"
    );
    assert_eq!(s.read_usage(&cold, QuotaWindow::Day).await, 40);
}

#[tokio::test]
async fn concurrent_reservations_never_admit_past_the_limit() {
    let s = std::sync::Arc::new(state().await);
    let k = key("reserve");
    let limit = 10;

    let tasks: Vec<_> = (0..50)
        .map(|_| {
            let s = s.clone();
            let k = k.clone();
            tokio::spawn(async move {
                let dim = brutor_gateway_core::services::limits::usage_dim(
                    &k,
                    QuotaWindow::Day,
                    Some(limit),
                    brutor_gateway_core::services::cache::usage_state::Measure::One,
                    brutor_gateway_core::services::limits::DimRefusal::Accounting,
                );
                brutor_gateway_core::services::limits::admit(
                    &s,
                    None,
                    vec![dim],
                    brutor_gateway_core::services::cache::usage_state::Shape::count(),
                )
                .await
                .unwrap()
                .is_ok()
            })
        })
        .collect();
    let mut admitted = 0;
    for t in tasks {
        if t.await.unwrap() {
            admitted += 1;
        }
    }
    assert_eq!(
        admitted, limit,
        "the reserve script is the linearisation point"
    );
}
