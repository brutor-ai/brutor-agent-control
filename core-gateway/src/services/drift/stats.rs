// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Robust statistics for behavioural baselines.
//!
//! Every measure here is deliberately unfashionable: median and MAD rather
//! than mean and standard deviation, Jensen–Shannon rather than KL,
//! nearest-rank percentiles rather than interpolated ones.
//!
//! Run metrics are heavy-tailed. One agent that loops 400 times moves a
//! mean far enough to hide a real shift in the other 500 runs, and a
//! standard deviation computed over that tail is wide enough to swallow
//! anything. A median barely notices the outlier — which is exactly what
//! a baseline needs, because the outlier is the thing we are trying to
//! detect, not the thing that should define normal.
//!
//! Nothing in this module touches a database, so every decision it makes
//! can be argued with against a table of numbers.

use std::collections::BTreeMap;

/// Scale factor making MAD comparable with a standard deviation for
/// normally-distributed data. Standard constant, not a tuning knob.
pub const MAD_TO_SIGMA: f64 = 1.4826;

/// Nearest-rank percentile — returns a value that is actually in the data.
///
/// Not interpolated on purpose: an operator checking p95 against the run
/// list should find the number there, not one computed between two rows
/// that never happened.
pub fn percentile(sorted: &[f64], pct: f64) -> Option<f64> {
    if sorted.is_empty() {
        return None;
    }
    if sorted.len() == 1 {
        return Some(sorted[0]);
    }
    let rank = ((pct * sorted.len() as f64) + 0.5).round() as usize;
    Some(sorted[rank.clamp(1, sorted.len()) - 1])
}

pub fn median(sorted: &[f64]) -> Option<f64> {
    if sorted.is_empty() {
        return None;
    }
    let mid = sorted.len() / 2;
    if sorted.len() % 2 == 1 {
        Some(sorted[mid])
    } else {
        Some((sorted[mid - 1] + sorted[mid]) / 2.0)
    }
}

/// Median absolute deviation.
pub fn mad(sorted: &[f64], med: f64) -> Option<f64> {
    if sorted.is_empty() {
        return None;
    }
    let mut deviations: Vec<f64> = sorted.iter().map(|v| (v - med).abs()).collect();
    deviations.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    median(&deviations)
}

/// How far an observation sits from the baseline, in robust units.
///
/// A MAD of zero means every run in the window had the identical value —
/// common for a system that always takes exactly 3 steps. Dividing by it
/// would give infinity for any deviation at all, so a zero-MAD baseline
/// reports a change only when the value genuinely moves, and reports a
/// fixed large score rather than infinity so severity ranking still works.
pub fn robust_z(observed: f64, median_value: f64, mad_value: f64) -> f64 {
    let scaled = mad_value * MAD_TO_SIGMA;
    if scaled <= f64::EPSILON {
        return if (observed - median_value).abs() <= f64::EPSILON {
            0.0
        } else {
            // Deliberately finite: an infinite z-score cannot be ranked
            // against other findings, and severity ordering is what an
            // operator actually uses.
            10.0
        };
    }
    (observed - median_value) / scaled
}

/// Normalize counts into a frequency vector.
pub fn normalize(counts: &BTreeMap<String, f64>) -> BTreeMap<String, f64> {
    let total: f64 = counts.values().sum();
    if total <= 0.0 {
        return BTreeMap::new();
    }
    counts.iter().map(|(k, v)| (k.clone(), v / total)).collect()
}

fn kl_divergence(p: &BTreeMap<String, f64>, m: &BTreeMap<String, f64>) -> f64 {
    p.iter()
        .filter(|(_, pv)| **pv > 0.0)
        .map(|(k, pv)| {
            let pv = *pv;
            let mv = m.get(k).copied().unwrap_or(0.0);
            if mv <= 0.0 {
                0.0
            } else {
                pv * (pv / mv).log2()
            }
        })
        .sum()
}

/// Jensen–Shannon divergence between two frequency vectors.
///
/// Bounded 0–1 (using log base 2) and symmetric, unlike KL. Both
/// properties matter operationally: an unbounded score cannot be
/// thresholded meaningfully, and an asymmetric one gives a different
/// answer depending on which distribution you happened to call the
/// baseline.
///
/// Handles categories present in one side and not the other, which is the
/// common case — a system that starts calling a tool it never used before
/// is exactly the shift worth catching.
pub fn js_divergence(p: &BTreeMap<String, f64>, q: &BTreeMap<String, f64>) -> f64 {
    if p.is_empty() || q.is_empty() {
        return 0.0;
    }
    let p = normalize(p);
    let q = normalize(q);

    let mut m: BTreeMap<String, f64> = BTreeMap::new();
    for k in p.keys().chain(q.keys()) {
        let pv = p.get(k).copied().unwrap_or(0.0);
        let qv = q.get(k).copied().unwrap_or(0.0);
        m.insert(k.clone(), (pv + qv) / 2.0);
    }

    let div = 0.5 * kl_divergence(&p, &m) + 0.5 * kl_divergence(&q, &m);
    div.clamp(0.0, 1.0)
}

/// Page–Hinkley change-point detector.
///
/// Answers a different question from "is the current value unusual": *when
/// did it start being unusual?* That timestamp is what the cause ladder
/// matches candidates against, and matching on detection time instead
/// would blame whatever happened during the sweep rather than whatever
/// actually caused the change.
///
/// `delta` is the slack that keeps ordinary noise from accumulating into a
/// change point; `threshold` is how much sustained deviation is required.
#[derive(Debug, Clone)]
pub struct PageHinkley {
    delta: f64,
    threshold: f64,
    count: usize,
    mean: f64,
    // Two independent cumulative sums, one per direction. A single sum
    // cannot do both: the `- delta` slack that stops noise accumulating
    // into a *rise* biases the series downward, which then looks exactly
    // like a sustained *fall*. That is not a subtle failure — it fires on
    // a perfectly flat series within ~16 observations.
    cum_up: f64,
    min_up: f64,
    cum_down: f64,
    max_down: f64,
}

impl PageHinkley {
    pub fn new(delta: f64, threshold: f64) -> Self {
        Self {
            delta,
            threshold,
            count: 0,
            mean: 0.0,
            cum_up: 0.0,
            min_up: 0.0,
            cum_down: 0.0,
            max_down: 0.0,
        }
    }

    /// Feed one observation. Returns true when a change point is declared.
    pub fn push(&mut self, value: f64) -> bool {
        self.count += 1;
        self.mean += (value - self.mean) / self.count as f64;

        // Rise detector: slack subtracted, so a flat series drifts the sum
        // down and its running minimum follows — leaving rise ~= 0.
        self.cum_up += value - self.mean - self.delta;
        if self.cum_up < self.min_up {
            self.min_up = self.cum_up;
        }

        // Fall detector: slack *added*, mirroring the above. Missing this
        // sign flip is what makes a single-sum implementation fire on flat
        // input.
        self.cum_down += value - self.mean + self.delta;
        if self.cum_down > self.max_down {
            self.max_down = self.cum_down;
        }

        // Two-sided so a system that got *cheaper* because it quietly
        // stopped doing half its work is caught too.
        let rise = self.cum_up - self.min_up;
        let fall = self.max_down - self.cum_down;
        rise > self.threshold || fall > self.threshold
    }

    /// Index of the first observation after which a change was declared,
    /// or `None` if the series never changed.
    pub fn first_change_index(series: &[f64], delta: f64, threshold: f64) -> Option<usize> {
        let mut ph = PageHinkley::new(delta, threshold);
        for (i, v) in series.iter().enumerate() {
            if ph.push(*v) {
                return Some(i);
            }
        }
        None
    }
}

/// Two-proportion comparison for rates, with an absolute floor.
///
/// The floor is the important half. Without it, 0.1% → 0.3% on a hundred
/// thousand runs is overwhelmingly "significant" and completely
/// uninteresting — and paging someone about it is how the drift feed gets
/// muted.
pub fn rate_changed(
    baseline_rate: f64,
    observed_rate: f64,
    observed_n: i64,
    min_absolute_change: f64,
) -> bool {
    if observed_n <= 0 {
        return false;
    }
    let absolute = (observed_rate - baseline_rate).abs();
    if absolute < min_absolute_change {
        return false;
    }
    // Standard error of the observed proportion; require the move to be
    // larger than the noise the sample size can produce.
    let p = observed_rate.clamp(0.0, 1.0);
    let se = (p * (1.0 - p) / observed_n as f64).sqrt();
    absolute > 2.0 * se.max(f64::EPSILON)
}

/// Sort a copy, dropping non-finite values.
pub fn sorted_finite(values: &[f64]) -> Vec<f64> {
    let mut v: Vec<f64> = values.iter().copied().filter(|x| x.is_finite()).collect();
    v.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    v
}

#[cfg(test)]
mod tests {
    use super::*;

    fn map(pairs: &[(&str, f64)]) -> BTreeMap<String, f64> {
        pairs.iter().map(|(k, v)| (k.to_string(), *v)).collect()
    }

    // -- robustness, the whole reason for these choices ---------------------

    #[test]
    fn median_ignores_the_runaway_that_a_mean_would_chase() {
        let mut v = vec![3.0; 100];
        v.push(400.0); // one agent that looped
        let sorted = sorted_finite(&v);
        assert_eq!(median(&sorted), Some(3.0));

        let mean: f64 = v.iter().sum::<f64>() / v.len() as f64;
        assert!(
            mean > 6.0,
            "the mean moves by >2x on a single outlier — which is why the \
             baseline does not use it"
        );
    }

    #[test]
    fn mad_stays_tight_where_a_standard_deviation_would_blow_out() {
        let mut v = vec![10.0; 50];
        v.push(5000.0);
        let sorted = sorted_finite(&v);
        let med = median(&sorted).unwrap();
        assert_eq!(
            mad(&sorted, med),
            Some(0.0),
            "50 identical values, one outlier"
        );
    }

    #[test]
    fn percentile_returns_a_value_that_actually_occurred() {
        let sorted = sorted_finite(&[1.0, 2.0, 3.0, 4.0, 100.0]);
        let p95 = percentile(&sorted, 0.95).unwrap();
        assert!(
            sorted.contains(&p95),
            "an interpolated percentile is a number no run produced"
        );
        assert_eq!(p95, 100.0);
    }

    #[test]
    fn percentile_and_median_handle_degenerate_input() {
        assert_eq!(percentile(&[], 0.5), None);
        assert_eq!(median(&[]), None);
        assert_eq!(percentile(&[7.0], 0.99), Some(7.0));
    }

    // -- robust z ----------------------------------------------------------

    #[test]
    fn robust_z_scales_by_mad() {
        // median 10, MAD 2 → scaled 2.9652
        let z = robust_z(16.0, 10.0, 2.0);
        assert!((z - 6.0 / (2.0 * MAD_TO_SIGMA)).abs() < 1e-9);
    }

    #[test]
    fn zero_mad_with_no_deviation_is_not_drift() {
        // A system that always takes exactly 3 steps and still does.
        assert_eq!(robust_z(3.0, 3.0, 0.0), 0.0);
    }

    #[test]
    fn zero_mad_with_deviation_is_finite_not_infinite() {
        // Always 3 steps, now 9. Real drift — but an infinite score cannot
        // be ranked against other findings, and ranking is what an
        // operator uses.
        let z = robust_z(9.0, 3.0, 0.0);
        assert!(z.is_finite());
        assert!(z >= 10.0);
    }

    // -- Jensen-Shannon ----------------------------------------------------

    #[test]
    fn identical_distributions_have_zero_divergence() {
        let p = map(&[("search", 5.0), ("fetch", 5.0)]);
        assert!(js_divergence(&p, &p) < 1e-12);
    }

    #[test]
    fn js_is_bounded_and_symmetric() {
        let p = map(&[("a", 1.0)]);
        let q = map(&[("b", 1.0)]);
        let pq = js_divergence(&p, &q);
        let qp = js_divergence(&q, &p);
        assert!(
            (pq - qp).abs() < 1e-12,
            "asymmetry would make the baseline's side matter"
        );
        assert!(
            pq <= 1.0 && pq > 0.9,
            "disjoint supports should be near the bound"
        );
    }

    #[test]
    fn js_detects_a_newly_used_tool() {
        // The shift most worth catching: a system starts calling something
        // it never called before.
        let before = map(&[("search", 90.0), ("fetch", 10.0)]);
        let after = map(&[("search", 45.0), ("fetch", 10.0), ("transfer_funds", 45.0)]);
        assert!(js_divergence(&before, &after) > 0.15);
    }

    #[test]
    fn js_ignores_scale() {
        let small = map(&[("a", 1.0), ("b", 1.0)]);
        let large = map(&[("a", 1000.0), ("b", 1000.0)]);
        assert!(js_divergence(&small, &large) < 1e-12);
    }

    #[test]
    fn js_on_empty_input_is_zero_not_a_panic() {
        assert_eq!(js_divergence(&BTreeMap::new(), &map(&[("a", 1.0)])), 0.0);
    }

    // -- Page-Hinkley ------------------------------------------------------

    #[test]
    fn stable_series_has_no_change_point() {
        let series: Vec<f64> = (0..200)
            .map(|i| 10.0 + ((i % 3) as f64 - 1.0) * 0.1)
            .collect();
        assert_eq!(
            PageHinkley::first_change_index(&series, 0.5, 8.0),
            None,
            "ordinary jitter must not accumulate into a change point"
        );
    }

    #[test]
    fn step_change_is_located_after_it_happens() {
        let mut series: Vec<f64> = vec![10.0; 100];
        series.extend(std::iter::repeat(30.0).take(100));
        let idx =
            PageHinkley::first_change_index(&series, 0.5, 8.0).expect("a 3x step must be detected");
        assert!(idx >= 100, "change point must not precede the change");
        assert!(idx < 130, "and must be found promptly, not 50 runs later");
    }

    #[test]
    fn a_drop_is_detected_as_well_as_a_rise() {
        // A system that got cheaper because it stopped doing half its work
        // is drift too, and a one-sided detector would miss it entirely.
        let mut series: Vec<f64> = vec![50.0; 100];
        series.extend(std::iter::repeat(5.0).take(100));
        assert!(PageHinkley::first_change_index(&series, 0.5, 8.0).is_some());
    }

    // -- rates -------------------------------------------------------------

    #[test]
    fn tiny_rate_move_on_huge_volume_is_not_reported() {
        assert!(
            !rate_changed(0.001, 0.003, 100_000, 0.02),
            "statistically certain and operationally meaningless — this is \
             the move that mutes an alert category"
        );
    }

    #[test]
    fn a_real_rate_move_is_reported() {
        assert!(rate_changed(0.02, 0.30, 500, 0.02));
    }

    #[test]
    fn a_move_within_sampling_noise_is_not_reported() {
        // 3 of 10 vs a 20% baseline: the absolute change clears the floor
        // but the sample is far too small to distinguish from chance.
        assert!(!rate_changed(0.20, 0.30, 10, 0.02));
    }

    #[test]
    fn zero_sample_never_reports() {
        assert!(!rate_changed(0.0, 1.0, 0, 0.0));
    }
}
