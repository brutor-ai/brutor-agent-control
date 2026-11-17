// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: batch jobs are an extension and nothing else in the gateway names
//! them.
//!
//! Why it matters: a Community gateway serves no batch route and starts no
//! batch task because `app.rs` mounts none and `tasks.rs` lists none. One
//! `.nest("/v1/proxy/batch", routes::batch::router())` back in `app.rs`, or
//! one call into `services::batch` from a task, and the Community build
//! serves an Enterprise feature again, with the Community route table and
//! the Enterprise one equal and no test of this tree's own noticing, since
//! this tree still builds both from one crate. `main.rs` is where a
//! distribution composes its extensions, so it is the one file outside the
//! batch modules that names them.
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

/// The batch modules themselves (`editions.yaml`, `enterprise_modules.batch_jobs`).
const BATCH_MODULES: &[&str] = &["enterprise/src/batch/routes.rs", "enterprise/src/batch/"];
/// Where this distribution composes its extensions.
const COMPOSITION_ROOT: &str = "enterprise/src/main.rs";
/// The module declarations, which move with the modules: the one line
/// each of these files may have.
const DECLARATIONS: &[&str] = &["enterprise/src/lib.rs"];

/// What names batch jobs in code: the modules, the extension, the mount and
/// the loops. Not the words `batch` or `batch_jobs` alone (a JSON-RPC batch,
/// the metrics flusher's batches, the Enterprise-data check's table list are not this feature's
/// code).
const NAMES: &[&str] = &[
    "routes::batch",
    "services::batch",
    "BatchJobs",
    "BATCH_MOUNT",
    "\"/v1/proxy/batch",
    "run_submitter_loop",
    "run_poller_loop",
    "fetch_and_write_results",
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

/// `(line number, name)` for every line of code in `text` that names batch
/// jobs. Comment lines are prose. Unit tests are scanned too.
fn batch_names(text: &str) -> Vec<(usize, &'static str)> {
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
fn nothing_but_the_composition_root_names_batch_jobs() {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    assert!(files.len() > 100, "the scan found {} files", files.len());
    let mut named = Vec::new();
    let mut own = 0;
    for file in &files {
        let rel = file.to_string_lossy().replace('\\', "/");
        let text = fs::read_to_string(file).unwrap();
        if BATCH_MODULES
            .iter()
            .any(|m| rel == *m || rel.starts_with(m))
        {
            own += batch_names(&text).len();
            continue;
        }
        if rel == COMPOSITION_ROOT {
            continue;
        }
        for (line, name) in batch_names(&text) {
            named.push(format!("{rel}:{line}: `{name}`"));
        }
        if DECLARATIONS.contains(&rel.as_str()) {
            assert_eq!(
                text.matches("pub mod batch;").count(),
                1,
                "{rel} declares the module"
            );
        }
    }
    assert!(
        named.is_empty(),
        "batch jobs are named outside their modules and the composition root; a gateway \
         composed without the extension would still have them:\n{}",
        named.join("\n")
    );
    assert!(
        own >= 5 || !here(COMPOSITION_ROOT),
        "the batch modules name themselves {own} times: the scan's names are stale"
    );
}

/// The composition root composes the extension and does nothing else with
/// batch jobs.
#[test]
fn main_composes_the_extension_and_nothing_else() {
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
            "use brutor_gateway_enterprise::batch::BatchJobs;",
            ".extension(BatchJobs::new())",
        ]
    );
}

/// The mount is the extension's and the route module declares no prefix of
/// its own: `/v1/proxy/batch` appears once in code, in the extension.
#[test]
fn the_batch_prefix_is_mounted_by_the_extension_only() {
    if here("enterprise/src/batch/extension.rs") {
        let extension = fs::read_to_string("enterprise/src/batch/extension.rs").unwrap();
        assert_eq!(
            extension
                .matches("pub const BATCH_MOUNT: &str = \"/v1/proxy/batch\";")
                .count(),
            1
        );
        assert_eq!(
            extension
                .matches("Router::new().nest(BATCH_MOUNT, crate::batch::routes::router())")
                .count(),
            1
        );
    }
    let app = fs::read_to_string("src/app.rs").unwrap();
    assert!(
        batch_names(&app).is_empty(),
        "src/app.rs: {:?}",
        batch_names(&app)
    );
    let tasks = fs::read_to_string("src/tasks.rs").unwrap();
    assert!(
        batch_names(&tasks).is_empty() && !tasks.contains("\"batch_"),
        "src/tasks.rs names a batch task"
    );
}

#[test]
fn the_scan_catches_the_shapes_it_forbids() {
    for (shape, expect) in [
        (
            "        .nest(\"/v1/proxy/batch\", routes::batch::router())",
            "routes::batch",
        ),
        (
            "        .nest(\"/v1/proxy/batch\", batch_router())",
            "\"/v1/proxy/batch",
        ),
        (
            "        services::batch::run_submitter_loop(",
            "services::batch",
        ),
        ("    use crate::services::batch::BatchJobs;", "BatchJobs"),
        (
            "        batch::run_poller_loop(state, db, http, enc, 30)",
            "run_poller_loop",
        ),
        ("        .nest(BATCH_MOUNT, router())", "BATCH_MOUNT"),
    ] {
        let found = batch_names(shape);
        assert!(
            found.iter().any(|(_, name)| *name == expect),
            "`{shape}` is not caught as `{expect}`: {found:?}"
        );
    }
    for innocent in [
        "    // The batch surface (`/v1/proxy/batch`): job submit / read / cancel rows",
        "    (\"batch_jobs\", \"batch_jobs\"),",
        "                \"refused a JSON-RPC batch on the MCP proxy\"",
        "    pub const BATCH_SUBMITTER: &str = \"batch_submitter\";",
    ] {
        assert!(batch_names(innocent).is_empty(), "{innocent}");
    }
}
