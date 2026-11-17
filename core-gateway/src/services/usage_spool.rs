// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Durable spool for `usage_logs` rows that cannot be written now.
//!
//! `usage_logs` is the billing ledger. The background writer used to hold a
//! row it could not queue in a spawned task per row (unbounded memory under a
//! database stall, lost on shutdown) and to drop a row whose insert failed
//! twice. Rows now go here instead, a Redis list shared by every replica:
//! Redis is required (core refuses to start without it), the list survives a
//! pod restart, and any replica's replayer writes it back once the database
//! takes writes again.
//!
//! Two lists:
//! * [`SPOOL_KEY`]: rows waiting for the database. Replayed.
//! * [`DEAD_KEY`]: rows the database REJECTED (a data error, not an outage),
//!   kept for an operator rather than retried forever or discarded.
//!
//! The audit stream (`proxy_logs`) uses the same mechanism, because
//! those rows are the audit record and feed run evidence and the cold seed
//! of two enforcement counters:
//! * [`AUDIT_SPOOL_KEY`]: rows the writer's channel had no room for. Not yet
//!   chain-stamped; replayed through the normal pipeline.
//! * [`AUDIT_STAMPED_KEY`]: whole batches that were stamped (chain sequence,
//!   row hash, digests, retention applied) when the database stopped taking
//!   writes. Replayed as they are, so the chain closes with the original
//!   sequence numbers instead of a declared gap.
//! * [`AUDIT_DEAD_KEY`]: spooled audit data that no longer decodes.

use redis::AsyncCommands;
use redis::aio::ConnectionManager;
use serde::Serialize;
use serde::de::DeserializeOwned;

use crate::models::db::UsageLogInsert;

/// Rows waiting to be written.
pub const SPOOL_KEY: &str = "brutor:spool:usage_logs";
/// Rows the database refused; never replayed automatically.
pub const DEAD_KEY: &str = "brutor:spool:usage_logs:dead";

/// Audit rows the writer's channel had no room for.
pub const AUDIT_SPOOL_KEY: &str = "brutor:spool:proxy_logs";
/// Stamped audit batches waiting for the database.
pub const AUDIT_STAMPED_KEY: &str = "brutor:spool:proxy_logs:stamped";
/// Spooled audit data that no longer decodes; kept for an operator.
pub const AUDIT_DEAD_KEY: &str = "brutor:spool:proxy_logs:dead";

/// Append `rows` to `key`.
pub async fn push<T: Serialize>(
    redis: &mut ConnectionManager,
    key: &str,
    rows: &[T],
) -> Result<(), String> {
    if rows.is_empty() {
        return Ok(());
    }
    let encoded: Vec<String> = rows
        .iter()
        .map(serde_json::to_string)
        .collect::<Result<_, _>>()
        .map_err(|e| format!("encode spooled row: {e}"))?;
    redis
        .rpush::<_, _, ()>(key, encoded)
        .await
        .map_err(|e| format!("spool push: {e}"))
}

/// Take up to `n` usage rows from the head of the spool.
pub async fn pop(redis: &mut ConnectionManager, n: usize) -> Result<Vec<UsageLogInsert>, String> {
    pop_from(redis, SPOOL_KEY, DEAD_KEY, n).await
}

/// Take up to `n` items from the head of `key`. `LPOP` hands each item to
/// exactly one replica. An item that no longer decodes (a format change
/// across versions) is moved to `dead_key` as is.
pub async fn pop_from<T: DeserializeOwned>(
    redis: &mut ConnectionManager,
    key: &str,
    dead_key: &str,
    n: usize,
) -> Result<Vec<T>, String> {
    let Some(count) = std::num::NonZeroUsize::new(n) else {
        return Ok(Vec::new());
    };
    let raw: Option<Vec<String>> = redis
        .lpop(key, Some(count))
        .await
        .map_err(|e| format!("spool pop: {e}"))?;
    let mut rows = Vec::new();
    for item in raw.unwrap_or_default() {
        match serde_json::from_str::<T>(&item) {
            Ok(row) => rows.push(row),
            Err(e) => {
                tracing::error!(error = %e, key, "spooled item no longer decodes; moved to the dead list");
                let _ = redis.rpush::<_, _, ()>(dead_key, item).await;
            }
        }
    }
    Ok(rows)
}

/// Rows waiting in the spool.
pub async fn len(redis: &mut ConnectionManager, key: &str) -> Result<u64, String> {
    redis.llen(key).await.map_err(|e| format!("spool len: {e}"))
}

/// Whether an insert error means "the database is not taking writes right
/// now" (spool and retry later) rather than "the database refused this
/// row" (dead-letter it).
pub fn is_transient(e: &sqlx::Error) -> bool {
    !matches!(e, sqlx::Error::Database(_))
}
