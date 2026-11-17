// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Our own vectors: `ai.brutor/action-record/v1` → AAC Capsule, built with
//! `brutor_evidence::RecordBuilder` in the shapes the gateway mints and the
//! seal API accepts. The expected Capsules are frozen in
//! `vectors/brutor-mapping/vectors.json`; the interlock fixtures (one
//! Capsule JSON + one Producer Envelope per mapped record) are frozen under
//! `vectors/brutor-mapping/interlock/`.
//!
//! Regenerate (only when the mapping changes on purpose — bump
//! `MAPPING_ID` if an existing Capsule would change):
//!
//! ```sh
//! BRUTOR_AAC_REGEN=1 cargo test -p brutor-aac --test mapping_vectors
//! ```
//!
//! Cross-verify with the upstream Python reference verifier (not run in
//! CI; needs a checkout of agent-action-capsule, see vectors/README.md):
//!
//! ```sh
//! AAC_UPSTREAM=/path/to/agent-action-capsule \
//!   cargo test -p brutor-aac --test mapping_vectors -- --ignored
//! ```

use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use brutor_aac::mapping::{MAPPING_ID, map_lineage};
use brutor_aac::verify::{Severity, verify_store};
use brutor_aac::{SPEC_VERSION, sign_producer_envelope, verify_producer_envelope};
use brutor_evidence::record::{
    ATTESTATION_GATE_OBSERVED, Attestation, Chain, Control, Disposition, Effect, Model,
    OBSERVATION_IN_PATH, RUNTIME, RecordBuilder, Reference, System, approver, decision,
    effect_status, kind, relation, verdict,
};
use ed25519_dalek::SigningKey;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

/// Public test seed — never a production key.
const TEST_SEED: [u8; 32] = [0x42; 32];

fn dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("vectors/brutor-mapping")
}

fn h(label: &str) -> String {
    hex::encode(Sha256::digest(label.as_bytes()))
}

const TENANT: &str = "tenant-acme";

fn system() -> Option<System> {
    Some(System {
        ai_system_id: "sys-claims-triage".into(),
        contract_version: Some(3),
        contract_hash: Some(h("contract-v3")),
    })
}

fn policy(dec: &str) -> Option<Disposition> {
    Some(Disposition {
        decision: dec.into(),
        approver: approver::POLICY.into(),
        human_disposed: false,
        authority: None,
        reason_digest: None,
    })
}

fn human(dec: &str, authority: &str) -> Option<Disposition> {
    Some(Disposition {
        decision: dec.into(),
        approver: approver::HUMAN.into(),
        human_disposed: true,
        authority: Some(authority.into()),
        reason_digest: None,
    })
}

fn effect(ty: &str, status: &str, req: Option<&str>, resp: Option<&str>) -> Option<Effect> {
    Some(Effect {
        kind: ty.into(),
        status: status.into(),
        request_digest: req.map(h),
        response_digest: resp.map(h),
        attestation: ATTESTATION_GATE_OBSERVED.into(),
        irreversibility: None,
    })
}

fn attestation(input: Option<&str>, output: Option<&str>) -> Option<Attestation> {
    Some(Attestation {
        runtime: RUNTIME.into(),
        observation: OBSERVATION_IN_PATH.into(),
        input_digest: input.map(h),
        output_digest: output.map(h),
        digest_salt: None,
        numbers_canonicalized: false,
    })
}

fn control(id: &str, result: &str, blocking: bool, k: &str) -> Control {
    Control {
        id: id.into(),
        result: result.into(),
        blocking,
        severity: Some(if result == "fail" { "high" } else { "info" }.into()),
        kind: Some(k.into()),
        evidence_digest: None,
    }
}

fn ctx(pairs: Value) -> Map<String, Value> {
    pairs.as_object().unwrap().clone()
}

fn gateway_row(surface: &str, log: &str, ts: &str, v: &str) -> RecordBuilder {
    let mut b = RecordBuilder::new(format!("{surface}/{log}"), TENANT, ts);
    b.kind = kind::DECIDE.into();
    b.verdict = v.into();
    b.system = system();
    b.epoch = Some(format!("contract:{}", h("contract-v3")));
    b.attestation = attestation(Some(&format!("{log}-req")), None);
    b.context = ctx(
        json!({"surface": surface, "proxy_log_id": log, "root_task_id": "task-9f2", "turn_id": "turn-3"}),
    );
    b.references = Some(vec![Reference {
        kind: "ai.brutor.proxy-log-row".into(),
        digest_alg: "SHA-256".into(),
        digest: h(&format!("{log}-row")),
        purpose: None,
    }]);
    b
}

fn seal(b: &RecordBuilder) -> (Value, String) {
    b.seal().expect("fixture record must seal")
}

fn chained(mut b: RecordBuilder, parent: &str, rel: &str) -> RecordBuilder {
    b.chain = Some(Chain {
        parent_record_id: parent.into(),
        relation: rel.into(),
    });
    b
}

/// `(name, description, lineage root-first)`.
fn fixtures() -> Vec<(&'static str, &'static str, Vec<Value>)> {
    let mut out = Vec::new();

    let mut b = gateway_row(
        "mcp",
        "pl-0001",
        "2026-09-19T10:00:00.123Z",
        verdict::EXECUTED,
    );
    b.disposition = policy(decision::ACCEPT);
    b.effect = effect(
        "tools/call:lookup_claim",
        effect_status::CONFIRMED,
        Some("pl-0001-req"),
        Some("pl-0001-resp"),
    );
    b.attestation = attestation(Some("pl-0001-req"), Some("pl-0001-resp"));
    b.controls = vec![control(
        "ai.brutor.guardrail.pii",
        "pass",
        false,
        "guardrail",
    )];
    b.context = ctx(json!({
        "surface": "mcp", "proxy_log_id": "pl-0001", "root_task_id": "task-9f2", "turn_id": "turn-3",
        "server_version": "1.4.0", "tool_definition_hash": h("tooldef"), "autonomy_level": "supervised",
        "autonomy_provenance": "contract", "http_status": 200, "tags": ["hipaa.phi", "soc2.cc7.2"]
    }));
    b.references.as_mut().unwrap().extend([
        Reference {
            kind: "ai.brutor.ai-system-contract".into(),
            digest_alg: "SHA-256".into(),
            digest: h("contract-v3"),
            purpose: None,
        },
        Reference {
            kind: "ai.brutor.mcp-tool-definition".into(),
            digest_alg: "SHA-256".into(),
            digest: h("tooldef"),
            purpose: None,
        },
    ]);
    out.push((
        "mcp-executed-confirmed",
        "MCP tool call allowed, upstream ok: confirmed effect, controls, tags, references",
        vec![seal(&b).0],
    ));

    let mut b = gateway_row(
        "llm",
        "pl-0002",
        "2026-09-19T10:00:01.000Z",
        verdict::EXECUTED,
    );
    b.disposition = policy(decision::ACCEPT);
    b.effect = effect(
        "inference_completion",
        effect_status::CONFIRMED,
        Some("pl-0002-req"),
        Some("pl-0002-resp"),
    );
    b.attestation = attestation(Some("pl-0002-req"), Some("pl-0002-resp"));
    b.model = Some(Model {
        model_id: Some("gpt-5.1".into()),
        provider: Some("openai".into()),
        deciding_model: None,
    });
    out.push((
        "llm-executed-model",
        "LLM completion: model attestation; effect.type is a CPB provisional value",
        vec![seal(&b).0],
    ));

    let mut b = gateway_row(
        "mcp",
        "pl-0003",
        "2026-09-19T10:00:02.000Z",
        verdict::BLOCKED,
    );
    b.disposition = policy(decision::REJECT);
    b.controls = vec![control(
        "ai.brutor.guardrail.prompt_injection",
        "fail",
        true,
        "guardrail",
    )];
    out.push((
        "mcp-blocked-guardrail",
        "guardrail block: no effect, blocking control failed",
        vec![seal(&b).0],
    ));

    let mut b = gateway_row(
        "mcp",
        "pl-0004",
        "2026-09-19T10:00:03.000Z",
        verdict::ERRORED,
    );
    b.disposition = policy(decision::ACCEPT);
    b.effect = effect(
        "tools/call:file_claim",
        effect_status::FAILED,
        Some("pl-0004-req"),
        Some("pl-0004-resp"),
    );
    out.push((
        "mcp-errored-failed",
        "tool isError: failed effect, dispatched_unconfirmed",
        vec![seal(&b).0],
    ));

    let mut b = gateway_row(
        "a2a",
        "pl-0005",
        "2026-09-19T10:00:04.000Z",
        verdict::TIMEOUT,
    );
    b.disposition = policy(decision::ACCEPT);
    b.effect = effect(
        "a2a:message",
        effect_status::DISPATCHED,
        Some("pl-0005-req"),
        None,
    );
    out.push((
        "a2a-timeout-dispatched",
        "A2A timeout after dispatch",
        vec![seal(&b).0],
    ));

    let mut d = gateway_row(
        "mcp",
        "pl-0006",
        "2026-09-19T10:00:05.000Z",
        verdict::DENIED,
    );
    d.system = None;
    d.epoch = None;
    d.disposition = policy(decision::REJECT);
    d.controls = vec![control(
        "ai.brutor.grant.tool_scope",
        "fail",
        true,
        "agent_grant",
    )];
    out.push((
        "ungoverned-denied",
        "no AI System: developer = urn:brutor:ungoverned",
        vec![seal(&d).0],
    ));

    // Approval: awaiting_human (planned) → granted executed ⟨supersedes⟩.
    let mut w = gateway_row(
        "mcp",
        "pl-0007",
        "2026-09-19T10:01:00.000Z",
        verdict::AWAITING_HUMAN,
    );
    w.disposition = policy(decision::NEEDS_INPUT);
    w.effect = effect("tools/call:pay_claim", effect_status::PLANNED, None, None);
    w.context
        .insert("approval_request_id".into(), json!("apr-77"));
    let (wait, wait_id) = seal(&w);
    let mut g = gateway_row(
        "mcp",
        "pl-0008",
        "2026-09-19T10:05:00.000Z",
        verdict::EXECUTED,
    );
    g.disposition = human(decision::ACCEPT, "apr-77");
    g.effect = effect(
        "tools/call:pay_claim",
        effect_status::CONFIRMED,
        Some("pl-0008-req"),
        Some("pl-0008-resp"),
    );
    let (granted, _) = seal(&chained(g, &wait_id, relation::SUPERSEDES));
    out.push((
        "approval-granted",
        "awaiting_human (planned carve) superseded by a human-approved execution",
        vec![wait.clone(), granted],
    ));

    let mut r = RecordBuilder::new("approval/apr-77-reject", TENANT, "2026-09-19T10:06:00.000Z");
    r.verdict = verdict::RESOLVED.into();
    r.system = system();
    r.disposition = human(decision::REJECT, "apr-77");
    let (rejected, _) = seal(&chained(r, &wait_id, relation::SUPERSEDES));
    out.push((
        "approval-rejected",
        "non-executing closure: resolved ⟨supersedes⟩, human",
        vec![wait.clone(), rejected],
    ));

    let mut e = RecordBuilder::new(
        "approval/apr-77-expired",
        TENANT,
        "2026-09-20T10:01:00.000Z",
    );
    e.verdict = verdict::EXPIRED.into();
    e.system = system();
    e.disposition = policy(decision::DEFERRED);
    e.context =
        ctx(json!({"record_type": "ai.brutor.approval-expired", "approval_request_id": "apr-77"}));
    let (expired, _) = seal(&chained(e, &wait_id, relation::SUPERSEDES));
    out.push((
        "approval-expired",
        "approval lapsed: expired ⟨supersedes⟩",
        vec![wait, expired],
    ));

    let mut p = RecordBuilder::new(
        "contract/sys-claims-triage/4",
        TENANT,
        "2026-09-19T12:00:00.000Z",
    );
    p.kind = kind::FYI.into();
    p.verdict = verdict::EPOCH_BOUNDARY.into();
    p.system = system();
    p.epoch = Some(format!("contract:{}", h("contract-v4")));
    p.disposition = policy(decision::ACCEPT);
    p.context = ctx(json!({"record_type": "ai.brutor.epoch-boundary", "contract_version": "4"}));
    p.references = Some(vec![Reference {
        kind: "ai.brutor.ai-system-contract".into(),
        digest_alg: "SHA-256".into(),
        digest: h("contract-v4"),
        purpose: None,
    }]);
    out.push((
        "epoch-boundary",
        "contract promotion: fyi epoch_boundary",
        vec![seal(&p).0],
    ));

    let mut decl = RecordBuilder::new("declaration/profile-1", TENANT, "2026-09-19T12:30:00.000Z");
    decl.kind = kind::FYI.into();
    decl.verdict = verdict::RECORDED.into();
    decl.system = system();
    decl.disposition = policy(decision::ACCEPT);
    decl.context = ctx(
        json!({"record_type": "ai.brutor.declaration", "framework_id": "eu-ai-act", "catalog_version": "2026.09"}),
    );
    decl.references = Some(vec![Reference {
        kind: "ai.brutor.notice-text".into(),
        digest_alg: "SHA-256".into(),
        digest: h("notice"),
        purpose: Some("declared_text".into()),
    }]);
    out.push((
        "declaration-recorded",
        "fyi declaration: unregistered verdict, reference purpose kept in the extension",
        vec![seal(&decl).0],
    ));

    let mut empty = RecordBuilder::new("report/r-1", TENANT, "2026-09-19T13:00:00.000Z");
    empty.kind = kind::FYI.into();
    empty.verdict = verdict::RECORDED.into();
    empty.disposition = policy(decision::ACCEPT);
    empty.context = ctx(json!({"record_type": "ai.brutor.report", "report_id": "r-1"}));
    empty.references = Some(vec![]);
    out.push((
        "report-empty-references",
        "references: [] is committed (tri-state preserved)",
        vec![seal(&empty).0],
    ));

    // Judged rows: turn → assessment ⟨assesses⟩ → adjudication ⟨adjudicates⟩.
    let mut t = gateway_row(
        "llm",
        "pl-0010",
        "2026-09-19T14:00:00.000Z",
        verdict::EXECUTED,
    );
    t.disposition = policy(decision::ACCEPT);
    t.effect = effect(
        "inference_completion",
        effect_status::CONFIRMED,
        Some("pl-0010-req"),
        Some("pl-0010-resp"),
    );
    let (turn, turn_id) = seal(&t);
    let mut a = RecordBuilder::new("assessment/as-1", TENANT, "2026-09-19T15:00:00.000Z");
    a.kind = kind::FYI.into();
    a.verdict = verdict::ASSESSMENT.into();
    a.system = system();
    a.disposition = policy(decision::ACCEPT);
    a.context = ctx(
        json!({"record_type": "ai.brutor.assessment", "check_id": "groundedness", "evaluator_fingerprint": h("judge")}),
    );
    a.controls = vec![Control {
        id: "ai.brutor.judge.groundedness".into(),
        result: "pass".into(),
        blocking: false,
        severity: None,
        kind: Some("judged".into()),
        evidence_digest: Some(h("rubric-score")),
    }];
    let (assessment, assessment_id) = seal(&chained(a, &turn_id, relation::ASSESSES));
    let mut j = RecordBuilder::new("adjudication/ad-1", TENANT, "2026-09-19T16:00:00.000Z");
    j.kind = kind::FYI.into();
    j.verdict = verdict::ADJUDICATION.into();
    j.system = system();
    j.disposition = human(decision::ACCEPT, "grader-pool-1");
    j.context = ctx(json!({"record_type": "ai.brutor.adjudication", "check_id": "groundedness"}));
    let (adjudication, _) = seal(&chained(j, &assessment_id, relation::ADJUDICATES));
    out.push((
        "judged-lineage",
        "executed turn → assessment ⟨assesses⟩ → adjudication ⟨adjudicates⟩",
        vec![turn, assessment, adjudication],
    ));

    out
}

fn generate() -> Value {
    let key = SigningKey::from_bytes(&TEST_SEED);
    let cases: Vec<Value> = fixtures()
        .into_iter()
        .map(|(name, description, lineage)| {
            let mapped = map_lineage(&lineage).unwrap_or_else(|e| panic!("{name}: {e}"));
            let expected: Vec<Value> = mapped
                .iter()
                .map(|m| {
                    let env = sign_producer_envelope(&m.capsule_id, &key).unwrap();
                    json!({
                        "record_id": m.record_id,
                        "capsule_id": m.capsule_id,
                        "lossy": m.lossy,
                        "envelope_hex": hex::encode(env),
                        "capsule": m.capsule,
                    })
                })
                .collect();
            json!({"name": name, "description": description, "lineage": lineage, "expected": expected})
        })
        .collect();
    json!({
        "mapping": MAPPING_ID,
        "spec": SPEC_VERSION,
        "record_schema": brutor_evidence::SCHEMA,
        "test_key_seed_hex": hex::encode(TEST_SEED),
        "test_public_key_hex": hex::encode(key.verifying_key().as_bytes()),
        "count": cases.len(),
        "cases": cases,
    })
}

fn write_interlock(v: &Value) {
    let out = dir().join("interlock");
    fs::create_dir_all(&out).unwrap();
    for case in v["cases"].as_array().unwrap() {
        for (i, e) in case["expected"].as_array().unwrap().iter().enumerate() {
            let stem = format!("{}-{i}", case["name"].as_str().unwrap());
            fs::write(
                out.join(format!("{stem}.capsule.json")),
                serde_json::to_string_pretty(&e["capsule"]).unwrap() + "\n",
            )
            .unwrap();
            fs::write(
                out.join(format!("{stem}.capsule_id.txt")),
                e["capsule_id"].as_str().unwrap(),
            )
            .unwrap();
            fs::write(
                out.join(format!("{stem}.envelope.cose")),
                hex::decode(e["envelope_hex"].as_str().unwrap()).unwrap(),
            )
            .unwrap();
        }
    }
}

#[test]
fn mapping_vectors_are_frozen_and_verify() {
    let generated = generate();
    let path = dir().join("vectors.json");
    if std::env::var("BRUTOR_AAC_REGEN").as_deref() == Ok("1") {
        fs::create_dir_all(dir()).unwrap();
        fs::write(
            &path,
            serde_json::to_string_pretty(&generated).unwrap() + "\n",
        )
        .unwrap();
        write_interlock(&generated);
    }
    let frozen: Value =
        serde_json::from_str(&fs::read_to_string(&path).expect("run with BRUTOR_AAC_REGEN=1 once"))
            .unwrap();
    assert_eq!(
        generated, frozen,
        "mapping output drifted from the frozen vectors"
    );

    let pk = frozen["test_public_key_hex"].as_str().unwrap();
    let mut n = 0;
    for case in frozen["cases"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let expected = case["expected"].as_array().unwrap();
        let capsules: Vec<Value> = expected.iter().map(|e| e["capsule"].clone()).collect();
        for (r, e) in verify_store(&capsules).iter().zip(expected) {
            assert!(r.ok, "{name}: {:?}", r.findings);
            assert_eq!(r.capsule_id.as_deref(), e["capsule_id"].as_str(), "{name}");
            // Only informational findings (unregistered namespaced values).
            assert!(
                r.findings.iter().all(|f| f.severity == Severity::Info),
                "{name}: {:?}",
                r.findings
            );
            let env = hex::decode(e["envelope_hex"].as_str().unwrap()).unwrap();
            let v = verify_producer_envelope(e["capsule_id"].as_str().unwrap(), &env);
            assert!(
                v.ok && v.public_key_hex.as_deref() == Some(pk),
                "{name}: {v:?}"
            );
            // The extension points back to the exact source record.
            assert_eq!(e["capsule"]["ai.brutor"]["record_id"], e["record_id"]);
            n += 1;
        }
        // Frozen interlock files match too.
        for (i, e) in expected.iter().enumerate() {
            let stem = dir().join("interlock").join(format!("{name}-{i}"));
            let c: Value = serde_json::from_str(
                &fs::read_to_string(stem.with_extension("capsule.json")).unwrap(),
            )
            .unwrap();
            assert_eq!(c, e["capsule"], "{name}-{i}");
        }
    }
    assert!(n >= 17, "{n}");
}

#[test]
fn mapping_semantics_spot_checks() {
    let v = generate();
    let case = |n: &str| {
        v["cases"]
            .as_array()
            .unwrap()
            .iter()
            .find(|c| c["name"] == n)
            .unwrap()
            .clone()
    };
    let c = &case("mcp-executed-confirmed")["expected"][0]["capsule"];
    assert_eq!(
        c["developer"],
        "urn:brutor:ai-system:sys-claims-triage@contract-v3"
    );
    assert_eq!(c["effect"]["effect_attestation"], "gate_executed");
    assert_eq!(c["assurance"]["effect_mode"], "confirmed");
    assert_eq!(
        c["compliance"]["framework_tags"],
        json!(["hipaa.phi", "soc2.cc7.2"])
    );
    assert_eq!(c["constraints"][0]["check_type"], "ai.brutor.guardrail");
    assert!(c["ai.brutor"]["context"].get("tags").is_none());
    assert!(
        c["model_attestation"]["compute_attestation"]
            .get("agent_input_digest")
            .is_none()
    );

    let g = &case("approval-granted")["expected"];
    assert_eq!(
        g[0]["capsule"]["disposition"]["verdict_class"],
        "hitl_dispatched"
    );
    assert!(
        g[0]["capsule"]["effect"]
            .get("effect_attestation")
            .is_none()
    );
    assert_eq!(g[1]["capsule"]["chain"]["relation"], "supersedes");
    assert_eq!(
        g[1]["capsule"]["chain"]["parent_capsule_id"],
        g[0]["capsule_id"]
    );
    assert_eq!(g[1]["capsule"]["disposition"]["human_disposed"], true);

    let j = &case("judged-lineage")["expected"];
    assert_eq!(j[1]["capsule"]["chain"]["relation"], "ai.brutor.assesses");
    assert_eq!(
        j[2]["capsule"]["disposition"]["verdict_class"],
        "ai.brutor.adjudication"
    );
    let lossy = j[2]["lossy"].as_array().unwrap();
    assert!(
        lossy.contains(&json!("verdict_unregistered"))
            && lossy.contains(&json!("chain_relation_unregistered"))
    );

    assert_eq!(
        case("ungoverned-denied")["expected"][0]["capsule"]["developer"],
        "urn:brutor:ungoverned"
    );
    assert_eq!(
        case("report-empty-references")["expected"][0]["capsule"]["references"],
        json!([])
    );
    let d = &case("declaration-recorded")["expected"][0]["capsule"];
    assert_eq!(
        d["ai.brutor"]["reference_purposes"],
        json!(["declared_text"])
    );
    assert!(d["references"][0].get("citation_purpose").is_none());
}

fn python_verify(upstream: &Path, capsule: &Path) -> Result<String, String> {
    let out = Command::new("python3")
        .arg(upstream.join("python/scripts/cross_language.py"))
        .arg("verify")
        .env("PYTHONPATH", upstream.join("python"))
        .stdin(Stdio::from(fs::File::open(capsule).unwrap()))
        .output()
        .map_err(|e| e.to_string())?;
    if out.status.success() {
        Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
    } else {
        Err(String::from_utf8_lossy(&out.stderr).into_owned())
    }
}

/// Interlock as producer `rust`: the upstream Python reference verifier
/// must accept every Capsule we map and print the same Capsule ID; ours
/// must accept what the Python producer seals.
#[test]
#[ignore = "needs python3 and an agent-action-capsule checkout (AAC_UPSTREAM)"]
fn interlock_python_reference_verifier() {
    let upstream = PathBuf::from(
        std::env::var("AAC_UPSTREAM")
            .expect("set AAC_UPSTREAM to a checkout of agent-action-capsule"),
    );
    let mut n = 0;
    for entry in fs::read_dir(dir().join("interlock")).unwrap() {
        let p = entry.unwrap().path();
        let Some(name) = p.file_name().and_then(|s| s.to_str()) else {
            continue;
        };
        let Some(stem) = name.strip_suffix(".capsule.json") else {
            continue;
        };
        let want = fs::read_to_string(
            dir()
                .join("interlock")
                .join(format!("{stem}.capsule_id.txt")),
        )
        .unwrap();
        let got =
            python_verify(&upstream, &p).unwrap_or_else(|e| panic!("python rejected {stem}: {e}"));
        assert_eq!(got, want.trim(), "{stem}");
        n += 1;
    }
    assert!(n >= 17, "{n}");

    // python-sealed → rust-verified.
    let seal = Command::new("python3")
        .arg(upstream.join("python/scripts/cross_language.py"))
        .arg("seal")
        .env("PYTHONPATH", upstream.join("python"))
        .stdin(Stdio::from(
            fs::File::open(upstream.join("vectors/cross-language/seal-input.json")).unwrap(),
        ))
        .output()
        .unwrap();
    assert!(seal.status.success());
    let sealed: Value = serde_json::from_slice(&seal.stdout).unwrap();
    let r = brutor_aac::verify(&sealed, None);
    assert!(
        r.ok && r.capsule_id.as_deref() == sealed["capsule_id"].as_str(),
        "{r:?}"
    );
}
