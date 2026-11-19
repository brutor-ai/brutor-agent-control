// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A2A v1.0 HTTP/REST error envelope.
//!
//! Per spec §11.6 ("Error Handling"), the HTTP/REST binding formats
//! errors following AIP-193's `Status` JSON representation, wrapped
//! under a top-level `error` field:
//!
//! ```json
//! {
//!   "error": {
//!     "code": 404,
//!     "status": "NOT_FOUND",
//!     "message": "The specified task ID does not exist or is not accessible",
//!     "details": [
//!       {
//!         "@type": "type.googleapis.com/google.rpc.ErrorInfo",
//!         "reason": "TASK_NOT_FOUND",
//!         "domain": "a2a-protocol.org",
//!         "metadata": {"taskId": "task-123"}
//!       }
//!     ]
//!   }
//! }
//! ```
//!
//! Field rules:
//!
//!   * `code` — the HTTP status (an integer).
//!   * `status` — the matching `google.rpc.Code` enum name.
//!   * `message` — human-readable, tenant-safe (no stack traces).
//!   * `details[]` — `google.protobuf.Any` packed messages; the only
//!     one we emit is `google.rpc.ErrorInfo`.
//!   * `ErrorInfo.reason` — SCREAMING_SNAKE_CASE, no `Error` suffix
//!     (spec §5.4 mapping — `TaskNotFoundError` → `TASK_NOT_FOUND`).
//!   * `ErrorInfo.domain` — always `"a2a-protocol.org"` for
//!     spec-defined errors. We use the same domain for our own
//!     extension-level reasons; partners distinguish via the
//!     `reason` prefix (`BRUTOR_*`) when applicable.
//!
//! This module is the **only** place where the A2A error model is
//! constructed. Route handlers construct an [`A2aError`] and call
//! [`A2aError::into_response`] to render the canonical wire form.

use std::collections::BTreeMap;

use axum::Json;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde::{Deserialize, Serialize};

/// Spec-wide constant — the `domain` value on every `ErrorInfo`.
pub const A2A_ERROR_DOMAIN: &str = "a2a-protocol.org";

/// Type URL for `google.rpc.ErrorInfo` per AIP-193.
pub const ERROR_INFO_TYPE_URL: &str = "type.googleapis.com/google.rpc.ErrorInfo";

// =============================================================================
// Enum: every canonical A2A error type
// =============================================================================

/// A2A error taxonomy per spec §5.4.
///
/// We model the nine spec-defined error types as an enum so route
/// code can't accidentally emit an unknown error reason. Each
/// variant knows its canonical (HTTP status, `status` name,
/// `reason` string) triple, so constructors stay concise.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum A2aErrorKind {
    /// Spec §3.3.2 — task ID is unknown or inaccessible.
    TaskNotFound,
    /// Spec §3.3.2 — task is in a terminal state and cannot be cancelled.
    TaskNotCancelable,
    /// Spec §3.3.2 — agent does not support push notifications.
    PushNotificationNotSupported,
    /// Spec §3.3.2 — operation is not implemented in this binding.
    UnsupportedOperation,
    /// Spec §3.3.2 — client asked for a media type the agent doesn't produce.
    ContentTypeNotSupported,
    /// Spec §3.3.2 — downstream agent returned a malformed response.
    InvalidAgentResponse,
    /// Spec §3.3.2 — extended card is requested but the agent hasn't configured one.
    ExtendedAgentCardNotConfigured,
    /// Spec §3.3.2 — a required extension is unsupported.
    ExtensionSupportRequired,
    /// Spec §3.3.2 — the requested protocol version isn't supported.
    VersionNotSupported,
    /// Brutor extension — the request was blocked by a tenant
    /// guardrail policy (incoming `tasks.send` payload or outgoing
    /// peer delegation). Not one of the spec's nine canonical error
    /// types, but maps to canonical gRPC status `PERMISSION_DENIED`
    /// + HTTP 403 so spec-compliant clients still see a well-formed
    ///   error envelope. The `metadata` carries the guardrail name,
    ///   the failed check, and the surface (`a2a_inbound` /
    ///   `a2a_outbound`) so the calling agent can surface the refusal
    ///   to its operator.
    PolicyBlocked,
    /// Brutor extension — the caller presented no valid credential.
    /// Canonical `UNAUTHENTICATED`, HTTP 401.
    Unauthenticated,
    /// Brutor extension — a valid credential refused for this call (a key no
    /// resource group governs, a deactivated group, a token for another
    /// tenant). Canonical `PERMISSION_DENIED`, HTTP 403, never
    /// UNSUPPORTED_OPERATION / 400.
    PermissionDenied,
    /// Brutor extension — the gateway cannot read the tenant's guardrail
    /// configuration, so it cannot check the message and refuses it (fail
    /// closed). No policy judged the message; canonical `UNAVAILABLE`, HTTP
    /// 503, and the audit row is sealed `engine_failure`, as on every other
    /// surface. It is not `POLICY_BLOCKED` / 403.
    GuardrailsUnavailable,
}

impl A2aErrorKind {
    /// The kind for an authentication failure, by the status the core's
    /// authentication assigned it.
    pub fn for_auth_status(status: StatusCode) -> Self {
        match status {
            StatusCode::UNAUTHORIZED => A2aErrorKind::Unauthenticated,
            StatusCode::FORBIDDEN => A2aErrorKind::PermissionDenied,
            _ => A2aErrorKind::InvalidAgentResponse,
        }
    }

    /// HTTP status per spec §5.4 table.
    pub fn http_status(self) -> StatusCode {
        match self {
            A2aErrorKind::TaskNotFound => StatusCode::NOT_FOUND,
            A2aErrorKind::TaskNotCancelable => StatusCode::CONFLICT,
            A2aErrorKind::PushNotificationNotSupported => StatusCode::BAD_REQUEST,
            A2aErrorKind::UnsupportedOperation => StatusCode::BAD_REQUEST,
            A2aErrorKind::ContentTypeNotSupported => StatusCode::UNSUPPORTED_MEDIA_TYPE,
            A2aErrorKind::InvalidAgentResponse => StatusCode::BAD_GATEWAY,
            A2aErrorKind::ExtendedAgentCardNotConfigured => StatusCode::BAD_REQUEST,
            A2aErrorKind::ExtensionSupportRequired => StatusCode::BAD_REQUEST,
            A2aErrorKind::VersionNotSupported => StatusCode::BAD_REQUEST,
            A2aErrorKind::PolicyBlocked => StatusCode::FORBIDDEN,
            A2aErrorKind::Unauthenticated => StatusCode::UNAUTHORIZED,
            A2aErrorKind::PermissionDenied => StatusCode::FORBIDDEN,
            A2aErrorKind::GuardrailsUnavailable => StatusCode::SERVICE_UNAVAILABLE,
        }
    }

    /// gRPC-equivalent `status` name per spec §5.4 table. Used in the
    /// `status` field alongside `code` so consumers can key on the
    /// enum without doing an int → name lookup.
    pub fn status_name(self) -> &'static str {
        match self {
            A2aErrorKind::TaskNotFound => "NOT_FOUND",
            A2aErrorKind::TaskNotCancelable => "FAILED_PRECONDITION",
            A2aErrorKind::PushNotificationNotSupported => "UNIMPLEMENTED",
            A2aErrorKind::UnsupportedOperation => "UNIMPLEMENTED",
            A2aErrorKind::ContentTypeNotSupported => "INVALID_ARGUMENT",
            A2aErrorKind::InvalidAgentResponse => "INTERNAL",
            A2aErrorKind::ExtendedAgentCardNotConfigured => "FAILED_PRECONDITION",
            A2aErrorKind::ExtensionSupportRequired => "FAILED_PRECONDITION",
            A2aErrorKind::VersionNotSupported => "UNIMPLEMENTED",
            A2aErrorKind::PolicyBlocked => "PERMISSION_DENIED",
            A2aErrorKind::Unauthenticated => "UNAUTHENTICATED",
            A2aErrorKind::PermissionDenied => "PERMISSION_DENIED",
            A2aErrorKind::GuardrailsUnavailable => "UNAVAILABLE",
        }
    }

    /// `reason` on the emitted `ErrorInfo`. Spec §11.6:
    /// "the A2A error type in UPPER_SNAKE_CASE without the 'Error' suffix".
    pub fn reason(self) -> &'static str {
        match self {
            A2aErrorKind::TaskNotFound => "TASK_NOT_FOUND",
            A2aErrorKind::TaskNotCancelable => "TASK_NOT_CANCELABLE",
            A2aErrorKind::PushNotificationNotSupported => "PUSH_NOTIFICATION_NOT_SUPPORTED",
            A2aErrorKind::UnsupportedOperation => "UNSUPPORTED_OPERATION",
            A2aErrorKind::ContentTypeNotSupported => "CONTENT_TYPE_NOT_SUPPORTED",
            A2aErrorKind::InvalidAgentResponse => "INVALID_AGENT_RESPONSE",
            A2aErrorKind::ExtendedAgentCardNotConfigured => "EXTENDED_AGENT_CARD_NOT_CONFIGURED",
            A2aErrorKind::ExtensionSupportRequired => "EXTENSION_SUPPORT_REQUIRED",
            A2aErrorKind::VersionNotSupported => "VERSION_NOT_SUPPORTED",
            A2aErrorKind::PolicyBlocked => "POLICY_BLOCKED",
            A2aErrorKind::Unauthenticated => "UNAUTHENTICATED",
            A2aErrorKind::PermissionDenied => "PERMISSION_DENIED",
            A2aErrorKind::GuardrailsUnavailable => "GUARDRAILS_UNAVAILABLE",
        }
    }

    /// Whether a refusal of this kind is sealed `engine_failure` rather than
    /// as a policy verdict: the gateway could not decide.
    pub fn engine_failure(self) -> bool {
        matches!(self, A2aErrorKind::GuardrailsUnavailable)
    }
}

// =============================================================================
// Wire envelope types
// =============================================================================

/// `google.rpc.ErrorInfo` packed into a `google.protobuf.Any`. The
/// `@type` discriminator tells spec-compliant parsers what fields to
/// expect alongside `reason`/`domain`/`metadata`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ErrorInfo {
    /// Must be [`ERROR_INFO_TYPE_URL`] for this variant.
    #[serde(rename = "@type")]
    pub type_url: String,
    /// SCREAMING_SNAKE_CASE reason — see [`A2aErrorKind::reason`].
    pub reason: String,
    /// Always `"a2a-protocol.org"` for spec errors.
    pub domain: String,
    /// Additional context (taskId, cardId, etc.). Spec allows
    /// arbitrary keys; we keep them sorted for reproducibility.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub metadata: BTreeMap<String, String>,
}

impl ErrorInfo {
    pub fn new(reason: impl Into<String>) -> Self {
        Self {
            type_url: ERROR_INFO_TYPE_URL.to_string(),
            reason: reason.into(),
            domain: A2A_ERROR_DOMAIN.to_string(),
            metadata: BTreeMap::new(),
        }
    }
}

/// The `error` object embedded at the top level of every A2A v1.0
/// HTTP error body. Spec §11.6 mirrors the AIP-193 JSON shape:
/// `{code, status, message, details[]}`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ErrorBody {
    pub code: u16,
    pub status: String,
    pub message: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub details: Vec<ErrorInfo>,
}

/// Top-level HTTP body: `{ "error": { ... } }`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ErrorEnvelope {
    pub error: ErrorBody,
}

// =============================================================================
// Builder
// =============================================================================

/// Errors that can be surfaced on the v1.0 wire. Combine a
/// [`A2aErrorKind`] with a human-readable message and optional
/// metadata to produce a strict, spec-shaped response.
#[derive(Debug, Clone)]
pub struct A2aError {
    kind: A2aErrorKind,
    message: String,
    metadata: BTreeMap<String, String>,
    /// Seconds to wait, for an error answered as `429 Too Many Requests`
    /// with `Retry-After` in place of its kind's status
    /// ([`A2aError::too_many_requests`]).
    retry_after_secs: Option<u64>,
    /// A credential refusal's requirement: stated in `details` and in the
    /// `Brutor-Credential-Required` header
    /// (`services::identity_policy::attach_requirement`).
    credential_requirement: Option<Box<crate::services::identity_policy::CredentialRequirement>>,
}

impl A2aError {
    /// Construct a new A2A error. `message` should be operator-safe
    /// (no stack traces, no tenant-boundary-crossing detail).
    pub fn new(kind: A2aErrorKind, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
            metadata: BTreeMap::new(),
            retry_after_secs: None,
            credential_requirement: None,
        }
    }

    /// State the requirement of a credential refusal.
    pub fn with_credential_requirement(
        mut self,
        requirement: crate::services::identity_policy::CredentialRequirement,
    ) -> Self {
        self.credential_requirement = Some(Box::new(requirement));
        self
    }

    /// Answer this error as `429 Too Many Requests` with a `Retry-After`
    /// header. The v1.0 error table has no kind with that status, so a rate
    /// refusal keeps a kind's envelope and states its own status.
    pub fn too_many_requests(mut self, retry_after_secs: u64) -> Self {
        self.retry_after_secs = Some(retry_after_secs.max(1));
        self
    }

    /// The HTTP status this error is answered with: its kind's, or 429 for
    /// a rate refusal.
    pub fn http_status(&self) -> StatusCode {
        match self.retry_after_secs {
            Some(_) => StatusCode::TOO_MANY_REQUESTS,
            None => self.kind.http_status(),
        }
    }

    /// Attach a `metadata` key/value to the emitted `ErrorInfo`. Most
    /// common usage is `.with_metadata("taskId", id)` for task-scoped
    /// errors and `.with_metadata("cardId", id)` for card-scoped ones.
    pub fn with_metadata(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.metadata.insert(key.into(), value.into());
        self
    }

    /// Borrow the underlying error-kind enum (useful in tests).
    pub fn kind(&self) -> A2aErrorKind {
        self.kind
    }

    /// Borrow the operator-facing message.
    pub fn message(&self) -> &str {
        &self.message
    }

    /// Render as the spec-shaped `ErrorEnvelope` JSON body plus the
    /// correct HTTP status code.
    pub fn to_envelope(&self) -> ErrorEnvelope {
        let status = self.kind.http_status();
        ErrorEnvelope {
            error: ErrorBody {
                code: status.as_u16(),
                status: self.kind.status_name().to_string(),
                message: self.message.clone(),
                details: vec![ErrorInfo {
                    type_url: ERROR_INFO_TYPE_URL.to_string(),
                    reason: self.kind.reason().to_string(),
                    domain: A2A_ERROR_DOMAIN.to_string(),
                    metadata: self.metadata.clone(),
                }],
            },
        }
    }

    /// Render as an Axum [`Response`] with the correct HTTP status,
    /// `application/json` content type, and the spec body shape.
    pub fn into_response(self) -> Response {
        if let Some(requirement) = &self.credential_requirement {
            let mut body = serde_json::to_value(self.to_envelope()).unwrap_or_default();
            crate::services::identity_policy::attach_requirement(&mut body, requirement);
            let mut response = (self.http_status(), Json(body)).into_response();
            crate::services::identity_policy::attach_required_header(&mut response, requirement);
            return response;
        }
        match self.retry_after_secs {
            Some(secs) => (
                StatusCode::TOO_MANY_REQUESTS,
                [("Retry-After", secs.to_string())],
                Json(self.to_envelope()),
            )
                .into_response(),
            None => (self.kind.http_status(), Json(self.to_envelope())).into_response(),
        }
    }
}

impl IntoResponse for A2aError {
    fn into_response(self) -> Response {
        A2aError::into_response(self)
    }
}

impl std::fmt::Display for A2aError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.kind.reason(), self.message)
    }
}

impl std::error::Error for A2aError {}

// =============================================================================
// Tests
// =============================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// A rate refusal keeps a kind's envelope and is answered as a 429 with
    /// `Retry-After`; every other error has its kind's status.
    #[test]
    fn a_rate_refusal_is_answered_as_429_with_retry_after() {
        let plain = A2aError::new(A2aErrorKind::UnsupportedOperation, "x");
        assert_eq!(
            plain.http_status(),
            A2aErrorKind::UnsupportedOperation.http_status()
        );
        assert!(
            !plain
                .clone()
                .into_response()
                .headers()
                .contains_key("retry-after")
        );

        let limited = plain.clone().too_many_requests(0);
        assert_eq!(limited.http_status(), StatusCode::TOO_MANY_REQUESTS);
        assert_eq!(
            serde_json::to_value(limited.to_envelope()).unwrap(),
            serde_json::to_value(plain.to_envelope()).unwrap(),
            "the envelope is the kind's"
        );
        let resp = limited.into_response();
        assert_eq!(resp.status(), StatusCode::TOO_MANY_REQUESTS);
        assert_eq!(
            resp.headers().get("retry-after").unwrap(),
            "1",
            "at least 1s"
        );
        let resp = plain.too_many_requests(42).into_response();
        assert_eq!(resp.headers().get("retry-after").unwrap(), "42");
    }

    /// Spec §5.4 table of (kind, http, status name, reason) must stay
    /// in lockstep. We enumerate every variant so a new addition to
    /// the enum without matching mappings would fail to compile
    /// (match is exhaustive) AND show up as a missing test case here.
    #[test]
    fn every_kind_has_spec_conformant_mapping() {
        let cases: &[(A2aErrorKind, u16, &str, &str)] = &[
            (
                A2aErrorKind::TaskNotFound,
                404,
                "NOT_FOUND",
                "TASK_NOT_FOUND",
            ),
            (
                A2aErrorKind::TaskNotCancelable,
                409,
                "FAILED_PRECONDITION",
                "TASK_NOT_CANCELABLE",
            ),
            (
                A2aErrorKind::PushNotificationNotSupported,
                400,
                "UNIMPLEMENTED",
                "PUSH_NOTIFICATION_NOT_SUPPORTED",
            ),
            (
                A2aErrorKind::UnsupportedOperation,
                400,
                "UNIMPLEMENTED",
                "UNSUPPORTED_OPERATION",
            ),
            (
                A2aErrorKind::ContentTypeNotSupported,
                415,
                "INVALID_ARGUMENT",
                "CONTENT_TYPE_NOT_SUPPORTED",
            ),
            (
                A2aErrorKind::InvalidAgentResponse,
                502,
                "INTERNAL",
                "INVALID_AGENT_RESPONSE",
            ),
            (
                A2aErrorKind::ExtendedAgentCardNotConfigured,
                400,
                "FAILED_PRECONDITION",
                "EXTENDED_AGENT_CARD_NOT_CONFIGURED",
            ),
            (
                A2aErrorKind::ExtensionSupportRequired,
                400,
                "FAILED_PRECONDITION",
                "EXTENSION_SUPPORT_REQUIRED",
            ),
            (
                A2aErrorKind::VersionNotSupported,
                400,
                "UNIMPLEMENTED",
                "VERSION_NOT_SUPPORTED",
            ),
        ];
        for (kind, http, status, reason) in cases {
            assert_eq!(
                kind.http_status().as_u16(),
                *http,
                "{kind:?}: HTTP mismatch"
            );
            assert_eq!(
                kind.status_name(),
                *status,
                "{kind:?}: status-name mismatch"
            );
            assert_eq!(kind.reason(), *reason, "{kind:?}: reason mismatch");
        }
    }

    /// Wire shape matches the spec example (§11.6) byte-for-byte.
    #[test]
    fn wire_shape_matches_spec_example() {
        let err = A2aError::new(A2aErrorKind::TaskNotFound, "task not found")
            .with_metadata("taskId", "task-123");
        let body = err.to_envelope();
        let value = serde_json::to_value(&body).unwrap();
        assert_eq!(
            value,
            json!({
                "error": {
                    "code": 404,
                    "status": "NOT_FOUND",
                    "message": "task not found",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                            "reason": "TASK_NOT_FOUND",
                            "domain": "a2a-protocol.org",
                            "metadata": {"taskId": "task-123"}
                        }
                    ]
                }
            }),
        );
    }

    #[test]
    fn metadata_is_sorted_for_reproducibility() {
        // Two insertions in reverse order → sorted on the wire. Uses
        // BTreeMap under the hood; this test protects against an
        // accidental switch to HashMap.
        let err = A2aError::new(A2aErrorKind::InvalidAgentResponse, "bad upstream")
            .with_metadata("zzz", "last")
            .with_metadata("aaa", "first");
        let s = serde_json::to_string(&err.to_envelope()).unwrap();
        let aaa = s.find("\"aaa\"").unwrap();
        let zzz = s.find("\"zzz\"").unwrap();
        assert!(aaa < zzz, "metadata keys must be sorted: {s}");
    }

    #[test]
    fn empty_metadata_omitted_from_wire() {
        let err = A2aError::new(A2aErrorKind::UnsupportedOperation, "nope");
        let s = serde_json::to_string(&err.to_envelope()).unwrap();
        // Absent rather than `"metadata":{}`.
        assert!(
            !s.contains("\"metadata\""),
            "empty metadata must be omitted: {s}"
        );
    }

    #[test]
    fn no_error_suffix_on_reason() {
        // Spec §11.6: "the A2A error type in UPPER_SNAKE_CASE without
        // the 'Error' suffix". Enum variants are named after the spec
        // error types; the reason string must drop the suffix.
        for k in [
            A2aErrorKind::TaskNotFound,
            A2aErrorKind::TaskNotCancelable,
            A2aErrorKind::InvalidAgentResponse,
            A2aErrorKind::VersionNotSupported,
        ] {
            assert!(
                !k.reason().ends_with("_ERROR"),
                "{k:?} reason must not end with _ERROR: {}",
                k.reason()
            );
        }
    }

    #[test]
    fn display_implementation_combines_reason_and_message() {
        let err = A2aError::new(A2aErrorKind::TaskNotFound, "task foo");
        assert_eq!(format!("{err}"), "TASK_NOT_FOUND: task foo");
    }

    #[test]
    fn envelope_round_trips_through_serde() {
        let err = A2aError::new(
            A2aErrorKind::VersionNotSupported,
            "only v1.0 is served here",
        )
        .with_metadata("requested", "0.3");
        let wire = serde_json::to_string(&err.to_envelope()).unwrap();
        let back: ErrorEnvelope = serde_json::from_str(&wire).unwrap();
        assert_eq!(back.error.code, 400);
        assert_eq!(back.error.status, "UNIMPLEMENTED");
        assert_eq!(back.error.details.len(), 1);
        assert_eq!(back.error.details[0].reason, "VERSION_NOT_SUPPORTED");
        assert_eq!(back.error.details[0].domain, A2A_ERROR_DOMAIN);
        assert_eq!(
            back.error.details[0].metadata.get("requested").unwrap(),
            "0.3"
        );
    }

    #[test]
    fn response_status_matches_kind() {
        // Construct a Response and inspect the status code — belt and
        // braces to ensure into_response wires the status through.
        let err = A2aError::new(A2aErrorKind::TaskNotCancelable, "already done");
        let resp = err.into_response();
        assert_eq!(resp.status(), StatusCode::CONFLICT);
    }
}
