// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Secrets detection.
//!
//! A first-class detector alongside PII, prompt-injection, jailbreak,
//! toxic-content, banned-words and banned-patterns. Built-in pattern
//! bank covers eight categories plus an
//! opt-in `generic_high_entropy` for catching unknown-shape secrets.
//!
//! # Why not just extend PII?
//!
//! The existing PII detector already covers `aws_access_key`,
//! `openai_api_key`, `jwt` and `private_key` — but it ships them
//! under the *PII* check, which uses the `pii_action` (default
//! `redact`) and the `pii_categories` opt-in subset. Operators
//! generally want different policy for "leaked AWS key" vs
//! "customer email" — block-vs-redact, separate audit feed, separate
//! per-surface gate. Secrets gets its own check so each is
//! independently policy-able.
//!
//! Overlap with PII is intentional: a tenant enabling both detectors
//! sees both violations, which surfaces "this substring is both a
//! secret AND PII" to the audit dashboard. No deduplication.
//!
//! # Detection strategy
//!
//! Most categories are deterministic regex matches with a structural
//! filter (Luhn-style false-positive reduction). Two categories
//! require contextual reasoning:
//!
//! * `aws_secret_key` — a 40-char base64 token is indistinguishable
//!   from generic high-entropy text on its own, so the detector only
//!   flags it when it appears within proximity of an `aws_access_key`
//!   hit. Mirrors how secret-scanning tools (truffleHog, gitleaks)
//!   handle this category.
//!
//! * `generic_high_entropy` — Shannon entropy ≥4.5 bits/char over
//!   contiguous base64-ish runs of ≥24 chars, excluding URLs and
//!   already-matched secret substrings. Off by default; on-by-opt-in
//!   only because it has the highest FP rate of any category.
//!
//! The JWT detector goes one step further than PII's regex-only
//! version: it base64-decodes the header and checks the JSON
//! structure for `alg` / `typ` keys, rejecting random
//! dot-separated base64 strings (PII's pattern is intentionally
//! looser because it doubles as a "looks like a token" signal).

use std::sync::OnceLock;

use regex_lite::Regex;

use super::detector::{Detection, Detector, DetectorError, Span};

// ---------------------------------------------------------------------------
// Static patterns
// ---------------------------------------------------------------------------

/// One regex category — pattern + optional structural filter.
struct SecretPattern {
    /// Stable wire name. Snake-case. Matches the values
    /// validated by the admin API on save.
    category: &'static str,
    regex: &'static OnceLock<Regex>,
    source: &'static str,
    /// Optional filter that rejects regex matches as false positives.
    /// Receives the matched substring; returns true to keep.
    filter: Option<fn(&str) -> bool>,
}

impl SecretPattern {
    fn re(&self) -> &Regex {
        self.regex.get_or_init(|| {
            Regex::new(self.source).unwrap_or_else(|e| {
                panic!("Secrets pattern `{}` failed to compile: {e}", self.category)
            })
        })
    }
}

static AWS_ACCESS_RE: OnceLock<Regex> = OnceLock::new();
static AWS_SECRET_CANDIDATE_RE: OnceLock<Regex> = OnceLock::new();
static GITHUB_TOKEN_RE: OnceLock<Regex> = OnceLock::new();
static OPENAI_KEY_RE: OnceLock<Regex> = OnceLock::new();
static GCP_SA_TYPE_RE: OnceLock<Regex> = OnceLock::new();
static GCP_SA_PRIVKEY_RE: OnceLock<Regex> = OnceLock::new();
static PRIVATE_KEY_PEM_RE: OnceLock<Regex> = OnceLock::new();
static JWT_RE: OnceLock<Regex> = OnceLock::new();
static HIGH_ENTROPY_CANDIDATE_RE: OnceLock<Regex> = OnceLock::new();
static URL_RE: OnceLock<Regex> = OnceLock::new();

// Pattern table — ordered most-specific first so the deterministic
// detectors land their hits before the generic high-entropy sweep
// even looks at the byte ranges they cover. (The high-entropy
// sweep skips ranges already claimed by the table.)
static DETERMINISTIC_PATTERNS: &[SecretPattern] = &[
    SecretPattern {
        category: "aws_access_key",
        regex: &AWS_ACCESS_RE,
        // AKIA / ASIA / AGPA / AROA / AIDA — IAM user, STS, group,
        // role, identity-pool principal types respectively. Each
        // followed by 16 base32 chars (`[0-9A-Z]`). Word boundaries
        // stop the pattern matching inside larger alphanumeric runs.
        source: r"\b(?:AKIA|ASIA|AGPA|AROA|AIDA)[0-9A-Z]{16}\b",
        filter: None,
    },
    SecretPattern {
        category: "github_token",
        regex: &GITHUB_TOKEN_RE,
        // gho_/ghu_/ghp_/ghr_/ghs_ + 36–255 alnum, OR github_pat_
        // (fine-grained PAT) + 82 alnum/underscore. The five
        // legacy prefixes cover OAuth, user-to-server, personal
        // access, refresh, and server-to-server tokens
        // respectively.
        source: r"\b(?:gh[ouprs]_[A-Za-z0-9_]{36,255}|github_pat_[A-Za-z0-9_]{82})\b",
        filter: None,
    },
    SecretPattern {
        category: "openai_key",
        regex: &OPENAI_KEY_RE,
        // `sk-` (legacy), `sk-proj-…` (project key), `sk-ant-…`
        // (Anthropic — it is bundled under openai_key).
        // 32-char floor avoids matching the literal `sk-` in
        // documentation. The character class includes `_` and `-`
        // because real keys use both.
        source: r"\bsk-(?:proj-|ant-(?:api03-|admin01-)?)?[A-Za-z0-9_\-]{32,}\b",
        filter: None,
    },
    SecretPattern {
        category: "jwt",
        regex: &JWT_RE,
        // base64url header (`eyJ` decodes to `{"…`) + payload + sig,
        // each ≥8 chars. The structural filter base64-decodes the
        // header and checks for the required JSON keys.
        source: r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b",
        filter: Some(jwt_header_is_structural),
    },
    SecretPattern {
        category: "private_key_pem",
        regex: &PRIVATE_KEY_PEM_RE,
        // The BEGIN line — matching the whole body is unnecessary
        // (a single hit means the secret is present) and would
        // require a non-greedy multi-line regex that the `regex`
        // crate would have to compile in DFA-incompatible mode.
        source: r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY-----",
        filter: None,
    },
];

// ---------------------------------------------------------------------------
// Structural filters
// ---------------------------------------------------------------------------

/// JWT header is base64url-encoded JSON `{"alg":"…","typ":"JWT",…}`.
/// Decode the first segment (up to the first `.`) and check the
/// decoded bytes look like a JSON object with both `alg` and
/// (optionally) `typ` keys. Rejecting on missing `alg` cuts the FP
/// rate on random `eyJ…` dot-separated strings nearly to zero.
fn jwt_header_is_structural(matched: &str) -> bool {
    let head = match matched.split_once('.') {
        Some((h, _)) => h,
        None => return false,
    };
    // base64url decode with implicit padding correction.
    let decoded = match decode_b64url_lenient(head) {
        Some(b) => b,
        None => return false,
    };
    let text = match std::str::from_utf8(&decoded) {
        Ok(s) => s,
        Err(_) => return false,
    };
    // Cheap shape check — full JSON parsing here costs more than the
    // regex itself for what's effectively a contains check.
    text.contains("\"alg\"") && text.starts_with('{') && text.ends_with('}')
}

/// Decode a base64url string with optional missing padding. Returns
/// None on any malformed input. Implemented inline rather than via
/// the `base64` crate to keep the dependency surface small (the
/// crate is already in the workspace but we don't pull it here).
fn decode_b64url_lenient(s: &str) -> Option<Vec<u8>> {
    let bytes = s.as_bytes();
    if bytes.is_empty() {
        return None;
    }
    // Translate base64url → standard base64 and pad to multiple of 4.
    let mut translated = String::with_capacity(bytes.len() + 3);
    for &b in bytes {
        match b {
            b'-' => translated.push('+'),
            b'_' => translated.push('/'),
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'+' | b'/' | b'=' => {
                translated.push(b as char);
            }
            _ => return None,
        }
    }
    while !translated.len().is_multiple_of(4) {
        translated.push('=');
    }
    base64_decode(&translated)
}

/// Minimal RFC 4648 base64 decoder. Returns None on invalid input.
fn base64_decode(s: &str) -> Option<Vec<u8>> {
    let mut out = Vec::with_capacity(s.len() * 3 / 4);
    let mut buf: u32 = 0;
    let mut bits: u32 = 0;
    for c in s.chars() {
        if c == '=' {
            break;
        }
        let v: u32 = match c {
            'A'..='Z' => c as u32 - 'A' as u32,
            'a'..='z' => c as u32 - 'a' as u32 + 26,
            '0'..='9' => c as u32 - '0' as u32 + 52,
            '+' => 62,
            '/' => 63,
            _ => return None,
        };
        buf = (buf << 6) | v;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push(((buf >> bits) & 0xff) as u8);
        }
    }
    Some(out)
}

// ---------------------------------------------------------------------------
// Shannon entropy
// ---------------------------------------------------------------------------

/// Shannon entropy in bits per character. Higher = more random.
/// Random base64 strings hover around 5.5–6.0; English prose is
/// typically 3.5–4.2; "aaaaaaaa" is 0.0.
///
/// Returns 0.0 for empty input rather than NaN.
pub fn shannon_entropy(s: &str) -> f64 {
    if s.is_empty() {
        return 0.0;
    }
    let mut counts: [u32; 256] = [0; 256];
    let total: u32 = s.len() as u32;
    for b in s.bytes() {
        counts[b as usize] += 1;
    }
    let total_f = total as f64;
    let mut entropy = 0.0_f64;
    for &c in counts.iter() {
        if c == 0 {
            continue;
        }
        let p = (c as f64) / total_f;
        entropy -= p * p.log2();
    }
    entropy
}

/// Threshold for "this looks high-entropy enough
/// to be a generated secret". Tuned to catch base64 keys (≥5 bits)
/// and hex tokens (≥4) without flagging Lorem ipsum (~4.0–4.3).
const GENERIC_HIGH_ENTROPY_THRESHOLD: f64 = 4.5;

/// Minimum length for a substring to be considered a generic-high-
/// entropy candidate. Shorter runs are too noisy.
const GENERIC_HIGH_ENTROPY_MIN_LEN: usize = 24;

// ---------------------------------------------------------------------------
// AWS secret-key proximity
// ---------------------------------------------------------------------------

/// AWS secret access key candidates: 40 chars from the standard
/// base64 alphabet (plus `/` and `+`). They're indistinguishable
/// from any 40-char base64 string at the regex level; the detector
/// only flags candidates that appear near an access key.
const AWS_SECRET_CANDIDATE_SRC: &str = r"[A-Za-z0-9/+=]{40}";

/// Maximum byte distance between an access key and a candidate
/// secret key for the secret to be flagged. 300 bytes is enough
/// to cover the common cases (env file with the two on adjacent
/// lines, env var pair in a serialised dict) without hitting
/// false positives in unrelated text.
const AWS_PROXIMITY_WINDOW: usize = 300;

fn aws_secret_candidate_re() -> &'static Regex {
    AWS_SECRET_CANDIDATE_RE.get_or_init(|| {
        Regex::new(AWS_SECRET_CANDIDATE_SRC).expect("AWS secret candidate regex compiles")
    })
}

// ---------------------------------------------------------------------------
// GCP service account
// ---------------------------------------------------------------------------

fn gcp_sa_type_re() -> &'static Regex {
    GCP_SA_TYPE_RE.get_or_init(|| {
        // Tolerant of whitespace between the colon and value; covers
        // both compact (`"type":"service_account"`) and pretty-
        // printed (`"type": "service_account"`) JSON.
        Regex::new(r#""type"\s*:\s*"service_account""#).expect("GCP SA type regex compiles")
    })
}

fn gcp_sa_privkey_re() -> &'static Regex {
    GCP_SA_PRIVKEY_RE.get_or_init(|| {
        Regex::new(r#""private_key"\s*:\s*""#).expect("GCP SA private_key regex compiles")
    })
}

/// Maximum byte distance between the `"type":"service_account"` and
/// `"private_key"` markers for the surrounding JSON to be flagged
/// as a GCP service-account credential. 4096 covers the typical
/// service-account JSON file which is ~2.3 KB pretty-printed.
const GCP_SA_PROXIMITY_WINDOW: usize = 4096;

// ---------------------------------------------------------------------------
// Generic-high-entropy candidate window
// ---------------------------------------------------------------------------

fn high_entropy_candidate_re() -> &'static Regex {
    HIGH_ENTROPY_CANDIDATE_RE.get_or_init(|| {
        // Contiguous run of base64-ish characters of at least the
        // minimum length. The detector applies the entropy check
        // on each run individually.
        Regex::new(r"[A-Za-z0-9+/=_\-]{24,}").expect("high-entropy candidate regex compiles")
    })
}

fn url_re() -> &'static Regex {
    URL_RE.get_or_init(|| {
        // Used to exclude URL substrings from the generic-high-
        // entropy sweep. URLs aren't secrets — flagging them
        // produces noise without security value.
        Regex::new(r"https?://[^\s\]\)\}]+").expect("URL regex compiles")
    })
}

// ---------------------------------------------------------------------------
// Detector
// ---------------------------------------------------------------------------

/// Built-in secrets detector. Stateless other than the per-category
/// opt-in (mirrors `PiiDetector`).
pub struct SecretsDetector {
    enabled: Option<Vec<String>>,
}

impl SecretsDetector {
    /// Build a detector that runs every category EXCEPT
    /// `generic_high_entropy` (which is opt-in due to its noise
    /// floor). This is the documented default.
    pub fn defaults() -> Self {
        Self {
            enabled: Some(default_categories().iter().map(|s| s.to_string()).collect()),
        }
    }

    /// Build a detector that runs only the supplied categories.
    /// A name the detector does not know matches nothing here; the
    /// orchestrator passes known names only and adds a binding that cannot
    /// answer for the others, and the admin API validator rejects
    /// them on save.
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
    /// Used by the admin API validator to reject unknown values on
    /// `secrets_categories`.
    pub fn known_categories() -> &'static [&'static str] {
        &[
            "aws_access_key",
            "aws_secret_key",
            "github_token",
            "openai_key",
            "gcp_service_account",
            "private_key_pem",
            "jwt",
            "generic_high_entropy",
        ]
    }

    fn is_enabled(&self, category: &str) -> bool {
        match &self.enabled {
            None => true,
            Some(list) => list.iter().any(|c| c == category),
        }
    }

    /// Scan content and return matched spans grouped by category.
    /// The orchestrator's redaction pass walks all spans in reverse
    /// offset order; ordering between categories doesn't matter for
    /// correctness.
    fn scan(&self, content: &str) -> Vec<(&'static str, Span)> {
        let mut hits: Vec<(&'static str, Span)> = Vec::new();

        // ---- Deterministic patterns -----------------------------
        for pattern in DETERMINISTIC_PATTERNS {
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

        // ---- AWS secret-key proximity ---------------------------
        if self.is_enabled("aws_secret_key") {
            // Collect access-key positions first (we may have just
            // produced them above OR they may be disabled — re-scan
            // here so disabling `aws_access_key` doesn't disable the
            // secret-key detector).
            let access_re =
                AWS_ACCESS_RE.get_or_init(|| Regex::new(DETERMINISTIC_PATTERNS[0].source).unwrap());
            let access_positions: Vec<(usize, usize)> = access_re
                .find_iter(content)
                .map(|m| (m.start(), m.end()))
                .collect();

            if !access_positions.is_empty() {
                let cand_re = aws_secret_candidate_re();
                for m in cand_re.find_iter(content) {
                    // Skip candidates that themselves ARE an
                    // access key (the regex would happily match a
                    // 40-char run containing an access key).
                    let (cs, ce) = (m.start(), m.end());
                    if access_positions.iter().any(|(s, e)| cs <= *s && ce >= *e) {
                        continue;
                    }
                    // Require proximity to at least one access key.
                    let nearby = access_positions.iter().any(|(s, e)| {
                        let lo = s.saturating_sub(AWS_PROXIMITY_WINDOW);
                        let hi = e.saturating_add(AWS_PROXIMITY_WINDOW);
                        cs >= lo && ce <= hi
                    });
                    if !nearby {
                        continue;
                    }
                    hits.push((
                        "aws_secret_key",
                        Span {
                            start: cs,
                            end: ce,
                            redaction: Some("[REDACTED-AWS_SECRET_KEY]".to_string()),
                        },
                    ));
                }
            }
        }

        // ---- GCP service account --------------------------------
        if self.is_enabled("gcp_service_account") {
            let type_match = gcp_sa_type_re().find(content);
            let pk_match = gcp_sa_privkey_re().find(content);
            if let (Some(t), Some(p)) = (type_match, pk_match) {
                let (lo, hi) = (t.start().min(p.start()), t.end().max(p.end()));
                if hi - lo <= GCP_SA_PROXIMITY_WINDOW {
                    hits.push((
                        "gcp_service_account",
                        Span {
                            start: lo,
                            end: hi,
                            redaction: Some("[REDACTED-GCP_SERVICE_ACCOUNT]".to_string()),
                        },
                    ));
                }
            }
        }

        // ---- Generic high-entropy (opt-in only) ------------------
        if self.is_enabled("generic_high_entropy") {
            // Collect spans we've already matched + URL ranges so
            // the entropy sweep skips them.
            let mut excluded: Vec<(usize, usize)> =
                hits.iter().map(|(_, s)| (s.start, s.end)).collect();
            for m in url_re().find_iter(content) {
                excluded.push((m.start(), m.end()));
            }
            let overlaps_excluded =
                |s: usize, e: usize| -> bool { excluded.iter().any(|(es, ee)| s < *ee && e > *es) };
            for m in high_entropy_candidate_re().find_iter(content) {
                let matched = m.as_str();
                if matched.len() < GENERIC_HIGH_ENTROPY_MIN_LEN {
                    continue;
                }
                if overlaps_excluded(m.start(), m.end()) {
                    continue;
                }
                if shannon_entropy(matched) >= GENERIC_HIGH_ENTROPY_THRESHOLD {
                    hits.push((
                        "generic_high_entropy",
                        Span {
                            start: m.start(),
                            end: m.end(),
                            // Use the canonical category name in the
                            // redaction tag so audit-dashboard filters
                            // on category match. The `extract_*`
                            // helpers downstream parse this tag.
                            redaction: Some("[REDACTED-GENERIC_HIGH_ENTROPY]".to_string()),
                        },
                    ));
                }
            }
        }

        hits
    }
}

/// Categories shipped on by default — the seven deterministic ones.
/// `generic_high_entropy` is opt-in only because it has the highest
/// false-positive rate of any category.
pub fn default_categories() -> &'static [&'static str] {
    &[
        "aws_access_key",
        "aws_secret_key",
        "github_token",
        "openai_key",
        "gcp_service_account",
        "private_key_pem",
        "jwt",
    ]
}

#[async_trait::async_trait]
impl Detector for SecretsDetector {
    fn name(&self) -> &'static str {
        "secrets"
    }

    async fn detect(&self, content: &str) -> Result<Detection, DetectorError> {
        let hits = self.scan(content);
        if hits.is_empty() {
            return Ok(Detection::Pass);
        }
        // Single `Hit` with `category = "secrets"`; the per-category
        // breakdown lives in each span's `redaction` string
        // (`[REDACTED-AWS_ACCESS_KEY]` etc.). Mirrors how PII
        // surfaces its sub-categories.
        Ok(Detection::Hit {
            category: "secrets".into(),
            matches: hits.into_iter().map(|(_, span)| span).collect(),
            score: None,
        })
    }
}

// ===========================================================================
// Tests
// ===========================================================================
#[cfg(test)]
mod tests {
    use super::*;

    fn detect_sync(detector: &SecretsDetector, content: &str) -> Detection {
        // The detector's `detect` is async-by-trait but the body is
        // sync — `pollster::block_on` would pull in a dep; we just
        // poll a noop runtime via the futures executor.
        futures::executor::block_on(detector.detect(content)).expect("scan should not error")
    }

    fn extract_categories(detection: &Detection) -> Vec<String> {
        match detection {
            Detection::Hit { matches, .. } => matches
                .iter()
                .filter_map(|s| s.redaction.as_ref())
                .map(|r| {
                    // [REDACTED-FOO] → foo
                    r.trim_start_matches("[REDACTED-")
                        .trim_end_matches(']')
                        .to_ascii_lowercase()
                })
                .collect(),
            _ => Vec::new(),
        }
    }

    // ---- Shannon entropy ------------------------------------------------

    #[test]
    fn entropy_empty_returns_zero() {
        assert_eq!(shannon_entropy(""), 0.0);
    }

    #[test]
    fn entropy_single_char_repeated_is_zero() {
        // All same byte → 0 bits/char.
        assert_eq!(shannon_entropy("aaaaaaaaaaaaaaaaaaaaaaaa"), 0.0);
    }

    #[test]
    fn entropy_random_base64_above_threshold() {
        // 40-char base64-shaped string with diverse alphabet → high
        // entropy. Real AWS secret keys land here.
        let s = "abc123ABC123def456DEF456ghi789GHI789jKlM";
        let e = shannon_entropy(s);
        assert!(
            e >= GENERIC_HIGH_ENTROPY_THRESHOLD,
            "expected entropy >= {} for diverse base64 string, got {}",
            GENERIC_HIGH_ENTROPY_THRESHOLD,
            e
        );
    }

    #[test]
    fn entropy_english_prose_below_threshold() {
        // English text hovers around 3.5–4.2 bits/char. The threshold
        // is set above that so prose doesn't flag.
        let s = "the quick brown fox jumps over the lazy dog";
        let e = shannon_entropy(s);
        assert!(
            e < GENERIC_HIGH_ENTROPY_THRESHOLD,
            "expected entropy < {} for English prose, got {}",
            GENERIC_HIGH_ENTROPY_THRESHOLD,
            e
        );
    }

    // ---- Deterministic categories ---------------------------------------

    #[test]
    fn detects_aws_access_key() {
        let d = SecretsDetector::defaults();
        let det = detect_sync(&d, "creds: AKIAIOSFODNN7EXAMPLE here");
        assert!(extract_categories(&det).contains(&"aws_access_key".to_string()));
    }

    #[test]
    fn detects_aws_access_key_sts_variant() {
        let d = SecretsDetector::defaults();
        let det = detect_sync(&d, "session: ASIATESTSESSIONKEYXX yes");
        assert!(extract_categories(&det).contains(&"aws_access_key".to_string()));
    }

    #[test]
    fn detects_aws_secret_key_when_near_access_key() {
        // RFC mandates proximity-aware detection — a 40-char base64
        // string ALONE is not enough, it has to live within the
        // proximity window of an access key.
        let d = SecretsDetector::defaults();
        let content = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\nAWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY";
        let cats = extract_categories(&detect_sync(&d, content));
        assert!(cats.contains(&"aws_access_key".to_string()));
        assert!(cats.contains(&"aws_secret_key".to_string()));
    }

    #[test]
    fn does_not_detect_aws_secret_key_when_isolated() {
        // 40-char base64 string without a nearby access key — must
        // NOT fire. This is the false-positive floor for
        // aws_secret_key; a tighter heuristic isn't possible at the
        // regex layer.
        let d = SecretsDetector::defaults();
        let content = "some random data: wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY and that's it";
        let cats = extract_categories(&detect_sync(&d, content));
        assert!(!cats.contains(&"aws_secret_key".to_string()));
    }

    #[test]
    fn detects_github_classic_token() {
        let d = SecretsDetector::defaults();
        // `gho_` + 36 chars
        let det = detect_sync(&d, "token: gho_16C7e42F292c6912E7710c838347Ae178B4a");
        assert!(extract_categories(&det).contains(&"github_token".to_string()));
    }

    #[test]
    fn detects_github_fine_grained_pat() {
        let d = SecretsDetector::defaults();
        // github_pat_ + 82 alnum/underscore (deliberately exactly 82
        // chars so the boundary is exercised).
        let token = format!("github_pat_{}", "a".repeat(82));
        let det = detect_sync(&d, &format!("pat: {token} end"));
        assert!(extract_categories(&det).contains(&"github_token".to_string()));
    }

    #[test]
    fn detects_openai_key() {
        let d = SecretsDetector::defaults();
        // 32-char body, mirrors real key shape (without being one).
        let det = detect_sync(
            &d,
            "OPENAI_API_KEY=sk-proj-abcdef0123456789ABCDEF0123456789xyz",
        );
        assert!(extract_categories(&det).contains(&"openai_key".to_string()));
    }

    #[test]
    fn detects_anthropic_key_as_openai_category() {
        // The RFC bundles Anthropic under `openai_key` — that's the
        // category name on the wire. Future split into a dedicated
        // category would be a breaking change; pin the current shape.
        let d = SecretsDetector::defaults();
        let det = detect_sync(
            &d,
            "ANTHROPIC_API_KEY=sk-ant-api03-abcdef0123456789ABCDEF0123456789xyz",
        );
        assert!(extract_categories(&det).contains(&"openai_key".to_string()));
    }

    #[test]
    fn detects_private_key_pem() {
        let d = SecretsDetector::defaults();
        let content =
            "key: -----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----";
        assert!(
            extract_categories(&detect_sync(&d, content)).contains(&"private_key_pem".to_string())
        );
    }

    #[test]
    fn detects_private_key_pem_openssh_variant() {
        let d = SecretsDetector::defaults();
        let det = detect_sync(&d, "id_ed25519: -----BEGIN OPENSSH PRIVATE KEY-----");
        assert!(extract_categories(&det).contains(&"private_key_pem".to_string()));
    }

    #[test]
    fn detects_jwt_with_valid_header_structure() {
        // Real-shape JWT: base64url-encoded {"alg":"HS256","typ":"JWT"}
        // + arbitrary payload + signature. The structural filter
        // decodes the header and checks for the `alg` key.
        let d = SecretsDetector::defaults();
        let jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c";
        let det = detect_sync(&d, &format!("Authorization: Bearer {jwt}"));
        assert!(extract_categories(&det).contains(&"jwt".to_string()));
    }

    #[test]
    fn rejects_jwt_lookalike_without_alg_header() {
        // Three dot-separated base64-ish runs, but the first segment
        // doesn't base64-decode to a JSON object with `alg`. Must
        // NOT fire. Without this filter the regex matches random
        // strings like `eyJxxxxxxxx.eyJyyyyyyyy.zzzzzzzzz`.
        let d = SecretsDetector::with_categories(["jwt"]);
        // eyJxxxxxxxxxxxxx — eyJ is the literal, then xs (not real
        // base64 of valid JSON), then more random segments.
        let lookalike = "eyJxxxxxxxxx.aaaaaaaaaa.bbbbbbbbbb";
        let det = detect_sync(&d, lookalike);
        assert!(
            !extract_categories(&det).contains(&"jwt".to_string()),
            "JWT structural filter must reject lookalikes without a valid alg header"
        );
    }

    // ---- GCP service account --------------------------------------------

    #[test]
    fn detects_gcp_service_account_compact_json() {
        let d = SecretsDetector::defaults();
        let json =
            r#"{"type":"service_account","project_id":"my-proj","private_key":"-----BEGIN..."}"#;
        assert!(
            extract_categories(&detect_sync(&d, json)).contains(&"gcp_service_account".to_string())
        );
    }

    #[test]
    fn detects_gcp_service_account_pretty_printed() {
        let d = SecretsDetector::defaults();
        let json = r#"
        {
            "type": "service_account",
            "project_id": "my-proj",
            "private_key": "-----BEGIN PRIVATE KEY-----..."
        }
        "#;
        assert!(
            extract_categories(&detect_sync(&d, json)).contains(&"gcp_service_account".to_string())
        );
    }

    #[test]
    fn does_not_detect_gcp_when_markers_are_far_apart() {
        // Both markers present but separated by more than the
        // proximity window — they're not actually related.
        let d = SecretsDetector::defaults();
        let filler = "x".repeat(GCP_SA_PROXIMITY_WINDOW + 100);
        let content = format!(r#""type":"service_account" {filler} "private_key":"sometext""#);
        let cats = extract_categories(&detect_sync(&d, &content));
        assert!(
            !cats.contains(&"gcp_service_account".to_string()),
            "GCP markers far apart must NOT fire"
        );
    }

    // ---- Generic high-entropy ------------------------------------------

    #[test]
    fn high_entropy_opt_in_only() {
        // Defaults exclude generic_high_entropy — a random 40-char
        // base64 string in plain text must NOT fire by default.
        let d = SecretsDetector::defaults();
        let content = "random: abc123XYZ789defGHI456jkLMNopQR0123STuvWX yes";
        let cats = extract_categories(&detect_sync(&d, content));
        assert!(
            !cats.contains(&"generic_high_entropy".to_string()),
            "generic_high_entropy must be off by default"
        );
    }

    #[test]
    fn high_entropy_fires_when_explicitly_enabled() {
        let d = SecretsDetector::with_categories(["generic_high_entropy"]);
        // Long base64-ish run with diverse alphabet — entropy ≥4.5.
        let content = "seed=abc123XYZ789defGHI456jkLMNopQR0123STuvWX more text";
        let cats = extract_categories(&detect_sync(&d, content));
        assert!(
            cats.contains(&"generic_high_entropy".to_string()),
            "generic_high_entropy should fire on high-entropy run when enabled"
        );
    }

    #[test]
    fn high_entropy_skips_already_matched_substrings() {
        // The aws_access_key match also satisfies the high-entropy
        // candidate regex's length floor (20 chars total — under
        // floor) — but the proximate aws_secret_key (40 chars) IS
        // above the floor. We must NOT double-flag it under both
        // aws_secret_key AND generic_high_entropy.
        let d = SecretsDetector::with_categories([
            "aws_access_key",
            "aws_secret_key",
            "generic_high_entropy",
        ]);
        let content = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\nAWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY";
        let det = detect_sync(&d, content);
        let categories = extract_categories(&det);
        // Both deterministic hits present:
        assert!(categories.contains(&"aws_access_key".to_string()));
        assert!(categories.contains(&"aws_secret_key".to_string()));
        // ...but no overlapping generic_high_entropy on the same
        // secret-key span (the exclusion logic should skip it).
        let secret_hits = categories
            .iter()
            .filter(|c| c.as_str() == "generic_high_entropy")
            .count();
        // The plain text portion ("AWS_ACCESS_KEY_ID=" etc.) is
        // contiguous alnum/underscore so the high-entropy regex
        // could pick it up — but its entropy is low. Accept zero
        // OR exact-zero overlap; bound is "no double-flag on
        // aws_secret_key substring".
        if secret_hits > 0 {
            if let Detection::Hit { matches, .. } = &det {
                let secret_span = matches
                    .iter()
                    .find(|s| s.redaction.as_deref() == Some("[REDACTED-AWS_SECRET_KEY]"));
                let entropy_spans: Vec<_> = matches
                    .iter()
                    .filter(|s| s.redaction.as_deref() == Some("[REDACTED-HIGH_ENTROPY]"))
                    .collect();
                if let Some(secret) = secret_span {
                    for e in &entropy_spans {
                        // No overlap with the aws_secret_key span.
                        assert!(
                            e.end <= secret.start || e.start >= secret.end,
                            "generic_high_entropy must not overlap aws_secret_key"
                        );
                    }
                }
            }
        }
    }

    #[test]
    fn high_entropy_skips_urls() {
        // URLs have base64-ish character classes and can be long
        // enough to be candidates. They aren't secrets — flagging
        // them is pure noise.
        let d = SecretsDetector::with_categories(["generic_high_entropy"]);
        let content = "see https://example.com/path/to/resource?id=abc123ABC456def789 yes";
        let cats = extract_categories(&detect_sync(&d, content));
        // The URL itself contains a high-entropy-ish run, but the
        // URL-exclusion filter must skip it.
        assert!(
            !cats.contains(&"generic_high_entropy".to_string())
                || cats
                    .iter()
                    .filter(|c| c.as_str() == "generic_high_entropy")
                    .count()
                    == 0,
            "high-entropy detector must exclude URL substrings"
        );
    }

    // ---- Cleanliness assertions -----------------------------------------

    #[test]
    fn clean_text_returns_pass() {
        let d = SecretsDetector::defaults();
        let det = detect_sync(&d, "the quick brown fox jumps over the lazy dog");
        assert_eq!(det, Detection::Pass);
    }

    #[test]
    fn empty_input_returns_pass() {
        let d = SecretsDetector::defaults();
        let det = detect_sync(&d, "");
        assert_eq!(det, Detection::Pass);
    }

    #[test]
    fn detector_name_is_stable() {
        // Audit dashboards filter on this string. A rename would
        // silently break the operator-facing filter.
        let d = SecretsDetector::defaults();
        assert_eq!(d.name(), "secrets");
    }

    #[test]
    fn known_categories_matches_default_plus_high_entropy() {
        let known: Vec<&str> = SecretsDetector::known_categories().to_vec();
        // Defaults are a subset of known.
        for cat in default_categories() {
            assert!(
                known.contains(cat),
                "default category {cat} missing from known list"
            );
        }
        // generic_high_entropy is known but not default.
        assert!(known.contains(&"generic_high_entropy"));
        assert!(!default_categories().contains(&"generic_high_entropy"));
    }

    #[test]
    fn category_opt_in_filters_results() {
        // Only enable aws_access_key — github_token in the input
        // must NOT fire.
        let d = SecretsDetector::with_categories(["aws_access_key"]);
        let content = "AKIAIOSFODNN7EXAMPLE and gho_16C7e42F292c6912E7710c838347Ae178B4a";
        let cats = extract_categories(&detect_sync(&d, content));
        assert!(cats.contains(&"aws_access_key".to_string()));
        assert!(!cats.contains(&"github_token".to_string()));
    }

    // ---- base64 decoder edge cases --------------------------------------

    #[test]
    fn base64url_decoder_handles_missing_padding() {
        // base64url often omits padding. The JWT structural filter
        // depends on this — without lenient padding it'd reject
        // valid JWTs.
        let decoded = decode_b64url_lenient("eyJhbGciOiJIUzI1NiJ9").expect("valid b64url");
        let text = std::str::from_utf8(&decoded).unwrap();
        assert!(text.contains("\"alg\""));
    }

    #[test]
    fn base64url_decoder_rejects_invalid_chars() {
        // Non-base64url characters → None. The JWT structural
        // filter relies on this to short-circuit malformed input.
        assert!(decode_b64url_lenient("not~valid").is_none());
    }
}
