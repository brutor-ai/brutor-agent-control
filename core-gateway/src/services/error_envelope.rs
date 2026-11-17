// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
//! Canonical error envelope for the proxy's HTTP routes.
//!
//! # Why this exists
//!
//! Every route file used to ship its own `fn error_response(status,
//! message) -> Response` doing the same thing:
//!
//! ```ignore
//! json!({ "error": message })
//! ```
//!
//! That works fine when `message` is a plain string, but the LLM
//! routes pass through *upstream provider error bodies* — Groq's
//! rate-limit JSON, OpenAI's content-policy envelope, etc. — as the
//! `message` argument. Those bodies are themselves
//! `{"error":{"code":"...","message":"..."}}`, and stuffing them
//! into the outer `{error: <string>}` produced this monstrosity:
//!
//! ```json
//! {"error":"{\"error\":{\"code\":\"rate_limit_exceeded\",
//!            \"message\":\"Request too large…\"}}"}
//! ```
//!
//! The portal then took the outer string at face value and rendered
//! the JSON-soup verbatim — operators saw an unreadable blob instead
//! of the human message Groq gave them. This module provides one
//! helper that all routes call, so the canonical client-facing shape
//! is the same regardless of which route emitted the error and
//! regardless of whether the inner payload was already structured.
//!
//! # Canonical shape
//!
//! ```json
//! { "error": { "message": "<text>", "code": "...", "type": "..." } }
//! ```
//!
//! Compatible with the OpenAI error envelope. Clients only need to
//! read `body.error.message` (with `.code` / `.type` optional) — see
//! the symmetric defensive parsing in the Portal's OpenAI client.
//!
//! # Routes that should NOT use this helper
//!
//! Some endpoints carry a wire-shape mandated by their protocol and
//! must keep emitting their own envelope:
//!
//! - **A2A v1.0** (`routes/a2a_v1.rs`) — Google RPC-style envelope
//!   via `services::a2a_v1::A2aError`.
//! - **MCP JSON-RPC** — `_McpError` / `code` / `message` per
//!   modelcontextprotocol.io spec.
//! - **OAuth protocol-level** — RFC 6749's
//!   `{error:"invalid_grant",error_description:"..."}` shape
//!   (different from `oauth.rs`'s admin-flow `error_response` which
//!   is fine to migrate).
//!
//! Everything else (LLM, batch, MCP admin, A2A admin, portal MCP
//! apps, skills, vmcp, oauth-admin) shares this envelope.

use axum::{
    Json,
    http::{HeaderValue, StatusCode},
    response::{IntoResponse, Response},
};
use serde_json::{Value, json};

/// RFC 6750 §3 challenge emitted on every 401 from this helper.
/// `realm="brutor"` identifies the protection space; `error="invalid_token"`
/// is the spec-defined code for a missing / malformed / expired credential
/// on a Bearer-protected resource. SDKs key off these values to decide
/// whether to refresh and retry vs. surface a login prompt.
pub const WWW_AUTHENTICATE_BEARER: &str = r#"Bearer realm="brutor", error="invalid_token""#;

/// Attach the RFC 6750 §3 Bearer challenge to a response. Used by
/// route-local `error()` helpers and inline 401 emitters whose body
/// shape differs from the canonical envelope and therefore can't go
/// through `canonical_error_response`. Idempotent — if the header
/// is already present (e.g. MCP route set its own), this is a no-op.
pub fn attach_bearer_challenge(resp: &mut Response) {
    let h = resp.headers_mut();
    if !h.contains_key(axum::http::header::WWW_AUTHENTICATE) {
        h.insert(
            axum::http::header::WWW_AUTHENTICATE,
            HeaderValue::from_static(WWW_AUTHENTICATE_BEARER),
        );
    }
}

/// Build a canonical `{"error":{"message":"..."}}` HTTP response.
///
/// `message` is interpreted in three steps:
///
/// 1. **Structured upstream envelope** —
///    `{"error":{"code":"...","message":"...","type":"..."}}`. The
///    inner `error` object is forwarded verbatim so provider-specific
///    fields (Groq's `type:"tokens"`, OpenAI's `code:"content_policy
///    _violation"`, etc.) survive the round-trip.
/// 2. **String-valued upstream envelope** — `{"error":"<text>"}`.
///    The inner string becomes `error.message`.
/// 3. **Plain text** — wrapped as `{"error":{"message":"<text>"}}`.
///
/// Never produces `{"error":"<JSON-encoded blob>"}` — that's the bug
/// this helper exists to prevent.
pub fn canonical_error_response(status: StatusCode, message: &str) -> Response {
    let mut resp = if let Ok(parsed) = serde_json::from_str::<Value>(message) {
        if let Some(err_obj) = parsed.get("error") {
            if err_obj.is_object() {
                (status, Json(json!({ "error": err_obj }))).into_response()
            } else if let Some(s) = err_obj.as_str() {
                (status, Json(json!({ "error": { "message": s } }))).into_response()
            } else {
                (status, Json(json!({ "error": { "message": message } }))).into_response()
            }
        } else {
            (status, Json(json!({ "error": { "message": message } }))).into_response()
        }
    } else {
        (status, Json(json!({ "error": { "message": message } }))).into_response()
    };

    // RFC 6750 §3 / RFC 6585 §3: 401 responses on a Bearer-protected
    // resource MUST carry a `WWW-Authenticate` challenge so the client
    // knows which scheme to retry with. Without it, SDKs treat the
    // failure as opaque and either give up or retry blindly.
    if status == StatusCode::UNAUTHORIZED {
        resp.headers_mut().insert(
            axum::http::header::WWW_AUTHENTICATE,
            HeaderValue::from_static(WWW_AUTHENTICATE_BEARER),
        );
    }

    resp
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::to_bytes;

    async fn read_body(resp: Response) -> Value {
        let bytes = to_bytes(resp.into_body(), 64 * 1024).await.expect("body");
        serde_json::from_slice(&bytes).expect("body should be JSON")
    }

    #[tokio::test]
    async fn unwraps_structured_provider_envelope() {
        // Real-world Groq rate-limit body. Inner object forwarded
        // verbatim — `code`, `type`, and `message` all survive.
        let upstream = r#"{"error":{"code":"rate_limit_exceeded","message":"Request too large for model `openai/gpt-oss-120b` … TPM: Limit 8000, Requested 11015","type":"tokens"}}"#;
        let resp = canonical_error_response(StatusCode::TOO_MANY_REQUESTS, upstream);
        assert_eq!(resp.status(), StatusCode::TOO_MANY_REQUESTS);

        let body = read_body(resp).await;
        assert_eq!(body["error"]["code"], "rate_limit_exceeded");
        assert_eq!(body["error"]["type"], "tokens");
        assert!(
            body["error"]["message"]
                .as_str()
                .unwrap()
                .contains("TPM: Limit 8000")
        );
    }

    #[tokio::test]
    async fn unwraps_provider_envelope_with_string_error() {
        let upstream = r#"{"error":"upstream timed out"}"#;
        let resp = canonical_error_response(StatusCode::BAD_GATEWAY, upstream);

        let body = read_body(resp).await;
        assert_eq!(body["error"]["message"], "upstream timed out");
    }

    #[tokio::test]
    async fn wraps_plain_text_messages() {
        let resp = canonical_error_response(StatusCode::INTERNAL_SERVER_ERROR, "no model selected");

        let body = read_body(resp).await;
        assert_eq!(body["error"]["message"], "no model selected");
    }

    #[tokio::test]
    async fn never_produces_double_wrapped_string_error() {
        // Defensive: even when the input is already canonical, the
        // top-level `error` field is an object — never a stringified
        // blob. Regression guard for the original bug.
        let already_canonical = r#"{"error":{"message":"foo"}}"#;
        let resp = canonical_error_response(StatusCode::BAD_REQUEST, already_canonical);
        let body = read_body(resp).await;
        assert!(body["error"].is_object());
        assert_eq!(body["error"]["message"], "foo");
    }

    #[tokio::test]
    async fn handles_invalid_json_gracefully() {
        // Truncated upstream body — falls through to plain-text
        // wrapping rather than panicking. The body bytes are
        // preserved so an operator can still see what came back.
        let raw = r#"{"error":{"message":"truncated"#;
        let resp = canonical_error_response(StatusCode::BAD_GATEWAY, raw);
        let body = read_body(resp).await;
        assert_eq!(body["error"]["message"], raw);
    }

    #[tokio::test]
    async fn handles_empty_message() {
        let resp = canonical_error_response(StatusCode::BAD_REQUEST, "");
        let body = read_body(resp).await;
        assert_eq!(body["error"]["message"], "");
    }

    // -----------------------------------------------------------------------
    // WWW-Authenticate challenge on 401 (RFC 6750 §3 / AUTH-23)
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn unauthorized_sets_www_authenticate_bearer_challenge() {
        let resp = canonical_error_response(StatusCode::UNAUTHORIZED, "Invalid API key");
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
        let v = resp
            .headers()
            .get(axum::http::header::WWW_AUTHENTICATE)
            .expect("WWW-Authenticate header required on 401 per RFC 6750 §3");
        assert_eq!(
            v.to_str().unwrap(),
            r#"Bearer realm="brutor", error="invalid_token""#
        );
    }

    #[tokio::test]
    async fn unauthorized_challenge_emitted_even_for_structured_upstream_envelope() {
        // 401 originating from a provider that already wrapped the
        // error — the header still has to land.
        let upstream = r#"{"error":{"code":"invalid_api_key","message":"key revoked"}}"#;
        let resp = canonical_error_response(StatusCode::UNAUTHORIZED, upstream);
        assert!(
            resp.headers()
                .contains_key(axum::http::header::WWW_AUTHENTICATE)
        );
    }

    #[tokio::test]
    async fn non_401_responses_do_not_carry_www_authenticate() {
        // 403 / 429 / 500 etc. must NOT advertise a Bearer challenge —
        // those statuses don't mean "authenticate", and adding the
        // header would mislead clients into retrying with new creds.
        for status in [
            StatusCode::BAD_REQUEST,
            StatusCode::FORBIDDEN,
            StatusCode::NOT_FOUND,
            StatusCode::TOO_MANY_REQUESTS,
            StatusCode::INTERNAL_SERVER_ERROR,
            StatusCode::BAD_GATEWAY,
        ] {
            let resp = canonical_error_response(status, "denied");
            assert!(
                !resp
                    .headers()
                    .contains_key(axum::http::header::WWW_AUTHENTICATE),
                "WWW-Authenticate must not be set on {status}",
            );
        }
    }
}
