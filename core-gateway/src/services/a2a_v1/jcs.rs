// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! JSON Canonicalization Scheme (RFC 8785) helpers.
//!
//! A2A v1.0 signs Agent Cards by producing a canonical byte sequence
//! from the card JSON and computing a JWS signature over those bytes.
//! "Canonical" here means RFC 8785, which nails down:
//!
//!   * **UTF-16 key ordering** on object properties (not byte order).
//!   * **ECMAScript `ToString` for numbers** — e.g. `1.5e0` → `"1.5"`,
//!     `1e21` → `"1e+21"`, trailing zeros dropped for non-integer
//!     decimals.
//!   * **String escape set**: backslash-escape only the small set of
//!     characters JSON requires, using the shortest form (e.g. `"\n"`
//!     for U+000A, `"\""` for U+0022, `"\\"` for U+005C, `"\uXXXX"`
//!     for controls < 0x20 without a short form).
//!   * **No superfluous whitespace** anywhere.
//!
//! The writer is `brutor_evidence::canon::canonical_bytes_ecmascript` —
//! the same RFC 8785 writer the evidence records use, in the mode that
//! formats numbers per §3.2.2.3 instead of refusing them (cards may carry
//! floats in extension params). It replaced `serde_jcs`, which orders
//! object members by Unicode code point rather than by UTF-16 code units:
//! the two orders disagree when one key holds a character above U+FFFF and
//! another one in U+E000–U+FFFF, so a card with such keys signed here
//! would not verify anywhere else. For every card whose member names are
//! ASCII (or free of that pairing) the bytes are unchanged, so existing
//! signatures still verify (pinned by `jws::tests`).
//!
//! Our job on top is:
//!
//!   * A clear error type so signing / verification errors are easy
//!     to diagnose in logs.
//!   * Conformance tests against the official RFC 8785 vectors (plus
//!     Brutor-specific fixtures exercising edge cases we hit, like
//!     nested `google.protobuf.Struct` metadata and unicode names).
//!   * A convenience `canonicalize_value` entry point that takes a
//!     `serde_json::Value` (which is how we'll usually hold the card
//!     in memory before signing).

use std::fmt;

/// Canonicalize an in-memory JSON value to its RFC 8785 byte form.
///
/// Returns the canonical UTF-8 bytes ready to feed into a signer.
/// Errors only if the underlying JSON tree contains a non-finite
/// number (NaN / ±Inf) — those are illegal per JSON and can't be
/// canonicalized. Practically unreachable when inputs come from
/// serde round-trips of our strongly-typed models.
pub fn canonicalize_value(value: &serde_json::Value) -> Result<Vec<u8>, JcsError> {
    brutor_evidence::canon::canonical_bytes_ecmascript(value).map_err(|e| JcsError {
        message: format!("JCS canonicalization failed: {e}"),
    })
}

/// Canonicalize any serializable in-memory type.
///
/// Wrapper around [`canonicalize_value`] useful when you already hold
/// a typed struct (e.g. [`crate::services::a2a_v1::AgentCard`]) and
/// want to avoid an intermediate [`serde_json::Value`] trip. The
/// resulting bytes are identical because serde's Serialize impl is
/// tree-agnostic.
pub fn canonicalize<T: serde::Serialize>(value: &T) -> Result<Vec<u8>, JcsError> {
    let v = serde_json::to_value(value).map_err(JcsError::from_serde)?;
    canonicalize_value(&v)
}

/// Canonicalization error. Wraps serde's error type so callers don't
/// need to import serde's module path to pattern-match.
#[derive(Debug)]
pub struct JcsError {
    message: String,
}

impl JcsError {
    fn from_serde(e: serde_json::Error) -> Self {
        Self {
            message: format!("JCS canonicalization failed: {e}"),
        }
    }
}

impl fmt::Display for JcsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for JcsError {}

// =============================================================================
// Tests
// =============================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    // ---------------------------------------------------------------------
    // RFC 8785 Appendix A vectors
    //
    // These are the normative examples from the RFC. The crate we use
    // (serde_jcs) claims compliance; we re-check here so an upgrade
    // that regressed behaviour would break our build rather than
    // silently break signatures in production.
    // ---------------------------------------------------------------------

    /// Appendix A.1 — ordering of member names.
    #[test]
    fn rfc8785_member_ordering() {
        let input = json!({"b": 2, "a": 1});
        let canonical = canonicalize_value(&input).unwrap();
        assert_eq!(canonical, br#"{"a":1,"b":2}"# as &[u8]);
    }

    /// Appendix A.1 — numbers use ECMAScript ToString semantics.
    ///
    /// The RFC's example has `1.5e+0` → `1.5`. We assert a set of
    /// canonicalisations that together exercise: integer preservation,
    /// decimal fraction minimisation, small-negative-exponent case,
    /// and the 1e21 boundary where ES ToString switches to exponent
    /// form.
    #[test]
    fn rfc8785_number_formatting() {
        assert_eq!(canonicalize_value(&json!(1.5)).unwrap(), b"1.5");
        assert_eq!(canonicalize_value(&json!(-0.0)).unwrap(), b"0");
        assert_eq!(canonicalize_value(&json!(100)).unwrap(), b"100");
        // 1e21 is the ES boundary where exponent form kicks in.
        assert_eq!(canonicalize_value(&json!(1e21)).unwrap(), b"1e+21");
    }

    /// Appendix A.1 — strings use the minimal escape set. Newlines
    /// become `\n`, DEL stays printable (it's ≥ 0x20), quotes become
    /// `\"`, backslashes become `\\`.
    #[test]
    fn rfc8785_string_escapes() {
        assert_eq!(
            canonicalize_value(&json!("line1\nline2")).unwrap(),
            br#""line1\nline2""# as &[u8],
        );
        assert_eq!(
            canonicalize_value(&json!("quote\"backslash\\")).unwrap(),
            br#""quote\"backslash\\""# as &[u8],
        );
        // Control character below 0x20 with no short form gets \uXXXX.
        assert_eq!(
            canonicalize_value(&json!("\u{0001}")).unwrap(),
            br#""\u0001""# as &[u8],
        );
    }

    /// Appendix A.1 — arrays preserve order; nested objects recurse.
    #[test]
    fn rfc8785_arrays_preserve_order_and_recurse() {
        let input = json!({
            "array": [3, 1, 2],
            "nested": {"zz": 9, "aa": 1}
        });
        let canonical = canonicalize_value(&input).unwrap();
        assert_eq!(
            canonical,
            br#"{"array":[3,1,2],"nested":{"aa":1,"zz":9}}"# as &[u8]
        );
    }

    /// Unicode member names must order by UTF-16 code units, not by
    /// byte order. Specifically: "é" (U+00E9) sorts **after** "z"
    /// because U+00E9 > U+007A as code units, not before despite its
    /// UTF-8 encoding starting with 0xC3.
    #[test]
    fn rfc8785_unicode_key_ordering_uses_utf16_codepoints() {
        let input = json!({"\u{00E9}": 1, "z": 2});
        let canonical = canonicalize_value(&input).unwrap();
        // "z" < "é" on UTF-16 ordering.
        let s = String::from_utf8(canonical).unwrap();
        let z_pos = s.find("\"z\"").unwrap();
        let e_pos = s.find("\"\u{00E9}\"").unwrap();
        assert!(z_pos < e_pos, "UTF-16 order places z before é: {s}");
    }

    /// No superfluous whitespace anywhere.
    #[test]
    fn rfc8785_no_whitespace() {
        let input = json!({
            "a":   [1, 2, 3],
            "b":   { "c": 4 }
        });
        let canonical = canonicalize_value(&input).unwrap();
        let s = std::str::from_utf8(&canonical).unwrap();
        // Neither spaces nor tabs nor newlines.
        assert!(
            !s.chars()
                .any(|c| c == ' ' || c == '\t' || c == '\n' || c == '\r'),
            "canonical form contained whitespace: {s:?}"
        );
    }

    /// Canonicalization is idempotent: feeding canonical bytes back
    /// through a parser + canonicalizer yields the same bytes.
    #[test]
    fn rfc8785_idempotent_roundtrip() {
        let input = json!({"x": [1, 2, "str\n"], "y": {"a": 1, "b": true, "c": null}});
        let once = canonicalize_value(&input).unwrap();
        let reparsed: serde_json::Value =
            serde_json::from_slice(&once).expect("canonical bytes are valid JSON");
        let twice = canonicalize_value(&reparsed).unwrap();
        assert_eq!(once, twice, "canonicalization must be idempotent");
    }

    // ---------------------------------------------------------------------
    // Brutor fixtures — shapes we actually hit when signing an Agent Card.
    // ---------------------------------------------------------------------

    /// The AgentCard struct carries a BTreeMap<String, SecurityScheme>
    /// for security_schemes. Its serde output is already ordered, but
    /// we signed-bytes via JCS so we don't rely on that —
    /// canonicalization is the single source of truth.
    #[test]
    fn agent_card_fragment_canonical_form_is_stable() {
        use crate::services::a2a_v1::*;
        use std::collections::BTreeMap;

        let card = AgentCard {
            name: "billing-agent".into(),
            description: "desc".into(),
            supported_interfaces: vec![AgentInterface {
                url: "https://api/".into(),
                protocol_binding: "JSONRPC".into(),
                tenant: None,
                protocol_version: "1.0".into(),
            }],
            provider: None,
            version: "1.0.0".into(),
            documentation_url: None,
            capabilities: AgentCapabilities::default(),
            security_schemes: BTreeMap::new(),
            security_requirements: vec![],
            default_input_modes: vec!["text/plain".into()],
            default_output_modes: vec!["application/json".into()],
            skills: vec![],
            signatures: vec![],
            icon_url: None,
        };
        // Canonicalise twice — must be byte-for-byte equal.
        let a = canonicalize(&card).expect("canonicalize");
        let b = canonicalize(&card).expect("canonicalize");
        assert_eq!(a, b);

        // Minor structural perturbation (empty `signatures` field
        // omitted vs present) must not change canonical output.
        // Because `#[serde(skip_serializing_if = "Vec::is_empty")]`
        // is in force on `signatures`, an empty vec is omitted, so
        // adding an explicit empty array via a JSON tree also yields
        // the same canonical bytes iff the signer strips it first.
        let mut as_value = serde_json::to_value(&card).unwrap();
        as_value["signatures"] = json!([]); // explicit empty
        let c = canonicalize_value(&as_value).expect("canonicalize value");
        // NOTE: c DOES differ from a — `signatures: []` is a distinct
        // JCS output from its absence. This asserts our
        // "strip signatures before signing" rule below: don't feed a
        // Value tree with an explicit empty `signatures` to the signer;
        // use the struct form.
        assert_ne!(a, c, "explicit signatures:[] changes canonical form");
    }

    /// RFC 8785 §3.2.3 orders members by UTF-16 code units. U+FFFD vs
    /// U+10000 is where that differs from code-point order (what
    /// `serde_jcs` does): U+10000 is the surrogate pair D800 DC00 and
    /// sorts first. A card carrying such keys signed with the old order
    /// would not verify in any other RFC 8785 implementation.
    #[test]
    fn card_member_order_is_utf16_not_code_point() {
        let card_fragment = json!({
            "name": "agent",
            "capabilities": {"extensions": [{"uri": "urn:x", "params": {
                "\u{FFFD}": "replacement",
                "\u{10000}": "linear-b"
            }}]}
        });
        let ours = String::from_utf8(canonicalize_value(&card_fragment).unwrap()).unwrap();
        assert!(
            ours.contains("{\"\u{10000}\":\"linear-b\",\"\u{FFFD}\":\"replacement\"}"),
            "{ours}"
        );
        let old = String::from_utf8(serde_jcs::to_vec(&card_fragment).unwrap()).unwrap();
        assert_ne!(ours, old, "serde_jcs orders these by code point");
    }

    /// For documents whose member names do not hit the UTF-16 corner case
    /// the bytes are exactly what `serde_jcs` produced — existing
    /// signatures keep verifying — including every number shape (the
    /// ECMAScript formatter is cross-checked against serde_jcs's ryu-js).
    #[test]
    fn ascii_documents_are_byte_identical_to_serde_jcs() {
        let doc = json!({
            "b": [1.5, 0.1, 1e21, 1e-7, -0.0, 333333333.3333333, 5e-324,
                  1.7976931348623157e308, 9007199254740992.0, 100.0, -2.5e-5],
            "a": {"z": "t\u{1}\n\"é/", "y": null, "x": true, "n": -42,
                  "u": 18446744073709551615u64},
            "é": 1,
            "Z": {}
        });
        assert_eq!(
            canonicalize_value(&doc).unwrap(),
            serde_jcs::to_vec(&doc).unwrap()
        );
        // A deterministic sweep over many magnitudes and digit counts.
        let mut x: u64 = 0x9E37_79B9_7F4A_7C15;
        for _ in 0..20_000 {
            x ^= x << 13;
            x ^= x >> 7;
            x ^= x << 17;
            let f = f64::from_bits(x);
            if !f.is_finite() {
                continue;
            }
            let v = json!(f);
            assert_eq!(
                canonicalize_value(&v).unwrap(),
                serde_jcs::to_vec(&v).unwrap(),
                "{f:e}"
            );
        }
    }
}
