// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A2A v1.0 Agent Card signing — JWS (RFC 7515) over JCS-canonical bytes.
//!
//! ## Protocol
//!
//! Per the v1.0 proto + spec (see
//! <https://github.com/a2aproject/A2A/blob/v1.0.0/docs/specification.md>
//! §8.4 "Agent Card Signing"):
//!
//!   1. Strip the card's `signatures` array so we sign the document
//!      that the signature covers, not a document that mentions itself.
//!   2. Canonicalize the remaining JSON under RFC 8785 (see
//!      [`super::jcs`]).
//!   3. Compute a JWS with `alg = EdDSA` (Ed25519) — the protected
//!      header + payload format follow RFC 7515 §7.2.1 "Flattened
//!      JSON Serialization".
//!   4. Serialise the JWS as an [`super::AgentCardSignature`] and
//!      append it to the original card's `signatures[]`.
//!
//! ## On-wire shape
//!
//! The on-wire `AgentCardSignature` is what the spec calls the
//! "flattened JSON serialization" of a JWS. Each signature carries:
//!
//!   * `protected` — base64url-encoded JSON with at least `{"alg": "EdDSA"}`.
//!     We always include `"kid"` (the tenant id) so verifiers can look
//!     up the right public key without out-of-band state. The `"typ"`
//!     header is set to `"a2a-card+json"` to namespace our signatures
//!     against other JWS use.
//!   * `signature` — base64url-encoded raw Ed25519 signature (64 bytes).
//!   * `header` — optional unprotected headers (we don't emit any; the
//!     field is present for spec compliance).
//!
//! The signing input is the RFC 7515 §5.1 concatenation:
//!
//! ```text
//! base64url(protected) + "." + base64url(payload)
//! ```
//!
//! where `payload` is the JCS-canonical card bytes. Ed25519 signs that
//! exact byte string; verification recomputes it and calls
//! `verifying_key.verify_strict`.
//!
//! ## Why `verify_strict`
//!
//! `ed25519-dalek`'s `verify_strict` implements the RFC 8032
//! verification rules that protect against malleability attacks — a
//! non-strict verify accepts signatures with non-canonical `s` values,
//! which would let an attacker craft a second valid signature for a
//! message already signed. Strict verification is the right default
//! for anything trust-propagating like Agent Cards.

use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD as B64URL};
use ed25519_dalek::{Signature, Signer, SigningKey, VerifyingKey};
use serde::{Deserialize, Serialize};

use super::jcs::canonicalize;
use super::{AgentCard, AgentCardSignature};

/// Content type we stamp into every protected header so our JWS
/// payloads are unambiguously "A2A Agent Card" and can't be confused
/// with JWTs or other JWS-using protocols.
pub const TYP_HEADER: &str = "a2a-card+json";

/// Algorithm value per RFC 8037 §3.1. Emitted in the protected header
/// as `"alg": "EdDSA"`. The spec (§"Signing") mandates this exact
/// string for Ed25519-signed cards.
pub const ALG_ED25519: &str = "EdDSA";

/// The protected JWS header we emit. Structured as a typed struct so
/// any new required field shows up as a compile error instead of a
/// silent wire drift.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
struct ProtectedHeader<'a> {
    alg: &'a str,
    /// RFC 7515 §4.1.9 — MIME type of the payload, prefixed by
    /// `application/` implicitly per §4.1.10. We emit the short form.
    #[serde(skip_serializing_if = "Option::is_none")]
    typ: Option<&'a str>,
    /// RFC 7515 §4.1.4 — key hint. We use the tenant id so verifiers
    /// can resolve the public key without additional protocol round-trips.
    #[serde(skip_serializing_if = "Option::is_none")]
    kid: Option<&'a str>,
}

// =============================================================================
// Errors
// =============================================================================

#[derive(Debug)]
pub enum JwsError {
    /// Card could not be serialized prior to canonicalization — almost
    /// always a bug (a valid `AgentCard` always serializes).
    Serialize(String),
    /// Canonicalization failed — see [`super::jcs`].
    Canonicalize(String),
    /// Protected header base64url decode failed.
    BadProtectedHeader(String),
    /// Signature bytes base64url decode failed.
    BadSignatureEncoding(String),
    /// Signature has the wrong byte length (must be 64 for Ed25519).
    BadSignatureLength(usize),
    /// Verifying key has the wrong byte length (must be 32 for Ed25519).
    BadVerifyingKeyLength(usize),
    /// Algorithm header is missing or not `EdDSA`.
    UnsupportedAlgorithm(String),
    /// `typ` header, when present, must match [`TYP_HEADER`].
    UnexpectedTyp(String),
    /// Signature does not verify against the provided public key.
    SignatureMismatch,
    /// Signing key is malformed.
    BadSigningKey(String),
}

impl std::fmt::Display for JwsError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            JwsError::Serialize(s) => write!(f, "Card serialisation failed: {s}"),
            JwsError::Canonicalize(s) => write!(f, "Canonicalisation failed: {s}"),
            JwsError::BadProtectedHeader(s) => write!(f, "Bad JWS protected header: {s}"),
            JwsError::BadSignatureEncoding(s) => write!(f, "Bad JWS signature encoding: {s}"),
            JwsError::BadSignatureLength(n) => {
                write!(f, "Ed25519 signature must be 64 bytes, got {n}")
            }
            JwsError::BadVerifyingKeyLength(n) => {
                write!(f, "Ed25519 public key must be 32 bytes, got {n}")
            }
            JwsError::UnsupportedAlgorithm(alg) => {
                write!(f, "Unsupported JWS alg {alg:?}; A2A requires EdDSA")
            }
            JwsError::UnexpectedTyp(typ) => {
                write!(
                    f,
                    "Unexpected JWS typ header {typ:?}; expected {TYP_HEADER:?}"
                )
            }
            JwsError::SignatureMismatch => {
                write!(
                    f,
                    "JWS signature does not verify against the supplied public key"
                )
            }
            JwsError::BadSigningKey(s) => write!(f, "Bad signing key: {s}"),
        }
    }
}

impl std::error::Error for JwsError {}

// =============================================================================
// Signing
// =============================================================================

/// Compute the canonical signing payload for a card.
///
/// Strips any existing `signatures` array before canonicalising so the
/// signed document is exactly the card without self-reference. Returns
/// the raw JCS bytes.
pub fn canonical_payload(card: &AgentCard) -> Result<Vec<u8>, JwsError> {
    // Clone and clear signatures. Cheap — signatures is usually empty
    // at sign time. We can't canonicalize `card` directly
    // because the struct's own `skip_serializing_if = "Vec::is_empty"`
    // handles that case, but a Verify path might receive a card with
    // signatures present (the ones we're verifying), so the strip
    // step is necessary for both directions.
    let mut value = serde_json::to_value(card).map_err(|e| JwsError::Serialize(e.to_string()))?;
    if let Some(obj) = value.as_object_mut() {
        obj.remove("signatures");
    }
    canonicalize(&value).map_err(|e| JwsError::Canonicalize(e.to_string()))
}

/// Sign an Agent Card with an Ed25519 private key.
///
/// Returns an [`AgentCardSignature`] ready to be appended to the
/// card's `signatures[]`. The returned header includes `kid` so
/// verifiers can resolve the public key.
pub fn sign_card(
    card: &AgentCard,
    signing_key: &SigningKey,
    kid: &str,
) -> Result<AgentCardSignature, JwsError> {
    let payload = canonical_payload(card)?;

    let protected = ProtectedHeader {
        alg: ALG_ED25519,
        typ: Some(TYP_HEADER),
        kid: Some(kid),
    };
    let protected_json =
        canonicalize(&protected).map_err(|e| JwsError::Serialize(e.to_string()))?;
    let protected_b64 = B64URL.encode(&protected_json);
    let payload_b64 = B64URL.encode(&payload);

    // RFC 7515 §5.1 signing input: ASCII(BASE64URL(header)) || '.' || ASCII(BASE64URL(payload)).
    let mut signing_input = Vec::with_capacity(protected_b64.len() + 1 + payload_b64.len());
    signing_input.extend_from_slice(protected_b64.as_bytes());
    signing_input.push(b'.');
    signing_input.extend_from_slice(payload_b64.as_bytes());

    let signature: Signature = signing_key.sign(&signing_input);
    let signature_b64 = B64URL.encode(signature.to_bytes());

    Ok(AgentCardSignature {
        protected: protected_b64,
        signature: signature_b64,
        header: None,
    })
}

// =============================================================================
// Verification
// =============================================================================

/// Verify a single signature on a card.
///
/// `verifying_key` is the Ed25519 public key resolved by the
/// verifier (typically via the `kid` protected-header field — see
/// [`extract_kid`] for a helper). Returns `Ok(())` iff the signature
/// is valid per RFC 8032 strict verification.
pub fn verify_card(
    card: &AgentCard,
    signature: &AgentCardSignature,
    verifying_key: &VerifyingKey,
) -> Result<(), JwsError> {
    // Reject anything other than `alg = EdDSA` (and `typ`, when set,
    // matching our namespaced type). A header mismatch means the
    // signature was computed under different rules and cannot be
    // trusted even if the bytes happen to verify.
    let protected = decode_protected(&signature.protected)?;
    if protected.alg != ALG_ED25519 {
        return Err(JwsError::UnsupportedAlgorithm(protected.alg.to_string()));
    }
    if let Some(typ) = protected.typ.as_deref() {
        if typ != TYP_HEADER {
            return Err(JwsError::UnexpectedTyp(typ.to_string()));
        }
    }

    let payload = canonical_payload(card)?;
    let payload_b64 = B64URL.encode(&payload);

    let mut signing_input = Vec::with_capacity(signature.protected.len() + 1 + payload_b64.len());
    signing_input.extend_from_slice(signature.protected.as_bytes());
    signing_input.push(b'.');
    signing_input.extend_from_slice(payload_b64.as_bytes());

    let sig_bytes = B64URL
        .decode(signature.signature.as_bytes())
        .map_err(|e| JwsError::BadSignatureEncoding(e.to_string()))?;
    if sig_bytes.len() != 64 {
        return Err(JwsError::BadSignatureLength(sig_bytes.len()));
    }
    let mut sig_arr = [0u8; 64];
    sig_arr.copy_from_slice(&sig_bytes);
    let sig = Signature::from_bytes(&sig_arr);

    verifying_key
        .verify_strict(&signing_input, &sig)
        .map_err(|_| JwsError::SignatureMismatch)
}

/// Extract the `kid` value from a signature's protected header.
///
/// Returns `None` if the header has no `kid` (which is allowed by
/// RFC 7515 but in practice means the verifier has to look up keys
/// out-of-band).
pub fn extract_kid(signature: &AgentCardSignature) -> Result<Option<String>, JwsError> {
    let protected = decode_protected(&signature.protected)?;
    Ok(protected.kid.map(|s| s.to_string()))
}

/// Owned version of [`ProtectedHeader`] for decoding.
#[derive(Debug, Clone, Deserialize)]
struct ProtectedHeaderOwned {
    alg: String,
    #[serde(default)]
    typ: Option<String>,
    #[serde(default)]
    kid: Option<String>,
}

fn decode_protected(b64: &str) -> Result<ProtectedHeaderOwned, JwsError> {
    let bytes = B64URL
        .decode(b64.as_bytes())
        .map_err(|e| JwsError::BadProtectedHeader(format!("base64url: {e}")))?;
    serde_json::from_slice::<ProtectedHeaderOwned>(&bytes)
        .map_err(|e| JwsError::BadProtectedHeader(format!("json: {e}")))
}

// =============================================================================
// Tests
// =============================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::a2a_v1::*;
    use ed25519_dalek::SigningKey;
    use rand::TryRngCore;
    use rand::rngs::OsRng;
    use std::collections::BTreeMap;

    fn make_card() -> AgentCard {
        AgentCard {
            name: "billing-agent".into(),
            description: "Issues refunds".into(),
            supported_interfaces: vec![AgentInterface {
                url: "https://api.example.com/a2a/v1".into(),
                protocol_binding: "JSONRPC".into(),
                tenant: None,
                protocol_version: "1.0".into(),
            }],
            provider: None,
            version: "1.0.0".into(),
            documentation_url: None,
            capabilities: AgentCapabilities {
                streaming: Some(true),
                push_notifications: None,
                extensions: vec![AgentExtension {
                    uri: BRUTOR_DELEGATION_EXTENSION_URI.into(),
                    description: Some("Brutor HMAC delegation chain".into()),
                    required: true,
                    params: None,
                }],
                extended_agent_card: None,
            },
            security_schemes: BTreeMap::new(),
            security_requirements: vec![],
            default_input_modes: vec!["text/plain".into()],
            default_output_modes: vec!["application/json".into()],
            skills: vec![AgentSkill {
                id: "billing.refund".into(),
                name: "Refund".into(),
                description: "Issue a refund".into(),
                tags: vec!["billing".into()],
                examples: vec![],
                input_modes: vec![],
                output_modes: vec![],
                security_requirements: vec![],
            }],
            signatures: vec![],
            icon_url: None,
        }
    }

    /// Convenience — generate an Ed25519 keypair using the OS CSPRNG
    /// via the `TryRngCore` bridge. `SigningKey::generate` needs a
    /// CryptoRng + RngCore; `OsRng` implements both under
    /// `rand 0.9`'s `TryRngCore` trait, which we adapt here.
    fn keypair() -> SigningKey {
        let mut bytes = [0u8; 32];
        OsRng.try_fill_bytes(&mut bytes).expect("OsRng");
        SigningKey::from_bytes(&bytes)
    }

    /// A card with ASCII member names and every number shape the ES6
    /// formatter must reproduce (integers, fractions, exponent forms, -0).
    fn fixture_card() -> AgentCard {
        let mut card = make_card();
        let params = serde_json::json!({
            "ratio": 0.1,
            "threshold": 1.5,
            "big": 1e21,
            "tiny": 1e-7,
            "third": 333333333.3333333,
            "neg_zero": -0.0,
            "limit": 9007199254740991u64,
            "count": 42,
            "nested": {"z": 2.5e-5, "a": [1.0, 100.25, -3.75e30]}
        });
        card.capabilities.extensions[0].params = params.as_object().cloned();
        card
    }

    const FIXTURE_SEED: [u8; 32] = [7u8; 32];

    /// Sign `card` exactly as this module did before the JCS writer moved
    /// to `brutor_evidence::canon` (serde_jcs for payload and header).
    fn legacy_serde_jcs_signature(
        card: &AgentCard,
        sk: &SigningKey,
        kid: &str,
    ) -> AgentCardSignature {
        let mut value = serde_json::to_value(card).unwrap();
        value.as_object_mut().unwrap().remove("signatures");
        let payload = serde_jcs::to_vec(&value).unwrap();
        let protected = ProtectedHeader {
            alg: ALG_ED25519,
            typ: Some(TYP_HEADER),
            kid: Some(kid),
        };
        let protected_b64 = B64URL.encode(serde_jcs::to_vec(&protected).unwrap());
        let input = format!("{protected_b64}.{}", B64URL.encode(&payload));
        AgentCardSignature {
            protected: protected_b64,
            signature: B64URL.encode(sk.sign(input.as_bytes()).to_bytes()),
            header: None,
        }
    }

    #[test]
    fn print_fixture_signature() {
        let sk = SigningKey::from_bytes(&FIXTURE_SEED);
        let sig = legacy_serde_jcs_signature(&fixture_card(), &sk, "fixture-kid");
        println!(
            "FIXTURE protected={} signature={}",
            sig.protected, sig.signature
        );
    }

    #[test]
    fn sign_then_verify_round_trip() {
        let card = make_card();
        let sk = keypair();
        let vk = sk.verifying_key();

        let sig = sign_card(&card, &sk, "tenant-1").expect("sign");
        verify_card(&card, &sig, &vk).expect("verify");
    }

    #[test]
    fn signature_contains_alg_typ_kid() {
        let card = make_card();
        let sk = keypair();
        let sig = sign_card(&card, &sk, "tenant-xyz").expect("sign");

        // Protected header decodes back to the values we set.
        let prot = decode_protected(&sig.protected).expect("decode");
        assert_eq!(prot.alg, "EdDSA");
        assert_eq!(prot.typ.as_deref(), Some(TYP_HEADER));
        assert_eq!(prot.kid.as_deref(), Some("tenant-xyz"));

        // Signature bytes are 64 raw bytes (Ed25519 signature length).
        let decoded = B64URL.decode(sig.signature.as_bytes()).unwrap();
        assert_eq!(decoded.len(), 64);
    }

    #[test]
    fn tampered_body_fails_verify() {
        let mut card = make_card();
        let sk = keypair();
        let vk = sk.verifying_key();
        let sig = sign_card(&card, &sk, "tenant-1").expect("sign");

        // Change a byte in the card body.
        card.description = "Refunds (evil edit)".into();
        let err = verify_card(&card, &sig, &vk).expect_err("should not verify");
        assert!(matches!(err, JwsError::SignatureMismatch), "got {err:?}");
    }

    #[test]
    fn tampered_signature_bytes_fail_verify() {
        let card = make_card();
        let sk = keypair();
        let vk = sk.verifying_key();
        let mut sig = sign_card(&card, &sk, "tenant-1").expect("sign");

        // Flip the last byte before the base64url padding ends.
        let mut bytes = B64URL.decode(sig.signature.as_bytes()).unwrap();
        let last = bytes.len() - 1;
        bytes[last] ^= 0x01;
        sig.signature = B64URL.encode(bytes);

        let err = verify_card(&card, &sig, &vk).expect_err("tampered sig");
        assert!(matches!(err, JwsError::SignatureMismatch), "got {err:?}");
    }

    #[test]
    fn wrong_public_key_fails_verify() {
        let card = make_card();
        let sk_signer = keypair();
        let sk_other = keypair();

        let sig = sign_card(&card, &sk_signer, "tenant-1").expect("sign");
        let err = verify_card(&card, &sig, &sk_other.verifying_key())
            .expect_err("other key must not verify");
        assert!(matches!(err, JwsError::SignatureMismatch), "got {err:?}");
    }

    #[test]
    fn existing_signatures_are_stripped_before_signing() {
        // Sign once. Then append another signature (simulating a
        // co-signed card). The second signature's protected header
        // payload is still computed over the ORIGINAL card body
        // (without either signature), so both verify independently.
        let mut card = make_card();
        let sk_a = keypair();
        let sk_b = keypair();

        let sig_a = sign_card(&card, &sk_a, "tenant-a").expect("sign a");
        card.signatures.push(sig_a.clone());

        let sig_b = sign_card(&card, &sk_b, "tenant-b").expect("sign b");

        // Both verify — proves the strip step worked on both signs
        // and on both verifies.
        verify_card(&card, &sig_a, &sk_a.verifying_key()).expect("a verifies");
        card.signatures.push(sig_b.clone());
        verify_card(&card, &sig_a, &sk_a.verifying_key()).expect("a still verifies with 2 sigs");
        verify_card(&card, &sig_b, &sk_b.verifying_key()).expect("b verifies");
    }

    #[test]
    fn reject_non_eddsa_alg() {
        let card = make_card();
        let sk = keypair();
        let vk = sk.verifying_key();
        let mut sig = sign_card(&card, &sk, "tenant-1").expect("sign");

        // Re-encode protected header with a bogus alg.
        let bad = serde_json::json!({"alg": "HS256", "typ": TYP_HEADER, "kid": "tenant-1"});
        sig.protected = B64URL.encode(serde_json::to_vec(&bad).unwrap());

        let err = verify_card(&card, &sig, &vk).expect_err("bad alg");
        assert!(matches!(err, JwsError::UnsupportedAlgorithm(_)));
    }

    #[test]
    fn reject_wrong_typ() {
        let card = make_card();
        let sk = keypair();
        let vk = sk.verifying_key();
        let mut sig = sign_card(&card, &sk, "tenant-1").expect("sign");

        let bad = serde_json::json!({"alg": "EdDSA", "typ": "jwt", "kid": "tenant-1"});
        sig.protected = B64URL.encode(serde_json::to_vec(&bad).unwrap());

        let err = verify_card(&card, &sig, &vk).expect_err("bad typ");
        assert!(matches!(err, JwsError::UnexpectedTyp(_)));
    }

    #[test]
    fn extract_kid_returns_header_value() {
        let card = make_card();
        let sk = keypair();
        let sig = sign_card(&card, &sk, "acme-corp").expect("sign");
        assert_eq!(extract_kid(&sig).unwrap().as_deref(), Some("acme-corp"));
    }

    #[test]
    fn bad_protected_header_b64_fails_cleanly() {
        let card = make_card();
        let sk = keypair();
        let vk = sk.verifying_key();
        let mut sig = sign_card(&card, &sk, "tenant-1").expect("sign");
        sig.protected = "!!!not base64url!!!".into();

        let err = verify_card(&card, &sig, &vk).expect_err("bad header");
        assert!(matches!(err, JwsError::BadProtectedHeader(_)));
    }

    #[test]
    fn bad_signature_length_fails_cleanly() {
        let card = make_card();
        let sk = keypair();
        let vk = sk.verifying_key();
        let mut sig = sign_card(&card, &sk, "tenant-1").expect("sign");
        // Truncate signature to an invalid length.
        let mut bytes = B64URL.decode(sig.signature.as_bytes()).unwrap();
        bytes.truncate(32);
        sig.signature = B64URL.encode(bytes);

        let err = verify_card(&card, &sig, &vk).expect_err("short sig");
        assert!(
            matches!(err, JwsError::BadSignatureLength(32)),
            "got {err:?}"
        );
    }

    #[test]
    fn canonical_payload_is_deterministic_across_runs() {
        // Same card → same canonical bytes → same signature if we use
        // the same key. Ed25519 is deterministic (RFC 8032), so this
        // is the ultimate regression test for our full signing path.
        let card = make_card();
        // Seed the signing key manually so signatures are bit-exact.
        let sk = SigningKey::from_bytes(&[7u8; 32]);

        let sig_1 = sign_card(&card, &sk, "tenant-1").expect("sign 1");
        let sig_2 = sign_card(&card, &sk, "tenant-1").expect("sign 2");
        assert_eq!(sig_1.signature, sig_2.signature);
        assert_eq!(sig_1.protected, sig_2.protected);
    }
}
