// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Evidence-layer metrics: "`record_lag` exported; shedding
//! is a `capture_gap` finding, never silent."

use metrics::{counter, histogram};

/// Why a governed row landed without a `record_id`.
pub mod gap_reason {
    /// No evidence signing key is configured on this deployment.
    pub const DISABLED: &str = "disabled";
    /// The record could not be built (a builder invariant refused it).
    pub const BUILD_FAILED: &str = "build_failed";
    /// The tenant's slice of the batch failed to persist.
    pub const SEAL_FAILED: &str = "seal_failed";
    /// A two-phase action whose `planned` record could not be sealed (or
    /// was not sealed in time), so the outcome carries no `confirms` chain.
    pub const PLANNED_UNCONFIRMED: &str = "planned_unconfirmed";
    /// A lookup the record depends on (attribution, contract, profile
    /// options, tool annotations, notice) failed: the row is left unsealed
    /// for backfill rather than sealed with a guessed value.
    pub const LOOKUP_FAILED: &str = "lookup_failed";
}

/// `brutor_evidence_record_lag_seconds` — seal time minus row timestamp.
pub fn record_lag(surface: &str, seconds: f64) {
    histogram!(
        "brutor_evidence_record_lag_seconds",
        "surface" => surface.to_string(),
    )
    .record(seconds.max(0.0));
}

/// `brutor_evidence_capture_gap_total{reason}`.
pub fn capture_gap(reason: &'static str, n: u64) {
    if n == 0 {
        return;
    }
    counter!("brutor_evidence_capture_gap_total", "reason" => reason).increment(n);
}
