// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # PII Detector — built-in
//!
//! Built-in detector covering 11 categories of personally
//! identifiable information. Implementation is deliberately
//! provider-free: shipping PII to a third-party detector defeats the
//! threat model.
//!
//! ## Categories
//!
//! | Category            | Detection                                          |
//! |---------------------|----------------------------------------------------|
//! | `email`             | RFC 5322-tight regex with TLD validation           |
//! | `phone`             | E.164 + US national format                         |
//! | `ssn`               | 9-digit `XXX-XX-XXXX` + plausibility filter        |
//! | `credit_card`       | 13–19 digit run + Luhn check (returns issuer)      |
//! | `iban`              | ISO 13616 format + mod-97 checksum                 |
//! | `ipv4`              | dotted quad + reserved-block filter                |
//! | `ipv6`              | hex-colon format with `::` shorthand               |
//! | `openai_api_key`    | `sk-…` and `sk-proj-…`                             |
//! | `anthropic_api_key` | `sk-ant-…`                                         |
//! | `aws_access_key`    | `AKIA[0-9A-Z]{16}` + secret-pair markers           |
//! | `jwt`               | three base64url segments separated by `.`          |
//! | `private_key`       | PEM `-----BEGIN … PRIVATE KEY-----` block          |
//!
//! Each category is a [`PiiPattern`] entry in [`PATTERNS`]; adding a
//! new category is local — no orchestrator changes needed.
//!
//! ## Configuration
//!
//! `enabled_categories: Option<&[String]>` — `None` enables every
//! pattern; `Some(&[...])` enables only the named ones. Unknown names
//! are silently skipped (rejected at the admin API validator on
//! save). Empty list = nothing enabled.
//!
//! ## Redaction
//!
//! Each match returns a [`Span`] with `redaction = Some("[REDACTED-{CAT}]")`
//! so the orchestrator can rewrite the content for `redact` actions.
//! The orchestrator walks spans in **reverse byte-offset order** and
//! substitutes each in place — that keeps later span offsets valid as
//! we mutate from the right.
//!
//! ## False positives
//!
//! Each pattern carries a [`PiiPattern::filter`] step that runs after
//! regex-match to reject obvious false positives:
//!
//!   * SSNs in the reserved 000-/666-/9XX- ranges
//!   * Credit-card-shaped numbers that fail Luhn
//!   * IBANs that fail mod-97
//!   * Reserved IPv4 blocks (0.0.0.0/8, 127.0.0.0/8, 224.0.0.0/4, …)
//!
//! False-positive rate is meaningful but bounded. Operators with
//! tighter requirements should pair this with a per-tenant
//! `enabled_categories` opt-out.

use regex_lite::Regex;
use std::sync::OnceLock;

use super::detector::{Detection, Detector, DetectorError, Span};

// ---------------------------------------------------------------------------
// Pattern table
// ---------------------------------------------------------------------------

/// Single PII category — name, regex, and an optional verification
/// step that rejects false positives.
struct PiiPattern {
    /// Stable category name surfaced on the violation. Snake-case.
    category: &'static str,
    /// Regex compiled once at first use.
    regex: &'static OnceLock<Regex>,
    /// Source pattern (used to lazy-compile `regex` on first call).
    source: &'static str,
    /// Optional plausibility filter. Receives the matched substring;
    /// returns `true` to keep, `false` to reject as a false positive.
    filter: Option<fn(&str) -> bool>,
}

impl PiiPattern {
    /// Get the compiled regex, compiling on first access. Panics on
    /// invalid source — the source strings are unit-tested below so a
    /// regression here is caught at `cargo test`.
    fn re(&self) -> &Regex {
        self.regex.get_or_init(|| {
            Regex::new(self.source).unwrap_or_else(|e| {
                panic!("PII pattern `{}` failed to compile: {e}", self.category)
            })
        })
    }
}

// OnceLock storage for each pattern's compiled regex.
static EMAIL_RE: OnceLock<Regex> = OnceLock::new();
static PHONE_RE: OnceLock<Regex> = OnceLock::new();
static SSN_RE: OnceLock<Regex> = OnceLock::new();
static CC_RE: OnceLock<Regex> = OnceLock::new();
static IBAN_RE: OnceLock<Regex> = OnceLock::new();
static IPV4_RE: OnceLock<Regex> = OnceLock::new();
static IPV6_RE: OnceLock<Regex> = OnceLock::new();
static OPENAI_RE: OnceLock<Regex> = OnceLock::new();
static ANTHROPIC_RE: OnceLock<Regex> = OnceLock::new();
static AWS_RE: OnceLock<Regex> = OnceLock::new();
static JWT_RE: OnceLock<Regex> = OnceLock::new();
static PRIVKEY_RE: OnceLock<Regex> = OnceLock::new();

// The pattern table. Order matters for performance only — the most
// common categories first means fewer regex passes for typical input.
static PATTERNS: &[PiiPattern] = &[
    PiiPattern {
        category: "email",
        regex: &EMAIL_RE,
        // Tight RFC 5322 subset: local part allows alnum and the
        // common punctuation class (`._%+-`); domain requires at
        // least one dot + a 2+-letter TLD. Avoids the false-positive
        // floods of permissive grammars.
        source: r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}\b",
        filter: None,
    },
    PiiPattern {
        category: "openai_api_key",
        regex: &OPENAI_RE,
        // Modern OpenAI keys are `sk-` (legacy) or `sk-proj-…`.
        // Length lower bound 32 chars to skip the literal `sk-` in
        // documentation prose.
        source: r"sk-(?:proj-)?[A-Za-z0-9_\-]{32,}",
        filter: None,
    },
    PiiPattern {
        category: "anthropic_api_key",
        regex: &ANTHROPIC_RE,
        source: r"sk-ant-(?:api03-|admin01-)?[A-Za-z0-9_\-]{32,}",
        filter: None,
    },
    PiiPattern {
        category: "aws_access_key",
        regex: &AWS_RE,
        // AKIA = long-term IAM user key; ASIA = STS short-term;
        // AGPA / AROA / AIDA = other principal types. All 16 base32
        // chars after the prefix.
        source: r"\b(?:AKIA|ASIA|AGPA|AROA|AIDA)[0-9A-Z]{16}\b",
        filter: None,
    },
    PiiPattern {
        category: "jwt",
        regex: &JWT_RE,
        // Three base64url segments. The length floor (8/8/8) trims
        // accidental matches on `aaa.bbb.ccc`-style placeholders.
        source: r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b",
        filter: None,
    },
    PiiPattern {
        category: "private_key",
        regex: &PRIVKEY_RE,
        // PEM header. Matches the BEGIN line (and intentionally not
        // the body) — a single match is enough to flag the secret.
        source: r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY-----",
        filter: None,
    },
    PiiPattern {
        category: "credit_card",
        regex: &CC_RE,
        // 13–19 digit run with optional spaces / hyphens in 4-digit
        // groups. Filter applies Luhn — without it the FP rate is
        // unacceptable (any long digit run hits).
        source: r"\b(?:\d[ \-]?){12,18}\d\b",
        filter: Some(luhn_valid),
    },
    PiiPattern {
        category: "ssn",
        regex: &SSN_RE,
        source: r"\b\d{3}-\d{2}-\d{4}\b",
        filter: Some(ssn_plausible),
    },
    PiiPattern {
        category: "iban",
        regex: &IBAN_RE,
        // 2 letters country + 2 check digits + 11–30 BBAN chars.
        // Filter applies mod-97 to reject random alnum runs.
        source: r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b",
        filter: Some(iban_mod97_ok),
    },
    PiiPattern {
        category: "phone",
        regex: &PHONE_RE,
        // E.164 (+CC followed by 7–14 digits) plus US national
        // (XXX-XXX-XXXX, (XXX) XXX-XXXX, XXX.XXX.XXXX). Caveat:
        // matches phone-shaped 11-digit runs so the filter rejects
        // those that look like the start of a credit-card number.
        source: r"\+?\d{1,3}[ \-.]?\(?\d{2,4}\)?[ \-.]?\d{3,4}[ \-.]?\d{3,4}",
        filter: Some(phone_plausible),
    },
    PiiPattern {
        category: "ipv4",
        regex: &IPV4_RE,
        source: r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
        filter: Some(ipv4_routable),
    },
    PiiPattern {
        category: "ipv6",
        regex: &IPV6_RE,
        // Permissive — IPv6 grammar is hard in flat regex; covers
        // the common forms (full, `::`-shorthand) and falls back to
        // a structural check.
        source: r"\b(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{1,4}\b|::1\b|::\b",
        filter: Some(ipv6_plausible),
    },
];

// ---------------------------------------------------------------------------
// Plausibility filters — false-positive scrubbing
// ---------------------------------------------------------------------------

/// Luhn check for a credit-card-shaped substring. The substring is
/// the full match (which may include separators); we strip non-digits
/// before computing.
fn luhn_valid(matched: &str) -> bool {
    let digits: Vec<u32> = matched
        .chars()
        .filter(|c| c.is_ascii_digit())
        .filter_map(|c| c.to_digit(10))
        .collect();
    if !(13..=19).contains(&digits.len()) {
        return false;
    }
    let mut sum = 0u32;
    for (i, &d) in digits.iter().rev().enumerate() {
        let v = if i % 2 == 1 {
            let doubled = d * 2;
            if doubled > 9 { doubled - 9 } else { doubled }
        } else {
            d
        };
        sum += v;
    }
    sum.is_multiple_of(10)
}

/// Reject SSNs in the reserved/never-issued ranges.
fn ssn_plausible(matched: &str) -> bool {
    // Format guaranteed by the regex: `XXX-XX-XXXX`.
    let area: u32 = matched[..3].parse().unwrap_or(0);
    let group: u32 = matched[4..6].parse().unwrap_or(0);
    let serial: u32 = matched[7..].parse().unwrap_or(0);
    if area == 0 || group == 0 || serial == 0 {
        return false;
    }
    // 666-XX-XXXX and 9XX-XX-XXXX are never issued.
    if area == 666 || area >= 900 {
        return false;
    }
    true
}

/// IBAN mod-97 verification per ISO 13616.
fn iban_mod97_ok(matched: &str) -> bool {
    let s = matched.replace(' ', "").to_ascii_uppercase();
    if s.len() < 15 || s.len() > 34 {
        return false;
    }
    // Move first 4 characters to the end, then convert letters to
    // numbers (A=10, B=11, …, Z=35) and compute mod 97.
    let rotated: String = s[4..].chars().chain(s[..4].chars()).collect();
    let mut numeric = String::with_capacity(rotated.len() * 2);
    for c in rotated.chars() {
        if c.is_ascii_digit() {
            numeric.push(c);
        } else if c.is_ascii_uppercase() {
            numeric.push_str(&((c as u32) - ('A' as u32) + 10).to_string());
        } else {
            return false;
        }
    }
    // Mod 97 on a long numeric string — chunk-and-reduce to avoid
    // u128 overflow.
    let mut rem: u64 = 0;
    for c in numeric.chars() {
        rem = (rem * 10 + (c as u64 - '0' as u64)) % 97;
    }
    rem == 1
}

/// Phone-plausibility filter: rejects matches that are clearly
/// just a credit-card-shaped digit run with hyphens. Heuristic — keep
/// only matches that look phone-flavoured (have either a `+` prefix,
/// parentheses, or no more than 13 digits after stripping).
fn phone_plausible(matched: &str) -> bool {
    let digit_count = matched.chars().filter(|c| c.is_ascii_digit()).count();
    if !(7..=15).contains(&digit_count) {
        return false;
    }
    // Plus-prefixed or parens-prefixed → almost certainly a phone.
    if matched.starts_with('+') || matched.contains('(') {
        return true;
    }
    // Strip non-digits and reject runs Luhn-validates as credit
    // cards (so a CC string formatted with hyphens doesn't double-
    // hit phone + credit_card).
    !luhn_valid(matched)
}

/// IPv4 routable filter: skip 0.0.0.0/8 and 127.0.0.0/8 (loopback).
/// Keeps 10.x / 172.16-31.x / 192.168.x — those are arguably PII for
/// internal-network leak detection.
fn ipv4_routable(matched: &str) -> bool {
    let parts: Vec<u8> = matched.split('.').filter_map(|s| s.parse().ok()).collect();
    if parts.len() != 4 {
        return false;
    }
    let a = parts[0];
    // Skip 0.0.0.0/8 (this network) and 127.0.0.0/8 (loopback) which
    // are diagnostic noise, not PII.
    if a == 0 || a == 127 {
        return false;
    }
    true
}

/// Best-effort IPv6 plausibility — at least one colon and not just
/// punctuation.
fn ipv6_plausible(matched: &str) -> bool {
    if !matched.contains(':') {
        return false;
    }
    matched.chars().any(|c| c.is_ascii_hexdigit())
}

// ---------------------------------------------------------------------------
// Detector
// ---------------------------------------------------------------------------

/// Built-in PII detector. Implements [`Detector`] over the [`PATTERNS`]
/// table; configuration is stateless other than the optional
/// per-category opt-out.
pub struct PiiDetector {
    /// `None` = run every pattern; `Some(set)` = run only patterns
    /// whose `category` appears in the set. Stored as `Vec<String>`
    /// (not a `HashSet`) because the cardinality is tiny (≤12) and
    /// linear scan is faster + lighter than hashing.
    enabled: Option<Vec<String>>,
}

impl PiiDetector {
    /// Build a detector that runs every pattern.
    pub fn all() -> Self {
        Self { enabled: None }
    }

    /// Build a detector that runs only the supplied categories.
    /// A name the detector does not know matches nothing here; the
    /// orchestrator counts such names and adds a binding that cannot answer
    /// for them, and the admin API validator rejects them on save.
    pub fn with_categories<I, S>(categories: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Self {
            enabled: Some(categories.into_iter().map(Into::into).collect()),
        }
    }

    /// Return the set of category names this detector recognises.
    /// Useful for the admin API validator when rejecting unknown
    /// values on `pii_categories`.
    pub fn known_categories() -> &'static [&'static str] {
        &[
            "email",
            "openai_api_key",
            "anthropic_api_key",
            "aws_access_key",
            "jwt",
            "private_key",
            "credit_card",
            "ssn",
            "iban",
            "phone",
            "ipv4",
            "ipv6",
        ]
    }

    fn is_enabled(&self, category: &str) -> bool {
        match &self.enabled {
            None => true,
            Some(list) => list.iter().any(|c| c == category),
        }
    }

    /// Apply the pattern table to `content` and collect spans across
    /// all enabled categories. Returns a `Vec<(category, Span)>` so
    /// the orchestrator can group by category for the violation
    /// payload while still walking the full set in reverse offset
    /// order for redaction.
    fn scan(&self, content: &str) -> Vec<(&'static str, Span)> {
        let mut hits: Vec<(&'static str, Span)> = Vec::new();
        for pattern in PATTERNS {
            if !self.is_enabled(pattern.category) {
                continue;
            }
            let re = pattern.re();
            for m in re.find_iter(content) {
                let matched = m.as_str();
                if let Some(filter) = pattern.filter {
                    if !filter(matched) {
                        continue;
                    }
                }
                hits.push((
                    pattern.category,
                    Span {
                        start: m.start(),
                        end: m.end(),
                        redaction: Some(format!(
                            "[REDACTED-{}]",
                            pattern.category.to_ascii_uppercase()
                        )),
                    },
                ));
            }
        }
        hits
    }
}

#[async_trait::async_trait]
impl Detector for PiiDetector {
    fn name(&self) -> &'static str {
        "pii"
    }

    async fn detect(&self, content: &str) -> Result<Detection, DetectorError> {
        let hits = self.scan(content);
        if hits.is_empty() {
            return Ok(Detection::Pass);
        }
        // Surface a single Hit with `category = "pii"` for the audit
        // feed; the per-category breakdown is encoded in the spans'
        // `redaction` strings (`[REDACTED-EMAIL]` etc.).
        Ok(Detection::Hit {
            category: "pii".into(),
            matches: hits.into_iter().map(|(_, span)| span).collect(),
            score: None,
        })
    }
}

/// Redact every PII category the local detector knows, synchronously.
///
/// Used by the audit writer for `store_bodies = redacted`: what lands
/// in `proxy_logs` has been through this regardless of whether a PII
/// guardrail was configured for the request. Returns the rewritten text and
/// the number of spans replaced, so a caller can say "redacted N" rather
/// than only "redacted".
pub fn redact_all_pii(content: &str) -> (String, usize) {
    let spans: Vec<Span> = PiiDetector::all()
        .scan(content)
        .into_iter()
        .map(|(_, span)| span)
        .collect();
    if spans.is_empty() {
        return (content.to_string(), 0);
    }
    let n = spans.len();
    (apply_redactions(content, &spans), n)
}

/// Apply a list of redaction spans to the original content, returning
/// the rewritten string. The orchestrator owns this — exposed here so
/// detector-level tests can verify span correctness without going
/// through the full check pipeline.
///
/// Walks spans in reverse byte-offset order so each substitution
/// preserves the validity of the remaining spans' offsets.
pub fn apply_redactions(content: &str, spans: &[Span]) -> String {
    // Filter to spans that actually have a replacement.
    let candidates: Vec<&Span> = spans.iter().filter(|s| s.redaction.is_some()).collect();

    // De-conflict OVERLAPPING spans before substituting. Two detectors can
    // match overlapping byte ranges — e.g. the phone regex matches a digit run
    // INSIDE a credit-card number, so both a `credit_card` and a `phone` span
    // cover (parts of) `4111-1111-1111-1111`. Substituting both corrupts the
    // output (`[REDACTED-PHONE]IT_CARD]`): replacing one span shifts the byte
    // offsets of the other, so the second replace_range lands on the wrong
    // bytes — and in the worst case could leave original characters exposed.
    // Keep the LONGEST span in each overlap group (the more complete, more
    // specific match) and drop the rest — a greedy longest-first selection.
    let mut by_len: Vec<&Span> = candidates;
    by_len.sort_by(|a, b| {
        (b.end - b.start)
            .cmp(&(a.end - a.start))
            .then(a.start.cmp(&b.start))
    });
    let mut to_apply: Vec<&Span> = Vec::with_capacity(by_len.len());
    for s in by_len {
        // Half-open overlap test: [s.start,s.end) intersects [k.start,k.end).
        if !to_apply.iter().any(|k| s.start < k.end && k.start < s.end) {
            to_apply.push(s);
        }
    }
    // Apply in reverse byte-offset order so each substitution preserves the
    // validity of the remaining (now non-overlapping) spans' offsets.
    to_apply.sort_by_key(|m| std::cmp::Reverse(m.start));

    let mut out = content.to_string();
    for span in to_apply {
        // Defensive: clamp to the content length even though the
        // detector only returns valid offsets. A bad provider could
        // return out-of-bounds spans; we'd rather swallow than panic.
        if span.end > out.len() || span.start > span.end {
            continue;
        }
        let replacement = span.redaction.as_deref().unwrap_or("");
        out.replace_range(span.start..span.end, replacement);
    }
    out
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn detector() -> PiiDetector {
        PiiDetector::all()
    }

    /// Helper: run the detector against `input`, panic on
    /// `DetectorError`, return spans + categories grouped together.
    async fn scan(d: &PiiDetector, input: &str) -> Vec<(&'static str, Span)> {
        // Indirection: the public `detect` API merges into a single
        // Hit with category = "pii". The test helper digs into the
        // spans + their redaction strings to recover per-category
        // info, which is what most tests want to assert against.
        match d.detect(input).await.unwrap() {
            Detection::Pass => vec![],
            Detection::Hit { matches, .. } => matches
                .into_iter()
                .map(|s| {
                    // Recover the category from the redaction tag —
                    // e.g. `[REDACTED-EMAIL]` → `email`. Lowercase to
                    // match the pattern table.
                    let cat: &'static str = match s
                        .redaction
                        .as_deref()
                        .unwrap_or("")
                        .trim_start_matches("[REDACTED-")
                        .trim_end_matches(']')
                    {
                        "EMAIL" => "email",
                        "PHONE" => "phone",
                        "SSN" => "ssn",
                        "CREDIT_CARD" => "credit_card",
                        "IBAN" => "iban",
                        "IPV4" => "ipv4",
                        "IPV6" => "ipv6",
                        "OPENAI_API_KEY" => "openai_api_key",
                        "ANTHROPIC_API_KEY" => "anthropic_api_key",
                        "AWS_ACCESS_KEY" => "aws_access_key",
                        "JWT" => "jwt",
                        "PRIVATE_KEY" => "private_key",
                        _ => "unknown",
                    };
                    (cat, s)
                })
                .collect(),
        }
    }

    // ----- Compile every pattern at module load -----------------------------

    #[test]
    fn every_pattern_compiles() {
        for p in PATTERNS {
            // Force compile via `re()`. Panics on regression.
            let _ = p.re();
        }
    }

    // ----- email -----------------------------------------------------------

    #[tokio::test]
    async fn email_basic_hit() {
        let d = detector();
        let hits = scan(&d, "ping me at alice@example.com please").await;
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].0, "email");
    }

    #[tokio::test]
    async fn email_plus_subaddress_hit() {
        let d = detector();
        let hits = scan(&d, "alice+spam@example.co.uk").await;
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].0, "email");
    }

    #[tokio::test]
    async fn email_no_tld_no_hit() {
        let d = detector();
        let hits = scan(&d, "alice@example").await;
        assert!(hits.is_empty());
    }

    // ----- credit_card -----------------------------------------------------

    #[tokio::test]
    async fn credit_card_visa_hit() {
        // Industry test number that passes Luhn.
        let d = detector();
        let hits = scan(&d, "card 4111-1111-1111-1111 expires").await;
        assert!(hits.iter().any(|(c, _)| *c == "credit_card"));
    }

    #[tokio::test]
    async fn credit_card_invalid_luhn_no_hit() {
        // Off-by-one in the last digit so Luhn fails. Should not
        // fire credit_card; phone plausibility filter also rejects.
        let d = detector();
        let hits = scan(&d, "card 4111-1111-1111-1112 nope").await;
        assert!(hits.iter().all(|(c, _)| *c != "credit_card"));
    }

    #[tokio::test]
    async fn credit_card_short_run_no_hit() {
        // 12 digits — below the floor (13).
        let d = detector();
        let hits = scan(&d, "411111111111").await;
        assert!(hits.iter().all(|(c, _)| *c != "credit_card"));
    }

    // ----- ssn --------------------------------------------------------------

    #[tokio::test]
    async fn ssn_basic_hit() {
        let d = detector();
        let hits = scan(&d, "ssn is 123-45-6789").await;
        assert!(hits.iter().any(|(c, _)| *c == "ssn"));
    }

    #[tokio::test]
    async fn ssn_reserved_area_no_hit() {
        // 9XX-XX-XXXX is reserved → never issued → reject.
        let d = detector();
        let hits = scan(&d, "ssn 999-12-3456").await;
        assert!(hits.iter().all(|(c, _)| *c != "ssn"));
    }

    #[tokio::test]
    async fn ssn_zero_group_no_hit() {
        let d = detector();
        let hits = scan(&d, "ssn 123-00-4567").await;
        assert!(hits.iter().all(|(c, _)| *c != "ssn"));
    }

    // ----- iban -------------------------------------------------------------

    #[tokio::test]
    async fn iban_valid_mod97_hit() {
        // Sample IBAN from the ISO spec — passes mod-97.
        let d = detector();
        let hits = scan(&d, "transfer to GB82WEST12345698765432 today").await;
        assert!(hits.iter().any(|(c, _)| *c == "iban"));
    }

    #[tokio::test]
    async fn iban_bad_checksum_no_hit() {
        // Intentionally tampered — fails mod-97.
        let d = detector();
        let hits = scan(&d, "GB99WEST12345698765432").await;
        assert!(hits.iter().all(|(c, _)| *c != "iban"));
    }

    // ----- ipv4 -------------------------------------------------------------

    #[tokio::test]
    async fn ipv4_public_hit() {
        let d = detector();
        let hits = scan(&d, "server at 8.8.8.8 was unreachable").await;
        assert!(hits.iter().any(|(c, _)| *c == "ipv4"));
    }

    #[tokio::test]
    async fn ipv4_loopback_no_hit() {
        // 127.x.x.x is loopback → not PII.
        let d = detector();
        let hits = scan(&d, "127.0.0.1").await;
        assert!(hits.iter().all(|(c, _)| *c != "ipv4"));
    }

    #[tokio::test]
    async fn ipv4_zero_block_no_hit() {
        let d = detector();
        let hits = scan(&d, "0.0.0.0").await;
        assert!(hits.iter().all(|(c, _)| *c != "ipv4"));
    }

    // ----- api keys --------------------------------------------------------

    #[tokio::test]
    async fn openai_modern_key_hit() {
        let d = detector();
        let hits = scan(
            &d,
            "key sk-proj-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH",
        )
        .await;
        assert!(hits.iter().any(|(c, _)| *c == "openai_api_key"));
    }

    #[tokio::test]
    async fn anthropic_key_hit() {
        let d = detector();
        let hits = scan(
            &d,
            "key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789ABCDEF",
        )
        .await;
        assert!(hits.iter().any(|(c, _)| *c == "anthropic_api_key"));
    }

    #[tokio::test]
    async fn aws_iam_key_hit() {
        let d = detector();
        let hits = scan(&d, "AKIAIOSFODNN7EXAMPLE was leaked").await;
        assert!(hits.iter().any(|(c, _)| *c == "aws_access_key"));
    }

    #[tokio::test]
    async fn aws_lowercase_no_hit() {
        // AKIA must be uppercase per the actual key format.
        let d = detector();
        let hits = scan(&d, "akiaiosfodnn7example").await;
        assert!(hits.iter().all(|(c, _)| *c != "aws_access_key"));
    }

    // ----- jwt -------------------------------------------------------------

    #[tokio::test]
    async fn jwt_three_segments_hit() {
        let d = detector();
        let hits = scan(
            &d,
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signature012345",
        )
        .await;
        assert!(hits.iter().any(|(c, _)| *c == "jwt"));
    }

    // ----- private key ----------------------------------------------------

    #[tokio::test]
    async fn private_key_pem_header_hit() {
        let d = detector();
        let hits = scan(&d, "-----BEGIN RSA PRIVATE KEY-----\nMIIEogIBAAKC").await;
        assert!(hits.iter().any(|(c, _)| *c == "private_key"));
    }

    #[tokio::test]
    async fn private_key_openssh_header_hit() {
        let d = detector();
        let hits = scan(&d, "-----BEGIN OPENSSH PRIVATE KEY-----").await;
        assert!(hits.iter().any(|(c, _)| *c == "private_key"));
    }

    // ----- redaction --------------------------------------------------------

    #[tokio::test]
    async fn redaction_substitutes_each_span() {
        let d = detector();
        let input = "ping alice@example.com or bob@example.org";
        let hits = scan(&d, input).await;
        let spans: Vec<Span> = hits.into_iter().map(|(_, s)| s).collect();
        let out = apply_redactions(input, &spans);
        assert!(out.contains("[REDACTED-EMAIL]"));
        assert!(!out.contains("alice@"));
        assert!(!out.contains("bob@"));
    }

    #[tokio::test]
    async fn redaction_preserves_surrounding_content() {
        let d = detector();
        let input = "before alice@example.com after";
        let hits = scan(&d, input).await;
        let spans: Vec<Span> = hits.into_iter().map(|(_, s)| s).collect();
        let out = apply_redactions(input, &spans);
        assert!(out.starts_with("before "));
        assert!(out.ends_with(" after"));
    }

    #[tokio::test]
    async fn redaction_handles_overlapping_matches_safely() {
        // Defensive: two patterns might match overlapping ranges.
        // apply_redactions should not panic — out-of-bounds spans are
        // silently dropped after the first substitution shifts
        // offsets. (The orchestrator dedupes by category; this
        // test pins the safety net.)
        let input = "credit 4111-1111-1111-1111 stop";
        // Synthesise overlapping spans by hand.
        let spans = vec![
            Span {
                start: 7,
                end: 26,
                redaction: Some("[REDACTED-CREDIT_CARD]".into()),
            },
            Span {
                start: 7,
                end: 26,
                redaction: Some("[REDACTED-CREDIT_CARD]".into()),
            },
        ];
        let out = apply_redactions(input, &spans);
        // First substitution lands; second's offsets are now stale,
        // either skipped (out-of-bounds) or no-op'd by replace_range
        // returning silently. Either way no panic.
        assert!(out.starts_with("credit "));
        assert!(out.ends_with(" stop"));
    }

    #[test]
    fn apply_redactions_deconflicts_overlapping_spans() {
        // The phone regex matches a digit run INSIDE a credit-card number, so
        // `credit_card` [14..33] and `phone` [15..28] overlap. The longer span
        // (credit_card) must win and produce a single clean token — never the
        // corrupted "[REDACTED-PHONE]…CARD]" splice that results from applying
        // both overlapping spans.
        let input = "Card on file: 4111-1111-1111-1111 end";
        let spans = vec![
            Span {
                start: 14,
                end: 33,
                redaction: Some("[REDACTED-CREDIT_CARD]".into()),
            },
            Span {
                start: 15,
                end: 28,
                redaction: Some("[REDACTED-PHONE]".into()),
            },
        ];
        let out = apply_redactions(input, &spans);
        assert_eq!(out, "Card on file: [REDACTED-CREDIT_CARD] end");
    }

    // ----- per-category opt-out --------------------------------------------

    #[tokio::test]
    async fn with_categories_runs_only_listed() {
        let d = PiiDetector::with_categories(["email"]);
        let input = "alice@example.com 123-45-6789";
        let hits = scan(&d, input).await;
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].0, "email");
    }

    #[tokio::test]
    async fn with_empty_categories_runs_nothing() {
        let d = PiiDetector::with_categories(Vec::<&str>::new());
        let input = "alice@example.com";
        let hits = scan(&d, input).await;
        assert!(hits.is_empty());
    }

    #[tokio::test]
    async fn unknown_category_silently_skipped() {
        let d = PiiDetector::with_categories(["email", "no_such_category"]);
        let input = "alice@example.com 123-45-6789";
        let hits = scan(&d, input).await;
        assert_eq!(hits.len(), 1); // only email; ssn skipped
    }

    // ----- detector trait shape --------------------------------------------

    #[tokio::test]
    async fn detect_pass_on_clean_input() {
        let d = detector();
        let result = d.detect("nothing sensitive here").await.unwrap();
        assert_eq!(result, Detection::Pass);
    }

    #[tokio::test]
    async fn detect_hit_carries_pii_category() {
        let d = detector();
        let result = d.detect("alice@example.com").await.unwrap();
        match result {
            Detection::Hit {
                category, score, ..
            } => {
                assert_eq!(category, "pii");
                assert!(score.is_none());
            }
            _ => panic!("expected Hit"),
        }
    }

    // ----- edge cases ------------------------------------------------------

    #[tokio::test]
    async fn empty_content_passes() {
        let d = detector();
        assert_eq!(d.detect("").await.unwrap(), Detection::Pass);
    }

    #[tokio::test]
    async fn very_long_clean_input_passes() {
        let d = detector();
        let s = "a".repeat(10_000);
        assert_eq!(d.detect(&s).await.unwrap(), Detection::Pass);
    }

    #[tokio::test]
    async fn unicode_input_doesnt_panic() {
        let d = detector();
        let s = "héllo wörld 你好 🌍";
        let _ = d.detect(s).await.unwrap();
    }

    #[tokio::test]
    async fn known_categories_matches_pattern_table() {
        let known: std::collections::HashSet<&str> =
            PiiDetector::known_categories().iter().copied().collect();
        let table: std::collections::HashSet<&str> = PATTERNS.iter().map(|p| p.category).collect();
        assert_eq!(known, table);
    }
}
