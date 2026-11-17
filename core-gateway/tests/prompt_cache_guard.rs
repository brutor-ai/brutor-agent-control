// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The request path reaches provider-side prompt caching only
//! through the hook (`services::prompt_caching`).
//!
//! Before the hook, the router, `routes/llm.rs`, `routes/gemini.rs` and the
//! native Anthropic pipeline each loaded the group's prompt-cache policy and
//! called the feature's functions themselves: six places that marked a
//! request, one that followed a pin, one that dropped it, two that set it.
//! The feature is Enterprise and those files are Community. This guard keeps
//! an eleventh call from growing back:
//!
//! * outside the feature's own files, no source file names the feature's
//!   request-path functions or types;
//! * the feature's service is named by no file outside the feature: it
//!   lives in the extension state, and its invalidation handlers, reconnect
//!   flush and console layer are registered by the extension;
//!   the composition root names the extension, and two Community files
//!   still name the service in their unit tests only;
//! * the router asks the session once for each thing it asks, every surface
//!   that marks opens a session, and the route reports a warmed cache only
//!   for an answer;
//! * a state built without an extension holds the default, and the
//!   extension installs the implementation.
//!
//! What prompt caching then does on each surface is pinned by behaviour, in
//! `tests/integration/prompt_cache_surfaces.rs`.

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

/// The feature's own files (`enterprise_modules.prompt_cache` in the
/// editions manifest).
const FEATURE_FILES: [&str; 4] = [
    "enterprise/src/prompt_cache.rs",
    "enterprise/src/prompt_cache/anthropic_cache.rs",
    "enterprise/src/prompt_cache/openai_cache.rs",
    "enterprise/src/prompt_cache/gemini_cache.rs",
];

/// What marks a request, follows a pin, sets or drops one, or reads a
/// policy. No file outside the feature names any of these.
const REQUEST_PATH: [&str; 14] = [
    "apply_to_request",
    "plan_breakpoints",
    "resolve_sticky_pinned_model",
    "record_sticky_pin_after_warm",
    "clear_sticky_pin_for_request",
    "compute_sticky_prefix_hash",
    "sticky_get(",
    "sticky_set(",
    ".policies.load(",
    "GroupPromptCachePolicy",
    "CacheBreakpointPlan",
    "anthropic_cache::",
    "gemini_cache::",
    "openai_cache::",
];

/// Ways of naming the feature's module or its service at all.
const HANDLE: [&str; 6] = [
    "prompt_cache::",
    ".prompt_cache.",
    "PromptCacheService",
    "PromptCachePolicyCache",
    "PromptCacheRegexCache",
    "ProviderPromptCache",
];

/// The files outside the feature that name its module, and why.
/// Prompt caching is an extension (`PromptCaching`, in the
/// feature's own module): `AppState`, the builder, the task list, the
/// invalidation listener and `system_notify` hold no handle and name
/// nothing of it. What is left is the composition root.
const HANDLE_HOLDERS: [(&str, &str); 1] = [(
    "enterprise/src/main.rs",
    "composes the extension: `.extension(PromptCaching::new())`",
)];

/// Files outside the feature whose *unit tests* reach the feature's
/// service: they record what the full composition evicts and reports, on a
/// state the feature is installed on. Their production code is held to the
/// rule like any other file. Since the move
/// these are the Enterprise workspace's tests; no Community file is left.
const TEST_HOLDERS: [(&str, &str); 1] = [(
    "enterprise/src/invalidation_tests.rs",
    "the invalidation recorder of the full composition, and the console's `response` layer",
)];

/// The file without its unit tests.
fn production(src: &str) -> &str {
    match src.find("#[cfg(test)]\nmod tests") {
        Some(at) => &src[..at],
        None => src,
    }
}

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) {
    for entry in fs::read_dir(dir).unwrap_or_else(|e| panic!("read {}: {e}", dir.display())) {
        let path = entry.expect("dir entry").path();
        if path.is_dir() {
            rust_files(&path, out);
        } else if path.extension().is_some_and(|x| x == "rs") {
            out.push(path);
        }
    }
}

fn code_lines(src: &str) -> impl Iterator<Item = (usize, &str)> {
    src.lines()
        .enumerate()
        .filter(|(_, l)| !l.trim_start().starts_with("//"))
        .map(|(n, l)| (n + 1, l))
}

fn uses(src: &str, needles: &[&str]) -> Vec<String> {
    let mut found = Vec::new();
    for (n, line) in code_lines(src) {
        for needle in needles {
            if line.contains(needle) {
                found.push(format!("{n}: {}", line.trim()));
            }
        }
    }
    found
}

fn count(src: &str, needle: &str) -> usize {
    code_lines(src)
        .map(|(_, l)| l.matches(needle).count())
        .sum()
}

#[test]
fn nothing_outside_the_feature_names_its_request_path_or_its_service() {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    assert!(files.len() > 100, "the scan found the source tree");
    let mut seen_holders = Vec::new();
    for path in files {
        let name = path.to_string_lossy().replace('\\', "/");
        if FEATURE_FILES.contains(&name.as_str()) {
            continue;
        }
        let src = fs::read_to_string(&path).unwrap_or_else(|e| panic!("read {name}: {e}"));
        let found = uses(&src, &REQUEST_PATH);
        assert!(
            found.is_empty(),
            "{name} reaches prompt caching without the hook \
             (services::prompt_caching):\n{}",
            found.join("\n")
        );
        if HANDLE_HOLDERS.iter().any(|(holder, _)| *holder == name) {
            // A holder names the module, and only to compose the extension.
            let found: Vec<String> = uses(&src, &HANDLE)
                .into_iter()
                .map(|l| {
                    l.split_once(": ")
                        .map_or(l.clone(), |(_, code)| code.to_string())
                })
                .collect();
            assert_eq!(
                found,
                ["use brutor_gateway_enterprise::prompt_cache::PromptCaching;"],
                "{name} names more of prompt caching than the extension it composes"
            );
            seen_holders.push(name);
            continue;
        }
        if TEST_HOLDERS.iter().any(|(holder, _)| *holder == name) {
            let found = uses(production(&src), &HANDLE);
            assert!(
                found.is_empty(),
                "{name}: production code names the prompt-cache service (only its unit \
                 tests may, until they move):\n{}",
                found.join("\n")
            );
            assert!(
                !uses(&src, &HANDLE).is_empty(),
                "{name} is listed as a test holder and its tests no longer name the \
                 service: update the list"
            );
            continue;
        }
        let found = uses(&src, &HANDLE);
        assert!(
            found.is_empty(),
            "{name} names the prompt-cache service; the request path asks \
             state.prompt_cache_policy, and only the listed files hold the handle:\n{}",
            found.join("\n")
        );
    }
    assert_eq!(
        seen_holders.len(),
        HANDLE_HOLDERS.iter().filter(|(h, _)| here(h)).count(),
        "a listed holder of the handle no longer exists: {seen_holders:?}"
    );
}

#[test]
fn the_feature_files_exist_and_hold_the_implementation() {
    for path in FEATURE_FILES.into_iter().filter(|p| here(p)) {
        assert!(Path::new(path).exists(), "{path}");
    }
    if here(FEATURE_FILES[0]) {
        let feature = fs::read_to_string(FEATURE_FILES[0]).expect("read prompt_cache.rs");
        assert!(feature.contains("pub struct ProviderPromptCache"));
        assert!(feature.contains("impl PromptCachePolicy for ProviderPromptCache"));
    }
    // The hook module and the accounting module name nothing of the feature
    // (they are in the general scan too; this says it of them by name).
    for community in [
        "src/services/prompt_caching.rs",
        "src/services/prompt_cache_usage.rs",
    ] {
        let src = fs::read_to_string(community).unwrap_or_else(|e| panic!("read {community}: {e}"));
        assert!(uses(&src, &REQUEST_PATH).is_empty(), "{community}");
        assert!(uses(&src, &HANDLE).is_empty(), "{community}");
    }
}

#[test]
fn a_state_starts_with_the_default_and_the_extension_installs_the_feature() {
    let state = fs::read_to_string("src/services/mod.rs").expect("read services/mod.rs");
    assert_eq!(
        count(
            &state,
            "prompt_cache_policy: Arc::new(prompt_caching::NoPromptCaching)"
        ),
        1,
        "AppState::new holds the Community default"
    );
    assert_eq!(
        count(&state, "ProviderPromptCache"),
        0,
        "and never the implementation"
    );
    // The builder installs nothing of it: the extension does, in its
    // `install_state`, and `main` composes the extension.
    let builder = fs::read_to_string("src/builder.rs").expect("read builder.rs");
    assert!(uses(&builder, &HANDLE).is_empty(), "the builder names it");
    assert_eq!(
        count(&state, "prompt_cache:"),
        0,
        "AppState holds no handle"
    );
    // A Community tree alone: the feature's module is not here to read.
    if !here(FEATURE_FILES[0]) {
        return;
    }
    let feature = fs::read_to_string(FEATURE_FILES[0]).expect("read prompt_cache.rs");
    assert_eq!(count(&feature, "impl Extension for PromptCaching"), 1);
    assert_eq!(count(&feature, "ProviderPromptCache::install(state)"), 1);
    let main = fs::read_to_string("enterprise/src/main.rs").expect("read main.rs");
    assert_eq!(count(&main, ".extension(PromptCaching::new())"), 1);
}

/// Where the request path asks the hook, and how often. A new place that
/// sends a request to a provider and should mark it shows up here as a
/// count that no longer matches, in one direction or the other.
#[test]
fn the_request_path_asks_the_hook_where_it_did_and_nowhere_else() {
    let read = |path: &str| fs::read_to_string(path).unwrap_or_else(|e| panic!("read {path}: {e}"));

    // The router is handed a session; it opens none.
    let router = read("src/services/llm_router.rs");
    assert_eq!(count(&router, ".prompt_cache_policy"), 0);
    assert_eq!(count(&router, "session.mark(&mut body, model)"), 1);
    assert_eq!(
        count(&router, "session.pinned_model(primary_model, request)"),
        1
    );
    assert_eq!(
        count(&router, "session.clear_pin(request, primary_model)"),
        1
    );
    assert_eq!(
        count(&router, ".record_warm("),
        0,
        "the router does not know whether the answer warmed anything"
    );

    // Sessions are opened by the chat handler (once, for both its
    // branches), the Gemini surface (unary, streamed, and the bridge from
    // the chat surface) and the native Anthropic pipeline (streamed and
    // not). Each of the last five marks the body it is about to send.
    for (path, sessions, marks) in [
        ("src/routes/llm.rs", 1, 0),
        ("src/routes/gemini.rs", 3, 3),
        ("src/services/anthropic_adapter/mod.rs", 2, 2),
        ("src/routes/anthropic.rs", 0, 0),
    ] {
        let src = read(path);
        assert_eq!(
            count(&src, ".prompt_cache_policy"),
            sessions,
            "{path}: sessions opened"
        );
        assert_eq!(
            count(&src, "prompt_cache.mark(&mut body"),
            marks,
            "{path}: requests marked"
        );
    }

    // The chat handler hands its session to the router, and reports a
    // warmed cache from both of its branches.
    let chat = read("src/routes/llm.rs");
    assert_eq!(count(&chat, "Some(prompt_cache.as_ref()),"), 1);
    assert_eq!(count(&chat, ".record_warm("), 2);
    assert!(
        warm_reports_without_a_success_check(&chat).is_empty(),
        "a non-streamed warm report is not under `if success`:\n{}",
        warm_reports_without_a_success_check(&chat).join("\n")
    );
}

/// Non-streamed warm reports (`prompt_cache` itself, not the stream task's
/// `prompt_cache_bg`) with no `if success {` on the line before. The
/// non-streamed branch also handles an upstream error, and an error body
/// with a usage block must not pin the model that just failed.
fn warm_reports_without_a_success_check(src: &str) -> Vec<String> {
    let lines: Vec<&str> = code_lines(src).map(|(_, l)| l).collect();
    let mut found = Vec::new();
    for (i, line) in lines.iter().enumerate() {
        let direct = line.trim() == "prompt_cache"
            && lines
                .get(i + 1)
                .is_some_and(|next| next.trim_start().starts_with(".record_warm("));
        let inline = line.contains("prompt_cache.record_warm(");
        if (direct || inline) && !(i > 0 && lines[i - 1].trim() == "if success {") {
            found.push(line.trim().to_string());
        }
    }
    found
}

#[test]
fn the_scans_see_what_they_forbid() {
    // The shape the routes had before the hook.
    let old_route = "let prompt_cache_policy = state\n    .prompt_cache\n    .policies\n    \
                     .load(&state.db, &tenant_id, None, Some(&model.id))\n    .await;\n\
                     if let Some(ref policy) = prompt_cache_policy {\n    \
                     let decision = crate::services::prompt_cache::apply_to_request_async(\n        \
                     &mut body, &model, policy, &tenant_id, state.redis.as_ref(),\n        \
                     &state.http_client, Some(&state.prompt_cache.regex_cache),\n    ).await;\n}\n";
    assert_eq!(
        uses(old_route, &REQUEST_PATH).len(),
        1,
        "{:?}",
        uses(old_route, &REQUEST_PATH)
    );
    assert_eq!(
        uses(old_route, &HANDLE).len(),
        2,
        "{:?}",
        uses(old_route, &HANDLE)
    );
    // The policy load written on one line.
    assert_eq!(
        uses(
            "let p = state.prompt_cache.policies.load(&state.db, t, g, m).await;\n",
            &REQUEST_PATH
        )
        .len(),
        1
    );
    // The shape the router had.
    let old_router = "prompt_cache_policy: Option<&crate::services::prompt_cache::GroupPromptCachePolicy>,\n\
                      crate::services::prompt_cache::resolve_sticky_pinned_model(db, m, r, p, t, redis)\n\
                      crate::services::prompt_cache::clear_sticky_pin_for_request(r, m, p, t, redis)\n\
                      crate::services::prompt_cache::record_sticky_pin_after_warm(r, m, w, p, t, redis)\n";
    assert_eq!(uses(old_router, &REQUEST_PATH).len(), 4);
    assert_eq!(uses(old_router, &HANDLE).len(), 4);
    // An adapter's marker code called directly.
    assert_eq!(
        uses(
            "let n = anthropic_cache::apply_plan(&mut body, &plan);\n",
            &REQUEST_PATH
        )
        .len(),
        1
    );
    // What is allowed: the hook, the accounting module, comments.
    for allowed in [
        "let session = state.prompt_cache_policy.session(&state, t, g, &model).await;\n",
        "let m = crate::services::prompt_cache_usage::mechanism_for_provider(p, n);\n",
        "crate::services::prompt_caching::record_decision(&decision);\n",
        "// crate::services::prompt_cache::apply_to_request_async( in a comment\n",
        "pub mod anthropic_cache;\n",
    ] {
        assert!(uses(allowed, &REQUEST_PATH).is_empty(), "{allowed}");
        assert!(uses(allowed, &HANDLE).is_empty(), "{allowed}");
    }

    // A warm report outside `if success`.
    let unchecked = "if pc_usage.write_tokens > 0 {\n    prompt_cache\n        \
                     .record_warm(&request, &used_model, pc_usage.write_tokens)\n        .await;\n}\n";
    assert_eq!(warm_reports_without_a_success_check(unchecked).len(), 1);
    let unchecked_inline = "prompt_cache.record_warm(&request, &used_model, w).await;\n";
    assert_eq!(
        warm_reports_without_a_success_check(unchecked_inline).len(),
        1
    );
    let checked = "if success {\n    prompt_cache\n        \
                   .record_warm(&request, &used_model, pc_usage.write_tokens)\n        .await;\n}\n";
    assert!(warm_reports_without_a_success_check(checked).is_empty());
    // The stream task's report is not this rule's: it runs only for a
    // stream the upstream opened with a success status.
    let streamed =
        "prompt_cache_bg\n    .record_warm(&request_for_cache, &model_bg, w)\n    .await;\n";
    assert!(warm_reports_without_a_success_check(streamed).is_empty());
}
