// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! v0.3 → v1.0 Agent Card translator (read-only / in-memory).
//!
//! Mirrors the Python `app.services.a2a_v1.translator` — we keep
//! both implementations in lockstep because either language may be
//! asked to read a v0.3 card and return a v1.0-shaped body:
//!
//!   * Python admin API serves `GET /v1/admin/agent-cards/{id}` with
//!     the v1.0 shape regardless of what's in `card_json`.
//!   * Rust discovery serves `GET /.well-known/agent-card/...` the
//!     same way.
//!
//! Without a read-time translator we'd either have to force-rewrite
//! every tenant's `agent_cards.card_json` column on the v1.0 rollout
//! (risky, schema-coupled) or ship a mixed-dialect discovery surface
//! (bad for partners).
//!
//! ## What the translator fixes
//!
//! | v0.3 field                                  | v1.0 destination                                              |
//! |---------------------------------------------|---------------------------------------------------------------|
//! | `url`                                       | `supportedInterfaces[0].url`                                  |
//! | `protocolVersion`                           | `supportedInterfaces[0].protocolVersion`                      |
//! | `preferredTransport`                        | `supportedInterfaces[0].protocolBinding` (defaults JSONRPC)   |
//! | `additionalInterfaces[]`                    | `supportedInterfaces[1..]` one-for-one                        |
//! | `supportsAuthenticatedExtendedCard`         | `capabilities.extendedAgentCard`                              |
//! | `signature` / `signatureAlgorithm` / `signedBy` / `signedAt` | **dropped** — v1.0 requires JWS-shaped `signatures[]`; the legacy flat form is not a JWS and is no longer cryptographically meaningful under v1.0 rules. |
//!
//! All other fields pass through unchanged. Anything the translator
//! doesn't recognise stays untouched so an unknown v0.3 extension
//! doesn't silently vanish.
//!
//! ## Delegation extension
//!
//! On translate we also ensure `capabilities.extensions[]` declares
//! the Brutor HMAC delegation extension URI — tenants that have a v0.3
//! card already under our gateway are implicitly opted into delegation
//! verification, so surfacing the extension matches reality.
//!
//! ## What the translator does NOT do
//!
//!   * It does **not** mutate the DB.
//!   * It does **not** re-sign the card. Legacy-signature cards come
//!     out of the translator with `signatures: []`; admin callers
//!     must re-sign via the Python card-signing flow.
//!   * It does **not** translate v1.0 back to v0.3 — one-way only.

use serde_json::{Map, Value, json};

use super::BRUTOR_DELEGATION_EXTENSION_URI;

/// Dialect detected on a stored card. See [`detect_dialect`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CardDialect {
    /// v1.0 — has `supportedInterfaces[]` at the top level.
    V1,
    /// v0.3 — top-level `url` string or `protocolVersion: "0.x"`.
    V03,
    /// Unknown — neither heuristic matched. We refuse to guess rather
    /// than silently mis-translate.
    Unknown,
}

/// Cheap heuristic that mirrors the Python sibling:
///
///   1. Presence of `supportedInterfaces` ⇒ v1.0.
///   2. Presence of `url` or `additionalInterfaces` at the top level
///      ⇒ v0.3.
///   3. `protocolVersion` starting with `0.` ⇒ v0.3.
///   4. Otherwise unknown.
pub fn detect_dialect(card_json: &Value) -> CardDialect {
    let Value::Object(map) = card_json else {
        return CardDialect::Unknown;
    };
    if map.contains_key("supportedInterfaces") {
        return CardDialect::V1;
    }
    if map.contains_key("url") || map.contains_key("additionalInterfaces") {
        return CardDialect::V03;
    }
    if let Some(Value::String(pv)) = map.get("protocolVersion") {
        if pv.starts_with("0.") {
            return CardDialect::V03;
        }
    }
    CardDialect::Unknown
}

/// Error returned when the translator refuses to produce output.
#[derive(Debug)]
pub enum TranslateError {
    /// Input wasn't a JSON object.
    NotAnObject,
    /// v0.3 card had neither `url` nor any usable `additionalInterfaces`
    /// entry — we can't build `supportedInterfaces[]` from nothing.
    NoUsableUrl,
    /// Dialect couldn't be determined. Surface this instead of
    /// silently treating unknown input as v1.0 (which would emit an
    /// under-populated card that then fails v1.0 validation).
    UnknownDialect,
}

impl std::fmt::Display for TranslateError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            TranslateError::NotAnObject => write!(f, "card_json is not a JSON object"),
            TranslateError::NoUsableUrl => write!(
                f,
                "v0.3 card has no `url` and no usable `additionalInterfaces`"
            ),
            TranslateError::UnknownDialect => write!(f, "card dialect could not be determined"),
        }
    }
}

impl std::error::Error for TranslateError {}

/// Translate a v0.3 card JSON value to v1.0 shape.
///
/// * A card that already looks like v1.0 is deep-cloned unchanged —
///   idempotent so a caller can translate unconditionally without
///   needing to detect the dialect themselves.
/// * A card whose dialect we can't detect yields
///   [`TranslateError::UnknownDialect`] — we refuse to guess.
/// * A v0.3 card with no usable URL yields [`TranslateError::NoUsableUrl`].
pub fn translate_v03_to_v1(card_json: &Value) -> Result<Value, TranslateError> {
    match detect_dialect(card_json) {
        CardDialect::V1 => Ok(ensure_delegation_extension(card_json.clone())),
        CardDialect::V03 => translate_v03(card_json),
        CardDialect::Unknown => Err(TranslateError::UnknownDialect),
    }
}

fn translate_v03(card_json: &Value) -> Result<Value, TranslateError> {
    let Value::Object(obj) = card_json else {
        return Err(TranslateError::NotAnObject);
    };
    let mut out: Map<String, Value> = obj.clone();

    let primary_url = out.remove("url");
    let primary_proto_version = out.remove("protocolVersion");
    let primary_binding = out.remove("preferredTransport");
    let additional = out.remove("additionalInterfaces");

    let default_version = primary_proto_version
        .as_ref()
        .and_then(|v| v.as_str())
        .unwrap_or("0.3")
        .to_string();

    let mut supported: Vec<Value> = Vec::new();
    if let Some(Value::String(u)) = &primary_url {
        supported.push(json!({
            "url": u,
            "protocolBinding": primary_binding
                .as_ref()
                .and_then(|v| v.as_str())
                .unwrap_or("JSONRPC"),
            "protocolVersion": default_version,
        }));
    }

    if let Some(Value::Array(arr)) = &additional {
        for entry in arr {
            let Value::Object(em) = entry else {
                continue;
            };
            let url = match em.get("url").and_then(|v| v.as_str()) {
                Some(s) if !s.is_empty() => s.to_string(),
                _ => continue,
            };
            let binding = em
                .get("transport")
                .or_else(|| em.get("protocolBinding"))
                .and_then(|v| v.as_str())
                .unwrap_or("JSONRPC")
                .to_string();
            let version = em
                .get("protocolVersion")
                .and_then(|v| v.as_str())
                .unwrap_or(&default_version)
                .to_string();
            supported.push(json!({
                "url": url,
                "protocolBinding": binding,
                "protocolVersion": version,
            }));
        }
    }

    if supported.is_empty() {
        return Err(TranslateError::NoUsableUrl);
    }
    out.insert("supportedInterfaces".to_string(), Value::Array(supported));

    // ── capabilities.extendedAgentCard from legacy flag ───────────
    let ext_card_flag = out.remove("supportsAuthenticatedExtendedCard");
    ensure_capabilities_object(&mut out);
    if let Some(Value::Bool(b)) = ext_card_flag {
        if let Some(Value::Object(caps)) = out.get_mut("capabilities") {
            caps.entry("extendedAgentCard").or_insert(Value::Bool(b));
        }
    }

    // ── Drop legacy flat signature fields ─────────────────────────
    for legacy in ["signature", "signatureAlgorithm", "signedBy", "signedAt"] {
        out.remove(legacy);
    }

    // v1.0 requires signatures[] field; seed an empty array when the
    // translated card has no JWS signatures yet. Admin can re-sign.
    out.entry("signatures".to_string())
        .or_insert_with(|| Value::Array(Vec::new()));

    // ── v1.0-required fields that v0.3 made optional ──────────────
    out.entry("defaultInputModes".to_string())
        .or_insert_with(|| Value::Array(Vec::new()));
    out.entry("defaultOutputModes".to_string())
        .or_insert_with(|| Value::Array(Vec::new()));
    out.entry("skills".to_string())
        .or_insert_with(|| Value::Array(Vec::new()));
    out.entry("description".to_string())
        .or_insert_with(|| Value::String(String::new()));
    out.entry("version".to_string())
        .or_insert_with(|| Value::String("0.0.0".to_string()));

    // Surface the Brutor delegation extension on translated cards.
    // Cards served through this gateway carry the HMAC delegation
    // chain unconditionally, so advertising it keeps v1.0 verifiers
    // honest rather than silently dropping trust signals.
    let translated = Value::Object(out);
    Ok(ensure_delegation_extension(translated))
}

fn ensure_capabilities_object(out: &mut Map<String, Value>) {
    match out.get("capabilities") {
        Some(Value::Object(_)) => {}
        _ => {
            out.insert("capabilities".to_string(), Value::Object(Map::new()));
        }
    }
}

/// Append `BRUTOR_DELEGATION_EXTENSION_URI` to `capabilities.extensions[]`
/// if it isn't already advertised. No-op when the array already
/// contains an entry with the same `uri`.
pub fn ensure_delegation_extension(card_json: Value) -> Value {
    let Value::Object(mut map) = card_json else {
        return card_json;
    };
    let caps = match map.entry("capabilities".to_string()) {
        serde_json::map::Entry::Occupied(e) => e.into_mut(),
        serde_json::map::Entry::Vacant(v) => v.insert(Value::Object(Map::new())),
    };
    let Value::Object(caps_map) = caps else {
        // Caller handed us a card with capabilities as a non-object.
        // Don't clobber it; returning the card unchanged is safer
        // than overwriting user-supplied data.
        return Value::Object(map);
    };
    let extensions = match caps_map.entry("extensions".to_string()) {
        serde_json::map::Entry::Occupied(e) => e.into_mut(),
        serde_json::map::Entry::Vacant(v) => v.insert(Value::Array(Vec::new())),
    };
    let Value::Array(ext_arr) = extensions else {
        return Value::Object(map);
    };
    let already = ext_arr.iter().any(|e| {
        matches!(
            e.get("uri").and_then(|u| u.as_str()),
            Some(u) if u == BRUTOR_DELEGATION_EXTENSION_URI
        )
    });
    if !already {
        ext_arr.push(json!({
            "uri": BRUTOR_DELEGATION_EXTENSION_URI,
            "description": "Brutor HMAC-signed delegation chain — anti-spoofing primitive across gateways.",
            "required": false
        }));
    }
    Value::Object(map)
}

// =============================================================================
// Tests
// =============================================================================

#[cfg(test)]
mod tests {
    use super::*;

    fn v03_card() -> Value {
        json!({
            "name": "billing-agent",
            "description": "Refunds + invoices",
            "url": "https://billing.example.com/rpc",
            "protocolVersion": "0.3",
            "preferredTransport": "JSONRPC",
            "additionalInterfaces": [
                {"url": "https://billing.example.com/rest", "transport": "HTTP+JSON"}
            ],
            "version": "1.2.3",
            "capabilities": {"streaming": true},
            "supportsAuthenticatedExtendedCard": true,
            "signature": "deadbeef",
            "signatureAlgorithm": "Ed25519",
            "signedBy": "tenant-1",
            "signedAt": "2026-04-24T10:00:00Z",
            "skills": [],
            "defaultInputModes": ["text"],
            "defaultOutputModes": ["text"]
        })
    }

    #[test]
    fn detects_v1_via_supported_interfaces() {
        let v = json!({"supportedInterfaces": []});
        assert_eq!(detect_dialect(&v), CardDialect::V1);
    }

    #[test]
    fn detects_v03_via_url() {
        let v = json!({"url": "https://x"});
        assert_eq!(detect_dialect(&v), CardDialect::V03);
    }

    #[test]
    fn detects_v03_via_protocol_version_prefix() {
        let v = json!({"protocolVersion": "0.3"});
        assert_eq!(detect_dialect(&v), CardDialect::V03);
    }

    #[test]
    fn detects_unknown_on_empty_object() {
        let v = json!({});
        assert_eq!(detect_dialect(&v), CardDialect::Unknown);
    }

    #[test]
    fn detects_unknown_on_non_object() {
        let v = json!("not an object");
        assert_eq!(detect_dialect(&v), CardDialect::Unknown);
    }

    #[test]
    fn v03_translation_promotes_url_to_supported_interfaces() {
        let out = translate_v03_to_v1(&v03_card()).unwrap();
        let interfaces = out.get("supportedInterfaces").unwrap().as_array().unwrap();
        assert_eq!(interfaces.len(), 2);
        assert_eq!(
            interfaces[0].get("url").unwrap(),
            "https://billing.example.com/rpc"
        );
        assert_eq!(interfaces[0].get("protocolBinding").unwrap(), "JSONRPC");
        assert_eq!(interfaces[0].get("protocolVersion").unwrap(), "0.3");
        assert_eq!(
            interfaces[1].get("url").unwrap(),
            "https://billing.example.com/rest"
        );
        assert_eq!(interfaces[1].get("protocolBinding").unwrap(), "HTTP+JSON");
    }

    #[test]
    fn v03_translation_strips_top_level_url_and_friends() {
        let out = translate_v03_to_v1(&v03_card()).unwrap();
        assert!(out.get("url").is_none());
        assert!(out.get("protocolVersion").is_none());
        assert!(out.get("preferredTransport").is_none());
        assert!(out.get("additionalInterfaces").is_none());
    }

    #[test]
    fn v03_translation_drops_legacy_flat_signature_fields() {
        let out = translate_v03_to_v1(&v03_card()).unwrap();
        for key in ["signature", "signatureAlgorithm", "signedBy", "signedAt"] {
            assert!(out.get(key).is_none(), "key {key} must be dropped");
        }
        assert!(
            out.get("signatures")
                .unwrap()
                .as_array()
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn v03_translation_promotes_extended_card_flag_into_capabilities() {
        let out = translate_v03_to_v1(&v03_card()).unwrap();
        let caps = out.get("capabilities").unwrap().as_object().unwrap();
        assert_eq!(caps.get("extendedAgentCard").unwrap(), &Value::Bool(true));
        assert_eq!(caps.get("streaming").unwrap(), &Value::Bool(true));
    }

    #[test]
    fn v03_translation_declares_delegation_extension() {
        let out = translate_v03_to_v1(&v03_card()).unwrap();
        let exts = out
            .get("capabilities")
            .unwrap()
            .get("extensions")
            .unwrap()
            .as_array()
            .unwrap();
        assert_eq!(exts.len(), 1);
        assert_eq!(exts[0].get("uri").unwrap(), BRUTOR_DELEGATION_EXTENSION_URI,);
    }

    #[test]
    fn delegation_extension_is_not_duplicated() {
        // If the card already advertises the delegation extension,
        // we must not add a second copy.
        let v = json!({
            "supportedInterfaces": [],
            "capabilities": {
                "extensions": [
                    {"uri": BRUTOR_DELEGATION_EXTENSION_URI, "required": true}
                ]
            }
        });
        let out = translate_v03_to_v1(&v).unwrap();
        let exts = out
            .get("capabilities")
            .unwrap()
            .get("extensions")
            .unwrap()
            .as_array()
            .unwrap();
        assert_eq!(exts.len(), 1);
        // Respects the caller's required=true flag — we don't overwrite.
        assert_eq!(exts[0].get("required").unwrap(), &Value::Bool(true));
    }

    #[test]
    fn v1_input_is_returned_unchanged_aside_from_extension() {
        let v = json!({
            "name": "agent",
            "supportedInterfaces": [
                {"url": "https://x", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
            ],
            "capabilities": {"streaming": true}
        });
        let out = translate_v03_to_v1(&v).unwrap();
        // Supported interfaces preserved verbatim.
        let ifaces = out.get("supportedInterfaces").unwrap().as_array().unwrap();
        assert_eq!(ifaces.len(), 1);
        assert_eq!(ifaces[0].get("protocolVersion").unwrap(), "1.0");
        // Delegation extension added.
        let exts = out
            .get("capabilities")
            .unwrap()
            .get("extensions")
            .unwrap()
            .as_array()
            .unwrap();
        assert_eq!(exts.len(), 1);
    }

    #[test]
    fn v03_translation_preserves_pass_through_fields() {
        let out = translate_v03_to_v1(&v03_card()).unwrap();
        assert_eq!(out.get("name").unwrap(), "billing-agent");
        assert_eq!(out.get("description").unwrap(), "Refunds + invoices");
        assert_eq!(out.get("version").unwrap(), "1.2.3");
        assert_eq!(
            out.get("defaultInputModes")
                .unwrap()
                .as_array()
                .unwrap()
                .len(),
            1,
        );
    }

    #[test]
    fn v03_translation_missing_url_fails() {
        let v = json!({
            "name": "bad",
            "protocolVersion": "0.3"
        });
        let err = translate_v03_to_v1(&v).unwrap_err();
        assert!(matches!(err, TranslateError::NoUsableUrl));
    }

    #[test]
    fn v03_translation_uses_primary_version_for_additional_interfaces() {
        // An additional interface without its own protocolVersion
        // inherits the primary's — matches the Python translator so
        // a partner reading either implementation gets the same card.
        let v = json!({
            "url": "https://x/rpc",
            "protocolVersion": "0.3",
            "additionalInterfaces": [{"url": "https://x/rest", "transport": "HTTP+JSON"}]
        });
        let out = translate_v03_to_v1(&v).unwrap();
        let ifaces = out.get("supportedInterfaces").unwrap().as_array().unwrap();
        assert_eq!(ifaces[1].get("protocolVersion").unwrap(), "0.3");
    }

    #[test]
    fn v03_translation_skips_malformed_additional_interfaces() {
        let v = json!({
            "url": "https://x/rpc",
            "additionalInterfaces": [
                {"url": ""},
                {"not_a_url_field": true},
                "this is not an object at all",
                {"url": "https://x/rest", "transport": "HTTP+JSON"}
            ]
        });
        let out = translate_v03_to_v1(&v).unwrap();
        let ifaces = out.get("supportedInterfaces").unwrap().as_array().unwrap();
        // Primary + one valid additional = 2.
        assert_eq!(ifaces.len(), 2);
        assert_eq!(ifaces[1].get("url").unwrap(), "https://x/rest");
    }

    #[test]
    fn unknown_dialect_surfaces_error() {
        // No url, no supportedInterfaces, no v0.x protocolVersion —
        // we can't tell what this is and refuse to guess.
        let v = json!({"name": "mystery"});
        let err = translate_v03_to_v1(&v).unwrap_err();
        assert!(matches!(err, TranslateError::UnknownDialect));
    }

    #[test]
    fn translated_card_parses_as_agent_card_type() {
        // Belt-and-braces: running the output through the strict
        // v1.0 AgentCard deserialization must succeed. Otherwise we
        // could ship a card that fails `validate_card_v1` on the
        // Python side — bad for round-trip confidence.
        let out = translate_v03_to_v1(&v03_card()).unwrap();
        let parsed: Result<super::super::AgentCard, _> = serde_json::from_value(out);
        assert!(
            parsed.is_ok(),
            "translated card must deserialize as v1.0 AgentCard: {parsed:?}"
        );
    }
}
