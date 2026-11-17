// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: one set of approvals per request, shared by every gate it passes.
//!
//! A request that is held for a person is retried with the one-time token
//! of each approved hold (`agent_approval::RequestApprovals`). The object is
//! built once, by the route that received the request, and handed to every
//! gate the request passes: the route's own and those of the skill it runs.
//! Two shapes break that, and both compile:
//!
//! * a surface that builds its `SkillExecuteRequest` with an empty set of
//!   approvals can raise the skill's hold and never redeem it: the call is
//!   held, approved, and can still not proceed;
//! * a gate that reads the headers into a set of its own does not know what
//!   the request's other gates redeemed: the second gate that asks the same
//!   question finds the token spent and refuses the call.
//!
//! The scan reads the production code of both editions.

#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::Path;

/// The lines of `code` that state an empty set of approvals: a request
/// built with one, or a gate handed one.
fn tokenless(code: &str) -> Vec<String> {
    code.split("#[cfg(test)]")
        .next()
        .unwrap_or(code)
        .lines()
        .map(str::trim)
        .filter(|l| !l.starts_with("//"))
        .filter(|l| {
            let l = l.replace(' ', "");
            l.contains("RequestApprovals::none()")
                || l.contains("RequestApprovals::default()")
                || l.starts_with("approvals:Default::default()")
                || l.starts_with("approvals:std::sync::Arc::default()")
                || l.starts_with("approvals:Arc::default()")
                || l.starts_with("approvals:&Default::default()")
        })
        .map(str::to_string)
        .collect()
}

/// How often `code` reads a request's headers into a set of approvals.
fn header_readings(code: &str) -> usize {
    code.split("#[cfg(test)]")
        .next()
        .unwrap_or(code)
        .lines()
        .filter(|l| !l.trim().starts_with("//"))
        .filter(|l| l.contains("RequestApprovals::from_headers("))
        .count()
}

fn scan(
    dir: &Path,
    root: &Path,
    found: &mut Vec<String>,
    readings: &mut Vec<(String, usize)>,
    read: &mut usize,
) {
    for entry in fs::read_dir(dir).unwrap_or_else(|e| panic!("{}: {e}", dir.display())) {
        let path = entry.expect("entry").path();
        if path.is_dir() {
            scan(&path, root, found, readings, read);
        } else if path.extension().is_some_and(|e| e == "rs") {
            let src = fs::read_to_string(&path).expect("read");
            *read += 1;
            let rel = path
                .strip_prefix(root)
                .expect("under the root")
                .display()
                .to_string();
            // The type's own file defines the empty set.
            if rel != "src/services/agent_approval.rs" {
                for line in tokenless(&src) {
                    found.push(format!("{rel}: {line}"));
                }
            }
            let n = header_readings(&src);
            if n > 0 {
                readings.push((rel, n));
            }
        }
    }
}

#[test]
fn every_gate_of_a_request_shares_the_requests_approvals() {
    let community = roots::tree("community").expect("the Community root");
    let mut found = Vec::new();
    let mut readings = Vec::new();
    let mut read = 0;
    scan(
        &community.join("src"),
        &community,
        &mut found,
        &mut readings,
        &mut read,
    );
    assert!(read > 200, "the scan read only {read} Community files");
    if let Some(enterprise) = roots::tree("enterprise") {
        let before = read;
        let mut theirs = Vec::new();
        scan(
            &enterprise.join("src"),
            &enterprise,
            &mut found,
            &mut theirs,
            &mut read,
        );
        assert!(
            read - before > 20,
            "the scan read only {} Enterprise files",
            read - before
        );
        assert!(
            theirs.is_empty(),
            "an Enterprise route reads approval tokens itself: {theirs:?}"
        );
    }
    assert!(
        found.is_empty(),
        "a gate or a skill execution is given an empty set of approvals (hand it the \
         request's `RequestApprovals`, so a held call can proceed once it is approved):\n{}",
        found.join("\n")
    );

    // One reading of the headers per route handler: the external MCP path
    // and the system-server path, A2A outbound and inbound, the v1.0
    // request context, the skill route. A second reading inside one handler
    // is a second set, which does not know what the first redeemed.
    readings.sort();
    assert_eq!(
        readings,
        vec![
            ("src/routes/a2a.rs".to_string(), 2),
            ("src/routes/a2a_v1.rs".to_string(), 1),
            ("src/routes/mcp.rs".to_string(), 2),
            ("src/routes/skills.rs".to_string(), 1),
        ],
        "a request's approval tokens are read once, by its route"
    );

    // Each surface that builds a skill execution hands it the request's set.
    let mut builders = 0;
    for file in [
        "src/routes/a2a.rs",
        "src/routes/a2a_v1.rs",
        "src/services/system_server.rs",
    ] {
        let src = fs::read_to_string(community.join(file)).expect("read");
        let code = src.split("#[cfg(test)]").next().unwrap_or(&src);
        let at = code
            .find("SkillExecuteRequest {")
            .unwrap_or_else(|| panic!("{file} no longer builds a SkillExecuteRequest"));
        let literal = &code[at..];
        let literal = &literal[..literal.find("\n}").expect("end of the function")];
        assert!(
            literal.contains("approvals: approvals.clone(),"),
            "{file}: the skill execution is not handed the request's approvals"
        );
        builders += 1;
    }
    assert_eq!(builders, 3);
    let rest = fs::read_to_string(community.join("src/routes/skills.rs")).expect("read");
    assert!(
        rest.contains("request.approvals = "),
        "the skill route no longer sets the request's approvals"
    );
}

/// The scan finds the shapes it forbids and leaves a shared set alone.
#[test]
fn the_scan_finds_an_empty_set_and_counts_a_reading() {
    for empty in [
        "        approvals: Default::default(),",
        "approvals:std::sync::Arc::default(),",
        "let a = agent_approval::RequestApprovals::none();",
        "approvals: &RequestApprovals::default(),",
    ] {
        assert_eq!(tokenless(empty).len(), 1, "{empty}");
    }
    for shared in [
        "approvals: approvals.clone(),",
        "approvals: &approvals,",
        "approvals: call.approvals,",
        "// approvals: Default::default(),",
        "pub approvals: std::sync::Arc<RequestApprovals>,",
        "#[cfg(test)]\nmod tests { fn f() { approvals: Default::default(), } }",
    ] {
        assert!(tokenless(shared).is_empty(), "{shared}");
    }
    assert_eq!(
        header_readings(
            "let a = RequestApprovals::from_headers(&headers);\n\
             // RequestApprovals::from_headers(&headers)\n\
             let b = agent_approval::RequestApprovals::from_headers(headers);\n\
             #[cfg(test)]\nRequestApprovals::from_headers(h)"
        ),
        2
    );
}
