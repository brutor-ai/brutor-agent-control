// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! What a person reads in this repository names the product and its
//! components as they are called today.
//!
//! The product is "Brutor Agent Control"; the components are the Control
//! Plane, the Core Gateway, the Admin Console, the User Portal and the Skill
//! Runner. Earlier names, sprint numbers and stock passwords do not belong
//! in the README, the documents, the manifest, the image or the source.

use std::fs;
use std::path::{Path, PathBuf};

#[path = "common/private_references.rs"]
mod private_references;
#[path = "common/workflows.rs"]
mod workflows;

/// Lower-case phrases that may not appear. Matched case-insensitively.
const RETIRED: &[&str] = &[
    "ai gateway",
    "brutor ai platform",
    "ai control plane",
    "core proxy",
    "admin ui",
    "management gateway",
    "litellm",
    "admin123",
    "trial123",
];

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn files_in(dir: &Path, extension: &str, out: &mut Vec<PathBuf>) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            files_in(&path, extension, out);
        } else if path.extension().and_then(|e| e.to_str()) == Some(extension) {
            out.push(path);
        }
    }
}

/// Every file a person reads: the documents, the manifest, the image
/// definition, the workflows and the source. This file and the reference
/// scanner name the retired phrases, so they are left out.
fn readable_files() -> Vec<PathBuf> {
    let root = root();
    let mut files = vec![
        root.join("README.md"),
        root.join("THREAT-MODEL.md"),
        root.join("Cargo.toml"),
        root.join("Dockerfile"),
    ];
    files_in(&root.join("docs"), "md", &mut files);
    files_in(&root.join("src"), "rs", &mut files);
    files_in(&root.join("tests"), "rs", &mut files);
    files.extend(workflows::find(&root, "skill-runner", "docker-build.yml").mine);
    files.retain(|f| {
        let name = f.file_name().and_then(|n| n.to_str());
        name != Some("readable_names.rs") && name != Some("private_references.rs")
    });
    files
}

/// `sprint 12`, `sprint #12`, `sprint 4.x`: a sprint followed by a number.
fn names_a_sprint(line: &str) -> bool {
    line.match_indices("sprint").any(|(at, word)| {
        let rest = line[at + word.len()..].trim_start_matches([' ', '#']);
        rest.starts_with(|c: char| c.is_ascii_digit())
    })
}

/// The retired wording found in `text`, as `line number: what`.
fn offences(text: &str) -> Vec<String> {
    let mut found = Vec::new();
    for (index, line) in text.lines().enumerate() {
        let lower = line.to_lowercase();
        for phrase in RETIRED {
            if lower.contains(phrase) {
                found.push(format!("{}: {phrase}", index + 1));
            }
        }
        if names_a_sprint(&lower) {
            found.push(format!("{}: a sprint number", index + 1));
        }
    }
    found
}

#[test]
fn nothing_a_person_reads_carries_a_retired_name() {
    let files = readable_files();
    assert!(
        files.len() > 12,
        "the scan found only {} files",
        files.len()
    );
    let mut report = Vec::new();
    for file in &files {
        let text = fs::read_to_string(file).unwrap_or_else(|e| panic!("{}: {e}", file.display()));
        for offence in offences(&text) {
            let name = file
                .strip_prefix(root())
                .unwrap_or(file)
                .display()
                .to_string();
            report.push(format!("{name}:{offence}"));
        }
    }
    assert!(report.is_empty(), "retired wording:\n{}", report.join("\n"));
}

#[test]
fn the_scan_finds_each_kind_and_leaves_the_current_names_alone() {
    let text = "\
sandbox for the Brutor AI Gateway
the Core Proxy signs the token
see the Admin UI
built in Sprint #663
sign in with Admin123!
Brutor Agent Control: the Core Gateway calls the Skill Runner
a sprint of work, then 3 more";
    assert_eq!(
        offences(text),
        vec![
            "1: ai gateway",
            "2: core proxy",
            "3: admin ui",
            "4: a sprint number",
            "5: admin123",
        ]
    );
}

#[test]
fn the_manifest_and_the_readme_name_the_product() {
    let manifest = fs::read_to_string(root().join("Cargo.toml")).unwrap();
    let description = manifest
        .lines()
        .find(|l| l.starts_with("description = "))
        .expect("Cargo.toml has a description");
    assert!(
        description.contains("Brutor Agent Control"),
        "{description}"
    );
    let readme = fs::read_to_string(root().join("README.md")).unwrap();
    let title = readme.lines().next().unwrap_or_default();
    assert!(title.contains("Brutor Agent Control"), "{title}");
    assert!(title.contains("Skill Runner"), "{title}");
}

/// Tracked files the private-reference scan does not read, each for a
/// reason: the scanner and this file spell out what they look for.
const NOT_SCANNED_FOR_REFERENCES: &[&str] = &[
    "tests/common/private_references.rs",
    "tests/readable_names.rs",
];

/// Private repositories a reader of this one can open, each with the reason.
const DECLARED: &[(&str, &str)] = &[];

#[test]
fn nothing_cites_a_record_the_reader_cannot_open() {
    let (report, read) = private_references::scan(&root(), NOT_SCANNED_FOR_REFERENCES, DECLARED);
    assert!(read > 12, "the scan read only {read} files");
    assert!(
        report.is_empty(),
        "{} references to private records (write the rule out, or delete):\n{}",
        report.len(),
        report.join("\n")
    );
}

#[test]
fn the_reference_scan_finds_each_kind_and_leaves_public_standards_alone() {
    private_references::check_the_scanner();
}

#[test]
fn the_reference_scanner_is_the_same_in_every_rust_repository() {
    let file = "tests/common/private_references.rs";
    let mine = fs::read_to_string(root().join(file)).unwrap();
    for sibling in [
        "brutor-skill-runner",
        "brutor-gateway-kb-uploader",
        "brutor-kb-connector-sync",
        "brutor-license",
    ] {
        match fs::read_to_string(root().join("..").join(sibling).join(file)) {
            Ok(theirs) => assert!(mine == theirs, "{file} differs from {sibling}'s copy"),
            // A checkout of this repository alone (CI) has no siblings.
            Err(_) => eprintln!("SKIPPED: {sibling} is not checked out beside this repository"),
        }
    }
}

/// The workflows read above are found where this tree's layout keeps them,
/// and a tree with none fails (`common/workflows.rs`).
#[test]
fn the_workflows_it_reads_are_found_in_either_layout() {
    workflows::check_the_layouts();
}
