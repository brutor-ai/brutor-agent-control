// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Per-session skill workspaces.
//!
//! L3 `skills__run_script` runs a named script inside a session-scoped
//! workspace (`<root>/<tenant>/<session>`) so a workflow's script runs can
//! pass files to each other. This module owns the ONE persistence surface
//! the runner has, so it is where path-traversal and tenant-isolation are
//! enforced:
//!
//! * `tenant_id` and `session_id` are encoded to a single safe path segment
//!   each — the encoding is injective (no collisions) and cannot contain `/`,
//!   `\`, `.` or `..`, so an attacker-controlled id can never escape the root
//!   or reach another tenant's tree.
//! * Every seeded filename (asset / resource / the script itself) must be a
//!   bare basename — no separators, no `..` — before it is joined and written.
//! * Workspaces older than a TTL are garbage-collected (best-effort, at run
//!   entry) and a per-tenant session cap bounds growth within the TTL.

use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

/// Max session workspaces kept per tenant (oldest reaped beyond this).
const MAX_SESSIONS_PER_TENANT: usize = 64;

/// Encode an arbitrary id (tenant/session) into ONE safe path segment.
///
/// Keeps `[A-Za-z0-9-]` verbatim and escapes every other byte as `_<hex>`.
/// Because `_` is itself escaped, the mapping is injective (distinct inputs →
/// distinct outputs, so two sessions never collide) and the result can never
/// be `.`, `..`, or contain a path separator — traversal is structurally
/// impossible.
pub fn safe_component(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' => out.push(b as char),
            _ => {
                out.push('_');
                out.push_str(&format!("{b:02x}"));
            }
        }
    }
    if out.is_empty() {
        out.push('0');
    }
    out.truncate(200);
    out
}

/// Validate a seeded filename to a bare basename inside the workspace.
///
/// Unlike [`safe_component`] the real name is preserved (a script must
/// `open("data.json")` by name) — so anything that could escape the workspace
/// is rejected outright rather than encoded.
pub fn safe_filename(name: &str) -> Result<String, String> {
    if name.is_empty() || name.len() > 255 {
        return Err(format!("invalid filename length: {name:?}"));
    }
    if name.contains('/')
        || name.contains('\\')
        || name.contains('\0')
        || name == "."
        || name == ".."
        || name.contains("..")
    {
        return Err(format!("unsafe filename (path traversal): {name:?}"));
    }
    Ok(name.to_string())
}

/// The workspace directory for a (tenant, session) pair. Pure path derivation —
/// does not touch the filesystem.
pub fn session_dir(root: &Path, tenant_id: &str, session_id: &str) -> PathBuf {
    root.join(safe_component(tenant_id))
        .join(safe_component(session_id))
}

/// Ensure the session workspace exists (creating `<root>/<tenant>/<session>`),
/// returning its path. Also enforces the per-tenant session cap and the TTL GC.
pub fn ensure_session(
    root: &Path,
    tenant_id: &str,
    session_id: &str,
    ttl_secs: u64,
) -> std::io::Result<PathBuf> {
    let tenant_dir = root.join(safe_component(tenant_id));
    // Reap expired sessions (best-effort) before creating a new one.
    gc_tenant(&tenant_dir, ttl_secs);
    enforce_session_cap(&tenant_dir);

    let dir = tenant_dir.join(safe_component(session_id));
    std::fs::create_dir_all(&dir)?;
    set_private_perms(&dir);
    Ok(dir)
}

/// Basenames of the regular files currently in a workspace (for reporting what
/// a script produced). Never returns paths, only names.
pub fn list_files(dir: &Path) -> Vec<String> {
    let mut names = Vec::new();
    if let Ok(entries) = std::fs::read_dir(dir) {
        for e in entries.flatten() {
            if e.file_type().map(|t| t.is_file()).unwrap_or(false) {
                names.push(e.file_name().to_string_lossy().to_string());
            }
        }
    }
    names.sort();
    names
}

/// GC every tenant's expired sessions under `root` (best-effort).
pub fn gc_all(root: &Path, ttl_secs: u64) {
    if let Ok(entries) = std::fs::read_dir(root) {
        for e in entries.flatten() {
            if e.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                gc_tenant(&e.path(), ttl_secs);
            }
        }
    }
}

/// Remove session dirs under one tenant dir whose mtime is older than the TTL.
fn gc_tenant(tenant_dir: &Path, ttl_secs: u64) {
    let ttl = Duration::from_secs(ttl_secs);
    let now = SystemTime::now();
    if let Ok(entries) = std::fs::read_dir(tenant_dir) {
        for e in entries.flatten() {
            let Ok(meta) = e.metadata() else { continue };
            if !meta.is_dir() {
                continue;
            }
            let expired = meta
                .modified()
                .ok()
                .and_then(|m| now.duration_since(m).ok())
                .map(|age| age > ttl)
                .unwrap_or(false);
            if expired {
                let _ = std::fs::remove_dir_all(e.path());
            }
        }
    }
}

/// Keep at most `MAX_SESSIONS_PER_TENANT` session dirs, reaping the oldest.
fn enforce_session_cap(tenant_dir: &Path) {
    let mut dirs: Vec<(SystemTime, PathBuf)> = Vec::new();
    if let Ok(entries) = std::fs::read_dir(tenant_dir) {
        for e in entries.flatten() {
            if let Ok(meta) = e.metadata()
                && meta.is_dir()
            {
                let mtime = meta.modified().unwrap_or(SystemTime::UNIX_EPOCH);
                dirs.push((mtime, e.path()));
            }
        }
    }
    if dirs.len() > MAX_SESSIONS_PER_TENANT {
        dirs.sort_by_key(|(t, _)| *t); // oldest first
        let excess = dirs.len() - MAX_SESSIONS_PER_TENANT;
        for (_, path) in dirs.into_iter().take(excess) {
            let _ = std::fs::remove_dir_all(path);
        }
    }
}

#[cfg(unix)]
pub fn set_private_perms(dir: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700));
}

#[cfg(not(unix))]
pub fn set_private_perms(_dir: &Path) {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn safe_component_keeps_plain_ids() {
        assert_eq!(safe_component("ten-01KP9ABC"), "ten-01KP9ABC");
        assert_eq!(safe_component("corr-123-abc"), "corr-123-abc");
    }

    #[test]
    fn safe_component_escapes_traversal_and_separators() {
        // No output may contain a separator or a dot run.
        for evil in [
            "../../etc",
            "..",
            ".",
            "a/b",
            "a\\b",
            "a.b",
            "x\0y",
            "../../../root",
        ] {
            let c = safe_component(evil);
            assert!(!c.contains('/'), "{evil} -> {c}");
            assert!(!c.contains('\\'), "{evil} -> {c}");
            assert!(!c.contains(".."), "{evil} -> {c}");
            assert_ne!(c, "..");
            assert_ne!(c, ".");
        }
    }

    #[test]
    fn safe_component_is_injective() {
        // Distinct ids never collide to the same segment (no cross-session bleed).
        assert_ne!(safe_component("a_b"), safe_component("a-b"));
        assert_ne!(safe_component("a.b"), safe_component("a-b"));
        assert_ne!(safe_component("session1"), safe_component("session2"));
    }

    #[test]
    fn session_dir_stays_under_root() {
        let root = PathBuf::from("/srv/ws");
        let d = session_dir(&root, "../../etc", "../../passwd");
        assert!(d.starts_with(&root), "escaped root: {d:?}");
        // Exactly root / tenant-seg / session-seg — two levels, no traversal.
        assert_eq!(d.components().count(), root.components().count() + 2);
    }

    #[test]
    fn safe_filename_accepts_plain_names() {
        assert_eq!(safe_filename("data.json").unwrap(), "data.json");
        assert_eq!(safe_filename("report_2026.csv").unwrap(), "report_2026.csv");
    }

    #[test]
    fn safe_filename_rejects_traversal() {
        for evil in [
            "../secret",
            "a/b.json",
            "..",
            ".",
            "/etc/passwd",
            "x\0y",
            "a/../b",
        ] {
            assert!(safe_filename(evil).is_err(), "should reject {evil:?}");
        }
    }
}
