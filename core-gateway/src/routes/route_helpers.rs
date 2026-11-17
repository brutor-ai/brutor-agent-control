// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Route Helpers
//!
//! Common utility functions shared across route handlers (LLM, MCP, batch, etc).
//! Eliminates duplication of tenant extraction, correlation ID generation
//! and error response formatting. The caller's address is
//! `routes::request::ClientIp`.

use axum::Json;
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use serde_json::json;

use crate::middleware::tenant::TenantContext;

/// Extract tenant ID from the tenant middleware extension.
///
/// Returns `Err(Response)` with 401 (+ `WWW-Authenticate`) if tenant context is missing.
/// The error response uses OpenAI-compatible JSON format `{"error": "..."}`.
pub fn extract_tenant_id(
    tenant: Option<axum::Extension<TenantContext>>,
) -> Result<String, Response> {
    // 401, not 400: a request that carries no tenant carries no usable
    // credential (the tenant comes from the token, the key or the header),
    // and RFC 6750 clients only refresh-and-retry on 401 with a challenge.
    // Routes under `/v1/proxy` never reach here (the tenant layer answers
    // first); this is the answer for every other surface, so they agree.
    tenant.map(|t| t.tenant_id.clone()).ok_or_else(|| {
        let mut resp = openai_error(
            StatusCode::UNAUTHORIZED,
            "Missing or invalid credentials. Provide X-Tenant-ID header or a valid bearer token / API key.",
        );
        crate::services::error_envelope::attach_bearer_challenge(&mut resp);
        resp
    })
}

/// Extract or generate a correlation ID from request headers.
///
/// Uses `X-Correlation-ID` header if present, otherwise generates a new UUID.
pub fn extract_correlation_id(headers: &HeaderMap) -> String {
    headers
        .get(crate::constants::headers::X_CORRELATION_ID)
        .and_then(|v| v.to_str().ok())
        .map(String::from)
        .unwrap_or_else(|| uuid::Uuid::new_v4().to_string())
}

/// Standard OpenAI-format error response: `{"error": "message"}`.
pub fn openai_error(status: StatusCode, message: &str) -> Response {
    (status, Json(json!({"error": message}))).into_response()
}

/// Standard Anthropic-format error response.
pub fn anthropic_error(status: StatusCode, message: &str) -> Response {
    let error_type = match status.as_u16() {
        401 => "authentication_error",
        403 => "permission_error",
        404 => "not_found_error",
        429 => "rate_limit_error",
        _ => "api_error",
    };
    (
        status,
        Json(json!({
            "type": "error",
            "error": {
                "type": error_type,
                "message": message,
            }
        })),
    )
        .into_response()
}
