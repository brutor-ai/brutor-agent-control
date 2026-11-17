// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Class-1 Capsule verifier (draft-04 §6 "Class 1 verification").
//!
//! Fixed check order, structured result, never panics, no clock, no
//! network, no model. The finding codes, severities and check numbers are
//! exactly those of the upstream reference verifiers (Go/Python/TS) so the
//! vendored conformance corpus pins them: `(check, severity, code)` tuples
//! are compared vector by vector.
//!
//! 1 structural · 2 identity · 3 confirmed-effect binding · 4 verdict/effect
//! orthogonality · 5 effect-attestation matrix · 6 chain (store-level) and
//! references boundary rule · 7 assurance reconciliation · 8 unknown
//! registry values (informational). The honesty invariant is asserted
//! defensively as a non-gating warning with no check number, as upstream.
//!
//! Only `self_attested` is ever derived for `attestation_mode`: this layer
//! verifies no Receipt, so a claimed `anchored` is reported as an
//! (informational) overclaim — "unknown never grades up".

use serde::Serialize;
use serde_json::{Map, Value};

use crate::canon::{self, CapsuleIdError, is_hex64, value_is_hex64};
use crate::registry;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Severity {
    Error,
    Warning,
    Info,
}

impl Severity {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Error => "error",
            Self::Warning => "warning",
            Self::Info => "info",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Finding {
    /// The governing Class-1 check (1..=8); `None` for the defensive
    /// honesty warning, which is not a numbered check.
    pub check: Option<u8>,
    pub severity: Severity,
    pub code: String,
    pub detail: String,
}

impl Finding {
    fn new(check: Option<u8>, severity: Severity, code: &str, detail: impl Into<String>) -> Self {
        Self {
            check,
            severity,
            code: code.into(),
            detail: detail.into(),
        }
    }
}

/// Assurance modes the verifier re-derived from the bytes it checked.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Derived {
    pub effect_mode: String,
    pub attestation_mode: String,
    pub ledger_mode: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cross_party_rung: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct VerificationResult {
    /// Gates trust in every other field.
    pub ok: bool,
    pub findings: Vec<Finding>,
    /// `None` only when the input is not a JSON object.
    pub derived: Option<Derived>,
    /// The recomputed `capsule_id`; `None` when the format/canonicalisation
    /// declaration fails closed or the value is not canonicalisable.
    pub capsule_id: Option<String>,
}

/// Derived `effect_mode` from the Effect Record (§5.2).
pub fn derive_effect_mode(effect: Option<&Map<String, Value>>) -> &'static str {
    let Some(e) = effect else {
        return "not_applicable";
    };
    match e.get("status").and_then(Value::as_str) {
        Some("planned") => "not_applicable",
        Some("confirmed") if value_is_hex64(e.get("response_digest")) => "confirmed",
        _ => "dispatched_unconfirmed",
    }
}

/// Derived `cross_party_rung` from the block's own bytes (§5.3).
pub fn derive_cross_party_rung(cp: &Map<String, Value>) -> &'static str {
    let counterparty = value_is_hex64(cp.get("counterparty_ref"));
    let correlator = cp
        .get("correlator")
        .and_then(Value::as_str)
        .map(|s| !s.is_empty())
        .unwrap_or(false);
    if !counterparty || !correlator {
        return "unilateral_fallback";
    }
    if cp.get("substantive").and_then(Value::as_bool) == Some(true) {
        "full_bilateral"
    } else {
        "acknowledged_receipt"
    }
}

fn rank(table: &[&str], v: &str) -> Option<usize> {
    table.iter().position(|x| *x == v)
}

const EFFECT_MODE_RANK: &[(&str, u8)] = &[
    ("not_applicable", 0),
    ("dispatched_unconfirmed", 0),
    ("confirmed", 1),
];
const ATTESTATION_RANK: &[&str] = &["self_attested", "anchored"];
const LEDGER_RANK: &[&str] = &["standalone", "chained", "anchored"];
const CROSS_PARTY_RANK: &[&str] = &[
    "unilateral_fallback",
    "acknowledged_receipt",
    "full_bilateral",
];

fn effect_mode_rank(v: &str) -> Option<u8> {
    EFFECT_MODE_RANK
        .iter()
        .find(|(k, _)| *k == v)
        .map(|(_, r)| *r)
}

const REQUIRED: &[&str] = &[
    "spec_version",
    "format_version",
    "capsule_id",
    "action_id",
    "action_type",
    "operator",
    "developer",
    "timestamp",
];

fn number_paths(v: &Value, path: &str, floats: &mut Vec<String>, unsafe_ints: &mut Vec<String>) {
    let here = || {
        if path.is_empty() {
            "<root>".to_string()
        } else {
            path.to_string()
        }
    };
    match v {
        Value::Number(n) => {
            if n.is_f64() {
                floats.push(here());
            } else {
                let safe = n
                    .as_i64()
                    .map(|i| i.unsigned_abs() <= canon::MAX_SAFE_INTEGER)
                    .or_else(|| n.as_u64().map(|u| u <= canon::MAX_SAFE_INTEGER))
                    .unwrap_or(false);
                if !safe {
                    unsafe_ints.push(here());
                }
            }
        }
        // serde_json's default Map is ordered by key bytes — the same
        // deterministic walk the reference verifier uses.
        Value::Object(m) => {
            for (k, child) in m {
                let p = if path.is_empty() {
                    k.clone()
                } else {
                    format!("{path}.{k}")
                };
                number_paths(child, &p, floats, unsafe_ints);
            }
        }
        Value::Array(items) => {
            for (i, child) in items.iter().enumerate() {
                let p = format!("{path}[{i}]");
                number_paths(child, &p, floats, unsafe_ints);
            }
        }
        _ => {}
    }
}

/// Reference findings (draft-04 §5.5.5), grouped by the check they belong
/// to so they are emitted in check order.
fn reference_findings(capsule: &Map<String, Value>) -> Vec<Finding> {
    let mut out = Vec::new();
    if capsule.get("format_version").and_then(Value::as_str) != Some("4") {
        return out;
    }
    let Some(raw) = capsule.get("references") else {
        return out;
    };
    let Some(refs) = raw.as_array() else {
        out.push(Finding::new(
            Some(1),
            Severity::Error,
            "references_malformed",
            "references MUST be an array (§5.5.5)",
        ));
        return out;
    };
    let parent = capsule
        .get("chain")
        .and_then(Value::as_object)
        .and_then(|c| c.get("parent_capsule_id"))
        .and_then(Value::as_str)
        .unwrap_or("");
    for (i, raw_ref) in refs.iter().enumerate() {
        let path = format!("references[{i}]");
        let Some(r) = raw_ref.as_object() else {
            out.push(Finding::new(
                Some(1),
                Severity::Error,
                "reference_malformed",
                format!("{path} MUST be an object (§5.5.5)"),
            ));
            continue;
        };
        for field in ["type", "digest_alg", "digest"] {
            let good = r
                .get(field)
                .and_then(Value::as_str)
                .map(|s| !s.is_empty())
                .unwrap_or(false);
            if !good {
                out.push(Finding::new(
                    Some(1),
                    Severity::Error,
                    "reference_malformed",
                    format!("{path}.{field} MUST be a non-empty string (§5.5.5)"),
                ));
            }
        }
        let digest = r.get("digest").and_then(Value::as_str).unwrap_or("");
        let aac_identity = r.get("type").and_then(Value::as_str) == Some("agent-action-capsule")
            && r.get("digest_alg").and_then(Value::as_str) == Some("SHA-256");
        if aac_identity {
            if !digest.is_empty() && !is_hex64(digest) {
                out.push(Finding::new(
                    Some(1),
                    Severity::Error,
                    "reference_malformed",
                    format!(
                        "{path}.digest MUST be an AAC Capsule ID for agent-action-capsule/SHA-256 (§5.5.5)"
                    ),
                ));
            }
            if !parent.is_empty() && digest == parent {
                out.push(Finding::new(
                    Some(6),
                    Severity::Error,
                    "reference_duplicates_chain_parent",
                    format!("{path} duplicates chain.parent_capsule_id (§5.5.5)"),
                ));
            }
        }
        if let Some(p) = r.get("citation_purpose") {
            match p.as_str() {
                Some(s) if !s.is_empty() => {
                    if !registry::seeded("citation_purpose", s) {
                        out.push(Finding::new(
                            Some(8),
                            Severity::Info,
                            "unknown_registry_value",
                            format!(
                                "{path}.citation_purpose is not seeded; informational, not rejected (§12)"
                            ),
                        ));
                    }
                }
                _ => out.push(Finding::new(
                    Some(1),
                    Severity::Error,
                    "reference_malformed",
                    format!("{path}.citation_purpose MUST be a non-empty string (§5.5.5)"),
                )),
            }
        }
        if let Some(lc) = r.get("log_coordinates") {
            let Some(coords) = lc.as_object() else {
                out.push(Finding::new(
                    Some(1),
                    Severity::Error,
                    "reference_log_coordinates_malformed",
                    format!("{path}.log_coordinates MUST be an object (§5.5.5)"),
                ));
                continue;
            };
            for field in ["log_id", "leaf_index", "inclusion_proof"] {
                if coords.get(field).map(Value::is_null).unwrap_or(true) {
                    out.push(Finding::new(
                        Some(1),
                        Severity::Error,
                        "reference_log_coordinates_malformed",
                        format!("{path}.log_coordinates requires {field} (§5.5.5)"),
                    ));
                }
            }
        }
    }
    out
}

/// Class-1 verification of one Capsule. `store` is the producer's ledger
/// (Capsule objects or bare capsule-id strings) for the chain checks;
/// `None` skips them with an informational finding.
pub fn verify(capsule: &Value, store: Option<&[Value]>) -> VerificationResult {
    let Some(c) = capsule.as_object() else {
        return VerificationResult {
            ok: false,
            findings: vec![Finding::new(
                Some(1),
                Severity::Error,
                "not_an_object",
                "Capsule is not a JSON object",
            )],
            derived: None,
            capsule_id: None,
        };
    };
    let mut f: Vec<Finding> = Vec::new();
    let effect = c.get("effect").and_then(Value::as_object);
    let disposition = c.get("disposition").and_then(Value::as_object);
    let chain = c.get("chain").and_then(Value::as_object);
    let cross_party = c.get("cross_party").and_then(Value::as_object);
    let refs = reference_findings(c);
    let refs_for = |n: u8| -> Vec<Finding> {
        refs.iter()
            .filter(|x| x.check == Some(n))
            .cloned()
            .collect()
    };

    // ---- 1 structural ---------------------------------------------------
    for fld in REQUIRED {
        match c.get(*fld) {
            None => f.push(Finding::new(
                Some(1),
                Severity::Error,
                "missing_required_field",
                format!("{fld} is REQUIRED (§5.1)"),
            )),
            Some(v) if !v.is_string() => f.push(Finding::new(
                Some(1),
                Severity::Error,
                "field_not_string",
                format!("{fld} MUST be a string (§5.1)"),
            )),
            _ => {}
        }
    }
    let mut carried_id = c.get("capsule_id").and_then(Value::as_str);
    if let Some(id) = carried_id
        && !is_hex64(id)
    {
        f.push(Finding::new(
            Some(1),
            Severity::Error,
            "capsule_id_malformed",
            "capsule_id MUST be 64 lowercase hex (§5.1)",
        ));
        carried_id = None;
    }
    if let Some(at) = c.get("action_type").and_then(Value::as_str)
        && at != "fyi"
        && at != "decide"
    {
        f.push(Finding::new(
            Some(1),
            Severity::Error,
            "action_type_invalid",
            "action_type MUST be 'fyi' or 'decide' (§5.1)",
        ));
    }
    let mut profile_valid = false;
    if let Some(fv) = c.get("format_version").and_then(Value::as_str) {
        if fv != "4" {
            f.push(Finding::new(
                Some(1),
                Severity::Error,
                "unsupported_format_version",
                format!("format_version {fv:?} is not supported; expected \"4\" (§5.1)"),
            ));
        } else {
            match c.get("canonicalization_id") {
                None => f.push(Finding::new(
                    Some(1),
                    Severity::Error,
                    "canonicalization_id_missing",
                    "format_version \"4\" REQUIRES canonicalization_id=\"jcs\" (§5.1)",
                )),
                Some(Value::String(s)) if s == "jcs" => profile_valid = true,
                Some(Value::String(_)) => f.push(Finding::new(
                    Some(1),
                    Severity::Error,
                    "canonicalization_profile_mismatch",
                    "format_version \"4\" REQUIRES canonicalization_id=\"jcs\" (§5.1)",
                )),
                Some(_) => f.push(Finding::new(
                    Some(1),
                    Severity::Error,
                    "canonicalization_id_not_string",
                    "canonicalization_id MUST be a string (§5.1)",
                )),
            }
        }
    }
    for fld in ["effect", "assurance", "disposition", "chain", "cross_party"] {
        if let Some(v) = c.get(fld)
            && !v.is_object()
        {
            f.push(Finding::new(
                Some(1),
                Severity::Error,
                "block_not_object",
                format!("{fld} MUST be a JSON object when present"),
            ));
        }
    }
    if let Some(v) = c.get("constraints")
        && !v.is_array()
    {
        f.push(Finding::new(
            Some(1),
            Severity::Error,
            "constraints_not_array",
            "constraints MUST be an array when present (§8.1)",
        ));
    }
    let (mut floats, mut unsafe_ints) = (Vec::new(), Vec::new());
    number_paths(capsule, "", &mut floats, &mut unsafe_ints);
    for p in floats {
        f.push(Finding::new(
            Some(1),
            Severity::Error,
            "float_in_digest_field",
            format!("floating-point value at {p}; §5.1 forbids it"),
        ));
    }
    for p in unsafe_ints {
        f.push(Finding::new(
            Some(1),
            Severity::Error,
            "unsafe_integer_in_digest_field",
            format!(
                "integer outside the JS-safe range (+/-{}) at {p}; large integers MUST be exact decimal strings",
                canon::MAX_SAFE_INTEGER
            ),
        ));
    }
    if let Some(d) = disposition {
        let approver = d.get("approver").and_then(Value::as_str);
        match approver {
            None => f.push(Finding::new(
                Some(1),
                Severity::Error,
                "missing_required_field",
                "disposition.approver is REQUIRED (§5.4)",
            )),
            Some(a) if !registry::APPROVERS.contains(&a) => f.push(Finding::new(
                Some(1),
                Severity::Error,
                "approver_invalid",
                format!("disposition.approver MUST be human|policy|counterparty (§5.4); got {a:?}"),
            )),
            _ => {}
        }
        if !d.contains_key("decision") {
            f.push(Finding::new(
                Some(1),
                Severity::Error,
                "missing_required_field",
                "disposition.decision is REQUIRED (§5.4)",
            ));
        }
        match d.get("human_disposed").and_then(Value::as_bool) {
            None => f.push(Finding::new(
                Some(1),
                Severity::Error,
                "field_not_bool",
                "disposition.human_disposed is REQUIRED and boolean (§5.4)",
            )),
            Some(true) if approver != Some("human") => f.push(Finding::new(
                None,
                Severity::Warning,
                "dishonest_human_disposed",
                "human_disposed=true with a non-human approver (§5.4); structurally unconstructable by a conforming producer — reported as a non-gating defensive warning",
            )),
            _ => {}
        }
    }
    f.extend(refs_for(1));

    // ---- 2 identity -----------------------------------------------------
    let mut recomputed = None;
    if let (Some(carried), true) = (carried_id, profile_valid) {
        match canon::compute_capsule_id(capsule) {
            Ok(id) => {
                if id != carried {
                    f.push(Finding::new(
                        Some(2),
                        Severity::Error,
                        "capsule_id_mismatch",
                        format!("recomputed {id} != carried {carried}"),
                    ));
                }
                recomputed = Some(id);
            }
            // Already reported structurally (check 1).
            Err(CapsuleIdError::Float(_) | CapsuleIdError::UnsafeInteger(_)) => {}
            Err(e) => f.push(Finding::new(
                Some(2),
                Severity::Error,
                "capsule_id_uncomputable",
                e.to_string(),
            )),
        }
    }

    // ---- 3 confirmed-effect binding ---------------------------------------
    if let Some(e) = effect
        && e.get("status").and_then(Value::as_str) == Some("confirmed")
        && !value_is_hex64(e.get("response_digest"))
    {
        f.push(Finding::new(
            Some(3),
            Severity::Error,
            "confirmed_without_response",
            "effect.status 'confirmed' requires a 64-hex response_digest (§5.2)",
        ));
    }
    let effect_mode = derive_effect_mode(effect);

    // ---- 4 verdict / effect orthogonality ---------------------------------
    let verdict_class = disposition
        .and_then(|d| d.get("verdict_class"))
        .and_then(Value::as_str)
        .unwrap_or("");
    if registry::NEVER_DISPATCH.contains(&verdict_class) && effect_mode != "not_applicable" {
        f.push(Finding::new(
            Some(4),
            Severity::Error,
            "verdict_effect_conflict",
            format!(
                "verdict_class {verdict_class:?} never dispatches, but derived effect_mode is {effect_mode:?} (§5.4.2)"
            ),
        ));
    }

    // ---- 5 effect-attestation matrix --------------------------------------
    let ea_present = effect
        .and_then(|e| e.get("effect_attestation"))
        .map(|v| !v.is_null())
        .unwrap_or(false);
    if effect_mode == "not_applicable" {
        if ea_present {
            f.push(Finding::new(
                Some(5),
                Severity::Error,
                "effect_attestation_present",
                "effect_attestation MUST be absent for effect_mode 'not_applicable' (§5.2)",
            ));
        }
    } else if !ea_present {
        f.push(Finding::new(
            Some(5),
            Severity::Error,
            "effect_attestation_missing",
            format!("effect_attestation REQUIRED for effect_mode {effect_mode:?} (§5.2)"),
        ));
    }

    // ---- 6 chain semantics --------------------------------------------------
    if let Some(ch) = chain {
        let parent = ch.get("parent_capsule_id");
        if !value_is_hex64(parent) {
            f.push(Finding::new(
                Some(6),
                Severity::Error,
                "chain_parent_malformed",
                "chain.parent_capsule_id MUST be a 64-hex capsule_id (§5.4.4)",
            ));
        }
        if !ch.contains_key("relation") {
            f.push(Finding::new(
                Some(6),
                Severity::Error,
                "missing_required_field",
                "chain.relation is REQUIRED when a chain block is present (§5.4.4)",
            ));
        }
        match store {
            None => f.push(Finding::new(
                Some(6),
                Severity::Info,
                "chain_check_store_level",
                "chain parent-existence and concurrent-supersedes are store-level checks (§6); not run without a store",
            )),
            Some(items) => {
                if let Some(p) = parent.and_then(Value::as_str) {
                    let found = items.iter().any(|it| match it {
                        Value::Object(m) => m.get("capsule_id").and_then(Value::as_str) == Some(p),
                        Value::String(s) => s == p,
                        _ => false,
                    });
                    if !found {
                        f.push(Finding::new(
                            Some(6),
                            Severity::Error,
                            "chain_parent_missing",
                            format!("chain parent {p} not found in the store (§6)"),
                        ));
                    }
                }
            }
        }
    }
    f.extend(refs_for(6));

    // ---- 7 assurance reconciliation ---------------------------------------
    let derived = Derived {
        effect_mode: effect_mode.into(),
        attestation_mode: "self_attested".into(),
        ledger_mode: if chain.is_some() {
            "chained"
        } else {
            "standalone"
        }
        .into(),
        cross_party_rung: cross_party.map(|cp| derive_cross_party_rung(cp).to_string()),
    };
    if let Some(stated) = c.get("assurance").and_then(Value::as_object) {
        if let Some(sm) = stated.get("effect_mode").and_then(Value::as_str)
            && let (Some(r), Some(d)) =
                (effect_mode_rank(sm), effect_mode_rank(&derived.effect_mode))
            && r > d
        {
            f.push(Finding::new(
                Some(7),
                Severity::Error,
                "assurance_overclaim",
                format!(
                    "claimed effect_mode {sm:?} but verifier derived {:?} (§5.3)",
                    derived.effect_mode
                ),
            ));
        }
        if let Some(sa) = stated.get("attestation_mode").and_then(Value::as_str)
            && let Some(r) = rank(ATTESTATION_RANK, sa)
            && r > 0
        {
            f.push(Finding::new(
                Some(7),
                Severity::Info,
                "assurance_overclaim",
                format!(
                    "claimed attestation_mode {sa:?} but no Receipt verified at this layer (§5.3)"
                ),
            ));
        }
        if let Some(sl) = stated.get("ledger_mode").and_then(Value::as_str)
            && let Some(r) = rank(LEDGER_RANK, sl)
            && r > rank(LEDGER_RANK, &derived.ledger_mode).unwrap_or(0)
        {
            f.push(Finding::new(
                Some(7),
                Severity::Info,
                "assurance_overclaim",
                format!(
                    "claimed ledger_mode {sl:?} but verifier derived {:?} (§5.3)",
                    derived.ledger_mode
                ),
            ));
        }
        if let Some(sc) = stated.get("cross_party_rung").and_then(Value::as_str)
            && let Some(r) = rank(CROSS_PARTY_RANK, sc)
        {
            let d = derived
                .cross_party_rung
                .as_deref()
                .and_then(|x| rank(CROSS_PARTY_RANK, x))
                .unwrap_or(0);
            if r > d {
                f.push(Finding::new(
                    Some(7),
                    Severity::Info,
                    "assurance_overclaim",
                    format!(
                        "claimed cross_party_rung {sc:?} but verifier derived {:?} (§5.3 Cross-party assurance)",
                        derived.cross_party_rung.as_deref().unwrap_or("")
                    ),
                ));
            }
        }
    }

    // ---- 8 unknown registry values ------------------------------------------
    const FIELDS: &[(&str, &str, &str)] = &[
        ("verdict_class", "disposition", "verdict_class"),
        ("disposition.decision", "disposition", "decision"),
        ("effect.type", "effect", "type"),
        ("irreversibility_class", "effect", "irreversibility_class"),
        ("effect_attestation", "effect", "effect_attestation"),
        ("chain.relation", "chain", "relation"),
    ];
    for (reg, block, member) in FIELDS {
        let Some(val) = c
            .get(*block)
            .and_then(Value::as_object)
            .and_then(|b| b.get(*member))
            .and_then(Value::as_str)
        else {
            continue;
        };
        if registry::seeded(reg, val) {
            continue;
        }
        if let Some(class) = registry::provisional_class(reg, val) {
            f.push(Finding::new(
                Some(8),
                Severity::Info,
                "known_provisional_registry_value",
                format!(
                    "{block}.{member}={val:?} resolves known status 'provisional' via vendored CPB registry (payload class {class:?}); informational, not rejected (§12)"
                ),
            ));
            continue;
        }
        f.push(Finding::new(
            Some(8),
            Severity::Info,
            "unknown_registry_value",
            format!(
                "{block}.{member}={val:?} is not a seeded {reg} value; informational, not rejected (§12)"
            ),
        ));
        if *reg == "effect_attestation" {
            f.push(Finding::new(
                Some(8),
                Severity::Info,
                "effect_attestation_graded_floor",
                "unknown effect_attestation graded no stronger than 'runtime_claimed' (§5.2)",
            ));
        }
    }
    f.extend(refs_for(8));

    let ok = !f.iter().any(|x| x.severity == Severity::Error);
    VerificationResult {
        ok,
        findings: f,
        derived: Some(derived),
        capsule_id: recomputed,
    }
}

/// Verify a ledger in order with the store-level chain checks: parent
/// existence, and concurrent supersedes (the earliest `supersedes` over a
/// parent is authoritative; later ones surface as info findings).
pub fn verify_store(ledger: &[Value]) -> Vec<VerificationResult> {
    let mut results: Vec<VerificationResult> =
        ledger.iter().map(|c| verify(c, Some(ledger))).collect();
    let mut seen: Vec<&str> = Vec::new();
    for (i, c) in ledger.iter().enumerate() {
        let Some(ch) = c.get("chain").and_then(Value::as_object) else {
            continue;
        };
        if ch.get("relation").and_then(Value::as_str) != Some("supersedes") {
            continue;
        }
        let parent = ch
            .get("parent_capsule_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        if parent.is_empty() {
            continue;
        }
        if seen.contains(&parent) {
            results[i].findings.push(Finding::new(
                Some(6),
                Severity::Info,
                "concurrent_supersedes",
                format!("a later supersedes over parent {parent}; the earliest is authoritative (§5.4.4)"),
            ));
        } else {
            seen.push(parent);
        }
    }
    results
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn non_object_is_structured_failure() {
        let r = verify(&json!([1, 2]), None);
        assert!(!r.ok);
        assert_eq!(r.findings[0].code, "not_an_object");
        assert!(r.derived.is_none());
    }

    #[test]
    fn effect_mode_derivation() {
        let m = |v: Value| derive_effect_mode(v.as_object());
        assert_eq!(m(json!({"status": "planned"})), "not_applicable");
        assert_eq!(
            m(json!({"status": "confirmed", "response_digest": "a".repeat(64)})),
            "confirmed"
        );
        assert_eq!(m(json!({"status": "confirmed"})), "dispatched_unconfirmed");
        assert_eq!(m(json!({"status": "failed"})), "dispatched_unconfirmed");
        assert_eq!(derive_effect_mode(None), "not_applicable");
    }
}
