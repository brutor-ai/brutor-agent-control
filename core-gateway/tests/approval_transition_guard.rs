// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: an approval request's status only ever moves *from a named
//! status*.
//!
//! Why it matters: a transition without a status predicate lands on top of
//! whatever is there. Reject was `UPDATE … SET status = 'rejected' WHERE id =
//! $4`, unchecked: it could overwrite a concurrent approval whose token an
//! agent was about to redeem, or an expiry already sealed as evidence, and it
//! answered 200 when the write failed.
//! Every statement that writes `tool_approval_requests.status` must say which
//! status it moves from.

#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::{Path, PathBuf};

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) {
    for entry in fs::read_dir(dir).expect("read dir") {
        let path = entry.expect("entry").path();
        if path.is_dir() {
            rust_files(&path, out);
        } else if path.extension().is_some_and(|e| e == "rs") {
            out.push(path);
        }
    }
}

/// Whether one SQL string literal moves the status without saying from what.
fn unguarded(sql: &str) -> bool {
    let flat = sql.split_whitespace().collect::<Vec<_>>().join(" ");
    let lower = flat.to_ascii_lowercase();
    let Some(at) = lower.find("update tool_approval_requests set ") else {
        return false;
    };
    let rest = &lower[at..];
    let Some(where_at) = rest.find(" where ") else {
        // no WHERE at all on a status write: every row
        return rest.contains("set status =") || rest.contains(", status =");
    };
    let (set, predicate) = rest.split_at(where_at);
    if !(set.contains("set status =") || set.contains(", status =")) {
        return false; // writes other columns only
    }
    // the predicate names the status it moves from, directly or through the
    // CTE the sweeper selects with (`… WHERE status = 'pending' … FOR UPDATE`)
    let names_status = predicate.contains("status = '");
    let through_cte =
        predicate.contains("where id in (select id from") && lower[..at].contains("status = '");
    !(names_status || through_cte)
}

#[test]
fn every_status_transition_names_the_status_it_moves_from() {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    files.sort();
    let mut bad = Vec::new();
    let mut seen = 0;
    for path in files {
        let src = fs::read_to_string(&path).expect("read");
        // A SQL statement lives inside one string literal.
        for lit in src.split('"') {
            if lit
                .to_ascii_lowercase()
                .split_whitespace()
                .collect::<Vec<_>>()
                .join(" ")
                .contains("update tool_approval_requests set ")
            {
                seen += 1;
                if unguarded(lit) {
                    bad.push(format!(
                        "{}: {}",
                        path.display(),
                        lit.split_whitespace().collect::<Vec<_>>().join(" ")
                    ));
                }
            }
        }
    }
    assert!(
        seen >= 5,
        "expected the approval status writers; found {seen}"
    );
    assert!(
        bad.is_empty(),
        "approval status written without a `status = '…'` predicate:\n{}",
        bad.join("\n")
    );
}

#[test]
fn the_guard_sees_the_shape_it_forbids() {
    // what reject was
    assert!(unguarded(
        "UPDATE tool_approval_requests
           SET status = 'rejected', resolved_by = $1, resolved_at = $2, resolution_note = $3
           WHERE id = $4"
    ));
    // no WHERE at all
    assert!(unguarded(
        "UPDATE tool_approval_requests SET status = 'expired'"
    ));
    // what is allowed
    assert!(!unguarded(
        "UPDATE tool_approval_requests SET status = 'approved', approval_token = $1 \
         WHERE id = $5 AND tenant_id = $6 AND status = 'pending'"
    ));
    assert!(!unguarded(
        "WITH expired AS ( SELECT id FROM tool_approval_requests WHERE status = 'pending' AND \
         expires_at < NOW() FOR UPDATE SKIP LOCKED ) UPDATE tool_approval_requests SET status = \
         'expired' WHERE id IN (SELECT id FROM expired) RETURNING id"
    ));
    // a write that does not touch the status
    assert!(!unguarded(
        "UPDATE tool_approval_requests SET resolution_note = $1 WHERE id = $2"
    ));
}
