// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! usage_logs rows the database cannot take are spooled durably and
//! written back, never held in an unbounded queue or dropped.
//!
//! Uses Redis database 14, which nothing else in the suite touches: a
//! replayer in another test process (each test has its own Postgres
//! database) must not take this test's rows.

use std::time::{Duration, Instant};

use brutor_gateway_core::models::db::UsageLogInsert;
use brutor_gateway_core::services::background_writer::BackgroundWriter;
use brutor_gateway_core::services::usage_spool::{DEAD_KEY, SPOOL_KEY};
use redis::AsyncCommands;
use sqlx::PgPool;
use sqlx::postgres::PgPoolOptions;

use crate::common;

const T: &str = "t-usage-spool";

/// The tests here own one Redis database and clear its spool lists on start, and
/// any writer's replayer takes rows from them. `.config/nextest.toml` serialises
/// them across processes; this lock does it under `cargo test` (one process).
static SERIAL: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

async fn redis14() -> redis::aio::ConnectionManager {
    let url =
        std::env::var("TEST_REDIS_URL").unwrap_or_else(|_| "redis://127.0.0.1:6379/15".into());
    let url = format!(
        "{}/14",
        url.rsplit_once('/').map(|(base, _)| base).unwrap_or(&url)
    );
    let client = redis::Client::open(url.as_str()).unwrap();
    redis::aio::ConnectionManager::new(client).await.unwrap()
}

fn row(request_id: &str, resource_id: Option<&str>) -> UsageLogInsert {
    UsageLogInsert {
        tenant_id: T.into(),
        user_id: None,
        group_id: None,
        ancestry_path: None,
        group_name: None,
        resource_type: "llm".into(),
        resource_id: resource_id.map(String::from),
        resource_name: None,
        prompt_tokens: Some(10),
        completion_tokens: Some(5),
        total_tokens: Some(15),
        model_name: None,
        provider: None,
        mcp_tool_name: None,
        mcp_call_count: None,
        mcp_execution_time_ms: None,
        mcp_data_size_bytes: None,
        estimated_cost: Some(0.01),
        currency: None,
        prompt_cost: None,
        completion_cost: None,
        request_id: request_id.into(),
        status_code: Some(200),
        response_time_ms: None,
        http_method: None,
        endpoint: None,
        client_ip: None,
        user_agent: None,
        is_inherited: false,
        inherited_from_group_id: None,
        created_at: crate::common::db_now(),
    }
}

async fn written(pool: &PgPool, request_id: &str) -> bool {
    sqlx::query_scalar::<_, i64>("SELECT count(*) FROM usage_logs WHERE request_id = $1")
        .bind(request_id)
        .fetch_one(pool)
        .await
        .unwrap()
        == 1
}

async fn until(what: &str, secs: u64, mut ok: impl AsyncFnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(secs);
    while !ok().await {
        assert!(Instant::now() < deadline, "{what} within {secs}s");
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_row_the_database_cannot_take_is_spooled_and_replayed(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    common::seed_tenant(&pool, T).await;
    let mut r = redis14().await;
    let _: () = r.del(&[SPOOL_KEY, DEAD_KEY]).await.unwrap();

    // A database that is not taking writes: the writer's pool is closed.
    let connect = pool.connect_options();
    let down = PgPoolOptions::new().connect_lazy_with((*connect).clone());
    down.close().await;
    let writer = BackgroundWriter::new().with_shadow_redis(Some(r.clone()));
    writer.write_usage_log(down.clone(), row("req-spooled", Some("m1")));
    until("the row is spooled", 15, async || {
        let n: u64 = r.clone().llen(SPOOL_KEY).await.unwrap();
        n == 1
    })
    .await;
    assert_eq!(
        writer
            .stats()
            .total_spooled
            .load(std::sync::atomic::Ordering::Relaxed),
        1
    );
    assert!(!written(&pool, "req-spooled").await);
    // Stop this replica (its replayer would only push the row back).
    writer.drain(Duration::from_secs(10)).await;

    // A replica whose database works writes the spool back.
    let live = BackgroundWriter::new().with_shadow_redis(Some(r.clone()));
    live.write_usage_log(pool.clone(), row("req-live", Some("m1")));
    until("the spooled row is written back", 30, async || {
        written(&pool, "req-spooled").await
    })
    .await;
    assert!(written(&pool, "req-live").await);
    let left: u64 = r.llen(SPOOL_KEY).await.unwrap();
    assert_eq!(left, 0, "the spool is empty after replay");
    live.drain(Duration::from_secs(10)).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_row_the_database_refuses_is_dead_lettered_not_lost(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    common::seed_tenant(&pool, T).await;
    let mut r = redis14().await;
    let _: () = r.del(&[SPOOL_KEY, DEAD_KEY]).await.unwrap();
    let writer = BackgroundWriter::new().with_shadow_redis(Some(r.clone()));

    // `resource_id` is NOT NULL: the database refuses this row. Its
    // batch-mate is fine and must still be written.
    writer.write_usage_log(pool.clone(), row("req-good", Some("m1")));
    writer.write_usage_log(pool.clone(), row("req-bad", None));
    writer.drain(Duration::from_secs(15)).await;

    assert!(written(&pool, "req-good").await, "the good row is written");
    let dead: Vec<String> = r.lrange(DEAD_KEY, 0, -1).await.unwrap();
    assert_eq!(dead.len(), 1, "{dead:?}");
    assert!(dead[0].contains("req-bad"));
    let spooled: u64 = r.llen(SPOOL_KEY).await.unwrap();
    assert_eq!(spooled, 0, "a refused row is not replayed forever");
}

/// The overflow path never spawns per row (the old shape grew without
/// bound under a stall).
#[test]
fn a_full_usage_channel_never_spawns_a_task_per_row() {
    let src = std::fs::read_to_string(
        crate::common::community_root().join("src/services/background_writer.rs"),
    )
    .unwrap();
    let start = src.find("pub fn write_usage_log").unwrap();
    let body = &src[start..start + src[start..].find("\n    }\n").unwrap()];
    assert!(
        !body.contains("tokio::spawn"),
        "write_usage_log spawns:\n{body}"
    );
}
