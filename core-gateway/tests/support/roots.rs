// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Where the trees a test reads are: ONE function, [`tree`], for every test
//! of this repository that opens a file outside its own crate's `src/`.
//!
//! A test never spells the layout itself (`"enterprise/src"`,
//! `"../brutor-gateway-control"`): the layout changes, and a guard that walks
//! a directory "if it is there" then passes without reading one file. Each
//! tree is named by an environment variable. A tree of this repository, or
//! of a repository checked out beside it, has today's layout as its default;
//! a tree kept anywhere else has none. A sibling's default is two places
//! beside the Community root, looked at in this order: the directory named
//! after its own repository, then its directory in the public monorepo
//! (the export's layout: `core-gateway/` beside `control-plane/` and
//! `skill-runner/`); the first that is there is the tree:
//!
//! | `tree(..)` | Variable | Default | When it is not there |
//! |---|---|---|---|
//! | `"community"` | `BRUTOR_COMMUNITY_ROOT` | the directory that holds this file's `tests/support/`, found from the crate being compiled | (always there) |
//! | `"enterprise"` | `BRUTOR_ENTERPRISE_ROOT` | the crate being compiled when that is the Enterprise crate; else `enterprise/` under the Community root, when it is there | `None`: a Community tree alone. **A root that was given and holds no source file is a failure**, never `None` |
//! | `"control-plane"` | `BRUTOR_CONTROL_REPO` | `../brutor-gateway-control`, else `../control-plane`, beside the Community root | a failure, unless `BRUTOR_TEST_SIBLINGS=optional`: then `None`, and the skip is recorded |
//! | `"edition-records"` | `BRUTOR_EDITION_RECORDS_DIR` | none: the directory that holds `editions.yaml` and the edition-install contract is kept outside this repository, and only the variable says where | not given: `None`, and the skip is recorded. **Given and not a directory: a failure** |
//! | `"skill-runner"` | `BRUTOR_SKILL_RUNNER_REPO` | `../brutor-skill-runner`, else `../skill-runner`, beside the Community root | `None`, and the skip is recorded |
//! | `"kb-uploader"`, `"kb-connector-sync"`, `"licence-library"` | `BRUTOR_KB_UPLOADER_REPO`, `BRUTOR_KB_CONNECTOR_SYNC_REPO`, `BRUTOR_LICENSE_REPO` | none: these are private repositories, which this one does not name; only the variable says where | not given: `None`, and the skip is recorded. **Given and not a directory: a failure** |
//!
//! A recorded skip is a line on stderr that starts with `SKIPPED:` and, when
//! `BRUTOR_TEST_SKIP_LOG` names a file, a line appended to it
//! (`<test>\t<tree>\t<where it was looked for>`): a passing test's output is
//! not shown, so the run that allows a skip (`scripts/ci-community-alone.sh`)
//! reads the file and prints what was skipped.
//!
//! Included by path (`#[path = "support/roots.rs"] mod roots;`); it has one
//! public function so that no including test leaves part of it unused.

use std::io::Write;
use std::path::{Path, PathBuf};

/// The root of the tree called `name`, or `None` where the table in the
/// module documentation says so. Panics on a name it does not know.
pub fn tree(name: &str) -> Option<PathBuf> {
    match name {
        "community" => Some(community()),
        "enterprise" => enterprise(),
        "control-plane" => sibling(
            name,
            "BRUTOR_CONTROL_REPO",
            ["brutor-gateway-control", "control-plane"],
            true,
        ),
        "edition-records" => named_only(name, "BRUTOR_EDITION_RECORDS_DIR"),
        "skill-runner" => sibling(
            name,
            "BRUTOR_SKILL_RUNNER_REPO",
            ["brutor-skill-runner", "skill-runner"],
            false,
        ),
        "kb-uploader" => named_only(name, "BRUTOR_KB_UPLOADER_REPO"),
        "kb-connector-sync" => named_only(name, "BRUTOR_KB_CONNECTOR_SYNC_REPO"),
        "licence-library" => named_only(name, "BRUTOR_LICENSE_REPO"),
        other => panic!("roots::tree: no tree is called {other:?}"),
    }
}

fn given(variable: &str) -> Option<PathBuf> {
    std::env::var(variable)
        .ok()
        .filter(|value| !value.trim().is_empty())
        .map(PathBuf::from)
}

fn community() -> PathBuf {
    if let Some(root) = given("BRUTOR_COMMUNITY_ROOT") {
        assert!(
            root.join("tests/support/roots.rs").is_file() && root.join("src/lib.rs").is_file(),
            "BRUTOR_COMMUNITY_ROOT={} is not the Community crate: it has no src/lib.rs and \
             tests/support/roots.rs",
            root.display()
        );
        return root;
    }
    // The crate being compiled is the Community crate, one of its workspace
    // members (`crates/*`), or a workspace nested in it (`enterprise/`).
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .find(|dir| dir.join("tests/support/roots.rs").is_file())
        .unwrap_or_else(|| {
            panic!(
                "the Community crate is not {} and does not enclose it: set BRUTOR_COMMUNITY_ROOT",
                env!("CARGO_MANIFEST_DIR")
            )
        })
        .to_path_buf()
}

fn has_rust_source(dir: &Path) -> bool {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return false;
    };
    entries.flatten().any(|entry| {
        let path = entry.path();
        if path.is_dir() {
            has_rust_source(&path)
        } else {
            path.extension().is_some_and(|x| x == "rs")
        }
    })
}

fn enterprise() -> Option<PathBuf> {
    let community = community();
    let root = if let Some(root) = given("BRUTOR_ENTERPRISE_ROOT") {
        // Given: it is the Enterprise tree or the run fails. A scan of
        // nothing is not a pass.
        assert!(
            has_rust_source(&root.join("src")),
            "BRUTOR_ENTERPRISE_ROOT={} holds no Rust source under src/: the Enterprise tree is \
             not there, and a guard that reads it would pass without reading a file",
            root.display()
        );
        root
    } else if env!("CARGO_PKG_NAME") == "brutor-gateway-enterprise" {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
    } else {
        let nested = community.join("enterprise");
        if !nested.is_dir() {
            return None;
        }
        assert!(
            has_rust_source(&nested.join("src")),
            "{} is there and holds no Rust source under src/",
            nested.display()
        );
        nested
    };
    // The guards run from the Community root and name files relative to it
    // (`enterprise/src/main.rs`); where the tree is that directory, it is
    // returned under that name.
    let nested = Path::new("enterprise");
    match (nested.canonicalize(), root.canonicalize()) {
        (Ok(a), Ok(b)) if a == b => Some(nested.to_path_buf()),
        _ => Some(root),
    }
}

/// `directories`: the sibling's directory beside the Community root, under
/// its repository's name and then under its name in the public monorepo.
fn sibling(name: &str, variable: &str, directories: [&str; 2], required: bool) -> Option<PathBuf> {
    let places = match given(variable) {
        Some(root) => vec![root],
        None => {
            let community = community();
            let community = community.canonicalize().unwrap_or(community);
            let beside = community.parent().expect("the Community root has a parent");
            directories.iter().map(|d| beside.join(d)).collect()
        }
    };
    if let Some(root) = places.iter().find(|place| place.is_dir()) {
        return Some(root.clone());
    }
    let root = places
        .iter()
        .map(|place| place.display().to_string())
        .collect::<Vec<_>>()
        .join(" or ");
    let optional = std::env::var("BRUTOR_TEST_SIBLINGS").is_ok_and(|v| v == "optional");
    assert!(
        optional || !required,
        "this test needs the {name} repository and it is not at {root}: check it out beside this \
         one or set {variable} to where it is (BRUTOR_TEST_SIBLINGS=optional skips the test, \
         and says so)"
    );
    skipped(
        name,
        &format!("it needs the {name} repository, which is not at {root} (set {variable})"),
        &root,
    );
    None
}

/// A tree that has no default place: it is where `variable` says, and
/// nowhere else. Not given: the test is skipped and the skip is recorded
/// with the variable to set. Given and not a directory: a failure, since a
/// run that names the tree means to read it.
fn named_only(name: &str, variable: &str) -> Option<PathBuf> {
    let Some(root) = given(variable) else {
        skipped(
            name,
            &format!(
                "it reads the {name} tree, and {variable} is not set (set {variable} to the directory that holds it)"
            ),
            &format!("{variable} is not set"),
        );
        return None;
    };
    assert!(
        root.is_dir(),
        "{variable}={} is not a directory: the {name} tree is not there, and a test that \
         reads it would pass without reading a file",
        root.display()
    );
    Some(root)
}

/// Record that the running test is skipped: a `SKIPPED:` line on stderr
/// and, when `BRUTOR_TEST_SKIP_LOG` names a file, a line in it.
fn skipped(name: &str, why: &str, looked_for: &str) {
    let test = std::thread::current()
        .name()
        .unwrap_or("unnamed")
        .to_string();
    eprintln!("SKIPPED: {test}: {why}");
    if let Some(log) = given("BRUTOR_TEST_SKIP_LOG") {
        let line = format!("{test}\t{name}\t{looked_for}\n");
        std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log)
            .and_then(|mut f| f.write_all(line.as_bytes()))
            .unwrap_or_else(|e| panic!("the skip log {} is not writable: {e}", log.display()));
    }
}
