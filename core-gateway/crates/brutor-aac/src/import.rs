// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Import: Capsules produced by `capsule-emit` adapters, parsed into a
//! **received** view. Nothing here seals, stores or trusts anything.
//!
//! A `capsule-emit` ledger is JSONL: one Capsule per line, carrying the
//! local-only `signature` (hex COSE_Sign1 Producer Envelope over the raw
//! Capsule ID) and `key_id` (hex raw Ed25519 key), interleaved with
//! non-capsule bookkeeping entries (`kind` = `checkpoint_stamp`,
//! `disclosure_record`, `witness_backfill`, `archived_segment`) that
//! occupy a sequence position but are not Capsules.
//!
//! Each Capsule is graded on two independent axes:
//!
//! * **Class 1** over the whole ledger as the store (chain parents must be
//!   present; concurrent supersedes surface as findings).
//! * **Authorship**, capsule-emit's tri-state: `authored` (claimed and the
//!   envelope verifies under the key it carries, which must equal
//!   `key_id`), `unclaimed` (neither field — e.g. a `log()` entry; *not*
//!   evidence of forgery), `invalid` (half a claim, or a claim that fails).
//!   Authentication only — the key is self-asserted; binding it to the
//!   operator is caller policy.
//!
//! `brutor_verdict` is the record verdict the Capsule's `verdict_class`
//! corresponds to (`hitl_dispatched` → `awaiting_human`, …); `deferred`,
//! `needs_decision`, `escalated` and unregistered classes have none.

use serde::Serialize;
use serde_json::Value;

use crate::envelope;
use crate::mapping::verdict_for_class;
use crate::verify::{self, VerificationResult, derive_effect_mode};

/// capsule-emit `NON_CAPSULE_KINDS`.
pub const NON_CAPSULE_KINDS: &[&str] = &[
    "checkpoint_stamp",
    "disclosure_record",
    "witness_backfill",
    "archived_segment",
];

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum Authorship {
    Authored { public_key_hex: String },
    Unclaimed,
    Invalid { code: String, detail: String },
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ReceivedCapsule {
    /// Always `"received"` — an imported Capsule is never our record.
    pub status: &'static str,
    /// 1-based ledger position (every line counts, as capsule-emit does).
    pub seq: Option<u64>,
    pub capsule_id: Option<String>,
    pub class1: VerificationResult,
    pub authorship: Authorship,
    /// `class1.ok` and authorship is not `invalid`.
    pub integrity_ok: bool,
    pub action_id: Option<String>,
    pub action_type: Option<String>,
    pub operator: Option<String>,
    pub developer: Option<String>,
    pub timestamp: Option<String>,
    pub verdict_class: Option<String>,
    pub decision: Option<String>,
    pub approver: Option<String>,
    pub human_disposed: Option<bool>,
    pub effect_type: Option<String>,
    pub effect_status: Option<String>,
    pub effect_mode: Option<String>,
    pub parent_capsule_id: Option<String>,
    pub chain_relation: Option<String>,
    pub brutor_verdict: Option<&'static str>,
    /// The Capsule exactly as received (local-only fields included).
    pub capsule: Value,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(tag = "entry", rename_all = "snake_case")]
pub enum LedgerEntry {
    Capsule(Box<ReceivedCapsule>),
    NonCapsule { seq: u64, kind: String },
    Unparseable { seq: u64, error: String },
}

fn s(v: &Value, path: &[&str]) -> Option<String> {
    let mut cur = v;
    for p in path {
        cur = cur.get(*p)?;
    }
    cur.as_str().map(str::to_owned)
}

/// Grade the self-attested producer-authorship claim.
pub fn authorship(capsule: &Value, recomputed_id: Option<&str>) -> Authorship {
    let sig = capsule.get("signature");
    let kid = capsule.get("key_id");
    match (sig, kid) {
        (None, None) => Authorship::Unclaimed,
        (Some(sig), Some(kid)) => {
            let (Some(sig), Some(kid)) = (sig.as_str(), kid.as_str()) else {
                return Authorship::Invalid {
                    code: "authorship_claim_malformed".into(),
                    detail: "signature and key_id must be hex strings".into(),
                };
            };
            let Ok(raw) = hex::decode(sig) else {
                return Authorship::Invalid {
                    code: "authorship_claim_malformed".into(),
                    detail: "signature is not hex".into(),
                };
            };
            let Some(id) = recomputed_id else {
                return Authorship::Invalid {
                    code: "capsule_id_uncomputable".into(),
                    detail: "the Capsule ID could not be recomputed".into(),
                };
            };
            let r = envelope::verify_producer_envelope(id, &raw);
            if !r.ok {
                let f = r.findings.into_iter().next();
                return Authorship::Invalid {
                    code: f.as_ref().map(|f| f.code.clone()).unwrap_or_default(),
                    detail: f.map(|f| f.detail).unwrap_or_default(),
                };
            }
            let pk = r.public_key_hex.unwrap_or_default();
            if !pk.eq_ignore_ascii_case(kid) {
                return Authorship::Invalid {
                    code: "key_id_mismatch".into(),
                    detail: "key_id does not equal the envelope's kid".into(),
                };
            }
            Authorship::Authored { public_key_hex: pk }
        }
        _ => Authorship::Invalid {
            code: "authorship_claim_partial".into(),
            detail: "only one of signature / key_id is present".into(),
        },
    }
}

/// Build the received view from a Class-1 result already computed.
pub fn receive_with(
    capsule: Value,
    seq: Option<u64>,
    class1: VerificationResult,
) -> ReceivedCapsule {
    let auth = authorship(&capsule, class1.capsule_id.as_deref());
    let effect = capsule.get("effect").and_then(Value::as_object);
    let verdict_class = s(&capsule, &["disposition", "verdict_class"]);
    ReceivedCapsule {
        status: "received",
        seq,
        capsule_id: s(&capsule, &["capsule_id"]),
        integrity_ok: class1.ok && !matches!(auth, Authorship::Invalid { .. }),
        authorship: auth,
        action_id: s(&capsule, &["action_id"]),
        action_type: s(&capsule, &["action_type"]),
        operator: s(&capsule, &["operator"]),
        developer: s(&capsule, &["developer"]),
        timestamp: s(&capsule, &["timestamp"]),
        brutor_verdict: verdict_class.as_deref().and_then(verdict_for_class),
        verdict_class,
        decision: s(&capsule, &["disposition", "decision"]),
        approver: s(&capsule, &["disposition", "approver"]),
        human_disposed: capsule
            .get("disposition")
            .and_then(|d| d.get("human_disposed"))
            .and_then(Value::as_bool),
        effect_type: s(&capsule, &["effect", "type"]),
        effect_status: s(&capsule, &["effect", "status"]),
        effect_mode: capsule
            .is_object()
            .then(|| derive_effect_mode(effect).to_string()),
        parent_capsule_id: s(&capsule, &["chain", "parent_capsule_id"]),
        chain_relation: s(&capsule, &["chain", "relation"]),
        class1,
        capsule,
    }
}

/// Receive one standalone Capsule (no store: chain checks are skipped).
pub fn receive(capsule: Value) -> ReceivedCapsule {
    let class1 = verify::verify(&capsule, None);
    receive_with(capsule, None, class1)
}

/// Parse a capsule-emit JSONL ledger. Blank lines are ignored and do not
/// consume a sequence number; every other line does.
pub fn parse_capsule_emit_ledger(jsonl: &str) -> Vec<LedgerEntry> {
    let mut parsed: Vec<(u64, Result<Value, String>)> = Vec::new();
    let mut seq = 0u64;
    for line in jsonl.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        seq += 1;
        parsed.push((
            seq,
            serde_json::from_str::<Value>(line).map_err(|e| e.to_string()),
        ));
    }
    let capsules: Vec<Value> = parsed
        .iter()
        .filter_map(|(_, r)| r.as_ref().ok())
        .filter(|v| !is_non_capsule(v))
        .cloned()
        .collect();
    let mut results = verify::verify_store(&capsules).into_iter();
    parsed
        .into_iter()
        .map(|(seq, r)| match r {
            Err(error) => LedgerEntry::Unparseable { seq, error },
            Ok(v) if is_non_capsule(&v) => LedgerEntry::NonCapsule {
                seq,
                kind: s(&v, &["kind"]).unwrap_or_default(),
            },
            Ok(v) => {
                let class1 = results.next().unwrap_or_else(|| verify::verify(&v, None));
                LedgerEntry::Capsule(Box::new(receive_with(v, Some(seq), class1)))
            }
        })
        .collect()
}

fn is_non_capsule(v: &Value) -> bool {
    v.get("kind")
        .and_then(Value::as_str)
        .map(|k| NON_CAPSULE_KINDS.contains(&k))
        .unwrap_or(false)
}
