// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
//! # Route pipeline audit (regression guard)
//!
//! Each LLM-mode proxy route MUST apply the same governance pipeline:
//! authenticate, enforce quota, check group access, write proxy_logs,
//! write usage_records, etc. Multimodal routes (image, audio_speech,
//! audio_transcribe, video) historically skipped most of these steps
//! and silently bypassed budget caps — that bug class is what these
//! tests catch.
//!
//! Approach: structural source-level grep. Each test reads the route
//! file and asserts that the canonical helper calls are present.
//! It's coarse — we're not running the routes — but it makes the
//! "did we forget the pipeline?" mistake impossible to land without
//! a compile-time test failure.
//!
//! When you add a new mode endpoint, add it to the list below.

use std::fs;
use std::path::PathBuf;

/// One row in the audit matrix that the README documents.
///
/// `path`        is the route file under `src/routes/`.
/// `needles`     are substrings that MUST appear in the file. Each
///               proves a specific pipeline step is invoked. If a
///               future refactor renames the helper, update this
///               list AND the README in lockstep.
/// `description` is what shows up in the assertion message so a
///               regression report points at the missing step
///               directly.
struct RouteSpec {
    name: &'static str,
    path: &'static str,
    needles: &'static [(&'static str, &'static str)],
}

fn read(rel: &str) -> String {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.push(rel);
    fs::read_to_string(&p).unwrap_or_else(|e| panic!("could not read {rel}: {e}"))
}

/// The four multimodal routes that the gap-fix patch wired into the
/// canonical pipeline. Each must call `multimodal_preflight` (auth +
/// model + group + quota + input guardrails) and
/// `record_multimodal_outcome` (proxy_log + usage_record). If a
/// future PR drops one of those calls the matching test fails and
/// the README's matrix stays accurate by construction.
fn multimodal_routes() -> Vec<RouteSpec> {
    vec![
        RouteSpec {
            name: "audio_speech",
            path: "src/routes/audio_speech.rs",
            needles: &[
                (
                    "multimodal_preflight(",
                    "preflight (auth+quota+access+input-guardrails)",
                ),
                (
                    "acquire_gate(",
                    "resilience gate (circuit breaker + concurrency)",
                ),
                (
                    "record_multimodal_outcome(",
                    "audit + usage record + breaker signal",
                ),
            ],
        },
        RouteSpec {
            name: "audio_transcribe",
            path: "src/routes/audio_transcribe.rs",
            needles: &[
                (
                    "multimodal_preflight(",
                    "preflight (auth+quota+access+input-guardrails)",
                ),
                (
                    "acquire_gate(",
                    "resilience gate (circuit breaker + concurrency)",
                ),
                (
                    "record_multimodal_outcome(",
                    "audit + usage record + breaker signal",
                ),
                // Output guardrails are critical here — STT
                // converts user voice to text and PII spoken into
                // a microphone has nowhere else to be filtered.
                (
                    "apply_output_guardrails(",
                    "output guardrails on transcribed text",
                ),
            ],
        },
        RouteSpec {
            name: "images",
            path: "src/routes/images.rs",
            needles: &[
                (
                    "multimodal_preflight(",
                    "preflight (auth+quota+access+input-guardrails)",
                ),
                (
                    "acquire_gate(",
                    "resilience gate (circuit breaker + concurrency)",
                ),
                (
                    "record_multimodal_outcome(",
                    "audit + usage record + breaker signal",
                ),
            ],
        },
        RouteSpec {
            name: "video",
            path: "src/routes/video.rs",
            needles: &[
                (
                    "multimodal_preflight(",
                    "preflight (auth+quota+access+input-guardrails)",
                ),
                (
                    "acquire_gate(",
                    "resilience gate (circuit breaker + concurrency)",
                ),
                (
                    "record_multimodal_outcome(",
                    "audit + usage record + breaker signal",
                ),
            ],
        },
    ]
}

/// Text-mode routes (chat / completion / responses / embedding)
/// already had the canonical pipeline before the multimodal fix.
/// We assert their calls too so a future refactor can't quietly
/// drop them — for example, if someone replaces `run_preflight`
/// with a thinner helper that forgets to enforce quotas.
fn text_routes() -> Vec<RouteSpec> {
    vec![RouteSpec {
        name: "llm.rs (chat / completion / responses / embedding)",
        path: "src/routes/llm.rs",
        needles: &[
            // Step 1-3 (auth + quota + access)
            ("run_preflight(", "preflight (auth+quota+access)"),
            // Step 4 — input guardrails. Must appear in the
            // file at all; the per-handler audit lower down
            // ensures responses + embedding got their own
            // calls in addition to chat + completion.
            // Bare-name match — both the historical `apply_guardrails(`
            // and the current `apply_guardrails_with_redaction(`
            // (the name with Redact-on-LLM-input)
            // satisfy this needle.
            ("apply_guardrails", "input guardrails"),
            // Step 9 — proxy_logs row
            ("build_proxy_log(", "proxy_logs row"),
            // Step 8 — usage_records row (text routes use the
            // mode-specific writers)
            ("write_llm_usage_log(", "llm usage record"),
            ("write_embedding_usage_log(", "embedding usage record"),
        ],
    }]
}

#[test]
fn multimodal_routes_apply_canonical_pipeline() {
    let mut failures: Vec<String> = Vec::new();
    for spec in multimodal_routes() {
        let body = read(spec.path);
        for (needle, desc) in spec.needles {
            if !body.contains(needle) {
                failures.push(format!(
                    "{} ({}): MISSING `{}` — step '{}'",
                    spec.name, spec.path, needle, desc,
                ));
            }
        }
    }
    assert!(
        failures.is_empty(),
        "Multimodal route pipeline regressed:\n  {}",
        failures.join("\n  ")
    );
}

#[test]
fn text_routes_keep_canonical_pipeline() {
    let mut failures: Vec<String> = Vec::new();
    for spec in text_routes() {
        let body = read(spec.path);
        for (needle, desc) in spec.needles {
            if !body.contains(needle) {
                failures.push(format!(
                    "{} ({}): MISSING `{}` — step '{}'",
                    spec.name, spec.path, needle, desc,
                ));
            }
        }
    }
    assert!(
        failures.is_empty(),
        "Text route pipeline regressed:\n  {}",
        failures.join("\n  ")
    );
}

#[test]
fn multimodal_pipeline_module_exists() {
    // The module name itself is load-bearing — it's referenced by
    // the README. Don't let someone rename it without updating the
    // docs.
    let body = read("src/services/mod.rs");
    assert!(
        body.contains("pub mod multimodal_pipeline;"),
        "src/services/mod.rs must register the multimodal_pipeline \
         module — it's the canonical helper layer for non-text \
         routes (audio, image, video). Renaming it requires \
         updating both the README pipeline table AND \
         tests/route_pipeline_audit.rs."
    );
}

#[test]
fn a2a_routes_use_agent_card_cache() {
    // Both A2A handlers must go through `state.agent_card_cache`
    // to look up cards / capabilities / discovery rows. If a future
    // refactor reintroduces a raw `FROM agent_cards` SELECT in
    // these route files, that's a per-request DB round-trip on
    // the A2A hot path — exactly the regression class this audit
    // exists to catch.
    for path in ["src/routes/a2a.rs", "src/routes/a2a_v1.rs"] {
        let body = read(path);
        assert!(
            body.contains("agent_card_cache"),
            "{path} must consult state.agent_card_cache for card \
             and capability lookups — see src/services/cache/README.md."
        );
        // Belt-and-braces: the legacy direct SELECT should not
        // come back. We allow the substring inside SQL comments
        // by scoping to lines starting with whitespace + SELECT.
        for (lineno, line) in body.lines().enumerate() {
            let trimmed = line.trim_start();
            if trimmed.starts_with("//") {
                continue;
            }
            if trimmed.contains("FROM agent_cards") {
                panic!(
                    "{path}:{} contains a direct `FROM agent_cards` \
                     SELECT — route files must go through \
                     AgentCardCache. If this query is genuinely \
                     needed (admin-only path?), move it out of the \
                     hot-path route module or update this assertion \
                     with a justification.\nLine: {}",
                    lineno + 1,
                    line.trim()
                );
            }
        }
    }
}

#[test]
fn cache_invalidator_holds_shared_caches() {
    // Regression guard for the bug discovered during the
    // resource-cache audit (May 2026): main.rs was constructing
    // (the block is now the `cache_invalidation_listener` task in
    // src/tasks.rs, where this test reads it)
    // FRESH `ModelCache::new(...)` etc. for the invalidator, which
    // produced throwaway caches divorced from the live ones in
    // `state.*`. Pub/sub invalidations evicted nothing.
    //
    // The fix: every cache passed to the invalidator must be a
    // Clone-handle of the corresponding `state.*` field (moka's
    // `Cache` clone shares the inner `Arc`-backed store). This
    // test asserts the shape so a refactor that reintroduces a
    // fresh `::new(` construction inside the invalidator block
    // fails CI loudly.
    let main_body = read("src/tasks.rs");
    let needle = "let invalidator = services::cache::CacheInvalidator {";
    let block = main_body
        .split_once(needle)
        .map(|(_, after)| after)
        .and_then(|after| after.split_once("};\n"))
        .map(|(block, _)| block)
        .unwrap_or_else(|| {
            panic!("Could not locate CacheInvalidator construction block in src/tasks.rs")
        });
    for fresh_ctor in [
        "ModelCache::new(",
        "ResourceGroupCache::new(",
        "McpServerCache::new(",
        "AgentSkillCache::new(",
        "AgentCardCache::new(",
        "GuardrailsCache::new(",
    ] {
        assert!(
            !block.contains(fresh_ctor),
            "src/tasks.rs constructs a fresh `{fresh_ctor})` inside \
             the CacheInvalidator block — pub/sub invalidations \
             will evict a throwaway cache, not the live one in \
             `state.*`. Use `state.*.clone()` instead. See \
             src/services/cache/README.md."
        );
    }
    for shared_field in [
        "model_cache: state.model_cache.clone()",
        "group_cache: state.group_cache.clone()",
        "mcp_cache: state.mcp_cache.clone()",
        "skill_cache: state.skill_cache.clone()",
        "agent_card_cache: state.agent_card_cache.clone()",
        "guardrails_cache: state.guardrails_cache.clone()",
    ] {
        assert!(
            block.contains(shared_field),
            "CacheInvalidator must wire `{shared_field}` to share \
             state with the request pipeline. See \
             src/services/cache/README.md."
        );
    }
}

#[test]
fn multimodal_routes_record_media_unit_counters() {
    // Each media route bills in its own unit (images, chars,
    // seconds). The proxy_logs row carries those as discrete
    // columns so dashboard SUM()s give clean per-mode roll-ups —
    // see the LLM Media panel in mission control + group usage.
    //
    // The assertions below catch the silent-drop class: a route
    // builds a `MultimodalOutcome` but forgets to populate the
    // counter that matches its mode. The whole point of typed
    // counters is to keep cost-per-unit dashboards honest, so we
    // refuse to let them go missing.
    let cases: &[(&str, &str, &str)] = &[
        (
            "src/routes/images.rs",
            "images_generated:",
            "image route must record `media_units.images_generated` so \
             the LLM Media panel can SUM image counts per tenant.",
        ),
        (
            "src/routes/audio_speech.rs",
            "audio_chars:",
            "TTS route must record `media_units.audio_chars` — the \
             billable unit for audio_speech is input characters.",
        ),
        (
            "src/routes/audio_transcribe.rs",
            "audio_seconds_input:",
            "STT route must record `media_units.audio_seconds_input` \
             — the billable unit for audio_transcription is the \
             provider-reported audio duration.",
        ),
        (
            "src/routes/video.rs",
            "video_seconds_output:",
            "Video route must record `media_units.video_seconds_output` \
             — submit-time logs the requested clip duration so \
             dashboards can roll up planned video seconds.",
        ),
    ];
    for (path, needle, msg) in cases {
        let body = read(path);
        assert!(body.contains(needle), "{path}: {msg} (missing `{needle}`)");
    }
}

#[test]
fn proxy_logs_use_resource_group_id() {
    // Every multimodal route's `record_multimodal_outcome` call
    // ends up populating `resource_group_id` so admins can filter
    // proxy_logs by resource group in the dashboard. Catches the
    // mistake where a future rewrite stops threading the group
    // into the log row.
    let body = read("src/services/multimodal_pipeline.rs");
    assert!(
        body.contains("resource_group_id ="),
        "multimodal_pipeline.rs must stamp `resource_group_id` on \
         the proxy_logs row — without it, admin proxy-log filters \
         by group return zero results for image/audio/video traffic."
    );
}

// ===========================================================================
// The MCP body has two values, and the difference is load-bearing
// ===========================================================================
//
// The MCP hot path parses the request body several times, and the obvious
// optimisation — "parse once into a struct and pass it down" — is wrong,
// because `body_bytes` is REWRITTEN mid-request while `parsed_body` is not:
//
//   * the era bridge (`ensure_modern_meta`) mutates `parsed_body` and
//     re-serialises it into `body_bytes`, so the two stay in step; but
//   * input-guardrail redaction rewrites `body_bytes` from
//     `apply_redactions_to_subtree` and leaves `parsed_body` alone.
//
// After that point they are deliberately different: `body_bytes` is what gets
// forwarded upstream (redacted), and `parsed_body` is what the caller actually
// sent. Argument-policy enforcement reads the latter, and must: a policy that
// judged redacted input would be defeated by turning PII redaction ON — an
// SSRF host check against `https://[REDACTED_EMAIL]@evil.example` is not the
// check anyone configured.
//
// So this guard pins the read, not the parse count. Collapsing the parses is
// fine; collapsing them onto the WRONG one silently weakens a governance
// control, with no test failure and no log line to notice it by.

#[test]
fn mcp_argument_policy_reads_the_caller_body_not_the_redacted_one() {
    let src = read("src/routes/mcp.rs");

    let policy_at = src
        .find("argument_policy::enforce(")
        .expect("MCP argument-policy enforcement");
    // The `arguments` value handed to `enforce` is built just above the call.
    let window_start = src[..policy_at].rfind("let arguments =").expect(
        "the `arguments` binding that feeds argument_policy::enforce — if this \
         moved, re-read the note above before adjusting the test",
    );
    let window = &src[window_start..policy_at];

    assert!(
        window.contains("parsed_body"),
        "MCP argument policy no longer reads `parsed_body`. It must evaluate \
         what the CALLER sent, not the post-redaction body that goes upstream \
         — otherwise enabling PII redaction quietly defeats every argument \
         policy that inspects the redacted fields."
    );
    assert!(
        !window.contains("body_bytes"),
        "MCP argument policy now reads `body_bytes`, which by this point may \
         have been rewritten by input-guardrail redaction. The policy would \
         then judge masked values instead of the caller's real arguments."
    );
}

#[test]
fn the_mcp_group_resolution_happens_once() {
    // The billing group and the capability-filter groups were
    // resolved by two separate calls that ran the same membership query and
    // the same per-group chain walk. They are equivalent (the billing group is
    // the first granting group), which is pinned against a real database in
    // tests/integration/mcp_group_resolution.rs; this keeps the route from
    // quietly going back to two.
    let src = read("src/routes/mcp.rs");
    assert!(
        !src.contains("resolve_mcp_server_access("),
        "src/routes/mcp.rs resolves the billing group separately again. \
         `resolve_all_mcp_server_groups` already returns it as the first \
         element — see the equivalence suite in \
         tests/integration/mcp_group_resolution.rs."
    );
    assert_eq!(
        src.matches("resolve_all_mcp_server_groups(").count(),
        1,
        "group resolution should happen exactly once per MCP request"
    );
}
