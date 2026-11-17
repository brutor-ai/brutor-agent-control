// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Swallowed-database-error guard.
//!
//! `sqlx::query…fetch_optional(db).await.unwrap_or(None)` turned "the
//! database failed" into "there is no row" with no log line, no metric and
//! nothing tying the wrong answer to its cause: access denied by accident,
//! a 404 or an empty list for an outage, an evidence record sealed without
//! its attribution, a policy check skipped.
//!
//! Two rules over `src/` (test code removed):
//!
//! 1. **No bare swallow.** A `fetch_*().await` whose error is replaced by a
//!    value (`.unwrap_or(..)`, `.unwrap_or_default()`, `.ok()`,
//!    `.unwrap_or_else(|_| ..)`, also after a `.map(..)`) fails the test.
//!    Propagate the error (`?`, a 503 via `db_errors::unavailable`) or, where
//!    degrading is genuinely right, use `db_errors::DbResultExt`.
//! 2. **Degraded sites only go down.** `DbResultExt` (`or_logged`,
//!    `or_logged_default`, `or_logged_else`, `ok_logged`) keeps the fallback
//!    but logs the error with its call site and counts it in
//!    `brutor_db_errors_degraded_total{site}`. Each such site carries a
//!    comment saying why the fallback is right. Their number is pinned at
//!    [`DEGRADED_BASELINE_COMMUNITY`] and [`DEGRADED_BASELINE_ENTERPRISE`];
//!    lower one when you convert a site, never raise it
//!    to add one without review.
#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::{Path, PathBuf};

/// Logged fallbacks today, in the Community crate (`src/`) and in the
/// Enterprise workspace (`enterprise/src/`). Each may only go down. The
/// count is taken over the trees this run has (`tests/support/roots.rs`):
/// both where the Enterprise workspace is here, the first alone otherwise.
const DEGRADED_BASELINE_COMMUNITY: usize = 32;
const DEGRADED_BASELINE_ENTERPRISE: usize = 5;

/// The baseline of the trees this run scans.
fn degraded_baseline() -> usize {
    if roots::tree("enterprise").is_some() {
        DEGRADED_BASELINE_COMMUNITY + DEGRADED_BASELINE_ENTERPRISE
    } else {
        DEGRADED_BASELINE_COMMUNITY
    }
}

const FETCHES: &[&str] = &[".fetch_optional(", ".fetch_one(", ".fetch_all("];

/// Ways to replace an error with a value.
const SWALLOWS: &[&str] = &[
    ".unwrap_or(",
    ".unwrap_or_default()",
    ".ok()",
    ".unwrap_or_else(|_|",
];

const DEGRADED: &[&str] = &[
    ".or_logged(",
    ".or_logged_default()",
    ".or_logged_else(",
    ".ok_logged()",
];

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) {
    for e in fs::read_dir(dir).unwrap().flatten() {
        let p = e.path();
        if p.is_dir() {
            rust_files(&p, out);
        } else if p.extension().and_then(|s| s.to_str()) == Some("rs") {
            out.push(p);
        }
    }
}

/// `src` with every `#[cfg(test)]` item removed (attribute to the end of
/// the brace-balanced block, or `;`).
fn without_test_items(src: &str) -> String {
    let mut out = String::with_capacity(src.len());
    let mut rest = src;
    while let Some(at) = rest.find("#[cfg(test)]") {
        out.push_str(&rest[..at]);
        let after = &rest[at..];
        let semi = after.find(';').unwrap_or(after.len());
        let Some(open) = after.find('{').filter(|o| *o < semi) else {
            rest = &after[(semi + 1).min(after.len())..];
            continue;
        };
        let mut depth = 0usize;
        let mut end = after.len();
        for (i, c) in after[open..].char_indices() {
            match c {
                '{' => depth += 1,
                '}' => {
                    depth -= 1;
                    if depth == 0 {
                        end = open + i + 1;
                        break;
                    }
                }
                _ => {}
            }
        }
        rest = &after[end..];
    }
    out.push_str(rest);
    out
}

/// Whitespace removed, so a chain split over lines reads as one.
fn compact(s: &str) -> String {
    s.split_whitespace().collect()
}

/// `s` with its leading `.map(..)` calls (balanced parentheses) removed.
fn skip_maps(mut s: &str) -> &str {
    while let Some(rest) = s.strip_prefix(".map(") {
        let mut depth = 1usize;
        let mut end = None;
        for (i, c) in rest.char_indices() {
            match c {
                '(' => depth += 1,
                ')' => {
                    depth -= 1;
                    if depth == 0 {
                        end = Some(i + 1);
                        break;
                    }
                }
                _ => {}
            }
        }
        match end {
            Some(e) => s = &rest[e..],
            None => return s,
        }
    }
    s
}

/// Line of each fetch whose awaited result is swallowed: what follows
/// `.await` (after any `.map(..)`) is one of [`SWALLOWS`].
fn swallowed(src: &str) -> Vec<usize> {
    let code = without_test_items(src);
    let mut found = Vec::new();
    for fetch in FETCHES {
        for (at, _) in code.match_indices(fetch) {
            let tail = &code[at..];
            let Some(await_at) = tail.find(".await") else {
                continue;
            };
            if tail[..await_at].contains(';') {
                continue;
            }
            // A window of what follows, ended on a char boundary (comments
            // carry multi-byte characters).
            let mut end = tail.len().min(await_at + 600);
            while !tail.is_char_boundary(end) {
                end -= 1;
            }
            let after = compact(&tail[await_at + ".await".len()..end]);
            let after = skip_maps(&after);
            if SWALLOWS.iter().any(|s| after.starts_with(&compact(s))) {
                found.push(code[..at].matches('\n').count() + 1);
            }
        }
    }
    found
}

/// Number of logged fallbacks in one file's product code.
fn degraded(src: &str) -> usize {
    let code = without_test_items(src);
    DEGRADED.iter().map(|d| code.matches(d).count()).sum()
}

fn product_files() -> Vec<(String, String)> {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    files.sort();
    files
        .into_iter()
        .map(|f| {
            let path = f.to_string_lossy().replace('\\', "/");
            let src = fs::read_to_string(&f).unwrap();
            (path, src)
        })
        // The helper's own definitions and tests are not call sites.
        .filter(|(p, _)| p != "src/services/db_errors.rs")
        .collect()
}

#[test]
fn no_database_error_is_swallowed() {
    let mut all = Vec::new();
    for (path, src) in product_files() {
        for line in swallowed(&src) {
            all.push(format!("{path}:{line}"));
        }
    }
    assert!(
        all.is_empty(),
        "a `fetch_*().await` turns a database failure into a value with nothing logged. \
         Propagate it (`?`, `db_errors::unavailable` for a 503) or, where degrading is right, \
         use `db_errors::DbResultExt` and say why:\n{}",
        all.join("\n")
    );
}

#[test]
fn degraded_database_errors_only_go_down() {
    let files = product_files();
    let count = |enterprise: bool| -> usize {
        files
            .iter()
            .filter(|(path, _)| path.starts_with("src/") != enterprise)
            .map(|(_, s)| degraded(s))
            .sum()
    };
    for (tree, n, baseline) in [
        ("src/", count(false), DEGRADED_BASELINE_COMMUNITY),
        (
            "the Enterprise workspace",
            count(true),
            degraded_baseline() - DEGRADED_BASELINE_COMMUNITY,
        ),
    ] {
        assert!(
            n <= baseline,
            "{n} logged fallbacks in {tree} (baseline {baseline}): a new one needs review, \
             prefer propagating the error"
        );
        assert_eq!(
            n, baseline,
            "the count in {tree} fell to {n}: lower its baseline in \
             tests/swallowed_db_error_guard.rs"
        );
    }
}

/// The scanner sees the shapes it bans and ignores the ones it doesn't.
#[test]
fn the_guard_sees_the_shapes_it_bans() {
    let bad = r#"
        let row: Option<(String,)> = sqlx::query_as("SELECT 1")
            .bind(id)
            .fetch_optional(&state.db)
            .await
            .unwrap_or(None);
        let n: i64 = sqlx::query_scalar("SELECT 1").fetch_one(db).await.unwrap_or(0);
        let v = sqlx::query_as("x").fetch_all(db).await.unwrap_or_default();
        let o = q.fetch_optional(db).await.ok();
        let w = q.fetch_one(db).await.unwrap_or_else(|_| fallback());
        let c = q.fetch_one(db).await.map(|(c,)| c.unwrap_or(0)).unwrap_or(0);
        let t: (i64,) = q.fetch_one(&mut *tx).await.unwrap_or((0,));
    "#;
    assert_eq!(swallowed(bad).len(), 7);

    let fine = r#"
        let row = q.fetch_optional(&state.db).await.map_err(|e| lookup_failed("x", e))?;
        let n = q.fetch_one(db).await?;
        let r = match q.fetch_all(db).await { Ok(v) => v, Err(e) => return Err(e.into()) };
        // A later Option combinator on an already-handled row is not a swallow.
        let name = q.fetch_optional(db).await?.unwrap_or_default();
        let logged = q.fetch_all(db).await.or_logged_default();
        let c = q.fetch_one(db).await.map(|(c,)| c.unwrap_or(0))?;
    "#;
    assert!(swallowed(fine).is_empty(), "{:?}", swallowed(fine));
    assert_eq!(degraded(fine), 1);

    let test_only =
        "fn f() {}\n#[cfg(test)]\nmod t { fn g() { q.fetch_one(db).await.unwrap_or(0); } }";
    assert!(swallowed(test_only).is_empty());

    // A multi-byte character inside the window must not panic the scan.
    let wide = format!("q.fetch_one(db).await?; // {}\n", "—".repeat(400));
    assert!(swallowed(&wide).is_empty());
}
