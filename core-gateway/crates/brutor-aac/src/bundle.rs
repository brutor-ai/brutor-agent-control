// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! AAC Evidence Bundle `evidence-bundle/v2`
//! (`draft-mih-zhang-agent-action-capsule-evidence-bundle-00`).
//!
//! ```text
//! { "bundle_version": "2", "bundle_kind": "evidence-bundle/v2",
//!   "root": <capsule_id>, "records": [ <unmodified Capsules> ],
//!   "completeness": { closure_depth, records_mode, payloads_mode, suppressed_fields, missing },
//!   "disclosures": { <capsule_id>: { <member>: <preimage> } },
//!   "extensions": { <kind>: {…} }, "verification": {…}, "countersignatures": [ … ] }
//! ```
//!
//! Where this follows upstream and not the Brutor bundle:
//!
//! * `records` are bare Capsules — the draft has **no slot for Producer
//!   Envelopes**. We carry them in a private, digest-covered extension
//!   `x-ai.brutor/producer-envelopes/v1` = `{capsule_id: [base64url COSE]}`
//!   (private `x-` kinds are explicitly unregistered, §6). Other verifiers
//!   report it "uninterpreted"; ours verifies every envelope in it.
//! * Interval coverage and per-record membership are proven with CLL MMR
//!   range/inclusion proofs (`completeness_certificate`, `checkpoint`),
//!   not RFC 9162 proofs. There is no MMR mirror (it would be built only if a
//!   witness demands it), so this crate never emits a certificate and,
//!   when one is present, reports both claims `not_evaluated`
//!   (`cll_mmr_not_implemented`) — never `pass`.
//! * The bundle digest excludes only `countersignatures` (same rule as our
//!   `evidence-bundle/v1`); countersignatures are always `unverified`.
//!
//! Graph closure, disclosures (DE-1..3), extensions, the bundle digest and
//! the fragment codec follow the draft exactly and agree with the upstream
//! verifier on the vendored bundle cases we can reconstruct.

use std::collections::{BTreeMap, BTreeSet};

use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use serde::Serialize;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::canon::{self, is_hex64};
use crate::disclosure;
use crate::envelope::{self, EnvelopeResult};
use crate::registry;
use crate::verify::{self, VerificationResult};

pub const BUNDLE_VERSION: &str = "2";
pub const BUNDLE_KIND: &str = "evidence-bundle/v2";
pub const DEFAULT_CLOSURE_DEPTH: u64 = 2;
pub const ENVELOPES_EXTENSION: &str = "x-ai.brutor/producer-envelopes/v1";

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum BundleError {
    #[error("record {0} is not a Capsule with a reproducible capsule_id")]
    BadRecord(usize),
    #[error("duplicate capsule_id {0}")]
    Duplicate(String),
    #[error("root {0} is not among the records")]
    RootMissing(String),
    #[error("envelopes supplied for {0}, which is not among the records")]
    EnvelopeForUnknownRecord(String),
    #[error("disclosure for {0}: {1}")]
    Disclosure(String, &'static str),
    #[error("canonicalisation: {0}")]
    Canon(String),
    #[error("bundle must be a JSON object")]
    NotObject,
    #[error("fragment: {0}")]
    Fragment(String),
}

/// Producer inputs. Everything else is derived deterministically.
#[derive(Debug, Clone, Default)]
pub struct BundleInput {
    pub root: String,
    pub records: Vec<Value>,
    /// Default [`DEFAULT_CLOSURE_DEPTH`].
    pub closure_depth: Option<u64>,
    /// `capsule_id → raw COSE envelopes`.
    pub envelopes: BTreeMap<String, Vec<Vec<u8>>>,
    /// `capsule_id → {member → preimage}`; every entry must DE-3 match.
    pub disclosures: BTreeMap<String, Map<String, Value>>,
    /// Producer self-report; labelled as such by every verifier.
    pub verification: Option<Value>,
}

/// Citation targets of one record: its chain parent and every
/// `agent-action-capsule`/`SHA-256` reference digest.
pub fn citation_targets(record: &Value) -> Vec<String> {
    let mut out = Vec::new();
    if let Some(p) = record
        .get("chain")
        .and_then(|c| c.get("parent_capsule_id"))
        .and_then(Value::as_str)
    {
        out.push(p.to_string());
    }
    if let Some(refs) = record.get("references").and_then(Value::as_array) {
        for r in refs {
            if r.get("type").and_then(Value::as_str) == Some("agent-action-capsule")
                && r.get("digest_alg").and_then(Value::as_str) == Some("SHA-256")
                && let Some(d) = r.get("digest").and_then(Value::as_str)
            {
                out.push(d.to_string());
            }
        }
    }
    out
}

/// Targets reached within `depth` edges from `root` that are not supplied.
fn unsupplied_targets(
    root: &str,
    records: &BTreeMap<String, &Value>,
    depth: u64,
) -> BTreeSet<String> {
    let mut missing = BTreeSet::new();
    let mut frontier = vec![root.to_string()];
    for _ in 0..depth {
        let mut next = Vec::new();
        for id in &frontier {
            let Some(rec) = records.get(id) else { continue };
            for t in citation_targets(rec) {
                if records.contains_key(&t) {
                    next.push(t);
                } else {
                    missing.insert(t);
                }
            }
        }
        frontier = next;
    }
    missing
}

/// Build a bundle. Every record must reproduce its own `capsule_id`
/// (records are carried unmodified); the closure is walked and every
/// unsupplied target is declared in `missing`, never dropped.
pub fn build_bundle(input: &BundleInput) -> Result<Value, BundleError> {
    let mut by_id: BTreeMap<String, &Value> = BTreeMap::new();
    for (i, r) in input.records.iter().enumerate() {
        let carried = r.get("capsule_id").and_then(Value::as_str).unwrap_or("");
        let recomputed = canon::compute_capsule_id(r).map_err(|_| BundleError::BadRecord(i))?;
        if recomputed != carried {
            return Err(BundleError::BadRecord(i));
        }
        if by_id.insert(recomputed.clone(), r).is_some() {
            return Err(BundleError::Duplicate(recomputed));
        }
    }
    if !by_id.contains_key(&input.root) {
        return Err(BundleError::RootMissing(input.root.clone()));
    }
    let depth = input.closure_depth.unwrap_or(DEFAULT_CLOSURE_DEPTH);
    let missing = unsupplied_targets(&input.root, &by_id, depth);

    let mut disclosures = Map::new();
    let mut suppressed = BTreeSet::new();
    for (id, rec) in &by_id {
        let supplied = input.disclosures.get(id);
        for (member, path) in registry::DISCLOSURE_ELIGIBLE {
            if disclosure::committed_digest(rec, path).is_some()
                && !supplied.map(|s| s.contains_key(*member)).unwrap_or(false)
            {
                suppressed.insert(member.to_string());
            }
        }
    }
    for (id, members) in &input.disclosures {
        let rec = by_id
            .get(id)
            .ok_or_else(|| BundleError::Disclosure(id.clone(), "no such record"))?;
        for (m, v) in members {
            let code = disclosure::check_member(rec, m, v);
            if code != disclosure::MATCH {
                return Err(BundleError::Disclosure(id.clone(), code));
            }
        }
        disclosures.insert(id.clone(), Value::Object(members.clone()));
    }

    let mut b = Map::new();
    b.insert("bundle_version".into(), json!(BUNDLE_VERSION));
    b.insert("bundle_kind".into(), json!(BUNDLE_KIND));
    b.insert("root".into(), json!(input.root));
    b.insert("records".into(), Value::Array(input.records.clone()));
    b.insert(
        "completeness".into(),
        json!({
            "closure_depth": depth,
            "records_mode": if missing.is_empty() { "complete" } else { "declared_incomplete" },
            "payloads_mode": if suppressed.is_empty() { "all" } else { "selected" },
            "suppressed_fields": suppressed.into_iter().collect::<Vec<_>>(),
            "missing": missing.into_iter().collect::<Vec<_>>(),
        }),
    );
    if !disclosures.is_empty() {
        b.insert("disclosures".into(), Value::Object(disclosures));
    }
    if !input.envelopes.is_empty() {
        let mut env = Map::new();
        for (id, list) in &input.envelopes {
            if !by_id.contains_key(id) {
                return Err(BundleError::EnvelopeForUnknownRecord(id.clone()));
            }
            env.insert(
                id.clone(),
                Value::Array(
                    list.iter()
                        .map(|e| json!(URL_SAFE_NO_PAD.encode(e)))
                        .collect(),
                ),
            );
        }
        b.insert("extensions".into(), json!({ ENVELOPES_EXTENSION: env }));
    }
    if let Some(v) = &input.verification {
        b.insert("verification".into(), v.clone());
    }
    Ok(Value::Object(b))
}

/// SHA-256 over JCS(bundle without `countersignatures`), lowercase hex.
pub fn bundle_digest(bundle: &Value) -> Result<String, BundleError> {
    let mut m = bundle.as_object().ok_or(BundleError::NotObject)?.clone();
    m.remove("countersignatures");
    let bytes = canon::jcs(&Value::Object(m)).map_err(|e| BundleError::Canon(e.to_string()))?;
    Ok(hex::encode(Sha256::digest(bytes)))
}

/// Permalink fragment: unpadded base64url over UTF-8 JCS of the bundle.
pub fn encode_fragment(bundle: &Value) -> Result<String, BundleError> {
    let bytes = canon::jcs(bundle).map_err(|e| BundleError::Canon(e.to_string()))?;
    Ok(URL_SAFE_NO_PAD.encode(bytes))
}

pub fn decode_fragment(fragment: &str) -> Result<Value, BundleError> {
    if !fragment
        .bytes()
        .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
    {
        return Err(BundleError::Fragment(
            "fragment must be unpadded base64url".into(),
        ));
    }
    let raw = URL_SAFE_NO_PAD
        .decode(fragment)
        .map_err(|e| BundleError::Fragment(e.to_string()))?;
    serde_json::from_slice(&raw).map_err(|e| BundleError::Fragment(format!("not UTF-8 JSON: {e}")))
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ClaimResult {
    /// `pass` | `fail` | `withheld` | `not_evaluated`
    pub status: &'static str,
    pub findings: Vec<String>,
}

impl ClaimResult {
    fn new(status: &'static str, findings: Vec<String>) -> Self {
        Self { status, findings }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DisclosureResult {
    pub capsule_id: String,
    pub member: String,
    /// `withheld` or a DE code.
    pub status: &'static str,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ExtensionResult {
    pub kind: String,
    /// `interpreted` (ours) or `uninterpreted` (integrity-covered only).
    pub status: &'static str,
    pub integrity_covered: bool,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub envelopes: Vec<EnvelopeResult>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub findings: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct BundleVerification {
    pub bundle_digest: Option<String>,
    pub graph_closure: ClaimResult,
    pub interval_coverage: ClaimResult,
    pub per_record_membership: ClaimResult,
    pub disclosures: Vec<DisclosureResult>,
    pub extensions: Vec<ExtensionResult>,
    /// Always `unverified` (reserved neutral slot).
    pub countersignatures: Vec<Value>,
    /// Producer self-report, never evidence.
    pub verification_self_report: Option<Value>,
    pub capsule_results: BTreeMap<String, VerificationResult>,
}

fn u64_member(v: &Value) -> Option<u64> {
    v.as_u64().filter(|n| *n <= canon::MAX_SAFE_INTEGER)
}

/// Verify a bundle from its own bytes. Never panics.
pub fn verify_bundle(bundle: &Value) -> BundleVerification {
    let Some(b) = bundle.as_object() else {
        let bad = ClaimResult::new("fail", vec!["bundle_malformed".into()]);
        return BundleVerification {
            bundle_digest: None,
            graph_closure: bad.clone(),
            interval_coverage: bad.clone(),
            per_record_membership: bad,
            disclosures: vec![],
            extensions: vec![],
            countersignatures: vec![],
            verification_self_report: None,
            capsule_results: BTreeMap::new(),
        };
    };
    let digest = bundle_digest(bundle).ok();

    // Records (each verified under the base profile, identity must match).
    let mut records: BTreeMap<String, &Value> = BTreeMap::new();
    let mut capsule_results = BTreeMap::new();
    let mut record_findings = Vec::new();
    match b.get("records").and_then(Value::as_array) {
        None => record_findings.push("records_malformed".to_string()),
        Some(list) => {
            for (i, r) in list.iter().enumerate() {
                if !r.is_object() {
                    record_findings.push(format!("record_malformed:{i}"));
                    continue;
                }
                let Some(id) = r
                    .get("capsule_id")
                    .and_then(Value::as_str)
                    .filter(|s| is_hex64(s))
                else {
                    record_findings.push(format!("record_identity_invalid:{i}"));
                    continue;
                };
                if records.contains_key(id) {
                    record_findings.push(format!("record_duplicate:{id}"));
                    continue;
                }
                let res = verify::verify(r, None);
                let good = res.ok && res.capsule_id.as_deref() == Some(id);
                capsule_results.insert(id.to_string(), res);
                if !good {
                    record_findings.push(format!("record_identity_invalid:{id}"));
                    continue;
                }
                records.insert(id.to_string(), r);
            }
        }
    }

    let graph_closure = graph(b, &records, record_findings);
    let (interval_coverage, per_record_membership) = if b
        .get("completeness_certificate")
        .is_some_and(Value::is_object)
        && b.get("checkpoint").is_some_and(Value::is_object)
    {
        let n = ClaimResult::new("not_evaluated", vec!["cll_mmr_not_implemented".into()]);
        (n.clone(), n)
    } else {
        let a = ClaimResult::new("withheld", vec!["completeness_evidence_absent".into()]);
        (a.clone(), a)
    };

    let empty = Value::Object(Map::new());
    let disclosures = disclosure_results(b.get("disclosures").unwrap_or(&empty), &records);
    let extensions = extension_results(b.get("extensions"), &records);
    let countersignatures = b
        .get("countersignatures")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .map(|v| json!({"value": v, "status": "unverified"}))
                .collect()
        })
        .unwrap_or_default();
    let verification_self_report = b
        .get("verification")
        .map(|v| json!({"value": v, "status": "producer_self_report"}));
    BundleVerification {
        bundle_digest: digest,
        graph_closure,
        interval_coverage,
        per_record_membership,
        disclosures,
        extensions,
        countersignatures,
        verification_self_report,
        capsule_results,
    }
}

fn graph(
    b: &Map<String, Value>,
    records: &BTreeMap<String, &Value>,
    mut findings: Vec<String>,
) -> ClaimResult {
    if b.get("bundle_version").and_then(Value::as_str) != Some(BUNDLE_VERSION)
        || b.get("bundle_kind").and_then(Value::as_str) != Some(BUNDLE_KIND)
    {
        findings.push("bundle_version_or_kind_invalid".into());
    }
    let Some(root) = b
        .get("root")
        .and_then(Value::as_str)
        .filter(|r| records.contains_key(*r))
    else {
        findings.push("root_not_supplied_with_matching_identity".into());
        return ClaimResult::new("fail", findings);
    };
    let Some(complete) = b.get("completeness").and_then(Value::as_object) else {
        findings.push("completeness_malformed".into());
        return ClaimResult::new("fail", findings);
    };
    let depth = match complete.get("closure_depth") {
        None => DEFAULT_CLOSURE_DEPTH,
        Some(v) => match u64_member(v) {
            Some(d) => d,
            None => {
                findings.push("closure_depth_invalid".into());
                return ClaimResult::new("fail", findings);
            }
        },
    };
    let empty = Vec::new();
    let missing_list = match complete.get("missing") {
        None => &empty,
        Some(Value::Array(a)) => a,
        Some(_) => {
            findings.push("missing_malformed".into());
            return ClaimResult::new("fail", findings);
        }
    };
    let mut missing = BTreeSet::new();
    for m in missing_list {
        let Some(id) = m.as_str().filter(|s| is_hex64(s)) else {
            findings.push("missing_malformed".into());
            return ClaimResult::new("fail", findings);
        };
        if !missing.insert(id.to_string()) {
            findings.push("missing_duplicate".into());
        }
    }
    let expected_mode = if missing.is_empty() {
        "complete"
    } else {
        "declared_incomplete"
    };
    if complete.get("records_mode").and_then(Value::as_str) != Some(expected_mode) {
        findings.push("records_mode_mismatch".into());
    }
    for t in unsupplied_targets(root, records, depth) {
        if !missing.contains(&t) {
            findings.push(format!("citation_dangling:{t}"));
        }
    }
    if !findings.is_empty() {
        return ClaimResult::new("fail", findings);
    }
    if !missing.is_empty() {
        return ClaimResult::new("withheld", vec!["declared_incomplete".into()]);
    }
    ClaimResult::new("pass", vec![])
}

fn disclosure_results(raw: &Value, records: &BTreeMap<String, &Value>) -> Vec<DisclosureResult> {
    let Some(overlay) = raw.as_object() else {
        return vec![DisclosureResult {
            capsule_id: String::new(),
            member: String::new(),
            status: disclosure::MISMATCH,
        }];
    };
    let mut out = Vec::new();
    // WITHHELD: an eligible committed digest with no overlay member.
    for (id, rec) in records {
        let supplied = match overlay.get(id) {
            None => None,
            Some(Value::Object(m)) => Some(m),
            Some(_) => continue,
        };
        for (member, path) in registry::DISCLOSURE_ELIGIBLE {
            if !supplied.map(|s| s.contains_key(*member)).unwrap_or(false)
                && disclosure::committed_digest(rec, path).is_some()
            {
                out.push(DisclosureResult {
                    capsule_id: id.clone(),
                    member: member.to_string(),
                    status: "withheld",
                });
            }
        }
    }
    for (id, members) in overlay {
        let (Some(members), Some(rec)) = (members.as_object(), records.get(id)) else {
            out.push(DisclosureResult {
                capsule_id: id.clone(),
                member: String::new(),
                status: disclosure::MISMATCH,
            });
            continue;
        };
        for f in disclosure::check_disclosures(rec, members) {
            out.push(DisclosureResult {
                capsule_id: id.clone(),
                member: f.member,
                status: f.code,
            });
        }
    }
    out
}

fn extension_results(
    raw: Option<&Value>,
    records: &BTreeMap<String, &Value>,
) -> Vec<ExtensionResult> {
    let Some(exts) = raw.and_then(Value::as_object) else {
        return vec![];
    };
    exts.iter()
        .map(|(kind, block)| {
            if kind != ENVELOPES_EXTENSION {
                return ExtensionResult {
                    kind: kind.clone(),
                    status: "uninterpreted",
                    integrity_covered: true,
                    envelopes: vec![],
                    findings: vec![],
                };
            }
            let mut envelopes = Vec::new();
            let mut findings = Vec::new();
            match block.as_object() {
                None => findings.push("extension_malformed".into()),
                Some(m) => {
                    for (id, list) in m {
                        if !records.contains_key(id) {
                            findings.push(format!("envelope_record_unknown:{id}"));
                        }
                        let Some(list) = list.as_array() else {
                            findings.push(format!("envelope_list_malformed:{id}"));
                            continue;
                        };
                        for e in list {
                            let raw = e.as_str().and_then(|s| URL_SAFE_NO_PAD.decode(s).ok());
                            match raw {
                                Some(bytes) => {
                                    envelopes.push(envelope::verify_producer_envelope(id, &bytes))
                                }
                                None => findings.push(format!("envelope_not_base64url:{id}")),
                            }
                        }
                    }
                }
            }
            ExtensionResult {
                kind: kind.clone(),
                status: "interpreted",
                integrity_covered: true,
                envelopes,
                findings,
            }
        })
        .collect()
}
