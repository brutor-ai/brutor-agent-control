// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The LLM routes reach the response cache only through the
//! hook (`services::response_cache`).
//!
//! Before the hook, five places in `routes/llm.rs` each called the cache
//! service with their own copy of its gating, and two of the copies had lost
//! a condition (a grounded answer stored; a refused answer
//! stored). The hook has one copy. This guard keeps a sixth from growing
//! back, and keeps the two conditions that stayed with the routes next to
//! every place they apply:
//!
//! * a route file names neither the cache service nor its store and lookup
//!   calls;
//! * a store is always a plan and a commit, never fewer commits than plans;
//! * wherever a chat request is looked up or planned, the grounded-group
//!   check is in the same statement's neighbourhood.
//!
//! What the cache then does on each surface is pinned by behaviour, in
//! `tests/integration/response_cache_surfaces.rs`.

use std::fs;

/// The route modules that answer LLM requests.
const LLM_ROUTES: [&str; 3] = [
    "src/routes/llm.rs",
    "src/routes/anthropic.rs",
    "src/routes/gemini.rs",
];

/// Ways of reaching the cache service without the hook. The field access,
/// not the bare word: the audit row for a hit is built by a helper whose
/// name contains it (`build_semantic_cache_hit_log`), and that is not a way
/// to the cache.
const DIRECT: [&str; 6] = [
    ".semantic_cache",
    "SemanticCacheService",
    "store_background(",
    "store_value_background(",
    "lookup_value(",
    "should_cache_value(",
];

/// The router and the routes do not operate the exact-match cache
/// themselves (R10b): its key, its identity, its tenant switch and its map
/// are reached only through `response_cache::DispatchCache`.
const DISPATCH_CALLERS: [&str; 4] = [
    "src/services/llm_router.rs",
    "src/routes/llm.rs",
    "src/routes/anthropic.rs",
    "src/routes/gemini.rs",
];

const DISPATCH_DIRECT: [&str; 7] = [
    "get_cached_response(",
    "cache_response(",
    "cache_gate_for(",
    "build_cache_key(",
    "cache_identity_for(",
    "ExactDispatchCache",
    "dispatch_exact_cache",
];

fn dispatch_direct_uses(src: &str) -> Vec<String> {
    let mut found = Vec::new();
    for (n, line) in src.lines().enumerate() {
        if line.trim_start().starts_with("//") {
            continue;
        }
        for needle in DISPATCH_DIRECT {
            if line.contains(needle) {
                found.push(format!("{}: {}", n + 1, line.trim()));
            }
        }
    }
    found
}

/// How far from a chat lookup or plan the grounded-group check may be.
const GROUNDED_WINDOW: usize = 12;

fn code_lines(src: &str) -> Vec<&str> {
    src.lines()
        .filter(|l| !l.trim_start().starts_with("//"))
        .collect()
}

fn direct_uses(src: &str) -> Vec<String> {
    let mut found = Vec::new();
    for (n, line) in src.lines().enumerate() {
        if line.trim_start().starts_with("//") {
            continue;
        }
        for needle in DIRECT {
            if line.contains(needle) {
                found.push(format!("{}: {}", n + 1, line.trim()));
            }
        }
    }
    found
}

/// Chat lookups and plans with no `kb_grounded` within the window before
/// or after them.
fn ungrounded_chat_uses(src: &str) -> Vec<String> {
    let lines = code_lines(src);
    let mut found = Vec::new();
    for (i, line) in lines.iter().enumerate() {
        if !line.contains("CacheBody::Chat(") {
            continue;
        }
        let from = i.saturating_sub(GROUNDED_WINDOW);
        let to = (i + GROUNDED_WINDOW).min(lines.len() - 1);
        if !lines[from..=to].iter().any(|l| l.contains("kb_grounded")) {
            found.push(line.trim().to_string());
        }
    }
    found
}

#[test]
fn llm_routes_reach_the_response_cache_only_through_the_hook() {
    for path in LLM_ROUTES {
        let src = fs::read_to_string(path).unwrap_or_else(|e| panic!("read {path}: {e}"));
        let found = direct_uses(&src);
        assert!(
            found.is_empty(),
            "{path} reaches the response cache without the hook \
             (services::response_cache):\n{}",
            found.join("\n")
        );
    }
}

#[test]
fn the_router_and_the_routes_reach_the_exact_match_cache_only_through_the_hook() {
    for path in DISPATCH_CALLERS {
        let src = fs::read_to_string(path).unwrap_or_else(|e| panic!("read {path}: {e}"));
        let found = dispatch_direct_uses(&src);
        assert!(
            found.is_empty(),
            "{path} operates the exact-match cache itself instead of asking \
             response_cache::DispatchCache:\n{}",
            found.join("\n")
        );
    }
    // The router asks the hook exactly once, and uses what it got for both
    // the read and the write.
    let router = fs::read_to_string("src/services/llm_router.rs").expect("read llm_router.rs");
    let code = code_lines(&router).join("\n");
    assert_eq!(code.matches("current_dispatch_cache()").count(), 1);
    assert_eq!(code.matches("cache.lookup(router_state").count(), 1);
    assert_eq!(code.matches("cache.store(router_state").count(), 1);
}

#[test]
fn the_dispatch_scan_sees_what_it_forbids() {
    let old_shape = "let cache_key = build_cache_key(&model.tenant_id, &model.id, identity, request);\n\
                     if let Some(hit) = router_state.get_cached_response(&cache_key) {}\n\
                     router_state.cache_response(&cache_key, body, status, gate.ttl);\n";
    assert_eq!(dispatch_direct_uses(old_shape).len(), 3);
    assert!(dispatch_direct_uses("// router_state.get_cached_response( in a comment\n").is_empty());
    assert!(dispatch_direct_uses("cache.lookup(router_state, model, request)\n").is_empty());
}

#[test]
fn every_store_plan_is_committed_and_nothing_is_stored_without_one() {
    let src = fs::read_to_string("src/routes/llm.rs").expect("read llm.rs");
    let code = code_lines(&src).join("\n");
    let plans = code.matches(".plan_store(").count();
    let commits = code.matches("plan.commit(").count();
    // Five surfaces store: chat, streamed chat, /responses, streamed
    // /responses, /completions.
    assert_eq!(plans, 5, "one store plan per LLM surface");
    assert_eq!(
        commits, plans,
        "every plan has its commit, and a commit has a plan"
    );
    let lookups = code.matches("cache_session.lookup(").count();
    assert_eq!(
        lookups, 3,
        "one lookup per request handler (chat, /responses, /completions)"
    );
}

#[test]
fn a_chat_request_is_never_looked_up_or_planned_without_the_grounded_check() {
    let src = fs::read_to_string("src/routes/llm.rs").expect("read llm.rs");
    let chat_uses = code_lines(&src)
        .iter()
        .filter(|l| l.contains("CacheBody::Chat("))
        .count();
    assert_eq!(
        chat_uses, 3,
        "the chat lookup, the chat store plan and the streamed chat store plan"
    );
    let found = ungrounded_chat_uses(&src);
    assert!(
        found.is_empty(),
        "a chat request reaches the response cache with no `kb_grounded` check \
         within {GROUNDED_WINDOW} lines:\n{}",
        found.join("\n")
    );
}

#[test]
fn the_scans_see_what_they_forbid() {
    let direct = "fn f() {\n    if let Some(ref cache) = state.semantic_cache {\n        \
                  cache.store_background(a, b);\n    }\n}\n";
    assert_eq!(direct_uses(direct).len(), 2, "{:?}", direct_uses(direct));
    assert!(direct_uses("// state.semantic_cache in a comment\n").is_empty());
    assert!(direct_uses("let log = llm_helpers::build_semantic_cache_hit_log(a);\n").is_empty());

    let far = format!(
        "let grounded = kb_grounded;\n{}body: CacheBody::Chat(&request),\n",
        "let x = 1;\n".repeat(GROUNDED_WINDOW + 1)
    );
    assert_eq!(ungrounded_chat_uses(&far).len(), 1);
    let near = "if !kb_grounded {\n    body: CacheBody::Chat(&request),\n}\n";
    assert!(ungrounded_chat_uses(near).is_empty());
}
