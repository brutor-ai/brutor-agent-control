// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The vector corpus generator.
//!
//! Shared by `examples/gen-vectors.rs` (writes the files) and
//! `tests/vectors.rs` (regenerates in memory and fails on any byte of
//! drift). Everything is deterministic: fixed key seeds, fixed timestamps,
//! Ed25519 (RFC 8032, deterministic signatures), RFC 6962 trees, JSON
//! written with sorted keys.
//!
//! Every expectation written into a file is asserted here first against
//! `brutor_verify` — generation panics if the verifier disagrees with the
//! expectation the corpus declares, so the corpus can never silently pin a
//! regression.

#![allow(dead_code)]

use base64::Engine as _;
use brutor_cose::merkle::{self, Hash, leaf_hash};
use brutor_cose::receipt::{InclusionProof, build_receipt};
use brutor_cose::sign1::CoseSign1;
use brutor_ctlog::{Log, MemoryStore, TreeHead};
use brutor_evidence::bundle::{
    self as bdl, BundleHead, BundleInput, BundleReceipt, BundleRecord, ConsistencyProof,
    RecordProof, selection,
};
use brutor_evidence::canon::{
    self, body_value, canonical_bytes, json_digest_hex, salted_digest_hex,
};
use brutor_evidence::record::{
    ATTESTATION_GATE_OBSERVED, Attestation, Chain, Control, Disposition, Effect, Model,
    OBSERVATION_IN_PATH, RUNTIME, RecordBuilder, Reference, System, compute_record_id, relation,
    verdict,
};
use brutor_evidence::statement::seal_record;
use brutor_verify::{BundleOptions, TrustedKeys};
use ed25519_dalek::SigningKey;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::path::PathBuf;

pub const ISS: &str = "urn:brutor:deployment:vectors";
pub const TENANT: &str = "tenant-vectors";
pub const SYSTEM_ID: &str = "rg-vectors-claims";
pub const RUN_ID: &str = "run-vectors-1";
pub const GENERATOR: &str = "cargo run -p brutor-verify --example gen-vectors";

/// Every generated file: path relative to the core repo root → content.
pub struct Corpus {
    pub files: BTreeMap<String, String>,
    /// `brutor-gateway-control/tests/fixtures/evidence_interop_v1.json`.
    pub cp_fixture: String,
}

pub fn core_root() -> PathBuf {
    let p = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    p.canonicalize().unwrap_or(p)
}

/// The control plane's parity fixture, relative to the control plane's tree.
pub const CP_FIXTURE: &str = "tests/fixtures/evidence_interop_v1.json";

/// Where the generator writes the control plane's fixture:
/// `BRUTOR_CONTROL_REPO` when set (CI checks the control plane out into the
/// workspace), else the control plane beside this repository under its own
/// name (`../brutor-gateway-control`) or, in the public monorepo, under its
/// directory there (`../control-plane`), the first that is there. Neither:
/// the first, which the generator reports as not found. The test does not
/// read this: it takes the control plane from `tests/support/roots.rs`.
pub fn cp_fixture_path() -> PathBuf {
    let base = std::env::var("BRUTOR_CONTROL_REPO")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            let beside =
                ["../brutor-gateway-control", "../control-plane"].map(|d| core_root().join(d));
            beside
                .iter()
                .find(|d| d.is_dir())
                .unwrap_or(&beside[0])
                .clone()
        });
    base.join(CP_FIXTURE)
}

// ---------------------------------------------------------------------------
// Deterministic JSON
// ---------------------------------------------------------------------------

/// Pretty JSON with object keys sorted (byte order), two-space indent and a
/// trailing newline — independent of serde_json's map implementation.
pub fn pretty(v: &Value) -> String {
    fn w(v: &Value, ind: usize, out: &mut String) {
        let pad = |n: usize| " ".repeat(n);
        match v {
            Value::Object(m) if !m.is_empty() => {
                let mut keys: Vec<&String> = m.keys().collect();
                keys.sort();
                out.push_str("{\n");
                for (i, k) in keys.iter().enumerate() {
                    out.push_str(&pad(ind + 2));
                    out.push_str(&serde_json::to_string(k).unwrap());
                    out.push_str(": ");
                    w(&m[k.as_str()], ind + 2, out);
                    if i + 1 < keys.len() {
                        out.push(',');
                    }
                    out.push('\n');
                }
                out.push_str(&pad(ind));
                out.push('}');
            }
            Value::Array(a) if !a.is_empty() => {
                out.push_str("[\n");
                for (i, x) in a.iter().enumerate() {
                    out.push_str(&pad(ind + 2));
                    w(x, ind + 2, out);
                    if i + 1 < a.len() {
                        out.push(',');
                    }
                    out.push('\n');
                }
                out.push_str(&pad(ind));
                out.push(']');
            }
            other => out.push_str(&serde_json::to_string(other).unwrap()),
        }
    }
    let mut s = String::new();
    w(v, 0, &mut s);
    s.push('\n');
    s
}

// ---------------------------------------------------------------------------
// Keys
// ---------------------------------------------------------------------------

/// Test key seeds: `SHA-256("brutor-verify/vectors/v1/<label>")`. Published
/// on purpose — these keys sign nothing but the corpus.
pub fn key(label: &str) -> SigningKey {
    let seed: [u8; 32] = Sha256::digest(format!("brutor-verify/vectors/v1/{label}")).into();
    SigningKey::from_bytes(&seed)
}

pub fn kid(k: &SigningKey) -> String {
    hex::encode(k.verifying_key().to_bytes())
}

fn jwk(k: &SigningKey, status: &str) -> Value {
    json!({
        "kid": kid(k),
        "kty": "OKP",
        "crv": "Ed25519",
        "alg": "EdDSA",
        "x": base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(k.verifying_key().to_bytes()),
        "use": "sig",
        "issuer": ISS,
        "status": status,
        "scope": "deployment",
    })
}

pub struct Keys {
    pub deployment: SigningKey,
    pub retired: SigningKey,
    pub stranger: SigningKey,
    pub witness: SigningKey,
}

impl Keys {
    fn new() -> Self {
        Self {
            deployment: key("deployment-key"),
            retired: key("retired-key"),
            stranger: key("stranger-key"),
            witness: key("witness-key"),
        }
    }
    fn well_known(&self) -> Value {
        json!({
            "issuer": ISS,
            "keys": [jwk(&self.deployment, "active"), jwk(&self.retired, "retired")],
            "statement_content_type": brutor_evidence::statement::RECORD_CONTENT_TYPE,
            "tree_head_content_type": brutor_ctlog::TREE_HEAD_CONTENT_TYPE,
            "record_schema": brutor_evidence::SCHEMA,
        })
    }
    fn trusted(&self) -> TrustedKeys {
        let mut t = TrustedKeys::unpinned();
        t.add_well_known(&self.well_known()).unwrap();
        t
    }
    fn stranger_doc(&self) -> Value {
        json!({"issuer": "urn:brutor:deployment:someone-else", "keys": [jwk(&self.stranger, "active")]})
    }
}

fn b64(b: &[u8]) -> String {
    base64::engine::general_purpose::STANDARD.encode(b)
}

fn unb64(s: &str) -> Vec<u8> {
    base64::engine::general_purpose::STANDARD.decode(s).unwrap()
}

fn sha_hex(s: &str) -> String {
    hex::encode(Sha256::digest(s.as_bytes()))
}

pub fn log_id() -> String {
    // Same shape as the gateway's `log_id_for` (SHA-256 of deployment id,
    // a zero byte and the tenant id) — opaque, never the tenant id.
    let mut h = Sha256::new();
    h.update(b"vectors");
    h.update([0u8]);
    h.update(TENANT.as_bytes());
    hex::encode(h.finalize())
}

// ---------------------------------------------------------------------------
// Records
// ---------------------------------------------------------------------------

fn contract_hash() -> String {
    sha_hex("ai-system-contract rg-vectors-claims v3")
}

fn governed(b: &mut RecordBuilder) {
    b.system = Some(System {
        ai_system_id: SYSTEM_ID.into(),
        contract_version: Some(3),
        contract_hash: Some(contract_hash()),
    });
    b.epoch = Some(format!("contract:{}", contract_hash()));
}

fn builder(action: &str, second: u32, verdict: &str) -> RecordBuilder {
    let mut b = RecordBuilder::new(action, TENANT, format!("2026-09-19T10:00:{second:02}.000Z"));
    b.verdict = verdict.into();
    governed(&mut b);
    b
}

fn disp(
    decision: &str,
    approver: &str,
    human: bool,
    authority: Option<&str>,
) -> Option<Disposition> {
    Some(Disposition {
        decision: decision.into(),
        approver: approver.into(),
        human_disposed: human,
        authority: authority.map(str::to_owned),
        reason_digest: None,
    })
}

fn effect(status: &str, req: Option<String>, resp: Option<String>) -> Option<Effect> {
    Some(Effect {
        kind: "mcp_tool_call".into(),
        status: status.into(),
        request_digest: req,
        response_digest: resp,
        attestation: ATTESTATION_GATE_OBSERVED.into(),
        irreversibility: None,
    })
}

fn ctx(pairs: &[(&str, Value)]) -> Map<String, Value> {
    pairs
        .iter()
        .map(|(k, v)| ((*k).to_owned(), v.clone()))
        .collect()
}

fn mcp_context(turn: &str) -> Map<String, Value> {
    ctx(&[
        ("root_task_id", json!(RUN_ID)),
        ("turn_id", json!(turn)),
        ("surface", json!("mcp")),
        ("server_version", json!("orders-mcp/2.4.1")),
        (
            "tool_definition_hash",
            json!(sha_hex("tool orders.refund v7")),
        ),
        ("autonomy_level", json!("supervised")),
        ("autonomy_provenance", json!("contract")),
        ("tags", json!(["eu_ai_act.art12", "soc2.cc7.2"])),
    ])
}

fn row_ref(n: u32) -> Reference {
    Reference {
        kind: "ai.brutor.proxy-log-row".into(),
        digest_alg: "SHA-256".into(),
        digest: sha_hex(&format!("proxy-log-row {n}")),
        purpose: None,
    }
}

fn contract_ref() -> Reference {
    Reference {
        kind: "ai.brutor.ai-system-contract".into(),
        digest_alg: "SHA-256".into(),
        digest: contract_hash(),
        purpose: Some("governing_contract".into()),
    }
}

fn gate_control(id: &str, result: &str, blocking: bool) -> Control {
    Control {
        id: id.into(),
        result: result.into(),
        blocking,
        severity: Some(if result == "fail" { "high" } else { "info" }.into()),
        kind: Some("gate".into()),
        evidence_digest: None,
    }
}

fn seal(b: &RecordBuilder) -> Value {
    b.seal().expect("builder rejected a corpus record").0
}

/// Recompute `record_id` after a deliberate edit (vectors of records a
/// builder would refuse to produce).
fn reseal(mut v: Value) -> Value {
    v.as_object_mut().unwrap().remove("record_id");
    let id = compute_record_id(&v).unwrap();
    v["record_id"] = json!(id);
    v
}

fn rid(v: &Value) -> String {
    v["record_id"].as_str().unwrap().to_owned()
}

fn statement(v: &Value, k: &SigningKey) -> Vec<u8> {
    seal_record(v, ISS, &format!("ai-system:{SYSTEM_ID}"), k).unwrap()
}

/// Literal preimages of the digests.
fn tool_args() -> Value {
    json!({"tool": "orders.refund", "arguments": {"order_id": "PO-88213", "amount": "1240.19", "currency": "EUR"}})
}
fn tool_result() -> Value {
    json!({"content": [{"type": "text", "text": "Refund R-5521 issued"}], "isError": false})
}
fn request_body() -> Value {
    json!({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": tool_args()})
}
fn response_body() -> Value {
    json!({"jsonrpc": "2.0", "id": 7, "result": tool_result()})
}

fn jcs_pre(v: &Value) -> Value {
    json!({"scheme": "jcs", "value": v})
}

/// Records of one realistic run, reused by the record vectors and the
/// golden bundles: approval chain, two-phase action, a block, a denial.
pub struct Run {
    pub awaiting: Value,
    pub approved: Value,
    pub planned: Value,
    pub confirmed: Value,
    pub blocked: Value,
    pub denied: Value,
}

fn executed_attestation() -> Option<Attestation> {
    Some(Attestation {
        runtime: RUNTIME.into(),
        observation: OBSERVATION_IN_PATH.into(),
        input_digest: Some(json_digest_hex(&request_body()).unwrap()),
        output_digest: Some(json_digest_hex(&response_body()).unwrap()),
        digest_salt: None,
        numbers_canonicalized: false,
    })
}

fn run() -> Run {
    // Approval requested (may) …
    let mut b = builder("mcp/01K5VEC0000000000000000001", 1, verdict::AWAITING_HUMAN);
    b.disposition = disp("needs_input", "policy", false, None);
    b.effect = effect("planned", None, None);
    b.controls = vec![gate_control("ai.brutor.approval.required", "fail", true)];
    b.context = mcp_context("turn-1");
    b.context
        .insert("approval_request_id".into(), json!("apr-01K5VEC0001"));
    b.references = Some(vec![row_ref(1), contract_ref()]);
    let awaiting = seal(&b);
    // … approved by a human and executed (did), superseding the request.
    let mut b = builder("mcp/01K5VEC0000000000000000002", 2, verdict::EXECUTED);
    b.disposition = disp("accept", "human", true, Some("apr-01K5VEC0001"));
    b.effect = effect(
        "confirmed",
        Some(json_digest_hex(&tool_args()).unwrap()),
        Some(json_digest_hex(&tool_result()).unwrap()),
    );
    b.attestation = executed_attestation();
    b.controls = vec![gate_control("ai.brutor.agent_grant.scope", "pass", false)];
    b.context = mcp_context("turn-1");
    b.chain = Some(Chain {
        parent_record_id: rid(&awaiting),
        relation: relation::SUPERSEDES.into(),
    });
    b.references = Some(vec![row_ref(2), contract_ref()]);
    let approved = seal(&b);
    // Two-phase: planned …
    let mut b = builder("mcp/01K5VEC0000000000000000003", 3, verdict::PLANNED);
    b.disposition = disp("accept", "policy", false, None);
    b.effect = effect("planned", None, None);
    b.context = mcp_context("turn-2");
    b.references = Some(vec![contract_ref()]);
    let planned = seal(&b);
    // … confirmed by the outcome.
    let mut b = builder("mcp/01K5VEC0000000000000000003", 4, verdict::EXECUTED);
    b.disposition = disp("accept", "policy", false, None);
    b.effect = effect(
        "confirmed",
        Some(json_digest_hex(&tool_args()).unwrap()),
        Some(json_digest_hex(&tool_result()).unwrap()),
    );
    b.attestation = executed_attestation();
    b.context = mcp_context("turn-2");
    b.chain = Some(Chain {
        parent_record_id: rid(&planned),
        relation: relation::CONFIRMS.into(),
    });
    b.references = Some(vec![row_ref(3), contract_ref()]);
    let confirmed = seal(&b);
    let mut b = builder("mcp/01K5VEC0000000000000000005", 5, verdict::BLOCKED);
    b.disposition = disp("reject", "policy", false, None);
    b.controls = vec![gate_control("ai.brutor.guardrail.pii", "fail", true)];
    b.context = mcp_context("turn-3");
    b.references = Some(vec![row_ref(5), contract_ref()]);
    let blocked = seal(&b);
    let mut b = builder("mcp/01K5VEC0000000000000000006", 6, verdict::DENIED);
    b.disposition = disp("reject", "policy", false, None);
    b.controls = vec![gate_control("ai.brutor.agent_grant.scope", "fail", true)];
    b.context = mcp_context("turn-4");
    b.references = Some(vec![row_ref(6), contract_ref()]);
    let denied = seal(&b);
    Run {
        awaiting,
        approved,
        planned,
        confirmed,
        blocked,
        denied,
    }
}

struct RecVector {
    name: &'static str,
    description: &'static str,
    record: Value,
    statement: Option<Vec<u8>>,
    store: Option<Vec<Value>>,
    preimages: Value,
    ok: bool,
    codes: Vec<&'static str>,
}

fn rv(
    name: &'static str,
    description: &'static str,
    record: Value,
    statement: Option<Vec<u8>>,
    ok: bool,
    codes: &[&'static str],
) -> RecVector {
    RecVector {
        name,
        description,
        record,
        statement,
        store: None,
        preimages: json!({}),
        ok,
        codes: codes.to_vec(),
    }
}

/// Resolve `a.b[0].c` in a record.
pub fn at<'a>(v: &'a Value, path: &str) -> Option<&'a Value> {
    let mut cur = v;
    for part in path.split('.') {
        let (name, idx) = match part.find('[') {
            Some(i) => (
                &part[..i],
                Some(part[i + 1..part.len() - 1].parse::<usize>().ok()?),
            ),
            None => (part, None),
        };
        cur = cur.get(name)?;
        if let Some(i) = idx {
            cur = cur.get(i)?;
        }
    }
    Some(cur)
}

/// Recompute a preimage digest the way the corpus README specifies.
pub fn preimage_digest(record: &Value, spec: &Value) -> String {
    let value = &spec["value"];
    match spec["scheme"].as_str().unwrap() {
        "jcs" => json_digest_hex(value).unwrap(),
        "jcs_salted" => salted_digest_hex(
            value,
            record["attestation"]["digest_salt"].as_str().unwrap(),
        )
        .unwrap(),
        "utf8" => sha_hex(value.as_str().unwrap()),
        other => panic!("unknown preimage scheme {other}"),
    }
}

fn record_vectors(k: &Keys, run: &Run) -> Vec<RecVector> {
    let d = &k.deployment;
    let mut out = Vec::new();
    let with = |v: Value| {
        let s = statement(&v, d);
        (v, Some(s))
    };

    // ---- the 14 verdicts ----------------------------------------------------
    let (r, s) = with(run.confirmed.clone());
    let mut x = rv(
        "verdict-executed-confirmed",
        "executed: allowed and upstream ok; effect confirmed with request/response digests over literal preimages; chain confirms a planned record (two-phase)",
        r,
        s,
        true,
        &[],
    );
    x.store = Some(vec![run.planned.clone()]);
    x.preimages = json!({
        "effect.request_digest": jcs_pre(&tool_args()),
        "effect.response_digest": jcs_pre(&tool_result()),
        "attestation.input_digest": jcs_pre(&request_body()),
        "attestation.output_digest": jcs_pre(&response_body()),
    });
    out.push(x);

    let mut b = builder("mcp/01K5VEC0000000000000000010", 10, verdict::ERRORED);
    b.disposition = disp("accept", "policy", false, None);
    b.effect = effect("failed", Some(json_digest_hex(&tool_args()).unwrap()), None);
    b.context = mcp_context("turn-10");
    let (r, s) = with(seal(&b));
    let mut x = rv(
        "verdict-errored-failed",
        "errored: tool returned isError / upstream 4xx-5xx; effect failed; derived mode dispatched_unconfirmed",
        r,
        s,
        true,
        &[],
    );
    x.preimages = json!({"effect.request_digest": jcs_pre(&tool_args())});
    out.push(x);

    let mut b = builder("mcp/01K5VEC0000000000000000011", 11, verdict::TIMEOUT);
    b.disposition = disp("accept", "policy", false, None);
    b.effect = effect(
        "dispatched",
        Some(json_digest_hex(&tool_args()).unwrap()),
        None,
    );
    b.context = mcp_context("turn-11");
    let (r, s) = with(seal(&b));
    out.push(rv(
        "verdict-timeout-dispatched",
        "timeout: dispatched, no observed result; effect dispatched",
        r,
        s,
        true,
        &[],
    ));

    let (r, s) = with(run.blocked.clone());
    out.push(rv(
        "verdict-blocked",
        "blocked: guardrail block; reject/policy/false; no effect (never effect.status blocked)",
        r,
        s,
        true,
        &[],
    ));
    let (r, s) = with(run.denied.clone());
    out.push(rv(
        "verdict-denied",
        "denied: grant/policy deny; reject/policy/false; no effect",
        r,
        s,
        true,
        &[],
    ));

    let mut b = builder(
        "mcp/01K5VEC0000000000000000012",
        12,
        verdict::ENGINE_FAILURE,
    );
    b.disposition = disp("reject", "policy", false, None);
    b.controls = vec![gate_control("ai.brutor.engine.guardrail", "fail", true)];
    b.context = mcp_context("turn-12");
    let (r, s) = with(seal(&b));
    out.push(rv(
        "verdict-engine-failure",
        "engine_failure: a gate failed closed because its engine could not decide",
        r,
        s,
        true,
        &[],
    ));

    let (r, s) = with(run.awaiting.clone());
    out.push(rv(
        "verdict-awaiting-human-planned",
        "awaiting_human: approval enqueued; needs_input/policy/false; effect planned without digests (the attestation on a planned effect is informational)",
        r,
        s,
        true,
        &["attestation_on_planned"],
    ));

    let mut b = builder("approval/apr-01K5VEC0001/resolved", 13, verdict::RESOLVED);
    b.disposition = disp("reject", "human", true, Some("apr-01K5VEC0001"));
    b.context = ctx(&[("root_task_id", json!(RUN_ID)), ("surface", json!("mcp"))]);
    b.chain = Some(Chain {
        parent_record_id: rid(&run.awaiting),
        relation: relation::SUPERSEDES.into(),
    });
    let (r, s) = with(seal(&b));
    let mut x = rv(
        "verdict-resolved-supersedes",
        "resolved: approval rejected by a human; reject/human/true with authority = request id; chain supersedes the awaiting_human record",
        r,
        s,
        true,
        &[],
    );
    x.store = Some(vec![run.awaiting.clone()]);
    out.push(x);

    let mut b = builder("approval/apr-01K5VEC0001/expired", 14, verdict::EXPIRED);
    b.disposition = disp("deferred", "policy", false, Some("apr-01K5VEC0001"));
    b.context = ctx(&[
        ("record_type", json!("ai.brutor.approval-expired")),
        ("surface", json!("mcp")),
        ("approval_request_id", json!("apr-01K5VEC0001")),
    ]);
    b.kind = "fyi".into();
    b.chain = Some(Chain {
        parent_record_id: rid(&run.awaiting),
        relation: relation::SUPERSEDES.into(),
    });
    let (r, s) = with(seal(&b));
    let mut x = rv(
        "verdict-expired-supersedes",
        "expired: approval lapsed; deferred/policy/false; chain supersedes the awaiting_human record",
        r,
        s,
        true,
        &[],
    );
    x.store = Some(vec![run.awaiting.clone()]);
    out.push(x);

    let mut b = builder(
        "ai.brutor.epoch-boundary/01K5VEC0015",
        15,
        verdict::EPOCH_BOUNDARY,
    );
    b.kind = "fyi".into();
    b.disposition = disp("accept", "policy", false, None);
    b.context = ctx(&[
        ("record_type", json!("ai.brutor.epoch-boundary")),
        ("surface", json!("control_plane")),
        ("contract_version", json!("4")),
    ]);
    b.references = Some(vec![contract_ref()]);
    let epoch = seal(&b);
    let (r, s) = with(epoch.clone());
    out.push(rv(
        "verdict-epoch-boundary",
        "epoch_boundary (fyi): contract promoted; closes the epoch",
        r,
        s,
        true,
        &[],
    ));

    let rubric = json!({"check": "refund-policy", "criteria": ["amount within limit", "order exists"], "scale": ["met", "not_met", "n/a"]});
    let mut b = builder("ai.brutor.assessment/01K5VEC0016", 16, verdict::ASSESSMENT);
    b.kind = "fyi".into();
    b.disposition = disp("accept", "policy", false, None);
    b.model = Some(Model {
        model_id: None,
        provider: None,
        deciding_model: Some("judge-model-2026-08".into()),
    });
    b.controls = vec![Control {
        id: "ai.brutor.judge.refund_policy".into(),
        result: "pass".into(),
        blocking: false,
        severity: None,
        kind: Some("judged".into()),
        evidence_digest: None,
    }];
    b.context = ctx(&[
        ("record_type", json!("ai.brutor.assessment")),
        ("check_id", json!("chk-refund-policy")),
        (
            "evaluator_fingerprint",
            json!(sha_hex("judge-model-2026-08 rubric v1")),
        ),
    ]);
    b.chain = Some(Chain {
        parent_record_id: rid(&run.confirmed),
        relation: relation::ASSESSES.into(),
    });
    b.references = Some(vec![Reference {
        kind: "ai.brutor.rubric".into(),
        digest_alg: "SHA-256".into(),
        digest: json_digest_hex(&rubric).unwrap(),
        purpose: Some("rubric".into()),
    }]);
    let assessment = seal(&b);
    let (r, s) = with(assessment.clone());
    let mut x = rv(
        "verdict-assessment-assesses",
        "assessment (fyi): judged verdict over a turn record; chain assesses; rubric enters only as a digest",
        r,
        s,
        true,
        &[],
    );
    x.store = Some(vec![run.confirmed.clone()]);
    x.preimages = json!({"references[0].digest": jcs_pre(&rubric)});
    out.push(x);

    let mut b = builder(
        "ai.brutor.adjudication/01K5VEC0017",
        17,
        verdict::ADJUDICATION,
    );
    b.kind = "fyi".into();
    b.disposition = disp("accept", "human", true, None);
    b.controls = vec![Control {
        id: "ai.brutor.judge.refund_policy".into(),
        result: "pass".into(),
        blocking: false,
        severity: None,
        kind: Some("adjudicated".into()),
        evidence_digest: None,
    }];
    b.context = ctx(&[
        ("record_type", json!("ai.brutor.adjudication")),
        ("check_id", json!("chk-refund-policy")),
    ]);
    b.chain = Some(Chain {
        parent_record_id: rid(&assessment),
        relation: relation::ADJUDICATES.into(),
    });
    let (r, s) = with(seal(&b));
    let mut x = rv(
        "verdict-adjudication-adjudicates",
        "adjudication (fyi): human blind grade; human/true; chain adjudicates the assessment",
        r,
        s,
        true,
        &[],
    );
    x.store = Some(vec![assessment.clone()]);
    out.push(x);

    let declaration = json!({"framework_id": "eu-ai-act", "field": "intended_purpose", "value": "Refund triage for consumer orders"});
    let mut b = builder("ai.brutor.declaration/01K5VEC0018", 18, verdict::RECORDED);
    b.kind = "fyi".into();
    b.context = ctx(&[
        ("record_type", json!("ai.brutor.declaration")),
        ("surface", json!("control_plane")),
        ("framework_id", json!("eu-ai-act")),
    ]);
    b.references = Some(vec![Reference {
        kind: "ai.brutor.payload".into(),
        digest_alg: "SHA-256".into(),
        digest: json_digest_hex(&declaration).unwrap(),
        purpose: Some("ai.brutor.declaration".into()),
    }]);
    let (r, s) = with(seal(&b));
    let mut x = rv(
        "verdict-recorded-declaration",
        "recorded (fyi): a profile declaration sealed through the seal API; the payload enters only as a digest",
        r,
        s,
        true,
        &[],
    );
    x.preimages = json!({"references[0].digest": jcs_pre(&declaration)});
    out.push(x);

    let (r, s) = with(run.planned.clone());
    out.push(rv(
        "verdict-planned",
        "planned: first half of a two-phase action; effect planned, no digests",
        r,
        s,
        true,
        &["attestation_on_planned"],
    ));

    // ---- chains and effect statuses ----------------------------------------------
    let (r, s) = with(run.approved.clone());
    let mut x = rv(
        "chain-supersedes-approval-granted",
        "approval granted then executed: accept/human/true, authority = request id; chain supersedes the awaiting_human record",
        r,
        s,
        true,
        &[],
    );
    x.store = Some(vec![run.awaiting.clone()]);
    out.push(x);

    let mut b = builder("mcp/01K5VEC0000000000000000019", 19, verdict::DENIED);
    b.disposition = disp("reject", "policy", false, None);
    b.context = ctx(&[("surface", json!("mcp"))]);
    b.chain = Some(Chain {
        parent_record_id: rid(&epoch),
        relation: relation::EPOCH_OPENS.into(),
    });
    let (r, s) = with(seal(&b));
    let mut x = rv(
        "chain-epoch-opens",
        "first record of a new epoch; chain epoch_opens the epoch_boundary record",
        r,
        s,
        true,
        &[],
    );
    x.store = Some(vec![epoch.clone()]);
    out.push(x);

    let mut b = builder("mcp/01K5VEC0000000000000000020", 20, verdict::ERRORED);
    b.disposition = disp("accept", "policy", false, None);
    b.effect = effect(
        "reverted",
        Some(json_digest_hex(&tool_args()).unwrap()),
        None,
    );
    b.effect.as_mut().unwrap().irreversibility = Some("reversible".into());
    let (r, s) = with(seal(&b));
    out.push(rv(
        "effect-reverted",
        "effect status reverted (a dispatched effect that was rolled back); derived mode dispatched_unconfirmed",
        r,
        s,
        true,
        &[],
    ));

    // ---- honesty, vocabulary, digests, canonicalisation ---------------------------
    let mut h = run.denied.clone();
    h["disposition"]["human_disposed"] = json!(true);
    let (r, s) = with(reseal(h));
    out.push(rv(
        "honesty-human-disposed-by-policy",
        "honesty check: human_disposed=true with approver=policy (a builder refuses this); the verifier reports a warning, never an error",
        r,
        s,
        true,
        &["dishonest_human_disposed"],
    ));

    let mut u = run.denied.clone();
    u["verdict"] = json!("escalated");
    u["disposition"]["decision"] = json!("abstain");
    let (r, s) = with(reseal(u));
    out.push(rv(
        "unknown-vocabulary",
        "a verdict and a decision from a later vocabulary: informational, never an error, never graded up",
        r,
        s,
        true,
        &["unknown_vocabulary_value", "unknown_vocabulary_value"],
    ));

    let salt = "5a17c0ffee5a17c0ffee5a17c0ffee00".to_string();
    let req_body = r#"{"order_id":"PO-88213","amount":1240.19,"lines":[{"sku":"A-1","qty":2}]}"#;
    let resp_body = "Refund R-5521 issued for PO-88213";
    let (req_v, changed) = body_value(req_body);
    let (resp_v, _) = body_value(resp_body);
    let mut b = builder("mcp/01K5VEC0000000000000000021", 21, verdict::EXECUTED);
    b.disposition = disp("accept", "policy", false, None);
    b.effect = effect(
        "confirmed",
        Some(salted_digest_hex(&req_v, &salt).unwrap()),
        Some(salted_digest_hex(&resp_v, &salt).unwrap()),
    );
    b.attestation = Some(Attestation {
        runtime: RUNTIME.into(),
        observation: OBSERVATION_IN_PATH.into(),
        input_digest: Some(salted_digest_hex(&req_v, &salt).unwrap()),
        output_digest: Some(salted_digest_hex(&resp_v, &salt).unwrap()),
        digest_salt: Some(salt.clone()),
        numbers_canonicalized: changed,
    });
    let (r, s) = with(seal(&b));
    let mut x = rv(
        "salted-digests",
        "salt_digests: digest = SHA-256(JCS(value) || \"|\" || digest_salt); the JSON body's float became a decimal string (numbers_canonicalized: true); the text body commits as a JSON string",
        r,
        s,
        true,
        &[],
    );
    x.preimages = json!({
        "effect.request_digest": {"scheme": "jcs_salted", "body": req_body, "value": req_v},
        "effect.response_digest": {"scheme": "jcs_salted", "body": resp_body, "value": resp_v},
        "attestation.input_digest": {"scheme": "jcs_salted", "body": req_body, "value": req_v},
        "attestation.output_digest": {"scheme": "jcs_salted", "body": resp_body, "value": resp_v},
    });
    out.push(x);

    // RFC 8785 §3.2.3: U+10000 (D800 DC00) sorts before U+FFFD and U+E000
    // by UTF-16 code units, after them by code point.
    let utf16_args = json!({"\u{FFFD}": "replacement", "\u{10000}": "linear-b", "\u{E000}": "private-use", "€": "euro", "a": "ascii"});
    let mut b = builder("mcp/01K5VEC0000000000000000022", 22, verdict::EXECUTED);
    b.disposition = disp("accept", "policy", false, None);
    b.effect = effect(
        "confirmed",
        Some(json_digest_hex(&utf16_args).unwrap()),
        Some(json_digest_hex(&tool_result()).unwrap()),
    );
    b.context = ctx(&[
        ("surface", json!("mcp")),
        ("tags", json!(["\u{10000}", "\u{FFFD}"])),
    ]);
    let (r, s) = with(seal(&b));
    let mut x = rv(
        "utf16-key-order",
        "RFC 8785 member order is by UTF-16 code units: U+10000 sorts before U+E000 and U+FFFD (a code-point sort gets this wrong); see preimages.canonical",
        r,
        s,
        true,
        &[],
    );
    x.preimages = json!({
        "effect.request_digest": {"scheme": "jcs", "value": utf16_args,
            "canonical": String::from_utf8(canonical_bytes(&utf16_args).unwrap()).unwrap()},
        "effect.response_digest": jcs_pre(&tool_result()),
    });
    out.push(x);

    let notice = "You are chatting with an AI system. Responses are generated automatically.";
    let mut b = builder(
        "ai.brutor.notice/01K5VEC0000000000000000023",
        23,
        verdict::RECORDED,
    );
    b.kind = "fyi".into();
    b.controls = vec![Control {
        id: "ai.brutor.notice.rendered".into(),
        result: "pass".into(),
        blocking: false,
        severity: None,
        kind: Some("transparency".into()),
        evidence_digest: None,
    }];
    b.context = ctx(&[
        ("record_type", json!("ai.brutor.notice")),
        ("surface", json!("portal")),
        ("conversation_handle", json!("01K5VEC0NOT1CE0000000000C0")),
        ("locale", json!("en")),
        ("method", json!("banner")),
    ]);
    b.references = Some(vec![Reference {
        kind: "ai.brutor.notice-text".into(),
        digest_alg: "SHA-256".into(),
        digest: sha_hex(notice),
        purpose: Some("rendered_text".into()),
    }]);
    let (r, s) = with(seal(&b));
    let mut x = rv(
        "notice-text-reference",
        "AI-interaction notice: the rendered text enters as SHA-256 of its UTF-8 bytes (not JCS)",
        r,
        s,
        true,
        &[],
    );
    x.preimages = json!({"references[0].digest": {"scheme": "utf8", "value": notice}});
    out.push(x);

    let mut b = RecordBuilder::new(
        "mcp/01K5VEC0000000000000000024",
        TENANT,
        "2026-09-19T10:00:24.000Z",
    );
    b.verdict = verdict::DENIED.into();
    b.disposition = disp("reject", "policy", false, None);
    let min = seal(&b);
    let (r, s) = with(min);
    out.push(rv(
        "ungoverned-minimal",
        "the smallest record: an ungoverned row (system \"ungoverned\", no epoch), no optional blocks",
        r,
        s,
        true,
        &[],
    ));

    let r = run.blocked.clone();
    let s = statement(&r, &k.retired);
    out.push(rv(
        "retired-kid",
        "statement signed by a key the keys document lists as retired: still trusted for what it signed; reported as info",
        r,
        Some(s),
        true,
        &["kid_retired"],
    ));

    // ---- negative ------------------------------------------------------------------
    let mut c = run.confirmed.clone();
    c["effect"]
        .as_object_mut()
        .unwrap()
        .remove("response_digest");
    c.as_object_mut().unwrap().remove("chain");
    let (r, s) = with(reseal(c));
    out.push(rv(
        "neg/confirmed-without-response",
        "effect.status confirmed without response_digest: confirmed is an observed result, never a promise",
        r,
        s,
        false,
        &["confirmed_without_response"],
    ));

    let mut p = run.planned.clone();
    p["effect"]["request_digest"] = json!(json_digest_hex(&tool_args()).unwrap());
    let (r, s) = with(reseal(p));
    out.push(rv(
        "neg/planned-with-digests",
        "effect.status planned carrying a request_digest",
        r,
        s,
        false,
        &["planned_with_digests", "attestation_on_planned"],
    ));

    let mut n = run.blocked.clone();
    n["effect"] = json!({"type": "mcp_tool_call", "status": "dispatched", "request_digest": json_digest_hex(&tool_args()).unwrap(), "attestation": "gate_observed"});
    let (r, s) = with(reseal(n));
    out.push(rv(
        "neg/never-dispatch-with-effect",
        "a never-dispatching verdict (blocked) with a dispatched effect",
        r,
        s,
        false,
        &["verdict_effect_conflict"],
    ));

    let mut f = run.denied.clone();
    f["context"]["score"] = json!(0.5);
    out.push(rv(
        "neg/float-rejected",
        "a JSON float anywhere in a record: refused before hashing (no identity is computable, so no statement exists)",
        f,
        None,
        false,
        &["float_in_digest_field"],
    ));

    let mut i = run.denied.clone();
    i["context"]["count"] = json!(9_007_199_254_740_993u64);
    out.push(rv(
        "neg/unsafe-integer",
        "an integer outside ±(2^53−1): refused before hashing",
        i,
        None,
        false,
        &["unsafe_integer_in_digest_field"],
    ));

    let mut ne = run.denied.clone();
    ne["subject_id"] = json!("user-8812");
    let (r, s) = with(reseal(ne));
    out.push(rv(
        "neg/never-enters-top-level",
        "a never-enters member (subject_id) at the top level: not an admitted field and a never-enters name",
        r,
        s,
        false,
        &["unadmitted_field", "never_enters_member"],
    ));

    let mut ne = run.denied.clone();
    ne["context"]["email"] = json!("jane@example.com");
    let (r, s) = with(reseal(ne));
    out.push(rv(
        "neg/never-enters-nested",
        "a never-enters member (email) nested in context: rejected structurally at any depth",
        r,
        s,
        false,
        &["never_enters_member"],
    ));

    let mut t = run.denied.clone();
    let s = statement(&t, d);
    t["record_id"] = json!(sha_hex("some other record"));
    out.push(rv(
        "neg/tampered-record-id",
        "carried record_id replaced; the content (and so the statement binding) is intact",
        t,
        Some(s),
        false,
        &["record_id_mismatch"],
    ));

    let mut t = run.denied.clone();
    let s = statement(&t, d);
    t["action_id"] = json!("mcp/01K5VEC0000000000000000099");
    out.push(rv(
        "neg/tampered-content",
        "a member changed after sealing, record_id left as signed",
        t,
        Some(s),
        false,
        &["record_id_mismatch", "statement_payload_mismatch"],
    ));

    out.push(rv(
        "neg/wrong-statement-payload",
        "a valid statement for a different record",
        run.denied.clone(),
        Some(statement(&run.blocked, d)),
        false,
        &["statement_payload_mismatch"],
    ));

    out.push(rv(
        "neg/wrong-kid",
        "statement signed by a key outside the trusted set",
        run.denied.clone(),
        Some(statement(&run.denied, &k.stranger)),
        false,
        &["untrusted_kid"],
    ));

    let mut bad = statement(&run.denied, d);
    let n = bad.len();
    bad[n - 1] ^= 0x01;
    out.push(rv(
        "neg/bad-signature",
        "statement signature with one bit flipped",
        run.denied.clone(),
        Some(bad),
        false,
        &["signature_invalid"],
    ));

    let mut b = builder("approval/apr-01K5VEC0001/resolved", 13, verdict::RESOLVED);
    b.disposition = disp("reject", "human", true, Some("apr-01K5VEC0001"));
    b.chain = Some(Chain {
        parent_record_id: rid(&run.awaiting),
        relation: relation::SUPERSEDES.into(),
    });
    let (r, s) = with(seal(&b));
    let mut x = rv(
        "neg/chain-parent-missing",
        "a supersedes chain whose parent is not in the supplied store",
        r,
        s,
        false,
        &["chain_parent_missing"],
    );
    x.store = Some(vec![run.denied.clone()]);
    out.push(x);

    let mut m = run.denied.clone();
    m.as_object_mut().unwrap().remove("verdict");
    let (r, s) = with(reseal(m));
    out.push(rv(
        "neg/missing-required-field",
        "no verdict",
        r,
        s,
        false,
        &["missing_required_field"],
    ));

    let mut dup = run.approved.clone();
    dup["references"].as_array_mut().unwrap().push(json!({
        "type": "ai.brutor.approval-request", "digest_alg": "SHA-256", "digest": rid(&run.awaiting)
    }));
    let (r, s) = with(reseal(dup));
    let mut x = rv(
        "neg/reference-duplicates-chain-parent",
        "a reference repeating the chain parent (a chain edge is stated once, in chain)",
        r,
        s,
        false,
        &["reference_duplicates_chain_parent"],
    );
    x.store = Some(vec![run.awaiting.clone()]);
    out.push(x);

    let mut a = run.denied.clone();
    a["verdict"] = json!("timeout");
    a["disposition"] = json!({"decision": "accept", "approver": "policy", "human_disposed": false});
    a["effect"] = json!({"type": "mcp_tool_call", "status": "dispatched", "attestation": ""});
    let (r, s) = with(reseal(a));
    out.push(rv(
        "neg/attestation-missing",
        "a dispatched effect without attestation",
        r,
        s,
        false,
        &["attestation_missing"],
    ));

    let mut ap = run.denied.clone();
    ap["disposition"]["approver"] = json!("robot");
    let (r, s) = with(reseal(ap));
    out.push(rv(
        "neg/approver-invalid",
        "an approver outside human | policy | counterparty",
        r,
        s,
        false,
        &["approver_invalid"],
    ));
    out
}

// ---------------------------------------------------------------------------
// Logs, heads, receipts, bundles
// ---------------------------------------------------------------------------

/// An in-memory tenant log whose entries are statements.
struct Ledger {
    store: MemoryStore,
    size: u64,
    entries: Vec<Vec<u8>>,
}

impl Ledger {
    fn new() -> Self {
        Self {
            store: MemoryStore::default(),
            size: 0,
            entries: Vec::new(),
        }
    }
    fn append(&mut self, entry: &[u8]) -> u64 {
        let mut log = Log::open(&mut self.store, self.size);
        let seq = log.append(leaf_hash(entry)).unwrap();
        self.size += 1;
        self.entries.push(entry.to_vec());
        seq
    }
    fn root(&mut self, n: u64) -> Hash {
        Log::open(&mut self.store, self.size).root_at(n).unwrap()
    }
    fn path(&mut self, i: u64, n: u64) -> Vec<Hash> {
        Log::open(&mut self.store, self.size)
            .inclusion_path(i, n)
            .unwrap()
    }
    fn consistency(&mut self, m: u64, n: u64) -> Vec<Hash> {
        Log::open(&mut self.store, self.size)
            .consistency_proof(m, n)
            .unwrap()
    }
    fn head(&mut self, prev: Option<&TreeHead>, issued_at: &str, log: &str) -> TreeHead {
        let size = self.size;
        TreeHead {
            log_id: log.to_owned(),
            tree_size: size,
            root: self.root(size),
            prev_size: prev.map_or(0, |p| p.tree_size),
            prev_root: prev.map(|p| p.root),
            issued_at: issued_at.to_owned(),
            consistency_proof: prev.map_or(vec![], |p| self.consistency(p.tree_size, size)),
        }
    }
}

fn hexes(v: &[Hash]) -> Vec<String> {
    v.iter().map(hex::encode).collect()
}

/// Everything a bundle needs from the log at head 2's size: the size,
/// each record's `(leaf_index, inclusion path)` in record order, and the
/// consistency proof from head 1.
struct LogAtHead2 {
    size: u64,
    proofs: Vec<(u64, Vec<Hash>)>,
    cons: Vec<Hash>,
}

/// A sealed record, its Signed Statement and its leaf index in the log.
type LoggedRecord = (Value, Vec<u8>, u64);

/// A golden bundle: file name, bundle JSON and the digest the verifier reported.
type GoldenBundle = (String, Value, String);

struct World {
    ledger: Ledger,
    records: Vec<LoggedRecord>,
    head1: (TreeHead, Vec<u8>),
    head2: (TreeHead, Vec<u8>),
    head3: (TreeHead, Vec<u8>),
    receipt: Vec<u8>,
    transparent: Vec<u8>,
    log_at_head2: LogAtHead2,
}

fn world(k: &Keys, run: &Run) -> World {
    let d = &k.deployment;
    let log = log_id();
    let mut l = Ledger::new();
    let mut records = Vec::new();
    for r in [&run.awaiting, &run.approved, &run.planned, &run.confirmed] {
        let st = statement(r, d);
        let seq = l.append(&st);
        records.push((r.clone(), st, seq));
    }
    let h1 = l.head(None, "2026-09-19T10:05:00.000Z", &log);
    let h1s = h1.sign(d).unwrap();
    l.append(&h1s); // the head's own log entry (kind tree_head), as the gateway stamps it
    for r in [&run.blocked, &run.denied] {
        let st = statement(r, d);
        let seq = l.append(&st);
        records.push((r.clone(), st, seq));
    }
    let h2 = l.head(Some(&h1), "2026-09-19T10:10:00.000Z", &log);
    let h2s = h2.sign(d).unwrap();
    // Everything a bundle needs at size 7, before the log grows further.
    let size2 = h2.tree_size;
    let proofs: Vec<(u64, Vec<Hash>)> = records
        .iter()
        .map(|(_, _, seq)| (*seq, l.path(*seq, size2)))
        .collect();
    let cons = l.consistency(h1.tree_size, size2);
    // A witness registers head 2 in its own log (entry = statement bytes).
    let mut wl = Ledger::new();
    wl.append(b"witness-log-entry-0");
    wl.append(&h2s);
    wl.append(b"witness-log-entry-2");
    let receipt = build_receipt(
        &InclusionProof {
            tree_size: 3,
            leaf_index: 1,
            path: wl.path(1, 3),
        },
        &wl.root(3),
        &k.witness,
        Some(1_789_812_600),
    )
    .unwrap();
    let mut ts = CoseSign1::decode(&h2s).unwrap();
    ts.attach_receipts(std::slice::from_ref(&receipt));
    let transparent = ts.encode().unwrap();
    // The log grows: head 2's own entry and two more records, then head 3.
    l.append(&h2s);
    l.append(&statement(&run.denied, d));
    l.append(&statement(&run.blocked, d));
    let h3 = l.head(Some(&h2), "2026-09-19T10:15:00.000Z", &log);
    let h3s = h3.sign(d).unwrap();
    World {
        ledger: l,
        records,
        head1: (h1, h1s),
        head2: (h2, h2s),
        head3: (h3, h3s),
        receipt,
        transparent,
        log_at_head2: LogAtHead2 {
            size: size2,
            proofs,
            cons,
        },
    }
}

fn bundle_input(w: &World, pick: &[usize]) -> BundleInput {
    let LogAtHead2 {
        size: size2,
        proofs,
        cons,
    } = &w.log_at_head2;
    BundleInput {
        bundle_id: "bdl-vectors-run".into(),
        root_record_id: None,
        selector: json!({"root_task_id": RUN_ID}),
        produced_at: "2026-09-19T11:00:00.000Z".into(),
        issuer: ISS.into(),
        log_id: log_id(),
        framework_id: Some("eu-ai-act".into()),
        catalog_version: Some("2026-09".into()),
        closure_depth: 1,
        selection: selection::CONTIGUOUS.into(),
        missing: vec![],
        records: pick
            .iter()
            .map(|&i| BundleRecord {
                record: w.records[i].0.clone(),
                statement_b64: b64(&w.records[i].1),
                seq: w.records[i].2,
            })
            .collect(),
        proofs: pick
            .iter()
            .map(|&i| RecordProof {
                record_id: rid(&w.records[i].0),
                leaf_index: proofs[i].0,
                tree_size: *size2,
                path: hexes(&proofs[i].1),
            })
            .collect(),
        tree_heads: vec![
            BundleHead {
                statement_b64: b64(&w.head1.1),
                transparent_statement_b64: None,
                receipts: vec![],
            },
            BundleHead {
                statement_b64: b64(&w.head2.1),
                transparent_statement_b64: Some(b64(&w.transparent)),
                receipts: vec![BundleReceipt {
                    kind: "scitt".into(),
                    receipt_b64: b64(&w.receipt),
                    grade: "witnessed".into(),
                }],
            },
        ],
        consistency: vec![ConsistencyProof {
            from_size: w.head1.0.tree_size,
            to_size: *size2,
            path: hexes(cons),
        }],
        prunes: vec![],
        extensions: Map::new(),
    }
}

struct BundleCase {
    file: String,
    description: &'static str,
    bundle: Option<Value>,
    keys: Option<&'static str>,
    witness: bool,
    expected_digest: Option<String>,
    ok: bool,
    codes: Vec<&'static str>,
}

fn check_bundle(k: &Keys, c: &BundleCase, bundle: &Value) -> brutor_verify::Report {
    let keys = match c.keys {
        Some("keys/well-known.json") => k.trusted(),
        Some("keys/stranger.json") => {
            let mut t = TrustedKeys::unpinned();
            t.add_well_known(&k.stranger_doc()).unwrap();
            t
        }
        None => TrustedKeys::unpinned(),
        Some(other) => panic!("{other}"),
    };
    brutor_verify::verify_bundle(
        bundle,
        &BundleOptions {
            keys,
            expected_digest: c.expected_digest.clone(),
            witness_keys: if c.witness {
                vec![kid(&k.witness)]
            } else {
                vec![]
            },
            tsa_pins: vec![],
        },
    )
}

fn bundles(k: &Keys, w: &World, files: &mut BTreeMap<String, String>) -> Vec<GoldenBundle> {
    let mut cases: Vec<BundleCase> = Vec::new();
    let golden = |file: &str, description: &'static str, b: Value| BundleCase {
        file: file.into(),
        description,
        bundle: Some(b),
        keys: Some("keys/well-known.json"),
        witness: true,
        expected_digest: None,
        ok: true,
        codes: vec![],
    };
    // G1 — a run, contiguous, two heads, consistency, a scitt receipt.
    let mut input = bundle_input(w, &[0, 1, 2, 3, 4, 5]);
    input.extensions.insert(
        "ai.brutor/evidence-report/v1".into(),
        json!({"period_start": "2026-09-19", "period_end": "2026-09-20", "statements": [{"key": "art12.logging", "status": "met", "n": 6, "m": 6}]}),
    );
    let (g1, g1_digest) = bdl::build(input).unwrap();
    let mut c = golden(
        "golden/run-contiguous.json",
        "a run (root_task_id selector, contiguous): approval chain, two-phase action, a block and a denial; two heads with a consistency proof; the newest head witnessed by a scitt receipt and carried as a Transparent Statement",
        g1.clone(),
    );
    c.expected_digest = Some(g1_digest.clone());
    cases.push(c);

    // G2 — explicit ids with their closure.
    let mut input = bundle_input(w, &[2, 3]);
    input.bundle_id = "bdl-vectors-selected".into();
    input.selector = json!({"record_ids": [rid(&w.records[3].0)]});
    input.root_record_id = Some(rid(&w.records[3].0));
    input.selection = selection::PRODUCER_SELECTED.into();
    let (g2, _) = bdl::build(input.clone()).unwrap();
    cases.push(golden("golden/producer-selected-closure.json", "an explicit record id (producer-selected) and its chain parent followed at closure_depth 1", g2.clone()));
    let g2_input = input;

    // G3 — parent not followed, listed missing.
    let mut input = bundle_input(w, &[1]);
    input.bundle_id = "bdl-vectors-incomplete".into();
    input.selector = json!({"record_ids": [rid(&w.records[1].0)]});
    input.root_record_id = Some(rid(&w.records[1].0));
    input.selection = selection::PRODUCER_SELECTED.into();
    input.closure_depth = 0;
    input.missing = vec![rid(&w.records[0].0)];
    let (g3, _) = bdl::build(input).unwrap();
    cases.push(golden("golden/declared-incomplete.json", "closure_depth 0: the chain parent is not followed and is listed in completeness.missing (records_mode declared_incomplete)", g3.clone()));

    // G4 — one record, one head, a fresh log.
    let decl = {
        let mut b = builder("ai.brutor.declaration/01K5VEC0030", 30, verdict::RECORDED);
        b.kind = "fyi".into();
        b.context = ctx(&[
            ("record_type", json!("ai.brutor.declaration")),
            ("surface", json!("control_plane")),
        ]);
        seal(&b)
    };
    let mut l = Ledger::new();
    let st = statement(&decl, &k.deployment);
    l.append(&st);
    let h = l.head(None, "2026-09-19T10:30:00.000Z", &log_id());
    let hs = h.sign(&k.deployment).unwrap();
    let (g4, _) = bdl::build(BundleInput {
        bundle_id: "bdl-vectors-single".into(),
        root_record_id: Some(rid(&decl)),
        selector: json!({"record_ids": [rid(&decl)]}),
        produced_at: "2026-09-19T11:00:00.000Z".into(),
        issuer: ISS.into(),
        log_id: log_id(),
        framework_id: None,
        catalog_version: None,
        closure_depth: 1,
        selection: selection::PRODUCER_SELECTED.into(),
        missing: vec![],
        records: vec![BundleRecord {
            record: decl.clone(),
            statement_b64: b64(&st),
            seq: 0,
        }],
        proofs: vec![RecordProof {
            record_id: rid(&decl),
            leaf_index: 0,
            tree_size: 1,
            path: vec![],
        }],
        tree_heads: vec![BundleHead {
            statement_b64: b64(&hs),
            transparent_statement_b64: None,
            receipts: vec![],
        }],
        consistency: vec![],
        prunes: vec![],
        extensions: Map::new(),
    })
    .unwrap();
    cases.push(golden(
        "golden/single-record.json",
        "the smallest bundle: one record, a one-leaf log, one head, no consistency proof",
        g4,
    ));

    // Trust variations over G1 (no new file).
    cases.push(BundleCase {
        file: "golden/run-contiguous.json".into(),
        description: "G1 with no trusted keys and no witness key: self-consistent under the keys the statements name; the receipt is not checked",
        bundle: None,
        keys: None,
        witness: false,
        expected_digest: None,
        ok: true,
        codes: vec!["kid_not_pinned", "receipt_not_checked"],
    });
    cases.push(BundleCase {
        file: "golden/run-contiguous.json".into(),
        description: "G1 against a keys document that does not list the signer (wrong kid): every head and record statement is untrusted",
        bundle: None,
        keys: Some("keys/stranger.json"),
        witness: true,
        expected_digest: None,
        ok: false,
        codes: vec!["untrusted_kid", "untrusted_kid", "untrusted_kid", "untrusted_kid", "untrusted_kid", "untrusted_kid", "untrusted_kid", "untrusted_kid"],
    });

    // ---- tampered ---------------------------------------------------------------
    let mut tamper = |file: &str, description: &'static str, b: Value, codes: &[&'static str]| {
        cases.push(BundleCase {
            file: format!("tampered/{file}"),
            description,
            bundle: Some(b),
            keys: Some("keys/well-known.json"),
            witness: true,
            expected_digest: None,
            ok: false,
            codes: codes.to_vec(),
        });
    };
    let mut t = g1.clone();
    t["records"][1]["record"]["action_id"] = json!("mcp/01K5VEC0000000000000000099");
    tamper(
        "flipped-record-byte.json",
        "one record member changed after sealing",
        t,
        &["record_semantic_error", "statement_payload_mismatch"],
    );

    let mut t = g1.clone();
    t["records"][0]["statement_b64"] = t["records"][5]["statement_b64"].clone();
    tamper(
        "swapped-statement.json",
        "record 0 presented with record 5's statement",
        t,
        &["statement_payload_mismatch"],
    );

    let mut t = g1.clone();
    t["proofs"][2]["path"].as_array_mut().unwrap().pop();
    tamper(
        "truncated-proof-path.json",
        "an inclusion path with its last node removed",
        t,
        &["proof_invalid"],
    );

    let mut t = g1.clone();
    t["proofs"].as_array_mut().unwrap().remove(4);
    tamper(
        "proof-dropped.json",
        "a record without an inclusion proof (the manifest self-report no longer matches either)",
        t,
        &["proof_missing", "self_report_mismatch"],
    );

    let mut t = g1.clone();
    t["proofs"][0]["leaf_index"] = json!(1);
    tamper(
        "leaf-index-mismatch.json",
        "a proof claiming another leaf index than the record's seq",
        t,
        &["leaf_index_mismatch"],
    );

    let mut t = g1.clone();
    let mut raw = unb64(t["tree_heads"][1]["statement_b64"].as_str().unwrap());
    let n = raw.len();
    raw[n - 1] ^= 0x01;
    t["tree_heads"][1]["statement_b64"] = json!(b64(&raw));
    tamper(
        "wrong-head-signature.json",
        "the newest head's signature with one bit flipped (its receipt no longer matches either)",
        t,
        &["head_invalid", "receipt_invalid"],
    );

    let mut t = g1.clone();
    t["tree_heads"][0]["statement_b64"] = json!(b64(&w.head1.0.sign(&k.stranger).unwrap()));
    tamper(
        "head-signed-by-stranger.json",
        "the first head re-signed by a key outside the trusted set",
        t,
        &["untrusted_kid"],
    );

    let mut t = g1.clone();
    t["consistency"][0]["path"][0] = json!("00".repeat(32));
    tamper(
        "broken-consistency.json",
        "a consistency proof node zeroed",
        t,
        &["consistency_invalid"],
    );

    let mut t = g1.clone();
    t["consistency"] = json!([]);
    tamper(
        "consistency-dropped.json",
        "two heads without the proof that links them",
        t,
        &["consistency_missing"],
    );

    let mut t = g1.clone();
    t["tree_heads"][1]["transparent_statement_b64"] = t["tree_heads"][0]["statement_b64"].clone();
    tamper(
        "transparent-statement-mismatch.json",
        "the Transparent Statement is not the head it is carried with",
        t,
        &["transparent_statement_mismatch"],
    );

    let mut t = g1.clone();
    let mut raw = unb64(
        t["tree_heads"][1]["receipts"][0]["receipt_b64"]
            .as_str()
            .unwrap(),
    );
    let n = raw.len();
    raw[n - 1] ^= 0x01;
    t["tree_heads"][1]["receipts"][0]["receipt_b64"] = json!(b64(&raw));
    tamper(
        "receipt-tampered.json",
        "the witness receipt's signature with one bit flipped",
        t,
        &["receipt_invalid"],
    );

    let mut t = g1.clone();
    t["extensions"]["ai.brutor/evidence-report/v1"]["statements"][0]["status"] = json!("not_met");
    cases.push(BundleCase {
        file: "tampered/digest-mismatch.json".into(),
        description: "an edit to digest-covered content (an extension) checked against the digest the producer sealed",
        bundle: Some(t),
        keys: Some("keys/well-known.json"),
        witness: true,
        expected_digest: Some(g1_digest.clone()),
        ok: false,
        codes: vec!["bundle_digest_mismatch"],
    });

    let mut t = g3.clone();
    t["manifest"]["completeness"]["missing"] = json!([]);
    t["manifest"]["completeness"]["records_mode"] = json!("complete");
    cases.push(BundleCase {
        file: "tampered/missing-parent-not-listed.json".into(),
        description: "a chain parent neither included nor listed missing (dropped, not declared)",
        bundle: Some(t),
        keys: Some("keys/well-known.json"),
        witness: true,
        expected_digest: None,
        ok: false,
        codes: vec!["closure_parent_dropped"],
    });

    let mut t = g3.clone();
    t["manifest"]["completeness"]["records_mode"] = json!("complete");
    cases.push(BundleCase {
        file: "tampered/records-mode-lie.json".into(),
        description: "missing is non-empty but the manifest claims records_mode complete",
        bundle: Some(t),
        keys: Some("keys/well-known.json"),
        witness: true,
        expected_digest: None,
        ok: false,
        codes: vec!["records_mode_mismatch"],
    });

    let mut input = g2_input;
    input.records.push(BundleRecord {
        record: w.records[4].0.clone(),
        statement_b64: b64(&w.records[4].1),
        seq: w.records[4].2,
    });
    input.proofs.push(RecordProof {
        record_id: rid(&w.records[4].0),
        leaf_index: w.log_at_head2.proofs[4].0,
        tree_size: w.log_at_head2.size,
        path: hexes(&w.log_at_head2.proofs[4].1),
    });
    let (t, _) = bdl::build(input).unwrap();
    cases.push(BundleCase {
        file: "tampered/extra-record-not-in-selector.json".into(),
        description: "an explicit-id bundle carrying a validly signed and proven record nobody selected",
        bundle: Some(t),
        keys: Some("keys/well-known.json"),
        witness: true,
        expected_digest: None,
        ok: false,
        codes: vec!["record_not_in_selector"],
    });

    // ---- assert and emit -------------------------------------------------------------
    let mut index = Vec::new();
    let mut by_file: BTreeMap<String, Value> = BTreeMap::new();
    let mut golden_out = Vec::new();
    for c in &cases {
        let bundle = c.bundle.clone().unwrap_or_else(|| by_file[&c.file].clone());
        let rep = check_bundle(k, c, &bundle);
        assert_eq!(
            (rep.ok, rep.codes()),
            (c.ok, c.codes.clone()),
            "bundle vector {} ({}) does not verify as declared: {:#?}",
            c.file,
            c.description,
            rep.findings
        );
        if c.bundle.is_some() {
            files.insert(format!("vectors/bundles/{}", c.file), pretty(&bundle));
            by_file.insert(c.file.clone(), bundle.clone());
            if c.file.starts_with("golden/") {
                golden_out.push((
                    c.file.clone(),
                    bundle.clone(),
                    rep.facts["bundle_digest"].as_str().unwrap().to_owned(),
                ));
            }
        }
        index.push(json!({
            "file": c.file,
            "description": c.description,
            "keys": c.keys,
            "witness_keys": if c.witness { json!([kid(&k.witness)]) } else { json!([]) },
            "expected_digest": c.expected_digest,
            "expect": {
                "ok": c.ok,
                "codes": c.codes,
                "bundle_digest": rep.facts["bundle_digest"],
                "records_verified": rep.facts["records_verified"],
                "proofs_verified": rep.facts["proofs_verified"],
                "heads_verified": rep.facts["heads_verified"],
            },
        }));
    }
    files.insert(
        "vectors/bundles/expect.json".into(),
        pretty(&json!({
            "description": "Expected brutor-verify outcome for every bundle file (paths relative to vectors/bundles/, key documents relative to vectors/). `codes` lists every finding code, all severities, in emission order.",
            "generator": GENERATOR,
            "cases": index,
        })),
    );
    golden_out
}

fn heads(k: &Keys, w: &World, files: &mut BTreeMap<String, String>) {
    let d = &k.deployment;
    let trusted = k.trusted();
    let (h1, h1s) = &w.head1;
    let (h2, h2s) = &w.head2;
    let (h3, h3s) = &w.head3;
    // A fork: a validly signed head at head 2's size with another root.
    let fork = TreeHead {
        root: leaf_hash(b"fork"),
        prev_size: 0,
        prev_root: None,
        consistency_proof: vec![],
        issued_at: "2026-09-19T10:10:30.000Z".into(),
        ..h2.clone()
    };
    let fork_s = fork.sign(d).unwrap();
    // A head whose embedded proof does not reproduce its root.
    let mut lying = h3.clone();
    lying.root = leaf_hash(b"rewritten history");
    let lying_s = lying.sign(d).unwrap();
    let other_log = TreeHead {
        log_id: sha_hex("another tenant's log"),
        ..h1.clone()
    };
    let other_log_s = other_log.sign(d).unwrap();
    let mut flipped = h2s.clone();
    let n = flipped.len();
    flipped[n - 1] ^= 0x01;
    let cons_1_9: Vec<String> = {
        let mut l = Ledger {
            store: w.ledger.store.clone(),
            size: w.ledger.size,
            entries: vec![],
        };
        hexes(&l.consistency(h1.tree_size, h3.tree_size))
    };
    struct HV {
        name: &'static str,
        description: &'static str,
        head: Vec<u8>,
        previous: Option<Vec<u8>>,
        consistency: Option<Vec<String>>,
        keys: TrustedKeys,
        keys_file: Option<&'static str>,
        ok: bool,
        codes: Vec<&'static str>,
        consistent: Value,
    }
    let stranger = {
        let mut t = TrustedKeys::unpinned();
        t.add_well_known(&k.stranger_doc()).unwrap();
        t
    };
    let hv = |name,
              description,
              head: &Vec<u8>,
              previous: Option<&Vec<u8>>,
              ok,
              codes: &[&'static str],
              consistent: Value| HV {
        name,
        description,
        head: head.clone(),
        previous: previous.cloned(),
        consistency: None,
        keys: trusted.clone(),
        keys_file: Some("keys/well-known.json"),
        ok,
        codes: codes.to_vec(),
        consistent,
    };
    let mut cases = vec![
        hv(
            "first-head",
            "a first head (prev_size 0): signature and payload only",
            h1s,
            None,
            true,
            &[],
            Value::Null,
        ),
        hv(
            "adjacent-heads",
            "head 2 chains from head 1 (prev_size = head 1's size): consistency follows from the embedded proof",
            h2s,
            Some(h1s),
            true,
            &[],
            json!(true),
        ),
        hv(
            "non-adjacent-without-proof",
            "head 3 against head 1 without a consistency proof: undetermined, never assumed",
            h3s,
            Some(h1s),
            false,
            &["consistency_undetermined"],
            Value::Null,
        ),
        hv(
            "fork-same-size",
            "two validly signed heads of the same size with different roots: a fork",
            h2s,
            Some(&fork_s),
            false,
            &["head_not_consistent"],
            json!(false),
        ),
        hv(
            "embedded-proof-fails",
            "a head whose root was rewritten: its embedded consistency proof from prev_root no longer holds",
            &lying_s,
            None,
            false,
            &["head_consistency_invalid"],
            Value::Null,
        ),
        hv(
            "different-logs",
            "heads of two different logs",
            h2s,
            Some(&other_log_s),
            false,
            &["head_log_mismatch"],
            Value::Null,
        ),
        hv(
            "bad-signature",
            "a head signature with one bit flipped",
            &flipped,
            None,
            false,
            &["head_signature_invalid"],
            Value::Null,
        ),
    ];
    let mut c = hv(
        "non-adjacent-with-proof",
        "head 3 against head 1 (not adjacent) with an RFC 6962 consistency proof from head 1's size to head 3's",
        h3s,
        Some(h1s),
        true,
        &[],
        json!(true),
    );
    c.consistency = Some(cons_1_9);
    cases.push(c);
    let mut c = hv(
        "untrusted-kid",
        "a head verified against a keys document that does not list its signer",
        h1s,
        None,
        false,
        &["untrusted_kid"],
        Value::Null,
    );
    c.keys = stranger;
    c.keys_file = Some("keys/stranger.json");
    cases.push(c);
    for c in cases {
        let rep = brutor_verify::verify_head(
            &c.head,
            c.previous.as_deref(),
            c.consistency.as_deref(),
            &c.keys,
        );
        assert_eq!(
            (rep.ok, rep.codes()),
            (c.ok, c.codes.clone()),
            "head vector {}: {:#?}",
            c.name,
            rep.findings
        );
        let got = rep
            .facts
            .get("consistent_with_previous")
            .cloned()
            .unwrap_or(Value::Null);
        assert_eq!(got, c.consistent, "head vector {} consistency", c.name);
        files.insert(
            format!("vectors/heads/{}.json", c.name),
            pretty(&json!({
                "description": c.description,
                "statement_b64": b64(&c.head),
                "previous_b64": c.previous.as_ref().map(|p| b64(p)),
                "consistency_proof": c.consistency,
                "keys": c.keys_file,
                "expect": {
                    "ok": c.ok,
                    "codes": c.codes,
                    "consistent_with_previous": c.consistent,
                    "log_id": rep.facts.get("log_id"),
                    "tree_size": rep.facts.get("tree_size"),
                    "root": rep.facts.get("root"),
                },
            })),
        );
    }
}

fn receipts(k: &Keys, w: &World, files: &mut BTreeMap<String, String>) {
    let fx =
        |name: &str| std::fs::read(core_root().join("tests/fixtures/rfc3161").join(name)).unwrap();
    let statement_bin = fx("statement.bin");
    struct RcV {
        name: String,
        description: String,
        kind: &'static str,
        receipt: Vec<u8>,
        statement: Vec<u8>,
        pin: Option<String>,
        grade: &'static str,
        codes: Vec<&'static str>,
    }
    let wk = kid(&k.witness);
    let mut cases = vec![
        RcV { name: "scitt-witnessed".into(), description: "an RFC 9942 receipt (vds 1) for head 2 from a witness log of 3 entries, leaf 1 = the head statement bytes; pinned witness key".into(), kind: "scitt", receipt: w.receipt.clone(), statement: w.head2.1.clone(), pin: Some(wk.clone()), grade: "witnessed", codes: vec![] },
        RcV { name: "scitt-unpinned".into(), description: "the same receipt with no pinned key: nothing checked (not evidence of forgery)".into(), kind: "scitt", receipt: w.receipt.clone(), statement: w.head2.1.clone(), pin: None, grade: "unverified", codes: vec!["receipt_unpinned"] },
        RcV { name: "scitt-wrong-pin".into(), description: "the same receipt under a key that is not the witness's".into(), kind: "scitt", receipt: w.receipt.clone(), statement: w.head2.1.clone(), pin: Some(kid(&k.stranger)), grade: "invalid", codes: vec!["receipt_invalid"] },
        RcV { name: "scitt-other-statement".into(), description: "the receipt presented for a different head".into(), kind: "scitt", receipt: w.receipt.clone(), statement: w.head1.1.clone(), pin: Some(wk.clone()), grade: "invalid", codes: vec!["receipt_invalid"] },
    ];
    for alg in ["ecp256", "ecp384", "rsa2048"] {
        cases.push(RcV {
            name: format!("rfc3161-{alg}"),
            description: format!("an RFC 3161 TimeStampToken ({alg} TSA) over SHA-256 of the statement bytes; pinned by the SHA-256 of the TSA certificate DER (fixture: tests/fixtures/rfc3161/{alg}.tsr)"),
            kind: "rfc3161",
            receipt: fx(&format!("{alg}.tsr")),
            statement: statement_bin.clone(),
            pin: Some(String::from_utf8(fx(&format!("{alg}.crt.sha256"))).unwrap().trim().to_owned()),
            grade: "timestamped",
            codes: vec![],
        });
    }
    cases.push(RcV {
        name: "rfc3161-other-statement".into(),
        description:
            "the ecp256 token presented for other bytes: the message imprint does not match".into(),
        kind: "rfc3161",
        receipt: fx("ecp256.tsr"),
        statement: w.head1.1.clone(),
        pin: Some(
            String::from_utf8(fx("ecp256.crt.sha256"))
                .unwrap()
                .trim()
                .to_owned(),
        ),
        grade: "invalid",
        codes: vec!["receipt_invalid"],
    });
    cases.push(RcV {
        name: "rfc3161-unpinned".into(),
        description: "the ecp256 token with no pinned certificate".into(),
        kind: "rfc3161",
        receipt: fx("ecp256.tsr"),
        statement: statement_bin.clone(),
        pin: None,
        grade: "unverified",
        codes: vec!["receipt_unpinned"],
    });
    for c in cases {
        let rep =
            brutor_verify::verify_receipt(&c.receipt, &c.statement, Some(c.kind), c.pin.as_deref());
        assert_eq!(
            rep.facts["grade"], c.grade,
            "receipt vector {}: {:#?}",
            c.name, rep.findings
        );
        assert_eq!(rep.codes(), c.codes, "receipt vector {}", c.name);
        // Detection agrees with the declared kind.
        assert_eq!(brutor_verify::receipt::detect_kind(&c.receipt), c.kind);
        files.insert(
            format!("vectors/receipts/{}.json", c.name),
            pretty(&json!({
                "description": c.description,
                "kind": c.kind,
                "receipt_b64": b64(&c.receipt),
                "statement_b64": b64(&c.statement),
                "pin": c.pin,
                "expect": {
                    "grade": c.grade,
                    "ok": c.codes.iter().all(|x| *x == "receipt_unpinned"),
                    "codes": c.codes,
                    "tree_size": rep.facts["tree_size"],
                    "leaf_index": rep.facts["leaf_index"],
                    "iat": rep.facts["iat"],
                },
            })),
        );
    }
}

/// RFC 6962 inclusion / consistency cases from our log, with entries, so
/// a verifier that takes entries (scitt-cose) and one that takes leaf
/// hashes (the CP's merkle.py) can both run them.
fn logs(files: &mut BTreeMap<String, String>) -> Value {
    const N: u64 = 13;
    let mut l = Ledger::new();
    let entries: Vec<Vec<u8>> = (0..N)
        .map(|i| format!("brutor-log-vector-{i:02}").into_bytes())
        .collect();
    for e in &entries {
        l.append(e);
    }
    let leaves: Vec<Hash> = entries.iter().map(|e| leaf_hash(e)).collect();
    let roots: Map<String, Value> = (1..=N)
        .map(|n| (n.to_string(), json!(hex::encode(l.root(n)))))
        .collect();
    // Cross-check the stored-node log against the recursive definition.
    for n in 1..=N {
        assert_eq!(l.root(n), merkle::tree_hash(&leaves[..n as usize]));
    }
    let mut inc = Vec::new();
    for n in [1u64, 2, 3, 4, 5, 6, 7, 8, 11, 13] {
        for i in 0..n {
            let p = l.path(i, n);
            assert!(merkle::verify_inclusion(&leaves[i as usize], i, n, &p, &l.root(n)).is_ok());
            inc.push(json!({"index": i, "tree_size": n, "path": hexes(&p), "root": hex::encode(l.root(n)), "expect": true}));
        }
    }
    let p = l.path(5, 13);
    let neg = |desc: &str, i: u64, n: u64, path: Vec<Hash>, root: Hash| {
        assert!(
            merkle::verify_inclusion(&leaves[5], i, n, &path, &root).is_err(),
            "{desc}"
        );
        json!({"description": desc, "entry_index": 5, "index": i, "tree_size": n, "path": hexes(&path), "root": hex::encode(root), "expect": false})
    };
    let mut truncated = p.clone();
    truncated.pop();
    let mut extended = p.clone();
    extended.push(leaves[0]);
    let mut flipped = p.clone();
    flipped[0][0] ^= 1;
    let negatives = vec![
        neg("path truncated by one node", 5, 13, truncated, l.root(13)),
        neg("path extended by one node", 5, 13, extended, l.root(13)),
        neg(
            "a path node with one bit flipped",
            5,
            13,
            flipped,
            l.root(13),
        ),
        neg("right path, wrong index", 4, 13, p.clone(), l.root(13)),
        neg(
            "right path, a tree size whose path shape differs (7)",
            5,
            7,
            p.clone(),
            l.root(13),
        ),
        neg(
            "right path, root of a smaller tree",
            5,
            13,
            p.clone(),
            l.root(12),
        ),
    ];
    let mut cons = Vec::new();
    for n in 2..=N {
        for m in 1..n {
            let proof = l.consistency(m, n);
            assert!(merkle::verify_consistency(m, &l.root(m), n, &l.root(n), &proof).is_ok());
            cons.push(json!({"first_size": m, "second_size": n, "first_root": hex::encode(l.root(m)), "second_root": hex::encode(l.root(n)), "path": hexes(&proof), "expect": true}));
        }
    }
    let proof = l.consistency(6, 13);
    let cneg = |desc: &str, m: u64, n: u64, path: Vec<Hash>, r1: Hash, r2: Hash| {
        assert!(
            merkle::verify_consistency(m, &r1, n, &r2, &path).is_err(),
            "{desc}"
        );
        json!({"description": desc, "first_size": m, "second_size": n, "first_root": hex::encode(r1), "second_root": hex::encode(r2), "path": hexes(&path), "expect": false})
    };
    let mut ctrunc = proof.clone();
    ctrunc.pop();
    let mut cflip = proof.clone();
    cflip[1][31] ^= 0x80;
    let cnegatives = vec![
        cneg(
            "proof truncated by one node",
            6,
            13,
            ctrunc,
            l.root(6),
            l.root(13),
        ),
        cneg(
            "a proof node with one bit flipped",
            6,
            13,
            cflip,
            l.root(6),
            l.root(13),
        ),
        cneg(
            "first root of another tree (a rewritten prefix)",
            6,
            13,
            proof.clone(),
            leaf_hash(b"rewritten"),
            l.root(13),
        ),
        cneg("sizes swapped", 13, 6, proof.clone(), l.root(13), l.root(6)),
        cneg(
            "right proof, wrong first size",
            5,
            13,
            proof.clone(),
            l.root(5),
            l.root(13),
        ),
    ];
    let mut nodes: Vec<(u32, u64, Hash)> = l
        .store
        .0
        .iter()
        .map(|((lv, ix), h)| (*lv, *ix, *h))
        .collect();
    nodes.sort_by_key(|(lv, ix, _)| (*lv, *ix));
    let node_json: Vec<Value> = nodes
        .iter()
        .map(|(lv, ix, h)| json!({"level": lv, "index": ix, "hash": hex::encode(h)}))
        .collect();
    let entries_hex: Vec<String> = entries.iter().map(hex::encode).collect();
    let common = json!({
        "entries_hex": entries_hex,
        "leaf_hashes": hexes(&leaves),
        "roots": roots,
    });
    files.insert(
        "vectors/logs/inclusion.json".into(),
        pretty(&json!({
            "description": "RFC 6962 / RFC 9162 inclusion proofs from brutor-ctlog's stored-node log of 13 entries. leaf = SHA-256(0x00 || entry); node = SHA-256(0x01 || left || right). Negative cases name the leaf by entry_index. The RFC 9162 published vectors are pinned in crates/brutor-cose/src/merkle.rs (tests rfc_9162_published_*), not duplicated here.",
            "log": common,
            "cases": inc,
            "negative": negatives,
        })),
    );
    files.insert(
        "vectors/logs/consistency.json".into(),
        pretty(&json!({
            "description": "RFC 6962 §2.1.2 consistency proofs PROOF(m, D[n]) for every 1 <= m < n <= 13 over the same log, and negative cases.",
            "log": common,
            "cases": cons,
            "negative": cnegatives,
        })),
    );
    files.insert(
        "vectors/logs/store-nodes.json".into(),
        pretty(&json!({
            "description": "The complete-subtree node set a store-backed log of 13 leaves holds: node (level, index) = MTH of leaves [index*2^level, (index+1)*2^level); it exists iff that range is full. Roots and inclusion paths of the other two files are derivable from these nodes alone.",
            "log": common,
            "nodes": node_json,
        })),
    );
    json!({"tree_size": N, "leaf_hashes": hexes(&leaves), "roots": roots, "nodes": node_json, "inclusion": inc})
}

fn canon_cases(files: &mut BTreeMap<String, String>) -> Value {
    let cases: Vec<(&str, &str)> = vec![
        (
            "members sorted by UTF-16 code units, recursively",
            r#"{"b":{"z":1,"a":[3,{"y":0,"x":0}]},"a":null,"c":true}"#,
        ),
        (
            "U+10000 (surrogate pair D800 DC00) sorts before U+E000 and U+FFFD",
            "{\"\u{FFFD}\":1,\"\u{E000}\":2,\"\u{10000}\":3,\"\u{20AC}\":4}",
        ),
        (
            "short escapes, \\u00xx for the rest of C0, DEL and non-ASCII literal, '/' not escaped",
            "{\"s\":\"\\u0001\\b\\t\\n\\f\\r\\\"\\\\/\u{e9}\u{7f}\\u2028\"}",
        ),
        (
            "whitespace removed, integers as plain decimals",
            "{ \"n\" : [ 0 , -7 , 9007199254740991 , -9007199254740991 ] }",
        ),
        (
            "empty containers and literals",
            r#"[{},[],"",null,true,false]"#,
        ),
        (
            "a float is refused: a digested document carries integers only, whose spelling every implementation agrees on",
            r#"{"a":1.5}"#,
        ),
        (
            "an integral float spelling is still a float",
            r#"{"a":1.0}"#,
        ),
        (
            "2^53 is outside the safe range",
            r#"{"a":9007199254740992}"#,
        ),
    ];
    let out: Vec<Value> = cases
        .iter()
        .map(|(desc, input)| {
            let v: Value = serde_json::from_str(input).unwrap();
            match canonical_bytes(&v) {
                Ok(b) => json!({"description": desc, "input": input, "canonical": String::from_utf8(b.clone()).unwrap(), "sha256": hex::encode(Sha256::digest(&b))}),
                Err(canon::CanonError::Float(p)) => json!({"description": desc, "input": input, "error": "float", "path": p}),
                Err(canon::CanonError::UnsafeInteger(p)) => json!({"description": desc, "input": input, "error": "unsafe_integer", "path": p}),
                Err(e) => panic!("{e}"),
            }
        })
        .collect();
    files.insert(
        "vectors/canon/jcs.json".into(),
        pretty(&json!({
            "description": "RFC 8785 JSON Canonicalization Scheme as Brutor digests use it: `input` is literal JSON text; `canonical` is the UTF-8 JCS output, `sha256` its digest; `error` names the digest-hygiene rule that refuses the input (floats anywhere; integers outside ±(2^53−1)).",
            "cases": out,
        })),
    );
    json!(out)
}

// ---------------------------------------------------------------------------
// Everything
// ---------------------------------------------------------------------------

pub fn generate() -> Corpus {
    let k = Keys::new();
    let run = run();
    let mut files: BTreeMap<String, String> = BTreeMap::new();

    files.insert(
        "vectors/keys/well-known.json".into(),
        pretty(&k.well_known()),
    );
    files.insert(
        "vectors/keys/stranger.json".into(),
        pretty(&k.stranger_doc()),
    );
    files.insert(
        "vectors/keys/witness.json".into(),
        pretty(&json!({
            "description": "The corpus witness (an RFC 9943 transparency service). Its Ed25519 public key is the pin for every scitt receipt in the corpus.",
            "witness_key_hex": kid(&k.witness),
            "operator": "corpus-witness",
        })),
    );

    // ---- records --------------------------------------------------------------------
    let trusted = k.trusted();
    let mut cp_records = Vec::new();
    for v in record_vectors(&k, &run) {
        let rep = brutor_verify::verify_record(
            &v.record,
            v.statement.as_deref(),
            v.store.as_deref(),
            &trusted,
        );
        assert_eq!(
            (rep.ok, rep.codes()),
            (v.ok, v.codes.clone()),
            "record vector {} does not verify as declared: {:#?}",
            v.name,
            rep.findings
        );
        let canonical = {
            let mut c = v.record.clone();
            c.as_object_mut().unwrap().remove("record_id");
            canonical_bytes(&c)
                .ok()
                .map(|b| String::from_utf8(b).unwrap())
        };
        if let Some(pre) = v.preimages.as_object() {
            for (path, spec) in pre {
                let carried = at(&v.record, path)
                    .and_then(Value::as_str)
                    .unwrap_or_else(|| panic!("{}: {path}", v.name));
                assert_eq!(
                    preimage_digest(&v.record, spec),
                    carried,
                    "{}: preimage {path}",
                    v.name
                );
                if let Some(body) = spec.get("body").and_then(Value::as_str) {
                    assert_eq!(body_value(body).0, spec["value"], "{}: body {path}", v.name);
                }
            }
        }
        let (dir, name) = match v.name.strip_prefix("neg/") {
            Some(n) => ("negative", n),
            None => ("positive", v.name),
        };
        let mut doc = json!({
            "description": v.description,
            "record": v.record,
            "preimages": v.preimages,
            "expect": {
                "ok": v.ok,
                "codes": v.codes,
                "record_id": rep.facts["record_id"],
                "effect_mode": rep.facts["effect_mode"],
            },
        });
        if let Some(s) = &v.statement {
            doc["statement_b64"] = json!(b64(s));
        }
        if let Some(s) = &v.store {
            doc["store"] = json!(s);
        }
        if let Some(c) = &canonical {
            doc["canonical"] = json!(c);
        }
        files.insert(format!("vectors/records/{dir}/{name}.json"), pretty(&doc));
        if let (true, Some(st)) = (dir == "positive", &v.statement) {
            cp_records.push(json!({
                "name": name,
                "record": v.record,
                "record_id": rep.facts["record_id"],
                "canonical": canonical,
                "statement_b64": b64(st),
                "leaf_hash": hex::encode(leaf_hash(st)),
                "preimages": v.preimages,
            }));
        }
    }

    // ---- logs, heads, receipts, bundles ----------------------------------------------
    let log = logs(&mut files);
    let canon = canon_cases(&mut files);
    let w = world(&k, &run);
    heads(&k, &w, &mut files);
    receipts(&k, &w, &mut files);
    let golden = bundles(&k, &w, &mut files);

    // ---- SHA256SUMS over everything generated ----------------------------------------
    let mut sums = String::new();
    for (path, content) in &files {
        sums.push_str(&format!(
            "{}  {}\n",
            hex::encode(Sha256::digest(content.as_bytes())),
            path.strip_prefix("vectors/").unwrap()
        ));
    }
    files.insert("vectors/SHA256SUMS".into(), sums);

    // ---- control-plane parity fixture ------------------------------------------------
    let cp = json!({
        "version": 1,
        "description": "Rust-produced evidence artefacts the control plane (Python, no cryptography) must reproduce: RFC 8785 canonical bytes and record_ids, salted digests, RFC 6962 leaf hashes, the stored-node set, roots and inclusion proofs (via app/services/evidence/merkle.py), and bundle digests. Regenerate with the generator named below; tests/test_evidence_rust_interop.py pins it.",
        "generator": GENERATOR,
        "canon": canon,
        "records": cp_records,
        "log": log,
        "bundles": golden.iter().map(|(f, b, d)| json!({"file": f, "bundle_digest": d, "bundle": b})).collect::<Vec<_>>(),
    });
    Corpus {
        files,
        cp_fixture: pretty(&cp),
    }
}
