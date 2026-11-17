// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: the licence is an extension and nothing else in the gateway names
//! it.
//!
//! Why it matters: the Community Edition has no licence code
//!  and starts with no key. That is true of a gateway composed without
//! the gate only while the builder, the task list, the configuration and
//! every other module know no licence: one `config.license_key` read back
//! into `tasks.rs`, or one call into `services::license` from a route, and
//! the Community build needs the module (and the private licence
//! library) again. `main.rs` is where a distribution composes its extensions,
//! so it is the one file that names the gate.
//!
//! The scan is proven against the shapes it forbids
//! ([`the_scan_catches_the_shapes_it_forbids`]).

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

/// The licence module itself.
const LICENCE_MODULE: &str = "enterprise/src/licence.rs";
/// Where this distribution composes its extensions.
const COMPOSITION_ROOT: &str = "enterprise/src/main.rs";

/// What names the licence in code. Chosen to be the identifiers of the
/// module, its crate, its type, its key and its variable, so that the
/// unrelated uses of the word (a skill's `license` field, a model licence in
/// a comment) are not caught.
const NAMES: &[&str] = &[
    "services::license",
    "license::",
    "licence::",
    "brutor_license",
    "LicenceGate",
    "license_key",
    "licence_key",
    "BRUTOR_LICENSE_KEY",
    "LICENCE_KEY_ENV",
    "licence_check",
    "verify_license",
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

/// `(line number, name)` for every line of code in `text` that names the
/// licence. Comment lines are prose, not code. A file's unit tests are
/// scanned too: a test that needs the licence module pins it in place as
/// surely as production code does.
fn licence_names(text: &str) -> Vec<(usize, &'static str)> {
    let mut found = Vec::new();
    for (i, line) in text.lines().enumerate() {
        if line.trim_start().starts_with("//") {
            continue;
        }
        for name in NAMES {
            if line.contains(name) {
                found.push((i + 1, *name));
            }
        }
    }
    found
}

#[test]
fn nothing_but_the_composition_root_names_the_licence() {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    assert!(files.len() > 100, "the scan found {} files", files.len());
    let mut named = Vec::new();
    let mut module_declared = 0;
    for file in &files {
        let rel = file.to_string_lossy().replace('\\', "/");
        if rel == LICENCE_MODULE || rel == COMPOSITION_ROOT {
            continue;
        }
        let text = fs::read_to_string(file).unwrap();
        for (line, name) in licence_names(&text) {
            named.push(format!("{rel}:{line}: `{name}`"));
        }
        // The one line that is allowed: the module's declaration, in the
        // Enterprise crate since the module moved there.
        if rel == "enterprise/src/lib.rs" {
            module_declared = text.matches("pub mod licence;").count();
        }
    }
    assert!(
        named.is_empty(),
        "the licence is named outside its module and the composition root; a gateway composed \
         without the gate would still need it:\n{}",
        named.join("\n")
    );
    assert_eq!(
        module_declared,
        usize::from(here("enterprise/src/lib.rs")),
        "enterprise/src/lib.rs declares the module"
    );
    // The scan reads something: the module and the root do name it.
    for file in [LICENCE_MODULE, COMPOSITION_ROOT]
        .into_iter()
        .filter(|f| here(f))
    {
        assert!(
            !licence_names(&fs::read_to_string(file).unwrap()).is_empty(),
            "{file} does not name the licence: the scan's names are stale"
        );
    }
}

/// The composition root composes the gate over the environment's key, and
/// does nothing else with the licence: no verification of its own, no other
/// constructor.
#[test]
fn main_composes_the_gate_from_the_environment_and_nothing_else() {
    // A Community tree alone has no composition root that composes a gate.
    if !here(COMPOSITION_ROOT) {
        return;
    }
    let main = fs::read_to_string(COMPOSITION_ROOT).unwrap();
    let named: Vec<String> = main
        .lines()
        .filter(|l| !l.trim_start().starts_with("//"))
        .filter(|l| NAMES.iter().any(|n| l.contains(n)))
        .map(|l| l.trim().to_string())
        .collect();
    assert_eq!(
        named,
        [
            "use brutor_gateway_enterprise::licence::LicenceGate;",
            ".extension(LicenceGate::from_env())",
        ]
    );
    // The constructor that takes a verifying key is for tests of the
    // composition; no code under src/ calls it.
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    for file in files {
        let rel = file.to_string_lossy().replace('\\', "/");
        let text = fs::read_to_string(&file).unwrap();
        let production = text.split("#[cfg(test)]\nmod tests").next().unwrap_or("");
        let calls = production.matches("with_verifying_key(").count();
        let allowed = if rel == LICENCE_MODULE { 2 } else { 0 }; // its definition and `with_key`
        assert_eq!(calls, allowed, "{rel}: `with_verifying_key(`");
    }
}

#[test]
fn the_scan_catches_the_shapes_it_forbids() {
    for (shape, expect) in [
        (
            "    if let Some(key) = &config.license_key {",
            "license_key",
        ),
        (
            "        services::license::spawn_periodic_check(key.clone(), token);",
            "services::license",
        ),
        (
            "            license_key: var(\"BRUTOR_LICENSE_KEY\").ok(),",
            "BRUTOR_LICENSE_KEY",
        ),
        ("use brutor_license::License;", "brutor_license"),
        (
            "        self.extension(LicenceGate::from_env())",
            "LicenceGate",
        ),
        ("        start: licence_check,", "licence_check"),
        ("use super::license::verify;", "license::"),
    ] {
        let found = licence_names(shape);
        assert!(
            found.iter().any(|(_, name)| *name == expect),
            "`{shape}` is not caught as `{expect}`: {found:?}"
        );
    }
    // What it must not catch: the word in prose, and the unrelated fields.
    for innocent in [
        "    // the licence gate refuses the start (services::license)",
        "    pub license: Option<String>,",
        "        \"license\": skill.license,",
        "//! Community Edition: Apache-2.0 licence.",
    ] {
        assert!(licence_names(innocent).is_empty(), "{innocent}");
    }
}
