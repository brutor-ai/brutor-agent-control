// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: whose word the gateway takes for the client's address, scheme and
//! host is decided in ONE place.
//!
//! Why it matters: `extract_client_ip` believed the first `X-Forwarded-For`
//! entry (or `X-Real-IP`) from any peer, so any caller wrote any address
//! into the audit rows, the proxy log and the run and agent identities. And
//! the OAuth routes built their issuer, metadata and redirect URIs from
//! `X-Forwarded-Proto` / `X-Forwarded-Host` / `Host` whenever the public URL
//! said "localhost": a caller chose the URLs the gateway advertised.
//!
//! The rules (`BRUTOR_TRUSTED_PROXIES`, the Control Plane's semantics) live in
//! `src/services/trusted_proxies.rs`; the TCP peer is read only by the
//! `ClientIp` extractor in `src/routes/request.rs`. So:
//!
//! * no production source names a forwarded header (`X-Forwarded-*`,
//!   `X-Real-IP`, `Forwarded`, `True-Client-IP`, `CF-Connecting-IP`)
//!   outside `src/services/trusted_proxies.rs`;
//! * no production source reads the peer address (`ConnectInfo`,
//!   `peer_addr`, `remote_addr`) outside `src/routes/request.rs`;
//! * no production source reads the `Host` header: absolute URLs come from
//!   `BRUTOR_GATEWAY_PUBLIC_URL` only.
//!
//! Scanned: the Community `src/` and `crates/`, and the Enterprise `src/`
//! where this checkout carries it. Test modules inside those files count:
//! a helper there would be a second reading.

#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::{Path, PathBuf};

/// Names of the headers a client could forge an address, scheme or host
/// with, as code spells them: a string literal or a constant (compared
/// lower-cased; prose that names a header is not a reading).
const FORWARDED: &[&str] = &[
    "\"x-forwarded-",
    "x_forwarded_",
    "\"x-real-ip\"",
    "x_real_ip",
    "\"forwarded\"",
    "header::forwarded",
    "\"true-client-ip\"",
    "\"cf-connecting-ip\"",
];
const FORWARDED_HOME: &str = "src/services/trusted_proxies.rs";

/// Ways to read the TCP peer's address.
const PEER: &[&str] = &["ConnectInfo", "peer_addr", "remote_addr"];
const PEER_HOME: &str = "src/routes/request.rs";

/// Ways to read the `Host` header (compared lower-cased).
const HOST: &[&str] = &["get(\"host\")", "header::host)", "typedheader<host>"];

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

/// Every production source file, as (path relative to its tree, text).
fn sources() -> Vec<(String, String)> {
    let community = roots::tree("community").expect("the Community tree");
    let mut trees = vec![(community.clone(), "src"), (community.clone(), "crates")];
    if let Some(enterprise) = roots::tree("enterprise") {
        trees.push((enterprise, "src"));
    }
    let mut out = Vec::new();
    for (root, dir) in trees {
        let mut files = Vec::new();
        rust_files(&root.join(dir), &mut files);
        files.sort();
        for path in files {
            let rel = path
                .strip_prefix(&root)
                .expect("under its root")
                .display()
                .to_string();
            let label = if root == community {
                rel
            } else {
                format!("enterprise/{rel}")
            };
            out.push((label, fs::read_to_string(&path).expect("read")));
        }
    }
    assert!(
        out.iter().any(|(p, _)| p == FORWARDED_HOME),
        "the scan did not see {FORWARDED_HOME}"
    );
    out
}

/// `file:line` of every line of `files` that contains one of `needles`,
/// outside the file `home`. `lower` compares the line lower-cased.
fn offenders(
    files: &[(String, String)],
    needles: &[&str],
    home: Option<&str>,
    lower: bool,
) -> Vec<String> {
    let mut found = Vec::new();
    for (path, text) in files {
        if Some(path.as_str()) == home {
            continue;
        }
        for (i, line) in text.lines().enumerate() {
            if line.trim_start().starts_with("//") {
                continue;
            }
            let line = if lower {
                line.to_ascii_lowercase()
            } else {
                line.to_string()
            };
            if needles.iter().any(|n| line.contains(n)) {
                found.push(format!("{path}:{}: {}", i + 1, line.trim()));
            }
        }
    }
    found
}

#[test]
fn no_code_but_the_trusted_proxies_module_names_a_forwarded_header() {
    let found = offenders(&sources(), FORWARDED, Some(FORWARDED_HOME), true);
    assert!(
        found.is_empty(),
        "forwarded headers are read only by {FORWARDED_HOME} \
         (BRUTOR_TRUSTED_PROXIES); found:\n{}",
        found.join("\n")
    );
}

#[test]
fn no_code_but_the_client_ip_extractor_reads_the_peer_address() {
    let found = offenders(&sources(), PEER, Some(PEER_HOME), false);
    assert!(
        found.is_empty(),
        "the peer address is read only by the ClientIp extractor in {PEER_HOME}; \
         take `client: ClientIp` instead. Found:\n{}",
        found.join("\n")
    );
}

#[test]
fn no_code_reads_the_host_header() {
    let found = offenders(&sources(), HOST, None, true);
    assert!(
        found.is_empty(),
        "absolute URLs come from BRUTOR_GATEWAY_PUBLIC_URL, never the Host header; found:\n{}",
        found.join("\n")
    );
}

/// The scan catches each shape it forbids, and passes the home files.
#[test]
fn the_guard_sees_every_shape_it_forbids() {
    let file = |path: &str, text: &str| vec![(path.to_string(), text.to_string())];
    for line in [
        r#"headers.get("x-forwarded-for")"#,
        r#"headers.get("X-Forwarded-Proto")"#,
        r#"headers.get("x-forwarded-host")"#,
        r#".or_else(|| headers.get("x-real-ip"))"#,
        r#"headers.get("Forwarded")"#,
        r#"headers.get("true-client-ip")"#,
        r#"headers.get("CF-Connecting-IP")"#,
        "headers.get(X_FORWARDED_FOR)",
        "headers.get(axum::http::header::FORWARDED)",
        r#"HeaderName::from_static("x-forwarded-host")"#,
    ] {
        assert_eq!(
            offenders(
                &file("src/routes/x.rs", line),
                FORWARDED,
                Some(FORWARDED_HOME),
                true
            )
            .len(),
            1,
            "{line}"
        );
        assert!(
            offenders(
                &file(FORWARDED_HOME, line),
                FORWARDED,
                Some(FORWARDED_HOME),
                true
            )
            .is_empty()
        );
    }
    for line in [
        "    ConnectInfo(peer): ConnectInfo<SocketAddr>,",
        "let peer = stream.peer_addr()?;",
        "req.remote_addr()",
    ] {
        assert_eq!(
            offenders(
                &file("enterprise/src/routes/p.rs", line),
                PEER,
                Some(PEER_HOME),
                false
            )
            .len(),
            1,
            "{line}"
        );
        assert!(offenders(&file(PEER_HOME, line), PEER, Some(PEER_HOME), false).is_empty());
    }
    for line in [
        r#".or_else(|| headers.get("host"))"#,
        "headers.get(axum::http::header::HOST)",
        "TypedHeader<Host>",
    ] {
        assert_eq!(
            offenders(&file("src/routes/oauth.rs", line), HOST, None, true).len(),
            1,
            "{line}"
        );
    }
    // not readings: a function called `forwarded`, prose, a comment
    for line in [
        "fn forwarded(headers: &HeaderMap) // never forwarded",
        r#"description: "the right-most X-Forwarded-For address that is not listed","#,
        r#"    /// reads `headers.get("x-forwarded-for")` no more"#,
    ] {
        assert!(
            offenders(
                &file("src/x.rs", line),
                FORWARDED,
                Some(FORWARDED_HOME),
                true
            )
            .is_empty(),
            "{line}"
        );
    }
}
