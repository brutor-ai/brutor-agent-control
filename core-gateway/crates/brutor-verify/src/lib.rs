// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! `brutor-verify` — offline verification of Brutor evidence.
//!
//! One entry point per artefact, each a thin layer over the crates that
//! produce the artefact — nothing here re-implements a check:
//!
//! | artefact | entry point | checks come from |
//! |---|---|---|
//! | action record | [`verify_record`] | `brutor_evidence::verify` (the record rules) + [`verify_statement`] |
//! | Signed Statement | [`verify_statement`] | `brutor_evidence::statement`, `brutor_cose::sign1` |
//! | tree head | [`verify_head`] | `brutor_ctlog::TreeHead`, `brutor_cose::merkle` |
//! | witness receipt | [`verify_receipt`] | [`receipt`] (`brutor_cose::receipt`, `brutor_tsp`) |
//! | evidence bundle | [`verify_bundle`] | `brutor_evidence::bundle::verify` + [`receipt`] |
//!
//! Trust comes from a [`TrustedKeys`] set — a
//! `/.well-known/brutor-evidence-keys.json` document or explicit hex keys.
//! Without one, signatures are checked under the key each statement names
//! and the report says so (`kid_not_pinned`): a self-consistency check, not
//! an attribution.
//!
//! Every entry point returns a [`Report`]: the checks that ran with their
//! literal outcome, the findings with stable codes (see [`codes`] and the
//! crates' own codes), artefact facts, and `ok` = no `error` finding. The
//! report never says more than the checks it names — no "verified",
//! "compliant" or "certified" claims; a relying party draws those
//! conclusions, not this tool.
//!
//! No clock, no network, no randomness: the same inputs give the same
//! report on every platform, including `wasm32-unknown-unknown`.

pub mod keys;
pub mod receipt;
mod report;
mod verify;

#[cfg(feature = "wasm")]
pub mod wasm;

/// wasm32-unknown-unknown has no entropy source; verification needs none.
/// Registering a backend that always fails keeps it that way (see the
/// `getrandom` note in Cargo.toml).
#[cfg(all(target_arch = "wasm32", target_os = "unknown"))]
fn no_randomness(_: &mut [u8]) -> Result<(), getrandom::Error> {
    Err(getrandom::Error::UNSUPPORTED)
}
#[cfg(all(target_arch = "wasm32", target_os = "unknown"))]
getrandom::register_custom_getrandom!(no_randomness);

pub use keys::{KeyError, KidStatus, TrustedKeys};
pub use report::{Check, Finding, Outcome, Report, Severity, exit_code};
pub use verify::{
    BundleOptions, unwrap_bundle, verify_bundle, verify_head, verify_receipt, verify_record,
    verify_statement, verify_statement_b64,
};

/// Finding codes this crate adds on top of the record verifier
/// (`brutor_evidence::verify`) and the bundle verifier
/// (`brutor_evidence::bundle`), whose codes pass through unchanged.
pub mod codes {
    /// An input could not be read or parsed (base64, JSON, CBOR envelope).
    pub const INPUT_INVALID: &str = "input_invalid";
    /// No trusted keys were supplied; signatures were checked under the
    /// key each statement names (warning).
    pub const KID_NOT_PINNED: &str = "kid_not_pinned";
    /// The statement's kid is not in the trusted set.
    pub const UNTRUSTED_KID: &str = "untrusted_kid";
    /// The trusted set lists the kid as retired (info — retired keys stay
    /// trusted for what they signed).
    pub const KID_RETIRED: &str = "kid_retired";
    /// The kid header is not a 32-byte Ed25519 public key.
    pub const KID_INVALID: &str = "kid_invalid";
    /// The statement is not a well-formed statement of its declared kind.
    pub const STATEMENT_MALFORMED: &str = "statement_malformed";
    /// The COSE_Sign1 signature does not verify under the key.
    pub const SIGNATURE_INVALID: &str = "signature_invalid";
    /// The statement payload is not the record's recomputed identity.
    pub const STATEMENT_PAYLOAD_MISMATCH: &str = "statement_payload_mismatch";
    /// A tree head statement is malformed (content type, payload, iss/sub).
    pub const HEAD_MALFORMED: &str = "head_malformed";
    /// A tree head's signature does not verify.
    pub const HEAD_SIGNATURE_INVALID: &str = "head_signature_invalid";
    /// A tree head's embedded consistency proof from `prev_root` fails.
    pub const HEAD_CONSISTENCY_INVALID: &str = "head_consistency_invalid";
    /// The previous head given for comparison is itself not acceptable.
    pub const PREVIOUS_HEAD_INVALID: &str = "previous_head_invalid";
    /// The two heads name different logs.
    pub const HEAD_LOG_MISMATCH: &str = "head_log_mismatch";
    /// The previous head is not a prefix of this head (fork or rewrite).
    pub const HEAD_NOT_CONSISTENT: &str = "head_not_consistent";
    /// Consistency cannot be decided from the inputs (non-adjacent heads
    /// and no consistency proof, or a previous head larger than this one).
    pub const CONSISTENCY_UNDETERMINED: &str = "consistency_undetermined";
    /// A receipt or timestamp token failed a check.
    pub const RECEIPT_INVALID: &str = "receipt_invalid";
    /// No pinned witness key / TSA certificate: nothing was checked
    /// (warning; not evidence of forgery).
    pub const RECEIPT_UNPINNED: &str = "receipt_unpinned";
    /// A bundle receipt was not checked because no witness key / TSA pin
    /// was supplied for its kind (info).
    pub const RECEIPT_NOT_CHECKED: &str = "receipt_not_checked";
    /// A receipt kind other than `scitt` or `rfc3161`.
    pub const RECEIPT_KIND_UNKNOWN: &str = "receipt_kind_unknown";
}
