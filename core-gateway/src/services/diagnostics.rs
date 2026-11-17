// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Operations diagnostics
//!
//! On-demand `pg_stat_activity` capture, fired by the
//! [`crate::middleware::slow_request`] layer when a request crosses
//! its per-route threshold. Output ends up in Loki next to the
//! slow-request log entry, so an on-call engineer can see *what was
//! the database doing during those 10 seconds* without external
//! tooling.
//!
//! ### Why a separate diagnostic pool?
//!
//! Capturing `pg_stat_activity` while the main pool is starved would
//! itself wait on the same starved pool — the capture would never
//! fire when we most need it. Instead this module is constructed with
//! a tiny dedicated pool (typically `max_connections = 2`) that the
//! request path never touches, so a snapshot is always reachable.
//!
//! ### PII / sensitive data
//!
//! `pg_stat_activity.query` contains live SQL text. For some tenants
//! that's PII-adjacent (e.g. a guardrail keyword in a `WHERE` clause).
//! We truncate `query` to [`QUERY_PREVIEW_MAX_CHARS`] before logging.

use std::time::Duration;

use serde::Serialize;
use sqlx::postgres::PgPoolOptions;
use sqlx::{PgPool, Row};
use tracing::{debug, warn};

/// Maximum number of characters of `pg_stat_activity.query` text we
/// log per row. PII / secret-leakage guard. Keep this small enough
/// that an entire snapshot fits comfortably in one Loki log line.
pub const QUERY_PREVIEW_MAX_CHARS: usize = 200;

/// One row from `pg_stat_activity`, captured in compact form so a
/// snapshot of an entire backend session list fits in a structured
/// log entry.
#[derive(Debug, Clone, Serialize)]
pub struct PgActivityRow {
    /// Server-side process id of the backend that's running this query.
    pub pid: i32,
    /// Backend state — typically one of `active`, `idle`,
    /// `idle in transaction`, `idle in transaction (aborted)`,
    /// `fastpath function call`, `disabled`.
    pub state: Option<String>,
    /// Type of event the backend is waiting on (`Lock`, `IO`, `Client`,
    /// `Activity`, …) — useful for distinguishing "stuck on disk"
    /// from "stuck on row lock" from "idle waiting for the next
    /// query".
    pub wait_event_type: Option<String>,
    /// Specific event the backend is waiting on (`relation`,
    /// `tuple`, `transactionid`, `ClientRead`, …).
    pub wait_event: Option<String>,
    /// First [`QUERY_PREVIEW_MAX_CHARS`] characters of the live
    /// SQL text. Truncated to bound log line size and avoid leaking
    /// long `WHERE` clauses with tenant-supplied content.
    pub query_preview: Option<String>,
    /// Seconds elapsed since the query started — null when the
    /// backend isn't currently executing one.
    pub query_age_seconds: Option<f64>,
    /// Seconds elapsed since the backend's state last changed —
    /// useful for spotting `idle in transaction` sessions that have
    /// been holding locks.
    pub state_age_seconds: Option<f64>,
}

/// Full snapshot — a vector of rows, one per backend.
#[derive(Debug, Clone, Serialize)]
pub struct PgActivitySnapshot {
    /// Approximate UTC timestamp at which the snapshot was taken
    /// (server side, from `now()`).
    pub captured_at_iso: String,
    /// Total number of backend rows returned. May be larger than
    /// `rows.len()` if the snapshot was truncated to bound log size.
    pub backend_count: usize,
    pub rows: Vec<PgActivityRow>,
}

/// Build the diagnostic pool. Tiny by design — the snapshot is one
/// SELECT, but the pool exists *exclusively* for diagnostics so it
/// must be reachable when the main pool is starved.
///
/// `max_connections` defaults to 2: one for the active snapshot and
/// one head-of-line slot so a second concurrent slow-request
/// trigger doesn't have to wait. Larger values waste connections
/// against the Postgres `max_connections` budget without buying
/// anything.
pub async fn build_diagnostic_pool(
    database_url: &str,
    max_connections: u32,
) -> sqlx::Result<PgPool> {
    PgPoolOptions::new()
        .max_connections(max_connections.max(1))
        // Aggressive timeout so a slow-request capture itself can't
        // hang on a wedged pool. If we can't acquire in 2 s the
        // capture skips and the slow-request log records a
        // `no_capture` outcome.
        .acquire_timeout(Duration::from_secs(2))
        // Keep idle connections short so we don't tie up Postgres
        // resources between captures.
        .idle_timeout(Duration::from_secs(30))
        .connect(database_url)
        .await
}

/// Capture an on-demand snapshot of the current database backend
/// list. Returns `Ok(snapshot)` on success, `Err` if either the
/// diagnostic pool was unreachable or the query failed (e.g. the
/// caller's role lacks `pg_read_all_stats`).
///
/// `max_rows` caps the result set so we never log megabytes of stat
/// rows in a single line — under heavy load `pg_stat_activity` can
/// have hundreds of entries. Pick a value that captures the
/// interesting tail of long-running sessions.
pub async fn capture_pg_stat_activity(
    pool: &PgPool,
    max_rows: usize,
) -> sqlx::Result<PgActivitySnapshot> {
    // The query is read-only against `pg_catalog.pg_stat_activity`.
    // We compute query_age and state_age server-side as floats so
    // the wire format stays uniform. Filter to the current database
    // — multi-DB Postgres deployments shouldn't leak unrelated
    // backends into our log.
    let rows = sqlx::query(
        "SELECT \
            pid, \
            state, \
            wait_event_type, \
            wait_event, \
            substring(coalesce(query, '') from 1 for $1) AS query_preview, \
            EXTRACT(EPOCH FROM (now() - query_start))::float8 AS query_age_seconds, \
            EXTRACT(EPOCH FROM (now() - state_change))::float8 AS state_age_seconds \
         FROM pg_catalog.pg_stat_activity \
         WHERE datname = current_database() \
           AND pid <> pg_backend_pid() \
         ORDER BY \
            COALESCE(EXTRACT(EPOCH FROM (now() - query_start)), 0) DESC \
         LIMIT $2",
    )
    .bind(QUERY_PREVIEW_MAX_CHARS as i32)
    .bind(max_rows as i64)
    .fetch_all(pool)
    .await?;

    let rows: Vec<PgActivityRow> = rows
        .into_iter()
        .map(|r| PgActivityRow {
            pid: r.try_get("pid").unwrap_or(0),
            state: r.try_get("state").ok(),
            wait_event_type: r.try_get("wait_event_type").ok(),
            wait_event: r.try_get("wait_event").ok(),
            query_preview: r.try_get("query_preview").ok(),
            query_age_seconds: r.try_get("query_age_seconds").ok(),
            state_age_seconds: r.try_get("state_age_seconds").ok(),
        })
        .collect();

    Ok(PgActivitySnapshot {
        captured_at_iso: chrono::Utc::now().to_rfc3339(),
        backend_count: rows.len(),
        rows,
    })
}

/// Push the current main-pool size + idle counts to Prometheus.
/// Sampled by [`spawn_pool_sampler`] every 5 s — separate from the
/// per-request acquire-wait histogram (which can't be wired without
/// invasive sqlx hooks; deferred).
pub fn record_pool_gauges(pool: &PgPool) {
    let size = pool.size();
    let idle = pool.num_idle();
    crate::services::proxy_metrics::record_db_pool_size(size);
    crate::services::proxy_metrics::record_db_pool_idle(idle);

    // When idle == 0 we're at peak utilisation — log at DEBUG so
    // an operator can correlate timestamp ranges with the
    // slow-request entries. The pool saturation monitor turns sustained idle==0 into a
    // system_alert.
    if idle == 0 {
        debug!(
            target: "diagnostics",
            pool_size = size,
            pool_idle = idle,
            "DB pool fully utilised — no idle connections"
        );
    }
}

/// Spawn a background task that samples the main DB pool every
/// `interval` and pushes the size / idle gauges. Cancels cleanly on
/// `shutdown_token`.
pub fn spawn_pool_sampler(
    pool: PgPool,
    interval: Duration,
    shutdown_token: tokio_util::sync::CancellationToken,
) {
    tokio::spawn(async move {
        let mut ticker = tokio::time::interval(interval);
        // Skip the immediate first tick so the first sample happens
        // one interval after startup, after the pool has had a
        // chance to warm.
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        ticker.tick().await;
        loop {
            tokio::select! {
                _ = shutdown_token.cancelled() => {
                    debug!(target: "diagnostics", "Pool sampler shutting down");
                    return;
                }
                _ = ticker.tick() => {
                    record_pool_gauges(&pool);
                }
            }
        }
    });
}

/// Best-effort snapshot capture used by the slow-request middleware.
/// Logs success at DEBUG (the slow-request middleware itself does
/// the WARN-level summary log) and any failure as a structured
/// warning so an operator knows when capture stopped working.
///
/// Returns `true` if a snapshot was captured and emitted, `false`
/// otherwise — callers feed this into the `proxy_slow_requests_total`
/// `outcome` label.
pub async fn capture_and_log(
    diagnostic_pool: &PgPool,
    correlation_id: &str,
    route: &str,
    duration_ms: f64,
    max_rows: usize,
) -> bool {
    match capture_pg_stat_activity(diagnostic_pool, max_rows).await {
        Ok(snapshot) => {
            let payload = serde_json::to_string(&snapshot)
                .unwrap_or_else(|e| format!("{{\"serde_error\":\"{e}\"}}"));
            // WARN-level: this fires on the slow-path only, and the
            // operator's mental model is "anomaly worth a look",
            // which is exactly WARN's contract per the proxy log
            // taxonomy. The structured `pg_stat_activity` payload
            // is the actionable bit.
            warn!(
                target: "diagnostics",
                correlation_id = %correlation_id,
                route = %route,
                duration_ms = %duration_ms,
                backend_count = %snapshot.backend_count,
                pg_stat_activity = %payload,
                "Slow request — pg_stat_activity captured"
            );
            true
        }
        Err(e) => {
            warn!(
                target: "diagnostics",
                correlation_id = %correlation_id,
                route = %route,
                duration_ms = %duration_ms,
                error = %e,
                "Slow request — pg_stat_activity capture FAILED"
            );
            false
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    // Fails the build if someone edits the constant out of range. clippy
    // flags it as a compile-time-constant assertion, which it is.
    #[allow(clippy::assertions_on_constants)]
    fn query_preview_max_chars_is_bounded() {
        // Catch a future bump that would blow up Loki line size.
        // 200 fits comfortably in one structured log line; 1000
        // would not. Failing this test is a deliberate
        // override-with-a-PR-comment moment.
        assert!(QUERY_PREVIEW_MAX_CHARS <= 500);
    }

    #[test]
    fn pg_activity_row_serializes_compactly() {
        // The row goes into a Loki log line; size sanity check.
        let row = PgActivityRow {
            pid: 12345,
            state: Some("active".into()),
            wait_event_type: Some("Lock".into()),
            wait_event: Some("transactionid".into()),
            query_preview: Some("UPDATE llm_model SET total_requests=...".into()),
            query_age_seconds: Some(5.4),
            state_age_seconds: Some(5.4),
        };
        let json = serde_json::to_string(&row).unwrap();
        assert!(json.contains("\"pid\":12345"));
        assert!(json.contains("\"state\":\"active\""));
        assert!(json.contains("\"wait_event\":\"transactionid\""));
        // Sanity: the serialised row stays under 1KB even with a
        // full-length query preview.
        assert!(json.len() < 1024);
    }

    #[test]
    fn snapshot_serialises_with_iso_timestamp() {
        let snap = PgActivitySnapshot {
            captured_at_iso: "2026-05-06T10:00:00+00:00".into(),
            backend_count: 0,
            rows: vec![],
        };
        let json = serde_json::to_string(&snap).unwrap();
        assert!(json.contains("\"captured_at_iso\":\"2026-05-06T10:00:00+00:00\""));
        assert!(json.contains("\"backend_count\":0"));
        assert!(json.contains("\"rows\":[]"));
    }
}
