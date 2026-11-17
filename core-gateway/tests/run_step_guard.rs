// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: every governed surface states whether its step counts against the
//! run's action ceiling, and the statement is the one written down here.
//!
//! The bug class: a new surface lets a run do work that is not a model call
//! (a tool, a skill, a hop to another agent) and the run's action ceiling
//! (`max_actions_per_run`) never hears of it, so a run that only ever uses
//! that surface has no ceiling at all. Or the opposite: one request passes
//! two surfaces and is counted twice.
//!
//! A surface asks "may this run take another step" through
//! `run_controls::check`, which takes the step's `StepKind`; a skill
//! execution is started through `skill_orchestrator::execute_skill`, which
//! takes one too. So the compiler already refuses a call that does not say.
//! This guard pins WHAT each call says: every production call of either
//! function, in both editions, must be in `STEPS` with the kind it passes
//! and why. A new call site fails until it is listed, and a listed site that
//! changes its answer fails until the list (and its reason) changes with it.
//!
//! A site that admits an action must also end the admission where its step
//! is dispatched (`StepAdmitted::commit`, `into_hold`), or the unit is given
//! back when the request ends and the step is never counted.
//!
//! Behaviour is pinned by `tests/integration/run_actions.rs`.
#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::{Path, PathBuf};

const CHECK: &str = "run_controls::check(";
const EXECUTE: &str = "skill_orchestrator::execute_skill(";

/// `(file, call, the StepKind it passes, how many such calls, why)`.
///
/// `caller's` is the one call that passes on what its own caller stated
/// (`execute_skill`'s `step` argument).
const STEPS: [(&str, &str, &str, usize, &str); 9] = [
    (
        "src/services/llm_preflight.rs",
        CHECK,
        "ModelCall",
        1,
        "every model surface (chat, responses, messages, embeddings, media): counted by the \
         model-call, token and cost terms in the call's own admission, never as an action",
    ),
    (
        "src/routes/mcp.rs",
        CHECK,
        "Action",
        2,
        "an MCP capability call to an external server, and a system server's tool call: each \
         request is one action",
    ),
    (
        "src/routes/a2a.rs",
        CHECK,
        "Action",
        2,
        "an outbound hop to a remote agent, and an inbound task: each is one action",
    ),
    (
        "src/routes/a2a_v1.rs",
        CHECK,
        "Action",
        1,
        "a message to an agent (the verbs that do work): one action",
    ),
    (
        "src/services/skill_orchestrator.rs",
        CHECK,
        "caller's",
        1,
        "a skill execution is an action of its own or part of the request that started it; \
         whoever calls execute_skill says which",
    ),
    (
        "src/routes/skills.rs",
        EXECUTE,
        "Action",
        1,
        "the skill route received the request: the execution is the action",
    ),
    (
        "src/services/system_server.rs",
        EXECUTE,
        "PartOfAction",
        1,
        "the MCP route admitted the tool call that runs the script",
    ),
    (
        "src/routes/a2a.rs",
        EXECUTE,
        "PartOfAction",
        1,
        "the inbound route admitted the task whose handler is the skill",
    ),
    (
        "src/routes/a2a_v1.rs",
        EXECUTE,
        "PartOfAction",
        1,
        "the message was admitted when the request was resolved",
    ),
];

/// What ends an admitted action at dispatch.
const DISPATCHED: [&str; 3] = [".commit().await", ".into_hold()", "step_dispatched().await"];

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

/// Production code only: everything before the file's test module, without
/// comment lines.
fn production(src: &str) -> String {
    let mut out = String::new();
    let lines: Vec<&str> = src.lines().collect();
    for (i, line) in lines.iter().enumerate() {
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
        out.push_str(line);
        out.push('\n');
    }
    out
}

/// The `StepKind` each call of `call` in `code` passes: the variant it
/// names, `caller's` when it passes a `step` it was given, `unstated`
/// otherwise.
fn kinds(code: &str, call: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut rest = code;
    while let Some(at) = rest.find(call) {
        let args_from = at + call.len();
        // The call's arguments: up to the parenthesis that closes it.
        let mut depth = 1usize;
        let mut end = rest.len();
        for (i, c) in rest[args_from..].char_indices() {
            match c {
                '(' => depth += 1,
                ')' => {
                    depth -= 1;
                    if depth == 0 {
                        end = args_from + i;
                        break;
                    }
                }
                _ => {}
            }
        }
        let args = &rest[args_from..end];
        let kind = match args.find("StepKind::") {
            Some(k) => args[k + "StepKind::".len()..]
                .chars()
                .take_while(|c| c.is_alphanumeric())
                .collect(),
            None if args
                .split(',')
                .any(|a| a.trim() == "step" || a.trim() == "kind: step") =>
            {
                "caller's".to_string()
            }
            None => "unstated".to_string(),
        };
        out.push(kind);
        rest = &rest[end..];
    }
    out
}

/// `(file, call, kind) -> count` for one tree, with paths as `STEPS` names
/// them.
fn found_in(root: &Path, prefix: &str) -> Vec<(String, &'static str, String, usize)> {
    let mut files = Vec::new();
    rust_files(&root.join("src"), &mut files);
    files.sort();
    let mut out: Vec<(String, &'static str, String, usize)> = Vec::new();
    for path in files {
        let rel = path.strip_prefix(root).unwrap().to_string_lossy();
        let file = format!("{prefix}{rel}");
        let code = production(&fs::read_to_string(&path).unwrap());
        for call in [CHECK, EXECUTE] {
            for kind in kinds(&code, call) {
                match out
                    .iter_mut()
                    .find(|(f, c, k, _)| *f == file && *c == call && *k == kind)
                {
                    Some(entry) => entry.3 += 1,
                    None => out.push((file.clone(), call, kind, 1)),
                }
            }
        }
    }
    out
}

fn found() -> Vec<(String, &'static str, String, usize)> {
    let community = roots::tree("community").expect("the Community tree");
    let mut out = found_in(&community, "");
    if let Some(enterprise) = roots::tree("enterprise") {
        out.extend(found_in(&enterprise, "enterprise/"));
    }
    out
}

#[test]
fn every_governed_surface_states_whether_its_step_counts() {
    let found = found();
    let mut problems = Vec::new();
    for (file, call, kind, n) in &found {
        match STEPS
            .iter()
            .find(|(f, c, k, _, _)| f == file && c == call && k == kind)
        {
            Some((_, _, _, expected, _)) if expected == n => {}
            Some((_, _, _, expected, _)) => problems.push(format!(
                "{file}: {n} call(s) of `{call}` pass `{kind}`, the list says {expected}"
            )),
            None => problems.push(format!(
                "{file}: `{call}` passing `{kind}` is not in the list"
            )),
        }
    }
    for (file, call, kind, expected, _) in STEPS.iter() {
        if !found
            .iter()
            .any(|(f, c, k, _)| f == file && c == call && k == kind)
        {
            problems.push(format!(
                "{file}: the list says {expected} call(s) of `{call}` pass `{kind}`; there is none"
            ));
        }
    }
    assert!(
        problems.is_empty(),
        "a surface that lets a run take a step must say whether the step counts against the \
         run's action ceiling, and tests/run_step_guard.rs must say so with the reason \
         (one request is one action, on the surface that received it; a model call is none):\n{}",
        problems.join("\n")
    );
}

#[test]
fn every_statement_carries_its_reason() {
    for (file, call, kind, _, why) in STEPS {
        assert!(
            why.len() > 20,
            "{file}: `{call}` passing `{kind}` has no reason"
        );
    }
}

#[test]
fn an_admitted_action_is_ended_where_its_step_is_dispatched() {
    let community = roots::tree("community").expect("the Community tree");
    for (file, call, kind, _, _) in STEPS {
        if call != CHECK || kind == "ModelCall" {
            continue;
        }
        let code = production(&fs::read_to_string(community.join(file)).unwrap());
        let after = &code[code.find(CHECK).expect("listed")..];
        assert!(
            DISPATCHED.iter().any(|d| after.contains(d)),
            "{file} admits an action and never ends the admission at dispatch \
             (StepAdmitted::commit or into_hold): the unit is given back when the request \
             ends, and the step is never counted"
        );
    }
}

#[test]
fn the_scan_reads_the_kind_each_call_passes() {
    let code = r#"
        let step = crate::services::run_controls::check(
            &state,
            crate::services::run_controls::Step {
                tenant_id: &tenant_id,
                group_id: group(),
                kind: crate::services::run_controls::StepKind::Action,
                starter: starter.as_deref(),
            },
        )
        .await;
        match crate::services::run_controls::check(state, Step { run_id, kind: step, cid }).await {}
        if let Err(r) = run_controls::check(state, Step { g: g(a, b), kind: StepKind::ModelCall }).await {}
    "#;
    assert_eq!(kinds(code, CHECK), ["Action", "caller's", "ModelCall"]);
    let code = "skill_orchestrator::execute_skill(state, auth, &req, cid, run, \
                crate::services::run_controls::StepKind::PartOfAction).await";
    assert_eq!(kinds(code, EXECUTE), ["PartOfAction"]);
}

/// The shapes the guard exists for: a new surface that asks the run controls
/// (in a file the list does not name), and a listed surface that changed its
/// answer.
#[test]
fn the_guard_catches_a_surface_that_is_not_listed() {
    let dir = std::env::temp_dir().join(format!("run-step-guard-{}", std::process::id()));
    let src = dir.join("src/routes");
    fs::create_dir_all(&src).unwrap();
    fs::write(
        src.join("new_surface.rs"),
        "async fn handle() {\n    let _ = crate::services::run_controls::check(\n        s, t, g, \
         r, d,\n        StepKind::ModelCall,\n        cid,\n    )\n    .await;\n}\n\
         // run_controls::check(a comment is no call)\n\
         #[cfg(test)]\nmod tests {\n    fn t() { run_controls::check(x, StepKind::Action); }\n}\n",
    )
    .unwrap();
    let found = found_in(&dir, "");
    fs::remove_dir_all(&dir).unwrap();
    assert_eq!(
        found,
        [(
            "src/routes/new_surface.rs".to_string(),
            CHECK,
            "ModelCall".to_string(),
            1
        )]
    );
    assert!(
        !STEPS
            .iter()
            .any(|(f, _, _, _, _)| *f == "src/routes/new_surface.rs"),
        "the list would let it through"
    );
}
