// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Pool saturation monitor
//!
//! Periodically samples the main DB pool and raises a
//! `system_alerts.downstream_provider`-style alert when sustained
//! saturation crosses an operator-configurable threshold. Pairs with
//! the `pool_sampler` (which only pushes Prometheus gauges) to
//! turn pool starvation into a paged on-call signal *before*
//! requests start failing.
//!
//! ### Detection
//!
//! Saturation = `pool.num_idle() == 0` AND the proxy is taking
//! (slow-acquire-style) requests. The acquire-wait histogram lives
//! in `services::diagnostics`; this module reads its snapshotted state via a small
//! ring buffer.
//!
//! Sustained = the saturation condition has held for at least
//! [`AppConfig::pool_saturation_sustained_secs`] (default 60 s).
//! Prevents one-shot spikes from page-storming.
//!
//! ### Dedup
//!
//! Uses the existing [`crate::services::system_alerts::spawn_raise`]
//! channel which dedups by `error_code`. We use the stable code
//! `pool_saturation_sustained` so repeated firings collapse into one
//! row that the on-call sees, with a count.

use std::collections::VecDeque;
use std::time::{Duration, Instant};

use sqlx::PgPool;
use tokio_util::sync::CancellationToken;
use tracing::{debug, info, warn};

use crate::services::system_alerts::{SystemAlertInput, spawn_raise};

/// State carried across sampler ticks. Kept minimal — a sliding
/// window of the last N idle counts, plus the timestamp of the
/// most recent transition into "saturated".
struct SaturationState {
    /// Window of recent idle counts (most recent first). Length
    /// equal to `sustained_secs / sample_interval_secs`. When every
    /// entry is 0 the pool has been at zero idle for the full
    /// window.
    window: VecDeque<u32>,
    /// Maximum window size — derived from sustained / interval.
    window_max: usize,
    /// Last firing timestamp; used to throttle alert emission so
    /// continuous saturation produces one alert per dedup window
    /// instead of one per tick.
    last_alerted: Option<Instant>,
}

impl SaturationState {
    fn new(window_max: usize) -> Self {
        Self {
            window: VecDeque::with_capacity(window_max.max(1)),
            window_max: window_max.max(1),
            last_alerted: None,
        }
    }

    /// Push a new idle count and return whether the entire window
    /// is now zero.
    fn record_and_check(&mut self, idle: u32) -> bool {
        self.window.push_front(idle);
        while self.window.len() > self.window_max {
            self.window.pop_back();
        }
        self.window.len() == self.window_max && self.window.iter().all(|&v| v == 0)
    }

    /// Should we emit an alert now? Throttles to one per dedup
    /// window to match the system_alerts dedup semantics.
    fn should_alert(&mut self, now: Instant, dedup_window: Duration) -> bool {
        match self.last_alerted {
            Some(t) if now.duration_since(t) < dedup_window => false,
            _ => {
                self.last_alerted = Some(now);
                true
            }
        }
    }
}

/// Spawn the saturation monitor. Samples every `sample_interval`,
/// uses `sustained_window` to determine when to fire. Cancels
/// cleanly on shutdown.
///
/// The default dedup window is 5 minutes — repeated firings within
/// that window roll up into one row in `system_alerts` with a
/// `last_seen_at` bump, matching how the rest of the alert pipeline
/// behaves.
pub fn spawn(
    pool: PgPool,
    db: PgPool,
    sample_interval: Duration,
    sustained_window: Duration,
    shutdown_token: CancellationToken,
) {
    let window_max = std::cmp::max(
        1,
        (sustained_window.as_secs_f64() / sample_interval.as_secs_f64().max(0.1)).ceil() as usize,
    );
    info!(
        target: "pool_saturation_monitor",
        sample_interval_secs = sample_interval.as_secs_f64(),
        sustained_secs = sustained_window.as_secs_f64(),
        window_max = window_max,
        "Pool saturation monitor starting"
    );

    tokio::spawn(async move {
        let mut state = SaturationState::new(window_max);
        let mut ticker = tokio::time::interval(sample_interval);
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        // Skip the first immediate tick — the pool needs a moment
        // to warm before we start judging it.
        ticker.tick().await;

        let dedup_window = Duration::from_secs(5 * 60);

        loop {
            tokio::select! {
                _ = shutdown_token.cancelled() => {
                    debug!(target: "pool_saturation_monitor", "shutdown");
                    return;
                }
                _ = ticker.tick() => {
                    let idle = pool.num_idle() as u32;
                    let saturated = state.record_and_check(idle);
                    if saturated && state.should_alert(Instant::now(), dedup_window) {
                        let total = pool.size();
                        warn!(
                            target: "pool_saturation_monitor",
                            pool_size = total,
                            sustained_for = ?sustained_window,
                            "DB pool sustained saturation — raising system alert"
                        );
                        // Tenant=None: this is a proxy-wide alert,
                        // not tenant-scoped. The dedup key encodes
                        // the resource so reruns coalesce.
                        spawn_raise(
                            db.clone(),
                            SystemAlertInput::downstream_provider(
                                None,
                                "db_pool",
                                Some("db_pool".to_string()),
                                Some("main".to_string()),
                                Some("pool_saturation_sustained".to_string()),
                                format!(
                                    "DB pool has been at zero idle for at least \
                                     {} seconds (size={total}). Requests are \
                                     waiting on `acquire` and may start \
                                     timing out. Investigate via the \
                                     slow-request panel and/or scale the \
                                     gateway's BRUTOR_GATEWAY_DB_POOL_SIZE.",
                                    sustained_window.as_secs(),
                                ),
                            ),
                        );
                    }
                }
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fresh_state_doesnt_fire_immediately() {
        let mut s = SaturationState::new(3);
        // First two samples can't fill the window.
        assert!(!s.record_and_check(0));
        assert!(!s.record_and_check(0));
        // Third sample — window full, all zero → fire.
        assert!(s.record_and_check(0));
    }

    #[test]
    fn nonzero_sample_resets_saturation() {
        let mut s = SaturationState::new(3);
        s.record_and_check(0);
        s.record_and_check(0);
        s.record_and_check(0);
        // One non-zero observation should clear the saturation
        // signal — operator briefly recovered.
        assert!(!s.record_and_check(2));
        // Then back to zero — needs to refill.
        assert!(!s.record_and_check(0));
        assert!(!s.record_and_check(0));
        assert!(s.record_and_check(0));
    }

    #[test]
    fn window_size_one_fires_immediately_on_zero() {
        let mut s = SaturationState::new(1);
        assert!(s.record_and_check(0));
    }

    #[test]
    fn dedup_window_throttles_alerts() {
        let mut s = SaturationState::new(1);
        let t0 = Instant::now();
        // First trigger fires.
        assert!(s.should_alert(t0, Duration::from_secs(60)));
        // Second within window — silenced.
        assert!(!s.should_alert(t0 + Duration::from_secs(10), Duration::from_secs(60)));
        // Past window — fires again.
        assert!(s.should_alert(t0 + Duration::from_secs(120), Duration::from_secs(60)));
    }
}
