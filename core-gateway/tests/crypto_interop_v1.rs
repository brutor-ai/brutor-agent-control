// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
//! Cross-language crypto interop — Python ↔ Rust.
//!
//! Mirror image of
//! `brutor-gateway-control/tests/test_crypto_interop_python_rust.py`.
//! Both files load the same fixture
//! (`brutor-gateway-control/tests/fixtures/crypto_interop_v1.json`) and
//! cross-check each other:
//!
//! * Rust decrypts tokens produced by Python.
//! * Rust verifies Ed25519 signatures produced by Python.
//! * Rust's own Ed25519 signature over the fixture message is
//!   byte-identical to Python's (per RFC 8032 determinism).
//!
//! Fixture lives in the Python tree because that's where the
//! interop pair tests run against it most often (pytest is the
//! default workflow for that codebase). The relative path here
//! climbs out of `gateway-core/tests/` → repo root → into the
//! gateway-control side: this file needs the control plane's repository
//! (see [`load_fixture`] for what happens where it is absent).

#[path = "support/roots.rs"]
mod roots;

use std::fs;

use brutor_gateway_core::services::encryption::EncryptionService;
use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};
use serde_json::Value;

/// The shared fixture, read from the control plane's tree
/// (`tests/support/roots.rs`: `BRUTOR_CONTROL_REPO`, else the control plane
/// checked out beside this repository).
///
/// **This test needs a sibling repository.** Where the control plane is not
/// there, the run fails, unless it said that sibling repositories are
/// optional (`BRUTOR_TEST_SIBLINGS=optional`, as
/// `scripts/ci-community-alone.sh` does): then every test of this file is
/// skipped, the skip is written to stderr and to the skip log, and the
/// script's summary lists it. `None` means skipped.
fn load_fixture() -> Option<Value> {
    let control = roots::tree("control-plane")?;
    let path = control
        .join("tests")
        .join("fixtures")
        .join("crypto_interop_v1.json");
    let text = fs::read_to_string(&path).unwrap_or_else(|e| {
        panic!(
            "Failed to read interop fixture at {}: {}. The control plane is there and the \
             fixture is not.",
            path.display(),
            e,
        )
    });
    Some(serde_json::from_str(&text).expect("fixture must be valid JSON"))
}

// ---------------------------------------------------------------------------
// Fernet — Rust decrypts tokens produced by Python
// ---------------------------------------------------------------------------

#[test]
fn rust_decrypts_every_python_produced_token() {
    let Some(fixture) = load_fixture() else {
        return; // skipped, and said so: the control plane is not here
    };
    assert_eq!(fixture["format_version"], 1);

    let master_key = fixture["master_key"].as_str().expect("master_key string");
    let svc = EncryptionService::new(master_key);

    let cases = fixture["tokens_from_python"]
        .as_object()
        .expect("tokens_from_python is an object");
    assert!(
        !cases.is_empty(),
        "expected at least one Python-produced token"
    );

    for (case_name, entry) in cases {
        let expected_plain = entry["plaintext"].as_str().expect("plaintext is string");
        let ciphertext = entry["ciphertext"].as_str().expect("ciphertext is string");
        let decrypted = svc.decrypt(ciphertext).unwrap_or_else(|e| {
            panic!(
                "Rust failed to decrypt the Python-produced token for case \
                 '{case_name}': {e}. Wire-format drift between the two \
                 Fernet implementations — most often PBKDF2 params, base64 \
                 alphabet, AES padding, HMAC key split, or version byte. \
                 Compare src/services/encryption.rs against \
                 app/services/oauth/encryption_service.py."
            )
        });
        assert_eq!(
            decrypted, expected_plain,
            "Rust decrypted the Python token for case '{case_name}' but \
             produced a different plaintext than recorded in the fixture. \
             This is a *severe* regression — the HMAC must have passed but \
             AES yielded the wrong output, suggesting key-derivation drift."
        );
    }
}

// ---------------------------------------------------------------------------
// Fernet — Rust round-trips its own historical tokens (defensive)
// ---------------------------------------------------------------------------

#[test]
fn rust_decrypts_its_own_legacy_tokens() {
    // Same shape as the Python-side `TestFernetPythonSelfRoundTrip`:
    // tokens minted by Rust at v0.7.95 must continue to decrypt under
    // any future Rust implementation. Otherwise upgrading Brutor strands
    // every encrypted secret the Rust side ever wrote.
    let Some(fixture) = load_fixture() else {
        return; // skipped, and said so: the control plane is not here
    };
    let master_key = fixture["master_key"].as_str().unwrap();
    let svc = EncryptionService::new(master_key);

    let cases = fixture["tokens_from_rust"].as_object().unwrap();
    for (case_name, entry) in cases {
        let expected = entry["plaintext"].as_str().unwrap();
        let token = entry["ciphertext"].as_str().unwrap();
        let got = svc
            .decrypt(token)
            .unwrap_or_else(|e| panic!("Rust legacy token '{case_name}' failed to decrypt: {e}"));
        assert_eq!(
            got, expected,
            "Rust legacy token '{case_name}' decrypted to wrong plaintext"
        );
    }
}

// ---------------------------------------------------------------------------
// Ed25519 — RFC 8032 determinism across implementations
// ---------------------------------------------------------------------------

#[test]
fn rust_ed25519_signature_byte_identical_to_python() {
    // Ed25519 is fully deterministic per RFC 8032 §5.1. For the same
    // (private key, message) pair, every conformant implementation must
    // produce the same 64-byte signature. If Python and Rust ever differ,
    // ONE of them has stopped being RFC-conformant — a serious bug.
    let Some(fixture) = load_fixture() else {
        return; // skipped, and said so: the control plane is not here
    };
    let ed = &fixture["ed25519"];

    let seed_hex = ed["private_key_hex_seed"].as_str().unwrap();
    let message = ed["message_utf8"].as_str().unwrap().as_bytes();

    let seed: [u8; 32] = hex::decode(seed_hex).unwrap().try_into().unwrap();
    let signing = SigningKey::from_bytes(&seed);
    let rust_sig = signing.sign(message);

    let recorded_rust = ed["signature_hex_from_rust"].as_str().unwrap();
    let recorded_python = ed["signature_hex_from_python"].as_str().unwrap();

    assert_eq!(
        hex::encode(rust_sig.to_bytes()),
        recorded_rust,
        "Rust regenerated its own signature differently from the fixture. \
         ed25519-dalek output drift?"
    );
    assert_eq!(
        recorded_python, recorded_rust,
        "Python and Rust Ed25519 signatures over the same (seed, message) \
         differ. One library has stopped honouring RFC 8032 determinism."
    );
}

#[test]
fn rust_verifies_python_produced_signature() {
    // The card-signing flow has Python sign and (in some cases) Rust verify
    // — and vice versa, with the A2A delegation chain. Exercise the
    // verifier directly here so a regression in the verifier itself
    // (not the signer) gets caught.
    let Some(fixture) = load_fixture() else {
        return; // skipped, and said so: the control plane is not here
    };
    let ed = &fixture["ed25519"];

    let pub_hex = ed["public_key_hex"].as_str().unwrap();
    let pub_bytes: [u8; 32] = hex::decode(pub_hex).unwrap().try_into().unwrap();
    let verifying = VerifyingKey::from_bytes(&pub_bytes).expect("valid Ed25519 public key");

    let sig_hex = ed["signature_hex_from_python"].as_str().unwrap();
    let sig_bytes: [u8; 64] = hex::decode(sig_hex).unwrap().try_into().unwrap();
    let sig = Signature::from_bytes(&sig_bytes);

    let message = ed["message_utf8"].as_str().unwrap().as_bytes();

    verifying
        .verify(message, &sig)
        .expect("Rust must verify the Python-produced signature");
}

#[test]
fn rust_rejects_tampered_python_signature() {
    // Defensive: flip a byte in the Python signature and confirm Rust
    // refuses it. Catches a (theoretical) over-permissive verifier
    // that would accept arbitrary 64 bytes.
    let Some(fixture) = load_fixture() else {
        return; // skipped, and said so: the control plane is not here
    };
    let ed = &fixture["ed25519"];
    let pub_bytes: [u8; 32] = hex::decode(ed["public_key_hex"].as_str().unwrap())
        .unwrap()
        .try_into()
        .unwrap();
    let verifying = VerifyingKey::from_bytes(&pub_bytes).unwrap();

    let mut sig_bytes: [u8; 64] = hex::decode(ed["signature_hex_from_python"].as_str().unwrap())
        .unwrap()
        .try_into()
        .unwrap();
    sig_bytes[10] ^= 0x42;
    let bad_sig = Signature::from_bytes(&sig_bytes);
    let message = ed["message_utf8"].as_str().unwrap().as_bytes();

    assert!(verifying.verify(message, &bad_sig).is_err());
}
