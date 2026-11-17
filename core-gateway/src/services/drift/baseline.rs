// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Turning runs into "what normal looks like".
//!
//! ## What is deliberately excluded
//!
//! A baseline is only as honest as the runs it is built from, so three
//! kinds are left out:
//!
//! * **Replays** are simulations against recorded traffic. They
//!   never happened, and teaching the detector a shape that never happened
//!   is how you get drift alerts about a system that behaved normally.
//! * **Broken or partial chains** are runs we only partly observed.
//!   Folding a fragment into a baseline lowers the apparent step count and
//!   then flags every complete run as drift — the single most likely way
//!   this feature loses trust in week one.
//! * **Fragmented runs** (reopened more than the cap) for the same reason.
//!
//! ## Learning mode
//!
//! Below [`MIN_RUNS_FOR_BASELINE`] the baseline is `learning` and emits no
//! alerts at all. The UI says "ready in ~N runs" rather than showing a
//! confident-looking chart. Being honest that value lands in week three
//! beats emitting noise on day one — an operator paged by a baseline built
//! from eight runs does not give the feature a second chance.

//! ## Two bases
//!
//! A root system is baselined from the runs it roots. A shared service —
//! which only ever works *inside* other systems' runs — is baselined from
//! its **segments** (`ai_system_run_segments`), the per-system slices the
//! run ledger writes at finalisation. Both feed the same statistics through
//! one [`Sample`] shape; [`SampleSource`] is the only thing that differs.
//! A system that does both keeps both sets, keyed apart by `basis`.
//!
//! On the segment basis the aggregate over every caller
//! ([`ALL_CALLERS`]) is always kept, and a caller gets its own sub-baseline
//! only when it is at least [`MIN_CALLER_SHARE`] of the service's segments
//! in the window **and** meets [`MIN_RUNS_FOR_BASELINE`] on its own — see
//! [`cohorts`].

use std::borrow::Borrow;
use std::collections::BTreeMap;
use std::sync::LazyLock;

use chrono::{DateTime, Duration, Utc};
use serde_json::json;
use sqlx::{FromRow, PgPool};

use super::{
    BASELINE_WINDOW_DAYS, MAX_BASELINE_SAMPLES, MIN_CALLER_SHARE, MIN_RUNS_FOR_BASELINE,
    baseline_state, basis, exclusion, metric, profiles, stats,
};

/// Sentinel for the system-wide (not actor-scoped) baseline row.
///
/// Deliberately a string rather than SQL NULL. `actor_id` is part of the
/// table's uniqueness key, and SQL treats two NULLs as **distinct**, so a
/// NULL here would make `ON CONFLICT` never fire: every sweep would insert
/// a new row rather than update the existing one. At one sweep every 15
/// minutes that is ~96 duplicate baselines per metric per system per day,
/// and no single row is "the" baseline any more. `bucket` already uses the
/// same `'all'` convention.
///
/// Mirrored in the control plane as
/// `app/database/models/ai_system_drift.BASELINE_ALL_ACTORS`.
pub const ALL_ACTORS: &str = "all";

/// Sentinel for the aggregate-over-every-caller baseline row.
/// Not NULL for the same reason as [`ALL_ACTORS`]: it is part of the key.
///
/// Mirrored in the control plane as
/// `app/database/models/ai_system_drift.BASELINE_ALL_CALLERS`.
pub const ALL_CALLERS: &str = "all";

/// The aggregate bucket: every run except the no-work ones.
/// Every other bucket is a full declared outcome id. Mirrored in the control
/// plane's readers of `ai_system_baselines.bucket`.
pub const ALL_BUCKET: &str = "all";

/// Where a baseline's samples come from.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SampleSource {
    /// Runs the system roots (`ai_system_runs`).
    Runs,
    /// Segments the system serves inside other systems' runs
    /// (`ai_system_run_segments`).
    Segments,
}

impl SampleSource {
    pub const ALL: [SampleSource; 2] = [SampleSource::Runs, SampleSource::Segments];

    /// The `basis` column value.
    pub fn basis(self) -> &'static str {
        match self {
            SampleSource::Runs => basis::RUNS,
            SampleSource::Segments => basis::SEGMENTS,
        }
    }

    fn sql(self) -> &'static str {
        match self {
            SampleSource::Runs => SAMPLES_SQL.as_str(),
            SampleSource::Segments => SEGMENT_SAMPLES_SQL.as_str(),
        }
    }
}

/// One unit of work — a run, or a served call (segment) — reduced to the
/// values a baseline cares about.
///
/// Fields only one basis carries are `Option`: the other basis's query
/// selects a typed NULL, and [`series_for`] skips it, so a metric a basis
/// cannot measure yields no samples rather than a series of zeros.
#[derive(Debug, Clone, FromRow)]
pub struct Sample {
    /// Run start, or the segment's first action.
    pub started_at: DateTime<Utc>,
    pub action_count: i32,
    /// The ACTIVE duration — elapsed less time parked on a human
    /// approval (runs: `active_duration_ms`; segments: `active_ms`). This is
    /// what [`metric::DURATION_MS`] baselines: a hold approved hours later is
    /// not the system getting slower, and a baseline learned from elapsed
    /// time would flag every slow approver as drift.
    pub active_duration_ms: i32,
    pub tool_call_count: i32,
    /// Segments only.
    pub llm_call_count: Option<i32>,
    /// Segments only.
    pub total_tokens: Option<i64>,
    /// Segments only.
    pub error_count: Option<i32>,
    /// Runs only.
    pub max_delegation_depth: Option<i32>,
    pub cost_usd: f64,
    pub tool_error_count: i32,
    /// Run `terminal_state`, or segment `outcome`.
    pub terminal_state: String,
    pub tool_set_signature: Option<String>,
    /// Runs only.
    pub first_prompt_tokens: Option<i32>,
    /// Runs only.
    pub input_bytes: Option<i32>,
    /// Runs only; `None` when no call in the run reported usage.
    pub peak_context_tokens: Option<i32>,
    /// Runs only; `None` when no call in the run reported usage.
    pub context_growth: Option<f64>,
    /// Runs only.
    pub client_label: Option<String>,
    /// Runs: `[{"model", "calls"}]`; segments: `{"<model>": calls}`.
    /// [`distribution_for`] reads both.
    pub models_used: Option<serde_json::Value>,
    /// Segments only: the IMMEDIATE calling AI System
    /// (`ai_system_run_segments.caller_system_id`) — the caller a
    /// per-caller baseline is keyed on. In A→B→C, C's caller is B, never the
    /// run's root A. NULL (caller in no AI System) counts only toward the
    /// aggregate.
    pub caller_system_id: Option<String>,
    /// `true` for a run, `false` for a segment.
    pub is_run: bool,
    /// The reported outcome (runs: the run's; segments: the delegate's).
    pub outcome_id: Option<String>,
    pub outcome_conformance: Option<String>,
    /// Runs only.
    pub outcome_counts_toward_baseline: Option<bool>,
    pub error_cause: Option<String>,
    /// Runs only.
    pub run_id: Option<String>,
    /// Runs only; `None` on a run that recorded none.
    pub tools_used: Option<serde_json::Value>,
}

impl Sample {
    /// The run's class, from the ONE classification
    /// (`run_outcome::run_class`). `None` for a segment: a served call has no
    /// terminal state of its own.
    pub fn run_class(&self) -> Option<&'static str> {
        self.is_run.then(|| {
            crate::services::run_outcome::run_class(
                &self.terminal_state,
                self.outcome_conformance.as_deref(),
                self.outcome_counts_toward_baseline,
            )
        })
    }

    fn is_success(&self) -> bool {
        self.run_class() == Some(crate::services::run_outcome::run_class::SUCCESS)
    }
}

/// Load the runs a baseline may legitimately be built from.
///
/// The `WHERE` clause here is load-bearing — see the module docs on what
/// is excluded and why.
///
/// The inner query takes the **newest** [`MAX_BASELINE_SAMPLES`] eligible
/// runs and the outer one puts them back in ascending order, because every
/// consumer downstream (`series_for`, the change-point walk, the
/// observation-window suffix in the sweeper) assumes oldest-first.
///
/// The cap is a deliberate exactness-for-cost trade. Without it a system
/// doing 50k runs/day pulls ~700k rows — each carrying a `models_used`
/// JSON value — into the proxy process every 15 minutes, next to the
/// request path. Above the cap the median/MAD are computed from the most
/// recent N runs rather than the full 14 days: for a system whose
/// behaviour is stable that is the same distribution, and for one that is
/// drifting the recent slice is the more honest baseline anyway. What it
/// does mean is that on a very busy system the baseline window is
/// effectively shorter than [`super::BASELINE_WINDOW_DAYS`] — worth
/// knowing before reading a sample_count that stops at the cap.
///
/// Samples inside a declared incident window are excluded here, by
/// the shared [`exclusion::incident_window_predicate`] (`{excl}`). Baseline
/// epochs are per metric and are applied afterwards by
/// [`exclusion::MetricFloors::slice`].
pub(crate) const SAMPLES_SQL_TEMPLATE: &str = r#"
SELECT * FROM (
    SELECT
        started_at,
        action_count,
        active_duration_ms,
        tool_call_count,
        NULL::integer AS llm_call_count,
        NULL::bigint AS total_tokens,
        NULL::integer AS error_count,
        max_delegation_depth,
        CAST(total_cost_usd AS DOUBLE PRECISION) AS cost_usd,
        tool_error_count,
        terminal_state,
        tool_set_signature,
        first_prompt_tokens,
        input_bytes,
        peak_context_tokens,
        context_growth,
        client_label,
        models_used,
        NULL::text AS caller_system_id,
        TRUE AS is_run,
        outcome_id,
        outcome_conformance,
        outcome_counts_toward_baseline,
        error_cause,
        id AS run_id,
        tools_used
    FROM ai_system_runs
    WHERE tenant_id = $1
      AND ai_system_id = $2
      AND started_at >= $3
      AND started_at < $4
      AND NOT is_replay
      AND chain_integrity IN ('intact', 'client_asserted', 'observed_external')
      AND reopened_count <= $5
      AND {excl}
    ORDER BY started_at DESC
    LIMIT $6
) recent
ORDER BY started_at ASC
"#;

static SAMPLES_SQL: LazyLock<String> = LazyLock::new(|| {
    SAMPLES_SQL_TEMPLATE.replace(
        "{excl}",
        &exclusion::incident_window_predicate("$1", "started_at", &["$2"]),
    )
});

/// The segment counterpart of [`SAMPLES_SQL`].
///
/// Same window, same cap, same ordering — and the **same honesty clauses**,
/// applied to the run the segment belongs to: a segment of a replay, of a
/// run whose chain could not be trusted, or of a run stitched back together
/// past the cap is a fragment of a fragment. (Replays write no segments
/// today; the clause keeps that true if one ever does.) The run-side
/// columns are unqualified on purpose — they exist only on
/// `ai_system_runs`, and the fragments stay textually identical to the
/// run query so the guards below pin both.
pub(crate) const SEGMENT_SAMPLES_SQL_TEMPLATE: &str = r#"
SELECT * FROM (
    SELECT
        s.first_action_at AS started_at,
        s.action_count,
        s.active_ms AS active_duration_ms,
        s.tool_call_count,
        s.llm_call_count,
        s.total_tokens,
        s.error_count,
        NULL::integer AS max_delegation_depth,
        CAST(s.cost_usd AS DOUBLE PRECISION) AS cost_usd,
        s.tool_error_count,
        s.outcome AS terminal_state,
        s.tool_set_signature,
        NULL::integer AS first_prompt_tokens,
        NULL::integer AS input_bytes,
        NULL::integer AS peak_context_tokens,
        NULL::double precision AS context_growth,
        NULL::text AS client_label,
        s.models_used,
        s.caller_system_id,
        FALSE AS is_run,
        s.outcome_id,
        s.outcome_conformance,
        NULL::boolean AS outcome_counts_toward_baseline,
        s.error_cause,
        NULL::varchar AS run_id,
        NULL::jsonb AS tools_used
    FROM ai_system_run_segments s
    JOIN ai_system_runs r ON r.id = s.run_id
    WHERE s.tenant_id = $1
      AND s.segment_system_id = $2
      AND s.first_action_at >= $3
      AND s.first_action_at < $4
      AND NOT is_replay
      AND chain_integrity IN ('intact', 'client_asserted', 'observed_external')
      AND reopened_count <= $5
      -- A delegation stopped by a hold for a person is
      -- governance waiting on someone, never the service's work.
      AND s.held_approval_request_id IS NULL
      AND {excl}
    ORDER BY s.first_action_at DESC
    LIMIT $6
) recent
ORDER BY started_at ASC
"#;

static SEGMENT_SAMPLES_SQL: LazyLock<String> = LazyLock::new(|| {
    SEGMENT_SAMPLES_SQL_TEMPLATE.replace(
        "{excl}",
        // A window on the service OR on the run's root covers the call.
        &exclusion::incident_window_predicate(
            "$1",
            "s.first_action_at",
            &["$2", "s.root_system_id"],
        ),
    )
});

pub async fn load_samples(
    db: &PgPool,
    source: SampleSource,
    tenant_id: &str,
    ai_system_id: &str,
    from: DateTime<Utc>,
    to: DateTime<Utc>,
    max_reopens: i32,
) -> Vec<Sample> {
    match sqlx::query_as::<_, Sample>(source.sql())
        .bind(tenant_id)
        .bind(ai_system_id)
        .bind(from)
        .bind(to)
        .bind(max_reopens)
        .bind(MAX_BASELINE_SAMPLES)
        .fetch_all(db)
        .await
    {
        Ok(rows) => rows,
        Err(e) => {
            // Quiet-but-logged: an empty sample set is a learning baseline,
            // which is safe, but a schema mismatch must not be invisible.
            tracing::warn!(error = %e, basis = source.basis(), "drift: sample query failed");
            Vec::new()
        }
    }
}

/// One population a baseline set is computed over: the whole basis, or —
/// on the segment basis — one qualifying caller's share of it.
#[derive(Debug, Clone)]
pub struct Cohort<'a> {
    /// [`ALL_CALLERS`] for the aggregate; otherwise the caller's AI System id.
    pub caller_system_id: String,
    /// Oldest-first, like the samples they are drawn from.
    pub samples: Vec<&'a Sample>,
}

impl Cohort<'_> {
    pub fn is_aggregate(&self) -> bool {
        self.caller_system_id == ALL_CALLERS
    }

    /// The drift event's `caller_system_id`: NULL for the aggregate.
    pub fn event_caller(&self) -> Option<&str> {
        (!self.is_aggregate()).then_some(self.caller_system_id.as_str())
    }
}

/// The callers of a service that earn a sub-baseline:
/// at least [`MIN_CALLER_SHARE`] of the samples AND at least
/// [`MIN_RUNS_FOR_BASELINE`] of them on their own. Sorted, so the upsert
/// and retirement order is deterministic.
///
/// Both conditions are needed. Share alone would give a caller with 12 of a
/// quiet service's 40 calls a baseline that can never be ready; the minimum
/// alone would split a busy service into dozens of sub-baselines for
/// callers too small to differ materially from the aggregate.
pub fn qualifying_callers(samples: &[Sample]) -> Vec<String> {
    let total = samples.len();
    if total == 0 {
        return Vec::new();
    }
    let mut counts: BTreeMap<&str, i64> = BTreeMap::new();
    for s in samples {
        if let Some(c) = s.caller_system_id.as_deref() {
            *counts.entry(c).or_insert(0) += 1;
        }
    }
    counts
        .into_iter()
        .filter(|(_, n)| {
            *n >= MIN_RUNS_FOR_BASELINE && (*n as f64) / (total as f64) >= MIN_CALLER_SHARE
        })
        .map(|(c, _)| c.to_string())
        .collect()
}

/// Every cohort a basis is baselined over.
///
/// Runs: the aggregate only. Segments: the aggregate **always** — even
/// below the minimum, so the UI can say "ready in ~N calls" — then one per
/// [`qualifying_callers`] entry. Segments with no caller (the calling step was
/// in no AI System, or its system was deleted) count toward the aggregate and
/// never toward a caller.
pub fn cohorts(source: SampleSource, samples: &[Sample]) -> Vec<Cohort<'_>> {
    let mut out = vec![Cohort {
        caller_system_id: ALL_CALLERS.to_string(),
        samples: samples.iter().collect(),
    }];
    if source == SampleSource::Segments {
        for caller in qualifying_callers(samples) {
            let mine = samples
                .iter()
                .filter(|s| s.caller_system_id.as_deref() == Some(caller.as_str()))
                .collect();
            out.push(Cohort {
                caller_system_id: caller,
                samples: mine,
            });
        }
    }
    out
}

/// One population a cohort's baselines are learned over.
#[derive(Debug, Clone)]
pub struct Bucket<'a> {
    /// [`ALL_BUCKET`] or a full outcome id.
    pub key: String,
    /// Oldest-first.
    pub samples: Vec<&'a Sample>,
}

impl<'a> Bucket<'a> {
    pub fn is_all(&self) -> bool {
        self.key == ALL_BUCKET
    }

    /// The part of this bucket a baseline may learn from: everything before
    /// the observation window. Samples are oldest-first.
    pub fn reference(&self, obs_start: DateTime<Utc>) -> Bucket<'a> {
        let end = self.samples.partition_point(|s| s.started_at < obs_start);
        Bucket {
            key: self.key.clone(),
            samples: self.samples[..end].to_vec(),
        }
    }
}

/// The buckets a cohort is baselined in.
///
/// * Runs: bucket `all` over every run except `no_work` (failed and
///   deviating runs with no outcome stay covered by the aggregate), then one
///   bucket per full outcome id over the `success` and `no_work` runs that
///   reported it — so a variant's own shape is learned without being blended
///   with another's, and a poll that starts calling models is caught in its
///   own small baseline. Sorted by key.
/// * Segments: one `all` bucket over every served call (a segment has no run
///   class).
pub fn buckets<'a>(source: SampleSource, samples: &[&'a Sample]) -> Vec<Bucket<'a>> {
    use crate::services::run_outcome::run_class;
    if source == SampleSource::Segments {
        return vec![Bucket {
            key: ALL_BUCKET.to_string(),
            samples: samples.to_vec(),
        }];
    }
    let all = samples
        .iter()
        .copied()
        .filter(|s| s.run_class() != Some(run_class::NO_WORK))
        .collect();
    let mut by_outcome: BTreeMap<&str, Vec<&'a Sample>> = BTreeMap::new();
    for s in samples.iter().copied() {
        if matches!(s.run_class(), Some(run_class::SUCCESS | run_class::NO_WORK)) {
            if let Some(o) = s.outcome_id.as_deref() {
                by_outcome.entry(o).or_default().push(s);
            }
        }
    }
    let mut out = vec![Bucket {
        key: ALL_BUCKET.to_string(),
        samples: all,
    }];
    out.extend(by_outcome.into_iter().map(|(k, v)| Bucket {
        key: k.to_string(),
        samples: v,
    }));
    out
}

/// The count a cohort's readiness is judged on: its `success`
/// runs (twenty empty polls are not twenty pieces of evidence about the
/// work). On the segment basis, every served call, as before.
pub fn evidence_count<S: Borrow<Sample>>(source: SampleSource, samples: &[S]) -> i64 {
    match source {
        SampleSource::Runs => samples
            .iter()
            .filter(|s| Borrow::<Sample>::borrow(*s).is_success())
            .count() as i64,
        SampleSource::Segments => samples.len() as i64,
    }
}

/// A computed baseline for one metric, ready to persist.
#[derive(Debug, Clone, PartialEq)]
pub struct ComputedBaseline {
    pub metric_key: String,
    pub bucket: String,
    pub median: Option<f64>,
    pub mad: Option<f64>,
    pub p50: Option<f64>,
    pub p90: Option<f64>,
    pub p99: Option<f64>,
    pub distribution: Option<BTreeMap<String, f64>>,
    pub sample_count: i64,
    pub state: &'static str,
    /// The baseline epoch this metric re-learned from, if any.
    /// Persisted as the row's `window_start` when later than the window's,
    /// so the console can say "re-learning since …".
    pub epoch: Option<DateTime<Utc>>,
}

impl ComputedBaseline {
    pub fn is_ready(&self) -> bool {
        self.state == baseline_state::READY
    }
}

/// Extract the continuous series for a metric.
///
/// Generic over owned or borrowed samples so a per-caller [`Cohort`] is
/// measured by exactly the same code as the whole basis, without copying.
pub fn series_for<S: Borrow<Sample>>(samples: &[S], metric_key: &str) -> Vec<f64> {
    samples
        .iter()
        .map(Borrow::borrow)
        .filter_map(|s: &Sample| match metric_key {
            metric::ACTION_COUNT => Some(s.action_count as f64),
            metric::DURATION_MS => Some(s.active_duration_ms as f64),
            metric::TOOL_CALL_COUNT => Some(s.tool_call_count as f64),
            metric::LLM_CALL_COUNT => s.llm_call_count.map(|v| v as f64),
            metric::TOTAL_TOKENS => s.total_tokens.map(|v| v as f64),
            metric::MAX_DELEGATION_DEPTH => s.max_delegation_depth.map(|v| v as f64),
            metric::COST_PER_RUN => Some(s.cost_usd),
            metric::FIRST_PROMPT_TOKENS => s.first_prompt_tokens.map(|v| v as f64),
            metric::INPUT_BYTES => s.input_bytes.map(|v| v as f64),
            metric::PEAK_CONTEXT_TOKENS => s.peak_context_tokens.map(|v| v as f64),
            metric::CONTEXT_GROWTH => s.context_growth,
            metric::ERROR_RATE => {
                // Per served call, like the tool error rate: a call that
                // took no actions (it never reached the service) has no
                // rate — its outcome is what the outcome mix is for.
                match s.error_count {
                    Some(e) if s.action_count > 0 => Some(e as f64 / s.action_count as f64),
                    _ => None,
                }
            }
            metric::TOOL_ERROR_RATE => {
                // Per-run rate, so a run with 1 error in 20 calls and one
                // with 1 in 2 are not treated as equally bad.
                if s.tool_call_count > 0 {
                    Some(s.tool_error_count as f64 / s.tool_call_count as f64)
                } else {
                    None
                }
            }
            metric::EXHAUSTION_RATE => Some(if s.terminal_state == "exhausted" {
                1.0
            } else {
                0.0
            }),
            _ => None,
        })
        .collect()
}

/// Extract the categorical frequency vector for a metric.
pub fn distribution_for<S: Borrow<Sample>>(
    samples: &[S],
    metric_key: &str,
) -> BTreeMap<String, f64> {
    let mut counts: BTreeMap<String, f64> = BTreeMap::new();
    for s in samples.iter().map(Borrow::borrow) {
        let s: &Sample = s;
        match metric_key {
            metric::TOOL_MIX => {
                // The unordered tool-set signature, not the ordered one:
                // a baseline over exact orderings would treat every
                // reshuffle as a new category and never stabilise.
                if let Some(sig) = &s.tool_set_signature {
                    *counts.entry(sig.clone()).or_insert(0.0) += 1.0;
                }
            }
            metric::TERMINAL_STATE_MIX => {
                *counts.entry(s.terminal_state.clone()).or_insert(0.0) += 1.0;
            }
            // The work's mix, over success runs only.
            metric::OUTCOME_MIX => {
                if s.is_success() {
                    if let Some(o) = &s.outcome_id {
                        *counts.entry(o.clone()).or_insert(0.0) += 1.0;
                    }
                }
            }
            // Why runs fail, over failed runs only.
            metric::ERROR_CAUSE_MIX => {
                if s.run_class() == Some(crate::services::run_outcome::run_class::FAILED) {
                    let key = s.error_cause.clone().unwrap_or_else(|| "unknown".into());
                    *counts.entry(key).or_insert(0.0) += 1.0;
                }
            }
            metric::CLIENT_MIX => {
                let key = s.client_label.clone().unwrap_or_else(|| "unknown".into());
                *counts.entry(key).or_insert(0.0) += 1.0;
            }
            metric::MODEL_MIX => match s.models_used.as_ref() {
                // Runs: `[{"model": .., "calls": ..}]`.
                Some(serde_json::Value::Array(models)) => {
                    for m in models {
                        if let Some(name) = m.get("model").and_then(|v| v.as_str()) {
                            let calls = m.get("calls").and_then(|v| v.as_f64()).unwrap_or(1.0);
                            *counts.entry(name.to_string()).or_insert(0.0) += calls;
                        }
                    }
                }
                // Segments: `{"<model>": <calls>}`.
                Some(serde_json::Value::Object(models)) => {
                    for (name, calls) in models {
                        let calls = calls.as_f64().unwrap_or(1.0);
                        *counts.entry(name.clone()).or_insert(0.0) += calls;
                    }
                }
                _ => {}
            },
            _ => {}
        }
    }
    counts
}

/// Compute every baseline this basis and system kind call for.
///
/// Each metric is computed over its OWN sample set — the suffix at
/// or after its baseline epoch (`floors`). A metric whose epoch leaves fewer
/// than [`MIN_RUNS_FOR_BASELINE`] samples is `learning` again, never `ready`
/// on the data an operator accepted a change away from. The statistics are
/// the same [`compute_one`] for every metric; only the slice differs.
///
/// Computed per [`buckets`]; see [`compute_bucket`].
pub fn compute_all<S: Borrow<Sample>>(
    samples: &[S],
    basis: &str,
    system_kind: Option<&str>,
    floors: &exclusion::MetricFloors,
) -> Vec<ComputedBaseline> {
    let source = if basis == basis::SEGMENTS {
        SampleSource::Segments
    } else {
        SampleSource::Runs
    };
    let refs: Vec<&Sample> = samples.iter().map(Borrow::borrow).collect();
    buckets(source, &refs)
        .iter()
        .flat_map(|b| compute_bucket(source, b, system_kind, floors))
        .collect()
}

/// The baselines of one bucket. Bucket `all` carries every collected metric;
/// an outcome bucket only the [`profiles::is_bucketed`] ones.
///
/// Readiness: on the runs basis bucket `all` is `ready` only when
/// its (post-epoch) slice also holds at least [`MIN_RUNS_FOR_BASELINE`]
/// `success` runs; an outcome bucket is homogeneous, so its own size decides.
pub fn compute_bucket(
    source: SampleSource,
    bucket: &Bucket<'_>,
    system_kind: Option<&str>,
    floors: &exclusion::MetricFloors,
) -> Vec<ComputedBaseline> {
    profiles::collected_metrics(source.basis(), system_kind)
        .into_iter()
        .filter(|m| bucket.is_all() || profiles::is_bucketed(m))
        .map(|m| {
            let slice = floors.slice(&bucket.samples, m);
            let mut b = compute_one(slice, m);
            b.bucket = bucket.key.clone();
            b.epoch = floors.floor(m);
            if bucket.is_all() && evidence_count(source, slice) < MIN_RUNS_FOR_BASELINE {
                b.state = baseline_state::LEARNING;
            }
            b
        })
        .collect()
}

pub fn compute_one<S: Borrow<Sample>>(samples: &[S], metric_key: &str) -> ComputedBaseline {
    let n = samples.len() as i64;
    let state = if n >= MIN_RUNS_FOR_BASELINE {
        baseline_state::READY
    } else {
        baseline_state::LEARNING
    };

    if profiles::is_categorical(metric_key) {
        let dist = distribution_for(samples, metric_key);
        // The outcome mixes are over a sub-population (success / failed
        // runs): their count is that population's, not the bucket's.
        let n = if matches!(metric_key, metric::OUTCOME_MIX | metric::ERROR_CAUSE_MIX) {
            dist.values().sum::<f64>() as i64
        } else {
            n
        };
        return ComputedBaseline {
            metric_key: metric_key.to_string(),
            bucket: "all".into(),
            median: None,
            mad: None,
            p50: None,
            p90: None,
            p99: None,
            distribution: Some(stats::normalize(&dist)),
            sample_count: n,
            state,
            epoch: None,
        };
    }

    let raw = series_for(samples, metric_key);
    let sorted = stats::sorted_finite(&raw);
    let med = stats::median(&sorted);
    ComputedBaseline {
        metric_key: metric_key.to_string(),
        bucket: "all".into(),
        median: med,
        mad: med.and_then(|m| stats::mad(&sorted, m)),
        p50: stats::percentile(&sorted, 0.50),
        p90: stats::percentile(&sorted, 0.90),
        p99: stats::percentile(&sorted, 0.99),
        distribution: None,
        // The per-metric count, not the run count: a metric that is null
        // on most runs (duration on a system that never records it) must
        // not claim a baseline it does not have.
        sample_count: sorted.len() as i64,
        state: if (sorted.len() as i64) >= MIN_RUNS_FOR_BASELINE {
            state
        } else {
            baseline_state::LEARNING
        },
        epoch: None,
    }
}

/// Persist a baseline, replacing any previous one for the same key —
/// `(tenant, system, metric, bucket, actor, basis, caller)`.
#[allow(clippy::too_many_arguments)]
pub async fn upsert(
    db: &PgPool,
    tenant_id: &str,
    ai_system_id: &str,
    basis: &str,
    caller_system_id: &str,
    computed: &ComputedBaseline,
    window_start: DateTime<Utc>,
    window_end: DateTime<Utc>,
) -> Result<(), sqlx::Error> {
    let dist = computed
        .distribution
        .as_ref()
        .map(|d| json!(d))
        .unwrap_or(serde_json::Value::Null);

    sqlx::query(
        r#"
        INSERT INTO ai_system_baselines
            (id, tenant_id, ai_system_id, metric_key, bucket, actor_id,
             median, mad, p50, p90, p99, distribution, sample_count,
             window_start, window_end, state, computed_at, basis, caller_system_id)
        VALUES ($1, $2, $3, $4, $5, 'all',
                $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, NOW(), $16, $17)
        ON CONFLICT (tenant_id, ai_system_id, metric_key, bucket, actor_id, basis, caller_system_id)
        DO UPDATE SET
            median = EXCLUDED.median,
            mad = EXCLUDED.mad,
            p50 = EXCLUDED.p50,
            p90 = EXCLUDED.p90,
            p99 = EXCLUDED.p99,
            distribution = EXCLUDED.distribution,
            sample_count = EXCLUDED.sample_count,
            window_start = EXCLUDED.window_start,
            window_end = EXCLUDED.window_end,
            state = EXCLUDED.state,
            computed_at = NOW()
        "#,
    )
    .bind(format!("base-{}", ulid::Ulid::new()))
    .bind(tenant_id)
    .bind(ai_system_id)
    .bind(&computed.metric_key)
    .bind(&computed.bucket)
    .bind(computed.median)
    .bind(computed.mad)
    .bind(computed.p50)
    .bind(computed.p90)
    .bind(computed.p99)
    .bind(dist)
    .bind(computed.sample_count)
    .bind(computed.epoch.map_or(window_start, |e| e.max(window_start)))
    .bind(window_end)
    .bind(computed.state)
    .bind(basis)
    .bind(caller_system_id)
    .execute(db)
    .await?;
    Ok(())
}

/// Mark `stale` every segment baseline of this system whose caller is not
/// in `keep` — a caller that fell under the share threshold or stopped
/// calling, or (with `keep` empty) a service no longer called at all.
///
/// Stale rather than deleted: the row is still what that caller's traffic
/// looked like, and the UI shows it as history. Stale rather than left
/// `ready`: a ready row nobody refreshes reads as a live baseline that
/// agrees with everything. The runs basis is never touched here — its
/// aggregate is rewritten on every sweep.
pub async fn retire_segment_cohorts(
    db: &PgPool,
    tenant_id: &str,
    ai_system_id: &str,
    keep: &[String],
) -> Result<u64, sqlx::Error> {
    let res = sqlx::query(
        "UPDATE ai_system_baselines SET state = $4 \
          WHERE tenant_id = $1 AND ai_system_id = $2 AND basis = $5 \
            AND state <> $4 AND caller_system_id <> ALL($3)",
    )
    .bind(tenant_id)
    .bind(ai_system_id)
    .bind(keep)
    .bind(baseline_state::STALE)
    .bind(basis::SEGMENTS)
    .execute(db)
    .await?;
    Ok(res.rows_affected())
}

/// The full window a sweep loads: the reference period a baseline is built
/// from, followed by the observation window judged against it. See
/// [`super::sweeper::observation_start`].
pub fn window(now: DateTime<Utc>) -> (DateTime<Utc>, DateTime<Utc>) {
    (now - Duration::days(BASELINE_WINDOW_DAYS), now)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::drift::exclusion::MetricFloors;

    fn sample(step: i32, cost: f64, state: &str) -> Sample {
        Sample {
            started_at: Utc::now(),
            action_count: step,
            active_duration_ms: 1000,
            tool_call_count: 4,
            llm_call_count: None,
            total_tokens: None,
            error_count: None,
            max_delegation_depth: Some(0),
            cost_usd: cost,
            tool_error_count: 0,
            terminal_state: state.into(),
            tool_set_signature: Some("sigA".into()),
            first_prompt_tokens: Some(100),
            input_bytes: Some(500),
            peak_context_tokens: Some(4_000),
            context_growth: Some(2.0),
            client_label: Some("sdk".into()),
            models_used: Some(serde_json::json!([{"model": "gpt-x", "calls": 2}])),
            caller_system_id: None,
            // A successful run of declared, counted work: the
            // population baselines are learned from.
            is_run: true,
            outcome_id: Some("task_completed".into()),
            outcome_conformance: Some("declared".into()),
            outcome_counts_toward_baseline: Some(true),
            error_cause: None,
            run_id: None,
            tools_used: None,
        }
    }

    fn run_with(outcome: Option<&str>, conformance: &str, counts: Option<bool>) -> Sample {
        Sample {
            outcome_id: outcome.map(str::to_string),
            outcome_conformance: Some(conformance.into()),
            outcome_counts_toward_baseline: counts,
            ..sample(5, 0.01, "completed")
        }
    }

    /// A served call, shaped the way `SEGMENT_SAMPLES_SQL` returns it.
    fn segment(caller: Option<&str>, actions: i32) -> Sample {
        Sample {
            started_at: Utc::now(),
            action_count: actions,
            active_duration_ms: 800,
            tool_call_count: 2,
            llm_call_count: Some(3),
            total_tokens: Some(1200),
            error_count: Some(0),
            max_delegation_depth: None,
            cost_usd: 0.002,
            tool_error_count: 0,
            terminal_state: "answered".into(),
            tool_set_signature: Some("sigS".into()),
            first_prompt_tokens: None,
            input_bytes: None,
            peak_context_tokens: None,
            context_growth: None,
            client_label: None,
            models_used: Some(serde_json::json!({"gpt-x": 3})),
            caller_system_id: caller.map(String::from),
            is_run: false,
            outcome_id: None,
            outcome_conformance: None,
            outcome_counts_toward_baseline: None,
            error_cause: None,
            run_id: None,
            tools_used: None,
        }
    }

    fn served(caller: &str, n: usize) -> Vec<Sample> {
        (0..n).map(|_| segment(Some(caller), 5)).collect()
    }

    fn many(n: usize) -> Vec<Sample> {
        (0..n).map(|_| sample(5, 0.01, "completed")).collect()
    }

    #[test]
    fn below_the_floor_a_baseline_is_learning() {
        let b = compute_one(&many(10), metric::ACTION_COUNT);
        assert_eq!(b.state, baseline_state::LEARNING);
        assert!(!b.is_ready(), "a baseline from ten runs must never alert");
    }

    #[test]
    fn at_the_floor_it_becomes_ready() {
        let b = compute_one(&many(MIN_RUNS_FOR_BASELINE as usize), metric::ACTION_COUNT);
        assert_eq!(b.state, baseline_state::READY);
        assert!(b.is_ready());
    }

    #[test]
    fn robust_statistics_survive_a_runaway() {
        let mut s = many(60);
        s.push(sample(400, 0.01, "completed"));
        let b = compute_one(&s, metric::ACTION_COUNT);
        assert_eq!(b.median, Some(5.0), "one loop must not redefine normal");
        assert_eq!(b.p99, Some(400.0), "but the tail is still visible");
    }

    #[test]
    fn categorical_baselines_are_normalized() {
        let mut s = many(30);
        for x in s.iter_mut().take(10) {
            x.terminal_state = "errored".into();
        }
        let b = compute_one(&s, metric::TERMINAL_STATE_MIX);
        let d = b.distribution.expect("distribution");
        let total: f64 = d.values().sum();
        assert!((total - 1.0).abs() < 1e-9);
        assert!((d["errored"] - 10.0 / 30.0).abs() < 1e-9);
    }

    #[test]
    fn tool_mix_uses_the_unordered_signature() {
        // Baselining exact orderings would create a new category on every
        // reshuffle and never stabilise.
        let mut s = many(20);
        s[0].tool_set_signature = Some("sigB".into());
        let b = compute_one(&s, metric::TOOL_MIX);
        let d = b.distribution.unwrap();
        assert_eq!(d.len(), 2);
    }

    #[test]
    fn a_run_without_a_measured_context_is_not_a_sample_of_zero() {
        let mut s = many(20);
        for x in &mut s[..10] {
            x.peak_context_tokens = None;
            x.context_growth = None;
        }
        let peak = compute_one(&s, metric::PEAK_CONTEXT_TOKENS);
        assert_eq!(peak.median, Some(4_000.0));
        assert_eq!(peak.sample_count, 10);
        let growth = compute_one(&s, metric::CONTEXT_GROWTH);
        assert_eq!(growth.median, Some(2.0));
    }

    #[test]
    fn model_mix_weights_by_call_count() {
        let mut s = many(2);
        s[1].models_used = Some(serde_json::json!([{"model": "claude-y", "calls": 6}]));
        let d = distribution_for(&s, metric::MODEL_MIX);
        assert_eq!(d["gpt-x"], 2.0);
        assert_eq!(d["claude-y"], 6.0);
    }

    #[test]
    fn a_metric_that_is_mostly_null_does_not_claim_a_baseline() {
        let mut s = many(60);
        for x in s.iter_mut() {
            x.first_prompt_tokens = None;
        }
        let b = compute_one(&s, metric::FIRST_PROMPT_TOKENS);
        assert_eq!(b.sample_count, 0);
        assert_eq!(b.state, baseline_state::LEARNING);
    }

    #[test]
    fn tool_error_rate_is_per_run_not_pooled() {
        let mut s = many(2);
        s[0].tool_call_count = 20;
        s[0].tool_error_count = 1;
        s[1].tool_call_count = 2;
        s[1].tool_error_count = 1;
        let series = series_for(&s, metric::TOOL_ERROR_RATE);
        assert_eq!(series, vec![0.05, 0.5]);
    }

    #[test]
    fn a_buckets_reference_stops_at_the_observation_window() {
        let mut s = many(10);
        for (i, x) in s.iter_mut().enumerate() {
            x.started_at = at_hour(i as i64);
        }
        let refs: Vec<&Sample> = s.iter().collect();
        let bucket = &buckets(SampleSource::Runs, &refs)[0];
        let reference = bucket.reference(at_hour(7));
        assert_eq!(reference.samples.len(), 7, "hours 0-6 only");
        assert!(reference.samples.iter().all(|x| x.started_at < at_hour(7)));
        assert_eq!(reference.key, bucket.key);
    }

    #[test]
    fn runs_with_no_tool_calls_are_excluded_from_the_error_rate() {
        let mut s = many(3);
        s[0].tool_call_count = 0;
        assert_eq!(series_for(&s, metric::TOOL_ERROR_RATE).len(), 2);
    }

    #[test]
    fn compute_all_respects_the_signal_profile() {
        let s = many(60);
        let app = compute_all(&s, basis::RUNS, Some("application"), &MetricFloors::none());
        assert!(
            !app.iter().any(|b| b.metric_key == metric::ACTION_COUNT),
            "an application's step count is 1 by construction"
        );
        assert!(app.iter().any(|b| b.metric_key == metric::COST_PER_RUN));

        let agent = compute_all(&s, basis::RUNS, Some("agent"), &MetricFloors::none());
        assert!(agent.iter().any(|b| b.metric_key == metric::ACTION_COUNT));
    }

    #[test]
    fn empty_input_produces_a_learning_baseline_not_a_panic() {
        let b = compute_one::<Sample>(&[], metric::ACTION_COUNT);
        assert_eq!(b.sample_count, 0);
        assert_eq!(b.median, None);
        assert_eq!(b.state, baseline_state::LEARNING);
    }

    #[test]
    fn the_exclusion_clause_is_in_the_sql() {
        // These three exclusions are what keep a baseline honest; losing
        // any of them silently teaches the detector a shape that never
        // happened.
        assert!(SAMPLES_SQL.contains("NOT is_replay"));
        assert!(
            SAMPLES_SQL.contains(crate::services::run_ledger::chain::OBSERVED_COMPLETE_SQL),
            "baseline sampling must accept every complete observation — excluding \
             client_asserted starves baselines for run-id-correlated agents"
        );
        assert!(SAMPLES_SQL.contains("reopened_count <= $5"));
        // And nothing inside a declared incident window.
        assert!(SAMPLES_SQL.contains(&exclusion::incident_window_predicate(
            "$1",
            "started_at",
            &["$2"]
        )));
        assert!(!SAMPLES_SQL.contains("{excl}"));
    }

    // -- baseline epochs ------------------------------------------

    fn at_hour(h: i64) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339("2026-09-14T00:00:00Z")
            .unwrap()
            .with_timezone(&Utc)
            + Duration::hours(h)
    }

    #[test]
    fn an_epoch_puts_its_metric_back_to_learning_until_the_minimum_is_met_after_it() {
        // 120 runs an hour apart; an accepted change at hour 100 leaves 20.
        let mut s = many(120);
        for (i, x) in s.iter_mut().enumerate() {
            x.started_at = at_hour(i as i64);
            x.action_count = if i >= 100 { 2 } else { 6 };
        }
        let floors = crate::services::drift::exclusion::Epochs::from_rows([(
            metric::ACTION_COUNT.to_string(),
            ALL_CALLERS.to_string(),
            at_hour(100),
        )])
        .for_cohort(ALL_CALLERS);
        let all = compute_all(&s, basis::RUNS, Some("agent"), &floors);
        let ac = all
            .iter()
            .find(|b| b.metric_key == metric::ACTION_COUNT)
            .unwrap();
        assert_eq!(
            ac.sample_count, 20,
            "pre-epoch samples are not its baseline"
        );
        assert_eq!(
            ac.state,
            baseline_state::LEARNING,
            "never ready on the data an operator accepted a change away from"
        );
        assert_eq!(ac.epoch, Some(at_hour(100)));
        let cost = all
            .iter()
            .find(|b| b.metric_key == metric::COST_PER_RUN)
            .unwrap();
        assert!(cost.is_ready(), "an epoch is per metric");
        assert_eq!(cost.epoch, None);

        // Once the minimum is met after the epoch, ready — on post-epoch data.
        let mut s2 = s.clone();
        for i in 120..(100 + MIN_RUNS_FOR_BASELINE as usize) {
            let mut x = sample(2, 0.01, "completed");
            x.started_at = at_hour(i as i64);
            s2.push(x);
        }
        let ac = compute_all(&s2, basis::RUNS, Some("agent"), &floors)
            .into_iter()
            .find(|b| b.metric_key == metric::ACTION_COUNT)
            .unwrap();
        assert!(ac.is_ready());
        assert_eq!(ac.sample_count, MIN_RUNS_FOR_BASELINE);
        assert_eq!(ac.median, Some(2.0), "the new normal, not the old one");
    }

    // -- the segment basis --------------------------------

    #[test]
    fn a_caller_needs_the_share_and_the_minimum_to_get_a_sub_baseline() {
        // 300 calls: A = 200 (67%), B = 60 (20% exactly, 60 >= 50),
        // C = 40 (13%, and under the minimum). Only A and B qualify.
        let mut s = served("sys-a", 200);
        s.extend(served("sys-b", 60));
        s.extend(served("sys-c", 40));
        assert_eq!(qualifying_callers(&s), vec!["sys-a", "sys-b"]);
    }

    #[test]
    fn a_large_share_below_the_minimum_does_not_qualify() {
        // 30 of 40 calls is 75% — but a sub-baseline from 30 calls can
        // never be ready, so it must not be created.
        let mut s = served("sys-a", 30);
        s.extend(served("sys-b", 10));
        assert!(qualifying_callers(&s).is_empty());
    }

    #[test]
    fn meeting_the_minimum_below_the_share_does_not_qualify() {
        // 60 calls out of 400 (15%) meets the minimum on its own but is too
        // small a share of the service to differ materially.
        let mut s = served("sys-big", 340);
        s.extend(served("sys-small", 60));
        assert_eq!(qualifying_callers(&s), vec!["sys-big"]);
    }

    #[test]
    fn callerless_segments_count_toward_the_aggregate_only() {
        // A deleted root leaves root_system_id NULL. Those calls still
        // happened to the service — they dilute every caller's share — but
        // belong to no caller.
        let mut s = served("sys-a", 60);
        s.extend((0..300).map(|_| segment(None, 5)));
        assert!(
            qualifying_callers(&s).is_empty(),
            "60 of 360 calls is 17%: callerless calls dilute the share, and \
             are never a caller of their own"
        );
        let mut s = served("sys-a", 60);
        s.extend((0..100).map(|_| segment(None, 5)));
        assert_eq!(qualifying_callers(&s), vec!["sys-a"]);
        let c = cohorts(SampleSource::Segments, &s);
        assert_eq!(c[0].samples.len(), 160, "the aggregate keeps every call");
        assert_eq!(c[1].samples.len(), 60);
    }

    #[test]
    fn the_aggregate_cohort_is_always_first_and_always_present() {
        let none: Vec<Sample> = Vec::new();
        let c = cohorts(SampleSource::Segments, &none);
        assert_eq!(c.len(), 1);
        assert!(c[0].is_aggregate());
        assert_eq!(c[0].event_caller(), None);

        let s = served("sys-a", 80);
        let c = cohorts(SampleSource::Segments, &s);
        assert_eq!(
            c.iter()
                .map(|c| c.caller_system_id.as_str())
                .collect::<Vec<_>>(),
            vec![ALL_CALLERS, "sys-a"]
        );
        assert_eq!(c[1].event_caller(), Some("sys-a"));
    }

    #[test]
    fn the_runs_basis_never_splits_by_caller() {
        let mut s = many(120);
        for x in s.iter_mut() {
            x.caller_system_id = Some("sys-a".into());
        }
        let c = cohorts(SampleSource::Runs, &s);
        assert_eq!(c.len(), 1, "per-caller sub-baselines are a service concept");
        assert!(c[0].is_aggregate());
    }

    #[test]
    fn a_per_caller_cohort_is_measured_by_the_same_statistics() {
        // The cohort borrows; the numbers must be identical to computing
        // over an owned copy of the same calls.
        let mut s = served("sys-a", 60);
        for (i, x) in s.iter_mut().enumerate() {
            x.action_count = 3 + (i % 4) as i32;
        }
        s.extend(served("sys-b", 60));
        let c = cohorts(SampleSource::Segments, &s);
        let a = c.iter().find(|c| c.caller_system_id == "sys-a").unwrap();
        let owned: Vec<Sample> = s[..60].to_vec();
        assert_eq!(
            compute_one(&a.samples, metric::ACTION_COUNT),
            compute_one(&owned, metric::ACTION_COUNT)
        );
    }

    #[test]
    fn segment_baselines_cover_the_segment_metric_set_only() {
        let s = served("sys-a", 60);
        let computed = compute_all(&s, basis::SEGMENTS, Some("agent"), &MetricFloors::none());
        let keys: Vec<&str> = computed.iter().map(|b| b.metric_key.as_str()).collect();
        assert!(keys.contains(&metric::LLM_CALL_COUNT));
        assert!(keys.contains(&metric::TOTAL_TOKENS));
        assert!(keys.contains(&metric::ERROR_RATE));
        assert!(!keys.contains(&metric::FIRST_PROMPT_TOKENS));
        assert!(!keys.contains(&metric::CLIENT_MIX));
        for b in &computed {
            assert!(
                b.is_ready(),
                "{} should be ready from 60 calls",
                b.metric_key
            );
        }
    }

    #[test]
    fn run_only_metrics_yield_no_samples_on_segments() {
        // A typed NULL must be skipped, never read as zero — a baseline of
        // zeros would flag the first real value as drift.
        let s = served("sys-a", 60);
        assert!(series_for(&s, metric::MAX_DELEGATION_DEPTH).is_empty());
        assert!(series_for(&s, metric::INPUT_BYTES).is_empty());
        let r = many(60);
        assert!(series_for(&r, metric::LLM_CALL_COUNT).is_empty());
        assert!(series_for(&r, metric::TOTAL_TOKENS).is_empty());
        assert!(series_for(&r, metric::ERROR_RATE).is_empty());
    }

    #[test]
    fn segment_model_mix_reads_the_object_shape() {
        let mut s = served("sys-a", 2);
        s[1].models_used = Some(serde_json::json!({"claude-y": 6, "gpt-x": 1}));
        let d = distribution_for(&s, metric::MODEL_MIX);
        assert_eq!(d["gpt-x"], 4.0);
        assert_eq!(d["claude-y"], 6.0);
    }

    #[test]
    fn segment_outcome_is_the_terminal_state_mix() {
        let mut s = served("sys-a", 4);
        s[0].terminal_state = "timed_out".into();
        let d = compute_one(&s, metric::TERMINAL_STATE_MIX)
            .distribution
            .unwrap();
        assert!((d["answered"] - 0.75).abs() < 1e-9);
        assert!((d["timed_out"] - 0.25).abs() < 1e-9);
    }

    #[test]
    fn error_rate_is_per_served_call_and_skips_calls_that_never_arrived() {
        let mut s = served("sys-a", 3);
        s[0].error_count = Some(1);
        s[0].action_count = 4;
        s[1].action_count = 0; // never reached the service
        assert_eq!(series_for(&s, metric::ERROR_RATE), vec![0.25, 0.0]);
    }

    #[test]
    fn the_segment_query_applies_the_run_honesty_clauses() {
        // A segment of a replay, an untrusted chain or an over-reopened run
        // is a fragment of a fragment.
        assert!(SEGMENT_SAMPLES_SQL.contains("NOT is_replay"));
        assert!(
            SEGMENT_SAMPLES_SQL.contains(crate::services::run_ledger::chain::OBSERVED_COMPLETE_SQL)
        );
        assert!(SEGMENT_SAMPLES_SQL.contains("reopened_count <= $5"));
        assert!(SEGMENT_SAMPLES_SQL.contains("s.segment_system_id = $2"));
        // A window on the service or on the run's root.
        assert!(
            SEGMENT_SAMPLES_SQL.contains(&exclusion::incident_window_predicate(
                "$1",
                "s.first_action_at",
                &["$2", "s.root_system_id"]
            ))
        );
        // The caller is the IMMEDIATE calling system, never the root.
        assert!(SEGMENT_SAMPLES_SQL.contains("s.caller_system_id,\n"));
        // The root is read only by the incident-window predicate (a window
        // on the run's root covers its calls) — never as the caller.
        assert!(!SEGMENT_SAMPLES_SQL_TEMPLATE.contains("root_system_id"));
        assert_eq!(SampleSource::Runs.basis(), "runs");
        assert_eq!(SampleSource::Segments.basis(), "segments");
    }

    // -- buckets, readiness, the new mixes --------------------------

    fn keys(b: &[Bucket<'_>]) -> Vec<String> {
        b.iter().map(|b| b.key.clone()).collect()
    }

    #[test]
    fn runs_are_bucketed_by_outcome_and_no_work_leaves_all() {
        let approved = run_with(Some("app.loan_approved"), "declared", Some(true));
        let poll = run_with(Some("nothing_to_do"), "declared", Some(false));
        let deviating = run_with(Some("app.loan_maybe"), "undeclared", None);
        let mut failed = sample(3, 0.0, "errored");
        failed.outcome_id = None;
        failed.outcome_conformance = Some("unreported".into());
        failed.outcome_counts_toward_baseline = None;
        let refs = vec![&approved, &poll, &deviating, &failed];
        let b = buckets(SampleSource::Runs, &refs);
        assert_eq!(keys(&b), vec!["all", "app.loan_approved", "nothing_to_do"]);
        assert_eq!(b[0].samples.len(), 3, "all = every run but no-work");
        assert!(!b[0].samples.iter().any(|s| std::ptr::eq(*s, &poll)));
        assert_eq!(b[1].samples.len(), 1);
        assert_eq!(b[2].samples.len(), 1, "no-work gets its own baseline");
        // Deviating / failed runs are in no outcome bucket.
        assert!(!keys(&b).contains(&"app.loan_maybe".to_string()));
        // Segments: one bucket, everything.
        let s1 = segment(Some("c"), 2);
        assert_eq!(keys(&buckets(SampleSource::Segments, &[&s1])), vec!["all"]);
    }

    #[test]
    fn readiness_counts_success_runs_only() {
        // 50 empty polls and 49 real runs: the polls are not evidence.
        let mut s: Vec<Sample> = (0..50)
            .map(|_| run_with(Some("nothing_to_do"), "declared", Some(false)))
            .collect();
        s.extend((0..49).map(|_| sample(5, 0.01, "completed")));
        let all = compute_all(&s, basis::RUNS, Some("agent"), &MetricFloors::none());
        let ac = |bucket: &str| {
            all.iter()
                .find(|b| b.metric_key == metric::ACTION_COUNT && b.bucket == bucket)
                .unwrap()
                .clone()
        };
        assert_eq!(ac("all").state, baseline_state::LEARNING, "49 success runs");
        assert_eq!(
            ac("nothing_to_do").state,
            baseline_state::READY,
            "a no-work bucket can be ready"
        );
        assert_eq!(ac("task_completed").state, baseline_state::LEARNING);
        // Deviating runs never make the aggregate ready either.
        let dev: Vec<Sample> = (0..80)
            .map(|_| run_with(Some("x"), "undeclared", None))
            .collect();
        let all = compute_all(&dev, basis::RUNS, Some("agent"), &MetricFloors::none());
        assert!(
            all.iter().all(|b| !b.is_ready()),
            "no success, no readiness"
        );
        // One more success run and the aggregate is ready.
        s.push(sample(5, 0.01, "completed"));
        let all = compute_all(&s, basis::RUNS, Some("agent"), &MetricFloors::none());
        assert!(
            all.iter()
                .find(|b| b.metric_key == metric::ACTION_COUNT && b.bucket == "all")
                .unwrap()
                .is_ready()
        );
        assert_eq!(evidence_count(SampleSource::Runs, &s), 50);
        assert_eq!(evidence_count(SampleSource::Segments, &served("a", 3)), 3);
    }

    #[test]
    fn outcome_buckets_learn_each_variants_own_shape() {
        // 60 approvals at 10 actions and 60 applied decisions at 1: the
        // aggregate blends them; each bucket does not.
        let mut s: Vec<Sample> = (0..60)
            .map(|_| Sample {
                action_count: 10,
                ..run_with(Some("app.loan_approved"), "declared", Some(true))
            })
            .collect();
        s.extend((0..60).map(|_| Sample {
            action_count: 1,
            ..run_with(Some("app.decision_applied"), "declared", Some(true))
        }));
        let all = compute_all(&s, basis::RUNS, Some("agent"), &MetricFloors::none());
        let med = |bucket: &str| {
            all.iter()
                .find(|b| b.metric_key == metric::ACTION_COUNT && b.bucket == bucket)
                .unwrap()
                .median
        };
        assert_eq!(med("app.loan_approved"), Some(10.0));
        assert_eq!(med("app.decision_applied"), Some(1.0));
        // Outcome buckets carry continuous, non-rate metrics only.
        for b in all.iter().filter(|b| b.bucket != "all") {
            assert!(profiles::is_bucketed(&b.metric_key), "{}", b.metric_key);
        }
        assert!(
            all.iter()
                .any(|b| b.bucket == "all" && b.metric_key == metric::OUTCOME_MIX)
        );
    }

    #[test]
    fn the_outcome_mix_is_over_success_runs_and_the_error_mix_over_failed() {
        let mut s = vec![
            run_with(Some("app.loan_approved"), "declared", Some(true)),
            run_with(Some("app.loan_approved"), "declared", Some(true)),
            run_with(Some("app.loan_referred"), "declared", Some(true)),
            run_with(Some("nothing_to_do"), "declared", Some(false)),
            run_with(Some("app.loan_maybe"), "undeclared", None),
        ];
        let mut f1 = sample(1, 0.0, "errored");
        f1.error_cause = Some("model_unavailable".into());
        let mut f2 = sample(1, 0.0, "exhausted");
        f2.error_cause = Some("iteration_limit".into());
        let mut f3 = sample(1, 0.0, "errored");
        f3.error_cause = None;
        s.extend([f1, f2, f3]);
        let om = distribution_for(&s, metric::OUTCOME_MIX);
        assert_eq!(om.len(), 2, "{om:?}");
        assert_eq!(om["app.loan_approved"], 2.0);
        assert_eq!(om["app.loan_referred"], 1.0);
        let em = distribution_for(&s, metric::ERROR_CAUSE_MIX);
        assert_eq!(em["model_unavailable"], 1.0);
        assert_eq!(em["iteration_limit"], 1.0);
        assert_eq!(em["unknown"], 1.0);
        assert_eq!(
            em.values().sum::<f64>(),
            3.0,
            "success runs are not failures"
        );
        assert_eq!(compute_one(&s, metric::OUTCOME_MIX).sample_count, 3);
        // Segments contribute to neither.
        assert!(distribution_for(&served("a", 3), metric::OUTCOME_MIX).is_empty());
        assert!(distribution_for(&served("a", 3), metric::ERROR_CAUSE_MIX).is_empty());
    }
}
