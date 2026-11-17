// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Shadow replay.
//!
//! Re-runs recorded production requests under a candidate contract and
//! diffs the resulting trajectory against the recorded one.
//!
//! ## The safety property
//!
//! **Replay must never move money.** Every case carries a
//! `side_effect_policy` defaulting to `mock`, and replay-produced runs are
//! written with `is_replay = true` so they can never reach a baseline,
//! a liveness window or a drift comparison. A simulation that taught the
//! detector a shape which never happened would produce drift alerts about
//! a system that behaved perfectly normally.
//!
//! ## Module layout
//!
//! * [`diff`] — comparing an observed trajectory against a recorded one.
//!   Pure, so the calibration decisions are arguable against a table of
//!   cases rather than only observable by running a replay.
//! * [`executor`] — re-deciding a recorded run's steps under the candidate
//!   contract. **Dispatches nothing**; see its docs for why authorization
//!   replay is both the safer and the more useful question than trajectory
//!   replay.
//! * [`runner`] — the suite-level orchestration that writes a replay run.

pub mod diff;
pub mod executor;
pub mod runner;

/// Wire contract for `ai_system_replay_cases.side_effect_policy`, mirrored
/// in `app/database/models/ai_system_replay.py`.
pub mod side_effect {
    pub const MOCK: &str = "mock";
    pub const SANDBOX: &str = "sandbox";
    pub const SKIP: &str = "skip_case";

    pub const ALL: [&str; 3] = [MOCK, SANDBOX, SKIP];
}

/// Wire contract for `ai_system_replay_runs.status`.
pub mod status {
    pub const RUNNING: &str = "running";
    pub const COMPLETED: &str = "completed";
    pub const FAILED: &str = "failed";
    pub const CANCELLED: &str = "cancelled";
}

/// Wire contract for `ai_system_replay_runs.triggered_by`.
pub mod trigger {
    pub const MANUAL: &str = "manual";
    pub const PROMOTION_GATE: &str = "promotion_gate";
    pub const DRIFT_RESPONSE: &str = "drift_response";
    pub const SCHEDULED: &str = "scheduled";
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_default_side_effect_policy_is_the_safe_one() {
        // Not a constant with a default here on purpose — the *database*
        // owns the default so a row inserted by any path gets it. This test
        // pins the vocabulary the two sides agree on.
        let mut all = side_effect::ALL.to_vec();
        all.sort_unstable();
        assert_eq!(all, vec!["mock", "sandbox", "skip_case"]);
    }
}
