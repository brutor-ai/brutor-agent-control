// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: every read of `idp_servers` takes OpenID Connect providers only.
//!
//! An identity provider row carries a `protocol` (`oidc` or `saml`). This
//! release serves no SAML: a query that selects a provider by id or by
//! issuer and ignores the protocol picks a stored SAML row and treats it as
//! an OAuth / OIDC provider (its key set verifies workload tokens, its
//! endpoints receive codes and refresh tokens). Every `FROM idp_servers` and
//! `JOIN idp_servers` in production code of both workspaces must state
//! `protocol = 'oidc'` in its statement. The one exception is the Enterprise
//! data check, which counts the SAML rows.
//!
//! Proven against the shape it forbids
//! ([`the_scan_catches_a_query_that_ignores_the_protocol`]).

#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::{Path, PathBuf};

/// How far after `FROM` / `JOIN idp_servers` the statement's protocol
/// condition must appear.
const WINDOW: usize = 400;
const OIDC_ONLY: &str = "protocol = 'oidc'";
/// `(file, condition)`: a file whose read of the table is about another
/// protocol, and the condition it states instead.
const OTHER_PROTOCOL: &[(&str, &str)] = &[("src/services/edition_guard.rs", "protocol = 'saml'")];

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) {
    for entry in fs::read_dir(dir).unwrap_or_else(|e| panic!("{}: {e}", dir.display())) {
        let path = entry.unwrap().path();
        if path.is_dir() {
            rust_files(&path, out);
        } else if path.extension().is_some_and(|x| x == "rs") {
            out.push(path);
        }
    }
}

/// Production code only: the text before the first unit-test module.
fn production(src: &str) -> &str {
    match src.find("#[cfg(test)]\nmod tests") {
        Some(at) => &src[..at],
        None => src,
    }
}

/// Line numbers of the reads in `src` that do not state `condition`.
fn unscoped_reads(src: &str, condition: &str) -> Vec<usize> {
    let src = production(src);
    let mut out = Vec::new();
    for needle in ["FROM idp_servers", "JOIN idp_servers"] {
        for (at, _) in src.match_indices(needle) {
            let line_start = src[..at].rfind('\n').map_or(0, |i| i + 1);
            if src[line_start..at].trim_start().starts_with("//") {
                continue;
            }
            let end = (at + WINDOW).min(src.len());
            let end = (end..=src.len())
                .find(|e| src.is_char_boundary(*e))
                .unwrap_or(src.len());
            if !src[at..end].contains(condition) {
                out.push(src[..at].lines().count());
            }
        }
    }
    out
}

#[test]
fn every_read_of_identity_providers_takes_oidc_only() {
    let community = roots::tree("community").expect("the Community tree");
    let mut trees = vec![(community.join("src"), "src".to_string())];
    if let Some(enterprise) = roots::tree("enterprise") {
        trees.push((enterprise.join("src"), "enterprise/src".to_string()));
    }
    let mut reads = 0;
    let mut found = Vec::new();
    for (dir, label) in trees {
        let mut files = Vec::new();
        rust_files(&dir, &mut files);
        for file in files {
            let rel = format!(
                "{label}/{}",
                file.strip_prefix(&dir).unwrap().to_string_lossy()
            );
            let text = fs::read_to_string(&file).unwrap();
            reads += production(&text).matches("idp_servers").count();
            let condition = OTHER_PROTOCOL
                .iter()
                .find(|(f, _)| *f == rel)
                .map_or(OIDC_ONLY, |(_, c)| *c);
            for line in unscoped_reads(&text, condition) {
                found.push(format!("{rel}:{line}"));
            }
        }
    }
    assert!(
        reads >= 4,
        "expected the identity-provider reads; found {reads}"
    );
    assert!(
        found.is_empty(),
        "these read identity providers without `{OIDC_ONLY}`: a stored SAML provider would be \
         used as an OpenID Connect one:\n{}",
        found.join("\n")
    );
}

#[test]
fn the_scan_catches_a_query_that_ignores_the_protocol() {
    let ignores = r#"
        sqlx::query_as("SELECT * FROM idp_servers WHERE id = $1 AND tenant_id = $2 AND enabled = true")
    "#;
    assert_eq!(unscoped_reads(ignores, OIDC_ONLY).len(), 1);
    let joined = "LEFT JOIN idp_servers idp ON oc.idp_id = idp.id\n\"#;";
    assert_eq!(unscoped_reads(joined, OIDC_ONLY).len(), 1);
    let scoped = r#"
        sqlx::query_as("SELECT * FROM idp_servers WHERE id = $1 AND tenant_id = $2 \
                        AND enabled = true AND protocol = 'oidc'")
    "#;
    assert!(unscoped_reads(scoped, OIDC_ONLY).is_empty());
    let in_a_comment = "// SELECT * FROM idp_servers WHERE id = $1\n";
    assert!(unscoped_reads(in_a_comment, OIDC_ONLY).is_empty());
    let in_a_test = "fn f() {}\n#[cfg(test)]\nmod tests {\n  const Q: &str = \"SELECT * FROM idp_servers\";\n}\n";
    assert!(unscoped_reads(in_a_test, OIDC_ONLY).is_empty());
}
