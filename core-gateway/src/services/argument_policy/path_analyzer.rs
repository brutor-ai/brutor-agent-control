// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Path analyzer.
//!
//! Lexical-only — we do NOT touch the filesystem. The intent is
//! to flag traversal attempts (`..`) and enforce a configured
//! root allowlist, both of which are decidable from the string
//! alone. Tool execution still has to do its own
//! `fs::canonicalize` if it wants symlink-aware enforcement.

use std::path::{Component, Path, PathBuf};

use serde_json::Value;

use super::{ArgFacts, ArgKindTag};

pub fn analyze(input: &str, _config: Option<&Value>) -> ArgFacts {
    let mut facts = ArgFacts {
        kind: ArgKindTag::Path,
        raw_excerpt: super::truncate_excerpt(input),
        ..Default::default()
    };

    let path = Path::new(input);
    facts.path_is_absolute = path.is_absolute();

    let mut normalized = PathBuf::new();
    let mut traverses = false;
    for c in path.components() {
        match c {
            Component::ParentDir => {
                traverses = true;
                // Lexically pop one component when possible, but
                // record traversal regardless — the operator
                // chose to deny based on intent, not on whether
                // the traversal escapes the cwd.
                normalized.pop();
            }
            Component::CurDir => {} // skip
            other => normalized.push(other.as_os_str()),
        }
    }
    facts.path_traverses_parent = traverses;
    facts.path_normalized = Some(normalized.to_string_lossy().into_owned());
    facts
}

/// Returns true if `path` is lexically inside any of `roots`
/// (compares normalized prefixes — does NOT canonicalize symlinks).
/// Used by the `inside_root` / `outside_root` predicates.
pub(crate) fn is_inside_any_root(path_str: &str, roots: &[&str]) -> bool {
    let normalized = analyze(path_str, None).path_normalized.unwrap_or_default();
    let p = Path::new(&normalized);
    roots.iter().any(|r| {
        let root = Path::new(r);
        p.starts_with(root)
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn relative_path_not_marked_absolute() {
        let f = analyze("foo/bar.txt", None);
        assert!(!f.path_is_absolute);
        assert!(!f.path_traverses_parent);
    }

    #[test]
    fn absolute_path_marked_absolute() {
        let f = analyze("/etc/passwd", None);
        assert!(f.path_is_absolute);
    }

    #[test]
    fn parent_traversal_detected() {
        let f = analyze("../../etc/passwd", None);
        assert!(f.path_traverses_parent);
    }

    #[test]
    fn embedded_parent_traversal_detected() {
        let f = analyze("/data/user/../../../etc/passwd", None);
        assert!(f.path_traverses_parent);
    }

    #[test]
    fn pure_current_dir_segments_are_stripped() {
        let f = analyze("./foo/./bar", None);
        assert!(!f.path_traverses_parent);
        assert_eq!(f.path_normalized.as_deref(), Some("foo/bar"));
    }

    #[test]
    fn normalization_does_not_resolve_symlinks() {
        // Purely lexical — we never touch the FS.
        let f = analyze("/var/lib/../log/syslog", None);
        assert!(f.path_traverses_parent);
        assert_eq!(f.path_normalized.as_deref(), Some("/var/log/syslog"));
    }

    #[test]
    fn root_check_inside() {
        assert!(is_inside_any_root("/data/file.txt", &["/data"]));
        assert!(is_inside_any_root("/data/sub/file.txt", &["/data"]));
    }

    #[test]
    fn root_check_outside() {
        assert!(!is_inside_any_root("/etc/passwd", &["/data"]));
        assert!(!is_inside_any_root("../etc/passwd", &["/data"]));
    }

    #[test]
    fn root_check_traversal_lexically_pops_out_of_root() {
        // /data/../etc/passwd lexically normalizes to /etc/passwd,
        // which is NOT inside /data — the predicate should reject.
        assert!(!is_inside_any_root("/data/../etc/passwd", &["/data"]));
    }
}
