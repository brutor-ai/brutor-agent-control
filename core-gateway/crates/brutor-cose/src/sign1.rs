// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! COSE_Sign1 (RFC 9052 §4.2) with the header labels this platform uses.
//!
//! The protected header is carried as the **exact bytes** received: a
//! verifier must never re-encode it before checking the signature,
//! because the signature covers those bytes and a re-encode may reorder
//! keys. We decode a copy for inspection and keep the original.

use crate::CoseError;
use crate::cbor::{self, Value, as_bytes, as_i64, bytes, int, map_get};
use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};

pub const TAG_SIGN1: u64 = 18;

/// COSE header labels (IANA "COSE Header Parameters").
pub mod label {
    pub const ALG: i64 = 1;
    pub const CRIT: i64 = 2;
    pub const CONTENT_TYPE: i64 = 3;
    pub const KID: i64 = 4;
    /// RFC 9597 — CWT claims in a COSE header. **15, never 13** (13 is
    /// `kcwt`, a different thing; getting this wrong is the founding
    /// bug class the scitt-cose reference guards against).
    pub const CWT_CLAIMS: i64 = 15;
    /// RFC 9995 COSE Hash Envelope.
    pub const PAYLOAD_HASH_ALG: i64 = 258;
    pub const PREIMAGE_CONTENT_TYPE: i64 = 259;
    pub const PAYLOAD_LOCATION: i64 = 260;
    /// RFC 9943 — receipts attached to a Transparent Statement.
    pub const RECEIPTS: i64 = 394;
    /// RFC 9942 — verifiable data structure (protected).
    pub const VDS: i64 = 395;
    /// RFC 9942 — verifiable data proofs (unprotected).
    pub const VDP: i64 = 396;
}

/// COSE algorithm identifiers (IANA "COSE Algorithms").
pub mod alg {
    pub const EDDSA: i64 = -8;
    pub const ES256: i64 = -7;
    pub const ES384: i64 = -35;
    pub const PS384: i64 = -38;
    /// SHA-256 as a hash algorithm (used for RFC 9995 label 258).
    pub const SHA256: i64 = -16;
}

/// CWT claim keys (IANA "CWT Claims").
pub mod cwt {
    pub const ISS: i64 = 1;
    pub const SUB: i64 = 2;
    pub const IAT: i64 = 6;
}

/// A decoded COSE_Sign1. `protected_bytes` are the original bytes.
#[derive(Debug, Clone)]
pub struct CoseSign1 {
    pub protected_bytes: Vec<u8>,
    pub protected: Value,
    pub unprotected: Value,
    /// `None` when the payload is detached (`nil` on the wire).
    pub payload: Option<Vec<u8>>,
    pub signature: Vec<u8>,
}

/// RFC 9052 §4.4 `Sig_structure` for a single signer: `["Signature1",
/// protected, external_aad, payload]` with an empty external AAD.
pub fn sig_structure(protected_bytes: &[u8], payload: &[u8]) -> Result<Vec<u8>, CoseError> {
    cbor::encode(&Value::Array(vec![
        cbor::text("Signature1"),
        bytes(protected_bytes),
        bytes(&[]),
        bytes(payload),
    ]))
}

/// Sign with Ed25519 (alg −8). `protected` must already be a canonical
/// map (use [`cbor::canonical_map`]) and must carry label 1 = −8; this
/// function inserts it when absent so callers cannot mis-declare.
pub fn sign_ed25519(
    protected: Vec<(Value, Value)>,
    unprotected: Vec<(Value, Value)>,
    payload: Option<&[u8]>,
    key: &SigningKey,
) -> Result<CoseSign1, CoseError> {
    let mut entries: Vec<(Value, Value)> = protected
        .into_iter()
        .filter(|(k, _)| as_i64(k) != Some(label::ALG))
        .collect();
    entries.push((int(label::ALG), int(alg::EDDSA)));
    let protected_map = cbor::canonical_map(entries)?;
    let protected_bytes = cbor::encode(&protected_map)?;
    let sig = key.sign(&sig_structure(&protected_bytes, payload.unwrap_or(&[]))?);
    Ok(CoseSign1 {
        protected_bytes,
        protected: protected_map,
        unprotected: cbor::canonical_map(unprotected)?,
        payload: payload.map(|p| p.to_vec()),
        signature: sig.to_bytes().to_vec(),
    })
}

impl CoseSign1 {
    pub fn encode(&self) -> Result<Vec<u8>, CoseError> {
        let payload = match &self.payload {
            Some(p) => bytes(p),
            None => Value::Null,
        };
        let empty_protected = matches!(&self.protected, Value::Map(m) if m.is_empty());
        let protected = if empty_protected && self.protected_bytes.is_empty() {
            // RFC 9052 §3: an empty protected map encodes as a zero-length bstr.
            bytes(&[])
        } else {
            bytes(&self.protected_bytes)
        };
        cbor::encode(&Value::Tag(
            TAG_SIGN1,
            Box::new(Value::Array(vec![
                protected,
                self.unprotected.clone(),
                payload,
                bytes(&self.signature),
            ])),
        ))
    }

    pub fn decode(raw: &[u8]) -> Result<Self, CoseError> {
        let v = cbor::decode(raw)?;
        let arr = match v {
            Value::Tag(TAG_SIGN1, inner) => match *inner {
                Value::Array(a) => a,
                _ => return Err(CoseError::NotSign1),
            },
            _ => return Err(CoseError::NotSign1),
        };
        if arr.len() != 4 {
            return Err(CoseError::NotSign1);
        }
        let protected_bytes = as_bytes(&arr[0]).ok_or(CoseError::NotSign1)?.to_vec();
        let protected = if protected_bytes.is_empty() {
            Value::Map(vec![])
        } else {
            let p = cbor::decode(&protected_bytes)?;
            if !matches!(p, Value::Map(_)) {
                return Err(CoseError::ProtectedNotMap);
            }
            cbor::check_unique_keys(&p, "protected")?;
            p
        };
        let unprotected = arr[1].clone();
        if !matches!(unprotected, Value::Map(_)) {
            return Err(CoseError::NotSign1);
        }
        cbor::check_unique_keys(&unprotected, "unprotected")?;
        let payload = match &arr[2] {
            Value::Null => None,
            Value::Bytes(b) => Some(b.clone()),
            _ => return Err(CoseError::NotSign1),
        };
        let signature = as_bytes(&arr[3]).ok_or(CoseError::NotSign1)?.to_vec();
        Ok(Self {
            protected_bytes,
            protected,
            unprotected,
            payload,
            signature,
        })
    }

    pub fn protected_get(&self, l: i64) -> Option<&Value> {
        map_get(&self.protected, l)
    }

    pub fn unprotected_get(&self, l: i64) -> Option<&Value> {
        map_get(&self.unprotected, l)
    }

    pub fn alg(&self) -> Option<i64> {
        self.protected_get(label::ALG).and_then(as_i64)
    }

    /// Raw `kid` bytes.
    pub fn kid(&self) -> Option<&[u8]> {
        self.protected_get(label::KID).and_then(as_bytes)
    }

    pub fn cwt_claim(&self, claim: i64) -> Option<&Value> {
        self.protected_get(label::CWT_CLAIMS)
            .and_then(|m| map_get(m, claim))
    }

    pub fn content_type(&self) -> Option<&str> {
        self.protected_get(label::CONTENT_TYPE)
            .and_then(cbor::as_text)
    }

    /// Verify an Ed25519 signature. For a detached payload the caller
    /// supplies the bytes; for an attached one `detached` is ignored.
    pub fn verify_ed25519(
        &self,
        key: &VerifyingKey,
        detached: Option<&[u8]>,
    ) -> Result<(), CoseError> {
        let a = self.alg().ok_or(CoseError::MissingAlg)?;
        if a != alg::EDDSA {
            return Err(CoseError::UnsupportedAlg(a));
        }
        let payload: &[u8] = match (&self.payload, detached) {
            (Some(p), _) => p.as_slice(),
            (None, Some(d)) => d,
            (None, None) => return Err(CoseError::DetachedPayload),
        };
        let sig = Signature::from_slice(&self.signature).map_err(|_| CoseError::BadSignature)?;
        key.verify(&sig_structure(&self.protected_bytes, payload)?, &sig)
            .map_err(|_| CoseError::BadSignature)
    }

    /// The Ed25519 key carried in `kid`, when it is a raw 32-byte key.
    pub fn kid_as_ed25519(&self) -> Result<VerifyingKey, CoseError> {
        let k = self.kid().ok_or(CoseError::BadKid)?;
        let arr: [u8; 32] = k.try_into().map_err(|_| CoseError::BadKid)?;
        VerifyingKey::from_bytes(&arr).map_err(|_| CoseError::BadKid)
    }

    /// Attach receipts (RFC 9943 Transparent Statement, unprotected 394)
    /// without touching the protected bytes or the signature.
    pub fn attach_receipts(&mut self, receipts: &[Vec<u8>]) {
        let mut entries = match std::mem::replace(&mut self.unprotected, Value::Map(vec![])) {
            Value::Map(e) => e,
            _ => vec![],
        };
        let mut existing: Vec<Value> = entries
            .iter()
            .position(|(k, _)| as_i64(k) == Some(label::RECEIPTS))
            .map(|i| match entries.remove(i).1 {
                Value::Array(a) => a,
                _ => vec![],
            })
            .unwrap_or_default();
        existing.extend(receipts.iter().map(|r| bytes(r)));
        entries.push((int(label::RECEIPTS), Value::Array(existing)));
        self.unprotected = Value::Map(entries);
    }

    pub fn receipts(&self) -> Vec<Vec<u8>> {
        match self.unprotected_get(label::RECEIPTS) {
            Some(Value::Array(items)) => items
                .iter()
                .filter_map(|i| as_bytes(i).map(|b| b.to_vec()))
                .collect(),
            _ => vec![],
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::SigningKey;

    fn key() -> SigningKey {
        SigningKey::from_bytes(&[7u8; 32])
    }

    #[test]
    fn round_trip_attached_and_detached() {
        let k = key();
        let protected = vec![
            (int(label::CONTENT_TYPE), cbor::text("text/plain")),
            (int(label::KID), bytes(k.verifying_key().as_bytes())),
        ];
        let s = sign_ed25519(protected, vec![], Some(b"hello"), &k).unwrap();
        let raw = s.encode().unwrap();
        assert_eq!(raw[0], 0xd2); // tag 18
        let d = CoseSign1::decode(&raw).unwrap();
        assert_eq!(d.alg(), Some(alg::EDDSA));
        assert_eq!(d.content_type(), Some("text/plain"));
        d.verify_ed25519(&k.verifying_key(), None).unwrap();
        assert_eq!(d.kid_as_ed25519().unwrap(), k.verifying_key());

        let det = sign_ed25519(vec![], vec![], None, &k).unwrap();
        // Detached: sign over an empty payload slot means "no payload bytes";
        // real callers pass the bytes at both sign and verify time.
        assert!(det.verify_ed25519(&k.verifying_key(), None).is_err());
    }

    #[test]
    fn detached_payload_signs_the_real_bytes() {
        let k = key();
        let root = [9u8; 32];
        let mut s = sign_ed25519(vec![], vec![], Some(&root), &k).unwrap();
        s.payload = None; // detach for the wire
        let raw = s.encode().unwrap();
        let d = CoseSign1::decode(&raw).unwrap();
        assert!(d.payload.is_none());
        d.verify_ed25519(&k.verifying_key(), Some(&root)).unwrap();
        assert!(
            d.verify_ed25519(&k.verifying_key(), Some(&[8u8; 32]))
                .is_err()
        );
    }

    #[test]
    fn tamper_breaks_signature() {
        let k = key();
        let s = sign_ed25519(vec![], vec![], Some(b"x"), &k).unwrap();
        let mut raw = s.encode().unwrap();
        let n = raw.len();
        raw[n - 1] ^= 0x01;
        let d = CoseSign1::decode(&raw).unwrap();
        assert!(matches!(
            d.verify_ed25519(&k.verifying_key(), None),
            Err(CoseError::BadSignature)
        ));
    }

    #[test]
    fn receipts_attach_without_touching_protected() {
        let k = key();
        let s = sign_ed25519(
            vec![(int(label::KID), bytes(&[1u8; 32]))],
            vec![],
            Some(b"p"),
            &k,
        )
        .unwrap();
        let before = s.protected_bytes.clone();
        let mut t = s.clone();
        t.attach_receipts(&[vec![1, 2, 3]]);
        t.attach_receipts(&[vec![4]]);
        assert_eq!(t.protected_bytes, before);
        assert_eq!(t.receipts(), vec![vec![1, 2, 3], vec![4]]);
        let d = CoseSign1::decode(&t.encode().unwrap()).unwrap();
        d.verify_ed25519(&k.verifying_key(), None).unwrap();
        assert_eq!(d.receipts().len(), 2);
    }

    #[test]
    fn rejects_untagged_and_short_arrays() {
        assert!(matches!(
            CoseSign1::decode(&[0x84, 0x40, 0xa0, 0x40, 0x40]),
            Err(CoseError::NotSign1)
        ));
        assert!(CoseSign1::decode(&[0xd2, 0x83, 0x40, 0xa0, 0x40]).is_err());
    }
}
