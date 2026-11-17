// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Dispatch-without-settle guard.
//!
//! `skill_orchestrator::execute_skill` authorises, gates and records a
//! RUNNING execution — it does not run the skill. RUNNING rows count against
//! the group's `max_concurrent_executions`, so a caller that dispatches and
//! never settles leaks a slot for good. Both inbound A2A paths did exactly
//! that ("Skill '…' dispatched … status=RUNNING") until every skill in the
//! group was refused.
//!
//! Rule, over `src/` (test code removed): every function that calls
//! `execute_skill(` must, after the call, either hand the execution to
//! `skill_orchestrator::run_and_settle(` (which runs it and settles it) or
//! settle it itself with BOTH
//! `complete_execution(` and `fail_execution(` (the MCP `run_script` tool,
//! which runs one named script rather than the whole skill).
#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::{Path, PathBuf};

/// Callers of `execute_skill` in `src/` today (REST, MCP system server, A2A
/// v1, legacy A2A). A drop means a caller was missed by the scan, not fixed.
const MIN_CALL_SITES: usize = 4;

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

/// End (exclusive) of the brace-balanced block opening at the first `{` at
/// or after `from`.
fn block_end(src: &str, from: usize) -> usize {
    let Some(open) = src[from..].find('{').map(|o| from + o) else {
        return src.len();
    };
    let mut depth = 0usize;
    for (i, c) in src[open..].char_indices() {
        match c {
            '{' => depth += 1,
            '}' => {
                depth -= 1;
                if depth == 0 {
                    return open + i + 1;
                }
            }
            _ => {}
        }
    }
    src.len()
}

/// `src` without `#[cfg(test)]` items and with whole-line comments blanked
/// (so a doc comment naming a call is not mistaken for one; line numbers
/// before the first test item are kept).
fn production_code(src: &str) -> String {
    let code: String = src
        .lines()
        .map(|l| {
            if l.trim_start().starts_with("//") {
                ""
            } else {
                l
            }
        })
        .collect::<Vec<_>>()
        .join("\n");
    let mut out = String::with_capacity(code.len());
    let mut rest = code.as_str();
    while let Some(at) = rest.find("#[cfg(test)]") {
        out.push_str(&rest[..at]);
        let after = &rest[at..];
        let semi = after.find(';').unwrap_or(after.len());
        if after.find('{').is_some_and(|o| o < semi) {
            rest = &after[block_end(after, 0)..];
        } else {
            rest = &after[(semi + 1).min(after.len())..];
        }
    }
    out.push_str(rest);
    out
}

/// Call sites of `execute_skill(` (not its definition) and, for each, the
/// rest of the enclosing function after the call.
fn call_sites(code: &str) -> Vec<(usize, &str)> {
    let mut sites = Vec::new();
    for (at, _) in code.match_indices("execute_skill(") {
        let before = &code[..at];
        if before.ends_with("fn ") || before.ends_with('_') {
            continue; // the definition, or a longer identifier
        }
        // Enclosing function: the last `fn ` before the call.
        let Some(fn_at) = before.rfind("fn ") else {
            continue;
        };
        let end = block_end(code, fn_at);
        if end <= at {
            continue; // the call is not inside that function's body
        }
        let line = before.matches('\n').count() + 1;
        sites.push((line, &code[at..end]));
    }
    sites
}

fn settles(rest_of_fn: &str) -> bool {
    rest_of_fn.contains("run_and_settle(")
        || (rest_of_fn.contains("complete_execution(") && rest_of_fn.contains("fail_execution("))
}

/// `(line, settles)` for every call site in `src`.
fn scan(src: &str) -> Vec<(usize, bool)> {
    let code = production_code(src);
    call_sites(&code)
        .into_iter()
        .map(|(line, rest)| (line, settles(rest)))
        .collect()
}

#[test]
fn every_execute_skill_caller_settles_the_execution() {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    let mut total = 0usize;
    let mut offenders = Vec::new();
    for f in &files {
        let src = fs::read_to_string(f).unwrap();
        for (line, ok) in scan(&src) {
            total += 1;
            if !ok {
                offenders.push(format!("{}:{line}", f.display()));
            }
        }
    }
    assert!(
        offenders.is_empty(),
        "execute_skill dispatches a RUNNING execution; these callers never run and settle it \
         (call skill_orchestrator::run_and_settle): {offenders:#?}"
    );
    assert!(
        total >= MIN_CALL_SITES,
        "found {total} execute_skill call sites, expected at least {MIN_CALL_SITES} — the scan \
         is missing callers"
    );
}

// ---- the guard, proven against the shape it forbids -----------------------

/// The pre-fix legacy A2A `dispatch_skill`: dispatch, then answer.
const DISPATCH_WITHOUT_SETTLE: &str = r#"
async fn dispatch_skill(state: &S, auth: &A, req: &R) -> Result<Value, SkillError> {
    // run_and_settle( in a comment does not count
    let result =
        skill_orchestrator::execute_skill(state, auth, req, "c", run).await?;
    Ok(json!({ "execution_id": result.execution_id, "status": format!("{}", 1) }))
}

async fn later(state: &S) {
    skill_orchestrator::run_and_settle(state).await;
}
"#;

const DISPATCH_THEN_SETTLE: &str = r#"
async fn dispatch_skill(state: &S, auth: &A, req: &R) -> Result<Value, SkillError> {
    let mut result =
        skill_orchestrator::execute_skill(state, auth, req, "c", run).await?;
    let out = skill_orchestrator::run_and_settle(state, auth, "t", &mut result, None).await;
    Ok(json!({ "output": out }))
}
"#;

const SETTLES_ITSELF: &str = r#"
async fn run_script(state: &S) -> Value {
    let orch = match skill_orchestrator::execute_skill(state, a, r, c, run).await { Ok(r) => r, Err(_) => return v };
    if ok { let _ = skill_orchestrator::complete_execution(db, &orch.execution_id, t).await; }
    else { let _ = skill_orchestrator::fail_execution(db, &orch.execution_id, t, "e", "m").await; }
    v
}
"#;

#[test]
fn guard_flags_a_dispatch_that_is_never_settled() {
    assert_eq!(scan(DISPATCH_WITHOUT_SETTLE), vec![(5, false)]);
}

#[test]
fn guard_accepts_run_and_settle_and_self_settling_callers() {
    assert_eq!(scan(DISPATCH_THEN_SETTLE), vec![(4, true)]);
    assert_eq!(scan(SETTLES_ITSELF), vec![(3, true)]);
}

#[test]
fn guard_ignores_the_definition_and_test_code() {
    let src = r#"
pub async fn execute_skill(state: &S) -> R { body() }
#[cfg(test)]
mod tests {
    async fn t() { skill_orchestrator::execute_skill(s, a, r, c, run).await; }
}
"#;
    assert!(scan(src).is_empty());
}
