// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Nothing in this repository points at a record its reader cannot open.
//!
//! Design documents, review findings, plan steps and decision records are
//! kept outside the repositories. A comment, a test name or a message that
//! cites one by number tells the reader nothing, so the rule or the reason
//! is written out where it applies and the citation is not. Published
//! standards are different: anyone can look them up, and the ones cited
//! here are listed below with their titles.
//!
//! This file is the same in every Rust repository of the product. It reads
//! every tracked text file, comments included, as one stream: a line break
//! and the comment marker after it count as one space, so a citation split
//! over two lines is found. Letter case and the punctuation between the
//! word and the number make no difference.
//!
//! A repository whose reader can open some of the private repositories
//! (a private repository itself) names them, each with the reason, in the
//! `declared` list it passes to [`scan`]. The rules and the lists in this
//! file do not change.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

/// Published RFCs that may be cited, with their titles. Any other RFC
/// number is taken for an internal document.
pub const PUBLIC_RFCS: &[(u32, &str)] = &[
    (1918, "Address Allocation for Private Internets"),
    (
        2119,
        "Key words for use in RFCs to Indicate Requirement Levels",
    ),
    (3339, "Date and Time on the Internet: Timestamps"),
    (3986, "Uniform Resource Identifier (URI): Generic Syntax"),
    (4122, "A Universally Unique IDentifier (UUID) URN Namespace"),
    (4648, "The Base16, Base32, and Base64 Data Encodings"),
    (
        5280,
        "Internet X.509 Public Key Infrastructure Certificate and CRL Profile",
    ),
    (6238, "TOTP: Time-Based One-Time Password Algorithm"),
    (6749, "The OAuth 2.0 Authorization Framework"),
    (
        6750,
        "The OAuth 2.0 Authorization Framework: Bearer Token Usage",
    ),
    (6890, "Special-Purpose IP Address Registries"),
    (7009, "OAuth 2.0 Token Revocation"),
    (7515, "JSON Web Signature (JWS)"),
    (7517, "JSON Web Key (JWK)"),
    (7518, "JSON Web Algorithms (JWA)"),
    (7519, "JSON Web Token (JWT)"),
    (7591, "OAuth 2.0 Dynamic Client Registration Protocol"),
    (7636, "Proof Key for Code Exchange by OAuth Public Clients"),
    (7662, "OAuth 2.0 Token Introspection"),
    (8032, "Edwards-Curve Digital Signature Algorithm (EdDSA)"),
    (
        8259,
        "The JavaScript Object Notation (JSON) Data Interchange Format",
    ),
    (8414, "OAuth 2.0 Authorization Server Metadata"),
    (
        8446,
        "The Transport Layer Security (TLS) Protocol Version 1.3",
    ),
    (8693, "OAuth 2.0 Token Exchange"),
    (8707, "Resource Indicators for OAuth 2.0"),
    (8785, "JSON Canonicalization Scheme (JCS)"),
    (
        9068,
        "JSON Web Token (JWT) Profile for OAuth 2.0 Access Tokens",
    ),
    (9110, "HTTP Semantics"),
    (9207, "OAuth 2.0 Authorization Server Issuer Identification"),
    (9457, "Problem Details for HTTP APIs"),
    (9562, "Universally Unique IDentifiers (UUIDs)"),
    (9728, "OAuth 2.0 Protected Resource Metadata"),
];

/// A capital letter and a number that name a public thing, not a plan step.
pub const PUBLIC_TERMS: &[(&str, &str)] = &[
    ("S3", "Amazon S3 object storage and its API"),
    ("R2", "Cloudflare R2 object storage"),
    ("A2", "the first half of A2A, the Agent2Agent protocol"),
];

/// Dates that are the published version of a public interface.
pub const PUBLIC_DATES: &[(&str, &str)] = &[
    (
        "2024-11-05",
        "Model Context Protocol specification revision",
    ),
    (
        "2025-03-26",
        "Model Context Protocol specification revision",
    ),
    (
        "2025-06-18",
        "Model Context Protocol specification revision",
    ),
    (
        "2025-11-25",
        "Model Context Protocol specification revision",
    ),
    ("2022-11-28", "GitHub REST API version"),
    ("2022-06-28", "Notion API version"),
];

/// Repositories of the product that are not published, by name: a reader
/// of a published one cannot open them. Matched as whole names in any
/// letter case. The list of the tooling that cuts the published trees, and
/// the licence library.
pub const PRIVATE_REPOSITORIES: &[&str] = &[
    "brutor-anthropic-enterprise-mock",
    "brutor-cli",
    "brutor-discovery-sdk",
    "brutor-endpoint-collector",
    "brutor-gateway-kb-uploader",
    "brutor-gateway-portal",
    "brutor-gateway-test",
    "brutor-kb-connector-sync",
    "brutor-license",
    "brutor-mcp-images",
    "brutor-mcp-registry",
    "brutor-rfcs",
    "brutor-saas-infra",
    "brutor-scripts",
    "brutor-trial-service",
    "brutor-trial-signup",
    "brutor-user-portal",
];

/// A home directory names the machine and the person a file was written
/// on. `/Users/<name>/` always; `/home/<name>/` unless the name is one of
/// these, each with what it is.
const GENERIC_HOMES: &[(&str, &str)] = &[(
    "brutor",
    "the non-root user the product's container images run as: a path inside the image",
)];

/// The kind a private repository is reported as.
const PRIVATE_REPOSITORY: &str = "private repository or record";

/// Paths and names that lead into a private record.
const PRIVATE_PATHS: &[&str] = &[
    "impl/rfc",
    "docs/plans/",
    "rename-list",
    "env-registry.md",
    "impl contracts",
    "implementation contracts",
    "implementation-contracts",
    "engineering-assurance",
    "engineering assurance",
];

/// Prefixes of numbered records kept outside the repositories, with the
/// fewest digits that make one: decisions, engineering-assurance items and
/// the work items of each component.
const TRACKED_PREFIXES: &[(&str, usize, &str)] = &[
    ("D-", 1, "plan step or decision id"),
    ("E-", 2, "engineering-assurance id"),
    ("S-", 2, "engineering-assurance id"),
    ("INV-", 1, "engineering-assurance id"),
    ("CTL-", 1, "engineering-assurance id"),
    ("CHG-", 1, "engineering-assurance id"),
    ("OVR-", 1, "engineering-assurance id"),
    ("PERF-", 1, "engineering-assurance id"),
    ("CORE-", 1, "engineering-assurance id"),
    ("ADMIN-", 1, "engineering-assurance id"),
    ("PORTAL-", 1, "engineering-assurance id"),
    ("CONTROL-", 1, "engineering-assurance id"),
    ("KBCONN-", 1, "engineering-assurance id"),
];

/// Words that, before `#12`, make it an entry of an internal work list.
const WORK_ITEMS: &[&str] = &["task", "regression", "bug", "defect", "ticket"];

/// Wording that tells how a rule came about instead of what it is.
const HISTORY_PHRASES: &[&str] = &[
    "owner's decision",
    "owner's instruction",
    "owner's ruling",
    "owner decided",
    "(was:",
];

/// A decision cited instead of the rule it made: what the owner decided
/// (`owner decision L1`, `owner ruling`), a numbered answer (`legal answer
/// 5`), the day it was made (`decided on 2026-11-16`) and a numbered
/// decision (`decision 11`, `Decision Q12`). The words the owner spoke in:
/// each followed by an id (a number, or up to six capitals and a number) or
/// not.
const OWNER_WORDS: &[&str] = &["decision", "ruling", "instruction", "answer"];
const DATE_WORDS: &[&str] = &["on", "in", "of", "from"];

/// Files that are not prose or source of this repository: resolved
/// dependency lists. Their hashes look like anything.
const LOCK_FILES: &[&str] = &["Cargo.lock", "package-lock.json", "uv.lock", "poetry.lock"];

/// Directories never entered when the file list comes from the disk
/// instead of from git.
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

/// One citation: the line it starts on, its kind and the cited text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Offence {
    pub line: usize,
    pub kind: &'static str,
    pub cited: String,
}

impl Offence {
    pub fn what(&self) -> String {
        format!("{}: {}", self.kind, self.cited)
    }
}

/// The text as one stream of characters, each with the line it came from.
struct Stream {
    chars: Vec<char>,
    lines: Vec<usize>,
}

impl Stream {
    fn new(text: &str) -> Self {
        let mut chars = Vec::with_capacity(text.len());
        let mut lines = Vec::with_capacity(text.len());
        for (index, raw) in text.lines().enumerate() {
            let mut line = raw.trim();
            // The comment marker a continued line starts with is not text.
            loop {
                let before = line;
                for marker in ["//!", "///", "//", "/*", "*/", "<!--", "*", "#", ">", "|"] {
                    if let Some(rest) = line.strip_prefix(marker) {
                        line = rest.trim_start();
                        break;
                    }
                }
                if line == before {
                    break;
                }
            }
            let mut last_was_space = true;
            for c in line.chars() {
                let space = c.is_whitespace();
                if space && last_was_space {
                    continue;
                }
                chars.push(if space { ' ' } else { c });
                lines.push(index + 1);
                last_was_space = space;
            }
            if !last_was_space {
                chars.push(' ');
                lines.push(index + 1);
            }
        }
        Stream { chars, lines }
    }

    fn at(&self, i: usize) -> char {
        self.chars.get(i).copied().unwrap_or(' ')
    }

    fn before(&self, i: usize) -> char {
        i.checked_sub(1).map_or(' ', |j| self.chars[j])
    }

    /// `word` at `i`, in any letter case.
    fn has(&self, i: usize, word: &str) -> bool {
        word.chars()
            .enumerate()
            .all(|(k, w)| self.at(i + k).to_ascii_lowercase() == w)
    }

    /// The index after the digits that start at `i`.
    fn digits(&self, i: usize) -> usize {
        let mut j = i;
        while self.at(j).is_ascii_digit() {
            j += 1;
        }
        j
    }

    /// The index after any of `separators` that start at `i`.
    fn skip(&self, i: usize, separators: &str) -> usize {
        let mut j = i;
        while j < self.chars.len() && separators.contains(self.at(j)) {
            j += 1;
        }
        j
    }

    fn text(&self, from: usize, to: usize) -> String {
        self.chars[from..to.min(self.chars.len())]
            .iter()
            .collect::<String>()
            .trim()
            .trim_end_matches('.')
            .to_string()
    }

    fn offence(&self, from: usize, kind: &'static str, to: usize) -> Offence {
        Offence {
            line: self.lines[from],
            kind,
            cited: self.text(from, to),
        }
    }

    /// A word starts at `i`: the character before is not a letter, or it
    /// is the lower-case end of the previous word in a camelCase name.
    fn word_starts(&self, i: usize) -> bool {
        let before = self.before(i);
        !before.is_ascii_alphabetic()
            || (before.is_ascii_lowercase() && self.at(i).is_ascii_uppercase())
    }

    /// An identifier-like token occupies `from..to` on its own.
    fn stands_alone(&self, from: usize, to: usize) -> bool {
        let before = self.before(from);
        let after = self.at(to);
        let glued_before = is_word(before) || "-#./".contains(before);
        let glued_after = is_word(after) || (".-".contains(after) && is_word(self.at(to + 1)));
        !glued_before && !glued_after
    }

    /// A number follows the token, as in the path data of a drawing
    /// (`M12 9v2`): coordinates, not a citation.
    fn before_a_number(&self, to: usize) -> bool {
        let next = self.at(to + 1);
        " ,".contains(self.at(to))
            && (next.is_ascii_digit() || ("-.".contains(next) && self.at(to + 2).is_ascii_digit()))
    }

    /// `i` is the first character of its line.
    fn line_starts(&self, i: usize) -> bool {
        i == 0 || self.lines[i - 1] != self.lines[i]
    }

    /// The token is a quoted value standing alone on its line as an item of
    /// a list (`"name",`).
    fn list_item(&self, from: usize, to: usize) -> bool {
        let quote = self.before(from);
        "\"'".contains(quote)
            && self.at(to) == quote
            && self.at(to + 1) == ','
            && from >= 1
            && self.line_starts(from - 1)
            && (to + 3 >= self.chars.len() || self.line_starts(to + 3))
    }

    /// The token is a quoted value: data, not a citation.
    fn quoted(&self, from: usize, to: usize) -> bool {
        "\"'".contains(self.before(from)) && "\"'".contains(self.at(to))
    }
}

fn is_word(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '_'
}

/// The end of the id of a decision after the separators at `i`: a number,
/// or up to six capitals and a number (`L1`, `Q12`, `D-4`).
fn decision_id(s: &Stream, i: usize) -> Option<usize> {
    let j = s.skip(i, " -#");
    let mut k = j;
    if !s.at(j).is_ascii_digit() {
        while k - j < 6 && s.at(k).is_ascii_uppercase() {
            k += 1;
        }
        if k == j {
            return None;
        }
        if s.at(k) == '-' {
            k += 1;
        }
    }
    let end = s.digits(k);
    (end > k && !is_word(s.at(end))).then_some(end)
}

/// The end of a date `2026-11-16` that starts at `i`.
fn date_at(s: &Stream, i: usize) -> Option<usize> {
    let shaped = s.at(i) == '2'
        && s.at(i + 1) == '0'
        && s.digits(i + 2) == i + 4
        && s.at(i + 4) == '-'
        && s.digits(i + 5) == i + 7
        && s.at(i + 7) == '-'
        && s.digits(i + 8) == i + 10
        && !is_word(s.at(i + 10));
    shaped.then_some(i + 10)
}

/// The end of a decision cited at `i`: `owner decision L1`, `legal answer
/// 5`, `decided on 2026-11-16`, `decision 11`.
fn cited_decision(s: &Stream, i: usize) -> Option<usize> {
    if !s.word_starts(i) {
        return None;
    }
    if s.has(i, "owner") {
        let j = s.skip(i + 5, " -");
        if j == i + 5 {
            return None;
        }
        for word in OWNER_WORDS {
            if s.has(j, word) {
                let mut end = j + word.len();
                if s.at(end).eq_ignore_ascii_case(&'s') {
                    end += 1;
                }
                if !s.at(end).is_ascii_alphabetic() {
                    return Some(decision_id(s, end).unwrap_or(end));
                }
            }
        }
        return None;
    }
    if s.has(i, "legal") {
        let j = s.skip(i + 5, " -");
        if j == i + 5 || !s.has(j, "answer") {
            return None;
        }
        let mut k = j + 6;
        if s.at(k).eq_ignore_ascii_case(&'s') {
            k += 1;
        }
        let d = s.skip(k, " -#");
        let end = s.digits(d);
        return (end > d && !is_word(s.at(end))).then_some(end);
    }
    let (word, decision) = if s.has(i, "decided") {
        (7, false)
    } else if s.has(i, "decision") {
        (8, true)
    } else {
        return None;
    };
    // decided on 2026-11-16, decision of 2026-11-17.
    let mut k = i + word;
    if decision && s.at(k).eq_ignore_ascii_case(&'s') {
        k += 1;
    }
    let j = s.skip(k, " -");
    if j > k {
        if let Some(end) = date_at(s, j) {
            return Some(end);
        }
        for preposition in DATE_WORDS {
            let after = j + preposition.len();
            let date = s.skip(after, " -");
            let dated = s.has(j, preposition) && date > after;
            if let Some(end) = dated.then(|| date_at(s, date)).flatten() {
                return Some(end);
            }
        }
    }
    // decision 11, Decision Q12, decisions 5.
    if !decision {
        return None;
    }
    let plural = s.at(i + word).eq_ignore_ascii_case(&'s');
    plural
        .then(|| decision_id(s, i + word + 1))
        .flatten()
        .or_else(|| decision_id(s, i + word))
}

/// Every reference to a private record in `text`.
pub fn offences(text: &str) -> Vec<Offence> {
    let s = Stream::new(text);
    let mut found = Vec::new();
    let mut public_rfc_ended = 0usize;
    let mut decided_until = 0usize;
    let mut i = 0;
    while i < s.chars.len() {
        let c = s.at(i);
        let lower = c.to_ascii_lowercase();

        // RFC 0021, RFC-0021, rfc0021, rfc 21, RFCs 0010; RFC §3.
        if lower == 'r' && s.has(i, "rfc") && s.word_starts(i) {
            let mut j = i + 3;
            if s.at(j).eq_ignore_ascii_case(&'s') {
                j += 1;
            }
            j = s.skip(j, " -_#");
            if s.at(j) == '§' {
                let to = s.skip(j + 1, "0123456789.");
                found.push(s.offence(i, "internal RFC", to));
                i = to;
                continue;
            }
            let end = s.digits(j);
            if end > j && end - j <= 5 && !s.at(end).is_ascii_alphabetic() {
                let number: u32 = s.text(j, end).parse().unwrap_or(0);
                if PUBLIC_RFCS.iter().any(|(n, _)| *n == number) {
                    public_rfc_ended = end;
                    i = end;
                } else {
                    // The section that follows belongs to the same citation.
                    let section = s.skip(end, " ");
                    let to = if s.at(section) == '§' {
                        s.skip(section + 1, "0123456789.")
                    } else {
                        end
                    };
                    found.push(s.offence(i, "internal RFC", to));
                    i = to;
                }
                continue;
            }
        }

        // §4.2: a section of a document that is not in this repository.
        if c == '§' {
            if public_rfc_ended == 0 || i > public_rfc_ended + 2 {
                let to = s.skip(i + 1, "0123456789.");
                found.push(s.offence(i, "section of an outside document", to));
                i = to;
            } else {
                i += 1;
            }
            continue;
        }

        // F245, finding 245.
        if c == 'F' {
            let end = s.digits(i + 1);
            if (2..=4).contains(&(end - i - 1)) && s.stands_alone(i, end) && !s.quoted(i, end) {
                let to = end;
                found.push(s.offence(i, "finding number", to));
                i = to;
                continue;
            }
        }
        if lower == 'f' && s.has(i, "finding") && s.word_starts(i) {
            let mut j = i + 7;
            if s.at(j) == 's' {
                j += 1;
            }
            let j = s.skip(j, " #");
            if s.at(j).is_ascii_digit() {
                let to = s.digits(j);
                found.push(s.offence(i, "finding number", to));
                i = to;
                continue;
            }
        }

        // ADR 0003, adr-3, Assessment 0001.
        for (word, kind) in [("adr", "ADR number"), ("assessment", "assessment number")] {
            if lower == word.chars().next().unwrap() && s.has(i, word) && s.word_starts(i) {
                let j = s.skip(i + word.len(), " -_#");
                if s.at(j).is_ascii_digit() {
                    let to = s.digits(j);
                    found.push(s.offence(i, kind, to));
                    i = to;
                    break;
                }
            }
        }
        if i >= s.chars.len() || s.at(i) != c {
            continue;
        }

        // Phase 3, phase-2, sub-phase 1.4; Sprint 12, sprint #12.
        for (word, separators, kind) in [
            ("phase", " -", "phase of an internal plan"),
            ("sprint", " #", "sprint number"),
        ] {
            if lower == word.chars().next().unwrap() && s.has(i, word) && s.word_starts(i) {
                let j = s.skip(i + word.len(), separators);
                let roman = s.skip(j, "IVX");
                if s.at(j).is_ascii_digit() {
                    found.push(s.offence(i, kind, s.skip(j, "0123456789.")));
                } else if word == "phase" && roman > j && j > i + 5 && !is_word(s.at(roman)) {
                    found.push(s.offence(i, kind, roman));
                }
            }
        }

        if c.is_ascii_uppercase() {
            // P3-11, R4-3: a step of a plan or a numbered decision.
            let first = s.digits(i + 1);
            if (1..=2).contains(&(first - i - 1)) && s.at(first) == '-' {
                let second = s.digits(first + 1);
                if (1..=2).contains(&(second - first - 1))
                    && s.stands_alone(i, second)
                    && !s.quoted(i, second)
                {
                    let to = second;
                    found.push(s.offence(i, "plan step or decision id", to));
                    i = to;
                    continue;
                }
            }
            // S6, M8, R7, K4, D15, A4.
            if "ADKMPRS".contains(c)
                && (1..=2).contains(&(first - i - 1))
                && s.stands_alone(i, first)
                && !s.quoted(i, first)
                && !s.before_a_number(first)
                && !PUBLIC_TERMS.iter().any(|(t, _)| *t == s.text(i, first))
            {
                let to = first;
                found.push(s.offence(i, "plan step or decision id", to));
                i = to;
                continue;
            }
            // D-12; E-03, S-07; INV-12, PORTAL-26, KBCONN-1.
            for (prefix, least, kind) in TRACKED_PREFIXES {
                let from = i + prefix.len();
                let end = s.digits(from);
                if s.text(i, from) == *prefix
                    && (*least..=4).contains(&(end - from))
                    && s.stands_alone(i, end)
                    && !s.quoted(i, end)
                {
                    found.push(s.offence(i, kind, end));
                }
            }
        }

        // task #503, regression #14, REGRESSION (#11), (#4).
        if c == '#' && s.at(i + 1).is_ascii_digit() {
            let end = s.digits(i + 1);
            let mut start = i;
            while start > 0 && " (".contains(s.chars[start - 1]) {
                start -= 1;
            }
            let mut word = start;
            while word > 0 && s.chars[word - 1].is_ascii_alphabetic() {
                word -= 1;
            }
            let named = WORK_ITEMS.contains(&s.text(word, start).to_ascii_lowercase().as_str());
            let bare = s.before(i) == '(' && s.at(end) == ')';
            if named || bare {
                found.push(s.offence(i, "numbered work item", end));
                i = end;
                continue;
            }
        }

        // a193: the nickname of an internal build.
        if c == 'a' {
            let end = s.digits(i + 1);
            if end - i - 1 == 3 && s.stands_alone(i, end) && !s.quoted(i, end) {
                let to = end;
                found.push(s.offence(i, "internal build nickname", to));
                i = to;
                continue;
            }
        }

        // brutor-trial-service: a repository that is not published. A quoted
        // name standing alone on its line as an item of a list (`"name",`)
        // is data: the checks that a shared file is the same in each sibling
        // repository look the siblings up by it.
        if lower == 'b' && !is_word(s.before(i)) && s.before(i) != '-' {
            for name in PRIVATE_REPOSITORIES {
                let to = i + name.len();
                let whole = !is_word(s.at(to)) && !(s.at(to) == '-' && is_word(s.at(to + 1)));
                if s.has(i, name) && whole && !s.list_item(i, to) {
                    found.push(s.offence(i, PRIVATE_REPOSITORY, to));
                }
            }
        }

        // /Users/alice/, /home/bob/: the machine a file was written on.
        if c == '/' && !is_word(s.before(i)) && !".~/-".contains(s.before(i)) {
            for (home, generic) in [("/Users/", false), ("/home/", true)] {
                let from = i + home.len();
                if s.text(i, from) != home || !s.at(from).is_ascii_alphanumeric() {
                    continue;
                }
                let mut end = from;
                while is_word(s.at(end)) || ".-".contains(s.at(end)) {
                    end += 1;
                }
                let name = s.text(from, end);
                if s.at(end) == '/' && !(generic && GENERIC_HOMES.iter().any(|(g, _)| *g == name)) {
                    found.push(s.offence(i, "home directory path", end + 1));
                }
            }
        }

        // A path into a private record; wording that tells history.
        for (phrases, kind) in [
            (PRIVATE_PATHS, "private repository or record"),
            (HISTORY_PHRASES, "history note"),
        ] {
            for phrase in phrases {
                if lower == phrase.chars().next().unwrap() && s.has(i, phrase) {
                    found.push(s.offence(i, kind, i + phrase.chars().count()));
                }
            }
        }

        // "owner decision L1", "legal answer 5", "decided on 2026-11-16",
        // "(decision 11)": a decision cited instead of the rule it made. Its
        // date is part of it.
        let may_cite = i >= decided_until && "old".contains(lower);
        if let Some(to) = may_cite.then(|| cited_decision(&s, i)).flatten() {
            found.push(s.offence(i, "cited decision", to));
            decided_until = to;
        }

        // "(2026-10-26)", "policy 2026-10-05", "since 2026-09-06": the day
        // something was decided or happened. A quoted date, a timestamp and
        // a `name: date` or `--flag date` value are data.
        if c == '2' && s.at(i + 1) == '0' && !is_word(s.before(i)) && i >= decided_until {
            let year = s.digits(i);
            let month = s.digits(year + 1);
            let day = s.digits(month + 1);
            let shaped = year - i == 4
                && s.at(year) == '-'
                && month - year == 3
                && s.at(month) == '-'
                && day - month == 3
                && !is_word(s.at(day))
                && !(s.at(day) == '-' && s.at(day + 1).is_ascii_digit());
            if shaped && !PUBLIC_DATES.iter().any(|(d, _)| *d == s.text(i, day)) {
                let before = s.before(i);
                let mut start = i.saturating_sub(1);
                while start > 0 && s.chars[start - 1] != ' ' {
                    start -= 1;
                }
                let word = s.text(start, i);
                let told = before == '('
                    || (before == ' '
                        && !word.is_empty()
                        && word
                            .chars()
                            .all(|w| w.is_ascii_alphabetic() || w == ',' || w == '('));
                if told {
                    let to = day;
                    found.push(s.offence(i, "history note", to));
                    i = to;
                    continue;
                }
            }
        }

        i += 1;
    }
    found
}

/// Every tracked file of the repository at `root`, as paths relative to it.
/// Without git (a source archive) the disk is walked instead.
fn tracked_files(root: &Path) -> Vec<PathBuf> {
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
            .map(PathBuf::from)
            .collect();
    }
    let mut files = Vec::new();
    walk(root, root, &mut files);
    files
}

fn walk(root: &Path, dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().to_string();
        if path.is_dir() {
            if !UNTRACKED_DIRS.contains(&name.as_str()) {
                walk(root, &path, out);
            }
        } else if let Ok(relative) = path.strip_prefix(root) {
            out.push(relative.to_path_buf());
        }
    }
}

/// The references to private records in the repository at `root`, as
/// `file:line: kind: cited text`, and the number of files read.
///
/// `left_out` names tracked paths (a file, or a directory with a trailing
/// slash) that are not scanned, each for a reason the caller states. Lock
/// files, files that are not text and anything under `keys/` are never read.
/// `declared` names the private repositories this repository's reader can
/// open, `(name, reason)`: they are not reported.
pub fn scan(root: &Path, left_out: &[&str], declared: &[(&str, &str)]) -> (Vec<String>, usize) {
    for (name, reason) in declared {
        assert!(
            PRIVATE_REPOSITORIES.contains(name),
            "{name}: not a private repository"
        );
        assert!(reason.len() > 8, "{name}: no reason");
    }
    let mut files = tracked_files(root);
    files.sort();
    let mut report = Vec::new();
    let mut read = 0;
    for file in files {
        let name = file.to_string_lossy().replace('\\', "/");
        let base = name.rsplit('/').next().unwrap_or(&name);
        if LOCK_FILES.contains(&base)
            || name.starts_with("keys/")
            || left_out
                .iter()
                .any(|l| name == *l || (l.ends_with('/') && name.starts_with(l)))
        {
            continue;
        }
        let Ok(bytes) = fs::read(root.join(&file)) else {
            continue;
        };
        if bytes.contains(&0) {
            continue;
        }
        let Ok(text) = String::from_utf8(bytes) else {
            continue;
        };
        read += 1;
        for offence in offences(&text) {
            let opened = offence.kind == PRIVATE_REPOSITORY
                && declared
                    .iter()
                    .any(|(name, _)| offence.cited.eq_ignore_ascii_case(name));
            if opened {
                continue;
            }
            report.push(format!("{name}:{}: {}", offence.line, offence.what()));
        }
    }
    (report, read)
}

/// The scanner's own examples. Call it from a test.
pub fn check_the_scanner() {
    let kinds = |text: &str| -> Vec<String> {
        offences(text)
            .into_iter()
            .map(|o| format!("{}: {}", o.line, o.what()))
            .collect()
    };

    // Each kind, in the spellings people use.
    assert_eq!(
        kinds(
            "// the notice of RFC 0021 §10\n// see RFC-0019 §5.1, rfc 21, RFC0021 and `rfc-0023`"
        ),
        [
            "1: internal RFC: RFC 0021 §10",
            "2: internal RFC: RFC-0019 §5.1",
            "2: internal RFC: rfc 21",
            "2: internal RFC: RFC0021",
            "2: internal RFC: rfc-0023",
        ]
    );
    assert_eq!(
        kinds("fn rfc0021_notice() {}\nfn noticeRfc0021() {}\nthe RFC §3.1 layout"),
        [
            "1: internal RFC: rfc0021",
            "2: internal RFC: Rfc0021",
            "3: internal RFC: RFC §3.1",
        ]
    );
    assert_eq!(
        kinds("fixed (F1027), see finding F245 and finding 12; findings are listed"),
        [
            "1: finding number: F1027",
            "1: finding number: F245",
            "1: finding number: finding 12",
        ]
    );
    assert_eq!(
        kinds("ADR 0003 and adr-2; Assessment 0001"),
        [
            "1: ADR number: ADR 0003",
            "1: ADR number: adr-2",
            "1: assessment number: Assessment 0001",
        ]
    );
    assert_eq!(
        kinds("Phase 3 K4, P3-11, step S6, M8 and R7; sub-phase 1.4"),
        [
            "1: phase of an internal plan: Phase 3",
            "1: plan step or decision id: K4",
            "1: plan step or decision id: P3-11",
            "1: plan step or decision id: S6",
            "1: plan step or decision id: M8",
            "1: plan step or decision id: R7",
            "1: phase of an internal plan: phase 1.4",
        ]
    );
    assert_eq!(
        kinds("D15, D18 §4, D19, R4-3 and D-12"),
        [
            "1: plan step or decision id: D15",
            "1: plan step or decision id: D18",
            "1: section of an outside document: §4",
            "1: plan step or decision id: D19",
            "1: plan step or decision id: R4-3",
            "1: plan step or decision id: D-12",
        ]
    );
    assert_eq!(
        kinds("built in Sprint #663; isolation work E-03; build a193"),
        [
            "1: sprint number: Sprint #663",
            "1: engineering-assurance id: E-03",
            "1: internal build nickname: a193",
        ]
    );
    assert_eq!(
        kinds("PORTAL-26, KBCONN-1 columns, INV-12; task #503, REGRESSION (#11), Phase I (#2)"),
        [
            "1: engineering-assurance id: PORTAL-26",
            "1: engineering-assurance id: KBCONN-1",
            "1: engineering-assurance id: INV-12",
            "1: numbered work item: #503",
            "1: numbered work item: #11",
            "1: phase of an internal plan: Phase I",
            "1: numbered work item: #2",
        ]
    );
    assert_eq!(
        kinds("see brutor-rfcs/impl/rfc-0029/phase-3-plan.md"),
        [
            "1: private repository or record: brutor-rfcs",
            "1: private repository or record: impl/rfc",
            "1: internal RFC: rfc-0029",
            "1: phase of an internal plan: phase-3",
        ]
    );
    assert_eq!(
        kinds(
            "one run per ref (cost policy 2026-10-05)\nthe owner's decision, 2026-11-13\n(was: OLD_NAME)"
        ),
        [
            "1: history note: 2026-10-05",
            "2: history note: owner's decision",
            "2: history note: 2026-11-13",
            "3: history note: (was:",
        ]
    );

    // A decision cited instead of the rule it made.
    assert_eq!(
        kinds(
            "# Owner decision (legal answer 5, 2026-11-16): ship them unmodified\n# (owner decision\n\
             # L1); decided on 2026-11-16, decision of 2026-11-17"
        ),
        [
            "1: cited decision: Owner decision",
            "1: cited decision: legal answer 5",
            "2: cited decision: owner decision L1",
            "3: cited decision: decided on 2026-11-16",
            "3: cited decision: decision of 2026-11-17",
        ]
    );
    assert_eq!(
        kinds(
            "the chunk size (decision 13), Decision Q12 and decisions 5; owner ruling, legal answers #4"
        ),
        [
            "1: cited decision: decision 13",
            "1: cited decision: Decision Q12",
            "1: cited decision: decisions 5",
            "1: cited decision: owner ruling",
            "1: cited decision: legal answers #4",
        ]
    );

    assert_eq!(
        kinds(
            "cd /Users/alice/src; KUBECONFIG=/home/bob.smith/.kube/config\n\
             C:/Users/Bob/AppData and the image's /home/brutor/.kube"
        ),
        [
            "1: home directory path: /Users/alice/",
            "1: home directory path: /home/bob.smith/",
            "2: home directory path: /Users/Bob/",
        ]
    );
    assert_eq!(
        kinds(
            "see brutor-trial-service/README.md and the Brutor-License crate\n    \
             for sibling in [\"brutor-kb-connector-sync\"] {\n\
             built by `brutor-gateway-kb-uploader`."
        ),
        [
            "1: private repository or record: brutor-trial-service",
            "1: private repository or record: Brutor-License",
            "2: private repository or record: brutor-kb-connector-sync",
            "3: private repository or record: brutor-gateway-kb-uploader",
        ]
    );

    // A citation split over a line break, with the comment marker between.
    assert_eq!(
        kinds("// the conversation handle of RFC\n// 0021 is a ULID"),
        ["1: internal RFC: RFC 0021"]
    );
    assert_eq!(
        kinds("    /// closes finding\n    /// F245 for good"),
        ["2: finding number: F245"]
    );
    assert_eq!(
        kinds("# decided in Phase\n# 3 of the plan"),
        ["1: phase of an internal plan: Phase 3"]
    );
    assert_eq!(
        kinds("// Widened 2026-09-09 to measure more; found live on\n// 2026-09-26"),
        ["1: history note: 2026-09-09", "2: history note: 2026-09-26"]
    );

    // Public standards, public names and data stay.
    for fine in [
        "a Bearer token (RFC 6750), a JWT (RFC 7519 §4.1), canonical JSON (RFC 8785)",
        "RFC 9207, RFC 9728 and https://www.rfc-editor.org/rfc/rfc8414",
        "DateTime::parse_from_rfc3339(s) and to_rfc3339()",
        "Ed25519 (RFC 8032); private ranges (RFC 1918)",
        "a presigned S3 URL; MiniMax M2.x; BRUTOR-V1-<base64url>",
        "the L3 call runs one script from the L2 manifest",
        "<c r=\"A1\"/> with handle 'H1' and id \"U-1\"",
        "color: #F63; background: #a193ff; key 5fa193b2",
        "\"X-GitHub-Api-Version\", \"2022-11-28\"",
        "Issued:  2026-03-14 and --expires 2027-03-14",
        "\"updated_at\": \"2026-05-23T10:00:00Z\"",
        "MCP protocol version 2025-06-18",
        "each finding is listed; a sprint of work; one phase at a time",
        "<!-- env-registry:begin (generated from src/env_vars.rs) -->",
        "issue #42 and PR #43 of the repository; `&#169;`; SHA-256, UTF-8, SEP-1024",
        "a two-phase commit; Phase Inversion",
        "a decision tree; the caller decides; decisions are logged; decided on the order\n\
         waits for a decision: 1 to 300 seconds; decision_1 = 0; the owner of a decision; \
         the resource owner decides; a legal answer",
        "<path d=\"M12 9v2m0 4h.01 M4 6h16 M4,12 S3 -1.5 2\"/>",
        "/home/<user>/.kube, /Users/<you>/src, $HOME/.aws, ~/.config, /home/brutor/.aws",
        "https://console.example.com/project/home/dashboard/ and app/home/view/; /Users/Errors",
        "brutor-gateway-core, brutor-skill-runner, brutor-client, brutor-licensed, brutor-cli-x",
        // A quoted name standing alone as an item of a list is data: the
        // checks that a shared file is the same in each sibling repository
        // name them.
        "    \"brutor-license\",\n    'brutor-kb-connector-sync',",
    ] {
        assert_eq!(kinds(fine), Vec::<String>::new(), "{fine}");
    }

    // Every allowed standard has a title, and the list is in order.
    assert!(PUBLIC_RFCS.iter().all(|(_, title)| title.len() > 8));
    assert!(PUBLIC_RFCS.windows(2).all(|w| w[0].0 < w[1].0));
    assert!(PRIVATE_REPOSITORIES.windows(2).all(|w| w[0] < w[1]));
    for name in PRIVATE_REPOSITORIES {
        assert!(name.starts_with("brutor-") && *name == name.to_ascii_lowercase());
    }
    assert!(GENERIC_HOMES.iter().all(|(_, what)| what.len() > 8));
}
