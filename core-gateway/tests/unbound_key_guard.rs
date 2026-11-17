// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: who may act without a resource group, decided in one place.
//!
//! The bug class: a capability granted by something the CALLER controls.
//! `scope = 'global'` is a tenant's shared key (a tenant admin mints it), and
//! `X-Brutor-Internal-Health-Check` is a header anyone can send. Each was
//! read on its own as "this is the platform": the probe exemption, the
//! system routes, the usage-ledger skip. The platform is identified only by
//! `api_key_is_platform` (`api_keys.is_platform_service`, set by the control
//! plane's seed), and a key no group governs is refused once, at
//! authentication. Behaviour is pinned by `tests/integration/unbound_keys.rs`;
//! this pins the shape, so a new route cannot reintroduce it quietly.

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

/// Source lines outside `#[cfg(test)]` modules and comments.
fn code_lines(src: &str) -> Vec<(usize, String)> {
    let cut = src.find("#[cfg(test)]").unwrap_or(src.len());
    src[..cut]
        .lines()
        .enumerate()
        .filter(|(_, l)| !l.trim_start().starts_with("//"))
        .map(|(i, l)| (i + 1, l.to_string()))
        .collect()
}

fn all_src() -> Vec<(String, String)> {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    files
        .into_iter()
        .map(|p| (p.display().to_string(), fs::read_to_string(&p).unwrap()))
        .collect()
}

/// The probe header is read in exactly these places, and every read that
/// grants anything is joined to the platform flag on the same line.
#[test]
fn the_probe_header_counts_only_with_the_platform_key() {
    // file → whether a read there must carry `api_key_is_platform`
    let allowed: &[(&str, bool)] = &[
        ("src/services/catalog_probe.rs", false), // the constant + `has_probe_header`, wrapped by `is_health_probe_request(headers, auth)`
        ("src/services/llm_preflight.rs", true),  // the access exemption
        ("src/routes/request.rs", true), // the synthetic portal identity (extract_portal_user)
        ("src/routes/llm_helpers.rs", false), // cache bypass only: costs the caller, grants nothing
    ];
    let mut bad = Vec::new();
    for (file, src) in all_src() {
        for (n, line) in code_lines(&src) {
            if !(line.contains("internal-health-check") || line.contains("HEALTH_PROBE_HEADER")) {
                continue;
            }
            match allowed.iter().find(|(f, _)| *f == file) {
                None => bad.push(format!("{file}:{n}: reads the probe header: use catalog_probe::is_health_probe_request(headers, auth)")),
                Some((_, true)) if !line.contains("api_key_is_platform") => {
                    bad.push(format!("{file}:{n}: probe header read without the platform key"))
                }
                _ => {}
            }
        }
    }
    assert!(bad.is_empty(), "{}", bad.join("\n"));
}

/// No access decision reads `scope == 'global'` as "the platform": that
/// scope is a tenant's shared key. One non-access use remains.
#[test]
fn a_global_scope_is_never_taken_for_the_platform() {
    let allowed = ["enterprise/src/batch/routes.rs"]; // job-ownership visibility, not access
    let mut bad = Vec::new();
    for (file, src) in all_src() {
        if allowed.contains(&file.as_str()) {
            continue;
        }
        for (n, line) in code_lines(&src) {
            if line.contains("api_key_scope") && line.contains("\"global\"") {
                bad.push(format!("{file}:{n}: {}", line.trim()));
            }
        }
    }
    assert!(
        bad.is_empty(),
        "decide on api_key_is_platform instead:\n{}",
        bad.join("\n")
    );
}

/// The unbound-key rule runs once, in authentication, before any route.
#[test]
fn authentication_refuses_a_key_no_group_governs() {
    let auth = fs::read_to_string("src/services/auth.rs").unwrap();
    let body = &auth[auth.find("if let Some(key) = api_key {").unwrap()..];
    let valid = body.find("key.is_valid()").unwrap();
    let rule = body
        .find("unbound_key_refusal(&key)?")
        .expect("auth must apply the unbound-key rule");
    let ctx = body.find("return Ok(AuthContext {").unwrap();
    assert!(
        valid < rule && rule < ctx,
        "the rule must run before an AuthContext is issued"
    );
    // Both key lookups join the group's active flag the rule reads.
    assert_eq!(auth.matches("rg.is_active as group_active").count(), 2);
}

/// Control-plane-facing routes admit the platform key, not any global key.
#[test]
fn service_routes_admit_only_the_platform_key() {
    let src = fs::read_to_string("src/services/service_auth.rs").unwrap();
    assert!(
        src.contains(
            "AuthType::ApiKey if ctx.api_key_is_platform => ServicePrincipal::PlatformKey"
        )
    );
    assert!(
        !src.contains("GlobalKey"),
        "the principal is the platform key, named as such"
    );
}

/// An API key is billed to its own group or none: never the user/Default
/// fall-through below it.
#[test]
fn an_api_key_never_falls_through_to_another_group() {
    let usage = fs::read_to_string("src/services/usage.rs").unwrap();
    // The resolution lives in the `_checked` form, which returns the lookup
    // error instead of reading it as "no group";
    // `resolve_target_group` only maps that error to `None`.
    let start = usage
        .find("pub async fn resolve_target_group_checked")
        .unwrap();
    let body = &usage[start..];
    let api = body
        .find("if auth.auth_type == crate::models::auth::AuthType::ApiKey {")
        .expect("API-key branch");
    let user = body.find("// 2. User's group membership").unwrap();
    let branch = &body[api..user];
    assert!(
        branch.trim_end().ends_with("return Ok(None);\n    }")
            || branch.contains("        return Ok(None);\n    }"),
        "the API-key branch must end in `return Ok(None)`, not fall through:\n{branch}"
    );
}
