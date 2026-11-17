// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The largest request the runner accepts follows from the largest skill.
//!
//! A skill is at most 30,000,000 bytes of files, uncompressed (the Control
//! Plane's `SKILL_MAX_BYTES`, checked at upload). The Core Gateway sends them
//! in one JSON request, a binary file base64-encoded: 4 bytes for every 3.
//! So the runner accepts 30,000,000 × 4/3 plus 1 MiB for the JSON around the
//! files, and the Core Gateway derives its own limit the same way: three
//! definitions, written the same in both repositories.

use std::fs;
use std::path::PathBuf;

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use brutor_skill_runner::api::{AppState, build_router};
use brutor_skill_runner::config::{
    Config, MAX_REQUEST_BYTES, REQUEST_FRAMING_BYTES, SKILL_MAX_BYTES,
};
use jsonwebtoken::{EncodingKey, Header, encode};
use tower::ServiceExt;

const SECRET: &str = "request-limit-secret-at-least-32-characters";

#[test]
fn the_request_limit_is_the_largest_skill_base64_encoded_plus_framing() {
    assert_eq!(SKILL_MAX_BYTES, 30_000_000);
    assert_eq!(REQUEST_FRAMING_BYTES, 1_048_576);
    assert_eq!(MAX_REQUEST_BYTES, 40_000_000 + 1_048_576);
}

/// The three definitions as one repository writes them: `name = expression`.
fn definitions(source: &str) -> Vec<(String, String)> {
    [
        "SKILL_MAX_BYTES",
        "REQUEST_FRAMING_BYTES",
        "MAX_REQUEST_BYTES",
    ]
    .iter()
    .map(|name| {
        let start = format!("pub const {name}: usize = ");
        let at = source
            .find(&start)
            .unwrap_or_else(|| panic!("no `{start}…;`"));
        let rest = &source[at + start.len()..];
        let expression = &rest[..rest.find(';').expect("the definition ends")];
        (name.to_string(), expression.trim().to_string())
    })
    .collect()
}

#[test]
fn the_core_gateway_derives_the_same_limit() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let mine = definitions(&fs::read_to_string(root.join("src/config.rs")).unwrap());
    let core = root.join("../brutor-gateway-core/src/services/skill_limits.rs");
    match fs::read_to_string(&core) {
        Ok(theirs) => assert_eq!(
            mine,
            definitions(&theirs),
            "the Core Gateway's limits ({}) differ from the runner's",
            core.display()
        ),
        // A checkout of this repository alone (CI) has no siblings.
        Err(_) => {
            eprintln!("SKIPPED: brutor-gateway-core is not checked out beside this repository")
        }
    }
}

fn router() -> axum::Router {
    let config = Config {
        listen_addr: "127.0.0.1:0".parse().unwrap(),
        jwt_secret: SECRET.to_string(),
        sandbox_timeout_secs: 5,
        trusted_timeout_secs: 5,
        workspace_root: std::env::temp_dir().join("brutor-request-limit-test"),
        trusted_env: Default::default(),
    };
    build_router(AppState::new(config))
}

fn token() -> String {
    let exp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs()
        + 300;
    encode(
        &Header::default(),
        &serde_json::json!({"exec": "e1", "tnt": "t", "skl": "s", "exp": exp}),
        &EncodingKey::from_secret(SECRET.as_bytes()),
    )
    .unwrap()
}

async fn post_bytes(len: usize) -> StatusCode {
    let request = Request::post("/v1/runner/execute")
        .header(header::AUTHORIZATION, format!("Bearer {}", token()))
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from(vec![b' '; len]))
        .unwrap();
    router().oneshot(request).await.unwrap().status()
}

#[tokio::test]
async fn a_request_of_the_largest_skill_is_not_refused_for_its_size() {
    // Whitespace only: not a valid request, but read in full first.
    let status = post_bytes(MAX_REQUEST_BYTES).await;
    assert_ne!(status, StatusCode::PAYLOAD_TOO_LARGE);
    assert!(status.is_client_error(), "{status}");
}

#[tokio::test]
async fn one_byte_more_is_refused_with_413() {
    assert_eq!(
        post_bytes(MAX_REQUEST_BYTES + 1).await,
        StatusCode::PAYLOAD_TOO_LARGE
    );
}
