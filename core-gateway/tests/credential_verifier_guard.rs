// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A session token becomes a credential only through the
//! token-type registry (`services::credential_verifier`), and what makes a
//! portal session stand is named only by the portal.
//!
//! Before the registry `auth::authenticate_request` held the portal's
//! revocation list and session cutoff itself and ran them on every gateway
//! token. The portal is Enterprise; authentication is Community. This guard
//! keeps the split from growing back, and keeps a surface from going round
//! the registry:
//!
//! * outside the portal's own files, no source file names the portal
//!   verifier, its caches, its tables or its functions (the files that must
//!   name the module are listed, each with its reason);
//! * every call of `authenticate_request` hands it the state's registry,
//!   and nothing but `AppState::new` builds a registry or the portal's
//!   `install` registers into one;
//! * a gateway token is decoded only by `services::auth`, and by the
//!   listed callers that read claims of a token authentication has already
//!   accepted (or, for the tenant layer, to name the tenant only);
//! * the state starts with the Community registry (administrators'
//!   sessions and service tokens, `RefuseUnregistered` for the rest), which
//!   does not register `portal`; the portal extension installs the portal's
//!   (`enterprise/src/portal.rs`, composed by `main.rs`).
//!
//! What the registry then answers is pinned by behaviour, in
//! `tests/integration/credential_surface_matrix.rs`.

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

/// The portal's own files: its verifier, and its routes
/// (`enterprise_modules.portal` in the editions manifest).
const PORTAL_FILES: &[&str] = &[
    "enterprise/src/portal.rs",
    "enterprise/src/portal_session.rs",
    "enterprise/src/routes/portal_auth.rs",
    "enterprise/src/routes/portal_alerts.rs",
    "enterprise/src/routes/portal_notifications.rs",
    "enterprise/src/routes/portal_models.rs",
    "enterprise/src/routes/portal_mcps.rs",
    "enterprise/src/routes/portal_mcp_apps.rs",
    "enterprise/src/routes/portal_skills.rs",
    "enterprise/src/routes/portal_evidence.rs",
    "enterprise/src/routes/public_tenant_theme.rs",
];

/// What decides whether a portal session stands. No file outside the
/// portal's names any of these.
const PORTAL_INTERNALS: &[&str] = &[
    "PortalSessionVerifier",
    "portal_revocation_cache",
    "portal_session_cutoff_cache",
    "end_user_revoked_tokens",
    "sessions_valid_after",
    "revoke_portal_token",
    "note_portal_sessions_ended",
    "clear_portal_revocation_cache",
    "prune_expired_portal_revocations",
    "verify_issued_after_session_cutoff",
    "verify_portal_session",
    "require_portal_token",
    "portal_session::",
];

/// Files outside the portal that name its module, what they must contain,
/// and why. None of them verifies anything. The
/// portal is an extension (`enterprise/src/portal.rs`): the builder and the
/// task list name nothing of it, and the one line left is the declaration
/// of the module, which moves with the module.
const MODULE_HOLDERS: &[(&str, &str, &str)] = &[(
    "enterprise/src/lib.rs",
    "pub mod portal_session;",
    "declares the module, in the Enterprise crate: no Community file names it",
)];

/// Callers of the two functions that decode a gateway token outside
/// `services/auth.rs`, and why each may.
const DECODERS: &[(&str, &str, &str)] = &[
    (
        "src/middleware/tenant.rs",
        "decode_gateway_jwt(",
        "names the tenant of the request from the token; grants nothing (authentication \
         follows in the route and verifies the token through the registry)",
    ),
    (
        "enterprise/src/routes/portal_auth.rs",
        "gateway_claims_from_headers(",
        "logout reads the `jti` of the session it has just authenticated, to revoke it",
    ),
    (
        "src/services/service_auth.rs",
        "gateway_claims_from_headers(",
        "reads `admin_type` of the token `authenticate_request` has just accepted",
    ),
];

fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).to_path_buf()
}

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

/// `(relative path, source)` of every file under `src/` and, where this
/// repository carries the Enterprise workspace, under `enterprise/src/`
/// (the source-scan guards are root-aware).
fn sources() -> Vec<(String, String)> {
    let mut files = Vec::new();
    rust_files(&root().join("src"), &mut files);
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&root().join(enterprise).join("src"), &mut files);
    }
    files.sort();
    files
        .into_iter()
        .map(|p| {
            let rel = p
                .strip_prefix(root())
                .expect("under the manifest")
                .to_string_lossy()
                .replace('\\', "/");
            (rel, fs::read_to_string(&p).expect("read"))
        })
        .collect()
}

/// Lines of code: comment lines dropped, numbered from 1.
fn code_lines(text: &str) -> impl Iterator<Item = (usize, &str)> {
    text.lines()
        .enumerate()
        .filter(|(_, l)| !l.trim_start().starts_with("//"))
        .map(|(i, l)| (i + 1, l))
}

/// The file without its unit tests (`#[cfg(test)] mod ...` to the end).
fn production(text: &str) -> &str {
    match text.find("#[cfg(test)]\nmod ") {
        Some(at) => &text[..at],
        None => text,
    }
}

fn names_portal_internals(text: &str) -> Vec<(usize, String)> {
    code_lines(text)
        .filter(|(_, l)| PORTAL_INTERNALS.iter().any(|n| l.contains(n)))
        .map(|(i, l)| (i, l.trim().to_string()))
        .collect()
}

#[test]
fn only_the_portal_names_what_makes_a_portal_session_stand() {
    let mut offenders = Vec::new();
    for (path, text) in sources() {
        if PORTAL_FILES.contains(&path.as_str()) {
            continue;
        }
        let holder = MODULE_HOLDERS.iter().find(|(f, _, _)| *f == path);
        for (line, code) in names_portal_internals(&text) {
            // A holder may name the module, and only as listed.
            let allowed = holder.is_some_and(|(_, listed, _)| code.contains(listed));
            if !allowed {
                offenders.push(format!("{path}:{line}: {code}"));
            }
        }
    }
    assert!(
        offenders.is_empty(),
        "Community code names the portal's session verifier or what it reads. Ask the \
         registry (`state.credential_verifiers`) or `auth::require_token_type` instead:\n{}",
        offenders.join("\n")
    );
    for (file, must, why) in MODULE_HOLDERS.iter().filter(|(f, _, _)| here(f)) {
        let text = fs::read_to_string(root().join(file)).expect("holder");
        assert!(
            text.contains(must),
            "{file} is listed as naming the portal's module ({why}) and no longer contains \
             `{must}`: update the list"
        );
    }
    for file in PORTAL_FILES.iter().filter(|f| here(f)) {
        assert!(root().join(file).exists(), "{file} is listed and gone");
    }
}

/// The arguments of every `authenticate_request(` call in `text`.
fn authenticate_calls(text: &str) -> Vec<(usize, String)> {
    let mut out = Vec::new();
    let mut from = 0;
    while let Some(i) = text[from..].find("authenticate_request(") {
        let at = from + i;
        let open = at + "authenticate_request(".len();
        from = open;
        let line_start = text[..at].rfind('\n').map_or(0, |n| n + 1);
        let before = &text[line_start..at];
        if before.trim_start().starts_with("//") || before.contains("fn ") {
            continue; // prose, or the definition
        }
        let mut depth = 1;
        let mut end = text.len();
        for (j, c) in text[open..].char_indices() {
            match c {
                '(' => depth += 1,
                ')' => depth -= 1,
                _ => {}
            }
            if depth == 0 {
                end = open + j;
                break;
            }
        }
        out.push((
            text[..at].lines().count().max(1),
            text[open..end].to_string(),
        ));
    }
    out
}

#[test]
fn every_surface_hands_authentication_the_states_registry() {
    let mut offenders = Vec::new();
    let mut calls = 0;
    for (path, text) in sources() {
        if path == "src/services/auth.rs" {
            continue;
        }
        for (line, args) in authenticate_calls(production(&text)) {
            calls += 1;
            if !args.contains("&state.credential_verifiers,") {
                offenders.push(format!("{path}:{line}"));
            }
        }
    }
    // The Enterprise workspace's surfaces are counted where it is here.
    let least = if here("enterprise/src/portal.rs") {
        25
    } else {
        20
    };
    assert!(
        calls >= least,
        "expected the gateway's surfaces; found {calls} calls"
    );
    assert!(
        offenders.is_empty(),
        "authenticate_request is not given `&state.credential_verifiers` (a registry built on \
         the spot would decide for itself which token types are credentials):\n{}",
        offenders.join("\n")
    );
}

#[test]
fn nothing_builds_or_changes_a_registry_but_the_state_and_the_portal() {
    let mut offenders = Vec::new();
    for (path, text) in sources() {
        let code = production(&text);
        for (line, l) in code_lines(code) {
            let builds = l.contains("CredentialVerifiers::community(")
                || l.contains("CredentialVerifiers {");
            let registers = l.contains("credential_verifiers") && l.contains(".register(")
                || l.contains(".credential_verifiers =")
                || l.trim_start().starts_with(".register(TOKEN_TYPE_")
                || l.contains(".register_admin_type(");
            let allowed = match path.as_str() {
                "src/services/credential_verifier.rs" => true,
                "src/services/mod.rs" => builds && !registers,
                "enterprise/src/portal_session.rs" => registers && !builds,
                // The kind of administrator the Enterprise Edition adds.
                "enterprise/src/system_admin.rs" => {
                    l.contains(".register_admin_type(ADMIN_TYPE_SYSTEM, ") && !builds
                }
                _ => false,
            };
            if (builds || registers) && !allowed {
                offenders.push(format!("{path}:{line}: {}", l.trim()));
            }
        }
    }
    assert!(
        offenders.is_empty(),
        "a registry is built by `AppState::new` and extended by the portal's `install`, \
         nowhere else:\n{}",
        offenders.join("\n")
    );
}

#[test]
fn a_gateway_token_is_decoded_only_by_authentication_and_the_listed_readers() {
    let mut offenders = Vec::new();
    for (path, text) in sources() {
        if path == "src/services/auth.rs" {
            continue;
        }
        for (line, l) in code_lines(production(&text)) {
            for call in ["decode_gateway_jwt(", "gateway_claims_from_headers("] {
                if l.contains(call) && !DECODERS.iter().any(|(f, c, _)| *f == path && *c == call) {
                    offenders.push(format!("{path}:{line}: {}", l.trim()));
                }
            }
            // A route that reads the claims of a session token for itself
            // has gone round the registry.
            if l.contains("decode::<GatewayJwtClaims>") {
                offenders.push(format!("{path}:{line}: {}", l.trim()));
            }
        }
    }
    assert!(
        offenders.is_empty(),
        "a gateway token is decoded outside `services::auth`; authenticate the request and \
         read `AuthContext` instead:\n{}",
        offenders.join("\n")
    );
    for (file, call, why) in DECODERS.iter().filter(|(f, _, _)| here(f)) {
        let text = fs::read_to_string(root().join(file)).expect("decoder");
        assert!(
            production(&text).contains(call),
            "{file} is listed as calling `{call}` ({why}) and no longer does: update the list"
        );
    }
}

#[test]
fn the_state_starts_with_the_community_registry_and_the_portal_extension_installs_the_portal() {
    let state = fs::read_to_string(root().join("src/services/mod.rs")).expect("mod.rs");
    let new_fn = &state[state.find("    pub fn new(").expect("AppState::new")..];
    let compact: String = new_fn.split_whitespace().collect();
    assert!(
        compact.contains(
            "credential_verifiers:credential_verifier::CredentialVerifiers::community(\
             credential_verifier::RefuseUnregistered,),"
        ),
        "AppState::new starts with the Community registry and RefuseUnregistered"
    );

    let hook =
        fs::read_to_string(root().join("src/services/credential_verifier.rs")).expect("hook");
    let community = &hook[hook.find("pub fn community(").expect("community()")..];
    let community = &community[..community.find("\n    }\n").expect("end of community()")];
    assert!(
        registers_exactly_admin_and_gateway(community),
        "CredentialVerifiers::community registers administrators' sessions and service \
         tokens and nothing else:\n{community}"
    );
    // The hook's module does not know the portal's implementation.
    assert!(
        code_lines(production(&hook)).all(|(_, l)| !l.contains("portal_session")),
        "the hook's module names the portal's implementation"
    );

    // The builder installs no verifier: the portal extension does, in
    // its `install_state`, and `main` composes it.
    let builder = fs::read_to_string(root().join("src/builder.rs")).expect("builder");
    assert!(
        code_lines(production(&builder))
            .all(|(_, l)| !l.contains("portal") && !l.contains("Portal")),
        "the builder names the portal"
    );
    // A Community tree alone: the extension is not here to read.
    if !here("enterprise/src/portal.rs") {
        return;
    }
    let extension = fs::read_to_string(root().join("enterprise/src/portal.rs")).expect("extension");
    let install = &extension[extension
        .find("fn install_state(&self, state: &mut AppState)")
        .expect("install_state")..];
    let install = &install[..install.find("\n    }\n").expect("end of install_state")];
    assert!(
        install.contains("PortalSessionVerifier::install(state)"),
        "the portal extension installs the portal's verifier:\n{install}"
    );
    let main = fs::read_to_string(root().join("enterprise/src/main.rs")).expect("main");
    assert_eq!(main.matches(".extension(Portal::new())").count(), 1);
}

/// Authentication and the Community
/// registry name no system administrator. Their session table is read by
/// the Enterprise verifier only, and the Community registry registers
/// tenant administrators and no other kind.
#[test]
fn community_authentication_names_no_system_administrator() {
    // (`services/service_auth.rs` is not in this list: it reads the
    // `admin_type` claim of a token authentication has already accepted, to
    // scope a control-plane call, and names the principal
    // `ServicePrincipal::SystemAdmin`. It verifies nothing; on a Community
    // gateway no such token is accepted, so that arm is not reached.)
    for file in [
        "src/services/auth.rs",
        "src/services/credential_verifier.rs",
        "src/middleware/tenant.rs",
    ] {
        let text = fs::read_to_string(root().join(file)).expect("read");
        let offenders = system_admin_names(production(&text));
        assert!(
            offenders.is_empty(),
            "{file} names a system administrator; their sessions are verified by the \
             Enterprise verifier registered for the kind:\n{offenders:#?}"
        );
    }
    let hook =
        fs::read_to_string(root().join("src/services/credential_verifier.rs")).expect("hook");
    let community = &hook[hook.find("pub fn community(").expect("community()")..];
    let community = &community[..community.find("\n    }\n").expect("end of community()")];
    assert!(
        community.contains(
            "admin_types: vec![(ADMIN_TYPE_TENANT, Arc::new(TenantAdminSessionVerifier))],"
        ),
        "CredentialVerifiers::community registers tenant administrators and no other kind:\n\
         {community}"
    );
    // The session check takes its table from the verifier that calls it.
    let auth = fs::read_to_string(root().join("src/services/auth.rs")).expect("auth");
    assert!(production(&auth).contains("table: &'static str,"));
    if here("enterprise/src/system_admin.rs") {
        let enterprise =
            fs::read_to_string(root().join("enterprise/src/system_admin.rs")).expect("ext");
        assert_eq!(
            enterprise
                .matches(
                    "auth::verify_admin_session_in(db, \"system_admin_sessions\", claims).await"
                )
                .count(),
            1
        );
    }
    // The scan, against the shape it forbids (what `auth.rs` held).
    assert_eq!(
        system_admin_names("        \"system\" => \"system_admin_sessions\",\n").len(),
        1
    );
    assert!(system_admin_names("        // \"system\" => \"system_admin_sessions\",\n").is_empty());
    assert!(system_admin_names("        \"tenant\" => \"tenant_admin_sessions\",\n").is_empty());
}

fn system_admin_names(text: &str) -> Vec<(usize, String)> {
    code_lines(text)
        .filter(|(_, l)| {
            l.contains("system_admin")
                || l.contains("\"system\"")
                || l.contains("ADMIN_TYPE_SYSTEM")
                || l.contains("SystemAdmin")
        })
        .map(|(i, l)| (i, l.trim().to_string()))
        .collect()
}

fn registers_exactly_admin_and_gateway(community_fn: &str) -> bool {
    let entries: Vec<&str> = community_fn
        .lines()
        .map(str::trim)
        .filter(|l| l.starts_with("(TOKEN_TYPE_") || l.starts_with("(\""))
        .collect();
    entries.len() == 2
        && entries[0].starts_with("(TOKEN_TYPE_ADMIN, Arc::new(AdminSessionVerifier))")
        && entries[1].starts_with("(TOKEN_TYPE_GATEWAY, Arc::new(GatewayTokenVerifier))")
        && !community_fn.contains("PORTAL")
        && !community_fn.contains("\"portal\"")
}

// ---------------------------------------------------------------------------
// The guards, against the shapes they forbid
// ---------------------------------------------------------------------------

#[test]
fn the_scans_see_the_shapes_they_forbid() {
    // 1. authentication that holds the portal's checks itself (the shape
    //    it forbids)
    let before = r#"
        let revoked = match portal_revocation_cache().get(jti) {
            Some(revoked) => revoked,
            None => sqlx::query_as("SELECT jti FROM end_user_revoked_tokens WHERE jti = $1"),
        };
        verify_portal_session(db, &claims).await?;
        // portal_session:: in a comment is prose
    "#;
    assert_eq!(names_portal_internals(before).len(), 3);
    assert!(
        names_portal_internals("    auth::require_token_type(&auth_ctx, TOKEN_TYPE_PORTAL)")
            .is_empty()
    );
    assert_eq!(
        names_portal_internals("    auth::require_portal_token(&headers, &secret)?;").len(),
        1
    );

    // 2. a surface that authenticates against a registry of its own
    let own_registry = r#"
        let auth_ctx = auth::authenticate_request(
            &headers,
            &tenant_id,
            &state.config.jwt_secret,
            &state.db,
            &CredentialVerifiers::community(RefuseUnregistered),
            client_ip,
            true,
        )
        .await;
    "#;
    let calls = authenticate_calls(own_registry);
    assert_eq!(calls.len(), 1);
    assert!(!calls[0].1.contains("&state.credential_verifiers,"));
    let through_state = own_registry.replace(
        "&CredentialVerifiers::community(RefuseUnregistered),",
        "&state.credential_verifiers,",
    );
    assert!(
        authenticate_calls(&through_state)[0]
            .1
            .contains("&state.credential_verifiers,")
    );
    // the definition and prose are not calls
    assert!(
        authenticate_calls("pub async fn authenticate_request(\n    headers: &HeaderMap,\n)")
            .is_empty()
    );
    assert!(authenticate_calls("    // see authenticate_request(...)").is_empty());

    // 3. Community registering the portal's type
    let with_portal = r#"pub fn community(unregistered: RefuseUnregistered) -> Self {
        Self {
            registered: vec![
                (TOKEN_TYPE_ADMIN, Arc::new(AdminSessionVerifier)),
                (TOKEN_TYPE_GATEWAY, Arc::new(GatewayTokenVerifier)),
                (TOKEN_TYPE_PORTAL, Arc::new(GatewayTokenVerifier)),
            ],
            unregistered,
        }"#;
    assert!(!registers_exactly_admin_and_gateway(with_portal));
    let as_built = with_portal.replace(
        "                (TOKEN_TYPE_PORTAL, Arc::new(GatewayTokenVerifier)),\n",
        "",
    );
    assert!(registers_exactly_admin_and_gateway(&as_built));

    // 4. production code is what is scanned, not a file's unit tests
    let file =
        "fn real() {}\n#[cfg(test)]\nmod tests {\n    fn t() { decode_gateway_jwt(a, b, c); }\n}\n";
    assert!(!production(file).contains("decode_gateway_jwt("));
}
