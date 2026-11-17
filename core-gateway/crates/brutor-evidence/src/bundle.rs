// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Evidence bundles — `ai.brutor/evidence-bundle/v1`.
//!
//! A bundle is everything a relying party needs to check a set of records
//! offline: the records and their Signed Statements, an RFC 6962 inclusion
//! proof for each against the newest included tree head, the tree heads
//! themselves (with any witness receipts), and consistency proofs between
//! consecutive heads so the heads are shown to be one append-only history.
//!
//! This module is pure — no database, no clock, no network. The gateway
//! assembles the inputs from its node store and calls [`build`]; the
//! gateway's `/verify/bundle`, `brutor-verify` and the vector corpus all
//! call [`verify`].
//!
//! Two rules make a bundle honest rather than merely well-formed:
//!
//! * **Missing is listed, never dropped.** Every chain parent a record cites
//!   is either in the bundle or named in `manifest.completeness.missing`;
//!   a parent that is neither is an error, and a non-empty `missing` forces
//!   `records_mode: "declared_incomplete"`.
//! * **The digest covers everything but countersignatures.**
//!   `bundle_digest = hex(SHA-256(JCS(bundle minus "countersignatures")))`,
//!   so a later countersignature (an auditor's) never changes the identity
//!   the producer sealed.

use crate::canon::{self, CanonError};
use crate::record::{compute_record_id, is_hex64};
use crate::statement::{self, parse_statement};
use base64::Engine as _;
use brutor_cose::merkle::{self, Hash};
use brutor_cose::sign1::CoseSign1;
use brutor_ctlog::TreeHead;
use ed25519_dalek::VerifyingKey;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use std::collections::{BTreeSet, HashMap, HashSet};

pub const BUNDLE_KIND: &str = "ai.brutor/evidence-bundle";
pub const BUNDLE_VERSION: i64 = 1;
/// Upper bound on records in one bundle. The gateway refuses larger
/// selections with a 413 rather than silently truncating.
pub const MAX_BUNDLE_RECORDS: usize = 10_000;

pub mod selection {
    /// Every record in a window or run — a verifier may check nothing was left out.
    pub const CONTIGUOUS: &str = "contiguous";
    /// A hand-picked list — the verifier is told so.
    pub const PRODUCER_SELECTED: &str = "producer-selected";
}

pub mod records_mode {
    pub const COMPLETE: &str = "complete";
    pub const DECLARED_INCOMPLETE: &str = "declared_incomplete";
}

pub const PAYLOADS_NONE: &str = "none";

#[derive(Debug, thiserror::Error)]
pub enum BundleError {
    #[error("canon: {0}")]
    Canon(#[from] CanonError),
    #[error("statement: {0}")]
    Statement(String),
    #[error("{0}")]
    Invalid(String),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct BundleRecord {
    pub record: Value,
    pub statement_b64: String,
    pub seq: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RecordProof {
    pub record_id: String,
    pub leaf_index: u64,
    pub tree_size: u64,
    pub path: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct BundleReceipt {
    pub kind: String,
    pub receipt_b64: String,
    pub grade: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct BundleHead {
    pub statement_b64: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub transparent_statement_b64: Option<String>,
    #[serde(default)]
    pub receipts: Vec<BundleReceipt>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ConsistencyProof {
    pub from_size: u64,
    pub to_size: u64,
    pub path: Vec<String>,
}

/// Everything [`build`] needs. Proofs are computed by the caller (it owns
/// the node store); `build` only arranges, counts and digests.
#[derive(Debug, Clone, Default)]
pub struct BundleInput {
    pub bundle_id: String,
    pub root_record_id: Option<String>,
    pub selector: Value,
    pub produced_at: String,
    pub issuer: String,
    pub log_id: String,
    pub framework_id: Option<String>,
    pub catalog_version: Option<String>,
    pub closure_depth: u32,
    /// [`selection::CONTIGUOUS`] or [`selection::PRODUCER_SELECTED`].
    pub selection: String,
    pub missing: Vec<String>,
    pub records: Vec<BundleRecord>,
    pub proofs: Vec<RecordProof>,
    pub tree_heads: Vec<BundleHead>,
    pub consistency: Vec<ConsistencyProof>,
    pub prunes: Vec<Value>,
    pub extensions: Map<String, Value>,
}

fn b64(s: &str) -> Result<Vec<u8>, String> {
    base64::engine::general_purpose::STANDARD
        .decode(s)
        .map_err(|e| e.to_string())
}

/// `hex(SHA-256(JCS(bundle minus "countersignatures")))`.
pub fn bundle_digest(bundle: &Value) -> Result<String, CanonError> {
    let mut clone = bundle.clone();
    if let Value::Object(m) = &mut clone {
        m.remove("countersignatures");
    }
    canon::json_digest_hex(&clone)
}

fn statement_kid(raw: &[u8]) -> Option<String> {
    CoseSign1::decode(raw)
        .ok()
        .and_then(|s| s.kid().map(hex::encode))
}

/// Assemble the bundle JSON and its digest.
pub fn build(mut input: BundleInput) -> Result<(Value, String), BundleError> {
    if input.records.len() > MAX_BUNDLE_RECORDS {
        return Err(BundleError::Invalid(format!(
            "{} records exceeds the bundle cap of {MAX_BUNDLE_RECORDS}",
            input.records.len()
        )));
    }
    if input.tree_heads.is_empty() && !input.records.is_empty() {
        return Err(BundleError::Invalid(
            "a bundle with records needs at least one tree head".into(),
        ));
    }
    input.records.sort_by_key(|r| r.seq);
    input.missing.sort();
    input.missing.dedup();
    let mut kids: BTreeSet<String> = BTreeSet::new();
    for r in &input.records {
        let raw = b64(&r.statement_b64).map_err(BundleError::Statement)?;
        kids.insert(
            statement_kid(&raw)
                .ok_or_else(|| BundleError::Statement("record statement has no kid".into()))?,
        );
    }
    for h in &input.tree_heads {
        let raw = b64(&h.statement_b64).map_err(BundleError::Statement)?;
        kids.insert(
            statement_kid(&raw)
                .ok_or_else(|| BundleError::Statement("tree head has no kid".into()))?,
        );
    }
    let mode = if input.missing.is_empty() {
        records_mode::COMPLETE
    } else {
        records_mode::DECLARED_INCOMPLETE
    };
    let mut manifest = json!({
        "kind": BUNDLE_KIND,
        "version": BUNDLE_VERSION,
        "bundle_id": input.bundle_id,
        "selector": input.selector,
        "produced_at": input.produced_at,
        "issuer": input.issuer,
        "log_id": input.log_id,
        "kids": kids.into_iter().collect::<Vec<_>>(),
        "completeness": {
            "closure_depth": input.closure_depth,
            "records_mode": mode,
            "selection": input.selection,
            "payloads_mode": PAYLOADS_NONE,
            "suppressed_fields": [],
            "missing": input.missing,
        },
        "verification": {"self_report": {
            "records": input.records.len(),
            "proofs": input.proofs.len(),
            "heads": input.tree_heads.len(),
        }},
    });
    if let Value::Object(m) = &mut manifest {
        if let Some(r) = input.root_record_id {
            m.insert("root_record_id".into(), json!(r));
        }
        if let Some(f) = input.framework_id {
            m.insert("framework_id".into(), json!(f));
        }
        if let Some(c) = input.catalog_version {
            m.insert("catalog_version".into(), json!(c));
        }
    }
    let to_value =
        |v: serde_json::Result<Value>| v.map_err(|e| BundleError::Invalid(e.to_string()));
    let bundle = json!({
        "manifest": manifest,
        "records": to_value(serde_json::to_value(&input.records))?,
        "proofs": to_value(serde_json::to_value(&input.proofs))?,
        "tree_heads": to_value(serde_json::to_value(&input.tree_heads))?,
        "consistency": to_value(serde_json::to_value(&input.consistency))?,
        "prunes": input.prunes,
        "disclosures": {},
        "extensions": Value::Object(input.extensions),
        "countersignatures": [],
    });
    let digest = bundle_digest(&bundle)?;
    Ok((bundle, digest))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Severity {
    Error,
    Warning,
    Info,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct BundleFinding {
    pub code: &'static str,
    pub severity: Severity,
    pub detail: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub record_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, Default)]
pub struct BundleVerification {
    pub ok: bool,
    pub findings: Vec<BundleFinding>,
    pub bundle_digest: Option<String>,
    pub records_verified: usize,
    pub proofs_verified: usize,
    pub heads_verified: usize,
    pub missing: Vec<String>,
}

/// What the verifier trusts.
#[derive(Debug, Clone, Default)]
pub struct VerifyOptions {
    /// Lowercase-hex raw Ed25519 public keys. `None` = no pin: signatures
    /// are checked under the key each statement names, and the result says
    /// so with a `kid_not_pinned` warning (an offline self-consistency
    /// check, not an attribution).
    pub trusted_kids: Option<HashSet<String>>,
    /// The digest the producer sealed (from its `ai.brutor.bundle`
    /// record); a mismatch is an error.
    pub expected_digest: Option<String>,
}

struct Ctx {
    findings: Vec<BundleFinding>,
}

impl Ctx {
    fn push(&mut self, code: &'static str, sev: Severity, detail: impl Into<String>) {
        self.findings.push(BundleFinding {
            code,
            severity: sev,
            detail: detail.into(),
            record_id: None,
        });
    }
    fn push_rec(
        &mut self,
        code: &'static str,
        sev: Severity,
        rid: &str,
        detail: impl Into<String>,
    ) {
        self.findings.push(BundleFinding {
            code,
            severity: sev,
            detail: detail.into(),
            record_id: Some(rid.to_string()),
        });
    }
}

fn parse_hashes(path: &[String]) -> Option<Vec<Hash>> {
    path.iter()
        .map(|h| hex::decode(h).ok().and_then(|b| b.try_into().ok()))
        .collect()
}

/// Resolve the key a statement names, subject to the trust set.
fn key_for(kid: &[u8], opts: &VerifyOptions) -> Result<VerifyingKey, &'static str> {
    let kid_hex = hex::encode(kid);
    if let Some(trusted) = &opts.trusted_kids
        && !trusted.contains(&kid_hex)
    {
        return Err("untrusted_kid");
    }
    let arr: [u8; 32] = kid.try_into().map_err(|_| "bad_kid")?;
    VerifyingKey::from_bytes(&arr).map_err(|_| "bad_kid")
}

/// Verify a bundle. Fixed order, structured result, never panics.
pub fn verify(bundle: &Value, opts: &VerifyOptions) -> BundleVerification {
    let mut out = BundleVerification::default();
    let mut cx = Ctx { findings: vec![] };
    let Some(obj) = bundle.as_object() else {
        cx.push(
            "bundle_not_object",
            Severity::Error,
            "bundle is not a JSON object",
        );
        out.findings = cx.findings;
        return out;
    };

    // ---- digest ------------------------------------------------------------
    match bundle_digest(bundle) {
        Ok(d) => {
            if let Some(exp) = &opts.expected_digest
                && !exp.eq_ignore_ascii_case(&d)
            {
                cx.push(
                    "bundle_digest_mismatch",
                    Severity::Error,
                    format!("computed {d}, expected {exp}"),
                );
            }
            out.bundle_digest = Some(d);
        }
        Err(e) => cx.push("digest_uncomputable", Severity::Error, e.to_string()),
    }

    // ---- manifest ----------------------------------------------------------
    let manifest = obj.get("manifest").and_then(Value::as_object);
    let Some(manifest) = manifest else {
        cx.push("manifest_invalid", Severity::Error, "manifest missing");
        out.findings = cx.findings;
        return out;
    };
    if manifest.get("kind").and_then(Value::as_str) != Some(BUNDLE_KIND)
        || manifest.get("version").and_then(Value::as_i64) != Some(BUNDLE_VERSION)
    {
        cx.push(
            "manifest_invalid",
            Severity::Error,
            format!("expected kind {BUNDLE_KIND} version {BUNDLE_VERSION}"),
        );
    }
    let log_id = manifest
        .get("log_id")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let completeness = manifest.get("completeness").cloned().unwrap_or(Value::Null);
    let missing: Vec<String> = completeness
        .get("missing")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|v| v.as_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default();
    out.missing = missing.clone();
    let declared_mode = completeness
        .get("records_mode")
        .and_then(Value::as_str)
        .unwrap_or("");
    let want_mode = if missing.is_empty() {
        records_mode::COMPLETE
    } else {
        records_mode::DECLARED_INCOMPLETE
    };
    if declared_mode != want_mode {
        cx.push(
            "records_mode_mismatch",
            Severity::Error,
            format!(
                "records_mode {declared_mode:?} but missing has {} ids",
                missing.len()
            ),
        );
    }
    if completeness.get("payloads_mode").and_then(Value::as_str) != Some(PAYLOADS_NONE) {
        cx.push(
            "payloads_mode_unsupported",
            Severity::Error,
            "v1 bundles carry no payloads",
        );
    }
    if opts.trusted_kids.is_none() {
        cx.push(
            "kid_not_pinned",
            Severity::Warning,
            "no trusted keys supplied; signatures checked under the keys the statements name",
        );
    }

    let records: Vec<BundleRecord> = match obj
        .get("records")
        .cloned()
        .map(serde_json::from_value::<Vec<BundleRecord>>)
    {
        Some(Ok(r)) => r,
        other => {
            cx.push(
                "structure_invalid",
                Severity::Error,
                format!(
                    "records: {}",
                    other
                        .map(|r| r.err().map(|e| e.to_string()).unwrap_or_default())
                        .unwrap_or_else(|| "absent".into())
                ),
            );
            vec![]
        }
    };
    let proofs: Vec<RecordProof> = parse_array(obj, "proofs", &mut cx);
    let heads: Vec<BundleHead> = parse_array(obj, "tree_heads", &mut cx);
    let consistency: Vec<ConsistencyProof> = parse_array(obj, "consistency", &mut cx);

    // ---- tree heads --------------------------------------------------------
    let mut verified_heads: Vec<TreeHead> = Vec::new();
    if heads.is_empty() && !records.is_empty() {
        cx.push("no_tree_heads", Severity::Error, "records need a tree head");
    }
    for (i, h) in heads.iter().enumerate() {
        let raw = match b64(&h.statement_b64) {
            Ok(r) => r,
            Err(e) => {
                cx.push("head_invalid", Severity::Error, format!("head {i}: {e}"));
                continue;
            }
        };
        let key = match CoseSign1::decode(&raw)
            .ok()
            .and_then(|s| s.kid().map(<[u8]>::to_vec))
        {
            Some(kid) => key_for(&kid, opts),
            None => Err("bad_kid"),
        };
        let key = match key {
            Ok(k) => k,
            Err(code) => {
                cx.push(
                    if code == "untrusted_kid" {
                        "untrusted_kid"
                    } else {
                        "head_invalid"
                    },
                    Severity::Error,
                    format!("head {i}: {code}"),
                );
                continue;
            }
        };
        match TreeHead::verify(&raw, &key) {
            Ok((head, signed)) => {
                if head.log_id != log_id {
                    cx.push(
                        "head_log_mismatch",
                        Severity::Error,
                        format!("head {i} is for log {}", head.log_id),
                    );
                    continue;
                }
                if let Some(ts) = &h.transparent_statement_b64 {
                    let same = b64(ts)
                        .ok()
                        .and_then(|t| CoseSign1::decode(&t).ok())
                        .map(|t| {
                            t.protected_bytes == signed.protected_bytes
                                && t.payload == signed.payload
                                && t.signature == signed.signature
                        })
                        .unwrap_or(false);
                    if !same {
                        cx.push(
                            "transparent_statement_mismatch",
                            Severity::Error,
                            format!("head {i}: transparent statement is not this head"),
                        );
                        continue;
                    }
                }
                if let Some(prev) = verified_heads.last()
                    && head.tree_size <= prev.tree_size
                {
                    cx.push(
                        "heads_not_ascending",
                        Severity::Error,
                        format!("head {i} size {} after {}", head.tree_size, prev.tree_size),
                    );
                    continue;
                }
                verified_heads.push(head);
            }
            Err(e) => cx.push("head_invalid", Severity::Error, format!("head {i}: {e}")),
        }
    }
    let all_heads_ok = verified_heads.len() == heads.len();
    out.heads_verified = verified_heads.len();

    // ---- consistency between consecutive heads -----------------------------
    for w in verified_heads.windows(2) {
        let (a, b) = (&w[0], &w[1]);
        let proof = consistency
            .iter()
            .find(|c| c.from_size == a.tree_size && c.to_size == b.tree_size);
        let Some(proof) = proof else {
            cx.push(
                "consistency_missing",
                Severity::Error,
                format!("{} -> {}", a.tree_size, b.tree_size),
            );
            continue;
        };
        let ok = parse_hashes(&proof.path)
            .map(|p| {
                merkle::verify_consistency(a.tree_size, &a.root, b.tree_size, &b.root, &p).is_ok()
            })
            .unwrap_or(false);
        if !ok {
            cx.push(
                "consistency_invalid",
                Severity::Error,
                format!("{} -> {}", a.tree_size, b.tree_size),
            );
        }
    }
    let last_head = if all_heads_ok {
        verified_heads.last()
    } else {
        None
    };

    // ---- records -------------------------------------------------------------
    let mut ids: HashSet<String> = HashSet::new();
    let mut leaves: HashMap<String, (Hash, u64)> = HashMap::new();
    for r in &records {
        let rid = r
            .record
            .get("record_id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        if !ids.insert(rid.clone()) {
            cx.push_rec(
                "duplicate_record",
                Severity::Error,
                &rid,
                "record appears twice",
            );
            continue;
        }
        let sem = crate::verify::verify(&r.record, None);
        for f in sem
            .findings
            .iter()
            .filter(|f| f.severity == crate::verify::Severity::Error)
        {
            cx.push_rec(
                "record_semantic_error",
                Severity::Error,
                &rid,
                format!("{}: {}", f.code, f.detail),
            );
        }
        let raw = match b64(&r.statement_b64) {
            Ok(raw) => raw,
            Err(e) => {
                cx.push_rec("record_statement_invalid", Severity::Error, &rid, e);
                continue;
            }
        };
        let key = match parse_statement(&raw) {
            Ok((info, _)) => key_for(&info.kid, opts),
            Err(e) => {
                cx.push_rec(
                    "record_statement_invalid",
                    Severity::Error,
                    &rid,
                    e.to_string(),
                );
                continue;
            }
        };
        let key = match key {
            Ok(k) => k,
            Err(code) => {
                cx.push_rec(
                    if code == "untrusted_kid" {
                        "untrusted_kid"
                    } else {
                        "record_statement_invalid"
                    },
                    Severity::Error,
                    &rid,
                    code,
                );
                continue;
            }
        };
        match statement::verify_record_statement(&raw, &r.record, &key) {
            Ok(_) => {
                if sem.ok && compute_record_id(&r.record).ok().as_deref() == Some(rid.as_str()) {
                    out.records_verified += 1;
                }
                leaves.insert(rid.clone(), (merkle::leaf_hash(&raw), r.seq));
            }
            Err(statement::StatementError::PayloadMismatch) => cx.push_rec(
                "statement_payload_mismatch",
                Severity::Error,
                &rid,
                "statement does not commit to this record",
            ),
            Err(e) => cx.push_rec(
                "record_statement_invalid",
                Severity::Error,
                &rid,
                e.to_string(),
            ),
        }
    }

    // ---- inclusion proofs against the newest head ------------------------------
    let mut proven: HashSet<&str> = HashSet::new();
    for p in &proofs {
        let Some((leaf, seq)) = leaves.get(&p.record_id) else {
            if !ids.contains(&p.record_id) {
                cx.push_rec(
                    "proof_for_unknown_record",
                    Severity::Error,
                    &p.record_id,
                    "proof names a record not in the bundle",
                );
            }
            continue;
        };
        let Some(head) = last_head else {
            continue;
        };
        if p.tree_size != head.tree_size {
            cx.push_rec(
                "proof_wrong_head",
                Severity::Error,
                &p.record_id,
                format!(
                    "proof size {} != newest head {}",
                    p.tree_size, head.tree_size
                ),
            );
            continue;
        }
        if p.leaf_index != *seq {
            cx.push_rec(
                "leaf_index_mismatch",
                Severity::Error,
                &p.record_id,
                format!("leaf_index {} != seq {seq}", p.leaf_index),
            );
            continue;
        }
        let ok = parse_hashes(&p.path)
            .map(|path| {
                merkle::verify_inclusion(leaf, p.leaf_index, p.tree_size, &path, &head.root).is_ok()
            })
            .unwrap_or(false);
        if ok {
            proven.insert(p.record_id.as_str());
            out.proofs_verified += 1;
        } else {
            cx.push_rec(
                "proof_invalid",
                Severity::Error,
                &p.record_id,
                "inclusion path does not reach the head root",
            );
        }
    }
    if last_head.is_some() {
        for rid in leaves.keys() {
            if !proven.contains(rid.as_str())
                && !cx
                    .findings
                    .iter()
                    .any(|f| f.record_id.as_deref() == Some(rid.as_str()))
            {
                cx.push_rec(
                    "proof_missing",
                    Severity::Error,
                    rid,
                    "record has no inclusion proof",
                );
            }
        }
    }

    // ---- closure: missing is listed, never dropped -------------------------------
    let missing_set: HashSet<&str> = missing.iter().map(String::as_str).collect();
    for r in &records {
        let Some(parent) = r
            .record
            .get("chain")
            .and_then(|c| c.get("parent_record_id"))
            .and_then(Value::as_str)
        else {
            continue;
        };
        if !is_hex64(parent) {
            continue; // the semantic verifier already reported it
        }
        if !ids.contains(parent) && !missing_set.contains(parent) {
            let rid = r
                .record
                .get("record_id")
                .and_then(Value::as_str)
                .unwrap_or_default();
            cx.push_rec(
                "closure_parent_dropped",
                Severity::Error,
                rid,
                format!("parent {parent} is neither included nor listed as missing"),
            );
        }
    }
    for m in &missing {
        if ids.contains(m) {
            cx.push(
                "missing_listed_but_present",
                Severity::Warning,
                format!("{m} is listed as missing but included"),
            );
        }
    }

    // ---- selector: an explicit id list admits only its ids and their closure --
    if let Some(selected) = manifest
        .get("selector")
        .and_then(|s| s.get("record_ids"))
        .and_then(Value::as_array)
    {
        let depth = completeness
            .get("closure_depth")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        for rid in records_outside_selector(&records, selected, depth) {
            cx.push_rec(
                "record_not_in_selector",
                Severity::Error,
                &rid,
                "record is neither selected by manifest.selector.record_ids nor a chain parent within closure_depth",
            );
        }
    }

    // ---- self report -------------------------------------------------------------
    let sr = manifest
        .get("verification")
        .and_then(|v| v.get("self_report"))
        .cloned()
        .unwrap_or(Value::Null);
    let claimed = |k: &str| sr.get(k).and_then(Value::as_u64);
    if claimed("records") != Some(records.len() as u64)
        || claimed("proofs") != Some(proofs.len() as u64)
        || claimed("heads") != Some(heads.len() as u64)
    {
        cx.push(
            "self_report_mismatch",
            Severity::Warning,
            "manifest.verification.self_report does not match the contents",
        );
    }

    out.ok = !cx.findings.iter().any(|f| f.severity == Severity::Error);
    out.findings = cx.findings;
    out
}

/// Record ids in `records` (bundle order) that an explicit `record_ids`
/// selector does not admit: admitted = the selected ids plus every chain
/// ancestor reachable from them within `closure_depth` hops.
fn records_outside_selector(
    records: &[BundleRecord],
    selected: &[Value],
    closure_depth: u64,
) -> Vec<String> {
    let parent_of: HashMap<&str, &str> = records
        .iter()
        .filter_map(|r| {
            Some((
                r.record.get("record_id").and_then(Value::as_str)?,
                r.record
                    .get("chain")
                    .and_then(|c| c.get("parent_record_id"))
                    .and_then(Value::as_str)?,
            ))
        })
        .collect();
    let mut admitted: HashSet<&str> = HashSet::new();
    for id in selected.iter().filter_map(Value::as_str) {
        admitted.insert(id);
        let mut cur = id;
        for _ in 0..closure_depth {
            let Some(p) = parent_of.get(cur) else { break };
            admitted.insert(p);
            cur = p;
        }
    }
    records
        .iter()
        .filter_map(|r| r.record.get("record_id").and_then(Value::as_str))
        .filter(|id| !admitted.contains(id))
        .map(str::to_owned)
        .collect()
}

fn parse_array<T: serde::de::DeserializeOwned>(
    obj: &Map<String, Value>,
    key: &'static str,
    cx: &mut Ctx,
) -> Vec<T> {
    match obj.get(key) {
        None => {
            cx.push(
                "structure_invalid",
                Severity::Error,
                format!("{key}: absent"),
            );
            vec![]
        }
        Some(v) => serde_json::from_value(v.clone()).unwrap_or_else(|e| {
            cx.push("structure_invalid", Severity::Error, format!("{key}: {e}"));
            vec![]
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::record::{Chain, Disposition, RecordBuilder, relation, verdict};
    use crate::statement::seal_record;
    use brutor_ctlog::{Log, MemoryStore};
    use ed25519_dalek::SigningKey;

    fn enc(b: &[u8]) -> String {
        base64::engine::general_purpose::STANDARD.encode(b)
    }

    struct Fixture {
        key: SigningKey,
        input: BundleInput,
        records: Vec<(Value, Vec<u8>)>,
    }

    fn record(i: usize, parent: Option<&str>) -> Value {
        let mut b = RecordBuilder::new(
            format!("mcp/log-{i}"),
            "tenant-a",
            format!("2026-09-19T10:00:{:02}.000Z", i % 60),
        );
        b.verdict = verdict::DENIED.into();
        b.disposition = Some(Disposition {
            decision: "reject".into(),
            approver: "policy".into(),
            human_disposed: false,
            authority: None,
            reason_digest: None,
        });
        b.chain = parent.map(|p| Chain {
            parent_record_id: p.into(),
            relation: relation::SUPERSEDES.into(),
        });
        b.seal().unwrap().0
    }

    /// Five records in a log of seven leaves (two heads' stamps are
    /// leaves too), heads at sizes 3 and 7, record 4 chained to record 1.
    fn fixture() -> Fixture {
        let key = SigningKey::from_bytes(&[21u8; 32]);
        let log_id = "log-bundle-test".to_string();
        let mut store = MemoryStore::default();
        let mut log = Log::open(&mut store, 0);
        let mut records: Vec<(Value, Vec<u8>, u64)> = Vec::new();
        let mut heads: Vec<(TreeHead, Vec<u8>)> = Vec::new();
        fn append_record(
            log: &mut Log<'_, MemoryStore>,
            records: &mut Vec<(Value, Vec<u8>, u64)>,
            key: &SigningKey,
            parent: Option<&str>,
        ) {
            let r = record(records.len(), parent);
            let st = seal_record(&r, "urn:brutor:deployment:test", "tenant:tenant-a", key).unwrap();
            let seq = log.append(merkle::leaf_hash(&st)).unwrap();
            records.push((r, st, seq));
        }
        append_record(&mut log, &mut records, &key, None);
        append_record(&mut log, &mut records, &key, None);
        append_record(&mut log, &mut records, &key, None);
        let h1 = TreeHead {
            log_id: log_id.clone(),
            tree_size: 3,
            root: log.root().unwrap(),
            prev_size: 0,
            prev_root: None,
            issued_at: "2026-09-19T10:00:00.000Z".into(),
            consistency_proof: vec![],
        };
        let h1s = h1.sign(&key).unwrap();
        log.append(merkle::leaf_hash(&h1s)).unwrap();
        heads.push((h1.clone(), h1s));
        let parent = records[1].0["record_id"].as_str().unwrap().to_string();
        append_record(&mut log, &mut records, &key, None);
        append_record(&mut log, &mut records, &key, Some(&parent));
        let size = log.size();
        let h2 = TreeHead {
            log_id: log_id.clone(),
            tree_size: size,
            root: log.root().unwrap(),
            prev_size: 3,
            prev_root: Some(h1.root),
            issued_at: "2026-09-19T10:15:00.000Z".into(),
            consistency_proof: log.consistency_proof(3, size).unwrap(),
        };
        let h2s = h2.sign(&key).unwrap();
        heads.push((h2.clone(), h2s));
        let proofs = records
            .iter()
            .map(|(r, _, seq)| RecordProof {
                record_id: r["record_id"].as_str().unwrap().into(),
                leaf_index: *seq,
                tree_size: size,
                path: log
                    .inclusion_path(*seq, size)
                    .unwrap()
                    .iter()
                    .map(hex::encode)
                    .collect(),
            })
            .collect();
        let consistency = vec![ConsistencyProof {
            from_size: 3,
            to_size: size,
            path: log
                .consistency_proof(3, size)
                .unwrap()
                .iter()
                .map(hex::encode)
                .collect(),
        }];
        let input = BundleInput {
            bundle_id: "bdl-1".into(),
            root_record_id: None,
            selector: json!({"root_task_id": "run-1"}),
            produced_at: "2026-09-19T11:00:00.000Z".into(),
            issuer: "urn:brutor:deployment:test".into(),
            log_id,
            framework_id: Some("eu-ai-act".into()),
            catalog_version: Some("2026-09".into()),
            closure_depth: 1,
            selection: selection::CONTIGUOUS.into(),
            missing: vec![],
            records: records
                .iter()
                .map(|(r, st, seq)| BundleRecord {
                    record: r.clone(),
                    statement_b64: enc(st),
                    seq: *seq,
                })
                .collect(),
            proofs,
            tree_heads: heads
                .iter()
                .map(|(_, s)| BundleHead {
                    statement_b64: enc(s),
                    transparent_statement_b64: None,
                    receipts: vec![],
                })
                .collect(),
            consistency,
            prunes: vec![],
            extensions: Map::new(),
        };
        Fixture {
            key,
            input,
            records: records.into_iter().map(|(r, s, _)| (r, s)).collect(),
        }
    }

    fn trusted(f: &Fixture) -> VerifyOptions {
        VerifyOptions {
            trusted_kids: Some(
                [hex::encode(f.key.verifying_key().to_bytes())]
                    .into_iter()
                    .collect(),
            ),
            expected_digest: None,
        }
    }

    fn codes(v: &BundleVerification) -> Vec<&'static str> {
        v.findings
            .iter()
            .filter(|f| f.severity == Severity::Error)
            .map(|f| f.code)
            .collect()
    }

    #[test]
    fn built_bundle_verifies() {
        let f = fixture();
        let (bundle, digest) = build(f.input.clone()).unwrap();
        let mut opts = trusted(&f);
        opts.expected_digest = Some(digest.clone());
        let v = verify(&bundle, &opts);
        assert!(v.ok, "{:?}", v.findings);
        assert_eq!(v.bundle_digest.as_deref(), Some(digest.as_str()));
        assert_eq!(v.records_verified, 5);
        assert_eq!(v.proofs_verified, 5);
        assert_eq!(v.heads_verified, 2);
        assert_eq!(
            bundle["manifest"]["completeness"]["records_mode"],
            "complete"
        );
        assert_eq!(bundle["manifest"]["kids"].as_array().unwrap().len(), 1);
        assert_eq!(f.records.len(), 5);
    }

    #[test]
    fn countersignatures_do_not_change_the_digest() {
        let f = fixture();
        let (mut bundle, digest) = build(f.input.clone()).unwrap();
        bundle["countersignatures"] = json!([{"by": "auditor", "sig": "…"}]);
        assert_eq!(bundle_digest(&bundle).unwrap(), digest);
        bundle["extensions"] = json!({"x": 1});
        assert_ne!(bundle_digest(&bundle).unwrap(), digest);
    }

    #[test]
    fn unpinned_verification_warns_but_passes() {
        let f = fixture();
        let (bundle, _) = build(f.input.clone()).unwrap();
        let v = verify(&bundle, &VerifyOptions::default());
        assert!(v.ok);
        assert!(v.findings.iter().any(|x| x.code == "kid_not_pinned"));
        // An unrelated trust set rejects every statement.
        let other = VerifyOptions {
            trusted_kids: Some(["00".repeat(32)].into_iter().collect()),
            expected_digest: None,
        };
        let v = verify(&bundle, &other);
        assert!(!v.ok);
        assert!(codes(&v).contains(&"untrusted_kid"));
    }

    #[test]
    fn flipped_record_byte_fails() {
        let f = fixture();
        let (mut bundle, _) = build(f.input.clone()).unwrap();
        bundle["records"][2]["record"]["verdict"] = json!("executed");
        let v = verify(&bundle, &trusted(&f));
        assert!(!v.ok);
        let c = codes(&v);
        assert!(c.contains(&"record_semantic_error"), "{c:?}");
    }

    #[test]
    fn wrong_statement_fails() {
        let f = fixture();
        let (mut bundle, _) = build(f.input.clone()).unwrap();
        // Record 0 presented with record 1's statement.
        let other = bundle["records"][1]["statement_b64"].clone();
        bundle["records"][0]["statement_b64"] = other;
        let v = verify(&bundle, &trusted(&f));
        assert!(!v.ok);
        assert!(codes(&v).contains(&"statement_payload_mismatch"));
    }

    #[test]
    fn bad_proof_path_fails() {
        let f = fixture();
        let (mut bundle, _) = build(f.input.clone()).unwrap();
        let p = bundle["proofs"][1]["path"][0].as_str().unwrap().to_string();
        let mut bytes = hex::decode(p).unwrap();
        bytes[0] ^= 1;
        bundle["proofs"][1]["path"][0] = json!(hex::encode(bytes));
        let v = verify(&bundle, &trusted(&f));
        assert!(!v.ok);
        assert_eq!(codes(&v), vec!["proof_invalid"]);
        // A dropped proof is named too.
        let (mut bundle, _) = build(f.input.clone()).unwrap();
        bundle["proofs"].as_array_mut().unwrap().remove(0);
        let v = verify(&bundle, &trusted(&f));
        assert!(codes(&v).contains(&"proof_missing"));
        // A proof against the wrong leaf index.
        let (mut bundle, _) = build(f.input.clone()).unwrap();
        bundle["proofs"][0]["leaf_index"] = json!(1);
        let v = verify(&bundle, &trusted(&f));
        assert!(codes(&v).contains(&"leaf_index_mismatch"));
    }

    #[test]
    fn wrong_head_signature_fails() {
        let f = fixture();
        let (mut bundle, _) = build(f.input.clone()).unwrap();
        let raw = b64(bundle["tree_heads"][1]["statement_b64"].as_str().unwrap()).unwrap();
        let mut raw = raw;
        let n = raw.len();
        raw[n - 1] ^= 1;
        bundle["tree_heads"][1]["statement_b64"] = json!(enc(&raw));
        let v = verify(&bundle, &trusted(&f));
        assert!(!v.ok);
        assert!(codes(&v).contains(&"head_invalid"));
        // A head re-signed by a stranger is untrusted.
        let (mut bundle, _) = build(f.input.clone()).unwrap();
        let stranger = SigningKey::from_bytes(&[99u8; 32]);
        let (head, _) = TreeHead::verify(
            &b64(bundle["tree_heads"][1]["statement_b64"].as_str().unwrap()).unwrap(),
            &f.key.verifying_key(),
        )
        .unwrap();
        bundle["tree_heads"][1]["statement_b64"] = json!(enc(&head.sign(&stranger).unwrap()));
        let v = verify(&bundle, &trusted(&f));
        assert!(codes(&v).contains(&"untrusted_kid"));
    }

    #[test]
    fn broken_consistency_fails() {
        let f = fixture();
        let (mut bundle, _) = build(f.input.clone()).unwrap();
        bundle["consistency"][0]["path"][0] = json!("00".repeat(32));
        let v = verify(&bundle, &trusted(&f));
        assert!(!v.ok);
        assert_eq!(codes(&v), vec!["consistency_invalid"]);
        let (mut bundle, _) = build(f.input.clone()).unwrap();
        bundle["consistency"] = json!([]);
        let v = verify(&bundle, &trusted(&f));
        assert_eq!(codes(&v), vec!["consistency_missing"]);
    }

    #[test]
    fn digest_mismatch_fails() {
        let f = fixture();
        let (bundle, digest) = build(f.input.clone()).unwrap();
        let mut opts = trusted(&f);
        opts.expected_digest = Some("ab".repeat(32));
        let v = verify(&bundle, &opts);
        assert!(!v.ok);
        assert_eq!(codes(&v), vec!["bundle_digest_mismatch"]);
        // Any edit after sealing moves the digest.
        let mut edited = bundle.clone();
        edited["manifest"]["issuer"] = json!("someone else");
        opts.expected_digest = Some(digest);
        assert!(!verify(&edited, &opts).ok);
    }

    #[test]
    fn missing_parent_must_be_listed() {
        let f = fixture();
        // Drop record 1 (the parent of record 4) and its proof.
        let mut input = f.input.clone();
        let parent = input.records[1].record["record_id"]
            .as_str()
            .unwrap()
            .to_string();
        input.records.remove(1);
        input.proofs.retain(|p| p.record_id != parent);
        // Not listed ⇒ error.
        let (bundle, _) = build(input.clone()).unwrap();
        let v = verify(&bundle, &trusted(&f));
        assert!(!v.ok);
        assert_eq!(codes(&v), vec!["closure_parent_dropped"]);
        // Listed ⇒ ok, and the manifest declares itself incomplete.
        input.missing = vec![parent.clone()];
        let (bundle, _) = build(input).unwrap();
        assert_eq!(
            bundle["manifest"]["completeness"]["records_mode"],
            "declared_incomplete"
        );
        let v = verify(&bundle, &trusted(&f));
        assert!(v.ok, "{:?}", v.findings);
        assert_eq!(v.missing, vec![parent]);
        // Lying about the mode is caught.
        let mut lying = bundle.clone();
        lying["manifest"]["completeness"]["records_mode"] = json!("complete");
        assert!(codes(&verify(&lying, &trusted(&f))).contains(&"records_mode_mismatch"));
    }

    #[test]
    fn explicit_selector_admits_only_its_ids_and_their_closure() {
        let f = fixture();
        let mut input = f.input.clone();
        let child = input.records[4].record["record_id"]
            .as_str()
            .unwrap()
            .to_string();
        let parent = input.records[1].record["record_id"]
            .as_str()
            .unwrap()
            .to_string();
        input.selector = json!({"record_ids": [child]});
        input.selection = selection::PRODUCER_SELECTED.into();
        input.closure_depth = 1;
        input.records.retain(|r| {
            [child.as_str(), parent.as_str()].contains(&r.record["record_id"].as_str().unwrap())
        });
        input
            .proofs
            .retain(|p| p.record_id == child || p.record_id == parent);
        let (bundle, _) = build(input.clone()).unwrap();
        let v = verify(&bundle, &trusted(&f));
        assert!(v.ok, "{:?}", v.findings);
        // The parent is admitted only through the closure.
        input.closure_depth = 0;
        input.missing = vec![];
        let (bundle, _) = build(input.clone()).unwrap();
        assert_eq!(
            codes(&verify(&bundle, &trusted(&f))),
            vec!["record_not_in_selector"]
        );
        // A record nobody selected is named.
        input.closure_depth = 1;
        input.records.push(f.input.records[0].clone());
        input.proofs.push(f.input.proofs[0].clone());
        let (bundle, _) = build(input).unwrap();
        let v = verify(&bundle, &trusted(&f));
        assert_eq!(codes(&v), vec!["record_not_in_selector"]);
        assert_eq!(
            v.findings[0].record_id.as_deref(),
            f.input.records[0].record["record_id"].as_str()
        );
    }

    #[test]
    fn garbage_never_panics() {
        for v in [
            json!(null),
            json!([]),
            json!({"manifest": 1}),
            json!({"manifest": {"kind": BUNDLE_KIND, "version": 1}, "records": "x"}),
        ] {
            assert!(!verify(&v, &VerifyOptions::default()).ok);
        }
    }
}
