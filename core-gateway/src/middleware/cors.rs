// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Cross-origin policy.
//!
//! The router used to carry `CorsLayer::permissive()`: any web page could
//! read any response a browser could fetch from the proxy, including the
//! unauthenticated `/health` (component state), `/v1/version` and the API
//! docs, from a browser that can reach a private deployment.
//!
//! Two classes of route:
//!
//! * **Protocol routes** ([`OPEN_PREFIXES`]): the LLM, MCP, A2A and OAuth
//!   surfaces and `/.well-known`. Browser-based clients (MCP Inspector, web
//!   apps holding their own API key, OAuth discovery) call them from origins
//!   the operator cannot list, so any origin may read them. They
//!   authenticate with a header the page must already hold, so an open
//!   origin grants nothing the page did not have.
//! * **Everything else** (the portal API, health, version, docs, runs,
//!   audit, assurance, evidence, ingest, public theme, assets): only the
//!   configured origins (`BRUTOR_CORS_ORIGINS`, the same variable the control
//!   plane reads; default the local portal and admin console). A new route
//!   is in this class unless it is added to the open list on purpose.
//!
//! No route ever sends `Access-Control-Allow-Credentials`: the proxy sets
//! no cookies, and the portal sends its token in a header.

use axum::http::HeaderValue;
use axum::http::request::Parts;
use tower_http::cors::{AllowHeaders, AllowMethods, AllowOrigin, CorsLayer, ExposeHeaders};

/// Path prefixes any origin may read.
pub const OPEN_PREFIXES: &[&str] = &[
    "/v1/proxy/",
    "/v1/messages",
    "/v1beta/",
    "/a2a/",
    "/.well-known/",
];

/// Origins allowed when `BRUTOR_CORS_ORIGINS` is unset: the local portal and
/// admin console. Never `*`.
pub const DEFAULT_ORIGINS: &[&str] = &["http://localhost:3001", "http://localhost:3002"];

/// Whether `path` is a protocol route any origin may read.
pub fn is_open_path(path: &str) -> bool {
    OPEN_PREFIXES.iter().any(|p| path.starts_with(p))
}

/// Parse `BRUTOR_CORS_ORIGINS` (comma-separated). Unset or empty gives
/// [`DEFAULT_ORIGINS`]; a trailing `/` is dropped so `https://x/` matches
/// the `Origin` header `https://x`.
pub fn parse_origins(raw: Option<&str>) -> Vec<String> {
    let parsed: Vec<String> = raw
        .unwrap_or("")
        .split(',')
        .map(|s| s.trim().trim_end_matches('/').to_string())
        .filter(|s| !s.is_empty())
        .collect();
    if parsed.is_empty() {
        DEFAULT_ORIGINS.iter().map(|s| s.to_string()).collect()
    } else {
        parsed
    }
}

/// The layer for the main router: the protocol routes for every origin,
/// the rest for `origins` only, never with credentials.
pub fn layer(origins: &[String]) -> CorsLayer {
    let origins: Vec<String> = origins.to_vec();
    let allow = AllowOrigin::predicate(move |origin: &HeaderValue, parts: &Parts| {
        is_open_path(parts.uri.path())
            || origin
                .to_str()
                .map(|o| origins.iter().any(|allowed| allowed == o))
                .unwrap_or(false)
    });
    CorsLayer::new()
        .allow_origin(allow)
        .allow_methods(AllowMethods::mirror_request())
        .allow_headers(AllowHeaders::mirror_request())
        .expose_headers(ExposeHeaders::any())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unset_or_blank_origins_fall_back_to_the_local_apps_never_any() {
        for raw in [None, Some(""), Some(" , ")] {
            let o = parse_origins(raw);
            assert_eq!(o, DEFAULT_ORIGINS);
            assert!(!o.iter().any(|x| x == "*"));
        }
        assert_eq!(
            parse_origins(Some(
                "https://portal.example.com/, https://admin.example.com"
            )),
            vec!["https://portal.example.com", "https://admin.example.com"]
        );
    }

    #[test]
    fn protocol_paths_are_open_and_the_rest_is_not() {
        for p in [
            "/v1/proxy/mcp/srv",
            "/v1/proxy/llm/chat/completions",
            "/v1/proxy/oauth/register",
            "/v1/messages",
            "/v1beta/models/gemini:generateContent",
            "/a2a/agent",
            "/.well-known/oauth-protected-resource",
        ] {
            assert!(is_open_path(p), "{p}");
        }
        for p in [
            "/v1/portal/models",
            "/v1/portal/auth/login",
            "/health",
            "/v1/version",
            "/docs",
            "/api-docs/openapi.json",
            "/v1/runs/x",
            "/v1/evidence/seal",
            "/v1/public/tenants/t/theme",
            "/metrics",
        ] {
            assert!(!is_open_path(p), "{p}");
        }
    }
}
