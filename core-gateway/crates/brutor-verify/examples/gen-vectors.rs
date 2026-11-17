// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Regenerate the evidence vector corpus (`vectors/` at the core repo root)
//! and the control plane's parity fixture, byte-for-byte deterministically:
//!
//! ```text
//! cargo run -p brutor-verify --example gen-vectors
//! ```
//!
//! The control-plane fixture is written to
//! `$BRUTOR_CONTROL_REPO/tests/fixtures/evidence_interop_v1.json`, else to
//! the control plane beside this repository (`../brutor-gateway-control/`,
//! or `../control-plane/` in the public monorepo); it is skipped (with a
//! note) when none exists. `tests/vectors.rs` fails when the committed
//! files differ from what this produces.

#[path = "../tests/corpus/mod.rs"]
mod corpus;

use std::collections::BTreeSet;

fn main() {
    let root = corpus::core_root();
    let c = corpus::generate();
    let dir = root.join("vectors");
    // Remove generated files that are no longer produced (never the
    // hand-written README or interop/).
    let wanted: BTreeSet<String> = c.files.keys().cloned().collect();
    for stale in walk(&dir) {
        let rel = stale
            .strip_prefix(&root)
            .unwrap()
            .to_string_lossy()
            .replace('\\', "/");
        if !wanted.contains(&rel) && !corpus_owned_elsewhere(&rel) {
            std::fs::remove_file(&stale).unwrap();
            println!("removed {rel}");
        }
    }
    for (rel, content) in &c.files {
        let p = root.join(rel);
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        std::fs::write(&p, content).unwrap();
    }
    println!("wrote {} files under {}", c.files.len(), dir.display());
    let cp = corpus::cp_fixture_path();
    if cp.parent().map(|d| d.is_dir()).unwrap_or(false) {
        std::fs::write(&cp, &c.cp_fixture).unwrap();
        println!("wrote {}", cp.display());
    } else {
        println!(
            "control plane checkout not found; skipped {} (set BRUTOR_CONTROL_REPO)",
            cp.display()
        );
    }
}

/// Hand-written files under vectors/ the generator never owns.
fn corpus_owned_elsewhere(rel: &str) -> bool {
    rel == "vectors/README.md" || rel.starts_with("vectors/interop/")
}

fn walk(d: &std::path::Path) -> Vec<std::path::PathBuf> {
    let mut out = Vec::new();
    if let Ok(rd) = std::fs::read_dir(d) {
        for e in rd.flatten() {
            let p = e.path();
            if p.is_dir() {
                out.extend(walk(&p));
            } else {
                out.push(p);
            }
        }
    }
    out
}
