// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Disclosure checks DE-1..DE-3 (companion draft
//! `draft-mih-scitt-agent-action-capsule-disclosure-envelope-00`), shared
//! by the per-Capsule Disclosure Envelope `{capsule, disclosures}` and the
//! bundle-level overlay of `evidence-bundle/v2`.
//!
//! DE-1 the member is eligible (registry table) · DE-2 the Capsule carries
//! a 64-hex committed digest at the member's path · DE-3
//! `JSON-DIGEST(preimage)` equals it. A disclosure never changes the
//! Capsule or its identity; a bad disclosure never changes the Capsule's
//! own Class-1 result.

use serde::Serialize;
use serde_json::{Map, Value};

use crate::canon::{self, is_hex64};
use crate::registry;
use crate::verify::{self, VerificationResult};

pub const MATCH: &str = "disclosure_match";
pub const MISMATCH: &str = "disclosure_mismatch";
pub const INELIGIBLE: &str = "disclosure_ineligible_field";
pub const NO_COMMITTED_DIGEST: &str = "disclosure_no_committed_digest";

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DisclosureFinding {
    pub member: String,
    pub code: &'static str,
}

/// The committed digest at a dotted path inside a Capsule.
pub fn committed_digest<'a>(capsule: &'a Value, path: &str) -> Option<&'a str> {
    let mut cur = capsule;
    for part in path.split('.') {
        cur = cur.as_object()?.get(part)?;
    }
    cur.as_str()
}

/// DE-1..DE-3 for one member.
pub fn check_member(capsule: &Value, member: &str, preimage: &Value) -> &'static str {
    let Some(path) = registry::disclosure_path(member) else {
        return INELIGIBLE;
    };
    let Some(stored) = committed_digest(capsule, path).filter(|s| is_hex64(s)) else {
        return NO_COMMITTED_DIGEST;
    };
    match canon::json_digest(preimage) {
        Ok(d) if d == stored => MATCH,
        _ => MISMATCH,
    }
}

/// DE-1..DE-3 over a `disclosures` object, members in sorted order.
pub fn check_disclosures(
    capsule: &Value,
    disclosures: &Map<String, Value>,
) -> Vec<DisclosureFinding> {
    // serde_json's Map iterates in sorted key order.
    disclosures
        .iter()
        .map(|(m, v)| DisclosureFinding {
            member: m.clone(),
            code: check_member(capsule, m, v),
        })
        .collect()
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct EnvelopeVerification {
    pub capsule: VerificationResult,
    pub disclosure_findings: Vec<DisclosureFinding>,
}

impl EnvelopeVerification {
    pub fn disclosures_checked(&self) -> usize {
        self.disclosure_findings.len()
    }
    pub fn disclosures_matched(&self) -> usize {
        self.disclosure_findings
            .iter()
            .filter(|f| f.code == MATCH)
            .count()
    }
    /// Class 1 passes and every presented disclosure matches.
    pub fn ok(&self) -> bool {
        self.capsule.ok && self.disclosure_findings.iter().all(|f| f.code == MATCH)
    }
}

/// Verify a Disclosure Envelope `{capsule, disclosures}`.
pub fn verify_disclosure_envelope(envelope: &Value) -> EnvelopeVerification {
    let capsule = match envelope {
        Value::Object(o) => o.get("capsule").unwrap_or(&Value::Null),
        other => other,
    };
    let result = verify::verify(capsule, None);
    let disclosure_findings = envelope
        .get("disclosures")
        .and_then(Value::as_object)
        .map(|d| check_disclosures(capsule, d))
        .unwrap_or_default();
    EnvelopeVerification {
        capsule: result,
        disclosure_findings,
    }
}
