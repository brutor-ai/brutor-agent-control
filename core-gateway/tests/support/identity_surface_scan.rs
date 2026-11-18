// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The scan behind the identity-policy surface guards (the Community
//! crate's `tests/identity_policy_surface_guard.rs` and the Enterprise
//! workspace's): every function that authenticates a caller also has the
//! caller's credential judged against the call's identity policy, or is
//! listed as exempt with its reason.
//!
//! Included by path (`#[path = ".../support/identity_surface_scan.rs"] mod scan;`).

use std::fs;
use std::path::Path;

/// Calls that authenticate a caller.
pub const AUTH: [&str; 3] = [
    "authenticate_request(",
    "authenticate_caller(",
    "extract_media_caller(",
];

/// Calls that judge the caller: the judge itself, the call every surface
/// makes (`identity_policy::judge_call`), and the helpers that make it
/// inside (the LLM preflight, the multimodal preflight that runs it, the
/// media caller).
pub const JUDGED_BY: [&str; 5] = [
    "identity_policy::judge(",
    "identity_policy::judge_call(",
    "run_preflight(",
    "multimodal_preflight(",
    "extract_media_caller(",
];

/// `(name, body)` of each top-level function before the first test module,
/// without line comments.
pub fn functions(src: &str) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    let lines: Vec<&str> = src.lines().collect();
    for (i, line) in lines.iter().copied().enumerate() {
        if line.starts_with("#[cfg(test)]")
            && lines
                .get(i + 1)
                .is_some_and(|next| next.trim_start().starts_with("mod "))
        {
            break;
        }
        if line.trim_start().starts_with("//") {
            continue;
        }
        let top_level = !line.starts_with(' ') && !line.starts_with('\t');
        if top_level && let Some(at) = line.find("fn ") {
            let prefix = &line[..at];
            if prefix
                .split_whitespace()
                .all(|w| w == "pub" || w.starts_with("pub(") || w == "async")
            {
                let name: String = line[at + 3..]
                    .chars()
                    .take_while(|c| c.is_alphanumeric() || *c == '_')
                    .collect();
                out.push((name, String::new()));
                continue;
            }
        }
        if let Some((_, body)) = out.last_mut() {
            body.push_str(line);
            body.push('\n');
        }
    }
    out
}

/// The functions of `src` that authenticate and are not judged, as
/// `file::function`, leaving out the `exempt` ones.
pub fn violations(file: &str, src: &str, exempt: &[(&str, &str, &str)]) -> Vec<String> {
    functions(src)
        .into_iter()
        .filter(|(_, body)| AUTH.iter().any(|a| body.contains(a)))
        .filter(|(_, body)| !JUDGED_BY.iter().any(|j| body.contains(j)))
        .filter(|(name, _)| !exempt.iter().any(|(f, n, _)| *f == file && n == name))
        .map(|(name, _)| format!("{file}::{name}"))
        .collect()
}

/// Every `.rs` file under `dir` (relative to `root`, `/`-separated) whose
/// code calls an [`AUTH`] function outside the place it is defined.
pub fn files_that_authenticate(root: &Path, dir: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut stack = vec![root.join(dir)];
    while let Some(at) = stack.pop() {
        let Ok(entries) = fs::read_dir(&at) else {
            continue;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                stack.push(path);
            } else if path.extension().is_some_and(|e| e == "rs") {
                let src = fs::read_to_string(&path).unwrap_or_default();
                let calls = functions(&src).iter().any(|(name, body)| {
                    AUTH.iter().any(|a| body.contains(a))
                        && !AUTH.iter().any(|a| a.trim_end_matches('(') == name)
                });
                if calls {
                    let rel = path
                        .strip_prefix(root)
                        .expect("under the root")
                        .to_string_lossy()
                        .replace('\\', "/");
                    out.push(rel);
                }
            }
        }
    }
    out.sort();
    out
}

/// The rule against the shape it forbids: a surface that authenticates and
/// goes on to serve without the judge.
pub fn check_the_rule() {
    let leaking = r#"
pub(crate) async fn proxy(state: S, headers: HeaderMap) -> Response {
    let auth_ctx = match auth::authenticate_request(&headers, t, s, db, v, ip, true).await {
        Ok(ctx) => ctx,
        Err(e) => return refuse(e),
    };
    serve(auth_ctx).await
}
"#;
    assert_eq!(violations("x.rs", leaking, &[]), vec!["x.rs::proxy"]);
    let judged = leaking.replace(
        "    serve(auth_ctx)",
        "    let mut auth_ctx = auth_ctx;\n    if let Err(e) = identity_policy::judge_call(&state, &mut auth_ctx).await { return refuse(e); }\n    serve(auth_ctx)",
    );
    assert!(violations("x.rs", &judged, &[]).is_empty());
    let commented = leaking.replace(
        "    serve(auth_ctx)",
        "    // identity_policy::judge_call( later\n    serve(auth_ctx)",
    );
    assert_eq!(violations("x.rs", &commented, &[]), vec!["x.rs::proxy"]);
    let exempt = [("x.rs", "proxy", "a reason")];
    assert!(violations("x.rs", leaking, &exempt).is_empty());
    // A route that authenticates through the media helper is judged inside it.
    let media = r#"
pub(crate) async fn upload(state: S) -> Response {
    let caller = crate::routes::request::extract_media_caller(&state, h, t, ip, p).await;
    serve(caller).await
}
"#;
    assert!(violations("x.rs", media, &[]).is_empty());
}
