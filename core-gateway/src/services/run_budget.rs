// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Run-level caps — the gate that stops a runaway agent at its own expense.
//!
//! Every other spend control Brutor has is scoped to a tenant, a key, a
//! resource group or a model, over a daily or monthly window. Those all fire
//! during an agent loop, but none of them is scoped to the *loop*: a runaway
//! consumes the whole group's daily budget and then 429s every other system
//! bound to that group until UTC midnight. Rate limits don't help either —
//! they cap velocity, so a loop under its RPM just runs all day at exactly
//! the permitted rate.
//!
//! These caps are ceilings for ONE run, keyed on the `root_task_id` minted at
//! request entry (`a2a_delegation::parse_or_mint`), so the run that caused the
//! problem is the thing that stops.
//!
//! ## Which envelope is enforced
//!
//! Enforcement reads the system's **live** `operating_envelope`, not the one
//! frozen in its active contract. That is deliberate and it differs from the
//! Assurance Report, which grades the *signed* envelope on purpose ("assurance
//! is against what was signed off"). If enforcement waited for a signature,
//! the systems that most need a runaway guard — the ones with no approved
//! contract at all — would have none. Loosening a live cap without approval
//! still shows up: the report flags `config_drifted` and the contract diff
//! shows the widening to the approver.
//!
//! ## Fail-open, deliberately
//!
//! An undeclared term is not a cap of zero. A system with no envelope is
//! ungated here, exactly as it is today — this feature can only ever *add*
//! refusals for operators who asked for them.

use crate::services::limits::LimitDim;
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use dashmap::DashMap;
use serde_json::Value;

use crate::services::cache::usage_state::{RUN_TTL_SECS, RunUsage, UsageState, now_epoch_ms};

/// Envelope term keys. These MUST match `ENVELOPE_TERMS` in the control
/// plane's `app/services/ai_system_envelope.py` — one vocabulary, two
/// consumers. The control plane refuses unknown terms at write time, so a
/// typo here silently disables a cap rather than erroring; the
/// `term_names_match_the_control_plane` test pins them.
pub const TERM_MAX_COST_PER_RUN: &str = "max_cost_per_run_usd";
pub const TERM_MAX_CALLS_PER_RUN: &str = "max_llm_calls_per_run";
pub const TERM_MAX_TOKENS_PER_RUN: &str = "max_tokens_per_run";
/// The most governed steps that are not model calls (tool calls, skill
/// executions, agent-to-agent hops) one run may take. A model call is counted
/// by [`TERM_MAX_CALLS_PER_RUN`] and by nothing here.
pub const TERM_MAX_ACTIONS_PER_RUN: &str = "max_actions_per_run";
pub const TERM_MAX_DEPTH_PER_RUN: &str = "max_delegation_depth_per_run";
pub const TERM_MAX_DURATION_PER_RUN: &str = "max_duration_seconds_per_run";
/// The most distinct runs one credential (an agent identity, else an API
/// key) may START in a window while it acts in this group. Not a ceiling of
/// one run: it bounds how many runs there are, so a caller cannot stay under
/// every per-run ceiling by putting each step in a run of its own.
pub const TERM_MAX_NEW_RUNS_PER_WINDOW: &str = "max_new_runs_per_window";
/// The length of that window in seconds; an hour when not declared.
pub const TERM_NEW_RUNS_WINDOW_SECONDS: &str = "new_runs_window_seconds";
pub const DEFAULT_NEW_RUNS_WINDOW_SECS: u64 = 3_600;
/// The longest window the gateway keeps a set of runs for.
pub const MAX_NEW_RUNS_WINDOW_SECS: u64 = 31 * 86_400;
/// Not a ceiling: the run's counters could not be read, so no ceiling can be
/// checked and the call is refused (fail closed).
pub const TERM_RUN_USAGE_UNAVAILABLE: &str = "run_usage_unavailable";

/// How long a group's caps are cached. Same reasoning as the residency
/// profile cache: an envelope changes at human speed, and re-reading it per
/// request would put a query on the hot path for a value that is almost
/// always identical.
const CAPS_TTL: Duration = Duration::from_secs(60);

#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct RunCaps {
    pub max_cost_usd: Option<f64>,
    pub max_calls: Option<i64>,
    pub max_tokens: Option<i64>,
    pub max_actions: Option<i64>,
    pub max_depth: Option<i64>,
    pub max_duration_secs: Option<i64>,
    /// The limit on new runs per credential ([`TERM_MAX_NEW_RUNS_PER_WINDOW`]).
    pub new_runs: Option<NewRunLimit>,
}

/// `max` distinct runs started per window of `window_secs` seconds.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NewRunLimit {
    pub max: i64,
    pub window_secs: u64,
}

impl RunCaps {
    /// Parse the declared terms out of an operating envelope. Terms that are
    /// absent, null, non-numeric or non-positive are treated as undeclared —
    /// a malformed bound must never become a cap of zero that refuses every
    /// call on the system.
    pub fn from_envelope(envelope: Option<&Value>) -> Self {
        let obj = match envelope.and_then(|v| v.as_object()) {
            Some(o) => o,
            None => return Self::default(),
        };
        let num = |key: &str| -> Option<f64> {
            obj.get(key)
                .and_then(|v| v.as_f64())
                .filter(|n| n.is_finite() && *n > 0.0)
        };
        Self {
            max_cost_usd: num(TERM_MAX_COST_PER_RUN),
            max_calls: num(TERM_MAX_CALLS_PER_RUN).map(|n| n as i64),
            max_tokens: num(TERM_MAX_TOKENS_PER_RUN).map(|n| n as i64),
            max_actions: num(TERM_MAX_ACTIONS_PER_RUN).map(|n| n as i64),
            max_depth: num(TERM_MAX_DEPTH_PER_RUN).map(|n| n as i64),
            max_duration_secs: num(TERM_MAX_DURATION_PER_RUN).map(|n| n as i64),
            // A malformed maximum is undeclared, like every other term. A
            // malformed window is the default one: the maximum the operator
            // declared still applies.
            new_runs: num(TERM_MAX_NEW_RUNS_PER_WINDOW).map(|max| NewRunLimit {
                max: max as i64,
                window_secs: num(TERM_NEW_RUNS_WINDOW_SECONDS)
                    .map(|secs| (secs as u64).clamp(1, MAX_NEW_RUNS_WINDOW_SECS))
                    .unwrap_or(DEFAULT_NEW_RUNS_WINDOW_SECS),
            }),
        }
    }

    pub fn is_empty(&self) -> bool {
        !self.has_read_terms() && self.max_actions.is_none()
    }

    /// Whether any term is judged by reading the run's usage or its chain
    /// ([`evaluate`]: cost, model calls, tokens, depth, duration). The action
    /// ceiling is not: it is admitted, never read and compared, so a group
    /// that declares only that costs a step no read of the run's counters.
    pub fn has_read_terms(&self) -> bool {
        self.max_cost_usd.is_some()
            || self.max_calls.is_some()
            || self.max_tokens.is_some()
            || self.max_depth.is_some()
            || self.max_duration_secs.is_some()
    }
}

/// A refusal: which ceiling, what it was, and what the run had reached.
#[derive(Debug, Clone, PartialEq)]
pub struct RunCapBlock {
    pub term: &'static str,
    pub limit: f64,
    pub observed: f64,
    pub message: String,
}

/// Decide whether this run may make another call.
///
/// Pure — no I/O — so the whole decision table is unit-testable without a
/// Redis or a database. Order matters only for which term is *reported*
/// first; each is independently sufficient to refuse.
pub fn evaluate(caps: &RunCaps, usage: &RunUsage, depth: u32) -> Option<RunCapBlock> {
    if let Some(limit) = caps.max_calls {
        if usage.calls >= limit {
            return Some(RunCapBlock {
                term: TERM_MAX_CALLS_PER_RUN,
                limit: limit as f64,
                observed: usage.calls as f64,
                message: format!(
                    "This run has made {} model call(s), reaching its ceiling of {}. \
                     The run was stopped here rather than allowed to consume the \
                     group's budget.",
                    usage.calls, limit
                ),
            });
        }
    }
    if let Some(limit) = caps.max_tokens {
        if usage.tokens >= limit {
            return Some(RunCapBlock {
                term: TERM_MAX_TOKENS_PER_RUN,
                limit: limit as f64,
                observed: usage.tokens as f64,
                message: format!(
                    "This run has consumed {} token(s), reaching its ceiling of {}.",
                    usage.tokens, limit
                ),
            });
        }
    }
    if let Some(limit) = caps.max_cost_usd {
        if usage.cost_usd >= limit {
            return Some(RunCapBlock {
                term: TERM_MAX_COST_PER_RUN,
                limit,
                observed: usage.cost_usd,
                message: format!(
                    "This run has spent ${:.4}, reaching its ceiling of ${:.4}.",
                    usage.cost_usd, limit
                ),
            });
        }
    }
    if let Some(limit) = caps.max_depth {
        // Depth is inherent in the verified delegation chain, so this needs
        // no counter: hop N already knows it is hop N.
        if i64::from(depth) > limit {
            return Some(RunCapBlock {
                term: TERM_MAX_DEPTH_PER_RUN,
                limit: limit as f64,
                observed: f64::from(depth),
                message: format!(
                    "This call is delegation hop {}, beyond the ceiling of {} for \
                     this system.",
                    depth, limit
                ),
            });
        }
    }
    if let Some(limit) = caps.max_duration_secs {
        if usage.age_secs >= limit {
            return Some(RunCapBlock {
                term: TERM_MAX_DURATION_PER_RUN,
                limit: limit as f64,
                observed: usage.age_secs as f64,
                message: format!(
                    "This run has been open for {}s, reaching its ceiling of {}s.",
                    usage.age_secs, limit
                ),
            });
        }
    }
    None
}

// ---------------------------------------------------------------------------
// Caps lookup (TTL-cached)
// ---------------------------------------------------------------------------

struct CachedCaps {
    caps: RunCaps,
    at: Instant,
}

fn cache() -> &'static DashMap<String, CachedCaps> {
    static CACHE: OnceLock<DashMap<String, CachedCaps>> = OnceLock::new();
    CACHE.get_or_init(DashMap::new)
}

/// Load the declared per-run caps for a resource group.
///
/// A DB error is cached as "no caps" for the TTL — the same fail-open choice
/// the rest of this module makes, and it keeps a database blip from turning
/// into a wall of 429s. `None` results are cached too, because the common
/// case is a system with no envelope and re-querying that every request is
/// exactly the hot-path cost this cache exists to avoid.
/// Scoped by tenant as well as id.
///
/// `resource_groups.id` is the primary key, so an unscoped lookup cannot
/// actually return another tenant's row by accident; you would need that
/// tenant's exact group id. The filter is here because every other read of
/// this table carries one, and a query that is correct only because of a
/// uniqueness property elsewhere in the schema is one schema change away from
/// being wrong. Callers already hold the tenant.
/// The run-cap verdict for any surface — LLM preflight and every action
/// surface reach this one function (through `run_controls::check`, which
/// pairs it with the abort check), so they cannot disagree about what a
/// run's age is. The ceiling guards ACTIONS as well as
/// inference — the ceiling that matters to a runaway is on what it *does*.
///
/// ## Time parked on a human is not runtime
///
/// The duration ceiling bounds the AGENT's own execution. A call held for
/// approval is retried on the same run once a person decides, possibly hours
/// later; counting that wait would refuse the approved retry for having been
/// approved slowly. So `usage.age_secs` is already net of the pause the run
/// clock knows about, and when the duration term would refuse, the pause is
/// reconciled from `tool_approval_requests` first — the retry that follows an
/// approval reaches this gate BEFORE its token is redeemed, so the clock
/// cannot have heard about that hold yet. The reconcile costs one indexed
/// query and only runs on that would-refuse path.
///
/// A failed reconcile keeps the refusal: the gate is no weaker than it was
/// before pauses existed, and a runaway guard should not relax on a
/// database error.
pub async fn check_run_caps(
    state: &crate::services::SharedState,
    tenant_id: &str,
    group_id: &str,
    run_id: &str,
    delegation_depth: u32,
) -> Option<RunCapBlock> {
    let caps = match caps_for_group(&state.db, tenant_id, group_id).await {
        Ok(c) => c,
        Err(e) => return Some(caps_unavailable(tenant_id, group_id, &e)),
    };
    if !caps.has_read_terms() {
        return None;
    }
    let usage = match state.usage_state.run_usage(tenant_id, run_id).await {
        Ok(u) => u,
        Err(e) => {
            tracing::error!(error = %e, tenant_id, run_id, "run usage unreadable; refusing (fail closed)");
            return Some(RunCapBlock {
                term: TERM_RUN_USAGE_UNAVAILABLE,
                limit: 0.0,
                observed: 0.0,
                message: "Refused: this run's usage cannot be read, so its caps cannot be checked"
                    .to_string(),
            });
        }
    };
    let block = evaluate(&caps, &usage, delegation_depth)?;
    if block.term != TERM_MAX_DURATION_PER_RUN {
        return Some(block);
    }
    match sync_run_pause(state, tenant_id, run_id, usage.started_ms, None).await {
        Some(paused_ms) => {
            let usage = usage.with_paused(paused_ms, now_epoch_ms());
            evaluate(&caps, &usage, delegation_depth)
        }
        None => Some(block),
    }
}

/// Recompute how long this run has been parked on a human since its clock
/// started, from `tool_approval_requests`, and record it on the run clock.
///
/// `started_ms` is the clock's start (epoch ms) when the caller already holds
/// it; `None` reads it (without starting a clock). `keep_alive_secs` extends
/// the run's keys — pass the hold window when one was just raised.
///
/// Returns the merged pause, or `None` when there is no clock to pause or the
/// lookup failed.
pub async fn sync_run_pause(
    state: &crate::services::SharedState,
    tenant_id: &str,
    run_id: &str,
    started_ms: i64,
    keep_alive_secs: Option<u64>,
) -> Option<i64> {
    sync_run_pause_with(
        &state.db,
        &state.usage_state,
        tenant_id,
        run_id,
        Some(started_ms).filter(|t| *t > 0),
        keep_alive_secs,
    )
    .await
}

/// [`sync_run_pause`] for callers that hold the pool and the usage state
/// rather than the whole shared state (the approval lifecycle).
pub async fn sync_run_pause_with(
    db: &sqlx::PgPool,
    usage_state: &UsageState,
    tenant_id: &str,
    run_id: &str,
    started_ms: Option<i64>,
    keep_alive_secs: Option<u64>,
) -> Option<i64> {
    let started_ms = match started_ms {
        Some(t) => t,
        None => usage_state.run_started_ms(tenant_id, run_id).await?,
    };
    let from = chrono::DateTime::<chrono::Utc>::from_timestamp_millis(started_ms)?;
    let now = chrono::Utc::now();
    match crate::services::run_ledger::held_ms_between(db, tenant_id, run_id, from, now).await {
        Ok(held) => {
            usage_state
                .run_record_paused(
                    tenant_id,
                    run_id,
                    held,
                    keep_alive_secs.unwrap_or(RUN_TTL_SECS),
                )
                .await;
            Some(held)
        }
        Err(e) => {
            tracing::warn!(run_id, error = %e, "run hold lookup failed; run clock not paused");
            None
        }
    }
}

/// The run caps a group declares.
///
/// `Err` only when they cannot be read and nothing is cached: a failed lookup
/// used to read as "no caps" — and was CACHED, so every run in the group went
/// uncapped for the TTL after a database blip. A stale entry is served as the
/// last known good set; with none, the caller fails closed.
pub async fn caps_for_group(
    pool: &sqlx::PgPool,
    tenant_id: &str,
    group_id: &str,
) -> Result<RunCaps, sqlx::Error> {
    if let Some(hit) = cache().get(group_id) {
        if hit.at.elapsed() < CAPS_TTL {
            return Ok(hit.caps);
        }
    }

    let row: Result<Option<(Option<Value>,)>, _> = sqlx::query_as(
        "SELECT operating_envelope FROM resource_groups WHERE id = $1 AND tenant_id = $2",
    )
    .bind(group_id)
    .bind(tenant_id)
    .fetch_optional(pool)
    .await;

    let caps = match row {
        Ok(Some((envelope,))) => RunCaps::from_envelope(envelope.as_ref()),
        Ok(None) => RunCaps::default(),
        Err(e) => {
            return match cache().get(group_id) {
                Some(stale) => {
                    tracing::error!(group_id, error = %e, "run caps lookup failed; enforcing the last known caps");
                    Ok(stale.caps)
                }
                None => Err(e),
            };
        }
    };
    cache().insert(
        group_id.to_string(),
        CachedCaps {
            caps,
            at: Instant::now(),
        },
    );
    Ok(caps)
}

/// Drop a group's cached caps — called when the control plane signals a
/// config change, so a tightened ceiling takes effect without waiting out
/// the TTL.
pub fn invalidate(group_id: &str) {
    cache().remove(group_id);
}

pub fn invalidate_all() {
    cache().clear();
}

// ---------------------------------------------------------------------------
// Admission
// ---------------------------------------------------------------------------
//
// `check_run_caps` reads the run's counters and the commit used to add to
// them after the call COMPLETED, so every call that arrived before the first
// one finished read "under the cap". Proven: with max_llm_calls_per_run = 3,
// 15 parallel calls on one run all reached the upstream, and so did 16 split
// across two replicas sharing Redis.
//
// The run's counted terms (calls, tokens, cost) are now dimensions of the
// call's ONE admission (`limits::admit`), judged against the call's worst
// case together with every other limit, and settled with the call's hold —
// so the run's totals carry exactly what each call used, in place of what it
// reserved, the moment it reports. `check_run_caps` still judges depth and
// duration (they need no counter) and gives the read-only verdict.

/// The refusal when a group's caps cannot be read (fail closed).
fn caps_unavailable(tenant_id: &str, group_id: &str, e: &sqlx::Error) -> RunCapBlock {
    tracing::error!(tenant_id, group_id, error = %e, "run caps unreadable; refusing (fail closed)");
    RunCapBlock {
        term: TERM_RUN_USAGE_UNAVAILABLE,
        limit: 0.0,
        observed: 0.0,
        message: "Refused: this run's caps cannot be read, so they cannot be checked".to_string(),
    }
}

/// The run's counted terms as admission dimensions: every term is accounted
/// (the run ledger and the duration clock read these counters), and limited
/// where the group's envelope caps it.
pub fn run_dims_for(caps: &RunCaps, tenant_id: &str, run_id: &str) -> Vec<LimitDim> {
    use crate::services::cache::usage_state::{Dim, Measure, RUN_TTL_SECS, UsageState};
    use crate::services::limits::{DimRefusal, Unit};
    [
        (
            "calls",
            caps.max_calls,
            Measure::One,
            TERM_MAX_CALLS_PER_RUN,
            Unit::Count,
        ),
        (
            "tokens",
            caps.max_tokens,
            Measure::TotalTokens,
            TERM_MAX_TOKENS_PER_RUN,
            Unit::Tokens,
        ),
        (
            "cost",
            caps.max_cost_usd
                .map(|usd| (usd * 1_000_000.0).round() as i64),
            Measure::CostMicros,
            TERM_MAX_COST_PER_RUN,
            Unit::Micros,
        ),
    ]
    .into_iter()
    .map(|(field, limit, measure, term, unit)| LimitDim {
        dim: Dim::counter(
            UsageState::run_counter_key(tenant_id, run_id, field),
            limit,
            RUN_TTL_SECS,
            measure,
        ),
        refusal: match limit {
            Some(limit) => DimRefusal::Run { term, limit, unit },
            None => DimRefusal::Accounting,
        },
    })
    .collect()
}

/// The run's action counter as an admission dimension, when the group's
/// envelope caps it: one unit for a governed step that is not a model call.
///
/// `None` without a ceiling. Unlike the model-call terms, which every model
/// call accounts whether capped or not, an uncapped run's actions are not
/// counted: an action surface has no other run counter to admit, so
/// accounting alone would cost every tool call a round trip to the shared
/// counters. The count therefore starts when the ceiling is declared.
pub fn action_dim_for(caps: &RunCaps, tenant_id: &str, run_id: &str) -> Option<LimitDim> {
    use crate::services::cache::usage_state::{Dim, Measure, RUN_TTL_SECS, UsageState};
    use crate::services::limits::{DimRefusal, Unit};
    let limit = caps.max_actions?;
    Some(LimitDim {
        dim: Dim::counter(
            UsageState::run_counter_key(tenant_id, run_id, RUN_FIELD_ACTIONS),
            Some(limit),
            RUN_TTL_SECS,
            Measure::One,
        ),
        refusal: DimRefusal::Run {
            term: TERM_MAX_ACTIONS_PER_RUN,
            limit,
            unit: Unit::Count,
        },
    })
}

/// The limit on new runs as an admission dimension, for a step of `run_id`
/// whose run `starter` would be starting if it is new
/// (`run_controls::run_starter`). `None` when the group declares no limit.
pub fn new_runs_dim_for(
    caps: &RunCaps,
    tenant_id: &str,
    starter: &str,
    run_id: &str,
) -> Option<LimitDim> {
    let limit = caps.new_runs?;
    Some(crate::services::limits::new_runs_dim(
        tenant_id,
        starter,
        run_id,
        limit.max,
        limit.window_secs,
        TERM_MAX_NEW_RUNS_PER_WINDOW,
    ))
}

/// The field of a run's action counter (`UsageState::run_counter_key`).
pub const RUN_FIELD_ACTIONS: &str = "actions";

/// [`run_dims_for`] with the group's caps. `Err` when the caps cannot be
/// read (fail closed — `caps_for_group` serves a stale cache before it
/// errors).
pub async fn run_dims(
    pool: &sqlx::PgPool,
    tenant_id: &str,
    group_id: &str,
    run_id: &str,
) -> Result<Vec<LimitDim>, sqlx::Error> {
    let caps = caps_for_group(pool, tenant_id, group_id).await?;
    Ok(run_dims_for(&caps, tenant_id, run_id))
}

/// The operator-facing block for a run term refused at admission. The
/// figures are in the term's unit (micro-USD for cost).
pub fn refusal_block(term: &'static str, limit: i64, current: f64) -> RunCapBlock {
    if term == TERM_MAX_ACTIONS_PER_RUN {
        // Every admitted action is one unit, so the counter stands at the
        // ceiling when it refuses: what the run has taken, or holds for
        // steps in flight.
        let taken = current.max(limit as f64) as i64;
        return RunCapBlock {
            term,
            limit: limit as f64,
            observed: current,
            message: format!(
                "This run has taken {taken} governed action(s) that are not model calls, \
                 reaching its ceiling of {limit} ({TERM_MAX_ACTIONS_PER_RUN}; steps in flight \
                 are counted)."
            ),
        };
    }
    let (caps, usage) = match term {
        TERM_MAX_CALLS_PER_RUN => (
            RunCaps {
                max_calls: Some(limit),
                ..Default::default()
            },
            RunUsage {
                calls: current.max(limit as f64) as i64,
                ..Default::default()
            },
        ),
        TERM_MAX_TOKENS_PER_RUN => (
            RunCaps {
                max_tokens: Some(limit),
                ..Default::default()
            },
            RunUsage {
                tokens: current.max(limit as f64) as i64,
                ..Default::default()
            },
        ),
        _ => (
            RunCaps {
                max_cost_usd: Some(limit as f64 / 1_000_000.0),
                ..Default::default()
            },
            RunUsage {
                cost_usd: current.max(limit as f64) / 1_000_000.0,
                ..Default::default()
            },
        ),
    };
    let mut block = evaluate(&caps, &usage, 0).unwrap_or(RunCapBlock {
        term,
        limit: limit as f64,
        observed: current,
        message: String::new(),
    });
    // What the run had reached, and that this call's worst case did not fit.
    block.observed = match term {
        TERM_MAX_COST_PER_RUN => current / 1_000_000.0,
        _ => current,
    };
    block.message.push_str(
        " (counting what this run's calls in flight have reserved, this call's worst case does not fit)",
    );
    block
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn usage(calls: i64, tokens: i64, cost_usd: f64, age_secs: i64) -> RunUsage {
        RunUsage {
            calls,
            tokens,
            cost_usd,
            age_secs,
            ..Default::default()
        }
    }

    // -- parsing ---------------------------------------------------------

    #[test]
    fn an_absent_envelope_declares_nothing() {
        assert!(RunCaps::from_envelope(None).is_empty());
        assert!(RunCaps::from_envelope(Some(&json!(null))).is_empty());
        assert!(RunCaps::from_envelope(Some(&json!({}))).is_empty());
    }

    #[test]
    fn only_declared_terms_become_caps() {
        let caps = RunCaps::from_envelope(Some(&json!({
            "max_llm_calls_per_run": 25,
            "min_completion_rate": 0.9
        })));
        assert_eq!(caps.max_calls, Some(25));
        assert_eq!(caps.max_tokens, None);
        assert_eq!(caps.max_cost_usd, None);
    }

    #[test]
    fn every_term_parses() {
        let caps = RunCaps::from_envelope(Some(&json!({
            "max_cost_per_run_usd": 1.25,
            "max_llm_calls_per_run": 40,
            "max_tokens_per_run": 500_000,
            "max_delegation_depth_per_run": 3,
            "max_duration_seconds_per_run": 900
        })));
        assert_eq!(caps.max_cost_usd, Some(1.25));
        assert_eq!(caps.max_calls, Some(40));
        assert_eq!(caps.max_tokens, Some(500_000));
        assert_eq!(caps.max_depth, Some(3));
        assert_eq!(caps.max_duration_secs, Some(900));
        assert!(!caps.is_empty());
    }

    #[test]
    fn a_malformed_bound_is_undeclared_never_a_zero_cap() {
        // A cap of zero would refuse every call on the system. Garbage in
        // the envelope must fail open, not brick the system.
        for bad in [
            json!({"max_llm_calls_per_run": 0}),
            json!({"max_llm_calls_per_run": -5}),
            json!({"max_llm_calls_per_run": "twenty"}),
            json!({"max_llm_calls_per_run": null}),
        ] {
            let caps = RunCaps::from_envelope(Some(&bad));
            assert!(caps.is_empty(), "{bad} should declare no cap");
        }
    }

    #[test]
    fn the_action_ceiling_parses_beside_the_model_call_ceiling() {
        let caps = RunCaps::from_envelope(Some(&json!({
            "max_llm_calls_per_run": 25,
            "max_actions_per_run": 40
        })));
        assert_eq!(caps.max_calls, Some(25));
        assert_eq!(caps.max_actions, Some(40));
        let only = RunCaps::from_envelope(Some(&json!({"max_actions_per_run": 3})));
        assert_eq!(only.max_actions, Some(3));
        assert_eq!(only.max_calls, None, "it is not the model-call ceiling");
        assert!(!only.is_empty(), "a group that caps only actions has caps");
        assert!(
            !only.has_read_terms(),
            "the action ceiling is admitted: nothing of the run is read for it"
        );
    }

    #[test]
    fn a_malformed_action_ceiling_is_undeclared_like_its_siblings() {
        for bad in [
            json!({"max_actions_per_run": 0}),
            json!({"max_actions_per_run": -5}),
            json!({"max_actions_per_run": "twenty"}),
            json!({"max_actions_per_run": null}),
            json!({"max_actions_per_run": [3]}),
        ] {
            let caps = RunCaps::from_envelope(Some(&bad));
            assert!(caps.is_empty(), "{bad} should declare no cap");
            assert!(action_dim_for(&caps, "t", "r").is_none());
        }
    }

    #[test]
    fn the_action_dimension_is_one_unit_on_the_runs_own_counter() {
        use crate::services::cache::usage_state::{DimKind, Measure};
        use crate::services::limits::{DimRefusal, Unit};
        assert!(action_dim_for(&RunCaps::default(), "t", "r").is_none());
        let caps = RunCaps {
            max_actions: Some(7),
            max_calls: Some(2),
            ..Default::default()
        };
        let d = action_dim_for(&caps, "t-1", "run-1").expect("a capped run has the dimension");
        assert_eq!(d.dim.measure, Measure::One);
        match &d.dim.kind {
            DimKind::Counter { key, limit, .. } => {
                assert_eq!(key, "brutor:run:t-1:run-1:actions");
                assert_eq!(*limit, Some(7));
            }
            other => panic!("not a counter: {other:?}"),
        }
        assert_eq!(
            d.refusal,
            DimRefusal::Run {
                term: TERM_MAX_ACTIONS_PER_RUN,
                limit: 7,
                unit: Unit::Count
            }
        );
        // The model-call dimensions do not move: an action is not a call.
        let model: Vec<String> = run_dims_for(&caps, "t-1", "run-1")
            .iter()
            .map(|d| d.dim.kind.key().to_string())
            .collect();
        assert!(model.iter().all(|k| !k.ends_with(":actions")), "{model:?}");
    }

    #[test]
    fn an_action_refusal_names_its_own_term() {
        let b = refusal_block(TERM_MAX_ACTIONS_PER_RUN, 5, 5.0);
        assert_eq!(b.term, TERM_MAX_ACTIONS_PER_RUN);
        assert_eq!((b.limit, b.observed), (5.0, 5.0));
        assert!(b.message.contains("max_actions_per_run"), "{}", b.message);
        assert!(b.message.contains("ceiling of 5"), "{}", b.message);
        assert!(!b.message.contains("model call(s)"), "{}", b.message);
    }

    #[test]
    fn the_limit_on_new_runs_parses_with_its_window() {
        let caps = RunCaps::from_envelope(Some(&json!({
            "max_new_runs_per_window": 50,
            "new_runs_window_seconds": 600
        })));
        assert_eq!(
            caps.new_runs,
            Some(NewRunLimit {
                max: 50,
                window_secs: 600
            })
        );
        // No window declared: an hour.
        let caps = RunCaps::from_envelope(Some(&json!({"max_new_runs_per_window": 5})));
        assert_eq!(
            caps.new_runs,
            Some(NewRunLimit {
                max: 5,
                window_secs: DEFAULT_NEW_RUNS_WINDOW_SECS
            })
        );
        // It is not a ceiling of one run: nothing of a run is read or
        // counted for it.
        assert!(caps.is_empty() && !caps.has_read_terms());
        assert!(
            run_dims_for(&caps, "t", "r")
                .iter()
                .all(|d| { matches!(d.refusal, crate::services::limits::DimRefusal::Accounting) })
        );
        assert!(action_dim_for(&caps, "t", "r").is_none());
    }

    #[test]
    fn a_malformed_limit_on_new_runs_is_undeclared_and_a_malformed_window_the_default() {
        for bad in [
            json!({"max_new_runs_per_window": 0}),
            json!({"max_new_runs_per_window": -1}),
            json!({"max_new_runs_per_window": "many"}),
            json!({"max_new_runs_per_window": null}),
            // A window alone limits nothing.
            json!({"new_runs_window_seconds": 60}),
        ] {
            let caps = RunCaps::from_envelope(Some(&bad));
            assert_eq!(caps.new_runs, None, "{bad}");
            assert!(new_runs_dim_for(&caps, "t", "key:k", "r").is_none());
        }
        for (window, secs) in [
            (json!(0), DEFAULT_NEW_RUNS_WINDOW_SECS),
            (json!(-60), DEFAULT_NEW_RUNS_WINDOW_SECS),
            (json!("soon"), DEFAULT_NEW_RUNS_WINDOW_SECS),
            (json!(null), DEFAULT_NEW_RUNS_WINDOW_SECS),
            (json!(86_400), 86_400),
            (json!(0.4), 1),
            (json!(1e12), MAX_NEW_RUNS_WINDOW_SECS),
        ] {
            let caps = RunCaps::from_envelope(Some(&json!({
                "max_new_runs_per_window": 3,
                "new_runs_window_seconds": window
            })));
            assert_eq!(
                caps.new_runs,
                Some(NewRunLimit {
                    max: 3,
                    window_secs: secs
                }),
                "{window}"
            );
        }
    }

    #[test]
    fn the_new_run_dimension_is_the_set_of_the_credentials_runs_in_the_window() {
        use crate::services::cache::usage_state::DimKind;
        let caps = RunCaps::from_envelope(Some(&json!({
            "max_new_runs_per_window": 4,
            "new_runs_window_seconds": 120
        })));
        let d = new_runs_dim_for(&caps, "t-1", "agent:a1", "run-9").expect("declared");
        match &d.dim.kind {
            DimKind::Members {
                key,
                member,
                max,
                ttl_secs,
            } => {
                assert!(key.starts_with("brutor:runs:t-1:agent:a1:120:"), "{key}");
                assert_eq!(member, "run-9");
                assert_eq!((*max, *ttl_secs), (4, 240));
            }
            other => panic!("not a member set: {other:?}"),
        }
        // Another credential, and another tenant, have their own set.
        let key_of = |tenant: &str, starter: &str| {
            new_runs_dim_for(&caps, tenant, starter, "run-9")
                .unwrap()
                .dim
                .kind
                .key()
                .to_string()
        };
        assert_ne!(key_of("t-1", "agent:a1"), key_of("t-1", "key:k1"));
        assert_ne!(key_of("t-1", "agent:a1"), key_of("t-2", "agent:a1"));
    }

    #[test]
    fn the_averaging_term_is_not_a_run_cap() {
        // `max_avg_cost_per_completed_task_usd` is graded over the window.
        // Enforcing a mean as a per-run ceiling would refuse calls on a
        // system that is behaving exactly as its envelope allows.
        let caps =
            RunCaps::from_envelope(Some(&json!({"max_avg_cost_per_completed_task_usd": 0.05})));
        assert!(caps.is_empty());
    }

    // -- the decision table ----------------------------------------------

    #[test]
    fn no_caps_never_blocks() {
        let caps = RunCaps::default();
        assert!(evaluate(&caps, &usage(9_999, 9_999_999, 500.0, 99_999), 31).is_none());
    }

    #[test]
    fn calls_block_on_reaching_the_ceiling_not_after_exceeding_it() {
        let caps = RunCaps {
            max_calls: Some(3),
            ..Default::default()
        };
        // Two calls made: the third is still allowed.
        assert!(evaluate(&caps, &usage(2, 0, 0.0, 0), 0).is_none());
        // Three made: the fourth is refused. Blocking at >= is what makes
        // the ceiling the number of calls actually permitted.
        let block = evaluate(&caps, &usage(3, 0, 0.0, 0), 0).expect("must block");
        assert_eq!(block.term, TERM_MAX_CALLS_PER_RUN);
        assert_eq!(block.limit, 3.0);
        assert_eq!(block.observed, 3.0);
    }

    #[test]
    fn tokens_block_independently_of_call_count() {
        // The failure a call-count cap misses: few calls, exploding context.
        let caps = RunCaps {
            max_calls: Some(100),
            max_tokens: Some(50_000),
            ..Default::default()
        };
        let block = evaluate(&caps, &usage(4, 50_000, 0.0, 0), 0).expect("must block");
        assert_eq!(block.term, TERM_MAX_TOKENS_PER_RUN);
    }

    #[test]
    fn cost_blocks_on_the_run_not_on_an_average() {
        let caps = RunCaps {
            max_cost_usd: Some(0.50),
            ..Default::default()
        };
        assert!(evaluate(&caps, &usage(1, 0, 0.49, 0), 0).is_none());
        let block = evaluate(&caps, &usage(1, 0, 0.50, 0), 0).expect("must block");
        assert_eq!(block.term, TERM_MAX_COST_PER_RUN);
    }

    #[test]
    fn depth_blocks_beyond_the_ceiling_and_needs_no_counter() {
        let caps = RunCaps {
            max_depth: Some(2),
            ..Default::default()
        };
        // Hop 2 is the last permitted one.
        assert!(evaluate(&caps, &usage(0, 0, 0.0, 0), 2).is_none());
        let block = evaluate(&caps, &usage(0, 0, 0.0, 0), 3).expect("must block");
        assert_eq!(block.term, TERM_MAX_DEPTH_PER_RUN);
        assert_eq!(block.observed, 3.0);
    }

    #[test]
    fn duration_blocks_the_stall_that_burns_nothing() {
        let caps = RunCaps {
            max_duration_secs: Some(600),
            ..Default::default()
        };
        // No calls, no tokens, no spend — only elapsed time.
        let block = evaluate(&caps, &usage(0, 0, 0.0, 601), 0).expect("must block");
        assert_eq!(block.term, TERM_MAX_DURATION_PER_RUN);
    }

    #[test]
    fn time_parked_on_a_human_is_not_run_age() {
        // A decision approved 13 h after it was held: 41 s of the
        // agent's own time, 13 h of a person's. The ceiling bounds the first.
        let caps = RunCaps {
            max_duration_secs: Some(300),
            ..Default::default()
        };
        let now = 2_000_000_000_000_i64;
        let elapsed = usage(0, 0, 0.0, 46_841);
        let elapsed = RunUsage {
            started_ms: now - 46_841_000,
            ..elapsed
        };
        assert!(
            evaluate(&caps, &elapsed, 0).is_some(),
            "on elapsed time the approved retry is refused — the defect"
        );
        let active = elapsed.with_paused(46_800_000, now);
        assert_eq!(active.age_secs, 41);
        assert!(evaluate(&caps, &active, 0).is_none());
        // Pause only grows: a stale, smaller figure never shrinks it back.
        assert_eq!(active.with_paused(1_000, now).paused_ms, 46_800_000);
    }

    #[test]
    fn a_runaway_with_no_hold_is_still_refused_after_reconcile() {
        let caps = RunCaps {
            max_duration_secs: Some(300),
            ..Default::default()
        };
        let now = 2_000_000_000_000_i64;
        let u = RunUsage {
            started_ms: now - 400_000,
            ..usage(0, 0, 0.0, 400)
        }
        .with_paused(0, now);
        assert_eq!(u.age_secs, 400);
        assert!(evaluate(&caps, &u, 0).is_some());
    }

    #[test]
    fn every_block_explains_itself() {
        let caps = RunCaps {
            max_cost_usd: Some(1.0),
            max_calls: Some(1),
            max_tokens: Some(1),
            max_actions: None,
            new_runs: None,
            max_depth: Some(1),
            max_duration_secs: Some(1),
        };
        for (u, d) in [
            (usage(5, 0, 0.0, 0), 0),
            (usage(0, 5, 0.0, 0), 0),
            (usage(0, 0, 5.0, 0), 0),
            (usage(0, 0, 0.0, 0), 5),
            (usage(0, 0, 0.0, 5), 0),
        ] {
            let block = evaluate(&caps, &u, d).expect("must block");
            assert!(
                !block.message.is_empty(),
                "a refusal with no explanation is not actionable"
            );
            assert!(block.observed >= block.limit || block.term == TERM_MAX_DEPTH_PER_RUN);
        }
    }

    #[test]
    fn term_names_match_the_control_plane() {
        // One vocabulary, two consumers. A rename on either side that is not
        // mirrored here silently disables the cap, which is the worst
        // possible failure for a control an operator believes is armed.
        assert_eq!(TERM_MAX_COST_PER_RUN, "max_cost_per_run_usd");
        assert_eq!(TERM_MAX_CALLS_PER_RUN, "max_llm_calls_per_run");
        assert_eq!(TERM_MAX_TOKENS_PER_RUN, "max_tokens_per_run");
        assert_eq!(TERM_MAX_ACTIONS_PER_RUN, "max_actions_per_run");
        assert_eq!(TERM_MAX_NEW_RUNS_PER_WINDOW, "max_new_runs_per_window");
        assert_eq!(TERM_NEW_RUNS_WINDOW_SECONDS, "new_runs_window_seconds");
        assert_eq!(TERM_MAX_DEPTH_PER_RUN, "max_delegation_depth_per_run");
        assert_eq!(TERM_MAX_DURATION_PER_RUN, "max_duration_seconds_per_run");
    }
}
