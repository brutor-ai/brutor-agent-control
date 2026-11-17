// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Upstream conformance corpora (vendored under `vectors/`, see its
//! README): every case must reproduce the reference verifiers' result.

use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

use brutor_aac::bundle::{self, BundleInput};
use brutor_aac::disclosure;
use brutor_aac::envelope::verify_producer_envelope;
use brutor_aac::import::{self, Authorship, LedgerEntry};
use brutor_aac::registry;
use brutor_aac::verify::{VerificationResult, verify, verify_store};
use brutor_aac::{canon, compute_capsule_id};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("vectors")
}

fn read_json(p: &Path) -> Value {
    let raw = fs::read_to_string(p).unwrap_or_else(|e| panic!("{}: {e}", p.display()));
    serde_json::from_str(&raw).unwrap_or_else(|e| panic!("{}: {e}", p.display()))
}

fn cases(corpus: &str) -> Vec<Value> {
    let m = read_json(&root().join(corpus).join("vectors.json"));
    let cases = m["cases"].as_array().unwrap().clone();
    assert_eq!(
        m["count"].as_u64().unwrap() as usize,
        cases.len(),
        "{corpus} count"
    );
    cases
}

fn finding_keys(v: &Value) -> Vec<String> {
    v.as_array()
        .map(|a| {
            a.iter()
                .map(|f| {
                    let check = f["check"]
                        .as_u64()
                        .map(|c| c.to_string())
                        .unwrap_or("null".into());
                    format!(
                        "({check},{},{})",
                        f["severity"].as_str().unwrap(),
                        f["code"].as_str().unwrap()
                    )
                })
                .collect()
        })
        .unwrap_or_default()
}

fn actual_keys(r: &VerificationResult) -> Vec<String> {
    r.findings
        .iter()
        .map(|f| {
            let check = f.check.map(|c| c.to_string()).unwrap_or("null".into());
            format!("({check},{},{})", f.severity.as_str(), f.code)
        })
        .collect()
}

fn assert_result(name: &str, actual: &VerificationResult, expected: &Value) {
    assert_eq!(
        actual.ok,
        expected["ok"].as_bool().unwrap(),
        "{name}: ok ({:?})",
        actual.findings
    );
    assert_eq!(
        actual.capsule_id.as_deref(),
        expected["capsule_id_recomputed"].as_str(),
        "{name}: capsule_id_recomputed"
    );
    let derived = serde_json::to_value(actual.derived.as_ref().unwrap()).unwrap();
    assert_eq!(derived, expected["derived"], "{name}: derived");
    assert_eq!(
        actual_keys(actual),
        finding_keys(&expected["findings"]),
        "{name}: findings"
    );
}

#[test]
fn vendored_bytes_match_upstream_checksums() {
    let mut checked = 0;
    for corpus in [
        "capsule",
        "producer-envelope",
        "disclosure-envelope",
        "bundle",
    ] {
        let dir = root().join(corpus);
        let sums = fs::read_to_string(dir.join("SHA256SUMS")).unwrap();
        for line in sums.lines().filter(|l| !l.trim().is_empty()) {
            let (hash, file) = line.split_once("  ").unwrap();
            let bytes = fs::read(dir.join(file)).unwrap();
            assert_eq!(hex::encode(Sha256::digest(&bytes)), hash, "{corpus}/{file}");
            checked += 1;
        }
    }
    assert!(checked > 200, "{checked}");
}

#[test]
fn capsule_corpus_class1() {
    let (mut pos, mut neg, mut other) = (0, 0, 0);
    for case in cases("capsule") {
        let name = case["name"].as_str().unwrap();
        let dir = root().join("capsule").join(name);
        let input = read_json(&dir.join("input.json"));
        let expected = read_json(&dir.join("expected.json"));
        if let Some(ledger) = input.get("ledger").and_then(Value::as_array) {
            let results = verify_store(ledger);
            let exp = expected["results"].as_array().unwrap();
            assert_eq!(results.len(), exp.len(), "{name}");
            for (i, (a, e)) in results.iter().zip(exp).enumerate() {
                assert_result(&format!("{name}[{i}]"), a, e);
            }
        } else if let Some(exc) = expected.get("exception").and_then(Value::as_str) {
            let err = compute_capsule_id(&input).unwrap_err();
            let got = match err {
                canon::CapsuleIdError::Float(_) => "FloatInDigestError",
                canon::CapsuleIdError::UnsafeInteger(_) => "UnsafeIntegerError",
                _ => "other",
            };
            assert_eq!(got, exc, "{name}");
        } else {
            let r = verify(&input, None);
            assert_result(name, &r, &expected);
            // Every positive vector's capsule id reproduces.
            if r.ok {
                assert_eq!(
                    r.capsule_id.as_deref(),
                    input["capsule_id"].as_str(),
                    "{name}"
                );
            }
        }
        match case["kind"].as_str().unwrap() {
            "positive" => pos += 1,
            "negative" => neg += 1,
            _ => other += 1,
        }
    }
    assert_eq!((pos, neg, other), (49, 35, 5));
}

#[test]
fn capsule_corpus_spec_derived_preimages_are_our_jcs() {
    // Spec-derived cases record the literal RFC 8785 string; our JCS must
    // produce it byte for byte.
    let mut n = 0;
    for case in cases("capsule") {
        let name = case["name"].as_str().unwrap();
        let dir = root().join("capsule").join(name);
        let expected = read_json(&dir.join("expected.json"));
        let Some(pre) = expected
            .get("canonical_preimages")
            .and_then(Value::as_object)
        else {
            continue;
        };
        let input = read_json(&dir.join("input.json"));
        for (k, s) in pre {
            let target = if k == "capsule_id" {
                input.clone()
            } else if let Some(i) = k
                .strip_prefix("ledger[")
                .and_then(|r| r.strip_suffix("].capsule_id"))
            {
                input["ledger"][i.parse::<usize>().unwrap()].clone()
            } else {
                continue;
            };
            let Ok(p) = canon::capsule_id_preimage(&target) else {
                continue;
            };
            let ours = String::from_utf8(canon::jcs(&p).unwrap()).unwrap();
            assert_eq!(ours, s.as_str().unwrap(), "{name}:{k}");
            n += 1;
        }
    }
    assert!(n >= 40, "{n}");
}

#[test]
fn producer_envelope_corpus() {
    let mut n = 0;
    for case in cases("producer-envelope") {
        let name = case["name"].as_str().unwrap();
        let dir = root().join("producer-envelope").join(name);
        let id = fs::read_to_string(dir.join("capsule_id.txt")).unwrap();
        let raw = fs::read(dir.join("envelope.cose")).unwrap();
        let expected = read_json(&dir.join("expected.json"));
        let r = verify_producer_envelope(id.trim(), &raw);
        assert_eq!(
            r.ok,
            expected["ok"].as_bool().unwrap(),
            "{name}: {:?}",
            r.findings
        );
        let codes: Vec<&str> = r.findings.iter().map(|f| f.code.as_str()).collect();
        let exp: Vec<&str> = expected["finding_codes"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| c.as_str().unwrap())
            .collect();
        assert_eq!(codes, exp, "{name}");
        assert_eq!(
            r.public_key_hex.as_deref(),
            expected["public_key_hex"].as_str(),
            "{name}"
        );
        n += 1;
    }
    assert_eq!(n, 8);
}

#[test]
fn disclosure_envelope_corpus() {
    let mut n = 0;
    for case in cases("disclosure-envelope") {
        let name = case["name"].as_str().unwrap();
        let dir = root().join("disclosure-envelope").join(name);
        let input = read_json(&dir.join("input.json"));
        let expected = read_json(&dir.join("expected.json"));
        let envelope = input.get("envelope").cloned().unwrap_or(input);
        let r = disclosure::verify_disclosure_envelope(&envelope);
        assert_eq!(r.ok(), expected["ok"].as_bool().unwrap(), "{name}");
        assert_result(name, &r.capsule, &expected["capsule"]);
        assert_eq!(
            r.disclosures_checked() as u64,
            expected["disclosures_checked"].as_u64().unwrap(),
            "{name}"
        );
        assert_eq!(
            r.disclosures_matched() as u64,
            expected["disclosures_matched"].as_u64().unwrap(),
            "{name}"
        );
        let got: Vec<(String, String)> = r
            .disclosure_findings
            .iter()
            .map(|f| (f.member.clone(), f.code.to_string()))
            .collect();
        let exp: Vec<(String, String)> = expected["disclosure_findings"]
            .as_array()
            .unwrap()
            .iter()
            .map(|f| {
                (
                    f["member"].as_str().unwrap().into(),
                    f["code"].as_str().unwrap().into(),
                )
            })
            .collect();
        assert_eq!(got, exp, "{name}");
        n += 1;
    }
    assert_eq!(n, 7);
}

// ---- evidence-bundle/v2 -------------------------------------------------

/// The upstream reference fixture (go/bundle/bundle_test.go `testCapsule`).
fn bundle_capsule(
    n: u32,
    output: Option<&Value>,
    input: Option<&Value>,
    chain: Option<Value>,
) -> Value {
    let mut c = json!({
        "spec_version": "draft-mih-scitt-agent-action-capsule-04",
        "format_version": "4",
        "canonicalization_id": "jcs",
        "action_id": format!("bundle-{n}"),
        "action_type": "decide",
        "operator": "ACME-CO",
        "developer": "agent@v1",
        "timestamp": format!("2026-09-14T00:00:0{n}Z"),
        "assurance": {"effect_mode": "not_applicable", "attestation_mode": "self_attested", "ledger_mode": "standalone"},
        "disposition": {"verdict_class": "blocked", "decision": "reject", "approver": "policy", "human_disposed": false},
    });
    if let Some(o) = output {
        c["model_attestation"] =
            json!({"compute_attestation": {"agent_output_digest": canon::json_digest(o).unwrap()}});
    }
    if let Some(i) = input {
        c["model_attestation"] =
            json!({"compute_attestation": {"agent_input_digest": canon::json_digest(i).unwrap()}});
    }
    if let Some(ch) = chain {
        c["chain"] = ch;
    }
    let id = compute_capsule_id(&c).unwrap();
    c["capsule_id"] = json!(id);
    c
}

fn raw_bundle(records: &[&Value], root: &Value, overlay: Value, missing: &[&str]) -> Value {
    json!({
        "bundle_version": "2", "bundle_kind": "evidence-bundle/v2", "root": root["capsule_id"],
        "records": records,
        "completeness": {
            "closure_depth": 2,
            "records_mode": if missing.is_empty() { "complete" } else { "declared_incomplete" },
            "payloads_mode": "selected", "suppressed_fields": ["agent_input"], "missing": missing,
        },
        "disclosures": overlay,
        "extensions": {"example/unimplemented": {"note": "digest-covered only"}},
    })
}

#[test]
fn bundle_corpus_graph_closure_and_disclosures() {
    let output = json!({"answer": {"steps": ["check", "approve"], "total": "42"}});
    let input = json!({"request": {"account": "A-17", "amount": "42.00"}});
    let first = bundle_capsule(1, Some(&output), None, None);
    let middle = bundle_capsule(2, None, Some(&input), None);
    let rootc = bundle_capsule(3, None, None, None);
    // README independent derivation of the root Capsule.
    assert_eq!(
        rootc["capsule_id"],
        "65fd43081d60ee74dbc702d1f6eb73c45e3509c20557ee9d55cbb09e0722277c"
    );
    let fid = first["capsule_id"].as_str().unwrap().to_string();
    let mid = middle["capsule_id"].as_str().unwrap().to_string();
    let overlay = json!({ fid.clone(): {"agent_output": output} });
    let absent = "f".repeat(64);
    let cited = bundle_capsule(
        4,
        None,
        None,
        Some(json!({"parent_capsule_id": absent, "relation": "derived_from"})),
    );

    let mut n = 0;
    for case in cases("bundle") {
        let name = case["name"].as_str().unwrap();
        let exp = &case["expected"];
        let b = match name {
            "neg-dangling-citation" => {
                raw_bundle(&[&first, &middle, &cited], &cited, json!({}), &[])
            }
            "pos-declared-missing-citation" => {
                raw_bundle(&[&first, &middle, &cited], &cited, json!({}), &[&absent])
            }
            "neg-disclosure-mismatch-nested-match" => {
                let mut o = overlay.clone();
                o[mid.as_str()] =
                    json!({"agent_input": {"request": {"account": "A-17", "amount": "99.00"}}});
                raw_bundle(&[&first, &middle, &rootc], &rootc, o, &[])
            }
            "pos-default-completeness-values" => {
                let mut b = raw_bundle(&[&first, &middle, &rootc], &rootc, overlay.clone(), &[]);
                let c = b["completeness"].as_object_mut().unwrap();
                c.remove("closure_depth");
                c.remove("missing");
                b
            }
            // Remaining cases differ only in CLL MMR membership material,
            // which this crate does not evaluate: their graph
            // claim is the valid bundle's.
            _ => raw_bundle(&[&first, &middle, &rootc], &rootc, overlay.clone(), &[]),
        };
        let r = bundle::verify_bundle(&b);
        assert_eq!(
            r.graph_closure.status,
            exp["graph_closure"].as_str().unwrap(),
            "{name}: {:?}",
            r.graph_closure
        );
        if let Some(d) = exp.get("disclosures").and_then(Value::as_array) {
            let mut got: Vec<&str> = r.disclosures.iter().map(|x| x.status).collect();
            let mut want: Vec<&str> = d.iter().map(|x| x.as_str().unwrap()).collect();
            got.sort_unstable();
            want.sort_unstable();
            assert_eq!(got, want, "{name}");
        }
        // Without a certificate the membership claims are withheld, never pass.
        assert_eq!(r.interval_coverage.status, "withheld", "{name}");
        assert_eq!(r.per_record_membership.status, "withheld", "{name}");
        // With one, we say we did not evaluate it.
        let mut with_cert = b.clone();
        with_cert["completeness_certificate"] = json!({"log_id": "bundle-log"});
        with_cert["checkpoint"] = json!({"root": "00"});
        let rc = bundle::verify_bundle(&with_cert);
        assert_eq!(rc.interval_coverage.status, "not_evaluated");
        assert_eq!(
            rc.per_record_membership.findings,
            vec!["cll_mmr_not_implemented"]
        );
        // Fragment round trip preserves every claim.
        let frag = bundle::encode_fragment(&b).unwrap();
        let back = bundle::decode_fragment(&frag).unwrap();
        assert_eq!(bundle::verify_bundle(&back), r, "{name}");
        n += 1;
    }
    assert_eq!(n, 12);
}

#[test]
fn bundle_fragment_codec_and_digest_match_reference() {
    // go/bundle/bundle_test.go TestFragmentCodecAndDigest.
    let b = json!({
        "b": 1, "a": "value",
        "countersignatures": [{"type": "cose-sign1", "signature": "ignored-by-digest"}],
    });
    let frag = bundle::encode_fragment(&b).unwrap();
    assert_eq!(
        frag,
        "eyJhIjoidmFsdWUiLCJiIjoxLCJjb3VudGVyc2lnbmF0dXJlcyI6W3sic2lnbmF0dXJlIjoiaWdub3JlZC1ieS1kaWdlc3QiLCJ0eXBlIjoiY29zZS1zaWduMSJ9XX0"
    );
    assert_eq!(bundle::decode_fragment(&frag).unwrap(), b);
    assert_eq!(
        bundle::bundle_digest(&b).unwrap(),
        "04a0f2c6e056b32f5659fdec506f9f6d6a0b250227004b0fd7ed43d0b77035a5"
    );
    assert!(bundle::decode_fragment("e30=").is_err()); // padded
    let r = bundle::verify_bundle(
        &json!({"countersignatures": ["reserved"], "verification": {"producer": "claimed"}}),
    );
    assert_eq!(
        r.countersignatures,
        vec![json!({"value": "reserved", "status": "unverified"})]
    );
    assert_eq!(
        r.verification_self_report,
        Some(json!({"value": {"producer": "claimed"}, "status": "producer_self_report"}))
    );
}

#[test]
fn bundle_builder_declares_closure_and_verifies() {
    let parent = bundle_capsule(1, None, None, None);
    let pid = parent["capsule_id"].as_str().unwrap().to_string();
    let child = bundle_capsule(
        2,
        None,
        None,
        Some(json!({"parent_capsule_id": pid, "relation": "confirms"})),
    );
    let cid = child["capsule_id"].as_str().unwrap().to_string();
    let key = ed25519_dalek::SigningKey::from_bytes(&[5u8; 32]);
    let env = brutor_aac::sign_producer_envelope(&cid, &key).unwrap();

    // Parent withheld → declared, not dropped.
    let mut input = BundleInput {
        root: cid.clone(),
        records: vec![child.clone()],
        ..Default::default()
    };
    input.envelopes.insert(cid.clone(), vec![env]);
    let b = bundle::build_bundle(&input).unwrap();
    assert_eq!(b["completeness"]["missing"], json!([pid]));
    assert_eq!(b["completeness"]["records_mode"], "declared_incomplete");
    let r = bundle::verify_bundle(&b);
    assert_eq!(r.graph_closure.status, "withheld");
    let ext = &r.extensions[0];
    assert_eq!(ext.status, "interpreted");
    assert!(ext.envelopes.iter().all(|e| e.ok) && ext.envelopes.len() == 1);

    // Parent supplied → complete.
    input.records.push(parent);
    let b = bundle::build_bundle(&input).unwrap();
    assert_eq!(b["completeness"]["records_mode"], "complete");
    assert_eq!(bundle::verify_bundle(&b).graph_closure.status, "pass");

    // A tampered record is refused at build time and fails verification.
    let mut bad = input.clone();
    bad.records[0]["operator"] = json!("EVIL");
    assert!(bundle::build_bundle(&bad).is_err());
    let mut tampered = b.clone();
    tampered["records"][0]["operator"] = json!("EVIL");
    assert_eq!(
        bundle::verify_bundle(&tampered).graph_closure.status,
        "fail"
    );
    assert_ne!(
        bundle::bundle_digest(&tampered).unwrap(),
        bundle::bundle_digest(&b).unwrap()
    );
}

// ---- registries: drift guard against the vendored registry of record ----

fn registry_md_sections() -> Vec<(String, BTreeSet<String>)> {
    let md = fs::read_to_string(root().join("registry/REGISTRY.md")).unwrap();
    let mut out: Vec<(String, BTreeSet<String>)> = Vec::new();
    let mut in_initial = false;
    for line in md.lines() {
        let t = line.trim();
        if let Some(rest) = t.strip_prefix("## ") {
            in_initial = false;
            let name = rest
                .split('`')
                .nth(1)
                .filter(|_| rest.chars().next().is_some_and(|c| c.is_ascii_digit()));
            out.push((name.unwrap_or("").to_string(), BTreeSet::new()));
            continue;
        }
        let Some((_, vals)) = out.last_mut() else {
            continue;
        };
        let ticks = |s: &str| {
            s.split('`')
                .skip(1)
                .step_by(2)
                .map(str::to_owned)
                .collect::<Vec<_>>()
        };
        if t.is_empty() {
            in_initial = false;
        } else if t.starts_with('|') {
            let first = t.trim_matches('|').split('|').next().unwrap_or("").trim();
            if first.len() > 2
                && first.starts_with('`')
                && first.ends_with('`')
                && first[1..first.len() - 1].find('`').is_none()
            {
                vals.insert(first[1..first.len() - 1].to_string());
            }
        } else if t.contains("Initial contents") {
            in_initial = true;
            let idx = t.find("Initial contents").unwrap();
            vals.extend(ticks(&t[idx..]));
        } else if in_initial {
            vals.extend(ticks(t));
        } else if let Some((num, rest)) = t.split_once(". ")
            && num.chars().all(|c| c.is_ascii_digit())
            && rest.starts_with('`')
            && rest.ends_with('`')
        {
            vals.insert(rest.trim_matches('`').to_string());
        }
    }
    out
}

#[test]
fn seeded_registries_match_vendored_registry_md() {
    let sections = registry_md_sections();
    for (name, seeded) in registry::REGISTRIES {
        let Some((_, vals)) = sections.iter().find(|(n, _)| n == name) else {
            // Older registry snapshots predate citation_purpose.
            assert_eq!(*name, "citation_purpose");
            continue;
        };
        let ours: BTreeSet<String> = seeded.iter().map(|s| s.to_string()).collect();
        assert_eq!(&ours, vals, "registry {name}");
    }
}

#[test]
fn provisional_values_match_vendored_snapshot() {
    let snap = read_json(&root().join("registry/cpb_provisional.json"));
    let mut got: BTreeSet<(String, String, String)> = BTreeSet::new();
    for (class, t) in snap["provisional_artifact_types"].as_object().unwrap() {
        if let Some(vals) = t.get("capsule_field_values").and_then(Value::as_object) {
            for (reg, list) in vals {
                for v in list.as_array().unwrap() {
                    got.insert((reg.clone(), v.as_str().unwrap().into(), class.clone()));
                }
            }
        }
    }
    let ours: BTreeSet<(String, String, String)> = registry::PROVISIONAL
        .iter()
        .map(|(a, b, c)| (a.to_string(), b.to_string(), c.to_string()))
        .collect();
    assert_eq!(ours, got);
}

// ---- cross-language + capsule-emit interop -------------------------------

#[test]
fn cross_language_seal_input_reproduces_reference_id() {
    // The id the Python/Go/TS producers all emit for this fixture
    // (`scripts/cross-language-interlock.sh`).
    let input = read_json(&root().join("cross-language/seal-input.json"));
    assert_eq!(
        compute_capsule_id(&input).unwrap(),
        "41d09edd772e6031975e790f62788070002aa7fa2fa642f505c6723baebb9950"
    );
    let mut sealed = input.clone();
    sealed["capsule_id"] = json!(compute_capsule_id(&input).unwrap());
    let r = verify(&sealed, None);
    assert!(r.ok, "{:?}", r.findings);
}

#[test]
fn capsule_emit_ledger_imports_as_received() {
    let jsonl = fs::read_to_string(root().join("capsule-emit/capsule_emit_ledger.jsonl")).unwrap();
    let entries = import::parse_capsule_emit_ledger(&jsonl);
    assert_eq!(entries.len(), 3);
    let mut chained = 0;
    for e in &entries {
        let LedgerEntry::Capsule(c) = e else {
            panic!("unexpected {e:?}")
        };
        assert_eq!(c.status, "received");
        assert!(c.class1.ok, "{:?}", c.class1.findings);
        assert_eq!(
            c.class1.capsule_id, c.capsule_id,
            "capsule-emit id reproduces"
        );
        let Authorship::Authored { public_key_hex } = &c.authorship else {
            panic!("{:?}", c.authorship)
        };
        assert_eq!(Some(public_key_hex.as_str()), c.capsule["key_id"].as_str());
        assert!(c.integrity_ok);
        if c.parent_capsule_id.is_some() {
            chained += 1;
        }
    }
    assert!(chained >= 1);

    // Tamper after signing: identity and authorship both fail, loudly.
    let mut lines: Vec<Value> = jsonl
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    lines[0]["operator"] = json!("someone-else");
    let r = import::receive(lines[0].clone());
    assert!(!r.class1.ok);
    assert!(!r.integrity_ok);
    // Removing the claim is "unclaimed", never "authored".
    let mut unsigned: Map<String, Value> = lines[1].as_object().unwrap().clone();
    unsigned.remove("signature");
    unsigned.remove("key_id");
    assert_eq!(
        import::receive(Value::Object(unsigned.clone())).authorship,
        Authorship::Unclaimed
    );
    unsigned.insert("key_id".into(), json!("00"));
    assert!(matches!(
        import::receive(Value::Object(unsigned)).authorship,
        Authorship::Invalid { .. }
    ));
    // Non-capsule bookkeeping lines are recognised, not verified.
    let with_stamp = format!("{jsonl}\n{{\"kind\":\"checkpoint_stamp\",\"seq\":4}}\nnot json\n");
    let e = import::parse_capsule_emit_ledger(&with_stamp);
    assert!(
        matches!(&e[3], LedgerEntry::NonCapsule { seq: 4, kind } if kind == "checkpoint_stamp")
    );
    assert!(matches!(&e[4], LedgerEntry::Unparseable { seq: 5, .. }));
}
