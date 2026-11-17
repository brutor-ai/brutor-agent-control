// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! MCP 2026-07-28 wire-protocol helpers.
//!
//! Shared by the main MCP proxy, the VMCP aggregator, and the synthesized
//! system servers. Covers:
//!
//!   * per-request protocol context (header + `params._meta` +
//!     legacy `params.protocolVersion`)
//!   * the `Mcp-Method` / `Mcp-Name` header↔body consistency check
//!     (SEP-2243) — a governance feature: a caller must not be able to
//!     present a benign name to header-routing infrastructure while the
//!     body calls something else
//!   * the `=?base64?…?=` sentinel decoding for non-ASCII header values
//!   * `server/discover` result assembly (SEP — stateless core)
//!
//! Spec facts: header/body mismatch or a
//! missing required header is HTTP 400 + JSON-RPC `-32020`; `Mcp-Name`
//! is only defined for `tools/call`, `resources/read`, `prompts/get`
//! (from `params.name` / `params.uri`); every modern result carries
//! `resultType`, and `server/discover` results are cacheable
//! (`ttlMs` + `cacheScope` required).

use axum::http::HeaderMap;
use base64::Engine as _;
use serde_json::{Value, json};

use crate::constants::mcp_protocol as mp;

/// Decode a spec sentinel-encoded header value (`=?base64?{b64}?=`).
/// Non-sentinel values pass through unchanged. Undecodable sentinels
/// return the raw input (the comparison will then fail loudly, which
/// is the safe direction for a consistency check).
pub fn decode_sentinel(value: &str) -> String {
    let Some(inner) = value
        .strip_prefix("=?base64?")
        .and_then(|rest| rest.strip_suffix("?="))
    else {
        return value.to_string();
    };
    match base64::engine::general_purpose::STANDARD.decode(inner) {
        Ok(bytes) => String::from_utf8(bytes).unwrap_or_else(|_| value.to_string()),
        Err(_) => value.to_string(),
    }
}

/// The protocol version a request is speaking, in precedence order:
/// `MCP-Protocol-Version` header → `params._meta` (2026-07-28) →
/// legacy `params.protocolVersion` (initialize). `None` = unversioned
/// legacy traffic.
pub fn requested_protocol_version(headers: &HeaderMap, body: &Value) -> Option<String> {
    if let Some(v) = headers
        .get(mp::headers::PROTOCOL_VERSION)
        .and_then(|v| v.to_str().ok())
    {
        return Some(v.to_string());
    }
    let params = body.get("params")?;
    if let Some(v) = params
        .get("_meta")
        .and_then(|m| m.get(mp::meta::PROTOCOL_VERSION))
        .and_then(|v| v.as_str())
    {
        return Some(v.to_string());
    }
    params
        .get("protocolVersion")
        .and_then(|v| v.as_str())
        .map(str::to_string)
}

/// Is this request speaking the stateless 2026-07-28 dialect? True when
/// the required `_meta` protocolVersion is present (the header alone is
/// also accepted — SDK betas set both).
pub fn is_stateless_request(headers: &HeaderMap, body: &Value) -> bool {
    requested_protocol_version(headers, body).as_deref() == Some(mp::V2026_07_28)
}

/// Body-only variant for call sites that don't carry HTTP headers
/// (the system-server dispatch receives only the JSON-RPC body; the
/// spec requires `_meta` on every modern request, so the body alone is
/// authoritative).
pub fn is_stateless_body(body: &Value) -> bool {
    body.get("params")
        .and_then(|p| p.get("_meta"))
        .and_then(|m| m.get(mp::meta::PROTOCOL_VERSION))
        .and_then(|v| v.as_str())
        == Some(mp::V2026_07_28)
}

/// Stamp a result for the modern era: every 2026-07-28 result carries
/// `resultType`; the cacheable methods (`tools/list`, `prompts/list`,
/// `resources/list`, `resources/templates/list`, `resources/read`,
/// `server/discover`) additionally carry `ttlMs` + `cacheScope`.
/// Idempotent — fields already present (e.g. from `discover_result`)
/// are left untouched. Legacy results must never pass through here.
pub fn stamp_modern_result(result: &mut Value, cacheable: Option<(u64, &str)>) {
    let Some(obj) = result.as_object_mut() else {
        return;
    };
    obj.entry("resultType")
        .or_insert_with(|| Value::String(mp::result_type::COMPLETE.into()));
    if let Some((ttl_ms, scope)) = cacheable {
        obj.entry("ttlMs").or_insert_with(|| json!(ttl_ms));
        obj.entry("cacheScope")
            .or_insert_with(|| Value::String(scope.into()));
    }
}

/// The body field `Mcp-Name` mirrors, per method. Only three methods
/// carry the header; for everything else it must simply be consistent
/// if present (we don't require it).
fn body_name_for_method(method: &str, body: &Value) -> Option<String> {
    let params = body.get("params")?;
    match method {
        "tools/call" | "prompts/get" => params
            .get("name")
            .and_then(|v| v.as_str())
            .map(str::to_string),
        "resources/read" => params
            .get("uri")
            .and_then(|v| v.as_str())
            .map(str::to_string),
        _ => None,
    }
}

/// SEP-2243 consistency check. Returns `Some(reason)` when a present
/// `Mcp-Method` / `Mcp-Name` header contradicts the body — the caller
/// must reject with HTTP 400 + JSON-RPC `-32020`.
///
/// Deliberately lenient about ABSENT headers: legacy clients don't send
/// them, and requiring them is the flag-gated stateless server's job.
/// Lying is what we never tolerate, from any dialect.
pub fn header_body_mismatch(headers: &HeaderMap, body: &Value) -> Option<String> {
    let body_method = body.get("method").and_then(|v| v.as_str()).unwrap_or("");

    if let Some(h) = headers
        .get(mp::headers::METHOD)
        .and_then(|v| v.to_str().ok())
    {
        let decoded = decode_sentinel(h);
        if decoded != body_method {
            return Some(format!(
                "Mcp-Method header '{decoded}' does not match body method '{body_method}'"
            ));
        }
    }

    if let Some(h) = headers.get(mp::headers::NAME).and_then(|v| v.to_str().ok()) {
        let decoded = decode_sentinel(h);
        match body_name_for_method(body_method, body) {
            Some(body_name) if body_name == decoded => {}
            Some(body_name) => {
                return Some(format!(
                    "Mcp-Name header '{decoded}' does not match body target '{body_name}'"
                ));
            }
            // Header present on a method that carries no name → the
            // header claims something the body cannot back.
            None => {
                return Some(format!(
                    "Mcp-Name header present but method '{body_method}' carries no name/uri"
                ));
            }
        }
    }

    None
}

/// JSON-RPC error body for a header/body mismatch (`-32020`, HTTP 400).
pub fn header_mismatch_error(id: Option<&Value>, reason: &str) -> Value {
    json!({
        "jsonrpc": "2.0",
        "id": id.cloned().unwrap_or(Value::Null),
        "error": {
            "code": mp::error::HEADER_MISMATCH,
            "message": format!("Header mismatch: {reason}"),
        }
    })
}

/// Emit the SEP-2243 routing headers on the upstream
/// send, derived from the (already consistency-checked) body. Values
/// the client sent are left untouched — they were verified inbound and
/// copied by the passthrough. Unknown headers are ignored by legacy
/// servers, so emission is unconditionally safe.
pub fn inject_routing_headers(upstream: &mut reqwest::header::HeaderMap, body: &Value) {
    use reqwest::header::{HeaderName, HeaderValue};

    let method = body.get("method").and_then(|v| v.as_str()).unwrap_or("");
    if method.is_empty() {
        return;
    }

    let method_name = HeaderName::from_static(mp::headers::METHOD);
    if !upstream.contains_key(&method_name) {
        if let Ok(v) = HeaderValue::from_str(method) {
            upstream.insert(method_name, v);
        }
    }

    let name_header = HeaderName::from_static(mp::headers::NAME);
    if !upstream.contains_key(&name_header) {
        if let Some(target) = body_name_for_method(method, body) {
            // ASCII targets go verbatim; anything else gets the spec's
            // base64 sentinel encoding.
            let value = if target.is_ascii() {
                target
            } else {
                format!(
                    "=?base64?{}?=",
                    base64::engine::general_purpose::STANDARD.encode(target.as_bytes())
                )
            };
            if let Ok(v) = HeaderValue::from_str(&value) {
                upstream.insert(name_header, v);
            }
        }
    }
}

/// Assemble a `server/discover` result (2026-07-28). `instructions` is
/// where the skills preamble+catalog now lives; access-scoped content
/// MUST use `cache_scope::PRIVATE`.
pub fn discover_result(
    supported_versions: &[&str],
    capabilities: Value,
    instructions: Option<String>,
    server_name: &str,
    server_version: &str,
    ttl_ms: u64,
    cache_scope: &str,
) -> Value {
    let mut result = json!({
        "resultType": mp::result_type::COMPLETE,
        "supportedVersions": supported_versions,
        "capabilities": capabilities,
        "ttlMs": ttl_ms,
        "cacheScope": cache_scope,
        "_meta": {
            mp::meta::SERVER_INFO: { "name": server_name, "version": server_version }
        }
    });
    if let Some(text) = instructions {
        result["instructions"] = Value::String(text);
    }
    result
}

/// Era bridge, request half. Ensure the body carries the
/// `_meta` fields the stateless dialect requires on EVERY request
/// (SEP-2328: protocolVersion + clientCapabilities). Legacy clients send
/// neither; a strict 2026-07-28 upstream rejects the request with
/// `-32602` without them. Returns `true` when the body was modified
/// (caller must re-serialize). Existing `_meta` values are never
/// overwritten, so modern clients pass through byte-identical.
pub fn ensure_modern_meta(body: &mut Value) -> bool {
    if body
        .get("method")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .is_empty()
    {
        return false;
    }
    let Some(obj) = body.as_object_mut() else {
        return false;
    };
    let params = obj
        .entry("params")
        .or_insert_with(|| Value::Object(Default::default()));
    if !params.is_object() {
        return false;
    }
    let meta = params
        .as_object_mut()
        .unwrap()
        .entry("_meta")
        .or_insert_with(|| Value::Object(Default::default()));
    let Some(meta) = meta.as_object_mut() else {
        return false;
    };
    let mut changed = false;
    if !meta.contains_key(mp::meta::PROTOCOL_VERSION) {
        meta.insert(
            mp::meta::PROTOCOL_VERSION.to_string(),
            Value::String(mp::V2026_07_28.to_string()),
        );
        changed = true;
    }
    if !meta.contains_key(mp::meta::CLIENT_CAPABILITIES) {
        meta.insert(
            mp::meta::CLIENT_CAPABILITIES.to_string(),
            Value::Object(Default::default()),
        );
        changed = true;
    }
    changed
}

/// Era bridge, handshake half. A stateless-only upstream
/// has no `initialize`, so the gateway answers a legacy client's
/// handshake itself from the capabilities recorded at discovery.
/// Capability *flags* are advertised for every list present in the
/// stored capabilities JSON (tools always — an empty list is still a
/// valid tools surface); the client fetches the actual lists with
/// `tools/list` etc., which the proxy bridges per-request.
pub fn legacy_initialize_result(
    client_requested: Option<&str>,
    server_name: &str,
    capabilities: Option<&Value>,
) -> Value {
    // Echo a legacy version the client asked for; anything else (missing,
    // or a modern/unknown version) negotiates down to the newest legacy.
    let pv = match client_requested {
        Some(v) if v == mp::V2024_11_05 || v == mp::V2025_03_26 || v == mp::V2025_06_18 => v,
        _ => mp::V2025_06_18,
    };
    let has = |key: &str| {
        capabilities
            .and_then(|c| c.get(key))
            .and_then(|v| v.as_array())
            .map(|a| !a.is_empty())
            .unwrap_or(false)
    };
    let mut caps = serde_json::Map::new();
    caps.insert("tools".into(), json!({ "listChanged": false }));
    if has("resources") || has("resource_templates") {
        caps.insert("resources".into(), json!({ "listChanged": false }));
    }
    if has("prompts") {
        caps.insert("prompts".into(), json!({ "listChanged": false }));
    }
    let mut result = json!({
        "protocolVersion": pv,
        "capabilities": Value::Object(caps),
        "serverInfo": { "name": server_name, "version": "unknown" }
    });
    if let Some(text) = capabilities
        .and_then(|c| c.get("instructions"))
        .and_then(|v| v.as_str())
    {
        result["instructions"] = Value::String(text.to_string());
    }
    result
}

// ============================================================================
// Tests
// ============================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::HeaderValue;

    fn headers(pairs: &[(&str, &str)]) -> HeaderMap {
        let mut h = HeaderMap::new();
        for (k, v) in pairs {
            h.insert(
                axum::http::HeaderName::from_bytes(k.as_bytes()).unwrap(),
                HeaderValue::from_str(v).unwrap(),
            );
        }
        h
    }

    #[test]
    fn sentinel_decodes_and_passes_through() {
        assert_eq!(decode_sentinel("plain-name"), "plain-name");
        // "søk" base64 = c8O4aw==
        assert_eq!(decode_sentinel("=?base64?c8O4aw==?="), "søk");
        // broken base64 → raw (comparison then fails loudly)
        assert_eq!(decode_sentinel("=?base64?!!!?="), "=?base64?!!!?=");
    }

    #[test]
    fn version_precedence_header_then_meta_then_legacy() {
        let body = serde_json::json!({
            "method": "tools/list",
            "params": {
                "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"},
                "protocolVersion": "2025-03-26"
            }
        });
        let h = headers(&[("mcp-protocol-version", "2025-06-18")]);
        assert_eq!(
            requested_protocol_version(&h, &body).as_deref(),
            Some("2025-06-18")
        );
        let none = HeaderMap::new();
        assert_eq!(
            requested_protocol_version(&none, &body).as_deref(),
            Some("2026-07-28")
        );
        let legacy = serde_json::json!({"method": "initialize", "params": {"protocolVersion": "2024-11-05"}});
        assert_eq!(
            requested_protocol_version(&none, &legacy).as_deref(),
            Some("2024-11-05")
        );
    }

    #[test]
    fn mismatch_detects_lying_headers() {
        let body = serde_json::json!({
            "method": "tools/call",
            "params": {"name": "db__delete_rows", "arguments": {}}
        });
        // benign header, malicious body — the spoofing case
        let h = headers(&[
            ("mcp-method", "tools/call"),
            ("mcp-name", "hubspot__get_contacts"),
        ]);
        assert!(
            header_body_mismatch(&h, &body)
                .unwrap()
                .contains("does not match body target")
        );
        // consistent → ok
        let h = headers(&[
            ("mcp-method", "tools/call"),
            ("mcp-name", "db__delete_rows"),
        ]);
        assert!(header_body_mismatch(&h, &body).is_none());
        // absent headers (legacy client) → ok
        assert!(header_body_mismatch(&HeaderMap::new(), &body).is_none());
        // method mismatch
        let h = headers(&[("mcp-method", "tools/list")]);
        assert!(
            header_body_mismatch(&h, &body)
                .unwrap()
                .contains("Mcp-Method")
        );
        // name header on a nameless method
        let listing = serde_json::json!({"method": "tools/list", "params": {}});
        let h = headers(&[("mcp-name", "anything")]);
        assert!(
            header_body_mismatch(&h, &listing)
                .unwrap()
                .contains("carries no name")
        );
    }

    #[test]
    fn resources_read_matches_on_uri() {
        let body = serde_json::json!({
            "method": "resources/read",
            "params": {"uri": "file:///docs/a.md"}
        });
        let h = headers(&[("mcp-name", "file:///docs/a.md")]);
        assert!(header_body_mismatch(&h, &body).is_none());
    }

    #[test]
    fn stamping_is_modern_only_and_idempotent() {
        let modern_body = serde_json::json!({
            "method": "tools/list",
            "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}
        });
        let legacy_body = serde_json::json!({"method": "tools/list", "params": {}});
        assert!(is_stateless_body(&modern_body));
        assert!(!is_stateless_body(&legacy_body));

        let mut result = serde_json::json!({"tools": []});
        stamp_modern_result(&mut result, Some((60_000, "private")));
        assert_eq!(result["resultType"], "complete");
        assert_eq!(result["ttlMs"], 60_000);
        assert_eq!(result["cacheScope"], "private");

        // idempotent — pre-set fields (e.g. discover_result) untouched
        let mut pre = serde_json::json!({"resultType": "input_required", "ttlMs": 5});
        stamp_modern_result(&mut pre, Some((60_000, "private")));
        assert_eq!(pre["resultType"], "input_required");
        assert_eq!(pre["ttlMs"], 5);

        // non-cacheable stamp: resultType only
        let mut call = serde_json::json!({"content": []});
        stamp_modern_result(&mut call, None);
        assert_eq!(call["resultType"], "complete");
        assert!(call.get("ttlMs").is_none());
    }

    #[test]
    fn discover_result_shape() {
        let r = discover_result(
            &["2026-07-28", "2025-06-18"],
            serde_json::json!({"tools": {"listChanged": false}}),
            Some("catalog here".into()),
            "brutor-vmcp",
            "1.0.0",
            60_000,
            crate::constants::mcp_protocol::cache_scope::PRIVATE,
        );
        assert_eq!(r["resultType"], "complete");
        assert_eq!(r["supportedVersions"][0], "2026-07-28");
        assert_eq!(r["cacheScope"], "private");
        assert_eq!(r["ttlMs"], 60_000);
        assert_eq!(r["instructions"], "catalog here");
        assert_eq!(
            r["_meta"]["io.modelcontextprotocol/serverInfo"]["name"],
            "brutor-vmcp"
        );
    }

    #[test]
    fn ensure_modern_meta_upgrades_legacy_body() {
        let mut body = serde_json::json!({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "echo", "arguments": {"text": "hi"}}
        });
        assert!(ensure_modern_meta(&mut body));
        assert_eq!(
            body["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"],
            "2026-07-28"
        );
        assert!(body["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"].is_object());
        // Original params untouched.
        assert_eq!(body["params"]["arguments"]["text"], "hi");
    }

    #[test]
    fn ensure_modern_meta_creates_params_when_absent() {
        let mut body = serde_json::json!({"jsonrpc": "2.0", "id": 2, "method": "tools/list"});
        assert!(ensure_modern_meta(&mut body));
        assert_eq!(
            body["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"],
            "2026-07-28"
        );
    }

    #[test]
    fn ensure_modern_meta_never_overwrites_modern_client() {
        let mut body = serde_json::json!({
            "jsonrpc": "2.0", "id": 3, "method": "tools/list",
            "params": {"_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {"elicitation": {}}
            }}
        });
        let before = body.clone();
        assert!(!ensure_modern_meta(&mut body));
        assert_eq!(body, before);
    }

    #[test]
    fn legacy_initialize_result_echoes_legacy_version_and_flags() {
        let caps = serde_json::json!({
            "tools": [{"name": "echo"}],
            "resources": [{"uri": "test://readme"}],
            "prompts": []
        });
        let r = legacy_initialize_result(Some("2025-03-26"), "MCP Stateless", Some(&caps));
        assert_eq!(r["protocolVersion"], "2025-03-26");
        assert_eq!(r["capabilities"]["tools"]["listChanged"], false);
        assert!(r["capabilities"]["resources"].is_object());
        assert!(r["capabilities"].get("prompts").is_none());
        assert_eq!(r["serverInfo"]["name"], "MCP Stateless");
    }

    #[test]
    fn legacy_initialize_result_clamps_unknown_version() {
        let r = legacy_initialize_result(Some("2026-07-28"), "s", None);
        assert_eq!(r["protocolVersion"], "2025-06-18");
        // tools capability always advertised, even with no stored caps.
        assert_eq!(r["capabilities"]["tools"]["listChanged"], false);
        let r = legacy_initialize_result(None, "s", None);
        assert_eq!(r["protocolVersion"], "2025-06-18");
    }
}
