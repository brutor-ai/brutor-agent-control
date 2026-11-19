// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! `ProxyLogInsert` → action record.
//!
//! # Data admission
//!
//! This module is the only place a proxy-log row is read into a record,
//! and it reads an explicit allow-list of fields. It never touches
//! `subject_id`, `api_key_id`, `end_user_session_id`, the `gateway_user_*`
//! / `idp_user_*` identity columns, `client_ip`, `user_agent`, headers or
//! body previews — the never-enters class. A static test greps this file
//! for those identifiers, so adding one is a build failure, not a review
//! comment. Tool inputs and outputs enter only as the content
//! digests (or, under `salt_digests`, the salted digests the writer
//! computed from the same captured value before retention touched it);
//! opaque run/turn/conversation handles enter in clear.
//!
//! # Verdicts
//!
//! The mapping from row state to `verdict` is one table, pinned row by row in
//! this module's tests. Refusals are sealed with **no `effect`** so the derived effect mode is
//! `not_applicable`; an executed action carries `effect.status: confirmed`
//! with the response digest, because the gateway observed the response.
//!
//! # Options
//!
//! What gets sealed beyond the defaults is profile-driven
//! ([`MintOptions`], resolved by the writer from cached lookups — never a
//! per-row query): LLM completions under `inference_records`, read-only
//! tools on sensitive resource groups, salted digests, and the
//! `confirms` chain of a two-phase action to its `planned` record.

use crate::constants::proxy_subtype as st;
use crate::models::db::ProxyLogInsert;
use crate::services::evidence::attribution::{Attribution, EvidenceOptions};
use brutor_evidence::record::{
    self, Attestation, Chain, Control, Disposition, Effect, Model, RecordBuilder, RecordError,
    Reference, System, approver, control_result, decision, effect_status, kind, relation, verdict,
};
use serde_json::{Map, Value, json};

/// A built-but-not-yet-persisted record.
#[derive(Debug, Clone)]
pub struct Minted {
    pub row_index: usize,
    pub tenant_id: String,
    pub record: Value,
    pub record_id: String,
    pub ai_system_id: Option<String>,
    pub root_task_id: Option<String>,
    /// `None` for records sealed at write (declarations, notices, reports).
    pub proxy_log_id: Option<String>,
    pub verdict: String,
    pub kind: String,
    pub surface: String,
    pub epoch: Option<String>,
    pub timestamp: chrono::DateTime<chrono::Utc>,
    pub tags: Option<Value>,
    /// Approval request id an `awaiting_human` record was raised for, so a
    /// later outcome can chain to it.
    pub approval_request_id: Option<String>,
    /// Approval request id this outcome consumed (chain `supersedes`).
    pub consumed_approval_request_id: Option<String>,
}

/// Salted content digests for one row (`salt_digests`), computed by
/// the writer from the bodies as captured — the same value the row
/// digests commit to — before any retention mode truncates or drops them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SaltedDigests {
    /// Lowercase hex; carried in `attestation.digest_salt`.
    pub salt: String,
    pub request: Option<String>,
    pub response: Option<String>,
    pub numbers_canonicalized: bool,
}

impl SaltedDigests {
    /// Fresh 128-bit random salt.
    pub fn compute(request_body: Option<&str>, response_body: Option<&str>) -> Self {
        let salt: [u8; 16] = rand::random();
        Self::compute_with_salt(&hex::encode(salt), request_body, response_body)
    }

    /// Deterministic given the salt (what a verifier recomputes).
    pub fn compute_with_salt(
        salt: &str,
        request_body: Option<&str>,
        response_body: Option<&str>,
    ) -> Self {
        let mut numbers = false;
        let mut digest = |body: Option<&str>| {
            body.and_then(|b| {
                let (v, changed) = brutor_evidence::canon::body_value(b);
                numbers |= changed;
                brutor_evidence::canon::salted_digest_hex(&v, salt).ok()
            })
        };
        let request = digest(request_body);
        let response = digest(response_body);
        Self {
            salt: salt.to_owned(),
            request,
            response,
            numbers_canonicalized: numbers,
        }
    }
}

/// Per-row inputs resolved by the writer (all from process caches).
#[derive(Debug, Clone, Default)]
pub struct MintOptions {
    pub evidence: EvidenceOptions,
    /// `readOnlyHint` of the called MCP tool; `None` = unknown (sealed).
    pub tool_read_only: Option<bool>,
    /// Present when the system salts digests and the bodies were captured.
    pub salted: Option<SaltedDigests>,
    /// The `planned` record this outcome confirms (two-phase actions).
    pub confirms: Option<String>,
    /// The notice record sealed for this row's conversation.
    pub notice_record_id: Option<String>,
}

/// The evidence surface of a row's `proxy_type`, or `None` for a type that
/// is not an action surface (OAuth — see the class tests below).
///
/// The batch surface is an LLM surface: a batch job is inference submitted
/// in bulk, and a refusal at its door (residency) is a refusal of LLM
/// traffic. It used to map to nothing, so the batch residency refusal —
/// written, correctly, with its own `proxy_type` — was never sealed. Only
/// its refusals seal: a batch row that is not one is job management
/// (submit / read / cancel), not an inference ([`is_inference`]).
pub(crate) fn surface_of(log: &ProxyLogInsert) -> Option<&'static str> {
    use crate::constants::proxy;
    match log.proxy_type.as_str() {
        proxy::MCP | proxy::VIRTUAL_MCP => Some("mcp"),
        proxy::A2A => Some("a2a"),
        proxy::SKILL => Some("skill"),
        proxy::LLM | proxy::BATCH => Some("llm"),
        _ => None,
    }
}

/// Refusal subtypes that map to `denied` (policy) — every one of them is a
/// gate saying no, never an upstream failure.
const DENY_SUBTYPES: &[&str] = &[
    st::AGENT_AUTHZ_BLOCK,
    st::SEMANTIC_POLICY_BLOCK,
    st::AUTONOMY_BLOCK,
    st::RESIDENCY_VIOLATION,
    st::RUN_ABORTED,
    st::RUN_CAP_REACHED,
    st::ACCESS_DENIED,
    st::SKILL_QUOTA_EXCEEDED,
    st::SKILL_CONCURRENCY_LIMIT,
    st::APPROVAL_TOKEN_INVALID,
    st::UNACCEPTED_CALLER,
    st::LLM_GOVERNANCE_BLOCK,
];

fn is_refusal(log: &ProxyLogInsert) -> bool {
    if log.guardrail_blocked || log.argument_policy_denied {
        return true;
    }
    if log.agent_authz_decision.as_deref() == Some(crate::constants::agent_decision::DENY) {
        return true;
    }
    match log.proxy_subtype.as_deref() {
        Some(s) if DENY_SUBTYPES.contains(&s) => true,
        Some(
            st::APPROVAL_REQUIRED
            | st::APPROVAL_APPROVED
            | st::APPROVAL_REJECTED
            | st::ENGINE_FAILURE,
        ) => true,
        Some(s) => s.starts_with("rate_limit_"),
        None => false,
    }
}

/// An LLM row that is a generation (not an embedding, a model listing or a
/// cache-only probe).
fn is_inference(log: &ProxyLogInsert) -> bool {
    log.proxy_type == crate::constants::proxy::LLM
        && !matches!(log.proxy_subtype.as_deref(), Some(st::EMBEDDINGS))
        && !log.endpoint.contains("embeddings")
}

/// The consequential rule with the default options (see [`should_record_with`]).
pub fn should_record(log: &ProxyLogInsert) -> bool {
    should_record_with(log, &MintOptions::default())
}

/// The consequential rule: commands seal, queries do not, unknown
/// defaults to sealed, every refusal seals. A tool annotated
/// `readOnlyHint: true` is a query unless its resource group is tagged
/// sensitive (`phi_resource` / `sensitive_data`) — the privileged-read
/// rule. LLM completions seal only under `inference_records`.
pub fn should_record_with(log: &ProxyLogInsert, opts: &MintOptions) -> bool {
    let Some(surface) = surface_of(log) else {
        return false;
    };
    if is_refusal(log) {
        return true;
    }
    match surface {
        "mcp" => {
            log.mcp_method.as_deref() == Some("tools/call")
                && !(opts.tool_read_only == Some(true) && !opts.evidence.rg_sensitive)
        }
        "a2a" => true,
        "skill" => {
            log.skill_execution_id.is_some()
                || matches!(log.proxy_subtype.as_deref(), Some(st::SKILL_RUN))
        }
        "llm" => opts.evidence.inference_records && is_inference(log),
        _ => false,
    }
}

pub(crate) fn classify(log: &ProxyLogInsert) -> (&'static str, &'static str, &'static str, bool) {
    // (verdict, decision, approver, human_disposed)
    match log.proxy_subtype.as_deref() {
        Some(st::APPROVAL_REQUIRED) => {
            return (
                verdict::AWAITING_HUMAN,
                decision::NEEDS_INPUT,
                approver::POLICY,
                false,
            );
        }
        Some(st::APPROVAL_APPROVED) => {
            return (verdict::RESOLVED, decision::ACCEPT, approver::HUMAN, true);
        }
        Some(st::APPROVAL_REJECTED) => {
            return (verdict::RESOLVED, decision::REJECT, approver::HUMAN, true);
        }
        // A step refused because its run was aborted is `denied` by
        // *policy*: the gate applied the abort. The operator's act is the
        // CP-sealed `ai.brutor.run-abort` record (`resolved`, human); claiming
        // a human disposed of this later step would overstate what happened.
        Some(st::RUN_ABORTED) => {
            return (verdict::DENIED, decision::REJECT, approver::POLICY, false);
        }
        // A gate that failed closed because its engine was down.
        Some(st::ENGINE_FAILURE) => {
            return (
                verdict::ENGINE_FAILURE,
                decision::REJECT,
                approver::POLICY,
                false,
            );
        }
        _ => {}
    }
    if log.guardrail_blocked {
        return (verdict::BLOCKED, decision::REJECT, approver::POLICY, false);
    }
    if is_refusal(log) {
        return (verdict::DENIED, decision::REJECT, approver::POLICY, false);
    }
    let human = log.consumed_approval_request_id.is_some();
    let appr = if human {
        approver::HUMAN
    } else {
        approver::POLICY
    };
    if log.success {
        return (verdict::EXECUTED, decision::ACCEPT, appr, human);
    }
    let timeout = log
        .error_type
        .as_deref()
        .map(|t| t.contains("timeout"))
        .unwrap_or(false);
    if timeout {
        (verdict::TIMEOUT, decision::ACCEPT, appr, human)
    } else {
        (verdict::ERRORED, decision::ACCEPT, appr, human)
    }
}

fn hex64(s: &Option<String>) -> Option<String> {
    s.as_deref()
        .filter(|v| record::is_hex64(v))
        .map(str::to_owned)
}

fn gate(id: &str, kind: &str, fail: bool, severity: Option<String>) -> Control {
    Control {
        id: id.into(),
        result: if fail {
            control_result::FAIL
        } else {
            control_result::PASS
        }
        .into(),
        blocking: fail,
        severity,
        kind: Some(kind.into()),
        evidence_digest: None,
    }
}

fn controls(log: &ProxyLogInsert) -> Vec<Control> {
    let mut out = Vec::new();
    if let Some(check) = &log.guardrail_check {
        out.push(gate(
            &format!("ai.brutor.guardrail.{check}"),
            "guardrail",
            log.guardrail_blocked,
            log.guardrail_action.clone(),
        ));
    }
    if log.argument_policy_id.is_some() || log.argument_policy_denied {
        out.push(gate(
            "ai.brutor.argument_policy",
            "argument_policy",
            log.argument_policy_denied,
            None,
        ));
    }
    let subtype = log.proxy_subtype.as_deref();
    if log.semantic_policy_id.is_some() || subtype == Some(st::SEMANTIC_POLICY_BLOCK) {
        out.push(gate(
            "ai.brutor.semantic_policy",
            "semantic_policy",
            subtype == Some(st::SEMANTIC_POLICY_BLOCK),
            None,
        ));
    }
    if let Some(d) = &log.agent_authz_decision {
        use crate::constants::agent_decision as ad;
        let (result, blocking) = match d.as_str() {
            ad::DENY => (control_result::FAIL, true),
            ad::APPROVAL_REQUIRED => (control_result::NA, false),
            _ => (control_result::PASS, false),
        };
        out.push(Control {
            id: "ai.brutor.agent_grant".into(),
            result: result.into(),
            blocking,
            severity: None,
            kind: Some("agent_grant".into()),
            evidence_digest: None,
        });
    }
    if log.autonomy_level.is_some() || subtype == Some(st::AUTONOMY_BLOCK) {
        out.push(gate(
            "ai.brutor.autonomy",
            "autonomy",
            subtype == Some(st::AUTONOMY_BLOCK),
            log.autonomy_level.clone(),
        ));
    }
    // The identity judge's verdict on the credential: the one fact of the
    // credential a record carries (its reference never enters).
    if let Some(outcome) = &log.credential_policy_outcome {
        let (result, blocking) = match outcome.as_str() {
            "met" => (control_result::PASS, false),
            "below_policy" => (control_result::FAIL, false),
            "exempt" => (control_result::NA, false),
            // `refused`, and any value that is no verdict: a refusal.
            _ => (control_result::FAIL, true),
        };
        out.push(Control {
            id: "ai.brutor.identity_policy".into(),
            result: result.into(),
            blocking,
            severity: None,
            kind: Some("identity_policy".into()),
            evidence_digest: None,
        });
    }
    let refusal = |id: &str, kind: &str| gate(id, kind, true, None);
    match subtype {
        Some(st::RESIDENCY_VIOLATION) => out.push(refusal("ai.brutor.residency", "residency")),
        Some(st::RUN_ABORTED) => out.push(refusal("ai.brutor.run_abort", "intervention")),
        Some(st::RUN_CAP_REACHED) => out.push(refusal("ai.brutor.run_cap", "run_budget")),
        Some(st::ACCESS_DENIED) => out.push(refusal("ai.brutor.access", "access")),
        // The service did not accept this caller system.
        Some(st::UNACCEPTED_CALLER) => out.push(refusal("ai.brutor.accepted_caller", "access")),
        Some(st::ENGINE_FAILURE) => out.push(refusal("ai.brutor.engine", "engine_failure")),
        Some(st::SKILL_QUOTA_EXCEEDED) => out.push(refusal("ai.brutor.skill_quota", "rate_limit")),
        Some(st::SKILL_CONCURRENCY_LIMIT) => {
            out.push(refusal("ai.brutor.skill_concurrency", "rate_limit"))
        }
        Some(st::APPROVAL_TOKEN_INVALID) => {
            out.push(refusal("ai.brutor.approval_token", "approval"))
        }
        Some(s) if s.starts_with("rate_limit_") => {
            out.push(refusal(&format!("ai.brutor.{s}"), "rate_limit"))
        }
        _ => {}
    }
    out
}

fn effect_type(log: &ProxyLogInsert, surface: &str) -> String {
    if let Some(c) = &log.mcp_capability_name {
        return c.clone();
    }
    if let Some(s) = &log.skill_id {
        return format!("skill:{s}");
    }
    match surface {
        "a2a" => "a2a:message".into(),
        "llm" => "inference_completion".into(),
        _ => log.endpoint.clone(),
    }
}

fn system_of(attr: Option<&Attribution>) -> (Option<System>, Option<String>) {
    let system = attr.map(|a| System {
        ai_system_id: a.ai_system_id.clone(),
        contract_version: a.contract_version,
        contract_hash: a.contract_hash.clone(),
    });
    let epoch = attr
        .and_then(|a| a.contract_hash.as_ref())
        .map(|h| format!("contract:{h}"));
    (system, epoch)
}

fn sha_ref(kind: &str, digest: String, purpose: Option<&str>) -> Reference {
    Reference {
        kind: kind.into(),
        digest_alg: "SHA-256".into(),
        digest,
        purpose: purpose.map(str::to_owned),
    }
}

fn contract_ref(attr: Option<&Attribution>) -> Option<Reference> {
    attr.and_then(|a| a.contract_hash.clone())
        .filter(|h| record::is_hex64(h))
        .map(|h| sha_ref("ai.brutor.ai-system-contract", h, None))
}

/// Build the record for one row with the default options.
pub fn build(
    log: &ProxyLogInsert,
    attr: Option<&Attribution>,
) -> Result<Option<Minted>, RecordError> {
    build_with(log, attr, &MintOptions::default())
}

/// Build the record for one row. `Ok(None)` when the row is on no evidence
/// surface.
pub fn build_with(
    log: &ProxyLogInsert,
    attr: Option<&Attribution>,
    opts: &MintOptions,
) -> Result<Option<Minted>, RecordError> {
    let Some(surface) = surface_of(log) else {
        return Ok(None);
    };
    let (v, dec, appr, human) = classify(log);
    let timestamp = log
        .timestamp
        .to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
    let mut b = RecordBuilder::new(
        format!("{surface}/{}", log.id),
        log.tenant_id.clone(),
        timestamp,
    );
    b.kind = kind::DECIDE.into();
    b.verdict = v.into();
    let (system, epoch) = system_of(attr);
    b.system = system;
    b.epoch = epoch.clone();
    b.disposition = Some(Disposition {
        decision: dec.into(),
        approver: appr.into(),
        human_disposed: human,
        authority: log.consumed_approval_request_id.clone(),
        reason_digest: None,
    });
    // The digests the record commits to. Salted when the system
    // salts; when it salts but the bodies were not captured (a backfill
    // from pruned rows), no digest at all rather than an unsalted one the
    // policy forbids publishing.
    let (req, resp, salt, numbers) = match (&opts.salted, opts.evidence.salt_digests) {
        (Some(s), _) => (
            s.request.clone(),
            s.response.clone(),
            Some(s.salt.clone()),
            s.numbers_canonicalized,
        ),
        (None, true) => (None, None, None, false),
        (None, false) => (
            hex64(&log.request_content_hash),
            hex64(&log.response_content_hash),
            None,
            false,
        ),
    };
    let dispatched = |status: &str, response: Option<String>| Effect {
        kind: effect_type(log, surface),
        status: status.into(),
        request_digest: req.clone(),
        response_digest: response,
        attestation: record::ATTESTATION_GATE_OBSERVED.into(),
        irreversibility: None,
    };
    b.effect = match v {
        verdict::EXECUTED => Some(dispatched(
            if resp.is_some() {
                effect_status::CONFIRMED
            } else {
                effect_status::DISPATCHED
            },
            resp.clone(),
        )),
        verdict::ERRORED => Some(dispatched(effect_status::FAILED, resp.clone())),
        verdict::TIMEOUT => Some(dispatched(effect_status::DISPATCHED, None)),
        _ => None,
    };
    b.controls = controls(log);
    if log.llm_model_used.is_some() || log.deciding_model.is_some() {
        b.model = Some(Model {
            model_id: log.llm_model_used.clone(),
            provider: log.llm_provider.clone(),
            deciding_model: log.deciding_model.clone(),
        });
    }
    b.attestation = Some(Attestation {
        runtime: record::RUNTIME.into(),
        observation: record::OBSERVATION_IN_PATH.into(),
        input_digest: req,
        output_digest: resp,
        digest_salt: salt,
        numbers_canonicalized: numbers,
    });
    let mut ctx = Map::new();
    ctx.insert("surface".into(), json!(surface));
    ctx.insert("proxy_log_id".into(), json!(log.id));
    if let Some(r) = &log.root_task_id {
        ctx.insert("root_task_id".into(), json!(r));
    }
    if let Some(t) = &log.turn_id {
        ctx.insert("turn_id".into(), json!(t));
    }
    if let Some(s) = &log.proxy_subtype {
        ctx.insert("proxy_subtype".into(), json!(s));
    }
    if let Some(v) = &log.server_version {
        ctx.insert("server_version".into(), json!(v));
    }
    if let Some(v) = &log.tool_definition_hash {
        ctx.insert("tool_definition_hash".into(), json!(v));
    }
    if let Some(v) = &log.autonomy_level {
        ctx.insert("autonomy_level".into(), json!(v));
    }
    if let Some(v) = &log.autonomy_provenance {
        ctx.insert("autonomy_provenance".into(), json!(v));
    }
    if let Some(code) = log.http_status_code {
        ctx.insert("http_status".into(), json!(code));
    }
    if let Some(tags) = &log.compliance_tags
        && tags.is_array()
    {
        ctx.insert("tags".into(), tags.clone());
    }
    let meta = |k: &str| {
        log.custom_metadata
            .as_ref()
            .and_then(|m| m.get(k))
            .and_then(Value::as_str)
    };
    // Re-validated: only a ULID is a clear-safe conversation handle.
    let conversation = meta("conversation_handle")
        .and_then(crate::services::evidence::request::parse_conversation_handle);
    if let Some(h) = &conversation {
        ctx.insert("conversation_handle".into(), json!(h));
    }
    let approval_request_id = if v == verdict::AWAITING_HUMAN {
        meta("approval_request_id").map(str::to_owned)
    } else {
        None
    };
    if let Some(id) = &approval_request_id {
        ctx.insert("approval_request_id".into(), json!(id));
    }
    // A refused agent credential: the route it was
    // refused on, how many refusals the record stands for, and the kind of
    // credential. Each re-validated: a route label of fixed words, a number,
    // one of two names. Never the credential.
    if log.proxy_subtype.as_deref() == Some(st::AGENT_AUTHZ_BLOCK) {
        use crate::services::refused_agent_credential as refused;
        if let Some(route) = meta(refused::META_ROUTE).filter(|r| refused::is_route_label(r)) {
            ctx.insert(refused::META_ROUTE.into(), json!(route));
        }
        if let Some(kind) =
            meta(refused::META_CREDENTIAL).filter(|k| refused::Credential::is_kind(k))
        {
            ctx.insert(refused::META_CREDENTIAL.into(), json!(kind));
        }
        if let Some(n) = log
            .custom_metadata
            .as_ref()
            .and_then(|m| m.get(refused::META_REFUSALS))
            .and_then(Value::as_u64)
        {
            ctx.insert(refused::META_REFUSALS.into(), json!(n));
        }
    }
    b.context = ctx;

    // The outcome of a two-phase action confirms its planned record.
    // (An outcome that consumed an approval is re-chained `supersedes` by
    // the store; the approval chain is the authoritative one.)
    if let Some(p) = opts.confirms.as_ref().filter(|p| record::is_hex64(p)) {
        b.chain = Some(Chain {
            parent_record_id: p.clone(),
            relation: relation::CONFIRMS.into(),
        });
    }

    let mut refs = Vec::new();
    if let Some(h) = hex64(&log.row_hash) {
        refs.push(sha_ref("ai.brutor.proxy-log-row", h, None));
    }
    refs.extend(contract_ref(attr));
    if let Some(h) = hex64(&log.tool_definition_hash) {
        refs.push(sha_ref("ai.brutor.mcp-tool-definition", h, None));
    }
    // The conversation's assistant turn cites the notice rendered
    // before it; `notice.before_first_turn` checks order in the log.
    if surface == "llm"
        && conversation.is_some()
        && let Some(n) = opts
            .notice_record_id
            .as_ref()
            .filter(|n| record::is_hex64(n))
    {
        refs.push(sha_ref("ai.brutor.notice", n.clone(), Some("responds_to")));
    }
    if !refs.is_empty() {
        b.references = Some(refs);
    }
    let (record, record_id) = b.seal()?;
    Ok(Some(Minted {
        row_index: 0,
        tenant_id: log.tenant_id.clone(),
        record,
        record_id,
        ai_system_id: attr.map(|a| a.ai_system_id.clone()),
        root_task_id: log.root_task_id.clone(),
        proxy_log_id: Some(log.id.clone()),
        verdict: v.into(),
        kind: kind::DECIDE.into(),
        surface: surface.into(),
        epoch,
        timestamp: log.timestamp,
        tags: log.compliance_tags.clone(),
        approval_request_id,
        consumed_approval_request_id: log.consumed_approval_request_id.clone(),
    }))
}

/// What a two-phase dispatch knows about itself, before any row exists.
#[derive(Debug, Clone)]
pub struct PlannedAction {
    pub tenant_id: String,
    /// Evidence surface: `mcp` / `a2a` / `skill`.
    pub surface: &'static str,
    /// The id the outcome row will carry (reserved in the request scope).
    pub proxy_log_id: String,
    /// `effect.type` — the tool name, `a2a:message` or `skill:<id>`.
    pub effect_type: String,
    pub root_task_id: Option<String>,
    /// An approval whose token authorised this dispatch.
    pub consumed_approval_request_id: Option<String>,
    pub timestamp: chrono::DateTime<chrono::Utc>,
}

/// The `planned` record of a two-phase action : the gates
/// accepted the action and it is being dispatched. `effect.status:
/// planned`, no digests (nothing has been observed yet), and the same
/// `action_id` the outcome record will carry.
pub fn build_planned(p: &PlannedAction, attr: Option<&Attribution>) -> Result<Minted, RecordError> {
    let timestamp = p
        .timestamp
        .to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
    let mut b = RecordBuilder::new(
        format!("{}/{}", p.surface, p.proxy_log_id),
        p.tenant_id.clone(),
        timestamp,
    );
    b.kind = kind::DECIDE.into();
    b.verdict = verdict::PLANNED.into();
    let (system, epoch) = system_of(attr);
    b.system = system;
    b.epoch = epoch.clone();
    let human = p.consumed_approval_request_id.is_some();
    b.disposition = Some(Disposition {
        decision: decision::ACCEPT.into(),
        approver: if human {
            approver::HUMAN
        } else {
            approver::POLICY
        }
        .into(),
        human_disposed: human,
        authority: p.consumed_approval_request_id.clone(),
        reason_digest: None,
    });
    b.effect = Some(Effect {
        kind: p.effect_type.clone(),
        status: effect_status::PLANNED.into(),
        request_digest: None,
        response_digest: None,
        attestation: record::ATTESTATION_GATE_OBSERVED.into(),
        irreversibility: None,
    });
    let mut ctx = Map::new();
    ctx.insert("surface".into(), json!(p.surface));
    ctx.insert("proxy_log_id".into(), json!(p.proxy_log_id));
    if let Some(r) = &p.root_task_id {
        ctx.insert("root_task_id".into(), json!(r));
    }
    b.context = ctx;
    if let Some(r) = contract_ref(attr) {
        b.references = Some(vec![r]);
    }
    let (record, record_id) = b.seal()?;
    Ok(Minted {
        row_index: 0,
        tenant_id: p.tenant_id.clone(),
        record,
        record_id,
        ai_system_id: attr.map(|a| a.ai_system_id.clone()),
        root_task_id: p.root_task_id.clone(),
        // The row does not exist yet, and `proxy_logs.record_id` names the
        // outcome; the planned record is found by `action_id`.
        proxy_log_id: None,
        verdict: verdict::PLANNED.into(),
        kind: kind::DECIDE.into(),
        surface: p.surface.into(),
        epoch,
        timestamp: p.timestamp,
        tags: None,
        approval_request_id: None,
        consumed_approval_request_id: None,
    })
}

/// Where the trees a test reads are (the test suite's helper).
#[cfg(test)]
#[path = "../../../tests/support/roots.rs"]
mod roots;

#[cfg(test)]
mod tests {
    use super::*;

    /// The never-enters class: identifiers this module must
    /// never reference. Grepping the source is deliberate — a refactor that
    /// threads one of these through a helper still trips it.
    #[test]
    fn mint_never_reads_a_never_enters_field() {
        const SRC: &str = include_str!("mint.rs");
        let body = SRC.split("#[cfg(test)]").next().unwrap();
        for forbidden in [
            "subject_id",
            "subject_kind",
            "api_key_id",
            "api_key_name",
            "end_user_session_id",
            "gateway_user_id",
            "gateway_user_username",
            "idp_user_id",
            "idp_user_username",
            "idp_user_email",
            "client_ip",
            "user_agent",
            "request_headers",
            "response_headers",
            "request_body_preview",
            "response_body_preview",
            "actor_agent_id",
            "credential_ref",
        ] {
            let mentions = body.matches(forbidden).count();
            // The doc comment at the top of the file lists these by name once
            // each; any further mention is a read.
            assert!(
                mentions <= 1,
                "{forbidden} referenced {mentions} times in mint.rs — never-enters fields must not reach a record"
            );
        }
    }

    #[test]
    fn admitted_record_fields_are_the_schema_allow_list() {
        // Every top-level member the builder can emit is in the crate's
        // allow-list, so the verifier's `unadmitted_field` check and the
        // builder agree.
        for f in [
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
        ] {
            assert!(record::RECORD_ADMITTED_FIELDS.contains(&f));
        }
    }

    // ---- verdict matrix ----------------------------------

    use crate::models::auth::{AuthContext, AuthType};
    use crate::services::proxy_logger::build_proxy_log;

    fn auth() -> AuthContext {
        AuthContext {
            credential_kind: crate::models::auth::CredentialKind::ApiKey,
            credential_assurance: Some(crate::models::auth::CredentialAssurance::Key),
            credential_ref: None,
            credential_detail: Default::default(),
            identity_verdict: None,
            auth_type: AuthType::ApiKey,
            tenant_id: "tenant-a".to_string(),
            gateway_user_id: None,
            gateway_username: None,
            gateway_tenant_id: None,
            idp_user_id: None,
            idp_username: None,
            idp_email: None,
            api_key_id: Some("key-secret-id".to_string()),
            api_key_name: None,
            api_key_scope: None,
            api_key_group_id: None,
            api_key_is_platform: false,
            api_key_user_id: None,
            allowed_skill_ids: None,
            session_key: None,
            client_ip: Some("10.0.0.9".to_string()),
            resolved_group_id: None,
            actor_agent_id: None,
            session_token_type: None,
            agent_impl: crate::services::agent_impl::request_carriers(),
            admission_hold: None,
        }
    }

    fn mcp_row(status: u16, success: bool) -> ProxyLogInsert {
        let mut log = build_proxy_log(
            "mcp",
            &auth(),
            "POST",
            "/mcp/orders",
            None,
            Some("srv-1"),
            status,
            12.0,
            success,
            if success {
                None
            } else {
                Some("upstream failed")
            },
            "corr-1",
        );
        log.mcp_method = Some("tools/call".into());
        log.mcp_capability_name = Some("write_order".into());
        log.request_content_hash = Some("a".repeat(64));
        log.response_content_hash = Some("b".repeat(64));
        log.row_hash = Some("c".repeat(64));
        log.root_task_id = Some("run-1".into());
        log.subject_id = Some("user-42".into());
        log
    }

    fn attr() -> Attribution {
        Attribution {
            ai_system_id: "sys-1".into(),
            contract_id: Some("ctr-1".into()),
            contract_hash: Some("d".repeat(64)),
            contract_version: Some(3),
        }
    }

    fn built(log: &ProxyLogInsert) -> Minted {
        build(log, Some(&attr())).unwrap().expect("record")
    }

    #[test]
    fn executed_call_is_confirmed_with_digests_and_references() {
        let m = built(&mcp_row(200, true));
        assert_eq!(m.verdict, verdict::EXECUTED);
        let r = &m.record;
        assert_eq!(r["effect"]["status"], "confirmed");
        assert_eq!(r["effect"]["response_digest"], "b".repeat(64));
        assert_eq!(
            r["effect"]["attestation"],
            record::ATTESTATION_GATE_OBSERVED
        );
        assert_eq!(r["system"]["ai_system_id"], "sys-1");
        assert_eq!(r["epoch"], format!("contract:{}", "d".repeat(64)));
        assert_eq!(r["disposition"]["approver"], "policy");
        assert_eq!(r["disposition"]["human_disposed"], false);
        let kinds: Vec<&str> = r["references"]
            .as_array()
            .unwrap()
            .iter()
            .map(|x| x["type"].as_str().unwrap())
            .collect();
        assert!(kinds.contains(&"ai.brutor.proxy-log-row"));
        assert!(kinds.contains(&"ai.brutor.ai-system-contract"));
        let v = brutor_evidence::verify(r, None);
        assert!(v.ok, "{:?}", v.findings);
        assert_eq!(v.effect_mode, Some("confirmed"));
        // never-enters: the identity that was on the row is not on the record
        let dumped = serde_json::to_string(r).unwrap();
        assert!(!dumped.contains("user-42"));
        assert!(!dumped.contains("key-secret-id"));
        assert!(!dumped.contains("10.0.0.9"));
    }

    #[test]
    fn errored_and_timeout_rows() {
        let m = built(&mcp_row(502, false));
        assert_eq!(m.verdict, verdict::ERRORED);
        assert_eq!(m.record["effect"]["status"], "failed");
        assert!(brutor_evidence::verify(&m.record, None).ok);

        let mut t = mcp_row(504, false);
        t.error_type = Some("upstream_timeout".into());
        t.response_content_hash = None;
        let m = built(&t);
        assert_eq!(m.verdict, verdict::TIMEOUT);
        assert_eq!(m.record["effect"]["status"], "dispatched");
        let v = brutor_evidence::verify(&m.record, None);
        assert!(v.ok, "{:?}", v.findings);
        assert_eq!(v.effect_mode, Some("dispatched_unconfirmed"));
    }

    /// The identity judge's verdict enters a record as one control, and
    /// nothing else of the credential does.
    #[test]
    fn the_identity_verdict_is_one_control_and_the_reference_never_enters() {
        for (outcome, result, blocking) in [
            ("met", "pass", false),
            ("below_policy", "fail", false),
            ("refused", "fail", true),
            ("exempt", "n/a", false),
        ] {
            let mut row = mcp_row(
                if outcome == "refused" { 403 } else { 200 },
                outcome != "refused",
            );
            if outcome == "refused" {
                row.proxy_subtype = Some(st::ACCESS_DENIED.into());
                row.error_type = Some("credential_policy".into());
            }
            row.credential_kind = Some("api_key".into());
            row.credential_assurance = Some("key".into());
            row.credential_ref = Some("api_key:key-secretish-1".into());
            row.credential_policy_outcome = Some(outcome.into());
            let m = built(&row);
            let controls = m.record["controls"].as_array().expect("controls");
            let identity: Vec<&Value> = controls
                .iter()
                .filter(|c| c["id"] == "ai.brutor.identity_policy")
                .collect();
            assert_eq!(identity.len(), 1, "{outcome}: {controls:?}");
            assert_eq!(identity[0]["kind"], "identity_policy");
            assert_eq!(identity[0]["result"], result, "{outcome}");
            assert_eq!(identity[0]["blocking"], blocking, "{outcome}");
            let text = m.record.to_string();
            for leaked in [
                "api_key:key-secretish-1",
                "credential_ref",
                "credential_kind",
                "credential_assurance",
            ] {
                assert!(
                    !text.contains(leaked),
                    "{outcome}: {leaked} entered the record"
                );
            }
            if outcome == "refused" {
                assert_eq!(m.verdict, verdict::DENIED);
                assert!(m.record.get("effect").is_none());
            }
            let v = brutor_evidence::verify(&m.record, None);
            assert!(v.ok, "{outcome}: {:?}", v.findings);
        }
        // No verdict, no control.
        let m = built(&mcp_row(200, true));
        let controls = m.record["controls"].as_array().cloned().unwrap_or_default();
        assert!(
            controls
                .iter()
                .all(|c| c["id"] != "ai.brutor.identity_policy")
        );
    }

    #[test]
    fn refusals_carry_no_effect_and_the_control_that_fired() {
        let mut g = mcp_row(403, false);
        g.guardrail_blocked = true;
        g.guardrail_check = Some("pii".into());
        g.guardrail_action = Some("block".into());
        let m = built(&g);
        assert_eq!(m.verdict, verdict::BLOCKED);
        assert!(m.record.get("effect").is_none());
        assert_eq!(m.record["controls"][0]["id"], "ai.brutor.guardrail.pii");
        assert_eq!(m.record["controls"][0]["result"], "fail");
        assert_eq!(m.record["controls"][0]["blocking"], true);
        let v = brutor_evidence::verify(&m.record, None);
        assert!(v.ok, "{:?}", v.findings);
        assert_eq!(v.effect_mode, Some("not_applicable"));

        let mut d = mcp_row(403, false);
        d.agent_authz_decision = Some("deny".into());
        d.proxy_subtype = Some(st::AGENT_AUTHZ_BLOCK.into());
        let m = built(&d);
        assert_eq!(m.verdict, verdict::DENIED);
        assert_eq!(m.record["controls"][0]["id"], "ai.brutor.agent_grant");
    }

    #[test]
    fn approval_lifecycle_rows() {
        let mut req = mcp_row(202, false);
        req.proxy_subtype = Some(st::APPROVAL_REQUIRED.into());
        req.custom_metadata = Some(json!({"approval_request_id": "apr-9"}));
        let m = built(&req);
        assert_eq!(m.verdict, verdict::AWAITING_HUMAN);
        assert_eq!(m.approval_request_id.as_deref(), Some("apr-9"));
        assert_eq!(m.record["context"]["approval_request_id"], "apr-9");
        assert!(brutor_evidence::verify(&m.record, None).ok);

        let mut ok = mcp_row(200, true);
        ok.consumed_approval_request_id = Some("apr-9".into());
        let m = built(&ok);
        assert_eq!(m.verdict, verdict::EXECUTED);
        assert_eq!(m.record["disposition"]["approver"], "human");
        assert_eq!(m.record["disposition"]["human_disposed"], true);
        assert_eq!(m.record["disposition"]["authority"], "apr-9");
        assert_eq!(m.consumed_approval_request_id.as_deref(), Some("apr-9"));

        let mut rej = mcp_row(403, false);
        rej.proxy_subtype = Some(st::APPROVAL_REJECTED.into());
        let m = built(&rej);
        assert_eq!(m.verdict, verdict::RESOLVED);
        assert_eq!(m.record["disposition"]["decision"], "reject");
        assert!(m.record.get("effect").is_none());
    }

    #[test]
    fn consequential_rule() {
        let call = mcp_row(200, true);
        assert!(should_record(&call));
        let mut list = mcp_row(200, true);
        list.mcp_method = Some("tools/list".into());
        assert!(!should_record(&list));
        let mut blocked_list = list.clone();
        blocked_list.guardrail_blocked = true;
        assert!(should_record(&blocked_list), "every refusal seals");
        let mut llm = mcp_row(200, true);
        llm.proxy_type = "llm".into();
        llm.mcp_method = None;
        assert!(!should_record(&llm), "inference is off by default");
        let mut oauth = mcp_row(200, true);
        oauth.proxy_type = "oauth".into();
        assert!(!should_record(&oauth));
    }

    #[test]
    fn ungoverned_traffic_still_seals() {
        let m = build(&mcp_row(200, true), None).unwrap().unwrap();
        assert_eq!(m.record["system"], "ungoverned");
        assert!(m.record.get("epoch").is_none());
        assert!(brutor_evidence::verify(&m.record, None).ok);
    }

    // ---- verdict table, one assertion per row -------------------------------

    fn verified(m: &Minted) -> &'static str {
        let v = brutor_evidence::verify(&m.record, None);
        assert!(v.ok, "{:?}", v.findings);
        v.effect_mode.expect("effect mode")
    }

    fn assert_policy_denial(log: &ProxyLogInsert, control: &str) {
        assert!(should_record(log), "every refusal seals");
        let m = built(log);
        assert_eq!(m.verdict, verdict::DENIED, "{:?}", log.proxy_subtype);
        let d = &m.record["disposition"];
        assert_eq!(d["decision"], "reject");
        assert_eq!(d["approver"], "policy");
        assert_eq!(d["human_disposed"], false);
        assert!(m.record.get("effect").is_none(), "refusals carry no effect");
        assert_eq!(verified(&m), "not_applicable");
        let ids: Vec<&str> = m.record["controls"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| c["id"].as_str().unwrap())
            .collect();
        assert!(ids.contains(&control), "{control} not in {ids:?}");
    }

    /// A service refusing a caller it did not accept is a
    /// policy denial on the A2A surface, never a failed delegation.
    #[test]
    fn row_unaccepted_caller_is_denied_by_policy() {
        let mut a = mcp_row(403, false);
        a.proxy_type = crate::constants::proxy::A2A.into();
        a.mcp_method = None;
        a.mcp_capability_name = None;
        a.proxy_subtype = Some(st::UNACCEPTED_CALLER.into());
        assert_policy_denial(&a, "ai.brutor.accepted_caller");
    }

    #[test]
    fn row_denials_policy_autonomy_residency_quota_run_cap() {
        let mut autonomy = mcp_row(403, false);
        autonomy.proxy_subtype = Some(st::AUTONOMY_BLOCK.into());
        assert_policy_denial(&autonomy, "ai.brutor.autonomy");

        let mut residency = mcp_row(403, false);
        residency.proxy_subtype = Some(st::RESIDENCY_VIOLATION.into());
        assert_policy_denial(&residency, "ai.brutor.residency");

        let mut quota = mcp_row(429, false);
        quota.proxy_subtype = Some("rate_limit_rpm".into());
        assert_policy_denial(&quota, "ai.brutor.rate_limit_rpm");

        let mut cap = mcp_row(429, false);
        cap.proxy_subtype = Some(st::RUN_CAP_REACHED.into());
        assert_policy_denial(&cap, "ai.brutor.run_cap");

        let mut sgp = mcp_row(403, false);
        sgp.proxy_subtype = Some(st::SEMANTIC_POLICY_BLOCK.into());
        assert_policy_denial(&sgp, "ai.brutor.semantic_policy");

        let mut arg = mcp_row(403, false);
        arg.argument_policy_denied = true;
        assert_policy_denial(&arg, "ai.brutor.argument_policy");
    }

    #[test]
    fn row_aborted_run_step_is_denied_by_policy_not_by_a_human() {
        // The abort itself is the CP's `resolved`/human record; the
        // later step the gate refused is `denied` reject/policy/false.
        let mut a = mcp_row(403, false);
        a.proxy_subtype = Some(st::RUN_ABORTED.into());
        assert_policy_denial(&a, "ai.brutor.run_abort");
    }

    #[test]
    fn row_skill_refusals_map_to_denied() {
        for (sub, control) in [
            (st::ACCESS_DENIED, "ai.brutor.access"),
            (st::SKILL_QUOTA_EXCEEDED, "ai.brutor.skill_quota"),
            (st::SKILL_CONCURRENCY_LIMIT, "ai.brutor.skill_concurrency"),
            (st::APPROVAL_TOKEN_INVALID, "ai.brutor.approval_token"),
            (st::RUN_CAP_REACHED, "ai.brutor.run_cap"),
        ] {
            let mut s = mcp_row(403, false);
            s.proxy_type = "skill".into();
            s.mcp_method = None;
            s.mcp_capability_name = None;
            s.skill_id = Some("sk-1".into());
            s.proxy_subtype = Some(sub.into());
            assert_policy_denial(&s, control);
        }
        // Before the subtypes, a refused skill run (skill_run, 403) read as a
        // failed execution. A skill_run row that genuinely failed still is.
        let mut failed = mcp_row(500, false);
        failed.proxy_subtype = Some(st::SKILL_RUN.into());
        assert_eq!(built(&failed).verdict, verdict::ERRORED);
    }

    #[test]
    fn row_engine_failure_fail_closed() {
        let mut e = mcp_row(403, false);
        e.proxy_subtype = Some(st::ENGINE_FAILURE.into());
        assert!(should_record(&e));
        let m = built(&e);
        assert_eq!(m.verdict, verdict::ENGINE_FAILURE);
        assert_eq!(m.record["disposition"]["decision"], "reject");
        assert_eq!(m.record["disposition"]["approver"], "policy");
        assert!(m.record.get("effect").is_none());
        assert_eq!(verified(&m), "not_applicable");
    }

    #[test]
    fn row_approval_granted_executes_with_the_human_as_approver() {
        let mut ok = mcp_row(200, true);
        ok.consumed_approval_request_id = Some("apr-1".into());
        let m = built(&ok);
        assert_eq!(m.verdict, verdict::EXECUTED);
        assert_eq!(verified(&m), "confirmed");
        assert_eq!(m.record["disposition"]["authority"], "apr-1");
    }

    // ---- consequential rule -----------------------------------------------

    #[test]
    fn read_only_tools_seal_only_on_sensitive_groups() {
        let call = mcp_row(200, true);
        let ro = MintOptions {
            tool_read_only: Some(true),
            ..Default::default()
        };
        assert!(!should_record_with(&call, &ro), "a query is not sealed");
        let ro_sensitive = MintOptions {
            tool_read_only: Some(true),
            evidence: EvidenceOptions {
                rg_sensitive: true,
                ..Default::default()
            },
            ..Default::default()
        };
        assert!(should_record_with(&call, &ro_sensitive), "privileged read");
        let write = MintOptions {
            tool_read_only: Some(false),
            ..Default::default()
        };
        assert!(should_record_with(&call, &write));
        assert!(
            should_record_with(&call, &MintOptions::default()),
            "unknown annotations default to sealed"
        );
        let mut refused_read = call.clone();
        refused_read.guardrail_blocked = true;
        assert!(
            should_record_with(&refused_read, &ro),
            "refusals always seal"
        );
    }

    // ---- inference records ------------------------------------------------

    fn llm_row(success: bool) -> ProxyLogInsert {
        let mut l = mcp_row(if success { 200 } else { 502 }, success);
        l.proxy_type = "llm".into();
        l.mcp_method = None;
        l.mcp_capability_name = None;
        l.proxy_subtype = Some(st::CHAT_COMPLETIONS.into());
        l.endpoint = "/v1/proxy/llm/chat/completions".into();
        l.llm_model_used = Some("gpt-test".into());
        l.llm_provider = Some("openai".into());
        l
    }

    fn inference_on() -> MintOptions {
        MintOptions {
            evidence: EvidenceOptions {
                inference_records: true,
                ..Default::default()
            },
            ..Default::default()
        }
    }

    #[test]
    fn inference_records_seal_completions_when_the_profile_asks() {
        let ok = llm_row(true);
        assert!(!should_record(&ok), "off by default");
        assert!(should_record_with(&ok, &inference_on()));
        let m = build_with(&ok, Some(&attr()), &inference_on())
            .unwrap()
            .unwrap();
        assert_eq!(m.verdict, verdict::EXECUTED);
        assert_eq!(m.surface, "llm");
        assert_eq!(m.record["effect"]["type"], "inference_completion");
        assert_eq!(m.record["model"]["model_id"], "gpt-test");
        assert_eq!(verified(&m), "confirmed");

        let failed = build_with(&llm_row(false), Some(&attr()), &inference_on())
            .unwrap()
            .unwrap();
        assert_eq!(failed.verdict, verdict::ERRORED);

        let mut emb = llm_row(true);
        emb.proxy_subtype = Some(st::EMBEDDINGS.into());
        emb.endpoint = "/v1/proxy/llm/embeddings".into();
        assert!(
            !should_record_with(&emb, &inference_on()),
            "not a generation"
        );

        let mut refused = llm_row(false);
        refused.proxy_subtype = Some(st::RUN_CAP_REACHED.into());
        assert!(should_record(&refused), "LLM refusals always seal");
        assert_eq!(built(&refused).verdict, verdict::DENIED);
    }

    #[test]
    fn inference_record_cites_the_conversation_notice() {
        let handle = ulid::Ulid::new().to_string();
        let mut l = llm_row(true);
        l.custom_metadata = Some(json!({"conversation_handle": handle}));
        let notice = "e".repeat(64);
        let opts = MintOptions {
            notice_record_id: Some(notice.clone()),
            ..inference_on()
        };
        let m = build_with(&l, Some(&attr()), &opts).unwrap().unwrap();
        assert_eq!(m.record["context"]["conversation_handle"], handle.as_str());
        let r = m.record["references"]
            .as_array()
            .unwrap()
            .iter()
            .find(|r| r["type"] == "ai.brutor.notice")
            .expect("notice reference");
        assert_eq!(r["digest"], notice.as_str());
        assert_eq!(r["purpose"], "responds_to");
        verified(&m);

        // A handle that is not a ULID never reaches the record.
        let mut bad = llm_row(true);
        bad.custom_metadata = Some(json!({"conversation_handle": "user-42"}));
        let m = build_with(&bad, Some(&attr()), &opts).unwrap().unwrap();
        assert!(m.record["context"].get("conversation_handle").is_none());
        assert!(!m.record.to_string().contains("ai.brutor.notice"));
    }

    // ---- salted digests ---------------------------------------------------

    #[test]
    fn salted_digests_replace_the_plain_ones_and_carry_the_salt() {
        let body = r#"{"order_id":"PO-1","total":12.5}"#;
        let salted = SaltedDigests::compute_with_salt(&"ab".repeat(16), Some(body), Some("ok"));
        let again = SaltedDigests::compute_with_salt(&"ab".repeat(16), Some(body), Some("ok"));
        assert_eq!(salted, again, "deterministic given the salt");
        assert!(salted.numbers_canonicalized, "12.5 became a string");
        let fresh = SaltedDigests::compute(Some(body), Some("ok"));
        assert_eq!(fresh.salt.len(), 32);
        assert_ne!(fresh.salt, salted.salt, "a fresh salt per record");
        assert_ne!(fresh.request, salted.request);

        let log = mcp_row(200, true);
        let opts = MintOptions {
            salted: Some(salted.clone()),
            evidence: EvidenceOptions {
                salt_digests: true,
                ..Default::default()
            },
            ..Default::default()
        };
        let m = build_with(&log, Some(&attr()), &opts).unwrap().unwrap();
        let a = &m.record["attestation"];
        assert_eq!(a["digest_salt"], salted.salt.as_str());
        assert_eq!(a["numbers_canonicalized"], true);
        assert_eq!(a["input_digest"], salted.request.clone().unwrap().as_str());
        assert_ne!(
            a["input_digest"],
            "a".repeat(64),
            "not the unsalted row digest"
        );
        assert_eq!(
            m.record["effect"]["response_digest"],
            salted.response.clone().unwrap().as_str()
        );
        // The row is still cited by its hash, unchanged.
        assert!(m.record.to_string().contains(&"c".repeat(64)));
        assert_eq!(verified(&m), "confirmed");
        // Recomputable from the value and the salt alone.
        let (v, _) = brutor_evidence::canon::body_value(body);
        assert_eq!(
            brutor_evidence::canon::salted_digest_hex(&v, &salted.salt).unwrap(),
            salted.request.unwrap()
        );
    }

    #[test]
    fn salting_required_without_captured_bodies_publishes_no_digest() {
        let opts = MintOptions {
            evidence: EvidenceOptions {
                salt_digests: true,
                ..Default::default()
            },
            ..Default::default()
        };
        let m = build_with(&mcp_row(200, true), None, &opts)
            .unwrap()
            .unwrap();
        let dumped = m.record.to_string();
        assert!(!dumped.contains(&"a".repeat(64)), "unsalted request digest");
        assert!(
            !dumped.contains(&"b".repeat(64)),
            "unsalted response digest"
        );
        assert_eq!(m.record["effect"]["status"], "dispatched");
        assert_eq!(verified(&m), "dispatched_unconfirmed");
    }

    // ---- two-phase actions ------------------------------------------------

    fn planned_action() -> PlannedAction {
        PlannedAction {
            tenant_id: "tenant-a".into(),
            surface: "mcp",
            proxy_log_id: "01J0000000000000000000000A".into(),
            effect_type: "write_order".into(),
            root_task_id: Some("run-1".into()),
            consumed_approval_request_id: None,
            timestamp: chrono::Utc::now(),
        }
    }

    #[test]
    fn planned_record_attests_the_decision_not_an_effect() {
        let p = build_planned(&planned_action(), Some(&attr())).unwrap();
        assert_eq!(p.verdict, verdict::PLANNED);
        assert_eq!(p.record["action_id"], "mcp/01J0000000000000000000000A");
        assert_eq!(p.record["effect"]["status"], "planned");
        assert!(p.record["effect"].get("request_digest").is_none());
        assert!(p.record["effect"].get("response_digest").is_none());
        assert_eq!(p.record["disposition"]["decision"], "accept");
        assert_eq!(p.record["disposition"]["approver"], "policy");
        assert!(p.proxy_log_id.is_none());
        assert_eq!(verified(&p), "not_applicable");

        let mut approved = planned_action();
        approved.consumed_approval_request_id = Some("apr-3".into());
        let p = build_planned(&approved, None).unwrap();
        assert_eq!(p.record["disposition"]["approver"], "human");
        assert_eq!(p.record["disposition"]["authority"], "apr-3");
        assert_eq!(p.record["system"], "ungoverned");
    }

    #[test]
    fn outcome_confirms_its_planned_record_under_one_action_id() {
        let pa = planned_action();
        let planned = build_planned(&pa, Some(&attr())).unwrap();
        let mut row = mcp_row(200, true);
        row.id = pa.proxy_log_id.clone();
        let opts = MintOptions {
            confirms: Some(planned.record_id.clone()),
            ..Default::default()
        };
        let out = build_with(&row, Some(&attr()), &opts).unwrap().unwrap();
        assert_eq!(out.record["action_id"], planned.record["action_id"]);
        assert_eq!(out.record["chain"]["relation"], "confirms");
        assert_eq!(
            out.record["chain"]["parent_record_id"],
            planned.record_id.as_str()
        );
        assert_eq!(verified(&out), "confirmed");
    }

    // ---- class: every refusal on every proxy type is sealed ---------------

    /// `proxy_type`s that are no action surface, so nothing on them is sealed.
    ///
    /// OAuth rows record the credential exchange (authorize, callback, token)
    /// that precedes any governed action; Portal rows record the end-user
    /// Portal API (login, listings) through a writer that takes no refusal
    /// facts. No governance gate writes a refusal on either — pinned by
    /// `the_non_action_surfaces_write_no_refusal`. Every other
    /// `constants::proxy` type must map to a surface —
    /// `the_proxy_type_vocabulary_is_closed` enforces it.
    const NON_ACTION_PROXY_TYPES: &[&str] = &[
        crate::constants::proxy::OAUTH,
        crate::constants::proxy::PORTAL,
    ];

    /// Every way a row says "the gateway refused": each subtype the writer
    /// reads as a refusal, the `rate_limit_<scope>` family (every scope the
    /// LLM refusal writer uses) and the three refusal flags.
    fn refusal_forms() -> Vec<(String, Box<dyn Fn(&mut ProxyLogInsert)>)> {
        let mut forms: Vec<(String, Box<dyn Fn(&mut ProxyLogInsert)>)> = Vec::new();
        let subtypes = DENY_SUBTYPES.iter().copied().chain([
            st::APPROVAL_REQUIRED,
            st::APPROVAL_APPROVED,
            st::APPROVAL_REJECTED,
            st::ENGINE_FAILURE,
        ]);
        for sub in subtypes {
            forms.push((
                sub.to_string(),
                Box::new(move |l: &mut ProxyLogInsert| l.proxy_subtype = Some(sub.to_string())),
            ));
        }
        use crate::services::llm_preflight as pf;
        for scope in [
            pf::QUOTA_SCOPE,
            pf::SESSION_SCOPE,
            pf::CONCURRENCY_SCOPE,
            pf::MEDIA_QUOTA_SCOPE,
            pf::ROUTING_SCOPE,
            pf::TOOL_CALLS_SCOPE,
        ] {
            let sub = format!("{}{scope}", crate::services::llm_refusal::RATE_LIMIT_PREFIX);
            forms.push((
                sub.clone(),
                Box::new(move |l: &mut ProxyLogInsert| l.proxy_subtype = Some(sub.clone())),
            ));
        }
        forms.push((
            "guardrail_blocked".into(),
            Box::new(|l: &mut ProxyLogInsert| l.guardrail_blocked = true),
        ));
        forms.push((
            "argument_policy_denied".into(),
            Box::new(|l: &mut ProxyLogInsert| l.argument_policy_denied = true),
        ));
        forms.push((
            "agent_authz deny".into(),
            Box::new(|l: &mut ProxyLogInsert| {
                l.agent_authz_decision = Some(crate::constants::agent_decision::DENY.into())
            }),
        ));
        forms
    }

    /// A refused (never-dispatched) row of `proxy_type`, carrying no action
    /// facts: it must seal on the refusal alone.
    fn refused_row(proxy_type: &str) -> ProxyLogInsert {
        let mut log = build_proxy_log(
            proxy_type,
            &auth(),
            "POST",
            "/refused",
            None,
            None,
            403,
            1.0,
            false,
            Some("refused"),
            "corr-r",
        );
        log.root_task_id = Some("run-1".into());
        log
    }

    #[test]
    fn every_refusal_on_every_proxy_type_is_sealed() {
        // The batch residency refusal was written with `proxy_type = batch`,
        // which mapped to no evidence surface, so it was never sealed — a
        // refusal nobody could prove. This enumerates the proxy types from
        // `constants::proxy::ALL`: a new type that is neither an action
        // surface nor a declared non-action type fails here.
        use crate::constants::proxy;
        for &pt in proxy::ALL {
            if NON_ACTION_PROXY_TYPES.contains(&pt) {
                continue;
            }
            for (form, apply) in refusal_forms() {
                let mut log = refused_row(pt);
                apply(&mut log);
                assert!(
                    should_record(&log),
                    "{pt} × {form}: a refusal must be sealed"
                );
                let m = build(&log, None)
                    .unwrap_or_else(|e| panic!("{pt} × {form}: {e:?}"))
                    .unwrap_or_else(|| panic!("{pt} × {form}: no evidence surface"));
                let expected = match form.as_str() {
                    st::APPROVAL_REQUIRED => verdict::AWAITING_HUMAN,
                    st::APPROVAL_APPROVED | st::APPROVAL_REJECTED => verdict::RESOLVED,
                    st::ENGINE_FAILURE => verdict::ENGINE_FAILURE,
                    "guardrail_blocked" => verdict::BLOCKED,
                    _ => verdict::DENIED,
                };
                assert_eq!(m.verdict, expected, "{pt} × {form}");
                assert_eq!(m.root_task_id.as_deref(), Some("run-1"), "{pt} × {form}");
                let v = brutor_evidence::verify(&m.record, None);
                assert!(v.ok, "{pt} × {form}: {:?}", v.findings);
            }
        }
    }

    #[test]
    fn a_batch_row_that_is_not_a_refusal_is_job_management_not_inference() {
        let mut submit = refused_row(crate::constants::proxy::BATCH);
        submit.http_status_code = Some(200);
        submit.success = true;
        let opts = MintOptions {
            evidence: EvidenceOptions {
                inference_records: true,
                ..Default::default()
            },
            ..Default::default()
        };
        assert!(
            !should_record_with(&submit, &opts),
            "a batch submit / read is not an inference record"
        );
        let mut llm = submit.clone();
        llm.proxy_type = crate::constants::proxy::LLM.into();
        assert!(
            should_record_with(&llm, &opts),
            "an LLM completion still is"
        );
    }

    #[test]
    fn the_proxy_type_vocabulary_is_closed() {
        use crate::constants::proxy;
        // Every declared type is an action surface or a declared non-action.
        for &pt in proxy::ALL {
            let row = refused_row(pt);
            assert!(
                surface_of(&row).is_some() != NON_ACTION_PROXY_TYPES.contains(&pt),
                "{pt}: must be exactly one of an evidence surface or NON_ACTION_PROXY_TYPES"
            );
        }
        // Every proxy_type the source writes is declared.
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
        let mut stack = vec![root];
        let mut written = std::collections::BTreeSet::new();
        while let Some(dir) = stack.pop() {
            for e in std::fs::read_dir(&dir).expect("read src").flatten() {
                let p = e.path();
                if p.is_dir() {
                    stack.push(p);
                    continue;
                }
                if p.extension().is_none_or(|x| x != "rs") {
                    continue;
                }
                let src = std::fs::read_to_string(&p).expect("read source");
                let src = src.split("#[cfg(test)]").next().unwrap_or_default();
                for needle in ["build_proxy_log(", "proxy_type = "] {
                    for (i, _) in src.match_indices(needle) {
                        let tail = src[i + needle.len()..].trim_start();
                        if let Some(rest) = tail.strip_prefix('"') {
                            let lit: String = rest.chars().take_while(|c| *c != '"').collect();
                            written.insert((lit, p.display().to_string()));
                        }
                    }
                }
            }
        }
        assert!(!written.is_empty(), "the scan found no proxy_type literals");
        let unknown: Vec<_> = written
            .iter()
            .filter(|(lit, _)| !proxy::ALL.contains(&lit.as_str()))
            .collect();
        assert!(
            unknown.is_empty(),
            "rows written with a proxy_type missing from constants::proxy::ALL — \
             the evidence writer cannot know whether their refusals seal: {unknown:?}"
        );
    }

    #[test]
    fn the_non_action_surfaces_write_no_refusal() {
        // OAuth and Portal are exempt from sealing only because nothing on
        // them refuses: their row writers stamp no refusal marker. A gate
        // added there must move the type out of NON_ACTION_PROXY_TYPES (and
        // give it a surface).
        // The Portal's sign-in routes are the Enterprise workspace's: read
        // where this run has it (`tests/support/roots.rs`).
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        let mut files = vec![("src/routes/oauth.rs", root.join("src/routes/oauth.rs"))];
        if let Some(enterprise) = roots::tree("enterprise") {
            files.push((
                "enterprise/src/routes/portal_auth.rs",
                root.join(enterprise).join("src/routes/portal_auth.rs"),
            ));
        }
        for (file, path) in files {
            let src = std::fs::read_to_string(path).expect("read source");
            let src = src.split("#[cfg(test)]").next().unwrap_or_default();
            for marker in [
                "proxy_subtype",
                "guardrail_blocked",
                "argument_policy_denied",
                "agent_authz_decision",
                "apply_guardrail",
                "apply_policy_deny",
                "apply_agent_authz_decision",
            ] {
                assert!(
                    !src.contains(marker),
                    "{file} writes `{marker}`: a refusal on a surface the \
                     evidence writer does not seal"
                );
            }
        }
    }
}
