// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Durations read from configuration.
//!
//! The control plane accepts any positive number of seconds for a timeout
//! (an MCP call's `execution.timeout_seconds`, a routing group's
//! `stream_timeout`, a model's `timeout`). `Duration::from_secs_f64` panics
//! on a number of seconds above `u64::MAX`, and adding a duration to an
//! `Instant` panics long before that (an `Instant` holds a few hundred
//! years). A deadline is taken from every one of these timeouts, so the
//! gateway reads them through here: a stated duration above
//! [`MAX_CONFIGURED_DURATION`] is that ceiling. A timeout never loosens by
//! this (the ceiling is lower than what was stated), and no request, stream
//! or poll the gateway runs comes near it.

use std::time::Duration;

/// The longest duration the gateway takes from configuration: 365 days.
/// Longer than any call, stream or poll the gateway waits on, so a timeout
/// at the ceiling behaves as the one stated; and far below where adding it
/// (and a few seconds of margin) to the current `Instant` could overflow,
/// on every platform the gateway runs on.
pub const MAX_CONFIGURED_DURATION: Duration = Duration::from_secs(365 * 24 * 60 * 60);

/// A stated number of seconds, at most [`MAX_CONFIGURED_DURATION`];
/// `None` when it is not a positive finite number (each caller applies its
/// own rule to that: unset, or refused).
pub fn positive_secs(secs: f64) -> Option<Duration> {
    if !(secs.is_finite() && secs > 0.0) {
        return None;
    }
    if secs >= MAX_CONFIGURED_DURATION.as_secs_f64() {
        return Some(MAX_CONFIGURED_DURATION);
    }
    Some(Duration::from_secs_f64(secs))
}

/// Whole seconds taken from configuration, at most
/// [`MAX_CONFIGURED_DURATION`].
pub fn whole_secs(secs: u64) -> Duration {
    Duration::from_secs(secs).min(MAX_CONFIGURED_DURATION)
}

#[cfg(test)]
mod tests {
    use super::*;

    const HUGE: [f64; 5] = [1e300, f64::MAX, f64::INFINITY, 1.9e19, 1e15];

    #[test]
    fn a_stated_duration_is_at_most_the_ceiling_and_a_deadline_can_be_taken_from_it() {
        for v in HUGE.into_iter().filter(|v| v.is_finite()) {
            let d = positive_secs(v).expect("a positive number is a duration");
            assert_eq!(d, MAX_CONFIGURED_DURATION, "{v}");
            let _ = std::time::Instant::now() + d;
            let _ = tokio::time::Instant::now() + d + Duration::from_secs(5);
        }
        for v in [u64::MAX, 1 << 60, 10_000_000_000_000_000] {
            let d = whole_secs(v);
            assert_eq!(d, MAX_CONFIGURED_DURATION, "{v}");
            let _ = std::time::Instant::now() + d;
        }
    }

    #[test]
    fn a_duration_below_the_ceiling_is_read_as_stated() {
        assert_eq!(positive_secs(1.5), Some(Duration::from_millis(1500)));
        assert_eq!(whole_secs(30), Duration::from_secs(30));
        assert_eq!(
            positive_secs(MAX_CONFIGURED_DURATION.as_secs_f64()),
            Some(MAX_CONFIGURED_DURATION)
        );
    }

    #[test]
    fn what_is_not_a_positive_finite_number_is_no_duration() {
        for v in [0.0, -0.0, -1.0, f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            assert_eq!(positive_secs(v), None, "{v}");
        }
    }
}
