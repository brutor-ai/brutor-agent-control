// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: nothing in this repository cites a record its reader cannot open:
//! an internal design document, a review finding, a plan step, a decision.
//!
//! The rule or the reason is written where it applies. Published standards
//! may be cited; the ones allowed everywhere are listed with their titles in
//! `common/private_references.rs`, which is the same file in every Rust
//! repository of the product, and what this repository adds is declared in
//! `support/private_reference_rules.rs`.
//!
//! The scan reads every tracked text file of the repository, the Enterprise
//! workspace included where this repository carries it. The Enterprise
//! workspace runs the same scan over its own files.

use std::fs;

#[path = "common/private_references.rs"]
mod private_references;
#[path = "support/roots.rs"]
mod roots;
#[path = "support/private_reference_rules.rs"]
mod rules;

#[test]
fn nothing_cites_a_record_the_reader_cannot_open() {
    let root = roots::tree("community").expect("the Community root");
    let (failures, read) = rules::check(&root, "");
    assert!(read > 500, "the scan read only {read} files");
    assert!(
        failures.is_empty(),
        "{} references to private records (write the rule out, or delete):\n{}",
        failures.len(),
        failures.join("\n")
    );
}

#[test]
fn the_reference_scan_finds_each_kind_and_leaves_public_standards_alone() {
    private_references::check_the_scanner();
}

#[test]
fn what_this_repository_declares_is_needed_and_has_a_reason() {
    rules::check_the_declarations();
}

/// A reference outside the declarations is a failure, with its place,
/// wherever it is: no file holds an allowance.
#[test]
fn a_reference_outside_the_declarations_fails() {
    let report = vec![
        "src/lib.rs:7: internal RFC: RFC 0021 §7.5".to_string(),
        "src/lib.rs:9: finding number: F245".to_string(),
    ];
    assert_eq!(rules::undeclared(&report, ""), report);
    assert_eq!(
        rules::undeclared(&report, "enterprise/"),
        [
            "enterprise/src/lib.rs:7: internal RFC: RFC 0021 §7.5",
            "enterprise/src/lib.rs:9: finding number: F245",
        ]
    );

    // A public standard this repository declares is let through; the same
    // words with an internal number are not.
    let public = vec!["src/lib.rs:3: internal RFC: RFC 3161 §2.4.2".to_string()];
    assert!(rules::undeclared(&public, "").is_empty());

    // No file may name the private records, the roots helper included: a
    // tree kept outside this repository is named by its variable.
    assert!(rules::NEEDED_IN_ONE_FILE.is_empty());
    let once = "tests/support/roots.rs:70: private repository or record: brutor-rfcs".to_string();
    let single = vec![once.clone()];
    assert_eq!(rules::undeclared(&single, ""), single);
    let elsewhere =
        vec!["tests/other.rs:70: private repository or record: brutor-rfcs".to_string()];
    assert_eq!(rules::undeclared(&elsewhere, ""), elsewhere);

    // The Enterprise workspace names the licence library it depends on, and
    // no other private repository; the Community tree names none.
    let licence = vec!["Cargo.toml:11: private repository or record: brutor-license".to_string()];
    assert!(rules::undeclared(&licence, "enterprise/").is_empty());
    assert_eq!(rules::undeclared(&licence, ""), licence);
    let uploader = vec![
        "src/bm25.rs:80: private repository or record: brutor-gateway-kb-uploader".to_string(),
    ];
    assert_eq!(
        rules::undeclared(&uploader, "enterprise/"),
        ["enterprise/src/bm25.rs:80: private repository or record: brutor-gateway-kb-uploader"]
    );

    // The published test vector and its generator once held one each.
    let vector = vec!["vectors/canon/jcs.json:34: internal RFC: RFC 0021 §5.2".to_string()];
    assert_eq!(rules::undeclared(&vector, ""), vector);
}

/// The shapes this repository adds to the shared scanner, each found, and
/// what looks like them and is not left alone.
#[test]
fn the_shapes_this_repository_adds_are_found() {
    let found = |text: &str, community: bool| -> Vec<String> {
        rules::more_offences(text, community)
            .into_iter()
            .map(|(line, kind, cited)| format!("{line}: {kind}: {cited}"))
            .collect()
    };
    let text = "// no `unsafe` (T-02); flaky (T-04)\n\
                /// Q14: the trial (decision Q25).\n\
                const C: &str = \"Contact sales@brutor.ai to upgrade.\";\n";
    assert_eq!(
        found(text, true),
        [
            "1: engineering-assurance id: T-02",
            "1: engineering-assurance id: T-04",
            "2: plan step or decision id: Q14",
            "2: plan step or decision id: Q25",
            "3: company address: sales@brutor.ai",
        ]
    );
    // The Enterprise workspace may name the address of its own message; the
    // ids are private in either tree.
    assert_eq!(
        found(text, false),
        [
            "1: engineering-assurance id: T-02",
            "1: engineering-assurance id: T-04",
            "2: plan step or decision id: Q14",
            "2: plan step or decision id: Q25",
        ]
    );
    for fine in [
        "Wire shape (Q2 2026); every run executed in Q3; {\"period\": \"Q2\"}",
        "an id \"T-01\" and 'Q14' as data; T-012, T-01-a, Q14b, XT-01, #Q14, v.Q14",
        "brutor-gateway-core, brutor-skill-runner, brutor-client, brutor-licensed",
        "security at example.com: sales@example.com",
    ] {
        assert_eq!(found(fine, true), Vec::<String>::new(), "{fine}");
    }
}

#[test]
fn the_reference_scanner_is_the_same_in_every_rust_repository() {
    let file = "tests/common/private_references.rs";
    let community = roots::tree("community").expect("the Community root");
    let mine = fs::read_to_string(community.join(file)).unwrap();
    for sibling in [
        "skill-runner",
        "kb-uploader",
        "kb-connector-sync",
        "licence-library",
    ] {
        // A sibling that is not checked out is skipped, and the skip is
        // recorded (`support/roots.rs`).
        let Some(root) = roots::tree(sibling) else {
            continue;
        };
        let theirs = fs::read_to_string(root.join(file))
            .unwrap_or_else(|e| panic!("{}: {e}", root.join(file).display()));
        assert!(mine == theirs, "{file} differs from the copy in {sibling}");
    }
}
