// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Static guards for agent implementation identity.
//!
//! Three failure modes are *absences* or *reads in the wrong place*, which no
//! behavioural test can catch in general, so the source is scanned (the house
//! pattern of `vocabulary_guard.rs` / `llm_surface_evidence_guard.rs`):
//!
//! 1. **Every `proxy_logs` row is stamped.** A `ProxyLogInsert` built anywhere
//!    but `proxy_logger::build_proxy_log` would carry no release, no source
//!    and no client label — silently, on whichever surface built it.
//! 2. **Baggage never leaves.** Every loop that copies the caller's headers
//!    to an outbound request must drop `baggage`, or be an allowlist
//!    whose target is the gateway itself.
//! 3. **Identity decides; declarations describe.** No authorization or
//!    access path may read anything an agent declares about its software.
//!
//! Each guard was proven to fail by temporarily re-introducing the bug.

#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::{Path, PathBuf};

/// Is `file` one this run has? A file of the Enterprise workspace is not, in
/// a Community tree alone (`tests/support/roots.rs`); what this guard holds
/// about it is held where the workspace is.
fn here(file: &str) -> bool {
    !file.starts_with("enterprise/") || roots::tree("enterprise").is_some()
}

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) {
    for e in fs::read_dir(dir).unwrap().flatten() {
        let p = e.path();
        if p.is_dir() {
            rust_files(&p, out);
        } else if p.extension().and_then(|s| s.to_str()) == Some("rs") {
            out.push(p);
        }
    }
}

fn read(path: &str) -> String {
    fs::read_to_string(path).unwrap_or_else(|e| panic!("read {path}: {e}"))
}

/// Production code only: everything before the file's first `#[cfg(test)]`
/// (test modules sit at the end of a file in this crate), and nothing from a
/// `*_tests.rs` sibling.
fn production_part(src: &str) -> &str {
    src.find("#[cfg(test)]").map_or(src, |i| &src[..i])
}

/// Source with `//` comments removed, so a doc comment naming a field is not
/// mistaken for a read of it.
fn code_only(src: &str) -> String {
    src.lines()
        .map(|l| match l.find("//") {
            Some(i) if !l[..i].contains('"') => &l[..i],
            _ => l,
        })
        .collect::<Vec<_>>()
        .join("\n")
}

// ---------------------------------------------------------------------------
// 1. Every proxy_logs row goes through the one stamping path
// ---------------------------------------------------------------------------

#[test]
fn every_proxy_log_row_is_built_by_the_stamping_path() {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    let mut offenders = Vec::new();
    for f in &files {
        let name = f.to_string_lossy().to_string();
        if name.ends_with("_tests.rs") {
            continue;
        }
        let src = fs::read_to_string(f).unwrap();
        let code = code_only(production_part(&src));
        for (i, line) in code.lines().enumerate() {
            let t = line.trim();
            let literal = t.contains("ProxyLogInsert {")
                && !t.contains("struct ProxyLogInsert")
                && !t.contains("-> ProxyLogInsert")
                && !t.contains("-> crate::models::db::ProxyLogInsert")
                && !t.starts_with("impl");
            if literal && name != "src/services/proxy_logger.rs" {
                offenders.push(format!("{name}:{}: {t}", i + 1));
            }
        }
    }
    assert!(
        offenders.is_empty(),
        "a ProxyLogInsert is constructed outside proxy_logger::build_proxy_log, so its row \
         would carry no agent release / source / client label. Build it with \
         build_proxy_log:\n{}",
        offenders.join("\n")
    );

    // …and inside proxy_logger there is exactly one construction, in
    // build_proxy_log, which stamps the carriers before returning.
    let logger = read("src/services/proxy_logger.rs");
    let prod = code_only(production_part(&logger));
    assert_eq!(
        prod.matches("ProxyLogInsert {").count() - prod.matches("-> ProxyLogInsert {").count(),
        1,
        "proxy_logger must construct ProxyLogInsert in exactly one place"
    );
    let start = prod
        .find("pub fn build_proxy_log(")
        .expect("build_proxy_log");
    let body = &prod[start..];
    let end = body.find("\n}\n").expect("end of build_proxy_log");
    let body = &body[..end];
    assert!(
        body.contains("ProxyLogInsert {")
            && body.contains("crate::services::agent_impl::stamp(&mut log, &auth.agent_impl)"),
        "build_proxy_log must stamp the request's agent implementation carriers on every row"
    );
}

#[test]
fn the_writer_persists_every_agent_impl_column() {
    let w = read("src/services/background_writer.rs");
    let insert = &w[w.find("INSERT INTO proxy_logs (").expect("insert")..];
    let insert = &insert[..insert.find("qb.build()").expect("end")];
    for col in [
        "agent_impl_name",
        "agent_impl_version",
        "agent_impl_build",
        "agent_impl_source",
        "client_label",
    ] {
        assert!(
            insert.contains(col) && insert.contains(&format!("push_bind(&log.{col})")),
            "background writer does not persist proxy_logs.{col}"
        );
    }
}

// ---------------------------------------------------------------------------
// 2. Caller baggage is never forwarded upstream
// ---------------------------------------------------------------------------

/// Files with a loop copying the INBOUND request headers onto an outbound
/// request. A new such loop fails here until it is classified.
const HEADER_COPY_SITES: &[&str] = &["src/routes/mcp.rs", "src/routes/vmcp.rs"];

#[test]
fn every_inbound_header_copy_drops_baggage() {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    let mut found = Vec::new();
    for f in &files {
        let name = f.to_string_lossy().to_string();
        let src = fs::read_to_string(f).unwrap();
        let code = code_only(production_part(&src));
        // A copy loop iterates the inbound headers and inserts the caller's
        // own values into another header map. (Logging loops that render
        // headers into JSON do not forward anything.)
        let copies = code.match_indices("in headers.iter() {").any(|(i, _)| {
            let window: String = code[i..].chars().take(6000).collect();
            window.contains("insert(name, value.clone())")
        });
        if copies {
            found.push(name);
        }
    }
    found.sort();
    let mut expected: Vec<String> = HEADER_COPY_SITES.iter().map(|s| s.to_string()).collect();
    expected.sort();
    assert_eq!(
        found, expected,
        "the set of inbound-header copy loops changed — classify the new one: it must drop \
         `baggage` via agent_impl::is_caller_only_header"
    );

    // The MCP proxy copies everything but a denylist to a THIRD PARTY: it
    // must consult the caller-only check.
    let mcp = code_only(&read("src/routes/mcp.rs"));
    assert!(
        mcp.contains("crate::services::agent_impl::is_caller_only_header(&key_str)"),
        "the MCP upstream header copy must drop caller-only headers (baggage)"
    );

    // VMCP forwards an ALLOWLIST to the gateway's own MCP route (still inside
    // the gateway), which then applies the check above.
    let vmcp = code_only(&read("src/routes/vmcp.rs"));
    let fwd = &vmcp[vmcp.find("fn forward_headers(").expect("forward_headers")..];
    let fwd = &fwd[..fwd.find("\n}\n").unwrap()];
    assert!(
        fwd.contains("matches!("),
        "VMCP forward_headers must stay an allowlist"
    );
    assert!(
        vmcp.contains("self_call::BASE_URL") || vmcp.contains("/v1/proxy/mcp"),
        "VMCP's forward target must be the gateway's own MCP route"
    );
}

// ---------------------------------------------------------------------------
// 3. No access path reads a declaration
// ---------------------------------------------------------------------------

/// Every path that decides whether a call may proceed: grants and the
/// autonomy gate, accepted callers, authentication, run controls and budgets,
/// approvals, tenant resolution. A rename here must be deliberate — a missing
/// file fails the guard rather than silently shrinking it.
const ACCESS_PATHS: &[&str] = &[
    "src/services/agent_authz.rs",
    "src/services/accepted_callers.rs",
    "src/services/auth.rs",
    // What verifies a session token moved out of auth.rs.
    "src/services/credential_verifier.rs",
    "enterprise/src/portal_session.rs",
    "src/services/workload_identity.rs",
    "src/services/service_auth.rs",
    "src/services/cimd.rs",
    "src/services/autonomy.rs",
    "src/services/agent_approval.rs",
    "src/services/run_controls.rs",
    "src/services/run_intervention.rs",
    "src/services/run_budget.rs",
    "src/services/session_budget.rs",
    "src/middleware/tenant.rs",
];

/// What an agent declares about its software, in every shape code could
/// read it: the AuthContext field, the stored columns, the declaration on
/// the identity, the release table.
const DECLARATION_READS: &[&str] = &[
    ".agent_impl",
    "agent_impl_name",
    "agent_impl_version",
    "agent_impl_build",
    "agent_impl_source",
    "agent_impl_trust",
    "implementation_fingerprint",
    "implementation_name",
    "approved_versions",
    "require_release",
    "agent_implementation_releases",
    "client_label",
    "x-brutor-agent-",
    "gen_ai.agent.",
    "clientInfo",
    "X_BRUTOR_AGENT_",
    "X_BRUTOR_CLIENT",
];

/// The only mentions of the module an access path may make: building the
/// AuthContext's snapshot (a write, never a read) and, in the verified
/// workload branch, attaching the verified claims.
const ALLOWED: &[&str] = &[
    "agent_impl: crate::services::agent_impl::request_carriers()",
    ".with_verified_token(resolved.software)",
    "crate::services::agent_impl::Release",
    "crate::services::agent_impl::release_from_claims(",
];

#[test]
fn no_access_path_reads_what_an_agent_declares() {
    let mut offenders = Vec::new();
    for path in ACCESS_PATHS.iter().copied().filter(|p| here(p)) {
        let src = read(path);
        let code = code_only(production_part(&src));
        for (i, line) in code.lines().enumerate() {
            let mut l = line.to_string();
            for ok in ALLOWED {
                l = l.replace(ok, "");
            }
            if l.contains("services::agent_impl::") || l.contains("agent_impl::") {
                offenders.push(format!("{path}:{}: {}", i + 1, line.trim()));
                continue;
            }
            if let Some(pat) = DECLARATION_READS.iter().find(|p| l.contains(*p)) {
                offenders.push(format!("{path}:{}: [{pat}] {}", i + 1, line.trim()));
            }
        }
    }
    assert!(
        offenders.is_empty(),
        "an authorization/access path reads an agent's DECLARED implementation. \
         Identity decides; declarations describe — only the credential may influence access:\n{}",
        offenders.join("\n")
    );
}

#[test]
fn the_workload_agent_is_resolved_by_subject_alone() {
    // The verified software claims ride out of workload_identity, but the
    // agent lookup must not take them as an input.
    let src = code_only(&read("src/services/workload_identity.rs"));
    let lookup = &src[src.find("FROM agent_identities").expect("agent lookup")..];
    let lookup = &lookup[..lookup.find(".fetch_optional").unwrap()];
    assert!(
        !lookup.contains("software"),
        "the workload agent lookup must bind (tenant, idp, sub) only — never a software claim"
    );
}
