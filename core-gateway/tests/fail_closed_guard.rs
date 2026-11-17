// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Fail-closed guard (platform rule): a control
//! that cannot decide refuses.
//!
//! These scan the source because the failure is silent by construction: a
//! control that turns "could not load" into "nothing to enforce" passes every
//! happy-path test and leaves a clean audit row. Each rule is proven against
//! the shape that shipped before the fix.
#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::{Path, PathBuf};

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

/// How far after a `.get_configs(` call its error handling must appear.
const GUARDRAIL_WINDOW: usize = 30;

/// `(file, helper)`: route helpers that refuse an unchecked call AND write its
/// `proxy_logs` row (`guardrails_unavailable_guard.rs`). The row
/// context they take is long enough to push the refusal text past the window,
/// so the helper's name stands for it. Each must always answer 503
/// (`every_recording_refuser_refuses`).
const RECORDING_REFUSERS: [(&str, &str); 2] = [
    ("src/routes/mcp.rs", "refuse_unchecked_mcp"),
    ("src/routes/skills.rs", "refuse_unchecked_skill"),
];

/// `line` of each `.get_configs(` in `src` whose next [`GUARDRAIL_WINDOW`]
/// lines never reach the fail-closed path (`configs_unavailable`, the
/// `guardrails_unavailable` refusal of the sites that build their own
/// surface-specific response, or one of [`RECORDING_REFUSERS`]).
fn unguarded_config_loads(src: &str) -> Vec<usize> {
    let lines: Vec<&str> = src.lines().collect();
    let mut out = Vec::new();
    for (i, l) in lines.iter().enumerate() {
        if !l.contains(".get_configs(") {
            continue;
        }
        let end = (i + GUARDRAIL_WINDOW).min(lines.len());
        // Code only: a comment that merely names the refusal does not refuse
        // (a comment must not satisfy this check).
        let window = lines[i..end]
            .iter()
            .filter(|l| !l.trim_start().starts_with("//"))
            .copied()
            .collect::<Vec<_>>()
            .join("\n");
        let refuses = window.contains("configs_unavailable")
            || window.contains("guardrails_unavailable")
            // The typed refusal: `run_and_settle` returns it and every
            // caller must refuse on it (`settle_callers_refuse_unavailable`).
            || window.contains("Settlement::GuardrailsUnavailable(")
            || RECORDING_REFUSERS
                .iter()
                .any(|(_, f)| window.contains(&format!("{f}(")));
        if !refuses {
            out.push(i + 1);
        }
    }
    out
}

/// Every guardrail config load on a request path fails closed.
/// `GuardrailConfigs` (no `Default`, no public constructor) already stops a
/// caller from defaulting the error; this catches the explicit
/// `Err(_) => Pass` that the type cannot.
#[test]
fn every_guardrail_config_load_fails_closed() {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    let mut offenders = Vec::new();
    for f in &files {
        let rel = f.to_string_lossy().replace('\\', "/");
        if rel == "src/services/guardrails/mod.rs" {
            continue; // the definition, not a caller
        }
        let src = fs::read_to_string(f).unwrap();
        for line in unguarded_config_loads(&src) {
            offenders.push(format!("{rel}:{line}"));
        }
    }
    assert!(
        offenders.is_empty(),
        "guardrail config loads that do not fail closed (use \
         `guardrails::configs_unavailable`): {offenders:?}"
    );
}

#[test]
fn the_guardrail_rule_catches_the_shapes_that_shipped() {
    // An explicit pass on a load error.
    let explicit_pass = r#"
        let configs = match state.guardrails_cache.get_configs(t, u, g, &db).await {
            Ok(c) => c,
            Err(e) => {
                tracing::warn!(error = %e, "Failed to load guardrail configs for A2A");
                return Ok(CheckResult::Pass);
            }
        };
    "#;
    assert_eq!(unguarded_config_loads(explicit_pass), vec![2]);
    // A defaulted load.
    let defaulted = r#"
        let configs = state.guardrails_cache.get_configs(t, u, g, &db)
            .await
            .unwrap_or_else(|e| { tracing::error!(error = %e, "x"); vec![] });
    "#;
    assert_eq!(unguarded_config_loads(defaulted), vec![2]);
    let fixed = r#"
        let configs = state.guardrails_cache.get_configs(t, u, g, &db)
            .await
            .map_err(|e| guardrails::configs_unavailable(surface, &e))?;
    "#;
    assert!(unguarded_config_loads(fixed).is_empty());
}

#[test]
fn a_comment_naming_the_refusal_is_not_a_refusal() {
    let commented = r#"
        let configs = match state.guardrails_cache.get_configs(t, u, g, &db).await {
            Ok(c) => c,
            // fail closed: configs_unavailable / guardrails_unavailable
            Err(_) => return Ok(CheckResult::Pass),
        };
    "#;
    assert_eq!(unguarded_config_loads(commented), vec![2]);
}

/// A helper in [`RECORDING_REFUSERS`] satisfies the config-load rule only
/// because it refuses: its body must build the 503 itself, unconditionally
/// (no `if` / `match` that could choose another status).
#[test]
fn every_recording_refuser_refuses() {
    for (file, helper) in RECORDING_REFUSERS {
        let src = fs::read_to_string(file).unwrap();
        let head = format!("fn {helper}(");
        let at = src
            .find(&head)
            .unwrap_or_else(|| panic!("{file}: {helper} is gone; drop it from RECORDING_REFUSERS"));
        let open = at + src[at..].find("{\n").expect("fn body");
        let body: String = src[open + 2..]
            .lines()
            .take_while(|l| !l.starts_with('}'))
            .filter(|l| !l.trim_start().starts_with("//"))
            .collect::<Vec<_>>()
            .join("\n");
        let first = body
            .lines()
            .find(|l| !l.trim().is_empty())
            .unwrap_or_default();
        assert!(
            first.contains("StatusCode::SERVICE_UNAVAILABLE"),
            "{file}::{helper} must open by building its 503: {first:?}"
        );
    }
}

#[test]
fn a_recording_refuser_counts_as_the_refusal() {
    let recorded = r#"
        let configs = match state.guardrails_cache.get_configs(t, u, g, &db).await {
            Ok(c) => c,
            Err(e) => return refuse_unchecked_mcp(ctx, "message"),
        };
    "#;
    assert!(unguarded_config_loads(recorded).is_empty());
    // Only the call counts, not a name in passing.
    let named = recorded.replace(
        "refuse_unchecked_mcp(ctx, \"message\")",
        "pass(refuse_unchecked_mcp)",
    );
    assert_eq!(unguarded_config_loads(&named), vec![2]);
}

/// `run_and_settle` reports an unreadable guardrail config as
/// `Settlement::GuardrailsUnavailable`; every caller must name that variant
/// (and so refuse), never fold it into a wildcard arm.
#[test]
fn settle_callers_refuse_unavailable() {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    let mut callers = 0;
    let mut bad = Vec::new();
    for f in &files {
        let rel = f.to_string_lossy().replace('\\', "/");
        let src = fs::read_to_string(f).unwrap();
        let code: String = src
            .lines()
            .filter(|l| !l.trim_start().starts_with("//"))
            .collect::<Vec<_>>()
            .join("\n");
        let calls = code.matches("run_and_settle(").count()
            - usize::from(code.contains("pub async fn run_and_settle("));
        if calls == 0 {
            continue;
        }
        callers += calls;
        if code.matches("Settlement::GuardrailsUnavailable").count() < calls {
            bad.push(rel);
        }
    }
    assert!(
        callers >= 3,
        "expected REST and both A2A paths to call run_and_settle"
    );
    assert!(
        bad.is_empty(),
        "callers that do not refuse on GuardrailsUnavailable: {bad:?}"
    );
}

// ---------------------------------------------------------------------------
// No awaited read is defaulted
// ---------------------------------------------------------------------------

/// The awaited calls whose error may become an empty value: the text of an
/// upstream's reply, read for an error message or a log line. (An upstream
/// body that is the answer itself is `upstream_body_guard.rs`'s and
/// `limits_guard.rs`'s business.)
const DEFAULTABLE_READS: [&str; 3] = [".text()", ".bytes()", ".json()"];

/// The code of a source file: everything before its unit-test module, with
/// comment lines removed.
fn production_code(src: &str) -> String {
    src.split("\n#[cfg(test)]\nmod tests")
        .next()
        .unwrap_or(src)
        .lines()
        .map(|l| {
            if l.trim_start().starts_with("//") {
                ""
            } else {
                l
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// The line of each `.await.unwrap_or_default()` in `code` (the two may be
/// on separate lines) that defaults something other than an upstream reply's
/// text: a read whose failure became "nothing configured", "no principals",
/// "no rows".
fn defaulted_reads(code: &str) -> Vec<usize> {
    let mut found = Vec::new();
    let mut from = 0;
    while let Some(at) = code[from..].find(".unwrap_or_default()") {
        let at = from + at;
        from = at + 1;
        let before = code[..at].trim_end();
        let Some(awaited) = before.strip_suffix(".await") else {
            continue;
        };
        let awaited = awaited.trim_end();
        if DEFAULTABLE_READS.iter().any(|read| awaited.ends_with(read)) {
            continue;
        }
        found.push(code[..at].lines().count().max(1));
    }
    found
}

/// A governance read that fails is not "nothing to enforce": an awaited
/// result is never replaced by its type's default. `swallowed_db_error_guard`
/// stops this for a query written in place; this stops it for a read behind
/// a function (a cache, a loader, a store), where the query is out of sight.
/// Propagate the error, or name the restrictive value it becomes.
#[test]
fn no_awaited_read_is_defaulted() {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    assert!(files.len() > 100, "the scan found {} files", files.len());
    let mut offenders = Vec::new();
    for f in &files {
        let rel = f.to_string_lossy().replace('\\', "/");
        // The developer benchmark binary enforces nothing.
        if rel == "src/bin/benchmark.rs" {
            continue;
        }
        let src = fs::read_to_string(f).unwrap();
        for line in defaulted_reads(&production_code(&src)) {
            offenders.push(format!("{rel}:{line}"));
        }
    }
    assert!(
        offenders.is_empty(),
        "an awaited read whose error becomes `Default` (propagate it, or refuse): {offenders:?}"
    );
}

#[test]
fn the_defaulted_read_rule_catches_the_shapes_that_shipped() {
    // A loader's error read as "no principals".
    let split = r#"
        let acl = service
            .load_user_acl_principals(&self.tenant_id, uid)
            .await
            .unwrap_or_default();
    "#;
    assert_eq!(defaulted_reads(&production_code(split)), vec![5]);
    // A governance read on one line.
    let inline = "let gov = cache.effective_governance(t, g).await.unwrap_or_default();";
    assert_eq!(defaulted_reads(inline), vec![1]);
    // A store read.
    let store = "let v: Vec<Option<f64>> = cmd.query_async(&mut conn).await.unwrap_or_default();";
    assert_eq!(defaulted_reads(store), vec![1]);
    // An upstream's error text, read for a message.
    let text = "let body = resp.text().await.unwrap_or_default();";
    assert!(defaulted_reads(text).is_empty());
    // Propagated, or matched.
    let fixed = r#"
        let acl = service.load_user_acl_principals(t, uid).await?;
        let gov = match cache.effective_governance(t, g).await {
            Ok(gov) => gov,
            Err(e) => return refuse(e),
        };
        let n = maybe.unwrap_or_default();
    "#;
    assert!(defaulted_reads(&production_code(fixed)).is_empty());
    // A comment is not code; a test module is not production.
    let commented = "// x.load().await.unwrap_or_default()\nfn f() {}\n#[cfg(test)]\nmod tests {\n    \
                     fn t() { let _ = x.load().await.unwrap_or_default(); }\n}\n";
    assert!(defaulted_reads(&production_code(commented)).is_empty());
}

// ---------------------------------------------------------------------------
// Unknown access levels and modes are the most restrictive
// ---------------------------------------------------------------------------

/// Shapes that read a governance value the gateway does not know as the
/// permissive one: a wildcard arm that allows, an unknown level dropped
/// before a min-wins, an unset-or-unknown default to the lenient reading.
const PERMISSIVE_UNKNOWNS: [&str; 6] = [
    "_ => CapabilityAccess::Allowed",
    "_ => return CapabilityAccess::Allowed",
    "_ => Decision::Allow",
    ".filter_map(Autonomy::parse)",
    ".unwrap_or(\"clamp\")",
    ".as_str()).unwrap_or(\"enabled\")",
];

/// The line of each permissive reading of an unknown value in `code`: one of
/// [`PERMISSIVE_UNKNOWNS`], or a `fn ..._strictness(` whose wildcard arm
/// ranks an unknown value lowest (`_ => 0`), so a restrictive merge drops it.
fn permissive_unknowns(code: &str) -> Vec<usize> {
    let mut found = Vec::new();
    let mut in_strictness = false;
    for (i, line) in code.lines().enumerate() {
        let squeezed = line.split_whitespace().collect::<Vec<_>>().join(" ");
        if PERMISSIVE_UNKNOWNS.iter().any(|p| squeezed.contains(p)) {
            found.push(i + 1);
        }
        if squeezed.starts_with("fn ") || squeezed.contains(" fn ") {
            in_strictness = squeezed
                .split('(')
                .next()
                .is_some_and(|head| head.ends_with("strictness"));
        }
        if in_strictness && squeezed.starts_with("_ => 0") {
            found.push(i + 1);
        }
        if line == "}" {
            in_strictness = false;
        }
    }
    found
}

/// Rule 1 of this repository: an unknown value (an access level, a mode, an
/// effect) is the most restrictive option, never the permissive one.
#[test]
fn no_unknown_value_reads_as_permissive() {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    assert!(files.len() > 100, "the scan found {} files", files.len());
    let mut offenders = Vec::new();
    for f in &files {
        let rel = f.to_string_lossy().replace('\\', "/");
        let src = fs::read_to_string(f).unwrap();
        for line in permissive_unknowns(&production_code(&src)) {
            offenders.push(format!("{rel}:{line}"));
        }
    }
    assert!(
        offenders.is_empty(),
        "an unknown governance value read as the permissive one (make it the most \
         restrictive): {offenders:?}"
    );
}

#[test]
fn the_permissive_unknown_rule_catches_the_shapes_that_shipped() {
    // The group-wide MCP default access.
    let mcp = r#"
    let access_str = gov.get(key).and_then(|v| v.as_str()).unwrap_or("enabled");
    match access_str {
        "disabled" => CapabilityAccess::Denied,
        _ => CapabilityAccess::Allowed,
    }
    "#;
    assert_eq!(permissive_unknowns(mcp), vec![2, 5]);
    // The LLM `enforce` merge and reader.
    let enforce = r#"
fn enforce_strictness(enforce: &str) -> u8 {
    match enforce {
        "clamp" => 0,
        "reject" => 1,
        _ => 0,
    }
}
fn f() {
    let enforce = ctx.enforce.as_deref().unwrap_or("clamp");
}
"#;
    assert_eq!(permissive_unknowns(enforce), vec![6, 10]);
    // The autonomy chain and an agent grant's effect.
    let dropped = "levels.into_iter().flatten().filter_map(Autonomy::parse).min()\n\
                   let effect = match eff { \"deny\" => Decision::Deny, _ => Decision::Allow };";
    assert_eq!(permissive_unknowns(dropped), vec![1, 2]);
    // The shapes that replace them, and a wildcard `0` outside a strictness
    // function.
    let fixed = r#"
fn enforce_strictness(enforce: &str) -> u8 {
    match enforce {
        "clamp" => 0,
        "reject" => 1,
        _ => 2,
    }
}
fn other(n: u8) -> u8 {
    match n {
        _ => 0,
    }
}
fn g() {
    match gov.get(key) {
        None | Some(Value::Null) => CapabilityAccess::Allowed,
        Some(_) => CapabilityAccess::Denied,
    }
}
"#;
    assert!(
        permissive_unknowns(fixed).is_empty(),
        "{:?}",
        permissive_unknowns(fixed)
    );
}

// ---------------------------------------------------------------------------
// A duration read from configuration never panics
// ---------------------------------------------------------------------------

/// Where a stated number of seconds becomes a `Duration`, and its ceiling is.
const CONFIGURED_DURATION: &str = "src/services/configured_duration.rs";

/// The line of each construction in `code` that panics on a large value read
/// from configuration: `Duration::from_secs_f64` / `from_secs_f32`, called
/// or passed as a function (they panic above `u64::MAX` seconds; the
/// `try_` forms do not), and an `Instant` plus a `Duration` made from a
/// variable (an overflowing deadline).
fn panicking_durations(code: &str) -> Vec<usize> {
    let mut found = Vec::new();
    for (i, line) in code.lines().enumerate() {
        let squeezed = line.split_whitespace().collect::<Vec<_>>().join(" ");
        let untried = squeezed.replace("try_from_secs_f", "");
        if untried.contains("from_secs_f64") || untried.contains("from_secs_f32") {
            found.push(i + 1);
            continue;
        }
        if let Some(at) = squeezed.find("Instant::now() + ") {
            let rest = &squeezed[at + "Instant::now() + ".len()..];
            let made = ["Duration::from_secs(", "Duration::from_millis("]
                .iter()
                .find_map(|ctor| rest.find(ctor).map(|p| &rest[p + ctor.len()..]));
            if made.is_some_and(|arg| arg.starts_with(|c: char| c.is_ascii_lowercase())) {
                found.push(i + 1);
            }
        }
    }
    found
}

/// A positive number of seconds the control plane accepts (`1e300`) must not
/// panic the request that reads it: read it through `configured_duration`,
/// which holds it to a ceiling.
#[test]
fn no_configured_duration_can_panic() {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    assert!(files.len() > 100, "the scan found {} files", files.len());
    let mut offenders = Vec::new();
    for f in &files {
        let rel = f.to_string_lossy().replace('\\', "/");
        if rel == CONFIGURED_DURATION {
            continue;
        }
        let src = fs::read_to_string(f).unwrap();
        for line in panicking_durations(&production_code(&src)) {
            offenders.push(format!("{rel}:{line}"));
        }
    }
    assert!(
        offenders.is_empty(),
        "a duration from configuration that can panic (use \
         services::configured_duration): {offenders:?}"
    );
}

#[test]
fn the_configured_duration_rule_catches_the_shapes_that_shipped() {
    // An MCP call's timeout, and a routing group's stream timeout.
    let mcp = "    .map(Duration::from_secs_f64);";
    let mcp_direct = "let t = Duration::from_secs_f64(secs);";
    let router = ".map(|t| (t, tokio::time::Instant::now() + Duration::from_secs_f64(t)));";
    assert_eq!(panicking_durations(mcp), vec![1], "passed as a function");
    assert_eq!(panicking_durations(mcp_direct), vec![1]);
    assert_eq!(panicking_durations(router), vec![1]);
    // A model's timeout made a polling deadline.
    let poll = "    let deadline = Instant::now() + Duration::from_secs(timeout_secs);";
    assert_eq!(panicking_durations(poll), vec![1]);
    // A constant, and the helper.
    let fixed = "let a = Instant::now() + Duration::from_secs(30);\n\
                 let b = Instant::now() + Duration::from_secs(POLL_SECS);\n\
                 let c = Instant::now() + configured_duration::whole_secs(timeout_secs);\n\
                 let d = Duration::try_from_secs_f64(secs);";
    assert!(panicking_durations(fixed).is_empty());
}
