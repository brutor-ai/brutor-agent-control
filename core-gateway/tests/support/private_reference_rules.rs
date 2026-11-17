// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! What this repository declares for the scan of references to records a
//! reader cannot open (`tests/common/private_references.rs`, the same file
//! in every Rust repository of the product), and the check itself. Shared
//! by the guard of the Community workspace and the guard of the Enterprise
//! workspace, which include it by path.
//!
//! Four lists, each entry with its reason, and no blanket exemption:
//!
//! * [`NOT_SCANNED`]: files the scan does not read.
//! * [`PUBLIC_HERE`]: public standards and public names this repository
//!   cites that the shared list does not carry.
//! * [`SECTIONS_OF_A_PUBLIC_DOCUMENT`]: sources that implement one
//!   published specification, where a bare section sign cites it.
//! * [`NEEDED_IN_ONE_FILE`]: the one place that says where a file kept
//!   outside this repository is.
//! * [`NAMED_BY_THE_ENTERPRISE_WORKSPACE`]: the private repositories the
//!   Enterprise workspace names (the shared scanner reports every private
//!   repository by name).
//!
//! And the shapes this repository's scan adds to the shared scanner's
//! ([`more_offences`]): an engineering-assurance id `T-01`, a decision id
//! `Q14`, and, in the Community tree, an address at the company's domain.
//! The shared file is the same in every Rust repository, so what one
//! repository adds is added here.
//!
//! Paths are relative to the Community root.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::OnceLock;

use regex_lite::Regex;

use super::private_references;

/// Files the scan does not read.
pub const NOT_SCANNED: &[(&str, &str)] = &[
    (
        "tests/common/private_references.rs",
        "the scanner spells out what it looks for",
    ),
    (
        "tests/support/private_reference_rules.rs",
        "these lists spell out what they declare",
    ),
    (
        "tests/private_references_guard.rs",
        "the guard's own tests spell out what a report line looks like",
    ),
    (
        "CLAUDE.md",
        "a private guide for contributors; it is not part of the published tree",
    ),
    (
        "THIRD_PARTY_LICENSES",
        "generated: third-party licence texts and crate names (pem-rfc7468, rfc6979), quoted as \
         their authors wrote them",
    ),
    (
        "enterprise/THIRD_PARTY_LICENSES",
        "generated: third-party licence texts and crate names (pem-rfc7468, rfc6979), quoted as \
         their authors wrote them",
    ),
    (
        "tests/fixtures/rfc3161/",
        "certificates and timestamp tokens: encoded data, in which any run of characters occurs",
    ),
    (
        "crates/brutor-aac/vectors/capsule/",
        "conformance vectors of the public agent-action-capsule project, kept unchanged and \
         checked against its checksums; they cite sections of its Internet-Draft",
    ),
    (
        "crates/brutor-aac/vectors/disclosure-envelope/",
        "conformance vectors of the public agent-action-capsule project, kept unchanged and \
         checked against its checksums; they cite sections of its Internet-Draft",
    ),
    (
        "crates/brutor-aac/vectors/registry/",
        "the registry text of the public agent-action-capsule project, kept unchanged",
    ),
];

/// Public things this repository cites: `(kind as the scanner reports it,
/// cited text, what it is)`. An RFC is matched by its number, whatever the
/// spelling or the section after it; a term by its exact text.
pub const PUBLIC_HERE: &[(&str, &str, &str)] = &[
    (
        "internal RFC",
        "RFC 2104",
        "HMAC: Keyed-Hashing for Message Authentication",
    ),
    (
        "internal RFC",
        "RFC 3161",
        "Internet X.509 Public Key Infrastructure Time-Stamp Protocol (TSP)",
    ),
    (
        "internal RFC",
        "RFC 4231",
        "Identifiers and Test Vectors for HMAC-SHA-224, HMAC-SHA-256, HMAC-SHA-384, and \
         HMAC-SHA-512",
    ),
    ("internal RFC", "RFC 5322", "Internet Message Format"),
    (
        "internal RFC",
        "RFC 5737",
        "IPv4 Address Blocks Reserved for Documentation",
    ),
    (
        "internal RFC",
        "RFC 6266",
        "Use of the Content-Disposition Header Field in the Hypertext Transfer Protocol (HTTP)",
    ),
    ("internal RFC", "RFC 6585", "Additional HTTP Status Codes"),
    ("internal RFC", "RFC 6962", "Certificate Transparency"),
    (
        "internal RFC",
        "RFC 7231",
        "Hypertext Transfer Protocol (HTTP/1.1): Semantics and Content",
    ),
    (
        "internal RFC",
        "RFC 7235",
        "Hypertext Transfer Protocol (HTTP/1.1): Authentication",
    ),
    ("internal RFC", "RFC 7239", "Forwarded HTTP Extension"),
    (
        "internal RFC",
        "RFC 8037",
        "CFRG Elliptic Curve Diffie-Hellman (ECDH) and Signatures in JSON Object Signing and \
         Encryption (JOSE)",
    ),
    (
        "internal RFC",
        "RFC 8949",
        "Concise Binary Object Representation (CBOR)",
    ),
    (
        "internal RFC",
        "RFC 9052",
        "CBOR Object Signing and Encryption (COSE): Structures and Process",
    ),
    (
        "internal RFC",
        "RFC 9162",
        "Certificate Transparency Version 2.0",
    ),
    (
        "internal RFC",
        "RFC 9597",
        "CBOR Web Token (CWT) Claims in COSE Headers",
    ),
    (
        "internal RFC",
        "RFC 9942",
        "CBOR Object Signing and Encryption (COSE) Receipts",
    ),
    (
        "internal RFC",
        "RFC 9943",
        "An Architecture for Trustworthy and Transparent Digital Supply Chains",
    ),
    ("internal RFC", "RFC 9995", "COSE Hash Envelope"),
    (
        "plan step or decision id",
        "P50",
        "the 50th percentile of a measurement",
    ),
    (
        "plan step or decision id",
        "P95",
        "the 95th percentile of a measurement",
    ),
    (
        "plan step or decision id",
        "P99",
        "the 99th percentile of a measurement",
    ),
    (
        "history note",
        "2026-07-28",
        "Model Context Protocol specification revision",
    ),
];

/// Source directories (or one file) that implement one published
/// specification: a bare section sign there cites that specification, which
/// the source names. `(directory or file, the specification)`. Only the kind
/// "section of an outside document" is let through, and only there.
pub const SECTIONS_OF_A_PUBLIC_DOCUMENT: &[(&str, &str)] = &[
    (
        "crates/brutor-aac/src/",
        "draft-mih-scitt-agent-action-capsule-04, the Internet-Draft the crate implements; the \
         verifier's messages carry the section a check comes from, as the reference verifier's do",
    ),
    (
        "src/services/a2a_v1/",
        "the A2A specification, version 1.0, a copy of which is in the directory's spec/",
    ),
    (
        "tests/a2a_v1_conformance.rs",
        "the conformance test of the same A2A specification, which names the section each \
         case comes from",
    ),
];

/// A reference one file must carry to do its work: `(path, kind, cited
/// text, why)`. Each may occur there once, and nowhere else. No file needs
/// one today: a tree kept outside this repository is named by an
/// environment variable (`tests/support/roots.rs`), never by a path.
pub const NEEDED_IN_ONE_FILE: &[(&str, &str, &str, &str)] = &[];

/// Private repositories the Enterprise workspace (`enterprise/`, private
/// itself) names, `(name, reason)`. The Community tree names none.
pub const NAMED_BY_THE_ENTERPRISE_WORKSPACE: &[(&str, &str)] = &[(
    "brutor-license",
    "the licence library the Enterprise workspace depends on: its manifest, deny.toml, \
     Dockerfile and README name it",
)];

/// A pattern of [`more_offences`]: what it finds (group 2, or the whole
/// match where it has no groups), the kind it reports, whether it applies to
/// the Enterprise workspace too, and whether the cited text must stand
/// alone (not glued to a word or a hyphen after it, not quoted).
struct Shape {
    pattern: Regex,
    kind: &'static str,
    both_trees: bool,
    an_id: bool,
}

fn shapes() -> &'static [Shape] {
    static SHAPES: OnceLock<Vec<Shape>> = OnceLock::new();
    SHAPES.get_or_init(|| {
        let id = |p: &str| Regex::new(&format!("(^|[^A-Za-z0-9_#./-])({p})")).unwrap();
        vec![
            Shape {
                pattern: id("T-[0-9]{2}"),
                kind: "engineering-assurance id",
                both_trees: true,
                an_id: true,
            },
            Shape {
                pattern: id("Q[0-9]{1,2}"),
                kind: "plan step or decision id",
                both_trees: true,
                an_id: true,
            },
            Shape {
                pattern: Regex::new(r"(?i)[a-z0-9._%+-]+@brutor\.ai\b").unwrap(),
                kind: "company address",
                both_trees: false,
                an_id: false,
            },
        ]
    })
}

/// What this repository's scan finds in `text` beyond the shared scanner,
/// as `(line, kind, cited)`. `community`: the text is a file of the
/// Community tree, not of the Enterprise workspace.
///
/// * `T-01`: an engineering-assurance id (the shared scanner knows `D-`,
///   `E-` and `S-`), in both trees.
/// * `Q14`: a decision id, in both trees. `Q1` to `Q4` are calendar
///   quarters (`Q2 2026`, `{"period": "Q2"}`) and are not reported.
/// * In the Community tree only: an address at the company's domain (the
///   Enterprise trial message names one).
///
/// An id glued to what follows (`T-01-x`, `Q14b`) or quoted (`"T-01"`) is
/// data, as in the shared scanner.
pub fn more_offences(text: &str, community: bool) -> Vec<(usize, &'static str, String)> {
    let mut found = Vec::new();
    for (n, line) in text.lines().enumerate() {
        for shape in shapes() {
            if !shape.both_trees && !community {
                continue;
            }
            for caps in shape.pattern.captures_iter(line) {
                let cited = caps.get(2).unwrap_or_else(|| caps.get(0).unwrap());
                if shape.an_id {
                    let before = line[..cited.start()].chars().next_back();
                    let after = line[cited.end()..].chars().next();
                    let glued =
                        after.is_some_and(|c| c.is_ascii_alphanumeric() || "_-.".contains(c));
                    let quoted = before.is_some_and(|c| "\"'".contains(c))
                        && after.is_some_and(|c| "\"'".contains(c));
                    let quarter = cited.as_str().strip_prefix('Q').is_some_and(|number| {
                        number.parse::<u32>().is_ok_and(|q| (1..=4).contains(&q))
                    });
                    if glued || quoted || quarter {
                        continue;
                    }
                }
                found.push((n + 1, shape.kind, cited.as_str().to_string()));
            }
        }
    }
    found
}

/// Directories never entered when the file list comes from the disk
/// instead of from git (as in the shared scanner).
const UNTRACKED_DIRS: &[&str] = &[
    ".git",
    "target",
    "node_modules",
    "dist",
    "coverage",
    "keys",
    ".venv",
    "__pycache__",
];
const LOCK_FILES: &[&str] = &["Cargo.lock", "package-lock.json", "uv.lock", "poetry.lock"];

/// The tracked files under `root`, relative to it; the disk without git.
fn files_of(root: &Path) -> Vec<String> {
    let listed = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(["ls-files", "-z"])
        .output()
        .ok()
        .filter(|out| out.status.success() && !out.stdout.is_empty());
    if let Some(out) = listed {
        return String::from_utf8_lossy(&out.stdout)
            .split('\0')
            .filter(|p| !p.is_empty())
            .map(str::to_string)
            .collect();
    }
    fn walk(root: &Path, dir: &Path, out: &mut Vec<String>) {
        let Ok(entries) = std::fs::read_dir(dir) else {
            return;
        };
        for entry in entries.flatten() {
            let path: PathBuf = entry.path();
            let name = entry.file_name().to_string_lossy().to_string();
            if path.is_dir() {
                if !UNTRACKED_DIRS.contains(&name.as_str()) {
                    walk(root, &path, out);
                }
            } else if let Ok(relative) = path.strip_prefix(root) {
                out.push(relative.to_string_lossy().replace('\\', "/"));
            }
        }
    }
    let mut out = Vec::new();
    walk(root, root, &mut out);
    out
}

/// [`more_offences`] over the files the shared scan reads, as report lines
/// (`file:line: kind: cited`). `prefix` as in [`undeclared`].
fn more_report(root: &Path, left_out: &[&str], prefix: &str) -> Vec<String> {
    let mut files = files_of(root);
    files.sort();
    let mut report = Vec::new();
    for name in files {
        let base = name.rsplit('/').next().unwrap_or(&name);
        if LOCK_FILES.contains(&base)
            || name.starts_with("keys/")
            || left_out
                .iter()
                .any(|l| name == *l || (l.ends_with('/') && name.starts_with(l)))
        {
            continue;
        }
        let Ok(bytes) = std::fs::read(root.join(&name)) else {
            continue;
        };
        if bytes.contains(&0) {
            continue;
        }
        let Ok(text) = String::from_utf8(bytes) else {
            continue;
        };
        let community = !format!("{prefix}{name}").starts_with("enterprise/");
        for (line, kind, cited) in more_offences(&text, community) {
            report.push(format!("{name}:{line}: {kind}: {cited}"));
        }
    }
    report
}

fn number_of(cited: &str) -> Option<u32> {
    let digits: String = cited
        .chars()
        .skip_while(|c| !c.is_ascii_digit())
        .take_while(char::is_ascii_digit)
        .collect();
    digits.parse().ok()
}

fn declared_public(kind: &str, cited: &str) -> bool {
    PUBLIC_HERE.iter().any(|(k, text, _)| {
        *k == kind
            && if kind == "internal RFC" {
                number_of(text).is_some() && number_of(text) == number_of(cited)
            } else {
                *text == cited
            }
    })
}

/// One line of the scanner's report, `file:line: kind: cited`, in parts.
fn parts(line: &str) -> Option<(&str, &str, &str)> {
    let (file, rest) = line.split_once(':')?;
    let (_, rest) = rest.split_once(": ")?;
    let (kind, cited) = rest.split_once(": ")?;
    Some((file, kind, cited))
}

/// What is left of `report` after the declarations: the lines that fail the
/// guard. `prefix` is what turns a path of the scanned tree into a path from
/// the Community root (`""`, or `"enterprise/"`).
pub fn undeclared(report: &[String], prefix: &str) -> Vec<String> {
    let mut failures = Vec::new();
    let mut needed = vec![0usize; NEEDED_IN_ONE_FILE.len()];
    for line in report {
        let Some((file, kind, cited)) = parts(line) else {
            failures.push(line.clone());
            continue;
        };
        let path = format!("{prefix}{file}");
        if declared_public(kind, cited) {
            continue;
        }
        if let Some(at) = NEEDED_IN_ONE_FILE
            .iter()
            .position(|(p, k, text, _)| *p == path && *k == kind && *text == cited)
        {
            needed[at] += 1;
            if needed[at] == 1 {
                continue;
            }
        }
        if kind == "private repository or record"
            && path.starts_with("enterprise/")
            && NAMED_BY_THE_ENTERPRISE_WORKSPACE
                .iter()
                .any(|(name, _)| cited.eq_ignore_ascii_case(name))
        {
            continue;
        }
        if kind == "section of an outside document"
            && SECTIONS_OF_A_PUBLIC_DOCUMENT
                .iter()
                .any(|(directory, _)| path.starts_with(directory))
        {
            continue;
        }
        failures.push(format!("{prefix}{line}"));
    }
    failures
}

/// Scan the tree at `root` and return the failures and the number of files
/// read. `prefix` as in [`undeclared`].
pub fn check(root: &Path, prefix: &str) -> (Vec<String>, usize) {
    let left_out: Vec<&str> = NOT_SCANNED
        .iter()
        .filter_map(|(path, _)| path.strip_prefix(prefix))
        .collect();
    let (mut report, read) = private_references::scan(root, &left_out, &[]);
    report.extend(more_report(root, &left_out, prefix));
    (undeclared(&report, prefix), read)
}

/// The declarations are well formed: every entry has a reason, and nothing
/// the shared list already allows is declared again.
pub fn check_the_declarations() {
    for (path, reason) in NOT_SCANNED {
        assert!(reason.len() > 12, "{path}: no reason");
    }
    for (kind, text, what) in PUBLIC_HERE {
        assert!(what.len() > 8, "{text}: no description");
        match *kind {
            "internal RFC" => {
                let number = number_of(text).unwrap_or_else(|| panic!("{text}: no RFC number"));
                assert!(
                    !private_references::PUBLIC_RFCS
                        .iter()
                        .any(|(n, _)| *n == number),
                    "RFC {number} is in the shared list already"
                );
            }
            _ => assert!(
                !private_references::PUBLIC_TERMS
                    .iter()
                    .any(|(t, _)| t == text),
                "{text} is in the shared list already"
            ),
        }
        // A declared item is one the scanner would otherwise report. A date
        // is reported only where a word or a bracket comes before it.
        let shown = if *kind == "history note" {
            format!("revision {text}")
        } else {
            (*text).to_string()
        };
        let found = private_references::offences(&shown);
        assert!(
            found.iter().any(|o| o.kind == *kind),
            "{text}: the scanner does not report it as {kind:?}, so it needs no declaration"
        );
    }
    for (directory, document) in SECTIONS_OF_A_PUBLIC_DOCUMENT {
        assert!(
            directory.ends_with('/') || directory.ends_with(".rs"),
            "{directory}: neither a directory nor a source file"
        );
        assert!(document.len() > 20, "{directory}: no document");
    }
    for (path, _, text, why) in NEEDED_IN_ONE_FILE {
        assert!(why.len() > 20, "{path}: {text}: no reason");
    }
    for (name, reason) in NAMED_BY_THE_ENTERPRISE_WORKSPACE {
        assert!(reason.len() > 20, "{name}: no reason");
        assert!(
            private_references::PRIVATE_REPOSITORIES.contains(name),
            "{name}: the shared scanner does not report it, so it needs no declaration"
        );
    }
}
