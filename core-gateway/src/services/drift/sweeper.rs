// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The drift sweep.
//!
//! Ties the pieces together once per tick: refresh baselines, compare the
//! recent window against them, attribute a cause, and raise **one** alert
//! with the explanation already attached.
//!
//! ## Why the input check happens before anything is reported
//!
//! Detection runs over every alertable metric first, and only then is a
//! finding turned into an alert. That ordering is what makes the
//! disambiguation possible: if the input metrics moved in the same window,
//! the behavioural change is far more likely to be "the questions changed"
//! than "the system changed", and the finding is reported that way with
//! its severity reduced.
//!
//! Reporting each metric the moment it trips would lose that context
//! entirely and page someone about a Monday-morning traffic shift.
//!
//! ## One alert, not one per metric
//!
//! A system whose model changed will trip step count, duration, cost and
//! tool mix simultaneously. Those are one event with four symptoms. The
//! sweep reports the most severe and lists the rest as correlated —
//! four alerts for one cause is how an operator learns to filter the
//! category out of their inbox.

use chrono::{DateTime, Duration, Utc};
use serde_json::json;
use sqlx::{FromRow, PgPool};

use crate::services::system_alerts::{self, AlertCategory, Severity, SystemAlertInput};

use super::{
    ALERT_COOLDOWN_SECONDS, MIN_RUNS_FOR_BASELINE,
    baseline::{self, Cohort, Sample, SampleSource},
    cause::{self, Attribution, CauseFacts},
    detect::{self, BaselineRef, Detection, ImpactContext},
    drift_class,
    exclusion::{Closures, Epochs, Evidence},
    metric, new_shape, profiles, stats,
};
use crate::services::db_errors::DbResultExt;

/// Observation window — how much recent behaviour is compared against the
/// baseline. Shorter than the baseline window on purpose: a change has to
/// show up against a longer history to count.
///
/// The baseline is built only from runs BEFORE this window. Runs being judged
/// must never be part of the "normal" they are judged against: when they were,
/// a third of a day's runs changing shape diluted itself below the threshold.
pub const OBSERVATION_WINDOW_HOURS: i64 = 24;

/// Where the reference period ends and the observation window begins.
pub fn observation_start(now: DateTime<Utc>) -> DateTime<Utc> {
    now - Duration::hours(OBSERVATION_WINDOW_HOURS)
}

/// An AI System worth sweeping.
#[derive(Debug, Clone, FromRow)]
pub struct DriftCandidate {
    pub tenant_id: String,
    pub ai_system_id: String,
    pub system_name: Option<String>,
    pub system_kind: Option<String>,
    /// Raises drift severity for systems that act unattended. The
    /// column is not there yet; the
    /// query selects a literal NULL so severity calibration degrades to
    /// "not known to be autonomous" rather than failing the sweep.
    pub autonomy_level: Option<String>,
    pub risk_tier: Option<String>,
    pub eligible_runs: i64,
}

const CANDIDATES_SQL: &str = r#"
SELECT
    rg.tenant_id,
    rg.id                                   AS ai_system_id,
    COALESCE(rg.display_name, rg.name)      AS system_name,
    rg.system_kind,
    NULL::text                              AS autonomy_level,
    rg.eu_ai_act_risk_tier                  AS risk_tier,
    COALESCE(r.eligible_runs, 0)            AS eligible_runs
FROM resource_groups rg
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS eligible_runs
      FROM ai_system_runs ar
     WHERE ar.tenant_id = rg.tenant_id
       AND ar.ai_system_id = rg.id
       AND NOT ar.is_replay
       AND ar.chain_integrity IN ('intact', 'client_asserted', 'observed_external')
       AND ar.started_at >= NOW() - MAKE_INTERVAL(days => $1)
) r ON TRUE
WHERE UPPER(rg.group_type::text) = 'AI_SYSTEM'
  AND rg.is_active = true
  AND COALESCE(rg.baseline_opt_out, false) = false
"#;

pub async fn load_candidates(db: &PgPool, window_days: i64) -> Vec<DriftCandidate> {
    match sqlx::query_as::<_, DriftCandidate>(CANDIDATES_SQL)
        .bind(window_days as i32)
        .fetch_all(db)
        .await
    {
        Ok(rows) => rows,
        Err(e) => {
            tracing::warn!(error = %e, "drift sweep: candidate query failed");
            Vec::new()
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct SweepOutcome {
    pub checked: usize,
    pub learning: usize,
    pub baselines_written: usize,
    pub findings: usize,
    pub alerted: usize,
    pub suppressed: usize,
    /// Findings not recorded because a human closure of the
    /// same key already settled their evidence.
    pub settled: usize,
}

/// One drift finding, ready to record.
#[derive(Debug, Clone)]
pub struct Finding {
    /// `runs` | `segments` — which baseline set it was detected against.
    pub basis: &'static str,
    /// Set only for a per-caller service baseline (the event column is
    /// NULL for every aggregate).
    pub caller_system_id: Option<String>,
    /// The baseline bucket the primary detection came from:
    /// `all`, or a full outcome id.
    pub bucket: String,
    pub detection: Detection,
    pub attribution: Attribution,
    /// Resolved against the cohort's own timestamps.
    pub change_point: Option<DateTime<Utc>>,
    /// The samples the primary detection was judged on, so
    /// [`record`] can refuse evidence a human closure already settled.
    pub evidence: Evidence,
    /// What to lead the alert with, when the cause ladder's summary is not it:
    /// a first-seen shape names the tool and the run.
    pub headline: Option<String>,
}

/// Sweep one AI System over every basis it has. Returns the findings it
/// produced, aggregate before per-caller so the cooldown in [`record`] lets
/// the aggregate speak first when both see the same change.
///
/// Runs are always baselined (a system with none is `learning`, which the
/// UI needs). Segments only when the system has served any in the window —
/// and when it has not, any segment baselines it still holds are retired
/// to `stale` rather than left looking live.
///
/// Samples inside a declared incident window never reach this
/// function (the sample query excludes them); baseline epochs (written by
/// `accepted_change` and `false_positive` closures) are loaded per basis
/// and applied per metric. If they cannot be read the basis is skipped —
/// judging it without them is exactly the re-fire the resolution exists to
/// prevent.
pub async fn sweep_system(
    db: &PgPool,
    candidate: &DriftCandidate,
    now: DateTime<Utc>,
    outcome: &mut SweepOutcome,
) -> Vec<Finding> {
    let (win_start, win_end) = baseline::window(now);
    let obs_start = observation_start(now);
    let mut findings = Vec::new();
    let (t, sys) = (
        candidate.tenant_id.as_str(),
        candidate.ai_system_id.as_str(),
    );

    for source in SampleSource::ALL {
        // Loaded before the samples: a basis whose epochs cannot be read is
        // skipped, never judged on pre-epoch data.
        let epochs = match Epochs::load(db, t, sys, source.basis()).await {
            Ok(e) => e,
            Err(e) => {
                tracing::warn!(
                    error = %e,
                    ai_system_id = sys,
                    basis = source.basis(),
                    "drift sweep: baseline epochs unreadable, basis skipped"
                );
                continue;
            }
        };
        // Loaded separately so each side has its own sample cap: on a busy
        // system one day's runs must not push the whole history out of a
        // single newest-first query and leave the baseline empty.
        let mut samples = baseline::load_samples(db, source, t, sys, win_start, obs_start, 3).await;
        samples.extend(baseline::load_samples(db, source, t, sys, obs_start, win_end, 3).await);

        let cohorts = if source == SampleSource::Segments && samples.is_empty() {
            Vec::new()
        } else {
            baseline::cohorts(source, &samples)
        };

        if source == SampleSource::Segments {
            let keep: Vec<String> = cohorts.iter().map(|c| c.caller_system_id.clone()).collect();
            if let Err(e) = baseline::retire_segment_cohorts(db, t, sys, &keep).await {
                tracing::warn!(error = %e, "drift sweep: failed to retire segment baselines");
            }
        }

        for cohort in &cohorts {
            if let Some(f) = sweep_cohort(
                db, candidate, source, cohort, &epochs, win_start, obs_start, outcome,
            )
            .await
            {
                findings.push(f);
            }
        }

        if source == SampleSource::Runs {
            findings.extend(
                shape_findings(
                    db, candidate, &samples, &epochs, win_start, obs_start, outcome,
                )
                .await,
            );
        }
    }
    findings
}

/// Refresh one cohort's baselines and compare its observation window
/// against them. The whole detection pipeline — profile, statistics,
/// disambiguation, one-event-not-one-per-symptom, cause ladder — is the
/// same for every basis and cohort; only the samples differ.
///
/// Every metric is judged on its own slice — the samples at or
/// after its baseline epoch — for the baseline AND the observation window
/// AND the change-point trend.
#[allow(clippy::too_many_arguments)]
async fn sweep_cohort(
    db: &PgPool,
    candidate: &DriftCandidate,
    source: SampleSource,
    cohort: &Cohort<'_>,
    epochs: &Epochs,
    win_start: DateTime<Utc>,
    obs_start: DateTime<Utc>,
    outcome: &mut SweepOutcome,
) -> Option<Finding> {
    let basis = source.basis();
    let samples: &[&Sample] = &cohort.samples;

    // --- refresh baselines -------------------------------------------------
    // Per bucket: `all` (every run but the no-work ones) and one
    // per full outcome id.
    let buckets = baseline::buckets(source, samples);
    let computed: Vec<baseline::ComputedBaseline> = buckets
        .iter()
        .flat_map(|b| {
            let floors = epochs.for_cohort_bucket(&cohort.caller_system_id, &b.key);
            baseline::compute_bucket(
                source,
                &b.reference(obs_start),
                candidate.system_kind.as_deref(),
                &floors,
            )
        })
        .collect();
    for c in &computed {
        if baseline::upsert(
            db,
            &candidate.tenant_id,
            &candidate.ai_system_id,
            basis,
            &cohort.caller_system_id,
            c,
            win_start,
            obs_start,
        )
        .await
        .is_ok()
        {
            outcome.baselines_written += 1;
        }
    }

    let reference = &samples[..samples.partition_point(|s| s.started_at < obs_start)];
    if baseline::evidence_count(source, reference) < MIN_RUNS_FOR_BASELINE {
        // Learning mode: baselines are recorded so the UI can say "ready
        // in ~N runs", but nothing is compared and nothing alerts.
        // On the runs basis only `success` runs are evidence. Runs inside the
        // observation window do not count until they have aged out of it.
        outcome.learning += 1;
        return None;
    }

    // --- detect across every alertable metric ------------------------------
    //
    // All of them, before reporting any. The input-drift disambiguation
    // below needs the whole picture.
    let mut detections: Vec<(Detection, Evidence, String)> = Vec::new();
    for bucket in &buckets {
        let floors = epochs.for_cohort_bucket(&cohort.caller_system_id, &bucket.key);
        for metric_key in profiles::alertable_metrics(basis, candidate.system_kind.as_deref()) {
            if !bucket.is_all() && !profiles::is_bucketed(metric_key) {
                continue;
            }
            let base = baseline_ref(&computed, &bucket.key, metric_key);
            if !base.is_ready {
                continue;
            }

            // The metric's own sample set (post-epoch), oldest-first; the
            // observation window is a contiguous suffix of it — borrowed
            // slices, not copies.
            let judged: &[&Sample] = floors.slice(&bucket.samples, metric_key);
            let observed: &[&Sample] =
                &judged[judged.partition_point(|s| s.started_at < obs_start)..];
            if observed.is_empty() {
                continue;
            }

            let impact = ImpactContext {
                observed_runs: observed.len() as i64,
                cost_delta_usd: 0.0,
                autonomy_level: candidate.autonomy_level.clone(),
                risk_tier: candidate.risk_tier.clone(),
                has_side_effecting_grants: false,
            };

            let found = if profiles::is_categorical(metric_key) {
                let dist = baseline::distribution_for(observed, metric_key);
                detect::detect_categorical(metric_key, &base, &dist, &impact)
            } else if profiles::is_rate(metric_key) {
                let series = baseline::series_for(observed, metric_key);
                let sorted = stats::sorted_finite(&series);
                match stats::median(&sorted) {
                    Some(rate) => {
                        detect::detect_rate(metric_key, &base, rate, observed.len() as i64, &impact)
                    }
                    None => None,
                }
            } else {
                let series = baseline::series_for(observed, metric_key);
                // The change point is located over the whole (post-epoch)
                // baseline window, not just the last 24h: a change that
                // happened three days ago and is still standing has a flat
                // observation window, and the cause ladder needs the real date
                // to match a model or tool change against. See
                // `detect::detect_continuous`.
                let trend = baseline::series_for(judged, metric_key);
                let mut cost_impact = impact.clone();
                if metric_key == metric::COST_PER_RUN {
                    // Give the grader the money in the operator's terms rather
                    // than a z-score's.
                    if let (Some(b), Some(o)) =
                        (base.median, stats::median(&stats::sorted_finite(&series)))
                    {
                        cost_impact.cost_delta_usd = (o - b).abs();
                    }
                }
                detect::detect_continuous(metric_key, &base, &series, &trend, &cost_impact)
            };

            if let Some(d) = found {
                // Resolved against the metric's own trend timestamps, because
                // that is what the index refers to. Resolving it against the
                // observation window would silently report a date up to two
                // weeks too recent and send the cause ladder looking in the
                // wrong place.
                let change_point = detect::change_point_time(
                    &judged.iter().map(|s| s.started_at).collect::<Vec<_>>(),
                    d.change_point_index,
                );
                let evidence = Evidence {
                    baseline_from: judged.first().map(|s| s.started_at),
                    observed_from: observed.first().map(|s| s.started_at),
                    observed_to: observed.last().map(|s| s.started_at),
                    change_point,
                };
                detections.push((d, evidence, bucket.key.clone()));
            }
        }
    }

    if detections.is_empty() {
        return None;
    }

    // --- one event, not one per symptom ------------------------------------
    //
    // A model change trips step count, duration, cost and tool mix at once.
    // Report the most severe; the rest travel as correlated metrics.
    detections.sort_by(|(a, _, _), (b, _, _)| {
        severity_rank(b.severity)
            .cmp(&severity_rank(a.severity))
            .then(
                b.effect_size
                    .partial_cmp(&a.effect_size)
                    .unwrap_or(std::cmp::Ordering::Equal),
            )
    });
    let (primary, evidence, bucket) = detections[0].clone();
    let detections: Vec<Detection> = detections.into_iter().map(|(d, _, _)| d).collect();

    // The disambiguator, applied before attribution.
    let input_moved = detect::input_co_moved(&detections);
    let change_point = evidence.change_point;

    let scope = FactScope {
        source,
        caller_system_id: cohort.event_caller(),
        window_start: win_start,
    };
    // A cause lookup that failed is not "no cause": a finding
    // persisted from it would say "unexplained" (or raise one inside a
    // declared incident). The evaluation is skipped and redone next tick.
    let facts = match gather_facts(
        db,
        candidate,
        &scope,
        change_point,
        input_moved,
        &detections,
        &primary,
    )
    .await
    {
        Ok(f) => f,
        Err(e) => {
            tracing::warn!(error = %e, ai_system_id = %candidate.ai_system_id, "drift: cause facts unreadable; evaluation skipped this tick");
            return None;
        }
    };
    let mut attribution = cause::attribute(&facts, change_point);
    // Say which population moved. No column: the bucket rides in
    // the evidence (and the alert), the metric key stays the plain one.
    if let Some(ev) = attribution.evidence.as_object_mut() {
        ev.insert("bucket".into(), json!(bucket));
    }

    outcome.findings += 1;
    Some(Finding {
        basis,
        caller_system_id: cohort.event_caller().map(String::from),
        bucket,
        detection: primary,
        attribution,
        change_point,
        evidence,
        headline: None,
    })
}

/// Turn first-seen shapes into findings. An explained one (the ladder found a
/// contract, release or dependency change that accounts for it) is expected
/// more often than not, so it is `medium`; an unexplained new tool is `high`,
/// which is what a response policy normally fires on.
async fn shape_findings(
    db: &PgPool,
    candidate: &DriftCandidate,
    samples: &[Sample],
    epochs: &Epochs,
    win_start: DateTime<Utc>,
    obs_start: DateTime<Utc>,
    outcome: &mut SweepOutcome,
) -> Vec<Finding> {
    let split = samples.partition_point(|s| s.started_at < obs_start);
    let reference: Vec<&Sample> = samples[..split].iter().collect();
    let observed: Vec<&Sample> = samples[split..].iter().collect();
    let floors = epochs.for_cohort_bucket(baseline::ALL_CALLERS, baseline::ALL_BUCKET);

    let mut out = Vec::new();
    for shape in new_shape::detect(&reference, &observed) {
        // A human accepted this tool or tool set, or called it a false alarm.
        if floors.floor(&shape.metric_key).is_some() {
            continue;
        }
        let is_tool = shape.kind == new_shape::ShapeKind::NewTool;
        let primary = Detection {
            metric_key: shape.metric_key.clone(),
            drift_class: drift_class::BEHAVIOURAL,
            baseline_value: Some(0.0),
            observed_value: Some(shape.run_count as f64),
            effect_size: shape.run_count as f64,
            direction: "shift",
            severity: detect::severity::MEDIUM,
            change_point_index: None,
        };
        let scope = FactScope {
            source: SampleSource::Runs,
            caller_system_id: None,
            window_start: win_start,
        };
        let change_point = Some(shape.first_seen);
        let detections = [primary.clone()];
        let facts = match gather_facts(
            db,
            candidate,
            &scope,
            change_point,
            false,
            &detections,
            &primary,
        )
        .await
        {
            Ok(f) => f,
            Err(e) => {
                tracing::warn!(error = %e, ai_system_id = %candidate.ai_system_id, "new shape: cause facts unreadable; evaluation skipped this tick");
                continue;
            }
        };
        let mut attribution = cause::attribute(&facts, change_point);
        let unexplained = attribution.cause == cause::kind::UNEXPLAINED;
        let mut detection = primary;
        if is_tool && unexplained {
            detection.severity = detect::severity::HIGH;
        }
        if let Some(ev) = attribution.evidence.as_object_mut() {
            ev.insert("bucket".into(), json!(baseline::ALL_BUCKET));
            ev.insert("tools".into(), json!(shape.tools));
            ev.insert("run_ids".into(), json!(shape.run_ids));
            ev.insert("run_count".into(), json!(shape.run_count));
        }
        let names = shape.tools.join(", ");
        let first_run = shape.run_ids.first().map_or("", String::as_str);
        let headline = if is_tool {
            format!(
                "A run used a tool this system has not used before: {names}. \
                 First seen on run {first_run}; {} run(s) so far.",
                shape.run_count
            )
        } else {
            format!(
                "A run used a combination of tools this system has not used before: {names}. \
                 First seen on run {first_run}; {} run(s) so far.",
                shape.run_count
            )
        };
        outcome.findings += 1;
        out.push(Finding {
            basis: super::basis::RUNS,
            caller_system_id: None,
            bucket: baseline::ALL_BUCKET.to_string(),
            detection,
            attribution,
            change_point,
            evidence: Evidence {
                baseline_from: reference.first().map(|s| s.started_at),
                observed_from: Some(shape.first_seen),
                observed_to: observed.last().map(|s| s.started_at),
                change_point,
            },
            headline: Some(headline),
        });
    }
    out
}

fn severity_rank(s: &str) -> u8 {
    match s {
        "critical" => 4,
        "high" => 3,
        "medium" => 2,
        _ => 1,
    }
}

fn baseline_ref(
    computed: &[baseline::ComputedBaseline],
    bucket: &str,
    metric_key: &str,
) -> BaselineRef {
    computed
        .iter()
        .find(|c| c.metric_key == metric_key && c.bucket == bucket)
        .map(|c| BaselineRef {
            median: c.median,
            mad: c.mad,
            distribution: c.distribution.clone(),
            sample_count: c.sample_count,
            is_ready: c.is_ready(),
        })
        .unwrap_or_default()
}

/// Which population a finding came from — decides which cause facts apply.
struct FactScope<'a> {
    source: SampleSource,
    /// Set for a per-caller service cohort.
    caller_system_id: Option<&'a str>,
    window_start: DateTime<Utc>,
}

async fn gather_facts(
    db: &PgPool,
    candidate: &DriftCandidate,
    scope: &FactScope<'_>,
    change_point: Option<DateTime<Utc>>,
    input_moved: bool,
    detections: &[Detection],
    primary: &Detection,
) -> Result<CauseFacts, sqlx::Error> {
    let mut facts = CauseFacts {
        input_drifted: input_moved && primary.drift_class != drift_class::INPUT,
        correlated_metrics: detect::correlated_metric_names(detections, &primary.metric_key),
        ..Default::default()
    };

    let Some(cp) = change_point else {
        return Ok(facts);
    };
    let (t, sys) = (
        candidate.tenant_id.as_str(),
        candidate.ai_system_id.as_str(),
    );
    let segments = scope.source == SampleSource::Segments;

    // A window a human declared is the first thing to rule out:
    // its samples are never judged, so a change point at its edge is the
    // incident's own onset or tail.
    facts.incident_window = cause::find_incident_window(db, t, sys, cp).await?;
    if facts.incident_window.is_some() {
        return Ok(facts);
    }

    // The ledger records the contract every served call ran
    // under, so a service promotion is the first thing to rule out.
    if segments {
        facts.contract_change = cause::find_segment_contract_change(db, t, sys, cp).await?;
    }
    if facts.contract_change.is_some() {
        return Ok(facts);
    }
    // Then the agent's own release: a redeploy is the most
    // common change there is, and the rung names both releases.
    facts.release_change = cause::find_release_change(db, t, sys, cp, segments).await?;
    if facts.release_change.is_some() {
        return Ok(facts);
    }

    facts.model_change = cause::find_model_retirement(db, t, sys, cp).await?;
    if facts.model_change.is_none() {
        // A prompt edit is the next most common no-release change.
        facts.prompt_change = cause::find_prompt_change(db, t, sys, cp).await?;
    }
    if facts.model_change.is_none() && facts.prompt_change.is_none() {
        facts.dependency_change = cause::find_dependency_change(db, t, sys, cp).await?;
    }
    let explained = |f: &CauseFacts| {
        f.model_change.is_some() || f.prompt_change.is_some() || f.dependency_change.is_some()
    };
    if segments && !explained(&facts) {
        facts.composition_change =
            cause::find_segment_composition_change(db, t, sys, scope.caller_system_id, cp).await?;
    }
    if !explained(&facts) && facts.composition_change.is_none() {
        facts.corpus_change = cause::find_corpus_change(db, t, sys, cp).await?;
    }
    // A new caller explains the aggregate's shift; a per-caller cohort is
    // by construction one caller, so the rung does not apply to it.
    if segments && scope.caller_system_id.is_none() {
        facts.new_caller = cause::find_new_caller(db, t, sys, scope.window_start, cp).await?;
    }

    Ok(facts)
}

/// Persist a finding and raise its alert, honouring the per-class cooldown.
///
/// The cooldown is per system, per drift class, **per basis**: a service's
/// own runs and the calls it serves are different populations and one must
/// not silence the other. Within the segment basis an aggregate event also
/// silences every caller's (it is the same change, already reported), while
/// a caller's event silences only that caller. Events the control plane
/// writes under `metric::CP_DEPENDENCY_PREFIX` are ignored here.
///
/// Before the cooldown: a key whose LATEST event was closed by a
/// human with a kind is not re-opened by evidence that closure settled
/// ([`super::exclusion::Closure::settles`]). The sample exclusion already keeps
/// that evidence out of detection; this is the independent second line, so
/// an operator's restore cannot be undone by the next sweep even if an
/// epoch or window row is missing.
pub async fn record(
    db: &PgPool,
    candidate: &DriftCandidate,
    finding: &Finding,
    now: DateTime<Utc>,
    outcome: &mut SweepOutcome,
) {
    let Finding {
        basis,
        caller_system_id,
        bucket,
        detection,
        attribution,
        change_point,
        evidence,
        headline,
    } = finding;
    let caller = caller_system_id.as_deref();
    let is_shape = metric::is_fact_finding(&detection.metric_key);

    match Closures::load(db, &candidate.tenant_id, &candidate.ai_system_id, basis).await {
        Ok(closures) if closures.settles(&detection.metric_key, caller, evidence) => {
            outcome.settled += 1;
            return;
        }
        Ok(_) => {}
        Err(e) => {
            // Not recorded: without the closures we cannot tell new
            // evidence from evidence a human already settled.
            tracing::warn!(error = %e, "drift sweep: closures unreadable, finding not recorded");
            return;
        }
    }

    let recent: Result<Option<(i64,)>, sqlx::Error> = if is_shape {
        // A first-seen shape is one fact: it stays one finding while it is
        // open, however many more runs show it, and does not touch or read the
        // statistical findings' per-class cooldown.
        sqlx::query_as(
            "SELECT COUNT(*) FROM ai_system_drift_events \
             WHERE tenant_id = $1 AND ai_system_id = $2 AND metric_key = $3 \
               AND status IN ($4, $5)",
        )
        .bind(&candidate.tenant_id)
        .bind(&candidate.ai_system_id)
        .bind(&detection.metric_key)
        .bind(super::drift_status::ACTIVE)
        .bind(super::drift_status::ACKNOWLEDGED)
        .fetch_optional(db)
        .await
    } else {
        sqlx::query_as(
            "SELECT COUNT(*) FROM ai_system_drift_events \
             WHERE tenant_id = $1 AND ai_system_id = $2 AND drift_class = $3 \
               AND detected_at > $4 AND basis = $5 \
               AND (caller_system_id IS NULL OR caller_system_id = $6) \
               AND metric_key NOT LIKE $7 AND metric_key NOT LIKE $8 \
               AND metric_key NOT LIKE $9",
        )
        .bind(&candidate.tenant_id)
        .bind(&candidate.ai_system_id)
        .bind(detection.drift_class)
        .bind(now - Duration::seconds(ALERT_COOLDOWN_SECONDS))
        .bind(*basis)
        .bind(caller)
        // The control plane's `dependency:*` findings on this system are not the
        // core's own drift: counting them here let a CP dependency finding
        // silence the core's own dependency-class alert for the cooldown.
        .bind(format!("{}%", super::metric::CP_DEPENDENCY_PREFIX))
        .bind(format!("{}%", super::metric::SHAPE_PREFIX))
        .bind(format!("{}%", super::metric::REACH_PREFIX))
        .fetch_optional(db)
        .await
    };
    // An unreadable cooldown is not "no recent alert": recording now
    // would raise the same alert twice. Skipped; the next tick decides.
    let recent = match recent {
        Ok(r) => r,
        Err(e) => {
            crate::services::db_errors::record(&e);
            return;
        }
    };

    if matches!(recent, Some((n,)) if n > 0) {
        outcome.suppressed += 1;
        return;
    }

    let event_id = format!("drift-{}", ulid::Ulid::new());
    let insert = sqlx::query(
        r#"
        INSERT INTO ai_system_drift_events
            (id, tenant_id, ai_system_id, drift_class, metric_key, detected_at,
             change_point_at, baseline_value, observed_value, effect_size,
             direction, severity, attributed_cause, cause_confidence,
             cause_evidence, correlated_metrics, status, basis, caller_system_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                $15, $16, 'active', $17, $18)
        "#,
    )
    .bind(&event_id)
    .bind(&candidate.tenant_id)
    .bind(&candidate.ai_system_id)
    .bind(detection.drift_class)
    .bind(&detection.metric_key)
    .bind(now)
    .bind(*change_point)
    .bind(detection.baseline_value)
    .bind(detection.observed_value)
    .bind(detection.effect_size)
    .bind(detection.direction)
    .bind(detection.severity)
    .bind(attribution.cause)
    .bind(attribution.confidence)
    .bind(&attribution.evidence)
    .bind(json!(
        attribution
            .evidence
            .get("correlated_metrics")
            .cloned()
            .unwrap_or(json!([]))
    ))
    .bind(*basis)
    .bind(caller)
    .execute(db)
    .await;

    if let Err(e) = insert {
        tracing::warn!(error = %e, "drift sweep: failed to record event");
        return;
    }

    let name = candidate
        .system_name
        .clone()
        .unwrap_or_else(|| candidate.ai_system_id.clone());

    let category = match detection.drift_class {
        drift_class::COST => AlertCategory::AiSystemCost,
        drift_class::DEPENDENCY | drift_class::MODEL => AlertCategory::AiSystemDependency,
        _ => AlertCategory::AiSystemBehaviour,
    };

    let severity = match detection.severity {
        "critical" => Severity::Critical,
        "high" => Severity::High,
        "medium" => Severity::Medium,
        _ => Severity::Low,
    };

    let caller_name = match caller {
        Some(c) => Some(system_display_name(db, &candidate.tenant_id, c).await),
        None => None,
    };
    let (mut title, mut scope_line) = alert_scope(&name, basis, caller_name.as_deref());
    if detection.metric_key.starts_with(metric::SHAPE_PREFIX) {
        title = format!("AI System '{name}' used a new tool");
    } else if detection.metric_key.starts_with(metric::REACH_PREFIX) {
        title = format!("AI System '{name}': a requester reached too many records");
    }
    if bucket != baseline::ALL_BUCKET {
        scope_line = format!("{scope_line}Measured on '{bucket}' runs only. ");
    }

    // The message leads with the cause, not the statistic. That ordering
    // is the entire point of the phase.
    let message = if let Some(line) = headline {
        format!("{line}\n\n{}", attribution.summary)
    } else {
        format!(
            "{}\n\n{}Metric: {} ({} {}). Baseline {:.4} → observed {:.4}.{}",
            attribution.summary,
            scope_line,
            detection.metric_key,
            detection.direction,
            if detection.direction == "shift" {
                "in distribution"
            } else {
                "vs baseline"
            },
            detection.baseline_value.unwrap_or(f64::NAN),
            detection.observed_value.unwrap_or(f64::NAN),
            change_point
                .map(|cp| format!(" Change point: {}.", cp.to_rfc3339()))
                .unwrap_or_default(),
        )
    };

    let input = SystemAlertInput {
        tenant_id: Some(candidate.tenant_id.clone()),
        severity,
        category,
        source: format!("ai_system.{}", candidate.ai_system_id),
        resource_type: Some("ai_system".to_string()),
        resource_id: Some(candidate.ai_system_id.clone()),
        resource_name: candidate.system_name.clone(),
        // The dedup key includes the error code, so the bases (and each
        // caller) must not collapse into one alert row.
        error_code: Some(alert_error_code(detection, basis, caller, bucket)),
        title,
        message,
        context: Some(json!({
            "drift_event_id": event_id,
            "drift_class": detection.drift_class,
            "metric_key": detection.metric_key,
            "basis": basis,
            "caller_system_id": caller,
            "bucket": bucket,
            "effect_size": detection.effect_size,
            "attributed_cause": attribution.cause,
            "cause_confidence": attribution.confidence,
            "cause_evidence": attribution.evidence,
            "change_point_at": change_point.map(|c| c.to_rfc3339()),
        })),
    };

    match system_alerts::raise(db, input).await {
        Ok(res) => {
            outcome.alerted += 1;
            let _ =
                sqlx::query("UPDATE ai_system_drift_events SET system_alert_id = $1 WHERE id = $2")
                    .bind(&res.alert_id)
                    .bind(&event_id)
                    .execute(db)
                    .await;
            tracing::info!(
                ai_system_id = %candidate.ai_system_id,
                metric = %detection.metric_key,
                basis = %basis,
                caller_system_id = caller.unwrap_or("-"),
                cause = %attribution.cause,
                "drift finding raised"
            );
        }
        Err(e) => {
            tracing::warn!(error = %e, "drift sweep: failed to raise alert");
        }
    }
}

/// The alert's `error_code` — its dedup identity. Unchanged for runs
/// (`class:metric`) so existing alert rows keep deduplicating.
fn alert_error_code(
    detection: &Detection,
    basis: &str,
    caller: Option<&str>,
    bucket: &str,
) -> String {
    let mut base = format!("{}:{}", detection.drift_class, detection.metric_key);
    // An outcome bucket's alert never collapses into the
    // aggregate's (whose identity is unchanged).
    if bucket != baseline::ALL_BUCKET {
        base = format!("{base}@{bucket}");
    }
    match (basis, caller) {
        (b, _) if b != super::basis::SEGMENTS => base,
        (b, None) => format!("{base}:{b}"),
        (b, Some(c)) => format!("{base}:{b}:{c}"),
    }
}

/// Title and a scope line saying which population moved.
fn alert_scope(name: &str, basis: &str, caller_name: Option<&str>) -> (String, String) {
    if basis != super::basis::SEGMENTS {
        return (
            format!("AI System '{name}' behaviour changed"),
            String::new(),
        );
    }
    match caller_name {
        None => (
            format!("AI System '{name}' behaviour changed as a shared service"),
            "Measured across every system that calls it. ".to_string(),
        ),
        Some(c) => (
            format!("AI System '{name}' behaviour changed when called by '{c}'"),
            format!("Measured on calls from '{c}' only. "),
        ),
    }
}

/// Cosmetic: on a failed read (logged and counted) the alert names the id.
async fn system_display_name(db: &PgPool, tenant_id: &str, id: &str) -> String {
    sqlx::query_as::<_, (String,)>(
        "SELECT COALESCE(display_name, name) FROM resource_groups WHERE tenant_id = $1 AND id = $2",
    )
    .bind(tenant_id)
    .bind(id)
    .fetch_optional(db)
    .await
    .ok_logged()
    .flatten()
    .map(|(n,)| n)
    .unwrap_or_else(|| id.to_string())
}

/// Sweep every watched AI System.
pub async fn sweep(db: &PgPool) -> SweepOutcome {
    let now = Utc::now();
    let candidates = load_candidates(db, super::BASELINE_WINDOW_DAYS).await;
    let mut outcome = SweepOutcome {
        checked: candidates.len(),
        ..Default::default()
    };

    for c in &candidates {
        for finding in sweep_system(db, c, now, &mut outcome).await {
            record(db, c, &finding, now, &mut outcome).await;
        }
    }
    outcome
}

/// Background loop.
pub async fn run_drift_loop(
    db: PgPool,
    interval_secs: u64,
    shutdown: tokio_util::sync::CancellationToken,
) {
    let mut ticker = tokio::time::interval(std::time::Duration::from_secs(interval_secs));
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);

    tracing::info!(
        interval_secs,
        min_runs_for_baseline = MIN_RUNS_FOR_BASELINE,
        "AI System drift detector started"
    );

    loop {
        tokio::select! {
            _ = shutdown.cancelled() => {
                tracing::info!("AI System drift detector received shutdown signal");
                return;
            }
            _ = ticker.tick() => {
                if let Err(e) = crate::services::system_ops_beacon::write_beacon(
                    &db,
                    crate::services::system_ops_beacon::schedulers::AI_SYSTEM_DRIFT,
                ).await {
                    tracing::warn!(error = %e, "drift beacon write failed");
                }

                let outcome = sweep(&db).await;
                // On the same tick: cross-run record reach.
                let (rules, reach_findings) =
                    crate::services::record_reach::sweep(&db, Utc::now()).await;
                if reach_findings > 0 {
                    tracing::info!(rules, reach_findings, "reach sweep raised findings");
                }
                // INFO only when this sweep actually raised something. Two
                // reasons the old condition was wrong: a finding suppressed by
                // cooldown means we already told someone, and `learning > 0`
                // is a *steady state* — every newly-registered system sits in
                // learning until its baselines are ready, so a tenant that
                // onboarded two systems logged INFO every 15 minutes
                // indefinitely while reporting zero findings. Neither is an
                // on-call signal. The per-finding raise site logs INFO once
                // per event; learning progress belongs in the UI, which shows
                // it, not in the operator's log stream.
                if outcome.alerted > 0 {
                    tracing::info!(
                        checked = outcome.checked,
                        learning = outcome.learning,
                        findings = outcome.findings,
                        alerted = outcome.alerted,
                        suppressed = outcome.suppressed,
                        "drift sweep raised findings"
                    );
                } else if outcome.findings > 0 || outcome.learning > 0 {
                    tracing::debug!(
                        checked = outcome.checked,
                        learning = outcome.learning,
                        findings = outcome.findings,
                        suppressed = outcome.suppressed,
                        "drift sweep complete"
                    );
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn det(metric_key: &str, class: &'static str, sev: &'static str, effect: f64) -> Detection {
        Detection {
            metric_key: metric_key.into(),
            drift_class: class,
            baseline_value: Some(1.0),
            observed_value: Some(2.0),
            effect_size: effect,
            direction: "up",
            severity: sev,
            change_point_index: Some(0),
        }
    }

    #[test]
    fn severity_ranking_is_total() {
        assert!(severity_rank("critical") > severity_rank("high"));
        assert!(severity_rank("high") > severity_rank("medium"));
        assert!(severity_rank("medium") > severity_rank("low"));
        assert_eq!(severity_rank("nonsense"), severity_rank("low"));
    }

    #[test]
    fn the_most_severe_symptom_becomes_the_finding() {
        // A model change trips several metrics at once; four alerts for
        // one cause is how an operator learns to filter the category out.
        let mut ds = [
            det(metric::DURATION_MS, drift_class::BEHAVIOURAL, "low", 3.1),
            det(metric::COST_PER_RUN, drift_class::COST, "critical", 12.0),
            det(
                metric::ACTION_COUNT,
                drift_class::BEHAVIOURAL,
                "medium",
                5.0,
            ),
        ];
        ds.sort_by(|a, b| {
            severity_rank(b.severity)
                .cmp(&severity_rank(a.severity))
                .then(b.effect_size.partial_cmp(&a.effect_size).unwrap())
        });
        assert_eq!(ds[0].metric_key, metric::COST_PER_RUN);
    }

    #[test]
    fn ties_are_broken_by_effect_size() {
        let mut ds = [
            det(metric::ACTION_COUNT, drift_class::BEHAVIOURAL, "high", 4.0),
            det(metric::DURATION_MS, drift_class::BEHAVIOURAL, "high", 9.0),
        ];
        ds.sort_by(|a, b| {
            severity_rank(b.severity)
                .cmp(&severity_rank(a.severity))
                .then(b.effect_size.partial_cmp(&a.effect_size).unwrap())
        });
        assert_eq!(ds[0].metric_key, metric::DURATION_MS);
    }

    #[test]
    fn input_drift_is_not_blamed_on_itself() {
        // When the primary finding *is* the input metric, the ladder must
        // not report "the inputs moved" as the explanation for the inputs
        // moving.
        let detections = vec![det(
            metric::FIRST_PROMPT_TOKENS,
            drift_class::INPUT,
            "medium",
            5.0,
        )];
        let primary = detections[0].clone();
        let input_moved = detect::input_co_moved(&detections);
        assert!(input_moved);
        let would_set = input_moved && primary.drift_class != drift_class::INPUT;
        assert!(!would_set, "circular attribution");
    }

    #[test]
    fn behavioural_drift_alongside_input_drift_is_attributed_to_input() {
        let detections = vec![
            det(metric::ACTION_COUNT, drift_class::BEHAVIOURAL, "high", 8.0),
            det(
                metric::FIRST_PROMPT_TOKENS,
                drift_class::INPUT,
                "medium",
                5.0,
            ),
        ];
        let primary = detections[0].clone();
        let input_moved = detect::input_co_moved(&detections);
        let facts = CauseFacts {
            input_drifted: input_moved && primary.drift_class != drift_class::INPUT,
            ..Default::default()
        };
        assert_eq!(
            cause::attribute(&facts, None).cause,
            cause::kind::INPUT_DRIFT
        );
    }

    #[test]
    fn the_candidate_query_excludes_untrustworthy_runs() {
        assert!(CANDIDATES_SQL.contains("NOT ar.is_replay"));
        assert!(
            CANDIDATES_SQL
                .contains("chain_integrity IN ('intact', 'client_asserted', 'observed_external')"),
            "candidate counting must match baseline eligibility"
        );
        assert!(CANDIDATES_SQL.contains("baseline_opt_out"));
    }

    #[test]
    fn observation_window_is_shorter_than_the_baseline_window() {
        // A change must show up against a longer history to count.
        //
        // Both sides are consts, so this is decided at compile time rather
        // than by running the test — `const _: () = assert!(..)` says that
        // and fails the build instead of the suite. A runtime `assert!` on
        // two constants is a check that can never have been false by the
        // time anyone reads the result.
        const _: () = assert!(OBSERVATION_WINDOW_HOURS < super::super::BASELINE_WINDOW_DAYS * 24);
    }

    // -- the segment basis ---------------------------------------------------

    #[test]
    fn run_alerts_keep_their_dedup_identity() {
        // Existing alert rows dedupe on `class:metric`; changing it for the
        // runs basis would re-raise every standing run alert once.
        let d = det(metric::ACTION_COUNT, drift_class::BEHAVIOURAL, "high", 4.0);
        assert_eq!(
            alert_error_code(&d, super::super::basis::RUNS, None, "all"),
            "behavioural:action_count"
        );
    }

    #[test]
    fn segment_alerts_never_collapse_into_run_or_other_caller_alerts() {
        let d = det(metric::ACTION_COUNT, drift_class::BEHAVIOURAL, "high", 4.0);
        let seg = super::super::basis::SEGMENTS;
        let codes = [
            alert_error_code(&d, super::super::basis::RUNS, None, "all"),
            alert_error_code(&d, seg, None, "all"),
            alert_error_code(&d, seg, Some("sys-a"), "all"),
            alert_error_code(&d, seg, Some("sys-b"), "all"),
        ];
        let unique: std::collections::BTreeSet<_> = codes.iter().collect();
        assert_eq!(unique.len(), codes.len(), "{codes:?}");
    }

    #[test]
    fn an_outcome_bucket_alert_has_its_own_identity() {
        let d = det(metric::ACTION_COUNT, drift_class::BEHAVIOURAL, "high", 4.0);
        let runs = super::super::basis::RUNS;
        assert_eq!(
            alert_error_code(&d, runs, None, "all"),
            "behavioural:action_count",
            "the aggregate keeps its dedup identity"
        );
        assert_eq!(
            alert_error_code(&d, runs, None, "app.loan_approved"),
            "behavioural:action_count@app.loan_approved"
        );
    }

    #[test]
    fn a_service_alert_says_which_population_moved() {
        let (t, line) = alert_scope("Fraud screener", super::super::basis::SEGMENTS, None);
        assert!(t.contains("shared service"));
        assert!(line.contains("every system that calls it"));
        let (t, line) = alert_scope(
            "Fraud screener",
            super::super::basis::SEGMENTS,
            Some("Loan desk"),
        );
        assert!(t.contains("called by 'Loan desk'"));
        assert!(line.contains("'Loan desk' only"));
        let (t, line) = alert_scope("Loan desk", super::super::basis::RUNS, None);
        assert_eq!(t, "AI System 'Loan desk' behaviour changed");
        assert!(line.is_empty());
    }
}
