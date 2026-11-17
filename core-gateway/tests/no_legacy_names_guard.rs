// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: a name the product no longer carries stays out of the repository.
//!
//! Brutor once routed through a third-party LLM gateway library. Nothing of
//! it is used any more, and its name is in no
//! identifier, comment, document or link.
//! The word is assembled here so this file does not contain it.

#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::{Path, PathBuf};

fn word() -> String {
    ["lite", "llm"].concat()
}

fn files(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().to_string();
        if path.is_dir() {
            if !matches!(
                name.as_str(),
                "target" | ".git" | ".claude" | "node_modules"
            ) {
                files(&path, out);
            }
        } else if name == "Dockerfile"
            || matches!(
                path.extension().and_then(|e| e.to_str()),
                Some("rs" | "toml" | "md" | "yml" | "yaml" | "sh" | "json" | "sql" | "example")
            )
        {
            out.push(path);
        }
    }
}

#[test]
fn the_name_is_nowhere_in_the_repository() {
    let needle = word();
    let mut all = Vec::new();
    files(Path::new("."), &mut all);
    assert!(all.len() > 500, "the scan found only {} files", all.len());
    // The walk is the whole repository, the Enterprise workspace included
    // where this repository carries it, and never a build
    // directory.
    if roots::tree("enterprise").is_some() {
        for under in ["./enterprise/src/", "./enterprise/tests/"] {
            assert!(
                all.iter()
                    .filter(|p| p.to_string_lossy().starts_with(under))
                    .count()
                    > 10,
                "the scan does not reach {under}"
            );
        }
    }
    assert!(
        !all.iter()
            .any(|p| p.components().any(|c| c.as_os_str() == "target")),
        "the scan walks a build directory"
    );
    let mut hits = Vec::new();
    for path in all {
        let Ok(text) = fs::read_to_string(&path) else {
            continue;
        };
        for (i, line) in text.lines().enumerate() {
            if line.to_ascii_lowercase().contains(&needle) {
                hits.push(format!("{}:{}: {}", path.display(), i + 1, line.trim()));
            }
        }
    }
    assert!(
        hits.is_empty(),
        "the legacy name is back:\n{}",
        hits.join("\n")
    );
}

/// The product is "Brutor Agent Control" and this component is its "Core
/// Gateway". The names it
/// carried before, assembled here so this file does not contain them:
/// the old product name with and without the company, the company and
/// the component as one name, and the same three with the component's
/// older word.
fn retired_product_names() -> Vec<String> {
    let company = "brutor";
    let mut names = Vec::new();
    for component in ["gateway", "proxy"] {
        names.push(format!("ai {component}"));
        names.push(format!("{company} {component}"));
    }
    names
}

/// What the live-provider benchmark binary writes (`src/bin/benchmark.rs`;
/// gitignored): output of a run, not a file of the repository.
const BENCHMARK_OUTPUT: &str = "./benchmark-results/";

fn retired_product_name_in(line: &str) -> Option<String> {
    // One space however the words were separated: a name split by a hyphen,
    // a dash or a line's indentation is still the name.
    let flat = line
        .to_ascii_lowercase()
        .split(|c: char| c.is_whitespace())
        .filter(|w| !w.is_empty())
        .collect::<Vec<_>>()
        .join(" ");
    retired_product_names()
        .into_iter()
        .find(|name| flat.contains(name.as_str()))
}

#[test]
fn no_retired_product_name_is_in_the_repository() {
    let mut all = Vec::new();
    files(Path::new("."), &mut all);
    assert!(all.len() > 500, "the scan found only {} files", all.len());
    let mut hits = Vec::new();
    for path in all {
        if path.to_string_lossy().starts_with(BENCHMARK_OUTPUT) {
            continue;
        }
        let Ok(text) = fs::read_to_string(&path) else {
            continue;
        };
        for (i, line) in text.lines().enumerate() {
            if let Some(name) = retired_product_name_in(line) {
                hits.push(format!(
                    "{}:{}: `{name}`: {}",
                    path.display(),
                    i + 1,
                    line.trim()
                ));
            }
        }
    }
    assert!(
        hits.is_empty(),
        "a retired product name (the product is Brutor Agent Control, this component its Core \
         Gateway):\n{}",
        hits.join("\n")
    );
}

/// What a reader of the running gateway sees — an error body, a log line, a
/// start-up refusal, an OpenAPI description — calls this component the
/// gateway, not by its older word with the article. Comments are
/// source only and are not held to it here; a `///` comment that becomes an
/// OpenAPI description is held to it through the document.
#[test]
fn nothing_a_reader_sees_calls_the_gateway_by_its_older_word() {
    let older = format!("the {}", "proxy");
    let says_it = |text: &str| {
        let lower = text.to_ascii_lowercase();
        lower.match_indices(&older).any(|(at, _)| {
            let before = lower[..at].chars().next_back();
            let after = lower[at + older.len()..].chars().next();
            !before.is_some_and(|c| c.is_ascii_alphanumeric())
                && !after.is_some_and(|c| c.is_ascii_alphanumeric() || c == '_' || c == '/')
        })
    };
    let mut all = Vec::new();
    files(Path::new("./src"), &mut all);
    if let Some(enterprise) = roots::tree("enterprise") {
        files(&Path::new(".").join(enterprise).join("src"), &mut all);
    }
    assert!(all.len() > 200, "the scan found only {} files", all.len());
    let mut hits = Vec::new();
    for path in all {
        let Ok(text) = fs::read_to_string(&path) else {
            continue;
        };
        for (i, line) in text.lines().enumerate() {
            if !line.trim_start().starts_with("//") && says_it(line) {
                hits.push(format!("{}:{}: {}", path.display(), i + 1, line.trim()));
            }
        }
    }
    let document = fs::read_to_string("openapi/proxy-openapi.json").expect("the OpenAPI document");
    for (i, line) in document.lines().enumerate() {
        if says_it(line) {
            hits.push(format!(
                "openapi/proxy-openapi.json:{}: {}",
                i + 1,
                line.trim().chars().take(200).collect::<String>()
            ));
        }
    }
    assert!(
        hits.is_empty(),
        "the component is the gateway (the Core Gateway):\n{}",
        hits.join("\n")
    );
    // The check sees what it looks for, and not a route or an identifier.
    assert!(says_it(&format!("start {older}.")));
    assert!(says_it(&format!("{}'s cache", older.to_uppercase())));
    assert!(!says_it(&format!("{older}_log row")));
    assert!(!says_it(&format!("under /v1/{}/llm", "proxy")));
}

#[test]
fn the_scan_sees_a_retired_product_name() {
    for name in retired_product_names() {
        assert_eq!(
            retired_product_name_in(&format!("// The {} - Core", name.to_uppercase())),
            Some(name.clone())
        );
        let split = name.replace(' ', "   ");
        assert!(retired_product_name_in(&split).is_some(), "{split}");
    }
    assert_eq!(retired_product_names().len(), 4);
    assert_eq!(
        retired_product_name_in("Brutor Agent Control: Core Gateway"),
        None
    );
    assert_eq!(retired_product_name_in("the gateway's router"), None);
    assert_eq!(retired_product_name_in("brutor-gateway-core"), None);
}

#[test]
fn the_scan_sees_the_word() {
    let line = format!("// this replaces {} for the proxy layer.", word());
    assert!(line.contains(&word()));
    assert!(!"the gateway's router".contains(&word()));
}
