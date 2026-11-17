// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Limits guard: the bug classes that let a configured limit be exceeded.
//!
//! Every usage limit is admitted atomically on the shared counters
//! (`usage_state::admission`). Each rule below forbids a shape that broke
//! that, and each is proven against the shape that shipped before it:
//!
//! 1. **No per-pod fallback.** A limiter that answers from an in-memory
//!    counter when Redis errors multiplies every limit by the replica count.
//! 2. **No read-then-decide.** A limit judged by reading a counter and
//!    comparing passes every request that arrives before the first commits.
//! 3. **No swallowed limit or governance configuration.** A limit or a
//!    governance policy whose row cannot be read must refuse, not read as
//!    "no limit" / "no policy".
//! 4. **Every LLM send site reports its response.** An unreported 2xx makes
//!    a hung-up stream look unbilled, and its reservation is handed back.
//! 5. **The proxy refuses to start without Redis.**
//! 6. **No hold is named from a correlation id.** The caller chooses the
//!    correlation id and may repeat it, across requests and across tenants;
//!    a hold named from it is one record every such request admits into,
//!    gives back and settles.
//! 7. **An MCP upstream answer is read under its payload limit.** A whole
//!    read buffers an answer of any size, and read "or default" turns a
//!    broken one into an empty success.
//! 8. **An MCP call's concurrency slots live as long as the call.** They
//!    ride the call's one `mcp-rate` admission and end only through
//!    `McpInFlight`: committed at dispatch, a slot is free while the call
//!    still runs.
//! 9. **One merge of inherited limits.** Limit documents are merged up a
//!    group's chain by `usage::restrictive_merge` alone, which never lets a
//!    level's `null` or unreadable value remove another level's limit. A
//!    second merge (the A2A cache had one) drifts: it let a child's `null`
//!    erase the parent's limit and turned integer limits into floats the
//!    readers could not read.
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

fn src_files() -> Vec<PathBuf> {
    let mut v = Vec::new();
    rust_files(Path::new("src"), &mut v);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut v);
    }
    v
}

/// Production code only: everything before the file's test module.
fn production(src: &str) -> &str {
    src.split("#[cfg(test)]\nmod tests").next().unwrap_or(src)
}

// ---------------------------------------------------------------------------
// 1. No per-pod fallback
// ---------------------------------------------------------------------------

/// Lines that fall back to a local counter when the shared one errors.
fn per_pod_fallbacks(src: &str) -> Vec<usize> {
    production(src)
        .lines()
        .enumerate()
        .filter(|(_, l)| {
            let l = l.to_ascii_lowercase();
            l.contains("in-memory fallback") && !l.trim_start().starts_with("//")
        })
        .map(|(i, _)| i + 1)
        .collect()
}

#[test]
fn no_limiter_falls_back_to_a_per_pod_counter() {
    let src = fs::read_to_string("src/services/cache/usage_state.rs").unwrap();
    let hits = per_pod_fallbacks(&src);
    assert!(
        hits.is_empty(),
        "usage_state.rs falls back to an in-memory counter on a Redis error at lines {hits:?}: \
         refuse instead (fail closed) — a per-pod counter multiplies the limit by the replica count"
    );
}

#[test]
fn the_fallback_rule_catches_the_shape_that_shipped() {
    let before = r#"
        Err(e) => {
            tracing::warn!(key, error = %e, "Redis token bucket failed — using in-memory fallback");
        }
    "#;
    assert_eq!(per_pod_fallbacks(before).len(), 1);
}

// ---------------------------------------------------------------------------
// 2. No read-then-decide
// ---------------------------------------------------------------------------

/// Calls that read a limit counter. Outside the files allowed below, a read
/// next to a comparison is a check-then-act limit.
const COUNTER_READS: [&str; 6] = [
    ".read_usage(",
    ".try_read_usage(",
    ".check_rpm(",
    ".check_tpm(",
    ".get_tool_calls_hour(",
    ".get_tool_calls_day(",
];

/// Where a counter read is legitimate, and why.
const READ_ALLOWED: [(&str, &str); 4] = [
    // The quota SNAPSHOT: a read-only verdict and the usage-warning headers;
    // the admission that follows it is what enforces.
    (
        "src/services/usage.rs",
        "snapshot verdict, admission enforces",
    ),
    // The routing SELECTION heuristic steers away from a member this replica
    // saw saturated; the call's admission enforces the member's caps.
    (
        "src/services/llm_router.rs",
        "selection heuristic, admission enforces",
    ),
    // RouterState's own definitions of the readers.
    ("src/services/router_state.rs", "definitions"),
    // UsageState's own definitions of the readers.
    ("src/services/cache/usage_state.rs", "definitions"),
];

fn counter_reads(src: &str) -> Vec<usize> {
    production(src)
        .lines()
        .enumerate()
        .filter(|(_, l)| !l.trim_start().starts_with("//"))
        .filter(|(_, l)| COUNTER_READS.iter().any(|c| l.contains(c)))
        .map(|(i, _)| i + 1)
        .collect()
}

#[test]
fn no_limit_is_read_then_decided_outside_the_allowed_readers() {
    let mut offenders = Vec::new();
    for f in src_files() {
        let path = f.to_string_lossy().replace('\\', "/");
        if READ_ALLOWED.iter().any(|(p, _)| path.ends_with(p)) {
            continue;
        }
        let src = fs::read_to_string(&f).unwrap();
        for line in counter_reads(&src) {
            offenders.push(format!("{path}:{line}"));
        }
    }
    assert!(
        offenders.is_empty(),
        "limit counters read on an enforcement path (check-then-act): {offenders:?}. \
         Admit the limit as a dimension (`limits::admit`) so the check and the charge are one step."
    );
}

#[test]
fn the_read_rule_catches_the_shape_that_shipped() {
    // The (since retired) control-attachment limit check, before admission dims.
    let before = r#"
            let current = usage_state.read_usage(&key, window).await;
            if current >= limit_units {
    "#;
    assert_eq!(counter_reads(before).len(), 1);
}

// ---------------------------------------------------------------------------
// 3. No swallowed limit or governance configuration
// ---------------------------------------------------------------------------

/// SQL that reads a limit or governance configuration, then swallows its
/// error within a few lines — the error read as "no limits" / "no policy".
fn swallowed_limit_reads(src: &str) -> Vec<usize> {
    let lines: Vec<&str> = production(src).lines().collect();
    let mut out = Vec::new();
    let mut in_heal = false;
    for (i, l) in lines.iter().enumerate() {
        // The reconcile / backfill passes are background heals, not a
        // decision: a failed read skips one round and the next retries.
        if let Some(name) = l
            .strip_prefix("pub async fn ")
            .or_else(|| l.strip_prefix("async fn "))
            .or_else(|| l.strip_prefix("pub fn "))
            .or_else(|| l.strip_prefix("fn "))
        {
            in_heal = name.starts_with("reconcile") || name.starts_with("backfill");
        }
        if in_heal {
            continue;
        }
        let reads_limits = l.contains("SELECT")
            && (l.contains("limits")
                || l.contains("max_parallel_requests")
                || l.contains("governance"))
            && !l.trim_start().starts_with("//");
        if !reads_limits {
            continue;
        }
        let end = (i + 10).min(lines.len());
        if lines[i..end].iter().any(|w| {
            w.contains(".unwrap_or(None)")
                || w.contains(".unwrap_or_default()")
                || w.trim() == ".ok()"
                || w.contains(".await.ok()")
        }) {
            out.push(i + 1);
        }
    }
    out
}

#[test]
fn no_limit_or_governance_read_swallows_its_error() {
    // Every production file: a governance read can live anywhere a surface
    // resolves its policy (usage.rs and the skill
    // orchestrator have them).
    let mut offenders = Vec::new();
    for f in src_files() {
        let src = fs::read_to_string(&f).unwrap();
        for line in swallowed_limit_reads(&src) {
            offenders.push(format!("{}:{line}", f.display()));
        }
    }
    assert!(
        offenders.is_empty(),
        "a limit / governance configuration read turns its error into 'no limit' / \
         'no policy': {offenders:?} (fail closed)"
    );
}

#[test]
fn the_config_rule_catches_the_shape_that_shipped() {
    let before = r#"
    let model_tl: Option<(Option<serde_json::Value>,)> =
        sqlx::query_as("SELECT tenant_limits FROM llm_model WHERE id = $1")
            .bind(model_id)
            .fetch_optional(db)
            .await
            .unwrap_or(None);
    "#;
    assert_eq!(swallowed_limit_reads(before).len(), 1);
}

#[test]
fn the_config_rule_catches_the_governance_shapes_that_shipped() {
    // `collect_effective_llm_governance` / media governance.
    let unwrapped = r#"
    let self_gov: Option<(Option<serde_json::Value>,)> = sqlx::query_as(
        "SELECT governance FROM resource_group_llm_governance \
         WHERE resource_group_id = $1 AND llm_model_id = $2",
    )
    .bind(&group.id)
    .bind(model_id)
    .fetch_optional(db)
    .await
    .unwrap_or(None);
    "#;
    assert_eq!(swallowed_limit_reads(unwrapped).len(), 1);
    // The execution mode on a skill's assignment.
    let okayed = r#"
    let row: Option<(Option<String>,)> = sqlx::query_as(
        "SELECT governance->>'execution_mode' \
         FROM resource_group_agent_skills \
         WHERE resource_group_id = $1 AND agent_skill_id = $2",
    )
    .bind(group_id)
    .bind(skill_id)
    .fetch_optional(db)
    .await
    .ok()
    .flatten();
    "#;
    assert_eq!(swallowed_limit_reads(okayed).len(), 1);
}

// ---------------------------------------------------------------------------
// 4. Every LLM send site reports its response
// ---------------------------------------------------------------------------

/// Files whose `.send()` calls dispatch an LLM call to a provider, with the
/// sends that do not (and why).
const LLM_SEND_FILES: [&str; 6] = [
    "src/services/bedrock_runtime/embeddings.rs",
    "src/routes/llm.rs",
    "src/routes/gemini.rs",
    "src/routes/anthropic.rs",
    "src/services/llm_router.rs",
    "src/services/anthropic_adapter/mod.rs",
];

/// A send that is not an LLM completion, recognised by its context.
const NOT_A_COMPLETION: [&str; 4] = [
    // Anthropic `count_tokens`: unbilled.
    "count_tokens",
    "count_url",
    // The MCP/tool retry helper's timeout probe.
    "192.0.2.0",
    // An SSE channel, not an HTTP send.
    "tx.send(",
];

/// `line` of each completion `.send()` with no `record_upstream_response`
/// within the next 25 lines.
fn unreported_sends(src: &str) -> Vec<usize> {
    let lines: Vec<&str> = production(src).lines().collect();
    let mut out = Vec::new();
    for (i, l) in lines.iter().enumerate() {
        if !(l.contains(".send().await") || l.trim_end().ends_with(".send()")) {
            continue;
        }
        if l.trim_start().starts_with("//") {
            continue;
        }
        let ctx_start = i.saturating_sub(12);
        let ctx = lines[ctx_start..(i + 1).min(lines.len())].join("\n");
        if NOT_A_COMPLETION.iter().any(|n| ctx.contains(n)) {
            continue;
        }
        let end = (i + 25).min(lines.len());
        if !lines[i..end]
            .iter()
            .any(|w| w.contains("record_upstream_response"))
        {
            out.push(i + 1);
        }
    }
    out
}

#[test]
fn every_llm_send_site_reports_the_providers_answer() {
    let mut offenders = Vec::new();
    for f in LLM_SEND_FILES {
        let src = fs::read_to_string(f).unwrap();
        for line in unreported_sends(&src) {
            offenders.push(format!("{f}:{line}"));
        }
    }
    assert!(
        offenders.is_empty(),
        "an LLM send site never calls `run_end::record_upstream_response`: {offenders:?}. \
         Without it a 2xx is invisible to the call's hold, and a client that hangs up \
         mid-stream gets its reservation handed back — spend that was never counted."
    );
}

#[test]
fn the_send_rule_catches_the_shape_that_shipped() {
    let before = r#"
    match req.json(&body).send().await {
        Ok(resp) => {
            let status = resp.status().as_u16();
            let duration = start.elapsed().as_millis() as f64;
        }
    }
    "#;
    assert_eq!(unreported_sends(before), vec![2]);
}

// ---------------------------------------------------------------------------
// 5. Redis is required
// ---------------------------------------------------------------------------

#[test]
fn the_proxy_refuses_to_start_without_redis() {
    // The connection is made by the builder (`src/builder.rs`).
    let main = fs::read_to_string("src/builder.rs").unwrap();
    let connect = main
        .split("let redis_pool = connect_redis(")
        .nth(1)
        .expect("the builder connects to Redis");
    let next = &connect[..connect.len().min(800)];
    assert!(
        next.contains("anyhow::bail!(\"Redis is required"),
        "the builder must refuse to start when Redis is unreachable or disabled — every usage limit \
         is enforced on it"
    );
}

// ---------------------------------------------------------------------------
// 6. No hold is named from a correlation id
// ---------------------------------------------------------------------------

/// The names a request's correlation id travels under.
const CORRELATION_NAMES: [&str; 3] = ["correlation_id", "corr_id", "request_id"];

/// Calls that name a hold: the argument list of each must not mention a
/// correlation id.
const HOLD_NAMING_CALLS: [&str; 9] = [
    "admit(",
    "HoldId::new(",
    ".adopt_hold(",
    ".set_hold_meta(",
    ".hold_meta(",
    ".forget_live_hold(",
    ".mark_hold_billable(",
    ".finish_hold(",
    ".finish_hold_with(",
];

/// The text between a call's opening parenthesis (at `open`) and its match.
fn call_arguments(src: &str, open: usize) -> &str {
    let mut depth = 0usize;
    for (i, c) in src[open..].char_indices() {
        match c {
            '(' => depth += 1,
            ')' => {
                depth -= 1;
                if depth == 0 {
                    return &src[open + 1..open + i];
                }
            }
            _ => {}
        }
    }
    &src[open + 1..]
}

fn names_a_correlation_id(text: &str) -> bool {
    CORRELATION_NAMES.iter().any(|n| text.contains(n))
}

/// Lines where a hold is named from a correlation id: a hold-naming call
/// whose arguments mention one, a hold id formatted from one, or a hold made
/// by the test-only constructor.
fn holds_named_from_a_correlation_id(src: &str) -> Vec<usize> {
    let src = production(src);
    let line_of = |at: usize| src[..at].matches('\n').count() + 1;
    let mut out = Vec::new();
    for call in HOLD_NAMING_CALLS {
        for (at, _) in src.match_indices(call) {
            let line_start = src[..at].rfind('\n').map_or(0, |n| n + 1);
            let before = src[line_start..at].trim_start();
            if before.starts_with("//") || before.ends_with("fn ") {
                continue;
            }
            if names_a_correlation_id(call_arguments(src, at + call.len() - 1)) {
                out.push(line_of(at));
            }
        }
    }
    for (i, l) in src.lines().enumerate() {
        let t = l.trim_start();
        if t.starts_with("//") {
            continue;
        }
        let formatted = l.contains("format!(")
            && l.to_ascii_lowercase().contains("hold")
            && names_a_correlation_id(l);
        if formatted || l.contains("HoldId::for_test(") {
            out.push(i + 1);
        }
    }
    out.sort_unstable();
    out.dedup();
    out
}

#[test]
fn no_hold_is_named_from_a_correlation_id() {
    let mut offenders = Vec::new();
    for f in src_files() {
        let src = fs::read_to_string(&f).unwrap();
        for line in holds_named_from_a_correlation_id(&src) {
            offenders.push(format!("{}:{line}", f.display()));
        }
    }
    assert!(
        offenders.is_empty(),
        "a hold is named from a correlation id (or by the test-only constructor): {offenders:?}. \
         The caller chooses that id and may send it on several requests; name the hold with \
         `limits::HoldId::new(tenant, surface)` and carry THAT to where the hold ends."
    );
}

#[test]
fn a_hold_can_only_be_named_by_its_constructor() {
    // `limits::admit` takes a `HoldId` and nothing else, and a `HoldId` has
    // one constructor outside tests: the type system does the rest.
    let limits = fs::read_to_string("src/services/limits.rs").unwrap();
    let limits = production(&limits);
    assert!(
        limits.contains("pub struct HoldId(String);"),
        "the hold id's field must stay private: a public one is a second constructor"
    );
    let admit = limits
        .split("pub async fn admit(")
        .nth(1)
        .expect("limits::admit");
    assert!(
        admit[..admit.find(')').unwrap()].contains("hold_id: Option<&HoldId>"),
        "limits::admit must take the hold as a HoldId, not as text"
    );
    let own = limits
        .split("impl HoldId {")
        .nth(1)
        .and_then(|rest| rest.split("\nimpl ").next())
        .expect("impl HoldId");
    let constructors = own.matches("-> Self {").count();
    assert_eq!(
        constructors, 2,
        "HoldId::new and the test-only HoldId::for_test are the only constructors"
    );
    // Nothing above `limits` builds the admission itself.
    for f in src_files() {
        let path = f.to_string_lossy().replace('\\', "/");
        if path.ends_with("src/services/limits.rs")
            || path.ends_with("src/services/cache/usage_state/admission.rs")
        {
            continue;
        }
        let src = fs::read_to_string(&f).unwrap();
        let hits: Vec<usize> = production(&src)
            .lines()
            .enumerate()
            .filter(|(_, l)| !l.trim_start().starts_with("//"))
            .filter(|(_, l)| l.contains(" Admission {") && !l.contains("struct "))
            .map(|(i, _)| i + 1)
            .collect();
        assert!(
            hits.is_empty(),
            "{path} builds an `Admission` itself at lines {hits:?}: admit through `limits::admit`, \
             which takes the hold as a `HoldId`"
        );
    }
}

#[test]
fn the_hold_rule_catches_the_shapes_that_shipped() {
    // The LLM hold: the bare correlation id.
    let llm = r#"
        match crate::services::limits::admit(&state.usage_state, Some(correlation_id), dims, shape)
            .await
        {
            Ok(Ok(out)) => {
                state.usage_state.set_hold_meta(correlation_id, &model.id);
                hold = Some(state.usage_state.adopt_hold(correlation_id));
            }
        }
    "#;
    assert_eq!(holds_named_from_a_correlation_id(llm), vec![2, 6, 7]);
    // The MCP and skill holds: the correlation id and a fixed suffix.
    let mcp = r#"
    let quota_hold_id = format!("{correlation_id}:mcpq");
    let rate_hold_id = format!("{correlation_id}:mcpr");
    let quota_hold_id = format!("{correlation_id}:skx");
    "#;
    assert_eq!(holds_named_from_a_correlation_id(mcp), vec![2, 3, 4]);
    // The settle that found the hold again by the caller's id.
    let settle = r#"
    let redirect = match (usage_state.hold_meta(request_id), group_id) {
        _ => Redirect::default(),
    };
    usage_state.forget_live_hold(request_id);
    "#;
    assert_eq!(holds_named_from_a_correlation_id(settle), vec![2, 5]);
    // A call spread over several lines, and the correlation id used as the
    // tenant part of a new id.
    let spread = r#"
        let admitted = limits::admit(
            us,
            Some(&HoldId::new(correlation_id, "llm")),
            dims,
            shape,
        );
    "#;
    assert_eq!(holds_named_from_a_correlation_id(spread), vec![2, 4]);
    // What replaced them passes.
    let after = r#"
        let hold_id = crate::services::limits::HoldId::new(tenant_id, "llm");
        match crate::services::limits::admit(&state.usage_state, Some(&hold_id), dims, shape)
            .await
        {
            Ok(Ok(out)) => {
                hold = Some(state.usage_state.adopt_hold(hold_id.as_str()));
            }
        }
    "#;
    assert!(holds_named_from_a_correlation_id(after).is_empty());
}

// ---------------------------------------------------------------------------
// A guard is adopted from an admission, and from nothing else
// ---------------------------------------------------------------------------

/// The lines that adopt a guard from something other than the proof an
/// admission returned (`limits::Held`): a hold id, a string, or the
/// test-only proof.
fn adoptions_without_an_admission(src: &str) -> Vec<usize> {
    production(src)
        .lines()
        .enumerate()
        .filter(|(_, l)| !l.trim_start().starts_with("//"))
        .filter(|(_, l)| {
            l.split(".adopt_hold(").skip(1).any(|arguments| {
                arguments.contains(".as_str()")
                    || arguments.contains("Held::for_test(")
                    || arguments.trim_start().starts_with('"')
                    || arguments.trim_start().starts_with('&')
            })
        })
        .map(|(i, _)| i + 1)
        .collect()
}

#[test]
fn a_guard_is_adopted_only_from_what_an_admission_returned() {
    // `UsageState::adopt_hold` takes `limits::Held`, which `limits::admit`
    // makes when it admitted under a hold. A request that was refused has
    // none, so it cannot end a hold it never had.
    let admission =
        fs::read_to_string("src/services/cache/usage_state/admission.rs").expect("admission.rs");
    assert!(
        production(&admission)
            .contains("pub fn adopt_hold(self: &Arc<Self>, held: crate::services::limits::Held)"),
        "adopt_hold must take the proof of an admission, not a hold id"
    );
    let limits = fs::read_to_string("src/services/limits.rs").expect("limits.rs");
    let limits = production(&limits);
    assert!(
        limits.contains("pub struct Held(String);"),
        "the proof's field must stay private: a public one is a second way to make it"
    );
    assert_eq!(
        limits.matches("Held(h.as_str().to_string())").count(),
        1,
        "limits::admit is the one place that makes the proof"
    );
    assert_eq!(
        limits.matches("Self(hold_id.to_string())").count(),
        1,
        "beside the test-only constructor"
    );
    let mut offenders = Vec::new();
    for f in src_files() {
        let src = fs::read_to_string(&f).unwrap();
        for line in adoptions_without_an_admission(&src) {
            offenders.push(format!("{}:{line}", f.display()));
        }
    }
    assert!(
        offenders.is_empty(),
        "a guard is adopted from a hold id instead of from the admission that reserved it: \
         {offenders:?}"
    );
}

#[test]
fn the_adoption_rule_catches_the_shapes_that_adopted_a_refused_hold() {
    for bad in [
        "mcp_holds.push(state.usage_state.adopt_hold(rate_hold_id.as_str()));",
        "let g = us.adopt_hold(\"c-1\");",
        "let g = us.adopt_hold(&id);",
        "let g = us.adopt_hold(limits::Held::for_test(id));",
    ] {
        assert_eq!(adoptions_without_an_admission(bad).len(), 1, "{bad}");
    }
    for good in [
        "hold = admitted.hold.map(|h| state.usage_state.adopt_hold(h));",
        "mcp_holds.extend(rate_hold.held().map(|h| state.usage_state.adopt_hold(h)));",
        "// us.adopt_hold(id.as_str())",
    ] {
        assert!(adoptions_without_an_admission(good).is_empty(), "{good}");
    }
}

// ---------------------------------------------------------------------------
// 7. An MCP upstream answer is read under its limit
// ---------------------------------------------------------------------------

/// Lines that read a whole upstream answer with no ceiling and no error
/// (`.bytes().await` / `.text().await`): an answer of any size is buffered,
/// and with `unwrap_or_default` a broken one becomes an empty 200.
fn unbounded_answer_reads(src: &str) -> Vec<usize> {
    src.lines()
        .enumerate()
        .filter(|(_, l)| !l.trim_start().starts_with("//"))
        .filter(|(_, l)| l.contains(".bytes().await") || l.contains(".text().await"))
        .map(|(i, _)| i + 1)
        .collect()
}

#[test]
fn an_mcp_answer_is_read_under_its_payload_limit() {
    // `mcp_limits::read_capped` is the one reader: a chunk loop that stops
    // at the limit, honours the deadline and turns a broken answer into an
    // error. Behaviour: `tests/integration/mcp_execution_limits.rs`.
    for file in ["src/routes/mcp.rs", "src/services/mcp_limits.rs"] {
        let src = fs::read_to_string(file).unwrap();
        let hits = unbounded_answer_reads(&src);
        assert!(
            hits.is_empty(),
            "{file} reads an upstream answer whole at lines {hits:?}: use mcp_limits::read_capped"
        );
    }
}

#[test]
fn the_answer_rule_catches_the_shape_that_shipped() {
    let before = "            let mut resp_body = upstream_resp.bytes().await.unwrap_or_default();";
    assert_eq!(unbounded_answer_reads(before), vec![1]);
    let read_then_check = "    let all = resp.bytes().await.map_err(|e| BodyReadError::Upstream(e.to_string()))?;\n    if all.len() > limit { return Err(BodyReadError::TooLarge(limit)); }";
    assert_eq!(unbounded_answer_reads(read_then_check), vec![1]);
    assert!(unbounded_answer_reads("            match resp.chunk().await {").is_empty());
}

// ---------------------------------------------------------------------------
// 8. An MCP call's concurrency slots live as long as the call
// ---------------------------------------------------------------------------

/// What breaks the rule that the `mcp-rate` hold carrying concurrency slots
/// is admitted with the frequency limits in one step and ended only through
/// `McpInFlight` (finished at the end of the call, abandoned on a drop).
fn slot_hold_violations(src: &str) -> Vec<String> {
    let mut out = Vec::new();
    let code: Vec<(usize, &str)> = src
        .lines()
        .enumerate()
        .filter(|(_, l)| !l.trim_start().starts_with("//"))
        .map(|(i, l)| (i + 1, l))
        .collect();
    for (n, l) in &code {
        // Committed at dispatch with the other holds: the slot is freed
        // while the call is still running.
        if l.contains("mcp_holds.extend(rate_hold") || l.contains("mcp_holds.push(rate_hold") {
            out.push(format!(
                "line {n}: the rate hold joins the holds committed at dispatch"
            ));
        }
        // The slot-carrying hold is only declared, set from the admission,
        // and handed to McpInFlight.
        if l.contains("slot_hold")
            && !l.contains("let mut slot_hold: Option<")
            && !l.contains("slot_hold = held;")
            && !l.contains("McpInFlight::new(")
        {
            out.push(format!(
                "line {n}: the slot hold is used outside McpInFlight: {}",
                l.trim()
            ));
        }
        // A second admission for the slots: two round trips, not atomic
        // with the frequency limits.
        if l.contains("limits::admit(") {
            out.push(format!(
                "line {n}: an admission outside usage::admit_mcp_rate_limits"
            ));
        }
    }
    let count = |needle: &str| code.iter().filter(|(_, l)| l.contains(needle)).count();
    let sites = count("\"mcp-rate\")");
    for (what, n) in [
        ("slot_dims(", count("slot_dims(")),
        ("slot_hold = held;", count("slot_hold = held;")),
        ("McpInFlight::new(.., slot_hold)", count("slot_hold);")),
    ] {
        if n != sites {
            out.push(format!("{sites} mcp-rate admissions but {n} `{what}`"));
        }
    }
    // Every rate admission is given the slots.
    let flat: String = code
        .iter()
        .map(|(_, l)| l.trim())
        .collect::<Vec<_>>()
        .join(" ");
    for call in flat.split("usage::admit_mcp_rate_limits(").skip(1) {
        let args = call.split(".await").next().unwrap_or_default();
        if !args.contains(" slots,") {
            out.push(format!("an mcp-rate admission without its slots: {args}"));
        }
    }
    out
}

#[test]
fn an_mcp_calls_slots_live_as_long_as_the_call() {
    let src = fs::read_to_string("src/routes/mcp.rs").unwrap();
    let bad = slot_hold_violations(&src);
    assert!(bad.is_empty(), "{}", bad.join("\n"));
    let limits = fs::read_to_string("src/services/mcp_limits.rs").unwrap();
    assert!(
        limits.contains("hold.mark_dispatched();") && limits.contains("hold.commit().await;"),
        "McpInFlight must mark its hold dispatched (a drop abandons it) and commit it at the end"
    );
}

#[test]
fn the_slot_rule_catches_the_shapes_it_forbids() {
    let site = |end: &str| {
        format!(
            "let mut slot_hold: Option<HoldGuard> = None;\n\
             let mut rate_hold = PendingHold::of(HoldId::new(&tenant_id, \"mcp-rate\"));\n\
             let slots = exec_limits.slot_dims(&t, &g, s, &p);\n\
             let block = usage::admit_mcp_rate_limits(\n    &state.usage_state,\n    group,\n    slots,\n    Some(&mut rate_hold),\n)\n.await;\n\
             let held = rate_hold.held().map(|h| state.usage_state.adopt_hold(h));\n\
             {end}"
        )
    };
    let good = site("slot_hold = held;\nlet in_flight = McpInFlight::new(permit, slot_hold);");
    assert!(
        slot_hold_violations(&good).is_empty(),
        "{:?}",
        slot_hold_violations(&good)
    );
    // The shape before: the rate hold committed at dispatch.
    let before = site(
        "mcp_holds.extend(rate_hold.held().map(|h| state.usage_state.adopt_hold(h)));\nlet in_flight = McpInFlight::new(permit, None);",
    );
    assert!(!slot_hold_violations(&before).is_empty());
    // The slot hold committed at dispatch.
    let committed = site(
        "slot_hold = held;\nif let Some(h) = slot_hold.take() { h.commit().await; }\nlet in_flight = McpInFlight::new(permit, slot_hold);",
    );
    assert!(!slot_hold_violations(&committed).is_empty());
    // The slots admitted on their own.
    let second = good.replace("    slots,\n", "    Vec::new(),\n")
        + "\nlet s = limits::admit(&state.usage_state, Some(&id), slots, Shape::count()).await;";
    assert!(slot_hold_violations(&second).len() >= 2);
}

// ---------------------------------------------------------------------------
// 9. One merge of inherited limits
// ---------------------------------------------------------------------------

/// Definitions of a restrictive merge of limit documents outside
/// `services/usage.rs` (`path` relative to the repository).
fn second_limit_merges(path: &str, src: &str) -> Vec<String> {
    if path.replace('\\', "/").ends_with("src/services/usage.rs") {
        return Vec::new();
    }
    production(src)
        .lines()
        .enumerate()
        .filter(|(_, l)| {
            let t = l.trim_start();
            !t.starts_with("//")
                && t.split("fn ").skip(1).any(|rest| {
                    rest.split(['(', '<'])
                        .next()
                        .is_some_and(|name| name.contains("restrictive_merge"))
                })
        })
        .map(|(i, l)| format!("{path}:{}: {}", i + 1, l.trim()))
        .collect()
}

#[test]
fn inherited_limits_have_one_merge() {
    let mut bad = Vec::new();
    for f in src_files() {
        let src = fs::read_to_string(&f).unwrap();
        bad.extend(second_limit_merges(&f.to_string_lossy(), &src));
    }
    assert!(
        bad.is_empty(),
        "merge limit documents with usage::restrictive_merge, not a second merge:\n{}",
        bad.join("\n")
    );
}

#[test]
fn the_merge_rule_catches_the_shape_that_shipped() {
    let shipped = "fn merge_a2a_settings_along_chain() {}\n\
                   fn restrictive_merge_json(base: &mut serde_json::Value, overlay: &serde_json::Value) {\n\
                       // Strings etc.: child value wins.\n\
                   }\n";
    assert_eq!(
        second_limit_merges("src/services/cache/group_cache.rs", shipped).len(),
        1
    );
    assert_eq!(
        second_limit_merges(
            "src/routes/a2a.rs",
            "pub(crate) fn a2a_restrictive_merge<'a>(x: &'a Value) {}"
        )
        .len(),
        1
    );
    // The one merge, and calls of it, pass.
    assert!(second_limit_merges(
        "src/services/usage.rs",
        "pub(crate) fn restrictive_merge(base: &mut Value, overlay: &Value, family: LimitFamily) {}"
    )
    .is_empty());
    assert!(second_limit_merges(
        "src/routes/a2a.rs",
        "Some(ref mut base) => crate::services::usage::restrictive_merge(base, tl, LimitFamily::A2a),"
    )
    .is_empty());
}
