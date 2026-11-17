// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: the tenant layer names no Enterprise path, and decides from the
//! registry.
//!
//! The Community tenant middleware (`src/middleware/tenant.rs`, Critical
//! class) used to hold three prefix lists, with the Enterprise portal's
//! prefixes among them, and treated a path on no list as "no tenant
//! required". The lists are now the registry `services::tenant_paths`, which
//! the Community Edition fills with its own paths and an Enterprise piece
//! adds to when it is installed; a path nobody registered gets the most
//! restrictive rule.
//!
//! This guard keeps the old shape from coming back: the middleware and the
//! Community registry name no Enterprise path, the middleware holds no path
//! list of its own and asks the registry, and the registry's default is the
//! most restrictive rule. Proven against the old shape
//! ([`the_guard_catches_the_old_shape`]); the behaviour is pinned by
//! `tests/integration/tenant_resolution_matrix.rs`.

#[path = "support/roots.rs"]
mod roots;

use std::fs;

const MIDDLEWARE: &str = "src/middleware/tenant.rs";
const REGISTRY: &str = "src/services/tenant_paths.rs";

/// The Enterprise route prefixes of `editions.yaml`
/// (`core_gateway.enterprise_route_prefixes`), by their first segments. The
/// portal's API, the public theme its sign-in screen reads, batch jobs, the
/// knowledge base's connector icons.
const ENTERPRISE_PATHS: &[&str] = &[
    "/v1/portal",
    "/v1/public",
    "/v1/proxy/batch",
    "/v1/assets/connector-icons",
];

/// The one path the middleware may still name: it chooses the level of a
/// log line (an unauthenticated probe of a system MCP server is expected),
/// not what the request gets.
const ALLOWED_IN_THE_MIDDLEWARE: &[&str] = &["/v1/proxy/mcp/system-"];

/// Lines of `text` that name an Enterprise path, comments and tests
/// included: `file:line: text`.
fn enterprise_paths_named(file: &str, text: &str) -> Vec<String> {
    let mut named = Vec::new();
    for (i, line) in text.lines().enumerate() {
        if ENTERPRISE_PATHS.iter().any(|p| line.contains(p)) {
            named.push(format!("{file}:{}: {}", i + 1, line.trim()));
        }
    }
    named
}

/// Every string literal of `text` that is a path (starts with `/` or `/.`),
/// outside comments.
fn path_literals(text: &str) -> Vec<String> {
    let mut literals = Vec::new();
    for line in text.lines() {
        let line = line.trim_start();
        if line.starts_with("//") {
            continue;
        }
        let mut rest = line;
        while let Some(open) = rest.find('"') {
            let after = &rest[open + 1..];
            let Some(close) = after.find('"') else { break };
            let literal = &after[..close];
            if literal.starts_with('/') && !literal.starts_with("//") {
                literals.push(literal.to_string());
            }
            rest = &after[close + 1..];
        }
    }
    literals
}

/// What is wrong with the middleware's shape; empty when it is right.
fn middleware_faults(text: &str) -> Vec<String> {
    let mut faults = Vec::new();
    for literal in path_literals(text) {
        if !ALLOWED_IN_THE_MIDDLEWARE.contains(&literal.as_str()) {
            faults.push(format!("the middleware names the path {literal:?}"));
        }
    }
    for old in [
        "fn should_skip_tenant_resolution",
        "fn requires_tenant",
        "fn is_runtime_path",
    ] {
        if text.contains(old) {
            faults.push(format!(
                "the middleware has its own path list again: `{old}`"
            ));
        }
    }
    let production = text.split("#[cfg(test)]").next().unwrap_or("");
    if production
        .matches("state.tenant_paths.rule_for(path)")
        .count()
        != 1
    {
        faults.push("the middleware does not ask the registry for the path's rule (once)".into());
    }
    for (question, use_of_it) in [
        ("rule.skips_resolution()", "skip tenant resolution"),
        (
            "rule.requires_tenant()",
            "refuse a request without a tenant",
        ),
        ("rule.blocks_suspended()", "block a suspended tenant"),
    ] {
        if production.matches(question).count() != 1 {
            faults.push(format!(
                "the middleware does not use the rule to {use_of_it} (`{question}`, once)"
            ));
        }
    }
    // Nothing else may let a request through without a tenant: the two
    // places a request reaches the inner service unresolved are the open
    // rule and "no tenant, none required".
    if production.matches("inner.call(req).await").count() != 3 {
        faults.push(
            "the middleware passes a request on in a new place (expected: the open rule, no \
             tenant where none is required, and the resolved request)"
                .into(),
        );
    }
    faults
}

#[test]
fn the_tenant_layer_names_no_enterprise_path() {
    let mut named = Vec::new();
    for file in [MIDDLEWARE, REGISTRY] {
        let text = fs::read_to_string(file).unwrap_or_else(|e| panic!("{file}: {e}"));
        named.extend(enterprise_paths_named(file, &text));
    }
    assert!(
        named.is_empty(),
        "the Community tenant layer names an Enterprise path. The piece that owns the path \
         registers it (`state.tenant_paths.register`) when it is installed:\n{}",
        named.join("\n")
    );
}

#[test]
fn the_middleware_decides_from_the_registry() {
    let text = fs::read_to_string(MIDDLEWARE).unwrap();
    let faults = middleware_faults(&text);
    assert!(faults.is_empty(), "{faults:#?}");
}

#[test]
fn the_registrys_default_is_the_most_restrictive_rule() {
    let text = fs::read_to_string(REGISTRY).unwrap();
    let production = text.split("#[cfg(test)]").next().unwrap_or("");
    let rule_for = &production[production.find("pub fn rule_for(").expect("rule_for")..];
    let rule_for = &rule_for[..rule_for.find("\n    }\n").expect("end of rule_for")];
    assert!(
        rule_for.contains("None => TenantRule::MOST_RESTRICTIVE,"),
        "a path nobody registered must get the most restrictive rule:\n{rule_for}"
    );
    assert!(
        production.contains(
            "pub const MOST_RESTRICTIVE: Self = Self::Resolve {\n        tenant_required: true,\n        blocked_when_suspended: true,\n    };"
        ),
        "the most restrictive rule requires a tenant and blocks a suspended one"
    );
    // The only places an open or optional rule is named are the table and
    // the constants: nothing computes one.
    for forbidden in [
        "unwrap_or(TenantRule::Open",
        "unwrap_or(TenantRule::TENANT_OPTIONAL",
        "unwrap_or_default",
    ] {
        assert!(
            !production.contains(forbidden),
            "`{forbidden}` in the registry"
        );
    }
}

/// `editions.yaml` lists the Enterprise route prefixes; when it is there,
/// every one of them is under a path this guard forbids.
#[test]
fn the_guard_knows_every_enterprise_prefix() {
    let Some(records) = roots::tree("edition-records") else {
        return; // skipped, and recorded: the file is not here
    };
    let path = records.join("editions.yaml");
    let text = fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("editions.yaml at {}: {e}", path.display()));
    let core = &text[text.find("\ncore_gateway:\n").expect("core_gateway")..];
    let block = &core[core
        .find("  enterprise_route_prefixes:\n")
        .expect("prefixes")..];
    let block = &block[..block.find("\n\n").expect("end of the block")];
    let mut prefixes = Vec::new();
    for line in block.lines().skip(1) {
        for word in line.split(|c: char| c.is_whitespace() || c == '[' || c == ']' || c == ',') {
            if word.starts_with('/') {
                prefixes.push(word.to_string());
            }
        }
    }
    assert!(prefixes.len() >= 10, "{prefixes:?}");
    for prefix in prefixes {
        assert!(
            ENTERPRISE_PATHS.iter().any(|p| prefix.starts_with(p)),
            "{prefix} is an Enterprise route prefix this guard does not look for"
        );
    }
}

/// The middleware as it was before the registry.
const OLD_SHAPE: &str = r#"
pub fn should_skip_tenant_resolution(path: &str) -> bool {
    if path == "/health" || path == "/healthz" || path == "/v1/version" || path == "/v1/auth/info" {
        return true;
    }
    if path.starts_with("/v1/proxy/oauth/") {
        return true;
    }
    // Public, unauthenticated read-only surface
    if path.starts_with("/v1/public/") {
        return true;
    }
    false
}
pub fn requires_tenant(path: &str) -> bool {
    path.starts_with("/v1/proxy/") || path.starts_with("/v1/messages")
}
pub fn is_runtime_path(path: &str) -> bool {
    path.starts_with("/v1/proxy/")
        || path.starts_with("/v1/messages")
        || path.starts_with("/v1/portal/")
        || path.starts_with("/v1/approvals/")
        || (path.starts_with("/v1/runs/") && path.ends_with("/feedback"))
        || path.starts_with("/v1beta/")
}
fn call() {
    if should_skip_tenant_resolution(path) {
        return inner.call(req).await;
    }
    if requires_tenant(path) {
        if path.starts_with("/v1/proxy/mcp/system-") {}
    }
    return inner.call(req).await;
    if ctx.is_blocked() && is_runtime_path(path) {}
    inner.call(req).await
}
"#;

#[test]
fn the_guard_catches_the_old_shape() {
    let named = enterprise_paths_named("old", OLD_SHAPE);
    assert_eq!(named.len(), 2, "{named:#?}");
    assert!(named[0].contains("/v1/public/") && named[1].contains("/v1/portal/"));

    let faults = middleware_faults(OLD_SHAPE);
    for expected in [
        "names the path \"/v1/portal/\"",
        "names the path \"/v1/public/\"",
        "names the path \"/v1/proxy/\"",
        "names the path \"/health\"",
        "`fn should_skip_tenant_resolution`",
        "`fn requires_tenant`",
        "`fn is_runtime_path`",
        "does not ask the registry",
        "`rule.requires_tenant()`",
    ] {
        assert!(
            faults.iter().any(|f| f.contains(expected)),
            "not caught: {expected}\n{faults:#?}"
        );
    }
    // The one allowed literal is not a fault, and a comment is not code.
    assert!(!faults.iter().any(|f| f.contains("system-")));
    assert_eq!(
        path_literals("// path.starts_with(\"/v1/portal/\")\nlet x = \"/v1/y\";"),
        vec!["/v1/y".to_string()]
    );

    // A path let through in a new place.
    let good = fs::read_to_string(MIDDLEWARE).unwrap();
    assert!(middleware_faults(&good).is_empty());
    let leak = good.replacen(
        "let rule = state.tenant_paths.rule_for(path);",
        "let rule = state.tenant_paths.rule_for(path);\n            if path.len() > 400 {\n                return inner.call(req).await;\n            }",
        1,
    );
    assert_ne!(leak, good);
    assert!(!middleware_faults(&leak).is_empty());
    // An Enterprise path back in the middleware, as a comparison or a note.
    let back = good.replacen(
        "if rule.skips_resolution() {",
        "if rule.skips_resolution() || path.starts_with(\"/v1/portal/auth/\") {",
        1,
    );
    assert_ne!(back, good);
    assert!(!enterprise_paths_named(MIDDLEWARE, &back).is_empty());
    assert!(!middleware_faults(&back).is_empty());
}
