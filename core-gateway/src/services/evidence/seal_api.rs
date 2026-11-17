// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! `POST /v1/evidence/seal` — records sealed at write by the control plane.
//!
//! The control plane holds no key and does no crypto: a declaration, an
//! epoch boundary, an assessment, a report or an incident change is sent
//! here as a *request* for a record, validated, built and sealed into the
//! tenant log by the same [`store::seal`] path the portal notice uses.
//!
//! Admission is stricter than the writer's, because the caller is a
//! different process:
//!
//! * **`context` is allow-listed** ([`CONTEXT_ALLOWED`]); any other key is a
//!   422. Values are short opaque scalars (≤ [`MAX_CONTEXT_VALUE`] chars) or,
//!   for `tags`, a short list of strings — never free text. The never-enters
//!   rule (no identity in a record) cannot be proved by a type, so it is kept structurally
//!   unlikely: no identity-shaped keys exist to put anything in.
//! * **Free text enters only as a digest.** `payload` is any JSON; the
//!   record carries `references[{type: "ai.brutor.payload", digest:
//!   json_digest_hex(payload), purpose: record_type}]` and the payload
//!   itself is dropped here, never stored.
//! * **Unknown top-level members are a 422** (`subject_id`, `user_id`, …).
//! * `RecordBuilder::validate` enforces the honesty invariant and digest
//!   shapes; a violation is a 422, never a silently corrected record.
//! * With `ai_system_id`, the core attaches `system{ai_system_id,
//!   contract_version, contract_hash}` and `epoch = contract:<hash>` from
//!   the attribution lookup — the caller never asserts them.

use crate::services::evidence::attribution;
use crate::services::evidence::mint::Minted;
use crate::services::evidence::store::{self, SealError, Sealed};
use brutor_evidence::record::{
    Chain, Control, Disposition, RecordBuilder, Reference, System, is_hex64, kind, relation,
    verdict,
};
use serde::Deserialize;
use serde_json::{Map, Value, json};
use sqlx::PgPool;

pub const MAX_RECORDS: usize = 100;
pub const MAX_CONTEXT_VALUE: usize = 256;
pub const MAX_TAGS: usize = 64;
pub const PAYLOAD_REFERENCE: &str = "ai.brutor.payload";
/// Surface stored on `action_records` for seal-API records without
/// `context.surface` (the column is 32 characters).
pub const DEFAULT_SURFACE: &str = "control_plane";

/// Context keys admitted on seal-API records.
pub const CONTEXT_ALLOWED: &[&str] = &[
    "record_type",
    "surface",
    "root_task_id",
    "turn_id",
    "conversation_handle",
    "framework_id",
    "catalog_version",
    "obligation_key",
    "check_id",
    "evaluator_fingerprint",
    "report_id",
    "cadence",
    "period_start",
    "period_end",
    "incident_id",
    "state",
    "bundle_id",
    "contract_version",
    "approval_request_id",
    "intervention_id",
    "tags",
    "locale",
    "method",
    "backfilled",
];

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SealRequest {
    pub tenant_id: String,
    pub records: Vec<RecordRequest>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RecordRequest {
    pub record_type: String,
    #[serde(default)]
    pub verdict: Option<String>,
    #[serde(default)]
    pub kind: Option<String>,
    #[serde(default)]
    pub ai_system_id: Option<String>,
    #[serde(default)]
    pub timestamp: Option<String>,
    #[serde(default)]
    pub action_id: Option<String>,
    #[serde(default)]
    pub disposition: Option<Disposition>,
    #[serde(default)]
    pub controls: Vec<Control>,
    #[serde(default)]
    pub context: Map<String, Value>,
    #[serde(default)]
    pub payload: Option<Value>,
    #[serde(default)]
    pub references: Vec<ReferenceRequest>,
    #[serde(default)]
    pub chain: Option<Chain>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReferenceRequest {
    #[serde(rename = "type")]
    pub kind: String,
    #[serde(default)]
    pub digest_alg: Option<String>,
    pub digest: String,
    #[serde(default)]
    pub purpose: Option<String>,
}

#[derive(Debug, thiserror::Error)]
pub enum SealApiError {
    #[error("{0}")]
    Invalid(String),
    #[error(transparent)]
    Seal(#[from] SealError),
}

fn invalid(i: usize, msg: impl std::fmt::Display) -> SealApiError {
    SealApiError::Invalid(format!("records[{i}]: {msg}"))
}

/// What the attribution lookup contributes for a record's AI System.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SystemAttribution {
    pub ai_system_id: String,
    pub contract_version: Option<i64>,
    pub contract_hash: Option<String>,
}

/// A validated record, ready for [`store::seal`], plus its payload digest.
#[derive(Debug, Clone)]
pub struct Prepared {
    pub minted: Minted,
    pub payload_digest: Option<String>,
}

fn is_identifier(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 128
        && s.bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b"._-/".contains(&b))
}

fn check_context(i: usize, ctx: &Map<String, Value>) -> Result<(), SealApiError> {
    for (k, v) in ctx {
        if !CONTEXT_ALLOWED.contains(&k.as_str()) {
            return Err(invalid(
                i,
                format!("context key {k:?} is not admitted (it is not on the allow-list)"),
            ));
        }
        let ok = match (k.as_str(), v) {
            ("tags", Value::Array(items)) => {
                items.len() <= MAX_TAGS
                    && items.iter().all(|t| {
                        t.as_str()
                            .map(|s| !s.is_empty() && s.len() <= MAX_CONTEXT_VALUE)
                            .unwrap_or(false)
                    })
            }
            ("backfilled", Value::Bool(_)) => true,
            ("contract_version", Value::Number(n)) => n.is_i64() || n.is_u64(),
            (_, Value::String(s)) => {
                !s.is_empty() && s.len() <= MAX_CONTEXT_VALUE && !s.contains('\n')
            }
            (_, Value::Number(n)) => n.is_i64() || n.is_u64(),
            (_, Value::Bool(_)) => true,
            _ => false,
        };
        if !ok {
            return Err(invalid(
                i,
                format!(
                    "context.{k} must be a short opaque scalar (≤{MAX_CONTEXT_VALUE} chars, no \
                     free text — send free text as `payload`)"
                ),
            ));
        }
    }
    Ok(())
}

/// Normalise an RFC 3339 timestamp to UTC with milliseconds and `Z`.
fn normalise_ts(raw: &str) -> Option<chrono::DateTime<chrono::Utc>> {
    chrono::DateTime::parse_from_rfc3339(raw)
        .ok()
        .map(|t| t.with_timezone(&chrono::Utc))
}

/// Validate and build one record. Pure: the attribution is looked up by the
/// caller (`None` when the request names no AI System).
pub fn prepare(
    i: usize,
    tenant: &str,
    req: &RecordRequest,
    system: Option<&SystemAttribution>,
    now: chrono::DateTime<chrono::Utc>,
) -> Result<Prepared, SealApiError> {
    if !is_identifier(&req.record_type) || !req.record_type.starts_with("ai.brutor.") {
        return Err(invalid(
            i,
            "record_type must be an `ai.brutor.` identifier (lowercase, digits, . _ - /)",
        ));
    }
    let v = req.verdict.as_deref().unwrap_or(verdict::RECORDED);
    if !verdict::ALL.contains(&v) {
        return Err(invalid(i, format!("unknown verdict {v:?}")));
    }
    let k = req.kind.as_deref().unwrap_or(kind::FYI);
    if k != kind::FYI && k != kind::DECIDE {
        return Err(invalid(i, format!("unknown kind {k:?}")));
    }
    let mut ctx = req.context.clone();
    check_context(i, &ctx)?;
    match ctx.get("record_type") {
        Some(Value::String(t)) if *t != req.record_type => {
            return Err(invalid(i, "context.record_type disagrees with record_type"));
        }
        _ => {}
    }
    ctx.insert("record_type".into(), json!(req.record_type));
    if let Some(c) = &req.chain
        && !relation::ALL.contains(&c.relation.as_str())
    {
        return Err(invalid(
            i,
            format!("unknown chain relation {:?}", c.relation),
        ));
    }
    let ts = match &req.timestamp {
        Some(raw) => normalise_ts(raw).ok_or_else(|| invalid(i, "timestamp is not RFC 3339"))?,
        None => now,
    };
    let action_id = match &req.action_id {
        Some(a) if !a.is_empty() && a.len() <= 255 && !a.contains(char::is_whitespace) => a.clone(),
        Some(_) => return Err(invalid(i, "action_id must be a short opaque handle")),
        None => format!("{}/{}", req.record_type, ulid::Ulid::new()),
    };
    let mut refs: Vec<Reference> = Vec::with_capacity(req.references.len() + 2);
    for r in &req.references {
        if !is_identifier(&r.kind) {
            return Err(invalid(
                i,
                format!("reference type {:?} is not an identifier", r.kind),
            ));
        }
        let alg = r.digest_alg.clone().unwrap_or_else(|| "SHA-256".into());
        if alg != "SHA-256" || !is_hex64(&r.digest) {
            return Err(invalid(
                i,
                "references carry SHA-256 digests as 64 lowercase hex",
            ));
        }
        if let Some(p) = &r.purpose
            && !is_identifier(p)
        {
            return Err(invalid(i, "reference purpose must be an identifier"));
        }
        refs.push(Reference {
            kind: r.kind.clone(),
            digest_alg: alg,
            digest: r.digest.clone(),
            purpose: r.purpose.clone(),
        });
    }
    let payload_digest = match &req.payload {
        None => None,
        Some(p) => {
            let (p, _) = brutor_evidence::canon::canonicalize_numbers(p);
            let d = brutor_evidence::json_digest_hex(&p)
                .map_err(|e| invalid(i, format!("payload is not digestable: {e}")))?;
            refs.push(Reference {
                kind: PAYLOAD_REFERENCE.into(),
                digest_alg: "SHA-256".into(),
                digest: d.clone(),
                purpose: Some(req.record_type.clone()),
            });
            Some(d)
        }
    };
    if let Some(s) = system
        && let Some(h) = s.contract_hash.as_ref().filter(|h| is_hex64(h))
        && !refs
            .iter()
            .any(|r| r.kind == "ai.brutor.ai-system-contract" && r.digest == *h)
    {
        refs.push(Reference {
            kind: "ai.brutor.ai-system-contract".into(),
            digest_alg: "SHA-256".into(),
            digest: h.clone(),
            purpose: None,
        });
    }

    let mut b = RecordBuilder::new(
        action_id,
        tenant,
        ts.to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
    );
    b.kind = k.into();
    b.verdict = v.into();
    b.system = system.map(|s| System {
        ai_system_id: s.ai_system_id.clone(),
        contract_version: s.contract_version,
        contract_hash: s.contract_hash.clone(),
    });
    b.epoch = system
        .and_then(|s| s.contract_hash.as_ref())
        .map(|h| format!("contract:{h}"));
    b.disposition = req.disposition.clone();
    b.controls = req.controls.clone();
    b.context = ctx.clone();
    b.chain = req.chain.clone();
    b.references = (!refs.is_empty()).then_some(refs);
    let (record, record_id) = b.seal().map_err(|e| invalid(i, e))?;
    let surface = ctx
        .get("surface")
        .and_then(Value::as_str)
        .filter(|s| s.len() <= 32)
        .unwrap_or(DEFAULT_SURFACE)
        .to_string();
    let str_ctx = |k: &str| ctx.get(k).and_then(Value::as_str).map(str::to_owned);
    Ok(Prepared {
        minted: Minted {
            row_index: i,
            tenant_id: tenant.to_string(),
            ai_system_id: system.map(|s| s.ai_system_id.clone()),
            root_task_id: str_ctx("root_task_id"),
            proxy_log_id: None,
            verdict: v.into(),
            kind: k.into(),
            surface,
            epoch: record
                .get("epoch")
                .and_then(Value::as_str)
                .map(str::to_owned),
            timestamp: ts,
            tags: ctx.get("tags").cloned(),
            approval_request_id: (v == verdict::AWAITING_HUMAN)
                .then(|| str_ctx("approval_request_id"))
                .flatten(),
            consumed_approval_request_id: None,
            record,
            record_id,
        },
        payload_digest,
    })
}

/// Parse a request body. Unknown members (anywhere) are a 422 with the path.
pub fn parse(body: Value) -> Result<SealRequest, SealApiError> {
    let req: SealRequest = serde_path_to_error::deserialize(body)
        .map_err(|e| SealApiError::Invalid(format!("{}: {}", e.path(), e.inner())))?;
    if req.tenant_id.trim().is_empty() {
        return Err(SealApiError::Invalid("tenant_id is required".into()));
    }
    if req.records.is_empty() {
        return Err(SealApiError::Invalid("records is empty".into()));
    }
    if req.records.len() > MAX_RECORDS {
        return Err(SealApiError::Invalid(format!(
            "at most {MAX_RECORDS} records per call"
        )));
    }
    Ok(req)
}

/// Resolve the AI System a record names. The id must *be* an AI System of
/// this tenant (not merely a group under one).
pub async fn resolve_system(
    pool: &PgPool,
    tenant: &str,
    ai_system_id: &str,
) -> Result<Option<SystemAttribution>, sqlx::Error> {
    let Some(a) = attribution::resolve(pool, tenant, Some(ai_system_id)).await? else {
        return Ok(None);
    };
    Ok((a.ai_system_id == ai_system_id).then(|| SystemAttribution {
        ai_system_id: a.ai_system_id.clone(),
        contract_version: a.contract_version,
        contract_hash: a.contract_hash.clone(),
    }))
}

/// Validate every record, then seal them all in one transaction (all or
/// nothing). Returns the sealed records with their payload digests.
pub async fn seal(
    pool: &PgPool,
    req: &SealRequest,
) -> Result<Vec<(Sealed, Option<String>)>, SealApiError> {
    let now = chrono::Utc::now();
    let mut prepared = Vec::with_capacity(req.records.len());
    for (i, r) in req.records.iter().enumerate() {
        let system = match &r.ai_system_id {
            None => None,
            Some(id) => Some(
                // A lookup error is a database error (503), not "unknown".
                resolve_system(pool, &req.tenant_id, id)
                    .await
                    .map_err(SealError::from)?
                    .ok_or_else(|| invalid(i, format!("unknown ai_system_id {id:?}")))?,
            ),
        };
        prepared.push(prepare(i, &req.tenant_id, r, system.as_ref(), now)?);
    }
    let minted: Vec<Minted> = prepared.iter().map(|p| p.minted.clone()).collect();
    let sealed = store::seal(pool, &req.tenant_id, &minted).await?;
    Ok(sealed
        .into_iter()
        .map(|s| {
            let d = prepared
                .get(s.row_index)
                .and_then(|p| p.payload_digest.clone());
            (s, d)
        })
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn req(v: Value) -> RecordRequest {
        serde_json::from_value(v).unwrap()
    }

    fn now() -> chrono::DateTime<chrono::Utc> {
        chrono::DateTime::parse_from_rfc3339("2026-09-19T12:00:00Z")
            .unwrap()
            .with_timezone(&chrono::Utc)
    }

    fn err(r: Result<Prepared, SealApiError>) -> String {
        match r {
            Err(SealApiError::Invalid(m)) => m,
            other => panic!("expected invalid, got {other:?}"),
        }
    }

    #[test]
    fn declaration_happy_path() {
        let r = req(json!({
            "record_type": "ai.brutor.declaration",
            "context": {"framework_id": "eu-ai-act", "tags": ["eu_ai_act.art13"]},
            "payload": {"rationale": "Free text the auditor must never see in clear", "n": 1.5},
            "disposition": {"decision":"accept","approver":"human","human_disposed":true,"authority":"prof-1"},
        }));
        let p = prepare(0, "t1", &r, None, now()).unwrap();
        let rec = &p.minted.record;
        let v = brutor_evidence::verify(rec, None);
        assert!(v.ok, "{:?}", v.findings);
        assert_eq!(rec["verdict"], "recorded");
        assert_eq!(rec["kind"], "fyi");
        assert_eq!(rec["context"]["record_type"], "ai.brutor.declaration");
        assert_eq!(rec["timestamp"], "2026-09-19T12:00:00.000Z");
        assert!(
            rec["action_id"]
                .as_str()
                .unwrap()
                .starts_with("ai.brutor.declaration/")
        );
        // The payload never appears; only its digest.
        let dumped = rec.to_string();
        assert!(!dumped.contains("Free text"));
        assert!(!dumped.contains("rationale"));
        let d = p.payload_digest.unwrap();
        assert_eq!(rec["references"][0]["type"], PAYLOAD_REFERENCE);
        assert_eq!(rec["references"][0]["digest"], d);
        assert_eq!(rec["references"][0]["purpose"], "ai.brutor.declaration");
        assert_eq!(p.minted.surface, DEFAULT_SURFACE);
        assert_eq!(rec["system"], "ungoverned");
    }

    #[test]
    fn system_attribution_is_attached_by_the_core() {
        let h = "c".repeat(64);
        let sys = SystemAttribution {
            ai_system_id: "rg-sys".into(),
            contract_version: Some(3),
            contract_hash: Some(h.clone()),
        };
        let r = req(
            json!({"record_type":"ai.brutor.epoch-boundary","verdict":"epoch_boundary",
                           "ai_system_id":"rg-sys","context":{"contract_version":3}}),
        );
        let p = prepare(0, "t1", &r, Some(&sys), now()).unwrap();
        assert_eq!(p.minted.record["system"]["contract_hash"], h);
        assert_eq!(p.minted.record["system"]["contract_version"], 3);
        assert_eq!(p.minted.record["epoch"], format!("contract:{h}"));
        assert_eq!(
            p.minted.epoch.as_deref(),
            Some(format!("contract:{h}").as_str())
        );
        assert_eq!(p.minted.ai_system_id.as_deref(), Some("rg-sys"));
        assert!(
            p.minted.record["references"]
                .as_array()
                .unwrap()
                .iter()
                .any(|r| r["type"] == "ai.brutor.ai-system-contract")
        );
    }

    #[test]
    fn allow_list_rejects_identity_and_free_text() {
        for bad in [
            json!({"subject_id": "u-1"}),
            json!({"user_id": "u-1"}),
            json!({"api_key_id": "k"}),
            json!({"email": "a@b"}),
            json!({"rationale": "x"}),
        ] {
            let r = req(json!({"record_type": "ai.brutor.declaration", "context": bad}));
            assert!(err(prepare(0, "t", &r, None, now())).contains("not admitted"));
        }
        let long = "x".repeat(MAX_CONTEXT_VALUE + 1);
        let r = req(json!({"record_type": "ai.brutor.incident", "context": {"state": long}}));
        assert!(err(prepare(0, "t", &r, None, now())).contains("short opaque scalar"));
        let r = req(json!({"record_type": "ai.brutor.incident", "context": {"state": {"x":1}}}));
        assert!(prepare(0, "t", &r, None, now()).is_err());
    }

    #[test]
    fn unknown_top_level_members_are_rejected() {
        let body = json!({"tenant_id":"t","records":[{"record_type":"ai.brutor.declaration","subject_id":"u-1"}]});
        match parse(body) {
            Err(SealApiError::Invalid(m)) => assert!(m.contains("subject_id"), "{m}"),
            other => panic!("{other:?}"),
        }
        let many: Vec<Value> = (0..=MAX_RECORDS)
            .map(|_| json!({"record_type":"ai.brutor.report"}))
            .collect();
        assert!(parse(json!({"tenant_id":"t","records": many})).is_err());
        assert!(parse(json!({"tenant_id":"t","records": []})).is_err());
    }

    #[test]
    fn honesty_invariant_is_a_422() {
        let r = req(
            json!({"record_type":"ai.brutor.run-abort","verdict":"resolved",
            "disposition":{"decision":"reject","approver":"policy","human_disposed":true}}),
        );
        assert!(err(prepare(0, "t", &r, None, now())).contains("human_disposed"));
    }

    #[test]
    fn vocabulary_and_shapes_are_checked() {
        for bad in [
            json!({"record_type":"declaration"}),
            json!({"record_type":"ai.brutor.x","verdict":"approved"}),
            json!({"record_type":"ai.brutor.x","kind":"maybe"}),
            json!({"record_type":"ai.brutor.x","timestamp":"yesterday"}),
            json!({"record_type":"ai.brutor.x","references":[{"type":"ai.brutor.rubric","digest":"xyz"}]}),
            json!({"record_type":"ai.brutor.x","chain":{"parent_record_id":"a".repeat(64),"relation":"likes"}}),
            json!({"record_type":"ai.brutor.x","chain":{"parent_record_id":"nothex","relation":"assesses"}}),
            json!({"record_type":"ai.brutor.x","context":{"record_type":"ai.brutor.y"}}),
        ] {
            let r = req(bad.clone());
            assert!(prepare(0, "t", &r, None, now()).is_err(), "{bad}");
        }
        // The same request twice gets two different default action ids.
        let r = req(json!({"record_type":"ai.brutor.report"}));
        let a = prepare(0, "t", &r, None, now()).unwrap();
        let b = prepare(0, "t", &r, None, now()).unwrap();
        assert_ne!(a.minted.record_id, b.minted.record_id);
    }
}
