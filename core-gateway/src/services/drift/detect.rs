// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Comparing now against normal.
//!
//! ## Severity is never a p-value
//!
//! A statistically bulletproof 3% cost rise is `low`. A noisy 4× jump in
//! max-iteration exhaustion on a refund system is `critical`.
//!
//! Ranking findings by *confidence* rather than *consequence* is how a
//! drift feed becomes noise: the top of the list fills with tiny,
//! certain, uninteresting moves while the thing that will cost money sits
//! below the fold. So severity here is effect size × run volume ×
//! business impact — cost delta, autonomy level, EU AI Act risk tier —
//! and the p-value is not consulted at all.
//!
//! ## Sustained, not instantaneous
//!
//! Continuous metrics must both exceed the robust-z threshold *and* have a
//! Page–Hinkley change point. One excursion is a bad afternoon; a change
//! point is a system that started behaving differently and stayed that
//! way. Alerting on the first is how operators learn to ignore the
//! second.

use std::collections::BTreeMap;

use chrono::{DateTime, Utc};

use super::{
    JS_DIVERGENCE_THRESHOLD, MIN_ABSOLUTE_RATE_CHANGE, PAGE_HINKLEY_DELTA, PAGE_HINKLEY_THRESHOLD,
    ROBUST_Z_THRESHOLD, metric, profiles, stats,
};

pub mod severity {
    pub const CRITICAL: &str = "critical";
    pub const HIGH: &str = "high";
    pub const MEDIUM: &str = "medium";
    pub const LOW: &str = "low";
}

/// The baseline half of a comparison.
#[derive(Debug, Clone, Default)]
pub struct BaselineRef {
    pub median: Option<f64>,
    pub mad: Option<f64>,
    pub distribution: Option<BTreeMap<String, f64>>,
    pub sample_count: i64,
    pub is_ready: bool,
}

/// Business context that turns an effect size into a severity.
#[derive(Debug, Clone, Default)]
pub struct ImpactContext {
    /// Runs in the observation window — a shift across 5 runs is not the
    /// same finding as the same shift across 5,000.
    pub observed_runs: i64,
    /// Extra spend per run implied by the change, if it is a cost metric.
    pub cost_delta_usd: f64,
    /// `autonomous` systems act without a human in the loop.
    pub autonomy_level: Option<String>,
    /// EU AI Act tier from the AI System's resource group.
    pub risk_tier: Option<String>,
    /// Whether the system holds grants that can change the world.
    pub has_side_effecting_grants: bool,
}

/// A detected change, before the cause ladder runs.
#[derive(Debug, Clone, PartialEq)]
pub struct Detection {
    pub metric_key: String,
    pub drift_class: &'static str,
    pub baseline_value: Option<f64>,
    pub observed_value: Option<f64>,
    pub effect_size: f64,
    pub direction: &'static str,
    pub severity: &'static str,
    /// Index into the **trend** series passed to [`detect_continuous`], not
    /// the observation window. Resolve it with [`change_point_time`] against
    /// the matching trend timestamps.
    pub change_point_index: Option<usize>,
}

/// Compare a continuous metric against its baseline.
///
/// Returns `None` when the baseline is not ready — a `learning` baseline
/// must never produce a finding, whatever the numbers look like.
///
/// Two series, doing two different jobs:
///
/// * `observed_series` — the recent window. Answers *"is it different
///   now?"*, by magnitude against the baseline.
/// * `trend_series` — a longer trailing series that **contains** the
///   observed window. Answers *"when did it change?"*.
///
/// They must be separate. Locating the change point inside the observation
/// window alone means a change can only ever be dated to the last few
/// hours, and once the window has rolled fully past the change the series
/// is flat again and nothing is detected at all — a system that drifted on
/// Tuesday and stayed drifted looks perfectly healthy by Thursday.
///
/// It also breaks the headline claim of this phase. The cause ladder
/// matches candidate causes within ±2h of the change point, so a change
/// point that cannot be older than the observation window means a model
/// update three days ago can never be named as the cause — the ladder
/// would report `unexplained` for exactly the case it was built for.
pub fn detect_continuous(
    metric_key: &str,
    baseline: &BaselineRef,
    observed_series: &[f64],
    trend_series: &[f64],
    impact: &ImpactContext,
) -> Option<Detection> {
    if !baseline.is_ready || observed_series.is_empty() {
        return None;
    }
    let (median, mad) = (baseline.median?, baseline.mad.unwrap_or(0.0));

    let sorted = stats::sorted_finite(observed_series);
    let observed = stats::median(&sorted)?;
    let z = stats::robust_z(observed, median, mad);

    if z.abs() < ROBUST_Z_THRESHOLD {
        return None;
    }

    // A single excursion is a bad afternoon. Requiring a change point is
    // what distinguishes "started behaving differently and stayed that
    // way" from noise — and it is what gives the cause ladder a timestamp
    // to match against.
    //
    // Page–Hinkley runs on the *robust z-series*, not the raw values. Its
    // delta and threshold are absolute, so feeding raw metrics would make
    // them scale-dependent: 0.5 units of slack is enormous for a cost
    // measured in hundredths of a cent and invisible for a duration
    // measured in thousands of milliseconds. Cost drift would simply never
    // be detected, silently, while step-count drift worked fine.
    //
    // It runs over the *trend* series so the change point can predate the
    // observation window — see this function's docs.
    let z_series: Vec<f64> = trend_series
        .iter()
        .map(|v| stats::robust_z(*v, median, mad))
        .collect();
    let change_point = stats::PageHinkley::first_change_index(
        &z_series,
        PAGE_HINKLEY_DELTA,
        PAGE_HINKLEY_THRESHOLD,
    )?;

    Some(Detection {
        metric_key: metric_key.to_string(),
        drift_class: profiles::drift_class_for(metric_key),
        baseline_value: Some(median),
        observed_value: Some(observed),
        effect_size: z.abs(),
        direction: if observed > median { "up" } else { "down" },
        severity: grade(z.abs(), metric_key, impact),
        change_point_index: Some(change_point),
    })
}

/// Compare a categorical metric against its baseline.
pub fn detect_categorical(
    metric_key: &str,
    baseline: &BaselineRef,
    observed: &BTreeMap<String, f64>,
    impact: &ImpactContext,
) -> Option<Detection> {
    if !baseline.is_ready {
        return None;
    }
    let base_dist = baseline.distribution.as_ref()?;
    if base_dist.is_empty() || observed.is_empty() {
        return None;
    }

    let js = stats::js_divergence(base_dist, observed);
    if js < JS_DIVERGENCE_THRESHOLD {
        return None;
    }

    Some(Detection {
        metric_key: metric_key.to_string(),
        drift_class: profiles::drift_class_for(metric_key),
        baseline_value: None,
        observed_value: None,
        // JS is bounded 0..1; scale to the same rough range as a z-score
        // so severity grading can treat both consistently.
        effect_size: js * 10.0,
        direction: "shift",
        severity: grade(js * 10.0, metric_key, impact),
        change_point_index: None,
    })
}

/// Compare a rate metric.
pub fn detect_rate(
    metric_key: &str,
    baseline: &BaselineRef,
    observed_rate: f64,
    observed_n: i64,
    impact: &ImpactContext,
) -> Option<Detection> {
    if !baseline.is_ready {
        return None;
    }
    let base = baseline.median?;
    if !stats::rate_changed(base, observed_rate, observed_n, MIN_ABSOLUTE_RATE_CHANGE) {
        return None;
    }

    let delta = (observed_rate - base).abs();
    Some(Detection {
        metric_key: metric_key.to_string(),
        drift_class: profiles::drift_class_for(metric_key),
        baseline_value: Some(base),
        observed_value: Some(observed_rate),
        // A rate moving from 2% to 40% is a bigger deal than 2% to 6%,
        // and proportional change captures that better than the raw delta.
        effect_size: if base > 0.0 {
            delta / base
        } else {
            delta * 10.0
        },
        direction: if observed_rate > base { "up" } else { "down" },
        severity: grade(delta * 20.0, metric_key, impact),
        change_point_index: None,
    })
}

/// Turn an effect size plus business context into a severity.
///
/// Deliberately not a function of statistical confidence — see the module
/// docs. The inputs are magnitude, how much traffic it affects, and what
/// the system is allowed to do.
pub fn grade(effect: f64, metric_key: &str, impact: &ImpactContext) -> &'static str {
    // A shift observed across a handful of runs is a curiosity whatever
    // its magnitude — it is one bad morning, not a trend.
    if impact.observed_runs < 10 {
        return severity::LOW;
    }

    let mut score = effect;

    // Volume: the same shift across thousands of runs matters more — but
    // only modestly. Volume should improve *ranking*, not escalate a
    // barely-detectable move into a real finding; a z of 3.1 is noise
    // whether it happened 50 times or 50,000.
    if impact.observed_runs >= 1000 {
        score *= 1.3;
    } else if impact.observed_runs >= 100 {
        score *= 1.15;
    }

    // Money, in the operator's terms rather than a z-score's.
    if impact.cost_delta_usd >= 1.0 {
        score *= 2.0;
    } else if impact.cost_delta_usd >= 0.1 {
        score *= 1.4;
    }

    // An unattended system that can act on the world is the case where
    // drift turns into consequences before anyone looks.
    if impact.autonomy_level.as_deref() == Some("autonomous") && impact.has_side_effecting_grants {
        score *= 1.6;
    }

    if matches!(
        impact.risk_tier.as_deref(),
        Some("high") | Some("unacceptable")
    ) {
        score *= 1.5;
    }

    // Exhaustion is the looping-agent signal — the one that runs up a
    // bill and produces nothing while looking busy.
    if metric_key == metric::EXHAUSTION_RATE {
        score *= 1.5;
    }

    // Continuous effects start at the detection floor (|z| >= 3.0), so the
    // bands are calibrated from there: a bare-threshold move stays LOW even
    // at high volume, and CRITICAL needs a large move *plus* a real
    // aggravating factor — money, unattended side effects, or a high-risk
    // tier.
    if score >= 18.0 {
        severity::CRITICAL
    } else if score >= 9.0 {
        severity::HIGH
    } else if score >= 4.5 {
        severity::MEDIUM
    } else {
        severity::LOW
    }
}

/// Whether behavioural drift is better explained by the inputs moving.
///
/// This is the disambiguator. If the questions changed at the same time
/// the behaviour did, the system probably did not change — and reporting
/// it as though it had is how every Monday-morning traffic shift becomes
/// a false alarm.
pub fn input_co_moved(detections: &[Detection]) -> bool {
    detections.iter().any(|d| {
        matches!(
            d.metric_key.as_str(),
            metric::FIRST_PROMPT_TOKENS | metric::INPUT_BYTES
        )
    })
}

/// Metrics that moved alongside a finding, for the `unexplained` case.
pub fn correlated_metric_names(detections: &[Detection], exclude: &str) -> Vec<String> {
    let mut names: Vec<String> = detections
        .iter()
        .filter(|d| d.metric_key != exclude)
        .map(|d| d.metric_key.clone())
        .collect();
    names.sort();
    names.truncate(3);
    names
}

/// Map a change-point index back to a timestamp.
pub fn change_point_time(starts: &[DateTime<Utc>], index: Option<usize>) -> Option<DateTime<Utc>> {
    index.and_then(|i| starts.get(i).copied())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ready(median: f64, mad: f64) -> BaselineRef {
        BaselineRef {
            median: Some(median),
            mad: Some(mad),
            distribution: None,
            sample_count: 100,
            is_ready: true,
        }
    }

    fn impact(runs: i64) -> ImpactContext {
        ImpactContext {
            observed_runs: runs,
            ..Default::default()
        }
    }

    fn dist(pairs: &[(&str, f64)]) -> BTreeMap<String, f64> {
        pairs.iter().map(|(k, v)| (k.to_string(), *v)).collect()
    }

    // -- learning mode is absolute ----------------------------------------

    #[test]
    fn a_learning_baseline_never_produces_a_finding() {
        let mut b = ready(5.0, 1.0);
        b.is_ready = false;
        let series = vec![500.0; 200];
        assert!(
            detect_continuous(metric::ACTION_COUNT, &b, &series, &series, &impact(200)).is_none(),
            "however extreme the numbers, a baseline from too few runs must not alert"
        );
    }

    #[test]
    fn a_learning_baseline_blocks_categorical_findings_too() {
        let mut b = ready(0.0, 0.0);
        b.is_ready = false;
        b.distribution = Some(dist(&[("a", 1.0)]));
        assert!(
            detect_categorical(metric::TOOL_MIX, &b, &dist(&[("z", 1.0)]), &impact(100)).is_none()
        );
    }

    // -- continuous --------------------------------------------------------

    #[test]
    fn a_stable_series_is_not_drift() {
        let series: Vec<f64> = (0..200)
            .map(|i| 5.0 + ((i % 3) as f64 - 1.0) * 0.1)
            .collect();
        assert!(
            detect_continuous(
                metric::ACTION_COUNT,
                &ready(5.0, 1.0),
                &series,
                &series,
                &impact(200)
            )
            .is_none()
        );
    }

    #[test]
    fn a_sustained_step_change_is_drift() {
        let mut series = vec![5.0; 100];
        series.extend(std::iter::repeat(25.0).take(100));
        let d = detect_continuous(
            metric::ACTION_COUNT,
            &ready(5.0, 1.0),
            &series,
            &series,
            &impact(200),
        )
        .expect("sustained 5x shift must be detected");
        assert_eq!(d.direction, "up");
        assert!(
            d.change_point_index.is_some(),
            "the cause ladder needs a timestamp"
        );
        assert_eq!(d.drift_class, super::super::drift_class::BEHAVIOURAL);
    }

    #[test]
    fn one_excursion_without_a_change_point_is_not_reported() {
        // A single bad afternoon in an otherwise stable series. The
        // z-score may clear the bar; the change point must not.
        let mut series = vec![5.0; 200];
        series[100] = 400.0;
        assert!(
            detect_continuous(
                metric::ACTION_COUNT,
                &ready(5.0, 0.0),
                &series,
                &series,
                &impact(200)
            )
            .is_none()
        );
    }

    #[test]
    fn a_drop_is_drift_as_much_as_a_rise() {
        let mut series = vec![50.0; 100];
        series.extend(std::iter::repeat(5.0).take(100));
        let d = detect_continuous(
            metric::ACTION_COUNT,
            &ready(50.0, 2.0),
            &series,
            &series,
            &impact(200),
        )
        .expect("a system doing half its work is drift");
        assert_eq!(d.direction, "down");
    }

    #[test]
    fn a_standing_change_is_still_found_once_the_window_has_rolled_past_it() {
        // The system changed days ago and has been steady at the new level
        // ever since, so the observation window on its own is flat: no
        // change point anywhere in it. Before the trend series existed this
        // detected nothing, and a permanently drifted system reported clean
        // — the exact opposite of the guarantee this phase sells.
        let trend: Vec<f64> = std::iter::repeat(5.0)
            .take(100)
            .chain(std::iter::repeat(30.0).take(100))
            .collect();
        let observed = vec![30.0; 20]; // the last 24h, entirely post-change

        let d = detect_continuous(
            metric::ACTION_COUNT,
            &ready(5.0, 1.0),
            &observed,
            &trend,
            &impact(20),
        )
        .expect("a system that changed on Tuesday is still drifted on Thursday");

        assert_eq!(d.direction, "up");
        let cp = d.change_point_index.expect("the cause ladder needs a date");
        assert!(
            (100..140).contains(&cp),
            "change point {cp} must land near the real change at index 100, \
             not somewhere in the flat recent window — the ladder matches \
             candidate causes within ±2h of this timestamp"
        );
    }

    #[test]
    fn the_observation_window_decides_magnitude_not_the_trend() {
        // A change that has already reverted must not keep alerting: the
        // trend still contains the excursion, but "is it different *now*"
        // is answered by the observation window alone.
        let trend: Vec<f64> = std::iter::repeat(5.0)
            .take(60)
            .chain(std::iter::repeat(40.0).take(60))
            .chain(std::iter::repeat(5.0).take(60))
            .collect();
        let observed = vec![5.0; 20]; // back to normal

        assert!(
            detect_continuous(
                metric::ACTION_COUNT,
                &ready(5.0, 1.0),
                &observed,
                &trend,
                &impact(20)
            )
            .is_none(),
            "a system that recovered is not drifting, however dramatic its week was"
        );
    }

    #[test]
    fn cost_drift_is_classified_as_cost() {
        let mut series = vec![0.01; 100];
        series.extend(std::iter::repeat(0.5).take(100));
        let d = detect_continuous(
            metric::COST_PER_RUN,
            &ready(0.01, 0.002),
            &series,
            &series,
            &impact(200),
        )
        .unwrap();
        assert_eq!(d.drift_class, super::super::drift_class::COST);
    }

    // -- categorical -------------------------------------------------------

    #[test]
    fn an_unchanged_mix_is_not_drift() {
        let mut b = ready(0.0, 0.0);
        b.distribution = Some(dist(&[("search", 0.9), ("fetch", 0.1)]));
        assert!(
            detect_categorical(
                metric::TOOL_MIX,
                &b,
                &dist(&[("search", 89.0), ("fetch", 11.0)]),
                &impact(100)
            )
            .is_none()
        );
    }

    #[test]
    fn a_new_tool_appearing_is_drift() {
        let mut b = ready(0.0, 0.0);
        b.distribution = Some(dist(&[("search", 0.9), ("fetch", 0.1)]));
        let d = detect_categorical(
            metric::TOOL_MIX,
            &b,
            &dist(&[("search", 45.0), ("fetch", 10.0), ("transfer_funds", 45.0)]),
            &impact(100),
        )
        .expect("a system using a tool it never used before is exactly the case");
        assert_eq!(d.direction, "shift");
    }

    #[test]
    fn model_mix_shift_is_classified_as_model_drift() {
        let mut b = ready(0.0, 0.0);
        b.distribution = Some(dist(&[("gpt-x", 1.0)]));
        let d = detect_categorical(
            metric::MODEL_MIX,
            &b,
            &dist(&[("gpt-y", 1.0)]),
            &impact(100),
        )
        .unwrap();
        assert_eq!(d.drift_class, super::super::drift_class::MODEL);
    }

    // -- rates -------------------------------------------------------------

    #[test]
    fn a_tiny_rate_move_on_huge_volume_is_not_reported() {
        let d = detect_rate(
            metric::TOOL_ERROR_RATE,
            &ready(0.001, 0.0),
            0.003,
            100_000,
            &impact(100_000),
        );
        assert!(
            d.is_none(),
            "certain and meaningless — the move that mutes a category"
        );
    }

    #[test]
    fn a_real_rate_move_is_reported_as_dependency_drift() {
        let d = detect_rate(
            metric::TOOL_ERROR_RATE,
            &ready(0.02, 0.0),
            0.35,
            800,
            &impact(800),
        )
        .expect("2% to 35% is a dependency falling over");
        assert_eq!(d.drift_class, super::super::drift_class::DEPENDENCY);
        assert_eq!(d.direction, "up");
    }

    // -- severity ----------------------------------------------------------

    #[test]
    fn a_small_certain_move_is_low() {
        assert_eq!(
            grade(3.2, metric::DURATION_MS, &impact(5000)),
            severity::LOW
        );
    }

    #[test]
    fn a_handful_of_runs_is_always_low() {
        // One bad morning, whatever the magnitude.
        assert_eq!(grade(50.0, metric::COST_PER_RUN, &impact(4)), severity::LOW);
    }

    #[test]
    fn money_raises_severity() {
        let cheap = grade(5.0, metric::COST_PER_RUN, &impact(500));
        let expensive = grade(
            5.0,
            metric::COST_PER_RUN,
            &ImpactContext {
                observed_runs: 500,
                cost_delta_usd: 2.5,
                ..Default::default()
            },
        );
        // The invariant is the ordering; the band is the calibration.
        assert_eq!(cheap, severity::MEDIUM);
        assert_eq!(expensive, severity::HIGH);
    }

    #[test]
    fn an_unattended_system_with_side_effects_ranks_higher() {
        let watched = grade(6.0, metric::ACTION_COUNT, &impact(500));
        let unattended = grade(
            6.0,
            metric::ACTION_COUNT,
            &ImpactContext {
                observed_runs: 500,
                autonomy_level: Some("autonomous".into()),
                has_side_effecting_grants: true,
                ..Default::default()
            },
        );
        assert_eq!(watched, severity::MEDIUM);
        assert_eq!(unattended, severity::HIGH);
    }

    #[test]
    fn high_risk_tier_raises_severity() {
        // Chosen to straddle a band boundary so the multiplier is visible
        // rather than absorbed inside one.
        let normal = grade(3.5, metric::ACTION_COUNT, &impact(200));
        let high = grade(
            3.5,
            metric::ACTION_COUNT,
            &ImpactContext {
                observed_runs: 200,
                risk_tier: Some("high".into()),
                ..Default::default()
            },
        );
        assert_eq!(normal, severity::LOW);
        assert_eq!(high, severity::MEDIUM);
    }

    #[test]
    fn exhaustion_is_weighted_up() {
        let other = grade(6.0, metric::ACTION_COUNT, &impact(200));
        let exhaustion = grade(6.0, metric::EXHAUSTION_RATE, &impact(200));
        assert_eq!(other, severity::MEDIUM);
        assert_eq!(exhaustion, severity::HIGH);
        assert_ne!(
            other, exhaustion,
            "the looping-agent signal must rank above the rest"
        );
    }

    #[test]
    fn severity_never_returns_an_unknown_value() {
        for effect in [0.0, 1.0, 5.0, 50.0, 1000.0] {
            for runs in [0, 10, 100, 10_000] {
                let s = grade(effect, metric::ACTION_COUNT, &impact(runs));
                assert!(
                    [
                        severity::LOW,
                        severity::MEDIUM,
                        severity::HIGH,
                        severity::CRITICAL
                    ]
                    .contains(&s)
                );
            }
        }
    }

    // -- the disambiguator -------------------------------------------------

    #[test]
    fn input_movement_is_recognised() {
        let d = vec![Detection {
            metric_key: metric::FIRST_PROMPT_TOKENS.into(),
            drift_class: super::super::drift_class::INPUT,
            baseline_value: None,
            observed_value: None,
            effect_size: 5.0,
            direction: "up",
            severity: severity::MEDIUM,
            change_point_index: None,
        }];
        assert!(input_co_moved(&d));
    }

    #[test]
    fn behaviour_alone_does_not_look_like_input_drift() {
        let d = vec![Detection {
            metric_key: metric::ACTION_COUNT.into(),
            drift_class: super::super::drift_class::BEHAVIOURAL,
            baseline_value: None,
            observed_value: None,
            effect_size: 5.0,
            direction: "up",
            severity: severity::MEDIUM,
            change_point_index: None,
        }];
        assert!(!input_co_moved(&d));
    }

    #[test]
    fn correlated_metrics_exclude_the_finding_and_cap_at_three() {
        let mk = |k: &str| Detection {
            metric_key: k.into(),
            drift_class: super::super::drift_class::BEHAVIOURAL,
            baseline_value: None,
            observed_value: None,
            effect_size: 1.0,
            direction: "up",
            severity: severity::LOW,
            change_point_index: None,
        };
        let all = vec![mk("a"), mk("b"), mk("c"), mk("d"), mk("action_count")];
        let names = correlated_metric_names(&all, "action_count");
        assert_eq!(names.len(), 3);
        assert!(!names.contains(&"action_count".to_string()));
    }
}
