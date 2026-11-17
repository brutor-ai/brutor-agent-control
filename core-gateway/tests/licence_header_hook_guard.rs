// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: the pre-commit hook that inserts a source header inserts the
//! header of the file's edition, and never fights a header a file has.
//!
//! The repository holds both editions, and their headers differ: a Community
//! file says `Copyright 2026 Brutor AI Ltd.` and
//! `SPDX-License-Identifier: Apache-2.0`, an Enterprise file (under
//! `enterprise/`) the Enterprise wording. Every header is replaced by the
//! cut tooling in one pass when the Community tree is exported, so before
//! that pass the Community files still carry the earlier header and after it
//! they carry the Community one. The hook (`insert-license` in
//! `.pre-commit-config.yaml`, one entry per edition) therefore inserts a
//! header only into a file that has none: a file whose first lines name the
//! company is skipped (`--skip-license-insertion-comment`), whatever header
//! it carries. A hook that inserted one edition's header into every file, or
//! inserted a header above an existing one, would put a second, contradicting
//! header into each file it touched.
//!
//! The Enterprise header's exact wording is pinned in the Enterprise
//! workspace (`enterprise/tests/licence_header.rs`).

use std::fs;
use std::process::Command;

#[path = "support/roots.rs"]
mod roots;

const CONFIG: &str = ".pre-commit-config.yaml";
/// The header a Community file gets, exactly.
const COMMUNITY_HEADER: &str =
    "Copyright 2026 Brutor AI Ltd.\nSPDX-License-Identifier: Apache-2.0\n";
const COMMUNITY_HEADER_FILE: &str = "license-header.txt";
const ENTERPRISE_HEADER_FILE: &str = "enterprise/license-header.txt";
/// What every header of either edition, before and after the cut, names.
const COMPANY: &str = "Brutor AI Ltd.";

/// One `insert-license` entry of the config: its `files` and `exclude`
/// patterns and its arguments.
#[derive(Debug, Default)]
struct Entry {
    files: String,
    exclude: Option<String>,
    args: Vec<String>,
}

impl Entry {
    fn arg(&self, flag: &str) -> Option<String> {
        let at = self.args.iter().position(|a| a == flag)?;
        self.args.get(at + 1).cloned()
    }

    fn applies_to(&self, path: &str) -> bool {
        let files = regex_lite::Regex::new(&self.files).expect("files is a pattern");
        let excluded = self
            .exclude
            .as_ref()
            .is_some_and(|e| regex_lite::Regex::new(e).expect("exclude").is_match(path));
        files.is_match(path) && !excluded
    }
}

/// The `insert-license` entries of `config`, read line by line: the file is
/// pre-commit's YAML, written one key per line.
fn entries(config: &str) -> Vec<Entry> {
    let mut out: Vec<Entry> = Vec::new();
    let mut inside = false;
    let mut in_args = false;
    for line in config.lines() {
        let trimmed = line.trim();
        if let Some(id) = trimmed.strip_prefix("- id:") {
            inside = id.trim() == "insert-license";
            in_args = false;
            if inside {
                out.push(Entry::default());
            }
            continue;
        }
        if trimmed.starts_with("- repo:") {
            inside = false;
            continue;
        }
        if !inside {
            continue;
        }
        let entry = out.last_mut().expect("an entry");
        if let Some(v) = trimmed.strip_prefix("files:") {
            entry.files = v.trim().to_string();
            in_args = false;
        } else if let Some(v) = trimmed.strip_prefix("exclude:") {
            entry.exclude = Some(v.trim().to_string());
            in_args = false;
        } else if trimmed == "args:" {
            in_args = true;
        } else if in_args && let Some(v) = trimmed.strip_prefix("- ") {
            // A quoted scalar is its text.
            entry.args.push(v.trim().trim_matches('"').to_string());
        } else if !trimmed.is_empty() {
            in_args = false;
        }
    }
    out
}

fn tracked_rust_files() -> Vec<String> {
    let out = Command::new("git")
        .args(["ls-files", "-z", "--", "*.rs"])
        .output()
        .expect("git ls-files");
    assert!(out.status.success(), "git ls-files failed");
    String::from_utf8_lossy(&out.stdout)
        .split('\0')
        .filter(|p| !p.is_empty())
        .map(str::to_string)
        .collect()
}

#[test]
fn each_edition_gets_its_own_header_and_no_header_is_fought() {
    let config = fs::read_to_string(CONFIG).expect("the pre-commit config");
    let entries = entries(&config);
    assert_eq!(
        entries.len(),
        2,
        "one insert-license entry per edition: {entries:#?}"
    );
    assert!(
        !config.contains("LICENSE_HEADER.txt"),
        "the hook still inserts LICENSE_HEADER.txt, the header of neither edition"
    );

    let header_of = |e: &Entry| e.arg("--license-filepath").expect("--license-filepath");
    let community = entries
        .iter()
        .find(|e| header_of(e) == COMMUNITY_HEADER_FILE)
        .expect("an entry that inserts the Community header");
    let enterprise = entries
        .iter()
        .find(|e| header_of(e) == ENTERPRISE_HEADER_FILE)
        .expect("an entry that inserts the Enterprise header");

    // The Community header, word for word.
    assert_eq!(
        fs::read_to_string(COMMUNITY_HEADER_FILE).expect("the Community header"),
        COMMUNITY_HEADER
    );
    if let Some(enterprise_root) = roots::tree("enterprise") {
        let theirs = fs::read_to_string(enterprise_root.join("license-header.txt"))
            .expect("the Enterprise header");
        assert!(
            theirs.starts_with("Copyright 2026 Brutor AI Ltd."),
            "{theirs}"
        );
        assert_ne!(theirs, COMMUNITY_HEADER);
    }

    // Every Rust file of the repository is one edition's: exactly one entry
    // applies to it, and it is the entry of the file's edition.
    let files = tracked_rust_files();
    assert!(files.len() > 500, "{} Rust files", files.len());
    for file in &files {
        let is_enterprise = file.starts_with("enterprise/");
        assert_eq!(community.applies_to(file), !is_enterprise, "{file}");
        assert_eq!(enterprise.applies_to(file), is_enterprise, "{file}");
    }

    // Neither entry fights a header: a file whose first lines name the
    // company is skipped, and those lines reach every header there is.
    for entry in [community, enterprise] {
        assert_eq!(
            entry.arg("--skip-license-insertion-comment").as_deref(),
            Some(COMPANY),
            "{entry:#?}"
        );
        assert_eq!(entry.arg("--comment-style").as_deref(), Some("//"));
    }
    let top: usize = community
        .arg("--detect-license-in-X-top-lines")
        .and_then(|n| n.parse().ok())
        .expect("--detect-license-in-X-top-lines");
    assert_eq!(
        enterprise.arg("--detect-license-in-X-top-lines"),
        Some(top.to_string())
    );
    let mut out_of_reach = Vec::new();
    for file in &files {
        let text = fs::read_to_string(file).unwrap_or_default();
        if let Some(at) = text.lines().position(|l| l.contains(COMPANY))
            && at >= top
        {
            out_of_reach.push(format!("{file}: line {}", at + 1));
        }
    }
    assert!(
        out_of_reach.is_empty(),
        "a header below the first {top} lines: the hook would insert a second header above \
         it:\n{}",
        out_of_reach.join("\n")
    );
}

/// The reading of the config is proven on the shape it replaced: one entry,
/// every Rust file, the header of neither edition.
#[test]
fn the_reading_of_the_config_finds_the_entries() {
    let old = "repos:\n  - repo: https://github.com/Lucas-C/pre-commit-hooks\n    rev: v1.5.5\n    \
               hooks:\n      - id: insert-license\n        files: \\.rs$\n        args:\n          \
               - --license-filepath\n          - LICENSE_HEADER.txt\n          - --comment-style\n          \
               - //\n";
    let found = entries(old);
    assert_eq!(found.len(), 1);
    assert_eq!(found[0].files, "\\.rs$");
    assert_eq!(
        found[0].arg("--license-filepath").as_deref(),
        Some("LICENSE_HEADER.txt")
    );
    assert!(found[0].applies_to("enterprise/src/lib.rs") && found[0].applies_to("src/lib.rs"));
    assert!(found[0].arg("--skip-license-insertion-comment").is_none());
}
