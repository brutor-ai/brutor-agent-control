// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The five entry points. Each composes checks the producing crates
//! already implement and reports them as [`Report`]s.

use crate::codes::*;
use crate::keys::{KidStatus, TrustedKeys};
use crate::receipt::{self, KIND_RFC3161, KIND_SCITT};
use crate::report::{Finding, Outcome, Report, Severity, short};
use base64::Engine as _;
use brutor_cose::CoseError;
use brutor_cose::cbor::as_text;
use brutor_cose::merkle::{self, Hash};
use brutor_cose::sign1::{CoseSign1, cwt, label};
use brutor_ctlog::{LogError, TREE_HEAD_CONTENT_TYPE, TreeHead};
use brutor_evidence::record::compute_record_id;
use brutor_evidence::statement::{RECORD_CONTENT_TYPE, parse_statement};
use ed25519_dalek::VerifyingKey;
use serde_json::{Value, json};

fn b64(s: &str) -> Result<Vec<u8>, String> {
    let t: String = s.chars().filter(|c| !c.is_whitespace()).collect();
    base64::engine::general_purpose::STANDARD
        .decode(t)
        .map_err(|e| e.to_string())
}

/// Resolve the key a statement names, subject to the trust set. Pushes the
/// trust finding (`untrusted_kid`, `kid_not_pinned`, `kid_retired`,
/// `kid_invalid`) and returns the key only when the signature may be
/// checked under it.
fn resolve_key(
    kid: Option<&[u8]>,
    keys: &TrustedKeys,
    r: &mut Report,
    what: &str,
) -> Option<VerifyingKey> {
    let Some(kid) = kid else {
        r.find(
            KID_INVALID,
            Severity::Error,
            format!("{what} has no kid (4)"),
        );
        return None;
    };
    let kid_hex = hex::encode(kid);
    let Some(key) = <[u8; 32]>::try_from(kid)
        .ok()
        .and_then(|a| VerifyingKey::from_bytes(&a).ok())
    else {
        r.find(
            KID_INVALID,
            Severity::Error,
            format!("{what} kid is not a 32-byte Ed25519 public key"),
        );
        return None;
    };
    match keys.status(&kid_hex) {
        KidStatus::Unpinned => {
            if !r.findings.iter().any(|f| f.code == KID_NOT_PINNED) {
                r.find(
                    KID_NOT_PINNED,
                    Severity::Warning,
                    "no trusted keys supplied; signatures are checked under the key each statement names",
                );
            }
            Some(key)
        }
        KidStatus::Untrusted => {
            r.find(
                UNTRUSTED_KID,
                Severity::Error,
                format!("{what} kid {kid_hex} is not in the trusted key set"),
            );
            None
        }
        KidStatus::Trusted { retired } => {
            if retired {
                r.find(
                    KID_RETIRED,
                    Severity::Info,
                    format!("{what} kid {kid_hex} is listed as retired"),
                );
            }
            Some(key)
        }
    }
}

fn key_phrase(keys: &TrustedKeys, kid: &str) -> String {
    if keys.is_pinned() {
        format!("valid under trusted kid {}", short(kid))
    } else {
        format!("valid under the kid the statement names ({})", short(kid))
    }
}

// ---------------------------------------------------------------------------
// Statements
// ---------------------------------------------------------------------------

/// The statement layer shared by [`verify_record`] and [`verify_statement`].
fn statement_layer(raw: &[u8], record: Option<&Value>, keys: &TrustedKeys, r: &mut Report) {
    let s = match CoseSign1::decode(raw) {
        Ok(s) => s,
        Err(e) => {
            r.find(
                STATEMENT_MALFORMED,
                Severity::Error,
                format!("not a COSE_Sign1: {e}"),
            );
            r.check("statement", Outcome::Failed, "not a tagged COSE_Sign1");
            return;
        }
    };
    let kid = s.kid().map(hex::encode).unwrap_or_default();
    let content_type = s
        .content_type()
        .or_else(|| {
            s.protected_get(label::PREIMAGE_CONTENT_TYPE)
                .and_then(as_text)
        })
        .map(str::to_owned);
    r.fact("kid", kid.clone());
    r.fact("alg", s.alg());
    r.fact(
        "iss",
        s.cwt_claim(cwt::ISS).and_then(as_text).map(str::to_owned),
    );
    r.fact(
        "sub",
        s.cwt_claim(cwt::SUB).and_then(as_text).map(str::to_owned),
    );
    r.fact("content_type", content_type.clone());

    if content_type.as_deref() == Some(TREE_HEAD_CONTENT_TYPE) && record.is_none() {
        if let Some(h) = head_layer(raw, keys, r, "tree head") {
            head_facts(r, &h);
        }
        return;
    }
    let is_record = content_type.as_deref() == Some(RECORD_CONTENT_TYPE) || record.is_some();
    if is_record {
        let info = match parse_statement(raw) {
            Ok((info, _)) => info,
            Err(e) => {
                r.find(STATEMENT_MALFORMED, Severity::Error, e.to_string());
                r.check(
                    "statement",
                    Outcome::Failed,
                    "not an action-record statement in hash-envelope mode",
                );
                return;
            }
        };
        r.fact("statement_record_id", info.record_id.clone());
        r.check(
            "statement",
            Outcome::Ok,
            "COSE_Sign1, hash-envelope mode (258 = SHA-256, 259 = action-record+json, no label 3)",
        );
        match resolve_key(s.kid(), keys, r, "statement") {
            None => r.check("signature", Outcome::Skipped, "kid not accepted"),
            Some(key) => match s.verify_ed25519(&key, None) {
                Ok(()) => r.check("signature", Outcome::Ok, key_phrase(keys, &kid)),
                Err(e) => {
                    r.find(SIGNATURE_INVALID, Severity::Error, e.to_string());
                    r.check(
                        "signature",
                        Outcome::Failed,
                        "does not verify under the named key",
                    );
                }
            },
        }
        if let Some(rec) = record {
            match compute_record_id(rec) {
                Err(e) => r.check(
                    "payload binding",
                    Outcome::Skipped,
                    format!("record identity is not computable ({e})"),
                ),
                Ok(id) if id == info.record_id => r.check(
                    "payload binding",
                    Outcome::Ok,
                    format!("statement payload equals the record's recomputed record_id {id}"),
                ),
                Ok(id) => {
                    r.find(
                        STATEMENT_PAYLOAD_MISMATCH,
                        Severity::Error,
                        format!(
                            "statement commits to {}, the record recomputes to {id}",
                            info.record_id
                        ),
                    );
                    r.check(
                        "payload binding",
                        Outcome::Failed,
                        "statement payload is not this record's identity",
                    );
                }
            }
        }
        return;
    }
    // Any other COSE_Sign1: signature only.
    r.check(
        "statement",
        Outcome::Ok,
        format!(
            "COSE_Sign1, content type {}",
            content_type.as_deref().unwrap_or("(none)")
        ),
    );
    match resolve_key(s.kid(), keys, r, "statement") {
        None => r.check("signature", Outcome::Skipped, "kid not accepted"),
        Some(key) => match s.verify_ed25519(&key, None) {
            Ok(()) => r.check("signature", Outcome::Ok, key_phrase(keys, &kid)),
            Err(e) => {
                r.find(SIGNATURE_INVALID, Severity::Error, e.to_string());
                r.check(
                    "signature",
                    Outcome::Failed,
                    "does not verify under the named key",
                );
            }
        },
    }
}

/// Verify one Signed Statement (record statement in hash-envelope mode,
/// tree head in full-content mode, or any Ed25519 COSE_Sign1), and — when
/// `record` is given — that its payload is that record's identity.
pub fn verify_statement(raw: &[u8], record: Option<&Value>, keys: &TrustedKeys) -> Report {
    let mut r = Report::new("statement");
    statement_layer(raw, record, keys, &mut r);
    r.finish()
}

/// [`verify_statement`] over base64 input.
pub fn verify_statement_b64(b64s: &str, record: Option<&Value>, keys: &TrustedKeys) -> Report {
    match b64(b64s) {
        Ok(raw) => verify_statement(&raw, record, keys),
        Err(e) => input_error("statement", format!("statement is not base64: {e}")),
    }
}

pub(crate) fn input_error(artefact: &'static str, detail: String) -> Report {
    let mut r = Report::new(artefact);
    r.find(INPUT_INVALID, Severity::Error, detail);
    r.finish()
}

// ---------------------------------------------------------------------------
// Records
// ---------------------------------------------------------------------------

/// Verify an action record against the record rules (`brutor_evidence::verify`)
/// and, when given, its Signed Statement. `store` (records in log order)
/// enables the chain-parent check.
pub fn verify_record(
    record: &Value,
    statement: Option<&[u8]>,
    store: Option<&[Value]>,
    keys: &TrustedKeys,
) -> Report {
    let mut r = Report::new("record");
    let sem = brutor_evidence::verify(record, store);
    for f in &sem.findings {
        r.findings.push(Finding {
            code: f.code.to_owned(),
            severity: f.severity.into(),
            detail: f.detail.clone(),
            record_id: None,
            rule: f.check,
        });
    }
    let errors = sem
        .findings
        .iter()
        .filter(|f| f.severity == brutor_evidence::Severity::Error)
        .count();
    r.check(
        "record rules",
        if errors == 0 {
            Outcome::Ok
        } else {
            Outcome::Failed
        },
        if errors == 0 {
            "structure, identity, effect binding, verdict/effect, attestation, chain and vocabulary rules raise no error".to_string()
        } else {
            format!("{errors} error finding(s)")
        },
    );
    match &sem.record_id {
        Some(id) if !sem.findings.iter().any(|f| f.code == "record_id_mismatch") => {
            r.check(
                "identity",
                Outcome::Ok,
                format!("record_id recomputes to {id} (SHA-256 of RFC 8785 JCS)"),
            );
        }
        Some(id) => r.check(
            "identity",
            Outcome::Failed,
            format!("carried record_id differs from the recomputed {id}"),
        ),
        None => r.check(
            "identity",
            Outcome::Skipped,
            "record is not canonicalisable",
        ),
    }
    if record.get("chain").is_some() {
        if store.is_some() {
            let missing = sem
                .findings
                .iter()
                .any(|f| f.code == "chain_parent_missing");
            r.check(
                "chain",
                if missing {
                    Outcome::Failed
                } else {
                    Outcome::Ok
                },
                if missing {
                    "parent record not in the supplied store"
                } else {
                    "parent record present in the supplied store"
                },
            );
        } else {
            r.check(
                "chain",
                Outcome::Skipped,
                "no store supplied; parent not looked up",
            );
        }
    }
    r.fact("record_id", sem.record_id.clone());
    r.fact(
        "carried_record_id",
        record.get("record_id").cloned().unwrap_or(Value::Null),
    );
    r.fact("effect_mode", sem.effect_mode);
    r.fact(
        "verdict",
        record.get("verdict").cloned().unwrap_or(Value::Null),
    );
    if let Some(raw) = statement {
        statement_layer(raw, Some(record), keys, &mut r);
    }
    r.finish()
}

// ---------------------------------------------------------------------------
// Tree heads
// ---------------------------------------------------------------------------

/// Verify a head; returns it when every check held.
fn head_layer(raw: &[u8], keys: &TrustedKeys, r: &mut Report, what: &str) -> Option<TreeHead> {
    let s = match CoseSign1::decode(raw) {
        Ok(s) => s,
        Err(e) => {
            r.find(
                HEAD_MALFORMED,
                Severity::Error,
                format!("{what}: not a COSE_Sign1: {e}"),
            );
            return None;
        }
    };
    if s.content_type() != Some(TREE_HEAD_CONTENT_TYPE) {
        r.find(
            HEAD_MALFORMED,
            Severity::Error,
            format!("{what}: content type (3) is not {TREE_HEAD_CONTENT_TYPE}"),
        );
        return None;
    }
    let kid = s.kid().map(hex::encode).unwrap_or_default();
    let key = resolve_key(s.kid(), keys, r, what)?;
    match TreeHead::verify(raw, &key) {
        Ok((head, _)) => {
            r.check(
                if what == "tree head" {
                    "head signature"
                } else {
                    "previous head signature"
                },
                Outcome::Ok,
                key_phrase(keys, &kid),
            );
            if head.prev_size > 0 && what == "tree head" {
                r.check(
                    "embedded consistency",
                    Outcome::Ok,
                    format!(
                        "proof from size {} (root {}) reproduces this root",
                        head.prev_size,
                        short(&hex::encode(head.prev_root.unwrap_or_default()))
                    ),
                );
            }
            Some(head)
        }
        Err(LogError::Cose(CoseError::BadSignature)) => {
            r.find(
                HEAD_SIGNATURE_INVALID,
                Severity::Error,
                format!("{what}: signature does not verify"),
            );
            None
        }
        Err(LogError::Merkle(e)) => {
            r.find(
                HEAD_CONSISTENCY_INVALID,
                Severity::Error,
                format!("{what}: embedded consistency proof from prev_root fails: {e}"),
            );
            None
        }
        Err(e) => {
            r.find(HEAD_MALFORMED, Severity::Error, format!("{what}: {e}"));
            None
        }
    }
}

fn head_facts(r: &mut Report, h: &TreeHead) {
    r.fact("log_id", h.log_id.clone());
    r.fact("tree_size", h.tree_size);
    r.fact("root", hex::encode(h.root));
    r.fact("prev_size", h.prev_size);
    r.fact("prev_root", h.prev_root.map(hex::encode));
    r.fact("issued_at", h.issued_at.clone());
}

/// Verify a signed tree head and, optionally, that `previous` (an earlier
/// head of the same log) is a prefix of it — directly when this head's
/// `prev_size` is the previous size, else through `consistency` (hex
/// RFC 6962 nodes from the previous size to this one).
pub fn verify_head(
    raw: &[u8],
    previous: Option<&[u8]>,
    consistency: Option<&[String]>,
    keys: &TrustedKeys,
) -> Report {
    let mut r = Report::new("tree_head");
    let head = head_layer(raw, keys, &mut r, "tree head");
    if let Some(h) = &head {
        head_facts(&mut r, h);
        r.fact(
            "kid",
            CoseSign1::decode(raw)
                .ok()
                .and_then(|s| s.kid().map(hex::encode)),
        );
    }
    let (Some(head), Some(prev_raw)) = (head, previous) else {
        return r.finish();
    };
    let mut pr = Report::new("tree_head");
    let prev = head_layer(prev_raw, keys, &mut pr, "previous head");
    for f in pr.findings {
        if f.severity == Severity::Error {
            r.find(
                PREVIOUS_HEAD_INVALID,
                Severity::Error,
                format!("{}: {}", f.code, f.detail),
            );
        } else if !r.findings.iter().any(|x| x.code == f.code) {
            r.findings.push(f);
        }
    }
    r.checks.extend(pr.checks);
    let Some(prev) = prev else {
        r.fact("consistent_with_previous", Value::Null);
        return r.finish();
    };
    r.fact("previous_tree_size", prev.tree_size);
    r.fact("previous_root", hex::encode(prev.root));
    let outcome: Result<(bool, String), (&str, String)> = if prev.log_id != head.log_id {
        Err((
            HEAD_LOG_MISMATCH,
            format!("previous head is for log {}", prev.log_id),
        ))
    } else if prev.tree_size > head.tree_size {
        Err((
            CONSISTENCY_UNDETERMINED,
            "the previous head is larger than this one; pass them the other way round".into(),
        ))
    } else if prev.tree_size == head.tree_size {
        Ok((
            prev.root == head.root,
            format!("both heads have size {}", head.tree_size),
        ))
    } else if head.prev_size == prev.tree_size {
        Ok((
            head.prev_root == Some(prev.root),
            format!(
                "this head's embedded proof starts from size {} and the previous head's root",
                prev.tree_size
            ),
        ))
    } else {
        match consistency {
            None => Err((
                CONSISTENCY_UNDETERMINED,
                format!(
                    "heads are not adjacent (this head chains from size {}, the previous head has size {}); supply a consistency proof",
                    head.prev_size, prev.tree_size
                ),
            )),
            Some(nodes) => match nodes
                .iter()
                .map(|h| {
                    hex::decode(h.trim())
                        .ok()
                        .and_then(|b| <Hash>::try_from(b).ok())
                })
                .collect::<Option<Vec<Hash>>>()
            {
                None => Err((
                    INPUT_INVALID,
                    "consistency proof is not a list of 32-byte hex nodes".into(),
                )),
                Some(path) => Ok((
                    merkle::verify_consistency(
                        prev.tree_size,
                        &prev.root,
                        head.tree_size,
                        &head.root,
                        &path,
                    )
                    .is_ok(),
                    format!(
                        "RFC 6962 consistency proof of {} nodes from size {} to {}",
                        path.len(),
                        prev.tree_size,
                        head.tree_size
                    ),
                )),
            },
        }
    };
    match outcome {
        Ok((true, how)) => {
            r.fact("consistent_with_previous", true);
            r.check(
                "consistency with previous",
                Outcome::Ok,
                format!("the previous head is a prefix of this head ({how})"),
            );
        }
        Ok((false, how)) => {
            r.fact("consistent_with_previous", false);
            r.find(
                HEAD_NOT_CONSISTENT,
                Severity::Error,
                format!("the previous head is not a prefix of this head ({how})"),
            );
            r.check("consistency with previous", Outcome::Failed, how);
        }
        Err((code, detail)) => {
            r.fact("consistent_with_previous", Value::Null);
            r.find(code, Severity::Error, detail.clone());
            r.check("consistency with previous", Outcome::Skipped, detail);
        }
    }
    r.finish()
}

// ---------------------------------------------------------------------------
// Receipts
// ---------------------------------------------------------------------------

/// Grade one receipt over `statement` (the head statement it was issued
/// for). `kind` is detected when absent; `pin` is the witness's Ed25519
/// key in hex (`scitt`) or the TSA certificate PEM / SHA-256 fingerprint
/// (`rfc3161`).
pub fn verify_receipt(
    receipt_raw: &[u8],
    statement: &[u8],
    kind: Option<&str>,
    pin: Option<&str>,
) -> Report {
    let mut r = Report::new("receipt");
    let kind = kind
        .map(str::to_owned)
        .unwrap_or_else(|| receipt::detect_kind(receipt_raw).to_owned());
    r.fact("kind", kind.clone());
    let g = match kind.as_str() {
        KIND_SCITT => receipt::grade_scitt(receipt_raw, statement, pin),
        KIND_RFC3161 => receipt::grade_rfc3161(receipt_raw, statement, pin),
        other => {
            r.find(
                RECEIPT_KIND_UNKNOWN,
                Severity::Error,
                format!("receipt kind {other:?} is neither scitt nor rfc3161"),
            );
            return r.finish();
        }
    };
    grade_into(&mut r, &kind, &g, "receipt");
    r.fact("grade", g.verdict);
    r.fact("tree_size", g.tree_size);
    r.fact("leaf_index", g.leaf_index);
    r.fact("iat", g.iat);
    r.fact("vendor_ext", g.vendor_ext.clone().unwrap_or(Value::Null));
    r.fact("errors", g.errors.clone());
    r.finish()
}

fn grade_into(r: &mut Report, kind: &str, g: &receipt::Graded, what: &str) {
    match g.verdict {
        "witnessed" => {
            let scheme = g
                .vendor_ext
                .as_ref()
                .and_then(|v| v.get("leaf_scheme"))
                .and_then(Value::as_str)
                .unwrap_or("");
            r.check(
                "receipt (scitt)",
                Outcome::Ok,
                format!(
                    "{what}: inclusion proof (tree_size {}, leaf_index {}, {scheme}) reproduces a root whose signature is valid under the pinned witness key",
                    g.tree_size.unwrap_or_default(),
                    g.leaf_index.unwrap_or_default()
                ),
            );
        }
        "timestamped" => {
            let gen_time = g
                .vendor_ext
                .as_ref()
                .and_then(|v| v.get("gen_time"))
                .and_then(Value::as_str)
                .unwrap_or("");
            r.check(
                "timestamp (rfc3161)",
                Outcome::Ok,
                format!(
                    "{what}: message imprint equals the statement digest; signed attributes and signature valid under the pinned TSA certificate; genTime {gen_time}"
                ),
            );
        }
        "unverified" => {
            r.find(
                RECEIPT_UNPINNED,
                Severity::Warning,
                format!("{what}: no pinned key for this {kind} receipt; nothing was checked"),
            );
            r.check(
                if kind == KIND_SCITT {
                    "receipt (scitt)"
                } else {
                    "timestamp (rfc3161)"
                },
                Outcome::Skipped,
                format!("{what}: no pinned key"),
            );
        }
        _ => {
            r.find(
                RECEIPT_INVALID,
                Severity::Error,
                format!("{what}: {}", g.errors.join("; ")),
            );
            r.check(
                if kind == KIND_SCITT {
                    "receipt (scitt)"
                } else {
                    "timestamp (rfc3161)"
                },
                Outcome::Failed,
                format!("{what}: {}", g.errors.join("; ")),
            );
        }
    }
}

// ---------------------------------------------------------------------------
// Bundles
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Default)]
pub struct BundleOptions {
    pub keys: TrustedKeys,
    /// The digest the producer sealed (its `ai.brutor.bundle` record).
    pub expected_digest: Option<String>,
    /// Pinned Ed25519 keys (hex) of witnesses whose `scitt` receipts to check.
    pub witness_keys: Vec<String>,
    /// Pinned TSA certificates (PEM or SHA-256 hex) for `rfc3161` tokens.
    pub tsa_pins: Vec<String>,
}

/// The bundle inside a `POST /v1/evidence/bundles` response
/// (`{"bundle_id", "bundle_digest", "bundle"}`), or the value itself.
pub fn unwrap_bundle(v: &Value) -> (&Value, Option<String>) {
    match v.get("bundle") {
        Some(b) if v.get("manifest").is_none() && b.is_object() => (
            b,
            v.get("bundle_digest")
                .and_then(Value::as_str)
                .map(str::to_owned),
        ),
        _ => (v, None),
    }
}

/// Verify an `ai.brutor/evidence-bundle/v1` (`brutor_evidence::bundle`),
/// then grade its witness receipts under the pinned witness keys / TSA
/// certificates in `opts`.
pub fn verify_bundle(bundle: &Value, opts: &BundleOptions) -> Report {
    let mut r = Report::new("bundle");
    let v = brutor_evidence::bundle::verify(
        bundle,
        &brutor_evidence::bundle::VerifyOptions {
            trusted_kids: opts.keys.kid_set(),
            expected_digest: opts.expected_digest.clone(),
        },
    );
    for f in &v.findings {
        r.findings.push(Finding {
            code: f.code.to_owned(),
            severity: f.severity.into(),
            detail: f.detail.clone(),
            record_id: f.record_id.clone(),
            rule: None,
        });
    }
    let has =
        |r: &Report, codes: &[&str]| r.findings.iter().any(|f| codes.contains(&f.code.as_str()));
    let manifest = bundle.get("manifest").cloned().unwrap_or(Value::Null);
    let kids: Vec<String> = manifest
        .get("kids")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|k| k.as_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default();
    for k in &kids {
        if opts.keys.status(k) == (KidStatus::Trusted { retired: true }) {
            r.find(
                KID_RETIRED,
                Severity::Info,
                format!("kid {k} is listed as retired"),
            );
        }
    }
    let n_records = bundle
        .get("records")
        .and_then(Value::as_array)
        .map_or(0, Vec::len);
    let heads = bundle
        .get("tree_heads")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let newest: Option<TreeHead> = heads
        .last()
        .and_then(|h| h.get("statement_b64"))
        .and_then(Value::as_str)
        .and_then(|s| b64(s).ok())
        .and_then(|raw| CoseSign1::decode(&raw).ok())
        .and_then(|s| s.payload)
        .and_then(|p| TreeHead::from_cbor(&p).ok());

    // ---- checks, from the findings ------------------------------------------
    let digest = v.bundle_digest.clone().unwrap_or_default();
    match &opts.expected_digest {
        Some(exp) if has(&r, &["bundle_digest_mismatch"]) => r.check(
            "bundle digest",
            Outcome::Failed,
            format!("computed {digest}, expected {exp}"),
        ),
        Some(_) => r.check(
            "bundle digest",
            Outcome::Ok,
            format!(
                "SHA-256 of JCS(bundle minus countersignatures) = {digest}, equal to the expected digest"
            ),
        ),
        None => r.check(
            "bundle digest",
            Outcome::Skipped,
            format!("computed {digest}; no expected digest supplied, not compared"),
        ),
    }
    let signer = if opts.keys.is_pinned() {
        "a trusted kid"
    } else {
        "the kid it names (unpinned)"
    };
    r.check(
        "records",
        if v.records_verified == n_records {
            Outcome::Ok
        } else {
            Outcome::Failed
        },
        format!(
            "{} of {n_records}: the record rules raise no error and the Signed Statement is valid under {signer} and commits to the record",
            v.records_verified
        ),
    );
    r.check(
        "tree heads",
        if v.heads_verified == heads.len() && !heads.is_empty() {
            Outcome::Ok
        } else {
            Outcome::Failed
        },
        format!(
            "{} of {}: signature valid under {signer}, log {}, sizes ascending",
            v.heads_verified,
            heads.len(),
            manifest
                .get("log_id")
                .and_then(Value::as_str)
                .unwrap_or("?")
        ),
    );
    if heads.len() > 1 {
        let bad = has(&r, &["consistency_invalid", "consistency_missing"]);
        r.check(
            "consistency",
            if bad {
                Outcome::Failed
            } else if v.heads_verified == heads.len() {
                Outcome::Ok
            } else {
                Outcome::Skipped
            },
            format!(
                "{} RFC 6962 consistency proof(s) between consecutive heads",
                heads.len() - 1
            ),
        );
    }
    let size = newest.as_ref().map_or(0, |h| h.tree_size);
    r.check(
        "inclusion",
        if v.heads_verified < heads.len() || heads.is_empty() {
            Outcome::Skipped
        } else if v.proofs_verified == n_records {
            Outcome::Ok
        } else {
            Outcome::Failed
        },
        format!(
            "{} of {n_records} inclusion proofs reproduce the root of the newest head (size {size})",
            v.proofs_verified
        ),
    );
    let comp = manifest.get("completeness").cloned().unwrap_or(Value::Null);
    let closure_bad = has(
        &r,
        &[
            "closure_parent_dropped",
            "records_mode_mismatch",
            "record_not_in_selector",
        ],
    );
    r.check(
        "closure",
        if closure_bad {
            Outcome::Failed
        } else {
            Outcome::Ok
        },
        format!(
            "every chain parent is included or listed missing ({} listed); records_mode {}; selection {}",
            v.missing.len(),
            comp.get("records_mode").and_then(Value::as_str).unwrap_or("?"),
            comp.get("selection").and_then(Value::as_str).unwrap_or("?"),
        ),
    );

    // ---- witness receipts --------------------------------------------------------
    let mut receipts_out = Vec::new();
    for (i, h) in heads.iter().enumerate() {
        let stmt = h
            .get("statement_b64")
            .and_then(Value::as_str)
            .and_then(|s| b64(s).ok())
            .unwrap_or_default();
        let list = h
            .get("receipts")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        for (j, rc) in list.iter().enumerate() {
            let what = format!("head {i} receipt {j}");
            let kind = rc.get("kind").and_then(Value::as_str).unwrap_or("");
            let claimed = rc.get("grade").and_then(Value::as_str).unwrap_or("");
            let raw = match rc.get("receipt_b64").and_then(Value::as_str).map(b64) {
                Some(Ok(raw)) => raw,
                _ => {
                    r.find(
                        RECEIPT_INVALID,
                        Severity::Error,
                        format!("{what}: receipt_b64 absent or not base64"),
                    );
                    continue;
                }
            };
            let pins: &[String] = match kind {
                KIND_SCITT => &opts.witness_keys,
                KIND_RFC3161 => &opts.tsa_pins,
                other => {
                    r.find(
                        RECEIPT_KIND_UNKNOWN,
                        Severity::Warning,
                        format!("{what}: kind {other:?} not checked"),
                    );
                    continue;
                }
            };
            if pins.is_empty() {
                r.find(
                    RECEIPT_NOT_CHECKED,
                    Severity::Info,
                    format!(
                        "{what}: {kind} receipt (claimed grade {claimed:?}) not checked — no pinned {} supplied",
                        if kind == KIND_SCITT {
                            "witness key"
                        } else {
                            "TSA certificate"
                        }
                    ),
                );
                receipts_out.push(json!({"head": i, "index": j, "kind": kind,
                    "claimed_grade": claimed, "computed_grade": Value::Null}));
                continue;
            }
            let mut best: Option<receipt::Graded> = None;
            for pin in pins {
                let g = if kind == KIND_SCITT {
                    receipt::grade_scitt(&raw, &stmt, Some(pin))
                } else {
                    receipt::grade_rfc3161(&raw, &stmt, Some(pin))
                };
                let good = matches!(g.verdict, "witnessed" | "timestamped");
                if good || best.is_none() {
                    best = Some(g);
                }
                if good {
                    break;
                }
            }
            let g = best.unwrap_or_default();
            grade_into(&mut r, kind, &g, &what);
            receipts_out.push(json!({"head": i, "index": j, "kind": kind,
                "claimed_grade": claimed, "computed_grade": g.verdict,
                "iat": g.iat, "tree_size": g.tree_size, "leaf_index": g.leaf_index}));
        }
    }

    r.fact("bundle_digest", v.bundle_digest.clone());
    r.fact("digest_compared", opts.expected_digest.is_some());
    r.fact(
        "bundle_id",
        manifest.get("bundle_id").cloned().unwrap_or(Value::Null),
    );
    r.fact(
        "log_id",
        manifest.get("log_id").cloned().unwrap_or(Value::Null),
    );
    r.fact("kids", kids);
    r.fact("records", n_records);
    r.fact("records_verified", v.records_verified);
    r.fact("proofs_verified", v.proofs_verified);
    r.fact("heads", heads.len());
    r.fact("heads_verified", v.heads_verified);
    r.fact("newest_tree_size", newest.as_ref().map(|h| h.tree_size));
    r.fact("newest_root", newest.as_ref().map(|h| hex::encode(h.root)));
    r.fact("missing", v.missing.clone());
    r.fact(
        "selection",
        comp.get("selection").cloned().unwrap_or(Value::Null),
    );
    r.fact(
        "records_mode",
        comp.get("records_mode").cloned().unwrap_or(Value::Null),
    );
    r.fact("receipts", receipts_out);
    r.finish()
}
