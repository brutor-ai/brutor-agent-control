// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! proxy_logs (the audit record) rows the writer cannot take now are spooled
//! durably and written back, never dropped. These rows are the audit
//! trail, the source of run evidence (run ledger, segments) and the cold
//! seed of the media-quota and conversation-budget counters, so a dropped
//! row was missing evidence and an under-counted limit.
//!
//! Uses Redis database 13, which nothing else in the suite touches: a
//! replayer in another test process (each test has its own Postgres
//! database) must not take this test's rows.

use std::time::{Duration, Instant};

use redis::AsyncCommands;
use sqlx::PgPool;
use sqlx::postgres::PgPoolOptions;
use ulid::Ulid;

use brutor_gateway_core::models::db::ProxyLogInsert;
use brutor_gateway_core::services::audit_verify;
use brutor_gateway_core::services::background_writer::BackgroundWriter;
use brutor_gateway_core::services::proxy_logger;

use crate::background_writer_chain::test_auth;
use crate::common;

const T: &str = "t-audit-spool";
// `usage_spool::AUDIT_SPOOL_KEY` / `AUDIT_STAMPED_KEY` (pinned by
// `the_spool_keys_are_the_writers`).
const RAW: &str = "brutor:spool:proxy_logs";
const STAMPED: &str = "brutor:spool:proxy_logs:stamped";
const DEAD: &str = "brutor:spool:proxy_logs:dead";

/// The tests here own one Redis database and clear its spool lists on start, and
/// any writer's replayer takes rows from them. `.config/nextest.toml` serialises
/// them across processes; this lock does it under `cargo test` (one process).
static SERIAL: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

async fn redis13() -> redis::aio::ConnectionManager {
    let url =
        std::env::var("TEST_REDIS_URL").unwrap_or_else(|_| "redis://127.0.0.1:6379/15".into());
    let url = format!(
        "{}/13",
        url.rsplit_once('/').map(|(base, _)| base).unwrap_or(&url)
    );
    let client = redis::Client::open(url.as_str()).unwrap();
    let mut r = redis::aio::ConnectionManager::new(client).await.unwrap();
    let _: () = r.del(&[RAW, STAMPED, DEAD]).await.unwrap();
    r
}

fn row() -> ProxyLogInsert {
    proxy_logger::build_proxy_log(
        "llm",
        &test_auth(T),
        "POST",
        "/v1/proxy/llm/chat/completions",
        None,
        None,
        200,
        5.0,
        true,
        None,
        &format!("corr-{}", Ulid::new()),
    )
}

/// A pool whose database is "not taking writes": closed.
async fn down(pool: &PgPool) -> PgPool {
    let p = PgPoolOptions::new().connect_lazy_with((*pool.connect_options()).clone());
    p.close().await;
    p
}

async fn landed(pool: &PgPool, ids: &[String]) -> usize {
    sqlx::query_scalar::<_, i64>("SELECT count(*) FROM proxy_logs WHERE id = ANY($1)")
        .bind(ids)
        .fetch_one(pool)
        .await
        .unwrap() as usize
}

/// Rows waiting in the spool: raw rows, plus every row of every stamped batch.
async fn spooled_rows(r: &mut redis::aio::ConnectionManager) -> usize {
    let raw: usize = r.llen(RAW).await.unwrap();
    let batches: Vec<String> = r.lrange(STAMPED, 0, -1).await.unwrap();
    raw + batches
        .iter()
        .map(|b| {
            serde_json::from_str::<serde_json::Value>(b).unwrap()["rows"]
                .as_array()
                .unwrap()
                .len()
        })
        .sum::<usize>()
}

async fn until(what: &str, secs: u64, mut ok: impl AsyncFnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(secs);
    while !ok().await {
        assert!(Instant::now() < deadline, "{what} within {secs}s");
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}

/// Was: the batch was stamped, its insert failed, the per-row retry failed,
/// and the `write_failed` declaration failed too (same database): the rows
/// were gone and the chain held an undeclared gap.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_batch_the_database_cannot_take_is_spooled_and_lands_with_its_chain(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    common::seed_tenant(&pool, T).await;
    let mut r = redis13().await;
    let rows: Vec<ProxyLogInsert> = (0..3).map(|_| row()).collect();
    let ids: Vec<String> = rows.iter().map(|l| l.id.clone()).collect();

    let writer = BackgroundWriter::new().with_shadow_redis(Some(r.clone()));
    let db_down = down(&pool).await;
    for l in rows {
        writer.write_proxy_log(db_down.clone(), l);
    }
    until("the rows are spooled", 20, async || {
        spooled_rows(&mut r.clone()).await == 3
    })
    .await;
    assert_eq!(landed(&pool, &ids).await, 0);
    // Stop this replica (its replayer would only push the batch back).
    writer.drain(Duration::from_secs(10)).await;

    // A replica whose database works writes the spool back.
    let live = BackgroundWriter::new().with_shadow_redis(Some(r.clone()));
    let control = row();
    let mut all = ids.clone();
    all.push(control.id.clone());
    live.write_proxy_log(pool.clone(), control);
    until("the spooled rows land", 40, async || {
        landed(&pool, &all).await == 4
    })
    .await;
    assert_eq!(spooled_rows(&mut r).await, 0, "the spool is empty");
    live.drain(Duration::from_secs(10)).await;

    // The chain closes: no gap, nothing declared unwritten.
    let (chain_id, from): (String, i64) = sqlx::query_as(
        "SELECT chain_id, min(chain_seq) FROM proxy_logs WHERE id = ANY($1) GROUP BY chain_id",
    )
    .bind(&all)
    .fetch_one(&pool)
    .await
    .unwrap();
    let report = audit_verify::verify_chain(&pool, &chain_id, from, 100)
        .await
        .unwrap();
    // `audit_chain::reserve_seq` is per process. Under `cargo test` every test
    // in this binary stamps from one counter into its own database, so any
    // overlap shows here as a gap that is not this test's. Only nextest (one
    // process per test, what CI and prepush-check run) can assert the chain
    // has none.
    if std::env::var_os("NEXTEST").is_some() {
        assert!(report.gaps.is_empty(), "{report:?}");
    }
    assert!(report.first_bad_seq.is_none(), "{report:?}");
    let declared: i64 =
        sqlx::query_scalar("SELECT count(*) FROM audit_chain_prunes WHERE reason = 'write_failed'")
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(declared, 0, "a spooled batch is not declared unwritten");
}

/// Was: `write_proxy_log` dropped the row when the channel was full.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_full_channel_spools_audit_rows_instead_of_dropping_them(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    common::seed_tenant(&pool, T).await;
    let mut r = redis13().await;
    let rows: Vec<ProxyLogInsert> = (0..200).map(|_| row()).collect();
    let ids: Vec<String> = rows.iter().map(|l| l.id.clone()).collect();

    // A one-row channel in front of a database that is down: almost every
    // row finds the channel full.
    let writer = BackgroundWriter::with_log_channel_capacity(1).with_shadow_redis(Some(r.clone()));
    let db_down = down(&pool).await;
    for l in rows {
        writer.write_proxy_log(db_down.clone(), l);
    }
    until("every row is spooled", 30, async || {
        spooled_rows(&mut r.clone()).await == 200
    })
    .await;
    let raw: usize = r.llen(RAW).await.unwrap();
    assert!(raw > 0, "the full-channel path was exercised");
    let stats = writer.stats();
    assert_eq!(
        stats.audit_lost.load(std::sync::atomic::Ordering::Relaxed),
        0
    );
    writer.drain(Duration::from_secs(10)).await;

    let live = BackgroundWriter::new().with_shadow_redis(Some(r.clone()));
    live.write_proxy_log(pool.clone(), row());
    until("every spooled row lands", 60, async || {
        landed(&pool, &ids).await == 200
    })
    .await;
    live.drain(Duration::from_secs(10)).await;
}

#[test]
fn the_spool_keys_are_the_writers() {
    use brutor_gateway_core::services::usage_spool as s;
    assert_eq!(s::AUDIT_SPOOL_KEY, RAW);
    assert_eq!(s::AUDIT_STAMPED_KEY, STAMPED);
    assert_eq!(s::AUDIT_DEAD_KEY, DEAD);
}

/// Guard: the enqueue never counts a full channel as a drop and never spawns
/// per row.
#[test]
fn write_proxy_log_never_drops_a_row_for_want_of_room() {
    let src = std::fs::read_to_string(
        crate::common::community_root().join("src/services/background_writer.rs"),
    )
    .unwrap();
    let start = src.find("pub fn write_proxy_log").unwrap();
    let body = &src[start..start + src[start..].find("\n    }\n").unwrap()];
    assert!(
        !body.contains("total_dropped"),
        "write_proxy_log sheds rows:\n{body}"
    );
    assert!(
        body.contains("log_overflow_tx"),
        "no overflow path:\n{body}"
    );
    let enqueue = &body[body.find("total_scheduled").unwrap()..];
    assert!(
        !enqueue.contains("tokio::spawn"),
        "the enqueue spawns per row:\n{enqueue}"
    );
}
