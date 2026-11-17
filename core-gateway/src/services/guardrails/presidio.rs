// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Microsoft Presidio PII guardrail detector
//!
//! POSTs content to a self-hosted [Presidio Analyzer] (`/analyze`) and
//! maps the recognised entities to redactable [`Span`]s. Presidio is an
//! alternative `pii_provider` to the built-in regex bank: it adds
//! ML-backed NER for names, locations, organisations, dates and the
//! like that a pure regex bank cannot catch.
//!
//! [Presidio Analyzer]: https://microsoft.github.io/presidio/
//!
//! ## Wire contract
//!
//! Request — `POST <endpoint>/analyze`:
//!
//! ```json
//! { "text": "<content>", "language": "en" }
//! ```
//!
//! Response — `200`, a JSON array of recognised entities:
//!
//! ```json
//! [ { "entity_type": "EMAIL_ADDRESS", "start": 11, "end": 27, "score": 0.85 } ]
//! ```
//!
//! `start` / `end` are **character** offsets (Python string indices);
//! they are converted to byte offsets for [`Span`], which the rest of
//! the proxy indexes by byte.
//!
//! Entities scoring below the configured `score_threshold` are dropped.
//! When at least one entity survives, the detector returns
//! [`Detection::Hit`] with one redactable span per entity — so a
//! `redact` action scrubs the PII in place; `block` / `warn` ignore the
//! span replacements and use the category only.

use std::time::Duration;

use reqwest::Client as HttpClient;
use serde_json::Value;

use super::detector::{Detection, Detector, DetectorError, Span};

const PROVIDER: &str = "presidio";
const DEFAULT_TIMEOUT_MS: u64 = 3_000;
const DEFAULT_SCORE_THRESHOLD: f64 = 0.5;
const DEFAULT_LANGUAGE: &str = "en";

/// Presidio Analyzer client. Reports as the `pii` check so it shares
/// the PII metric, audit category and fail-open override with the
/// built-in regex detector.
#[derive(Debug)]
pub struct PresidioDetector {
    /// Fully-resolved `/analyze` URL.
    analyze_url: String,
    language: String,
    /// Minimum entity score to count as a hit, in `[0.0, 1.0]`.
    score_threshold: f64,
    /// Entity-type allowlist (`config.entities`). When non-empty, only these
    /// Presidio entity types are redacted — forwarded on the `/analyze`
    /// request so the analyzer restricts its recognizers server-side, and
    /// enforced again client-side for older analyzers that ignore the field.
    /// Empty → every recognized entity redacts (legacy behavior). Without an
    /// allowlist Presidio's broad recognizers over-redact operational text —
    /// e.g. the word "quarter" in an echoed SQL statement comes back as a
    /// DATE_TIME entity and gets masked, which is noise, not privacy.
    entities: Vec<String>,
    timeout: Duration,
    http: HttpClient,
}

impl PresidioDetector {
    /// Build from a `providers[]` entry of type `presidio`.
    ///
    /// Entry shape:
    /// ```json
    /// {
    ///   "provider": "presidio",
    ///   "endpoint": "http://presidio-analyzer:3000",
    ///   "config": { "score_threshold": 0.5, "language": "en", "timeout_ms": 3000 }
    /// }
    /// ```
    ///
    /// `endpoint` is the Analyzer base URL (or the full `/analyze`
    /// URL). It is required — Presidio is self-hosted and has no
    /// public default.
    pub fn build(entry: &Value, http: HttpClient) -> Result<Self, DetectorError> {
        crate::services::guardrails::detector::refuse_unreadable_secret(entry, PROVIDER)?;
        let endpoint = entry
            .get("endpoint")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or(DetectorError::MissingProviderConfig {
                provider: PROVIDER,
                field: "endpoint",
            })?
            .trim_end_matches('/')
            .to_string();
        let analyze_url = if endpoint.ends_with("/analyze") {
            endpoint
        } else {
            format!("{endpoint}/analyze")
        };
        super::detector::check_endpoint(PROVIDER, &analyze_url)?;

        let cfg = entry.get("config");
        let score_threshold = cfg
            .and_then(|c| c.get("score_threshold"))
            .and_then(|v| v.as_f64())
            .unwrap_or(DEFAULT_SCORE_THRESHOLD)
            .clamp(0.0, 1.0);
        let language = cfg
            .and_then(|c| c.get("language"))
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .unwrap_or(DEFAULT_LANGUAGE)
            .to_string();
        let timeout_ms = cfg
            .and_then(|c| c.get("timeout_ms"))
            .and_then(|v| v.as_u64())
            .unwrap_or(DEFAULT_TIMEOUT_MS);
        let entities: Vec<String> = cfg
            .and_then(|c| c.get("entities"))
            .and_then(|v| v.as_array())
            .map(|a| {
                a.iter()
                    .filter_map(|e| e.as_str())
                    .filter(|s| !s.is_empty())
                    .map(|s| s.to_string())
                    .collect()
            })
            .unwrap_or_default();

        Ok(Self {
            analyze_url,
            language,
            score_threshold,
            entities,
            timeout: Duration::from_millis(timeout_ms),
            http,
        })
    }

    /// Operator override for the analyzer's score_threshold.
    /// `Some(t)` replaces the value read from the provider's
    /// `config.score_threshold`; `None` leaves the build-time default
    /// intact. Clamped to `[0.0, 1.0]` so callers can't push the
    /// analyzer into a pathological state.
    pub fn with_score_threshold(mut self, threshold: Option<f64>) -> Self {
        if let Some(t) = threshold {
            self.score_threshold = t.clamp(0.0, 1.0);
        }
        self
    }

    async fn analyze(&self, content: &str) -> Result<Vec<Value>, DetectorError> {
        // Forward `score_threshold` on the request so Presidio can
        // drop low-confidence entities server-side rather than make us
        // filter the full list afterwards. Same for the `entities`
        // allowlist: the analyzer then only runs the requested
        // recognizers. We ALSO filter both client-side in `map_entities`
        // because (a) older Presidio builds ignore the request fields and
        // (b) belt-and-braces matches our other adapters.
        let mut body = serde_json::json!({
            "text": content,
            "language": self.language,
            "score_threshold": self.score_threshold,
        });
        if !self.entities.is_empty() {
            body["entities"] = serde_json::json!(self.entities);
        }
        let resp = self
            .http
            .post(&self.analyze_url)
            .json(&body)
            .timeout(self.timeout)
            .send()
            .await
            .map_err(|e| {
                if e.is_timeout() {
                    DetectorError::Timeout { provider: PROVIDER }
                } else {
                    DetectorError::Other {
                        provider: PROVIDER,
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
                provider: PROVIDER,
                status: status.as_u16(),
                body_excerpt,
            });
        }

        let body: Value = resp
            .json()
            .await
            .map_err(|e| DetectorError::MalformedResponse {
                provider: PROVIDER,
                reason: format!("JSON decode failed: {e}"),
            })?;
        body.as_array()
            .cloned()
            .ok_or_else(|| DetectorError::MalformedResponse {
                provider: PROVIDER,
                reason: "expected a JSON array of entities".into(),
            })
    }
}

#[async_trait::async_trait]
impl Detector for PresidioDetector {
    fn name(&self) -> &'static str {
        // Reports as the PII check — shares the metric, audit category
        // and fail-open override with the built-in regex detector.
        "pii"
    }

    async fn detect(&self, content: &str) -> Result<Detection, DetectorError> {
        let entities = self.analyze(content).await?;
        Ok(map_entities(
            &entities,
            content,
            self.score_threshold,
            &self.entities,
        ))
    }
}

/// Char-index → byte-offset table. `map[i]` is the byte offset of the
/// `i`-th character; `map[char_count]` is `content.len()` so an
/// end-exclusive offset that points one past the last char resolves.
fn char_byte_map(content: &str) -> Vec<usize> {
    let mut map: Vec<usize> = content.char_indices().map(|(b, _)| b).collect();
    map.push(content.len());
    map
}

/// Convert Presidio's recognised entities into a [`Detection`].
/// `allowed` is the entity-type allowlist — empty means "everything".
fn map_entities(
    entities: &[Value],
    content: &str,
    threshold: f64,
    allowed: &[String],
) -> Detection {
    let byte_at = char_byte_map(content);
    let char_count = byte_at.len() - 1;

    // (byte_start, byte_end, entity_type, score)
    let mut hits: Vec<(usize, usize, String, f64)> = Vec::new();
    for ent in entities {
        let score = ent.get("score").and_then(|v| v.as_f64()).unwrap_or(0.0);
        if score < threshold {
            continue;
        }
        // Entity allowlist — enforced here as well as on the request so an
        // analyzer that ignores the request-level `entities` field can't
        // reintroduce over-redaction (e.g. DATE_TIME on the word "quarter").
        if !allowed.is_empty() {
            let ty = ent
                .get("entity_type")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            if !allowed.iter().any(|a| a == ty) {
                continue;
            }
        }
        let (Some(cs), Some(ce)) = (
            ent.get("start")
                .and_then(|v| v.as_u64())
                .map(|v| v as usize),
            ent.get("end").and_then(|v| v.as_u64()).map(|v| v as usize),
        ) else {
            continue;
        };
        // Defensive: Presidio offsets should always be in range, but a
        // provider regression must never panic the byte slicing.
        if cs >= ce || ce > char_count {
            continue;
        }
        let entity_type = ent
            .get("entity_type")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .unwrap_or("PII")
            .to_string();
        hits.push((byte_at[cs], byte_at[ce], entity_type, score));
    }

    if hits.is_empty() {
        return Detection::Pass;
    }

    // Sort by start, then greedily drop any span that overlaps an
    // already-kept one — overlapping replacements would corrupt the
    // redacted output.
    hits.sort_by_key(|h| h.0);
    let mut spans: Vec<Span> = Vec::new();
    let mut types: Vec<String> = Vec::new();
    let mut max_score = 0.0_f64;
    let mut last_end = 0_usize;
    for (start, end, entity_type, score) in hits {
        if start < last_end {
            continue;
        }
        last_end = end;
        max_score = max_score.max(score);
        spans.push(Span {
            start,
            end,
            redaction: Some(format!("[REDACTED-{entity_type}]")),
        });
        if !types.contains(&entity_type) {
            types.push(entity_type);
        }
    }

    // One distinct entity type → name it; several → the generic `pii`.
    let category = if types.len() == 1 {
        types.remove(0)
    } else {
        "pii".to_string()
    };

    Detection::Hit {
        category,
        matches: spans,
        score: Some(max_score),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use wiremock::matchers::{method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    fn detector_for(server: &MockServer, config: Value) -> PresidioDetector {
        let mut entry = json!({ "provider": "presidio", "endpoint": server.uri() });
        if let Some(obj) = config.as_object() {
            for (k, v) in obj {
                entry[k] = v.clone();
            }
        }
        PresidioDetector::build(&entry, reqwest::Client::new()).unwrap()
    }

    // ----- Constructor -----------------------------------------------------

    #[test]
    fn build_rejects_missing_endpoint() {
        let err =
            PresidioDetector::build(&json!({ "provider": "presidio" }), reqwest::Client::new())
                .unwrap_err();
        assert!(matches!(
            err,
            DetectorError::MissingProviderConfig {
                provider: "presidio",
                field: "endpoint"
            }
        ));
    }

    #[test]
    fn build_appends_analyze_path() {
        let d = PresidioDetector::build(
            &json!({ "provider": "presidio", "endpoint": "http://presidio:3000" }),
            reqwest::Client::new(),
        )
        .unwrap();
        assert_eq!(d.analyze_url, "http://presidio:3000/analyze");
    }

    #[test]
    fn build_keeps_explicit_analyze_path() {
        let d = PresidioDetector::build(
            &json!({ "provider": "presidio", "endpoint": "http://presidio:3000/analyze" }),
            reqwest::Client::new(),
        )
        .unwrap();
        assert_eq!(d.analyze_url, "http://presidio:3000/analyze");
    }

    #[test]
    fn build_clamps_score_threshold() {
        let d = PresidioDetector::build(
            &json!({
                "provider": "presidio",
                "endpoint": "http://p:3000",
                "config": { "score_threshold": 2.0 }
            }),
            reqwest::Client::new(),
        )
        .unwrap();
        assert!((d.score_threshold - 1.0).abs() < 1e-9);
    }

    // ----- score_threshold override ------------------------------------
    //
    // These pin the wire contract the orchestrator relies on:
    //   1. `with_score_threshold(Some(x))` REPLACES the build-time value.
    //   2. `with_score_threshold(None)` is a no-op.
    //   3. The override is clamped to [0.0, 1.0] (no analyzer crash).
    //   4. The threshold reaches the `/analyze` request body — that's
    //      the contract Presidio uses to filter server-side; without
    //      it operators only get client-side filtering, doubling the
    //      bandwidth on large entity lists.

    #[test]
    fn with_score_threshold_some_replaces_build_value() {
        let d = PresidioDetector::build(
            &json!({
                "provider": "presidio",
                "endpoint": "http://p:3000",
                "config": { "score_threshold": 0.5 }
            }),
            reqwest::Client::new(),
        )
        .unwrap()
        .with_score_threshold(Some(0.85));
        assert!((d.score_threshold - 0.85).abs() < 1e-9);
    }

    #[test]
    fn with_score_threshold_none_keeps_build_value() {
        let d = PresidioDetector::build(
            &json!({
                "provider": "presidio",
                "endpoint": "http://p:3000",
                "config": { "score_threshold": 0.42 }
            }),
            reqwest::Client::new(),
        )
        .unwrap()
        .with_score_threshold(None);
        assert!((d.score_threshold - 0.42).abs() < 1e-9);
    }

    #[test]
    fn with_score_threshold_clamps_override() {
        let d = PresidioDetector::build(
            &json!({ "provider": "presidio", "endpoint": "http://p:3000" }),
            reqwest::Client::new(),
        )
        .unwrap()
        .with_score_threshold(Some(-3.0));
        assert!((d.score_threshold - 0.0).abs() < 1e-9);
    }

    #[tokio::test]
    async fn analyze_forwards_score_threshold_on_request_body() {
        use wiremock::matchers::{body_partial_json, method, path};
        use wiremock::{Mock, ResponseTemplate};

        let server = MockServer::start().await;
        // Match ONLY when the request body carries our overridden
        // threshold — Presidio will then server-side-filter and we
        // halve the bandwidth on large responses. If the orchestrator
        // forgets to forward, this Mock won't fire and the test fails
        // with "no matching mock", which is exactly the signal we want.
        Mock::given(method("POST"))
            .and(path("/analyze"))
            .and(body_partial_json(json!({ "score_threshold": 0.85 })))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!([])))
            .expect(1)
            .mount(&server)
            .await;

        let d = detector_for(&server, json!({})).with_score_threshold(Some(0.85));
        // Body doesn't matter — we're testing the request shape, not detection.
        let _ = d.detect("test").await.unwrap();
    }

    // ----- Offset conversion ----------------------------------------------

    #[test]
    fn char_byte_map_handles_ascii() {
        let m = char_byte_map("hello");
        assert_eq!(m, vec![0, 1, 2, 3, 4, 5]);
    }

    #[test]
    fn char_byte_map_handles_multibyte() {
        // "café" — 'c','a','f' = 1 byte each, 'é' = 2 bytes.
        let m = char_byte_map("café");
        assert_eq!(m, vec![0, 1, 2, 3, 5]);
    }

    #[test]
    fn map_entities_converts_char_offsets_to_byte_offsets() {
        // "café jane@x.com" — the email starts at char 5, byte 6
        // (because 'é' is two bytes).
        let content = "café jane@x.com";
        let entities = vec![json!({
            "entity_type": "EMAIL_ADDRESS", "start": 5, "end": 15, "score": 0.9
        })];
        match map_entities(&entities, content, 0.5, &[]) {
            Detection::Hit {
                matches, category, ..
            } => {
                assert_eq!(category, "EMAIL_ADDRESS");
                assert_eq!(matches.len(), 1);
                assert_eq!(matches[0].start, 6);
                assert_eq!(matches[0].end, 16);
                assert_eq!(&content[matches[0].start..matches[0].end], "jane@x.com");
                assert_eq!(
                    matches[0].redaction.as_deref(),
                    Some("[REDACTED-EMAIL_ADDRESS]")
                );
            }
            _ => panic!("expected Hit"),
        }
    }

    #[test]
    fn map_entities_drops_below_threshold() {
        let entities = vec![json!({
            "entity_type": "PERSON", "start": 0, "end": 4, "score": 0.3
        })];
        assert_eq!(
            map_entities(&entities, "John doe", 0.5, &[]),
            Detection::Pass
        );
    }

    #[test]
    fn map_entities_empty_passes() {
        assert_eq!(map_entities(&[], "clean text", 0.5, &[]), Detection::Pass);
    }

    #[test]
    fn map_entities_drops_overlapping_spans() {
        // Two entities, the second overlapping the first — only the
        // first survives so redaction can't corrupt.
        let entities = vec![
            json!({ "entity_type": "PERSON", "start": 0, "end": 8, "score": 0.9 }),
            json!({ "entity_type": "NAME", "start": 5, "end": 12, "score": 0.9 }),
        ];
        match map_entities(&entities, "John Smith ok", 0.5, &[]) {
            Detection::Hit { matches, .. } => assert_eq!(matches.len(), 1),
            _ => panic!("expected Hit"),
        }
    }

    #[test]
    fn map_entities_multiple_types_uses_generic_category() {
        let entities = vec![
            json!({ "entity_type": "EMAIL_ADDRESS", "start": 0, "end": 3, "score": 0.9 }),
            json!({ "entity_type": "PHONE_NUMBER", "start": 4, "end": 7, "score": 0.9 }),
        ];
        match map_entities(&entities, "abc def hij", 0.5, &[]) {
            Detection::Hit {
                category, matches, ..
            } => {
                assert_eq!(category, "pii");
                assert_eq!(matches.len(), 2);
            }
            _ => panic!("expected Hit"),
        }
    }

    #[test]
    fn map_entities_skips_out_of_range_offsets() {
        // A provider regression returning an end past the content
        // length must be skipped, never panic.
        let entities = vec![json!({
            "entity_type": "PERSON", "start": 0, "end": 999, "score": 0.9
        })];
        assert_eq!(map_entities(&entities, "short", 0.5, &[]), Detection::Pass);
    }

    // ----- Entity allowlist (`config.entities`) -----------------------------
    //
    // Without an allowlist Presidio's broad recognizers over-redact
    // operational text (the demo hit: "quarter" in an echoed SQL statement
    // came back as DATE_TIME and was masked). These pin the contract:
    //   1. `config.entities` parses into the detector.
    //   2. Non-allowlisted entity types are dropped client-side.
    //   3. The allowlist is forwarded on the `/analyze` request body.
    //   4. Empty/absent list keeps legacy redact-everything behavior.

    #[test]
    fn build_parses_entities_allowlist() {
        let d = PresidioDetector::build(
            &json!({
                "provider": "presidio",
                "endpoint": "http://p:3000",
                "config": { "entities": ["EMAIL_ADDRESS", "PERSON"] }
            }),
            reqwest::Client::new(),
        )
        .unwrap();
        assert_eq!(d.entities, vec!["EMAIL_ADDRESS", "PERSON"]);
    }

    #[test]
    fn map_entities_drops_types_outside_allowlist() {
        // DATE_TIME ("quarter") must be dropped; PERSON survives.
        let entities = vec![
            json!({ "entity_type": "DATE_TIME", "start": 0, "end": 7, "score": 0.85 }),
            json!({ "entity_type": "PERSON", "start": 8, "end": 12, "score": 0.85 }),
        ];
        let allowed = vec!["PERSON".to_string(), "EMAIL_ADDRESS".to_string()];
        match map_entities(&entities, "quarter Jane", 0.5, &allowed) {
            Detection::Hit {
                category, matches, ..
            } => {
                assert_eq!(category, "PERSON");
                assert_eq!(matches.len(), 1);
                assert_eq!(matches[0].redaction.as_deref(), Some("[REDACTED-PERSON]"));
            }
            _ => panic!("expected Hit with only PERSON"),
        }
    }

    #[test]
    fn map_entities_all_filtered_passes() {
        let entities =
            vec![json!({ "entity_type": "DATE_TIME", "start": 0, "end": 7, "score": 0.9 })];
        let allowed = vec!["PERSON".to_string()];
        assert_eq!(
            map_entities(&entities, "quarter", 0.5, &allowed),
            Detection::Pass
        );
    }

    #[tokio::test]
    async fn analyze_forwards_entities_on_request_body() {
        use wiremock::matchers::body_partial_json;
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/analyze"))
            .and(body_partial_json(
                json!({ "entities": ["EMAIL_ADDRESS", "PERSON"] }),
            ))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!([])))
            .expect(1)
            .mount(&server)
            .await;
        let d = detector_for(
            &server,
            json!({ "config": { "entities": ["EMAIL_ADDRESS", "PERSON"] } }),
        );
        let _ = d.detect("test").await.unwrap();
    }

    // ----- HTTP round-trip -------------------------------------------------

    #[tokio::test]
    async fn detect_clean_content_passes() {
        // Request body now also carries `score_threshold` so
        // Presidio can server-side-filter. Use `body_partial_json` so
        // this matcher pins only the fields we control here (text +
        // language); the threshold default is exercised by the
        // dedicated `analyze_forwards_score_threshold_on_request_body`
        // test above.
        use wiremock::matchers::body_partial_json;
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/analyze"))
            .and(body_partial_json(
                json!({ "text": "all clear", "language": "en" }),
            ))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!([])))
            .mount(&server)
            .await;
        let d = detector_for(&server, json!({}));
        assert_eq!(d.detect("all clear").await.unwrap(), Detection::Pass);
    }

    #[tokio::test]
    async fn detect_pii_content_hits_with_redaction_spans() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/analyze"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!([
                { "entity_type": "EMAIL_ADDRESS", "start": 6, "end": 16, "score": 0.95 }
            ])))
            .mount(&server)
            .await;
        let d = detector_for(&server, json!({}));
        match d.detect("mail: jane@x.com").await.unwrap() {
            Detection::Hit {
                category,
                matches,
                score,
            } => {
                assert_eq!(category, "EMAIL_ADDRESS");
                assert_eq!(score, Some(0.95));
                assert!(matches[0].redaction.is_some());
            }
            _ => panic!("expected Hit"),
        }
    }

    #[tokio::test]
    async fn detect_maps_5xx_to_provider_http_error() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(500))
            .mount(&server)
            .await;
        let d = detector_for(&server, json!({}));
        let err = d.detect("x").await.unwrap_err();
        assert!(matches!(
            err,
            DetectorError::ProviderHttp {
                provider: "presidio",
                status: 500,
                ..
            }
        ));
    }

    #[tokio::test]
    async fn detect_maps_non_array_body_to_malformed() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({ "oops": true })))
            .mount(&server)
            .await;
        let d = detector_for(&server, json!({}));
        let err = d.detect("x").await.unwrap_err();
        assert!(matches!(
            err,
            DetectorError::MalformedResponse {
                provider: "presidio",
                ..
            }
        ));
    }
}
