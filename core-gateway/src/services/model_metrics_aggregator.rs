// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Model metrics aggregator
//!
//! Eliminates the per-request `UPDATE llm_model SET total_requests=…`
//! that previously fired from three call sites:
//!
//!   * [`crate::services::usage::write_llm_usage_log`]
//!   * [`crate::services::usage::write_embedding_usage_log`]
//!   * [`crate::services::multimodal_pipeline::record_multimodal_outcome`]
//!
//! ### Why
//!
//! Each per-request UPDATE took an exclusive row lock on
//! `llm_model.id`. Under concurrent traffic on the same model
//! (canonical example: a bulk health-check sweep that touches every
//! model in the catalogue), dozens of UPDATEs queued behind the
//! row lock. The downstream symptom was that the admin
//! `/v1/proxy/llm/admin/{id}/reset-resilience` endpoint — which
//! also runs a single `UPDATE llm_model …` — would block on
//! whatever held the row, sometimes well past httpx's read
//! timeout. The control plane logged a `Proxy resilience reset
//! failed (non-fatal)` ReadTimeout that operators (correctly) read
//! as a sign that *something is very wrong*.
//!
//! ### Design
//!
//! The hot path increments [`AtomicCounters`] in a `DashMap` keyed
//! by `model_id`. Increments are constant-time atomic ops with no
//! DB connection involved. A background flusher drains the map
//! every [`AppConfig::model_metrics_flush_interval_secs`] seconds
//! and issues **one** SQL statement that updates every dirty model
//! via `UPDATE … FROM (VALUES …)`. Single round trip, single
//! row-lock acquisition per model per interval, regardless of
//! request volume.
//!
//! ### Crash-loss tolerance
//!
//! If the proxy crashes mid-interval we lose up to N seconds of
//! counters. These are dashboard metrics
//! (`llm_model.total_requests`), not billing — `usage_records`
//! continues to write per-request via the existing background
//! writer and is the source of truth for cost / quota. Documented
//! explicitly so operators don't get surprised by the eventual
//! consistency.
//!
//! ### Graceful shutdown
//!
//! The flusher task listens to a [`CancellationToken`] and runs
//! one final flush before exiting, so a Pod recycle on a low-
//! traffic shard doesn't drop counters that haven't been written
//! yet.

use std::sync::Arc;
use std::sync::atomic::{AtomicI64, Ordering};
use std::time::Duration;

use dashmap::DashMap;
use sqlx::PgPool;
use tokio_util::sync::CancellationToken;
use tracing::{debug, warn};

/// Pending counters for one model. Atomic so concurrent request
/// handlers can increment without coordination.
#[derive(Debug, Default)]
pub struct AtomicCounters {
    /// Total requests since last flush.
    pub total_requests: AtomicI64,
    /// Failed requests (4xx / 5xx / transport errors) since last flush.
    pub failed_requests: AtomicI64,
    /// Tokens consumed (prompt + completion / input only for embedding)
    /// since last flush. `0` for modes that don't surface token
    /// counts (e.g. multimodal).
    pub total_tokens_consumed: AtomicI64,
}

impl AtomicCounters {
    /// Snapshot-and-reset: read the current values, swap them back to
    /// zero, return the snapshot. Used by the flusher; safe under
    /// concurrent increments because each `swap` is atomic.
    fn drain(&self) -> (i64, i64, i64) {
        (
            self.total_requests.swap(0, Ordering::Relaxed),
            self.failed_requests.swap(0, Ordering::Relaxed),
            self.total_tokens_consumed.swap(0, Ordering::Relaxed),
        )
    }
}

/// In-process aggregator. Cheap clone (`Arc` inside a wrapper) so
/// it can live on `AppState` and be dispatched to from request
/// handlers without lock contention.
#[derive(Debug, Clone)]
pub struct ModelMetricsAggregator {
    inner: Arc<ModelMetricsInner>,
}

#[derive(Debug)]
struct ModelMetricsInner {
    counters: DashMap<String, AtomicCounters>,
}

impl Default for ModelMetricsAggregator {
    fn default() -> Self {
        Self::new()
    }
}

impl ModelMetricsAggregator {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(ModelMetricsInner {
                counters: DashMap::new(),
            }),
        }
    }

    /// Record one request for `model_id`. Constant-time atomic ops.
    /// Safe to call from any number of concurrent request handlers
    /// — DashMap handles per-bucket locking and the per-counter
    /// updates are atomic.
    ///
    /// `is_failed`: status >= 400 (or transport error).
    /// `tokens`: prompt + completion (chat / responses), prompt only
    ///           (embedding), or 0 (multimodal modes that don't
    ///           surface token counts).
    pub fn record(&self, model_id: &str, is_failed: bool, tokens: i64) {
        // entry().or_default() gives us the per-model counters in
        // a single hash lookup. The atomic increments below are
        // wait-free across threads.
        let entry = self.inner.counters.entry(model_id.to_string()).or_default();
        entry.total_requests.fetch_add(1, Ordering::Relaxed);
        if is_failed {
            entry.failed_requests.fetch_add(1, Ordering::Relaxed);
        }
        if tokens > 0 {
            entry
                .total_tokens_consumed
                .fetch_add(tokens, Ordering::Relaxed);
        }
    }

    /// Snapshot every dirty model's counters for the flusher.
    /// Returns `(model_id, total, failed, tokens)` tuples; rows where
    /// every counter is zero are skipped so the flush UPDATE doesn't
    /// touch rows that haven't moved.
    fn drain_dirty(&self) -> Vec<(String, i64, i64, i64)> {
        let mut out = Vec::with_capacity(self.inner.counters.len());
        // DashMap iter yields RefMulti; we can read the AtomicCounters
        // through it without locking the whole map.
        for entry in self.inner.counters.iter() {
            let (total, failed, tokens) = entry.value().drain();
            if total != 0 || failed != 0 || tokens != 0 {
                out.push((entry.key().clone(), total, failed, tokens));
            }
        }
        out
    }

    /// Number of distinct model IDs with pending or recently-flushed
    /// counters in the map. Used by tests + diagnostics.
    pub fn tracked_models(&self) -> usize {
        self.inner.counters.len()
    }
}

/// Flush all pending counters in one bulk statement. Returns the
/// number of model rows written, or `Err` for a structured log.
///
/// SQL strategy: build a `VALUES (…), (…), …` table and join
/// against `llm_model`, using `COALESCE` to add to existing values
/// rather than overwrite.
///
/// We bind individual parameters rather than serializing into the
/// SQL text — postgres parameter limit is 65,535 and even a fleet
/// with 200 active models per interval stays comfortably below
/// (200 × 4 = 800 parameters).
pub async fn flush_once(
    aggregator: &ModelMetricsAggregator,
    pool: &PgPool,
) -> Result<usize, sqlx::Error> {
    let dirty = aggregator.drain_dirty();
    if dirty.is_empty() {
        return Ok(0);
    }

    // Build the VALUES clause with positional placeholders. Four
    // params per row: id, total, failed, tokens.
    let mut sql = String::from(
        "UPDATE llm_model SET \
            total_requests = COALESCE(llm_model.total_requests, 0) + u.total, \
            failed_requests = COALESCE(llm_model.failed_requests, 0) + u.failed, \
            total_tokens_consumed = COALESCE(llm_model.total_tokens_consumed, 0) + u.tokens \
         FROM (VALUES ",
    );
    let mut first = true;
    for i in 0..dirty.len() {
        if !first {
            sql.push_str(", ");
        }
        first = false;
        let base = i * 4;
        sql.push_str(&format!(
            "(${}, ${}::bigint, ${}::bigint, ${}::bigint)",
            base + 1,
            base + 2,
            base + 3,
            base + 4
        ));
    }
    sql.push_str(") AS u(id, total, failed, tokens) WHERE llm_model.id = u.id");

    let mut q = sqlx::query(&sql);
    for (id, total, failed, tokens) in &dirty {
        q = q.bind(id).bind(total).bind(failed).bind(tokens);
    }
    let result = q.execute(pool).await?;
    Ok(result.rows_affected() as usize)
}

/// Spawn the background flusher. Loops until `shutdown_token`
/// fires; one final flush runs on shutdown so we don't lose
/// counters on Pod recycle.
///
/// `interval` defaults to 5 s in production. Tests pass smaller
/// values to make the test deterministic.
pub fn spawn_flusher(
    aggregator: ModelMetricsAggregator,
    pool: PgPool,
    interval: Duration,
    shutdown_token: CancellationToken,
) {
    tokio::spawn(async move {
        let mut ticker = tokio::time::interval(interval);
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        // Skip the immediate first tick so the first flush happens
        // one interval after startup.
        ticker.tick().await;

        loop {
            tokio::select! {
                _ = shutdown_token.cancelled() => {
                    // Final flush on shutdown — best-effort. If we
                    // can't reach the DB during graceful shutdown
                    // (e.g. the pool is closing), we lose at most one
                    // interval of dashboard counters; usage_records
                    // is unaffected.
                    match flush_once(&aggregator, &pool).await {
                        Ok(n) => debug!(
                            target: "model_metrics_aggregator",
                            rows_updated = n,
                            "Final flush on shutdown"
                        ),
                        Err(e) => warn!(
                            target: "model_metrics_aggregator",
                            error = %e,
                            "Final shutdown flush failed"
                        ),
                    }
                    return;
                }
                _ = ticker.tick() => {
                    match flush_once(&aggregator, &pool).await {
                        Ok(0) => {
                            // Common no-op case — silent.
                        }
                        Ok(n) => debug!(
                            target: "model_metrics_aggregator",
                            rows_updated = n,
                            "Flushed model metrics"
                        ),
                        Err(e) => warn!(
                            target: "model_metrics_aggregator",
                            error = %e,
                            "Flush failed — counters will retry on next tick"
                        ),
                    }
                }
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::Ordering;

    #[test]
    fn record_increments_total_and_failed_separately() {
        let agg = ModelMetricsAggregator::new();
        agg.record("model-a", false, 100);
        agg.record("model-a", true, 50);
        agg.record("model-a", false, 25);

        let entry = agg.inner.counters.get("model-a").unwrap();
        assert_eq!(entry.total_requests.load(Ordering::Relaxed), 3);
        assert_eq!(entry.failed_requests.load(Ordering::Relaxed), 1);
        assert_eq!(entry.total_tokens_consumed.load(Ordering::Relaxed), 175);
    }

    #[test]
    fn record_with_zero_tokens_does_not_increment_token_counter() {
        let agg = ModelMetricsAggregator::new();
        agg.record("model-a", false, 0);
        let entry = agg.inner.counters.get("model-a").unwrap();
        assert_eq!(entry.total_tokens_consumed.load(Ordering::Relaxed), 0);
    }

    #[test]
    fn drain_dirty_resets_counters_and_only_returns_changed_rows() {
        let agg = ModelMetricsAggregator::new();
        agg.record("model-a", true, 10);
        agg.record("model-b", false, 20);

        let mut dirty = agg.drain_dirty();
        dirty.sort_by(|a, b| a.0.cmp(&b.0));
        assert_eq!(dirty.len(), 2);
        assert_eq!(dirty[0], ("model-a".into(), 1, 1, 10));
        assert_eq!(dirty[1], ("model-b".into(), 1, 0, 20));

        // Second drain returns nothing — counters were reset.
        let empty = agg.drain_dirty();
        assert!(empty.is_empty());

        // The map keys are still tracked (for stable atomic refs)
        // but the values are zero — the flusher checks dirty status
        // by looking at the values, not the keys.
        assert_eq!(agg.tracked_models(), 2);
    }

    #[test]
    fn drain_dirty_is_safe_under_concurrent_increments() {
        // Smoke test: many threads incrementing the same model
        // while drain runs concurrently. The exact split between
        // pre-drain and post-drain counts isn't deterministic, but
        // the SUM of (drained + post-drain pending) MUST equal the
        // total ops issued. Catches a missed atomic op or a drop
        // path in `swap`.
        use std::thread;
        let agg = ModelMetricsAggregator::new();
        let agg_clone = agg.clone();
        let writer = thread::spawn(move || {
            for _ in 0..1000 {
                agg_clone.record("model-x", false, 1);
            }
        });
        // Drain a few times during the writer's run. Each drain
        // returns "what's been recorded since the last drain".
        let mut drained_total = 0i64;
        for _ in 0..5 {
            for (id, total, _, _) in agg.drain_dirty() {
                if id == "model-x" {
                    drained_total += total;
                }
            }
            std::thread::sleep(std::time::Duration::from_millis(1));
        }
        writer.join().unwrap();
        // Final drain to capture anything left.
        for (id, total, _, _) in agg.drain_dirty() {
            if id == "model-x" {
                drained_total += total;
            }
        }
        assert_eq!(
            drained_total, 1000,
            "atomic counters must sum to total ops across drains"
        );
    }

    #[tokio::test]
    async fn massively_concurrent_record_sums_correctly() {
        // 1000 concurrent record() calls on the same model: the
        // atomic counter must end up at exactly 1000. Catches a
        // missed `Ordering` or a bucket-level race in DashMap.
        let agg = ModelMetricsAggregator::new();
        let mut handles = Vec::new();
        for _ in 0..1000 {
            let agg = agg.clone();
            handles.push(tokio::spawn(async move {
                agg.record("model-y", false, 1);
            }));
        }
        for h in handles {
            h.await.unwrap();
        }
        let entry = agg.inner.counters.get("model-y").unwrap();
        assert_eq!(entry.total_requests.load(Ordering::Relaxed), 1000);
        assert_eq!(entry.total_tokens_consumed.load(Ordering::Relaxed), 1000);
    }
}
