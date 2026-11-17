// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Class guard — a run's and a segment's clock start when the first call
//! STARTED, not when it was stamped.
//!
//! A `proxy_logs` row is stamped when its call FINISHES, so elapsed time
//! measured between stamps (`MAX(timestamp) - MIN(timestamp)`) reads 0 ms
//! for a one-call run or segment and cuts the first call out of every
//! longer one. That fed every shared service's duration baseline zeros
//! (live: all 105 Fraud Screener segments were `active_ms = 0`).
//!
//! The one definition of a step's start is `run_ledger::step_started_at_sql!`.
//! A source scan, house style: the defect is a *shape* — a stamp-to-stamp
//! difference — and a new copy of it passes every behavioural test that
//! doesn't happen to seed `duration_ms`.

#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::{Path, PathBuf};

use regex_lite::Regex;

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) {
    for e in fs::read_dir(dir).unwrap().flatten() {
        let p = e.path();
        if p.is_dir() {
            rust_files(&p, out);
        } else if p.extension().and_then(|s| s.to_str()) == Some("rs") {
            out.push(p);
        }
    }
}

/// Production text: everything before the first `#[cfg(test)]`, comment
/// lines dropped.
fn production(src: &str) -> String {
    let prod = src.split("#[cfg(test)]").next().unwrap_or("");
    prod.lines()
        .filter(|l| !l.trim_start().starts_with("//"))
        .collect::<Vec<_>>()
        .join("\n")
}

/// `MAX(x.timestamp) - MIN(x.timestamp)` in any spelling: an elapsed time
/// measured between completion stamps.
fn stamp_to_stamp() -> Regex {
    Regex::new(r"(?is)max\(\s*(\w+\.)?timestamp\s*\)\s*-\s*min\(\s*(\w+\.)?timestamp\s*\)").unwrap()
}

fn read(path: &str) -> String {
    fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join(path)).unwrap()
}

#[test]
fn the_detector_flags_the_old_shape() {
    // The exact expression `run_ledger::AGGREGATE_SQL` used before the fix.
    let old = "GREATEST(0, CAST(EXTRACT(EPOCH FROM (MAX(pl.timestamp) - MIN(pl.timestamp))) * 1000 AS BIGINT))";
    assert!(stamp_to_stamp().is_match(old));
    assert!(stamp_to_stamp().is_match("max(timestamp)\n  -  MIN( timestamp )"));
    assert!(
        !stamp_to_stamp().is_match("MAX(pl.timestamp) AS ended_at, MIN(pl.timestamp) AS first_at")
    );
}

#[test]
fn no_elapsed_time_is_measured_between_stamps() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"));
    let mut files = Vec::new();
    rust_files(&root.join("src"), &mut files);
    // The Enterprise workspace too, where this repository carries it.
    if let Some(enterprise) = roots::tree("enterprise") {
        let before = files.len();
        rust_files(&root.join(enterprise).join("src"), &mut files);
        assert!(files.len() > before + 20, "the Enterprise sources");
    }
    let re = stamp_to_stamp();
    let hits: Vec<String> = files
        .iter()
        .filter(|p| re.is_match(&production(&fs::read_to_string(p).unwrap())))
        .map(|p| p.strip_prefix(root).unwrap().display().to_string())
        .collect();
    assert!(
        hits.is_empty(),
        "elapsed time measured between completion stamps (use \
         run_ledger::step_started_at_sql! for the start): {hits:?}"
    );
}

#[test]
fn the_run_and_its_segments_start_from_the_one_definition() {
    let ledger = production(&read("src/services/run_ledger.rs"));
    assert!(ledger.contains("macro_rules! step_started_at_sql"));
    assert!(
        ledger.contains("MIN(\"#,\n    step_started_at_sql!(),\n    r#\")")
            && ledger.matches("step_started_at_sql!(),").count() >= 2,
        "run started_at and duration_ms must both start at the first call's start"
    );

    let segments = production(&read("src/services/run_segments.rs"));
    assert!(
        segments.contains("crate::services::run_ledger::step_started_at_sql!()"),
        "PARTS_SQL must derive active_from from the shared step start"
    );
    // Active time and its hold clipping both run from `active_from`.
    assert!(segments.contains(".bind(d.active_from)"));
    assert!(segments.contains("let span_ms = active_span_ms(&d);"));
    assert!(segments.contains("(d.last_action_at - d.active_from)"));
}
