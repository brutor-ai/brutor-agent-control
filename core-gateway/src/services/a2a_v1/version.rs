// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A2A protocol version detection.
//!
//! The v1.0 spec (§3.6 Versioning) specifies that version negotiation
//! is carried via the `A2A-Version` service parameter — conveyed as a
//! request header in the HTTP binding. It's also common in the wild
//! to see `A2A-Protocol-Version` (Google's original proposal). We
//! accept both to keep interop broad.
//!
//! Precedence (first match wins):
//!
//!   1. Explicit `A2A-Version` header — authoritative.
//!   2. Explicit `A2A-Protocol-Version` header — accepted for
//!      pre-LF-governance clients still on the old name.
//!   3. Path-based fallback — canonical v1.0 routes
//!      (`/message:send`, `/tasks/*`, `/extendedAgentCard`) imply
//!      `1.0`. Anything else defaults to the caller's declared
//!      default.
//!
//! Each step is tested explicitly below so a future header rename
//! (or new path) doesn't silently skew the detector.

use axum::http::HeaderMap;

use crate::services::a2a_v1::AgentInterface;

pub const VERSION_V1_0: &str = "1.0";
pub const VERSION_V0_3: &str = "0.3";

pub const HEADER_A2A_VERSION: &str = "a2a-version";
pub const HEADER_A2A_PROTOCOL_VERSION: &str = "a2a-protocol-version";

/// Outcome of a detection pass.
///
/// Equality compares the major.minor string only. Per spec §3.6,
/// patch versions MUST NOT affect negotiation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProtocolVersion {
    V0_3,
    V1_0,
    /// Something we don't recognise. Callers typically return a
    /// `VersionNotSupportedError` per spec §3.3.2.
    Unknown,
}

impl ProtocolVersion {
    /// Parse a canonical `"1.0"` / `"0.3"` / `"1.0.1"` form, trimming
    /// any patch component per §3.6.
    // Not `std::str::FromStr`: that returns Result, and this parse is
    // infallible by design — anything unrecognised maps to a default
    // rather than erroring.
    #[allow(clippy::should_implement_trait)]
    pub fn from_str(raw: &str) -> Self {
        let trimmed = raw.trim();
        // Split on first '.' and keep major.minor for comparison.
        // This handles "1.0", "1.0.1", "v1.0" (we strip an optional "v").
        let normalised = trimmed.strip_prefix('v').unwrap_or(trimmed);
        let major_minor: Vec<&str> = normalised.splitn(3, '.').collect();
        let mm = match major_minor.as_slice() {
            [maj, min] => format!("{maj}.{min}"),
            [maj, min, _patch] => format!("{maj}.{min}"),
            _ => normalised.to_string(),
        };
        match mm.as_str() {
            "1.0" => ProtocolVersion::V1_0,
            "0.3" => ProtocolVersion::V0_3,
            _ => ProtocolVersion::Unknown,
        }
    }

    /// Wire form — what we'd emit on an outbound `A2A-Version` header
    /// or store in `proxy_logs.a2a_protocol_version`.
    pub fn as_str(self) -> &'static str {
        match self {
            ProtocolVersion::V1_0 => VERSION_V1_0,
            ProtocolVersion::V0_3 => VERSION_V0_3,
            ProtocolVersion::Unknown => "",
        }
    }
}

/// Pick the preferred `AgentInterface` for a target card.
///
/// Walks the `supported_interfaces[]` array in order (the spec says
/// "first entry is preferred") and picks the first interface whose
/// binding we understand and whose protocol version we can speak.
/// Priority order:
///
///   1. JSONRPC 1.0
///   2. HTTP+JSON 1.0
///   3. JSONRPC 0.3 (legacy)
///   4. HTTP+JSON 0.3 (legacy)
///
/// GRPC bindings are ignored — we only speak JSON wire formats.
///
/// Returns `None` when no interface we can speak is advertised.
pub fn pick_interface(interfaces: &[AgentInterface]) -> Option<&AgentInterface> {
    // Build a (binding, version) priority score. Lower wins.
    fn score(iface: &AgentInterface) -> Option<u8> {
        let binding = iface.protocol_binding.as_str();
        let version = ProtocolVersion::from_str(&iface.protocol_version);
        match (binding, version) {
            ("JSONRPC", ProtocolVersion::V1_0) => Some(0),
            ("HTTP+JSON", ProtocolVersion::V1_0) => Some(1),
            ("JSONRPC", ProtocolVersion::V0_3) => Some(2),
            ("HTTP+JSON", ProtocolVersion::V0_3) => Some(3),
            _ => None,
        }
    }
    interfaces
        .iter()
        .enumerate()
        .filter_map(|(position, iface)| score(iface).map(|s| (s, position, iface)))
        // Stable: same score → earlier position wins (matches the
        // spec's "first entry is preferred" guidance for ties).
        .min_by_key(|(score, position, _)| (*score, *position))
        .map(|(_, _, iface)| iface)
}

/// Detect the negotiated protocol version for an inbound HTTP request.
pub fn detect_from_headers_and_path(headers: &HeaderMap, path: &str) -> ProtocolVersion {
    if let Some(v) = headers
        .get(HEADER_A2A_VERSION)
        .and_then(|h| h.to_str().ok())
    {
        return ProtocolVersion::from_str(v);
    }
    if let Some(v) = headers
        .get(HEADER_A2A_PROTOCOL_VERSION)
        .and_then(|h| h.to_str().ok())
    {
        return ProtocolVersion::from_str(v);
    }
    // Legacy path of this gateway: `/v1/proxy/a2a/{inbound,outbound}`.
    // Checked first so a v0.3 request never gets classified as v1.0
    // by the tenant-segment heuristic below.
    let stripped = path.trim_start_matches('/');
    if stripped.starts_with("v1/proxy/a2a/") {
        return ProtocolVersion::V0_3;
    }

    // Path-based v1.0 fallback. The canonical v1.0 HTTP endpoints all
    // start with `/message:`, `/tasks`, or `/extendedAgentCard`. The
    // spec also allows an optional leading tenant segment, e.g.
    // `/{tenant}/message:send`. A single path can match EITHER form.
    fn starts_v1(prefix: &str) -> bool {
        prefix.starts_with("message:send")
            || prefix.starts_with("message:stream")
            || prefix == "tasks"
            || prefix.starts_with("tasks/")
            || prefix.starts_with("tasks:")
            || prefix.starts_with("extendedAgentCard")
    }
    if starts_v1(stripped) {
        return ProtocolVersion::V1_0;
    }
    // Retry with an optional tenant segment dropped.
    if let Some((_tenant, rest)) = stripped.split_once('/') {
        if starts_v1(rest) {
            return ProtocolVersion::V1_0;
        }
    }
    ProtocolVersion::Unknown
}

// =============================================================================
// Tests
// =============================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::HeaderValue;

    fn iface(binding: &str, version: &str) -> AgentInterface {
        AgentInterface {
            url: "https://x/".into(),
            protocol_binding: binding.into(),
            tenant: None,
            protocol_version: version.into(),
        }
    }

    #[test]
    fn from_str_strips_patch_and_v_prefix() {
        assert_eq!(ProtocolVersion::from_str("1.0"), ProtocolVersion::V1_0);
        assert_eq!(ProtocolVersion::from_str("1.0.1"), ProtocolVersion::V1_0);
        assert_eq!(ProtocolVersion::from_str("v1.0"), ProtocolVersion::V1_0);
        assert_eq!(ProtocolVersion::from_str("0.3"), ProtocolVersion::V0_3);
        assert_eq!(ProtocolVersion::from_str("0.3.2"), ProtocolVersion::V0_3);
    }

    #[test]
    fn from_str_unknown_for_noise() {
        assert_eq!(ProtocolVersion::from_str(""), ProtocolVersion::Unknown);
        assert_eq!(ProtocolVersion::from_str("1.5"), ProtocolVersion::Unknown);
        assert_eq!(
            ProtocolVersion::from_str("garbage"),
            ProtocolVersion::Unknown
        );
    }

    #[test]
    fn headers_take_precedence_over_path() {
        let mut h = HeaderMap::new();
        h.insert("a2a-version", HeaderValue::from_static("0.3"));
        // Path suggests v1.0 but the header wins.
        let v = detect_from_headers_and_path(&h, "/message:send");
        assert_eq!(v, ProtocolVersion::V0_3);
    }

    #[test]
    fn falls_back_to_older_header_name() {
        let mut h = HeaderMap::new();
        h.insert("a2a-protocol-version", HeaderValue::from_static("0.3"));
        let v = detect_from_headers_and_path(&h, "/message:send");
        assert_eq!(v, ProtocolVersion::V0_3);
    }

    #[test]
    fn path_based_detection_canonical_v1_paths() {
        let h = HeaderMap::new();
        for p in [
            "/message:send",
            "/message:stream",
            "/tasks",
            "/tasks/abc",
            "/tasks/abc:cancel",
            "/tasks/abc:subscribe",
            "/extendedAgentCard",
            "/tenant-a/message:send",
            "/tenant-a/tasks",
        ] {
            assert_eq!(
                detect_from_headers_and_path(&h, p),
                ProtocolVersion::V1_0,
                "path {p} should be v1.0"
            );
        }
    }

    #[test]
    fn path_based_detection_legacy_brutor_paths() {
        let h = HeaderMap::new();
        assert_eq!(
            detect_from_headers_and_path(&h, "/v1/proxy/a2a/inbound"),
            ProtocolVersion::V0_3,
        );
        assert_eq!(
            detect_from_headers_and_path(&h, "/v1/proxy/a2a/outbound"),
            ProtocolVersion::V0_3,
        );
    }

    #[test]
    fn path_based_detection_unknown_by_default() {
        let h = HeaderMap::new();
        assert_eq!(
            detect_from_headers_and_path(&h, "/health"),
            ProtocolVersion::Unknown,
        );
        assert_eq!(
            detect_from_headers_and_path(&h, "/"),
            ProtocolVersion::Unknown,
        );
    }

    #[test]
    fn pick_interface_prefers_v1_jsonrpc() {
        let ifaces = vec![
            iface("JSONRPC", "0.3"),
            iface("JSONRPC", "1.0"),
            iface("HTTP+JSON", "1.0"),
        ];
        let chosen = pick_interface(&ifaces).unwrap();
        assert_eq!(chosen.protocol_binding, "JSONRPC");
        assert_eq!(chosen.protocol_version, "1.0");
    }

    #[test]
    fn pick_interface_falls_back_to_v03_when_only_v03_advertised() {
        let ifaces = vec![iface("JSONRPC", "0.3")];
        let chosen = pick_interface(&ifaces).unwrap();
        assert_eq!(chosen.protocol_version, "0.3");
    }

    #[test]
    fn pick_interface_skips_grpc_and_unknown_bindings() {
        let ifaces = vec![
            iface("GRPC", "1.0"),
            iface("JSONRPC", "1.0"),
            iface("WEIRD", "1.0"),
        ];
        let chosen = pick_interface(&ifaces).unwrap();
        assert_eq!(chosen.protocol_binding, "JSONRPC");
    }

    #[test]
    fn pick_interface_none_when_all_unsupported() {
        let ifaces = vec![iface("GRPC", "1.0"), iface("WEIRD", "1.0")];
        assert!(pick_interface(&ifaces).is_none());
    }

    #[test]
    fn pick_interface_stable_with_same_score() {
        // Two JSONRPC 1.0 entries — the first one (per spec "first entry
        // is preferred") wins the tie.
        let mut a = iface("JSONRPC", "1.0");
        a.url = "https://first/".into();
        let mut b = iface("JSONRPC", "1.0");
        b.url = "https://second/".into();
        let ifaces = vec![a, b];
        let chosen = pick_interface(&ifaces).unwrap();
        assert_eq!(chosen.url, "https://first/");
    }
}
