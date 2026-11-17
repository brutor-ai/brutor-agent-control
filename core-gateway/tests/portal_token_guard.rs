// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Portal-token guard: a portal route that accepts a gateway JWT
//! also requires it to be a portal session token.
//!
//! Admin and service tokens are signed with the same `JWT_SECRET`, so
//! `authenticate_request` accepts them as `AuthType::GatewayJwt`. A portal
//! route that stops at `auth_type != AuthType::GatewayJwt` would let an admin
//! token act as the end user whose id equals the admin's. Every such check
//! in `src/routes/*.rs` must be followed by
//! `auth::require_token_type(&auth_ctx, TOKEN_TYPE_PORTAL)`.
//!
//! The check is not
//! `auth::require_portal_token(headers, secret)`, which decoded the request's
//! headers a second time; it is now a question to the context authentication
//! returned (the type the credential-verifier registry verified the token
//! as). The old function is gone, and this guard also fails if it comes back.
#[path = "support/roots.rs"]
mod roots;

use std::fs;

const GATE: &str = "!= AuthType::GatewayJwt";
/// The check, on an owned context or on a reference to one.
const CHECKS: [&str; 2] = [
    "require_token_type(&auth_ctx, TOKEN_TYPE_PORTAL)",
    "require_token_type(auth_ctx, TOKEN_TYPE_PORTAL)",
];
/// The forbidden check: a second decoding of the headers, apart from the
/// token that was authenticated.
const OLD_CHECK: &str = "require_portal_token(";
/// How far after the gate the check must appear (the gate's error return
/// sits in between).
const WINDOW: usize = 400;
/// The shared gate the Enterprise portal routes call.
const SHARED_GATE: &str = "request::portal_user_id(";

/// Gates in `src` with no portal-token check right after them, as line
/// numbers.
fn unchecked_gates(src: &str) -> Vec<usize> {
    let mut out = Vec::new();
    let mut from = 0;
    while let Some(i) = src[from..].find(GATE) {
        let at = from + i;
        let end = (at + WINDOW).min(src.len());
        let end = (end..=src.len())
            .find(|e| src.is_char_boundary(*e))
            .unwrap_or(src.len());
        if !CHECKS.iter().any(|c| src[at..end].contains(c)) {
            out.push(src[..at].lines().count());
        }
        from = at + GATE.len();
    }
    out
}

#[test]
fn every_portal_jwt_gate_requires_a_portal_token() {
    let mut found = Vec::new();
    let mut old = Vec::new();
    let mut gates = 0;
    // The Community routes and, where this repository carries the Enterprise
    // workspace, its routes (the guard is root-aware).
    let mut entries: Vec<std::path::PathBuf> = Vec::new();
    let mut dirs = vec![std::path::PathBuf::from("src/routes")];
    let enterprise = roots::tree("enterprise");
    if let Some(enterprise) = &enterprise {
        dirs.push(enterprise.join("src/routes"));
    }
    for dir in dirs {
        entries.extend(
            fs::read_dir(&dir)
                .unwrap_or_else(|e| panic!("{}: {e}", dir.display()))
                .flatten()
                .map(|e| e.path()),
        );
    }
    for p in entries {
        let name = p
            .strip_prefix("src/routes")
            .map(|n| n.to_string_lossy().to_string())
            .unwrap_or_else(|_| {
                format!("(enterprise) {}", p.file_name().unwrap().to_string_lossy())
            });
        // Every route module, not only `portal_*`: the shared
        // `extract_portal_user` (and its gate) is in `request.rs`, and a gate
        // anywhere under routes/ must be followed by the check.
        if !name.ends_with(".rs") {
            continue;
        }
        let src = fs::read_to_string(&p).unwrap();
        gates += src.matches(GATE).count();
        if src.contains(OLD_CHECK) {
            old.push(format!("src/routes/{name}"));
        }
        for line in unchecked_gates(&src) {
            found.push(format!("src/routes/{name}:{line}"));
        }
    }
    // The one gate is `request::portal_user_id` in `request.rs`; the
    // Enterprise portal routes that authenticate a session themselves call it
    // (`portal_auth.rs` once, `portal_models.rs` twice) instead of a gate of
    // their own.
    assert!(gates >= 1, "expected the portal JWT gate; found {gates}");
    if let Some(enterprise) = &enterprise {
        let shared = ["portal_auth.rs", "portal_models.rs"]
            .iter()
            .map(|f| {
                fs::read_to_string(enterprise.join("src/routes").join(f))
                    .unwrap_or_else(|e| panic!("{f}: {e}"))
                    .matches(SHARED_GATE)
                    .count()
            })
            .sum::<usize>();
        assert!(
            shared >= 3,
            "the Enterprise portal routes call {SHARED_GATE} for each session they \
             authenticate; found {shared}"
        );
    }
    assert!(
        found.is_empty(),
        "portal routes accept any gateway JWT (admin/service tokens too); call \
         auth::require_token_type(&auth_ctx, TOKEN_TYPE_PORTAL) after the GatewayJwt check:\n{}",
        found.join("\n")
    );
    assert!(
        old.is_empty(),
        "require_portal_token (a second decoding of the headers) is back; ask the \
         authenticated context with auth::require_token_type:\n{}",
        old.join("\n")
    );
}

#[test]
fn the_guard_catches_a_gate_without_the_check() {
    let unguarded = r#"
        if auth_ctx.auth_type != AuthType::GatewayJwt {
            return error(StatusCode::UNAUTHORIZED, "Gateway JWT authentication required");
        }
        let user_id = auth_ctx.gateway_user_id.clone();
    "#;
    assert_eq!(unchecked_gates(unguarded).len(), 1);
    let guarded = r#"
        if auth_ctx.auth_type != AuthType::GatewayJwt {
            return error(StatusCode::UNAUTHORIZED, "Gateway JWT authentication required");
        }
        auth::require_token_type(&auth_ctx, TOKEN_TYPE_PORTAL)?;
    "#;
    assert!(unchecked_gates(guarded).is_empty());
    assert!(unchecked_gates(&guarded.replace("(&auth_ctx,", "(auth_ctx,")).is_empty());
    // Asking for another type, or decoding the headers again, is not the check.
    let another_type = guarded.replace("TOKEN_TYPE_PORTAL", "TOKEN_TYPE_ADMIN");
    assert_eq!(unchecked_gates(&another_type).len(), 1);
    let old_check = guarded.replace(
        "auth::require_token_type(&auth_ctx, TOKEN_TYPE_PORTAL)?;",
        "auth::require_portal_token(&effective_headers, &state.config.jwt_secret)?;",
    );
    assert_eq!(unchecked_gates(&old_check).len(), 1);
    assert!(old_check.contains(OLD_CHECK));
}
