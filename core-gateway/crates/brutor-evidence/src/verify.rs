// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The semantic verifier.
//!
//! Contract: a structured result, never a panic; a single `ok` gates
//! trust in every other field; findings in a fixed order with stable
//! codes; no clock, no network, no model. Only `error` findings gate
//! `ok` — `warning` and `info` never do.
//!
//! Checks, in order:
//! 1. structural — schema, required members, types, digest hygiene,
//!    never-enters member names at any depth
//! 2. identity — recompute `record_id`
//! 3. confirmed-effect binding
//! 4. verdict / effect orthogonality
//! 5. attestation presence
//! 6. chain — parent present in the store; concurrent supersedes
//! 7. (reserved: claims never above derived — no claimed modes in v1)
//! 8. unknown vocabulary values (informational)
//! + defensive honesty check (`warning`)

use crate::canon::CanonError;
use crate::record::{self, SCHEMA, derive_effect_mode, is_hex64};
use serde::Serialize;
use serde_json::Value;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Severity {
    Error,
    Warning,
    Info,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct Finding {
    pub check: Option<u8>,
    pub code: &'static str,
    pub severity: Severity,
    pub detail: String,
}

#[derive(Debug, Clone, Serialize, Default)]
pub struct VerificationResult {
    pub ok: bool,
    pub findings: Vec<Finding>,
    /// Recomputed identity, when computable.
    pub record_id: Option<String>,
    pub effect_mode: Option<&'static str>,
}

const REQUIRED: &[&str] = &[
    "schema",
    "action_id",
    "kind",
    "operator",
    "system",
    "timestamp",
    "verdict",
];

fn push(
    f: &mut Vec<Finding>,
    check: Option<u8>,
    code: &'static str,
    severity: Severity,
    detail: impl Into<String>,
) {
    f.push(Finding {
        check,
        code,
        severity,
        detail: detail.into(),
    });
}

/// JSON paths of every member, at any depth, whose name is in the
/// never-enters class ([`record::NEVER_ENTERS_MEMBERS`]), in document order
/// of a sorted walk (deterministic whatever the map implementation).
fn never_enters_paths(v: &Value, path: &mut String, out: &mut Vec<String>) {
    match v {
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            for k in keys {
                let len = path.len();
                path.push('.');
                path.push_str(k);
                if record::NEVER_ENTERS_MEMBERS.contains(&k.as_str()) {
                    out.push(path.clone());
                }
                never_enters_paths(&map[k.as_str()], path, out);
                path.truncate(len);
            }
        }
        Value::Array(items) => {
            for (i, item) in items.iter().enumerate() {
                let len = path.len();
                path.push_str(&format!("[{i}]"));
                never_enters_paths(item, path, out);
                path.truncate(len);
            }
        }
        _ => {}
    }
}

/// Verify one record. `store` (records in log order) enables the
/// store-level chain checks; without it check 6 reports `info`.
pub fn verify(record: &Value, store: Option<&[Value]>) -> VerificationResult {
    let mut r = VerificationResult::default();
    let f = &mut r.findings;

    // ---- 1. structural ----------------------------------------------------
    let Some(obj) = record.as_object() else {
        push(
            f,
            Some(1),
            "not_an_object",
            Severity::Error,
            "record is not a JSON object",
        );
        return r;
    };
    if obj.get("schema").and_then(Value::as_str) != Some(SCHEMA) {
        push(
            f,
            Some(1),
            "unsupported_schema",
            Severity::Error,
            format!("expected {SCHEMA}"),
        );
    }
    for k in REQUIRED {
        match obj.get(*k) {
            None => push(f, Some(1), "missing_required_field", Severity::Error, *k),
            Some(v) if *k != "system" && !v.is_string() => {
                push(f, Some(1), "field_not_string", Severity::Error, *k)
            }
            _ => {}
        }
    }
    match obj.get("record_id").and_then(Value::as_str) {
        None => push(
            f,
            Some(1),
            "missing_required_field",
            Severity::Error,
            "record_id",
        ),
        Some(id) if !is_hex64(id) => push(f, Some(1), "record_id_malformed", Severity::Error, id),
        _ => {}
    }
    for k in [
        "disposition",
        "effect",
        "model",
        "attestation",
        "context",
        "chain",
    ] {
        if let Some(v) = obj.get(k)
            && !v.is_object()
        {
            push(f, Some(1), "block_not_object", Severity::Error, k);
        }
    }
    for k in ["controls", "references"] {
        if let Some(v) = obj.get(k)
            && !v.is_array()
        {
            push(f, Some(1), "field_not_array", Severity::Error, k);
        }
    }
    for k in obj.keys() {
        if !record::RECORD_ADMITTED_FIELDS.contains(&k.as_str()) {
            push(f, Some(1), "unadmitted_field", Severity::Error, k.clone());
        }
    }
    let mut forbidden = Vec::new();
    never_enters_paths(record, &mut String::from("$"), &mut forbidden);
    for p in forbidden {
        push(f, Some(1), "never_enters_member", Severity::Error, p);
    }
    let kind = obj.get("kind").and_then(Value::as_str).unwrap_or("");
    if !kind.is_empty() && kind != record::kind::DECIDE && kind != record::kind::FYI {
        push(f, Some(1), "kind_invalid", Severity::Error, kind);
    }
    let mut hygiene_failed = false;
    match crate::canon::canonical_bytes(record) {
        Ok(_) => {}
        Err(CanonError::Float(p)) => {
            hygiene_failed = true;
            push(f, Some(1), "float_in_digest_field", Severity::Error, p);
        }
        Err(CanonError::UnsafeInteger(p)) => {
            hygiene_failed = true;
            push(
                f,
                Some(1),
                "unsafe_integer_in_digest_field",
                Severity::Error,
                p,
            );
        }
        Err(CanonError::Jcs(e)) => {
            hygiene_failed = true;
            push(f, Some(1), "not_canonicalizable", Severity::Error, e);
        }
    }
    if let Some(d) = obj.get("disposition").and_then(Value::as_object) {
        match d.get("approver").and_then(Value::as_str) {
            None => push(
                f,
                Some(1),
                "missing_required_field",
                Severity::Error,
                "disposition.approver",
            ),
            Some(a) if !record::approver::ALL.contains(&a) => {
                push(f, Some(1), "approver_invalid", Severity::Error, a)
            }
            _ => {}
        }
        if d.get("decision").and_then(Value::as_str).is_none() {
            push(
                f,
                Some(1),
                "missing_required_field",
                Severity::Error,
                "disposition.decision",
            );
        }
        if !d
            .get("human_disposed")
            .map(Value::is_boolean)
            .unwrap_or(false)
        {
            push(
                f,
                Some(1),
                "field_not_bool",
                Severity::Error,
                "disposition.human_disposed",
            );
        }
    }
    if let Some(refs) = obj.get("references").and_then(Value::as_array) {
        for (i, e) in refs.iter().enumerate() {
            let Some(o) = e.as_object() else {
                push(
                    f,
                    Some(1),
                    "reference_malformed",
                    Severity::Error,
                    format!("references[{i}] not an object"),
                );
                continue;
            };
            for k in ["type", "digest_alg", "digest"] {
                if o.get(k)
                    .and_then(Value::as_str)
                    .map(str::is_empty)
                    .unwrap_or(true)
                {
                    push(
                        f,
                        Some(1),
                        "reference_malformed",
                        Severity::Error,
                        format!("references[{i}].{k}"),
                    );
                }
            }
            if o.get("digest_alg").and_then(Value::as_str) == Some("SHA-256")
                && !o
                    .get("digest")
                    .and_then(Value::as_str)
                    .map(is_hex64)
                    .unwrap_or(false)
            {
                push(
                    f,
                    Some(1),
                    "reference_malformed",
                    Severity::Error,
                    format!("references[{i}].digest not hex64"),
                );
            }
        }
    }
    // defensive honesty — warning, never gates
    if let Some(d) = obj.get("disposition").and_then(Value::as_object)
        && d.get("human_disposed").and_then(Value::as_bool) == Some(true)
        && d.get("approver").and_then(Value::as_str) != Some(record::approver::HUMAN)
    {
        push(
            f,
            None,
            "dishonest_human_disposed",
            Severity::Warning,
            "human_disposed=true with non-human approver",
        );
    }

    // ---- 2. identity ------------------------------------------------------
    if !hygiene_failed {
        match record::compute_record_id(record) {
            Ok(id) => {
                r.record_id = Some(id.clone());
                if let Some(carried) = obj.get("record_id").and_then(Value::as_str)
                    && is_hex64(carried)
                    && carried != id
                {
                    push(
                        f,
                        Some(2),
                        "record_id_mismatch",
                        Severity::Error,
                        format!("carried {carried}, recomputed {id}"),
                    );
                }
            }
            Err(e) => push(
                f,
                Some(2),
                "record_id_uncomputable",
                Severity::Error,
                e.to_string(),
            ),
        }
    }

    // ---- 3. confirmed-effect binding -------------------------------------
    let effect = obj.get("effect");
    if let Some(e) = effect.and_then(Value::as_object)
        && e.get("status").and_then(Value::as_str) == Some(record::effect_status::CONFIRMED)
        && !e
            .get("response_digest")
            .and_then(Value::as_str)
            .map(is_hex64)
            .unwrap_or(false)
    {
        push(
            f,
            Some(3),
            "confirmed_without_response",
            Severity::Error,
            "effect.status=confirmed without response_digest",
        );
    }
    if let Some(e) = effect.and_then(Value::as_object)
        && e.get("status").and_then(Value::as_str) == Some(record::effect_status::PLANNED)
        && (e.get("request_digest").is_some() || e.get("response_digest").is_some())
    {
        push(
            f,
            Some(3),
            "planned_with_digests",
            Severity::Error,
            "effect.status=planned carries digests",
        );
    }

    // ---- 4. verdict / effect orthogonality -------------------------------
    let mode = derive_effect_mode(effect);
    r.effect_mode = Some(mode);
    let verdict = obj.get("verdict").and_then(Value::as_str).unwrap_or("");
    if record::verdict::NEVER_DISPATCH.contains(&verdict)
        && mode != record::effect_mode::NOT_APPLICABLE
    {
        push(
            f,
            Some(4),
            "verdict_effect_conflict",
            Severity::Error,
            format!("{verdict} with effect mode {mode}"),
        );
    }

    // ---- 5. attestation presence -----------------------------------------
    if let Some(e) = effect.and_then(Value::as_object) {
        let has = e
            .get("attestation")
            .and_then(Value::as_str)
            .map(|s| !s.is_empty())
            .unwrap_or(false);
        if mode == record::effect_mode::NOT_APPLICABLE
            && has
            && e.get("status").and_then(Value::as_str) == Some(record::effect_status::PLANNED)
        {
            // planned carve: attestation is meaningless before dispatch, tolerated as info
            push(
                f,
                Some(5),
                "attestation_on_planned",
                Severity::Info,
                "attestation present on a planned effect",
            );
        } else if mode != record::effect_mode::NOT_APPLICABLE && !has {
            push(
                f,
                Some(5),
                "attestation_missing",
                Severity::Error,
                "dispatched effect without attestation",
            );
        }
    }

    // ---- 6. chain ---------------------------------------------------------
    if let Some(c) = obj.get("chain").and_then(Value::as_object) {
        let parent = c.get("parent_record_id").and_then(Value::as_str);
        match parent {
            Some(p) if is_hex64(p) => match store {
                None => push(
                    f,
                    Some(6),
                    "chain_check_store_level",
                    Severity::Info,
                    "no store supplied; parent not checked",
                ),
                Some(s) => {
                    if !s
                        .iter()
                        .any(|x| x.get("record_id").and_then(Value::as_str) == Some(p))
                    {
                        push(f, Some(6), "chain_parent_missing", Severity::Error, p);
                    }
                }
            },
            _ => push(
                f,
                Some(6),
                "chain_parent_malformed",
                Severity::Error,
                "chain.parent_record_id",
            ),
        }
        if c.get("relation")
            .and_then(Value::as_str)
            .map(str::is_empty)
            .unwrap_or(true)
        {
            push(
                f,
                Some(6),
                "missing_required_field",
                Severity::Error,
                "chain.relation",
            );
        }
        if let (Some(p), Some(refs)) = (parent, obj.get("references").and_then(Value::as_array))
            && refs
                .iter()
                .any(|e| e.get("digest").and_then(Value::as_str) == Some(p))
        {
            push(
                f,
                Some(6),
                "reference_duplicates_chain_parent",
                Severity::Error,
                p,
            );
        }
    }

    // ---- 8. unknown vocabulary (informational) ---------------------------
    if !verdict.is_empty() && !record::verdict::ALL.contains(&verdict) {
        push(
            f,
            Some(8),
            "unknown_vocabulary_value",
            Severity::Info,
            format!("verdict={verdict}"),
        );
    }
    if let Some(d) = obj
        .get("disposition")
        .and_then(|d| d.get("decision"))
        .and_then(Value::as_str)
        && !record::decision::ALL.contains(&d)
    {
        push(
            f,
            Some(8),
            "unknown_vocabulary_value",
            Severity::Info,
            format!("decision={d}"),
        );
    }
    if let Some(s) = effect.and_then(|e| e.get("status")).and_then(Value::as_str)
        && !record::effect_status::ALL.contains(&s)
    {
        push(
            f,
            Some(8),
            "unknown_vocabulary_value",
            Severity::Info,
            format!("effect.status={s}"),
        );
    }
    if let Some(rel) = obj
        .get("chain")
        .and_then(|c| c.get("relation"))
        .and_then(Value::as_str)
        && !record::relation::ALL.contains(&rel)
    {
        push(
            f,
            Some(8),
            "unknown_vocabulary_value",
            Severity::Info,
            format!("chain.relation={rel}"),
        );
    }

    r.ok = !r.findings.iter().any(|x| x.severity == Severity::Error);
    r
}

/// Verify every record of a store in log order and surface concurrent
/// `supersedes` over one parent: the earliest is authoritative, later
/// ones are `info` findings on their own result.
pub fn verify_store(records: &[Value]) -> Vec<VerificationResult> {
    let mut out: Vec<VerificationResult> =
        records.iter().map(|r| verify(r, Some(records))).collect();
    let mut seen: Vec<&str> = Vec::new();
    for (i, rec) in records.iter().enumerate() {
        if let Some(c) = rec.get("chain").and_then(Value::as_object)
            && c.get("relation").and_then(Value::as_str) == Some(record::relation::SUPERSEDES)
            && let Some(p) = c.get("parent_record_id").and_then(Value::as_str)
        {
            if seen.contains(&p) {
                push(
                    &mut out[i].findings,
                    Some(6),
                    "concurrent_supersedes",
                    Severity::Info,
                    p,
                );
            } else {
                seen.push(p);
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::record::*;
    use serde_json::json;

    fn executed() -> Value {
        let mut b = RecordBuilder::new("mcp/1", "t", "2026-09-19T00:00:00Z");
        b.verdict = verdict::EXECUTED.into();
        b.disposition = Some(Disposition {
            decision: "accept".into(),
            approver: "policy".into(),
            human_disposed: false,
            authority: None,
            reason_digest: None,
        });
        b.effect = Some(Effect {
            kind: "tool".into(),
            status: "confirmed".into(),
            request_digest: Some("1".repeat(64)),
            response_digest: Some("2".repeat(64)),
            attestation: ATTESTATION_GATE_OBSERVED.into(),
            irreversibility: None,
        });
        b.seal().unwrap().0
    }

    fn codes(r: &VerificationResult) -> Vec<&'static str> {
        r.findings.iter().map(|f| f.code).collect()
    }

    #[test]
    fn sealed_record_verifies_clean() {
        let r = verify(&executed(), None);
        assert!(r.ok, "{:?}", r.findings);
        assert_eq!(r.effect_mode, Some(effect_mode::CONFIRMED));
        assert!(r.findings.is_empty());
    }

    #[test]
    fn tamper_is_identity_mismatch() {
        let mut v = executed();
        v["verdict"] = json!("blocked");
        let r = verify(&v, None);
        assert!(!r.ok);
        assert!(codes(&r).contains(&"record_id_mismatch"));
        assert!(codes(&r).contains(&"verdict_effect_conflict"));
    }

    #[test]
    fn honesty_is_warning_not_gate() {
        let mut v = executed();
        v["disposition"]["human_disposed"] = json!(true);
        v["record_id"] = json!(compute_record_id(&v).unwrap());
        let r = verify(&v, None);
        assert!(r.ok);
        assert_eq!(codes(&r), vec!["dishonest_human_disposed"]);
    }

    #[test]
    fn unknown_vocabulary_is_info_and_unadmitted_field_is_error() {
        let mut v = executed();
        v["verdict"] = json!("assessed");
        v["record_id"] = json!(compute_record_id(&v).unwrap());
        let r = verify(&v, None);
        assert!(r.ok);
        assert_eq!(codes(&r), vec!["unknown_vocabulary_value"]);
        v["subject_id"] = json!("user-1");
        v["record_id"] = json!(compute_record_id(&v).unwrap());
        let r = verify(&v, None);
        assert!(!r.ok);
        assert!(codes(&r).contains(&"unadmitted_field"));
    }

    #[test]
    fn store_level_chain_and_concurrent_supersedes() {
        let mut b = RecordBuilder::new("mcp/a", "t", "2026-09-19T00:00:00Z");
        b.verdict = verdict::AWAITING_HUMAN.into();
        b.disposition = Some(Disposition {
            decision: "needs_input".into(),
            approver: "policy".into(),
            human_disposed: false,
            authority: None,
            reason_digest: None,
        });
        let (parent, pid) = b.seal().unwrap();
        let mk = |aid: &str| {
            let mut c = RecordBuilder::new(aid, "t", "2026-09-19T00:01:00Z");
            c.verdict = verdict::RESOLVED.into();
            c.disposition = Some(Disposition {
                decision: "reject".into(),
                approver: "human".into(),
                human_disposed: true,
                authority: Some("apr-1".into()),
                reason_digest: None,
            });
            c.chain = Some(Chain {
                parent_record_id: pid.clone(),
                relation: relation::SUPERSEDES.into(),
            });
            c.seal().unwrap().0
        };
        let s1 = mk("mcp/b");
        let s2 = mk("mcp/c");
        let orphan = verify(&s1, Some(std::slice::from_ref(&s2)));
        assert!(codes(&orphan).contains(&"chain_parent_missing"));
        let results = verify_store(&[parent, s1, s2]);
        assert!(results.iter().all(|r| r.ok));
        assert_eq!(codes(&results[2]), vec!["concurrent_supersedes"]);
        assert!(codes(&results[1]).is_empty());
    }

    #[test]
    fn never_enters_member_is_rejected_at_any_depth() {
        let mut v = executed();
        v["context"] = json!({"root_task_id": "run-1", "meta": [{"email": "a@b.c"}]});
        v["record_id"] = json!(compute_record_id(&v).unwrap());
        let r = verify(&v, None);
        assert!(!r.ok);
        assert_eq!(codes(&r), vec!["never_enters_member"]);
        assert_eq!(r.findings[0].detail, "$.context.meta[0].email");
    }

    #[test]
    fn float_and_bad_shape_are_structural() {
        let r = verify(
            &json!({"schema": SCHEMA, "action_id": "x", "kind": "decide", "operator": "t", "system": "ungoverned", "timestamp": "t", "verdict": "executed", "record_id": "0".repeat(64), "context": {"score": 0.5}}),
            None,
        );
        assert!(codes(&r).contains(&"float_in_digest_field"));
        assert!(!codes(&r).contains(&"record_id_mismatch"));
        let r = verify(&json!([]), None);
        assert_eq!(codes(&r), vec!["not_an_object"]);
    }
}
