// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The `ai.brutor/action-record/v1` vocabulary and a builder that makes
//! an invalid record unrepresentable.
//!
//! Two invariants are enforced at construction rather than checked
//! afterwards, because a record that violates them must never be signed:
//!
//! * **Honesty** — `human_disposed: true` requires `approver: "human"`.
//!   A producer MUST NOT claim a human disposed when policy did.
//! * **Confirmed-effect binding** — `effect.status: "confirmed"` requires
//!   a `response_digest` over the actually observed response; `planned`
//!   forbids both digests. "Confirmed is an observed result, never a
//!   promise."
//!
//! Vocabularies are plain strings on the wire so a verifier written
//! against a later version treats unknown values as informational.

use crate::canon::{self, CanonError};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

pub const SCHEMA: &str = "ai.brutor/action-record/v1";

pub mod verdict {
    pub const EXECUTED: &str = "executed";
    pub const ERRORED: &str = "errored";
    pub const TIMEOUT: &str = "timeout";
    pub const BLOCKED: &str = "blocked";
    pub const DENIED: &str = "denied";
    pub const ENGINE_FAILURE: &str = "engine_failure";
    pub const AWAITING_HUMAN: &str = "awaiting_human";
    pub const RESOLVED: &str = "resolved";
    pub const EXPIRED: &str = "expired";
    pub const EPOCH_BOUNDARY: &str = "epoch_boundary";
    /// A judged verdict over earlier records, chained `assesses`.
    pub const ASSESSMENT: &str = "assessment";
    /// A human blind grade of an assessment, chained `adjudicates`.
    pub const ADJUDICATION: &str = "adjudication";
    /// An `fyi` statement that something was declared, rendered, generated
    /// or changed — profile declarations, notices, rubrics, reports,
    /// incident changes, row-chain heads, bundles, key rotations.
    pub const RECORDED: &str = "recorded";
    /// The first half of a two-phase action (`two_phase_actions`):
    /// the gates accepted the action and it is being dispatched. Carries
    /// `effect.status: planned` and no digests; the outcome record chains
    /// `confirms` to it. Never-dispatching *as a record* — it attests the
    /// decision, not the effect, so its derived effect mode is
    /// `not_applicable`. An unconfirmed `planned` record is the visible
    /// may/did gap a crash or a lost outcome leaves behind.
    pub const PLANNED: &str = "planned";
    pub const ALL: &[&str] = &[
        EXECUTED,
        ERRORED,
        TIMEOUT,
        BLOCKED,
        DENIED,
        ENGINE_FAILURE,
        AWAITING_HUMAN,
        RESOLVED,
        EXPIRED,
        EPOCH_BOUNDARY,
        ASSESSMENT,
        ADJUDICATION,
        RECORDED,
        PLANNED,
    ];
    /// Verdicts that never dispatch an effect; their derived effect
    /// mode must be `not_applicable`.
    pub const NEVER_DISPATCH: &[&str] = &[
        BLOCKED,
        DENIED,
        ENGINE_FAILURE,
        AWAITING_HUMAN,
        RESOLVED,
        EXPIRED,
        EPOCH_BOUNDARY,
        ASSESSMENT,
        ADJUDICATION,
        RECORDED,
        PLANNED,
    ];
    /// Verdicts that leave an item open until a `supersedes` record closes it.
    pub const OPEN_ITEM: &[&str] = &[AWAITING_HUMAN];
}

pub mod kind {
    pub const DECIDE: &str = "decide";
    pub const FYI: &str = "fyi";
}

pub mod decision {
    pub const ACCEPT: &str = "accept";
    pub const REJECT: &str = "reject";
    pub const NEEDS_INPUT: &str = "needs_input";
    pub const DEFERRED: &str = "deferred";
    pub const ALL: &[&str] = &[ACCEPT, REJECT, NEEDS_INPUT, DEFERRED];
}

pub mod approver {
    pub const HUMAN: &str = "human";
    pub const POLICY: &str = "policy";
    pub const COUNTERPARTY: &str = "counterparty";
    pub const ALL: &[&str] = &[HUMAN, POLICY, COUNTERPARTY];
}

pub mod effect_status {
    pub const PLANNED: &str = "planned";
    pub const DISPATCHED: &str = "dispatched";
    pub const CONFIRMED: &str = "confirmed";
    pub const FAILED: &str = "failed";
    pub const REVERTED: &str = "reverted";
    pub const ALL: &[&str] = &[PLANNED, DISPATCHED, CONFIRMED, FAILED, REVERTED];
}

pub mod effect_mode {
    pub const NOT_APPLICABLE: &str = "not_applicable";
    pub const DISPATCHED_UNCONFIRMED: &str = "dispatched_unconfirmed";
    pub const CONFIRMED: &str = "confirmed";
}

pub mod relation {
    pub const CONFIRMS: &str = "confirms";
    pub const SUPERSEDES: &str = "supersedes";
    pub const EPOCH_OPENS: &str = "epoch_opens";
    pub const ASSESSES: &str = "assesses";
    pub const ADJUDICATES: &str = "adjudicates";
    pub const ALL: &[&str] = &[CONFIRMS, SUPERSEDES, EPOCH_OPENS, ASSESSES, ADJUDICATES];
}

pub mod control_result {
    pub const PASS: &str = "pass";
    pub const FAIL: &str = "fail";
    pub const NA: &str = "n/a";
}

pub const ATTESTATION_GATE_OBSERVED: &str = "gate_observed";
pub const OBSERVATION_IN_PATH: &str = "in_path";
pub const RUNTIME: &str = "brutor-gateway";

/// Top-level members a record may carry. Anything not listed is
/// structurally unreachable from the builder.
pub const RECORD_ADMITTED_FIELDS: &[&str] = &[
    "schema",
    "record_id",
    "action_id",
    "kind",
    "operator",
    "system",
    "timestamp",
    "epoch",
    "verdict",
    "disposition",
    "effect",
    "controls",
    "model",
    "attestation",
    "context",
    "chain",
    "references",
];

/// Member names of the never-enters class: identities,
/// credentials, network identifiers, headers and body previews. No record
/// may carry a member with one of these names at any depth — the verifier
/// rejects it structurally (`never_enters_member`), so a producer bug that
/// threads one through `context` fails verification rather than landing
/// in a permanent log. The gateway's static tests grep its builders for
/// the same names.
pub const NEVER_ENTERS_MEMBERS: &[&str] = &[
    "subject_id",
    "subject_kind",
    "api_key_id",
    "api_key_name",
    "end_user_session_id",
    "session_id",
    "user_id",
    "gateway_user_id",
    "gateway_user_username",
    "idp_user_id",
    "idp_user_username",
    "idp_user_email",
    "email",
    "client_ip",
    "ip_address",
    "user_agent",
    "headers",
    "request_headers",
    "response_headers",
    "authorization",
    "request_body_preview",
    "response_body_preview",
    "actor_agent_id",
];

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum RecordError {
    #[error("human_disposed=true requires approver=\"human\"")]
    DishonestHumanDisposed,
    #[error("effect.status=confirmed requires response_digest")]
    ConfirmedWithoutResponse,
    #[error("effect.status=planned forbids request/response digests")]
    PlannedWithDigests,
    #[error("never-dispatching verdict {0} cannot carry a dispatched/confirmed effect")]
    VerdictEffectConflict(String),
    #[error("{0} is required")]
    Missing(&'static str),
    #[error("digest is not 64 lowercase hex: {0}")]
    BadDigest(String),
    #[error("{0}")]
    Canon(#[from] CanonError),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct System {
    pub ai_system_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub contract_version: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub contract_hash: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Disposition {
    pub decision: String,
    pub approver: String,
    pub human_disposed: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub authority: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason_digest: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Effect {
    #[serde(rename = "type")]
    pub kind: String,
    pub status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub request_digest: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub response_digest: Option<String>,
    pub attestation: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub irreversibility: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Control {
    pub id: String,
    pub result: String,
    pub blocking: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub severity: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub kind: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub evidence_digest: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Model {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub model_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub provider: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub deciding_model: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Attestation {
    pub runtime: String,
    pub observation: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub input_digest: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub output_digest: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub digest_salt: Option<String>,
    pub numbers_canonicalized: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Chain {
    pub parent_record_id: String,
    pub relation: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Reference {
    #[serde(rename = "type")]
    pub kind: String,
    pub digest_alg: String,
    pub digest: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub purpose: Option<String>,
}

pub fn is_hex64(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

/// Derived effect mode (verifier rule, not a stored field).
pub fn derive_effect_mode(effect: Option<&Value>) -> &'static str {
    let Some(e) = effect else {
        return effect_mode::NOT_APPLICABLE;
    };
    let status = e.get("status").and_then(Value::as_str).unwrap_or("");
    let resp_ok = e
        .get("response_digest")
        .and_then(Value::as_str)
        .map(is_hex64)
        .unwrap_or(false);
    match status {
        effect_status::PLANNED => effect_mode::NOT_APPLICABLE,
        effect_status::CONFIRMED if resp_ok => effect_mode::CONFIRMED,
        _ => effect_mode::DISPATCHED_UNCONFIRMED,
    }
}

/// Typed builder. `seal()` validates, canonicalises, derives `record_id`
/// and returns the finished JSON object.
#[derive(Debug, Clone, Default)]
pub struct RecordBuilder {
    pub action_id: String,
    pub kind: String,
    pub operator: String,
    pub system: Option<System>,
    pub timestamp: String,
    pub epoch: Option<String>,
    pub verdict: String,
    pub disposition: Option<Disposition>,
    pub effect: Option<Effect>,
    pub controls: Vec<Control>,
    pub model: Option<Model>,
    pub attestation: Option<Attestation>,
    pub context: Map<String, Value>,
    pub chain: Option<Chain>,
    pub references: Option<Vec<Reference>>,
}

impl RecordBuilder {
    pub fn new(
        action_id: impl Into<String>,
        operator: impl Into<String>,
        timestamp: impl Into<String>,
    ) -> Self {
        Self {
            action_id: action_id.into(),
            kind: kind::DECIDE.into(),
            operator: operator.into(),
            timestamp: timestamp.into(),
            ..Default::default()
        }
    }

    pub fn validate(&self) -> Result<(), RecordError> {
        if self.action_id.is_empty() {
            return Err(RecordError::Missing("action_id"));
        }
        if self.operator.is_empty() {
            return Err(RecordError::Missing("operator"));
        }
        if self.timestamp.is_empty() {
            return Err(RecordError::Missing("timestamp"));
        }
        if self.verdict.is_empty() {
            return Err(RecordError::Missing("verdict"));
        }
        if let Some(d) = &self.disposition {
            if d.human_disposed && d.approver != approver::HUMAN {
                return Err(RecordError::DishonestHumanDisposed);
            }
            if let Some(r) = &d.reason_digest
                && !is_hex64(r)
            {
                return Err(RecordError::BadDigest(r.clone()));
            }
        }
        if let Some(e) = &self.effect {
            for d in [&e.request_digest, &e.response_digest]
                .into_iter()
                .flatten()
            {
                if !is_hex64(d) {
                    return Err(RecordError::BadDigest(d.clone()));
                }
            }
            match e.status.as_str() {
                effect_status::CONFIRMED if e.response_digest.is_none() => {
                    return Err(RecordError::ConfirmedWithoutResponse);
                }
                effect_status::PLANNED
                    if e.request_digest.is_some() || e.response_digest.is_some() =>
                {
                    return Err(RecordError::PlannedWithDigests);
                }
                _ => {}
            }
        }
        if verdict::NEVER_DISPATCH.contains(&self.verdict.as_str()) {
            let mode = derive_effect_mode(
                self.effect
                    .as_ref()
                    .map(|e| serde_json::to_value(e).unwrap_or(Value::Null))
                    .as_ref(),
            );
            if mode != effect_mode::NOT_APPLICABLE {
                return Err(RecordError::VerdictEffectConflict(self.verdict.clone()));
            }
        }
        for c in &self.controls {
            if let Some(d) = &c.evidence_digest
                && !is_hex64(d)
            {
                return Err(RecordError::BadDigest(d.clone()));
            }
        }
        if let Some(refs) = &self.references {
            for r in refs {
                if r.digest_alg == "SHA-256" && !is_hex64(&r.digest) {
                    return Err(RecordError::BadDigest(r.digest.clone()));
                }
            }
        }
        if let Some(c) = &self.chain
            && !is_hex64(&c.parent_record_id)
        {
            return Err(RecordError::BadDigest(c.parent_record_id.clone()));
        }
        Ok(())
    }

    /// Validate and seal. The returned object carries `record_id`.
    pub fn seal(&self) -> Result<(Value, String), RecordError> {
        self.validate()?;
        let mut obj = Map::new();
        obj.insert("schema".into(), json!(SCHEMA));
        obj.insert("action_id".into(), json!(self.action_id));
        obj.insert("kind".into(), json!(self.kind));
        obj.insert("operator".into(), json!(self.operator));
        obj.insert(
            "system".into(),
            match &self.system {
                Some(s) => serde_json::to_value(s).map_err(|e| CanonError::Jcs(e.to_string()))?,
                None => json!("ungoverned"),
            },
        );
        obj.insert("timestamp".into(), json!(self.timestamp));
        if let Some(e) = &self.epoch {
            obj.insert("epoch".into(), json!(e));
        }
        obj.insert("verdict".into(), json!(self.verdict));
        if let Some(d) = &self.disposition {
            obj.insert(
                "disposition".into(),
                serde_json::to_value(d).map_err(|e| CanonError::Jcs(e.to_string()))?,
            );
        }
        if let Some(e) = &self.effect {
            obj.insert(
                "effect".into(),
                serde_json::to_value(e).map_err(|e| CanonError::Jcs(e.to_string()))?,
            );
        }
        if !self.controls.is_empty() {
            obj.insert(
                "controls".into(),
                serde_json::to_value(&self.controls).map_err(|e| CanonError::Jcs(e.to_string()))?,
            );
        }
        if let Some(m) = &self.model {
            obj.insert(
                "model".into(),
                serde_json::to_value(m).map_err(|e| CanonError::Jcs(e.to_string()))?,
            );
        }
        if let Some(a) = &self.attestation {
            obj.insert(
                "attestation".into(),
                serde_json::to_value(a).map_err(|e| CanonError::Jcs(e.to_string()))?,
            );
        }
        if !self.context.is_empty() {
            obj.insert("context".into(), Value::Object(self.context.clone()));
        }
        if let Some(c) = &self.chain {
            obj.insert(
                "chain".into(),
                serde_json::to_value(c).map_err(|e| CanonError::Jcs(e.to_string()))?,
            );
        }
        if let Some(r) = &self.references {
            obj.insert(
                "references".into(),
                serde_json::to_value(r).map_err(|e| CanonError::Jcs(e.to_string()))?,
            );
        }
        let mut value = Value::Object(obj);
        let id = compute_record_id(&value)?;
        if let Value::Object(m) = &mut value {
            m.insert("record_id".into(), json!(id));
        }
        Ok((value, id))
    }
}

/// SHA-256 over the JCS of every member except `record_id` (top level only).
pub fn compute_record_id(record: &Value) -> Result<String, CanonError> {
    let mut clone = record.clone();
    if let Value::Object(m) = &mut clone {
        m.remove("record_id");
    }
    canon::json_digest_hex(&clone)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn base() -> RecordBuilder {
        let mut b = RecordBuilder::new("mcp/log-1", "tenant-a", "2026-09-19T10:00:00Z");
        b.verdict = verdict::EXECUTED.into();
        b.disposition = Some(Disposition {
            decision: decision::ACCEPT.into(),
            approver: approver::POLICY.into(),
            human_disposed: false,
            authority: None,
            reason_digest: None,
        });
        b
    }

    #[test]
    fn seals_and_recomputes() {
        let (v, id) = base().seal().unwrap();
        assert_eq!(v["record_id"], id);
        assert_eq!(compute_record_id(&v).unwrap(), id);
        assert_eq!(v["schema"], SCHEMA);
        assert_eq!(v["system"], "ungoverned");
    }

    #[test]
    fn honesty_invariant_unrepresentable() {
        let mut b = base();
        b.disposition.as_mut().unwrap().human_disposed = true;
        assert_eq!(b.seal().unwrap_err(), RecordError::DishonestHumanDisposed);
        b.disposition.as_mut().unwrap().approver = approver::HUMAN.into();
        assert!(b.seal().is_ok());
    }

    #[test]
    fn confirmed_requires_response_and_planned_forbids_digests() {
        let mut b = base();
        b.effect = Some(Effect {
            kind: "write_order".into(),
            status: effect_status::CONFIRMED.into(),
            request_digest: None,
            response_digest: None,
            attestation: ATTESTATION_GATE_OBSERVED.into(),
            irreversibility: None,
        });
        assert_eq!(b.seal().unwrap_err(), RecordError::ConfirmedWithoutResponse);
        b.effect.as_mut().unwrap().response_digest = Some("a".repeat(64));
        assert!(b.seal().is_ok());
        b.effect.as_mut().unwrap().status = effect_status::PLANNED.into();
        assert_eq!(b.seal().unwrap_err(), RecordError::PlannedWithDigests);
    }

    #[test]
    fn blocked_cannot_carry_dispatched_effect() {
        let mut b = base();
        b.verdict = verdict::BLOCKED.into();
        b.effect = Some(Effect {
            kind: "x".into(),
            status: effect_status::DISPATCHED.into(),
            request_digest: Some("b".repeat(64)),
            response_digest: None,
            attestation: ATTESTATION_GATE_OBSERVED.into(),
            irreversibility: None,
        });
        assert!(matches!(
            b.seal().unwrap_err(),
            RecordError::VerdictEffectConflict(_)
        ));
        b.effect = None;
        assert!(b.seal().is_ok());
    }

    /// The vocabulary is pinned on both sides: the Python
    /// mirror is `app/database/models/action_record.py::RECORD_VERDICTS` /
    /// `NON_DISPATCHING_VERDICTS`, pinned by the control plane's
    /// `tests/test_evidence_vocabulary_parity.py` with the same literals.
    #[test]
    fn verdict_vocabulary_is_pinned() {
        let mut all = verdict::ALL.to_vec();
        all.sort_unstable();
        assert_eq!(
            all,
            vec![
                "adjudication",
                "assessment",
                "awaiting_human",
                "blocked",
                "denied",
                "engine_failure",
                "epoch_boundary",
                "errored",
                "executed",
                "expired",
                "planned",
                "recorded",
                "resolved",
                "timeout",
            ]
        );
        let mut dispatching: Vec<&str> = verdict::ALL
            .iter()
            .copied()
            .filter(|v| !verdict::NEVER_DISPATCH.contains(v))
            .collect();
        dispatching.sort_unstable();
        assert_eq!(dispatching, vec!["errored", "executed", "timeout"]);
    }

    #[test]
    fn planned_record_seals_with_a_planned_effect_and_nothing_else() {
        let mut b = base();
        b.verdict = verdict::PLANNED.into();
        b.effect = Some(Effect {
            kind: "write_order".into(),
            status: effect_status::PLANNED.into(),
            request_digest: None,
            response_digest: None,
            attestation: ATTESTATION_GATE_OBSERVED.into(),
            irreversibility: None,
        });
        assert!(b.seal().is_ok());
        b.effect.as_mut().unwrap().status = effect_status::DISPATCHED.into();
        assert!(matches!(
            b.seal().unwrap_err(),
            RecordError::VerdictEffectConflict(_)
        ));
    }

    #[test]
    fn references_tri_state_changes_identity() {
        let mut a = base();
        let (_, id_absent) = a.seal().unwrap();
        a.references = Some(vec![]);
        let (_, id_empty) = a.seal().unwrap();
        assert_ne!(id_absent, id_empty);
    }
}
