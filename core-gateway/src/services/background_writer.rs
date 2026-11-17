// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Brutor Agent Control Core Gateway — Background Writer Service
//!
//! For external Operational observability systems (Prometheus, OTel collector)
//! integration see telemetry.rs + proxy_metrics.rs
//!
//! This module provides a high-performance, non-blocking telemetry and logging
//! system. It decouples the primary AI request/response path from the database
//! persistence layer.
//!
//! Writes detailed per-request records to Postgres tables (proxy_logs,
//! proxy_metrics, usage_logs).
//!
//! ## Design: bounded channels + batch consumers
//!
//! Earlier this module did `tokio::spawn` per write, and each task grabbed its
//! own pool connection to run a single-row INSERT. Under load that meant up to
//! three concurrent connection checkouts *per request* — the writer competed
//! with the live request path for the pool, and there was no backpressure: a
//! slow database let spawned tasks pile up without bound.
//!
//! Now each `write_*` call is a non-blocking `try_send` onto a bounded
//! `mpsc` channel. A single long-lived consumer task per stream drains its
//! channel, coalesces whatever is queued into a batch (up to
//! [`MAX_BATCH_ROWS`]), and persists it with ONE multi-row INSERT. Net effect:
//!
//! - **Backpressure**: the channel is bounded. proxy_metrics are *dropped*
//!   when full (observability is best-effort; a `total_dropped` counter
//!   surfaces it). proxy_logs are the audit record and feed run evidence and
//!   two enforcement seeds: a row the channel has no room for goes to
//!   a bounded overflow and on to the durable Redis spool, and a stamped
//!   batch the database cannot take right now is spooled whole and replayed
//!   with its chain sequence numbers. usage_logs are billing data: on a full
//!   channel a row goes to a bounded overflow channel whose single consumer
//!   moves it to the durable Redis spool (`usage_spool`); a batch the
//!   database cannot take right now is spooled too, and a row the database
//!   rejects is dead-lettered. The replayer writes spooled rows back. A row
//!   is lost only if the overflow channel is also full, and that is counted
//!   and logged as an error.
//! - **Connection pressure**: three consumers hold AT MOST three concurrent
//!   checkouts total (only during a flush; they park on `recv()` while idle),
//!   regardless of request rate, instead of O(requests).
//! - **Throughput**: one round-trip per batch instead of per row.
//!
//! Fault isolation is preserved: if a batched INSERT fails, the consumer falls
//! back to per-row inserts so a single poison row can't discard the whole batch
//! (critical for `usage_logs`).
//!
//! The consumers are started lazily on the first write (that's when the pool is
//! first available to this struct), so `BackgroundWriter::new()` stays
//! infallible and pool-free and every existing call site is unchanged.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};

use sqlx::{PgPool, Postgres, QueryBuilder};
use tokio::sync::mpsc;

use crate::models::db::{ProxyLogInsert, ProxyMetricInsert, UsageLogInsert};

/// Channel capacity per stream. Sized so a multi-second DB stall can be
/// absorbed at realistic request rates before we start shedding load.
const LOG_CHANNEL_CAPACITY: usize = 20_000;
const METRIC_CHANNEL_CAPACITY: usize = 20_000;
const USAGE_CHANNEL_CAPACITY: usize = 20_000;
/// Usage rows waiting to be spooled when the usage channel is full.
/// Bounded: this replaces one spawned task per row.
const USAGE_OVERFLOW_CAPACITY: usize = 50_000;
/// Audit rows waiting to be spooled when the log channel is full.
/// Smaller than the usage overflow: an audit row carries body previews.
const LOG_OVERFLOW_CAPACITY: usize = 10_000;
/// How often the replayer looks at the spool.
const SPOOL_REPLAY_INTERVAL: std::time::Duration = std::time::Duration::from_secs(5);

/// Max rows per multi-row INSERT. proxy_logs has 87 columns; Postgres caps a
/// statement at 65535 bind parameters (87 × 500 = 43_500, comfortably under).
const MAX_BATCH_ROWS: usize = 500;

/// Fire-and-forget background database writer.
///
/// Writes are enqueued onto bounded channels and persisted in batches by
/// per-stream consumer tasks. See the module docs for the rationale.
pub struct BackgroundWriter {
    stats: Arc<WriterStats>,
    log_tx: mpsc::Sender<ProxyLogInsert>,
    metric_tx: mpsc::Sender<ProxyMetricInsert>,
    usage_tx: mpsc::Sender<UsageLogInsert>,
    /// Usage rows the usage channel had no room for.
    usage_overflow_tx: mpsc::Sender<UsageLogInsert>,
    /// Audit rows the log channel had no room for.
    log_overflow_tx: mpsc::Sender<ProxyLogInsert>,
    /// Receivers, taken once when the consumers are lazily started.
    pending: Mutex<PendingReceivers>,
    started: AtomicBool,
    /// The consumer tasks, so shutdown can wait for them.
    ///
    /// Without these the runtime was simply dropped at the end of `main`,
    /// aborting all three consumers wherever they happened to be. Each channel
    /// holds up to 20 000 rows, so every rolling deploy silently discarded
    /// whatever was queued — including `usage_logs`, the billing and quota
    /// ledger this module's own docs say is NEVER dropped.
    consumers: Mutex<Vec<tokio::task::JoinHandle<()>>>,
    /// Signals the consumers to sweep their channels and flush.
    shutdown: tokio_util::sync::CancellationToken,
    /// Cloned Redis handle for the quota-refactor SHADOW counters. `None` when
    /// Redis isn't configured → shadow recording is a no-op. Held here (not via
    /// `UsageState`) so `write_llm_usage_log` can fire it through the
    /// already-threaded `&BackgroundWriter` without touching ~13 call sites.
    shadow_redis: Option<redis::aio::ConnectionManager>,
}

struct PendingReceivers {
    log_rx: Option<mpsc::Receiver<ProxyLogInsert>>,
    metric_rx: Option<mpsc::Receiver<ProxyMetricInsert>>,
    usage_rx: Option<mpsc::Receiver<UsageLogInsert>>,
    usage_overflow_rx: Option<mpsc::Receiver<UsageLogInsert>>,
    log_overflow_rx: Option<mpsc::Receiver<ProxyLogInsert>>,
}

pub struct WriterStats {
    pub total_scheduled: AtomicU64,
    pub total_completed: AtomicU64,
    pub total_failed: AtomicU64,
    /// Rows shed because a bounded channel was full (proxy_metrics; a
    /// usage_logs row only when its overflow is full too). Surfaced on the
    /// health endpoint.
    pub total_dropped: AtomicU64,
    /// proxy_logs rows moved to the durable spool: queued for want of
    /// room, or stamped and waiting for the database.
    pub audit_spooled: AtomicU64,
    /// proxy_logs rows neither written nor spooled (overflow full, or no
    /// Redis to spool to). Each is logged at ERROR.
    pub audit_lost: AtomicU64,
    /// usage_logs rows moved to the durable spool.
    pub total_spooled: AtomicU64,
    /// usage_logs rows the database rejected, kept on the dead list.
    pub total_dead_lettered: AtomicU64,
}

impl Default for BackgroundWriter {
    fn default() -> Self {
        Self::new()
    }
}

impl BackgroundWriter {
    pub fn new() -> Self {
        Self::with_log_channel_capacity(LOG_CHANNEL_CAPACITY)
    }

    /// A writer whose audit channel holds `capacity` rows. Production uses
    /// [`BackgroundWriter::new`]; a small channel lets a test reach the
    /// full-channel path without a flood.
    pub fn with_log_channel_capacity(capacity: usize) -> Self {
        let (log_tx, log_rx) = mpsc::channel(capacity.max(1));
        let (log_overflow_tx, log_overflow_rx) = mpsc::channel(LOG_OVERFLOW_CAPACITY);
        let (metric_tx, metric_rx) = mpsc::channel(METRIC_CHANNEL_CAPACITY);
        let (usage_tx, usage_rx) = mpsc::channel(USAGE_CHANNEL_CAPACITY);
        let (usage_overflow_tx, usage_overflow_rx) = mpsc::channel(USAGE_OVERFLOW_CAPACITY);
        Self {
            stats: Arc::new(WriterStats {
                total_scheduled: AtomicU64::new(0),
                total_completed: AtomicU64::new(0),
                total_failed: AtomicU64::new(0),
                total_dropped: AtomicU64::new(0),
                audit_spooled: AtomicU64::new(0),
                audit_lost: AtomicU64::new(0),
                total_spooled: AtomicU64::new(0),
                total_dead_lettered: AtomicU64::new(0),
            }),
            log_tx,
            metric_tx,
            usage_tx,
            usage_overflow_tx,
            log_overflow_tx,
            pending: Mutex::new(PendingReceivers {
                log_rx: Some(log_rx),
                metric_rx: Some(metric_rx),
                usage_rx: Some(usage_rx),
                usage_overflow_rx: Some(usage_overflow_rx),
                log_overflow_rx: Some(log_overflow_rx),
            }),
            started: AtomicBool::new(false),
            consumers: Mutex::new(Vec::new()),
            shutdown: tokio_util::sync::CancellationToken::new(),
            shadow_redis: None,
        }
    }

    /// Attach a Redis handle for shadow quota-counter recording. Builder-style
    /// so existing `BackgroundWriter::new()` callers (tests) are unaffected.
    pub fn with_shadow_redis(mut self, redis: Option<redis::aio::ConnectionManager>) -> Self {
        self.shadow_redis = redis;
        self
    }

    pub fn stats(&self) -> &Arc<WriterStats> {
        &self.stats
    }

    /// Fire-and-forget usage-counter increments for one LLM usage event. No-op
    /// without a Redis handle. Each increment is its own spawned task so the
    /// caller (the synchronous `write_*_usage_log`) never blocks.
    pub fn record_llm_shadow(
        &self,
        counters: Vec<(
            String,
            crate::services::cache::usage_state::QuotaWindow,
            i64,
        )>,
    ) {
        let Some(ref conn) = self.shadow_redis else {
            return;
        };
        for (counter_key, window, delta) in counters {
            let conn = conn.clone();
            tokio::spawn(crate::services::cache::usage_state::shadow_settle(
                conn,
                counter_key,
                window,
                delta,
            ));
        }
    }

    /// Start the three consumer tasks on first use. Idempotent and cheap on the
    /// hot path (one relaxed atomic load once started). Must be called from
    /// within a Tokio runtime — every `write_*` caller is on the request path,
    /// so that always holds.
    fn ensure_started(&self, pool: &PgPool) {
        if self.started.load(Ordering::Acquire) {
            return;
        }
        let mut pending = self.pending.lock().unwrap();
        if self.started.load(Ordering::Acquire) {
            return;
        }
        if let (
            Some(log_rx),
            Some(metric_rx),
            Some(usage_rx),
            Some(overflow_rx),
            Some(log_overflow_rx),
        ) = (
            pending.log_rx.take(),
            pending.metric_rx.take(),
            pending.usage_rx.take(),
            pending.usage_overflow_rx.take(),
            pending.log_overflow_rx.take(),
        ) {
            let mut handles = vec![
                tokio::spawn(log_consumer(
                    pool.clone(),
                    self.shutdown.clone(),
                    log_rx,
                    self.stats.clone(),
                    self.shadow_redis.clone(),
                )),
                tokio::spawn(overflow_consumer(
                    Stream::Audit,
                    self.shutdown.clone(),
                    log_overflow_rx,
                    self.log_tx.clone(),
                    self.stats.clone(),
                    self.shadow_redis.clone(),
                )),
                tokio::spawn(metric_consumer(
                    pool.clone(),
                    self.shutdown.clone(),
                    metric_rx,
                    self.stats.clone(),
                )),
                tokio::spawn(usage_consumer(
                    pool.clone(),
                    self.shutdown.clone(),
                    usage_rx,
                    self.stats.clone(),
                    self.shadow_redis.clone(),
                )),
                tokio::spawn(overflow_consumer(
                    Stream::Usage,
                    self.shutdown.clone(),
                    overflow_rx,
                    self.usage_tx.clone(),
                    self.stats.clone(),
                    self.shadow_redis.clone(),
                )),
            ];
            if let Some(redis) = self.shadow_redis.clone() {
                handles.push(tokio::spawn(usage_spool_replayer(
                    pool.clone(),
                    self.shutdown.clone(),
                    self.usage_tx.clone(),
                    self.stats.clone(),
                    redis.clone(),
                )));
                handles.push(tokio::spawn(audit_spool_replayer(
                    pool.clone(),
                    self.shutdown.clone(),
                    self.log_tx.clone(),
                    self.stats.clone(),
                    redis,
                )));
            }
            *self.consumers.lock().unwrap_or_else(|e| e.into_inner()) = handles;
        }
        self.started.store(true, Ordering::Release);
    }

    /// Flush everything queued, then wait for the consumers to finish.
    ///
    /// Call this after the HTTP server has drained and before
    /// the process exits. Dropping the senders is what lets each consumer see
    /// its channel close, write its final partial batch and return; without
    /// that the runtime just aborts them mid-batch.
    ///
    /// Bounded: a database that is already unreachable must not hold a
    /// shutdown open forever. On timeout we say how many rows were still
    /// queued, because "we lost some" is a fact an operator needs, and
    /// silently discarding them is what this exists to stop.
    pub async fn drain(&self, timeout: std::time::Duration) {
        // The senders live on `SharedState` and cannot be dropped from here,
        // so the signal is the token: each consumer sweeps its channel with
        // `try_recv` and flushes what it finds.
        self.shutdown.cancel();

        let handles: Vec<tokio::task::JoinHandle<()>> = {
            let mut guard = self.consumers.lock().unwrap_or_else(|e| e.into_inner());
            std::mem::take(&mut *guard)
        };
        if handles.is_empty() {
            return;
        }
        let joined = tokio::time::timeout(timeout, async {
            for h in handles {
                let _ = h.await;
            }
        })
        .await;
        match joined {
            Ok(()) => tracing::info!("background writer drained"),
            Err(_) => tracing::error!(
                queued_logs = self.log_tx.max_capacity() - self.log_tx.capacity(),
                queued_usage = self.usage_tx.max_capacity() - self.usage_tx.capacity(),
                "background writer did not drain within the shutdown budget; \
                 queued rows were not persisted"
            ),
        }
    }

    /// Enqueue a proxy log write. Never blocks the caller and never spawns.
    /// A row the channel has no room for goes to the bounded overflow, whose
    /// consumer spools it durably; it is lost only if the overflow is
    /// full too (counted in `audit_lost`, logged at ERROR).
    pub fn write_proxy_log(&self, pool: PgPool, mut log: ProxyLogInsert) {
        self.ensure_started(&pool);
        // No row holds an approval token, whichever surface built it.
        crate::services::proxy_logger::scrub_approval_tokens(&mut log);
        // A check that was configured but did not run is a governance
        // control that stopped working. The audit row records it; this makes
        // it *visible*, because nobody reads stderr and the row only helps
        // someone already looking. Raised here rather than in the
        // orchestrator, which is deliberately DB-free — this is the first
        // point that holds both the pool and the finished row.
        //
        // Fires before the enqueue on purpose: the row may wait in the spool
        // (or, past every bound, be lost), but the fact that a guardrail
        // silently skipped must not wait with it.
        //
        // `spawn_raise` dedupes on a deterministic key, so a config failing on
        // every request in a storm produces one alert, not thousands.
        if let Some(skipped) = log.guardrails_skipped.as_ref() {
            for alert in build_skipped_guardrail_alerts(Some(log.tenant_id.as_str()), skipped) {
                crate::services::system_alerts::spawn_raise(pool.clone(), alert);
            }
        }
        // A guardrail hit is a review finding. Raised here for
        // the same reason as the skipped-check alerts above: this is the
        // first point holding both the pool and the finished row, and the
        // upsert dedupes on (group, config, check) so a storm is one item
        // with a counter. Spawned: the request path never waits on it.
        if log.guardrail_blocked || log.guardrail_action.is_some() {
            let inbox_pool = pool.clone();
            let tenant = log.tenant_id.clone();
            let rg = log.resource_group_id.clone();
            let cfg = log.guardrail_config_id.clone();
            let check = log.guardrail_check.clone();
            let action = log.guardrail_action.clone();
            let log_id = log.id.clone();
            let surface = log.proxy_type.clone();
            tokio::spawn(async move {
                crate::services::assurance_inbox::raise_guardrail_block(
                    &inbox_pool,
                    &tenant,
                    rg.as_deref(),
                    cfg.as_deref(),
                    check.as_deref(),
                    action.as_deref(),
                    &log_id,
                    &surface,
                )
                .await;
            });
        }
        self.stats.total_scheduled.fetch_add(1, Ordering::Relaxed);
        let log = match self.log_tx.try_send(log) {
            Ok(()) => return,
            Err(mpsc::error::TrySendError::Full(log))
            | Err(mpsc::error::TrySendError::Closed(log)) => log,
        };
        if self.log_overflow_tx.try_send(log).is_err() {
            self.stats.audit_lost.fetch_add(1, Ordering::Relaxed);
            tracing::error!(
                "proxy_logs audit row LOST: writer channel and overflow both full \
                 (database and spool not keeping up)"
            );
        }
    }

    /// Enqueue a proxy metric write. Best-effort, same drop policy as logs.
    pub fn write_proxy_metric(&self, pool: PgPool, metric: ProxyMetricInsert) {
        self.ensure_started(&pool);
        self.stats.total_scheduled.fetch_add(1, Ordering::Relaxed);
        match self.metric_tx.try_send(metric) {
            Ok(()) => {}
            Err(mpsc::error::TrySendError::Full(_)) => {
                self.stats.total_dropped.fetch_add(1, Ordering::Relaxed);
                tracing::warn!(
                    "proxy_metrics writer channel full — dropping metric row (telemetry backpressure)"
                );
            }
            Err(mpsc::error::TrySendError::Closed(_)) => {
                self.stats.total_dropped.fetch_add(1, Ordering::Relaxed);
            }
        }
    }

    /// Enqueue a usage log write. Usage rows are billing/quota data:
    /// on a full channel the row goes to the bounded overflow channel, whose
    /// consumer spools it durably. Never blocks the caller and never spawns.
    pub fn write_usage_log(&self, pool: PgPool, usage: UsageLogInsert) {
        self.ensure_started(&pool);
        self.stats.total_scheduled.fetch_add(1, Ordering::Relaxed);
        let usage = match self.usage_tx.try_send(usage) {
            Ok(()) => return,
            Err(mpsc::error::TrySendError::Full(usage))
            | Err(mpsc::error::TrySendError::Closed(usage)) => usage,
        };
        if self.usage_overflow_tx.try_send(usage).is_err() {
            // 70 000 rows behind: the database and the spool are both not
            // keeping up. Bounded memory wins; the loss is counted and loud.
            self.stats.total_dropped.fetch_add(1, Ordering::Relaxed);
            tracing::error!(
                "usage_logs row LOST: the usage channel and its overflow are both full \
                 (database and spool not keeping up)"
            );
        }
    }
}

// ---------------------------------------------------------------------------
// Consumers — one long-lived task per stream.
// ---------------------------------------------------------------------------

/// Block for the first item, then opportunistically drain whatever else is
/// already queued (up to `MAX_BATCH_ROWS`) without waiting. Under load this
/// naturally coalesces the rows that accumulated while the previous flush was
/// in flight; under light load it degrades to batches of one. Returns `false`
/// when the channel is closed and drained (shutdown).
/// Build one health alert per guardrail config that skipped a check.
///
/// Pure so the grouping and message shape are testable without a pool.
///
/// Grouped by config rather than per check: an operator needs to know "this
/// guardrail is not running", and three checks failing on one broken provider
/// is one problem, not three.
pub fn build_skipped_guardrail_alerts(
    tenant_id: Option<&str>,
    skipped: &serde_json::Value,
) -> Vec<crate::services::system_alerts::SystemAlertInput> {
    let Some(entries) = skipped.as_array() else {
        return Vec::new();
    };
    // config -> (checks it skipped in first-seen order, first reason seen).
    let mut by_config: Vec<(String, Vec<String>, String)> = Vec::new();
    for e in entries {
        let config = e
            .get("config")
            .and_then(|v| v.as_str())
            .unwrap_or("unknown");
        let check = e.get("check").and_then(|v| v.as_str()).unwrap_or("unknown");
        let reason = e
            .get("reason")
            .and_then(|v| v.as_str())
            .unwrap_or("detector error");
        match by_config.iter_mut().find(|(c, _, _)| c == config) {
            Some((_, checks, _)) => {
                // Same check failing twice in one call is still one skip.
                if !checks.iter().any(|c| c == check) {
                    checks.push(check.to_string());
                }
            }
            None => by_config.push((
                config.to_string(),
                vec![check.to_string()],
                reason.to_string(),
            )),
        }
    }

    by_config
        .into_iter()
        .map(|(config, checks, reason)| {
            crate::services::system_alerts::SystemAlertInput::resource_health_change(
                tenant_id.map(str::to_string),
                "guardrail",
                &config,
                Some(config.clone()),
                // "unhealthy" (High), not "degraded" (Medium): a check that
                // is not running is not a partially-working control, and
                // traffic is passing unchecked while it stays that way.
                "unhealthy",
                format!(
                    "Guardrail '{}' did not run {} ({}) on at least one call \u{2014} the \
                     check failed and this config is set to allow on failure. \
                     Traffic passed UNCHECKED. Cause: {}",
                    config,
                    if checks.len() == 1 { "check" } else { "checks" },
                    checks.join(", "),
                    reason,
                ),
            )
            .with_title("Guardrail not running (traffic passed unchecked)")
        })
        .collect()
}

async fn collect_batch<T>(rx: &mut mpsc::Receiver<T>, batch: &mut Vec<T>) -> bool {
    match rx.recv().await {
        Some(item) => batch.push(item),
        None => return false,
    }
    while batch.len() < MAX_BATCH_ROWS {
        match rx.try_recv() {
            Ok(item) => batch.push(item),
            Err(_) => break, // empty or disconnected — flush what we have
        }
    }
    true
}

async fn log_consumer(
    pool: PgPool,
    shutdown: tokio_util::sync::CancellationToken,
    mut rx: mpsc::Receiver<ProxyLogInsert>,
    stats: Arc<WriterStats>,
    mut redis: Option<redis::aio::ConnectionManager>,
) {
    let mut batch: Vec<ProxyLogInsert> = Vec::with_capacity(MAX_BATCH_ROWS);
    loop {
        tokio::select! {
            more = collect_batch(&mut rx, &mut batch) => {
                flush_logs(&pool, &mut batch, &stats, redis.as_mut()).await;
                if !more {
                    break;
                }
            }
            // Shutdown: take everything still queued and
            // write it, rather than being aborted mid-flight with the channel
            // full. `try_recv` in a loop because the senders are still alive
            // (they live on `SharedState`), so the channel never closes on its
            // own and `recv()` would block forever here.
            _ = shutdown.cancelled() => {
                while let Ok(item) = rx.try_recv() {
                    batch.push(item);
                    if batch.len() >= MAX_BATCH_ROWS {
                        flush_logs(&pool, &mut batch, &stats, redis.as_mut()).await;
                    }
                }
                flush_logs(&pool, &mut batch, &stats, redis.as_mut()).await;
                break;
            }
        }
    }
    if !batch.is_empty() {
        flush_logs(&pool, &mut batch, &stats, redis.as_mut()).await;
    }
}

async fn metric_consumer(
    pool: PgPool,
    shutdown: tokio_util::sync::CancellationToken,
    mut rx: mpsc::Receiver<ProxyMetricInsert>,
    stats: Arc<WriterStats>,
) {
    let mut batch: Vec<ProxyMetricInsert> = Vec::with_capacity(MAX_BATCH_ROWS);
    loop {
        tokio::select! {
            more = collect_batch(&mut rx, &mut batch) => {
                flush_metrics(&pool, &mut batch, &stats).await;
                if !more {
                    break;
                }
            }
            // Shutdown: take everything still queued and
            // write it, rather than being aborted mid-flight with the channel
            // full. `try_recv` in a loop because the senders are still alive
            // (they live on `SharedState`), so the channel never closes on its
            // own and `recv()` would block forever here.
            _ = shutdown.cancelled() => {
                while let Ok(item) = rx.try_recv() {
                    batch.push(item);
                    if batch.len() >= MAX_BATCH_ROWS {
                        flush_metrics(&pool, &mut batch, &stats).await;
                    }
                }
                flush_metrics(&pool, &mut batch, &stats).await;
                break;
            }
        }
    }
    if !batch.is_empty() {
        flush_metrics(&pool, &mut batch, &stats).await;
    }
}

async fn usage_consumer(
    pool: PgPool,
    shutdown: tokio_util::sync::CancellationToken,
    mut rx: mpsc::Receiver<UsageLogInsert>,
    stats: Arc<WriterStats>,
    redis: Option<redis::aio::ConnectionManager>,
) {
    let mut redis = redis;
    let mut batch: Vec<UsageLogInsert> = Vec::with_capacity(MAX_BATCH_ROWS);
    loop {
        tokio::select! {
            more = collect_batch(&mut rx, &mut batch) => {
                flush_usage(&pool, &mut batch, &stats, redis.as_mut()).await;
                if !more {
                    break;
                }
            }
            // Shutdown: take everything still queued and
            // write it, rather than being aborted mid-flight with the channel
            // full. `try_recv` in a loop because the senders are still alive
            // (they live on `SharedState`), so the channel never closes on its
            // own and `recv()` would block forever here.
            _ = shutdown.cancelled() => {
                while let Ok(item) = rx.try_recv() {
                    batch.push(item);
                    if batch.len() >= MAX_BATCH_ROWS {
                        flush_usage(&pool, &mut batch, &stats, redis.as_mut()).await;
                    }
                }
                flush_usage(&pool, &mut batch, &stats, redis.as_mut()).await;
                break;
            }
        }
    }
    if !batch.is_empty() {
        flush_usage(&pool, &mut batch, &stats, redis.as_mut()).await;
    }
}

// ---------------------------------------------------------------------------
// Flush helpers — batch INSERT with per-row fallback on failure.
// ---------------------------------------------------------------------------

/// Persist `$batch` with one multi-row INSERT (`$insert`). On batch failure,
/// fall back to per-row inserts so a single poison row can't discard the whole
/// batch — then clear the batch. A macro (not a generic fn) because the per-row
/// fallback returns a future borrowing each row, which is awkward to express as
/// a closure bound. `$insert` must accept `(&PgPool, &[T])`.
macro_rules! flush_with_fallback {
    ($pool:expr, $batch:expr, $stats:expr, $insert:ident, $kind:literal) => {{
        let n = $batch.len() as u64;
        match $insert($pool, $batch).await {
            Ok(_) => {
                $stats.total_completed.fetch_add(n, Ordering::Relaxed);
            }
            Err(e) => {
                tracing::warn!(
                    "Background {} batch write failed ({} rows): {}; retrying per-row",
                    $kind,
                    n,
                    e
                );
                for row in $batch.iter() {
                    match $insert($pool, std::slice::from_ref(row)).await {
                        Ok(_) => {
                            $stats.total_completed.fetch_add(1, Ordering::Relaxed);
                        }
                        Err(e2) => {
                            tracing::warn!("Background {} per-row write failed: {}", $kind, e2);
                            $stats.total_failed.fetch_add(1, Ordering::Relaxed);
                        }
                    }
                }
            }
        }
        $batch.clear();
    }};
}

async fn flush_logs(
    pool: &PgPool,
    batch: &mut Vec<ProxyLogInsert>,
    stats: &WriterStats,
    redis: Option<&mut redis::aio::ConnectionManager>,
) {
    if batch.is_empty() {
        return;
    }

    // Compliance tag stamp. Done here so we don't have to
    // thread ComplianceContext through every audit-row build site.
    //
    // The per-flush memo that used to sit here is gone:
    // `load_for_audit` now owns a process-wide `(tenant, RG)` cache with the
    // same 60 s TTL as its neighbour `load_store_bodies` below. A memo
    // rebuilt for every batch meant a busy writer re-queried the same
    // handful of pairs thousands of times an hour inside the single-threaded
    // task the batching design exists to keep fast.
    for log in batch.iter_mut() {
        if log.compliance_tags.is_some() {
            continue;
        }
        let entry = match crate::services::compliance::load_for_audit(
            pool,
            &log.tenant_id,
            log.resource_group_id.as_deref(),
        )
        .await
        {
            Ok(e) => e,
            Err(e) => {
                // The row is audit data and is written anyway, untagged; the
                // gap is logged and counted per row, and the lookup is
                // retried for the next row rather than cached.
                tracing::error!(proxy_log_id = %log.id, error = %e, "compliance profile unreadable; row written without compliance tags");
                metrics::counter!("brutor_compliance_tag_gap_total").increment(1);
                continue;
            }
        };
        if let Some((profile, rg_fields)) = entry {
            let tags =
                crate::services::compliance::derive_tags_from_log_state(&profile, &rg_fields, log);
            crate::services::proxy_logger::apply_compliance_tags(log, tags);
        }
    }

    // Snapshot any explicit run-end markers before the
    // flush macro clears the batch. Collected here, acted on below: the whole
    // point of carrying the marker on the row is that the run is only closed
    // once that row is durably in `proxy_logs`, so the aggregate sees the step
    // that ended it.
    let ended: Vec<(String, String, String)> = batch
        .iter()
        .filter_map(|log| {
            let state = log.run_end_state.as_deref()?;
            let root = log.root_task_id.as_deref()?;
            Some((log.tenant_id.clone(), root.to_string(), state.to_string()))
        })
        .collect();

    // Seal content digests over the bodies as captured, before any
    // retention mode truncates, redacts or drops them. They are inside the
    // row hash stamped below, so what retention does to the preview cannot
    // touch what the row proves about the content.
    for log in batch.iter_mut() {
        crate::services::data_retention::seal_content_digests(log);
    }
    resolve_deciding_models(pool, batch).await;

    // Resolve evidence options and, for systems that salt
    // their digests, digest the bodies now: `store_bodies` below may
    // truncate or drop them, and the salted digest must commit to the same
    // captured value the content digests above do.
    let evidence = crate::services::evidence::writer::prepare(pool, batch).await;

    // Apply each tenant's `store_bodies` before anything is written.
    // One lookup per distinct tenant per flush; bodies are outside the hash
    // chain so this can run before stamping without affecting it.
    {
        let mut modes: HashMap<String, crate::services::data_retention::StoreBodies> =
            HashMap::new();
        for log in batch.iter_mut() {
            let mode = match modes.get(&log.tenant_id) {
                Some(m) => *m,
                None => {
                    let m =
                        crate::services::data_retention::load_store_bodies(pool, &log.tenant_id)
                            .await;
                    modes.insert(log.tenant_id.clone(), m);
                    m
                }
            };
            crate::services::data_retention::apply_store_bodies(log, mode);
        }
    }

    // Hash-chain the batch immediately before it is written. Stamping
    // here rather than at build time means a row shed under backpressure never
    // consumes a sequence number, so shedding cannot manufacture a gap that
    // reads as a deletion.
    let range = crate::services::audit_chain::stamp_batch(batch);

    // Mint the sealed action records now that every row carries
    // its row hash, and stamp `record_id` onto the rows before they are
    // inserted. Best-effort per tenant; a row that lands without a record
    // is the visible capture gap the compliance resolvers report.
    crate::services::evidence::writer::mint_and_store(pool, batch, Some(evidence)).await;

    land_logs(pool, std::mem::take(batch), range, ended, stats, redis).await;
}

/// Stamp each action row with the model that decided it.
///
/// "Which model chose to call this tool" is the first question an incident
/// review asks of a tool-call row, and the answer lives on a *sibling* row:
/// the LLM call whose completion carried the tool call. Resolved here so it
/// is on the row an exporter reads rather than in a join they would have to
/// know to write. Rows in this batch are consulted first (same run, same
/// turn preferred, latest not after the action); the ledger answers for runs
/// whose model call was flushed earlier. Derived, not witnessed, and
/// best-effort: a lookup failure leaves the column empty rather than holding
/// the flush. Runs BEFORE `stamp_batch`, so the value is inside the row hash
/// from format v3 — the sibling it came from may be on another
/// writer's chain or already pruned, so the row itself has to prove it.
async fn resolve_deciding_models(pool: &PgPool, batch: &mut [ProxyLogInsert]) {
    let llm_rows: Vec<(
        String,
        Option<String>,
        chrono::DateTime<chrono::Utc>,
        String,
    )> = batch
        .iter()
        .filter(|l| l.proxy_type == "llm")
        .filter_map(|l| {
            Some((
                l.root_task_id.clone()?,
                l.turn_id.clone(),
                l.timestamp,
                l.llm_model_used.clone()?,
            ))
        })
        .collect();

    let mut need: Vec<(String, String)> = Vec::new();
    for log in batch.iter_mut() {
        if log.proxy_type == "llm" || log.deciding_model.is_some() {
            continue;
        }
        let Some(root) = log.root_task_id.clone() else {
            continue;
        };
        // A candidate must not post-date the action it decided.
        //
        // `ts <= log.timestamp` used to be one term of the sort key rather
        // than a filter, so when EVERY llm row for the run came after the
        // action (a batch flushed out of order, a tool call recorded before
        // the completion that carried it) `max_by_key` still returned one:
        // the latest of them. The column then named a model call that had not
        // happened yet as the thing that decided the action. For a field an
        // incident review reads as causal, a confident wrong answer is worse
        // than an empty one.
        let best = llm_rows
            .iter()
            .filter(|(r, _, ts, _)| *r == root && *ts <= log.timestamp)
            .max_by_key(|(_, turn, ts, _)| (turn.is_some() && *turn == log.turn_id, *ts))
            .map(|(_, _, _, m)| m.clone());
        match best {
            Some(m) => log.deciding_model = Some(m),
            None => need.push((log.tenant_id.clone(), root)),
        }
    }
    if need.is_empty() {
        return;
    }
    need.sort();
    need.dedup();
    let tenants: Vec<String> = need.iter().map(|(t, _)| t.clone()).collect();
    let roots: Vec<String> = need.iter().map(|(_, r)| r.clone()).collect();
    // The ledger path needs the same "not after the action" rule,
    // and it cannot be expressed as one row per run: two actions in the same
    // run at different times have different answers. So fetch the CANDIDATES
    // bounded by the latest action that needs one, and choose per row below.
    // `LEDGER_CANDIDATE_LIMIT` bounds a pathological run rather than the
    // common case (a handful of model calls per run); ordering by timestamp
    // DESC means the rows dropped by the cap are the oldest, which are the
    // least likely to be the answer for any action in this batch.
    let latest_needed = batch
        .iter()
        .filter(|l| l.proxy_type != "llm" && l.deciding_model.is_none())
        .map(|l| l.timestamp)
        .max();
    let Some(latest_needed) = latest_needed else {
        return;
    };
    const LEDGER_CANDIDATE_LIMIT: i64 = 2_000;
    let found: Vec<(String, String, chrono::DateTime<chrono::Utc>, String)> = match sqlx::query_as(
        "SELECT tenant_id, root_task_id, timestamp, llm_model_used \
             FROM proxy_logs \
             WHERE tenant_id = ANY($1) AND root_task_id = ANY($2) \
               AND proxy_type = 'llm' AND llm_model_used IS NOT NULL \
               AND timestamp <= $3 \
             ORDER BY timestamp DESC \
             LIMIT $4",
    )
    .bind(&tenants)
    .bind(&roots)
    .bind(latest_needed)
    .bind(LEDGER_CANDIDATE_LIMIT)
    .fetch_all(pool)
    .await
    {
        Ok(rows) => rows,
        Err(e) => {
            tracing::warn!(error = %e, "deciding_model lookup failed — leaving column empty");
            return;
        }
    };
    if found.is_empty() {
        return;
    }
    for log in batch.iter_mut() {
        if log.proxy_type == "llm" || log.deciding_model.is_some() {
            continue;
        }
        let Some(root) = log.root_task_id.as_ref() else {
            continue;
        };
        let best = found
            .iter()
            .filter(|(t, r, ts, _)| t == &log.tenant_id && r == root && *ts <= log.timestamp)
            .max_by_key(|(_, _, ts, _)| *ts)
            .map(|(_, _, _, m)| m.clone());
        if let Some(m) = best {
            log.deciding_model = Some(m);
        }
    }
}

/// A stamped audit batch waiting in the spool: its rows as they were
/// to be inserted (chain sequence, row hash, digests, retention applied, any
/// stored `record_id`), the chain range a checkpoint will attest to, and the
/// run-end markers to act on once the rows land.
#[derive(serde::Serialize, serde::Deserialize)]
struct StampedBatch {
    range: Option<crate::services::audit_chain::ChainRange>,
    ended: Vec<(String, String, String)>,
    rows: Vec<ProxyLogInsert>,
}

/// Land a stamped batch: insert it, declare any row the database refused,
/// checkpoint the range, then close the runs whose end marker landed. Shared
/// by the live flush and the spool replay.
///
/// When the database is not taking writes (a transient error on the
/// whole batch), the batch is spooled whole instead: no declaration, no
/// checkpoint, nothing finalized. The replay lands it later with its original
/// sequence numbers, so the chain closes intact. The insert is idempotent
/// (`ON CONFLICT (request_id) DO NOTHING`), so a timeout that did commit is
/// harmless to replay. Without Redis, or when the spool push fails, the old
/// path runs: per-row retry, and what still fails is declared `write_failed`.
async fn land_logs(
    pool: &PgPool,
    rows: Vec<ProxyLogInsert>,
    range: Option<crate::services::audit_chain::ChainRange>,
    ended: Vec<(String, String, String)>,
    stats: &WriterStats,
    redis: Option<&mut redis::aio::ConnectionManager>,
) {
    if rows.is_empty() {
        return;
    }
    let first = insert_logs(pool, &rows).await.map(|_| ());
    let (range, ended, mut rows) = match (first.as_ref().err(), redis) {
        (Some(e), Some(r)) if crate::services::usage_spool::is_transient(e) => {
            let n = rows.len();
            let item = StampedBatch { range, ended, rows };
            match crate::services::usage_spool::push(
                r,
                crate::services::usage_spool::AUDIT_STAMPED_KEY,
                std::slice::from_ref(&item),
            )
            .await
            {
                Ok(()) => {
                    stats.audit_spooled.fetch_add(n as u64, Ordering::Relaxed);
                    tracing::warn!(rows = n, error = %e, "proxy_logs batch not written; spooled with its chain range");
                    return;
                }
                Err(pe) => {
                    tracing::error!(rows = n, error = %pe, "proxy_logs batch could not be spooled; retrying per row");
                    (item.range, item.ended, item.rows)
                }
            }
        }
        _ => (range, ended, rows),
    };

    // A row that never lands still consumed its sequence
    // number.
    //
    // Sequences are reserved and the head advanced BEFORE the insert, which is
    // what makes the chain tamper-evident: a shed row must not be able to
    // renumber the ones after it. But it also means a failed insert leaves a
    // hole, and the verifier cannot tell a hole from a deletion — so a
    // transient database blip made `verify_chain` report `gap`, i.e. "rows were
    // removed by something other than a policy", which is the one verdict an
    // auditor is meant to escalate on. Retention already solved this shape:
    // deletions are DECLARED, and a declared span reads as `pruned`. A write
    // that never happened is declared the same way, with `write_failed` as the
    // reason so it is never confused with a retention deletion. (A
    // batch the database could not take at all is spooled above instead; this
    // covers rows it refused, and the no-Redis path.)
    let stamped: Vec<(i64, String, String)> = rows
        .iter()
        .filter_map(|l| Some((l.chain_seq?, l.row_hash.clone()?, l.tenant_id.clone())))
        .collect();
    let landed = flush_logs_recording_failures(pool, &mut rows, first, stats).await;
    if let Some(range) = range.as_ref() {
        declare_unwritten(pool, range, &stamped, &landed).await;
    }

    // Checkpoint after the rows land. If the INSERT failed, the rows are not
    // there to attest to; signing first would leave a checkpoint claiming
    // evidence that does not exist, which is worse than no checkpoint.
    if let Some(range) = range {
        write_checkpoint(pool, range).await;
    }

    if !ended.is_empty() {
        finalize_marked_runs(pool, ended).await;
    }
}

/// Insert a batch, returning the chain sequences that actually landed.
///
/// Mirrors `flush_with_fallback!` (batch attempt, then per-row) but reports
/// which rows persisted, so the caller can declare the ones that did not.
async fn flush_logs_recording_failures(
    pool: &PgPool,
    batch: &mut Vec<ProxyLogInsert>,
    first: Result<(), sqlx::Error>,
    stats: &WriterStats,
) -> std::collections::HashSet<i64> {
    use std::collections::HashSet;
    let n = batch.len() as u64;
    let mut landed: HashSet<i64> = HashSet::new();
    match first {
        Ok(_) => {
            stats.total_completed.fetch_add(n, Ordering::Relaxed);
            landed.extend(batch.iter().filter_map(|l| l.chain_seq));
        }
        Err(e) => {
            tracing::warn!(
                "Background proxy_log batch write failed ({n} rows): {e}; retrying per-row"
            );
            for row in batch.iter() {
                match insert_logs(pool, std::slice::from_ref(row)).await {
                    Ok(_) => {
                        stats.total_completed.fetch_add(1, Ordering::Relaxed);
                        if let Some(seq) = row.chain_seq {
                            landed.insert(seq);
                        }
                    }
                    Err(e2) => {
                        tracing::warn!("Background proxy_log per-row write failed: {e2}");
                        stats.total_failed.fetch_add(1, Ordering::Relaxed);
                    }
                }
            }
        }
    }
    batch.clear();
    landed
}

/// Declare the sequence numbers this flush burned without writing.
///
/// Best-effort: the write that failed probably failed because the database is
/// unreachable, so this one may fail too. That is no worse than today — an
/// undeclared gap — and when the database is merely rejecting one row (a
/// constraint, an oversized field) the declaration lands and the chain still
/// verifies as `pruned` rather than `gap`.
async fn declare_unwritten(
    pool: &PgPool,
    range: &crate::services::audit_chain::ChainRange,
    stamped: &[(i64, String, String)],
    landed: &std::collections::HashSet<i64>,
) {
    let mut missing: Vec<&(i64, String, String)> = stamped
        .iter()
        .filter(|(seq, _, _)| !landed.contains(seq))
        .collect();
    if missing.is_empty() {
        return;
    }
    missing.sort_by_key(|(seq, _, _)| *seq);

    // Contiguous runs, each carrying the hash of its last row so a survivor
    // can be checked against the declared tail exactly as with a retention
    // prune.
    let mut runs: Vec<serde_json::Value> = Vec::new();
    let mut start = missing[0].0;
    let mut prev = missing[0].0;
    let mut tail = missing[0].1.clone();
    for (seq, hash, _) in missing.iter().skip(1) {
        if *seq == prev + 1 {
            prev = *seq;
            tail = hash.clone();
        } else {
            runs.push(serde_json::json!({"from": start, "to": prev, "tail_hash": tail}));
            start = *seq;
            prev = *seq;
            tail = hash.clone();
        }
    }
    runs.push(serde_json::json!({"from": start, "to": prev, "tail_hash": tail}));

    let tenant = missing[0].2.clone();
    let row_count = missing.len() as i32;
    let res = sqlx::query(
        "INSERT INTO audit_chain_prunes \
         (id, chain_id, tenant_id, seq_min, seq_max, row_count, runs, reason, actor, pruned_at) \
         VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, 'write_failed', 'background_writer', now())",
    )
    .bind(format!("prune-{}", ulid::Ulid::new()))
    .bind(&range.chain_id)
    .bind(&tenant)
    .bind(missing.first().map(|m| m.0).unwrap_or(0))
    .bind(missing.last().map(|m| m.0).unwrap_or(0))
    .bind(row_count)
    .bind(serde_json::Value::Array(runs).to_string())
    .execute(pool)
    .await;
    match res {
        Ok(_) => tracing::warn!(
            chain_id = %range.chain_id,
            rows = row_count,
            "declared {row_count} unwritten audit row(s) so the chain verifies as pruned, not gapped"
        ),
        Err(e) => tracing::error!(
            error = %e,
            chain_id = %range.chain_id,
            "could not declare unwritten audit rows; the chain will report an undeclared gap"
        ),
    }
}

/// Persist one signed checkpoint over a flushed range.
///
/// Best-effort by design. A failure here costs attestation for one batch; the
/// row hashes still chain, so modification remains detectable. Taking the
/// request path down over an audit-metadata write would trade a much larger
/// availability loss for a much smaller evidence gain.
async fn write_checkpoint(pool: &PgPool, range: crate::services::audit_chain::ChainRange) {
    let Some(cp) = crate::services::audit_chain::sign_range(&range) else {
        return; // no signing key configured — hashes still chain
    };
    let res = sqlx::query(
        "INSERT INTO audit_chain_checkpoints \
         (id, chain_id, seq_start, seq_end, head_hash, signature, public_key) \
         VALUES ($1, $2, $3, $4, $5, $6, $7)",
    )
    .bind(format!("ckpt-{}", ulid::Ulid::new()))
    .bind(&cp.range.chain_id)
    .bind(cp.range.seq_start)
    .bind(cp.range.seq_end)
    .bind(&cp.range.head_hash)
    .bind(&cp.signature)
    .bind(&cp.public_key)
    .execute(pool)
    .await;

    if let Err(e) = res {
        tracing::warn!(
            error = %e,
            chain_id = %cp.range.chain_id,
            seq_start = cp.range.seq_start,
            seq_end = cp.range.seq_end,
            "audit checkpoint write failed; rows remain hash-chained but unattested",
        );
    }
}

/// Close the runs whose final step carried an `X-Brutor-Run-End` marker.
///
/// Runs **after** the batch INSERT has committed, so `aggregate_run` counts
/// the step that asked for the close. Doing it before would produce a rollup
/// missing its own last step, which the late row would then reopen — a
/// `reopened_count` bump on every well-behaved client, i.e. a data-quality
/// signal fired by correct behaviour.
///
/// Deliberately inline rather than spawned. The module removed per-write
/// `tokio::spawn` because it let tasks pile up without bound under a slow
/// database, and re-adding a spawn here would walk back into that. The cost is
/// bounded by the *run completion* rate, not the step rate — orders of
/// magnitude lower — and this function is already on a path that does a DB
/// round trip per batch (see the compliance memo above).
///
/// Failures are WARN, not ERROR: the idle sweeper is the backstop and will
/// close the run on a later tick, still as `explicit`, because it reads the
/// same marker off the same row.
///
/// No outcome is passed: the one the client reported is on the
/// same durable row (`proxy_logs.outcome_id`) the aggregate reads, so this
/// path and the sweeper backstop judge the run on exactly the same fact.
async fn finalize_marked_runs(pool: &PgPool, ended: Vec<(String, String, String)>) {
    // One batch can carry several steps of the same run (a retry, or a client
    // that set the header more than once). Closing it once is enough, and
    // repeating it would bump `reopened_count` for no reason.
    let mut seen: HashSet<(String, String)> = HashSet::new();

    for (tenant_id, root_task_id, state) in ended {
        if !seen.insert((tenant_id.clone(), root_task_id.clone())) {
            continue;
        }
        match crate::services::run_ledger::finalize_run(
            pool,
            &tenant_id,
            &root_task_id,
            crate::services::run_ledger::finalized_by::EXPLICIT,
            Some(&state),
            None,
        )
        .await
        {
            Ok(Some(run)) => tracing::debug!(
                run_id = %run.run_id,
                root_task_id = %run.root_task_id,
                terminal_state = %run.terminal_state,
                reopened = run.reopened,
                "run finalized by client run-end marker"
            ),
            // The marked row is in `proxy_logs` — we just wrote it — so an
            // empty aggregate means the run was filtered out (e.g. already
            // fragmented past MAX_REOPENS_BEFORE_FRAGMENTED). Not an error,
            // but worth a line, since a client asked for something that did
            // not happen.
            Ok(None) => tracing::debug!(
                %root_task_id,
                "run-end marker matched no aggregable run"
            ),
            Err(e) => tracing::warn!(
                error = %e,
                %root_task_id,
                "run-end finalization failed; idle sweep will retry"
            ),
        }
    }
}

async fn flush_metrics(pool: &PgPool, batch: &mut Vec<ProxyMetricInsert>, stats: &WriterStats) {
    if batch.is_empty() {
        return;
    }
    flush_with_fallback!(pool, batch, stats, insert_metrics, "proxy_metric");
}

/// Write a usage batch. Unlike the observability streams, a usage row that
/// cannot be written is not dropped:
/// * the database is not taking writes (connection, pool, timeout): the
///   whole batch is spooled at once, without a per-row retry against a
///   database that is down;
/// * the database refused the batch: per-row inserts isolate the bad row,
///   which is dead-lettered (a transient failure mid-way spools the rest).
///
/// Without Redis (tests, a misconfigured dev box) a row that cannot be
/// written is counted as failed and logged, as before.
async fn flush_usage(
    pool: &PgPool,
    batch: &mut Vec<UsageLogInsert>,
    stats: &WriterStats,
    mut redis: Option<&mut redis::aio::ConnectionManager>,
) {
    if batch.is_empty() {
        return;
    }
    let n = batch.len() as u64;
    let err = match insert_usage(pool, batch).await {
        Ok(_) => {
            stats.total_completed.fetch_add(n, Ordering::Relaxed);
            batch.clear();
            return;
        }
        Err(e) => e,
    };
    if crate::services::usage_spool::is_transient(&err) {
        tracing::warn!(rows = n, error = %err, "usage_logs batch not written; spooling");
        spool_or_fail(
            redis.as_deref_mut(),
            Stream::Usage,
            crate::services::usage_spool::SPOOL_KEY,
            batch,
            stats,
        )
        .await;
        batch.clear();
        return;
    }
    tracing::warn!(rows = n, error = %err, "usage_logs batch refused; retrying per row");
    let rows = std::mem::take(batch);
    let mut retry_later = Vec::new();
    let mut dead = Vec::new();
    for row in rows {
        match insert_usage(pool, std::slice::from_ref(&row)).await {
            Ok(_) => {
                stats.total_completed.fetch_add(1, Ordering::Relaxed);
            }
            Err(e) if crate::services::usage_spool::is_transient(&e) => retry_later.push(row),
            Err(e) => {
                tracing::error!(error = %e, request_id = %row.request_id, "usage_logs row refused by the database");
                dead.push(row);
            }
        }
    }
    spool_or_fail(
        redis.as_deref_mut(),
        Stream::Usage,
        crate::services::usage_spool::SPOOL_KEY,
        &retry_later,
        stats,
    )
    .await;
    spool_or_fail(
        redis,
        Stream::Usage,
        crate::services::usage_spool::DEAD_KEY,
        &dead,
        stats,
    )
    .await;
}

/// The two durable streams: which counters a spool push moves.
#[derive(Clone, Copy)]
enum Stream {
    /// usage_logs.
    Usage,
    /// proxy_logs.
    Audit,
}

impl Stream {
    fn name(self) -> &'static str {
        match self {
            Stream::Usage => "usage_logs",
            Stream::Audit => "proxy_logs",
        }
    }
    fn spool_key(self) -> &'static str {
        match self {
            Stream::Usage => crate::services::usage_spool::SPOOL_KEY,
            Stream::Audit => crate::services::usage_spool::AUDIT_SPOOL_KEY,
        }
    }
    fn spooled(self, s: &WriterStats) -> &AtomicU64 {
        match self {
            Stream::Usage => &s.total_spooled,
            Stream::Audit => &s.audit_spooled,
        }
    }
    fn lost(self, s: &WriterStats) -> &AtomicU64 {
        match self {
            Stream::Usage => &s.total_failed,
            Stream::Audit => &s.audit_lost,
        }
    }
}

/// Push `rows` to `key`, counting them as spooled or dead-lettered; if that
/// fails too (or there is no Redis) they are counted as lost and logged.
async fn spool_or_fail<T: serde::Serialize>(
    redis: Option<&mut redis::aio::ConnectionManager>,
    stream: Stream,
    key: &str,
    rows: &[T],
    stats: &WriterStats,
) {
    if rows.is_empty() {
        return;
    }
    let n = rows.len() as u64;
    let pushed = match redis {
        Some(r) => crate::services::usage_spool::push(r, key, rows).await,
        None => Err("no Redis to spool to".into()),
    };
    match pushed {
        Ok(()) if key == crate::services::usage_spool::DEAD_KEY => {
            stats.total_dead_lettered.fetch_add(n, Ordering::Relaxed);
        }
        Ok(()) => {
            stream.spooled(stats).fetch_add(n, Ordering::Relaxed);
        }
        Err(e) => {
            stream.lost(stats).fetch_add(n, Ordering::Relaxed);
            tracing::error!(rows = n, error = %e, stream = stream.name(), "rows LOST: not written and not spooled");
        }
    }
}

/// Moves rows a channel had no room for to the spool (usage and audit).
/// One task, one bounded channel: memory stays bounded however long
/// the database stalls. Without Redis it waits for room in the stream's
/// channel instead (still one task).
async fn overflow_consumer<T: serde::Serialize>(
    stream: Stream,
    shutdown: tokio_util::sync::CancellationToken,
    mut rx: mpsc::Receiver<T>,
    tx: mpsc::Sender<T>,
    stats: Arc<WriterStats>,
    mut redis: Option<redis::aio::ConnectionManager>,
) {
    let mut batch: Vec<T> = Vec::with_capacity(MAX_BATCH_ROWS);
    loop {
        let more = tokio::select! {
            more = collect_batch(&mut rx, &mut batch) => more,
            _ = shutdown.cancelled() => {
                while let Ok(item) = rx.try_recv() {
                    batch.push(item);
                }
                false
            }
        };
        match redis.as_mut() {
            Some(r) => {
                spool_or_fail(Some(r), stream, stream.spool_key(), &batch, &stats).await;
            }
            None => {
                for row in batch.drain(..) {
                    if tx.send(row).await.is_err() {
                        stream.lost(&stats).fetch_add(1, Ordering::Relaxed);
                        tracing::error!(
                            stream = stream.name(),
                            "row LOST: writer stopped before it could be queued"
                        );
                    }
                }
            }
        }
        batch.clear();
        if !more || shutdown.is_cancelled() {
            break;
        }
    }
}

/// Writes spooled audit data back, from any replica (`LPOP` hands
/// each item to exactly one).
///
/// Stamped batches first: they already hold chain sequence numbers, and they
/// are the ones a still-failing database sent back. Queued rows only when no
/// stamped batch is left, and only while this replica's log channel is more
/// than half free: they go through the normal pipeline (stamped, sealed and
/// checkpointed like any new row), so replay never competes with live
/// traffic and never bypasses it.
async fn audit_spool_replayer(
    pool: PgPool,
    shutdown: tokio_util::sync::CancellationToken,
    log_tx: mpsc::Sender<ProxyLogInsert>,
    stats: Arc<WriterStats>,
    mut redis: redis::aio::ConnectionManager,
) {
    use crate::services::usage_spool::{AUDIT_DEAD_KEY, AUDIT_SPOOL_KEY, AUDIT_STAMPED_KEY};
    loop {
        tokio::select! {
            _ = shutdown.cancelled() => return,
            _ = tokio::time::sleep(SPOOL_REPLAY_INTERVAL) => {}
        }
        let mut stamped_left = false;
        while !shutdown.is_cancelled() {
            let item = match crate::services::usage_spool::pop_from::<StampedBatch>(
                &mut redis,
                AUDIT_STAMPED_KEY,
                AUDIT_DEAD_KEY,
                1,
            )
            .await
            {
                Ok(mut items) => match items.pop() {
                    Some(item) => item,
                    None => break,
                },
                Err(e) => {
                    tracing::warn!(error = %e, "audit spool unreadable; retrying later");
                    stamped_left = true;
                    break;
                }
            };
            let n = item.rows.len();
            let before = stats.audit_spooled.load(Ordering::Relaxed);
            land_logs(
                &pool,
                item.rows,
                item.range,
                item.ended,
                &stats,
                Some(&mut redis),
            )
            .await;
            if stats.audit_spooled.load(Ordering::Relaxed) > before {
                // Back on the spool: the database is still not taking writes.
                stamped_left = true;
                break;
            }
            tracing::info!(rows = n, "stamped audit batch replayed");
        }
        if stamped_left {
            continue;
        }
        while log_tx.capacity() * 2 >= log_tx.max_capacity() && !shutdown.is_cancelled() {
            let rows = match crate::services::usage_spool::pop_from::<ProxyLogInsert>(
                &mut redis,
                AUDIT_SPOOL_KEY,
                AUDIT_DEAD_KEY,
                MAX_BATCH_ROWS,
            )
            .await
            {
                Ok(rows) if rows.is_empty() => break,
                Ok(rows) => rows,
                Err(e) => {
                    tracing::warn!(error = %e, "audit spool unreadable; retrying later");
                    break;
                }
            };
            let n = rows.len();
            for row in rows {
                if log_tx.send(row).await.is_err() {
                    stats.audit_lost.fetch_add(1, Ordering::Relaxed);
                    tracing::error!("proxy_logs audit row LOST: writer stopped during replay");
                }
            }
            tracing::info!(rows = n, "queued audit rows replayed");
        }
    }
}

/// Writes spooled usage rows back, from any replica: `LPOP` hands
/// each row to exactly one. Waits while this replica's usage channel is
/// more than half full, so replay never competes with live traffic.
async fn usage_spool_replayer(
    pool: PgPool,
    shutdown: tokio_util::sync::CancellationToken,
    usage_tx: mpsc::Sender<UsageLogInsert>,
    stats: Arc<WriterStats>,
    mut redis: redis::aio::ConnectionManager,
) {
    loop {
        tokio::select! {
            _ = shutdown.cancelled() => return,
            _ = tokio::time::sleep(SPOOL_REPLAY_INTERVAL) => {}
        }
        while usage_tx.capacity() * 2 >= usage_tx.max_capacity() && !shutdown.is_cancelled() {
            let mut rows = match crate::services::usage_spool::pop(&mut redis, MAX_BATCH_ROWS).await
            {
                Ok(rows) if rows.is_empty() => break,
                Ok(rows) => rows,
                Err(e) => {
                    tracing::warn!(error = %e, "usage spool unreadable; retrying later");
                    break;
                }
            };
            let n = rows.len();
            let before = stats.total_completed.load(Ordering::Relaxed);
            flush_usage(&pool, &mut rows, &stats, Some(&mut redis)).await;
            let written = stats.total_completed.load(Ordering::Relaxed) - before;
            tracing::info!(rows = n, written, "usage spool replayed");
            if (written as usize) < n {
                // Some went back to the spool: the database is still not
                // taking writes. Try again next interval.
                break;
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Multi-row INSERT builders. Column lists and bind order mirror the original
// single-row statements exactly — `tests/integration/background_writer_chain.rs`
// guards the proxy_logs bind positions against drift.
// ---------------------------------------------------------------------------

/// The `proxy_logs` columns the writer inserts, in bind order.
///
/// A constant rather than an inline literal so the audit chain's
/// column-coverage guard (`audit_chain::tests::every_inserted_column_is_covered_or_excluded`)
/// can read the same list the INSERT uses: every column here must be in the
/// canonical field list or on the chain's explicit exclusion list, so a new
/// column cannot land outside the hash chain without a recorded decision.
pub const PROXY_LOG_INSERT_COLUMNS: &str = "
            id, tenant_id, timestamp, proxy_type, proxy_subtype,
            skill_id, skill_execution_id,
            correlation_id, parent_request_id, request_id,
            end_user_session_id,
            gateway_user_id, gateway_user_username,
            idp_user_id, idp_user_username, idp_user_email,
            client_ip, user_agent, server_id, idp_server_id,
            resource_group_id, api_key_id, api_key_name,
            http_method, endpoint, target_url,
            request_size_bytes, request_headers, request_body_preview,
            http_status_code,
            response_size_bytes, response_headers, response_body_preview,
            success, error_message, error_type, duration_ms,
            mcp_method, mcp_request_id, mcp_capability_name,
            llm_model_requested, llm_model_used, llm_provider,
            llm_prompt_tokens, llm_completion_tokens, llm_total_tokens,
            llm_streaming, llm_temperature, llm_max_tokens,
            llm_function_calling, llm_used_fallback, llm_fallback_reason,
            llm_estimated_cost_usd,
            media_images_generated, media_audio_chars,
            media_audio_seconds_input, media_video_seconds_output,
            llm_cache_hit, llm_cache_layer, llm_tokens_saved, llm_cache_similarity_score,
            prompt_cache_write_tokens, prompt_cache_read_tokens, prompt_cache_savings_usd,
            llm_context_tokens,
            rag_enabled, rag_chunks_retrieved, rag_context_tokens,
            rag_collection_ids, prompt_fingerprint, rag_top_similarity_score, rag_retrieval_duration_ms,
            oauth_flow_type, oauth_scope,
            root_task_id, delegation_depth,
            current_task_id, parent_task_id,
            trace_root_kind, trace_continuity, run_end_state, outcome_id, source,
            region_routed_to, residency_class,
            compliance_tags,
            guardrail_blocked, argument_policy_denied,
            pii_detected, is_auth_event,
            actor_agent_id, subject_id, subject_kind,
            agent_authz_decision, agent_grant_id, semantic_policy_id,
            step_id, step_name, turn_id, turn_seq,
            chain_id, chain_seq, prev_hash, row_hash, chain_version,
            argument_policy_id,
            guardrail_config_id, guardrail_check, guardrail_action,
            guardrails_skipped,
            guardrails_degraded,
            autonomy_level, autonomy_provenance,
            consumed_approval_request_id, upstream_request_id,
            pre_action_state,
            request_content_hash, response_content_hash,
            trace_id, span_id,
            server_version, tool_definition_hash, deciding_model,
            custom_metadata, tags,
            record_id, a2a_task_state,
            agent_impl_name, agent_impl_version, agent_impl_build, agent_impl_source,
            client_label
";

async fn insert_logs(
    pool: &PgPool,
    rows: &[ProxyLogInsert],
) -> Result<sqlx::postgres::PgQueryResult, sqlx::Error> {
    // AI Systems are Resource Groups; per-RG audit attribution is
    // carried by `resource_group_id` below (the separate ai_system_id column +
    // its resolver were removed).
    let mut qb: QueryBuilder<Postgres> = QueryBuilder::new(format!(
        "INSERT INTO proxy_logs ({PROXY_LOG_INSERT_COLUMNS}) "
    ));

    qb.push_values(rows.iter(), |mut b, log| {
        b.push_bind(&log.id)
            .push_bind(&log.tenant_id)
            .push_bind(log.timestamp)
            .push_bind(&log.proxy_type)
            .push_bind(&log.proxy_subtype)
            .push_bind(&log.skill_id)
            .push_bind(&log.skill_execution_id)
            .push_bind(&log.correlation_id)
            .push_bind(&log.parent_request_id)
            .push_bind(&log.request_id)
            .push_bind(&log.end_user_session_id)
            .push_bind(&log.gateway_user_id)
            .push_bind(&log.gateway_user_username)
            .push_bind(&log.idp_user_id)
            .push_bind(&log.idp_user_username)
            .push_bind(&log.idp_user_email)
            .push_bind(&log.client_ip)
            .push_bind(&log.user_agent)
            .push_bind(&log.server_id)
            .push_bind(&log.idp_server_id)
            .push_bind(&log.resource_group_id)
            .push_bind(&log.api_key_id)
            .push_bind(&log.api_key_name)
            .push_bind(&log.http_method)
            .push_bind(&log.endpoint)
            .push_bind(&log.target_url)
            .push_bind(log.request_size_bytes)
            .push_bind(&log.request_headers)
            .push_bind(&log.request_body_preview)
            .push_bind(log.http_status_code)
            .push_bind(log.response_size_bytes)
            .push_bind(&log.response_headers)
            .push_bind(&log.response_body_preview)
            .push_bind(log.success)
            .push_bind(&log.error_message)
            .push_bind(&log.error_type)
            .push_bind(log.duration_ms)
            .push_bind(&log.mcp_method)
            .push_bind(&log.mcp_request_id)
            .push_bind(&log.mcp_capability_name)
            .push_bind(&log.llm_model_requested)
            .push_bind(&log.llm_model_used)
            .push_bind(&log.llm_provider)
            .push_bind(log.llm_prompt_tokens)
            .push_bind(log.llm_completion_tokens)
            .push_bind(log.llm_total_tokens)
            .push_bind(log.llm_streaming)
            .push_bind(log.llm_temperature)
            .push_bind(log.llm_max_tokens)
            .push_bind(log.llm_function_calling)
            .push_bind(log.llm_used_fallback)
            .push_bind(&log.llm_fallback_reason)
            .push_bind(log.llm_estimated_cost_usd)
            .push_bind(log.media_images_generated)
            .push_bind(log.media_audio_chars)
            .push_bind(log.media_audio_seconds_input)
            .push_bind(log.media_video_seconds_output)
            .push_bind(log.llm_cache_hit)
            .push_bind(&log.llm_cache_layer)
            .push_bind(log.llm_tokens_saved)
            .push_bind(log.llm_cache_similarity_score)
            .push_bind(log.prompt_cache_write_tokens)
            .push_bind(log.prompt_cache_read_tokens)
            .push_bind(log.prompt_cache_savings_usd)
            .push_bind(crate::services::proxy_logger::call_context_tokens(log))
            .push_bind(log.rag_enabled)
            .push_bind(log.rag_chunks_retrieved)
            .push_bind(log.rag_context_tokens)
            .push_bind(&log.rag_collection_ids)
            .push_bind(&log.prompt_fingerprint)
            .push_bind(log.rag_top_similarity_score)
            .push_bind(log.rag_retrieval_duration_ms)
            .push_bind(&log.oauth_flow_type)
            .push_bind(&log.oauth_scope)
            .push_bind(&log.root_task_id)
            .push_bind(log.delegation_depth)
            .push_bind(&log.current_task_id)
            .push_bind(&log.parent_task_id)
            .push_bind(&log.trace_root_kind)
            .push_bind(&log.trace_continuity)
            .push_bind(&log.run_end_state)
            .push_bind(&log.outcome_id)
            .push_bind(&log.source)
            .push_bind(&log.region_routed_to)
            .push_bind(&log.residency_class)
            .push_bind(&log.compliance_tags)
            .push_bind(log.guardrail_blocked)
            .push_bind(log.argument_policy_denied)
            .push_bind(log.pii_detected)
            .push_bind(log.is_auth_event)
            .push_bind(&log.actor_agent_id)
            .push_bind(&log.subject_id)
            .push_bind(&log.subject_kind)
            .push_bind(&log.agent_authz_decision)
            .push_bind(&log.agent_grant_id)
            .push_bind(&log.semantic_policy_id)
            // Decision record — bind order mirrors the column list above;
            // `background_writer_chain.rs` guards these positions against drift.
            .push_bind(&log.step_id)
            .push_bind(&log.step_name)
            .push_bind(&log.turn_id)
            .push_bind(log.turn_seq)
            .push_bind(&log.chain_id)
            .push_bind(log.chain_seq)
            .push_bind(&log.prev_hash)
            .push_bind(&log.row_hash)
            .push_bind(&log.chain_version)
            .push_bind(&log.argument_policy_id)
            .push_bind(&log.guardrail_config_id)
            .push_bind(&log.guardrail_check)
            .push_bind(&log.guardrail_action)
            .push_bind(&log.guardrails_skipped)
            .push_bind(&log.guardrails_degraded)
            .push_bind(&log.autonomy_level)
            .push_bind(&log.autonomy_provenance)
            .push_bind(&log.consumed_approval_request_id)
            .push_bind(&log.upstream_request_id)
            .push_bind(&log.pre_action_state)
            // Content digests, trace context, tool provenance, deciding model.
            .push_bind(&log.request_content_hash)
            .push_bind(&log.response_content_hash)
            .push_bind(&log.trace_id)
            .push_bind(&log.span_id)
            .push_bind(&log.server_version)
            .push_bind(&log.tool_definition_hash)
            .push_bind(&log.deciding_model)
            .push_bind(&log.custom_metadata)
            .push_bind(&log.tags)
            // Sealed evidence record id (derived, outside the chain).
            .push_bind(&log.record_id)
            // Outbound A2A task state (derived, outside the chain).
            .push_bind(&log.a2a_task_state)
            // The caller's release + client label.
            .push_bind(&log.agent_impl_name)
            .push_bind(&log.agent_impl_version)
            .push_bind(&log.agent_impl_build)
            .push_bind(&log.agent_impl_source)
            .push_bind(&log.client_label);
    });

    qb.build().execute(pool).await
}

async fn insert_metrics(
    pool: &PgPool,
    rows: &[ProxyMetricInsert],
) -> Result<sqlx::postgres::PgQueryResult, sqlx::Error> {
    let mut qb: QueryBuilder<Postgres> = QueryBuilder::new(
        r#"INSERT INTO proxy_metrics (
            id, tenant_id, timestamp, proxy_type, request_id, correlation_id,
            server_id, http_method, endpoint, endpoint_category,
            total_duration_ms, http_status_code, success, error_type,
            request_size_bytes, response_size_bytes,
            request_headers_count, response_headers_count,
            gateway_user_id, gateway_username, end_user_type, client_ip,
            user_agent_hash, mcp_data, llm_data,
            request_hour, request_day_of_week
        ) "#,
    );

    qb.push_values(rows.iter(), |mut b, metric| {
        b.push_bind(&metric.id)
            .push_bind(&metric.tenant_id)
            .push_bind(metric.timestamp)
            .push_bind(&metric.proxy_type)
            .push_bind(&metric.request_id)
            .push_bind(&metric.correlation_id)
            .push_bind(&metric.server_id)
            .push_bind(&metric.http_method)
            .push_bind(&metric.endpoint)
            .push_bind(&metric.endpoint_category)
            .push_bind(metric.total_duration_ms)
            .push_bind(metric.http_status_code)
            .push_bind(metric.success)
            .push_bind(&metric.error_type)
            .push_bind(metric.request_size_bytes)
            .push_bind(metric.response_size_bytes)
            .push_bind(metric.request_headers_count)
            .push_bind(metric.response_headers_count)
            .push_bind(&metric.gateway_user_id)
            .push_bind(&metric.gateway_username)
            .push_bind(&metric.end_user_type)
            .push_bind(&metric.client_ip)
            .push_bind(&metric.user_agent_hash)
            .push_bind(&metric.mcp_data)
            .push_bind(&metric.llm_data)
            .push_bind(metric.request_hour)
            .push_bind(metric.request_day_of_week);
    });

    qb.build().execute(pool).await
}

async fn insert_usage(
    pool: &PgPool,
    rows: &[UsageLogInsert],
) -> Result<sqlx::postgres::PgQueryResult, sqlx::Error> {
    // AI Systems are Resource Groups; per-RG cost/usage attribution
    // is carried by `group_id` below (the separate ai_system_id column + its
    // resolver were removed).
    let mut qb: QueryBuilder<Postgres> = QueryBuilder::new(
        r#"INSERT INTO usage_logs (
            tenant_id, user_id, group_id, ancestry_path, group_name,
            resource_type, resource_id, resource_name,
            prompt_tokens, completion_tokens, total_tokens,
            model_name, provider,
            mcp_tool_name, mcp_call_count,
            mcp_execution_time_ms, mcp_data_size_bytes,
            estimated_cost, currency, prompt_cost, completion_cost,
            request_id, status_code, response_time_ms,
            http_method, endpoint, client_ip, user_agent,
            is_inherited, inherited_from_group_id, created_at
        ) "#,
    );

    qb.push_values(rows.iter(), |mut b, usage| {
        b.push_bind(&usage.tenant_id)
            .push_bind(usage.user_id)
            .push_bind(&usage.group_id)
            .push_bind(&usage.ancestry_path)
            .push_bind(&usage.group_name)
            .push_bind(&usage.resource_type)
            .push_bind(&usage.resource_id)
            .push_bind(&usage.resource_name)
            .push_bind(usage.prompt_tokens)
            .push_bind(usage.completion_tokens)
            .push_bind(usage.total_tokens)
            .push_bind(&usage.model_name)
            .push_bind(&usage.provider)
            .push_bind(&usage.mcp_tool_name)
            .push_bind(usage.mcp_call_count)
            .push_bind(usage.mcp_execution_time_ms)
            .push_bind(usage.mcp_data_size_bytes)
            .push_bind(usage.estimated_cost)
            .push_bind(&usage.currency)
            .push_bind(usage.prompt_cost)
            .push_bind(usage.completion_cost)
            .push_bind(&usage.request_id)
            .push_bind(usage.status_code)
            .push_bind(usage.response_time_ms)
            .push_bind(&usage.http_method)
            .push_bind(&usage.endpoint)
            .push_bind(&usage.client_ip)
            .push_bind(&usage.user_agent)
            .push_bind(usage.is_inherited)
            .push_bind(&usage.inherited_from_group_id)
            .push_bind(usage.created_at);
    });

    // Idempotent on request_id (matches the original single-row statement).
    // ON CONFLICT DO NOTHING tolerates duplicate request_ids within one
    // multi-row INSERT — extra dupes are silently skipped, not errored.
    qb.push(" ON CONFLICT (request_id) DO NOTHING");

    qb.build().execute(pool).await
}

#[cfg(test)]
mod skipped_guardrail_alert_tests {
    //! The fail-open alert.
    //!
    //! A guardrail with `fail_open = true` that errors lets the call through
    //! unchecked. That is the opt-in case, not the default, and
    //! it is stamped on the audit row. These cover the third half: making it
    //! visible to an operator who is not already looking at the row.
    use super::build_skipped_guardrail_alerts;
    use crate::services::system_alerts::{Severity, compute_dedup_key};
    use serde_json::json;

    fn skip(config: &str, check: &str, reason: &str) -> serde_json::Value {
        json!({"config": config, "check": check, "reason": reason})
    }

    #[test]
    fn one_skipped_check_yields_one_alert() {
        let alerts = build_skipped_guardrail_alerts(
            Some("acme"),
            &json!([skip("pii-policy", "pii", "provider timeout")]),
        );
        assert_eq!(alerts.len(), 1);
        let a = &alerts[0];
        assert_eq!(a.tenant_id.as_deref(), Some("acme"));
        assert_eq!(a.resource_type.as_deref(), Some("guardrail"));
        assert_eq!(a.resource_id.as_deref(), Some("pii-policy"));
        assert_eq!(a.title, "Guardrail not running (traffic passed unchecked)");
        assert!(a.message.contains("pii-policy"));
        assert!(a.message.contains("provider timeout"));
    }

    #[test]
    fn severity_is_high_not_medium() {
        // A control that is not running is not "partially working". If this
        // drops to Medium it stops paging and the whole alert is decorative.
        let alerts =
            build_skipped_guardrail_alerts(Some("acme"), &json!([skip("c", "pii", "boom")]));
        assert!(matches!(alerts[0].severity, Severity::High));
    }

    #[test]
    fn message_names_the_traffic_consequence() {
        // The operator-facing point is not "a detector errored" (routine) but
        // "calls went out ungoverned" (not routine). Keep it in the text.
        let alerts =
            build_skipped_guardrail_alerts(Some("acme"), &json!([skip("c", "pii", "boom")]));
        assert!(
            alerts[0].message.contains("UNCHECKED"),
            "message must state that traffic passed unchecked: {}",
            alerts[0].message
        );
    }

    #[test]
    fn checks_from_one_config_group_into_a_single_alert() {
        // Three checks failing on one broken provider is one problem.
        let alerts = build_skipped_guardrail_alerts(
            Some("acme"),
            &json!([
                skip("strict", "pii", "provider down"),
                skip("strict", "toxicity", "provider down"),
                skip("strict", "prompt_injection", "provider down"),
            ]),
        );
        assert_eq!(alerts.len(), 1);
        let m = &alerts[0].message;
        assert!(m.contains("checks"), "plural form expected: {m}");
        for check in ["pii", "toxicity", "prompt_injection"] {
            assert!(m.contains(check), "{check} missing from {m}");
        }
    }

    #[test]
    fn distinct_configs_get_distinct_alerts_and_dedup_keys() {
        // The dedup key is (tenant, category, source, error_code) — NOT
        // resource_id. `resource_health_change` folds the id into `source`,
        // which is what keeps two broken configs from collapsing into one
        // alert and hiding the second outage.
        let alerts = build_skipped_guardrail_alerts(
            Some("acme"),
            &json!([
                skip("pii-policy", "pii", "x"),
                skip("tox-policy", "toxicity", "y")
            ]),
        );
        assert_eq!(alerts.len(), 2);
        let keys: Vec<String> = alerts
            .iter()
            .map(|a| {
                compute_dedup_key(
                    a.tenant_id.as_deref(),
                    a.category,
                    &a.source,
                    a.error_code.as_deref(),
                )
            })
            .collect();
        assert_ne!(keys[0], keys[1], "configs must not share a dedup key");
    }

    #[test]
    fn repeat_of_same_config_is_dedup_stable() {
        // A storm must bump occurrence_count on one row, not create thousands.
        // That depends on the key being identical across calls.
        let key_for = |reason: &str| {
            let alerts =
                build_skipped_guardrail_alerts(Some("acme"), &json!([skip("c", "pii", reason)]));
            compute_dedup_key(
                alerts[0].tenant_id.as_deref(),
                alerts[0].category,
                &alerts[0].source,
                alerts[0].error_code.as_deref(),
            )
        };
        // Even when the underlying error text differs between calls.
        assert_eq!(key_for("timeout after 5s"), key_for("connection reset"));
    }

    #[test]
    fn same_check_twice_in_one_call_is_not_listed_twice() {
        let alerts = build_skipped_guardrail_alerts(
            Some("acme"),
            &json!([skip("c", "pii", "boom"), skip("c", "pii", "boom")]),
        );
        assert_eq!(alerts.len(), 1);
        assert!(
            alerts[0].message.contains("(pii)") && alerts[0].message.contains("check ("),
            "singular, de-duplicated: {}",
            alerts[0].message
        );
    }

    #[test]
    fn null_and_empty_produce_no_alerts() {
        // The overwhelming majority of rows. Must be free and silent.
        assert!(build_skipped_guardrail_alerts(Some("acme"), &json!([])).is_empty());
        assert!(build_skipped_guardrail_alerts(Some("acme"), &json!(null)).is_empty());
        assert!(
            build_skipped_guardrail_alerts(Some("acme"), &json!({"not": "an array"})).is_empty()
        );
    }

    #[test]
    fn malformed_entry_still_alerts_rather_than_vanishing() {
        // A shape we did not expect is not a reason to drop the signal that a
        // guardrail stopped running — degrade to "unknown" and still page.
        let alerts = build_skipped_guardrail_alerts(Some("acme"), &json!([{"junk": 1}]));
        assert_eq!(alerts.len(), 1);
        assert_eq!(alerts[0].resource_id.as_deref(), Some("unknown"));
    }

    #[test]
    fn missing_tenant_is_carried_as_none_not_invented() {
        let alerts = build_skipped_guardrail_alerts(None, &json!([skip("c", "pii", "boom")]));
        assert!(alerts[0].tenant_id.is_none());
    }
}
