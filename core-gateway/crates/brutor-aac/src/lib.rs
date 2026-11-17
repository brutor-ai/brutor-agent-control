// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The optional **Agent Action Capsule** profile, pinned
//! to `draft-mih-scitt-agent-action-capsule-04` (format 4, plain JCS).
//!
//! The AAC drafts are individual Internet-Drafts from one company, not
//! WG-adopted; this crate is the whole of our exposure to them. Our stored
//! record model (`ai.brutor/action-record/v1`) never changes because of
//! AAC: a Capsule is a deterministic *projection* of a record, computed on
//! demand, behind the `BRUTOR_EVIDENCE_PROFILE_AAC_ENABLED` flag in the gateway.
//!
//! * [`mapping`] — record → Capsule (field map and every lossy field
//!   documented there and carried inside the Capsule).
//! * [`canon`] — `JSON-DIGEST`, the Capsule-ID derivation.
//! * [`envelope`] — the Producer Envelope (COSE_Sign1 over the raw ID).
//! * [`verify`] — the Class-1 verifier (upstream finding codes).
//! * [`disclosure`] — DE-1..DE-3 disclosure checks.
//! * [`bundle`] — `evidence-bundle/v2` build/verify, fragment codec.
//! * [`import`] — capsule-emit ledgers → a `received` view (never sealed).
//!
//! Pure: no database, no network, no clock. Upstream conformance vectors
//! are vendored under `vectors/` (see its README) and run by
//! `tests/vectors.rs`.

pub mod bundle;
pub mod canon;
pub mod disclosure;
pub mod envelope;
pub mod import;
pub mod mapping;
pub mod registry;
pub mod verify;

/// The draft revision this crate implements (the Capsule `spec_version`).
pub const SPEC_VERSION: &str = "draft-mih-scitt-agent-action-capsule-04";
/// The only supported serialization suite.
pub const FORMAT_VERSION: &str = "4";
/// Plain RFC 8785 (the withdrawn `jcs-n` fails closed).
pub const CANONICALIZATION_ID: &str = "jcs";
/// Media type of the Capsule JSON.
pub const CAPSULE_MEDIA_TYPE: &str = "application/agent-action-capsule+json";
/// Media type of the raw 32-byte Capsule-ID payload (envelope label 3).
pub const CAPSULE_ID_MEDIA_TYPE: &str = "application/agent-action-capsule-id";

pub use canon::compute_capsule_id;
pub use envelope::{sign_producer_envelope, verify_producer_envelope};
pub use mapping::{MapError, MappedCapsule, map_lineage, map_record};
pub use verify::{VerificationResult, verify, verify_store};
