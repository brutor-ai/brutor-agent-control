// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Batched `api_keys.usage_count` / `last_used_at` accounting.
//!
//! Every authenticated API-key request used to spawn a task that ran
//! `UPDATE api_keys SET usage_count = usage_count + 1, last_used_at = NOW()`
//! for its key — one hot-row write per request, serialised on the row
//! lock for any busy key and an unbounded spawn backlog under load. The
//! model-metrics aggregator solved the identical problem for `llm_model`;
//! this is the same shape: increment in memory, flush one bulk statement
//! every few seconds, and flush once more on shutdown.
//!
//! The counters are process-wide because `authenticate_request` has no
//! handle on `AppState`. Losing at most one flush interval of counts on a
//! crash is acceptable for what is a usage indicator, not a ledger.

use std::collections::HashMap;
use std::sync::Mutex;
use std::sync::OnceLock;
use std::time::Duration;

use sqlx::PgPool;
use tokio_util::sync::CancellationToken;

/// How often pending counts are written to the database.
pub const FLUSH_INTERVAL_SECS: u64 = 5;

fn pending() -> &'static Mutex<HashMap<String, i64>> {
    static PENDING: OnceLock<Mutex<HashMap<String, i64>>> = OnceLock::new();
    PENDING.get_or_init(|| Mutex::new(HashMap::new()))
}

/// Count one use of `key_id`. Never blocks on I/O.
pub fn record(key_id: &str) {
    let mut map = pending().lock().unwrap_or_else(|p| p.into_inner());
    *map.entry(key_id.to_string()).or_insert(0) += 1;
}

/// Take back one counted use of `key_id`: for a route that authenticates a
/// request to learn who is calling and then hands it to a pipeline that
/// authenticates the same request again (the media routes).
/// The two are one use. Never goes below what was counted here: a use that
/// was already flushed stays counted.
pub fn uncount(key_id: &str) {
    let mut map = pending().lock().unwrap_or_else(|p| p.into_inner());
    if let Some(n) = map.get_mut(key_id) {
        *n -= 1;
        if *n <= 0 {
            map.remove(key_id);
        }
    }
}

/// Take everything recorded since the last flush.
fn drain() -> HashMap<String, i64> {
    let mut map = pending().lock().unwrap_or_else(|p| p.into_inner());
    std::mem::take(&mut *map)
}

/// Write pending counts in one statement. Returns the number of keys
/// updated. On failure the counts are put back so the next flush retries
/// them rather than dropping them.
pub async fn flush_once(db: &PgPool) -> Result<usize, sqlx::Error> {
    let batch = drain();
    if batch.is_empty() {
        return Ok(0);
    }
    let (ids, counts): (Vec<String>, Vec<i64>) = batch.iter().map(|(k, v)| (k.clone(), *v)).unzip();
    let result = sqlx::query(
        "UPDATE api_keys AS k \
         SET usage_count = k.usage_count + v.n, last_used_at = NOW() \
         FROM UNNEST($1::text[], $2::bigint[]) AS v(id, n) \
         WHERE k.id = v.id",
    )
    .bind(&ids)
    .bind(&counts)
    .execute(db)
    .await;
    match result {
        Ok(done) => Ok(done.rows_affected() as usize),
        Err(e) => {
            let mut map = pending().lock().unwrap_or_else(|p| p.into_inner());
            for (k, v) in batch {
                *map.entry(k).or_insert(0) += v;
            }
            Err(e)
        }
    }
}

/// Number of keys with unflushed counts (test/diagnostic hook).
pub fn pending_keys() -> usize {
    pending().lock().unwrap_or_else(|p| p.into_inner()).len()
}

/// Flush every `interval` until `shutdown` fires, then flush one last time.
pub fn spawn_flusher(db: PgPool, interval: Duration, shutdown: CancellationToken) {
    tokio::spawn(async move {
        let mut tick = tokio::time::interval(interval);
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            tokio::select! {
                _ = shutdown.cancelled() => break,
                _ = tick.tick() => {
                    if let Err(e) = flush_once(&db).await {
                        tracing::warn!(error = %e, "api key usage flush failed; will retry");
                    }
                }
            }
        }
        match flush_once(&db).await {
            Ok(n) if n > 0 => tracing::info!(keys = n, "api key usage flushed on shutdown"),
            Ok(_) => {}
            Err(e) => tracing::warn!(error = %e, "final api key usage flush failed"),
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn record_accumulates_and_drain_empties() {
        // Other tests in the process may record too; use unique ids.
        let a = format!("k-{}", uuid::Uuid::new_v4());
        let b = format!("k-{}", uuid::Uuid::new_v4());
        record(&a);
        record(&a);
        record(&b);
        let taken = drain();
        assert_eq!(taken.get(&a), Some(&2));
        assert_eq!(taken.get(&b), Some(&1));
        assert!(!drain().contains_key(&a));
    }
}
