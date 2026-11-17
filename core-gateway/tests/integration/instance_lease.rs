// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The Community gateway runs as one instance.
//!
//! Against the suite's real Redis (`TEST_REDIS_URL`), each test with a lease
//! key of its own and timings scaled down from the gateway's (15 s lease,
//! renewed every 5 s, a start waits 20 s): the design's ratios, not its
//! seconds. Nothing is flushed; a test's key lapses by itself.
//!
//! - A second instance that finds a live lease held by another is refused,
//!   with the message that names the Enterprise Edition.
//! - A restart inside the lease's lifetime starts, after waiting for the
//!   previous process's lease to lapse; after a graceful stop, at once.
//! - Once a lease has lapsed, another instance may start.
//! - The lease is renewed for as long as the gateway runs.
//! - A lease lost while serving does not stop the gateway: it is logged and
//!   a system alert is raised.
//! - A composition one of whose extensions allows several instances takes
//!   no lease, and two of them start.

use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::body::Body;
use axum::http::{Request, StatusCode};
use sqlx::PgPool;
use tower::ServiceExt;

use brutor_gateway_core::builder::{Extension, Gateway, GatewayBuilder};
use brutor_gateway_core::services::instance_lease::{
    ALERT_SOURCE, InstanceLease, LeaseSettings, Renewal, TakeError,
};
use brutor_gateway_core::tasks::TaskContext;

use crate::common::{self, connectable_config};

pub async fn redis() -> redis::aio::ConnectionManager {
    let url =
        std::env::var("TEST_REDIS_URL").unwrap_or_else(|_| "redis://127.0.0.1:6379/15".to_string());
    let client = redis::Client::open(url.clone()).expect("TEST_REDIS_URL");
    redis::aio::ConnectionManager::new(client)
        .await
        .unwrap_or_else(|e| panic!("connect to the test Redis at {url} (set TEST_REDIS_URL): {e}"))
}

/// A lease of this test's own, with the given timings in milliseconds.
pub fn lease(ttl: u64, renew_every: u64, start_wait: u64, start_retry: u64) -> LeaseSettings {
    LeaseSettings {
        ttl: Duration::from_millis(ttl),
        renew_every: Duration::from_millis(renew_every),
        start_wait: Duration::from_millis(start_wait),
        start_retry: Duration::from_millis(start_retry),
        ..common::test_lease()
    }
}

pub async fn holder(key: &str) -> Option<String> {
    let mut redis = redis().await;
    redis::cmd("GET")
        .arg(key)
        .query_async(&mut redis)
        .await
        .expect("GET the lease")
}

async fn build(pool: &PgPool, settings: &LeaseSettings) -> anyhow::Result<Gateway> {
    GatewayBuilder::new(connectable_config(pool))
        .instance_lease(settings.clone())
        .build()
        .await
}

fn id_of(gateway: &Gateway) -> String {
    gateway
        .state
        .instance_lease
        .as_ref()
        .expect("a gateway with no extensions holds a lease")
        .id()
        .to_string()
}

/// Start the one task that renews the lease, as the binary starts it: from
/// the registry, by name.
fn start_renewal(gateway: &Gateway) {
    let task = gateway
        .tasks
        .iter()
        .find(|t| t.name == "instance_lease_renewal")
        .expect("the renewal is a registered task");
    (task.start)(&TaskContext {
        state: &gateway.state,
        config: &gateway.state.config,
    });
}

pub async fn health(gateway: &Gateway) -> StatusCode {
    gateway
        .app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/health")
                .body(Body::empty())
                .expect("request"),
        )
        .await
        .expect("router")
        .status()
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_second_instance_is_refused_and_told_about_the_enterprise_edition(pool: PgPool) {
    // A start waits a little (not the design's 20 s) for a lease that does
    // not lapse in that time.
    let settings = lease(15_000, 5_000, 1_200, 200);
    let before_first = Instant::now();
    let first = build(&pool, &settings).await.expect("the first instance");
    let first_id = id_of(&first);
    assert_eq!(holder(&settings.key).await.as_deref(), Some(&*first_id));

    let started = Instant::now();
    let refused = match build(&pool, &settings).await {
        Ok(_) => panic!("a second instance started beside a live one"),
        Err(e) => e,
    };
    let since_first_started = before_first.elapsed();
    assert!(
        started.elapsed() >= settings.start_wait,
        "the second instance waited for the lease to lapse before it gave up: {:?}",
        started.elapsed()
    );
    let message = refused.to_string();
    let other = match refused.downcast_ref::<TakeError>() {
        Some(TakeError::AnotherInstance(other)) => other.clone(),
        _ => panic!("refused for another reason: {message}"),
    };
    assert_eq!(other.holder, first_id);
    // "Last seen" is the age of the first instance's heartbeat: the time
    // since it set the lease (nothing renews it in this test). It was set
    // after `before_first` and before the second start began, and read
    // after the second start had waited `start_wait`, so, whatever the
    // machine's load, it lies between those two spans (whole seconds,
    // rounded down). A fixed bound (it was `<= 3`) fails when building the
    // gateways is slow.
    let seen_ms = other.last_seen_secs * 1000;
    assert!(
        seen_ms <= since_first_started.as_millis() as u64,
        "the heartbeat is no older than the first instance: {other:?}, \
         first started {since_first_started:?} ago"
    );
    assert!(
        other.last_seen_secs >= settings.start_wait.as_secs(),
        "the heartbeat is at least as old as the second start's wait: {other:?}"
    );
    assert!(
        seen_ms < settings.ttl.as_millis() as u64,
        "a live lease was renewed within its lifetime: {other:?}"
    );
    assert_eq!(
        message,
        format!(
            "Another Core Gateway instance is already running against this Redis and database \
             (instance {first_id}, last seen {} s ago). The Community Edition runs as a single \
             instance. Running several instances for capacity or availability needs the \
             Enterprise Edition. If the other instance has just been stopped, start this one \
             again in a few seconds.",
            other.last_seen_secs
        )
    );
    // The first instance's lease is untouched, and it serves.
    assert_eq!(holder(&settings.key).await.as_deref(), Some(&*first_id));
    assert_eq!(health(&first).await, StatusCode::OK);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_restart_inside_the_lease_lifetime_starts_once_the_old_lease_lapses(pool: PgPool) {
    // The design's ratio: a start waits longer than a lease lives.
    let settings = lease(4_000, 1_000, 8_000, 100);
    // The process before the restart. It is killed: nothing is released
    // and nothing renews.
    let before = InstanceLease::take(redis().await, settings.clone())
        .await
        .expect("the instance before the restart");
    let before_id = before.id().to_string();
    drop(before);
    assert_eq!(holder(&settings.key).await.as_deref(), Some(&*before_id));

    let started = Instant::now();
    let restarted = build(&pool, &settings)
        .await
        .expect("a restart inside the lease's lifetime starts");
    let waited = started.elapsed();
    assert!(
        waited >= Duration::from_millis(3_000),
        "it waited for the old lease to lapse: {waited:?}"
    );
    let restarted_id = id_of(&restarted);
    assert_ne!(
        restarted_id, before_id,
        "a restarted process is a new instance id"
    );
    assert_eq!(holder(&settings.key).await.as_deref(), Some(&*restarted_id));

    // A graceful stop gives the lease back: the next start does not wait.
    restarted
        .state
        .instance_lease
        .as_ref()
        .expect("lease")
        .release()
        .await;
    assert_eq!(holder(&settings.key).await, None);
    let started = Instant::now();
    let again = InstanceLease::take(redis().await, settings.clone())
        .await
        .expect("a start after a stop");
    assert!(
        started.elapsed() < Duration::from_millis(1_500),
        "nothing to wait for: {:?}",
        started.elapsed()
    );
    assert_eq!(holder(&settings.key).await.as_deref(), Some(again.id()));
    // Releasing a lease that is no longer this instance's deletes nothing.
    restarted
        .state
        .instance_lease
        .as_ref()
        .expect("lease")
        .release()
        .await;
    assert_eq!(holder(&settings.key).await.as_deref(), Some(again.id()));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn after_the_lease_expires_another_instance_may_start(pool: PgPool) {
    // No waiting at start at all: only a lapsed lease lets the second in.
    let settings = lease(2_500, 300, 0, 50);
    let first = InstanceLease::take(redis().await, settings.clone())
        .await
        .expect("the first instance");
    // While it is live, another is refused at once.
    match build(&pool, &settings).await {
        Ok(_) => panic!("started beside a live lease"),
        Err(e) => assert!(
            matches!(
                e.downcast_ref::<TakeError>(),
                Some(TakeError::AnotherInstance(other)) if other.holder == first.id()
            ),
            "{e}"
        ),
    }
    // The first stops renewing (it never did) and its lease lapses.
    tokio::time::sleep(settings.ttl + Duration::from_millis(300)).await;
    assert_eq!(holder(&settings.key).await, None);
    let second = build(&pool, &settings)
        .await
        .expect("another instance starts once the lease has expired");
    start_renewal(&second);
    assert_eq!(
        holder(&settings.key).await.as_deref(),
        Some(&*id_of(&second))
    );
    // What the first finds when it wakes: the lease is another's.
    assert_eq!(
        first.renew().await,
        Renewal::Lost {
            holder: id_of(&second)
        }
    );
    assert!(first.is_lost());
    second.state.shutdown_token.cancel();
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_lease_is_renewed_for_as_long_as_the_gateway_runs(pool: PgPool) {
    let settings = lease(1_200, 300, 0, 50);
    let gateway = build(&pool, &settings).await.expect("the instance");
    let id = id_of(&gateway);
    start_renewal(&gateway);

    // Three lifetimes later the lease is still this instance's, and a
    // second instance is still refused.
    tokio::time::sleep(settings.ttl * 3).await;
    assert_eq!(holder(&settings.key).await.as_deref(), Some(&*id));
    assert!(build(&pool, &settings).await.is_err());
    assert_eq!(health(&gateway).await, StatusCode::OK);

    // The gateway stops: the renewal stops with it and the lease lapses.
    gateway.state.shutdown_token.cancel();
    tokio::time::sleep(settings.ttl + Duration::from_millis(400)).await;
    assert_eq!(holder(&settings.key).await, None);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_lost_lease_keeps_the_gateway_serving_and_raises_the_alert(pool: PgPool) {
    let settings = lease(2_000, 200, 0, 50);
    let gateway = build(&pool, &settings).await.expect("the instance");
    let id = id_of(&gateway);
    start_renewal(&gateway);

    // This instance was paused for longer than its lease lives and a second
    // one started meanwhile: the key is the other's.
    let other = "another-host/0123456789ab";
    let mut redis = redis().await;
    let _: () = redis::cmd("SET")
        .arg(&settings.key)
        .arg(other)
        .arg("PX")
        .arg(60_000)
        .query_async(&mut redis)
        .await
        .expect("the other instance's lease");

    let mut alert: Option<(String, String, String, String, i32)> = None;
    for _ in 0..100 {
        alert = sqlx::query_as(
            "SELECT severity, category, title, message, occurrence_count FROM system_alerts \
             WHERE source = $1 AND status = 'active'",
        )
        .bind(ALERT_SOURCE)
        .fetch_optional(&pool)
        .await
        .expect("system_alerts");
        if alert.is_some() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let (severity, category, title, message, _) = alert.expect("a system alert for the lost lease");
    assert_eq!(severity, "high");
    assert_eq!(category, "infrastructure_threshold");
    assert_eq!(title, "A second gateway instance is running");
    assert!(
        message.contains(&id) && message.contains(other) && message.contains("keeps serving"),
        "{message}"
    );
    assert!(message.contains("Enterprise Edition"), "{message}");

    // It keeps serving, it has not shut itself down, and it has not taken
    // the lease from the other instance.
    let lease = gateway.state.instance_lease.clone().expect("lease");
    assert!(lease.is_lost());
    assert_eq!(health(&gateway).await, StatusCode::OK);
    assert!(!gateway.state.shutdown_token.is_cancelled());
    tokio::time::sleep(settings.renew_every * 4).await;
    assert_eq!(holder(&settings.key).await.as_deref(), Some(other));
    assert_eq!(health(&gateway).await, StatusCode::OK);
    // One loss, one alert: the renewals since have not raised it again.
    let (occurrences,): (i32,) = sqlx::query_as(
        "SELECT occurrence_count FROM system_alerts WHERE source = $1 AND status = 'active'",
    )
    .bind(ALERT_SOURCE)
    .fetch_one(&pool)
    .await
    .expect("the alert");
    assert_eq!(occurrences, 1);

    // The other instance goes away: this one holds the lease again.
    let _: () = redis::cmd("DEL")
        .arg(&settings.key)
        .query_async(&mut redis)
        .await
        .expect("the other instance stops");
    for _ in 0..100 {
        if holder(&settings.key).await.as_deref() == Some(&*id) {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert_eq!(holder(&settings.key).await.as_deref(), Some(&*id));
    assert!(!lease.is_lost());
    gateway.state.shutdown_token.cancel();
}

/// An extension that makes several instances a deployment, as the
/// Enterprise Edition's clustering extension does.
struct SeveralInstances;

impl Extension for SeveralInstances {
    fn name(&self) -> &'static str {
        "several_instances_test"
    }

    fn interface_version(&self) -> u32 {
        1
    }

    fn allows_multiple_instances(&self) -> bool {
        true
    }
}

/// An extension that says nothing about instances.
struct Plain;

impl Extension for Plain {
    fn name(&self) -> &'static str {
        "plain_test"
    }

    fn interface_version(&self) -> u32 {
        1
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_composition_that_allows_several_instances_takes_no_lease(pool: PgPool) {
    let settings = lease(15_000, 5_000, 0, 50);
    let mut gateways = Vec::new();
    for _ in 0..2 {
        let gateway = GatewayBuilder::new(connectable_config(&pool))
            .instance_lease(settings.clone())
            .extension(Plain)
            .extension(SeveralInstances)
            .build()
            .await
            .expect("an instance of a composition that allows several");
        assert!(gateway.allows_multiple_instances());
        assert!(gateway.state.instance_lease.is_none());
        gateways.push(gateway);
    }
    assert_eq!(holder(&settings.key).await, None, "no lease was written");
    for gateway in &gateways {
        assert_eq!(health(gateway).await, StatusCode::OK);
        // Its renewal task has nothing to renew.
        start_renewal(gateway);
    }
    assert_eq!(holder(&settings.key).await, None);

    // An extension that does not say so changes nothing: one instance.
    let one = GatewayBuilder::new(connectable_config(&pool))
        .instance_lease(settings.clone())
        .extension(Plain)
        .build()
        .await
        .expect("the first instance");
    assert!(!one.allows_multiple_instances());
    let refused = GatewayBuilder::new(connectable_config(&pool))
        .instance_lease(settings.clone())
        .extension(Plain)
        .build()
        .await;
    assert!(
        refused.is_err(),
        "a second instance of a single-instance composition"
    );
    let _keep: Arc<InstanceLease> = one.state.instance_lease.clone().expect("lease");
}
