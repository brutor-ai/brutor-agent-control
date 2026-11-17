// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Generate the Rust-side Ed25519 signature for the cross-language
//! interop fixture. Output goes to stdout; copy the hex value into
//! crypto_interop_v1.json → `ed25519.signature_hex_from_rust`.
//!
//! Ed25519 is deterministic per RFC 8032 — Python's and Rust's
//! signatures over the same (seed, message) MUST be byte-identical.

use ed25519_dalek::{Signer, SigningKey};

fn main() {
    let seed_hex = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60";
    let message = b"{\"name\":\"test-card\",\"version\":\"1.0.0\"}";

    let seed: [u8; 32] = hex::decode(seed_hex)
        .expect("seed hex")
        .try_into()
        .expect("32 bytes");
    let signing = SigningKey::from_bytes(&seed);
    let sig = signing.sign(message);
    println!("rust_signature_hex:  {}", hex::encode(sig.to_bytes()));
    println!(
        "derived_public_hex:  {}",
        hex::encode(signing.verifying_key().to_bytes())
    );
}
