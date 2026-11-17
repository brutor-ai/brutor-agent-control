// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Signal profiles per `system_kind`.
//!
//! This table is what lets one drift engine serve six shapes of AI System
//! without becoming noise.
//!
//! Trajectory drift is the headline signal for an `agent` and a
//! `workflow`, and near-meaningless for an `application` that makes one
//! model call per request — an application's step count is 1 by
//! construction, so "step count changed" would either never fire or fire
//! on nothing. Cadence matters for an `integration` and is wrong for an
//! `assistant`, whose rhythm is set by humans who go on holiday.
//!
//! Applying agent-shaped detectors to an application generates alerts
//! nobody can act on, and the operator's response is to mute the category
//! — taking the useful signals with it. So the profile is not cosmetic:
//! it is the difference between a feature that gets used and one that gets
//! switched off in week two.

use super::metric;

/// How seriously a metric is taken for a given kind of system.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Sensitivity {
    /// Alertable by default.
    Primary,
    /// Collected and charted; alertable only on explicit opt-in.
    Secondary,
    /// Not collected — no storage cost, and no confusing empty chart.
    Off,
}

impl Sensitivity {
    pub fn is_alertable(&self) -> bool {
        matches!(self, Sensitivity::Primary)
    }
    pub fn is_collected(&self) -> bool {
        !matches!(self, Sensitivity::Off)
    }
    pub fn as_str(&self) -> &'static str {
        match self {
            Sensitivity::Primary => "primary",
            Sensitivity::Secondary => "secondary",
            Sensitivity::Off => "off",
        }
    }
}

use Sensitivity::{Off, Primary, Secondary};

/// The profile for one `system_kind`.
///
/// Unknown kinds fall back to the `agent` profile: it is the strictest
/// superset, so a system whose kind we cannot read is watched rather than
/// silently ignored.
pub fn sensitivity(system_kind: Option<&str>, metric_key: &str) -> Sensitivity {
    let kind = system_kind.unwrap_or("agent");
    match metric_key {
        // -- trajectory ----------------------------------------------------
        metric::ACTION_COUNT | metric::TOOL_MIX => match kind {
            "agent" | "workflow" => Primary,
            "assistant" | "integration" | "service" => Secondary,
            // An application makes one call per request; its step count is
            // 1 by construction and cannot meaningfully drift.
            "application" => Off,
            _ => Primary,
        },
        metric::TOOL_CALL_COUNT | metric::LLM_CALL_COUNT => match kind {
            "agent" | "workflow" => Primary,
            "application" => Off,
            _ => Secondary,
        },
        metric::MAX_DELEGATION_DEPTH => match kind {
            "agent" => Primary,
            "workflow" | "service" => Secondary,
            _ => Off,
        },
        metric::EXHAUSTION_RATE => match kind {
            "agent" | "workflow" | "integration" => Primary,
            "application" => Off,
            _ => Secondary,
        },

        // -- context: what the agent carries from turn to turn. An application
        //    sends what its product code builds, and a person sets the pace of
        //    an assistant's conversation: charted there, or not collected.
        metric::PEAK_CONTEXT_TOKENS | metric::CONTEXT_GROWTH => match kind {
            "agent" | "workflow" => Primary,
            "application" => Off,
            _ => Secondary,
        },

        // -- volume per served call: charted, never paged on by default.
        //    Tokens move with cost, and cost is the alertable one — paging
        //    on both is two alerts for one change.
        metric::TOTAL_TOKENS => Secondary,

        // -- cost: universal. Every kind of system can start costing more
        //    for the same work, and it is the number a CFO reacts to.
        metric::COST_PER_RUN => Primary,

        // -- latency -------------------------------------------------------
        metric::DURATION_MS => match kind {
            "assistant" | "application" | "service" => Primary,
            _ => Secondary,
        },

        // -- dependencies --------------------------------------------------
        metric::TOOL_ERROR_RATE => match kind {
            "application" => Secondary,
            _ => Primary,
        },

        // -- outcomes: universal. For a service the outcome IS its
        //    reliability as seen by the systems that depend on it.
        metric::TERMINAL_STATE_MIX | metric::ERROR_RATE => Primary,

        // -- model + input: universal. Input drift is the disambiguator
        //    that separates "the system changed" from "the questions
        //    changed", and it is worthless if it only runs for some kinds.
        metric::MODEL_MIX => Primary,
        metric::FIRST_PROMPT_TOKENS | metric::INPUT_BYTES => Primary,

        // -- conformance: universal.
        metric::CLIENT_MIX => Primary,

        // -- What the work produced, and why it failed. Universal:
        //    every kind declares its outcomes and can start failing for a
        //    new reason.
        metric::OUTCOME_MIX | metric::ERROR_CAUSE_MIX => Primary,

        _ => Secondary,
    }
}

/// The metrics a basis can measure at all.
///
/// A segment is a slice of someone else's run: it has no first prompt, no
/// input size, no client label and no terminal "exhausted" state of its own,
/// so those metrics are not in its set — a baseline over values that are
/// never there is an empty chart forever. Unknown bases get the run set.
pub fn metrics_for(basis: &str) -> &'static [&'static str] {
    if basis == super::basis::SEGMENTS {
        &SEGMENT_METRICS
    } else {
        &ALL_METRICS
    }
}

/// Metrics worth computing a baseline for, given a basis and a system's kind.
pub fn collected_metrics(basis: &str, system_kind: Option<&str>) -> Vec<&'static str> {
    metrics_for(basis)
        .iter()
        .copied()
        .filter(|m| sensitivity(system_kind, m).is_collected())
        .collect()
}

/// Metrics that may raise an alert without an explicit opt-in.
pub fn alertable_metrics(basis: &str, system_kind: Option<&str>) -> Vec<&'static str> {
    metrics_for(basis)
        .iter()
        .copied()
        .filter(|m| sensitivity(system_kind, m).is_alertable())
        .collect()
}

/// Which metrics are categorical (compared with JS divergence) rather than
/// continuous (compared with a robust z-score).
pub fn is_categorical(metric_key: &str) -> bool {
    matches!(
        metric_key,
        metric::TOOL_MIX
            | metric::TERMINAL_STATE_MIX
            | metric::MODEL_MIX
            | metric::CLIENT_MIX
            | metric::OUTCOME_MIX
            | metric::ERROR_CAUSE_MIX
    )
}

/// Which metrics are rates in 0..1 (compared with the two-proportion test).
pub fn is_rate(metric_key: &str) -> bool {
    matches!(
        metric_key,
        metric::TOOL_ERROR_RATE | metric::EXHAUSTION_RATE | metric::ERROR_RATE
    )
}

/// The drift class a metric belongs to.
pub fn drift_class_for(metric_key: &str) -> &'static str {
    use super::drift_class;
    match metric_key {
        metric::COST_PER_RUN => drift_class::COST,
        // A change in WHY runs fail is first a dependency question
        // (a model, a tool, a delegate); the cause ladder refines it.
        metric::TOOL_ERROR_RATE | metric::ERROR_CAUSE_MIX => drift_class::DEPENDENCY,
        metric::MODEL_MIX => drift_class::MODEL,
        metric::FIRST_PROMPT_TOKENS | metric::INPUT_BYTES => drift_class::INPUT,
        metric::CLIENT_MIX => drift_class::CONFORMANCE,
        _ => drift_class::BEHAVIOURAL,
    }
}

pub const ALL_METRICS: [&str; 17] = [
    metric::ACTION_COUNT,
    metric::COST_PER_RUN,
    metric::DURATION_MS,
    metric::TOOL_CALL_COUNT,
    metric::MAX_DELEGATION_DEPTH,
    metric::TOOL_ERROR_RATE,
    metric::EXHAUSTION_RATE,
    metric::TOOL_MIX,
    metric::TERMINAL_STATE_MIX,
    metric::MODEL_MIX,
    metric::FIRST_PROMPT_TOKENS,
    metric::INPUT_BYTES,
    metric::CLIENT_MIX,
    metric::OUTCOME_MIX,
    metric::ERROR_CAUSE_MIX,
    metric::PEAK_CONTEXT_TOKENS,
    metric::CONTEXT_GROWTH,
];

/// Whether a metric is learned per outcome bucket as well as in
/// `all`: the continuous, non-rate ones. Rates and mixes describe the whole
/// population and live in `all` only.
pub fn is_bucketed(metric_key: &str) -> bool {
    !is_categorical(metric_key) && !is_rate(metric_key)
}

/// What a shared service is baselined on, per served
/// call: actions, model calls, tool calls, tokens, cost, active duration,
/// tool mix, model mix, outcome mix and error rates.
///
/// `cost_per_run`, `duration_ms` and `terminal_state_mix` keep their keys:
/// on this basis the "run" is the served call and the terminal state is the
/// segment outcome. The key is the join between baseline, event and label,
/// and the basis column says which population it describes.
pub const SEGMENT_METRICS: [&str; 11] = [
    metric::ACTION_COUNT,
    metric::LLM_CALL_COUNT,
    metric::TOOL_CALL_COUNT,
    metric::TOTAL_TOKENS,
    metric::COST_PER_RUN,
    metric::DURATION_MS,
    metric::TOOL_MIX,
    metric::MODEL_MIX,
    metric::TERMINAL_STATE_MIX,
    metric::ERROR_RATE,
    metric::TOOL_ERROR_RATE,
];

pub const SYSTEM_KINDS: [&str; 6] = [
    "agent",
    "assistant",
    "application",
    "integration",
    "workflow",
    "service",
];

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn context_is_watched_on_agents_and_only_charted_where_a_person_sets_the_pace() {
        for m in [metric::PEAK_CONTEXT_TOKENS, metric::CONTEXT_GROWTH] {
            assert!(ALL_METRICS.contains(&m));
            assert!(
                !SEGMENT_METRICS.contains(&m),
                "a segment has no context of its own"
            );
            assert_eq!(sensitivity(Some("agent"), m), Primary);
            assert_eq!(sensitivity(Some("workflow"), m), Primary);
            assert_eq!(sensitivity(None, m), Primary, "an unknown kind is watched");
            assert_eq!(sensitivity(Some("assistant"), m), Secondary);
            assert_eq!(sensitivity(Some("application"), m), Off);
            assert_eq!(
                drift_class_for(m),
                crate::services::drift::drift_class::BEHAVIOURAL
            );
            assert!(is_bucketed(m));
        }
    }

    #[test]
    fn trajectory_is_the_headline_for_agents_and_workflows() {
        for kind in ["agent", "workflow"] {
            assert_eq!(
                sensitivity(Some(kind), metric::ACTION_COUNT),
                Primary,
                "{kind}"
            );
            assert_eq!(sensitivity(Some(kind), metric::TOOL_MIX), Primary, "{kind}");
        }
    }

    #[test]
    fn trajectory_is_off_for_applications() {
        // One model call per request — step count is 1 by construction, so
        // the detector would either never fire or fire on nothing.
        assert_eq!(sensitivity(Some("application"), metric::ACTION_COUNT), Off);
        assert_eq!(
            sensitivity(Some("application"), metric::TOOL_CALL_COUNT),
            Off
        );
    }

    #[test]
    fn cost_is_alertable_for_every_kind() {
        for kind in SYSTEM_KINDS {
            assert_eq!(
                sensitivity(Some(kind), metric::COST_PER_RUN),
                Primary,
                "cost per completed run matters for {kind} as much as any other"
            );
        }
    }

    #[test]
    fn input_drift_runs_everywhere() {
        // It is the disambiguator between "the system changed" and "the
        // questions changed". Restricting it to some kinds would mean the
        // others cannot tell those apart at all.
        for kind in SYSTEM_KINDS {
            assert!(sensitivity(Some(kind), metric::FIRST_PROMPT_TOKENS).is_alertable());
        }
    }

    #[test]
    fn latency_leads_for_user_facing_kinds() {
        for kind in ["assistant", "application", "service"] {
            assert_eq!(
                sensitivity(Some(kind), metric::DURATION_MS),
                Primary,
                "{kind}"
            );
        }
        assert_eq!(sensitivity(Some("agent"), metric::DURATION_MS), Secondary);
    }

    #[test]
    fn unknown_kind_falls_back_to_the_strictest_profile() {
        // Watched rather than silently ignored.
        assert_eq!(sensitivity(None, metric::ACTION_COUNT), Primary);
        assert_eq!(
            sensitivity(Some("something-new"), metric::ACTION_COUNT),
            Primary
        );
    }

    #[test]
    fn off_metrics_are_not_collected_at_all() {
        let collected = collected_metrics(super::super::basis::RUNS, Some("application"));
        assert!(!collected.contains(&metric::ACTION_COUNT));
        // No storage cost, and no chart that renders empty forever.
        assert!(collected.contains(&metric::COST_PER_RUN));
    }

    #[test]
    fn alertable_is_a_subset_of_collected() {
        for b in super::super::basis::ALL {
            for kind in SYSTEM_KINDS {
                let collected = collected_metrics(b, Some(kind));
                for m in alertable_metrics(b, Some(kind)) {
                    assert!(
                        collected.contains(&m),
                        "{b}/{kind}/{m} alertable but not collected"
                    );
                }
            }
        }
    }

    #[test]
    fn every_metric_has_a_drift_class() {
        use super::super::drift_class;
        for m in ALL_METRICS.iter().chain(SEGMENT_METRICS.iter()) {
            assert!(drift_class::ALL.contains(&drift_class_for(m)), "{m}");
            // The CP-only classes (`contract`, `agent`) are never a
            // sweep's: emitting one would share a cooldown with the control
            // plane's findings.
            assert!(
                !drift_class::CP_ONLY.contains(&drift_class_for(m)),
                "{m} maps to a control-plane-only class"
            );
        }
    }

    #[test]
    fn the_core_never_emits_a_key_in_the_control_planes_namespace() {
        use super::super::metric::CP_DEPENDENCY_PREFIX;
        assert_eq!(
            CP_DEPENDENCY_PREFIX, "dependency:",
            "wire contract with the control plane"
        );
        for m in ALL_METRICS.iter().chain(SEGMENT_METRICS.iter()) {
            assert!(
                !m.starts_with(CP_DEPENDENCY_PREFIX),
                "{m} is in the control plane's namespace"
            );
        }
    }

    #[test]
    fn categorical_and_rate_metrics_do_not_overlap() {
        for m in ALL_METRICS.iter().chain(SEGMENT_METRICS.iter()) {
            assert!(
                !(is_categorical(m) && is_rate(m)),
                "{m} cannot be compared two ways"
            );
        }
    }

    #[test]
    fn categorical_metrics_are_the_mix_metrics() {
        assert!(is_categorical(metric::TOOL_MIX));
        assert!(is_categorical(metric::TERMINAL_STATE_MIX));
        assert!(is_categorical(metric::MODEL_MIX));
        assert!(is_categorical(metric::CLIENT_MIX));
        assert!(is_categorical(metric::OUTCOME_MIX));
        assert!(is_categorical(metric::ERROR_CAUSE_MIX));
        assert!(!is_categorical(metric::ACTION_COUNT));
    }

    #[test]
    fn the_outcome_metrics_are_run_metrics_alertable_everywhere() {
        use super::super::basis;
        assert_eq!(metric::OUTCOME_MIX, "outcome_mix");
        assert_eq!(metric::ERROR_CAUSE_MIX, "error_cause_mix");
        for m in [metric::OUTCOME_MIX, metric::ERROR_CAUSE_MIX] {
            assert!(metrics_for(basis::RUNS).contains(&m));
            assert!(
                !metrics_for(basis::SEGMENTS).contains(&m),
                "segments have no run class"
            );
            for kind in SYSTEM_KINDS {
                assert!(sensitivity(Some(kind), m).is_alertable(), "{kind}/{m}");
            }
            assert!(!is_bucketed(m));
        }
        // Only continuous, non-rate metrics are learned per outcome.
        assert!(is_bucketed(metric::ACTION_COUNT));
        assert!(is_bucketed(metric::COST_PER_RUN));
        assert!(!is_bucketed(metric::TOOL_ERROR_RATE));
        assert!(!is_bucketed(metric::TERMINAL_STATE_MIX));
    }

    #[test]
    fn no_kind_has_everything_switched_off() {
        for b in super::super::basis::ALL {
            for kind in SYSTEM_KINDS {
                assert!(
                    !alertable_metrics(b, Some(kind)).is_empty(),
                    "{b}/{kind} would be registered but never watched"
                );
            }
        }
    }

    #[test]
    fn the_segment_basis_measures_what_a_segment_carries() {
        // The contract: actions, llm calls, tool calls, tokens,
        // cost, active duration, tool mix, model mix, outcome mix, error rate.
        use super::super::basis;
        let seg = metrics_for(basis::SEGMENTS);
        for m in [
            metric::ACTION_COUNT,
            metric::LLM_CALL_COUNT,
            metric::TOOL_CALL_COUNT,
            metric::TOTAL_TOKENS,
            metric::COST_PER_RUN,
            metric::DURATION_MS,
            metric::TOOL_MIX,
            metric::MODEL_MIX,
            metric::TERMINAL_STATE_MIX,
            metric::ERROR_RATE,
        ] {
            assert!(seg.contains(&m), "segments must baseline {m}");
        }
        // A segment has no first prompt, input size, client label, own
        // delegation depth or exhausted state: an empty chart forever.
        for m in [
            metric::FIRST_PROMPT_TOKENS,
            metric::INPUT_BYTES,
            metric::CLIENT_MIX,
            metric::MAX_DELEGATION_DEPTH,
            metric::EXHAUSTION_RATE,
        ] {
            assert!(!seg.contains(&m), "segments cannot measure {m}");
        }
        // The runs basis is unchanged by it.
        assert_eq!(metrics_for(basis::RUNS), &ALL_METRICS);
    }
}
