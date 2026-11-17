// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Drift-detector regression corpus.
//!
//! Labelled synthetic run streams with **injected, known** changes, plus —
//! the half that matters — **no-change control streams**.
//!
//! A detector suite that only contains streams where something happened
//! proves the detector fires. It says nothing about whether it fires when
//! nothing happened, and that is the failure mode that kills the feature:
//! alert fatigue is the critical risk, and a
//! trigger-happy detector produces it faster than any amount of tuning can
//! fix.
//!
//! So this module asserts two things of equal weight:
//!
//!   * **recall** — each injected change is detected;
//!   * **a false-positive ceiling** — the control streams stay silent.
//!
//! Every stream is generated from a deterministic pseudo-random sequence
//! so a failure is reproducible. `Math.random`-style nondeterminism in a
//! statistical test suite produces failures nobody can chase.

use std::collections::BTreeMap;

/// Deterministic LCG. Not cryptographic, and does not need to be — it
/// needs to be *reproducible*, so a failing case can be re-run.
pub struct Rng(u64);

impl Rng {
    pub fn new(seed: u64) -> Self {
        Self(seed.wrapping_mul(6364136223846793005).wrapping_add(1))
    }
    fn next_u64(&mut self) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        self.0
    }
    /// Uniform in [0, 1).
    pub fn unit(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 / (1u64 << 53) as f64
    }
    /// Roughly normal, via the sum of twelve uniforms.
    pub fn normal(&mut self) -> f64 {
        (0..12).map(|_| self.unit()).sum::<f64>() - 6.0
    }
}

/// A generated stream and what was done to it.
pub struct Stream {
    pub label: &'static str,
    pub values: Vec<f64>,
    /// Index at which the injected change begins, if any.
    pub change_at: Option<usize>,
}

/// Steady behaviour with ordinary jitter. **No change.**
pub fn control_steady(seed: u64, n: usize, level: f64, noise: f64) -> Stream {
    let mut rng = Rng::new(seed);
    Stream {
        label: "control_steady",
        values: (0..n).map(|_| level + rng.normal() * noise).collect(),
        change_at: None,
    }
}

/// Steady but heavy-tailed — occasional legitimate long runs. **No change.**
///
/// This is the control that a mean-and-sigma detector fails: the outliers
/// widen sigma until nothing is ever detected, or move the mean until
/// everything is.
pub fn control_heavy_tail(seed: u64, n: usize, level: f64) -> Stream {
    let mut rng = Rng::new(seed);
    Stream {
        label: "control_heavy_tail",
        values: (0..n)
            .map(|_| {
                if rng.unit() < 0.02 {
                    level * 20.0 // a rare genuine outlier
                } else {
                    level + rng.normal() * (level * 0.05)
                }
            })
            .collect(),
        change_at: None,
    }
}

/// Steady with a weekly rhythm. **No change** — but a flat baseline would
/// call every Monday a regression.
pub fn control_seasonal(seed: u64, n: usize, level: f64) -> Stream {
    let mut rng = Rng::new(seed);
    Stream {
        label: "control_seasonal",
        values: (0..n)
            .map(|i| {
                let weekly = ((i as f64 / 24.0) * std::f64::consts::TAU / 7.0).sin() * level * 0.1;
                level + weekly + rng.normal() * (level * 0.03)
            })
            .collect(),
        change_at: None,
    }
}

/// One bad afternoon in an otherwise steady stream. **No sustained change.**
pub fn control_single_spike(seed: u64, n: usize, level: f64) -> Stream {
    let mut s = control_steady(seed, n, level, level * 0.03);
    let at = n / 2;
    s.values[at] = level * 30.0;
    Stream {
        label: "control_single_spike",
        values: s.values,
        change_at: None,
    }
}

/// Gradual, tiny upward creep — below anything worth paging about.
/// **No actionable change.**
pub fn control_slow_creep(seed: u64, n: usize, level: f64) -> Stream {
    let mut rng = Rng::new(seed);
    Stream {
        label: "control_slow_creep",
        values: (0..n)
            .map(|i| level * (1.0 + i as f64 / (n as f64 * 50.0)) + rng.normal() * (level * 0.02))
            .collect(),
        change_at: None,
    }
}

/// A sustained step change. **Real drift.**
pub fn injected_step(seed: u64, n: usize, level: f64, multiplier: f64) -> Stream {
    let mut rng = Rng::new(seed);
    let at = n / 2;
    Stream {
        label: "injected_step",
        values: (0..n)
            .map(|i| {
                let base = if i < at { level } else { level * multiplier };
                base + rng.normal() * (base * 0.03)
            })
            .collect(),
        change_at: Some(at),
    }
}

/// A sustained drop — a system quietly doing less work. **Real drift.**
pub fn injected_drop(seed: u64, n: usize, level: f64, fraction: f64) -> Stream {
    injected_step(seed, n, level, fraction)
}

/// Categorical mixes for tool/model/outcome drift.
pub fn mix(pairs: &[(&str, f64)]) -> BTreeMap<String, f64> {
    pairs.iter().map(|(k, v)| (k.to_string(), *v)).collect()
}

/// Every no-change control, for the false-positive ceiling.
pub fn all_controls(seed: u64, n: usize, level: f64) -> Vec<Stream> {
    vec![
        control_steady(seed, n, level, level * 0.03),
        control_heavy_tail(seed + 1, n, level),
        control_seasonal(seed + 2, n, level),
        control_single_spike(seed + 3, n, level),
        control_slow_creep(seed + 4, n, level),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::drift::{
        detect::{self, BaselineRef, ImpactContext},
        metric, stats,
    };

    fn baseline_from(values: &[f64]) -> BaselineRef {
        let sorted = stats::sorted_finite(values);
        let med = stats::median(&sorted);
        BaselineRef {
            median: med,
            mad: med.and_then(|m| stats::mad(&sorted, m)),
            distribution: None,
            sample_count: sorted.len() as i64,
            is_ready: true,
        }
    }

    fn impact(n: usize) -> ImpactContext {
        ImpactContext {
            observed_runs: n as i64,
            ..Default::default()
        }
    }

    /// Build the baseline from the *pre-change* half and observe the whole
    /// stream, which is what the sweeper does.
    fn detect_on(stream: &Stream) -> Option<detect::Detection> {
        let split = stream.change_at.unwrap_or(stream.values.len() / 2);
        let base = baseline_from(&stream.values[..split]);
        // Observation and trend are the same stream here: the corpus asks
        // "given this whole history, is there a sustained change and where",
        // which is precisely the job the trend series does in production.
        detect::detect_continuous(
            metric::ACTION_COUNT,
            &base,
            &stream.values,
            &stream.values,
            &impact(stream.values.len()),
        )
    }

    // ---------------------------------------------------------------
    // The false-positive ceiling — the half that keeps the feature alive
    // ---------------------------------------------------------------

    #[test]
    fn no_change_controls_stay_silent() {
        // Run every control across many seeds. A single spurious detection
        // here means a customer gets paged about a system that did nothing,
        // which is how the whole category gets muted.
        let mut false_positives = 0;
        let mut total = 0;

        for seed in 0..40u64 {
            for stream in all_controls(seed, 400, 8.0) {
                total += 1;
                if detect_on(&stream).is_some() {
                    false_positives += 1;
                    eprintln!("false positive: {} (seed {seed})", stream.label);
                }
            }
        }

        assert_eq!(
            false_positives, 0,
            "{false_positives} of {total} no-change streams produced a finding — \
             if v1 pages people about nothing, there is no v2"
        );
    }

    #[test]
    fn heavy_tailed_but_stable_traffic_is_not_drift() {
        // The control a mean-and-sigma detector fails.
        for seed in 0..25u64 {
            let s = control_heavy_tail(seed, 400, 6.0);
            assert!(
                detect_on(&s).is_none(),
                "occasional legitimate long runs are not a behavioural change (seed {seed})"
            );
        }
    }

    #[test]
    fn one_bad_afternoon_is_not_a_sustained_change() {
        for seed in 0..25u64 {
            let s = control_single_spike(seed, 400, 10.0);
            assert!(
                detect_on(&s).is_none(),
                "single spike flagged (seed {seed})"
            );
        }
    }

    #[test]
    fn a_weekly_rhythm_is_not_a_regression() {
        for seed in 0..25u64 {
            let s = control_seasonal(seed, 500, 12.0);
            assert!(detect_on(&s).is_none(), "seasonality flagged (seed {seed})");
        }
    }

    // ---------------------------------------------------------------
    // Recall
    // ---------------------------------------------------------------

    #[test]
    fn sustained_step_changes_are_detected() {
        let mut missed = 0;
        for seed in 0..25u64 {
            let s = injected_step(seed, 400, 5.0, 4.0);
            if detect_on(&s).is_none() {
                missed += 1;
            }
        }
        assert_eq!(
            missed, 0,
            "{missed}/25 injected 4x step changes were missed"
        );
    }

    #[test]
    fn sustained_drops_are_detected_too() {
        // A system that got cheaper because it stopped doing half its work
        // is drift, and a one-sided detector would miss every one of these.
        let mut missed = 0;
        for seed in 0..25u64 {
            let s = injected_drop(seed, 400, 40.0, 0.25);
            if detect_on(&s).is_none() {
                missed += 1;
            }
        }
        assert_eq!(missed, 0, "{missed}/25 injected drops were missed");
    }

    #[test]
    fn detection_is_scale_free() {
        // The bug this pins: Page–Hinkley constants in raw metric units
        // silently never fire for small-magnitude metrics. A cost measured
        // in hundredths of a cent must behave exactly like a step count.
        for (label, level) in [
            ("cost", 0.004_f64),
            ("actions", 7.0),
            ("duration_ms", 2400.0),
        ] {
            let mut missed = 0;
            for seed in 0..15u64 {
                let s = injected_step(seed, 400, level, 5.0);
                if detect_on(&s).is_none() {
                    missed += 1;
                }
            }
            assert_eq!(missed, 0, "{missed}/15 missed at {label} scale ({level})");
        }
    }

    #[test]
    fn a_change_point_is_located_near_the_injection() {
        for seed in 0..15u64 {
            let s = injected_step(seed, 400, 6.0, 5.0);
            let d = detect_on(&s).expect("must detect");
            let at = s.change_at.unwrap();
            let cp = d.change_point_index.expect("must locate");
            assert!(cp >= at, "change point before the change (seed {seed})");
            assert!(
                cp < at + 40,
                "change point {cp} far after the injection at {at} (seed {seed}) — \
                 the cause ladder matches on this timestamp"
            );
        }
    }

    // ---------------------------------------------------------------
    // Categorical
    // ---------------------------------------------------------------

    #[test]
    fn an_unchanged_tool_mix_is_not_drift() {
        let mut base = BaselineRef {
            is_ready: true,
            ..Default::default()
        };
        base.distribution = Some(stats::normalize(&mix(&[("search", 80.0), ("fetch", 20.0)])));
        // Same shape, different sample size and slight jitter.
        let observed = mix(&[("search", 158.0), ("fetch", 42.0)]);
        assert!(
            detect::detect_categorical(metric::TOOL_MIX, &base, &observed, &impact(200)).is_none()
        );
    }

    #[test]
    fn a_new_high_risk_tool_is_drift() {
        let mut base = BaselineRef {
            is_ready: true,
            ..Default::default()
        };
        base.distribution = Some(stats::normalize(&mix(&[("search", 80.0), ("fetch", 20.0)])));
        let observed = mix(&[("search", 40.0), ("fetch", 20.0), ("transfer_funds", 40.0)]);
        assert!(
            detect::detect_categorical(metric::TOOL_MIX, &base, &observed, &impact(100)).is_some()
        );
    }

    // ---------------------------------------------------------------
    // Determinism
    // ---------------------------------------------------------------

    #[test]
    fn streams_are_reproducible() {
        // A statistical suite whose failures cannot be reproduced is a
        // suite nobody will debug.
        let a = injected_step(7, 100, 5.0, 3.0);
        let b = injected_step(7, 100, 5.0, 3.0);
        assert_eq!(a.values, b.values);
        let c = injected_step(8, 100, 5.0, 3.0);
        assert_ne!(a.values, c.values);
    }
}
