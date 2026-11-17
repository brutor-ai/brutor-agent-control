// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Vocabulary guard: the leaf unit of a run is an action.
//!
//! The rename `step -> action` is only durable if it cannot silently revert.
//! `step` is now a *phase*, one level above `turn`; anything that reintroduces
//! it as the leaf unit puts us back to being incomparable with LangGraph,
//! OpenAI and Anthropic — which is the whole reason for the vocabulary.
//!
//! This scans the source rather than asserting on behaviour, because the
//! failure mode is a *name*, and no runtime test can catch a wrong name that
//! is otherwise wired correctly.
#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::Path;

/// `step_count` is legal ONLY as the asserted-phase count. Everywhere it
/// appears it must be within a few lines of that meaning, or be one of the
/// known phase-layer sites.
const PHASE_SITES: &[&str] = &[
    "src/services/run_ledger.rs", // the rollup column + struct field
    "src/middleware/run_end.rs",  // the doc comment on STEP_CONTEXT
];

fn rust_files(dir: &Path, out: &mut Vec<std::path::PathBuf>) {
    for e in fs::read_dir(dir).unwrap().flatten() {
        let p = e.path();
        if p.is_dir() {
            rust_files(&p, out);
        } else if p.extension().and_then(|s| s.to_str()) == Some("rs") {
            out.push(p);
        }
    }
}

#[test]
fn leaf_unit_is_never_called_a_step() {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }

    let mut offenders = Vec::new();
    for f in &files {
        let full = f.to_string_lossy().replace('\\', "/");
        if PHASE_SITES.contains(&full.as_str()) {
            continue;
        }
        let src = fs::read_to_string(f).unwrap();
        for (i, line) in src.lines().enumerate() {
            // The banned shapes: the old column/field name, and the old
            // drift metric key.
            if line.contains("step_count") || line.contains("STEP_COUNT") {
                offenders.push(format!("{full}:{}: {}", i + 1, line.trim()));
            }
        }
    }
    assert!(
        offenders.is_empty(),
        "the leaf unit is an ACTION, not a step. \
         `step_count` outside the phase-layer sites means the rename regressed:\n{}",
        offenders.join("\n")
    );
}

#[test]
fn hop_stayed_out_of_the_vocabulary() {
    // "hop" is retired (it was A2A's odd one out). Network vocabulary
    // arrived here by accident once; it should not arrive twice.
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    let mut offenders = Vec::new();
    for f in &files {
        let src = fs::read_to_string(f).unwrap();
        for (i, line) in src.lines().enumerate() {
            let l = line.to_ascii_lowercase();
            if l.contains("hop_count") || l.contains("a2a_hop") {
                offenders.push(format!("{}:{}", f.display(), i + 1));
            }
        }
    }
    assert!(
        offenders.is_empty(),
        "`hop` is retired; use `a2a_call_count`:\n{}",
        offenders.join("\n")
    );
}

/// Guardrail surface tokens must come from `constants::guardrail_surface`,
/// never be typed at the call site.
///
/// This is a *silent*-failure vocabulary: `guardrails::surface_enabled`
/// answers "not enabled" for a token it does not recognise, so a mistyped
/// surface does not 500 — it switches every check on that path OFF and the
/// request sails through ungoverned. `"llm"` was exactly that typo once, and
/// it cost a route its output guardrails until someone read the code. A
/// constant makes the same mistake a compile error.
///
/// Scanning the source rather than asserting on behaviour, for the same
/// reason as the tests above: the defect is a *name*, and a name that is
/// wrong but consistently wrong passes every runtime test.
#[test]
fn guardrail_surfaces_are_never_typed_inline_in_route_files() {
    // Route files still holding raw literals. This list may only ever
    // SHRINK — adding to it means a new call site skipped the constants,
    // which is the drift this test exists to stop.
    //
    // Now empty: the two files that were pending (`routes/llm.rs`,
    // `services/stream_guard.rs`) were converted once the concurrent change
    // that owned them landed. An empty allowlist is the point; if you find
    // yourself adding an entry, convert the call site instead.
    const PENDING: &[&str] = &[];

    let mut files = Vec::new();
    rust_files(Path::new("src/routes"), &mut files);
    // The Enterprise workspace too, where this repository carries it.
    // Its route modules are the Portal's and the knowledge base's
    // (`enterprise/src/routes`) and the batch API's.
    if let Some(enterprise) = roots::tree("enterprise") {
        let before = files.len();
        rust_files(&enterprise.join("src/routes"), &mut files);
        files.push(enterprise.join("src/batch/routes.rs"));
        assert!(files.len() > before + 10, "the Enterprise route modules");
    }

    let mut offenders = Vec::new();
    for f in &files {
        let full = f.to_string_lossy().to_string();
        if PENDING.contains(&full.as_str()) {
            continue;
        }
        let src = fs::read_to_string(f).unwrap();
        // Everything from `#[cfg(test)]` on is fixture data: a test that
        // asserts on the wire string SHOULD spell it out, or it is only
        // testing that a constant equals itself.
        let prod = match src.find("#[cfg(test)]") {
            Some(i) => &src[..i],
            None => &src[..],
        };
        for (i, line) in prod.lines().enumerate() {
            for token in ["\"chat_input\"", "\"chat_output\"", "\"embeddings_input\""] {
                if line.contains(token) && !line.trim_start().starts_with("//") {
                    offenders.push(format!("{full}:{}: {}", i + 1, line.trim()));
                }
            }
        }
    }
    assert!(
        offenders.is_empty(),
        "guardrail surface tokens must come from `constants::guardrail_surface` — \
         an unrecognised token silently disables every check on that surface:\n{}",
        offenders.join("\n")
    );
}
