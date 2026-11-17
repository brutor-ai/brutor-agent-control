// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Standards-only COSE + Merkle substrate.
//!
//! Everything in this crate is defined by a published document:
//!
//! * COSE_Sign1 — RFC 9052 (`Sig_structure`, tag 18).
//! * COSE Hash Envelope — RFC 9995 (protected labels 258/259/260).
//! * CWT claims in a COSE header — RFC 9597 (label **15**, never 13).
//! * COSE Receipts — RFC 9942 (`vds` 395 in the protected header,
//!   `vdp` 396 in the unprotected header, inclusion proofs at key −1).
//! * Merkle tree — RFC 6962 / RFC 9162 (leaf `0x00`, node `0x01`).
//! * Transparent Statement — RFC 9943 (receipts at unprotected 394).
//!
//! It knows nothing about action records, tree heads or any framework.
//! That neutrality is deliberate: a relying party with any COSE library
//! can verify what this crate signs, and this crate can verify receipts
//! from any RFC 9943 transparency service.

pub mod cbor;
pub mod merkle;
pub mod receipt;
pub mod sign1;

pub use cbor::Value;
pub use merkle::Hash;

/// Errors surfaced by every module. Never panics on hostile bytes.
#[derive(Debug, thiserror::Error)]
pub enum CoseError {
    #[error("cbor decode: {0}")]
    Decode(String),
    #[error("cbor encode: {0}")]
    Encode(String),
    #[error("not a tagged COSE_Sign1 (tag 18, 4-element array)")]
    NotSign1,
    #[error("protected header is not a map")]
    ProtectedNotMap,
    #[error("duplicate map key in {0}")]
    DuplicateKey(&'static str),
    #[error("unsupported algorithm {0}")]
    UnsupportedAlg(i64),
    #[error("algorithm header (1) missing")]
    MissingAlg,
    #[error("signature verification failed")]
    BadSignature,
    #[error("payload is detached; caller must supply it")]
    DetachedPayload,
    #[error("kid header (4) is not a 32-byte Ed25519 key")]
    BadKid,
    #[error("merkle: {0}")]
    Merkle(#[from] merkle::MerkleError),
    #[error("receipt: {0}")]
    Receipt(String),
}
