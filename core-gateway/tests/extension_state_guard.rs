// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: `AppState.extensions` holds state an extension owns,
//! and nothing built in puts anything there or reads anything
//! from it.
//!
//! Why it matters: the map exists so a second distribution can carry state
//! the built-in gateway does not know by name. Built-in code that inserted
//! into it, or read from it, would make the built-in gateway depend on the
//! map's contents, which is what the named fields are for.
//!
//! Some features ARE extensions,
//! in `enterprise/`. An extension's own
//! module keeps its own state in the map, which is what the map is for:
//! those modules are listed in [`EXTENSION_MODULES`], and a listed module
//! must really be one (`impl Extension for`). Everything else under `src/`
//! is built in and is held to the rule as before.

#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::Path;

/// Is `file` one this run has? A file of the Enterprise workspace is not, in
/// a Community tree alone (`tests/support/roots.rs`); what this guard holds
/// about it is held where the workspace is.
fn here(file: &str) -> bool {
    !file.starts_with("enterprise/") || roots::tree("enterprise").is_some()
}

fn rust_files(dir: &Path, out: &mut Vec<std::path::PathBuf>) {
    for entry in fs::read_dir(dir).expect("read src") {
        let path = entry.expect("entry").path();
        if path.is_dir() {
            rust_files(&path, out);
        } else if path.extension().is_some_and(|e| e == "rs") {
            out.push(path);
        }
    }
}

/// The modules that are extensions and keep state in the extension state
/// (`editions.yaml`, `core_gateway.extensions_built`): all of the
/// Enterprise workspace. Add one only with the step that makes
/// the feature an extension.
const EXTENSION_MODULES: &[&str] = &[
    "enterprise/src/licence.rs",
    "enterprise/src/prompt_cache.rs",
    "enterprise/src/semantic_response_cache.rs",
    "enterprise/src/knowledge_base.rs",
    // The name an instance replies under in the statistics
    // fan-out.
    "enterprise/src/clustering.rs",
];

// (The system administrators' session verifier is an extension that keeps
// nothing in the extension state: it registers a verifier.)

#[test]
fn a_module_listed_as_an_extension_is_one() {
    for module in EXTENSION_MODULES.iter().filter(|m| here(m)) {
        let text = fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join(module))
            .unwrap_or_else(|e| panic!("{module}: {e}"));
        assert!(
            text.contains("\nimpl Extension for "),
            "{module} is listed as an extension and implements none: it is built-in code \
             and may not touch the extension state"
        );
    }
}

#[test]
fn nothing_built_in_inserts_into_or_reads_the_extension_state() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
    let mut files = Vec::new();
    rust_files(&root, &mut files);
    // The Enterprise workspace too, where this repository carries it.
    // There the rule reads: only a
    // module that IS an extension keeps state in the extension state; a
    // helper module of a feature reaches it through its extension's module.
    if let Some(enterprise) = roots::tree("enterprise") {
        let enterprise = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join(enterprise)
            .join("src");
        let before = files.len();
        rust_files(&enterprise, &mut files);
        assert!(files.len() > before + 20, "the Enterprise sources");
    }
    files.sort();
    let module = root.join("services").join("extension_state.rs");

    let mut offenders = Vec::new();
    for path in &files {
        if *path == module {
            continue; // the map's own definition and unit tests
        }
        if EXTENSION_MODULES
            .iter()
            .any(|m| *path == Path::new(env!("CARGO_MANIFEST_DIR")).join(m))
        {
            continue; // an extension's own state
        }
        let text = fs::read_to_string(path).expect("read file");
        for (i, line) in text.lines().enumerate() {
            let trimmed = line.trim_start();
            if trimmed.starts_with("//") {
                continue;
            }
            if line.contains("extensions.insert")
                || line.contains("extensions.get::<")
                || line.contains("extensions.contains::<")
            {
                offenders.push(format!("{}:{}: {}", path.display(), i + 1, line.trim()));
            }
        }
    }
    assert!(
        offenders.is_empty(),
        "built-in code touches the extension state; it is for extensions only:\n{}",
        offenders.join("\n")
    );
}

#[test]
fn app_state_carries_exactly_one_extension_state_field() {
    let text =
        fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("src/services/mod.rs"))
            .expect("read services/mod.rs");
    let state = &text[text.find("pub struct AppState {").expect("AppState")..];
    let state = &state[..state.find("\n}").expect("end of struct")];
    let fields: Vec<&str> = state
        .lines()
        .filter(|l| l.contains("ExtensionState"))
        .collect();
    assert_eq!(
        fields,
        vec!["    pub extensions: extension_state::ExtensionState,"],
        "AppState holds the extension state as the one field `extensions`"
    );
    // and new() starts it empty: the field is initialised from `new()`, not from a value
    let new_fn = &text[text.find("    pub fn new(").expect("new")..];
    assert!(
        new_fn.contains("extensions: extension_state::ExtensionState::new(),"),
        "AppState::new initialises `extensions` empty"
    );
}

#[test]
fn the_guard_above_sees_an_insertion() {
    // the same predicate, on a line a built-in module could contain
    let line = "        app_state.extensions.insert(Reports::default())?;";
    assert!(line.contains("extensions.insert"));
    let comment = "        // app_state.extensions.insert(...) is for extensions";
    assert!(comment.trim_start().starts_with("//"));
}
