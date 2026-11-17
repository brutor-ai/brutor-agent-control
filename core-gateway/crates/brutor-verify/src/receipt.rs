// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Receipt grading — pure, offline, shared by the gateway
//! (`services::evidence::witness` re-exports it), `brutor-verify` and the
//! wasm page, so a relying party grades a receipt exactly as the gateway
//! stored it.
//!
//! * `scitt` — an RFC 9942 COSE Receipt (vds 1) from an RFC 9943
//!   transparency service, verified under the witness's **pinned** Ed25519
//!   key: `witnessed` | `unverified` (no pin) | `invalid`.
//! * `rfc3161` — an RFC 3161 TimeStampToken over the statement bytes,
//!   verified by `brutor-tsp` under the pinned TSA certificate (PEM or the
//!   SHA-256 of its DER): `timestamped` | `unverified` | `invalid`.
//!
//! Grades never go up: a head's grade counts only verified `scitt`
//! receipts ([`head_grade`]); a timestamp proves *when*, not that anyone
//! else holds the head in an append-only structure.

use brutor_cose::merkle::leaf_hash;
use brutor_cose::receipt::verify_receipt;
use brutor_cose::sign1::CoseSign1;
use ed25519_dalek::VerifyingKey;
use sha2::{Digest, Sha256};

pub const KIND_SCITT: &str = "scitt";
pub const KIND_RFC3161: &str = "rfc3161";

/// Outcome of grading one receipt.
#[derive(Debug, Clone, Default)]
pub struct Graded {
    /// `witnessed` | `timestamped` | `unverified` | `invalid`.
    pub verdict: &'static str,
    pub errors: Vec<String>,
    pub vendor_ext: Option<serde_json::Value>,
    pub iat: Option<i64>,
    pub tree_size: Option<i64>,
    pub leaf_index: Option<i64>,
}

impl Graded {
    fn fail(verdict: &'static str, e: impl Into<String>) -> Self {
        Self {
            verdict,
            errors: vec![e.into()],
            ..Default::default()
        }
    }
}

/// `scitt` when the bytes are a COSE_Sign1, else `rfc3161`.
pub fn detect_kind(receipt: &[u8]) -> &'static str {
    if CoseSign1::decode(receipt).is_ok() {
        KIND_SCITT
    } else {
        KIND_RFC3161
    }
}

/// Grade an RFC 3161 token over `statement` under the pinned TSA
/// certificate (PEM or SHA-256 fingerprint of its DER).
pub fn grade_rfc3161(token: &[u8], statement: &[u8], pinned_key: Option<&str>) -> Graded {
    let pin = match pinned_key.map(str::trim).filter(|p| !p.is_empty()) {
        None => None,
        Some(p) => match brutor_tsp::Pin::parse(p) {
            Ok(pin) => Some(pin),
            Err(e) => return Graded::fail("invalid", e.to_string()),
        },
    };
    let v = brutor_tsp::verify_token(token, statement, pin.as_ref());
    let ext = v.info.as_ref().map(|i| {
        serde_json::json!({
            "gen_time": i.gen_time_rfc3339,
            "serial": i.serial_hex,
            "policy": i.policy,
            "imprint_alg": i.imprint_alg,
            "signer_cert_sha256": i.signer_cert_sha256,
            "signature_alg": i.signature_alg,
        })
    });
    let mut errors = v.errors;
    if pin.is_none() && errors.is_empty() {
        errors.push("witness has no pinned key".into());
    }
    Graded {
        verdict: v.grade.as_str(),
        errors,
        vendor_ext: ext,
        iat: v.info.map(|i| i.gen_time),
        tree_size: None,
        leaf_index: None,
    }
}

/// Verify a scitt receipt under the pinned key. Transparency services differ
/// in what they hash as the leaf entry (RFC 9943 leaves it to the service):
/// the whole statement bytes, SHA-256 of them, or SHA-256 of the
/// `Sig_structure`. Try each; report which one verified.
pub fn grade_scitt(receipt: &[u8], statement: &[u8], pinned_key: Option<&str>) -> Graded {
    let Some(pin) = pinned_key.map(str::trim).filter(|p| !p.is_empty()) else {
        return Graded::fail("unverified", "witness has no pinned key");
    };
    let key = hex::decode(pin)
        .ok()
        .and_then(|b| <[u8; 32]>::try_from(b).ok())
        .and_then(|b| VerifyingKey::from_bytes(&b).ok());
    let Some(key) = key else {
        return Graded::fail("invalid", "pinned key is not a 32-byte Ed25519 key in hex");
    };
    let sha_stmt: [u8; 32] = Sha256::digest(statement).into();
    let sig_structure = CoseSign1::decode(statement)
        .ok()
        .and_then(|s| {
            brutor_cose::sign1::sig_structure(
                &s.protected_bytes,
                s.payload.as_deref().unwrap_or(&[]),
            )
            .ok()
        })
        .map(|ss| Sha256::digest(ss).into());
    let candidates: Vec<(&str, [u8; 32])> = {
        let mut c = vec![
            ("leaf=sha256(0x00||statement)", leaf_hash(statement)),
            ("leaf=sha256(0x00||sha256(statement))", leaf_hash(&sha_stmt)),
        ];
        if let Some(ss) = sig_structure {
            let ss: [u8; 32] = ss;
            c.push(("leaf=sha256(0x00||sha256(sig_structure))", leaf_hash(&ss)));
        }
        c
    };
    let mut last = Vec::new();
    for (scheme, leaf) in candidates {
        let r = verify_receipt(receipt, &leaf, &key);
        if r.ok {
            let ext = serde_json::json!({
                "leaf_scheme": scheme,
                "iat": r.iat,
                "vendor_labels": r.vendor_ext.iter().map(|(l, _)| l).collect::<Vec<_>>(),
            });
            return Graded {
                verdict: "witnessed",
                errors: vec![],
                vendor_ext: Some(ext),
                iat: r.iat,
                tree_size: r.tree_size.map(|v| v as i64),
                leaf_index: r.leaf_index.map(|v| v as i64),
            };
        }
        last = r.errors;
    }
    Graded {
        verdict: "invalid",
        errors: last,
        ..Default::default()
    }
}

/// Grade one head's receipts (pure).
/// `rows` = `(witness_id, verdict, same_operator)`.
pub fn head_grade(rows: &[(String, String, bool)]) -> &'static str {
    let mut independent: Vec<&str> = Vec::new();
    let mut self_witnessed = false;
    for (wid, verdict, same_op) in rows {
        if verdict == "witnessed" {
            if *same_op {
                self_witnessed = true;
            } else if !independent.contains(&wid.as_str()) {
                independent.push(wid);
            }
        }
    }
    match independent.len() {
        0 if self_witnessed => "self_witnessed",
        0 => "standalone",
        1 => "witnessed",
        _ => "plural",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture(name: &str) -> Vec<u8> {
        std::fs::read(format!(
            "{}/../../tests/fixtures/rfc3161/{name}",
            env!("CARGO_MANIFEST_DIR")
        ))
        .unwrap()
    }

    #[test]
    fn rfc3161_grades() {
        let token = fixture("ecp256.tsr");
        let statement = fixture("statement.bin");
        let fp = String::from_utf8(fixture("ecp256.crt.sha256")).unwrap();
        let g = grade_rfc3161(&token, &statement, Some(&fp));
        assert_eq!(g.verdict, "timestamped", "{:?}", g.errors);
        assert!(g.iat.unwrap() > 1_700_000_000);
        assert_eq!(
            grade_rfc3161(&token, b"other", Some(&fp)).verdict,
            "invalid"
        );
        assert_eq!(
            grade_rfc3161(&token, &statement, None).verdict,
            "unverified"
        );
        assert_eq!(
            grade_rfc3161(&token, &statement, Some("not-a-pin")).verdict,
            "invalid"
        );
        assert_eq!(detect_kind(&token), KIND_RFC3161);
    }

    #[test]
    fn scitt_grades() {
        use brutor_cose::merkle::{inclusion_path, tree_hash};
        use brutor_cose::receipt::{InclusionProof, build_receipt};
        let witness = ed25519_dalek::SigningKey::from_bytes(&[7u8; 32]);
        let statement = b"a statement".to_vec();
        let leaves = vec![leaf_hash(b"x"), leaf_hash(&statement), leaf_hash(b"y")];
        let proof = InclusionProof {
            tree_size: 3,
            leaf_index: 1,
            path: inclusion_path(1, &leaves),
        };
        let r = build_receipt(&proof, &tree_hash(&leaves), &witness, Some(1_800_000_000)).unwrap();
        let pin = hex::encode(witness.verifying_key().to_bytes());
        let g = grade_scitt(&r, &statement, Some(&pin));
        assert_eq!(g.verdict, "witnessed", "{:?}", g.errors);
        assert_eq!(
            (g.tree_size, g.leaf_index, g.iat),
            (Some(3), Some(1), Some(1_800_000_000))
        );
        assert_eq!(grade_scitt(&r, b"other", Some(&pin)).verdict, "invalid");
        assert_eq!(grade_scitt(&r, &statement, None).verdict, "unverified");
        assert_eq!(grade_scitt(&r, &statement, Some("zz")).verdict, "invalid");
        assert_eq!(detect_kind(&r), KIND_SCITT);
    }

    #[test]
    fn timestamps_never_raise_the_head_grade() {
        let r = |w: &str, v: &str, same: bool| (w.to_string(), v.to_string(), same);
        assert_eq!(head_grade(&[]), "standalone");
        assert_eq!(head_grade(&[r("tsa", "timestamped", false)]), "standalone");
        assert_eq!(
            head_grade(&[r("local", "witnessed", true)]),
            "self_witnessed"
        );
        assert_eq!(
            head_grade(&[r("a", "witnessed", false), r("tsa", "timestamped", false)]),
            "witnessed"
        );
        assert_eq!(
            head_grade(&[r("a", "witnessed", false), r("a", "witnessed", false)]),
            "witnessed"
        );
        assert_eq!(
            head_grade(&[
                r("a", "witnessed", false),
                r("b", "witnessed", false),
                r("c", "invalid", false)
            ]),
            "plural"
        );
        assert_eq!(
            head_grade(&[r("a", "witnessed", false), r("local", "witnessed", true)]),
            "witnessed"
        );
    }
}
