// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The request path reaches the knowledge base only through
//! the context-enricher hook (`services::context_enrichment`).
//!
//! Before the hook, `routes/llm.rs` read the group's knowledge-base
//! settings (twice: once to keep a grounded answer out of the response
//! cache, once to retrieve), resolved the asker's document permissions,
//! called the retrieval service and formatted its result, and
//! `llm_preflight.rs` read the same settings to reserve room for the
//! context. The knowledge base is Enterprise and those files are Community.
//! This guard keeps a direct call from growing back:
//!
//! * outside the feature's own files, no source file names the feature's
//!   retrieval functions or types, or reads a group's knowledge-base
//!   settings;
//! * the feature's module, its service handle (`AppState.knowledge_base`)
//!   and its settings type are named only by the files listed here, with the
//!   reason each is on the list;
//! * the chat route opens one session, asks it whether the group is
//!   grounded, enriches through `enrich_chat` and writes the audit columns
//!   on both of its branches; the admission asks the hook for the bytes to
//!   reserve; nothing else asks the hook;
//! * a state built without an extension holds the default, and the builder
//!   installs the implementation.
//!
//! What the knowledge base then does to a request is pinned by behaviour, in
//! `tests/integration/context_enricher_surfaces.rs`.

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

/// The feature's own files (`enterprise_modules.knowledge_base` in the
/// editions manifest). The two portal route files are the end user's
/// knowledge-base pages (collections, uploads and their status, identity
/// links): not the request path, and already the feature's.
const FEATURE_FILES: [&str; 5] = [
    "enterprise/src/knowledge_base.rs",
    "enterprise/src/bm25.rs",
    "enterprise/src/qdrant_auth.rs",
    "enterprise/src/routes/portal_kb.rs",
    "enterprise/src/routes/portal_kb_identities.rs",
];

/// What retrieves, resolves an asker's permissions, words the context,
/// bounds it, or reads a group's settings. No file outside the feature
/// names any of these.
const REQUEST_PATH: [&str; 11] = [
    "load_user_acl_principals",
    "format_context(",
    "KbRetrievalResult",
    "KbRetrievalError",
    "KbChunk",
    "KbAclError",
    "rag_context_bound_bytes",
    "knowledge_base::context_bound_bytes",
    "build_kb_search_filter",
    ".kb_config",
    "kb_service",
];

/// Ways of naming the feature's module, its service or its settings type.
const HANDLE: [&str; 6] = [
    "knowledge_base::",
    ".knowledge_base",
    "KnowledgeBaseService",
    "KnowledgeBaseEnricher",
    "KbGroupConfig",
    "KbConfig",
];

/// The files outside the feature that name its module, and why.
/// The knowledge base is an extension (`KnowledgeBase`, in
/// the feature's own module): `AppState`, the builder, `app.rs`, the
/// invalidation handlers and the alert constructors name nothing of it
/// (nor does the group cache). What is left is the
/// composition root.
const HANDLE_HOLDERS: [(&str, &str); 1] = [(
    "enterprise/src/main.rs",
    "composes the extension: `.extension(KnowledgeBase::new())`",
)];

/// A Community file whose *unit tests* still install the feature: the
/// invalidation recorder, which records what the full composition evicts.
/// Its production code is held to the rule like any other file. The tests
/// move with the features.
const TEST_HOLDERS: [(&str, &str); 1] = [(
    "enterprise/src/invalidation_tests.rs",
    "the invalidation recorder of the full composition (the Enterprise workspace's; \
     no Community file is left)",
)];

/// The file without its unit tests.
fn production(src: &str) -> &str {
    match src.find("#[cfg(test)]\nmod tests") {
        Some(at) => &src[..at],
        None => src,
    }
}

/// Knowledge-base code outside the feature's files that is not the request
/// path either, as `(file, what must be in it, why it stays)`. Each names
/// the feature by a string, not by its module, so the scans above do not see
/// it; listed so that the list of what was left where it was is in one
/// place and stays true. It is empty: the invalidation handler,
/// the alert constructor and the route mounts are in the feature's module, and
/// the installation's knowledge-base
/// settings are not in `AppConfig` but in the Enterprise workspace, which defines
/// and reads their variables ([`SETTINGS_THAT_LEFT_THE_CONFIGURATION`]).
const NOT_THE_REQUEST_PATH: [(&str, &str, &str); 0] = [];

/// Fields the gateway's configuration held for the knowledge base and the
/// response cache. None may come back.
const SETTINGS_THAT_LEFT_THE_CONFIGURATION: [&str; 5] = [
    "kb_enabled",
    "kb_default_",
    "semantic_cache_",
    "qdrant_",
    "similarity_threshold",
];

/// What belongs in the feature's module, as `(what the module holds
/// now, where it stood)`.
const MOVED_INTO_THE_FEATURE: [(&str, &str); 4] = [
    (
        "feature: Some(\"knowledge_base\"),",
        "src/services/cache/invalidation.rs",
    ),
    (
        "pub(crate) fn kb_vector_dim_mismatch(",
        "src/services/system_alerts.rs",
    ),
    (
        ".nest(\"/v1/portal/knowledgebases\", routes::portal_kb::router())",
        "enterprise/src/portal.rs",
    ),
    ("\"/v1/portal/kb-identities\",", "enterprise/src/portal.rs"),
];

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

fn read(path: &str) -> String {
    fs::read_to_string(path).unwrap_or_else(|e| panic!("read {path}: {e}"))
}

fn source_files() -> Vec<(String, String)> {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    assert!(files.len() > 100, "the scan found the source tree");
    files
        .into_iter()
        .map(|path| {
            let name = path.to_string_lossy().replace('\\', "/");
            let src = read(&name);
            (name, src)
        })
        .collect()
}

#[test]
fn nothing_outside_the_feature_names_its_request_path_or_its_service() {
    let mut seen_holders = Vec::new();
    for (name, src) in source_files() {
        if FEATURE_FILES.contains(&name.as_str()) {
            continue;
        }
        let found = uses(&src, &REQUEST_PATH);
        assert!(
            found.is_empty(),
            "{name} reaches the knowledge base without the hook \
             (services::context_enrichment):\n{}",
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
                ["use brutor_gateway_enterprise::knowledge_base::KnowledgeBase;"],
                "{name} names more of the knowledge base than the extension it composes"
            );
            seen_holders.push(name);
            continue;
        }
        if TEST_HOLDERS.iter().any(|(holder, _)| *holder == name) {
            let found = uses(production(&src), &HANDLE);
            assert!(
                found.is_empty(),
                "{name}: production code names the knowledge base (only its unit tests \
                 may, until they move):\n{}",
                found.join("\n")
            );
            assert!(
                !uses(&src, &HANDLE).is_empty(),
                "{name} is listed as a test holder and its tests no longer name the \
                 feature: update the list"
            );
            continue;
        }
        let found = uses(&src, &HANDLE);
        assert!(
            found.is_empty(),
            "{name} names the knowledge base; the request path asks \
             state.context_enricher, and only the listed files name the module:\n{}",
            found.join("\n")
        );
    }
    assert_eq!(
        seen_holders.len(),
        HANDLE_HOLDERS.iter().filter(|(h, _)| here(h)).count(),
        "a listed holder no longer exists: {seen_holders:?}"
    );
}

#[test]
fn the_feature_files_exist_and_hold_the_implementation() {
    for path in FEATURE_FILES.into_iter().filter(|p| here(p)) {
        assert!(Path::new(path).exists(), "{path}");
    }
    if here(FEATURE_FILES[0]) {
        let feature = read(FEATURE_FILES[0]);
        assert!(feature.contains("pub struct KnowledgeBaseEnricher"));
        assert!(feature.contains("impl ContextEnricher for KnowledgeBaseEnricher"));
    }
    // The hook module names nothing of the feature (it is in the general
    // scan too; this says it of the module by name).
    let hook = read("src/services/context_enrichment.rs");
    assert!(hook.contains("pub trait ContextEnricher"));
    assert!(uses(&hook, &REQUEST_PATH).is_empty());
    assert!(uses(&hook, &HANDLE).is_empty());
}

#[test]
fn what_was_left_where_it_was_is_still_there() {
    for (path, needle, why) in NOT_THE_REQUEST_PATH {
        assert_eq!(
            count(&read(path), needle),
            1,
            "{path} no longer holds `{needle}` ({why}): update the list"
        );
    }
    let configuration = read("src/config/mod.rs");
    for field in SETTINGS_THAT_LEFT_THE_CONFIGURATION {
        assert_eq!(
            count(&configuration, field),
            0,
            "src/config/mod.rs holds `{field}` again: an Enterprise setting is the Enterprise \
             workspace's to define and read (the module of the extension that reads it)"
        );
    }
    for (needle, stood_in) in MOVED_INTO_THE_FEATURE {
        if here(FEATURE_FILES[0]) {
            assert_eq!(
                count(&read(FEATURE_FILES[0]), needle),
                1,
                "{} no longer holds `{needle}`",
                FEATURE_FILES[0]
            );
        }
        if here(stood_in) {
            assert_eq!(
                count(&read(stood_in), needle),
                0,
                "{stood_in} holds `{needle}` again"
            );
        }
    }
}

#[test]
fn a_state_starts_with_the_default_and_the_extension_installs_the_feature() {
    let state = read("src/services/mod.rs");
    assert_eq!(
        count(
            &state,
            "context_enricher: Arc::new(context_enrichment::NoContextEnrichment)"
        ),
        1,
        "AppState::new holds the Community default"
    );
    assert_eq!(
        count(&state, "KnowledgeBaseEnricher"),
        0,
        "and never the implementation"
    );
    assert_eq!(count(&state, "knowledge_base:"), 0, "nor a service handle");
    // The builder installs nothing of it: the extension does, in its
    // `install_state`, after checking that the Portal is composed.
    let builder = read("src/builder.rs");
    assert!(uses(&builder, &HANDLE).is_empty(), "the builder names it");
    // A Community tree alone: the feature's module is not here to read.
    if !here(FEATURE_FILES[0]) {
        return;
    }
    let feature = read(FEATURE_FILES[0]);
    assert_eq!(count(&feature, "impl Extension for KnowledgeBase {"), 1);
    assert_eq!(
        count(
            &feature,
            "KnowledgeBaseEnricher::install(state, self.prepared.get().cloned())"
        ),
        1
    );
    assert_eq!(count(&feature, "pub const REQUIRES: &str = \"portal\";"), 1);
    assert_eq!(count(&feature, ".contains(&REQUIRES)"), 1);
    assert_eq!(
        count(
            &read("enterprise/src/main.rs"),
            ".extension(KnowledgeBase::new())"
        ),
        1
    );
}

/// Where the request path asks the hook, and how often. A new surface that
/// should ground its requests, or a second read of the group's grounding,
/// shows up here as a count that no longer matches.
#[test]
fn the_request_path_asks_the_hook_where_it_did_and_nowhere_else() {
    // Who touches the hook at all: the chat route, the admission, and the
    // implementation's `install`.
    let mut askers: Vec<String> = source_files()
        .into_iter()
        .filter(|(_, src)| count(src, ".context_enricher") > 0)
        .map(|(name, _)| name)
        .collect();
    askers.sort();
    let expected: Vec<&str> = [
        "enterprise/src/knowledge_base.rs",
        "src/routes/llm.rs",
        "src/services/llm_preflight.rs",
    ]
    .into_iter()
    .filter(|f| here(f))
    .collect();
    assert_eq!(askers, expected);

    // The chat handler opens one session for both of its branches, takes
    // "grounded" from it, and enriches once, through the Community helper.
    // It also asks the installation's enricher, once, whether retrievals
    // call the gateway's own routes: the headers those calls forward then
    // carry the request's run.
    let chat = read("src/routes/llm.rs");
    assert_eq!(count(&chat, ".context_enricher"), 2);
    assert_eq!(count(&chat, "state.context_enricher.makes_self_calls()"), 1);
    assert_eq!(
        count(&chat, "let kb_grounded = enrichment.grounded();"),
        1,
        "the response-cache rule  follows the enricher's answer"
    );
    assert_eq!(count(&chat, "let kb_grounded"), 1, "and nothing else");
    assert_eq!(count(&chat, "context_enrichment::enrich_chat("), 1);
    assert_eq!(
        count(&chat, ".retrieve("),
        0,
        "the route does not retrieve; `enrich_chat` does, and bounds what it injects"
    );
    // The audit columns are written from the enrichment's record on the
    // streamed and the non-streamed branch.
    assert_eq!(
        count(
            &chat,
            "llm_helpers::populate_rag_log_fields(&mut log, &rag_result"
        ),
        2
    );
    assert_eq!(
        rag_column_writes(&read("src/routes/llm_helpers.rs")).len(),
        6,
        "the helper writes the six columns"
    );
    assert!(
        rag_columns_written_outside_the_helper().is_empty(),
        "a rag_* audit column is written outside populate_rag_log_fields:\n{}",
        rag_columns_written_outside_the_helper().join("\n")
    );

    // The admission reserves what the hook says, once.
    let preflight = read("src/services/llm_preflight.rs");
    assert_eq!(
        count(
            &preflight,
            "state.context_enricher.reserve_bytes(&effective)"
        ),
        1
    );

    // The helper is the one place that changes a request, and it cuts the
    // context to the session's bound before it does.
    let hook = read("src/services/context_enrichment.rs");
    assert_eq!(
        count(
            &hook,
            "let text = cut_to(context.text, session.context_bound_bytes());"
        ),
        1
    );
    assert_eq!(count(&hook, "request.messages.insert("), 1);
}

/// Assignments of a non-null value to a `rag_*` audit column by a route,
/// anywhere but in the one helper that writes them from an enrichment
/// record.
fn rag_columns_written_outside_the_helper() -> Vec<String> {
    let mut found = Vec::new();
    let mut routes = 0;
    for (name, src) in source_files() {
        if !name.starts_with("src/routes/") || name == "src/routes/llm_helpers.rs" {
            continue;
        }
        routes += 1;
        found.extend(
            rag_column_writes(&src)
                .into_iter()
                .map(|line| format!("{name}: {line}")),
        );
    }
    assert!(routes > 20, "the scan found the routes");
    found
}

fn rag_column_writes(src: &str) -> Vec<String> {
    code_lines(src)
        .filter(|(_, l)| {
            let l = l.trim();
            (l.starts_with("log.rag_") || l.starts_with("rag_"))
                && (l.contains("= Some(") || l.contains(": Some("))
        })
        .map(|(n, l)| format!("{n}: {}", l.trim()))
        .collect()
}

#[test]
fn the_scans_see_what_they_forbid() {
    // The shape the chat route had before the hook: the grounding read...
    let old_grounded = "let kb_grounded = match target_group.as_ref() {\n    \
                        Some(group) => state.group_cache.get_effective_group(&tenant_id, &group.id)\n        \
                        .await\n        .map(|e| {\n            e.kb_config\n                .as_ref()\n                \
                        .map(|kb| kb.enabled && !kb.collection_ids.is_empty())\n                \
                        .unwrap_or(false)\n        })\n        .unwrap_or(false),\n    None => false,\n};\n";
    assert_eq!(
        uses(old_grounded, &REQUEST_PATH).len(),
        1,
        "{:?}",
        uses(old_grounded, &REQUEST_PATH)
    );
    // ...and the retrieval block.
    let old_route = "let mut rag_result: Option<crate::services::knowledge_base::KbRetrievalResult> = None;\n\
                     if let Some(ref kb_service) = state.knowledge_base {\n    \
                     if let Some(ref kb_config) = effective.kb_config {\n        \
                     let acl = kb_service.load_user_acl_principals(&tenant_id, uid).await.unwrap_or_default();\n        \
                     let result = kb_service.retrieve(&tenant_id, kb_config, &query, &h, acl.as_deref()).await;\n        \
                     let mut context_text = crate::services::knowledge_base::KnowledgeBaseService::format_context(\n            \
                     &result, kb_config.system_prompt_template.as_deref());\n        \
                     let bound = llm_preflight::rag_context_bound_bytes(kb_config);\n    }\n}\n";
    // Seven of its eight lines name something of the feature's.
    let lines: std::collections::BTreeSet<String> = uses(old_route, &REQUEST_PATH)
        .into_iter()
        .map(|hit| hit.split(':').next().unwrap_or_default().to_string())
        .collect();
    assert_eq!(lines.len(), 7, "{lines:?}");
    assert_eq!(
        uses(old_route, &HANDLE).len(),
        4,
        "{:?}",
        uses(old_route, &HANDLE)
    );
    // The shape the admission had.
    let old_preflight = "pub fn rag_context_bound_bytes(kb: &crate::services::knowledge_base::KbGroupConfig) -> usize {\n\
                         if let Some(kb) = effective\n    .kb_config\n    .as_ref()\n";
    assert_eq!(uses(old_preflight, &REQUEST_PATH).len(), 2);
    assert_eq!(uses(old_preflight, &HANDLE).len(), 2);
    // The service handle read by a route, in any spelling.
    for direct in [
        "if let Some(kb) = state.knowledge_base.as_ref() {\n",
        "let kb = &state\n    .knowledge_base;\n",
        "use crate::services::knowledge_base::KnowledgeBaseService;\n",
        "let bound = crate::services::knowledge_base::context_bound_bytes(kb);\n",
    ] {
        assert!(!uses(direct, &HANDLE).is_empty(), "{direct}");
    }
    // What is allowed: the hook, its types, comments, the audit helper.
    for allowed in [
        "let enrichment = state.context_enricher.session(&state, &tenant_id, group).await;\n",
        "let kb_grounded = enrichment.grounded();\n",
        "use crate::services::context_enrichment::EnrichmentRecord;\n",
        "bytes += state.context_enricher.reserve_bytes(&effective);\n",
        "// state.knowledge_base and kb_service.retrieve( in a comment\n",
        "pub mod knowledge_base;\n",
        "name: \"knowledge_base\",\n",
    ] {
        assert!(uses(allowed, &REQUEST_PATH).is_empty(), "{allowed}");
        assert!(uses(allowed, &HANDLE).is_empty(), "{allowed}");
    }

    // An audit column written by hand.
    assert_eq!(
        rag_column_writes("log.rag_collection_ids = Some(ids.join(\",\"));\n").len(),
        1
    );
    assert_eq!(rag_column_writes("    rag_enabled: Some(true),\n").len(), 1);
    assert!(
        rag_column_writes("    rag_enabled: None,\n    rag_collection_ids: None,\n").is_empty()
    );
    assert!(
        rag_column_writes("llm_helpers::populate_rag_log_fields(&mut log, &rag_result);\n")
            .is_empty()
    );
}
