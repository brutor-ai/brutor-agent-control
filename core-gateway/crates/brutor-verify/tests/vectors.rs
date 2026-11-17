// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The vector corpus as a regression suite.
//!
//! * `vectors_are_up_to_date` — regenerating the corpus reproduces the
//!   committed files byte for byte (and the control plane's parity fixture
//!   when that checkout is present). Fix: `cargo run -p brutor-verify
//!   --example gen-vectors`, then review the diff — a changed vector is a
//!   changed contract.
//! * `every_*` — each committed vector, read from disk, meets its declared
//!   expectation. These read only the files, as a third-party implementation
//!   would.
//! * `cli_*` — the binary's exit codes and neutral wording.

mod corpus;
#[path = "../../../tests/support/roots.rs"]
mod roots;

use brutor_verify::{BundleOptions, TrustedKeys};
use serde_json::Value;
use std::path::{Path, PathBuf};
use std::process::Command;

fn vectors() -> PathBuf {
    corpus::core_root().join("vectors")
}

fn read(p: &Path) -> Value {
    let t = std::fs::read_to_string(p).unwrap_or_else(|e| panic!("{}: {e}", p.display()));
    serde_json::from_str(&t).unwrap_or_else(|e| panic!("{}: {e}", p.display()))
}

fn json_files(dir: &Path) -> Vec<PathBuf> {
    let mut v: Vec<PathBuf> = std::fs::read_dir(dir)
        .unwrap_or_else(|e| panic!("{}: {e}", dir.display()))
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    v.sort();
    v
}

fn keys_doc(rel: Option<&str>) -> TrustedKeys {
    match rel {
        None => TrustedKeys::unpinned(),
        Some(r) => {
            let mut t = TrustedKeys::unpinned();
            t.add_well_known(&read(&vectors().join(r))).unwrap();
            t
        }
    }
}

fn b64(s: &str) -> Vec<u8> {
    use base64::Engine as _;
    base64::engine::general_purpose::STANDARD.decode(s).unwrap()
}

fn codes(v: &Value) -> Vec<String> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|c| c.as_str().unwrap().to_owned())
        .collect()
}

fn walk(d: &Path, out: &mut Vec<PathBuf>) {
    for e in std::fs::read_dir(d).unwrap().flatten() {
        let p = e.path();
        if p.is_dir() {
            walk(&p, out);
        } else {
            out.push(p);
        }
    }
}

#[test]
fn vectors_are_up_to_date() {
    let c = corpus::generate();
    let root = corpus::core_root();
    let mut stale = Vec::new();
    for (rel, want) in &c.files {
        match std::fs::read_to_string(root.join(rel)) {
            Ok(have) if &have == want => {}
            Ok(_) => stale.push(format!("differs: {rel}")),
            Err(_) => stale.push(format!("missing: {rel}")),
        }
    }
    let mut on_disk = Vec::new();
    walk(&vectors(), &mut on_disk);
    for p in on_disk {
        let rel = p
            .strip_prefix(&root)
            .unwrap()
            .to_string_lossy()
            .replace('\\', "/");
        let hand_written = rel == "vectors/README.md" || rel.starts_with("vectors/interop/");
        if !hand_written && !c.files.contains_key(&rel) {
            stale.push(format!("not generated: {rel}"));
        }
    }
    // The control plane's parity fixture, in the control plane's tree as
    // `tests/support/roots.rs` of the gateway crate finds it. Not there: a
    // failure, unless the run said sibling repositories are optional, and
    // then a skip that is recorded.
    if let Some(control) = roots::tree("control-plane") {
        let cp = control.join(corpus::CP_FIXTURE);
        match std::fs::read_to_string(&cp) {
            Ok(have) if have == c.cp_fixture => {}
            Ok(_) => stale.push(format!("differs: {}", cp.display())),
            Err(_) => stale.push(format!("missing: {}", cp.display())),
        }
    }
    assert!(
        stale.is_empty(),
        "the vector corpus is out of date — run `{}` and review the diff:\n  {}",
        corpus::GENERATOR,
        stale.join("\n  ")
    );
}

#[test]
fn every_record_vector_meets_its_expectation() {
    let keys = keys_doc(Some("keys/well-known.json"));
    let mut n = 0;
    for dir in ["positive", "negative"] {
        for f in json_files(&vectors().join("records").join(dir)) {
            let v = read(&f);
            let st = v.get("statement_b64").and_then(Value::as_str).map(b64);
            let store: Option<Vec<Value>> = v
                .get("store")
                .map(|s| serde_json::from_value(s.clone()).unwrap());
            let rep =
                brutor_verify::verify_record(&v["record"], st.as_deref(), store.as_deref(), &keys);
            let name = f.display();
            assert_eq!(
                rep.ok,
                v["expect"]["ok"].as_bool().unwrap(),
                "{name}: {:#?}",
                rep.findings
            );
            assert_eq!(rep.ok, dir == "positive", "{name} is filed under {dir}");
            assert_eq!(rep.codes(), codes(&v["expect"]["codes"]), "{name}");
            assert_eq!(rep.facts["record_id"], v["expect"]["record_id"], "{name}");
            assert_eq!(
                rep.facts["effect_mode"], v["expect"]["effect_mode"],
                "{name}"
            );
            if let Some(c) = v.get("canonical").and_then(Value::as_str) {
                let mut r = v["record"].clone();
                r.as_object_mut().unwrap().remove("record_id");
                let bytes = brutor_evidence::canonical_bytes(&r).unwrap();
                assert_eq!(std::str::from_utf8(&bytes).unwrap(), c, "{name}: canonical");
                assert_eq!(
                    Some(hex::encode(<sha2::Sha256 as sha2::Digest>::digest(&bytes))),
                    rep.facts["record_id"].as_str().map(str::to_owned),
                    "{name}: record_id = SHA-256(canonical)"
                );
            }
            for (path, spec) in v["preimages"].as_object().unwrap() {
                let carried = corpus::at(&v["record"], path)
                    .and_then(Value::as_str)
                    .unwrap();
                assert_eq!(
                    corpus::preimage_digest(&v["record"], spec),
                    carried,
                    "{name}: {path}"
                );
                if let Some(c) = spec.get("canonical").and_then(Value::as_str) {
                    let bytes = brutor_evidence::canonical_bytes(&spec["value"]).unwrap();
                    assert_eq!(
                        std::str::from_utf8(&bytes).unwrap(),
                        c,
                        "{name}: {path} canonical"
                    );
                }
                if let Some(body) = spec.get("body").and_then(Value::as_str) {
                    assert_eq!(
                        brutor_evidence::canon::body_value(body).0,
                        spec["value"],
                        "{name}: {path} body"
                    );
                }
            }
            n += 1;
        }
    }
    assert!(n >= 40, "only {n} record vectors");
}

#[test]
fn every_verdict_effect_status_and_relation_is_covered() {
    let mut verdicts = std::collections::BTreeSet::new();
    let mut statuses = std::collections::BTreeSet::new();
    let mut relations = std::collections::BTreeSet::new();
    for f in json_files(&vectors().join("records/positive")) {
        let r = &read(&f)["record"];
        verdicts.insert(r["verdict"].as_str().unwrap_or("").to_owned());
        if let Some(s) = r.get("effect").and_then(|e| e["status"].as_str()) {
            statuses.insert(s.to_owned());
        }
        if let Some(s) = r.get("chain").and_then(|c| c["relation"].as_str()) {
            relations.insert(s.to_owned());
        }
    }
    for v in brutor_evidence::record::verdict::ALL {
        assert!(verdicts.contains(*v), "no positive vector for verdict {v}");
    }
    for s in brutor_evidence::record::effect_status::ALL {
        assert!(
            statuses.contains(*s),
            "no positive vector for effect status {s}"
        );
    }
    for r in brutor_evidence::record::relation::ALL {
        assert!(
            relations.contains(*r),
            "no positive vector for relation {r}"
        );
    }
}

#[test]
fn every_bundle_vector_meets_its_expectation() {
    let index = read(&vectors().join("bundles/expect.json"));
    let cases = index["cases"].as_array().unwrap();
    for c in cases {
        let file = c["file"].as_str().unwrap();
        let bundle = read(&vectors().join("bundles").join(file));
        let opts = BundleOptions {
            keys: keys_doc(c["keys"].as_str()),
            expected_digest: c["expected_digest"].as_str().map(str::to_owned),
            witness_keys: codes(&c["witness_keys"]),
            tsa_pins: vec![],
        };
        let rep = brutor_verify::verify_bundle(&bundle, &opts);
        let e = &c["expect"];
        assert_eq!(
            rep.ok,
            e["ok"].as_bool().unwrap(),
            "{file}: {:#?}",
            rep.findings
        );
        assert_eq!(
            rep.codes(),
            codes(&e["codes"]),
            "{file} ({})",
            c["description"]
        );
        for k in [
            "bundle_digest",
            "records_verified",
            "proofs_verified",
            "heads_verified",
        ] {
            assert_eq!(rep.facts[k], e[k], "{file}: {k}");
        }
    }
    // Every tampered file is exercised and fails.
    for f in json_files(&vectors().join("bundles/tampered")) {
        let name = format!("tampered/{}", f.file_name().unwrap().to_string_lossy());
        assert!(
            cases
                .iter()
                .any(|c| c["file"] == name.as_str() && c["expect"]["ok"] == false),
            "{name} has no failing expectation"
        );
    }
}

#[test]
fn every_head_and_receipt_vector_meets_its_expectation() {
    for f in json_files(&vectors().join("heads")) {
        let v = read(&f);
        let prev = v["previous_b64"].as_str().map(b64);
        let cons: Option<Vec<String>> = v["consistency_proof"]
            .as_array()
            .map(|a| a.iter().map(|x| x.as_str().unwrap().to_owned()).collect());
        let rep = brutor_verify::verify_head(
            &b64(v["statement_b64"].as_str().unwrap()),
            prev.as_deref(),
            cons.as_deref(),
            &keys_doc(v["keys"].as_str()),
        );
        let e = &v["expect"];
        assert_eq!(
            rep.ok,
            e["ok"].as_bool().unwrap(),
            "{}: {:#?}",
            f.display(),
            rep.findings
        );
        assert_eq!(rep.codes(), codes(&e["codes"]), "{}", f.display());
        assert_eq!(
            rep.facts
                .get("consistent_with_previous")
                .cloned()
                .unwrap_or(Value::Null),
            e["consistent_with_previous"],
            "{}",
            f.display()
        );
    }
    for f in json_files(&vectors().join("receipts")) {
        let v = read(&f);
        let rep = brutor_verify::verify_receipt(
            &b64(v["receipt_b64"].as_str().unwrap()),
            &b64(v["statement_b64"].as_str().unwrap()),
            v["kind"].as_str(),
            v["pin"].as_str(),
        );
        let e = &v["expect"];
        assert_eq!(
            rep.facts["grade"],
            e["grade"],
            "{}: {:#?}",
            f.display(),
            rep.findings
        );
        assert_eq!(rep.codes(), codes(&e["codes"]), "{}", f.display());
        assert_eq!(rep.ok, e["ok"].as_bool().unwrap(), "{}", f.display());
        for k in ["tree_size", "leaf_index", "iat"] {
            assert_eq!(rep.facts[k], e[k], "{}: {k}", f.display());
        }
    }
}

#[test]
fn every_log_and_canon_vector_meets_its_expectation() {
    use brutor_cose::merkle::{Hash, leaf_hash, verify_consistency, verify_inclusion};
    let h = |s: &Value| -> Hash {
        hex::decode(s.as_str().unwrap())
            .unwrap()
            .try_into()
            .unwrap()
    };
    let path = |v: &Value| -> Vec<Hash> { v.as_array().unwrap().iter().map(h).collect() };
    let inc = read(&vectors().join("logs/inclusion.json"));
    let leaves: Vec<Hash> = inc["log"]["entries_hex"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| leaf_hash(&hex::decode(e.as_str().unwrap()).unwrap()))
        .collect();
    assert_eq!(
        leaves.iter().map(hex::encode).collect::<Vec<_>>(),
        codes(&inc["log"]["leaf_hashes"])
    );
    for c in inc["cases"]
        .as_array()
        .unwrap()
        .iter()
        .chain(inc["negative"].as_array().unwrap())
    {
        let i = c
            .get("entry_index")
            .unwrap_or(&c["index"])
            .as_u64()
            .unwrap() as usize;
        let ok = verify_inclusion(
            &leaves[i],
            c["index"].as_u64().unwrap(),
            c["tree_size"].as_u64().unwrap(),
            &path(&c["path"]),
            &h(&c["root"]),
        )
        .is_ok();
        assert_eq!(ok, c["expect"].as_bool().unwrap(), "{c}");
    }
    let cons = read(&vectors().join("logs/consistency.json"));
    for c in cons["cases"]
        .as_array()
        .unwrap()
        .iter()
        .chain(cons["negative"].as_array().unwrap())
    {
        let ok = verify_consistency(
            c["first_size"].as_u64().unwrap(),
            &h(&c["first_root"]),
            c["second_size"].as_u64().unwrap(),
            &h(&c["second_root"]),
            &path(&c["path"]),
        )
        .is_ok();
        assert_eq!(ok, c["expect"].as_bool().unwrap(), "{c}");
    }
    let nodes = read(&vectors().join("logs/store-nodes.json"));
    let mut store = brutor_ctlog::MemoryStore::default();
    for n in nodes["nodes"].as_array().unwrap() {
        brutor_ctlog::NodeStore::put(
            &mut store,
            n["level"].as_u64().unwrap() as u32,
            n["index"].as_u64().unwrap(),
            h(&n["hash"]),
        );
    }
    let size = leaves.len() as u64;
    let log = brutor_ctlog::Log::open(&mut store, size);
    for (n, root) in nodes["log"]["roots"].as_object().unwrap() {
        assert_eq!(
            hex::encode(log.root_at(n.parse().unwrap()).unwrap()),
            root.as_str().unwrap()
        );
    }
    for c in read(&vectors().join("canon/jcs.json"))["cases"]
        .as_array()
        .unwrap()
    {
        let v: Value = serde_json::from_str(c["input"].as_str().unwrap()).unwrap();
        match brutor_evidence::canonical_bytes(&v) {
            Ok(b) => assert_eq!(
                std::str::from_utf8(&b).unwrap(),
                c["canonical"].as_str().unwrap(),
                "{c}"
            ),
            Err(brutor_evidence::CanonError::Float(_)) => assert_eq!(c["error"], "float", "{c}"),
            Err(brutor_evidence::CanonError::UnsafeInteger(_)) => {
                assert_eq!(c["error"], "unsafe_integer", "{c}")
            }
            Err(e) => panic!("{e}"),
        }
    }
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

fn cli(args: &[&str]) -> (i32, String, String) {
    let out = Command::new(env!("CARGO_BIN_EXE_brutor-verify"))
        .args(args)
        .current_dir(corpus::core_root())
        .output()
        .unwrap();
    (
        out.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&out.stdout).into_owned(),
        String::from_utf8_lossy(&out.stderr).into_owned(),
    )
}

/// Words the tool never uses as a claim: it reports what each check
/// established, not a verdict about the system.
const CLAIM_WORDS: &[&str] = &[
    "verified",
    "compliant",
    "certified",
    "guaranteed",
    "tamper-proof",
    "approved",
];

fn assert_neutral(text: &str) {
    let lower = text.to_lowercase();
    for w in CLAIM_WORDS {
        assert!(!lower.contains(w), "output uses {w:?}:\n{text}");
    }
}

#[test]
fn cli_bundle_exit_codes_and_wording() {
    let witness = read(&vectors().join("keys/witness.json"))["witness_key_hex"]
        .as_str()
        .unwrap()
        .to_owned();
    let (code, out, _) = cli(&[
        "bundle",
        "vectors/bundles/golden/run-contiguous.json",
        "--keys",
        "vectors/keys/well-known.json",
        "--witness-key",
        &witness,
    ]);
    assert_eq!(code, 0, "{out}");
    assert!(
        out.contains("inclusion: 6 of 6 inclusion proofs reproduce the root"),
        "{out}"
    );
    assert!(out.contains("receipt (scitt)"), "{out}");
    assert_neutral(&out);

    let (code, out, _) = cli(&[
        "bundle",
        "vectors/bundles/tampered/swapped-statement.json",
        "--keys",
        "vectors/keys/well-known.json",
        "--json",
    ]);
    assert_eq!(code, 1);
    let rep: Value = serde_json::from_str(&out).unwrap();
    assert_eq!(rep["ok"], false);
    assert_eq!(rep["exit_code"], 1);
    assert!(
        rep["findings"]
            .as_array()
            .unwrap()
            .iter()
            .any(|f| f["code"] == "statement_payload_mismatch")
    );
    // JSON keys mirror the gateway's verify API (`records_verified`, …);
    // the neutral-wording rule covers the prose a person reads.
    for c in rep["checks"].as_array().unwrap() {
        assert_neutral(c["detail"].as_str().unwrap());
    }

    let (code, _, err) = cli(&["bundle", "vectors/bundles/golden/no-such-file.json"]);
    assert_eq!(code, 2, "{err}");
    let (code, _, _) = cli(&["bundle"]);
    assert_eq!(code, 2);
}

#[test]
fn cli_record_head_statement_receipt() {
    let rec = read(&vectors().join("records/positive/verdict-denied.json"));
    let dir = std::env::temp_dir().join(format!("brutor-verify-cli-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let rec_path = dir.join("record.json");
    std::fs::write(&rec_path, serde_json::to_string(&rec["record"]).unwrap()).unwrap();
    let st_path = dir.join("statement.b64");
    std::fs::write(&st_path, rec["statement_b64"].as_str().unwrap()).unwrap();
    // raw CBOR works as well as base64
    let raw_path = dir.join("statement.cbor");
    std::fs::write(&raw_path, b64(rec["statement_b64"].as_str().unwrap())).unwrap();
    for st in [&st_path, &raw_path] {
        let (code, out, err) = cli(&[
            "record",
            rec_path.to_str().unwrap(),
            "--statement",
            st.to_str().unwrap(),
            "--keys",
            "vectors/keys/well-known.json",
        ]);
        assert_eq!(code, 0, "{out}{err}");
        assert!(out.contains("signature: valid under trusted kid"), "{out}");
        assert!(
            out.contains("payload binding: statement payload equals"),
            "{out}"
        );
        assert_neutral(&out);
    }
    let (code, out, _) = cli(&[
        "statement",
        "--b64",
        rec["statement_b64"].as_str().unwrap(),
        "--key",
        "not-a-key",
    ]);
    // a key that is not 64 hex ⇒ usage error
    assert_eq!(code, 2, "{out}");

    let head = read(&vectors().join("heads/adjacent-heads.json"));
    let (code, out, _) = cli(&[
        "head",
        "--b64",
        head["statement_b64"].as_str().unwrap(),
        "--previous-b64",
        head["previous_b64"].as_str().unwrap(),
        "--keys",
        "vectors/keys/well-known.json",
    ]);
    assert_eq!(code, 0, "{out}");
    assert!(
        out.contains("consistency with previous: the previous head is a prefix"),
        "{out}"
    );
    assert_neutral(&out);

    let rc = read(&vectors().join("receipts/rfc3161-ecp256.json"));
    let (code, out, _) = cli(&[
        "receipt",
        "--b64",
        rc["receipt_b64"].as_str().unwrap(),
        "--statement-b64",
        rc["statement_b64"].as_str().unwrap(),
        "--pin",
        rc["pin"].as_str().unwrap(),
    ]);
    assert_eq!(code, 0, "{out}");
    assert!(out.contains("timestamp (rfc3161)"), "{out}");
    assert_neutral(&out);
    let _ = std::fs::remove_dir_all(&dir);
}

/// Cross-implementation interop against the upstream `scitt-cose` (Python)
/// and, when built, its Go verifier. Needs Python with scitt-cose installed,
/// so CI never runs it:
///
/// ```text
/// SCITT_PYTHON=/path/to/venv/bin/python [SCITT_GO_VERIFY=/path/to/scitt-cose-go-verify] \
///   cargo test -p brutor-verify --test vectors -- --ignored interop_scitt_cose
/// ```
#[test]
#[ignore = "needs a Python venv with scitt-cose (SCITT_PYTHON); see vectors/README.md"]
fn interop_scitt_cose() {
    let py = std::env::var("SCITT_PYTHON")
        .expect("set SCITT_PYTHON to a python with scitt-cose installed");
    let mut cmd = Command::new(py);
    cmd.arg(vectors().join("interop/verify_with_scitt_cose.py"));
    if let Ok(go) = std::env::var("SCITT_GO_VERIFY") {
        cmd.args(["--go", &go]);
    }
    let out = cmd.output().unwrap();
    let text = String::from_utf8_lossy(&out.stdout);
    assert!(
        out.status.success(),
        "{text}{}",
        String::from_utf8_lossy(&out.stderr)
    );
}
