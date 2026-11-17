// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Every LLM surface must write the evidence it owes.
//!
//! The Vertex-Gemini OpenAI-surface bridge used to end both of its exit
//! branches with `let _ = (auth_ctx, target_group, headers, correlation_id,
//! start);` — an explicit discard of exactly the context an audit row, a
//! usage record and an output guardrail need. The result was a provider
//! whose traffic produced no evidence at all: nothing in `proxy_logs`,
//! nothing in `usage_records`, no PII or secret redaction on the way out.
//!
//! This scans the source rather than asserting on behaviour, for the same
//! reason `vocabulary_guard.rs` does: the failure mode is an *absence*.
//! No runtime test can fail on a call that was never written, and the
//! surface itself cannot be driven end-to-end in a test because Vertex
//! builds a fixed `googleapis.com` URL and demands a real ADC token.
//!
//! If the bridge is refactored, keep the guarantees, not the spelling:
//! every path that returns a response to a caller must first record what
//! happened.

#[path = "support/roots.rs"]
mod roots;

use std::fs;

/// Is `file` one this run has? A file of the Enterprise workspace is not, in
/// a Community tree alone (`tests/support/roots.rs`); what this guard holds
/// about it is held where the workspace is.
fn here(file: &str) -> bool {
    !file.starts_with("enterprise/") || roots::tree("enterprise").is_some()
}

const BRIDGE: &str = "src/routes/gemini.rs";

fn bridge_source() -> String {
    fs::read_to_string(BRIDGE).expect("read gemini.rs")
}

/// The body of `forward_via_openai_surface`, from its signature to the
/// start of the next top-level item.
fn bridge_fn() -> String {
    let src = bridge_source();
    let start = src
        .find("pub async fn forward_via_openai_surface(")
        .expect("bridge function is present");
    let rest = &src[start..];
    let end = rest
        .find("\n// ---------------------------------------------------------------------------")
        .unwrap_or(rest.len());
    rest[..end].to_string()
}

#[test]
fn the_bridge_never_discards_its_audit_context() {
    let f = bridge_fn();
    assert!(
        !f.contains("let _ = (auth_ctx, target_group, headers, correlation_id, start)"),
        "the Vertex-Gemini bridge is discarding the context its evidence tail needs — \
         without it the bridge writes no evidence",
    );
}

#[test]
fn every_bridge_exit_records_what_happened() {
    let f = bridge_fn();
    let writes = f.matches("write_bridge_evidence(").count();
    // Four responses can leave this function: upstream send failed,
    // upstream returned non-2xx, the stream finished, the buffered
    // response was returned. Each owes a record.
    assert!(
        writes >= 4,
        "expected every exit path of the Vertex-Gemini bridge to write evidence, \
         found {writes} call(s) to write_bridge_evidence",
    );
}

#[test]
fn the_bridge_guards_its_output_on_both_branches() {
    let f = bridge_fn();
    assert!(
        f.contains("apply_output_guardrails("),
        "the buffered branch must run output guardrails before returning model output",
    );
    assert!(
        f.contains("StreamGuard::new("),
        "the streaming branch must run the output-guardrail state machine",
    );
}

#[test]
fn the_bridge_settles_the_call_it_admitted() {
    // Per-run ceilings (and every other limit of the call) are
    // admitted in preflight under the request's correlation id; the usage
    // log settles that admission BY that id. Settling under any other id is
    // how a surface's calls would stop moving the run's counters.
    let f = bridge_source();
    let tail = f
        .split("async fn write_bridge_evidence(")
        .nth(1)
        .expect("evidence tail is present");
    let call = settle_calls(tail)
        .into_iter()
        .next()
        .expect("the evidence tail settles the call");
    assert!(
        request_id_arg(&call).contains("correlation_id"),
        "the bridge must settle under the request's correlation id: {call}",
    );
}

#[test]
fn the_evidence_tail_writes_all_three_records() {
    let f = bridge_source();
    let tail = f
        .split("async fn write_bridge_evidence(")
        .nth(1)
        .expect("evidence tail is present");
    for needed in [
        "build_proxy_log(",
        "write_proxy_log(",
        "write_llm_usage_log(",
        "record_request(",
    ] {
        assert!(
            tail.contains(needed),
            "the evidence tail is missing `{needed}` — a bridged call would be \
             invisible to that surface",
        );
    }
}

// ---------------------------------------------------------------------------
// Every LLM surface charges the run it belongs to.
// ---------------------------------------------------------------------------
//
// Per-run ceilings bind only on calls counted into the run. Four of
// the five LLM entry points used to pass no run id to the charge, so a run's
// `calls`, `tokens` and `cost` stayed at zero and the gate compared against
// nothing. A run's counted terms
// are dimensions of the call's ONE admission in preflight (`run_budget::
// run_dims`, unless the surface opts out with `not_run_accounted`), and the
// usage log SETTLES that admission by the request's correlation id. So the
// failure now has two shapes, both silent: a surface that opts out of run
// accounting, and a surface that settles under some other id (its run
// reservation is then kept as the worst case, and its actual usage lands
// nowhere). The guard is a source scan.

const CHARGE_SITES: &[&str] = &[
    "src/routes/llm.rs",
    "src/routes/anthropic.rs",
    "src/routes/gemini.rs",
    "src/services/anthropic_adapter/mod.rs",
];

/// The argument list of every `write_llm_usage_log(...)` call in a file,
/// with `//` comment lines removed (comments contain commas, which would
/// shift the argument split).
fn settle_calls(src: &str) -> Vec<String> {
    let mut out = Vec::new();
    for (i, _) in src.match_indices("write_llm_usage_log(") {
        let rest = &src[i..];
        let end = rest.find(");").map(|e| e + 2).unwrap_or(rest.len());
        let code_only: String = rest[..end]
            .lines()
            .map(str::trim)
            .filter(|l| !l.starts_with("//"))
            .collect::<Vec<_>>()
            .join("\n");
        out.push(code_only);
    }
    out
}

/// The request-id argument of a `write_llm_usage_log` call: the 17th.
fn request_id_arg(call: &str) -> String {
    let args = call
        .split_once('(')
        .map(|(_, a)| a)
        .unwrap_or("")
        .trim_end_matches(");");
    args.split(',')
        .map(str::trim)
        .filter(|a| !a.is_empty())
        .nth(16)
        .unwrap_or("")
        .to_string()
}

#[test]
fn every_llm_surface_settles_under_the_requests_id() {
    let mut offenders = Vec::new();
    for path in CHARGE_SITES {
        let src = fs::read_to_string(path).unwrap_or_else(|e| panic!("read {path}: {e}"));
        for (n, call) in settle_calls(&src).iter().enumerate() {
            let id = request_id_arg(call);
            if !id.contains("corr") {
                offenders.push(format!("{path} (call #{}: request id `{id}`)", n + 1));
            }
        }
    }
    assert!(
        offenders.is_empty(),
        "these LLM usage logs settle under something other than the request's \
         correlation id, so the call's admission (its run ceilings included) is \
         never settled: {offenders:?}",
    );
}

#[test]
fn every_llm_surface_charges_at_least_once() {
    // A surface that never settles at all is the same failure wearing a
    // different hat: the run ceiling cannot bind on traffic it never sees.
    for path in CHARGE_SITES {
        let src = fs::read_to_string(path).unwrap_or_else(|e| panic!("read {path}: {e}"));
        assert!(
            !settle_calls(&src).is_empty(),
            "{path} runs LLM traffic but never settles a call",
        );
    }
}

#[test]
fn only_embeddings_opt_out_of_run_accounting() {
    // An LLM surface that generates output is counted into the run. The one
    // exception in these files is the embeddings route.
    let mut opted_out = 0;
    for path in CHARGE_SITES {
        let src = fs::read_to_string(path).unwrap_or_else(|e| panic!("read {path}: {e}"));
        opted_out += src.matches(".not_run_accounted()").count();
    }
    assert_eq!(
        opted_out, 1,
        "a generating LLM surface opted out of run accounting — its calls would \
         never count toward the run's ceilings",
    );
}

// ---------------------------------------------------------------------------
// Every cached forward carries a per-caller identity.
// ---------------------------------------------------------------------------
//
// The exact-match response cache keys on (tenant, model, identity, body).
// A surface that forwarded without scoping an identity used to fall through
// to a shared bucket, so two end users in one tenant sending an identical
// body to the same model were served each other's completions. `/v1/messages`
// did exactly that.
//
// The cache is behind the response cache hook: a route
// forwards inside `response_cache::with_dispatch_cache`, and the dispatch
// cache it is given is built for one caller. The router reads or writes none
// when none was given, so the worst case is a missed hit rather than a leak.
// This guard keeps the *other* half true: that user-facing surfaces do ask
// for one, instead of quietly losing caching — and that the identity cannot
// be left out where the key is built.

const DISPATCH_CACHE_IMPL: &str = "enterprise/src/dispatch_exact_cache.rs";

#[test]
fn every_cached_forward_scopes_a_cache_identity() {
    let surfaces = ["src/routes/llm.rs", "src/routes/anthropic.rs"];
    for path in surfaces {
        let src = fs::read_to_string(path).unwrap_or_else(|e| panic!("read {path}: {e}"));
        let forwards = src.matches("forward_with_fallbacks(").count();
        if forwards == 0 {
            continue;
        }
        let scopes = src.matches("with_dispatch_cache(").count();
        assert!(
            scopes >= forwards,
            "{path} calls forward_with_fallbacks {forwards} time(s) but forwards \
             inside a dispatch cache only {scopes} time(s) — an unscoped call gets \
             no cache at best, and must never share a bucket across users",
        );
        // The cache it forwards with is the one the hook built for this caller.
        let asked = src.matches(".dispatch_cache(&headers, &auth_ctx)").count();
        assert!(
            asked >= scopes,
            "{path}: a dispatch cache that was not asked of the hook for this \
             request's caller ({asked} asked, {scopes} forwards)",
        );
    }
}

#[test]
fn the_cache_key_cannot_be_built_without_an_identity() {
    // The type carries the rule: an `Option` here is what let the missing
    // identity become the literal "-" shared bucket.
    if !here(DISPATCH_CACHE_IMPL) {
        return; // a Community tree alone has no dispatch cache
    }
    let cache = fs::read_to_string(DISPATCH_CACHE_IMPL).expect("read the dispatch cache");
    let sig_start = cache
        .find("fn build_cache_key(")
        .expect("build_cache_key is present");
    let sig = &cache[sig_start..sig_start + 200];
    assert!(
        sig.contains("identity: &str"),
        "build_cache_key must take a required identity, not an Option",
    );
    assert!(
        !cache.contains(r#"identity.unwrap_or("-")"#),
        "the shared-bucket fallback is back",
    );
    // And the cache object itself cannot exist without one.
    assert!(
        cache.contains("identity: String,") && !cache.contains("identity: Option<"),
        "an ExactDispatchCache always has an identity",
    );
}

#[test]
fn one_definition_of_the_cache_identity() {
    // Two copies would drift, and a drifted identity silently splits or
    // merges caches — the same class of bug wearing a different hat.
    let mut defs = 0;
    for path in [
        DISPATCH_CACHE_IMPL,
        "src/services/response_cache.rs",
        "src/services/llm_router.rs",
        "src/routes/llm.rs",
        "src/routes/anthropic.rs",
        "src/routes/gemini.rs",
    ]
    .into_iter()
    .filter(|p| here(p))
    {
        let src = fs::read_to_string(path).unwrap_or_else(|e| panic!("read {path}: {e}"));
        defs += src.matches("fn cache_identity_for(").count();
        defs += src.matches("fn response_cache_identity(").count();
    }
    // The one definition is the dispatch cache's; Community code has none.
    assert_eq!(
        defs,
        usize::from(here(DISPATCH_CACHE_IMPL)),
        "expected exactly one cache-identity definition, found {defs}"
    );
}

// ---------------------------------------------------------------------------
// Every LLM surface runs the actor planes.
// ---------------------------------------------------------------------------
//
// Agent authorization, the AI System autonomy gate and semantic policy used
// to live inline in `chat_completions` and nowhere else. `/v1/messages` and
// the Gemini native route are mounted, take the same API keys and reach the
// same models, so a caller could bypass a denied agent grant, a *suspended*
// AI System and every enforcing policy by changing endpoint. The planes now
// live in one helper; this keeps every surface calling it.
//
// Runtime coverage of the bypass would need a live agent grant, a suspended
// group and a judge, against three surfaces. The absence is what matters and
// the absence is visible in the source.

const ACTOR_PLANE_SURFACES: &[&str] = &[
    "src/routes/llm.rs",
    "src/routes/anthropic.rs",
    "src/routes/gemini.rs",
];

#[test]
fn every_llm_surface_runs_the_actor_planes() {
    for path in ACTOR_PLANE_SURFACES {
        let src = fs::read_to_string(path).unwrap_or_else(|e| panic!("read {path}: {e}"));
        assert!(
            src.contains("enforce_actor_planes("),
            "{path} serves LLM traffic but never runs the actor planes — a suspended \
             AI System, a denied agent grant and every enforcing semantic policy are \
             all bypassable through it",
        );
    }
}

/// One file serves several routes: `src/routes/llm.rs` held four handlers
/// and ran the planes in one, which the "file calls
/// the helper" check above could not see. Every handler that runs the LLM
/// preflight runs the planes: the two are called the same number of times in
/// each surface file.
/// Enterprise surfaces that run the LLM preflight
/// (batch submission ran it and none of the planes). Scanned where the
/// Enterprise workspace is present.
const ENTERPRISE_ACTOR_PLANE_SURFACES: &[&str] = &["enterprise/src/batch/routes.rs"];

#[test]
fn every_preflight_of_an_llm_surface_is_followed_by_the_actor_planes() {
    let enterprise = ENTERPRISE_ACTOR_PLANE_SURFACES.iter().filter(|f| here(f));
    for path in ACTOR_PLANE_SURFACES.iter().chain(enterprise) {
        let src = fs::read_to_string(path).unwrap_or_else(|e| panic!("read {path}: {e}"));
        let (preflights, planes) = preflights_and_planes(&src);
        assert!(preflights >= 1, "{path}: the scan found no preflight");
        assert_eq!(
            preflights, planes,
            "{path} runs the LLM preflight {preflights} times and the actor planes {planes} \
             times: a handler serves model calls without agent authorization, the autonomy \
             kill switch and semantic policy",
        );
    }
    // The shape that was there: four preflights, one call of the planes.
    let before = "llm_preflight::run_preflight(\n".repeat(4)
        + "    if let Err(resp) = llm_preflight::enforce_actor_planes(\n";
    assert_eq!(preflights_and_planes(&before), (4, 1));
    // A comment that names either is not a call.
    assert_eq!(
        preflights_and_planes("    // llm_preflight::enforce_actor_planes( is called below\n"),
        (0, 0)
    );
}

/// The same for data residency: `/completions` ran
/// the preflight and no residency check while the three other handlers of
/// `src/routes/llm.rs` did, which "the file calls the helper" cannot see.
#[test]
fn every_preflight_of_an_llm_surface_is_followed_by_the_residency_check() {
    for path in ACTOR_PLANE_SURFACES {
        let src = fs::read_to_string(path).unwrap_or_else(|e| panic!("read {path}: {e}"));
        let (preflights, checks) = preflights_and_residency_checks(&src);
        assert!(preflights >= 1, "{path}: the scan found no preflight");
        assert_eq!(
            preflights, checks,
            "{path} runs the LLM preflight {preflights} times and the residency check {checks} \
             times: a handler sends prompts to a provider without asking whether the tenant's \
             residency profile permits its region",
        );
    }
    // The shape that was there: four preflights, three checks.
    let before = "llm_preflight::run_preflight(\n".repeat(4)
        + &"    let residency = match llm_preflight::enforce_residency(\n".repeat(3);
    assert_eq!(preflights_and_residency_checks(&before), (4, 3));
    assert_eq!(
        preflights_and_residency_checks("    // see `llm_preflight::enforce_residency(`\n"),
        (0, 0)
    );
}

fn preflights_and_residency_checks(src: &str) -> (usize, usize) {
    let code: String = src
        .lines()
        .filter(|l| !l.trim_start().starts_with("//"))
        .collect::<Vec<_>>()
        .join("\n");
    (
        code.matches("llm_preflight::run_preflight(").count(),
        code.matches("llm_preflight::enforce_residency(").count(),
    )
}

fn preflights_and_planes(src: &str) -> (usize, usize) {
    let code: String = src
        .lines()
        .filter(|l| !l.trim_start().starts_with("//"))
        .collect::<Vec<_>>()
        .join("\n");
    (
        code.matches("llm_preflight::run_preflight(").count(),
        code.matches("llm_preflight::enforce_actor_planes(").count(),
    )
}

/// The platform's embedding self-calls (the semantic cache's, the Knowledge
/// Base's) are built in one place, `internal_call::embedding_self_call`,
/// which forwards the caller's credential and run and sets the assertion the
/// embeddings route verifies. A self-call built by
/// hand would be the caller's ordinary call: refused for a default-deny
/// agent, and not recorded as the platform's.
#[test]
fn every_embedding_self_call_is_built_by_the_one_marked_builder() {
    fn rust_files(dir: &std::path::Path, out: &mut Vec<std::path::PathBuf>) {
        for entry in fs::read_dir(dir).unwrap_or_else(|e| panic!("read {dir:?}: {e}")) {
            let path = entry.expect("entry").path();
            if path.is_dir() {
                rust_files(&path, out);
            } else if path.extension().is_some_and(|e| e == "rs") {
                out.push(path);
            }
        }
    }
    let mut files = Vec::new();
    rust_files(std::path::Path::new("src"), &mut files);
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    assert!(files.len() > 100, "the scan found {} files", files.len());

    let mut builders = 0;
    for path in &files {
        let src = fs::read_to_string(path).expect("read");
        let name = path.to_string_lossy().replace('\\', "/");
        let hand_built = embeddings_self_call_sites(&src);
        match name.as_str() {
            // The route constant's definition.
            "src/constants.rs" => {}
            "src/services/internal_call.rs" => {
                builders += src.matches("pub fn embedding_self_call(").count();
                assert!(
                    src.contains("embedding_assertion(purpose)"),
                    "the builder no longer sets the assertion"
                );
            }
            _ => assert_eq!(
                hand_built, 0,
                "{name} addresses the gateway's own embeddings route directly: build the \
                 call with `internal_call::embedding_self_call`",
            ),
        }
    }
    assert_eq!(builders, 1, "expected exactly one embedding_self_call");
    // The shape that was there.
    assert_eq!(
        embeddings_self_call_sites("    brutor_gateway_core::constants::routes::LLM_EMBEDDINGS\n"),
        1
    );
    assert_eq!(
        embeddings_self_call_sites("    // posts to routes::LLM_EMBEDDINGS\n"),
        0
    );
}

fn embeddings_self_call_sites(src: &str) -> usize {
    src.lines()
        .filter(|l| !l.trim_start().starts_with("//"))
        .filter(|l| l.contains("LLM_EMBEDDINGS"))
        .count()
}

#[test]
fn the_actor_planes_have_one_implementation() {
    // Three copies would drift, and a drifted kill switch is worse than an
    // absent one because it looks enforced.
    let mut defs = 0;
    for path in ["src/services/llm_preflight.rs"]
        .iter()
        .chain(ACTOR_PLANE_SURFACES.iter())
    {
        let src = fs::read_to_string(path).unwrap_or_else(|e| panic!("read {path}: {e}"));
        defs += src.matches("pub async fn enforce_actor_planes(").count();
    }
    assert_eq!(
        defs, 1,
        "expected exactly one enforce_actor_planes, found {defs}"
    );
}

#[test]
fn the_planes_still_cover_all_three_controls() {
    // Guards the helper's contents, not just that it is called: dropping one
    // plane from it would silently disarm that control everywhere at once.
    let src = fs::read_to_string("src/services/llm_preflight.rs").expect("read llm_preflight.rs");
    let helper = src
        .split("pub async fn enforce_actor_planes(")
        .nth(1)
        .expect("helper is present");
    for needed in [
        // `decide_for_agent` with the grant requirement stated:
        // the same decision, every gate of it.
        "decide_for_agent_with(",
        // Every membership is evaluated (strictest wins) since the group
        // header stopped being trusted verbatim — see autonomy_group_header.rs.
        "autonomy::enforce_for_groups(",
        "semantic_policy::evaluate(",
    ] {
        assert!(
            helper.contains(needed),
            "the actor-plane helper no longer calls `{needed}` — that control is now \
             disarmed on every LLM surface at once",
        );
    }
}

// ---------------------------------------------------------------------------
// Semantic policy runs on every surface that accepts content.
// ---------------------------------------------------------------------------
//
// Policy evaluation lived on `message:send` but not `message:stream`, and on
// the deprecated skills REST route but not on the canonical
// progressive-disclosure `skills__run_script` path that agents actually use.
// An enforcing policy that one verb honours and its twin ignores is decided
// by which entry point the caller picked, which is not a policy.

#[test]
fn both_a2a_verbs_evaluate_semantic_policy() {
    let src = fs::read_to_string("src/routes/a2a_v1.rs").expect("read a2a_v1.rs");
    let calls = src.matches("enforce_a2a_semantic_policy(").count();
    // One definition plus one call from each verb.
    assert!(
        calls >= 3,
        "expected message:send and message:stream to both evaluate semantic policy, \
         found {calls} reference(s) to the shared helper",
    );
}

#[test]
fn the_skills_dispatch_path_evaluates_semantic_policy() {
    let src = fs::read_to_string("src/services/system_server.rs").expect("read system_server.rs");
    assert!(
        src.contains("semantic_policy::load_and_evaluate("),
        "skills__run_script — the canonical skills path — does not evaluate \
         semantic policy, while the deprecated REST route does",
    );
    // The judge calls the governed LLM surface as this caller; without the
    // caller's headers that self-call is unauthenticated and the evaluation
    // silently never blocks.
    assert!(
        src.contains("auth_headers,"),
        "the skills policy evaluation must forward the caller's auth headers to the judge",
    );
}

// ---------------------------------------------------------------------------
// A published invalidation must have somewhere to land.
// ---------------------------------------------------------------------------
//
// The control plane publishes a `semantic_policy` invalidation on every write.
// The proxy had no arm for it, so the message was received and dropped: an
// operator who tightened a policy kept enforcing the old one until the TTL
// lapsed, while the console showed the new one. A publisher with no subscriber
// is the worst shape of this bug — both sides look correct in isolation.

#[test]
fn the_proxy_handles_every_invalidation_type_the_control_plane_publishes() {
    // The handlers are a registry; it is asked, not the
    // source. A type without a handler gets the broad eviction, which does
    // not touch every cache, so a type that only it answers is silently a
    // no-op for that cache.
    let registry = brutor_gateway_core::services::cache::InvalidationRegistry::builtin();
    // These are the values the control plane actually publishes — verified
    // against `invalidate_proxy_cache(...)` call sites in
    // brutor-gateway-control/app/api/v1/endpoints/admin/. Keep this list in
    // step with that side; a name that matches on neither side is a message
    // nobody handles.
    for resource_type in [
        "semantic_policy",
        "guardrail",
        "agent_card",
        "model",
        "mcp_server",
        "skill",
        "resource_group",
    ] {
        assert!(
            registry.handlers_for(resource_type).next().is_some(),
            "no invalidation handler for `{resource_type}` — the control plane \
             publishes it and the proxy would drop it",
        );
    }
}

// ===========================================================================
// The LLM cost formula has exactly one definition
//
// The formula was reimplemented at nine sites across four files, and five of
// them used `if let (Some(pt), Some(ct))` / `match (Some, Some)`: if a provider
// reported only one of the two token counts — routine at the end of some SSE
// streams, and permanent for embeddings — the whole cost evaluated to `None`.
// `None` is not "unknown, ask later" here. It is written to
// `proxy_logs.llm_estimated_cost_usd` and charged to every budget counter as
// $0, so the calls most likely to report partial usage were the ones billed at
// nothing.
//
// `estimate_llm_cost` is now the single definition and prices a missing side at
// zero. This guard fails if the arithmetic reappears inline anywhere, because
// a tenth copy would be free to disagree with the other nine again.
// ===========================================================================

/// Files that carry LLM cost arithmetic, and are therefore where a
/// reimplementation would land.
const COST_SURFACES: &[&str] = &[
    "src/routes/llm.rs",
    "src/routes/gemini.rs",
    "src/routes/anthropic.rs",
    "src/services/anthropic_adapter/mod.rs",
];

/// Strip `//` line comments so prose describing the formula does not read as
/// the formula. (An explanatory comment tripping its own guard is exactly how
/// an earlier guard in this file was made to pass against a live defect.)
fn without_line_comments(src: &str) -> String {
    src.lines()
        .map(|l| match l.find("//") {
            Some(i) => &l[..i],
            None => l,
        })
        .collect::<Vec<_>>()
        .join("\n")
}

#[test]
fn the_cost_formula_is_not_reimplemented_on_any_llm_surface() {
    for path in COST_SURFACES {
        let src =
            std::fs::read_to_string(path).unwrap_or_else(|e| panic!("could not read {path}: {e}"));
        let code = without_line_comments(&src);
        assert!(
            !code.contains("/ 1_000_000.0"),
            "{path} divides by 1_000_000.0 outside llm_helpers — the per-token \
             cost formula has one definition, `llm_helpers::estimate_llm_cost`. \
             A local copy is free to disagree with it about a partially-reported \
             usage block, which is how five surfaces came to bill $0 for calls \
             that reported only one token count. If this is genuinely a \
             different quantity (micro-dollars, a savings estimate), name it so \
             and route the cost itself through the helper."
        );
    }
}

#[test]
fn every_cost_surface_calls_the_shared_estimator() {
    // The mirror of the test above: absence of the formula is only good news
    // if the helper is what replaced it.
    for path in COST_SURFACES {
        let src =
            std::fs::read_to_string(path).unwrap_or_else(|e| panic!("could not read {path}: {e}"));
        assert!(
            src.contains("estimate_llm_cost("),
            "{path} prices LLM calls but never calls estimate_llm_cost — either \
             the surface stopped recording cost (a governance regression: budget \
             counters go dark) or it grew its own formula under a new name."
        );
    }
}

// ===========================================================================
// Data residency has one implementation
//
// The provider-region lookup → profile load → check → 403-with-audit-row
// sequence was copied verbatim across three route files. The finding's own
// argument is the reason this guard exists: that duplication is *why* the
// sticky prompt-cache pin and the Gemini bridge each
// came to skip residency independently. There was no single place to add a
// check, so each surface drifted on its own schedule.
// ===========================================================================

/// Every route file that dispatches LLM traffic to a provider.
const RESIDENCY_SURFACES: &[&str] = &[
    "src/routes/llm.rs",
    "src/routes/gemini.rs",
    "src/routes/anthropic.rs",
];

#[test]
fn residency_is_never_re_implemented_in_a_route_file() {
    for path in RESIDENCY_SURFACES {
        let src =
            std::fs::read_to_string(path).unwrap_or_else(|e| panic!("could not read {path}: {e}"));
        let code = without_line_comments(&src);
        assert!(
            !code.contains("residency::load_tenant_profile"),
            "{path} loads the residency profile directly. The gate lives in \
             `llm_preflight::enforce_residency` and every surface calls it; a \
             local copy is one that can be forgotten when a new surface is \
             added."
        );
        assert!(
            !code.contains("residency::check("),
            "{path} calls residency::check directly instead of going through \
             `llm_preflight::enforce_residency`, which also writes the \
             residency_violation audit row. A bare check that refuses without \
             the row is worse than no check: the traffic is blocked and the \
             evidence that it was blocked does not exist."
        );
    }
}

#[test]
fn every_llm_surface_enforces_residency() {
    // The mirror: no local copy is only good news if the shared gate replaced
    // it. A surface that stopped calling it entirely would pass the test above.
    for path in RESIDENCY_SURFACES {
        let src =
            std::fs::read_to_string(path).unwrap_or_else(|e| panic!("could not read {path}: {e}"));
        assert!(
            src.contains("enforce_residency("),
            "{path} never calls enforce_residency — this surface can send a \
             tenant's prompts to a provider in a region their residency profile \
             forbids, and record nothing about it."
        );
    }
}

// ===========================================================================
// The semantic cache's governance contract
//
// The cache returns a stored completion WITHOUT running output guardrails over
// it. That is deliberate and is what makes the cache worth having, but it rests
// on one invariant: nothing reaches the cache that the policy would have
// refused. The store site therefore sits after the block check and after the
// redaction loop, and says so.
//
// Two ways that invariant fails, both guarded here.
//
// 1. The store moves back above the guardrail pass. Then a blocked or
//    unredacted completion is cached and served, forever after, with no check.
// 2. The policy changes under a cache that already holds content cleared by the
//    OLD policy. Tighten a `chat_output` rule and every matching cached
//    response keeps being served — up to 24 hours on the semantic layer's
//    default TTL — while the console shows the new rule. The invalidation arm
//    has to drop the responses, not just the rule cache.
// ===========================================================================

#[test]
fn a_guardrail_change_reaches_the_rule_caches_and_the_cached_responses_are_the_extensions() {
    // The arm is two registered handlers: `guardrail`
    // (the rule caches) and `guardrail_cached_responses` (the invariant
    // above). The second is registered by the response
    // cache's extension, and that extension is the Enterprise
    // workspace's, where the rest of this test went
    // (`enterprise/tests/response_cache_handler_guard.rs`,
    // `a_guardrail_change_drops_the_cached_responses_it_cleared`): a gateway
    // without the extension has no cached responses to drop. Held here: the
    // built-in handler answers both guardrail types, and the responses'
    // handler has not come back into the built-in registry.
    let registry = brutor_gateway_core::services::cache::InvalidationRegistry::builtin();
    assert!(
        registry
            .handlers()
            .iter()
            .all(|h| h.name != "guardrail_cached_responses"),
        "the handler is built in again: it is the extension's"
    );
    let rule_types: Vec<&'static str> = registry
        .handlers()
        .iter()
        .find(|h| h.name == "guardrail")
        .map(|h| h.resource_types.to_vec())
        .unwrap_or_default();
    assert!(
        rule_types.contains(&"guardrail") && rule_types.contains(&"guardrail_config"),
        "the guardrail handler no longer answers both guardrail types"
    );
    let order: Vec<&str> = registry.handlers_for("guardrail").map(|h| h.name).collect();
    assert_eq!(order, ["guardrail"]);
}

#[test]
fn every_semantic_cache_store_is_gated_on_the_guardrail_outcome() {
    // Both store sites, checked on their own terms — they protect the
    // invariant differently and a single ordering rule would describe neither.
    //
    // The NON-STREAMING store is guarded by position: it sits after the block
    // check and after the redaction loop, so `resp_body` is already clean.
    //
    // The STREAMING store cannot be guarded by position, because the content
    // is accumulated chunk by chunk and is NOT cleared when the guard fires.
    // At the store site `accumulated_content` still holds whatever was emitted
    // before the block. It has to be gated on the flag explicitly, and for a
    // while it was not: a blocked stream was logged 403 and cached anyway,
    // which turns one refusal into a permanent cached answer.
    let src = std::fs::read_to_string("src/routes/llm.rs").expect("read llm.rs");
    let code = without_line_comments(&src);

    // The routes no longer call the cache service; a store is
    // a plan from the response cache hook, committed later. The two chat
    // stores are therefore the two chat plans, and the invariant is checked
    // where it now lives: on the plan for the stream, on the commit for the
    // non-streamed answer. (The other surfaces' stores, and what every one of
    // them does, are pinned by behaviour in
    // `tests/integration/response_cache_surfaces.rs`.)
    assert_eq!(
        code.matches("store_background(").count(),
        0,
        "a route stores in the response cache directly; a store is a plan \
         from `services::response_cache`, committed after the verdict"
    );
    let streaming: Vec<usize> = code
        .match_indices("cache_session_bg.plan_store(")
        .map(|(i, _)| i)
        .collect();
    let non_streaming_plans = code
        .matches("cache_store_plan = cache_session.plan_store(")
        .count();
    assert_eq!(
        (streaming.len(), non_streaming_plans),
        (1, 1),
        "expected exactly two chat store plans (streaming and non-streaming); \
         a third needs its own guardrail gate and its own line in this test"
    );

    // Streaming store: an explicit `!guardrail_blocked` in the condition the
    // plan is asked for in. The plan call is itself part of that condition
    // (`if !guardrail_blocked && … && let Some(plan) = ….plan_store(`), so
    // between the `if` and the call there is no block.
    let streaming = streaming[0];
    let if_start = code[..streaming]
        .rfind("if ")
        .expect("the streaming store plan's enclosing condition");
    let cond = &code[if_start..streaming];
    assert!(
        !cond.contains('{'),
        "the streaming store plan is no longer asked for inside its own \
         condition; this test must be able to read that condition whole"
    );
    assert!(
        cond.contains("!guardrail_blocked"),
        "the streaming response-cache store is not gated on `guardrail_blocked`. \
         `accumulated_content` is not cleared when the guard fires, so a blocked \
         stream's truncated text is cached and then served without a guardrail \
         pass on every later hit."
    );

    // Non-streaming store: committed downstream of the output guardrail pass.
    let commit = code
        .find("cache_store_plan.take()")
        .expect("the non-streaming store plan's commit");
    let guard = code
        .find("apply_output_guardrails")
        .expect("the output guardrail pass");
    assert!(
        guard < commit,
        "the non-streaming response-cache store is now committed BEFORE the \
         output guardrail pass. The lookup path returns cached bodies without a \
         guardrail pass, so this ordering is the only thing keeping refused or \
         unredacted content out of the cache."
    );
}

#[test]
fn the_cache_hit_path_does_not_pretend_to_run_guardrails() {
    // The complement of the two above, and the reason they matter: this
    // records the design honestly instead of leaving a reader to assume a
    // check happens on the way out. If someone later adds an output-guardrail
    // pass to the hit path, this test should be deleted along with the
    // store-side ordering requirement — but not before, because half the
    // change would leave the cache both slow and unguarded.
    let src = std::fs::read_to_string("src/routes/llm.rs").expect("read llm.rs");
    let hit = src
        .find("// Return cached response.")
        .expect("the cache-hit return");
    let window = &src[hit.saturating_sub(4000)..hit];
    assert!(
        !window.contains("apply_output_guardrails"),
        "the cache-hit path now runs output guardrails. That is a legitimate \
         design change, but it makes the store-side ordering guard redundant \
         rather than load-bearing — update both together."
    );
}
