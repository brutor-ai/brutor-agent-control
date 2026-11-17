// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: a model is loaded for a call, an offer or a pick only through the
//! one availability predicate.
//!
//! The bug class: "may this model be called" was the literal `enabled =
//! true`, written out in every query that loads a model. A model past its
//! retire date and a model whose provider record is switched off were
//! enabled rows, so every one of those queries returned them: a direct
//! call reached the provider, a routing group picked the member, a failing
//! call fell back to it, and the model lists offered it.
//!
//! The predicate is the macro `model_available_sql!` (`src/lib.rs`). This
//! guard pins, for every production file of both editions that reads the
//! `llm_model` table (`FROM llm_model` / `JOIN llm_model`):
//!
//! * how many such reads it has and how many of them use the predicate;
//! * for each file with a read that does not, why that read is no call, no
//!   offer and no pick.
//!
//! A new read fails until it is listed, and a listed read that drops the
//! predicate fails. No query filters a model on `enabled` by hand.
//!
//! Behaviour is pinned by `tests/integration/model_availability.rs`.
#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::{Path, PathBuf};

const PREDICATE: &str = "model_available_sql!(";

/// `(file, reads of llm_model, of which use the predicate, why the rest do
/// not need it)`.
const READS: [(&str, usize, usize, &str); 19] = [
    (
        "src/services/cache/model_cache.rs",
        3,
        2,
        "the third maps an id or a name to a model's cache keys so that a changed row can be \
         evicted, whatever state the row is in",
    ),
    ("src/services/llm_router.rs", 4, 4, ""),
    ("src/services/llm_preflight.rs", 2, 2, ""),
    ("src/services/semantic_policy.rs", 3, 3, ""),
    (
        "src/routes/llm.rs",
        5,
        3,
        "the other two read the row an operator's probe names (the catalog probe and its \
         recorded result), with the provider address of that row: a probe is how an operator \
         examines a model that is not serving",
    ),
    (
        "src/services/usage.rs",
        3,
        0,
        "the tenant-wide limits and policy of a model the call has already resolved, and the \
         list of models that carry limits for the counter reconcile: no model is chosen",
    ),
    (
        "src/services/cache/group_cache.rs",
        2,
        0,
        "which models a resource group may reach (access): a model that is not available is \
         refused where it is resolved, whoever may reach it",
    ),
    (
        "src/services/video_pipeline.rs",
        1,
        0,
        "the poller reads the model of a video job a resolved call has already submitted to \
         the provider, to fetch its result",
    ),
    (
        "src/builder.rs",
        1,
        0,
        "the start-up check decrypts one stored key to prove the encryption key matches; it \
         calls no model",
    ),
    (
        "src/services/system_alerts_resolver.rs",
        1,
        0,
        "resolves an alert about a model row: it reads the row's state, it calls nothing",
    ),
    (
        "src/services/run_ledger.rs",
        1,
        0,
        "the run ledger names the catalog entry of a model a recorded step used",
    ),
    (
        "src/services/drift/cause.rs",
        1,
        0,
        "the drift analysis reads what a recorded call's model was",
    ),
    (
        "src/services/replay/executor.rs",
        1,
        0,
        "replay asks whether a group is bound to a model by name (access); the replayed call \
         resolves its model like any other",
    ),
    ("enterprise/src/prompt_cache.rs", 1, 1, ""),
    ("enterprise/src/knowledge_base.rs", 1, 1, ""),
    ("enterprise/src/semantic_cache.rs", 1, 1, ""),
    (
        "enterprise/src/batch/db.rs",
        2,
        1,
        "a batch job's model row is read whatever its state, so that a disabled model is told \
         apart from a deleted one; the second read asks the predicate about that row and hands \
         a model that is not available on as a disabled one",
    ),
    (
        "enterprise/src/routes/portal_models.rs",
        2,
        1,
        "the other read is the detail of one model the portal user names; the chat that \
         follows resolves the model like any other call",
    ),
    ("src/lib.rs", 0, 0, "defines the predicate"),
];

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for e in entries.flatten() {
        let p = e.path();
        if p.is_dir() {
            rust_files(&p, out);
        } else if p.extension().and_then(|s| s.to_str()) == Some("rs") {
            out.push(p);
        }
    }
}

/// Production code only: everything before the file's test module, without
/// comment lines.
fn production(src: &str) -> String {
    let mut out = String::new();
    let lines: Vec<&str> = src.lines().collect();
    for (i, line) in lines.iter().enumerate() {
        if line.starts_with("#[cfg(test)]")
            && lines
                .get(i + 1)
                .is_some_and(|next| next.trim_start().starts_with("mod "))
        {
            break;
        }
        if line.trim_start().starts_with("//") {
            continue;
        }
        out.push_str(line);
        out.push('\n');
    }
    out
}

/// How many times `code` reads the `llm_model` table: `FROM llm_model` or
/// `JOIN llm_model`, whatever whitespace separates the words, and not a
/// table whose name merely starts with it.
fn reads(code: &str) -> usize {
    let words: Vec<&str> = code.split_whitespace().collect();
    words
        .windows(2)
        .filter(|w| {
            let keyword = w[0].trim_start_matches(['"', '(', '#', 'r']);
            let table = w[1].trim_end_matches(|c: char| !(c.is_ascii_alphanumeric() || c == '_'));
            (keyword.ends_with("FROM") || keyword.ends_with("JOIN")) && table == "llm_model"
        })
        .count()
}

/// Whether `code` filters a model row on `enabled` by hand: an `llm_model`
/// read whose statement says `enabled = true` outside the predicate.
fn filters_by_hand(code: &str) -> bool {
    // A statement: from one `llm_model` read to the next semicolon.
    let mut rest = code;
    while let Some(at) = rest.find("llm_model") {
        let after = &rest[at + "llm_model".len()..];
        let is_table = !after.starts_with(|c: char| c.is_ascii_alphanumeric() || c == '_');
        let end = after.find(';').unwrap_or(after.len());
        let statement = &after[..end];
        if is_table
            && (rest[..at].trim_end().ends_with("FROM") || rest[..at].trim_end().ends_with("JOIN"))
            && !statement.contains(PREDICATE)
            && (statement.contains("m.enabled = true")
                || statement.contains("AND enabled = true")
                || statement.contains("m2.enabled = true"))
        {
            return true;
        }
        rest = after;
    }
    false
}

/// `(path as the list names it, reads, predicate uses, filters by hand)`.
fn found() -> Vec<(String, usize, usize, bool)> {
    let community = roots::tree("community").expect("the Community tree");
    let mut trees = vec![(community, String::new())];
    if let Some(enterprise) = roots::tree("enterprise") {
        trees.push((enterprise, "enterprise/".to_string()));
    }
    let mut out = Vec::new();
    for (root, prefix) in trees {
        let mut files = Vec::new();
        rust_files(&root.join("src"), &mut files);
        files.sort();
        for path in files {
            let rel = path.strip_prefix(&root).unwrap().to_string_lossy();
            let code = production(&fs::read_to_string(&path).unwrap());
            let (n, with) = (reads(&code), code.matches(PREDICATE).count());
            if n > 0 || with > 0 {
                out.push((format!("{prefix}{rel}"), n, with, filters_by_hand(&code)));
            }
        }
    }
    out
}

#[test]
fn every_read_of_a_model_says_whether_it_may_be_called() {
    let enterprise_here = roots::tree("enterprise").is_some();
    let found = found();
    let mut problems = Vec::new();
    for (file, n, with, _) in &found {
        if file == "src/lib.rs" {
            continue;
        }
        match READS.iter().find(|(f, ..)| f == file) {
            Some((_, reads, predicate, _)) if reads == n && predicate == with => {}
            Some((_, reads, predicate, _)) => problems.push(format!(
                "{file}: {n} read(s) of llm_model, {with} with the availability predicate; the \
                 list says {reads} and {predicate}"
            )),
            None => problems.push(format!(
                "{file}: reads llm_model ({n} read(s), {with} with the availability predicate) \
                 and is not in the list"
            )),
        }
    }
    for (file, reads, ..) in READS {
        let listed_tree_here = enterprise_here || !file.starts_with("enterprise/");
        if listed_tree_here && reads > 0 && !found.iter().any(|(f, ..)| f == file) {
            problems.push(format!("{file}: listed, and reads llm_model nowhere"));
        }
    }
    assert!(
        problems.is_empty(),
        "a query that loads a model to call it, to offer it or to pick it uses \
         `model_available_sql!` (enabled, not retired, provider enabled); a read that does not \
         is listed in tests/model_availability_guard.rs with the reason:\n{}",
        problems.join("\n")
    );
}

#[test]
fn no_query_filters_a_model_on_enabled_by_hand() {
    let by_hand: Vec<String> = found()
        .into_iter()
        .filter(|(file, .., by_hand)| {
            // Access, not availability: see the list.
            *by_hand && file != "src/services/cache/group_cache.rs"
        })
        .map(|(file, ..)| file)
        .collect();
    assert!(
        by_hand.is_empty(),
        "these files filter an llm_model row with `enabled = true` written out; use \
         `model_available_sql!`, which also refuses a retired model and a model of a disabled \
         provider:\n{}",
        by_hand.join("\n")
    );
}

#[test]
fn every_read_without_the_predicate_carries_its_reason() {
    for (file, reads, predicate, why) in READS {
        if predicate < reads {
            assert!(
                why.len() > 40,
                "{file}: a read without the predicate has no reason"
            );
        }
    }
}

#[test]
fn the_predicate_says_enabled_not_retired_and_provider_enabled() {
    let sql = brutor_gateway_core::model_available_sql!("m.");
    for part in [
        "m.enabled = true",
        "m.retire_date IS NULL OR m.retire_date > (now() AT TIME ZONE 'UTC')::date",
        "m.mode = 'routing' OR EXISTS (SELECT 1 FROM llm_providers available_provider",
        "available_provider.id = m.provider_id AND available_provider.enabled = true",
    ] {
        assert!(sql.contains(part), "{part}\n in {sql}");
    }
    assert!(brutor_gateway_core::model_available_sql!("").starts_with("enabled = true AND ("));
}

/// The shapes the guard exists for.
#[test]
fn the_scan_counts_reads_and_finds_a_filter_written_by_hand() {
    let by_hand = r##"
        let m = sqlx::query_as(
            "SELECT * FROM llm_model WHERE id = $1 AND tenant_id = $2 AND enabled = true",
        );
        let n = sqlx::query("SELECT 1 FROM llm_model_metrics WHERE id = $1");
        let j = sqlx::query(r#"SELECT m.id FROM tenants t
                 JOIN llm_model m ON m.id = t.judge AND m.enabled = true"#);
    "##;
    assert_eq!(
        reads(by_hand),
        2,
        "two reads; llm_model_metrics is another table"
    );
    assert!(filters_by_hand(by_hand));

    let with_predicate = r#"
        let m = sqlx::query_as(concat!(
            "SELECT * FROM llm_model WHERE id = $1 AND tenant_id = $2 AND ",
            crate::model_available_sql!("")
        ));
        let other = sqlx::query("SELECT 1 FROM api_keys WHERE id = $1 AND enabled = true");
    "#;
    assert_eq!(reads(with_predicate), 1);
    assert!(!filters_by_hand(with_predicate));
}
