// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Deterministic mapping `ai.brutor/action-record/v1` → AAC format-4
//! Capsule (draft-mih-scitt-agent-action-capsule-04).
//!
//! The mapping is a pure function of the record bytes (plus, for a
//! chained record, the parent's already-mapped Capsule ID): the same
//! record always yields the same Capsule and the same `capsule_id`. The
//! record is re-identified first (its `record_id` is recomputed) and a
//! record that does not reproduce its own identity is refused. The stored
//! record model is never changed by this profile; everything AAC-specific
//! lives in this crate.
//!
//! # Field map
//!
//! | record | capsule | notes |
//! |---|---|---|
//! | — | `spec_version` = `draft-mih-scitt-agent-action-capsule-04`, `format_version` = `"4"`, `canonicalization_id` = `"jcs"` | constants |
//! | `action_id` | `action_id` | verbatim (`<surface>/<proxy_log id>`) |
//! | `kind` | `action_type` | same vocabulary (`decide` / `fyi`) |
//! | `operator` | `operator` | tenant id, verbatim |
//! | `system` | `developer` | `urn:brutor:ai-system:<id>[@contract-v<n>]`, or `urn:brutor:ungoverned` |
//! | `timestamp` | `timestamp` | verbatim (RFC 3339 UTC `Z`) |
//! | `epoch` | `epoch_id` | verbatim (`contract:<hash>`) |
//! | `verdict` | `disposition.verdict_class` | see verdict table |
//! | `disposition.{decision,approver,human_disposed,authority,reason_digest}` | `disposition.*` | identical vocabularies; the honesty invariant is re-asserted and a violating record is refused |
//! | `effect.{type,status,request_digest,response_digest}` | `effect.*` | identical status vocabulary |
//! | `effect.irreversibility` | `effect.irreversibility_class` | verbatim value |
//! | `effect.attestation` (`gate_observed`) | `effect.effect_attestation` (`gate_executed`) | omitted when `status = planned` (the planned carve) |
//! | (derived) | `assurance` | `attestation_mode: self_attested` always; `effect_mode` re-derived; `ledger_mode` = `chained` iff `chain` |
//! | `model.{model_id,provider}` | `model_attestation.{model_id,provider}` | |
//! | `model.deciding_model`, `attestation.runtime` | `model_attestation.compute_attestation.{deciding_model,runtime}` | |
//! | `attestation.{input_digest,output_digest,digest_salt}` | `model_attestation.compute_attestation.ai.brutor.{input_digest,output_digest,digest_salt}` | **namespaced on purpose**, see lossy |
//! | `controls[]` | `constraints[]` | `id`,`result`,`blocking`,`severity`,`evidence_digest` verbatim; `kind` → `check_type` (namespaced `ai.brutor.<kind>`) |
//! | `chain.{parent_record_id,relation}` | `chain.{parent_capsule_id,relation}` | the parent's **Capsule ID** (map the lineage root-first, [`map_lineage`]); `assesses`/`adjudicates` → `ai.brutor.assesses`/`ai.brutor.adjudicates` |
//! | `references[]` | `references[]` | `{type,digest_alg,digest}` verbatim; tri-state (absent / `[]` / entries) preserved |
//! | `context.tags` | `compliance.framework_tags` | verbatim |
//! | everything else | `ai.brutor` (namespaced top-level extension member) | see below |
//!
//! Verdicts: `executed errored timeout blocked denied engine_failure
//! resolved expired epoch_boundary` map to the same `verdict_class`;
//! `awaiting_human` → `hitl_dispatched`; `assessment`, `adjudication`,
//! `recorded` have no AAC equivalent and map to the namespaced
//! (unregistered, informational) values `ai.brutor.assessment`,
//! `ai.brutor.adjudication`, `ai.brutor.recorded`. Any other value is
//! namespaced the same way.
//!
//! # The `ai.brutor` extension member
//!
//! The draft puts every extension point in the Capsule JSON (§9) and
//! requires new vocabulary to be reverse-DNS namespaced (§9.1), so the
//! untranslatable remainder travels in one namespaced member, committed
//! to `capsule_id` like everything else:
//!
//! ```json
//! "ai.brutor": {
//!   "mapping": "ai.brutor/aac-mapping/v1",
//!   "record_schema": "ai.brutor/action-record/v1",
//!   "record_id": "<the source record_id>",
//!   "verdict": "<the record verdict>",
//!   "system": { … } | "ungoverned",
//!   "context": { …context minus tags… },
//!   "effect_attestation": "gate_observed",
//!   "attestation": {"observation": "in_path", "numbers_canonicalized": false},
//!   "chain": {"parent_record_id": "…", "relation": "…"},
//!   "reference_purposes": [ "…" | null, … ],
//!   "lossy": [ <codes below, sorted> ]
//! }
//! ```
//!
//! # Lossy / untranslatable (the `lossy` codes)
//!
//! * `verdict_unregistered` — `assessment` / `adjudication` / `recorded`
//!   (and unknown verdicts) have no seeded `verdict_class`; generic AAC
//!   verifiers treat the namespaced value as informational and do not know
//!   these never dispatch (our builder guarantees they carry no effect, so
//!   the derived `effect_mode` is `not_applicable` anyway).
//! * `chain_relation_unregistered` — `assesses` / `adjudicates` are not
//!   seeded `chain.relation` values (informational); the parent is still
//!   our own same-stream record, so `chain` (not `references`) is correct.
//! * `effect_attestation_planned_carve` — a `planned` effect's
//!   `gate_observed` is dropped (the draft forbids grading a non-execution).
//! * `effect_attestation_unregistered` — an attestation other than
//!   `gate_observed` is namespaced and graded no stronger than
//!   `runtime_claimed` by AAC verifiers.
//! * `payload_digests_opaque` — record digests are carried, but the record
//!   does not declare their construction: gateway-minted rows digest the
//!   observed body *bytes*, seal-API records digest `JCS(value)`. The draft
//!   requires `response_digest` to be a JSON digest of the response and
//!   defines Disclosure-Envelope eligibility only for
//!   `agent_input_digest` / `agent_output_digest` under JSON-DIGEST. So
//!   input/output digests go under namespaced `ai.brutor.*` keys (a DE-3
//!   disclosure is not offered rather than offered-and-failing), and
//!   `effect.request_digest` / `response_digest` are carried verbatim as
//!   the gate's binding to the observed exchange.
//! * `reference_purpose_untranslated` — the record's reference `purpose`
//!   is neither CPB's `purpose` nor AAC's `citation_purpose`; it is kept in
//!   the extension, and no `citation_purpose` is asserted.
//! * `disposition_absent` — a record without a disposition yields a
//!   Capsule without one (the verdict survives only in the extension).
//! * `attestation_tier_not_projected` — the record's witnessed tenant-log
//!   inclusion (RFC 9162 receipts) is not an AAC registration of the
//!   Capsule ID, so `attestation_mode` is always `self_attested` and
//!   `ledger_mode` is never `anchored`; the extension's `record_id` is the
//!   route back to that evidence. Always present.
//!
//! Constraint determinism: the draft wants every constraint `result` to be
//! the output of a deterministic predicate. Our `guardrail` and
//! `semantic_policy` controls may be model-assisted; their `check_type`
//! says which kind of check produced them, and the recorded result is the
//! gate's decision at the time, which a verifier must not re-run.

use serde::Serialize;
use serde_json::{Map, Value, json};

use brutor_evidence::record::{RECORD_ADMITTED_FIELDS, SCHEMA, compute_record_id};

use crate::canon::{self, is_hex64};
use crate::{CANONICALIZATION_ID, FORMAT_VERSION, SPEC_VERSION};

pub const MAPPING_ID: &str = "ai.brutor/aac-mapping/v1";
pub const EXTENSION_MEMBER: &str = "ai.brutor";
pub const NAMESPACE: &str = "ai.brutor.";
/// The deepest lineage [`map_lineage`] accepts; real chains are ≤ 4
/// (adjudication → assessment → outcome → awaiting_human).
pub const MAX_LINEAGE: usize = 32;

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum MapError {
    #[error("not an {SCHEMA} record")]
    NotARecord,
    #[error("record member {0} is missing or malformed")]
    Malformed(&'static str),
    #[error("record carries a member outside RECORD_ADMITTED_FIELDS: {0}")]
    UnknownMember(String),
    #[error("record_id does not reproduce (carried {carried}, recomputed {recomputed})")]
    RecordIdMismatch { carried: String, recomputed: String },
    #[error("human_disposed=true with a non-human approver; refusing to map")]
    DishonestHumanDisposed,
    #[error("chained record: the parent record {0} must be mapped first")]
    ParentUnresolved(String),
    #[error("record has no chain but a parent capsule id was supplied")]
    UnexpectedParent,
    #[error("lineage is broken at position {0}")]
    BrokenLineage(usize),
    #[error("lineage deeper than {MAX_LINEAGE}")]
    LineageTooDeep,
    #[error("canonicalisation: {0}")]
    Canon(String),
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct MappedCapsule {
    /// The sealed Capsule (carries `capsule_id`).
    pub capsule: Value,
    pub capsule_id: String,
    pub record_id: String,
    /// Sorted, de-duplicated lossy codes (also inside the capsule).
    pub lossy: Vec<&'static str>,
}

/// The `verdict_class` for a record verdict, and whether it is seeded.
pub fn verdict_class_for(verdict: &str) -> (String, bool) {
    match verdict {
        "executed" | "errored" | "timeout" | "blocked" | "denied" | "engine_failure"
        | "resolved" | "expired" | "epoch_boundary" => (verdict.to_string(), true),
        "awaiting_human" => ("hitl_dispatched".to_string(), true),
        other => (namespaced(other), false),
    }
}

/// The record verdict a received `verdict_class` corresponds to, if any.
pub fn verdict_for_class(verdict_class: &str) -> Option<&'static str> {
    Some(match verdict_class {
        "executed" => "executed",
        "errored" => "errored",
        "timeout" => "timeout",
        "blocked" => "blocked",
        "denied" => "denied",
        "engine_failure" => "engine_failure",
        "resolved" => "resolved",
        "expired" => "expired",
        "epoch_boundary" => "epoch_boundary",
        "hitl_dispatched" => "awaiting_human",
        "ai.brutor.assessment" => "assessment",
        "ai.brutor.adjudication" => "adjudication",
        "ai.brutor.recorded" => "recorded",
        _ => return None,
    })
}

fn relation_for(relation: &str) -> (String, bool) {
    match relation {
        "confirms" | "supersedes" | "epoch_opens" => (relation.to_string(), true),
        other => (namespaced(other), false),
    }
}

/// Bare names are reserved for seeded values (§9.1): anything we
/// introduce is reverse-DNS prefixed unless it is already namespaced.
pub fn namespaced(v: &str) -> String {
    if v.contains('.') || v.contains(':') || v.contains('/') {
        v.to_string()
    } else {
        format!("{NAMESPACE}{v}")
    }
}

fn str_member<'a>(o: &'a Map<String, Value>, k: &'static str) -> Result<&'a str, MapError> {
    o.get(k)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or(MapError::Malformed(k))
}

fn copy_opt_str(src: &Map<String, Value>, from: &str, dst: &mut Map<String, Value>, to: &str) {
    if let Some(v) = src.get(from).and_then(Value::as_str) {
        dst.insert(to.into(), json!(v));
    }
}

fn developer_for(system: Option<&Value>) -> String {
    match system.and_then(Value::as_object) {
        Some(s) => {
            let id = s.get("ai_system_id").and_then(Value::as_str).unwrap_or("");
            match s.get("contract_version").and_then(Value::as_i64) {
                Some(v) => format!("urn:brutor:ai-system:{id}@contract-v{v}"),
                None => format!("urn:brutor:ai-system:{id}"),
            }
        }
        None => "urn:brutor:ungoverned".to_string(),
    }
}

/// Map one record. `parent_capsule_id` is required exactly when the
/// record carries a `chain` (it is the parent's mapped Capsule ID).
pub fn map_record(
    record: &Value,
    parent_capsule_id: Option<&str>,
) -> Result<MappedCapsule, MapError> {
    let r = record.as_object().ok_or(MapError::NotARecord)?;
    if r.get("schema").and_then(Value::as_str) != Some(SCHEMA) {
        return Err(MapError::NotARecord);
    }
    if let Some(k) = r
        .keys()
        .find(|k| !RECORD_ADMITTED_FIELDS.contains(&k.as_str()))
    {
        return Err(MapError::UnknownMember(k.clone()));
    }
    let carried = str_member(r, "record_id")?;
    let recomputed = compute_record_id(record).map_err(|e| MapError::Canon(e.to_string()))?;
    if carried != recomputed {
        return Err(MapError::RecordIdMismatch {
            carried: carried.into(),
            recomputed,
        });
    }
    let verdict = str_member(r, "verdict")?;
    let mut lossy: Vec<&'static str> = vec!["attestation_tier_not_projected"];
    let mut ext = Map::new();
    ext.insert("mapping".into(), json!(MAPPING_ID));
    ext.insert("record_schema".into(), json!(SCHEMA));
    ext.insert("record_id".into(), json!(recomputed));
    ext.insert("verdict".into(), json!(verdict));
    if let Some(s) = r.get("system") {
        ext.insert("system".into(), s.clone());
    }

    let mut c = Map::new();
    c.insert("spec_version".into(), json!(SPEC_VERSION));
    c.insert("format_version".into(), json!(FORMAT_VERSION));
    c.insert("canonicalization_id".into(), json!(CANONICALIZATION_ID));
    c.insert("action_id".into(), json!(str_member(r, "action_id")?));
    c.insert("action_type".into(), json!(str_member(r, "kind")?));
    c.insert("operator".into(), json!(str_member(r, "operator")?));
    c.insert("developer".into(), json!(developer_for(r.get("system"))));
    c.insert("timestamp".into(), json!(str_member(r, "timestamp")?));
    if let Some(e) = r.get("epoch") {
        let e = e.as_str().ok_or(MapError::Malformed("epoch"))?;
        c.insert("epoch_id".into(), json!(e));
    }

    // disposition
    let (verdict_class, seeded) = verdict_class_for(verdict);
    if !seeded {
        lossy.push("verdict_unregistered");
    }
    match r.get("disposition") {
        None => lossy.push("disposition_absent"),
        Some(d) => {
            let d = d.as_object().ok_or(MapError::Malformed("disposition"))?;
            let decision = str_member(d, "decision")
                .map_err(|_| MapError::Malformed("disposition.decision"))?;
            let approver = str_member(d, "approver")
                .map_err(|_| MapError::Malformed("disposition.approver"))?;
            let human = d
                .get("human_disposed")
                .and_then(Value::as_bool)
                .ok_or(MapError::Malformed("disposition.human_disposed"))?;
            if human && approver != "human" {
                return Err(MapError::DishonestHumanDisposed);
            }
            let mut out = Map::new();
            out.insert("decision".into(), json!(decision));
            out.insert("approver".into(), json!(approver));
            out.insert("human_disposed".into(), json!(human));
            copy_opt_str(d, "authority", &mut out, "authority");
            copy_opt_str(d, "reason_digest", &mut out, "reason_digest");
            out.insert("verdict_class".into(), json!(verdict_class));
            c.insert("disposition".into(), Value::Object(out));
        }
    }

    // effect
    let mut effect_out: Option<Map<String, Value>> = None;
    if let Some(e) = r.get("effect") {
        let e = e.as_object().ok_or(MapError::Malformed("effect"))?;
        let status = str_member(e, "status").map_err(|_| MapError::Malformed("effect.status"))?;
        let mut out = Map::new();
        copy_opt_str(e, "type", &mut out, "type");
        out.insert("status".into(), json!(status));
        copy_opt_str(e, "request_digest", &mut out, "request_digest");
        copy_opt_str(e, "response_digest", &mut out, "response_digest");
        copy_opt_str(e, "irreversibility", &mut out, "irreversibility_class");
        if out.contains_key("request_digest") || out.contains_key("response_digest") {
            lossy.push("payload_digests_opaque");
        }
        let attestation = e.get("attestation").and_then(Value::as_str);
        if let Some(a) = attestation {
            ext.insert("effect_attestation".into(), json!(a));
        }
        if status == "planned" {
            if attestation.is_some() {
                lossy.push("effect_attestation_planned_carve");
            }
        } else {
            let a = attestation.ok_or(MapError::Malformed("effect.attestation"))?;
            let mapped = if a == brutor_evidence::record::ATTESTATION_GATE_OBSERVED {
                "gate_executed".to_string()
            } else {
                lossy.push("effect_attestation_unregistered");
                namespaced(a)
            };
            out.insert("effect_attestation".into(), json!(mapped));
        }
        effect_out = Some(out);
    }
    let effect_mode = crate::verify::derive_effect_mode(effect_out.as_ref());
    if let Some(out) = effect_out {
        c.insert("effect".into(), Value::Object(out));
    }

    // chain
    match (r.get("chain"), parent_capsule_id) {
        (Some(ch), parent) => {
            let ch = ch.as_object().ok_or(MapError::Malformed("chain"))?;
            let parent_record = str_member(ch, "parent_record_id")
                .map_err(|_| MapError::Malformed("chain.parent_record_id"))?;
            let relation =
                str_member(ch, "relation").map_err(|_| MapError::Malformed("chain.relation"))?;
            let parent = parent.ok_or_else(|| MapError::ParentUnresolved(parent_record.into()))?;
            if !is_hex64(parent) {
                return Err(MapError::Malformed("parent_capsule_id"));
            }
            let (rel, seeded) = relation_for(relation);
            if !seeded {
                lossy.push("chain_relation_unregistered");
            }
            c.insert(
                "chain".into(),
                json!({"parent_capsule_id": parent, "relation": rel}),
            );
            ext.insert(
                "chain".into(),
                json!({"parent_record_id": parent_record, "relation": relation}),
            );
        }
        (None, Some(_)) => return Err(MapError::UnexpectedParent),
        (None, None) => {}
    }

    c.insert(
        "assurance".into(),
        json!({
            "attestation_mode": "self_attested",
            "effect_mode": effect_mode,
            "ledger_mode": if c.contains_key("chain") { "chained" } else { "standalone" },
        }),
    );

    // model_attestation
    let mut model_att = Map::new();
    let mut compute = Map::new();
    if let Some(m) = r.get("model") {
        let m = m.as_object().ok_or(MapError::Malformed("model"))?;
        copy_opt_str(m, "model_id", &mut model_att, "model_id");
        copy_opt_str(m, "provider", &mut model_att, "provider");
        copy_opt_str(m, "deciding_model", &mut compute, "deciding_model");
    }
    if let Some(a) = r.get("attestation") {
        let a = a.as_object().ok_or(MapError::Malformed("attestation"))?;
        copy_opt_str(a, "runtime", &mut compute, "runtime");
        let before = compute.len();
        copy_opt_str(a, "input_digest", &mut compute, "ai.brutor.input_digest");
        copy_opt_str(a, "output_digest", &mut compute, "ai.brutor.output_digest");
        if compute.len() > before {
            lossy.push("payload_digests_opaque");
        }
        copy_opt_str(a, "digest_salt", &mut compute, "ai.brutor.digest_salt");
        let mut att_ext = Map::new();
        if let Some(o) = a.get("observation") {
            att_ext.insert("observation".into(), o.clone());
        }
        if let Some(n) = a.get("numbers_canonicalized") {
            att_ext.insert("numbers_canonicalized".into(), n.clone());
        }
        if !att_ext.is_empty() {
            ext.insert("attestation".into(), Value::Object(att_ext));
        }
    }
    if !compute.is_empty() {
        model_att.insert("compute_attestation".into(), Value::Object(compute));
    }
    if !model_att.is_empty() {
        c.insert("model_attestation".into(), Value::Object(model_att));
    }

    // constraints
    if let Some(controls) = r.get("controls") {
        let controls = controls.as_array().ok_or(MapError::Malformed("controls"))?;
        let mut out = Vec::with_capacity(controls.len());
        for ctl in controls {
            let ctl = ctl.as_object().ok_or(MapError::Malformed("controls[]"))?;
            let mut o = Map::new();
            o.insert(
                "id".into(),
                json!(str_member(ctl, "id").map_err(|_| MapError::Malformed("controls[].id"))?),
            );
            o.insert(
                "result".into(),
                json!(
                    str_member(ctl, "result")
                        .map_err(|_| MapError::Malformed("controls[].result"))?
                ),
            );
            if let Some(b) = ctl.get("blocking").and_then(Value::as_bool) {
                o.insert("blocking".into(), json!(b));
            }
            copy_opt_str(ctl, "severity", &mut o, "severity");
            if let Some(k) = ctl.get("kind").and_then(Value::as_str) {
                o.insert("check_type".into(), json!(namespaced(k)));
            }
            copy_opt_str(ctl, "evidence_digest", &mut o, "evidence_digest");
            out.push(Value::Object(o));
        }
        if !out.is_empty() {
            c.insert("constraints".into(), Value::Array(out));
        }
    }

    // references (tri-state preserved)
    if let Some(refs) = r.get("references") {
        let refs = refs.as_array().ok_or(MapError::Malformed("references"))?;
        let mut out = Vec::with_capacity(refs.len());
        let mut purposes = Vec::with_capacity(refs.len());
        for rf in refs {
            let rf = rf.as_object().ok_or(MapError::Malformed("references[]"))?;
            out.push(json!({
                "type": str_member(rf, "type").map_err(|_| MapError::Malformed("references[].type"))?,
                "digest_alg": str_member(rf, "digest_alg").map_err(|_| MapError::Malformed("references[].digest_alg"))?,
                "digest": str_member(rf, "digest").map_err(|_| MapError::Malformed("references[].digest"))?,
            }));
            purposes.push(rf.get("purpose").cloned().unwrap_or(Value::Null));
        }
        if purposes.iter().any(|p| !p.is_null()) {
            lossy.push("reference_purpose_untranslated");
            ext.insert("reference_purposes".into(), Value::Array(purposes));
        }
        c.insert("references".into(), Value::Array(out));
    }

    // context → compliance.framework_tags + extension
    if let Some(ctx) = r.get("context") {
        let ctx = ctx.as_object().ok_or(MapError::Malformed("context"))?;
        let mut rest = ctx.clone();
        if let Some(tags) = rest.remove("tags")
            && tags.as_array().map(|a| !a.is_empty()).unwrap_or(false)
        {
            c.insert("compliance".into(), json!({"framework_tags": tags}));
        }
        if !rest.is_empty() {
            ext.insert("context".into(), Value::Object(rest));
        }
    }

    lossy.sort_unstable();
    lossy.dedup();
    ext.insert("lossy".into(), json!(lossy));
    c.insert(EXTENSION_MEMBER.into(), Value::Object(ext));

    let mut capsule = Value::Object(c);
    let capsule_id =
        canon::compute_capsule_id(&capsule).map_err(|e| MapError::Canon(e.to_string()))?;
    if let Value::Object(m) = &mut capsule {
        m.insert("capsule_id".into(), json!(capsule_id));
    }
    Ok(MappedCapsule {
        capsule,
        capsule_id,
        record_id: recomputed,
        lossy,
    })
}

/// Map a chained lineage, **root first**: `lineage[0]` has no chain and
/// every later record chains to the one before it. Returns one mapped
/// Capsule per record (the last is the one asked for).
pub fn map_lineage(lineage: &[Value]) -> Result<Vec<MappedCapsule>, MapError> {
    if lineage.len() > MAX_LINEAGE {
        return Err(MapError::LineageTooDeep);
    }
    let mut out: Vec<MappedCapsule> = Vec::with_capacity(lineage.len());
    for (i, rec) in lineage.iter().enumerate() {
        let parent_record = rec
            .get("chain")
            .and_then(|c| c.get("parent_record_id"))
            .and_then(Value::as_str);
        let parent_capsule = match (out.last(), parent_record) {
            (None, None) => None,
            (Some(prev), Some(p)) if prev.record_id == p => Some(prev.capsule_id.clone()),
            _ => return Err(MapError::BrokenLineage(i)),
        };
        out.push(map_record(rec, parent_capsule.as_deref())?);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use brutor_evidence::record::{
        Chain, Disposition, Effect, RecordBuilder, approver, decision, effect_status, relation,
        verdict,
    };

    fn rec(v: &str) -> RecordBuilder {
        let mut b = RecordBuilder::new("mcp/log-1", "tenant-a", "2026-09-19T10:00:00.000Z");
        b.verdict = v.into();
        b.disposition = Some(Disposition {
            decision: decision::REJECT.into(),
            approver: approver::POLICY.into(),
            human_disposed: false,
            authority: None,
            reason_digest: None,
        });
        b
    }

    #[test]
    fn deterministic_and_tamper_refused() {
        let (r, _) = rec(verdict::BLOCKED).seal().unwrap();
        let a = map_record(&r, None).unwrap();
        let b = map_record(&r, None).unwrap();
        assert_eq!(a, b);
        assert_eq!(canon::compute_capsule_id(&a.capsule).unwrap(), a.capsule_id);
        let mut t = r.clone();
        t["operator"] = json!("tenant-b");
        assert!(matches!(
            map_record(&t, None),
            Err(MapError::RecordIdMismatch { .. })
        ));
    }

    #[test]
    fn planned_carve_and_awaiting_human() {
        let mut b = rec(verdict::AWAITING_HUMAN);
        b.disposition.as_mut().unwrap().decision = decision::NEEDS_INPUT.into();
        b.effect = Some(Effect {
            kind: "tools/call".into(),
            status: effect_status::PLANNED.into(),
            request_digest: None,
            response_digest: None,
            attestation: brutor_evidence::record::ATTESTATION_GATE_OBSERVED.into(),
            irreversibility: None,
        });
        let (r, _) = b.seal().unwrap();
        let m = map_record(&r, None).unwrap();
        assert_eq!(m.capsule["disposition"]["verdict_class"], "hitl_dispatched");
        assert!(m.capsule["effect"].get("effect_attestation").is_none());
        assert!(m.lossy.contains(&"effect_attestation_planned_carve"));
        let v = crate::verify::verify(&m.capsule, None);
        assert!(v.ok, "{:?}", v.findings);
    }

    #[test]
    fn chained_record_needs_parent_capsule() {
        let (parent, parent_id) = rec(verdict::AWAITING_HUMAN).seal().unwrap();
        let mut b = rec(verdict::RESOLVED);
        b.disposition = Some(Disposition {
            decision: decision::REJECT.into(),
            approver: approver::HUMAN.into(),
            human_disposed: true,
            authority: Some("apr-1".into()),
            reason_digest: None,
        });
        b.chain = Some(Chain {
            parent_record_id: parent_id.clone(),
            relation: relation::SUPERSEDES.into(),
        });
        let (child, _) = b.seal().unwrap();
        assert_eq!(
            map_record(&child, None),
            Err(MapError::ParentUnresolved(parent_id.clone()))
        );
        let lineage = map_lineage(&[parent.clone(), child.clone()]).unwrap();
        assert_eq!(
            lineage[1].capsule["chain"]["parent_capsule_id"],
            json!(lineage[0].capsule_id)
        );
        assert_eq!(lineage[1].capsule["assurance"]["ledger_mode"], "chained");
        assert_eq!(
            map_lineage(&[child, parent]),
            Err(MapError::BrokenLineage(0))
        );
    }

    #[test]
    fn namespacing() {
        assert_eq!(namespaced("assessment"), "ai.brutor.assessment");
        assert_eq!(namespaced("ai.brutor.x"), "ai.brutor.x");
        assert_eq!(
            verdict_class_for("recorded"),
            ("ai.brutor.recorded".into(), false)
        );
        assert_eq!(verdict_for_class("hitl_dispatched"), Some("awaiting_human"));
        assert_eq!(verdict_for_class("deferred"), None);
    }
}
