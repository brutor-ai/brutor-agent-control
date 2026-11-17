// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Producer Envelope (draft-04 §3.1): a tagged COSE_Sign1 whose attached
//! payload is the raw 32-byte Capsule ID.
//!
//! ```text
//! protected   = { 1: -8, 3: "application/agent-action-capsule-id", 4: <raw 32-byte Ed25519 key> }
//! unprotected = {}
//! payload     = h'<32 bytes decoded from capsule_id>'
//! ```
//!
//! Exactly three protected entries, an empty unprotected map; anything
//! else is a profile failure, not an extension. A bare envelope is **not**
//! an RFC 9943 Signed Statement (no CWT `iss`/`sub`), so it is never
//! registered as one. Verification authenticates *a key*, returned to the
//! caller; whether that key is authorised for the Capsule's operator is
//! caller policy (§3.2).
//!
//! Domain separation from our own action-record statements: those carry
//! labels 15/258/259 and no label 3, so no Brutor statement signature can
//! be replayed as an envelope (or vice versa) even under the same key.

use brutor_cose::cbor::{self, Value as Cbor, as_bytes, as_i64, as_text, bytes, int, text};
use brutor_cose::sign1::{self, CoseSign1, alg, label};
use ed25519_dalek::{Signature, SigningKey, Verifier, VerifyingKey};
use serde::Serialize;

use crate::CAPSULE_ID_MEDIA_TYPE;
use crate::canon::is_hex64;

/// Upstream decoder cap; a conforming envelope is ~150 bytes.
pub const MAX_ENVELOPE_BYTES: usize = 4096;

#[derive(Debug, thiserror::Error)]
pub enum EnvelopeError {
    #[error("capsule_id MUST be 64 lowercase hexadecimal characters")]
    CapsuleId,
    #[error("cose: {0}")]
    Cose(#[from] brutor_cose::CoseError),
}

/// Sign `capsule_id` (lowercase hex) into a tagged COSE_Sign1 envelope.
pub fn sign_producer_envelope(
    capsule_id: &str,
    key: &SigningKey,
) -> Result<Vec<u8>, EnvelopeError> {
    if !is_hex64(capsule_id) {
        return Err(EnvelopeError::CapsuleId);
    }
    let payload = hex::decode(capsule_id).map_err(|_| EnvelopeError::CapsuleId)?;
    let protected = vec![
        (int(label::CONTENT_TYPE), text(CAPSULE_ID_MEDIA_TYPE)),
        (int(label::KID), bytes(key.verifying_key().as_bytes())),
    ];
    Ok(sign1::sign_ed25519(protected, vec![], Some(&payload), key)?.encode()?)
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct EnvelopeFinding {
    pub code: String,
    pub detail: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct EnvelopeResult {
    pub ok: bool,
    pub findings: Vec<EnvelopeFinding>,
    pub capsule_id: String,
    /// Hex of the authenticated raw Ed25519 key — set only when `ok`.
    /// Authentication, not authorisation.
    pub public_key_hex: Option<String>,
}

fn fail(capsule_id: &str, code: &str, detail: impl Into<String>) -> EnvelopeResult {
    EnvelopeResult {
        ok: false,
        findings: vec![EnvelopeFinding {
            code: code.into(),
            detail: detail.into(),
        }],
        capsule_id: capsule_id.into(),
        public_key_hex: None,
    }
}

/// Verify one envelope against a carried Capsule ID. Never panics; the
/// first failing profile rule is reported with the upstream finding code.
pub fn verify_producer_envelope(capsule_id: &str, data: &[u8]) -> EnvelopeResult {
    if !is_hex64(capsule_id) {
        return fail(
            capsule_id,
            "capsule_id_malformed",
            "capsule_id MUST be 64 lowercase hexadecimal characters",
        );
    }
    let expected = match hex::decode(capsule_id) {
        Ok(b) => b,
        Err(e) => return fail(capsule_id, "capsule_id_malformed", e.to_string()),
    };
    if data.len() > MAX_ENVELOPE_BYTES {
        return fail(
            capsule_id,
            "envelope_too_large",
            format!(
                "producer envelope is {} bytes; maximum is {MAX_ENVELOPE_BYTES}",
                data.len()
            ),
        );
    }
    let s = match CoseSign1::decode(data) {
        Ok(s) => s,
        Err(e) => {
            return fail(
                capsule_id,
                "envelope_malformed",
                format!("decoding COSE_Sign1: {e}"),
            );
        }
    };
    if s.protected_bytes.is_empty() {
        return fail(
            capsule_id,
            "envelope_malformed",
            "protected header MUST be a non-empty byte string",
        );
    }
    if !matches!(&s.unprotected, Cbor::Map(m) if m.is_empty()) {
        return fail(
            capsule_id,
            "envelope_malformed",
            "unprotected header MUST be an empty map",
        );
    }
    let Some(payload) = s.payload.as_deref() else {
        return fail(
            capsule_id,
            "envelope_malformed",
            "attached payload MUST be a byte string",
        );
    };
    if s.protected_get(label::ALG).and_then(as_i64) != Some(alg::EDDSA) {
        return fail(
            capsule_id,
            "envelope_algorithm_mismatch",
            "protected alg (label 1) MUST be EdDSA (-8)",
        );
    }
    if s.protected_get(label::CONTENT_TYPE).and_then(as_text) != Some(CAPSULE_ID_MEDIA_TYPE) {
        return fail(
            capsule_id,
            "envelope_content_type_mismatch",
            format!("protected content type (label 3) MUST be {CAPSULE_ID_MEDIA_TYPE:?}"),
        );
    }
    let kid: [u8; 32] = match s.protected_get(label::KID).and_then(as_bytes) {
        Some(k) if k.len() == 32 => k.try_into().unwrap_or([0u8; 32]),
        _ => {
            return fail(
                capsule_id,
                "envelope_kid_invalid",
                "protected kid (label 4) MUST be the raw 32-byte Ed25519 public key",
            );
        }
    };
    if !matches!(&s.protected, Cbor::Map(m) if m.len() == 3) {
        return fail(
            capsule_id,
            "envelope_protected_headers_invalid",
            "protected header MUST contain exactly alg, content type, and kid",
        );
    }
    if payload != expected.as_slice() {
        return fail(
            capsule_id,
            "envelope_payload_mismatch",
            "attached payload MUST equal the raw 32-byte Capsule ID",
        );
    }
    if s.signature.len() != 64 {
        return fail(
            capsule_id,
            "envelope_signature_invalid",
            "Ed25519 signature MUST be 64 bytes",
        );
    }
    // A 32-byte kid that is not a valid curve point cannot verify anything:
    // same outcome as a bad signature (the upstream Go verifier agrees).
    let Ok(vk) = VerifyingKey::from_bytes(&kid) else {
        return fail(
            capsule_id,
            "envelope_signature_invalid",
            "Ed25519 signature verification failed",
        );
    };
    let Ok(sig) = Signature::from_slice(&s.signature) else {
        return fail(
            capsule_id,
            "envelope_signature_invalid",
            "Ed25519 signature MUST be 64 bytes",
        );
    };
    let tbs = match sign1::sig_structure(&s.protected_bytes, payload) {
        Ok(t) => t,
        Err(e) => {
            return fail(
                capsule_id,
                "envelope_malformed",
                format!("encoding Sig_structure: {e}"),
            );
        }
    };
    if vk.verify(&tbs, &sig).is_err() {
        return fail(
            capsule_id,
            "envelope_signature_invalid",
            "Ed25519 signature verification failed",
        );
    }
    EnvelopeResult {
        ok: true,
        findings: vec![],
        capsule_id: capsule_id.into(),
        public_key_hex: Some(hex::encode(kid)),
    }
}

/// The protected map this crate emits, for documentation and tests.
pub fn protected_header_for(key: &VerifyingKey) -> Result<Vec<u8>, brutor_cose::CoseError> {
    let map = cbor::canonical_map(vec![
        (int(label::ALG), int(alg::EDDSA)),
        (int(label::CONTENT_TYPE), text(CAPSULE_ID_MEDIA_TYPE)),
        (int(label::KID), bytes(key.as_bytes())),
    ])?;
    cbor::encode(&map)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key() -> SigningKey {
        SigningKey::from_bytes(&[42u8; 32])
    }

    #[test]
    fn round_trip_and_exact_profile() {
        let id = "ab".repeat(32);
        let raw = sign_producer_envelope(&id, &key()).unwrap();
        let r = verify_producer_envelope(&id, &raw);
        assert!(r.ok, "{r:?}");
        assert_eq!(
            r.public_key_hex.as_deref(),
            Some(hex::encode(key().verifying_key().as_bytes()).as_str())
        );
        let s = CoseSign1::decode(&raw).unwrap();
        assert_eq!(
            s.protected_bytes,
            protected_header_for(&key().verifying_key()).unwrap()
        );
        // Wrong id → payload mismatch, never ok.
        let other = "cd".repeat(32);
        let r = verify_producer_envelope(&other, &raw);
        assert_eq!(r.findings[0].code, "envelope_payload_mismatch");
        // Garbage never panics.
        assert_eq!(
            verify_producer_envelope(&id, &[0xd2, 0x84]).findings[0].code,
            "envelope_malformed"
        );
        assert!(sign_producer_envelope("XYZ", &key()).is_err());
    }
}
