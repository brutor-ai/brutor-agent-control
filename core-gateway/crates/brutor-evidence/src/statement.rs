// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Sealing a record as an RFC 9943 Signed Statement in RFC 9995
//! hash-envelope mode.
//!
//! ```text
//! protected = { 1: -8, 4: <raw pubkey>, 15: {1: iss, 2: sub},
//!               258: -16, 259: "application/vnd.brutor.action-record+json" }
//! payload   = raw 32-byte SHA-256 of the JCS record bytes  (== record_id)
//! ```
//!
//! No label 3 in hash-envelope mode (RFC 9995). Any COSE library can
//! check the signature; recomputing the payload from the JSON is one
//! hash. The record JSON is stored once beside the ~200-byte statement.

use crate::canon::CanonError;
use crate::record::compute_record_id;
use brutor_cose::CoseError;
use brutor_cose::cbor::{self, as_i64, as_text, bytes, int, text};
use brutor_cose::sign1::{self, CoseSign1, alg, cwt, label};
use ed25519_dalek::{SigningKey, VerifyingKey};
use serde_json::Value;

pub const RECORD_CONTENT_TYPE: &str = "application/vnd.brutor.action-record+json";

#[derive(Debug, thiserror::Error)]
pub enum StatementError {
    #[error("cose: {0}")]
    Cose(#[from] CoseError),
    #[error("canon: {0}")]
    Canon(#[from] CanonError),
    #[error("statement malformed: {0}")]
    Malformed(String),
    #[error("statement payload does not match record_id")]
    PayloadMismatch,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StatementInfo {
    pub iss: String,
    pub sub: String,
    pub kid: [u8; 32],
    /// hex of the 32-byte payload (the record_id it commits to)
    pub record_id: String,
}

/// Seal `record` (already carrying `record_id`) with `key`.
pub fn seal_record(
    record: &Value,
    iss: &str,
    sub: &str,
    key: &SigningKey,
) -> Result<Vec<u8>, StatementError> {
    let id = compute_record_id(record)?;
    let digest = hex::decode(&id).map_err(|e| StatementError::Malformed(e.to_string()))?;
    let protected = vec![
        (int(label::KID), bytes(key.verifying_key().as_bytes())),
        (
            int(label::CWT_CLAIMS),
            cbor::canonical_map(vec![(int(cwt::ISS), text(iss)), (int(cwt::SUB), text(sub))])?,
        ),
        (int(label::PAYLOAD_HASH_ALG), int(alg::SHA256)),
        (int(label::PREIMAGE_CONTENT_TYPE), text(RECORD_CONTENT_TYPE)),
    ];
    Ok(sign1::sign_ed25519(protected, vec![], Some(&digest), key)?.encode()?)
}

/// Parse and verify a statement under a trusted key, then check that its
/// payload equals the record's recomputed identity.
pub fn verify_record_statement(
    raw: &[u8],
    record: &Value,
    key: &VerifyingKey,
) -> Result<StatementInfo, StatementError> {
    let (info, s) = parse_statement(raw)?;
    s.verify_ed25519(key, None)?;
    let id = compute_record_id(record)?;
    if info.record_id != id {
        return Err(StatementError::PayloadMismatch);
    }
    Ok(info)
}

/// Structural parse (no signature check) — used to read `iss`/`sub`/`kid`
/// before deciding which key to trust.
pub fn parse_statement(raw: &[u8]) -> Result<(StatementInfo, CoseSign1), StatementError> {
    let s = CoseSign1::decode(raw)?;
    if s.protected_get(label::CONTENT_TYPE).is_some() {
        return Err(StatementError::Malformed(
            "label 3 present in hash-envelope mode".into(),
        ));
    }
    if s.protected_get(label::PAYLOAD_HASH_ALG).and_then(as_i64) != Some(alg::SHA256) {
        return Err(StatementError::Malformed(
            "payload-hash-alg (258) must be SHA-256".into(),
        ));
    }
    if s.protected_get(label::PREIMAGE_CONTENT_TYPE)
        .and_then(as_text)
        != Some(RECORD_CONTENT_TYPE)
    {
        return Err(StatementError::Malformed(
            "preimage content type (259)".into(),
        ));
    }
    let iss = s
        .cwt_claim(cwt::ISS)
        .and_then(as_text)
        .ok_or_else(|| StatementError::Malformed("iss".into()))?
        .to_owned();
    let sub = s
        .cwt_claim(cwt::SUB)
        .and_then(as_text)
        .ok_or_else(|| StatementError::Malformed("sub".into()))?
        .to_owned();
    let kid: [u8; 32] = s
        .kid()
        .and_then(|k| k.try_into().ok())
        .ok_or(CoseError::BadKid)?;
    let payload = s.payload.as_ref().ok_or(CoseError::DetachedPayload)?;
    if payload.len() != 32 {
        return Err(StatementError::Malformed("payload is not 32 bytes".into()));
    }
    let info = StatementInfo {
        iss,
        sub,
        kid,
        record_id: hex::encode(payload),
    };
    Ok((info, s))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::record::{Disposition, RecordBuilder, verdict};
    use serde_json::json;

    #[test]
    fn seal_parse_verify_and_mismatch() {
        let mut b = RecordBuilder::new("mcp/1", "tenant", "2026-09-19T00:00:00Z");
        b.verdict = verdict::DENIED.into();
        b.disposition = Some(Disposition {
            decision: "reject".into(),
            approver: "policy".into(),
            human_disposed: false,
            authority: None,
            reason_digest: None,
        });
        let (rec, id) = b.seal().unwrap();
        let key = SigningKey::from_bytes(&[11u8; 32]);
        let raw = seal_record(&rec, "urn:brutor:deployment:dev", "ai-system:sys-1", &key).unwrap();
        assert!(
            raw.len() < 260,
            "statement should be small, got {}",
            raw.len()
        );
        let info = verify_record_statement(&raw, &rec, &key.verifying_key()).unwrap();
        assert_eq!(info.record_id, id);
        assert_eq!(info.iss, "urn:brutor:deployment:dev");
        assert_eq!(info.kid, key.verifying_key().to_bytes());

        let mut other = rec.clone();
        other["verdict"] = json!("executed");
        assert!(matches!(
            verify_record_statement(&raw, &other, &key.verifying_key()),
            Err(StatementError::PayloadMismatch)
        ));
        let wrong = SigningKey::from_bytes(&[12u8; 32]);
        assert!(verify_record_statement(&raw, &rec, &wrong.verifying_key()).is_err());
    }
}
