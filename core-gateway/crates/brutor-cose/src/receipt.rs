// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! COSE Receipts (RFC 9942) for the `RFC9162_SHA256` verifiable data
//! structure (`vds = 1`).
//!
//! Wire shape:
//!
//! ```text
//! protected   = { 1: alg, 395: 1, 15: {6: iat}? , <vendor labels>? }
//! unprotected = { 396: { -1: [ bstr( cbor([tree_size, leaf_index, [path…]]) ) ] } }
//! payload     = nil (detached) — the signed payload is the 32-byte tree root
//! ```
//!
//! `vds` is read from the **protected** header only; an unprotected
//! `vds` is ignored, which defeats downgrade. Labels this crate does
//! not know are surfaced as `vendor_ext` and never trusted. The
//! inclusion path is decoded, its length checked against the tree
//! shape, and the root recomputed **before** the signature is checked,
//! so a tampered path fails as `TAMPERED_INCLUSION_PATH`, not as a
//! generic signature error.

use crate::CoseError;
use crate::cbor::{self, Value, as_bytes, as_i64, as_u64, bytes, int};
use crate::merkle::{self, Hash};
use crate::sign1::{self, CoseSign1, alg, cwt, label};
use ed25519_dalek::{SigningKey, VerifyingKey};

pub const VDS_RFC9162_SHA256: i64 = 1;
pub const VDS_CCF_LEDGER_SHA256: i64 = 2;
pub const VDP_INCLUSION_PROOFS: i64 = -1;
pub const MAX_AUDIT_PATH: usize = 64;
pub const MAX_INCLUSION_PROOFS: usize = 16;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InclusionProof {
    pub tree_size: u64,
    pub leaf_index: u64,
    pub path: Vec<Hash>,
}

pub fn encode_inclusion_proof(p: &InclusionProof) -> Result<Vec<u8>, CoseError> {
    cbor::encode(&Value::Array(vec![
        int(p.tree_size as i64),
        int(p.leaf_index as i64),
        Value::Array(p.path.iter().map(|h| bytes(h)).collect()),
    ]))
}

pub fn decode_inclusion_proof(raw: &[u8]) -> Result<InclusionProof, CoseError> {
    let v = cbor::decode(raw)?;
    let arr = match v {
        Value::Array(a) if a.len() == 3 => a,
        _ => {
            return Err(CoseError::Receipt(
                "inclusion proof is not a 3-array".into(),
            ));
        }
    };
    if matches!(arr[0], Value::Bool(_)) || matches!(arr[1], Value::Bool(_)) {
        return Err(CoseError::Receipt("bool where integer expected".into()));
    }
    let tree_size = as_u64(&arr[0]).ok_or_else(|| CoseError::Receipt("tree_size".into()))?;
    let leaf_index = as_u64(&arr[1]).ok_or_else(|| CoseError::Receipt("leaf_index".into()))?;
    let path_v = match &arr[2] {
        Value::Array(a) => a,
        _ => return Err(CoseError::Receipt("path is not an array".into())),
    };
    if path_v.len() > MAX_AUDIT_PATH {
        return Err(CoseError::Receipt("path too long".into()));
    }
    let mut path = Vec::with_capacity(path_v.len());
    for n in path_v {
        let b = as_bytes(n).ok_or_else(|| CoseError::Receipt("path node not bstr".into()))?;
        let h: Hash = b
            .try_into()
            .map_err(|_| CoseError::Receipt("path node not 32 bytes".into()))?;
        path.push(h);
    }
    Ok(InclusionProof {
        tree_size,
        leaf_index,
        path,
    })
}

/// Build a receipt over `root` for `proof`, signed with Ed25519. This is
/// what a transparency service does; we use it for `brutor-witness`
/// parity tests and for self-issued receipts in the trial bundle.
pub fn build_receipt(
    proof: &InclusionProof,
    root: &Hash,
    key: &SigningKey,
    iat: Option<i64>,
) -> Result<Vec<u8>, CoseError> {
    let mut protected = vec![(int(label::VDS), int(VDS_RFC9162_SHA256))];
    if let Some(t) = iat {
        protected.push((
            int(label::CWT_CLAIMS),
            cbor::canonical_map(vec![(int(cwt::IAT), int(t))])?,
        ));
    }
    let unprotected = vec![(
        int(label::VDP),
        cbor::canonical_map(vec![(
            int(VDP_INCLUSION_PROOFS),
            Value::Array(vec![bytes(&encode_inclusion_proof(proof)?)]),
        )])?,
    )];
    let mut s = sign1::sign_ed25519(protected, unprotected, Some(root), key)?;
    s.payload = None; // detached
    s.encode()
}

#[derive(Debug, Clone, Default)]
pub struct ReceiptResult {
    pub ok: bool,
    pub root: Option<Hash>,
    pub tree_size: Option<u64>,
    pub leaf_index: Option<u64>,
    pub iat: Option<i64>,
    /// Protected labels outside {1, 2, 15, 395}: vendor-private, surfaced, untrusted.
    pub vendor_ext: Vec<(i64, Value)>,
    pub errors: Vec<String>,
}

/// Verify a receipt for `leaf` (already hashed per RFC 6962) under a key
/// the caller already trusts. Never panics; returns a structured result.
pub fn verify_receipt(raw: &[u8], leaf: &Hash, key: &VerifyingKey) -> ReceiptResult {
    let mut r = ReceiptResult::default();
    let s = match CoseSign1::decode(raw) {
        Ok(s) => s,
        Err(e) => {
            r.errors.push(format!("NOT_COSE_SIGN1: {e}"));
            return r;
        }
    };
    let Some(a) = s.alg() else {
        r.errors.push("MISSING_ALG".into());
        return r;
    };
    if a != alg::EDDSA {
        r.errors.push(format!("UNSUPPORTED_ALG:{a}"));
        return r;
    }
    if let Some(Value::Integer(i)) = s.cwt_claim(cwt::IAT) {
        r.iat = i64::try_from(*i).ok();
    }
    if let Value::Map(entries) = &s.protected {
        for (k, v) in entries {
            if let Some(l) = as_i64(k)
                && ![label::ALG, label::CRIT, label::CWT_CLAIMS, label::VDS].contains(&l)
            {
                r.vendor_ext.push((l, v.clone()));
            }
        }
    }
    // vds from the PROTECTED header only.
    match s.protected_get(label::VDS).and_then(as_i64) {
        Some(VDS_RFC9162_SHA256) => {}
        Some(_) => {
            r.errors.push("UNSUPPORTED_VDS".into());
            return r;
        }
        None => {
            r.errors.push("MISSING_VDS".into());
            return r;
        }
    }
    let proofs = match s
        .unprotected_get(label::VDP)
        .and_then(|m| cbor::map_get(m, VDP_INCLUSION_PROOFS))
    {
        Some(Value::Array(items)) if !items.is_empty() && items.len() <= MAX_INCLUSION_PROOFS => {
            items
        }
        _ => {
            r.errors.push("MISSING_INCLUSION_PROOF".into());
            return r;
        }
    };
    let Some(first) = as_bytes(&proofs[0]) else {
        r.errors.push("INCLUSION_PROOF_NOT_BSTR".into());
        return r;
    };
    let proof = match decode_inclusion_proof(first) {
        Ok(p) => p,
        Err(e) => {
            r.errors.push(format!("MALFORMED_INCLUSION_PROOF: {e}"));
            return r;
        }
    };
    r.tree_size = Some(proof.tree_size);
    r.leaf_index = Some(proof.leaf_index);
    let root =
        match merkle::root_from_inclusion(leaf, proof.leaf_index, proof.tree_size, &proof.path) {
            Ok(h) => h,
            Err(e) => {
                r.errors.push(format!("TAMPERED_INCLUSION_PATH: {e}"));
                return r;
            }
        };
    r.root = Some(root);
    match s.verify_ed25519(key, Some(&root)) {
        Ok(()) => r.ok = true,
        Err(_) => r.errors.push("BAD_RECEIPT_SIGNATURE".into()),
    }
    r
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::merkle::{inclusion_path, leaf_hash, tree_hash};

    #[test]
    fn build_and_verify_receipt() {
        let key = SigningKey::from_bytes(&[3u8; 32]);
        let leaves: Vec<Hash> = (0..8u8).map(|i| leaf_hash(&[i])).collect();
        let root = tree_hash(&leaves);
        let proof = InclusionProof {
            tree_size: 8,
            leaf_index: 2,
            path: inclusion_path(2, &leaves),
        };
        let raw = build_receipt(&proof, &root, &key, Some(1_760_000_000)).unwrap();
        let res = verify_receipt(&raw, &leaves[2], &key.verifying_key());
        assert!(res.ok, "{:?}", res.errors);
        assert_eq!(res.root, Some(root));
        assert_eq!(res.iat, Some(1_760_000_000));
        assert!(res.vendor_ext.is_empty());

        // wrong leaf ⇒ root mismatch surfaces as a signature failure over a different root
        let bad = verify_receipt(&raw, &leaves[3], &key.verifying_key());
        assert!(!bad.ok);
        // wrong key
        let other = SigningKey::from_bytes(&[4u8; 32]);
        let bad = verify_receipt(&raw, &leaves[2], &other.verifying_key());
        assert_eq!(bad.errors, vec!["BAD_RECEIPT_SIGNATURE".to_string()]);
    }

    #[test]
    fn tampered_path_is_named() {
        let key = SigningKey::from_bytes(&[3u8; 32]);
        let leaves: Vec<Hash> = (0..5u8).map(|i| leaf_hash(&[i])).collect();
        let mut proof = InclusionProof {
            tree_size: 5,
            leaf_index: 4,
            path: inclusion_path(4, &leaves),
        };
        proof.path.pop();
        let raw = build_receipt(&proof, &tree_hash(&leaves), &key, None).unwrap();
        let res = verify_receipt(&raw, &leaves[4], &key.verifying_key());
        assert!(!res.ok);
        assert!(res.errors[0].starts_with("TAMPERED_INCLUSION_PATH"));
    }

    #[test]
    fn vds_read_from_protected_only() {
        let key = SigningKey::from_bytes(&[3u8; 32]);
        let leaves: Vec<Hash> = (0..2u8).map(|i| leaf_hash(&[i])).collect();
        let proof = InclusionProof {
            tree_size: 2,
            leaf_index: 0,
            path: inclusion_path(0, &leaves),
        };
        // Sign a receipt with vds=2 in protected and a "helpful" vds=1 in unprotected.
        let protected = vec![(int(label::VDS), int(VDS_CCF_LEDGER_SHA256))];
        let unprotected = vec![
            (int(label::VDS), int(VDS_RFC9162_SHA256)),
            (
                int(label::VDP),
                cbor::canonical_map(vec![(
                    int(VDP_INCLUSION_PROOFS),
                    Value::Array(vec![bytes(&encode_inclusion_proof(&proof).unwrap())]),
                )])
                .unwrap(),
            ),
        ];
        let mut s =
            sign1::sign_ed25519(protected, unprotected, Some(&tree_hash(&leaves)), &key).unwrap();
        s.payload = None;
        let res = verify_receipt(&s.encode().unwrap(), &leaves[0], &key.verifying_key());
        assert_eq!(res.errors, vec!["UNSUPPORTED_VDS".to_string()]);
    }
}
