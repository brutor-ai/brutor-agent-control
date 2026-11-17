// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: the Portal is an extension, and the Community assembly names
//! nothing of it.
//!
//! Why it matters: a Community gateway serves no portal route, starts no
//! portal task and verifies no portal session because `app.rs` mounts none,
//! `tasks.rs` lists none and the builder installs none. One
//! `.nest("/v1/portal/auth", routes::portal_auth::router())` back in
//! `app.rs` and the Community build serves the Enterprise sign-in again,
//! under the tenant layer's most restrictive rule (so it would look broken,
//! not absent). And a Community route that calls a helper living in a
//! portal module (`portal_common`, T11) cannot be built once the portal
//! modules leave the crate.
//!
//! What makes a portal session stand is held by
//! `tests/credential_verifier_guard.rs`; this holds the assembly.
//!
//! The scans are proven against the shapes they forbid
//! ([`the_scans_catch_the_shapes_they_forbid`]).

#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::{Path, PathBuf};

/// Is `file` one this run has? A file of the Enterprise workspace is not, in
/// a Community tree alone (`tests/support/roots.rs`); what this guard holds
/// about it is held where the workspace is.
fn here(file: &str) -> bool {
    !file.starts_with("enterprise/") || roots::tree("enterprise").is_some()
}

/// The files that assemble a gateway whatever its edition.
const ASSEMBLY: &[&str] = &[
    "src/app.rs",
    "src/builder.rs",
    "src/tasks.rs",
    "src/config/mod.rs",
];

/// What names the Portal in code.
const NAMES: &[&str] = &[
    "routes::portal_",
    "routes::public_tenant_theme",
    "portal_common",
    "services::portal",
    "portal_session",
    "PortalSessionVerifier",
    "Portal::new(",
    "\"/v1/portal",
    "\"/v1/public",
    "portal_revocation_prune",
];

/// The portal's own modules (`editions.yaml`, `enterprise_modules.portal`
/// and the knowledge-base portal routes), which may name `portal_common`.
const PORTAL_MODULES: &[&str] = &[
    "enterprise/src/portal.rs",
    "enterprise/src/portal_session.rs",
    "enterprise/src/routes/portal_alerts.rs",
    "enterprise/src/routes/portal_auth.rs",
    "enterprise/src/routes/portal_common.rs",
    "enterprise/src/routes/portal_evidence.rs",
    "enterprise/src/routes/portal_kb.rs",
    "enterprise/src/routes/portal_kb_identities.rs",
    "enterprise/src/routes/portal_mcp_apps.rs",
    "enterprise/src/routes/portal_mcps.rs",
    "enterprise/src/routes/portal_models.rs",
    "enterprise/src/routes/portal_notifications.rs",
    "enterprise/src/routes/portal_skills.rs",
    "enterprise/src/routes/public_tenant_theme.rs",
];

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) {
    for entry in fs::read_dir(dir).unwrap() {
        let path = entry.unwrap().path();
        if path.is_dir() {
            rust_files(&path, out);
        } else if path.extension().is_some_and(|x| x == "rs") {
            out.push(path);
        }
    }
}

/// `(line number, name)` for every line of code in `text` that names the
/// Portal. Comment lines are prose.
fn portal_names(text: &str) -> Vec<(usize, &'static str)> {
    let mut found = Vec::new();
    for (i, line) in text.lines().enumerate() {
        if line.trim_start().starts_with("//") {
            continue;
        }
        for name in NAMES {
            if line.contains(name) {
                found.push((i + 1, *name));
            }
        }
    }
    found
}

/// Lines of code in `text` that reach into `portal_common`.
fn portal_common_uses(text: &str) -> Vec<usize> {
    text.lines()
        .enumerate()
        .filter(|(_, l)| !l.trim_start().starts_with("//"))
        .filter(|(_, l)| l.contains("portal_common::") || l.contains("portal_common;"))
        .map(|(i, _)| i + 1)
        .collect()
}

#[test]
fn the_assembly_names_nothing_of_the_portal() {
    let mut named = Vec::new();
    for file in ASSEMBLY {
        let text = fs::read_to_string(file).unwrap_or_else(|e| panic!("{file}: {e}"));
        for (line, name) in portal_names(&text) {
            named.push(format!("{file}:{line}: `{name}`"));
        }
    }
    assert!(
        named.is_empty(),
        "the assembly names the Portal; a gateway composed without the extension would \
         still have it:\n{}",
        named.join("\n")
    );
    // The scan reads something: the extension is full of these names.
    if !here("enterprise/src/portal.rs") {
        return;
    }
    let extension = fs::read_to_string("enterprise/src/portal.rs").unwrap();
    assert!(
        portal_names(&extension).len() >= 12,
        "the portal extension names itself {} times: the scan's names are stale",
        portal_names(&extension).len()
    );
}

/// The extension mounts every portal prefix, once, and `main` composes it.
#[test]
fn the_extension_mounts_the_portal_and_main_composes_it() {
    // A Community tree alone has no Portal extension to read.
    if !here("enterprise/src/portal.rs") {
        return;
    }
    let extension = fs::read_to_string("enterprise/src/portal.rs").unwrap();
    for (prefix, router) in [
        (
            "/v1/public/tenants",
            "routes::public_tenant_theme::router()",
        ),
        ("/v1/portal/auth", "routes::portal_auth::router()"),
        ("/v1/portal/alerts", "routes::portal_alerts::router()"),
        ("/v1/portal/evidence", "routes::portal_evidence::router()"),
        ("/v1/portal/models", "routes::portal_models::router()"),
        ("/v1/portal/skills", "routes::portal_skills::router()"),
        (
            "/v1/portal/notifications",
            "routes::portal_notifications::router()",
        ),
        ("/v1/portal/servers", "routes::portal_mcps::router()"),
        ("/v1/portal/mcp-apps", "routes::portal_mcp_apps::router()"),
        ("/v1/portal/settings", "routes::portal_settings::router()"),
    ] {
        assert_eq!(
            extension.matches(&format!("\"{prefix}\"")).count(),
            1,
            "{prefix} is mounted once by the extension"
        );
        assert_eq!(extension.matches(router).count(), 1, "{router}");
    }
    let main = fs::read_to_string("enterprise/src/main.rs").unwrap();
    let named: Vec<String> = main
        .lines()
        .filter(|l| !l.trim_start().starts_with("//"))
        .filter(|l| l.contains("ortal"))
        .map(|l| l.trim().to_string())
        .collect();
    assert_eq!(
        named,
        [
            "use brutor_gateway_enterprise::portal::Portal;",
            ".extension(Portal::new())",
        ]
    );
}

/// T11: no file outside the portal's own reaches into `portal_common`. The
/// group-membership helper approvals use is `routes::request`'s.
#[test]
fn nothing_outside_the_portal_calls_a_portal_helper() {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    assert!(files.len() > 100, "the scan found {} files", files.len());
    let mut offenders = Vec::new();
    let mut own = 0;
    for file in &files {
        let rel = file.to_string_lossy().replace('\\', "/");
        let text = fs::read_to_string(file).unwrap();
        if PORTAL_MODULES.contains(&rel.as_str()) {
            own += portal_common_uses(&text).len();
            continue;
        }
        // The declaration of the module, in the Enterprise crate.
        if rel == "enterprise/src/routes.rs" {
            assert_eq!(text.matches("pub mod portal_common;").count(), 1);
            continue;
        }
        for line in portal_common_uses(&text) {
            offenders.push(format!("{rel}:{line}"));
        }
    }
    assert!(
        offenders.is_empty(),
        "Community code calls a helper that lives in a portal module:\n{}",
        offenders.join("\n")
    );
    assert!(
        own >= 10 || !here("enterprise/src/portal.rs"),
        "the portal's own uses: {own}; the scan is stale"
    );
    let request = fs::read_to_string("src/routes/request.rs").unwrap();
    assert_eq!(
        request.matches("pub async fn get_user_group_ids(").count(),
        1,
        "the group-membership helper is a Community one"
    );
    if here("enterprise/src/routes/portal_common.rs") {
        let common = fs::read_to_string("enterprise/src/routes/portal_common.rs").unwrap();
        assert_eq!(
            common.matches("pub async fn get_user_group_ids(").count(),
            0,
            "portal_common defines a second copy"
        );
    }
}

#[test]
fn the_scans_catch_the_shapes_they_forbid() {
    // What `app.rs`, `tasks.rs` and the builder must not hold.
    for (shape, name) in [
        (
            "        .nest(\"/v1/portal/auth\", routes::portal_auth::router())",
            "routes::portal_",
        ),
        (
            "        .nest(\"/v1/public/tenants\", routes::public_tenant_theme::router())",
            "routes::public_tenant_theme",
        ),
        (
            "        services::portal_session::PortalSessionVerifier::install(&mut app_state)?;",
            "PortalSessionVerifier",
        ),
        (
            "        name: \"portal_revocation_prune\",",
            "portal_revocation_prune",
        ),
        (
            "        let extensions = vec![Box::new(services::portal::Portal::new())];",
            "Portal::new(",
        ),
    ] {
        let found = portal_names(shape);
        assert!(
            found.iter().any(|(_, n)| *n == name),
            "not caught: {shape} ({found:?})"
        );
    }
    // Prose and the Community names that only look alike are not it.
    for fine in [
        "        // .nest(\"/v1/portal/auth\", routes::portal_auth::router())",
        "        .nest(\"/v1/approvals\", routes::approvals::router())",
        "    let headers = request::portal_headers(&headers);",
        "        auth::require_token_type(&auth_ctx, TOKEN_TYPE_PORTAL)?;",
    ] {
        assert!(portal_names(fine).is_empty(), "{fine}");
    }
    // T11, as approvals had it.
    assert_eq!(
        portal_common_uses(
            "use crate::routes::portal_common;\n// portal_common::x\nlet g = \
             portal_common::get_user_group_ids(&state.db, &user_id, &tenant_id).await;\n"
        ),
        vec![1, 3]
    );
}
