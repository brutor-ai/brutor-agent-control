// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A2A v1.0 tenant key distribution — RFC 7517 JWK Set.
//!
//! External verifiers need to resolve the `kid` in an Agent Card's
//! [`super::AgentCardSignature::protected`] header to the underlying
//! Ed25519 public key. Standard way to publish that mapping is
//! a JWK Set at a well-known URL (RFC 7517 §5):
//!
//! ```json
//! {
//!   "keys": [
//!     {
//!       "kty": "OKP",
//!       "crv": "Ed25519",
//!       "x":   "<base64url(public key bytes)>",
//!       "kid": "<tenant id>",
//!       "alg": "EdDSA",
//!       "use": "sig"
//!     }
//!   ]
//! }
//! ```
//!
//! Field rules (RFC 8037 §2):
//!   * `kty = OKP` — Octet Key Pair (the class Ed25519 belongs to).
//!   * `crv = Ed25519` — curve name per RFC 8037 §3.1.
//!   * `x` — the 32-byte raw public key, base64url-encoded without
//!     padding.
//!   * `kid` — key identifier. We use the tenant id so `sign_card`'s
//!     `kid` field (also the tenant id) resolves cleanly.
//!   * `alg = EdDSA` — matches [`super::jws::ALG_ED25519`].
//!   * `use = sig` — narrows the key's declared purpose to signing.
//!
//! This module is transport-agnostic: the route handler in
//! `routes::a2a` pulls the public-key row from `tenant_a2a_keys`,
//! constructs a [`JwkSet`] via [`public_key_to_jwk_set`], and
//! serializes.

use serde::{Deserialize, Serialize};

/// JWK `kty` value for Edwards-curve Octet Key Pairs per RFC 8037.
pub const JWK_KTY_OKP: &str = "OKP";
/// JWK `crv` value for Ed25519 per RFC 8037 §3.1.
pub const JWK_CRV_ED25519: &str = "Ed25519";
/// JWK `alg` value — matches [`super::jws::ALG_ED25519`].
pub const JWK_ALG_ED25519: &str = "EdDSA";
/// JWK `use` value indicating signing-only keys.
pub const JWK_USE_SIG: &str = "sig";

/// A single JWK. Fields tracked in declaration order to match the
/// RFC's recommended parameter ordering; serde respects it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Jwk {
    /// `kty` — key type.
    pub kty: String,
    /// `crv` — elliptic curve.
    pub crv: String,
    /// `x` — base64url-encoded public key bytes.
    pub x: String,
    /// `kid` — key identifier. Our convention: tenant id.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub kid: Option<String>,
    /// `alg` — algorithm.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub alg: Option<String>,
    /// `use` — key usage.
    #[serde(rename = "use", skip_serializing_if = "Option::is_none", default)]
    pub use_: Option<String>,
}

/// A JWK Set per RFC 7517 §5.
///
/// We only ever emit a one-key set today (each tenant has a single
/// signing keypair), but the spec mandates the `keys[]` wrapper and
/// third-party verifiers expect it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct JwkSet {
    pub keys: Vec<Jwk>,
}

/// Build a signing-only Ed25519 JWK from a base64url-encoded public
/// key and a key id. This is the main entry point for the route
/// handler.
pub fn ed25519_signing_jwk(public_key_b64url: &str, kid: &str) -> Jwk {
    Jwk {
        kty: JWK_KTY_OKP.to_string(),
        crv: JWK_CRV_ED25519.to_string(),
        x: public_key_b64url.to_string(),
        kid: Some(kid.to_string()),
        alg: Some(JWK_ALG_ED25519.to_string()),
        use_: Some(JWK_USE_SIG.to_string()),
    }
}

/// Build a single-key JWK Set for a tenant. The tenant id is used as
/// both `kid` and the set's only member.
pub fn public_key_to_jwk_set(public_key_b64url: &str, tenant_id: &str) -> JwkSet {
    JwkSet {
        keys: vec![ed25519_signing_jwk(public_key_b64url, tenant_id)],
    }
}

// =============================================================================
// Tests
// =============================================================================

#[cfg(test)]
mod tests {
    use super::*;

    /// Example public key used throughout — 32 bytes of 0xAB, base64url.
    /// Not a real key; it's just a value that round-trips cleanly.
    const EXAMPLE_PUBKEY_B64URL: &str = "q6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6s";

    #[test]
    fn jwk_emits_required_fields() {
        let jwk = ed25519_signing_jwk(EXAMPLE_PUBKEY_B64URL, "tenant-1");
        let s = serde_json::to_value(&jwk).unwrap();
        assert_eq!(s.get("kty").unwrap(), "OKP");
        assert_eq!(s.get("crv").unwrap(), "Ed25519");
        assert_eq!(s.get("x").unwrap(), EXAMPLE_PUBKEY_B64URL);
        assert_eq!(s.get("kid").unwrap(), "tenant-1");
        assert_eq!(s.get("alg").unwrap(), "EdDSA");
        assert_eq!(s.get("use").unwrap(), "sig");
    }

    #[test]
    fn jwk_set_wraps_keys_in_array() {
        let set = public_key_to_jwk_set(EXAMPLE_PUBKEY_B64URL, "tenant-1");
        let s = serde_json::to_value(&set).unwrap();
        assert!(s.get("keys").unwrap().is_array());
        assert_eq!(s.get("keys").unwrap().as_array().unwrap().len(), 1);
    }

    #[test]
    fn jwk_set_round_trips_through_serde() {
        let set = public_key_to_jwk_set(EXAMPLE_PUBKEY_B64URL, "tenant-1");
        let s = serde_json::to_string(&set).unwrap();
        let back: JwkSet = serde_json::from_str(&s).unwrap();
        assert_eq!(back, set);
    }

    #[test]
    fn jwk_use_field_rendered_as_use_not_use_underscore() {
        // Serde's `#[serde(rename = "use")]` must emit the reserved
        // keyword back to its spec form, not our Rust field alias.
        let set = public_key_to_jwk_set(EXAMPLE_PUBKEY_B64URL, "tenant-1");
        let s = serde_json::to_string(&set).unwrap();
        assert!(
            s.contains("\"use\":\"sig\""),
            "use field must be on-wire-named: {s}"
        );
        assert!(!s.contains("use_"));
    }

    #[test]
    fn optional_fields_are_omitted_when_absent() {
        let jwk = Jwk {
            kty: "OKP".into(),
            crv: "Ed25519".into(),
            x: "abc".into(),
            kid: None,
            alg: None,
            use_: None,
        };
        let s = serde_json::to_string(&jwk).unwrap();
        assert!(!s.contains("\"kid\""));
        assert!(!s.contains("\"alg\""));
        assert!(!s.contains("\"use\""));
    }
}
