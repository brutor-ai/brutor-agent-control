// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! How a metric is MEASURED — pinned, so a change to it is a declared event.
//!
//! A drift finding says "this AI System behaves differently". When the
//! platform itself changes how a metric is computed, every system's samples
//! shift on upgrade day and the detector cannot tell that apart from the
//! agent changing. When a run's client label changed from the raw
//! User-Agent to its product name, `client_mix`
//! drift fired and a response policy demoted a system, for a
//! change nobody made to the agent.
//!
//! The control plane keeps a versioned registry of measurement changes
//! (`app/constants/measurement_changes.py`) and, once per installation,
//! writes a `measurement_change` baseline epoch for every affected system.
//! This module is the other half: it names the source that
//! DEFINES the metrics — the sample queries, the per-metric extraction, the
//! run and segment aggregation, the client-label derivation — and pins a
//! hash of it to the newest registry entry. Changing any of them fails
//! `the_metric_definitions_are_the_reviewed_ones` until the author either
//! adds a registry entry (the change alters a measurement) and bumps
//! [`MEASUREMENT_REGISTRY_HEAD`] in BOTH repos, or states that it does not
//! and re-pins the hash. Either way it is a decision, not an accident.

use sha2::{Digest, Sha256};

/// The newest entry of the control plane's measurement-change registry the
/// definitions below were reviewed against. Wire contract with
/// `app/constants/measurement_changes.MEASUREMENT_REGISTRY_HEAD` (the
/// control plane's `tests/test_measurement_changes.py` reads this line).
/// `m0003_step_clock_start`: run and segment `duration_ms`
/// start at the first call's START (`run_ledger::step_started_at_sql!`),
/// not its completion stamp.
/// `m0004_held_delegations`: a segment stopped by a hold for a
/// person (`held_approval_request_id`) is not a sample of the service;
/// a held delegation is a segment, and a hold on the
/// service's inbound side does not read as the service failing.
/// `m0005_first_prompt_context`: `first_prompt_tokens` is the first
/// model call's context with cached tokens included, on every provider.
pub const MEASUREMENT_REGISTRY_HEAD: &str = "m0005_first_prompt_context";

/// sha256 over [`definitions`], normalised (comment lines dropped,
/// whitespace collapsed) so a reformat or a comment does not trip it.
///
/// `peak_context_tokens` and `context_growth` need no
/// registry entry of their own: new metrics have no baseline to re-learn.
///
/// Re-pinned without an entry: the sample queries select two
/// columns (`run_id`, `tools_used`) and the run aggregate one (`tools_used`)
/// for first-seen shapes. No existing metric's value changes for the same
/// traffic. The baseline does not include the observation window;
/// that changes what is compared, not how any sample is measured.
///
/// Re-pinned without an entry: comments inside the SQL of the sample queries
/// and the run aggregate were reworded. No statement changed.
pub const MEASUREMENT_DEFINITIONS_SHA256: &str =
    "588b224ce3d50e3df6f3911880cc68e159da698e60f637fa9352cda6c786ccd4";

/// The text of a top-level `pub fn <name>` in `source`, up to the `}` that
/// closes it at column 0.
fn function_text<'a>(source: &'a str, name: &str) -> &'a str {
    let needle = format!("pub fn {name}");
    let start = source
        .find(&needle)
        .unwrap_or_else(|| panic!("measurement definition `{name}` not found"));
    let rest = &source[start..];
    let end = rest
        .find("\n}\n")
        .unwrap_or_else(|| panic!("end of `{name}` not found"));
    &rest[..end + 2]
}

/// Every source text that decides a metric's value, by name.
pub fn definitions() -> Vec<(&'static str, String)> {
    const BASELINE_RS: &str = include_str!("baseline.rs");
    const AGENT_IMPL_RS: &str = include_str!("../agent_impl.rs");
    vec![
        (
            "run_samples_sql",
            super::baseline::SAMPLES_SQL_TEMPLATE.to_string(),
        ),
        (
            "segment_samples_sql",
            super::baseline::SEGMENT_SAMPLES_SQL_TEMPLATE.to_string(),
        ),
        (
            "series_for",
            function_text(BASELINE_RS, "series_for").to_string(),
        ),
        (
            "distribution_for",
            function_text(BASELINE_RS, "distribution_for").to_string(),
        ),
        (
            "run_aggregate_sql",
            crate::services::run_ledger::AGGREGATE_SQL.to_string(),
        ),
        (
            "segment_parts_sql",
            crate::services::run_segments::PARTS_SQL.to_string(),
        ),
        (
            "segment_calls_sql",
            crate::services::run_segments::CALLS_SQL.to_string(),
        ),
        (
            "client_label",
            function_text(AGENT_IMPL_RS, "client_label").to_string(),
        ),
    ]
}

fn normalise(text: &str) -> String {
    text.lines()
        .filter(|l| !l.trim_start().starts_with("//"))
        .flat_map(|l| l.split_whitespace())
        .collect::<Vec<_>>()
        .join(" ")
}

/// The hash [`MEASUREMENT_DEFINITIONS_SHA256`] pins.
pub fn definitions_sha256() -> String {
    let mut h = Sha256::new();
    for (name, text) in definitions() {
        h.update(name.as_bytes());
        h.update([0u8]);
        h.update(normalise(&text).as_bytes());
        h.update([0u8]);
    }
    hex::encode(h.finalize())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_metric_definitions_are_the_reviewed_ones() {
        let now = definitions_sha256();
        assert_eq!(
            now, MEASUREMENT_DEFINITIONS_SHA256,
            "\nThe source that defines how a drift metric is MEASURED changed \
             (services::drift::measurement::definitions).\n\
             If the change alters any metric's value for the same traffic, add an entry to \
             the control plane's registry (brutor-gateway-control/app/constants/\
             measurement_changes.py) naming the metric keys, bases and reason, and set \
             MEASUREMENT_REGISTRY_HEAD to its id here AND there — every installation then \
             re-learns those baselines from the upgrade instead of reporting agent drift.\n\
             If it does not (a refactor, a column the metrics do not read), say so in \
             the change and re-pin MEASUREMENT_DEFINITIONS_SHA256 = \"{now}\"."
        );
    }

    #[test]
    fn normalising_ignores_comments_and_layout_only() {
        assert_eq!(normalise("a  b\n  // note\nc"), normalise("a b c"));
        assert_ne!(normalise("a b c"), normalise("a b d"));
    }

    #[test]
    fn every_definition_is_found_and_non_trivial() {
        for (name, text) in definitions() {
            assert!(text.len() > 40, "{name} looks empty: {text:?}");
        }
        assert!(
            function_text(include_str!("../agent_impl.rs"), "client_label").contains("User-Agent")
                || function_text(include_str!("../agent_impl.rs"), "client_label")
                    .contains("user_agent"),
            "client_label must be the derivation the registry's m0001 describes"
        );
    }

    #[test]
    fn the_registry_head_is_a_registry_id() {
        // `mNNNN_<slug>`, ≤ 64 chars — the control plane pins the same id.
        assert!(MEASUREMENT_REGISTRY_HEAD.len() <= 64);
        assert!(MEASUREMENT_REGISTRY_HEAD.starts_with('m'));
        assert!(
            MEASUREMENT_REGISTRY_HEAD[1..5]
                .chars()
                .all(|c| c.is_ascii_digit())
        );
        assert_eq!(MEASUREMENT_REGISTRY_HEAD, "m0005_first_prompt_context");
    }
}
