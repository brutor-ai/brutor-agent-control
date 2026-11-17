// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: the group cache loads no Enterprise configuration itself, and a
//! loader contribution cannot fail open.
//!
//! Why it matters. The effective group is what access control, quotas and
//! governance read (Critical class). Two Enterprise features keep settings
//! per group in five tables of their own; the group cache used to read them
//! in its load. They are now loaded by contributions the features register
//! ([`GroupLoader`]), inside the same load. Three things would undo that
//! quietly:
//!
//! * the group cache reading an Enterprise table again: a Community gateway
//!   then depends on tables its edition does not own;
//! * the load swallowing a loader's error: a group is built, and cached for
//!   the TTL, without the extras of a feature that is composed in, which to
//!   the feature is "switched off for this group";
//! * a loader swallowing its own error, with the same result.
//!
//! Behaviour is pinned by `tests/integration/group_extras.rs`; this pins the
//! shapes. Each scan is proven against the shape it forbids
//! ([`the_scans_catch_the_shapes_they_forbid`]).

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

const GROUP_CACHE: &str = "src/services/cache/group_cache.rs";

/// What the group cache may not name: the five Enterprise-only tables of
/// the group load, the two settings types, and the feature modules.
const ENTERPRISE_NAMES: &[&str] = &[
    "semantic_cache_configs",
    "resource_group_cache_configs",
    "semantic_cache_tool_rules",
    "kb_collections",
    "resource_group_kb_collections",
    "CacheGroupConfig",
    "KbGroupConfig",
    "semantic_cache::",
    "knowledge_base::",
    "cache_config",
    "kb_config",
];

/// The modules that register a loader, with the loader each registers.
/// Enterprise feature modules only.
const LOADERS: &[(&str, &str)] = &[
    (
        "enterprise/src/semantic_response_cache.rs",
        "CacheGroupConfigLoader",
    ),
    ("enterprise/src/knowledge_base.rs", "KbGroupConfigLoader"),
];

/// What turns a failed read into a value, inside a loader. (`.unwrap_or(`
/// on its own is not listed: the knowledge-base loader folds the rows it
/// read with `.max().unwrap_or(..)`. A read that is defaulted instead of
/// propagated is caught by the other rule: every `.await` is `.await?`.)
const SWALLOWS: &[&str] = &[
    ".ok()",
    ".unwrap_or_default()",
    "Err(_) =>",
    "if let Ok(",
    "let Ok(",
    ".is_err()",
    ".is_ok()",
];

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) {
    for entry in fs::read_dir(dir).unwrap() {
        let path = entry.unwrap().path();
        if path.is_dir() {
            rust_files(&path, out);
        } else if path.extension().is_some_and(|x| x == "rs") {
            out.push(path);
        }
    }
}

fn read(path: &str) -> String {
    fs::read_to_string(path).unwrap_or_else(|e| panic!("{path}: {e}"))
}

/// Lines of code: comment lines dropped, numbered from 1.
fn code_lines(text: &str) -> impl Iterator<Item = (usize, &str)> {
    text.lines()
        .enumerate()
        .filter(|(_, l)| !l.trim_start().starts_with("//"))
        .map(|(i, l)| (i + 1, l))
}

fn production(text: &str) -> &str {
    match text.find("#[cfg(test)]\nmod tests") {
        Some(at) => &text[..at],
        None => text,
    }
}

/// The text from `open_at` (an opening brace) to the brace that closes it.
fn braces(text: &str, open_at: usize) -> &str {
    let mut depth = 0usize;
    for (i, c) in text[open_at..].char_indices() {
        match c {
            '{' => depth += 1,
            '}' => {
                depth -= 1;
                if depth == 0 {
                    return &text[open_at..=open_at + i];
                }
            }
            _ => {}
        }
    }
    panic!("unbalanced braces");
}

fn enterprise_names_in(text: &str) -> Vec<String> {
    code_lines(text)
        .filter(|(_, l)| ENTERPRISE_NAMES.iter().any(|n| l.contains(n)))
        .map(|(i, l)| format!("{i}: {}", l.trim()))
        .collect()
}

/// What is wrong with how the load runs the loaders; empty when every
/// loader runs inside `load_effective_group` and its error is the load's.
fn load_faults(group_cache: &str) -> Vec<String> {
    let mut faults = Vec::new();
    let production = production(group_cache);
    let Some(at) = production.find("async fn load_effective_group(") else {
        return vec!["no load_effective_group".to_string()];
    };
    let open = at + production[at..].find(") -> ").expect("return type");
    let open = open + production[open..].find('{').expect("body");
    let body = braces(production, open);
    let flat: String = code_lines(body)
        .map(|(_, l)| l.trim())
        .collect::<Vec<_>>()
        .join(" ");
    let run = "for loader in self.registered_loaders() { loader.load(&self.db, &load, &mut extras).await?; }";
    if flat.matches(run).count() != 1 {
        faults.push(
            "load_effective_group does not run every registered loader with `?`, once".to_string(),
        );
    }
    if flat.matches("loader.load(").count() != 1 {
        faults.push("a loader is run in more than one place".to_string());
    }
    // The extras the loaders filled are the ones the group carries.
    if !flat.contains("let mut extras = GroupExtras::default();") {
        faults.push("the load does not start from empty extras".to_string());
    }
    let built = flat.rfind("Ok(Some(EffectiveGroupData {");
    match (flat.find(run), built) {
        (Some(ran), Some(built)) if ran < built => {
            if !flat[built..].contains(" extras, ") {
                faults.push("the group is built without the loaders' extras".to_string());
            }
        }
        _ => faults.push("the group is built before the loaders ran".to_string()),
    }
    // Nothing else in production code runs a loader.
    if production.matches(".load(&self.db, &load").count() != 1 {
        faults.push("loaders are run outside the load".to_string());
    }
    faults
}

/// The body of `async fn load` of `impl GroupLoader for <name>` in `text`.
fn loader_body<'a>(text: &'a str, name: &str) -> &'a str {
    let at = text
        .find(&format!("impl GroupLoader for {name} {{"))
        .unwrap_or_else(|| panic!("no impl GroupLoader for {name}"));
    let whole = braces(text, at + text[at..].find('{').expect("impl body"));
    let load = whole.find("async fn load(").expect("async fn load");
    let open = load + whole[load..].find(") -> ").expect("return type");
    let open = open + whole[open..].find('{').expect("load body");
    braces(whole, open)
}

fn swallows_in(body: &str) -> Vec<String> {
    code_lines(body)
        .filter(|(_, l)| SWALLOWS.iter().any(|s| l.contains(s)))
        .map(|(i, l)| format!("{i}: {}", l.trim()))
        .collect()
}

#[test]
fn the_group_cache_names_no_enterprise_table_or_type() {
    let found = enterprise_names_in(&read(GROUP_CACHE));
    assert!(
        found.is_empty(),
        "{GROUP_CACHE} names Enterprise configuration; it is loaded by a GroupLoader the \
         feature registers:\n{}",
        found.join("\n")
    );
    // The scan reads something: the loaders are full of these names.
    for (file, _) in LOADERS.iter().filter(|(f, _)| here(f)) {
        assert!(
            enterprise_names_in(&read(file)).len() >= 5,
            "{file}: the scan's names are stale"
        );
    }
}

#[test]
fn the_load_runs_every_loader_and_fails_with_it() {
    let faults = load_faults(&read(GROUP_CACHE));
    assert!(faults.is_empty(), "{faults:#?}");
}

#[test]
fn loaders_are_registered_by_the_listed_feature_modules_and_propagate_their_errors() {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    assert!(files.len() > 100, "the scan found {} files", files.len());
    let mut registering = Vec::new();
    let mut implementing = Vec::new();
    for file in &files {
        let rel = file.to_string_lossy().replace('\\', "/");
        let text = fs::read_to_string(file).unwrap();
        let code = production(&text);
        if code_lines(code).any(|(_, l)| l.contains(".register_loader(")) {
            registering.push(rel.clone());
        }
        if code_lines(code).any(|(_, l)| l.contains("impl GroupLoader for ")) {
            implementing.push(rel);
        }
    }
    registering.sort();
    implementing.sort();
    let mut listed: Vec<String> = LOADERS
        .iter()
        .filter(|(f, _)| here(f))
        .map(|(f, _)| f.to_string())
        .collect();
    listed.sort();
    assert_eq!(
        registering, listed,
        "a loader is registered outside the listed feature modules (or a listed one no longer \
         registers)"
    );
    assert_eq!(implementing, listed, "a loader is implemented elsewhere");

    for (file, loader) in LOADERS.iter().filter(|(f, _)| here(f)) {
        let text = read(file);
        let body = loader_body(&text, loader);
        let swallowed = swallows_in(body);
        assert!(
            swallowed.is_empty(),
            "{file}: {loader} turns a failed read into a value; a loader's error must fail \
             the group load:\n{}",
            swallowed.join("\n")
        );
        assert!(
            body.matches(".await?;").count() >= 1,
            "{file}: {loader} reads nothing with `?`"
        );
        // Every query's result is propagated: as many `?` as awaits.
        assert_eq!(
            body.matches(".await").count(),
            body.matches(".await?").count(),
            "{file}: {loader} awaits a read without `?`"
        );
        assert_eq!(
            text.matches(&format!(".register_loader(Arc::new({loader}))?;"))
                .count(),
            text.matches(".register_loader(").count(),
            "{file}: a registration that is not `{loader}` with `?`"
        );
    }
}

#[test]
fn the_scans_catch_the_shapes_they_forbid() {
    // 1. The group cache reading a feature's table again.
    let before = "            let kb_rows: Vec<(String,)> = sqlx::query_as(\n                r#\"SELECT kc.id FROM kb_collections kc\n                   JOIN resource_group_kb_collections rgkc ON rgkc.kb_collection_id = kc.id\"#,\n            // kb_collections in a comment is prose\n";
    assert_eq!(enterprise_names_in(before).len(), 2);
    assert_eq!(
        enterprise_names_in(
            "    pub kb_config: Option<crate::services::knowledge_base::KbGroupConfig>,"
        )
        .len(),
        1
    );
    assert!(enterprise_names_in("    pub extras: GroupExtras,").is_empty());

    // 2. The load, as it is, and with each way of losing a loader's error.
    let good = read(GROUP_CACHE);
    assert!(load_faults(&good).is_empty());
    let run = "                loader.load(&self.db, &load, &mut extras).await?;";
    assert_eq!(
        good.matches(run).count(),
        1,
        "the marker the plants replace"
    );
    for (what, planted) in [
        (
            "the error dropped",
            good.replace(run, "                let _ = loader.load(&self.db, &load, &mut extras).await;"),
        ),
        (
            "the error logged and the group built anyway",
            good.replace(
                run,
                "                if let Err(e) = loader.load(&self.db, &load, &mut extras).await {\n                    tracing::warn!(\"{e}\");\n                }",
            ),
        ),
        ("the loaders not run", good.replace(run, "                let _ = &loader;")),
        (
            "the extras thrown away",
            good.replace("            ancestor_ids,\n            extras,\n        }))", "            ancestor_ids,\n            extras: GroupExtras::default(),\n        }))"),
        ),
    ] {
        assert_ne!(planted, good, "{what}: the planted shape did not apply");
        assert!(!load_faults(&planted).is_empty(), "{what}: not caught");
    }

    // 3. A loader that answers "no settings" when its read fails.
    let fail_open = "impl GroupLoader for X {\n    async fn load(\n        &self,\n        db: &sqlx::PgPool,\n    ) -> Result<(), sqlx::Error> {\n        let rows: Vec<(String,)> = sqlx::query_as(\"SELECT id FROM t\")\n            .fetch_all(db)\n            .await\n            .unwrap_or_default();\n        Ok(())\n    }\n}\n";
    let body = loader_body(fail_open, "X");
    assert_eq!(swallows_in(body).len(), 1);
    assert_ne!(
        body.matches(".await").count(),
        body.matches(".await?").count()
    );
    let closed = fail_open.replace(
        "            .await\n            .unwrap_or_default();",
        "            .await?;",
    );
    let body = loader_body(&closed, "X");
    assert!(swallows_in(body).is_empty());
    assert_eq!(
        body.matches(".await").count(),
        body.matches(".await?").count()
    );
}
