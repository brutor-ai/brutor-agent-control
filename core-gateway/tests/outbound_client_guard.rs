// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Outbound-client guard: every HTTP client in product code is built
//! through `outbound_policy::client_builder`, and nothing replaces the
//! redirect policy it installs.
//!
//! `client_builder` checks destinations twice: the DNS resolver drops denied
//! addresses for hostnames, and the redirect policy checks every hop's URL,
//! because an IP-literal redirect target never reaches the resolver. A
//! client built with `reqwest::Client::builder()` has neither, and a later
//! `.redirect(Policy::limited(n))` silently removes the hop check: that is
//! how the shared client followed `302 → http://169.254.169.254/` before.
//!
//! Scans `src/` (not `src/bin/`, which holds tools) with `#[cfg(test)]`
//! items removed.
#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::{Path, PathBuf};

/// Files that may construct a client directly. The skill runner and the
/// reranker only ever reach operator-configured services; see the
/// `outbound_policy` module doc. This list may only shrink.
const BARE_CLIENT_ALLOWED: &[&str] = &[
    "src/services/outbound_policy.rs",
    "src/services/skill_runner_client.rs",
    "enterprise/src/knowledge_base.rs",
];

const BARE_CLIENT: &[&str] = &["Client::builder()", "Client::new()", "ClientBuilder::new()"];

/// Files that may use the public-fetch client, which follows cross-origin
/// redirects: only uncredentialed fetches of public URLs. This list
/// may only shrink.
const PUBLIC_FETCH_ALLOWED: &[&str] = &[
    "src/builder.rs",
    "src/services/outbound_policy.rs",
    "src/services/search_providers.rs",
    "src/routes/images.rs",
    "src/services/video_pipeline.rs",
];

/// Ways to attach a credential to a request.
const CREDENTIALS: &[&str] = &[
    ".bearer_auth(",
    ".basic_auth(",
    "\"authorization\"",
    "\"Authorization\"",
    "AUTHORIZATION",
    "api-key",
    "api_key",
    "token",
];

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) {
    for e in fs::read_dir(dir).unwrap().flatten() {
        let p = e.path();
        if p.is_dir() {
            if p != Path::new("src/bin") {
                rust_files(&p, out);
            }
        } else if p.extension().and_then(|s| s.to_str()) == Some("rs") {
            out.push(p);
        }
    }
}

/// `src` with every `#[cfg(test)]` item removed: from the attribute to the
/// end of the brace-balanced block (or `;`) that follows it.
fn without_test_items(src: &str) -> String {
    let mut out = String::with_capacity(src.len());
    let mut rest = src;
    while let Some(at) = rest.find("#[cfg(test)]") {
        out.push_str(&rest[..at]);
        let after = &rest[at..];
        let semi = after.find(';').unwrap_or(after.len());
        let Some(open) = after.find('{').filter(|o| *o < semi) else {
            rest = &after[(semi + 1).min(after.len())..];
            continue;
        };
        let mut depth = 0usize;
        let mut end = after.len();
        for (i, c) in after[open..].char_indices() {
            match c {
                '{' => depth += 1,
                '}' => {
                    depth -= 1;
                    if depth == 0 {
                        end = open + i + 1;
                        break;
                    }
                }
                _ => {}
            }
        }
        rest = &after[end..];
    }
    out.push_str(rest);
    out
}

/// Violations in one file's product code, as `rule: snippet`.
/// `src` without comments: whole-line `//` comments and trailing ones
/// preceded by whitespace (so `"http://…"` in a string literal stays).
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

fn violations(path: &str, src: &str) -> Vec<String> {
    let code = without_comments(&without_test_items(src));
    let mut found = Vec::new();
    if !BARE_CLIENT_ALLOWED.contains(&path) {
        for pat in BARE_CLIENT {
            if code.contains(pat) {
                found.push(format!(
                    "{path}: `{pat}` — build tenant-reachable clients with outbound_policy::client_builder()"
                ));
            }
        }
    }
    for (at, _) in code.match_indices("public_fetch_client()") {
        if !PUBLIC_FETCH_ALLOWED.contains(&path) {
            found.push(format!(
                "{path}: `public_fetch_client()` follows cross-origin redirects; use the shared client"
            ));
            break;
        }
        // The request built on it: up to the `.send()` that ends it.
        let call = &code[at..];
        let call = &call[..call.find(".send()").unwrap_or(call.len().min(600))];
        for cred in CREDENTIALS {
            if call.contains(cred) {
                found.push(format!(
                    "{path}: a request on public_fetch_client() carries `{cred}`; credentials go on the shared client only"
                ));
            }
        }
    }
    if path != "src/services/outbound_policy.rs" {
        let mut rest = code.as_str();
        while let Some(at) = rest.find(".redirect(") {
            let call: String = rest[at..].chars().take(80).collect();
            let compact: String = call.split_whitespace().collect();
            if !compact.starts_with(".redirect(reqwest::redirect::Policy::none())") {
                found.push(format!(
                    "{path}: `{}` replaces the checked redirect policy (only Policy::none() may)",
                    call.lines().next().unwrap_or("")
                ));
            }
            rest = &rest[at + 1..];
        }
    }
    found
}

#[test]
fn every_product_client_keeps_the_outbound_policy() {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    let mut found = Vec::new();
    for f in &files {
        let path = f.to_string_lossy().replace('\\', "/");
        found.extend(violations(&path, &fs::read_to_string(f).unwrap()));
    }
    assert!(
        found.is_empty(),
        "outbound client guard:\n{}",
        found.join("\n")
    );
}

/// The guard catches the two shapes it exists for, and ignores test code.
#[test]
fn the_guard_catches_what_it_guards() {
    let regressed = r#"
        let c = services::outbound_policy::client_builder()
            .timeout(t)
            .redirect(reqwest::redirect::Policy::limited(5))
            .build()?;
    "#;
    assert_eq!(violations("src/main.rs", regressed).len(), 1);

    let bare = "fn f() { let c = reqwest::Client::new(); }";
    assert_eq!(violations("src/routes/x.rs", bare).len(), 1);

    let commented = "        // No `.redirect(…)` here: the builder's policy checks every hop\n        .build()?;";
    assert!(
        violations("src/main.rs", commented).is_empty(),
        "comments are not code"
    );
    let url = r#"let u = "http://x/"; let c = b.redirect(reqwest::redirect::Policy::limited(5));"#;
    assert_eq!(
        violations("src/main.rs", url).len(),
        1,
        "a URL string does not hide the call"
    );

    let public_elsewhere = "let r = outbound_policy::public_fetch_client().get(u).send().await;";
    assert_eq!(violations("src/routes/mcp.rs", public_elsewhere).len(), 1);
    let keyed = "let r = outbound_policy::public_fetch_client()\n    .get(u)\n    .header(\"x-api-key\", key)\n    .send()\n    .await;";
    assert_eq!(violations("src/routes/images.rs", keyed).len(), 1);
    let bearer = "public_fetch_client().get(u).bearer_auth(t).send().await";
    assert_eq!(violations("src/routes/images.rs", bearer).len(), 1);
    assert!(violations("src/routes/images.rs", public_elsewhere).is_empty());

    let none = "let c = client_builder().redirect(reqwest::redirect::Policy::none()).build();";
    assert!(violations("src/services/cimd.rs", none).is_empty());

    let test_only = "fn f() {}\n#[cfg(test)]\nmod tests { fn t() { let c = reqwest::Client::new(); } }\nfn g() {}";
    assert!(violations("src/routes/x.rs", test_only).is_empty());
    assert!(
        without_test_items(test_only).contains("fn g()"),
        "code after a test module is still scanned"
    );
}
