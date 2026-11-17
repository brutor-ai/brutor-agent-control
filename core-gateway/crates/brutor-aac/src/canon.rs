// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Format-4 canonicalisation and Capsule identity (draft-04 §2, §5.1).
//!
//! * `JSON-DIGEST(v)` = lowercase-hex SHA-256 over `UTF8(JCS(v))`, plain
//!   RFC 8785 over the whole value — no member filtering at any depth.
//! * `capsule_id` = `JSON-DIGEST(capsule \ {capsule_id, signature, key_id})`.
//!   `signature` / `key_id` are *local-only* producer-envelope bookkeeping
//!   fields a `capsule-emit` ledger line carries next to the Capsule; they
//!   are attached after the id is computed and are never in its preimage.
//! * Floats anywhere, and integers outside ±(2^53−1), make a value
//!   undigestable (the draft requires exact decimal strings instead).
//!
//! The JCS is implemented here rather than borrowed from `serde_jcs`
//! (which `brutor_evidence` uses for record ids): the vendored vector
//! `canonical-key-sort-utf16-vs-codepoint` shows `serde_jcs` orders object
//! members by code point, while RFC 8785 §3.2.3 orders them by UTF-16 code
//! units (`"😀"` U+1F600 = D83D DE00 sorts *before* `"Ｚ"` U+FF3A). The two
//! only disagree when one key has a supplementary-plane character and the
//! other a BMP character at or above U+E000.

use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::{CANONICALIZATION_ID, FORMAT_VERSION};

/// Members that never participate in a Capsule-ID preimage.
pub const LOCAL_ONLY_FIELDS: &[&str] = &["signature", "key_id"];

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum CapsuleIdError {
    #[error("capsule is not a JSON object")]
    NotObject,
    #[error("format_version must be \"4\"")]
    FormatVersion,
    #[error("canonicalization_id is required")]
    CanonicalizationMissing,
    #[error("canonicalization_id must be a string")]
    CanonicalizationNotString,
    #[error("unsupported canonicalization_id {0:?}")]
    CanonicalizationUnsupported(String),
    #[error("float at {0}: floating-point values are forbidden in digest-bearing material")]
    Float(String),
    #[error("integer at {0} outside ±(2^53−1)")]
    UnsafeInteger(String),
    #[error("canonicalisation failed: {0}")]
    Jcs(String),
}

pub const MAX_SAFE_INTEGER: u64 = (1u64 << 53) - 1;

/// `JSON-DIGEST(value)` (lowercase hex).
pub fn json_digest(value: &Value) -> Result<String, CapsuleIdError> {
    Ok(hex::encode(Sha256::digest(jcs(value)?)))
}

/// RFC 8785 bytes of `value` — the shared, UTF-16-ordered writer in
/// `brutor_evidence::canon`. Floats and integers outside ±(2^53−1) are
/// refused (the draft forbids them in digest-bearing material).
pub fn jcs(value: &Value) -> Result<Vec<u8>, CapsuleIdError> {
    use brutor_evidence::canon::{CanonError, canonical_bytes};
    canonical_bytes(value).map_err(|e| match e {
        CanonError::Float(p) => CapsuleIdError::Float(p),
        CanonError::UnsafeInteger(p) => CapsuleIdError::UnsafeInteger(p),
        CanonError::Jcs(m) => CapsuleIdError::Jcs(m),
    })
}

/// The Capsule-ID preimage object: the Capsule minus `capsule_id` and the
/// local-only envelope fields. Fails closed on any declaration other than
/// `format_version: "4"` + `canonicalization_id: "jcs"`.
pub fn capsule_id_preimage(capsule: &Value) -> Result<Value, CapsuleIdError> {
    let obj = capsule.as_object().ok_or(CapsuleIdError::NotObject)?;
    if obj.get("format_version").and_then(Value::as_str) != Some(FORMAT_VERSION) {
        return Err(CapsuleIdError::FormatVersion);
    }
    match obj.get("canonicalization_id") {
        None => return Err(CapsuleIdError::CanonicalizationMissing),
        Some(Value::String(s)) if s == CANONICALIZATION_ID => {}
        Some(Value::String(s)) => {
            return Err(CapsuleIdError::CanonicalizationUnsupported(s.clone()));
        }
        Some(_) => return Err(CapsuleIdError::CanonicalizationNotString),
    }
    let mut pre = obj.clone();
    pre.remove("capsule_id");
    for f in LOCAL_ONLY_FIELDS {
        pre.remove(*f);
    }
    Ok(Value::Object(pre))
}

/// Recompute a format-4 Capsule ID.
pub fn compute_capsule_id(capsule: &Value) -> Result<String, CapsuleIdError> {
    json_digest(&capsule_id_preimage(capsule)?)
}

pub fn is_hex64(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

pub(crate) fn value_is_hex64(v: Option<&Value>) -> bool {
    v.and_then(Value::as_str).map(is_hex64).unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn base() -> Value {
        json!({
            "spec_version": "draft-mih-scitt-agent-action-capsule-04",
            "format_version": "4",
            "canonicalization_id": "jcs",
            "action_id": "a", "action_type": "fyi", "operator": "o",
            "developer": "d", "timestamp": "2026-09-19T00:00:00Z"
        })
    }

    #[test]
    fn local_only_fields_and_carried_id_never_in_preimage() {
        let a = compute_capsule_id(&base()).unwrap();
        let mut b = base();
        b["capsule_id"] = json!("0".repeat(64));
        b["signature"] = json!("d284");
        b["key_id"] = json!("ab");
        assert_eq!(compute_capsule_id(&b).unwrap(), a);
        // Any other member participates.
        let mut c = base();
        c["references"] = json!([]);
        assert_ne!(compute_capsule_id(&c).unwrap(), a);
    }

    #[test]
    fn jcs_sorts_by_utf16_code_units_and_escapes_minimally() {
        // U+1F600 (D83D DE00) sorts before U+FF3A by UTF-16 units; code-point
        // order would reverse them.
        let v = json!({"\u{FF3A}": 1, "\u{1F600}": 2, "b": "a\u{1}\"\\/\u{7f}\u{2028}"});
        let s = String::from_utf8(jcs(&v).unwrap()).unwrap();
        assert_eq!(
            s,
            "{\"b\":\"a\\u0001\\\"\\\\/\u{7f}\u{2028}\",\"\u{1F600}\":2,\"\u{FF3A}\":1}"
        );
        assert_eq!(
            jcs(&json!({"n": 9007199254740992u64})),
            Err(CapsuleIdError::UnsafeInteger("$.n".into()))
        );
        assert_eq!(
            jcs(&json!([1, -9007199254740991i64])).unwrap(),
            b"[1,-9007199254740991]"
        );
    }

    #[test]
    fn fails_closed_on_declarations() {
        let mut v = base();
        v["canonicalization_id"] = json!("jcs-n");
        assert_eq!(
            compute_capsule_id(&v),
            Err(CapsuleIdError::CanonicalizationUnsupported("jcs-n".into()))
        );
        v["canonicalization_id"] = Value::Null;
        assert_eq!(
            compute_capsule_id(&v),
            Err(CapsuleIdError::CanonicalizationNotString)
        );
        v.as_object_mut().unwrap().remove("canonicalization_id");
        assert_eq!(
            compute_capsule_id(&v),
            Err(CapsuleIdError::CanonicalizationMissing)
        );
        let mut w = base();
        w["format_version"] = json!("3");
        assert_eq!(compute_capsule_id(&w), Err(CapsuleIdError::FormatVersion));
        let mut f = base();
        f["x"] = json!(1.5);
        assert!(matches!(
            compute_capsule_id(&f),
            Err(CapsuleIdError::Float(_))
        ));
    }
}
