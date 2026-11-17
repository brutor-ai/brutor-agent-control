// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Provider-base guard: no product code names
//! OpenAI's host except the one function that decides a base URL.
//!
//! The generic forwarder ended in `format!("https://api.openai.com/v1…")`
//! for a row with no `api_url` and no known base, and four media and batch
//! sites carried their own `unwrap_or("https://api.openai.com…")`: a row of
//! another provider had its prompt and credential sent to OpenAI. The base
//! is now chosen in `services::providers` (`upstream_base_for`, over the
//! tables `known_base` and `discover_base_url`, where `"openai"` maps to
//! its own host); a row with neither an `api_url` nor a known base of its
//! own provider is refused. A second copy of the host anywhere else is the
//! fallback coming back.
//!
//! Scans `src/` (not `src/bin/`, which holds tools) and `enterprise/src/`,
//! with `#[cfg(test)]` items, test files and comments removed.
#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::{Path, PathBuf};

const HOST: &str = "api.openai.com";

/// The file that decides, and the functions in it that may name the host.
const DECIDER: &str = "src/services/providers.rs";
const DECIDER_FNS: &[&str] = &["fn discover_base_url(", "fn known_base("];

/// Other product code that names the host, each with its reason. This list
/// may only shrink.
const ALLOWED: &[(&str, &str)] = &[
    // The OpenAI Moderation guardrail's own endpoint: a guardrail provider
    // named `openai_moderation`, called with the guardrail's own key. No
    // model row is dispatched there.
    (
        "src/services/guardrails/toxic.rs",
        "const OPENAI_DEFAULT_ENDPOINT: &str = \"https://api.openai.com/v1/moderations\";",
    ),
];

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) {
    for e in fs::read_dir(dir).unwrap().flatten() {
        let p = e.path();
        if p.is_dir() {
            if p != Path::new("src/bin") {
                rust_files(&p, out);
            }
        } else if p.extension().and_then(|s| s.to_str()) == Some("rs") {
            let name = p.file_name().and_then(|s| s.to_str()).unwrap_or("");
            // `#[cfg(test)] mod tests;` files.
            if name != "tests.rs" && !name.ends_with("_tests.rs") {
                out.push(p);
            }
        }
    }
}

/// The end of the brace-balanced block that starts at the first `{` of
/// `src` (exclusive), or `src.len()`.
fn block_end(src: &str) -> usize {
    let Some(open) = src.find('{') else {
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

/// `src` with every `#[cfg(test)]` item removed.
fn without_test_items(src: &str) -> String {
    let mut out = String::with_capacity(src.len());
    let mut rest = src;
    while let Some(at) = rest.find("#[cfg(test)]") {
        out.push_str(&rest[..at]);
        let after = &rest[at..];
        let semi = after.find(';').unwrap_or(after.len());
        match after.find('{').filter(|o| *o < semi) {
            Some(_) => rest = &after[block_end(after)..],
            None => rest = &after[(semi + 1).min(after.len())..],
        }
    }
    out.push_str(rest);
    out
}

/// `src` without comments: whole-line `//` comments and trailing ones
/// preceded by whitespace (so `"https://…"` in a string literal stays).
fn without_comments(src: &str) -> String {
    src.lines()
        .map(|line| {
            if line.trim_start().starts_with("//") {
                return "";
            }
            match line.find(" //") {
                Some(at) => &line[..at],
                None => line,
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// `code` with the bodies of the named functions removed.
fn without_fns(code: &str, fns: &[&str]) -> String {
    let mut code = code.to_string();
    for name in fns {
        if let Some(at) = code.find(name) {
            let end = at + block_end(&code[at..]);
            code.replace_range(at..end, "");
        }
    }
    code
}

/// The lines of one file's product code that name the host where they may
/// not.
fn violations(path: &str, src: &str) -> Vec<String> {
    let mut code = without_comments(&without_test_items(src));
    if path == DECIDER {
        code = without_fns(&code, DECIDER_FNS);
    }
    code.lines()
        .filter(|line| line.contains(HOST))
        .filter(|line| {
            !ALLOWED
                .iter()
                .any(|(file, allowed)| *file == path && line.trim() == *allowed)
        })
        .map(|line| {
            format!(
                "{path}: `{}` names OpenAI's host; a base URL is chosen by \
                 providers::upstream_base_for only",
                line.trim()
            )
        })
        .collect()
}

#[test]
fn only_the_base_decision_names_openais_host() {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    assert!(files.len() > 100, "the scan found the sources");
    let mut found = Vec::new();
    for f in &files {
        let path = f.to_string_lossy().replace('\\', "/");
        found.extend(violations(&path, &fs::read_to_string(f).unwrap()));
    }
    assert!(
        found.is_empty(),
        "provider base guard:\n{}",
        found.join("\n")
    );
    // The decider still names it, where it may: the guard reads the file it
    // exempts.
    let decider = fs::read_to_string(DECIDER).unwrap();
    assert!(decider.contains(HOST));
    for name in DECIDER_FNS {
        assert!(decider.contains(name), "{DECIDER} has `{name}`");
    }
}

/// The guard catches the shapes it exists for, and only those.
#[test]
fn the_guard_catches_what_it_guards() {
    // The generic forwarder's last line, in the decider's own file.
    let fallback = r#"
pub fn discover_base_url(provider: &str) -> Option<&'static str> {
    match provider {
        "openai" => Some("https://api.openai.com/v1"),
        _ => None,
    }
}
fn base_url_and_endpoint(model: &LlmModel, endpoint: &str) -> String {
    format!("https://api.openai.com/v1{endpoint}")
}
"#;
    let found = violations(DECIDER, fallback);
    assert_eq!(found.len(), 1, "{found:?}");
    assert!(found[0].contains("format!"), "{found:?}");

    // A media site's own default.
    let media = "let base_url = model\n    .api_url\n    .clone()\n    .unwrap_or_else(|| \"https://api.openai.com\".to_string());";
    assert_eq!(violations("src/routes/images.rs", media).len(), 1);
    // A constant, as the batch provider had it.
    let constant = "const OPENAI_BASE_URL: &str = \"https://api.openai.com\";";
    assert_eq!(
        violations("enterprise/src/batch/providers/openai.rs", constant).len(),
        1
    );
    // The allowed line is allowed in its own file only.
    assert!(violations(ALLOWED[0].0, ALLOWED[0].1).is_empty());
    assert_eq!(violations("src/routes/llm.rs", ALLOWED[0].1).len(), 1);

    // Comments and test items are not product code.
    let comment = "    // went to `https://api.openai.com/v1`\n    let x = 1; // api.openai.com";
    assert!(violations("src/routes/llm.rs", comment).is_empty());
    let test_item = "#[cfg(test)]\nmod tests {\n    const U: &str = \"https://api.openai.com/v1\";\n}\nfn f() {}";
    assert!(violations("src/routes/llm.rs", test_item).is_empty());
    // A table entry of the decider is not a violation.
    let table = "fn known_base(p: &str) -> Option<&'static str> {\n    match p { \"openai\" => Some(\"https://api.openai.com\"), _ => None }\n}";
    assert!(violations(DECIDER, table).is_empty());
    assert_eq!(violations("src/routes/llm.rs", table).len(), 1);
}
