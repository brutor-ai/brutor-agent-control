// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Per-tick beacon writer for the Background Tasks page.
//!
//! Each gateway-core scheduler (batch_submitter, batch_poller,
//! system_alerts_resolver) calls [`write_beacon`] at the top of its
//! tick. The control plane's Background Tasks page reads
//! `scheduler_runtime_state.last_beacon_at` and renders the state
//! badge as "running" when the stamp is within the staleness
//! threshold.
//!
//! # Why this lives in gateway-core
//!
//! The Python side has its own implementation in
//! `app/services/system_ops/schedulers.py::write_beacon`. The
//! semantics are identical (UPSERT with `last_beacon_at = NOW()` on
//! conflict) — duplicating the helper avoids cross-repo runtime
//! dependencies for what is one INSERT.
//!
//! # Best-effort posture
//!
//! A beacon failure must NEVER kill the scheduler loop. Callers wrap
//! the call in their own tick-level error handler; this function
//! returns `Result` so callers can log + continue.

use sqlx::PgPool;

/// UPSERT this scheduler's `last_beacon_at` to `NOW()`.
///
/// The conflict target is `scheduler_name` (the table's primary key).
/// On a fresh scheduler that has no row, this inserts one with
/// `paused = false`. On any existing row it overwrites only the
/// beacon column — the pause flags + audit metadata are untouched.
///
/// `scheduler_name` MUST match the wire constant the Python
/// `KNOWN_SCHEDULERS` set declares — a typo silently never appears
/// on the operator page. The compile-time wire constants in
/// `KNOWN_SCHEDULERS_RUST` below mirror the Python set and should be
/// the only source of these strings inside the proxy.
pub async fn write_beacon(db: &PgPool, scheduler_name: &str) -> Result<(), sqlx::Error> {
    sqlx::query(
        r#"
        INSERT INTO scheduler_runtime_state
            (scheduler_name, paused, last_beacon_at, updated_at)
        VALUES ($1, FALSE, NOW(), NOW())
        ON CONFLICT (scheduler_name) DO UPDATE
            SET last_beacon_at = NOW(),
                updated_at     = NOW()
        "#,
    )
    .bind(scheduler_name)
    .execute(db)
    .await?;
    Ok(())
}

/// Stable wire constants for the schedulers running inside gateway-core.
///
/// Kept here next to the writer so a new scheduler's author has to
/// touch exactly one file to wire the beacon — add the constant,
/// pass it to `write_beacon` in their tick loop, done. Match the
/// Python `KNOWN_SCHEDULERS` set in
/// `app/database/models/scheduler_runtime_state.py`.
pub mod schedulers {
    pub const BATCH_SUBMITTER: &str = "batch_submitter";
    pub const BATCH_POLLER: &str = "batch_poller";
    pub const SYSTEM_ALERTS_RESOLVER: &str = "system_alerts_resolver";
    /// Closes runs that went quiet without an end signal.
    pub const RUN_LEDGER_SWEEPER: &str = "run_ledger_sweeper";
    /// The only detector that fires on the ABSENCE of
    /// traffic, so its beacon is the only evidence it is alive at all.
    pub const AI_SYSTEM_LIVENESS: &str = "ai_system_liveness";
    /// Baselines + drift detection.
    pub const AI_SYSTEM_DRIFT: &str = "ai_system_drift";
    /// Executes replay runs the control plane requests.
    pub const AI_SYSTEM_REPLAY: &str = "ai_system_replay";
}
