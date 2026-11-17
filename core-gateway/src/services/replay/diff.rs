// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Diffing a replayed run against its recorded baseline.
//!
//! ## What a verdict means
//!
//! The classification is deliberately coarse, and the ordering is the
//! point:
//!
//! * `matched` — same outcome, same shape.
//! * `shape_changed` — same outcome, different route. **This passes.**
//! * `policy_changed` — a governance decision differs: something is now
//!   blocked that was not, or vice versa.
//! * `outcome_changed` — the run ended differently.
//! * `failed` — the replay itself could not run.
//!
//! `shape_changed` passing is the single most important calibration
//! decision here. An agent that reached the same answer in seven steps
//! instead of five has not regressed; failing it would make every prompt
//! tweak look like a breakage, and a gate that cries wolf on every deploy
//! is a gate somebody disables inside a month.
//!
//! ## What this is not
//!
//! It diffs **behaviour, not truth**. Nothing here knows whether the new
//! answer was better — the proxy has no ground truth. A changed trajectory
//! is a prompt to look, never a verdict, and it is never presented as
//! a correctness eval.
//!
//! ## Why policy changes outrank shape changes
//!
//! A replay whose guardrail verdict flipped is reporting that the
//! *candidate contract* governs this traffic differently — which is
//! precisely the question the promotion gate was asked. It ranks above a
//! shape change even when the run completed either way, because
//! "completed, but the PII check no longer fires" is not a pass.

use serde::{Deserialize, Serialize};

/// Wire contract — mirrored in
/// `app/database/models/ai_system_replay.py`.
pub mod verdict {
    pub const MATCHED: &str = "matched";
    pub const SHAPE_CHANGED: &str = "shape_changed";
    pub const POLICY_CHANGED: &str = "policy_changed";
    pub const OUTCOME_CHANGED: &str = "outcome_changed";
    pub const FAILED: &str = "failed";
    pub const SKIPPED: &str = "skipped";

    pub const ALL: [&str; 6] = [
        MATCHED,
        SHAPE_CHANGED,
        POLICY_CHANGED,
        OUTCOME_CHANGED,
        FAILED,
        SKIPPED,
    ];
}

/// How far a step count may move before it counts as a shape change.
///
/// Proportional, not absolute: one extra step on a three-step run is a
/// different event from one extra step on a forty-step run, and an
/// absolute threshold would either flag every short run or miss every
/// long one.
pub const STEP_TOLERANCE_FRACTION: f64 = 0.25;

/// How far cost may move before it is worth reporting. Replays hit live
/// providers whose token accounting is not bit-identical between calls, so
/// a tight bound would report a cost change on every single case.
pub const COST_TOLERANCE_FRACTION: f64 = 0.20;

/// One side of the comparison.
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct Trajectory {
    pub terminal_state: Option<String>,
    pub action_count: Option<i32>,
    /// Membership, not order — an agent that calls the same two tools in
    /// the other order has not changed what it can reach.
    pub tool_set_signature: Option<String>,
    pub total_cost_usd: Option<f64>,
    /// Governance outcomes. A change in any of these is a policy change.
    pub guardrail_block_count: Option<i32>,
    pub policy_denial_count: Option<i32>,
    pub approval_request_count: Option<i32>,
}

/// The verdict plus the human-readable reasons behind it.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CaseDiff {
    pub verdict: &'static str,
    /// Plain sentences, in the operator's terms. A diff that reports
    /// `action_count: 5 -> 14` and stops has handed the investigation back.
    pub reasons: Vec<String>,
    pub recorded: Trajectory,
    pub observed: Trajectory,
}

impl CaseDiff {
    pub fn passes(&self) -> bool {
        matches!(self.verdict, verdict::MATCHED | verdict::SHAPE_CHANGED)
    }
}

/// A replay that could not be executed at all.
pub fn failed(recorded: Trajectory, reason: impl Into<String>) -> CaseDiff {
    CaseDiff {
        verdict: verdict::FAILED,
        reasons: vec![reason.into()],
        recorded,
        observed: Trajectory::default(),
    }
}

/// A case deliberately not run.
pub fn skipped(recorded: Trajectory, reason: impl Into<String>) -> CaseDiff {
    CaseDiff {
        verdict: verdict::SKIPPED,
        reasons: vec![reason.into()],
        recorded,
        observed: Trajectory::default(),
    }
}

fn moved_proportionally(before: f64, after: f64, tolerance: f64) -> bool {
    if before == 0.0 {
        return after != 0.0;
    }
    ((after - before) / before).abs() > tolerance
}

/// Compare an observed trajectory against the recorded one.
pub fn compare(recorded: &Trajectory, observed: &Trajectory) -> CaseDiff {
    let mut policy_reasons: Vec<String> = Vec::new();
    let mut shape_reasons: Vec<String> = Vec::new();

    // --- outcome: the strongest signal, checked first -------------------
    let outcome_changed = match (&recorded.terminal_state, &observed.terminal_state) {
        (Some(a), Some(b)) => a != b,
        // A recorded outcome with none observed is a failure to reproduce,
        // not a match. Treating "unknown" as agreement would let a replay
        // that produced nothing count as a pass.
        (Some(_), None) => true,
        _ => false,
    };

    // --- governance -----------------------------------------------------
    for (label, a, b) in [
        (
            "guardrail blocks",
            recorded.guardrail_block_count,
            observed.guardrail_block_count,
        ),
        (
            "policy denials",
            recorded.policy_denial_count,
            observed.policy_denial_count,
        ),
        (
            "approval requests",
            recorded.approval_request_count,
            observed.approval_request_count,
        ),
    ] {
        if let (Some(a), Some(b)) = (a, b) {
            if a != b {
                let direction = if b > a { "more" } else { "fewer" };
                policy_reasons.push(format!(
                    "{direction} {label} under the candidate contract ({a} → {b})"
                ));
            }
        }
    }

    // --- shape ----------------------------------------------------------
    if let (Some(a), Some(b)) = (recorded.action_count, observed.action_count) {
        if moved_proportionally(a as f64, b as f64, STEP_TOLERANCE_FRACTION) {
            shape_reasons.push(format!("took {b} steps instead of {a}"));
        }
    }
    match (&recorded.tool_set_signature, &observed.tool_set_signature) {
        (Some(a), Some(b)) if a != b => {
            shape_reasons.push("used a different set of tools".to_string());
        }
        _ => {}
    }
    if let (Some(a), Some(b)) = (recorded.total_cost_usd, observed.total_cost_usd) {
        if moved_proportionally(a, b, COST_TOLERANCE_FRACTION) {
            let direction = if b > a { "more" } else { "less" };
            shape_reasons.push(format!("cost {direction}: ${a:.4} → ${b:.4}"));
        }
    }

    let (verdict, reasons) = if outcome_changed {
        let mut reasons = vec![format!(
            "ended as {} instead of {}",
            observed.terminal_state.as_deref().unwrap_or("nothing"),
            recorded.terminal_state.as_deref().unwrap_or("unknown"),
        )];
        reasons.extend(policy_reasons);
        reasons.extend(shape_reasons);
        (verdict::OUTCOME_CHANGED, reasons)
    } else if !policy_reasons.is_empty() {
        // Ranked above a shape change even when the run completed either
        // way: "completed, but the PII check no longer fires" is not a pass.
        let mut reasons = policy_reasons;
        reasons.extend(shape_reasons);
        (verdict::POLICY_CHANGED, reasons)
    } else if !shape_reasons.is_empty() {
        (verdict::SHAPE_CHANGED, shape_reasons)
    } else {
        (verdict::MATCHED, Vec::new())
    };

    CaseDiff {
        verdict,
        reasons,
        recorded: recorded.clone(),
        observed: observed.clone(),
    }
}

/// Roll per-case verdicts into the counters a replay run stores.
///
/// Mirrors `summarize()` in `app/database/models/ai_system_replay.py`; the
/// Python side owns the gate arithmetic and this owns the execution-side
/// tally, so both are asserted against the same numbers from their own
/// test suites.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct Summary {
    pub total_cases: usize,
    pub matched: usize,
    pub shape_changed: usize,
    pub policy_changed: usize,
    pub outcome_changed: usize,
    pub failed: usize,
    pub skipped: usize,
    /// `None` when nothing was scored — a confident 100% derived from an
    /// entirely skipped suite would mislead.
    pub pass_rate: Option<f64>,
    pub critical_outcome_changed: usize,
}

pub fn summarize(results: &[(CaseDiff, bool)]) -> Summary {
    let mut s = Summary {
        total_cases: results.len(),
        ..Default::default()
    };

    for (diff, is_critical) in results {
        match diff.verdict {
            verdict::MATCHED => s.matched += 1,
            verdict::SHAPE_CHANGED => s.shape_changed += 1,
            verdict::POLICY_CHANGED => s.policy_changed += 1,
            verdict::OUTCOME_CHANGED => {
                s.outcome_changed += 1;
                if *is_critical {
                    s.critical_outcome_changed += 1;
                }
            }
            verdict::FAILED => s.failed += 1,
            verdict::SKIPPED => s.skipped += 1,
            _ => {}
        }
    }

    let scored = s.total_cases - s.skipped;
    s.pass_rate = if scored > 0 {
        Some((s.matched + s.shape_changed) as f64 / scored as f64)
    } else {
        None
    };
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    fn t(state: &str, steps: i32, tools: &str) -> Trajectory {
        Trajectory {
            terminal_state: Some(state.into()),
            action_count: Some(steps),
            tool_set_signature: Some(tools.into()),
            total_cost_usd: Some(0.01),
            guardrail_block_count: Some(0),
            policy_denial_count: Some(0),
            approval_request_count: Some(0),
        }
    }

    // -- the calibration decision ----------------------------------------

    #[test]
    fn an_identical_run_matches() {
        let a = t("completed", 5, "search|fetch");
        assert_eq!(compare(&a, &a).verdict, verdict::MATCHED);
    }

    #[test]
    fn a_different_route_to_the_same_answer_passes() {
        // The most important calibration here. An agent that took seven
        // steps instead of five has not regressed, and failing it would make
        // every prompt tweak look like a breakage.
        let d = compare(
            &t("completed", 5, "search|fetch"),
            &t("completed", 9, "search|fetch"),
        );
        assert_eq!(d.verdict, verdict::SHAPE_CHANGED);
        assert!(d.passes());
    }

    #[test]
    fn small_action_wobble_is_not_even_a_shape_change() {
        // 5 → 6 is within tolerance; flagging it would make the gate noise.
        let d = compare(&t("completed", 20, "s"), &t("completed", 22, "s"));
        assert_eq!(d.verdict, verdict::MATCHED);
    }

    #[test]
    fn the_action_tolerance_is_proportional_not_absolute() {
        // One extra step on a 3-step run is a different event from one extra
        // step on a 40-step run.
        assert_eq!(
            compare(&t("completed", 3, "s"), &t("completed", 5, "s")).verdict,
            verdict::SHAPE_CHANGED
        );
        assert_eq!(
            compare(&t("completed", 40, "s"), &t("completed", 42, "s")).verdict,
            verdict::MATCHED
        );
    }

    // -- outcome ----------------------------------------------------------

    #[test]
    fn a_different_terminal_state_is_an_outcome_change() {
        let d = compare(&t("completed", 5, "s"), &t("errored", 5, "s"));
        assert_eq!(d.verdict, verdict::OUTCOME_CHANGED);
        assert!(!d.passes());
        assert!(d.reasons[0].contains("errored"));
    }

    #[test]
    fn a_missing_observed_outcome_fails_rather_than_matching() {
        // Treating "unknown" as agreement would let a replay that produced
        // nothing count as a pass — the quietest possible way to ship a
        // broken gate.
        let recorded = t("completed", 5, "s");
        let mut observed = t("completed", 5, "s");
        observed.terminal_state = None;
        assert_eq!(
            compare(&recorded, &observed).verdict,
            verdict::OUTCOME_CHANGED
        );
    }

    #[test]
    fn outcome_beats_everything_else() {
        let mut observed = t("errored", 50, "different");
        observed.guardrail_block_count = Some(3);
        let d = compare(&t("completed", 5, "s"), &observed);
        assert_eq!(d.verdict, verdict::OUTCOME_CHANGED);
        // ...but the other differences still travel with it.
        assert!(d.reasons.len() > 1);
    }

    // -- governance -------------------------------------------------------

    #[test]
    fn a_guardrail_that_stopped_firing_is_a_policy_change() {
        // "Completed, but the PII check no longer fires" is not a pass.
        let mut recorded = t("completed", 5, "s");
        recorded.guardrail_block_count = Some(1);
        let d = compare(&recorded, &t("completed", 5, "s"));
        assert_eq!(d.verdict, verdict::POLICY_CHANGED);
        assert!(!d.passes());
        assert!(d.reasons[0].contains("fewer guardrail blocks"));
    }

    #[test]
    fn a_new_denial_is_a_policy_change() {
        let mut observed = t("completed", 5, "s");
        observed.policy_denial_count = Some(2);
        let d = compare(&t("completed", 5, "s"), &observed);
        assert_eq!(d.verdict, verdict::POLICY_CHANGED);
        assert!(d.reasons[0].contains("more policy denials"));
    }

    #[test]
    fn a_new_approval_requirement_is_a_policy_change() {
        // This is what tightening autonomy looks like from replay's side.
        let mut observed = t("completed", 5, "s");
        observed.approval_request_count = Some(1);
        assert_eq!(
            compare(&t("completed", 5, "s"), &observed).verdict,
            verdict::POLICY_CHANGED
        );
    }

    #[test]
    fn policy_changes_outrank_shape_changes() {
        let mut observed = t("completed", 40, "different");
        observed.guardrail_block_count = Some(5);
        assert_eq!(
            compare(&t("completed", 5, "s"), &observed).verdict,
            verdict::POLICY_CHANGED
        );
    }

    // -- tools and cost ---------------------------------------------------

    #[test]
    fn a_different_tool_set_is_a_shape_change() {
        assert_eq!(
            compare(
                &t("completed", 5, "search"),
                &t("completed", 5, "search|transfer")
            )
            .verdict,
            verdict::SHAPE_CHANGED
        );
    }

    #[test]
    fn a_large_cost_move_is_reported() {
        let mut observed = t("completed", 5, "s");
        observed.total_cost_usd = Some(0.05);
        let d = compare(&t("completed", 5, "s"), &observed);
        assert_eq!(d.verdict, verdict::SHAPE_CHANGED);
        assert!(d.reasons.iter().any(|r| r.contains("cost more")));
    }

    #[test]
    fn ordinary_token_accounting_noise_is_not_a_finding() {
        // Replays hit live providers whose accounting is not bit-identical.
        // A tight bound would report a cost change on every single case.
        let mut observed = t("completed", 5, "s");
        observed.total_cost_usd = Some(0.011);
        assert_eq!(
            compare(&t("completed", 5, "s"), &observed).verdict,
            verdict::MATCHED
        );
    }

    #[test]
    fn an_absent_field_on_either_side_is_not_a_difference() {
        // Half the recorded facts are optional; treating absence as change
        // would make every case with a sparse record a false finding.
        let recorded = Trajectory {
            terminal_state: Some("completed".into()),
            ..Default::default()
        };
        let observed = Trajectory {
            terminal_state: Some("completed".into()),
            action_count: Some(9),
            ..Default::default()
        };
        assert_eq!(compare(&recorded, &observed).verdict, verdict::MATCHED);
    }

    // -- reasons ----------------------------------------------------------

    #[test]
    fn reasons_are_sentences_not_field_dumps() {
        // A diff that reports `action_count: 5 -> 14` and stops has handed the
        // investigation back to the person reading it.
        let d = compare(&t("completed", 5, "s"), &t("completed", 14, "s"));
        assert!(d.reasons[0].contains("steps instead of"));
        assert!(!d.reasons[0].contains("action_count:"));
    }

    #[test]
    fn a_matched_case_has_nothing_to_say() {
        assert!(
            compare(&t("completed", 5, "s"), &t("completed", 5, "s"))
                .reasons
                .is_empty()
        );
    }

    // -- failure and skip -------------------------------------------------

    #[test]
    fn a_failed_replay_does_not_pass() {
        let d = failed(t("completed", 5, "s"), "upstream refused the connection");
        assert_eq!(d.verdict, verdict::FAILED);
        assert!(!d.passes());
    }

    #[test]
    fn a_skipped_case_does_not_pass_either() {
        // It is excluded from the rate rather than counted as a pass.
        let d = skipped(t("completed", 5, "s"), "side effects cannot be mocked");
        assert_eq!(d.verdict, verdict::SKIPPED);
        assert!(!d.passes());
    }

    // -- summary ----------------------------------------------------------

    #[test]
    fn the_summary_agrees_with_the_control_plane() {
        let results = vec![
            (
                compare(&t("completed", 5, "s"), &t("completed", 5, "s")),
                false,
            ),
            (
                compare(&t("completed", 5, "s"), &t("completed", 12, "s")),
                false,
            ),
            (
                compare(&t("completed", 5, "s"), &t("errored", 5, "s")),
                true,
            ),
            (skipped(t("completed", 5, "s"), "unsafe"), false),
        ];
        let s = summarize(&results);
        assert_eq!(s.total_cases, 4);
        assert_eq!(s.matched, 1);
        assert_eq!(s.shape_changed, 1);
        assert_eq!(s.outcome_changed, 1);
        assert_eq!(s.skipped, 1);
        // Skipped excluded from the denominator: 2 of 3 scored.
        assert!((s.pass_rate.unwrap() - 2.0 / 3.0).abs() < 1e-9);
        assert_eq!(s.critical_outcome_changed, 1);
    }

    #[test]
    fn an_entirely_skipped_suite_has_no_rate() {
        let results = vec![
            (skipped(Trajectory::default(), "unsafe"), false),
            (skipped(Trajectory::default(), "unsafe"), false),
        ];
        assert_eq!(summarize(&results).pass_rate, None);
    }

    #[test]
    fn an_empty_suite_has_no_rate() {
        assert_eq!(summarize(&[]).pass_rate, None);
    }

    #[test]
    fn the_verdict_vocabulary_matches_the_control_plane() {
        let mut all = verdict::ALL.to_vec();
        all.sort_unstable();
        assert_eq!(
            all,
            vec![
                "failed",
                "matched",
                "outcome_changed",
                "policy_changed",
                "shape_changed",
                "skipped",
            ]
        );
    }
}
