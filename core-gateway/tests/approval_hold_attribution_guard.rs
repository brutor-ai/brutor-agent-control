// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Every approval hold names the run it belongs to.
//!
//! The run ledger (`awaiting_human_ms`) and the live run clock (the pause
//! behind `max_duration_seconds_per_run`) both find a run's holds by
//! `tool_approval_requests.originating_trace_root_id`. MCP stamped it; the
//! A2A, A2A v1 and skill surfaces passed `None`, so the time those runs spent
//! waiting on a person was invisible to both, and an approved retry could be
//! refused by the duration ceiling for having been approved slowly.
//!
//! This scans the source rather than driving requests, because the failure is
//! an absence: a new surface that raises a hold without its run compiles and
//! runs fine. The integration suite (`approval_hold_attribution.rs`) proves
//! the rows on today's surfaces; this keeps tomorrow's honest.
//!
//! The shape it enforces:
//!
//! 1. Exactly one place writes a `tool_approval_requests` row, and the
//!    function that does is private — the only way in is
//!    `agent_approval::require_or_enqueue`.
//! 2. Every `ApprovalContext {.. }` literal names its run with an expression
//!    derived from the request, never a literal `None`, and hands the run
//!    clock over.
//! 3. Every hold's WINDOW comes from the one resolver,
//!    `approval_window::resolve`: an `ApprovalContext` states only the
//!    policy's own window (`policy_window_seconds`, never a number), the row
//!    writer is handed the resolved value, the skill execution-mode parking
//!    resolves through the same function, and no second default exists.
//!    On the live demo five call sites passed a literal `300`, so every hold
//!    no policy gave a window lapsed after five minutes.

use std::fs;
use std::path::{Path, PathBuf};

const WRITER: &str = "src/services/agent_approval.rs";

fn rust_sources(dir: &Path, out: &mut Vec<PathBuf>) {
    for entry in fs::read_dir(dir).unwrap_or_else(|e| panic!("read {}: {e}", dir.display())) {
        let path = entry.expect("dir entry").path();
        if path.is_dir() {
            rust_sources(&path, out);
        } else if path.extension().is_some_and(|e| e == "rs") {
            out.push(path);
        }
    }
}

fn sources() -> Vec<(String, String)> {
    let mut files = Vec::new();
    rust_sources(Path::new("src"), &mut files);
    files.sort();
    files
        .into_iter()
        .map(|p| {
            let src =
                fs::read_to_string(&p).unwrap_or_else(|e| panic!("read {}: {e}", p.display()));
            (p.to_string_lossy().replace('\\', "/"), src)
        })
        .collect()
}

/// The text of every `ApprovalContext { ... }` struct literal in `src`,
/// brace-matched so nested literals and closures stay inside their parent.
fn approval_context_literals(src: &str) -> Vec<String> {
    let mut out = Vec::new();
    let needle = "ApprovalContext {";
    let mut from = 0;
    while let Some(i) = src[from..].find(needle) {
        let start = from + i;
        // Skip the struct definition itself.
        let line_start = src[..start].rfind('\n').map_or(0, |n| n + 1);
        if src[line_start..start].contains("struct ") {
            from = start + needle.len();
            continue;
        }
        let open = start + needle.len() - 1;
        let mut depth = 0usize;
        let mut end = open;
        for (off, ch) in src[open..].char_indices() {
            match ch {
                '{' => depth += 1,
                '}' => {
                    depth -= 1;
                    if depth == 0 {
                        end = open + off;
                        break;
                    }
                }
                _ => {}
            }
        }
        out.push(strip_line_comments(&src[start..=end]));
        from = end + 1;
    }
    out
}

/// Drop `// ...` comments, whose prose may carry unbalanced brackets.
fn strip_line_comments(text: &str) -> String {
    text.lines()
        .map(|l| match l.find("//") {
            Some(i) => &l[..i],
            None => l,
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// The value expression of `field:` inside a struct literal, up to the
/// field's trailing comma at depth zero.
fn field_expr(literal: &str, field: &str) -> Option<String> {
    let key = format!("{field}:");
    let body = &literal[literal.find('{')? + 1..];
    let mut depth = 0i32;
    let mut i = 0;
    let bytes = body.as_bytes();
    while i < bytes.len() {
        match bytes[i] {
            b'(' | b'[' | b'{' => depth += 1,
            b')' | b']' | b'}' => depth -= 1,
            _ => {}
        }
        if depth == 0 && bytes[i..].starts_with(key.as_bytes()) {
            let prev_ok = i == 0 || !(bytes[i - 1].is_ascii_alphanumeric() || bytes[i - 1] == b'_');
            if prev_ok {
                let rest = &body[i + key.len()..];
                let mut d = 0i32;
                for (off, ch) in rest.char_indices() {
                    match ch {
                        '(' | '[' | '{' => d += 1,
                        ')' | ']' | '}' => {
                            if d == 0 {
                                return Some(rest[..off].trim().to_string());
                            }
                            d -= 1;
                        }
                        ',' if d == 0 => return Some(rest[..off].trim().to_string()),
                        _ => {}
                    }
                }
                return Some(rest.trim().to_string());
            }
        }
        i += 1;
    }
    None
}

#[test]
fn only_the_approval_lifecycle_writes_a_hold() {
    let writers: Vec<String> = sources()
        .into_iter()
        .filter(|(_, src)| src.contains("INSERT INTO tool_approval_requests"))
        .map(|(path, _)| path)
        .collect();
    assert_eq!(
        writers,
        vec![WRITER.to_string()],
        "a hold row is written outside `agent_approval` — it bypasses the \
         ApprovalContext that carries the run, so nothing guarantees it names one",
    );
}

#[test]
fn the_row_writer_is_not_callable_from_a_surface() {
    let src = fs::read_to_string(WRITER).expect("read agent_approval.rs");
    for public in [
        "pub async fn create_approval_request(",
        "pub async fn create_approval_request_with_metadata(",
        "pub(crate) async fn create_approval_request_with_metadata(",
    ] {
        assert!(
            !src.contains(public),
            "`{public}` is reachable from a surface: loose `originating_*` \
             arguments are how holds came to be raised without a run. Surfaces \
             go through `require_or_enqueue`.",
        );
    }
}

#[test]
fn every_surface_names_the_run_on_its_holds() {
    let mut sites = Vec::new();
    for (path, src) in sources() {
        for lit in approval_context_literals(&src) {
            sites.push((path.clone(), lit));
        }
    }
    // Today: MCP (grant + capability filter), A2A outbound, A2A v1 inbound,
    // skills (grant + argument band). A drop means a surface stopped
    // raising holds through the shared lifecycle — look before lowering.
    assert!(
        sites.len() >= 6,
        "expected at least 6 ApprovalContext sites, found {}: {:?}",
        sites.len(),
        sites.iter().map(|(p, _)| p).collect::<Vec<_>>(),
    );
    for (path, lit) in &sites {
        let run = field_expr(lit, "run")
            .unwrap_or_else(|| panic!("{path}: ApprovalContext without a `run` field:\n{lit}"));
        assert!(
            run != "None",
            "{path}: a hold is raised with `run: None`. The ledger and the run \
             clock find holds by their run; pass the RunScope this request \
             derived from its call chain.\n{lit}",
        );
        let clock = field_expr(lit, "run_clock")
            .unwrap_or_else(|| panic!("{path}: ApprovalContext without `run_clock`:\n{lit}"));
        assert!(
            clock != "None",
            "{path}: a hold is raised without the run clock, so a redeemed \
             token does not take the wait off the run's duration.\n{lit}",
        );
    }
}

#[test]
fn the_scanner_sees_a_literal_none() {
    // Guard the guard: the field parser must actually catch the regression.
    let lit = "ApprovalContext { db: &state.db, run: None, extra_metadata: Some(json!({\"a\": 1})), run_clock: Some(&s.usage_state) }";
    assert_eq!(field_expr(lit, "run").as_deref(), Some("None"));
    assert_eq!(
        field_expr(lit, "run_clock").as_deref(),
        Some("Some(&s.usage_state)")
    );
    let found = approval_context_literals(&format!("fn f() {{ let c = {lit}; }}"));
    assert_eq!(found.len(), 1);
}

// ---------------------------------------------------------------------------
// One approval window resolver
// ---------------------------------------------------------------------------

const RESOLVER: &str = "src/services/approval_window.rs";

/// True when `expr` contains an integer literal (a digit that does not sit
/// inside an identifier such as `i32` or `t1`).
fn has_integer_literal(expr: &str) -> bool {
    let b = expr.as_bytes();
    (0..b.len()).any(|i| {
        b[i].is_ascii_digit() && (i == 0 || !(b[i - 1].is_ascii_alphanumeric() || b[i - 1] == b'_'))
    })
}

#[test]
fn every_hold_states_only_its_policys_window() {
    let mut sites = 0;
    for (path, src) in sources() {
        for lit in approval_context_literals(&src) {
            sites += 1;
            assert!(
                field_expr(&lit, "timeout_seconds").is_none(),
                "{path}: an ApprovalContext carries a resolved timeout. Holds take \
                 their window from `approval_window::resolve` inside \
                 `require_or_enqueue`; state only the policy's own window.\n{lit}",
            );
            let w = field_expr(&lit, "policy_window_seconds").unwrap_or_else(|| {
                panic!("{path}: ApprovalContext without `policy_window_seconds`:\n{lit}")
            });
            assert!(
                !has_integer_literal(&w),
                "{path}: `policy_window_seconds: {w}` is a number written at the call \
                 site. A literal window is how every unconfigured hold came to lapse \
                 after 300 s; pass the window the policy states, or None.\n{lit}",
            );
        }
    }
    assert!(
        sites >= 6,
        "expected at least 6 ApprovalContext sites, found {sites}"
    );
}

#[test]
fn the_row_writer_gets_the_resolved_window() {
    let src = fs::read_to_string(WRITER).expect("read agent_approval.rs");
    assert!(
        src.contains("approval_window::resolve("),
        "`require_or_enqueue` must resolve the window through approval_window::resolve",
    );
    assert!(
        src.contains("window.seconds,"),
        "the hold row must be written with the resolved window",
    );
}

/// A skill held by its execution mode is a hold like any other: the
/// orchestrator states the policy's window and `require_or_enqueue` resolves
/// it. A resolver call of its own would be a second way to arrive at a
/// deadline.
fn resolves_a_window_itself(orchestrator: &str) -> bool {
    orchestrator.contains("approval_window::resolve(")
}

#[test]
fn the_skill_orchestrator_resolves_no_window_of_its_own() {
    let src = fs::read_to_string("src/services/skill_orchestrator.rs").expect("read");
    assert!(
        !resolves_a_window_itself(&src),
        "skill_orchestrator.rs calls approval_window::resolve: a skill's hold states its \
         policy window (`policy_window_seconds`) and require_or_enqueue resolves it",
    );
    assert!(
        src.contains("policy_window_seconds: "),
        "the skill gate no longer goes through an ApprovalContext",
    );
}

#[test]
fn the_window_guard_sees_a_resolver_call_in_the_orchestrator() {
    assert!(resolves_a_window_itself(
        "let w = crate::services::approval_window::resolve(db, tenant_id, None, own).await;"
    ));
    assert!(!resolves_a_window_itself(
        "policy_window_seconds: gate.window,"
    ));
}

#[test]
fn there_is_one_default_window() {
    for (path, src) in sources() {
        for banned in [
            "DEFAULT_APPROVAL_HOLD_SECONDS",
            "DEFAULT_SKILL_APPROVAL_TIMEOUT_SECS",
            "MAX_SKILL_APPROVAL_TIMEOUT_SECS",
        ] {
            assert!(
                !src.contains(banned),
                "{path}: `{banned}` is a second approval-window default; the only one is \
                 approval_window::PLATFORM_DEFAULT_SECONDS",
            );
        }
        if path != RESOLVER {
            for (i, _) in src.match_indices("PLATFORM_DEFAULT_SECONDS") {
                let line_start = src[..i].rfind('\n').map_or(0, |n| n + 1);
                let line = &src[line_start..src[i..].find('\n').map_or(src.len(), |n| i + n)];
                assert!(
                    path.ends_with("enterprise/src/routes/portal_mcps.rs"),
                    "{path}: PLATFORM_DEFAULT_SECONDS used outside the resolver: {line}",
                );
            }
        }
    }
}

#[test]
fn the_window_scanner_sees_a_literal() {
    assert!(has_integer_literal("300"));
    assert!(has_integer_literal("Some(86_400)"));
    assert!(!has_integer_literal("None"));
    assert!(!has_integer_literal("grant_hold_window"));
    assert!(!has_integer_literal("i32::MAX.min(t1)"));
}
