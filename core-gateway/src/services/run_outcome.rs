// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Declared outcomes. The ONE place a finished run is judged.
//!
//! A run is judged on three independent questions:
//!
//! 1. **Did it complete without errors?** — `terminal_state`, decided by
//!    `run_ledger::derive_terminal_state`; when it did not, *why* is the
//!    [`error_cause`] derived here from the failing call.
//! 2. **Was the outcome one the contract declares?** — the agent reports
//!    `X-Brutor-Outcome-Id`; it is looked up in the `outcomes` section of the
//!    system's active contract closure ([`Catalogue`], [`judge`]).
//! 3. **Does it count toward the operating baseline?** — the contract says,
//!    per declared outcome.
//!
//! [`run_class`] folds the three answers into the classification every
//! consumer reads (`success | no_work | deviation | failed | not_decisive`).
//! It mirrors the control plane's `app/services/ai_system_outcomes.py`
//! exactly; `tests/run_outcome_guard.rs` fails if any other module in the
//! core decides success from `terminal_state` or derives an error cause.
//!
//! ## Fail-closed choices
//!
//! * A malformed outcome id is never declared, and never fails the request
//!   that carried it: it is stored verbatim (truncated to
//!   [`MAX_STORED_LEN`]) and judged `undeclared` — a deviation, not a
//!   success.
//! * A catalogue entry this module cannot read (no valid id, a
//!   `counts_toward_baseline` that is not a boolean, `variants` that is not
//!   a list) declares nothing: an outcome is only `declared` when the
//!   contract unambiguously says so.
//! * An unknown `terminal_state` is `failed`, the class that counts against
//!   the success rate — never `success`, never "not decisive".
//! * A contract that cannot be READ is an error the caller handles (the
//!   ledger refuses to finalize and the sweeper retries) — never "no
//!   contract", which would judge every run `undeclared` and stamp a false
//!   high-severity finding into history.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use serde::Deserialize;
use serde_json::Value;
use sqlx::PgPool;

use crate::constants::{proxy, proxy_subtype};

// ---------------------------------------------------------------------------
// Wire
// ---------------------------------------------------------------------------

/// Request header on the run-closing call; response header on an A2A
/// delegate's reply (and on the reply the gateway relays to the caller).
pub const OUTCOME_ID_HEADER: &str = "X-Brutor-Outcome-Id";
/// Response header the gateway adds to the reply it returns to an A2A caller.
pub const OUTCOME_CONFORMANCE_HEADER: &str = "X-Brutor-Outcome-Conformance";
/// Final A2A `Task` metadata key carrying the delegate's outcome (streamed /
/// async replies, whose headers are sent before the result exists).
pub const METADATA_OUTCOME_ID: &str = "brutor.outcome.id";
/// Final A2A `Task` metadata key the gateway adds for the caller.
pub const METADATA_OUTCOME_CONFORMANCE: &str = "brutor.outcome.conformance";
/// OTel root-span attribute read by the OTel inlet (same grammar).
pub const OTEL_OUTCOME_ID_ATTRIBUTE: &str = "brutor.outcome.id";

/// Longest `<outcome>` or `<variant>` part.
pub const MAX_PART_LEN: usize = 48;
/// Column width of every stored outcome id (`VARCHAR(128)`). A reported value
/// is stored verbatim up to this many characters.
pub const MAX_STORED_LEN: usize = 128;

/// `outcome_conformance` — mirrored in the control plane.
pub mod conformance {
    pub const DECLARED: &str = "declared";
    pub const UNDECLARED: &str = "undeclared";
    pub const UNREPORTED: &str = "unreported";
    pub const ALL: [&str; 3] = [DECLARED, UNDECLARED, UNREPORTED];
}

/// The one classification, mirrored in the control plane.
pub mod run_class {
    pub const SUCCESS: &str = "success";
    pub const NO_WORK: &str = "no_work";
    pub const DEVIATION: &str = "deviation";
    pub const FAILED: &str = "failed";
    pub const NOT_DECISIVE: &str = "not_decisive";
    pub const ALL: [&str; 5] = [SUCCESS, NO_WORK, DEVIATION, FAILED, NOT_DECISIVE];
}

/// `error_cause`, in the order of precedence given below.
pub mod error_cause {
    pub const BLOCKED_BY_POLICY: &str = "blocked_by_policy";
    pub const RATE_LIMITED: &str = "rate_limited";
    pub const MODEL_UNAVAILABLE: &str = "model_unavailable";
    pub const MODEL_REJECTED: &str = "model_rejected";
    pub const TOOL_FAILED: &str = "tool_failed";
    pub const SKILL_FAILED: &str = "skill_failed";
    pub const AGENT_UNREACHABLE: &str = "agent_unreachable";
    pub const DELEGATE_DEVIATED: &str = "delegate_deviated";
    pub const ITERATION_LIMIT: &str = "iteration_limit";
    pub const CLIENT_REPORTED: &str = "client_reported";
    pub const ALL: [&str; 10] = [
        BLOCKED_BY_POLICY,
        RATE_LIMITED,
        MODEL_UNAVAILABLE,
        MODEL_REJECTED,
        TOOL_FAILED,
        SKILL_FAILED,
        AGENT_UNREACHABLE,
        DELEGATE_DEVIATED,
        ITERATION_LIMIT,
        CLIENT_REPORTED,
    ];
}

/// Step kinds — the control plane's `run_signals._step_kind` mapping.
pub mod step_kind {
    pub const MODEL: &str = "model";
    pub const SKILL: &str = "skill";
    pub const TOOL: &str = "tool";
    pub const AGENT: &str = "agent";
    pub const OTHER: &str = "other";
}

/// The kind of one `proxy_logs` row, as the run ledger counts it: a skill
/// run arrives as `proxy_type = skill` or as an MCP call with subtype
/// `skill_run`, and is a skill either way, never a tool.
pub fn step_kind_of(proxy_type: &str, proxy_subtype: Option<&str>) -> &'static str {
    if proxy_type == proxy::LLM {
        step_kind::MODEL
    } else if proxy_type == proxy::SKILL || proxy_subtype == Some(proxy_subtype::SKILL_RUN) {
        step_kind::SKILL
    } else if proxy_type == proxy::MCP || proxy_type == proxy::VIRTUAL_MCP {
        step_kind::TOOL
    } else if proxy_type == proxy::A2A {
        step_kind::AGENT
    } else {
        step_kind::OTHER
    }
}

// ---------------------------------------------------------------------------
// Grammar
// ---------------------------------------------------------------------------

fn part_is_well_formed(p: &str) -> bool {
    (1..=MAX_PART_LEN).contains(&p.len())
        && p.bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_' || b == b'-')
}

/// `^[a-z0-9_-]{1,48}(\.[a-z0-9_-]{1,48})?$` — `<outcome>` or
/// `<outcome>.<variant>`.
pub fn is_well_formed(id: &str) -> bool {
    match id.split_once('.') {
        Some((o, v)) => part_is_well_formed(o) && part_is_well_formed(v),
        None => part_is_well_formed(id),
    }
}

/// What is stored for a value a client reported on a proxy call: the value
/// verbatim (surrounding whitespace trimmed, as HTTP does), truncated to
/// [`MAX_STORED_LEN`] characters. `None` for an absent or blank value — that
/// is "nothing reported", not a malformed report. Never fails: a malformed
/// value is stored and later judged `undeclared`.
pub fn stored(raw: &str) -> Option<String> {
    let v = raw.trim();
    if v.is_empty() {
        return None;
    }
    Some(v.chars().take(MAX_STORED_LEN).collect())
}

/// [`stored`] over raw header bytes. A value that is not valid UTF-8 is kept
/// (lossily) rather than dropped: it is malformed, and a malformed report is
/// evidence of a deviation, not of silence.
pub fn stored_from_header(value: &axum::http::HeaderValue) -> Option<String> {
    stored(&String::from_utf8_lossy(value.as_bytes()))
}

// ---------------------------------------------------------------------------
// The classification
// ---------------------------------------------------------------------------

/// The ONE classification of a finished run.
///
/// ```text
/// errored | exhausted                                   -> failed
/// abandoned | cancelled                                 -> not_decisive
/// completed | completed_degraded, declared, counts      -> success
/// completed | completed_degraded, declared, not counts  -> no_work
/// completed | completed_degraded, otherwise             -> deviation
/// ```
///
/// An unknown terminal state is `failed`: it counts against the success rate
/// rather than leaving it.
pub fn run_class(
    terminal_state: &str,
    outcome_conformance: Option<&str>,
    counts_toward_baseline: Option<bool>,
) -> &'static str {
    use crate::services::run_ledger::terminal;
    match terminal_state {
        terminal::ABANDONED | terminal::CANCELLED => run_class::NOT_DECISIVE,
        terminal::COMPLETED | terminal::COMPLETED_DEGRADED => {
            if outcome_conformance == Some(conformance::DECLARED) {
                if counts_toward_baseline == Some(true) {
                    run_class::SUCCESS
                } else {
                    run_class::NO_WORK
                }
            } else {
                run_class::DEVIATION
            }
        }
        // errored, exhausted, and anything unrecognised.
        _ => run_class::FAILED,
    }
}

// ---------------------------------------------------------------------------
// The catalogue — closure section `outcomes`
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
struct DeclaredOutcome {
    counts_toward_baseline: bool,
    /// Empty = the outcome is reported bare.
    variants: BTreeSet<String>,
}

/// The `outcomes` section of a contract closure, reduced to what judging
/// needs: which ids exist, which carry variants, and whether each counts
/// toward the baseline.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Catalogue {
    outcomes: BTreeMap<String, DeclaredOutcome>,
}

impl Catalogue {
    /// Read the closure's `outcomes` section. `None` when there is no
    /// section (absent, `null`, or not a list) — "no catalogue", under which
    /// every reported id is `undeclared`. Entries that cannot be read are
    /// skipped (they declare nothing); see the module docs.
    pub fn from_section(section: Option<&Value>) -> Option<Catalogue> {
        let entries = section?.as_array()?;
        let mut outcomes = BTreeMap::new();
        for e in entries {
            let Some(id) = e
                .get("id")
                .and_then(Value::as_str)
                .filter(|s| part_is_well_formed(s))
            else {
                continue;
            };
            let Some(counts) = e.get("counts_toward_baseline").and_then(Value::as_bool) else {
                continue;
            };
            let variants = match e.get("variants") {
                None | Some(Value::Null) => Some(BTreeSet::new()),
                Some(Value::Array(vs)) => vs
                    .iter()
                    .map(|v| {
                        v.get("id")
                            .and_then(Value::as_str)
                            .filter(|s| part_is_well_formed(s))
                            .map(str::to_string)
                    })
                    .collect::<Option<BTreeSet<String>>>(),
                Some(_) => None,
            };
            let Some(variants) = variants else {
                continue;
            };
            outcomes.insert(
                id.to_string(),
                DeclaredOutcome {
                    counts_toward_baseline: counts,
                    variants,
                },
            );
        }
        Some(Catalogue { outcomes })
    }

    /// Whether a well-formed id is declared, and if so whether it counts.
    fn lookup(&self, id: &str) -> Option<bool> {
        let (outcome, variant) = match id.split_once('.') {
            Some((o, v)) => (o, Some(v)),
            None => (id, None),
        };
        let o = self.outcomes.get(outcome)?;
        match variant {
            // An outcome WITH variants must be reported with one.
            None if o.variants.is_empty() => Some(o.counts_toward_baseline),
            None => None,
            Some(v) if o.variants.contains(v) => Some(o.counts_toward_baseline),
            Some(_) => None,
        }
    }
}

/// The verdict on one reported outcome.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Judgement {
    /// The value as stored (verbatim, ≤ [`MAX_STORED_LEN`]); `None` when
    /// nothing was reported.
    pub outcome_id: Option<String>,
    pub conformance: &'static str,
    /// Set only when `declared`.
    pub counts_toward_baseline: Option<bool>,
}

/// Judge a reported outcome against a catalogue.
///
/// * nothing reported → `unreported`;
/// * malformed, no catalogue, unknown id, an outcome-with-variants reported
///   bare, or an unknown variant → `undeclared`;
/// * otherwise `declared`, with the outcome's `counts_toward_baseline`.
pub fn judge(reported: Option<&str>, catalogue: Option<&Catalogue>) -> Judgement {
    let Some(id) = reported.and_then(stored) else {
        return Judgement {
            outcome_id: None,
            conformance: conformance::UNREPORTED,
            counts_toward_baseline: None,
        };
    };
    let counts = if is_well_formed(&id) {
        catalogue.and_then(|c| c.lookup(&id))
    } else {
        None
    };
    Judgement {
        outcome_id: Some(id),
        conformance: if counts.is_some() {
            conformance::DECLARED
        } else {
            conformance::UNDECLARED
        },
        counts_toward_baseline: counts,
    }
}

// ---------------------------------------------------------------------------
// Contract lookup — the system's ACTIVE contract
// ---------------------------------------------------------------------------

/// The contract a run (or segment) is judged against.
#[derive(Debug, Clone, PartialEq)]
pub struct JudgingContract {
    pub id: String,
    pub hash: String,
    pub version: i32,
    /// `None` when the closure has no `outcomes` section.
    pub catalogue: Option<Catalogue>,
}

type ContractRow = (String, String, String, i32, Option<Value>);

fn to_contract((_, id, hash, version, outcomes): ContractRow) -> JudgingContract {
    JudgingContract {
        id,
        hash,
        version,
        catalogue: Catalogue::from_section(outcomes.as_ref()),
    }
}

/// The contract that judges a run of `ai_system_id`: the one already stamped
/// on it (`pinned_contract_id` — a run straddling a promotion stays governed
/// by the contract it started under, the same first-write-wins rule as the
/// stamp), else the system's active one. `Ok(None)` = no contract. A read
/// failure is returned, never read as "no contract".
pub async fn contract_for_run(
    db: &PgPool,
    tenant_id: &str,
    ai_system_id: &str,
    pinned_contract_id: Option<&str>,
) -> Result<Option<JudgingContract>, sqlx::Error> {
    let row: Option<ContractRow> = sqlx::query_as(
        "SELECT ai_system_id, id, contract_hash, version, resolved_closure->'outcomes' \
           FROM ai_system_contracts \
          WHERE tenant_id = $1 \
            AND (id = $3 OR ($3 IS NULL AND ai_system_id = $2 AND status = 'active')) \
          LIMIT 1",
    )
    .bind(tenant_id)
    .bind(ai_system_id)
    .bind(pinned_contract_id)
    .fetch_optional(db)
    .await?;
    Ok(row.map(to_contract))
}

/// The active contract of each system, keyed by system id. Systems without
/// one are absent. A read failure is returned.
pub async fn active_contracts(
    db: &PgPool,
    tenant_id: &str,
    systems: &[String],
) -> Result<HashMap<String, JudgingContract>, sqlx::Error> {
    if systems.is_empty() {
        return Ok(HashMap::new());
    }
    let rows: Vec<ContractRow> = sqlx::query_as(
        "SELECT ai_system_id, id, contract_hash, version, resolved_closure->'outcomes' \
           FROM ai_system_contracts \
          WHERE tenant_id = $1 AND ai_system_id = ANY($2) AND status = 'active'",
    )
    .bind(tenant_id)
    .bind(systems)
    .fetch_all(db)
    .await?;
    Ok(rows
        .into_iter()
        .map(|r| (r.0.clone(), to_contract(r)))
        .collect())
}

// ---------------------------------------------------------------------------
// Error causes: why a run errored
// ---------------------------------------------------------------------------

/// Proxy subtypes that mean the GATEWAY refused the call: the control
/// plane's `DENY_SUBTYPES` plus an approval denied (`approval_rejected`) —
/// an expired approval is `approval_token_invalid`, already in the set. The
/// same list the run segments call a refusal (one definition).
pub use crate::services::run_segments::GATEWAY_REFUSAL_SUBTYPES as REFUSAL_SUBTYPES;

/// The facts about one call that decide its error cause.
#[derive(Debug, Clone, Default, PartialEq, Eq, Deserialize)]
pub struct CallFacts {
    pub id: String,
    /// When the call was logged — orders a failing step against the
    /// delegated calls whose segments carry a cause.
    #[serde(default)]
    pub at: Option<chrono::DateTime<chrono::Utc>>,
    pub proxy_type: String,
    pub proxy_subtype: Option<String>,
    pub http_status_code: Option<i32>,
    #[serde(default)]
    pub guardrail_blocked: bool,
    #[serde(default)]
    pub argument_policy_denied: bool,
    /// For an A2A call that entered a segment: that segment's
    /// [`segment_error_cause`]. Filled by the ledger, never by SQL.
    #[serde(skip)]
    pub segment_cause: Option<&'static str>,
}

/// Whether a call was refused by the gateway (guardrail block, argument
/// policy, access / ACL denial, approval denied or expired).
pub fn is_refusal(
    guardrail_blocked: bool,
    argument_policy_denied: bool,
    subtype: Option<&str>,
) -> bool {
    guardrail_blocked
        || argument_policy_denied
        || subtype.is_some_and(|s| REFUSAL_SUBTYPES.contains(&s))
}

/// The error cause one failing call explains, in the spec's precedence
/// order. `None` when the call's kind explains nothing (`other`).
pub fn cause_of_call(c: &CallFacts) -> Option<&'static str> {
    if is_refusal(
        c.guardrail_blocked,
        c.argument_policy_denied,
        c.proxy_subtype.as_deref(),
    ) {
        return Some(error_cause::BLOCKED_BY_POLICY);
    }
    if c.http_status_code == Some(429) {
        return Some(error_cause::RATE_LIMITED);
    }
    match step_kind_of(&c.proxy_type, c.proxy_subtype.as_deref()) {
        step_kind::MODEL => Some(match c.http_status_code {
            Some(s) if (400..500).contains(&s) => error_cause::MODEL_REJECTED,
            // 5xx, a connect error or timeout (no status, 502/504), or a
            // failure the provider reported in a 2xx stream: the model was
            // not available to do the work.
            _ => error_cause::MODEL_UNAVAILABLE,
        }),
        step_kind::TOOL => Some(error_cause::TOOL_FAILED),
        step_kind::SKILL => Some(error_cause::SKILL_FAILED),
        // A delegated call: what its segment says, else (the delegate is in
        // no AI System, so there is no segment) it did not answer.
        step_kind::AGENT => Some(c.segment_cause.unwrap_or(error_cause::AGENT_UNREACHABLE)),
        _ => None,
    }
}

/// A run's error cause (set iff `terminal_state ∈ {errored, exhausted}`).
///
/// * `failing` — the run's failing call: the last unsuccessful action before
///   close (a call held for approval is not unsuccessful; a delegated call
///   whose segment carries a cause is).
/// * `failing_is_last_step` — whether that call is the run's last action.
///
/// `errored`: the failing call's cause, else `client_reported` (the client
/// closed it errored and no call explains it). `exhausted`: the client said
/// the agent ran out of iterations — a failing call only overrides that when
/// the run ENDED on it (a run that recovered from a 429 an hour earlier and
/// then looped is an iteration limit, not a rate limit).
pub fn derive_error_cause(
    terminal_state: &str,
    failing: Option<&CallFacts>,
    failing_is_last_step: bool,
) -> Option<&'static str> {
    use crate::services::run_ledger::terminal;
    match terminal_state {
        terminal::ERRORED => Some(
            failing
                .and_then(cause_of_call)
                .unwrap_or(error_cause::CLIENT_REPORTED),
        ),
        terminal::EXHAUSTED => Some(
            failing
                .filter(|_| failing_is_last_step)
                .and_then(cause_of_call)
                .unwrap_or(error_cause::ITERATION_LIMIT),
        ),
        _ => None,
    }
}

/// A run segment's error cause.
///
/// * a gateway refusal (or a segment entered only by calls held for a
///   person) → `blocked_by_policy`;
/// * an A2A-entered segment that `failed` / `timed_out` → `agent_unreachable`;
/// * an A2A-entered segment that answered (or declined) with an outcome its
///   own contract does not declare, or none → `delegate_deviated`;
/// * otherwise none.
pub fn segment_error_cause(
    outcome: &str,
    refused: bool,
    entered_by_a2a: bool,
    outcome_conformance: Option<&str>,
) -> Option<&'static str> {
    use crate::services::run_segments::outcome as seg;
    if refused {
        return Some(error_cause::BLOCKED_BY_POLICY);
    }
    if !entered_by_a2a {
        return None;
    }
    match outcome {
        seg::FAILED | seg::TIMED_OUT => Some(error_cause::AGENT_UNREACHABLE),
        _ if outcome_conformance != Some(conformance::DECLARED) => {
            Some(error_cause::DELEGATE_DEVIATED)
        }
        _ => None,
    }
}

#[cfg(test)]
#[path = "run_outcome_tests.rs"]
mod tests;
