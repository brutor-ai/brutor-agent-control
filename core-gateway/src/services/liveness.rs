// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! AI System liveness.
//!
//! > *"We found out on Thursday it had been dead since Monday."*
//!
//! Every other signal in the platform is a function of traffic that
//! arrived. An AI System that stops calling therefore looks **perfect** on
//! all of them: its error rate is zero, its cost is zero, its latency is
//! zero, because there is nothing to be wrong. Liveness is the only signal
//! that fires on the *absence* of traffic, which is why it cannot live on
//! a request path and has to be swept for.
//!
//! ## Where this runs
//!
//! Silence detection runs here, in the gateway.
//! There is no control-plane scheduler — every periodic loop in the
//! platform lives here in the core, registered in
//! `system_ops_beacon::schedulers` so the System Operations Console can
//! SEE it: each tick writes a beacon row, and the console renders liveness
//! and last-run from that. Pause and resume are advisory — the console
//! records the operator's intent in `system_operations` and publishes
//! `brutor:scheduler:pause`, but this loop has no pause flag to honour and
//! keeps ticking. (Said plainly here because the previous wording claimed
//! otherwise, and a detector an operator believes they have paused is
//! worse than one they know they cannot.) The alert-raising machinery
//! (`services::system_alerts`) is here too. The RFC's actual requirement —
//! that detection fire on absence rather than on a request — is satisfied
//! either way; only the address was wrong.
//!
//! ## The detectors
//!
//! | Detector | Fires when | Guards against |
//! |---|---|---|
//! | `silence` | no run started within `window + grace` | the dead system nobody noticed |
//! | `volume_collapse` | ran, but far below the expected rate | the half-dead system |
//! | `stall` | runs start and never finish | the hung system |
//! | `hard_fail_cliff` | almost everything in the window errored | the broken system |
//!
//! ## Alert-fatigue rules, enforced here rather than left to the UI
//!
//! Alert fatigue is the critical risk to this whole
//! feature: *if v1 pages people about nothing, there is no v2.* So:
//!
//!   * `sporadic` and `on_demand` systems are **never** silence-alerted. A
//!     monthly batch job is supposed to be quiet.
//!   * A system that has never run is not silent — it is unstarted. There
//!     is nothing to have stopped.
//!   * Re-alerting is suppressed for a cooldown while a system stays
//!     silent; one alert per outage, not one per tick.
//!   * Share-based detectors need a floor of runs before a share means
//!     anything. Two runs both failing is not a cliff, it is Tuesday.

use std::sync::LazyLock;

use chrono::{DateTime, Utc};
use sqlx::{FromRow, PgPool};

use crate::services::drift::exclusion;

use crate::services::system_alerts::{self, AlertCategory, Severity, SystemAlertInput};

// ---------------------------------------------------------------------------
// Wire contract — mirrored in app/database/models/ai_system_liveness.py
// ---------------------------------------------------------------------------

pub mod mode {
    pub const CONTINUOUS: &str = "continuous";
    pub const SCHEDULED: &str = "scheduled";
    pub const SPORADIC: &str = "sporadic";
    pub const ON_DEMAND: &str = "on_demand";
}

pub mod detector {
    pub const SILENCE: &str = "silence";
    pub const MISSED_SCHEDULE: &str = "missed_schedule";
    pub const VOLUME_COLLAPSE: &str = "volume_collapse";
    pub const STALL: &str = "stall";
    pub const HARD_FAIL_CLIFF: &str = "hard_fail_cliff";

    pub const ALL: [&str; 5] = [
        SILENCE,
        MISSED_SCHEDULE,
        VOLUME_COLLAPSE,
        STALL,
        HARD_FAIL_CLIFF,
    ];
}

/// Share of a window's runs that must be abandoned before a system counts
/// as stalled. High on purpose: a few abandoned runs usually indict the
/// client, not the system.
pub const STALL_ABANDONED_SHARE: f64 = 0.5;

/// Share of a window's runs that must be errored to count as a cliff.
pub const HARD_FAIL_SHARE: f64 = 0.8;

/// Below this many runs, a share is not evidence.
pub const MIN_RUNS_FOR_SHARE_DETECTORS: i64 = 5;

/// A system running at or below this fraction of its expected rate has
/// collapsed rather than merely dipped.
pub const VOLUME_COLLAPSE_FRACTION: f64 = 0.34;

/// How long before the same system may be alerted again for the same
/// condition. Without this, a system silent for a week produces one alert
/// every tick — which is how a category gets muted.
pub const ALERT_COOLDOWN_SECONDS: i64 = 3600;

// ---------------------------------------------------------------------------
// Inputs
// ---------------------------------------------------------------------------

/// One AI System's expectation joined to what actually happened in its
/// window. Everything the decision needs, so the decision itself is pure.
#[derive(Debug, Clone, FromRow)]
pub struct LivenessCandidate {
    pub id: String,
    pub tenant_id: String,
    pub ai_system_id: String,
    pub system_name: Option<String>,
    pub system_kind: Option<String>,
    pub mode: String,
    pub expected_min_runs: i32,
    pub window_seconds: i32,
    pub grace_seconds: i32,
    pub enabled: bool,
    /// Start of the most recent run, ever — not just in the window.
    pub last_run_at: Option<DateTime<Utc>>,
    /// Whether this system has *ever* produced a run.
    pub has_ever_run: bool,
    /// Every run in the window — what `volume_collapse` counts: a run inside
    /// a declared incident window still proves the system is alive.
    pub runs_in_window: i64,
    /// Runs in the window that lie inside a declared incident
    /// window. Not judged: the share detectors grade only the rest.
    pub excluded_in_window: i64,
    /// Judged runs only (outside every incident window).
    pub abandoned_in_window: i64,
    /// Judged runs only (outside every incident window).
    pub errored_in_window: i64,
    pub last_alert_at: Option<DateTime<Utc>>,
}

/// What a detector concluded.
#[derive(Debug, Clone, PartialEq)]
pub struct LivenessFinding {
    pub detector: &'static str,
    pub severity: Severity,
    pub title: String,
    pub message: String,
}

// ---------------------------------------------------------------------------
// The decision — pure, so it can be argued with against a table of cases
// ---------------------------------------------------------------------------

/// Evaluate one AI System. `None` means healthy (or deliberately unwatched).
///
/// Ordered most-severe-first and returns at most one finding: a system
/// that is silent is not *also* volume-collapsed, and reporting both would
/// double-page for one outage.
pub fn evaluate(candidate: &LivenessCandidate, now: DateTime<Utc>) -> Option<LivenessFinding> {
    if !candidate.enabled || candidate.mode == mode::ON_DEMAND {
        return None;
    }

    let name = candidate
        .system_name
        .clone()
        .unwrap_or_else(|| candidate.ai_system_id.clone());

    // --- silence ----------------------------------------------------------
    //
    // Only for modes where quiet is abnormal. `sporadic` systems are
    // supposed to be quiet; alerting on them is how an operator learns to
    // ignore the whole category.
    if candidate.mode == mode::CONTINUOUS || candidate.mode == mode::SCHEDULED {
        // A system that has never run is unstarted, not silent. There is
        // nothing to have stopped, and paging someone because they
        // registered a system before wiring it up is a false positive
        // they will remember.
        if candidate.has_ever_run {
            let deadline = i64::from(candidate.window_seconds) + i64::from(candidate.grace_seconds);
            let quiet_for = candidate
                .last_run_at
                .map(|t| (now - t).num_seconds())
                .unwrap_or(i64::MAX);

            if quiet_for > deadline {
                let detector = if candidate.mode == mode::SCHEDULED {
                    detector::MISSED_SCHEDULE
                } else {
                    detector::SILENCE
                };
                return Some(LivenessFinding {
                    detector,
                    // A silent system with a schedule is worse than a
                    // quiet continuous one: something was supposed to
                    // happen at a known time and did not.
                    severity: if candidate.mode == mode::SCHEDULED {
                        Severity::High
                    } else {
                        Severity::Medium
                    },
                    title: format!("AI System '{name}' has gone silent"),
                    message: format!(
                        "No run started for {} (expected at least {} run(s) every {}s, \
                         plus {}s grace). Last run: {}.",
                        humanize_seconds(quiet_for),
                        candidate.expected_min_runs,
                        candidate.window_seconds,
                        candidate.grace_seconds,
                        candidate
                            .last_run_at
                            .map(|t| t.to_rfc3339())
                            .unwrap_or_else(|| "never".into()),
                    ),
                });
            }
        }
    }

    // --- hard-fail cliff --------------------------------------------------
    //
    // Before stall: a system where everything errors is more urgent than
    // one where things hang, and a run can be both.
    //
    // Both shares are over the JUDGED runs — runs inside a declared
    // incident window (an outage someone already explained) grade nothing,
    // so the same outage cannot keep paging as a cliff.
    let judged = candidate.runs_in_window - candidate.excluded_in_window;
    if judged >= MIN_RUNS_FOR_SHARE_DETECTORS {
        let error_share = candidate.errored_in_window as f64 / judged as f64;
        if error_share >= HARD_FAIL_SHARE {
            return Some(LivenessFinding {
                detector: detector::HARD_FAIL_CLIFF,
                severity: Severity::High,
                title: format!("AI System '{name}' is failing almost every run"),
                message: format!(
                    "{} of {} runs in the last {}s errored ({:.0}%).",
                    candidate.errored_in_window,
                    judged,
                    candidate.window_seconds,
                    error_share * 100.0,
                ),
            });
        }

        // --- stall --------------------------------------------------------
        //
        // Runs start and never finish. Distinguished from silence because
        // the fix is different: silence means nothing is calling, a stall
        // means something is calling and hanging.
        let abandoned_share = candidate.abandoned_in_window as f64 / judged as f64;
        if abandoned_share >= STALL_ABANDONED_SHARE {
            return Some(LivenessFinding {
                detector: detector::STALL,
                severity: Severity::Medium,
                title: format!("AI System '{name}' is stalling"),
                message: format!(
                    "{} of {} runs in the last {}s never finished ({:.0}%). Runs are \
                     starting and then going quiet — often a hung client rather than \
                     the system itself.",
                    candidate.abandoned_in_window,
                    judged,
                    candidate.window_seconds,
                    abandoned_share * 100.0,
                ),
            });
        }
    }

    // --- volume collapse --------------------------------------------------
    //
    // The half-dead case: still running, far below its own expectation.
    // Only meaningful for `continuous` — a scheduled job legitimately runs
    // exactly once.
    if candidate.mode == mode::CONTINUOUS
        && candidate.has_ever_run
        && candidate.runs_in_window > 0
        && candidate.expected_min_runs > 1
    {
        let floor = (f64::from(candidate.expected_min_runs) * VOLUME_COLLAPSE_FRACTION).floor();
        if (candidate.runs_in_window as f64) <= floor {
            return Some(LivenessFinding {
                detector: detector::VOLUME_COLLAPSE,
                severity: Severity::Medium,
                title: format!("AI System '{name}' volume has collapsed"),
                message: format!(
                    "{} run(s) in the last {}s against an expected minimum of {}. \
                     Still alive, but running at a fraction of its normal rate.",
                    candidate.runs_in_window, candidate.window_seconds, candidate.expected_min_runs,
                ),
            });
        }
    }

    None
}

/// Whether an alert may be raised, given when we last alerted.
///
/// One alert per outage, not one per tick. A system silent for a week
/// should not produce 336 alerts.
pub fn cooldown_elapsed(last_alert_at: Option<DateTime<Utc>>, now: DateTime<Utc>) -> bool {
    match last_alert_at {
        None => true,
        Some(t) => (now - t).num_seconds() >= ALERT_COOLDOWN_SECONDS,
    }
}

fn humanize_seconds(secs: i64) -> String {
    if secs >= 86400 {
        format!("{:.1} days", secs as f64 / 86400.0)
    } else if secs >= 3600 {
        format!("{:.1} hours", secs as f64 / 3600.0)
    } else {
        format!("{secs} seconds")
    }
}

// ---------------------------------------------------------------------------
// The sweep
// ---------------------------------------------------------------------------

/// Load every enabled expectation with its window's run facts.
///
/// The LEFT JOINs matter: a system with **no** runs at all must still come
/// back as a row, because "produced nothing" is exactly the case this
/// feature exists to catch. An INNER JOIN here would make the dead systems
/// invisible — the precise bug the whole phase is about.
///
/// The share counts (abandoned, errored) and the excluded count go
/// through the shared incident-window predicate
/// ([`exclusion::incident_window_predicate`], `{excl}`); the total does not.
const CANDIDATES_SQL_TEMPLATE: &str = r#"
SELECT
    e.id,
    e.tenant_id,
    e.ai_system_id,
    COALESCE(rg.display_name, rg.name)              AS system_name,
    rg.system_kind,
    e.mode,
    e.expected_min_runs,
    e.window_seconds,
    e.grace_seconds,
    e.enabled,
    ever.last_run_at,
    (ever.last_run_at IS NOT NULL)                  AS has_ever_run,
    COALESCE(win.runs, 0)                           AS runs_in_window,
    COALESCE(win.excluded, 0)                       AS excluded_in_window,
    COALESCE(win.abandoned, 0)                      AS abandoned_in_window,
    COALESCE(win.errored, 0)                        AS errored_in_window,
    e.last_alert_at
FROM ai_system_liveness_expectations e
JOIN resource_groups rg
  ON rg.id = e.ai_system_id AND rg.tenant_id = e.tenant_id
LEFT JOIN LATERAL (
    SELECT MAX(r.started_at) AS last_run_at
      FROM ai_system_runs r
     WHERE r.tenant_id = e.tenant_id
       AND r.ai_system_id = e.ai_system_id
       AND NOT r.is_replay
) ever ON TRUE
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS runs,
           COUNT(*) FILTER (WHERE NOT {excl})                                AS excluded,
           COUNT(*) FILTER (WHERE r.terminal_state = 'abandoned' AND {excl}) AS abandoned,
           COUNT(*) FILTER (WHERE r.terminal_state = 'errored'   AND {excl}) AS errored
      FROM ai_system_runs r
     WHERE r.tenant_id = e.tenant_id
       AND r.ai_system_id = e.ai_system_id
       AND NOT r.is_replay
       AND r.started_at > NOW() - MAKE_INTERVAL(secs => e.window_seconds)
) win ON TRUE
WHERE e.enabled = true
  AND e.mode <> 'on_demand'
  AND rg.is_active = true
-- Bounded, and ordered so the bound cannot starve anyone.
-- The two LATERAL aggregates run per candidate row, so an estate with
-- thousands of watched systems made this the most expensive query in the
-- process, every 60 s, on every replica. Least-recently-checked first means
-- a fleet larger than the cap is swept over consecutive ticks in rotation
-- rather than the first N being re-checked forever: the trade is detection
-- latency for a very large estate (ceil(N / SWEEP_MAX_CANDIDATES) ticks to
-- cover everything), not coverage. NULLS FIRST puts never-checked
-- expectations at the head, so a newly-watched system is evaluated on the
-- next tick rather than waiting out the rotation.
ORDER BY e.last_checked_at ASC NULLS FIRST
LIMIT $1
"#;

static CANDIDATES_SQL: LazyLock<String> = LazyLock::new(|| {
    CANDIDATES_SQL_TEMPLATE.replace(
        "{excl}",
        &exclusion::incident_window_predicate("r.tenant_id", "r.started_at", &["r.ai_system_id"]),
    )
});

/// How many expectations one tick will evaluate.
///
/// At the 60 s cadence this is 2,000 systems per minute — an estate that
/// large is already past the point where a single-process sweeper is the
/// right design, and the rotation in `CANDIDATES_SQL` keeps it correct
/// rather than merely fast in the meantime.
pub const SWEEP_MAX_CANDIDATES: i64 = 2_000;

/// Load the systems worth checking this tick.
pub async fn load_candidates(db: &PgPool) -> Vec<LivenessCandidate> {
    match sqlx::query_as::<_, LivenessCandidate>(CANDIDATES_SQL.as_str())
        .bind(SWEEP_MAX_CANDIDATES)
        .fetch_all(db)
        .await
    {
        Ok(rows) => rows,
        Err(e) => {
            tracing::warn!(error = %e, "liveness sweep: candidate query failed");
            Vec::new()
        }
    }
}

/// Outcome of one sweep, for the log line and tests.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct SweepOutcome {
    pub checked: usize,
    pub findings: usize,
    pub alerted: usize,
    pub suppressed: usize,
}

/// Check every watched AI System and raise alerts for what is wrong.
pub async fn sweep(db: &PgPool) -> SweepOutcome {
    let now = Utc::now();
    let candidates = load_candidates(db).await;
    let mut outcome = SweepOutcome {
        checked: candidates.len(),
        ..Default::default()
    };

    // Stamp the check regardless of outcome, so an operator can tell
    // "healthy" from "never evaluated" — which look identical if only
    // findings are recorded.
    //
    // One statement for the whole batch. This used to be a
    // single-row UPDATE per candidate inside the loop below, so a fleet of
    // 500 watched systems issued 500 round trips every 60 s, forever, on
    // every replica — for a column nothing reads in real time. Unnesting two
    // parallel arrays keeps the per-row values (each expectation keeps its
    // own `last_seen_run_at`) while costing one round trip.
    //
    // Stamped BEFORE the findings loop, matching the old ordering: a stamp
    // must land even for a candidate whose alert-raise then fails, or an
    // unreachable alerting backend would make every watched system look
    // "never evaluated".
    stamp_checked(db, &candidates, now).await;

    for c in &candidates {
        let Some(finding) = evaluate(c, now) else {
            continue;
        };
        outcome.findings += 1;

        if !cooldown_elapsed(c.last_alert_at, now) {
            outcome.suppressed += 1;
            continue;
        }

        let input = SystemAlertInput {
            tenant_id: Some(c.tenant_id.clone()),
            severity: finding.severity,
            category: AlertCategory::AiSystemLiveness,
            // Keyed by system *and* detector so a stalling system and a
            // silent one are separate alerts rather than one flapping row.
            source: format!("ai_system.{}", c.ai_system_id),
            resource_type: Some("ai_system".to_string()),
            resource_id: Some(c.ai_system_id.clone()),
            resource_name: c.system_name.clone(),
            error_code: Some(finding.detector.to_string()),
            title: finding.title.clone(),
            message: finding.message.clone(),
            context: Some(serde_json::json!({
                "detector": finding.detector,
                "mode": c.mode,
                "system_kind": c.system_kind,
                "window_seconds": c.window_seconds,
                "grace_seconds": c.grace_seconds,
                "expected_min_runs": c.expected_min_runs,
                "runs_in_window": c.runs_in_window,
                "excluded_in_window": c.excluded_in_window,
                "abandoned_in_window": c.abandoned_in_window,
                "errored_in_window": c.errored_in_window,
                "last_run_at": c.last_run_at.map(|t| t.to_rfc3339()),
            })),
        };

        match system_alerts::raise(db, input).await {
            Ok(raised) => {
                outcome.alerted += 1;
                // A liveness miss is also a human-review
                // finding; cross-linked to the system alert it raised.
                crate::services::assurance_inbox::raise_liveness_miss(
                    db,
                    &c.tenant_id,
                    &c.ai_system_id,
                    finding.detector,
                    finding.severity.as_str(),
                    &finding.title,
                    Some(&raised.alert_id),
                )
                .await;
                let _ = sqlx::query(
                    "UPDATE ai_system_liveness_expectations SET last_alert_at = $1 WHERE id = $2",
                )
                .bind(now)
                .bind(&c.id)
                .execute(db)
                .await;
                tracing::info!(
                    ai_system_id = %c.ai_system_id,
                    detector = %finding.detector,
                    "liveness finding raised"
                );
            }
            Err(e) => {
                tracing::warn!(
                    error = %e,
                    ai_system_id = %c.ai_system_id,
                    "liveness sweep: failed to raise alert"
                );
            }
        }
    }

    outcome
}

/// Record that this tick evaluated `candidates`, in one statement.
///
/// Best-effort like the rest of the sweep: a failed stamp means an operator
/// sees a stale "last checked" timestamp for a minute, which is strictly
/// less harmful than a sweep that aborts partway and raises nothing.
async fn stamp_checked(db: &PgPool, candidates: &[LivenessCandidate], now: DateTime<Utc>) {
    if candidates.is_empty() {
        return;
    }
    let ids: Vec<String> = candidates.iter().map(|c| c.id.clone()).collect();
    let seen: Vec<Option<DateTime<Utc>>> = candidates.iter().map(|c| c.last_run_at).collect();

    if let Err(e) = sqlx::query(
        "UPDATE ai_system_liveness_expectations e \
         SET last_checked_at = $1, last_seen_run_at = s.seen \
         FROM UNNEST($2::text[], $3::timestamptz[]) AS s(id, seen) \
         WHERE e.id = s.id",
    )
    .bind(now)
    .bind(&ids)
    .bind(&seen)
    .execute(db)
    .await
    {
        tracing::warn!(
            error = %e,
            candidates = candidates.len(),
            "liveness sweep: batch check-stamp failed"
        );
    }
}

/// Background loop. Honours `shutdown_token` so it exits promptly.
pub async fn run_liveness_loop(
    db: PgPool,
    interval_secs: u64,
    shutdown: tokio_util::sync::CancellationToken,
) {
    let mut ticker = tokio::time::interval(std::time::Duration::from_secs(interval_secs));
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);

    tracing::info!(
        interval_secs,
        alert_cooldown_secs = ALERT_COOLDOWN_SECONDS,
        "AI System liveness detector started"
    );

    loop {
        tokio::select! {
            _ = shutdown.cancelled() => {
                tracing::info!("AI System liveness detector received shutdown signal");
                return;
            }
            _ = ticker.tick() => {
                // Beacon first, so the operator console shows the loop as
                // healthy on a tick that finds nothing — which is most of
                // them, and is the whole point.
                if let Err(e) = crate::services::system_ops_beacon::write_beacon(
                    &db,
                    crate::services::system_ops_beacon::schedulers::AI_SYSTEM_LIVENESS,
                ).await {
                    tracing::warn!(error = %e, "liveness beacon write failed");
                }

                let outcome = sweep(&db).await;
                // INFO only when this sweep actually raised something. A
                // finding that was suppressed by the per-outage cooldown is
                // the detector confirming it *already* told someone — and a
                // system stays silent for as long as it is dead, so at a 60s
                // cadence an unfixed outage that alerted once on Monday would
                // emit ~1,440 INFO lines a day through Thursday saying
                // `alerted: 0`. That is exactly the fatigue the detector's own
                // cooldown exists to prevent, reintroduced in the log stream.
                // The per-finding raise site already logs INFO once per
                // outage; totals stay observable via the beacon and metrics.
                if outcome.alerted > 0 {
                    tracing::info!(
                        checked = outcome.checked,
                        findings = outcome.findings,
                        alerted = outcome.alerted,
                        suppressed = outcome.suppressed,
                        "liveness sweep raised findings"
                    );
                } else if outcome.findings > 0 {
                    tracing::debug!(
                        checked = outcome.checked,
                        findings = outcome.findings,
                        suppressed = outcome.suppressed,
                        "liveness sweep found problems (all suppressed)"
                    );
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Duration;

    fn now() -> DateTime<Utc> {
        DateTime::parse_from_rfc3339("2026-07-27T12:00:00Z")
            .unwrap()
            .with_timezone(&Utc)
    }

    fn candidate(mode_str: &str) -> LivenessCandidate {
        LivenessCandidate {
            id: "live-1".into(),
            tenant_id: "t1".into(),
            ai_system_id: "sys-1".into(),
            system_name: Some("Nightly Sync".into()),
            system_kind: Some("integration".into()),
            mode: mode_str.into(),
            expected_min_runs: 1,
            window_seconds: 3600,
            grace_seconds: 300,
            enabled: true,
            last_run_at: Some(now() - Duration::minutes(5)),
            has_ever_run: true,
            runs_in_window: 10,
            excluded_in_window: 0,
            abandoned_in_window: 0,
            errored_in_window: 0,
            last_alert_at: None,
        }
    }

    // -- silence -----------------------------------------------------------

    #[test]
    fn healthy_system_produces_no_finding() {
        assert!(evaluate(&candidate(mode::CONTINUOUS), now()).is_none());
    }

    #[test]
    fn silence_fires_past_window_plus_grace() {
        let mut c = candidate(mode::CONTINUOUS);
        c.last_run_at = Some(now() - Duration::hours(3));
        c.runs_in_window = 0;
        let f = evaluate(&c, now()).expect("should fire");
        assert_eq!(f.detector, detector::SILENCE);
    }

    #[test]
    fn silence_does_not_fire_inside_the_grace_period() {
        let mut c = candidate(mode::CONTINUOUS);
        // 3600 window + 300 grace = 3900s; 3800s quiet is still fine.
        c.last_run_at = Some(now() - Duration::seconds(3800));
        c.runs_in_window = 0;
        assert!(
            evaluate(&c, now()).is_none(),
            "grace exists to absorb ordinary jitter; firing inside it is a false positive"
        );
    }

    #[test]
    fn scheduled_silence_is_a_missed_schedule_and_ranks_higher() {
        let mut c = candidate(mode::SCHEDULED);
        c.last_run_at = Some(now() - Duration::hours(5));
        c.runs_in_window = 0;
        let f = evaluate(&c, now()).expect("should fire");
        assert_eq!(f.detector, detector::MISSED_SCHEDULE);
        assert_eq!(
            f.severity,
            Severity::High,
            "something was due at a known time and did not happen"
        );
    }

    #[test]
    fn sporadic_systems_are_never_silence_alerted() {
        let mut c = candidate(mode::SPORADIC);
        c.last_run_at = Some(now() - Duration::days(40));
        c.runs_in_window = 0;
        assert!(
            evaluate(&c, now()).is_none(),
            "a monthly batch job is supposed to be quiet — alerting teaches \
             operators to mute the category"
        );
    }

    #[test]
    fn on_demand_systems_are_not_evaluated_at_all() {
        let mut c = candidate(mode::ON_DEMAND);
        c.last_run_at = Some(now() - Duration::days(400));
        c.runs_in_window = 0;
        c.errored_in_window = 0;
        assert!(evaluate(&c, now()).is_none());
    }

    #[test]
    fn disabled_expectation_is_not_evaluated() {
        let mut c = candidate(mode::CONTINUOUS);
        c.enabled = false;
        c.last_run_at = Some(now() - Duration::days(10));
        assert!(evaluate(&c, now()).is_none());
    }

    #[test]
    fn a_system_that_never_ran_is_unstarted_not_silent() {
        let mut c = candidate(mode::CONTINUOUS);
        c.has_ever_run = false;
        c.last_run_at = None;
        c.runs_in_window = 0;
        assert!(
            evaluate(&c, now()).is_none(),
            "paging because someone registered a system before wiring it up \
             is a false positive they will remember"
        );
    }

    // -- hard-fail cliff ---------------------------------------------------

    #[test]
    fn hard_fail_cliff_fires_when_nearly_everything_errors() {
        let mut c = candidate(mode::CONTINUOUS);
        c.runs_in_window = 10;
        c.errored_in_window = 9;
        let f = evaluate(&c, now()).expect("should fire");
        assert_eq!(f.detector, detector::HARD_FAIL_CLIFF);
        assert_eq!(f.severity, Severity::High);
    }

    #[test]
    fn a_couple_of_failures_is_not_a_cliff() {
        let mut c = candidate(mode::CONTINUOUS);
        c.runs_in_window = 4;
        c.errored_in_window = 4;
        assert!(
            evaluate(&c, now()).is_none(),
            "below the floor a share is not evidence — two runs failing is Tuesday"
        );
    }

    #[test]
    fn a_normal_error_rate_does_not_fire() {
        let mut c = candidate(mode::CONTINUOUS);
        c.runs_in_window = 100;
        c.errored_in_window = 10;
        assert!(evaluate(&c, now()).is_none());
    }

    // -- stall -------------------------------------------------------------

    #[test]
    fn stall_fires_when_runs_start_and_never_finish() {
        let mut c = candidate(mode::CONTINUOUS);
        c.runs_in_window = 10;
        c.abandoned_in_window = 6;
        let f = evaluate(&c, now()).expect("should fire");
        assert_eq!(f.detector, detector::STALL);
    }

    #[test]
    fn a_few_abandoned_runs_do_not_indict_the_system() {
        let mut c = candidate(mode::CONTINUOUS);
        c.runs_in_window = 20;
        c.abandoned_in_window = 3;
        assert!(evaluate(&c, now()).is_none());
    }

    #[test]
    fn runs_inside_a_declared_incident_window_grade_no_share() {
        // 10 runs, 8 of them inside an outage someone declared:
        // the 2 judged runs are below the floor, and the errored ones inside
        // the window were never counted (`errored_in_window` is judged-only).
        let mut c = candidate(mode::CONTINUOUS);
        c.runs_in_window = 10;
        c.excluded_in_window = 8;
        c.errored_in_window = 2;
        assert!(
            evaluate(&c, now()).is_none(),
            "an explained outage is not a cliff"
        );
        // The same judged failures over enough judged runs still fire.
        c.runs_in_window = 14;
        c.errored_in_window = 5;
        assert_eq!(
            evaluate(&c, now()).unwrap().detector,
            detector::HARD_FAIL_CLIFF
        );
        assert!(
            evaluate(&c, now())
                .unwrap()
                .message
                .starts_with("5 of 6 runs")
        );
    }

    #[test]
    fn the_share_counts_go_through_the_shared_exclusion() {
        let excl = exclusion::incident_window_predicate(
            "r.tenant_id",
            "r.started_at",
            &["r.ai_system_id"],
        );
        assert_eq!(CANDIDATES_SQL.matches(excl.as_str()).count(), 3);
        assert!(!CANDIDATES_SQL.contains("{excl}"));
    }

    #[test]
    fn cliff_outranks_stall_when_both_apply() {
        let mut c = candidate(mode::CONTINUOUS);
        c.runs_in_window = 10;
        c.errored_in_window = 9;
        c.abandoned_in_window = 6;
        assert_eq!(
            evaluate(&c, now()).unwrap().detector,
            detector::HARD_FAIL_CLIFF,
            "a system failing outright is more urgent than one hanging"
        );
    }

    // -- volume collapse ---------------------------------------------------

    #[test]
    fn volume_collapse_fires_for_a_half_dead_system() {
        let mut c = candidate(mode::CONTINUOUS);
        c.expected_min_runs = 100;
        c.runs_in_window = 5;
        let f = evaluate(&c, now()).expect("should fire");
        assert_eq!(f.detector, detector::VOLUME_COLLAPSE);
    }

    #[test]
    fn a_modest_dip_is_not_a_collapse() {
        let mut c = candidate(mode::CONTINUOUS);
        c.expected_min_runs = 100;
        c.runs_in_window = 80;
        assert!(evaluate(&c, now()).is_none());
    }

    #[test]
    fn scheduled_systems_are_not_volume_collapsed() {
        // A nightly job legitimately runs exactly once.
        let mut c = candidate(mode::SCHEDULED);
        c.expected_min_runs = 10;
        c.runs_in_window = 1;
        assert!(evaluate(&c, now()).is_none());
    }

    #[test]
    fn silence_outranks_volume_collapse() {
        let mut c = candidate(mode::CONTINUOUS);
        c.expected_min_runs = 100;
        c.runs_in_window = 0;
        c.last_run_at = Some(now() - Duration::hours(5));
        assert_eq!(
            evaluate(&c, now()).unwrap().detector,
            detector::SILENCE,
            "one outage, one alert — a silent system is not also collapsed"
        );
    }

    // -- cooldown ----------------------------------------------------------

    #[test]
    fn cooldown_blocks_re_alerting_while_a_system_stays_silent() {
        assert!(cooldown_elapsed(None, now()));
        assert!(!cooldown_elapsed(Some(now() - Duration::minutes(5)), now()));
        assert!(cooldown_elapsed(Some(now() - Duration::hours(2)), now()));
    }

    // -- wire contract -----------------------------------------------------

    #[test]
    fn modes_match_the_control_plane() {
        assert_eq!(mode::CONTINUOUS, "continuous");
        assert_eq!(mode::SCHEDULED, "scheduled");
        assert_eq!(mode::SPORADIC, "sporadic");
        assert_eq!(mode::ON_DEMAND, "on_demand");
    }

    #[test]
    fn detector_names_match_the_control_plane() {
        let mut all = detector::ALL.to_vec();
        all.sort_unstable();
        assert_eq!(
            all,
            vec![
                "hard_fail_cliff",
                "missed_schedule",
                "silence",
                "stall",
                "volume_collapse",
            ]
        );
    }

    #[test]
    fn thresholds_match_the_control_plane() {
        assert_eq!(STALL_ABANDONED_SHARE, 0.5);
        assert_eq!(HARD_FAIL_SHARE, 0.8);
        assert_eq!(MIN_RUNS_FOR_SHARE_DETECTORS, 5);
    }

    #[test]
    fn alert_category_wire_string() {
        assert_eq!(
            AlertCategory::AiSystemLiveness.as_str(),
            "ai_system_liveness"
        );
    }

    #[test]
    fn humanize_reads_naturally() {
        assert_eq!(humanize_seconds(45), "45 seconds");
        assert_eq!(humanize_seconds(7200), "2.0 hours");
        assert_eq!(humanize_seconds(172800), "2.0 days");
    }
}
