// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: every step a run takes is judged against its data scope.
//!
//! The bug class: a surface lets a run do work (it asks the run controls
//! "may this run take another step") but never compares the step's arguments
//! with the run's declared scope, so a run bound to customer C0001 can name
//! C0002 there. The check is on MCP tool calls, skills
//! and A2A messages alike.
//!
//! Every non-test function in `FILES` that calls `run_controls::check(` must
//! also call the data-scope gate, or be listed in `EXEMPT` with the reason
//! its step names no record the gate could judge. Behaviour is pinned by
//! `tests/integration/data_scope.rs` (MCP) and
//! `tests/integration/data_scope_surfaces.rs` (skills, A2A v1, legacy A2A).

use std::fs;

const FILES: [&str; 6] = [
    "src/routes/mcp.rs",
    "src/routes/a2a.rs",
    "src/routes/a2a_v1.rs",
    "src/routes/skills.rs",
    "src/services/skill_orchestrator.rs",
    "src/services/llm_preflight.rs",
];

const STEP: &str = "run_controls::check(";

/// `gate_recording` is the gate with a choice of what it records (a preview
/// records a refusal only); it judges the same way.
const GATES: [&str; 3] = [
    "data_scope::gate(",
    "data_scope::gate_recording(",
    "ds::gate(",
];

/// `(file, function, why the step names no record the gate could judge)`.
const EXEMPT: [(&str, &str, &str); 2] = [
    (
        "src/services/llm_preflight.rs",
        "run_preflight",
        "a model call names no record; the tool calls it asks for are judged where they run",
    ),
    (
        "src/routes/a2a.rs",
        "inbound",
        "a remote agent's call: its only working handler is a skill, judged in execute_skill",
    ),
];

/// `(name, body)` of each top-level function before the first test module,
/// without line comments.
fn functions(src: &str) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    let lines: Vec<&str> = src.lines().collect();
    for (i, line) in lines.iter().copied().enumerate() {
        if line.starts_with("#[cfg(test)]")
            && lines
                .get(i + 1)
                .is_some_and(|next| next.trim_start().starts_with("mod "))
        {
            break;
        }
        if line.trim_start().starts_with("//") {
            continue;
        }
        let top_level = !line.starts_with(' ') && !line.starts_with('\t');
        if top_level && let Some(at) = line.find("fn ") {
            let prefix = &line[..at];
            if prefix
                .split_whitespace()
                .all(|w| w == "pub" || w.starts_with("pub(") || w == "async")
            {
                let name: String = line[at + 3..]
                    .chars()
                    .take_while(|c| c.is_alphanumeric() || *c == '_')
                    .collect();
                out.push((name, String::new()));
                continue;
            }
        }
        if let Some((_, body)) = out.last_mut() {
            body.push_str(line);
            body.push('\n');
        }
    }
    out
}

fn violations(file: &str, src: &str) -> Vec<String> {
    functions(src)
        .into_iter()
        .filter(|(_, body)| body.contains(STEP))
        .filter(|(_, body)| !GATES.iter().any(|g| body.contains(g)))
        .filter(|(name, _)| !EXEMPT.iter().any(|(f, n, _)| *f == file && n == name))
        .map(|(name, _)| format!("{file}::{name}"))
        .collect()
}

#[test]
fn every_step_is_judged_against_the_runs_data_scope() {
    let mut found = Vec::new();
    for file in FILES {
        let src = fs::read_to_string(file).unwrap_or_else(|e| panic!("{file}: {e}"));
        found.extend(violations(file, &src));
    }
    assert!(
        found.is_empty(),
        "these let a run take a step without judging its arguments against the \
         run's data scope; call services::data_scope::gate or add an \
         EXEMPT entry saying why the step names no record:\n{}",
        found.join("\n")
    );
}

#[test]
fn every_exemption_still_names_a_step() {
    for (file, name, why) in EXEMPT {
        assert!(!why.is_empty());
        let src = fs::read_to_string(file).unwrap();
        assert!(
            functions(&src)
                .iter()
                .any(|(n, body)| n == name && body.contains(STEP)),
            "stale exemption {file}::{name}"
        );
    }
}

/// The shape that shipped: the skill pipeline asked the run controls and
/// went on to run the skill; the fixed one also asks the gate.
#[test]
fn the_rule_catches_the_shape_that_shipped() {
    let leaking = r#"
pub async fn execute_skill(state: S, run: &RunScope) -> Result<R, E> {
    if let Some(refusal) = crate::services::run_controls::check(state, t, g, &run.run_id, 0).await {
        return Err(refusal.into());
    }
    run_it().await
}
"#;
    assert_eq!(violations("x.rs", leaking), vec!["x.rs::execute_skill"]);
    let fixed = leaking.replace(
        "    run_it()",
        "    if let Some(r) = crate::services::data_scope::gate(db, call).await { return Err(r.into()); }\n    run_it()",
    );
    assert!(violations("x.rs", &fixed).is_empty());
    let commented = leaking.replace(
        "    run_it()",
        "    // data_scope::gate( later\n    run_it()",
    );
    assert_eq!(violations("x.rs", &commented), vec!["x.rs::execute_skill"]);
    let recording = leaking.replace(
        "    run_it()",
        "    if let Some(r) = crate::services::data_scope::gate_recording(db, call, rec).await { return Err(r.into()); }\n    run_it()",
    );
    assert!(violations("x.rs", &recording).is_empty());
}
