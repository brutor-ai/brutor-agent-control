// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: the caller-identity helpers have one definition.
//!
//! Why it matters: `extract_client_ip` existed three times, byte for byte,
//! in `portal_auth.rs`, `route_helpers.rs` and privately in `mcp.rs`. Three
//! copies of a function that decides which address the audit row records is
//! three places a fix can miss. The same holds for the two helpers that
//! decide who a request is from.

#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::{Path, PathBuf};

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) {
    for entry in fs::read_dir(dir).expect("read dir") {
        let path = entry.expect("entry").path();
        if path.is_dir() {
            rust_files(&path, out);
        } else if path.extension().is_some_and(|e| e == "rs") {
            out.push(path);
        }
    }
}

/// The function a line defines, if it defines one: visibility and the
/// `async` / `const` / `unsafe` qualifiers are skipped, so `pub async fn x(`
/// and a private `fn x(` are both definitions of `x`.
fn defined_fn(line: &str) -> Option<&str> {
    let mut rest = line.trim_start();
    if rest.starts_with("//") {
        return None;
    }
    if let Some(r) = rest.strip_prefix("pub") {
        rest = r.trim_start();
        if rest.starts_with('(') {
            rest = rest[rest.find(')')? + 1..].trim_start();
        }
    }
    for qualifier in ["const ", "async ", "unsafe "] {
        if let Some(r) = rest.strip_prefix(qualifier) {
            rest = r.trim_start();
        }
    }
    let rest = rest.strip_prefix("fn ")?;
    let end = rest.find(|c: char| !(c.is_alphanumeric() || c == '_'))?;
    Some(&rest[..end])
}

/// `file:line` of every definition of `name` under `src/`, test modules
/// included (a test that defines a same-named helper would be a copy too).
fn definitions(name: &str) -> Vec<String> {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    files.sort();
    let mut found = Vec::new();
    for path in files {
        let text = fs::read_to_string(&path).expect("read");
        for (i, line) in text.lines().enumerate() {
            if defined_fn(line) == Some(name) {
                found.push(format!("{}:{}", path.display(), i + 1));
            }
        }
    }
    found
}

#[test]
fn each_caller_identity_helper_is_defined_once_in_the_request_module() {
    for name in ["extract_client_ip", "portal_headers", "extract_portal_user"] {
        let defs = definitions(name);
        assert_eq!(
            defs.len(),
            1,
            "`{name}` must have exactly one definition, in src/routes/request.rs; found {defs:?}"
        );
        assert!(
            defs[0].starts_with("src/routes/request.rs:"),
            "`{name}` is defined outside src/routes/request.rs: {defs:?}"
        );
    }
}

#[test]
fn the_guard_recognises_every_way_a_function_is_declared() {
    for (line, name) in [
        (
            "fn extract_client_ip(headers: &HeaderMap) -> Option<String> {",
            "extract_client_ip",
        ),
        (
            "pub fn portal_headers(headers: &HeaderMap) -> HeaderMap {",
            "portal_headers",
        ),
        ("pub async fn extract_portal_user(", "extract_portal_user"),
        (
            "    pub(crate) async fn extract_portal_user(",
            "extract_portal_user",
        ),
        ("async fn extract_client_ip(", "extract_client_ip"),
        (
            "pub(in crate::routes) const fn portal_headers(",
            "portal_headers",
        ),
    ] {
        assert_eq!(defined_fn(line), Some(name), "{line}");
    }
    // not definitions: a call, a comment, a longer name
    assert_eq!(
        defined_fn("    let ip = extract_client_ip(&headers, None);"),
        None
    );
    assert_eq!(
        defined_fn("// fn extract_client_ip( used to live here"),
        None
    );
    assert_eq!(
        defined_fn("fn extract_client_ip_v2("),
        Some("extract_client_ip_v2")
    );
}
