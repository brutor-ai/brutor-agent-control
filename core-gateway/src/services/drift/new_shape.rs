// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! First-seen tools and tool sets.
//!
//! Drift compares populations, so it cannot fire on a single run, and a
//! minority of runs barely moves it. Yet the most useful thing a system can
//! say about an agent is often about one run: "this run called a tool this
//! system has never used". In a detection test one
//! exfiltration run in 73 changed nothing in the statistics.
//!
//! Two findings, both on the runs basis:
//!
//! * **New tool** — a run used a tool absent from the whole reference period.
//! * **New tool set** — every tool is known but this combination is not, and
//!   it is not just part of a known one (a run that stops early is not
//!   news). Only raised for a system whose shapes are stable: an agent that
//!   already mixes many combinations would make this noise.
//!
//! Not every stored run has tool names, so the reference period only counts runs
//! that have them. Otherwise every tool would look new.

use std::collections::{BTreeMap, BTreeSet};

use chrono::{DateTime, Utc};
use sha2::{Digest, Sha256};

use super::{MIN_RUNS_FOR_BASELINE, baseline::Sample, metric::SHAPE_PREFIX};

/// Above this many distinct known tool sets a system's shape is not stable
/// enough for a new combination to mean anything.
pub const MAX_STABLE_TOOL_SETS: usize = 10;

/// Runs named on a finding. The rest are one query away.
pub const MAX_NAMED_RUNS: usize = 5;

/// `ai_system_drift_events.metric_key` is `varchar(64)`.
const MAX_METRIC_KEY_LEN: usize = 64;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ShapeKind {
    NewTool,
    NewToolSet,
}

/// One first-seen shape, with the runs that showed it.
#[derive(Debug, Clone, PartialEq)]
pub struct ShapeFinding {
    pub kind: ShapeKind,
    pub metric_key: String,
    /// The new tool, or every tool in the new set.
    pub tools: Vec<String>,
    pub first_seen: DateTime<Utc>,
    pub run_count: i64,
    /// Oldest first, capped at [`MAX_NAMED_RUNS`].
    pub run_ids: Vec<String>,
}

fn tools_of(sample: &Sample) -> Option<BTreeSet<String>> {
    sample.tools_used.as_ref()?.as_array().map(|a| {
        a.iter()
            .filter_map(|t| t.as_str())
            .map(str::to_string)
            .collect()
    })
}

/// `shape:new_tool:<tool>`, with long names cut and made unique by a hash.
pub fn new_tool_key(tool: &str) -> String {
    let key = format!("{SHAPE_PREFIX}new_tool:{tool}");
    if key.len() <= MAX_METRIC_KEY_LEN {
        return key;
    }
    let hash = format!("{:x}", Sha256::digest(tool.as_bytes()));
    let suffix = &hash[..12];
    let mut cut = MAX_METRIC_KEY_LEN - suffix.len() - 1;
    while !key.is_char_boundary(cut) {
        cut -= 1;
    }
    format!("{}~{suffix}", &key[..cut])
}

fn new_tool_set_key(tools: &BTreeSet<String>) -> String {
    let joined = tools.iter().cloned().collect::<Vec<_>>().join(",");
    let hash = format!("{:x}", Sha256::digest(joined.as_bytes()));
    format!("{SHAPE_PREFIX}new_tool_set:{}", &hash[..32])
}

/// Compare the runs being judged against the reference period. Both are
/// oldest-first. Nothing is raised until the reference holds
/// [`MIN_RUNS_FOR_BASELINE`] runs with recorded tools: below that, "never seen"
/// means little.
pub fn detect(reference: &[&Sample], observed: &[&Sample]) -> Vec<ShapeFinding> {
    let known_sets: BTreeSet<BTreeSet<String>> =
        reference.iter().filter_map(|s| tools_of(s)).collect();
    let recorded = reference.iter().filter(|s| tools_of(s).is_some()).count() as i64;
    if recorded < MIN_RUNS_FOR_BASELINE {
        return Vec::new();
    }
    let known_tools: BTreeSet<&String> = known_sets.iter().flatten().collect();
    let stable = known_sets.len() <= MAX_STABLE_TOOL_SETS;

    let mut found: BTreeMap<String, ShapeFinding> = BTreeMap::new();
    let mut note = |key: String, kind, tools: Vec<String>, s: &Sample| {
        let f = found.entry(key.clone()).or_insert_with(|| ShapeFinding {
            kind,
            metric_key: key,
            tools,
            first_seen: s.started_at,
            run_count: 0,
            run_ids: Vec::new(),
        });
        f.run_count += 1;
        if let (Some(id), true) = (&s.run_id, f.run_ids.len() < MAX_NAMED_RUNS) {
            f.run_ids.push(id.clone());
        }
    };

    for sample in observed {
        let Some(tools) = tools_of(sample) else {
            continue;
        };
        let unknown: Vec<&String> = tools.iter().filter(|t| !known_tools.contains(t)).collect();
        if !unknown.is_empty() {
            for tool in unknown {
                note(
                    new_tool_key(tool),
                    ShapeKind::NewTool,
                    vec![tool.clone()],
                    sample,
                );
            }
            continue;
        }
        let part_of_known = known_sets.iter().any(|k| tools.is_subset(k));
        if stable && !part_of_known {
            note(
                new_tool_set_key(&tools),
                ShapeKind::NewToolSet,
                tools.iter().cloned().collect(),
                sample,
            );
        }
    }
    found.into_values().collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Duration;
    use serde_json::json;

    fn t(minute: i64) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339("2026-11-07T00:00:00Z")
            .unwrap()
            .with_timezone(&Utc)
            + Duration::minutes(minute)
    }

    fn run(id: &str, minute: i64, tools: &[&str]) -> Sample {
        Sample {
            started_at: t(minute),
            action_count: 2,
            active_duration_ms: 1,
            tool_call_count: tools.len() as i32,
            llm_call_count: None,
            total_tokens: None,
            error_count: None,
            max_delegation_depth: None,
            cost_usd: 0.0,
            tool_error_count: 0,
            terminal_state: "completed".into(),
            tool_set_signature: None,
            first_prompt_tokens: None,
            input_bytes: None,
            peak_context_tokens: None,
            context_growth: None,
            client_label: None,
            models_used: None,
            caller_system_id: None,
            is_run: true,
            outcome_id: None,
            outcome_conformance: None,
            outcome_counts_toward_baseline: None,
            error_cause: None,
            run_id: Some(id.into()),
            tools_used: Some(json!(tools)),
        }
    }

    fn history(tools: &[&str]) -> Vec<Sample> {
        (0..MIN_RUNS_FOR_BASELINE)
            .map(|i| run(&format!("h{i}"), i, tools))
            .collect()
    }

    fn refs(s: &[Sample]) -> Vec<&Sample> {
        s.iter().collect()
    }

    #[test]
    fn one_run_with_a_never_seen_tool_is_enough() {
        let h = history(&["crm_get", "crm_note"]);
        let o = [
            run("ok", 100, &["crm_get", "crm_note"]),
            run("bad", 101, &["crm_get", "http_post_external"]),
        ];
        let found = detect(&refs(&h), &refs(&o));
        assert_eq!(found.len(), 1, "{found:?}");
        assert_eq!(found[0].kind, ShapeKind::NewTool);
        assert_eq!(found[0].metric_key, "shape:new_tool:http_post_external");
        assert_eq!(found[0].tools, vec!["http_post_external"]);
        assert_eq!(found[0].first_seen, t(101));
        assert_eq!(found[0].run_ids, vec!["bad"]);
    }

    #[test]
    fn the_same_shape_is_not_news() {
        let h = history(&["crm_get", "crm_note"]);
        let o = [run("ok", 100, &["crm_note", "crm_get"])];
        assert!(detect(&refs(&h), &refs(&o)).is_empty());
    }

    #[test]
    fn a_run_that_stops_early_is_not_a_new_shape() {
        let h = history(&["crm_get", "crm_note"]);
        let o = [run("short", 100, &["crm_get"]), run("none", 101, &[])];
        assert!(detect(&refs(&h), &refs(&o)).is_empty());
    }

    #[test]
    fn known_tools_in_a_new_combination_are_raised_on_a_stable_system() {
        let mut h = history(&["crm_get"]);
        h.push(run("x", 90, &["mail_send"]));
        let o = [run("combo", 100, &["crm_get", "mail_send"])];
        let found = detect(&refs(&h), &refs(&o));
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].kind, ShapeKind::NewToolSet);
        assert_eq!(found[0].tools, vec!["crm_get", "mail_send"]);
    }

    #[test]
    fn a_system_with_many_combinations_only_reports_new_tools() {
        let mut h = history(&["a"]);
        for i in 0..=MAX_STABLE_TOOL_SETS {
            h.push(run("v", 90, &[&format!("tool{i}")]));
        }
        let o = [
            run("combo", 100, &["a", "tool1"]),
            run("new", 101, &["brand_new"]),
        ];
        let found = detect(&refs(&h), &refs(&o));
        assert_eq!(found.len(), 1, "{found:?}");
        assert_eq!(found[0].kind, ShapeKind::NewTool);
    }

    #[test]
    fn runs_with_no_recorded_tools_do_not_make_every_tool_look_new() {
        // Upgrade day: a full history, none of it with tool names.
        let mut h = history(&["crm_get"]);
        for s in &mut h {
            s.tools_used = None;
        }
        let o = [run("after", 100, &["crm_get"])];
        assert!(detect(&refs(&h), &refs(&o)).is_empty());

        // One run short of enough recorded history is still not enough.
        let mut h = history(&["crm_get"]);
        h[0].tools_used = None;
        assert!(detect(&refs(&h), &refs(&o)).is_empty());
    }

    #[test]
    fn repeats_are_one_finding_and_name_a_few_runs() {
        let h = history(&["crm_get"]);
        let o: Vec<Sample> = (0..8)
            .map(|i| run(&format!("r{i}"), 100 + i, &["exfil"]))
            .collect();
        let found = detect(&refs(&h), &refs(&o));
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].run_count, 8);
        assert_eq!(found[0].run_ids.len(), MAX_NAMED_RUNS);
        assert_eq!(found[0].first_seen, t(100));
    }

    #[test]
    fn a_long_tool_name_still_fits_and_stays_distinct() {
        let a = new_tool_key(&format!("{}a", "x".repeat(200)));
        let b = new_tool_key(&format!("{}b", "x".repeat(200)));
        assert!(a.len() <= MAX_METRIC_KEY_LEN, "{a}");
        assert_ne!(a, b);
        assert!(a.starts_with("shape:new_tool:"));
    }
}
