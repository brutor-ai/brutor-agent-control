// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Brutor action records — the portable evidence unit.
//!
//! * [`canon`] — RFC 8785 canonical bytes with the digest-hygiene rules
//!   (no floats, safe integers) and the `record_id` derivation.
//! * [`record`] — the `ai.brutor/action-record/v1` vocabulary, a typed
//!   builder that makes invalid records unrepresentable (the honesty
//!   invariant, the confirmed-effect binding), and `seal`.
//! * [`verify`] — the semantic verifier: fixed check order, stable
//!   finding codes, never throws, no clock, no network, no model.
//! * [`statement`] — sealing a record as an RFC 9943 Signed Statement in
//!   RFC 9995 hash-envelope mode, and verifying one.
//!
//! * [`bundle`] — `ai.brutor/evidence-bundle/v1`: build and verify.
//!
//! Nothing here knows about any regulatory framework.

pub mod bundle;
pub mod canon;
pub mod record;
pub mod statement;
pub mod verify;

pub use canon::{CanonError, canonical_bytes, json_digest, json_digest_hex};
pub use record::{RecordBuilder, RecordError, SCHEMA};
pub use verify::{Finding, Severity, VerificationResult, verify, verify_store};
