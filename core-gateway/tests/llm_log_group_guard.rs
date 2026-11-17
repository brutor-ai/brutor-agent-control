// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: every LLM `proxy_logs` row names its resource group.
//!
//! The bug class: a writer builds an LLM row with `build_proxy_log` (which
//! leaves `resource_group_id` empty) and writes it without stamping the group
//! the call ran in. The row then drops out of every per-group view and out of
//! the group's compliance tagging, while the call's usage is still billed to
//! the group. The Anthropic-native `/v1/messages` writers, the `/v1/messages`
//! bridge and the chat-completions router-error rows can all do this.
//!
//! Every non-test `build_proxy_log` whose proxy type is LLM must set the
//! group (`populate_group_id(..)` or an assignment to `resource_group_id`)
//! before the row is written, or before the function that builds it ends.
//! Behaviour is pinned by `tests/integration/llm_row_group_attribution.rs`;
//! this pins the shape for the writers those tests do not drive.

#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::{Path, PathBuf};

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) {
    for entry in fs::read_dir(dir).unwrap() {
        let p = entry.unwrap().path();
        if p.is_dir() {
            rust_files(&p, out);
        } else if p.extension().is_some_and(|e| e == "rs")
            // `#[cfg(test)] #[path = "…_tests.rs"]` modules.
            && !p.to_string_lossy().ends_with("_tests.rs")
        {
            out.push(p);
        }
    }
}

/// The file's code before its first `#[cfg(test)]` module, without
/// line comments.
fn production_lines(src: &str) -> Vec<&str> {
    let lines: Vec<&str> = src.lines().collect();
    let end = lines
        .windows(2)
        .position(|w| w[0].trim() == "#[cfg(test)]" && w[1].trim_start().starts_with("mod "))
        .unwrap_or(lines.len());
    lines[..end]
        .iter()
        .map(|l| {
            if l.trim_start().starts_with("//") {
                ""
            } else {
                l
            }
        })
        .collect()
}

const LLM_KINDS: [&str; 3] = ["\"llm\"", "constants::proxy::LLM", "proxy::LLM"];
const STAMPS: [&str; 2] = ["populate_group_id(", "resource_group_id ="];

/// `(line, snippet)` of every LLM row built without its group.
fn violations(src: &str) -> Vec<(usize, String)> {
    let lines = production_lines(src);
    let mut out = Vec::new();
    for (i, line) in lines.iter().enumerate() {
        let Some(col) = line.find("build_proxy_log(") else {
            continue;
        };
        if line.contains("fn build_proxy_log") {
            continue;
        }
        // The proxy-type argument: the rest of this line or the next one.
        let first_arg = format!(
            "{} {}",
            &line[col..],
            lines.get(i + 1).copied().unwrap_or("")
        );
        let after_paren = first_arg["build_proxy_log(".len()..].trim_start();
        // The constant as either crate spells it: `crate::constants::…` here,
        // `brutor_gateway_core::constants::…` in the Enterprise workspace.
        let after_paren = after_paren.strip_prefix("crate::").unwrap_or(after_paren);
        let after_paren = after_paren
            .strip_prefix("brutor_gateway_core::")
            .unwrap_or(after_paren);
        if !LLM_KINDS.iter().any(|k| after_paren.starts_with(k)) {
            continue;
        }
        // Up to the write, or until the scan leaves the block that holds
        // the row (the end of its scope: nothing after it can stamp it).
        let mut stamped = false;
        let mut depth: i64 = 0;
        for l in &lines[i..] {
            if STAMPS.iter().any(|s| l.contains(s)) {
                stamped = true;
                break;
            }
            if l.contains("write_proxy_log(") {
                break;
            }
            depth += l.matches('{').count() as i64 - l.matches('}').count() as i64;
            if depth < 0 {
                break;
            }
        }
        if !stamped {
            out.push((i + 1, line.trim().to_string()));
        }
    }
    out
}

#[test]
fn every_llm_row_names_its_group() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
    let mut files = Vec::new();
    rust_files(&root, &mut files);
    // The Enterprise workspace too, where this repository carries it.
    if let Some(enterprise) = roots::tree("enterprise") {
        let enterprise = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join(enterprise)
            .join("src");
        let before = files.len();
        rust_files(&enterprise, &mut files);
        assert!(files.len() > before + 20, "the Enterprise sources");
    }
    let mut bad = Vec::new();
    for f in files {
        let src = fs::read_to_string(&f).unwrap();
        for (line, snippet) in violations(&src) {
            bad.push(format!("{}:{line}: {snippet}", f.display()));
        }
    }
    assert!(
        bad.is_empty(),
        "LLM proxy_logs rows built without the call's resource group \
         (stamp it with `llm_helpers::populate_group_id(&mut log, &target_group)` \
         or set `log.resource_group_id`):\n{}",
        bad.join("\n")
    );
}

#[test]
fn the_guard_catches_the_shape_it_forbids() {
    let unstamped = "fn f() {\n        let mut log = proxy_logger::build_proxy_log(\n            \"llm\",\n            &auth,\n        );\n        state.background_writer.write_proxy_log(db, log);\n}\n";
    assert_eq!(violations(unstamped).len(), 1);

    let constant = "fn f() {\n        let mut log = build_proxy_log(crate::constants::proxy::LLM, &auth);\n        w.write_proxy_log(db, log);\n}\n";
    assert_eq!(violations(constant).len(), 1);

    // Stamped after the write does not count.
    let late = "fn f() {\n        let mut log = build_proxy_log(\"llm\", &auth);\n        w.write_proxy_log(db, log);\n        populate_group_id(&mut other, &g);\n}\n";
    assert_eq!(violations(late).len(), 1);

    // A comment saying so does not count either.
    let commented = "fn f() {\n        let mut log = build_proxy_log(\"llm\", &auth);\n        // populate_group_id(&mut log, &g);\n        w.write_proxy_log(db, log);\n}\n";
    assert_eq!(violations(commented).len(), 1);

    let ok = "fn f() {\n        let mut log = build_proxy_log(\"llm\", &auth);\n        llm_helpers::populate_group_id(&mut log, &target_group);\n        w.write_proxy_log(db, log);\n}\n";
    assert!(violations(ok).is_empty());

    let assigned = "fn f() -> ProxyLogInsert {\n    let mut log = build_proxy_log(\"llm\", &auth);\n    log.resource_group_id = scope.group_id.clone();\n    log\n}\n";
    assert!(violations(assigned).is_empty());

    // A nested block between the build and the stamp is still in scope.
    let nested = "fn f() -> ProxyLogInsert {\n    let mut log = build_proxy_log(\"llm\", &auth);\n    if let Some(m) = model {\n        log.llm_model_used = Some(m);\n    }\n    log.resource_group_id = g;\n    log\n}\n";
    assert!(violations(nested).is_empty());

    // A stamp after the row's scope has ended is someone else's row.
    let out_of_scope = "fn f() {\n    {\n        let log = build_proxy_log(\"llm\", &auth);\n        keep(log);\n    }\n    other.resource_group_id = g;\n}\n";
    assert_eq!(violations(out_of_scope).len(), 1);

    // The Enterprise workspace's spelling of the constant.
    let enterprise = "fn f() {\n        let mut log = build_proxy_log(brutor_gateway_core::constants::proxy::LLM, &auth);\n        w.write_proxy_log(db, log);\n}\n";
    assert_eq!(violations(enterprise).len(), 1);

    // Other surfaces are out of scope here.
    let mcp = "fn f() {\n        let mut log = build_proxy_log(\"mcp\", &auth);\n        w.write_proxy_log(db, log);\n}\n";
    assert!(violations(mcp).is_empty());

    // Test modules are not production writers.
    let in_tests = "#[cfg(test)]\nmod tests {\n    fn f() {\n        let log = build_proxy_log(\"llm\", &auth);\n    }\n}\n";
    assert!(violations(in_tests).is_empty());
}
