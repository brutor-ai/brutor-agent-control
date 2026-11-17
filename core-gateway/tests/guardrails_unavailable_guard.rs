// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: a refusal because the guardrails could not be loaded is recorded.
//!
//! The bug class: the guardrail configs cannot be read, the gateway refuses
//! the call (fail closed), and returns its 503 without writing a
//! `proxy_logs` row. The refusal happened; the record says nothing, so a
//! reviewer asking "what did the gateway refuse, and why?" cannot see it.
//! Every LLM surface (chat, Responses, completions streams,
//! `/v1/messages`, Gemini, the Bedrock / Vertex bridge), the MCP proxy, the
//! MCP system server and the skills route can have this.
//!
//! The rule: wherever `configs_unavailable(` is called or a
//! `"guardrails_unavailable` refusal is built, the innermost `{ … }` block
//! around it must also record it (one of `RECORDERS`), or the enclosing
//! function must be in `EXEMPT` with the place that records it instead.

#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::Path;

const NEEDLES: [&str; 2] = ["configs_unavailable(", "\"guardrails_unavailable"];

const RECORDERS: [&str; 7] = [
    // LLM surfaces: the one refusal writer.
    "llm_refusal::guardrails_unavailable(",
    "refuse(",
    "refuse_with(",
    "write_proxy_log(",
    // MCP and skills route helpers that write the row.
    "refuse_unchecked_mcp(",
    "refuse_unchecked_skill(",
    // MCP system server.
    "write_skill_layer_log(",
];

/// `(file, function, where the refusal is recorded instead)`.
const EXEMPT: [(&str, &str, &str); 5] = [
    (
        "src/services/guardrails/mod.rs",
        "configs_unavailable",
        "the response builder itself; every caller records",
    ),
    (
        "src/services/a2a_v1/guardrail.rs",
        "run",
        "returns A2aErrorKind::GuardrailsUnavailable; the A2A v1 handlers record it with \
         log_guardrail_block / record_block, sealed engine_failure (stamp_guardrail_refusal), \
         and legacy A2A with its log_guardrail_block",
    ),
    (
        "src/services/system_server.rs",
        "guard_skill_output",
        "returns OutGuard::Blocked; the caller writes it as a skill-layer block row",
    ),
    (
        "src/routes/a2a.rs",
        "dispatch_skill",
        "returns a SkillError refused engine_failure; inbound records it with \
         log_inbound_and_refuse_as",
    ),
    (
        "src/routes/a2a_v1.rs",
        "dispatch_bound_handler",
        "returns A2aErrorKind::GuardrailsUnavailable; both callers fail the task and record \
         it sealed engine_failure (log_guardrail_block / record_block)",
    ),
];

/// `src` with comments and the insides of string and char literals blanked
/// to spaces, so braces in them do not count. Lengths are kept, so an index
/// into one is an index into the other.
fn blank(src: &str) -> Vec<u8> {
    let b = src.as_bytes();
    let mut out = b.to_vec();
    let mut i = 0;
    while i < b.len() {
        match b[i] {
            b'/' if b.get(i + 1) == Some(&b'/') => {
                while i < b.len() && b[i] != b'\n' {
                    out[i] = b' ';
                    i += 1;
                }
            }
            b'"' => {
                i += 1;
                while i < b.len() && b[i] != b'"' {
                    if b[i] == b'\\' {
                        out[i] = b' ';
                        i += 1;
                    }
                    if i < b.len() && b[i] != b'\n' {
                        out[i] = b' ';
                    }
                    i += 1;
                }
                i += 1;
            }
            b'\'' if b.get(i + 2) == Some(&b'\'') => {
                out[i + 1] = b' ';
                i += 3;
            }
            _ => i += 1,
        }
    }
    out
}

/// The innermost `{ … }` around `at`, as text of `src`.
fn block(src: &str, blanked: &[u8], at: usize) -> String {
    let mut depth = 0i32;
    let mut open = 0;
    for j in (0..at).rev() {
        match blanked[j] {
            b'}' => depth += 1,
            b'{' if depth == 0 => {
                open = j;
                break;
            }
            b'{' => depth -= 1,
            _ => {}
        }
    }
    let mut depth = 0i32;
    let mut close = blanked.len();
    for (j, c) in blanked.iter().enumerate().skip(open) {
        match c {
            b'{' => depth += 1,
            b'}' => {
                depth -= 1;
                if depth == 0 {
                    close = j + 1;
                    break;
                }
            }
            _ => {}
        }
    }
    src[open..close].to_string()
}

/// The top-level (or `impl`-level) function `at` sits in.
fn enclosing_fn(src: &str, at: usize) -> String {
    let head = &src[..at];
    head.rmatch_indices("fn ")
        .find_map(|(i, _)| {
            let before = head[..i].chars().last();
            if before.is_some_and(|c| c.is_alphanumeric() || c == '_') {
                return None;
            }
            let name: String = head[i + 3..]
                .chars()
                .take_while(|c| c.is_alphanumeric() || *c == '_')
                .collect();
            (!name.is_empty()).then_some(name)
        })
        .unwrap_or_default()
}

fn without_tests(src: &str) -> &str {
    match src.find("#[cfg(test)]\nmod ") {
        Some(i) => &src[..i],
        None => src,
    }
}

fn violations(file: &str, raw: &str) -> Vec<String> {
    let src = without_tests(raw);
    let blanked = blank(src);
    let mut out = Vec::new();
    for needle in NEEDLES {
        for (at, _) in src.match_indices(needle) {
            // Inside a comment: blanked away.
            if blanked[at] == b' ' {
                continue;
            }
            if needle == "configs_unavailable(" && src[..at].ends_with("fn ") {
                continue;
            }
            let func = enclosing_fn(src, at);
            if EXEMPT.iter().any(|(f, n, _)| *f == file && *n == func) {
                continue;
            }
            let b = block(src, &blanked, at);
            if RECORDERS.iter().any(|r| b.contains(r)) {
                continue;
            }
            out.push(format!(
                "{file}:{} in fn {func}",
                src[..at].matches('\n').count() + 1
            ));
        }
    }
    out
}

fn rust_files(dir: &Path, out: &mut Vec<String>) {
    for entry in fs::read_dir(dir).unwrap().flatten() {
        let p = entry.path();
        if p.is_dir() {
            rust_files(&p, out);
        } else if p.extension().is_some_and(|e| e == "rs") {
            out.push(p.to_string_lossy().replace('\\', "/"));
        }
    }
}

#[test]
fn every_guardrails_unavailable_refusal_is_recorded() {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    let mut found = Vec::new();
    for file in &files {
        let src = fs::read_to_string(file).unwrap();
        found.extend(violations(file, &src));
    }
    assert!(
        found.is_empty(),
        "these refuse because the guardrails could not be loaded and record \
         nothing; go through llm_refusal::guardrails_unavailable on an LLM \
         surface, or write the row (sealed engine_failure), or add an EXEMPT \
         entry naming where it is recorded:\n{}",
        found.join("\n")
    );
}

#[test]
fn every_exemption_still_names_a_refusal() {
    for (file, func, why) in EXEMPT {
        assert!(!why.is_empty());
        let raw = fs::read_to_string(file).unwrap();
        let src = without_tests(&raw);
        let hit = NEEDLES.iter().any(|n| {
            src.match_indices(n)
                .any(|(at, _)| enclosing_fn(src, at) == func)
        });
        assert!(hit, "stale exemption {file}::{func}");
    }
}

/// The shape that shipped, and its fix.
#[test]
fn the_rule_catches_the_shape_that_shipped() {
    let silent = r#"
async fn chat(state: S) -> Response {
    let configs = match state.guardrails_cache.get_configs(t, u, g, &db).await {
        Ok(c) => c,
        Err(e) => {
            return crate::services::guardrails::configs_unavailable("chat_output", &e);
        }
    };
    let x = format!("{y}");
    ok()
}
"#;
    assert_eq!(violations("x.rs", silent), vec!["x.rs:6 in fn chat"]);
    let recorded = silent.replace(
        "return crate::services::guardrails::configs_unavailable(\"chat_output\", &e);",
        "return crate::services::llm_refusal::guardrails_unavailable(&state, &scope, \"chat_output\", &e);",
    );
    assert!(violations("x.rs", &recorded).is_empty());
    // A recorder elsewhere in the function does not cover this block.
    let elsewhere = silent.replace(
        "    ok()",
        "    state.background_writer.write_proxy_log(db, log);\n    ok()",
    );
    assert_eq!(violations("x.rs", &elsewhere), vec!["x.rs:6 in fn chat"]);
    let bare = r#"
fn skill(state: S) -> Response {
    match x {
        Err(e) => {
            return error_response(StatusCode::SERVICE_UNAVAILABLE, "guardrails_unavailable: no");
        }
    }
}
"#;
    assert_eq!(violations("x.rs", bare), vec!["x.rs:5 in fn skill"]);
}
