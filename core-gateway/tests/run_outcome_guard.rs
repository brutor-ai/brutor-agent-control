// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Class guard — ONE classification, ONE error-cause derivation.
//!
//! The control plane pins the same rule for `app/` (its down-rule guard
//! pattern): a run's success is decided only by `ai_system_outcomes`, and in
//! the core only by `services/run_outcome.rs`. The defect this stops is a
//! second copy — `terminal_state IN (completed, completed_degraded)` read as
//! "success" somewhere — which silently disagrees with the contract (an empty
//! poll counted as work, an undeclared outcome counted as success). Equally,
//! an error cause derived anywhere else is a second, drifting definition of
//! "why the run failed".
//!
//! A source scan, house style: the defect is a *shape*, and a second copy
//! that happens to agree today passes every behavioural test.

#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::{Path, PathBuf};

/// The one module allowed to decide success and derive causes.
const THE_MODULE: &str = "src/services/run_outcome.rs";

/// Files that may NAME the completed terminal states for reasons other than
/// deciding success: the ledger defines and derives the terminal state.
const TERMINAL_STATE_OWNERS: &[&str] = &["src/services/run_ledger.rs"];

/// The only callers of the derivation functions: the ledger (run cause) and
/// the segments (segment cause), both at finalize.
const CAUSE_CALLERS: &[&str] = &["src/services/run_ledger.rs", "src/services/run_segments.rs"];

/// Files that may READ an error-cause constant (never assign one): the inbox
/// skips a run the gateway refused, as it skipped `blocked_policy`.
const CAUSE_READERS: &[&str] = &["src/services/assurance_inbox.rs"];

/// Files that may READ a run class `run_outcome::run_class` returned (never
/// decide one): drift buckets runs by class.
const CLASS_READERS: &[&str] = &["src/services/drift/baseline.rs"];

/// `rate_limited` is also an unrelated, older API error code (portal
/// evidence renders, batch provider errors); its constant is still pinned
/// by `the_module_exists_and_owns_the_vocabularies`.
const NOT_SCANNED_INLINE: &[&str] = &["rate_limited"];

const ERROR_CAUSES: &[&str] = &[
    "blocked_by_policy",
    "rate_limited",
    "model_unavailable",
    "model_rejected",
    "tool_failed",
    "skill_failed",
    "agent_unreachable",
    "delegate_deviated",
    "iteration_limit",
    "client_reported",
];

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
/// lines dropped. Test modules may spell vocabularies out on purpose.
fn production(src: &str) -> String {
    let prod = src.split("#[cfg(test)]").next().unwrap_or("");
    prod.lines()
        .filter(|l| {
            let t = l.trim_start();
            !t.starts_with("//") && !t.starts_with("///") && !t.starts_with("//!")
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// Every violation in one file's production text.
fn violations(path: &str, src: &str) -> Vec<String> {
    let mut out = Vec::new();
    if path == THE_MODULE || path.ends_with("_tests.rs") {
        return out;
    }
    let prod = production(src);
    for (i, line) in prod.lines().enumerate() {
        let at = || format!("{path}:{}: {}", i + 1, line.trim());
        // 1. Deciding success from the terminal state.
        if !TERMINAL_STATE_OWNERS.contains(&path)
            && (line.contains("COMPLETED_DEGRADED") || line.contains("\"completed_degraded\""))
        {
            out.push(format!("decides success from terminal_state — {}", at()));
        }
        if (line.contains("run_class::SUCCESS") || line.contains("run_class::NO_WORK"))
            && !CLASS_READERS.contains(&path)
        {
            out.push(format!("names a run class outside run_outcome — {}", at()));
        }
        // 2. Deriving an error cause.
        for f in [
            "derive_error_cause(",
            "segment_error_cause(",
            "cause_of_call(",
        ] {
            if line.contains(f) && !CAUSE_CALLERS.contains(&path) {
                out.push(format!("derives an error cause — {}", at()));
            }
        }
        if line.contains("error_cause::") && !CAUSE_READERS.contains(&path) {
            out.push(format!("names an error-cause constant — {}", at()));
        }
        for c in ERROR_CAUSES
            .iter()
            .filter(|c| !NOT_SCANNED_INLINE.contains(c))
        {
            if line.contains(&format!("\"{c}\"")) {
                out.push(format!("types an error cause inline — {}", at()));
            }
        }
    }
    out
}

#[test]
fn no_module_but_run_outcome_decides_success_or_derives_a_cause() {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    let mut offenders = Vec::new();
    for f in &files {
        let path = f.to_string_lossy().replace('\\', "/");
        let src = fs::read_to_string(f).unwrap();
        offenders.extend(violations(&path, &src));
    }
    assert!(
        offenders.is_empty(),
        "success and error causes are decided ONLY in {THE_MODULE} \
         (mirrored by the control plane's ai_system_outcomes):\n{}",
        offenders.join("\n")
    );
}

#[test]
fn the_module_exists_and_owns_the_vocabularies() {
    let src = fs::read_to_string(THE_MODULE).unwrap();
    for needle in [
        "pub fn run_class(",
        "pub fn derive_error_cause(",
        "pub fn segment_error_cause(",
        "pub fn cause_of_call(",
        "pub fn judge(",
    ] {
        assert!(src.contains(needle), "{THE_MODULE} lost `{needle}`");
    }
    for c in ERROR_CAUSES {
        assert!(
            src.contains(&format!("\"{c}\"")),
            "{c} not defined in {THE_MODULE}"
        );
    }
}

/// Proven against the shapes it forbids (house rule: a guard that cannot
/// fail is not a guard).
#[test]
fn the_guard_catches_each_forbidden_shape() {
    let p = "src/services/somewhere.rs";
    let cases = [
        r#"let ok = matches!(ts, terminal::COMPLETED | terminal::COMPLETED_DEGRADED);"#,
        r#"if s == "completed_degraded" { success += 1 }"#,
        r#"let c = run_class::SUCCESS;"#,
        r#"let cause = derive_error_cause(ts, None, false);"#,
        r#"let cause = run_outcome::error_cause::MODEL_UNAVAILABLE;"#,
        r#"let cause = "model_unavailable";"#,
    ];
    for c in cases {
        assert!(!violations(p, c).is_empty(), "guard missed: {c}");
    }
    // ...and lets the legitimate shapes through.
    assert!(violations(THE_MODULE, cases[0]).is_empty());
    assert!(violations("src/services/run_ledger.rs", cases[0]).is_empty());
    assert!(violations("src/services/run_ledger.rs", cases[3]).is_empty());
    assert!(violations("src/services/assurance_inbox.rs", cases[4]).is_empty());
    assert!(violations(p, "// \"model_unavailable\" in a comment").is_empty());
    assert!(violations(p, "#[cfg(test)]\nlet c = \"model_unavailable\";").is_empty());
}
