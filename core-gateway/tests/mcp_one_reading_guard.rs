// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: an MCP body is read ONCE, and the system-server branch asks its
//! gates in one order.
//!
//! Why it matters: every MCP control decides from the parsed message. When a
//! control (or the executor) parses the bytes again, a body the two readings
//! disagree on is judged as one thing and executed as another.
//! `jsonrpc_body::read` is the one reading; this guard keeps
//! the system-server branch (`handle_system_server` and the system server it
//! dispatches to) on it, stops a NEW re-parse from appearing on the proxy
//! path, and pins the order of the system-server gates: run controls, then
//! the run's data scope, then rate limits, then the argument policy, then
//! the tool.

#[path = "support/roots.rs"]
mod roots;

use std::fs;

/// A file without its test section.
fn production(src: &str) -> &str {
    src.split("#[cfg(test)]").next().unwrap_or(src)
}

/// The text of the function that starts at `signature`, up to the next
/// top-level item.
fn function_body<'a>(code: &'a str, signature: &str) -> &'a str {
    let at = code
        .find(signature)
        .unwrap_or_else(|| panic!("`{signature}` is gone; update this guard"));
    let rest = &code[at..];
    let end = rest.find("\n}\n").expect("end of the function");
    &rest[..end]
}

/// Ways of reading the request body a second time.
fn reparses(code: &str) -> Vec<&str> {
    code.lines()
        .map(str::trim)
        .filter(|l| !l.starts_with("//"))
        .filter(|l| {
            l.contains("extract_rpc_id(")
                || ((l.contains("serde_json::from_slice") || l.contains("serde_json::from_str"))
                    && (l.contains("body_bytes") || l.contains("(body)")))
        })
        .collect()
}

#[test]
fn the_system_server_branch_reads_the_body_once() {
    let mcp = fs::read_to_string("src/routes/mcp.rs").expect("read mcp.rs");
    let body = function_body(production(&mcp), "async fn handle_system_server(");
    assert_eq!(
        body.matches("jsonrpc_body::read(").count(),
        1,
        "handle_system_server must read the body exactly once"
    );
    assert_eq!(
        reparses(body),
        Vec::<&str>::new(),
        "handle_system_server parses the request bytes again; read `parsed_rpc` instead"
    );

    // The system server executes the route's reading: it is handed the
    // parsed message and has no bytes to parse.
    let sys = fs::read_to_string("src/services/system_server.rs").expect("read system_server.rs");
    let handler = function_body(production(&sys), "pub async fn handle_request(");
    assert!(
        handler.contains("rpc_request: Option<&Value>") && !handler.contains("body_bytes"),
        "system_server::handle_request must take the parsed message, not the bytes"
    );
}

/// There are re-parses of the (already read) bytes on the proxy
/// path; they agree with the one reading because the reader refuses what a
/// last-wins parser would read differently. The count may only go down.
#[test]
fn no_new_second_parse_on_the_proxy_path() {
    const CEILING: usize = 10;
    let mcp = fs::read_to_string("src/routes/mcp.rs").expect("read mcp.rs");
    let code = production(&mcp);
    let found: Vec<&str> = reparses(code)
        .into_iter()
        .filter(|l| !l.starts_with("fn extract_rpc_id("))
        .collect();
    assert!(
        found.len() <= CEILING,
        "a new second parse of the request body in routes/mcp.rs ({} > {CEILING}); every \
         control reads `parsed_body` (jsonrpc_body::read):\n{}",
        found.len(),
        found.join("\n")
    );
    assert_eq!(
        found.len(),
        CEILING,
        "a second parse was removed: lower CEILING to {}",
        found.len()
    );
}

#[test]
fn the_system_server_gates_run_in_one_order() {
    let mcp = fs::read_to_string("src/routes/mcp.rs").expect("read mcp.rs");
    let body = function_body(production(&mcp), "async fn handle_system_server(");
    let at = |needle: &str| {
        body.find(needle)
            .unwrap_or_else(|| panic!("handle_system_server no longer calls `{needle}`"))
    };
    let order = [
        at("jsonrpc_body::read("),
        at("run_controls::check("),
        at("data_scope::gate("),
        at("usage::admit_mcp_rate_limits("),
        at("argument_policy::enforce("),
        at("agent_approval::require_or_enqueue("),
        at("system_server::handle_request("),
    ];
    assert!(
        order.windows(2).all(|w| w[0] < w[1]),
        "the system-server gates are out of order (read, run controls, data scope, rate \
         limits, argument policy, approval hold, tool): {order:?}"
    );
    // The run controls are asked for every tools/call, not only when the
    // caller has a group or the call names a tool: the check sits outside
    // the `(tool, group)` block the rate limits and the policy share.
    let gate = &body[order[1]..order[3]];
    assert!(
        !body[..order[1]].contains("if let (Some(tool), Some(group))")
            && gate.contains("if let (Some(tool), Some(group))"),
        "run_controls::check must sit before the `(tool, group)` block"
    );

    // And the route is the only way in: a caller that reached the system
    // server without it would skip every gate above.
    let mut callers = Vec::new();
    let mut stack = vec![std::path::PathBuf::from("src")];
    if let Some(enterprise) = roots::tree("enterprise") {
        stack.push(enterprise.join("src"));
    }
    while let Some(dir) = stack.pop() {
        for entry in fs::read_dir(&dir).expect("read dir").flatten() {
            let path = entry.path();
            if path.is_dir() {
                stack.push(path);
            } else if path.extension().is_some_and(|e| e == "rs") {
                let src = fs::read_to_string(&path).expect("read source");
                if production(&src).lines().any(|l| {
                    !l.trim().starts_with("//") && l.contains("system_server::handle_request(")
                }) {
                    callers.push(path.to_string_lossy().replace('\\', "/"));
                }
            }
        }
    }
    assert_eq!(callers, vec!["src/routes/mcp.rs".to_string()]);
}

/// The guards prove themselves against the shapes they forbid.
#[test]
fn the_guard_sees_a_second_parse_and_a_gate_out_of_order() {
    let bad = "async fn handle_system_server(\n    let rpc_id = extract_rpc_id(&body_bytes);\n    \
               let p: Option<Value> = serde_json::from_slice(&body_bytes).ok();\n    // \
               serde_json::from_slice(&body_bytes) in a comment is not a parse\n}\n";
    let body = function_body(bad, "async fn handle_system_server(");
    assert_eq!(reparses(body).len(), 2);

    let late = "argument_policy::enforce( … run_controls::check(";
    assert!(late.find("run_controls::check(") > late.find("argument_policy::enforce("));
}
