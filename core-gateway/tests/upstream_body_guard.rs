// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: a provider reply is never read as "JSON or null".
//!
//! `resp.json().await.unwrap_or_default()` turns a plain-text or HTML provider
//! error into `null`, so the caller and the audit row lose what the provider
//! said. Read provider replies with `providers::upstream_json`. Behaviour is
//! pinned by `tests/integration/upstream_error_bodies.rs`.

#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::Path;

fn rust_files(dir: &Path, out: &mut Vec<std::path::PathBuf>) {
    for entry in fs::read_dir(dir).unwrap() {
        let p = entry.unwrap().path();
        if p.is_dir() {
            rust_files(&p, out);
        } else if p.extension().is_some_and(|e| e == "rs") {
            out.push(p);
        }
    }
}

fn violations(file: &str, src: &str) -> Vec<String> {
    // Code only: the helper's own documentation quotes the pattern.
    let flat: String = src
        .lines()
        .filter(|l| !l.trim_start().starts_with("//"))
        .flat_map(str::split_whitespace)
        .collect();
    let mut out = Vec::new();
    for pat in [
        ".json().await.unwrap_or_default()",
        ".json::<Value>().await.unwrap_or_default()",
    ] {
        if flat.contains(pat) {
            out.push(format!(
                "{file}: reads a reply as JSON-or-null ({pat}); use providers::upstream_json"
            ));
        }
    }
    out
}

#[test]
fn provider_replies_are_never_read_as_json_or_null() {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    let bad: Vec<String> = files
        .iter()
        .flat_map(|p| violations(&p.display().to_string(), &fs::read_to_string(p).unwrap()))
        .collect();
    assert!(bad.is_empty(), "{}", bad.join("\n"));
}

#[test]
fn the_guard_catches_the_shape_it_forbids() {
    let old = "let resp_body: Value = resp\n    .json()\n    .await\n    .unwrap_or_default();";
    assert_eq!(violations("src/routes/x.rs", old).len(), 1);
}
