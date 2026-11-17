// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: a user's groups are ordered in one place.
//!
//! The bug class: a surface reads the user's memberships with its own query
//! and picks "the deepest" (`ORDER BY hierarchy_level DESC`). Two top-level
//! groups then tie, the database decides, and a user working in one group is
//! billed to and guarded by another. Every membership read that orders the
//! user's groups goes through `usage::user_groups_ordered` (workspace, then
//! primary, then depth, then id). Behaviour is pinned by
//! `tests/integration/user_group_choice.rs`; this pins the shape.

#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::Path;

fn rust_files(dir: &Path, out: &mut Vec<std::path::PathBuf>) {
    for entry in fs::read_dir(dir).unwrap() {
        let p = entry.unwrap().path();
        if p.is_dir() {
            rust_files(&p, out);
        } else if p.extension().is_some_and(|e| e == "rs") {
            out.push(p);
        }
    }
}

/// Raw-string SQL literals (`r#"…"#`) with their starting line.
fn sql_literals(src: &str) -> Vec<(usize, &str)> {
    let mut out = Vec::new();
    let mut rest = src;
    let mut offset = 0;
    while let Some(start) = rest.find("r#\"") {
        let body_start = start + 3;
        let Some(len) = rest[body_start..].find("\"#") else {
            break;
        };
        let line = src[..offset + start].lines().count();
        out.push((line, &rest[body_start..body_start + len]));
        offset += body_start + len + 2;
        rest = &rest[body_start + len + 2..];
    }
    out
}

/// A query that reads the user's memberships and orders by depth is a user's
/// group choice: only `user_groups_ordered` may make it.
fn violations(file: &str, src: &str) -> Vec<String> {
    let helper = src.find("pub async fn user_groups_ordered(");
    sql_literals(src)
        .into_iter()
        .filter(|(_, sql)| {
            (sql.contains("resource_group_memberships") || sql.contains("end_user_group_members"))
                && sql.contains("hierarchy_level DESC")
        })
        .filter(|(line, _)| {
            // The helper's own query is the one allowed.
            let Some(h) = helper else { return true };
            let helper_line = src[..h].lines().count();
            !(file.ends_with("src/services/usage.rs") && *line > helper_line && *line < helper_line + 25)
        })
        .map(|(line, _)| format!("{file}:{line}: orders a user's groups by depth itself; use usage::user_groups_ordered"))
        .collect()
}

#[test]
fn a_users_groups_are_ordered_in_one_place() {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    let bad: Vec<String> = files
        .iter()
        .flat_map(|p| violations(&p.display().to_string(), &fs::read_to_string(p).unwrap()))
        .collect();
    assert!(bad.is_empty(), "{}", bad.join("\n"));
}

/// Proven against the shape it forbids: the query every surface used to run.
#[test]
fn the_guard_catches_a_depth_ordered_membership_pick() {
    let old = concat!(
        "let g = sqlx::query_as(\n    r#\"SELECT rg.* FROM resource_groups rg WHERE rg.id IN (\n",
        "        SELECT group_id FROM resource_group_memberships WHERE user_id = $1::int)\n",
        "       ORDER BY rg.hierarchy_level DESC NULLS LAST LIMIT 1\"#,\n);\n"
    );
    assert_eq!(violations("src/routes/x.rs", old).len(), 1);
}
