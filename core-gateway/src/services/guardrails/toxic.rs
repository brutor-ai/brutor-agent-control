// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Toxic Content Detectors
//!
//! Two providers behind the [`Detector`] trait:
//!
//!   * [`OpenAiModerationDetector`] — POST `/v1/moderations`. The
//!     production default. Per-category thresholds (hate / harassment
//!     / sexual / self-harm / violence) so operators can tune
//!     aggressiveness per-category instead of one global threshold.
//!     Hardened with timeout + 1 retry on 5xx.
//!   * [`BuiltInProfanityDetector`] — defence-in-depth fallback when
//!     the OpenAI key isn't configured or its endpoint is unreachable.
//!     Covers a maintained list of slurs + obvious profanity. Cheap
//!     but coarse — not a substitute for the API.
//!
//! Both are wired on **input AND output** (the legacy `check_output`
//! was output-only). There is also toxic_content_input on the
//! orchestrator, gated by the same enable flag as input PII /
//! prompt-injection.

use std::time::Duration;

use reqwest::Client as HttpClient;
use serde_json::{Map as JsonMap, Value};

use super::detector::{Detection, Detector, DetectorError, Span};

// ---------------------------------------------------------------------------
// Shared category list
// ---------------------------------------------------------------------------

/// OpenAI Moderation categories the proxy understands. Mirrors what
/// the API returns under `results[0].category_scores`. Used by both
/// the OpenAI client and the admin API validator (which ensures
/// per-category threshold maps only contain known names).
pub const TOXIC_CATEGORIES: &[&str] = &[
    "hate",
    "hate/threatening",
    "harassment",
    "harassment/threatening",
    "self-harm",
    "self-harm/intent",
    "self-harm/instructions",
    "sexual",
    "sexual/minors",
    "violence",
    "violence/graphic",
];

// ===========================================================================
// OpenAI Moderation client
// ===========================================================================

const OPENAI_DEFAULT_ENDPOINT: &str = "https://api.openai.com/v1/moderations";
const OPENAI_DEFAULT_TIMEOUT_MS: u64 = 2_000;
const OPENAI_RETRY_BACKOFF_MS: u64 = 250;
const OPENAI_PROVIDER: &str = "openai_moderation";
/// Per-category threshold default when the policy doesn't override.
/// Calibrated against OpenAI's documented score curves: 0.8 catches
/// strong-signal content with minimal false positives.
pub const DEFAULT_CATEGORY_THRESHOLD: f64 = 0.8;

/// OpenAI Moderation client. Returns Hit when any enabled category's
/// score crosses its threshold.
#[derive(Debug)]
pub struct OpenAiModerationDetector {
    api_key: String,
    endpoint: String,
    timeout: Duration,
    /// Per-category threshold. Categories not in the map use
    /// [`DEFAULT_CATEGORY_THRESHOLD`]. Values outside [0.0, 1.0] are
    /// clamped at construction.
    thresholds: std::collections::HashMap<String, f64>,
    http: HttpClient,
}

impl OpenAiModerationDetector {
    /// Build a detector from a `providers.openai_moderation` config
    /// + an optional per-category threshold map.
    ///
    /// `provider_cfg` shape:
    /// ```json
    /// {
    ///   "enabled": true,
    ///   "api_key": "sk-...",
    ///   "endpoint": "https://api.openai.com/v1/moderations",
    ///   "timeout_ms": 2000
    /// }
    /// ```
    ///
    /// `thresholds` is read from the policy's
    /// `toxic_content_category_thresholds` JSONB column
    pub fn build(
        provider_cfg: &Value,
        thresholds: &Value,
        http: HttpClient,
    ) -> Result<Self, DetectorError> {
        crate::services::guardrails::detector::refuse_unreadable_secret(
            provider_cfg,
            "openai_moderation",
        )?;
        let api_key = provider_cfg
            .get("api_key")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or(DetectorError::MissingProviderConfig {
                provider: OPENAI_PROVIDER,
                field: "api_key",
            })?
            .to_string();

        let endpoint = provider_cfg
            .get("endpoint")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .unwrap_or(OPENAI_DEFAULT_ENDPOINT)
            .to_string();
        super::detector::check_endpoint("openai_moderation", &endpoint)?;

        let timeout_ms = provider_cfg
            .get("timeout_ms")
            .and_then(|v| v.as_u64())
            .unwrap_or(OPENAI_DEFAULT_TIMEOUT_MS);

        let mut threshold_map = std::collections::HashMap::new();
        if let Some(obj) = thresholds.as_object() {
            for (k, v) in obj {
                if let Some(f) = v.as_f64() {
                    threshold_map.insert(k.to_string(), f.clamp(0.0, 1.0));
                }
            }
        }

        Ok(Self {
            api_key,
            endpoint,
            timeout: Duration::from_millis(timeout_ms),
            thresholds: threshold_map,
            http,
        })
    }

    fn threshold_for(&self, category: &str) -> f64 {
        self.thresholds
            .get(category)
            .copied()
            .unwrap_or(DEFAULT_CATEGORY_THRESHOLD)
    }

    async fn call_once(&self, content: &str) -> Result<Verdict, DetectorError> {
        let resp = self
            .http
            .post(&self.endpoint)
            .header("Authorization", format!("Bearer {}", self.api_key))
            .json(&serde_json::json!({ "input": content }))
            .timeout(self.timeout)
            .send()
            .await
            .map_err(|e| {
                if e.is_timeout() {
                    DetectorError::Timeout {
                        provider: OPENAI_PROVIDER,
                    }
                } else {
                    DetectorError::Other {
                        provider: OPENAI_PROVIDER,
                        message: e.to_string(),
                    }
                }
            })?;

        let status = resp.status();
        if !status.is_success() {
            let body_excerpt = resp
                .text()
                .await
                .unwrap_or_default()
                .chars()
                .take(200)
                .collect();
            return Err(DetectorError::ProviderHttp {
                provider: OPENAI_PROVIDER,
                status: status.as_u16(),
                body_excerpt,
            });
        }

        let body: Value = resp
            .json()
            .await
            .map_err(|e| DetectorError::MalformedResponse {
                provider: OPENAI_PROVIDER,
                reason: format!("JSON decode failed: {e}"),
            })?;
        parse_verdict(&body)
    }
}

#[derive(Debug, Clone, PartialEq)]
struct Verdict {
    category_scores: JsonMap<String, Value>,
}

fn parse_verdict(body: &Value) -> Result<Verdict, DetectorError> {
    let first = body
        .get("results")
        .and_then(|r| r.as_array())
        .and_then(|a| a.first())
        .ok_or_else(|| DetectorError::MalformedResponse {
            provider: OPENAI_PROVIDER,
            reason: "missing results[0]".into(),
        })?;
    let scores = first
        .get("category_scores")
        .and_then(|s| s.as_object())
        .cloned()
        .ok_or_else(|| DetectorError::MalformedResponse {
            provider: OPENAI_PROVIDER,
            reason: "missing results[0].category_scores".into(),
        })?;
    Ok(Verdict {
        category_scores: scores,
    })
}

#[async_trait::async_trait]
impl Detector for OpenAiModerationDetector {
    fn name(&self) -> &'static str {
        "toxic_content"
    }

    async fn detect(&self, content: &str) -> Result<Detection, DetectorError> {
        // First attempt + retry on 5xx / transport failures.
        let first = self.call_once(content).await;
        let verdict = match first {
            Ok(v) => v,
            Err(DetectorError::ProviderHttp { status, .. }) if status >= 500 => {
                tokio::time::sleep(Duration::from_millis(OPENAI_RETRY_BACKOFF_MS)).await;
                self.call_once(content).await?
            }
            Err(DetectorError::Timeout { .. }) | Err(DetectorError::Other { .. }) => {
                tokio::time::sleep(Duration::from_millis(OPENAI_RETRY_BACKOFF_MS)).await;
                self.call_once(content).await?
            }
            Err(e) => return Err(e),
        };

        // Find the highest-scoring category that crosses its
        // threshold. Returns the category name + the score for the
        // violation payload.
        let mut worst: Option<(String, f64)> = None;
        for (cat, value) in &verdict.category_scores {
            let score = match value.as_f64() {
                Some(s) => s,
                None => continue,
            };
            if score >= self.threshold_for(cat) {
                match &worst {
                    Some((_, best)) if score <= *best => {}
                    _ => worst = Some((cat.clone(), score)),
                }
            }
        }

        match worst {
            Some((cat, score)) => Ok(Detection::Hit {
                category: cat,
                matches: vec![Span::whole(content)],
                score: Some(score),
            }),
            None => Ok(Detection::Pass),
        }
    }
}

// ===========================================================================
// Built-in profanity fallback
// ===========================================================================

/// Maintained slur + profanity wordlist used as the toxic-content
/// fallback when no external provider is configured. Coarse but
/// always available. Operators are encouraged to also configure
/// OpenAI Moderation.
///
/// The list is intentionally short and conservative: famous slurs
/// (which any production tenant wants blocked regardless of OpenAI's
/// availability) plus the most common English profanity. Tenants
/// with stricter requirements should configure OpenAI Moderation
/// AND extend `banned_words` with their domain-specific terms.
const PROFANITY_WORDLIST: &[&str] = &[
    // Slurs (truncated for the source code review — the production
    // list lives in a sibling crate that's not OSS-licensed; this
    // placeholder set is what ships in the trial bundle).
    "[slur-placeholder-1]",
    "[slur-placeholder-2]",
    // Common profanity.
    "fuck",
    "shit",
    "asshole",
    "bitch",
    "bastard",
    "dick",
    "piss",
    "cunt",
    "twat",
    "wanker",
];

/// Built-in profanity detector. Word-boundary matching to avoid the
/// Scunthorpe problem.
#[derive(Debug)]
pub struct BuiltInProfanityDetector;

impl BuiltInProfanityDetector {
    pub fn new() -> Self {
        Self
    }
}

impl Default for BuiltInProfanityDetector {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait::async_trait]
impl Detector for BuiltInProfanityDetector {
    fn name(&self) -> &'static str {
        "toxic_content"
    }

    async fn detect(&self, content: &str) -> Result<Detection, DetectorError> {
        // Lowercase comparison. Walk the wordlist and look for
        // word-boundary matches. We use a manual boundary check
        // (rather than building a regex) because the wordlist is
        // small and a regex_lite alternation of 12+ entries is
        // slower than the inline scan.
        let lower = content.to_lowercase();
        let bytes = lower.as_bytes();

        for word in PROFANITY_WORDLIST {
            let needle = word.as_bytes();
            let mut start = 0;
            while let Some(found) = find_subslice(&bytes[start..], needle) {
                let abs = start + found;
                let after = abs + needle.len();
                let left_ok = abs == 0 || !is_word_byte(bytes[abs - 1]);
                let right_ok = after == bytes.len() || !is_word_byte(bytes[after]);
                if left_ok && right_ok {
                    return Ok(Detection::Hit {
                        category: "profanity".into(),
                        matches: vec![Span {
                            start: abs,
                            end: after,
                            redaction: None,
                        }],
                        score: None,
                    });
                }
                start = abs + 1;
            }
        }
        Ok(Detection::Pass)
    }
}

fn find_subslice(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).position(|w| w == needle)
}

fn is_word_byte(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'_'
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod openai_tests {
    use super::*;
    use serde_json::json;
    use wiremock::matchers::{header, method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    async fn detector_against(server: &MockServer, thresholds: Value) -> OpenAiModerationDetector {
        let cfg = json!({
            "api_key": "sk-test",
            "endpoint": format!("{}/v1/moderations", server.uri()),
            "timeout_ms": 1000
        });
        OpenAiModerationDetector::build(&cfg, &thresholds, reqwest::Client::new()).unwrap()
    }

    fn moderation_body(scores: Value) -> Value {
        json!({ "results": [{ "category_scores": scores }] })
    }

    // ----- Constructor validation ------------------------------------------

    #[tokio::test]
    async fn build_rejects_missing_api_key() {
        let err = OpenAiModerationDetector::build(&json!({}), &json!({}), reqwest::Client::new())
            .unwrap_err();
        assert!(matches!(
            err,
            DetectorError::MissingProviderConfig {
                provider: "openai_moderation",
                field: "api_key"
            }
        ));
    }

    #[tokio::test]
    async fn threshold_clamps_out_of_range() {
        let d = OpenAiModerationDetector::build(
            &json!({ "api_key": "sk-x" }),
            &json!({ "hate": 1.5, "violence": -0.2 }),
            reqwest::Client::new(),
        )
        .unwrap();
        assert!((d.threshold_for("hate") - 1.0).abs() < 1e-9);
        assert!((d.threshold_for("violence") - 0.0).abs() < 1e-9);
    }

    #[tokio::test]
    async fn unknown_category_uses_default_threshold() {
        let d = OpenAiModerationDetector::build(
            &json!({ "api_key": "sk-x" }),
            &json!({}),
            reqwest::Client::new(),
        )
        .unwrap();
        assert!((d.threshold_for("hate") - DEFAULT_CATEGORY_THRESHOLD).abs() < 1e-9);
    }

    // ----- Detection -------------------------------------------------------

    #[tokio::test]
    async fn category_above_threshold_fires() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/v1/moderations"))
            .and(header("Authorization", "Bearer sk-test"))
            .respond_with(
                ResponseTemplate::new(200).set_body_json(moderation_body(json!({
                    "hate": 0.92, "harassment": 0.1
                }))),
            )
            .mount(&server)
            .await;
        let d = detector_against(&server, json!({})).await;
        match d.detect("anything").await.unwrap() {
            Detection::Hit {
                category, score, ..
            } => {
                assert_eq!(category, "hate");
                assert_eq!(score, Some(0.92));
            }
            _ => panic!("expected Hit"),
        }
    }

    #[tokio::test]
    async fn category_below_threshold_passes() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/v1/moderations"))
            .respond_with(
                ResponseTemplate::new(200).set_body_json(moderation_body(json!({
                    "hate": 0.40, "harassment": 0.30
                }))),
            )
            .mount(&server)
            .await;
        let d = detector_against(&server, json!({})).await;
        assert_eq!(d.detect("anything").await.unwrap(), Detection::Pass);
    }

    #[tokio::test]
    async fn per_category_threshold_overrides_default() {
        // Default 0.8; lower harassment to 0.5 — input score 0.6 now fires.
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/v1/moderations"))
            .respond_with(
                ResponseTemplate::new(200).set_body_json(moderation_body(json!({
                    "hate": 0.10, "harassment": 0.60
                }))),
            )
            .mount(&server)
            .await;
        let d = detector_against(&server, json!({ "harassment": 0.5 })).await;
        match d.detect("anything").await.unwrap() {
            Detection::Hit { category, .. } => assert_eq!(category, "harassment"),
            _ => panic!("expected Hit"),
        }
    }

    #[tokio::test]
    async fn highest_scoring_winning_category_reported() {
        // Two categories cross — report the higher score.
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/v1/moderations"))
            .respond_with(
                ResponseTemplate::new(200).set_body_json(moderation_body(json!({
                    "hate": 0.85, "violence": 0.95
                }))),
            )
            .mount(&server)
            .await;
        let d = detector_against(&server, json!({})).await;
        match d.detect("anything").await.unwrap() {
            Detection::Hit {
                category, score, ..
            } => {
                assert_eq!(category, "violence");
                assert_eq!(score, Some(0.95));
            }
            _ => panic!("expected Hit"),
        }
    }

    // ----- Failure paths ---------------------------------------------------

    #[tokio::test]
    async fn http_5xx_retries_then_propagates() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/v1/moderations"))
            .respond_with(ResponseTemplate::new(503))
            .expect(2)
            .mount(&server)
            .await;
        let d = detector_against(&server, json!({})).await;
        let err = d.detect("anything").await.unwrap_err();
        match err {
            DetectorError::ProviderHttp { status, .. } => assert_eq!(status, 503),
            other => panic!("expected ProviderHttp 503, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn http_4xx_does_not_retry() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/v1/moderations"))
            .respond_with(ResponseTemplate::new(401))
            .expect(1)
            .mount(&server)
            .await;
        let d = detector_against(&server, json!({})).await;
        let _ = d.detect("anything").await.unwrap_err();
    }

    #[tokio::test]
    async fn malformed_response_propagates() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/v1/moderations"))
            .respond_with(ResponseTemplate::new(200).set_body_string("not json"))
            .mount(&server)
            .await;
        let d = detector_against(&server, json!({})).await;
        let err = d.detect("anything").await.unwrap_err();
        assert!(matches!(err, DetectorError::MalformedResponse { .. }));
    }
}

#[cfg(test)]
mod profanity_tests {
    use super::*;

    fn detector() -> BuiltInProfanityDetector {
        BuiltInProfanityDetector::new()
    }

    async fn detect(d: &BuiltInProfanityDetector, input: &str) -> Detection {
        d.detect(input).await.unwrap()
    }

    #[tokio::test]
    async fn known_profanity_fires() {
        let d = detector();
        let det = detect(&d, "what the fuck is going on").await;
        match det {
            Detection::Hit { category, .. } => assert_eq!(category, "profanity"),
            _ => panic!("expected Hit"),
        }
    }

    #[tokio::test]
    async fn case_insensitive_match() {
        let d = detector();
        let det = detect(&d, "this SHIT is broken").await;
        assert!(matches!(det, Detection::Hit { .. }));
    }

    #[tokio::test]
    async fn word_boundary_prevents_scunthorpe_problem() {
        // "Scunthorpe" contains the literal letters of a profanity
        // but isn't itself profane. Word boundary check prevents
        // the false positive.
        let d = detector();
        let det = detect(&d, "I live in Scunthorpe").await;
        assert_eq!(det, Detection::Pass);
    }

    #[tokio::test]
    async fn boundary_check_works_at_start_and_end() {
        let d = detector();
        // Word at very start of input.
        assert!(matches!(
            detect(&d, "fuck this").await,
            Detection::Hit { .. }
        ));
        // Word at very end of input.
        assert!(matches!(
            detect(&d, "this is shit").await,
            Detection::Hit { .. }
        ));
    }

    #[tokio::test]
    async fn benign_input_passes() {
        let d = detector();
        assert_eq!(
            detect(&d, "Could you summarise this article for me?").await,
            Detection::Pass
        );
    }

    #[tokio::test]
    async fn empty_input_passes() {
        assert_eq!(detect(&detector(), "").await, Detection::Pass);
    }

    #[tokio::test]
    async fn name_is_toxic_content() {
        // Same logical detector name as the OpenAI client so the
        // admin audit feed groups them together regardless of which
        // backend produced the violation.
        assert_eq!(detector().name(), "toxic_content");
    }
}
