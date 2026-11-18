// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: every governed surface judges the caller's credential against the
//! call's identity policy before it serves the call.
//!
//! The bug class: a route authenticates the caller (`authenticate_request`,
//! the media caller) and goes on to serve it without asking
//! `services::identity_policy` whether that credential is enough for the
//! AI System it acts in, so a copied API key reaches a production system
//! through the one surface that forgot.
//!
//! Every non-test function in `FILES` that authenticates must also judge
//! (`identity_policy::judge_call`, or a helper that does it inside: the LLM
//! preflight, the multimodal preflight, the media caller), or be listed in
//! `EXEMPT` with the reason its caller is never one an identity policy
//! judges. Every source file that authenticates must be in `FILES`, so a new
//! surface cannot escape the list. The Enterprise workspace has the same
//! guard over its own surfaces.

#[path = "support/identity_surface_scan.rs"]
mod scan;

use std::fs;
use std::path::Path;

/// The files with governed surfaces (and those that would be, if they
/// authenticated directly).
const FILES: [&str; 21] = [
    "src/services/llm_preflight.rs",
    "src/services/multimodal_pipeline.rs",
    "src/services/service_auth.rs",
    "src/routes/llm.rs",
    "src/routes/anthropic.rs",
    "src/routes/gemini.rs",
    "src/routes/mcp.rs",
    "src/routes/vmcp.rs",
    "src/routes/skills.rs",
    "src/routes/a2a.rs",
    "src/routes/a2a_v1.rs",
    "src/routes/request.rs",
    "src/routes/otel_ingest.rs",
    "src/routes/runs.rs",
    "src/routes/approvals.rs",
    "src/routes/oauth.rs",
    "src/routes/files.rs",
    "src/routes/images.rs",
    "src/routes/audio_speech.rs",
    "src/routes/audio_transcribe.rs",
    "src/routes/video.rs",
];

/// `(file, function, why its caller is never judged by an identity policy)`.
const EXEMPT: [(&str, &str, &str); 4] = [
    (
        "src/routes/request.rs",
        "authenticate_caller",
        "the shared helper; its two callers judge (extract_media_caller) or accept only a \
         portal session (extract_portal_user)",
    ),
    (
        "src/routes/request.rs",
        "extract_portal_user",
        "accepts only a person's portal session, which no AI System policy judges",
    ),
    (
        "src/routes/oauth.rs",
        "token_exchange",
        "authentication is optional and only names the gateway user a stored OAuth token \
         belongs to; that token is judged where it is used",
    ),
    (
        "src/services/service_auth.rs",
        "require_service_principal",
        "accepts only the platform service key or an administrator's session, both outside \
         the assurance ladder",
    ),
];

#[test]
fn every_surface_that_authenticates_judges_the_credential() {
    let mut found = Vec::new();
    for file in FILES {
        let src = fs::read_to_string(file).unwrap_or_else(|e| panic!("{file}: {e}"));
        found.extend(scan::violations(file, &src, &EXEMPT));
    }
    assert!(
        found.is_empty(),
        "these authenticate a caller and serve it without judging its credential against \
         the call's identity policy; call services::identity_policy::judge_call after \
         authentication, or add an EXEMPT entry saying why the caller is never judged:\n{}",
        found.join("\n")
    );
}

#[test]
fn every_file_that_authenticates_is_in_the_list() {
    let missing: Vec<String> = scan::files_that_authenticate(Path::new("."), "src")
        .into_iter()
        .filter(|file| file != "src/services/auth.rs")
        .filter(|file| !FILES.contains(&file.as_str()))
        .collect();
    assert!(
        missing.is_empty(),
        "these source files authenticate callers and are not in this guard's FILES: {missing:?}"
    );
}

#[test]
fn every_exemption_still_names_an_authentication() {
    for (file, name, why) in EXEMPT {
        assert!(!why.is_empty());
        let src = fs::read_to_string(file).unwrap();
        assert!(
            scan::functions(&src)
                .iter()
                .any(|(n, body)| n == name && scan::AUTH.iter().any(|a| body.contains(a))),
            "stale exemption {file}::{name}"
        );
    }
}

/// The helpers other surfaces lean on judge themselves.
#[test]
fn the_helpers_that_judge_for_their_callers_do() {
    for (file, name) in [
        ("src/services/llm_preflight.rs", "run_preflight"),
        ("src/routes/request.rs", "extract_media_caller"),
    ] {
        let src = fs::read_to_string(file).unwrap();
        let body = scan::functions(&src)
            .into_iter()
            .find(|(n, _)| n == name)
            .unwrap_or_else(|| panic!("{file}::{name} is gone"))
            .1;
        assert!(
            body.contains("identity_policy::judge_call("),
            "{file}::{name} is counted as judging for its callers but does not judge"
        );
    }
}

#[test]
fn the_rule_catches_a_surface_that_serves_without_the_verdict() {
    scan::check_the_rule();
}
