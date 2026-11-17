// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The seven registry-governed vocabularies of draft-04 §12 (seeded
//! values, verbatim) plus the vendored CPB *provisional* projection the
//! upstream verifiers consult for check 8.
//!
//! Registration governs shared meaning, never acceptance: an unregistered
//! value is an informational finding, never a rejection. A drift test
//! (`tests/vectors.rs`) re-parses the vendored `REGISTRY.md` and
//! `cpb_provisional.json` and fails if these tables disagree with them.

pub const VERDICT_CLASS: &[&str] = &[
    "executed",
    "blocked",
    "hitl_dispatched",
    "denied",
    "timeout",
    "errored",
    "engine_failure",
    "deferred",
    "needs_decision",
    "expired",
    "escalated",
    "resolved",
    "epoch_boundary",
];
pub const DISPOSITION_DECISION: &[&str] = &["accept", "reject", "needs_input", "deferred"];
pub const EFFECT_TYPE: &[&str] = &["write_order", "send_payment"];
pub const IRREVERSIBILITY_CLASS: &[&str] = &[
    "two_way",
    "one_way_recoverable",
    "one_way_consequential",
    "one_way_terminal",
];
pub const EFFECT_ATTESTATION: &[&str] = &["gate_executed", "runtime_claimed"];
pub const CHAIN_RELATION: &[&str] = &["confirms", "supersedes", "epoch_opens"];
pub const CITATION_PURPOSE: &[&str] = &["acted_on", "responds_to"];

/// `(registry name, seeded values)` for every registry, in check-8
/// emission order (`citation_purpose` is checked per reference entry,
/// after the rest).
pub const REGISTRIES: &[(&str, &[&str])] = &[
    ("verdict_class", VERDICT_CLASS),
    ("disposition.decision", DISPOSITION_DECISION),
    ("effect.type", EFFECT_TYPE),
    ("irreversibility_class", IRREVERSIBILITY_CLASS),
    ("effect_attestation", EFFECT_ATTESTATION),
    ("chain.relation", CHAIN_RELATION),
    ("citation_purpose", CITATION_PURPOSE),
];

/// `disposition.approver` is a closed enum, not a registry (§5.4).
pub const APPROVERS: &[&str] = &["human", "policy", "counterparty"];

/// verdict_class values that by their kind never dispatch (§5.4.2);
/// `epoch_boundary` is NOT in the upstream verifier's set (its registry
/// entry states the requirement, the Class-1 check does not enforce it).
pub const NEVER_DISPATCH: &[&str] = &[
    "blocked",
    "hitl_dispatched",
    "denied",
    "engine_failure",
    "deferred",
    "needs_decision",
    "expired",
    "escalated",
    "resolved",
];

/// Open-items predicate verdict classes (§5.4.4).
pub const OPEN_ITEM: &[&str] = &[
    "deferred",
    "needs_decision",
    "hitl_dispatched",
    "escalated",
    "blocked",
];

/// CPB provisional (Rung 3) values that resolve to a known artifact
/// class: `(registry, value, artifact class)`. Vendored from
/// `vectors/registry/cpb_provisional.json` (scitt-payload-binding
/// `e0ad1c7e`); diagnostic metadata only, never grants trust.
pub const PROVISIONAL: &[(&str, &str, &str)] = &[
    (
        "effect.type",
        "inference_completion",
        "mesh-inference-exchange",
    ),
    (
        "effect_attestation",
        "host_served_observed",
        "mesh-inference-exchange",
    ),
    ("chain.relation", "follows", "mesh-inference-exchange"),
];

pub fn seeded(registry: &str, value: &str) -> bool {
    REGISTRIES
        .iter()
        .find(|(n, _)| *n == registry)
        .map(|(_, vals)| vals.contains(&value))
        .unwrap_or(false)
}

pub fn provisional_class(registry: &str, value: &str) -> Option<&'static str> {
    PROVISIONAL
        .iter()
        .find(|(r, v, _)| *r == registry && *v == value)
        .map(|(_, _, c)| *c)
}

/// Disclosure Envelope eligible members → committed-digest path
/// (companion draft `-disclosure-envelope-00`, REGISTRY.md §10).
pub const DISCLOSURE_ELIGIBLE: &[(&str, &str)] = &[
    (
        "agent_input",
        "model_attestation.compute_attestation.agent_input_digest",
    ),
    (
        "agent_output",
        "model_attestation.compute_attestation.agent_output_digest",
    ),
];

pub fn disclosure_path(member: &str) -> Option<&'static str> {
    DISCLOSURE_ELIGIBLE
        .iter()
        .find(|(m, _)| *m == member)
        .map(|(_, p)| *p)
}
