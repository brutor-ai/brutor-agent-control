// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Banned Words / Regex Detectors
//!
//! Production refactor of the legacy `check_banned_words` /
//! `check_banned_patterns` helpers from `super::mod`. Lifts both
//! behind the [`Detector`] trait with three production additions:
//!
//!   * **Word-boundary mode** — the legacy default ("substring"
//!     match) catches "fuck" in legitimate "Scunthorpe". Operators
//!     can opt rows into `word_boundary` matching to fix that.
//!     Substring stays the default for backward compatibility.
//!   * **Per-entry action** — each entry can be `block` (default) or
//!     `warn`. Lets a single policy carry both an absolute block
//!     list and a softer audit list without splitting into two
//!     policies.
//!   * **Save-time regex pre-validation** — [`validate_pattern`] is
//!     called by the admin API before persisting. Today's runtime
//!     silently no-ops broken regexes, leaving the operator with a
//!     dead policy; the validator now returns a structured
//!     `policy_validation_error` with the offending pattern.
//!
//! Engine: `regex_lite` (linear-time, no look-around, no back-refs).

use regex_lite::Regex;
use std::sync::OnceLock;

use super::detector::{Detection, Detector, DetectorError, Span};

// ---------------------------------------------------------------------------
// Entry types
// ---------------------------------------------------------------------------

/// Per-entry action policy. Mirrors the global [`super::Action`] but
/// with only the two values that make sense for a list entry —
/// `redact` doesn't apply (banned words / patterns don't carry
/// per-match replacement strings).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EntryAction {
    Block,
    Warn,
}

impl EntryAction {
    pub fn parse(value: Option<&str>) -> Self {
        match value.unwrap_or("block").to_ascii_lowercase().as_str() {
            "warn" => EntryAction::Warn,
            // Block is the default — unknown values fall back to
            // it for safety.
            _ => EntryAction::Block,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            EntryAction::Block => "block",
            EntryAction::Warn => "warn",
        }
    }
}

/// Match strategy for banned words.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MatchMode {
    /// Substring match anywhere in the input: "fuck" hits "Scunthorpe".
    /// The mode of a policy that states none. Useful for partial-string
    /// blocklists.
    Substring,
    /// `\bword\b`-style word-boundary match. Fixes the Scunthorpe
    /// problem at the cost of missing partial-string matches.
    WordBoundary,
}

impl MatchMode {
    /// The mode a policy states: `substring`, `word_boundary`, or none
    /// (`substring`, the Control Plane's documented default). `None` for any
    /// other value: the gateway does not know how the list was meant to be
    /// matched, and the caller treats the check as one it cannot build.
    pub fn parse(value: Option<&str>) -> Option<Self> {
        match value {
            None | Some("substring") => Some(MatchMode::Substring),
            Some("word_boundary") => Some(MatchMode::WordBoundary),
            Some(_) => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            MatchMode::Substring => "substring",
            MatchMode::WordBoundary => "word_boundary",
        }
    }
}

/// One row in the banned-words list.
#[derive(Debug, Clone)]
pub struct BannedWord {
    pub word: String,
    pub action: EntryAction,
}

/// One row in the banned-patterns list. The regex is pre-compiled
/// at construction so runtime matching is fast and any compile error
/// surfaces at policy-load time, not on the request path.
#[derive(Debug, Clone)]
pub struct BannedPattern {
    pub source: String,
    pub action: EntryAction,
    pub regex: Regex,
}

impl BannedPattern {
    /// Compile + wrap a single pattern entry. Use [`validate_pattern`]
    /// from the admin API at save time so operators don't ship
    /// broken patterns to production.
    pub fn new(source: impl Into<String>, action: EntryAction) -> Result<Self, BannedRegexError> {
        let source = source.into();
        let regex = Regex::new(&source).map_err(|e| BannedRegexError {
            pattern: source.clone(),
            message: e.to_string(),
        })?;
        Ok(Self {
            source,
            action,
            regex,
        })
    }
}

/// Returned by [`validate_pattern`] / [`BannedPattern::new`] when a
/// regex doesn't compile. The admin API surfaces this as a 422 with
/// the offending pattern.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BannedRegexError {
    pub pattern: String,
    pub message: String,
}

impl std::fmt::Display for BannedRegexError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "regex `{}` failed to compile: {}",
            self.pattern, self.message
        )
    }
}

impl std::error::Error for BannedRegexError {}

/// Compile-only validation. Returns `Ok(())` if `pattern` would be
/// usable as a [`BannedPattern`]. Cheap (compiles + drops). Used by
/// the admin API at save time.
pub fn validate_pattern(pattern: &str) -> Result<(), BannedRegexError> {
    Regex::new(pattern)
        .map(|_| ())
        .map_err(|e| BannedRegexError {
            pattern: pattern.to_string(),
            message: e.to_string(),
        })
}

// ---------------------------------------------------------------------------
// Banned-words detector
// ---------------------------------------------------------------------------

/// Banned-words detector. Walks the configured wordlist and emits a
/// [`Detection::Hit`] on first match.
pub struct BannedWordsDetector {
    words: Vec<BannedWord>,
    mode: MatchMode,
}

impl BannedWordsDetector {
    pub fn new(words: Vec<BannedWord>, mode: MatchMode) -> Self {
        Self { words, mode }
    }
}

#[async_trait::async_trait]
impl Detector for BannedWordsDetector {
    fn name(&self) -> &'static str {
        "banned_words"
    }

    async fn detect(&self, content: &str) -> Result<Detection, DetectorError> {
        if self.words.is_empty() {
            return Ok(Detection::Pass);
        }
        let lower = content.to_lowercase();
        let bytes = lower.as_bytes();

        for entry in &self.words {
            let needle = entry.word.to_lowercase();
            let nb = needle.as_bytes();
            if nb.is_empty() || nb.len() > bytes.len() {
                continue;
            }
            for i in 0..=bytes.len() - nb.len() {
                if &bytes[i..i + nb.len()] != nb {
                    continue;
                }
                if matches!(self.mode, MatchMode::WordBoundary) {
                    let left_ok = i == 0 || !is_word_byte(bytes[i - 1]);
                    let right_ok =
                        (i + nb.len() == bytes.len()) || !is_word_byte(bytes[i + nb.len()]);
                    if !(left_ok && right_ok) {
                        continue;
                    }
                }
                // Hit — return first match. The orchestrator looks
                // at `Detection::Hit::category` plus the inline
                // entry to decide block vs warn; we encode the
                // entry's per-row action via the redaction string
                // (slight repurposing — `[ACTION:warn]` vs
                // `[ACTION:block]`) so the orchestrator can read it
                // without a second lookup. A per-match
                // action field on Detection::Hit would formalise this; the redaction-channel
                // workaround keeps the Detector trait stable.
                let redaction = match entry.action {
                    EntryAction::Block => "[BANNED-WORD:block]".to_string(),
                    EntryAction::Warn => "[BANNED-WORD:warn]".to_string(),
                };
                return Ok(Detection::Hit {
                    category: format!("banned_words:{}", entry.word),
                    matches: vec![Span {
                        start: i,
                        end: i + nb.len(),
                        redaction: Some(redaction),
                    }],
                    score: None,
                });
            }
        }
        Ok(Detection::Pass)
    }
}

// ---------------------------------------------------------------------------
// Banned-patterns detector
// ---------------------------------------------------------------------------

/// Banned-regex detector. Uses pre-compiled regexes from
/// [`BannedPattern`] entries.
pub struct BannedPatternsDetector {
    patterns: Vec<BannedPattern>,
}

impl BannedPatternsDetector {
    pub fn new(patterns: Vec<BannedPattern>) -> Self {
        Self { patterns }
    }
}

#[async_trait::async_trait]
impl Detector for BannedPatternsDetector {
    fn name(&self) -> &'static str {
        "banned_pattern"
    }

    async fn detect(&self, content: &str) -> Result<Detection, DetectorError> {
        for pattern in &self.patterns {
            if let Some(m) = pattern.regex.find(content) {
                let redaction = match pattern.action {
                    EntryAction::Block => "[BANNED-PATTERN:block]".to_string(),
                    EntryAction::Warn => "[BANNED-PATTERN:warn]".to_string(),
                };
                return Ok(Detection::Hit {
                    category: format!("banned_pattern:{}", pattern.source),
                    matches: vec![Span {
                        start: m.start(),
                        end: m.end(),
                        redaction: Some(redaction),
                    }],
                    score: None,
                });
            }
        }
        Ok(Detection::Pass)
    }
}

fn is_word_byte(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'_'
}

// Suppress the unused-import warning when no debug paths use OnceLock.
#[allow(dead_code)]
fn _ensure_oncelock_dep() -> OnceLock<()> {
    OnceLock::new()
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn words(entries: &[(&str, EntryAction)], mode: MatchMode) -> BannedWordsDetector {
        BannedWordsDetector::new(
            entries
                .iter()
                .map(|(w, a)| BannedWord {
                    word: (*w).to_string(),
                    action: *a,
                })
                .collect(),
            mode,
        )
    }

    fn patterns(entries: &[(&str, EntryAction)]) -> BannedPatternsDetector {
        BannedPatternsDetector::new(
            entries
                .iter()
                .map(|(p, a)| BannedPattern::new(*p, *a).expect("test pattern compiles"))
                .collect(),
        )
    }

    // ----- EntryAction parsing ---------------------------------------------

    #[test]
    fn entry_action_parses_known() {
        assert_eq!(EntryAction::parse(Some("block")), EntryAction::Block);
        assert_eq!(EntryAction::parse(Some("warn")), EntryAction::Warn);
    }

    #[test]
    fn entry_action_unknown_defaults_block() {
        assert_eq!(EntryAction::parse(None), EntryAction::Block);
        assert_eq!(EntryAction::parse(Some("yolo")), EntryAction::Block);
    }

    #[test]
    fn match_mode_parses_the_two_modes_and_the_default() {
        assert_eq!(
            MatchMode::parse(Some("substring")),
            Some(MatchMode::Substring)
        );
        assert_eq!(
            MatchMode::parse(Some("word_boundary")),
            Some(MatchMode::WordBoundary)
        );
        // Unset: the documented default.
        assert_eq!(MatchMode::parse(None), Some(MatchMode::Substring));
    }

    #[test]
    fn an_unknown_match_mode_is_no_mode() {
        for value in [
            "typo",
            "WordBoundary",
            "wordboundary",
            "boundary",
            "regex",
            "",
        ] {
            assert_eq!(MatchMode::parse(Some(value)), None, "{value:?}");
        }
    }

    // ----- Pattern validation ----------------------------------------------

    #[test]
    fn validate_pattern_accepts_compiling_regex() {
        assert!(validate_pattern(r"\d{3}-\d{2}-\d{4}").is_ok());
        assert!(validate_pattern(r"^foo").is_ok());
    }

    #[test]
    fn validate_pattern_rejects_broken_regex() {
        let err = validate_pattern(r"unclosed[group").unwrap_err();
        assert_eq!(err.pattern, "unclosed[group");
        assert!(!err.message.is_empty());
    }

    #[test]
    fn banned_pattern_new_propagates_compile_error() {
        let err = BannedPattern::new("(unclosed", EntryAction::Block).unwrap_err();
        assert!(err.message.contains("group") || err.message.contains("paren"));
    }

    // ----- Banned words: substring ------------------------------------------

    #[tokio::test]
    async fn substring_basic_hit() {
        let d = words(&[("forbidden", EntryAction::Block)], MatchMode::Substring);
        match d.detect("this is forbidden text").await.unwrap() {
            Detection::Hit { category, .. } => {
                assert!(category.starts_with("banned_words:"));
            }
            _ => panic!("expected Hit"),
        }
    }

    #[tokio::test]
    async fn substring_case_insensitive() {
        let d = words(&[("Forbidden", EntryAction::Block)], MatchMode::Substring);
        assert!(matches!(
            d.detect("this is FORBIDDEN content").await.unwrap(),
            Detection::Hit { .. }
        ));
    }

    #[tokio::test]
    async fn substring_matches_inside_word() {
        // The legacy substring mode catches "scun" in "Scunthorpe".
        // This test pins the documented behaviour — operators who
        // don't want this opt into word_boundary mode.
        let d = words(&[("scun", EntryAction::Block)], MatchMode::Substring);
        assert!(matches!(
            d.detect("Scunthorpe").await.unwrap(),
            Detection::Hit { .. }
        ));
    }

    // ----- Banned words: word_boundary --------------------------------------

    #[tokio::test]
    async fn word_boundary_basic_hit() {
        let d = words(&[("dan", EntryAction::Block)], MatchMode::WordBoundary);
        assert!(matches!(
            d.detect("you are DAN now").await.unwrap(),
            Detection::Hit { .. }
        ));
    }

    #[tokio::test]
    async fn word_boundary_skips_substring_inside_word() {
        // The Scunthorpe problem fix.
        let d = words(&[("dan", EntryAction::Block)], MatchMode::WordBoundary);
        assert_eq!(
            d.detect("Sudan is a country").await.unwrap(),
            Detection::Pass
        );
    }

    #[tokio::test]
    async fn word_boundary_handles_punctuation() {
        let d = words(&[("dan", EntryAction::Block)], MatchMode::WordBoundary);
        assert!(matches!(
            d.detect("(dan), here we go").await.unwrap(),
            Detection::Hit { .. }
        ));
    }

    // ----- Banned words: per-entry action -----------------------------------

    #[tokio::test]
    async fn block_action_encoded_in_redaction() {
        let d = words(&[("secret", EntryAction::Block)], MatchMode::Substring);
        match d.detect("my secret stash").await.unwrap() {
            Detection::Hit { matches, .. } => {
                let red = matches[0].redaction.as_ref().unwrap();
                assert!(red.contains(":block"));
            }
            _ => panic!("expected Hit"),
        }
    }

    #[tokio::test]
    async fn warn_action_encoded_in_redaction() {
        let d = words(&[("secret", EntryAction::Warn)], MatchMode::Substring);
        match d.detect("my secret stash").await.unwrap() {
            Detection::Hit { matches, .. } => {
                let red = matches[0].redaction.as_ref().unwrap();
                assert!(red.contains(":warn"));
            }
            _ => panic!("expected Hit"),
        }
    }

    // ----- Banned patterns -------------------------------------------------

    #[tokio::test]
    async fn pattern_basic_hit() {
        let d = patterns(&[(r"\d{3}-\d{2}-\d{4}", EntryAction::Block)]);
        match d.detect("ssn is 123-45-6789").await.unwrap() {
            Detection::Hit { category, .. } => {
                assert!(category.starts_with("banned_pattern:"));
            }
            _ => panic!("expected Hit"),
        }
    }

    #[tokio::test]
    async fn pattern_no_match_passes() {
        let d = patterns(&[(r"\d{3}-\d{2}-\d{4}", EntryAction::Block)]);
        assert_eq!(
            d.detect("nothing numeric here").await.unwrap(),
            Detection::Pass
        );
    }

    #[tokio::test]
    async fn pattern_first_match_wins() {
        // Two patterns both match. First one in the list returns
        // its action.
        let d = patterns(&[(r"foo", EntryAction::Warn), (r"foo", EntryAction::Block)]);
        match d.detect("foo bar").await.unwrap() {
            Detection::Hit { matches, .. } => {
                assert!(matches[0].redaction.as_ref().unwrap().contains(":warn"));
            }
            _ => panic!("expected Hit"),
        }
    }

    // ----- Empty / passthrough ---------------------------------------------

    #[tokio::test]
    async fn empty_word_list_passes() {
        let d = BannedWordsDetector::new(vec![], MatchMode::Substring);
        assert_eq!(d.detect("anything").await.unwrap(), Detection::Pass);
    }

    #[tokio::test]
    async fn empty_pattern_list_passes() {
        let d = BannedPatternsDetector::new(vec![]);
        assert_eq!(d.detect("anything").await.unwrap(), Detection::Pass);
    }

    #[tokio::test]
    async fn empty_input_passes() {
        let d = words(&[("forbidden", EntryAction::Block)], MatchMode::Substring);
        assert_eq!(d.detect("").await.unwrap(), Detection::Pass);
    }

    // ----- Detector trait shape --------------------------------------------

    #[tokio::test]
    async fn words_detector_name() {
        let d = BannedWordsDetector::new(vec![], MatchMode::Substring);
        assert_eq!(d.name(), "banned_words");
    }

    #[tokio::test]
    async fn patterns_detector_name() {
        let d = BannedPatternsDetector::new(vec![]);
        assert_eq!(d.name(), "banned_pattern");
    }
}
