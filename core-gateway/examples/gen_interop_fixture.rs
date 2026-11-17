// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! One-off helper that emits Rust-produced Fernet tokens for the
//! cross-language interop fixture. Run with:
//!
//!   cargo run --example gen_interop_fixture
//!
//! The output goes to stdout; copy it into the
//! `tokens_from_rust` block of:
//!   brutor-gateway-control/tests/fixtures/crypto_interop_v1.json

use brutor_gateway_core::services::encryption::EncryptionService;

fn main() {
    let master_key = "brutor-interop-fixture-master-key-do-not-use-in-production";
    let svc = EncryptionService::new(master_key);

    let plaintexts: Vec<(&str, &str)> = vec![
        ("ascii_short", "sk_brutor_api_test_xyz"),
        ("oauth_token", "ya29.a0AfH6SMB-example-access-token"),
        ("unicode", "汉字テスト中文"),
        (
            "json_blob",
            r#"{"endpoint":"https://api.example.com","model":"gpt-4o-mini"}"#,
        ),
        ("newlines", "line1\nline2\tindented\r"),
    ];

    println!("{{");
    let last_idx = plaintexts.len() - 1;
    for (idx, (name, pt)) in plaintexts.iter().enumerate() {
        let token = svc.encrypt(pt).expect("encrypt");
        // Round-trip self-check
        let back = svc.decrypt(&token).expect("decrypt");
        assert_eq!(back, *pt, "self round-trip failed for {name}");
        let comma = if idx == last_idx { "" } else { "," };
        println!(
            "  \"{name}\": {{\n    \"plaintext\": {},\n    \"ciphertext\": \"{}\"\n  }}{comma}",
            serde_json::to_string(pt).unwrap(),
            token
        );
    }
    println!("}}");
}
