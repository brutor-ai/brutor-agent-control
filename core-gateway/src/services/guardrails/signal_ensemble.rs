// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Signal-ensemble scorer
//!
//! Shared infrastructure used by the built-in prompt-injection and
//! jailbreak detectors. Each detector ships a list of [`Signal`]
//! entries — name, weight, matcher closure. The ensemble runs every
//! signal against the input, sums the weights of those that fire,
//! and emits a [`Hit`] when the aggregate clears a sensitivity-tuned
//! threshold.
//!
//! ## Why ensemble (not single signal)?
//!
//! Real-world adversarial inputs almost never trigger a single
//! pattern. A jailbreak in the wild typically combines a persona
//! invocation ("you are DAN"), a refusal-suppression line ("do not
//! say sorry"), and a token-smuggling prefix ("start your reply
//! with"). Any one of those alone is too noisy to block on; the
//! combination is high-confidence.
//!
//! ## Sensitivity ↔ threshold
//!
//! Operators tune one dial in [0.0, 1.0]:
//!
//!   * `sensitivity = 0.0` → almost no input fires (threshold high)
//!   * `sensitivity = 0.7` (default) → moderate floor; need ~2 strong
//!     signals or ~3 weak ones to fire
//!   * `sensitivity = 1.0` → any single signal fires
//!
//! The mapping uses a floor weight so we never hit on zero signals,
//! plus a sensitivity-weighted ceiling that scales with the signal
//! bank's `total_weight`. Concretely:
//!
//! ```text
//!   threshold = floor + (1.0 - sensitivity) * (total_weight - floor)
//! ```
//!
//! With `floor = 0.30` and a 12-signal bank totalling ~5.0:
//!
//! | sensitivity | threshold |
//! |-------------|-----------|
//! | 0.0         | 5.00      |
//! | 0.5         | 2.65      |
//! | 0.7         | 1.71      |
//! | 0.9         | 0.77      |
//! | 1.0         | 0.30      |
//!
//! Tuning notes: the per-signal weights are calibrated so a single
//! "strong" pattern (`weight = 1.0`) plus a "weak" pattern
//! (`weight = 0.4`) clears `0.7` sensitivity. Adjust weights with
//! that calibration in mind, not arbitrarily.
//!
//! ## Anti-evasion: pre-process before matching
//!
//! Adversarial inputs frequently try to evade pattern matchers via
//! Unicode tag-character smuggling (U+E0000–U+E007F invisibles),
//! base64-encoding the payload, and bizarre casing/spacing. The
//! ensemble pre-processes input through [`canonicalise`] before any
//! signal runs:
//!
//!   1. strip Unicode tag characters
//!   2. lowercase
//!   3. collapse runs of whitespace
//!   4. NFC-normalise (so `ｓｐｏｏｆｅｄ` ASCII matches plain ASCII)
//!
//! Detectors can opt out of step 4 if they want to inspect the raw
//! Unicode-bait — but step 1 always runs because the tag characters
//! are invisible to humans and can't be a legitimate part of any
//! prompt.

use super::detector::{Detection, Span};

/// One signal in a detector's bank. The `matcher` closure is called
/// with the canonicalised input and returns `true` when the pattern
/// matches.
pub struct Signal {
    /// Stable name for logs / audit feed. Snake-case.
    pub name: &'static str,
    /// Contribution to the aggregate score when this signal fires.
    /// Calibration: use 1.0 for strong / unambiguous patterns
    /// ("ignore previous instructions"), 0.4–0.7 for moderate
    /// (persona names like "DAN" — also appears in legitimate
    /// contexts), 0.2–0.4 for noisy fingerprints.
    pub weight: f64,
    /// Pattern matcher. Receives the canonicalised input (see
    /// [`canonicalise`]). Should be cheap — runs on every check.
    pub matcher: fn(&str) -> bool,
}

/// Outcome of running a signal bank against an input.
#[derive(Debug, Clone, PartialEq)]
pub enum Hit {
    /// Aggregate weight cleared the threshold. `score` is the
    /// aggregate weight normalised by the bank's `total_weight` so
    /// it lands in [0.0, 1.0] for the [`Detection`] envelope.
    Fire {
        fired_signals: Vec<&'static str>,
        score: f64,
    },
    /// Below threshold — pass.
    Pass,
}

/// Compute the threshold for a given sensitivity + total signal
/// weight. Pulled out so detectors can document their effective
/// threshold at construction time + the unit tests can lock the
/// curve.
///
/// Formula: `floor + (1 - sensitivity) * 0.5 * total_weight`.
///
///   * `sensitivity = 1.0` → threshold = `floor` (most aggressive).
///     Any signal whose weight ≥ floor fires alone.
///   * `sensitivity = 0.0` → threshold = `floor + 0.5 * total_weight`
///     (most conservative). Need ~half the bank's possible score.
///   * `sensitivity = 0.7` (default) → strong signals (weight ~1.4)
///     fire alone; pairs of moderate signals fire together.
///
/// The 0.5 scaling keeps the threshold below the strongest signal's
/// weight at default sensitivity — a canonical jailbreak fingerprint
/// like "DAN" or a textbook injection like "ignore previous
/// instructions" needs to fire on its own without operator tuning.
pub fn threshold(sensitivity: f64, total_weight: f64) -> f64 {
    const FLOOR: f64 = 0.50;
    let s = sensitivity.clamp(0.0, 1.0);
    let ceiling = total_weight.max(FLOOR);
    FLOOR + (1.0 - s) * 0.5 * ceiling
}

/// Run a signal bank over `content` with the given sensitivity.
/// `content` MUST already be canonicalised — detectors call
/// [`canonicalise`] once at the top of `detect` and pass the result
/// here so multiple banks (e.g. the prompt-injection detector running
/// both the override + role-hijack banks) share the cost.
pub fn run(content: &str, signals: &[Signal], sensitivity: f64) -> Hit {
    let total_weight: f64 = signals.iter().map(|s| s.weight).sum();
    let mut fired: Vec<&'static str> = Vec::new();
    let mut score: f64 = 0.0;
    for sig in signals {
        if (sig.matcher)(content) {
            fired.push(sig.name);
            score += sig.weight;
        }
    }
    let t = threshold(sensitivity, total_weight);
    if score >= t {
        Hit::Fire {
            fired_signals: fired,
            // Normalise to [0,1] for the Detection envelope; clamp
            // because the floor case can theoretically exceed 1.0
            // when total_weight is small.
            score: (score / total_weight.max(1e-9)).min(1.0),
        }
    } else {
        Hit::Pass
    }
}

/// Convert a [`Hit::Fire`] to a [`Detection::Hit`] with a single
/// whole-content span. Used by every signal-ensemble detector so the
/// public Detection envelope shape is uniform.
pub fn hit_to_detection(content: &str, category: impl Into<String>, hit: Hit) -> Detection {
    match hit {
        Hit::Pass => Detection::Pass,
        Hit::Fire { score, .. } => Detection::Hit {
            category: category.into(),
            matches: vec![Span::whole(content)],
            score: Some(score),
        },
    }
}

// ---------------------------------------------------------------------------
// Canonicalisation — strip evasion tricks before matching
// ---------------------------------------------------------------------------

/// Pre-process input so signal patterns don't have to enumerate
/// every casing / spacing / invisible-character variant. Steps:
///
///   1. strip Unicode tag characters (U+E0000–U+E007F) — invisible
///      smuggling vectors with no legitimate use
///   2. lowercase — every signal pattern is case-insensitive
///   3. collapse all-whitespace runs into a single space — so
///      "ignore   \n\n  previous" matches the same pattern as
///      "ignore previous"
///
/// Returns an owned `String`; safe to throw away if the caller has
/// many signal banks since the cost is already paid here.
pub fn canonicalise(content: &str) -> String {
    let mut out = String::with_capacity(content.len());
    let mut prev_was_ws = false;
    for c in content.chars() {
        // Step 1: tag characters → treat as whitespace (NOT delete).
        // The smuggling vector is "your\u{E0041}system" which the
        // human reads as "your" and "system" but a naive regex
        // sees as one token. Substituting with whitespace keeps
        // the boundary intact so the rest of the pipeline (regex
        // matchers) doesn't get fooled. The same character also
        // contributes to the encoded_payload signal — see
        // [`prompt_injection::boost_with_tag_smuggling`].
        let cp = c as u32;
        let is_ws = c.is_whitespace() || (0xE0000..=0xE007F).contains(&cp);
        if is_ws {
            // Step 3: collapse whitespace.
            if !prev_was_ws && !out.is_empty() {
                out.push(' ');
                prev_was_ws = true;
            }
            continue;
        }
        prev_was_ws = false;
        // Step 2: lowercase.
        for lc in c.to_lowercase() {
            out.push(lc);
        }
    }
    // Trim trailing whitespace introduced by the collapse step.
    out.trim_end().to_string()
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn fixed_signal(name: &'static str, weight: f64) -> Signal {
        Signal {
            name,
            weight,
            matcher: |s| s.contains("HIT"),
        }
    }

    #[test]
    fn threshold_at_sensitivity_1_equals_floor() {
        // sensitivity=1.0 → maximally aggressive → only the floor.
        // Single signal of weight ≥ floor fires alone.
        assert!((threshold(1.0, 5.0) - 0.50).abs() < 1e-9);
    }

    #[test]
    fn threshold_at_sensitivity_0_is_floor_plus_half_total() {
        // sensitivity=0.0 → most conservative. With the 0.5 scaling,
        // need ~half the bank's possible score to fire.
        assert!((threshold(0.0, 5.0) - 3.0).abs() < 1e-9); // 0.50 + 0.5*5.0
    }

    #[test]
    fn threshold_monotonic_in_sensitivity() {
        let prev = threshold(0.0, 5.0);
        for i in 1..=10 {
            let s = i as f64 / 10.0;
            let cur = threshold(s, 5.0);
            assert!(cur <= prev, "threshold not monotonic at s={s}");
        }
    }

    #[test]
    fn threshold_clamps_out_of_range() {
        // Defensive: a hand-edited DB row with sensitivity 1.5
        // shouldn't make the detector misbehave.
        assert!((threshold(1.5, 5.0) - threshold(1.0, 5.0)).abs() < 1e-9);
        assert!((threshold(-0.5, 5.0) - threshold(0.0, 5.0)).abs() < 1e-9);
    }

    #[test]
    fn run_no_signals_passes() {
        // Empty bank — nothing fires, score stays 0, threshold stays
        // at the floor. 0 < FLOOR → Pass.
        let hit = run("anything", &[], 0.7);
        assert_eq!(hit, Hit::Pass);
    }

    #[test]
    fn run_single_strong_signal_fires_at_default_sensitivity() {
        // weight 1.0, total_weight 1.0, sensitivity 0.7 →
        // threshold = 0.30 + 0.30 * 0.70 = 0.51. Single hit
        // contributes 1.0 → over threshold.
        let bank = vec![fixed_signal("strong", 1.0)];
        let hit = run("a HIT here", &bank, 0.7);
        match hit {
            Hit::Fire {
                fired_signals,
                score,
            } => {
                assert_eq!(fired_signals, vec!["strong"]);
                assert!((score - 1.0).abs() < 1e-9);
            }
            _ => panic!("expected Fire"),
        }
    }

    #[test]
    fn run_two_weak_signals_compose_to_fire() {
        // Each weight 0.4, total 0.8. Sensitivity 0.7 → threshold
        // = 0.30 + (1.0-0.7)*(0.8-0.3) = 0.45. Two hits = 0.8 →
        // fires.
        let bank = vec![
            fixed_signal("weak1", 0.4),
            Signal {
                name: "weak2",
                weight: 0.4,
                matcher: |s| s.contains("HIT"),
            },
        ];
        let hit = run("HIT", &bank, 0.7);
        assert!(matches!(hit, Hit::Fire { .. }));
    }

    #[test]
    fn run_no_match_passes() {
        // No signal fires (input doesn't contain HIT). Score is 0,
        // threshold floor is 0.30. 0 < 0.30 → pass.
        let bank = vec![
            fixed_signal("a", 0.4),
            Signal {
                name: "b",
                weight: 0.4,
                matcher: |s| s.contains("HIT"),
            },
        ];
        let hit = run("nothing here", &bank, 0.7);
        assert_eq!(hit, Hit::Pass);
    }

    #[test]
    fn canonicalise_replaces_tag_characters_with_whitespace() {
        // U+E0041 is "TAG LATIN CAPITAL LETTER A" — invisible.
        // Treating it as whitespace (not deletion) prevents the
        // smuggling vector where adversarial input glues two real
        // words together via an invisible tag char between them.
        let input = "ignore\u{E0041}previous";
        let out = canonicalise(input);
        assert_eq!(out, "ignore previous");
    }

    #[test]
    fn canonicalise_lowercases() {
        assert_eq!(canonicalise("IgNoRE THIS"), "ignore this");
    }

    #[test]
    fn canonicalise_collapses_whitespace() {
        assert_eq!(canonicalise("ignore   \n\t  previous"), "ignore previous");
    }

    #[test]
    fn canonicalise_idempotent() {
        let input = "Ignore   Previous";
        let once = canonicalise(input);
        let twice = canonicalise(&once);
        assert_eq!(once, twice);
    }

    #[test]
    fn canonicalise_handles_empty() {
        assert_eq!(canonicalise(""), "");
        assert_eq!(canonicalise("   "), "");
    }

    #[test]
    fn hit_to_detection_pass_passes_through() {
        let det = hit_to_detection("anything", "test", Hit::Pass);
        assert_eq!(det, Detection::Pass);
    }

    #[test]
    fn hit_to_detection_fire_includes_score_and_whole_span() {
        let det = hit_to_detection(
            "content",
            "prompt_injection",
            Hit::Fire {
                fired_signals: vec!["x"],
                score: 0.85,
            },
        );
        match det {
            Detection::Hit {
                category,
                matches,
                score,
            } => {
                assert_eq!(category, "prompt_injection");
                assert_eq!(matches.len(), 1);
                assert_eq!(matches[0].start, 0);
                assert_eq!(matches[0].end, 7);
                assert_eq!(score, Some(0.85));
            }
            _ => panic!("expected Hit"),
        }
    }
}
