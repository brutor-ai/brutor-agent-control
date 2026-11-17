// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Rule-based prompt complexity scoring for tier-based model selection.
//!
//! # Why rules and not a model
//!
//! The alternative approach — asking a small LLM to classify the prompt — costs
//! a network round trip (200–330 ms in the reference implementations) and a
//! model call on *every* request, and returns a non-deterministic answer that
//! cannot be explained to an auditor. Scoring locally costs microseconds, is
//! reproducible, and yields the *reason* alongside the verdict, which is what
//! lets the routing decision sit on the audit trail as a governed decision
//! rather than an opaque one.
//!
//! # What it scores
//!
//! Eight weighted dimensions summed into one score, then mapped to a tier:
//! `Simple → Medium → Complex → Reasoning`. A tier is mapped to a model by the
//! routing group; this module never touches models, config, or the database.
//!
//! # Deliberate design choices
//!
//! * **Asymmetric text scope.** Code / technical / simple-indicator dimensions
//!   read the system prompt *and* the user message, because a system prompt
//!   ("You are a Python assistant") is real evidence about the work. Reasoning
//!   markers read the **user message only** — otherwise a system prompt
//!   containing "think step by step" pins every request in the deployment to
//!   the most expensive tier forever.
//! * **Word-boundary matching.** Naive substring matching fires `api` inside
//!   *capital* and `error` inside *terrorism*. Single-word keywords must match
//!   on boundaries; multi-word phrases are matched as substrings, since a
//!   phrase is already specific enough.
//! * **Bounded tool signal.** An agent step carries a long tool list. If tool
//!   count fed the score without a ceiling, every agent request would grade
//!   `Complex` and the strategy would save nothing. Tools contribute a small
//!   capped term and can never on their own reach the top tier.
//! * **Reasoning override.** Two or more explicit reasoning markers jump
//!   straight to the top tier regardless of the weighted sum — an explicit
//!   request to reason is not something to average away.
//!
//! # Limitations (state these, never paper over them)
//!
//! Keyword lists are English. The scorer is a **cost heuristic, never a
//! control**: it is defeatable by unusual phrasing, and nothing security- or
//! policy-relevant may ever depend on its verdict. The governance answer to a
//! bad score is the routing group's floor tier, not a better keyword list.

use std::fmt;

/// Complexity tiers, ordered cheapest → most capable.
///
/// `Ord` follows that order so a floor tier can be enforced with `max()`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum ComplexityTier {
    Simple,
    Medium,
    Complex,
    Reasoning,
}

impl ComplexityTier {
    /// Stable wire/DB representation. Pinned by test — changing these strings
    /// silently rewrites operators' tier→model mappings.
    pub fn as_str(&self) -> &'static str {
        match self {
            ComplexityTier::Simple => "simple",
            ComplexityTier::Medium => "medium",
            ComplexityTier::Complex => "complex",
            ComplexityTier::Reasoning => "reasoning",
        }
    }

    /// Parse a tier from its stored string. Case-insensitive on read so a
    /// hand-edited config is forgiving, while [`Self::as_str`] always writes
    /// the canonical lowercase form.
    ///
    /// Deliberately not `std::str::FromStr`: an unknown tier is a routine
    /// "no floor configured" outcome, not an error worth a type.
    #[allow(clippy::should_implement_trait)]
    pub fn from_str(s: &str) -> Option<Self> {
        match s.trim().to_ascii_lowercase().as_str() {
            "simple" => Some(ComplexityTier::Simple),
            "medium" => Some(ComplexityTier::Medium),
            "complex" => Some(ComplexityTier::Complex),
            "reasoning" => Some(ComplexityTier::Reasoning),
            _ => None,
        }
    }
}

impl fmt::Display for ComplexityTier {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// What the scorer reads. Deliberately borrowed and tiny: the caller extracts
/// these from the request, and the scorer never sees the request itself.
#[derive(Debug, Default, Clone)]
pub struct ClassifyInput<'a> {
    /// The latest user message. The primary signal.
    pub user_text: &'a str,
    /// Concatenated system/developer instructions, if any.
    pub system_text: Option<&'a str>,
    /// Number of tools offered on this request (not their contents).
    pub tool_count: usize,
}

/// The verdict, including *why* — the signals are what make the decision
/// explainable on the audit trail and in the admin UI.
#[derive(Debug, Clone, PartialEq)]
pub struct ComplexityVerdict {
    pub tier: ComplexityTier,
    pub score: f64,
    /// Names of dimensions that contributed, in evaluation order.
    pub signals: Vec<&'static str>,
}

// ===========================================================================
// Configuration
// ===========================================================================

/// Per-dimension weights. Defaults follow the reference implementation, whose
/// balance has been exercised in production; retune deliberately, not casually.
#[derive(Debug, Clone, PartialEq)]
pub struct Weights {
    pub token_count: f64,
    pub code_presence: f64,
    pub reasoning_markers: f64,
    pub technical_terms: f64,
    pub simple_indicators: f64,
    pub multi_step: f64,
    pub question_complexity: f64,
    pub tool_presence: f64,
}

impl Default for Weights {
    fn default() -> Self {
        Self {
            token_count: 0.10,
            code_presence: 0.30,
            reasoning_markers: 0.25,
            technical_terms: 0.25,
            // Negative-scoring dimension; the weight is its magnitude.
            simple_indicators: 0.05,
            multi_step: 0.03,
            question_complexity: 0.02,
            // Ours. Small on purpose — see the module docs on agent steps.
            tool_presence: 0.05,
        }
    }
}

/// Upper score bound for each tier. A score above `complex_max` is `Reasoning`.
#[derive(Debug, Clone, PartialEq)]
pub struct Boundaries {
    pub simple_max: f64,
    pub medium_max: f64,
    pub complex_max: f64,
}

impl Default for Boundaries {
    fn default() -> Self {
        Self {
            simple_max: 0.15,
            medium_max: 0.35,
            complex_max: 0.60,
        }
    }
}

pub const DEFAULT_CODE_KEYWORDS: &[&str] = &[
    "function",
    "class",
    "def ",
    "import",
    "return",
    "async",
    "await",
    "const",
    "struct",
    "impl",
    "trait",
    "interface",
    "compile",
    "refactor",
    "stack trace",
    "traceback",
    "exception",
    "unit test",
    "regex",
    "sql",
    "query",
    "schema",
    "api",
    "endpoint",
    "docker",
    "kubernetes",
    "git",
    "merge conflict",
];

pub const DEFAULT_REASONING_KEYWORDS: &[&str] = &[
    "step by step",
    "step-by-step",
    "think through",
    "reason about",
    "explain why",
    "walk me through",
    "prove",
    "derive",
    "analyse",
    "analyze",
    "trade-off",
    "tradeoff",
    "compare and contrast",
    "pros and cons",
    "root cause",
    "justify",
    "in detail",
];

pub const DEFAULT_TECHNICAL_KEYWORDS: &[&str] = &[
    "architecture",
    "distributed",
    "concurrency",
    "latency",
    "throughput",
    "algorithm",
    "complexity",
    "optimise",
    "optimize",
    "benchmark",
    "migration",
    "encryption",
    "authentication",
    "authorization",
    "protocol",
    "topology",
    "idempotent",
    "consistency",
    "partition",
    "scalability",
];

pub const DEFAULT_SIMPLE_KEYWORDS: &[&str] = &[
    "what is",
    "who is",
    "when is",
    "where is",
    "define",
    "definition of",
    "hello",
    "hi ",
    "thanks",
    "thank you",
    "translate",
    "spell",
    "capital of",
    "how do you say",
];

pub const DEFAULT_MULTI_STEP_PATTERNS: &[&str] = &[
    "first",
    "then",
    "next",
    "finally",
    "after that",
    "step 1",
    "step 2",
    "1.",
    "2.",
    "3.",
];

/// Full scorer configuration. `Default` is the shipped behaviour; operators
/// override per routing group.
#[derive(Debug, Clone, PartialEq)]
pub struct ComplexityConfig {
    pub weights: Weights,
    pub boundaries: Boundaries,
    pub code_keywords: Vec<String>,
    pub reasoning_keywords: Vec<String>,
    pub technical_keywords: Vec<String>,
    pub simple_keywords: Vec<String>,
    pub multi_step_patterns: Vec<String>,
    /// Reasoning-marker count that forces the top tier outright.
    pub reasoning_override_count: usize,
    /// Tool count at which the (capped) tool signal saturates.
    pub tool_saturation_count: usize,
    /// Characters per token for the cheap length estimate. Real tokenisation on
    /// the hot path would cost more than the routing decision saves.
    pub chars_per_token: usize,
    /// Token count at which the length dimension saturates.
    pub token_saturation: usize,
}

impl Default for ComplexityConfig {
    fn default() -> Self {
        Self {
            weights: Weights::default(),
            boundaries: Boundaries::default(),
            code_keywords: to_owned(DEFAULT_CODE_KEYWORDS),
            reasoning_keywords: to_owned(DEFAULT_REASONING_KEYWORDS),
            technical_keywords: to_owned(DEFAULT_TECHNICAL_KEYWORDS),
            simple_keywords: to_owned(DEFAULT_SIMPLE_KEYWORDS),
            multi_step_patterns: to_owned(DEFAULT_MULTI_STEP_PATTERNS),
            reasoning_override_count: 2,
            tool_saturation_count: 8,
            chars_per_token: 4,
            token_saturation: 2000,
        }
    }
}

fn to_owned(xs: &[&str]) -> Vec<String> {
    xs.iter().map(|s| (*s).to_string()).collect()
}

// ===========================================================================
// Matching
// ===========================================================================

/// Does `haystack` contain `needle` as a *word*?
///
/// Multi-word or punctuation-bearing needles ("step by step", "def ", "1.")
/// are specific enough to match as plain substrings. Single words must sit on
/// non-alphanumeric boundaries, so `api` does not fire inside *capital* and
/// `git` does not fire inside *digital*.
///
/// Both inputs are expected lowercase.
fn contains_word(haystack: &str, needle: &str) -> bool {
    if needle.is_empty() {
        return false;
    }
    let multiword = needle.contains(|c: char| !c.is_alphanumeric());
    if multiword {
        return haystack.contains(needle);
    }

    let mut from = 0usize;
    while let Some(rel) = haystack[from..].find(needle) {
        let start = from + rel;
        let end = start + needle.len();

        let before_ok = haystack[..start]
            .chars()
            .next_back()
            .is_none_or(|c| !c.is_alphanumeric() && c != '_');
        let after_ok = haystack[end..]
            .chars()
            .next()
            .is_none_or(|c| !c.is_alphanumeric() && c != '_');

        if before_ok && after_ok {
            return true;
        }
        // Advance past this occurrence. `needle` is non-empty so this
        // terminates.
        from = end;
        if from >= haystack.len() {
            break;
        }
    }
    false
}

/// Count how many distinct keywords from `keywords` appear in `text`.
fn count_matches(text: &str, keywords: &[String]) -> usize {
    keywords.iter().filter(|k| contains_word(text, k)).count()
}

/// Map a match count onto `[0, 1]`: 0 matches → 0.0, 1 → `single`, ≥2 → 1.0.
fn graded(count: usize, single: f64) -> f64 {
    match count {
        0 => 0.0,
        1 => single,
        _ => 1.0,
    }
}

// ===========================================================================
// Scoring
// ===========================================================================

/// Score a prompt and map it to a tier.
///
/// Pure and allocation-light: no I/O, no locks, no config loading. Typical
/// cost is a few microseconds, which is why this strategy can run inline on
/// every request without a budget.
pub fn classify(input: &ClassifyInput<'_>, cfg: &ComplexityConfig) -> ComplexityVerdict {
    let user_text = input.user_text.to_lowercase();
    // Code/technical/simple dimensions see the system prompt too: it is real
    // evidence about the deployment's work. Reasoning markers deliberately do
    // not — see the module docs.
    let full_text = match input.system_text {
        Some(sys) if !sys.is_empty() => format!("{} {}", sys.to_lowercase(), user_text),
        _ => user_text.clone(),
    };

    let mut signals: Vec<&'static str> = Vec::new();
    let w = &cfg.weights;
    let mut score = 0.0f64;

    // -- length ------------------------------------------------------------
    let est_tokens = input.user_text.len() / cfg.chars_per_token.max(1);
    let length_score = (est_tokens as f64 / cfg.token_saturation.max(1) as f64).clamp(0.0, 1.0);
    if length_score > 0.0 {
        score += length_score * w.token_count;
        if length_score > 0.5 {
            signals.push("long_prompt");
        }
    }

    // -- code --------------------------------------------------------------
    let code_score = graded(count_matches(&full_text, &cfg.code_keywords), 0.5);
    if code_score > 0.0 {
        score += code_score * w.code_presence;
        signals.push("code");
    }

    // -- reasoning (USER TEXT ONLY) ----------------------------------------
    let reasoning_matches = count_matches(&user_text, &cfg.reasoning_keywords);
    let reasoning_score = graded(reasoning_matches, 0.7);
    if reasoning_score > 0.0 {
        score += reasoning_score * w.reasoning_markers;
        signals.push("reasoning");
    }

    // -- technical ---------------------------------------------------------
    let technical_matches = count_matches(&full_text, &cfg.technical_keywords);
    let technical_score = if technical_matches >= 2 {
        1.0
    } else if technical_matches == 1 {
        0.5
    } else {
        0.0
    };
    if technical_score > 0.0 {
        score += technical_score * w.technical_terms;
        signals.push("technical");
    }

    // -- simple indicators (negative) --------------------------------------
    let simple_matches = count_matches(&full_text, &cfg.simple_keywords);
    if simple_matches > 0 {
        score -= w.simple_indicators;
        signals.push("simple_phrasing");
    }

    // -- multi-step --------------------------------------------------------
    let multi_matches = count_matches(&user_text, &cfg.multi_step_patterns);
    if multi_matches >= 2 {
        score += w.multi_step;
        signals.push("multi_step");
    }

    // -- question complexity ------------------------------------------------
    let questions = input.user_text.matches('?').count();
    if questions > 1 {
        score += w.question_complexity;
        signals.push("multi_question");
    }

    // -- tools (bounded; ours) ---------------------------------------------
    if input.tool_count > 0 {
        let tool_score =
            (input.tool_count as f64 / cfg.tool_saturation_count.max(1) as f64).clamp(0.0, 1.0);
        score += tool_score * w.tool_presence;
        signals.push("tools");
    }

    // Clamp before tiering so a pathological keyword-stuffed prompt cannot
    // produce a score outside the configured boundary space.
    let score = score.clamp(0.0, 1.0);

    // Explicit reasoning requests bypass the weighted sum entirely: someone
    // who asked twice to be reasoned with should not be averaged down to a
    // cheap model by an otherwise short prompt.
    let tier = if reasoning_matches >= cfg.reasoning_override_count {
        signals.push("reasoning_override");
        ComplexityTier::Reasoning
    } else {
        tier_for_score(score, &cfg.boundaries)
    };

    ComplexityVerdict {
        tier,
        score,
        signals,
    }
}

/// Map a score to a tier using the configured boundaries.
pub fn tier_for_score(score: f64, b: &Boundaries) -> ComplexityTier {
    if score < b.simple_max {
        ComplexityTier::Simple
    } else if score < b.medium_max {
        ComplexityTier::Medium
    } else if score < b.complex_max {
        ComplexityTier::Complex
    } else {
        ComplexityTier::Reasoning
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn input<'a>(user: &'a str) -> ClassifyInput<'a> {
        ClassifyInput {
            user_text: user,
            system_text: None,
            tool_count: 0,
        }
    }

    fn tier_of(user: &str) -> ComplexityTier {
        classify(&input(user), &ComplexityConfig::default()).tier
    }

    // ---- word-boundary matching: the false-positive class ----------------
    // These are the failures the reference implementation shipped regression
    // tests for. Naive `contains` fires on every one of them.

    #[test]
    fn keyword_matching_respects_word_boundaries() {
        assert!(!contains_word("what is the capital of france", "api"));
        assert!(!contains_word("the digital divide", "git"));
        assert!(!contains_word("a history of terrorism", "error"));
        assert!(!contains_word("classical music", "class"));
        assert!(!contains_word("a new industry emerged", "merge"));
        assert!(!contains_word("she tried poetry", "try"));

        // ...while still matching the real thing.
        assert!(contains_word("call the api endpoint", "api"));
        assert!(contains_word("git rebase failed", "git"));
        assert!(contains_word("throws an error", "error"));
    }

    #[test]
    fn multiword_keywords_match_as_substrings() {
        assert!(contains_word("please think through this", "think through"));
        assert!(contains_word("do it step by step", "step by step"));
        assert!(!contains_word("thinking about it", "think through"));
    }

    #[test]
    fn word_matching_handles_repeats_and_bounds() {
        // First occurrence is embedded, second is a real word — must find the
        // second rather than stopping at the first.
        assert!(contains_word("capital api", "api"));
        // Trailing occurrence at end of string.
        assert!(contains_word("call the api", "api"));
        // Leading occurrence at start of string.
        assert!(contains_word("api call", "api"));
        // Underscores count as word characters (identifiers, not prose).
        assert!(!contains_word("some_api_call", "api"));
    }

    // ---- tiering ---------------------------------------------------------

    #[test]
    fn trivial_questions_are_simple() {
        assert_eq!(tier_of("hello"), ComplexityTier::Simple);
        assert_eq!(
            tier_of("what is the capital of france?"),
            ComplexityTier::Simple
        );
        assert_eq!(tier_of("thanks!"), ComplexityTier::Simple);
    }

    #[test]
    fn explicit_reasoning_requests_reach_the_top_tier() {
        // Two markers ("step by step" + "explain why") trigger the override.
        let v = classify(
            &input("walk me through this step by step and explain why it works"),
            &ComplexityConfig::default(),
        );
        assert_eq!(v.tier, ComplexityTier::Reasoning);
        assert!(v.signals.contains(&"reasoning_override"));
    }

    #[test]
    fn code_and_technical_content_scores_above_trivial() {
        let simple = classify(&input("hi there"), &ComplexityConfig::default());
        let code = classify(
            &input("refactor this function and fix the stack trace in the sql query"),
            &ComplexityConfig::default(),
        );
        assert!(
            code.score > simple.score,
            "code-bearing prompt must outscore a greeting ({} vs {})",
            code.score,
            simple.score
        );
        assert!(code.tier > ComplexityTier::Simple);
    }

    // ---- asymmetric scope: the system-prompt trap ------------------------

    #[test]
    fn system_prompt_cannot_force_the_reasoning_tier() {
        // The trap: a deployment whose system prompt says "think step by step"
        // would otherwise pin EVERY request to the most expensive model.
        let cfg = ComplexityConfig::default();
        let with_sys = classify(
            &ClassifyInput {
                user_text: "hi",
                system_text: Some(
                    "You are a careful assistant. Think step by step. Reason about every answer.",
                ),
                tool_count: 0,
            },
            &cfg,
        );
        assert_ne!(
            with_sys.tier,
            ComplexityTier::Reasoning,
            "reasoning markers must be read from the user message only"
        );
        assert!(!with_sys.signals.contains(&"reasoning_override"));
    }

    #[test]
    fn system_prompt_still_informs_code_and_technical_signals() {
        // The other half of the asymmetry: a system prompt IS evidence for what
        // kind of work this deployment does.
        let cfg = ComplexityConfig::default();
        let bare = classify(&input("fix it"), &cfg);
        let with_sys = classify(
            &ClassifyInput {
                user_text: "fix it",
                system_text: Some("You are a Python assistant. Return a function and a unit test."),
                tool_count: 0,
            },
            &cfg,
        );
        assert!(
            with_sys.score > bare.score,
            "system prompt must contribute to code/technical dimensions"
        );
        assert!(with_sys.signals.contains(&"code"));
    }

    // ---- the agent case: bounded tool signal -----------------------------

    #[test]
    fn a_long_tool_list_alone_cannot_reach_the_top_tier() {
        // Agent steps carry many tools. If tools fed the score unbounded, every
        // agent request would grade expensive and the strategy would save
        // nothing.
        let cfg = ComplexityConfig::default();
        let v = classify(
            &ClassifyInput {
                user_text: "ok",
                system_text: None,
                tool_count: 64,
            },
            &cfg,
        );
        assert!(
            v.tier < ComplexityTier::Reasoning,
            "tool count alone must not reach the top tier (got {:?}, score {})",
            v.tier,
            v.score
        );
    }

    #[test]
    fn tools_still_contribute_a_signal() {
        let cfg = ComplexityConfig::default();
        let without = classify(&input("ok"), &cfg);
        let with = classify(
            &ClassifyInput {
                user_text: "ok",
                system_text: None,
                tool_count: 4,
            },
            &cfg,
        );
        assert!(with.score > without.score);
        assert!(with.signals.contains(&"tools"));
    }

    // ---- determinism and bounds ------------------------------------------

    #[test]
    fn classification_is_deterministic() {
        let cfg = ComplexityConfig::default();
        let prompt = "analyse the trade-offs in this distributed architecture";
        let a = classify(&input(prompt), &cfg);
        let b = classify(&input(prompt), &cfg);
        assert_eq!(a, b, "same input and config must always yield same verdict");
    }

    #[test]
    fn score_stays_within_bounds_on_pathological_input() {
        let cfg = ComplexityConfig::default();
        // Every keyword class at once, very long, many questions and tools.
        let stuffed = format!(
            "{} {}",
            "function class import async await step by step explain why prove derive \
             architecture distributed concurrency latency algorithm encryption ??? ",
            "x".repeat(50_000)
        );
        let v = classify(
            &ClassifyInput {
                user_text: &stuffed,
                system_text: Some("docker kubernetes sql schema"),
                tool_count: 500,
            },
            &cfg,
        );
        assert!(
            (0.0..=1.0).contains(&v.score),
            "score must stay in [0,1], got {}",
            v.score
        );
    }

    #[test]
    fn empty_and_unicode_input_do_not_panic() {
        let cfg = ComplexityConfig::default();
        for text in ["", "   ", "🎉🎉🎉", "日本語のテキスト", "café naïve"] {
            let v = classify(&input(text), &cfg);
            assert!((0.0..=1.0).contains(&v.score));
        }
    }

    // ---- config -----------------------------------------------------------

    #[test]
    fn boundaries_are_configurable() {
        // Force everything into the top tier by collapsing the boundaries.
        let cfg = ComplexityConfig {
            boundaries: Boundaries {
                simple_max: 0.0,
                medium_max: 0.0,
                complex_max: 0.0,
            },
            ..Default::default()
        };
        assert_eq!(
            tier_for_score(0.0, &cfg.boundaries),
            ComplexityTier::Reasoning
        );
        assert_eq!(
            classify(&input("hello"), &cfg).tier,
            ComplexityTier::Reasoning
        );
    }

    #[test]
    fn keyword_lists_are_configurable() {
        let cfg = ComplexityConfig {
            reasoning_keywords: vec!["zork".to_string()],
            reasoning_override_count: 1,
            ..Default::default()
        };
        // The default marker no longer counts...
        assert_ne!(
            classify(&input("think through this"), &cfg).tier,
            ComplexityTier::Reasoning
        );
        // ...and the custom one does.
        assert_eq!(
            classify(&input("zork this please"), &cfg).tier,
            ComplexityTier::Reasoning
        );
    }

    // ---- wire contract ----------------------------------------------------

    #[test]
    fn tier_strings_are_stable() {
        // These strings are stored against operators' tier→model mappings.
        // Changing one silently repoints their traffic.
        assert_eq!(ComplexityTier::Simple.as_str(), "simple");
        assert_eq!(ComplexityTier::Medium.as_str(), "medium");
        assert_eq!(ComplexityTier::Complex.as_str(), "complex");
        assert_eq!(ComplexityTier::Reasoning.as_str(), "reasoning");

        for t in [
            ComplexityTier::Simple,
            ComplexityTier::Medium,
            ComplexityTier::Complex,
            ComplexityTier::Reasoning,
        ] {
            assert_eq!(ComplexityTier::from_str(t.as_str()), Some(t));
        }
        assert_eq!(
            ComplexityTier::from_str("REASONING"),
            Some(ComplexityTier::Reasoning)
        );
        assert_eq!(ComplexityTier::from_str("nonsense"), None);
    }

    #[test]
    fn tiers_order_cheapest_first() {
        // `Ord` is what lets a routing group enforce a floor with `max()`.
        assert!(ComplexityTier::Simple < ComplexityTier::Medium);
        assert!(ComplexityTier::Medium < ComplexityTier::Complex);
        assert!(ComplexityTier::Complex < ComplexityTier::Reasoning);
        assert_eq!(
            ComplexityTier::Simple.max(ComplexityTier::Complex),
            ComplexityTier::Complex,
            "floor enforcement relies on max()"
        );
    }
}
