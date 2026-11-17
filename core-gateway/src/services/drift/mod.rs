// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Behavioural drift.
//!
//! The run ledger records what an AI System did. Liveness says whether it is
//! alive. This says whether what it does *now* differs from what it used
//! to do — and, the part that matters, **why**.
//!
//! ## The alert this is trying not to write
//!
//! > *action_count up 180%, p=0.001*
//!
//! That transfers the entire investigation to the operator. What ships
//! instead is:
//!
//! > *action_count up 180% since 14:00 on the 14th; the provider shipped a
//! > new point release of the underlying model at 13:52.*
//!
//! Brutor is the only component that sees both the traffic and the model
//! registry, so it is the only thing that can make that correlation. It is
//! the reason this phase exists.
//!
//! ## Where it runs
//!
//! In gateway-core, alongside the run sweeper and the liveness detector.
//! There is no control-plane scheduler, and
//! alert-raising lives here, so all three detectors share one home, one
//! beacon convention, and one operator console.
//!
//! ## Module layout
//!
//! * [`stats`] — robust statistics. No database, so every decision is
//!   arguable against a table of numbers.
//! * [`profiles`] — which metrics are alertable for which `system_kind`.
//! * [`baseline`] — turning runs into "what normal looks like".
//! * [`detect`] — comparing now against normal.
//! * [`cause`] — the attribution ladder.
//! * [`exclusion`] — which samples may be judged after a human resolution.

pub mod baseline;
pub mod cause;
/// Labelled synthetic streams incl. no-change controls.
pub mod corpus;
pub mod detect;
/// Which samples may be judged (incident windows, baseline
/// epochs, closures).
pub mod exclusion;
/// The pinned definition of how each metric is measured.
pub mod measurement;
/// First-seen tools and tool sets: one run can be enough.
pub mod new_shape;
pub mod profiles;
pub mod stats;
pub mod sweeper;

// ---------------------------------------------------------------------------
// Wire contract — mirrored in app/database/models/ai_system_drift.py
// ---------------------------------------------------------------------------

pub mod drift_class {
    pub const BEHAVIOURAL: &str = "behavioural";
    pub const COST: &str = "cost";
    pub const DEPENDENCY: &str = "dependency";
    pub const MODEL: &str = "model";
    pub const INPUT: &str = "input";
    pub const CONFORMANCE: &str = "conformance";
    /// A declared operating-envelope term breached. Emitted by the
    /// control plane's assurance tick, never by these sweeps: a breach is
    /// not a statistical finding, it is a signed bound the ledger says was
    /// exceeded. In the vocabulary so both planes agree on what a
    /// `drift_class` may say.
    pub const CONTRACT: &str = "contract";
    /// An `agent.*` finding (unapproved release, name
    /// mismatch, missing release, undeclared change). Opened by the control
    /// plane's checks, never by these sweeps (no metric maps to it), so a
    /// class-`agent` event can never trip or silence the core's per-class
    /// cooldown. In the vocabulary so both planes agree on it.
    pub const AGENT: &str = "agent";

    pub const ALL: [&str; 8] = [
        BEHAVIOURAL,
        COST,
        DEPENDENCY,
        MODEL,
        INPUT,
        CONFORMANCE,
        CONTRACT,
        AGENT,
    ];

    /// Classes only the control plane emits. The sweeps' metric → class map
    /// must never produce one (pinned in `profiles`).
    pub const CP_ONLY: [&str; 2] = [CONTRACT, AGENT];
}

pub mod baseline_state {
    pub const LEARNING: &str = "learning";
    pub const READY: &str = "ready";
    pub const STALE: &str = "stale";
}

pub mod drift_status {
    pub const ACTIVE: &str = "active";
    pub const ACKNOWLEDGED: &str = "acknowledged";
    pub const RESOLVED: &str = "resolved";
    pub const FALSE_POSITIVE: &str = "false_positive";
}

/// Metric keys. Stable strings — they are the join between a baseline row,
/// a drift event and the UI label, so a rename is a wire change.
pub mod metric {
    pub const ACTION_COUNT: &str = "action_count";
    pub const COST_PER_RUN: &str = "cost_per_run";
    /// Measured over the run's ACTIVE duration (`ai_system_runs.active_duration_ms`,
    /// time awaiting a human excluded). The key keeps its original name: it
    /// joins stored baselines and drift events, so renaming it is a wire change.
    pub const DURATION_MS: &str = "duration_ms";
    pub const TOOL_CALL_COUNT: &str = "tool_call_count";
    pub const MAX_DELEGATION_DEPTH: &str = "max_delegation_depth";
    pub const TOOL_ERROR_RATE: &str = "tool_error_rate";
    pub const EXHAUSTION_RATE: &str = "exhaustion_rate";
    /// Categorical — ordered tool trajectory shape.
    pub const TOOL_MIX: &str = "tool_mix";
    /// Categorical — outcome mix.
    pub const TERMINAL_STATE_MIX: &str = "terminal_state_mix";
    /// Categorical — which models served the system.
    pub const MODEL_MIX: &str = "model_mix";
    /// Input-distribution proxy: the size of the task as posed.
    pub const FIRST_PROMPT_TOKENS: &str = "first_prompt_tokens";
    pub const INPUT_BYTES: &str = "input_bytes";
    /// The largest context any call in the run sent its model, cached tokens
    /// included. A run with no reported usage is not a sample.
    pub const PEAK_CONTEXT_TOKENS: &str = "peak_context_tokens";
    /// That peak over the same agent's first measured context. With
    /// `first_prompt_tokens` flat, a rise means the agent changed rather than
    /// the questions.
    pub const CONTEXT_GROWTH: &str = "context_growth";
    /// Categorical — which clients called.
    pub const CLIENT_MIX: &str = "client_mix";
    /// Categorical: the full declared outcome id over SUCCESS runs.
    /// Catches a shift in the mix of work ("`loan_referred` rose from 12% to
    /// 35%") that per-outcome baselines cannot see. Runs basis only.
    pub const OUTCOME_MIX: &str = "outcome_mix";
    /// Categorical: the error cause over FAILED runs ("now mostly
    /// `model_unavailable`"). Runs basis only.
    pub const ERROR_CAUSE_MIX: &str = "error_cause_mix";
    /// Model calls per served call. Segment basis only: a run
    /// row does not carry it.
    pub const LLM_CALL_COUNT: &str = "llm_call_count";
    /// Tokens per served call. Segment basis only.
    pub const TOTAL_TOKENS: &str = "total_tokens";
    /// Share of a served call's actions that errored. Segment
    /// basis only.
    pub const ERROR_RATE: &str = "error_rate";

    /// Metric-key namespace owned by the CONTROL PLANE:
    /// `dependency:<target system>` drift events it opens on a ROOT when a
    /// critical dependency is unhealthy
    /// (`ai_system_service_signals.DEPENDENCY_METRIC_PREFIX`). They are a
    /// different producer describing a different fact, so they never share
    /// the core's per-class cooldown, and the core never emits a key under
    /// this prefix. Wire contract: pinned here and in the control plane's
    /// `tests/test_wire_contract.py`.
    pub const CP_DEPENDENCY_PREFIX: &str = "dependency:";

    /// Findings about one run's shape, not a population's statistics
    /// ([`super::new_shape`]): `shape:new_tool:<tool>` and
    /// `shape:new_tool_set:<signature>`. They never share a per-class cooldown
    /// with statistical findings, in either direction. Wire contract with the
    /// control plane's `app/database/models/ai_system_drift.py`.
    pub const SHAPE_PREFIX: &str = "shape:";

    /// `reach:<rule>:<group hash>`: a requester over a declared
    /// distinct-record limit. A fact finding like [`SHAPE_PREFIX`]: one open
    /// event per key, outside the per-class cooldown.
    pub const REACH_PREFIX: &str = "reach:";

    /// Findings about facts rather than statistics.
    pub fn is_fact_finding(metric_key: &str) -> bool {
        metric_key.starts_with(SHAPE_PREFIX) || metric_key.starts_with(REACH_PREFIX)
    }
}

/// What a baseline (and a drift event) is computed from.
///
/// A root system is baselined from the runs it roots; a shared service
/// from the segments it serves inside other systems' runs. A system that
/// does both keeps two independent sets of baselines — the work it starts
/// and the work it is called for are different populations, and pooling
/// them would let one mask drift in the other.
///
/// Mirrored in the control plane as
/// `app/database/models/ai_system_drift.BASIS_RUNS` / `BASIS_SEGMENTS`.
pub mod basis {
    pub const RUNS: &str = "runs";
    pub const SEGMENTS: &str = "segments";

    pub const ALL: [&str; 2] = [RUNS, SEGMENTS];
}

/// A caller gets its own sub-baseline of a service only
/// when it accounts for at least this share of the service's segments in
/// the window (and meets [`MIN_RUNS_FOR_BASELINE`] on its own). The
/// aggregate over every caller is always kept.
pub const MIN_CALLER_SHARE: f64 = 0.20;

/// Below this many baseline-eligible runs a system is `learning` and emits
/// **no** drift alerts at all.
///
/// Being honest that value lands in week three beats emitting noise on day
/// one. An operator paged by a baseline built from eight runs does not
/// give the feature a second chance.
pub const MIN_RUNS_FOR_BASELINE: i64 = 50;

pub const BASELINE_WINDOW_DAYS: i64 = 14;

/// Hard ceiling on how many runs one baseline refresh pulls into memory.
///
/// The sweep runs every 15 minutes, per AI System, per tenant, in the same
/// process that serves proxy traffic — an unbounded 14-day load is a
/// periodic multi-gigabyte allocation spike on a busy estate. 20k runs is
/// three orders of magnitude past [`MIN_RUNS_FOR_BASELINE`], so the
/// median/MAD it produces is statistically indistinguishable from one over
/// the full window; see `baseline::SAMPLES_SQL` for what the cap costs in
/// exactness when a system exceeds it.
pub const MAX_BASELINE_SAMPLES: i64 = 20_000;
pub const BASELINE_MAX_AGE_HOURS: i64 = 36;

/// Jensen–Shannon threshold for categorical drift.
pub const JS_DIVERGENCE_THRESHOLD: f64 = 0.15;
/// Robust z magnitude before a continuous metric is a candidate.
pub const ROBUST_Z_THRESHOLD: f64 = 3.0;
// Page–Hinkley operates on *robust z-scores*, not raw metric values —
// see `detect_continuous`. That makes both constants scale-free: `delta`
// is half a robust sigma of slack, and the threshold is accumulated
// sigma-units.
//
// The threshold is deliberately larger than any single observation can
// contribute (robust_z saturates at 10). A change point must be *earned*
// over several observations, otherwise one bad afternoon reads as a
// system that started behaving differently.
pub const PAGE_HINKLEY_THRESHOLD: f64 = 25.0;
pub const PAGE_HINKLEY_DELTA: f64 = 0.5;
/// A rate must move this much in absolute terms as well as proportionally.
pub const MIN_ABSOLUTE_RATE_CHANGE: f64 = 0.02;

/// How far either side of a change point a candidate cause may sit.
pub const CAUSE_WINDOW_HOURS: i64 = 2;

/// One alert per system per drift class per this window.
pub const ALERT_COOLDOWN_SECONDS: i64 = 6 * 3600;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn drift_classes_match_the_control_plane() {
        let mut all = drift_class::ALL.to_vec();
        all.sort_unstable();
        assert_eq!(
            all,
            vec![
                // CP-emitted `agent.*` findings; never swept here.
                "agent",
                "behavioural",
                "conformance",
                // CP-emitted envelope breaches; the sweeps never
                // emit it, but the vocabulary is shared.
                "contract",
                "cost",
                "dependency",
                "input",
                "model",
            ]
        );
    }

    #[test]
    fn thresholds_match_the_control_plane() {
        assert_eq!(MIN_RUNS_FOR_BASELINE, 50);
        assert_eq!(JS_DIVERGENCE_THRESHOLD, 0.15);
        assert_eq!(ROBUST_Z_THRESHOLD, 3.0);
        assert_eq!(PAGE_HINKLEY_THRESHOLD, 25.0);
        assert_eq!(PAGE_HINKLEY_DELTA, 0.5);
        assert_eq!(MIN_ABSOLUTE_RATE_CHANGE, 0.02);
        assert_eq!(CAUSE_WINDOW_HOURS, 2);
    }

    #[test]
    fn bases_match_the_control_plane() {
        assert_eq!(basis::ALL, ["runs", "segments"]);
        assert_eq!(MIN_CALLER_SHARE, 0.20);
    }

    #[test]
    fn baseline_states_match_the_control_plane() {
        assert_eq!(baseline_state::LEARNING, "learning");
        assert_eq!(baseline_state::READY, "ready");
        assert_eq!(baseline_state::STALE, "stale");
    }
}
