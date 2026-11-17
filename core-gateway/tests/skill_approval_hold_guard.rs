// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A skill that needs a person is a hold in the approval queue.
//!
//! A skill whose execution mode is `approval_required` is held by
//! `agent_approval::require_or_enqueue`, like a call held by an agent grant
//! or an argument-policy band: an approval request a person answers, and a
//! token the caller retries with. There is no second mechanism in which the
//! execution row itself waits for approval: such a row is a request nobody
//! can answer.
//!
//! Rule, over `src/` and the Enterprise `src/`: none of the names of that
//! second mechanism appears.
#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::{Path, PathBuf};

/// A name, and whether it must stand as a whole identifier (`requires_approval`
/// is forbidden; `band_requires_approval`, another thing, is not).
const FORBIDDEN: [(&str, bool); 7] = [
    ("requires_approval", true),
    ("Settlement::Parked", false),
    ("pending_approval", true),
    ("approval_expires_at", true),
    ("parked_execution_id", true),
    ("stamp_parked_skill_execution", true),
    ("expire_pending_skill_approvals", true),
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

fn is_identifier_char(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// The forbidden names `src` contains, each with its line number.
fn offences(src: &str) -> Vec<(usize, &'static str)> {
    let mut found = Vec::new();
    for (name, whole) in FORBIDDEN {
        for (at, _) in src.match_indices(name) {
            let before = src[..at].chars().next_back();
            let after = src[at + name.len()..].chars().next();
            if whole
                && (before.is_some_and(is_identifier_char) || after.is_some_and(is_identifier_char))
            {
                continue;
            }
            found.push((src[..at].lines().count().max(1), name));
        }
    }
    found.sort();
    found
}

#[test]
fn no_execution_row_waits_for_an_approval() {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    assert!(files.len() > 100, "the scan found {} files", files.len());
    let mut found = Vec::new();
    for file in files {
        let src = fs::read_to_string(&file).expect("read");
        for (line, name) in offences(&src) {
            found.push(format!("{}:{line}: `{name}`", file.display()));
        }
    }
    assert!(
        found.is_empty(),
        "a skill that needs a person is held by agent_approval::require_or_enqueue, \
         never recorded as an execution that waits:\n{}",
        found.join("\n")
    );
}

#[test]
fn the_guard_sees_the_shapes_it_forbids() {
    let parked = r#"
    if result.requires_approval || result.execution_status == "PENDING" {
        return Settlement::Parked;
    }
    "status": "pending_approval",
    .bind(approval_expires_at(parked_window_seconds, now))
    WHERE approval_expires_at < NOW()
    note_parked(v.parked_execution_id.as_deref());
    stamp_parked_skill_execution(&mut log, exec);
    expire_pending_skill_approvals(&db).await
    "#;
    let names: Vec<&str> = offences(parked).into_iter().map(|(_, n)| n).collect();
    for (name, _) in FORBIDDEN {
        assert!(names.contains(&name), "`{name}` not seen in {names:?}");
    }
    assert_eq!(offences(parked)[0].0, 2, "line numbers start at 1");
}

#[test]
fn the_guard_leaves_other_names_alone() {
    let fine = r#"
    band_requires_approval: bool,
    match (band_requires_approval, &resolution.access) {}
    let autonomy_requires_approval = level == "approval_required";
    routes::approvals::expire_pending_approvals(&db).await
    "#;
    assert_eq!(offences(fine), []);
}

// ---------------------------------------------------------------------------
// The preview refuses what the execution refuses
// ---------------------------------------------------------------------------

/// The gates that can refuse a skill execution. Each is called from
/// `judge_execution` and from nowhere else in the orchestrator: the
/// execution and its preview both go through that one function, so a front
/// gate cannot hold a call the execution would refuse.
const REFUSING_GATES: [&str; 9] = [
    "resolve_skill(",
    "resolve_group(",
    "check_skill_group_access(",
    "check_permissions(",
    "execution_mode_gate(",
    "run_controls::check(",
    "data_scope::gate_recording(",
    "check_input_policy(",
    "autonomy::enforce_for_group(",
];

/// The body of `async fn name(` in `src`: from its opening brace to the
/// next line that is a lone closing brace.
fn async_fn_body<'a>(src: &'a str, name: &str) -> Option<&'a str> {
    let at = src.find(&format!("async fn {name}("))?;
    let rest = &src[at..];
    let open = rest.find("{\n")?;
    let end = rest.find("\n}\n")?;
    Some(&rest[open..end])
}

/// What is wrong with how `src` (the orchestrator) judges an execution and
/// previews one.
fn preview_problems(src: &str) -> Vec<String> {
    let mut problems = Vec::new();
    let Some(judge) = async_fn_body(src, "judge_execution") else {
        return vec!["`judge_execution` is gone".to_string()];
    };
    for gate in REFUSING_GATES {
        if !judge.contains(gate) {
            problems.push(format!("`judge_execution` does not call `{gate}`"));
        }
    }
    for caller in ["execute_skill", "skill_hold"] {
        let Some(body) = async_fn_body(src, caller) else {
            problems.push(format!("`{caller}` is gone"));
            continue;
        };
        if !body.contains("judge_execution(") {
            problems.push(format!(
                "`{caller}` does not judge through `judge_execution`"
            ));
        }
        for gate in REFUSING_GATES {
            if body.contains(gate) {
                problems.push(format!(
                    "`{caller}` calls `{gate}` itself: a gate one of the two reads and the \
                     other does not"
                ));
            }
        }
        if !body.contains("person_gate(") {
            problems.push(format!(
                "`{caller}` does not decide the hold through `person_gate`"
            ));
        }
    }
    problems
}

#[test]
fn the_preview_and_the_execution_judge_through_one_function() {
    let src = fs::read_to_string("src/services/skill_orchestrator.rs").expect("read");
    let code = src
        .split("\n#[cfg(test)]\nmod tests")
        .next()
        .unwrap_or(&src);
    let problems = preview_problems(code);
    assert!(
        problems.is_empty(),
        "src/services/skill_orchestrator.rs: the preview of a skill's gates must refuse what \
         the execution refuses:\n{}",
        problems.join("\n")
    );
}

#[test]
fn the_preview_guard_catches_a_preview_that_reads_fewer_gates() {
    let judge = format!(
        "async fn judge_execution(x: X) -> Y {{\n    {}\n}}\n",
        REFUSING_GATES.join("x);\n    ") + "x);"
    );
    let through = |name: &str| {
        format!("async fn {name}(x: X) -> Y {{\n    judge_execution(x);\n    person_gate(x);\n}}\n")
    };
    let good = format!(
        "{judge}{}{}",
        through("execute_skill"),
        through("skill_hold")
    );
    assert_eq!(preview_problems(&good), Vec::<String>::new());

    // The shape that shipped: the preview read some gates itself and left
    // out the run's controls, the data scope and the autonomy.
    let own_reading = "async fn skill_hold(x: X) -> Y {\n    resolve_skill(x);\n    \
                       resolve_group(x);\n    check_skill_group_access(x);\n    \
                       check_permissions(x);\n    execution_mode_gate(x);\n    \
                       check_input_policy(x);\n    person_gate(x);\n}\n";
    let shipped = format!("{judge}{}{own_reading}", through("execute_skill"));
    let problems = preview_problems(&shipped);
    assert_eq!(problems.len(), 7, "{problems:?}");

    // A gate dropped from the one function.
    let fewer = good.replace("    data_scope::gate_recording(x);\n", "");
    assert_eq!(preview_problems(&fewer).len(), 1);
}
