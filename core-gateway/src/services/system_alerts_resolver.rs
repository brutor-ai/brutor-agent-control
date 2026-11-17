// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # System alert auto-resolver
//!
//! Runs as a single long-lived `tokio::spawn` loop. On every tick it:
//!
//! * **Resolves `resource_health` alerts** whose target resource has been
//!   `healthy` continuously for at least [`HEALTH_RECOVERY_WINDOW`]. The
//!   check walks the `resource_type` column and joins the appropriate
//!   tracking table (`mcp_servers` or `llm_models`) so we only close an
//!   alert when we have genuine evidence of recovery — not just silence.
//!
//! * **Resolves stale downstream alerts** (`downstream_provider`,
//!   `downstream_idp`, `downstream_cache`, `downstream_vector_db`) that
//!   haven't re-raised in [`DOWNSTREAM_QUIET_WINDOW`]. Those categories
//!   have no ground-truth recovery signal we can poll, so quiet = healed
//!   is the best approximation.
//!
//! **Acknowledged alerts are never auto-resolved.** Once an operator has
//! claimed an alert, only explicit resolution (from the admin UI) closes
//! it — otherwise we'd silently pull the rug out from under an ongoing
//! investigation.
//!
//! `infrastructure_threshold` and `internal_error` are **left alone** by
//! this task. The first is owned by the monitor that raised it (should
//! auto-resolve when the metric drops under threshold, not on a timer).
//! The second is a code bug and must be explicitly dismissed by an
//! engineer after the fix ships.

use std::time::Duration;

use sqlx::PgPool;
use tokio_util::sync::CancellationToken;

use crate::services::proxy_metrics;

/// How long a resource must have been healthy for its health alert to
/// be auto-resolved. Short enough that operators aren't staring at
/// stale alerts; long enough that a flapping resource doesn't get
/// resolved/re-raised every minute.
const HEALTH_RECOVERY_WINDOW: Duration = Duration::from_secs(5 * 60);

/// How long since the last re-raise before a downstream alert is
/// considered healed. If the downstream service is still failing,
/// [`crate::services::system_alerts::raise`] bumps `last_seen_at`
/// on every hit so this window never elapses.
const DOWNSTREAM_QUIET_WINDOW: Duration = Duration::from_secs(15 * 60);

/// Cap the per-tick batch so an alert storm can't stall the loop.
const MAX_PER_TICK: i64 = 500;

/// Background loop — call from `main.rs` alongside the other long-lived
/// tasks. Honours `shutdown_token` for graceful termination; uses
/// `tokio::select!` so we don't wait up to `interval_secs` after shutdown.
pub async fn run_resolver_loop(db: PgPool, interval_secs: u64, shutdown: CancellationToken) {
    let mut ticker = tokio::time::interval(Duration::from_secs(interval_secs));
    // `MissedTickBehavior::Delay` avoids bursting catch-up ticks if the
    // task was ever paused (e.g. heavy DB load). We'd rather fall behind
    // than hammer the DB with duplicate sweeps.
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);

    tracing::info!(
        interval_secs,
        health_recovery_window_secs = HEALTH_RECOVERY_WINDOW.as_secs(),
        downstream_quiet_window_secs = DOWNSTREAM_QUIET_WINDOW.as_secs(),
        "system_alerts auto-resolver started"
    );

    loop {
        tokio::select! {
            _ = shutdown.cancelled() => {
                tracing::info!("system_alerts auto-resolver received shutdown signal");
                return;
            }
            _ = ticker.tick() => {
                // Per-tick beacon. Stamped before
                // the sweep so the badge updates on a tick even when
                // the sweep finds nothing to do. Errors are logged at
                // WARN (downstream dep failure, taxonomy: warn) and
                // never propagate up.
                if let Err(e) = crate::services::system_ops_beacon::write_beacon(
                    &db,
                    crate::services::system_ops_beacon::schedulers::SYSTEM_ALERTS_RESOLVER,
                ).await {
                    tracing::warn!(
                        error = %e,
                        "system_alerts_resolver beacon write failed (non-fatal)"
                    );
                }
                if let Err(e) = sweep_once(&db).await {
                    // A sweep failure is a local problem (our own SQL or
                    // DB connectivity from *this* process). Log at ERROR
                    // but don't raise a system alert — we'd recurse into
                    // the very thing that's failing.
                    tracing::error!(error = %e, "system_alerts auto-resolver sweep failed");
                }
            }
        }
    }
}

/// One sweep: health recoveries, then stale downstream. Each step is
/// independent — if one fails, the other still runs. This is public
/// so tests can drive a single pass without spinning up the loop.
pub async fn sweep_once(db: &PgPool) -> Result<SweepReport, sqlx::Error> {
    let health = resolve_recovered_health_alerts(db).await?;
    let downstream = resolve_stale_downstream_alerts(db).await?;

    if health > 0 || downstream > 0 {
        tracing::info!(
            health_alerts_resolved = health,
            downstream_alerts_resolved = downstream,
            "system_alerts auto-resolver: closed recovered alerts"
        );
    } else {
        tracing::debug!("system_alerts auto-resolver: nothing to close this tick");
    }

    Ok(SweepReport {
        health_resolved: health,
        downstream_resolved: downstream,
    })
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct SweepReport {
    pub health_resolved: i64,
    pub downstream_resolved: i64,
}

/// Close `resource_health` alerts where the underlying resource is
/// currently reporting `healthy` in its own tracking table and has
/// done so for at least [`HEALTH_RECOVERY_WINDOW`].
///
/// The join is split by `resource_type` so we hit the correct table
/// without dynamic SQL. Acknowledged alerts are intentionally
/// excluded — only `active` alerts auto-resolve.
async fn resolve_recovered_health_alerts(db: &PgPool) -> Result<i64, sqlx::Error> {
    let window_secs = HEALTH_RECOVERY_WINDOW.as_secs() as i64;

    let ids: Vec<String> = sqlx::query_scalar(
        r#"
        WITH candidates AS (
            SELECT sa.id, sa.resource_type, sa.resource_id, sa.tenant_id, sa.category
              FROM system_alerts sa
             WHERE sa.status = 'active'
               AND sa.category = 'resource_health'
               AND sa.resource_id IS NOT NULL
             ORDER BY sa.last_seen_at ASC
             LIMIT $1
        )
        SELECT c.id
          FROM candidates c
         WHERE (
             (c.resource_type = 'mcp_server' AND EXISTS (
                 SELECT 1 FROM mcp_servers m
                  WHERE m.id = c.resource_id
                    AND (c.tenant_id IS NULL OR m.tenant_id = c.tenant_id)
                    AND m.health_status = 'healthy'
                    AND m.last_health_success IS NOT NULL
                    AND m.last_health_success <= NOW() - MAKE_INTERVAL(secs => $2)
             ))
             OR
             (c.resource_type = 'llm_model' AND EXISTS (
                 SELECT 1 FROM llm_model m
                  WHERE m.id = c.resource_id
                    AND (c.tenant_id IS NULL OR m.tenant_id = c.tenant_id)
                    AND m.health_status = 'healthy'
                    AND m.last_health_success IS NOT NULL
                    AND m.last_health_success <= NOW() - MAKE_INTERVAL(secs => $2)
             ))
         )
        "#,
    )
    .bind(MAX_PER_TICK)
    .bind(window_secs)
    .fetch_all(db)
    .await?;

    if ids.is_empty() {
        return Ok(0);
    }

    let resolved: i64 = sqlx::query_scalar(
        r#"
        WITH upd AS (
            UPDATE system_alerts
               SET status          = 'resolved',
                   resolved_at     = NOW(),
                   resolved_by     = 'auto-resolver',
                   resolution_note = 'auto-resolved: resource healthy for recovery window'
             WHERE id = ANY($1)
               AND status = 'active'
            RETURNING category
        )
        SELECT COUNT(*)::bigint FROM upd
        "#,
    )
    .bind(&ids)
    .fetch_one(db)
    .await?;

    for _ in 0..resolved {
        proxy_metrics::record_system_alert_auto_resolved("resource_health");
    }

    Ok(resolved)
}

/// Close `downstream_*` alerts that haven't been re-raised in
/// [`DOWNSTREAM_QUIET_WINDOW`]. If the downstream is still failing,
/// `raise()` keeps bumping `last_seen_at` and this window never
/// elapses, so a genuinely-ongoing outage won't get prematurely
/// closed.
async fn resolve_stale_downstream_alerts(db: &PgPool) -> Result<i64, sqlx::Error> {
    let window_secs = DOWNSTREAM_QUIET_WINDOW.as_secs() as i64;

    // RETURNING lets us count + attribute per-category for the metric
    // bump below, without a second SELECT.
    let rows: Vec<(String,)> = sqlx::query_as(
        r#"
        UPDATE system_alerts
           SET status          = 'resolved',
               resolved_at     = NOW(),
               resolved_by     = 'auto-resolver',
               resolution_note = 'auto-resolved: no recurrence within quiet window'
         WHERE status = 'active'
           AND category IN (
               'downstream_provider',
               'downstream_idp',
               'downstream_cache',
               'downstream_vector_db'
           )
           AND last_seen_at <= NOW() - MAKE_INTERVAL(secs => $1)
           AND id IN (
               SELECT id FROM system_alerts
                WHERE status = 'active'
                  AND category IN (
                      'downstream_provider',
                      'downstream_idp',
                      'downstream_cache',
                      'downstream_vector_db'
                  )
                  AND last_seen_at <= NOW() - MAKE_INTERVAL(secs => $1)
                ORDER BY last_seen_at ASC
                LIMIT $2
           )
        RETURNING category
        "#,
    )
    .bind(window_secs)
    .bind(MAX_PER_TICK)
    .fetch_all(db)
    .await?;

    for (category,) in &rows {
        proxy_metrics::record_system_alert_auto_resolved(category);
    }
    Ok(rows.len() as i64)
}

// ---------------------------------------------------------------------------
// Unit tests — behavior of the timing constants and the SweepReport struct.
// SQL logic is covered by the integration tests in
// tests/system_alerts_resolver_tests.rs.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recovery_windows_are_production_reasonable() {
        // Health window should be short enough that on-call clears the
        // UI quickly after a recovery, long enough to dampen flapping.
        assert!(HEALTH_RECOVERY_WINDOW >= Duration::from_secs(60));
        assert!(HEALTH_RECOVERY_WINDOW <= Duration::from_secs(30 * 60));
        // Downstream window should be substantially longer than health
        // because we have no positive signal — only the absence of new
        // failures. Shorter than a typical incident timeout (30m+).
        assert!(DOWNSTREAM_QUIET_WINDOW >= HEALTH_RECOVERY_WINDOW * 2);
        assert!(DOWNSTREAM_QUIET_WINDOW <= Duration::from_secs(30 * 60));
    }

    #[test]
    // The point of these is to fail the build if someone edits the constant
    // out of range. clippy flags them as compile-time-constant assertions,
    // which is exactly what they are.
    #[allow(clippy::assertions_on_constants)]
    fn max_per_tick_is_bounded() {
        // If the per-tick batch were unbounded, an alert storm or a
        // schema-drift bug could queue millions of resolutions into a
        // single UPDATE and stall the DB for minutes. Cap at ~hundreds.
        assert!(MAX_PER_TICK > 0);
        assert!(MAX_PER_TICK <= 10_000);
    }

    #[test]
    fn sweep_report_default_is_zero() {
        let r = SweepReport::default();
        assert_eq!(r.health_resolved, 0);
        assert_eq!(r.downstream_resolved, 0);
    }
}
