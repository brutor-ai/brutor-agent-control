// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! OpenAPI surface snapshot — Rust proxy.
//!
//! The document is the one the Community gateway serves
//! (`GatewayBuilder::openapi_document` with no extensions), so its
//! `info.license` is the Community assembly's: Apache-2.0.
//!
//! Mirrors the Python control-plane snapshot test
//! (`brutor-gateway-control/tests/test_openapi_snapshot.py`) so that
//! any change to the Rust proxy's routes or utoipa schemas is caught
//! in a single diff at CI time.
//!
//! When this test fails
//! --------------------
//! The diff shown by cargo-test's panic payload is exactly the set of
//! API-surface changes a reviewer should sign off on. Two ways to
//! handle a failure:
//!
//! 1. **The change is intentional.** Regenerate the snapshot:
//!    `BRUTOR_UPDATE_OPENAPI_SNAPSHOT=1 cargo test --test openapi_snapshot_tests`
//!    Commit the updated fixture. The reviewer sees the diff in the PR.
//!
//! 2. **The change is unintentional.** Fix the code; the test passes
//!    again without touching the fixture.
//!
//! What this does NOT do
//! ---------------------
//! * Not a behavior test — the individual route integration tests
//!   (`system_alerts_tests`, `system_alerts_resolver_tests`, etc.)
//!   cover that.
//! * Not coupled to the TypeScript codegen — it emits a
//!   `proxy-openapi.json` at the repo root when explicitly asked to
//!   (`BRUTOR_EXPORT_OPENAPI=1`), which the admin/portal's
//!   `openapi-typescript` build step consumes.

use std::env;
use std::fs;
use std::path::PathBuf;

use brutor_gateway_core::builder::GatewayBuilder;

const UPDATE_ENV: &str = "BRUTOR_UPDATE_OPENAPI_SNAPSHOT";
const EXPORT_ENV: &str = "BRUTOR_EXPORT_OPENAPI";

fn snapshot_path() -> PathBuf {
    // `CARGO_MANIFEST_DIR` is the crate root at test-run time. Using
    // it makes the path stable whether cargo test is invoked from the
    // workspace root or the crate dir.
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join("openapi_snapshot.json")
}

fn export_path() -> PathBuf {
    // This is the file the TypeScript codegen consumes. Writing it
    // from the snapshot test means the codegen is always fed the
    // version the test just validated — not a stale copy checked in
    // by hand. CI runs the test with BRUTOR_EXPORT_OPENAPI=1 so the
    // build artifact is refreshed.
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("openapi")
        .join("proxy-openapi.json")
}

/// Render the utoipa `OpenApi` struct to a canonical JSON string.
///
/// We go through `serde_json::Value` as the intermediate so we can
/// sort keys deterministically — utoipa's derive preserves insertion
/// order of its internal BTreeMaps (stable) but inline structs use
/// attribute-declaration order, which could change if someone
/// reorders fields in a response type. Sorting kills that class of
/// spurious diff.
fn canonicalize() -> String {
    render(sort_json_keys(spec_value()))
}

/// The snapshot form: the canonical spec with `info.version` masked.
///
/// `info.version` is `CARGO_PKG_VERSION`, which the release script bumps
/// on every release. Left in, the snapshot drifted on every tag and the
/// test was red for reasons that had nothing to do with the API surface.
/// The export written for the TypeScript codegen keeps the real version.
fn canonicalize_for_snapshot() -> String {
    let mut value = sort_json_keys(spec_value());
    if let Some(info) = value.get_mut("info").and_then(|v| v.as_object_mut()) {
        info.insert(
            "version".to_string(),
            serde_json::Value::String(VERSION_PLACEHOLDER.to_string()),
        );
    }
    render(value)
}

const VERSION_PLACEHOLDER: &str = "<crate version — masked in the snapshot>";

/// The document the Community gateway serves: the derived `ApiDoc` as the
/// Community assembly (no extensions) composes it, licence included.
fn spec_value() -> serde_json::Value {
    GatewayBuilder::openapi_document(&[]).expect("the Community document")
}

/// serde_json's Value::Object is insertion-ordered, so to keep the
/// snapshot stable across utoipa's internal reorderings the caller
/// sorts map keys first. `to_string_pretty` then emits a diff-friendly
/// 2-space-indented form.
fn render(value: serde_json::Value) -> String {
    let mut s = serde_json::to_string_pretty(&value).expect("pretty print sorted value");
    s.push('\n');
    s
}

/// Recursively sort object keys so map insertion order doesn't leak
/// into the snapshot.
fn sort_json_keys(v: serde_json::Value) -> serde_json::Value {
    use serde_json::{Map, Value};
    match v {
        Value::Object(m) => {
            // BTreeMap gives ordered iteration for free; collect back
            // into serde_json's Map (which preserves insertion order)
            // after sorting.
            let mut sorted = Map::new();
            let mut pairs: Vec<(String, Value)> = m.into_iter().collect();
            pairs.sort_by(|a, b| a.0.cmp(&b.0));
            for (k, val) in pairs {
                sorted.insert(k, sort_json_keys(val));
            }
            Value::Object(sorted)
        }
        Value::Array(a) => Value::Array(a.into_iter().map(sort_json_keys).collect()),
        other => other,
    }
}

fn write_file(path: &std::path::Path, contents: &str) {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).expect("create parent dirs");
    }
    fs::write(path, contents).expect("write snapshot");
}

/// The Community gateway's published document states its licence,
/// Apache-2.0, as the Community assembly sets it: not the package
/// manifest's field and not a string in the derived document.
#[test]
fn the_community_document_states_the_apache_licence() {
    assert_eq!(
        spec_value()["info"]["license"],
        serde_json::json!({"name": "Apache License 2.0", "identifier": "Apache-2.0"})
    );
}

#[test]
fn openapi_surface_snapshot() {
    let current = canonicalize();
    let snapshot = canonicalize_for_snapshot();

    // Sibling artifact the TypeScript codegen consumes. Regenerating
    // the snapshot also refreshes the export — they're the same bits,
    // and letting them drift apart would mean the admin/portal
    // `npm run gen:api-types` picks up stale types on the next commit.
    // Either env var (UPDATE_ENV or EXPORT_ENV) triggers the write.
    let regen = env::var(UPDATE_ENV).ok().as_deref() == Some("1");
    let export = env::var(EXPORT_ENV).ok().as_deref() == Some("1");
    if regen || export {
        write_file(&export_path(), &current);
    }

    if regen {
        write_file(&snapshot_path(), &snapshot);
        // A regen is never a green build. We explicitly bail so the
        // engineer can commit the updated fixture in the same PR as
        // the code change that caused the drift.
        panic!(
            "{UPDATE_ENV}=1 — snapshot + export regenerated at {} and {}. \
             Commit the files and re-run without the env var to verify.",
            snapshot_path().display(),
            export_path().display()
        );
    }

    let path = snapshot_path();
    if !path.exists() {
        write_file(&path, &snapshot);
        panic!(
            "No snapshot existed at {}. Created one from the current spec. \
             Commit it and re-run.",
            path.display()
        );
    }

    let expected = fs::read_to_string(&path).expect("read existing snapshot");
    let current = snapshot;
    if current != expected {
        // Find the first differing line so the error message gives
        // the reviewer a usable coordinate even before they open the
        // diff in their editor. Whole-string asserts on 50k-line
        // files produce unreadable output.
        let first_diff_line = current
            .lines()
            .zip(expected.lines())
            .enumerate()
            .find(|(_, (c, e))| c != e)
            .map(|(i, _)| i + 1)
            .unwrap_or_else(|| {
                if current.lines().count() != expected.lines().count() {
                    expected.lines().count().min(current.lines().count()) + 1
                } else {
                    0
                }
            });

        panic!(
            "\n\nOpenAPI surface drifted from the committed snapshot.\n\
             \n\
             First differing line: {first_diff_line}\n\
             Snapshot path: {}\n\
             \n\
             If the change is INTENTIONAL, regenerate with:\n    \
             {UPDATE_ENV}=1 cargo test --test openapi_snapshot_tests\n\
             and commit the fixture.\n\
             \n\
             If UNINTENTIONAL, fix the code — do not edit the fixture.\n",
            snapshot_path().display(),
        );
    }
}
