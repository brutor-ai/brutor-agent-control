// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Custom Webhook guardrail detector
//!
//! Calls an operator-supplied HTTP endpoint with the content under
//! inspection and maps the response to a [`Detection`]. This lets a
//! tenant plug in a guardrail engine the platform does not ship a
//! first-class integration for (an in-house classifier, a third-party
//! API behind a small adapter, etc.).
//!
//! Unlike the per-check providers (Lakera for prompt-injection, OpenAI
//! Moderation for toxic content) a custom webhook is a **standalone**
//! detector: the orchestrator runs it over the whole content as its
//! own binding, alongside the built-in checks.
//!
//! ## Wire contract
//!
//! Request — `POST <endpoint>`, `Content-Type: application/json`:
//!
//! ```json
//! { "content": "<text under inspection>" }
//! ```
//!
//! Response — `200`, `Content-Type: application/json`:
//!
//! ```json
//! {
//!   "flagged": true,
//!   "violations": [
//!     { "category": "self_harm", "message": "...", "score": 0.93 }
//!   ]
//! }
//! ```
//!
//! * `flagged` is authoritative when present. When omitted, a non-empty
//!   `violations` array is treated as flagged.
//! * `flagged: false` (or omitted with no violations) → [`Detection::Pass`].
//! * Any non-2xx, timeout, or unparseable body → [`DetectorError`]; the
//!   orchestrator resolves it via the policy's fail-open rule.

use std::time::Duration;

use reqwest::Client as HttpClient;
use serde_json::Value;

use super::detector::{Detection, Detector, DetectorError, Span};

const PROVIDER: &str = "custom_webhook";
const DEFAULT_TIMEOUT_MS: u64 = 3_000;
const RETRY_BACKOFF_MS: u64 = 250;

/// Custom-webhook detector. Stateless apart from the resolved config.
pub struct CustomWebhookDetector {
    endpoint: String,
    /// Optional auth header sent on every request: `(name, value)`.
    auth_header: Option<(String, String)>,
    timeout: Duration,
    http: HttpClient,
}

// Manual Debug — never render the auth header *value* (a secret).
impl std::fmt::Debug for CustomWebhookDetector {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("CustomWebhookDetector")
            .field("endpoint", &self.endpoint)
            .field(
                "auth_header",
                &self.auth_header.as_ref().map(|(name, _)| name),
            )
            .field("timeout", &self.timeout)
            .finish()
    }
}

impl CustomWebhookDetector {
    /// Build from a `providers[]` entry of type `custom`.
    ///
    /// Entry shape:
    /// ```json
    /// {
    ///   "provider": "custom",
    ///   "endpoint": "https://guardrails.example.com/check",
    ///   "api_key": "optional-secret",
    ///   "config": { "auth_header": "X-Api-Key", "timeout_ms": 3000 }
    /// }
    /// ```
    ///
    /// When `api_key` is set it is sent as `Authorization: Bearer
    /// <key>`, or under the header named by `config.auth_header` when
    /// that is provided.
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
            .to_string();
        super::detector::check_endpoint(PROVIDER, &endpoint)?;

        let cfg = entry.get("config");
        let api_key = entry
            .get("api_key")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty());
        let header_name = cfg
            .and_then(|c| c.get("auth_header"))
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty());
        let auth_header = match (api_key, header_name) {
            (Some(key), Some(name)) => Some((name.to_string(), key.to_string())),
            (Some(key), None) => Some(("Authorization".to_string(), format!("Bearer {key}"))),
            (None, _) => None,
        };

        let timeout_ms = cfg
            .and_then(|c| c.get("timeout_ms"))
            .and_then(|v| v.as_u64())
            .unwrap_or(DEFAULT_TIMEOUT_MS);

        Ok(Self {
            endpoint,
            auth_header,
            timeout: Duration::from_millis(timeout_ms),
            http,
        })
    }

    async fn call_once(&self, content: &str) -> Result<Value, DetectorError> {
        let mut req = self
            .http
            .post(&self.endpoint)
            .json(&serde_json::json!({ "content": content }))
            .timeout(self.timeout);
        if let Some((name, value)) = &self.auth_header {
            req = req.header(name.as_str(), value.as_str());
        }

        let resp = req.send().await.map_err(|e| {
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

        resp.json()
            .await
            .map_err(|e| DetectorError::MalformedResponse {
                provider: PROVIDER,
                reason: format!("JSON decode failed: {e}"),
            })
    }
}

#[async_trait::async_trait]
impl Detector for CustomWebhookDetector {
    fn name(&self) -> &'static str {
        "custom_webhook"
    }

    async fn detect(&self, content: &str) -> Result<Detection, DetectorError> {
        // First attempt + one retry on a 5xx / timeout / transport
        // failure — webhooks are operator-run and can hiccup.
        let body = match self.call_once(content).await {
            Ok(b) => b,
            Err(DetectorError::ProviderHttp { status, .. }) if status >= 500 => {
                tokio::time::sleep(Duration::from_millis(RETRY_BACKOFF_MS)).await;
                self.call_once(content).await?
            }
            Err(DetectorError::Timeout { .. }) | Err(DetectorError::Other { .. }) => {
                tokio::time::sleep(Duration::from_millis(RETRY_BACKOFF_MS)).await;
                self.call_once(content).await?
            }
            Err(e) => return Err(e),
        };
        parse_response(&body, content)
    }
}

/// Map a webhook response body to a [`Detection`].
fn parse_response(body: &Value, content: &str) -> Result<Detection, DetectorError> {
    let violations = body.get("violations").and_then(|v| v.as_array());
    let has_violations = violations.map(|a| !a.is_empty()).unwrap_or(false);
    // `flagged` is authoritative when present; otherwise infer it from
    // a non-empty `violations` array.
    let flagged = body
        .get("flagged")
        .and_then(|v| v.as_bool())
        .unwrap_or(has_violations);

    if !flagged {
        return Ok(Detection::Pass);
    }

    let first = violations.and_then(|a| a.first());
    let category = first
        .and_then(|v| v.get("category"))
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .unwrap_or("policy_violation")
        .to_string();
    let score = first.and_then(|v| v.get("score")).and_then(|v| v.as_f64());

    Ok(Detection::Hit {
        category,
        matches: vec![Span::whole(content)],
        score,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use wiremock::matchers::{body_json, header, method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    fn detector_for(server: &MockServer, extra_config: Value) -> CustomWebhookDetector {
        let mut entry = json!({
            "provider": "custom",
            "endpoint": format!("{}/check", server.uri()),
        });
        if let Some(obj) = extra_config.as_object() {
            for (k, v) in obj {
                entry[k] = v.clone();
            }
        }
        CustomWebhookDetector::build(&entry, reqwest::Client::new()).unwrap()
    }

    // ----- Constructor validation ------------------------------------------

    #[test]
    fn build_rejects_missing_endpoint() {
        let err =
            CustomWebhookDetector::build(&json!({ "provider": "custom" }), reqwest::Client::new())
                .unwrap_err();
        assert!(matches!(
            err,
            DetectorError::MissingProviderConfig {
                provider: "custom_webhook",
                field: "endpoint"
            }
        ));
    }

    #[test]
    fn build_rejects_empty_endpoint() {
        let err = CustomWebhookDetector::build(
            &json!({ "provider": "custom", "endpoint": "" }),
            reqwest::Client::new(),
        )
        .unwrap_err();
        assert!(matches!(err, DetectorError::MissingProviderConfig { .. }));
    }

    // ----- Response parsing ------------------------------------------------

    #[test]
    fn parse_pass_when_not_flagged() {
        let d = parse_response(&json!({ "flagged": false }), "hi").unwrap();
        assert_eq!(d, Detection::Pass);
    }

    #[test]
    fn parse_pass_when_empty_body() {
        let d = parse_response(&json!({}), "hi").unwrap();
        assert_eq!(d, Detection::Pass);
    }

    #[test]
    fn parse_hit_when_flagged_true() {
        let d = parse_response(
            &json!({ "flagged": true, "violations": [{ "category": "abuse", "score": 0.8 }] }),
            "hi",
        )
        .unwrap();
        match d {
            Detection::Hit {
                category, score, ..
            } => {
                assert_eq!(category, "abuse");
                assert_eq!(score, Some(0.8));
            }
            _ => panic!("expected Hit"),
        }
    }

    #[test]
    fn parse_infers_flag_from_violations_when_field_absent() {
        let d = parse_response(&json!({ "violations": [{ "category": "x" }] }), "hi").unwrap();
        assert!(matches!(d, Detection::Hit { .. }));
    }

    #[test]
    fn parse_flagged_true_with_no_violations_uses_default_category() {
        let d = parse_response(&json!({ "flagged": true }), "hi").unwrap();
        match d {
            Detection::Hit { category, .. } => assert_eq!(category, "policy_violation"),
            _ => panic!("expected Hit"),
        }
    }

    #[test]
    fn parse_flagged_false_overrides_present_violations() {
        // Explicit `flagged: false` wins even if violations is non-empty.
        let d = parse_response(
            &json!({ "flagged": false, "violations": [{ "category": "x" }] }),
            "hi",
        )
        .unwrap();
        assert_eq!(d, Detection::Pass);
    }

    // ----- HTTP round-trip -------------------------------------------------

    #[tokio::test]
    async fn detect_clean_content_passes() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/check"))
            .and(body_json(json!({ "content": "hello" })))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({ "flagged": false })))
            .mount(&server)
            .await;
        let d = detector_for(&server, json!({}));
        assert_eq!(d.detect("hello").await.unwrap(), Detection::Pass);
    }

    #[tokio::test]
    async fn detect_flagged_content_hits() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/check"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "flagged": true,
                "violations": [{ "category": "self_harm", "score": 0.97 }]
            })))
            .mount(&server)
            .await;
        let d = detector_for(&server, json!({}));
        match d.detect("bad stuff").await.unwrap() {
            Detection::Hit {
                category,
                score,
                matches,
            } => {
                assert_eq!(category, "self_harm");
                assert_eq!(score, Some(0.97));
                assert_eq!(matches.len(), 1);
            }
            _ => panic!("expected Hit"),
        }
    }

    #[tokio::test]
    async fn detect_sends_bearer_auth_when_api_key_set() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/check"))
            .and(header("Authorization", "Bearer s3cret"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({ "flagged": false })))
            .mount(&server)
            .await;
        let d = detector_for(&server, json!({ "api_key": "s3cret" }));
        assert_eq!(d.detect("hello").await.unwrap(), Detection::Pass);
    }

    #[tokio::test]
    async fn detect_sends_custom_auth_header_when_configured() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/check"))
            .and(header("X-Api-Key", "s3cret"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({ "flagged": false })))
            .mount(&server)
            .await;
        let d = detector_for(
            &server,
            json!({ "api_key": "s3cret", "config": { "auth_header": "X-Api-Key" } }),
        );
        assert_eq!(d.detect("hello").await.unwrap(), Detection::Pass);
    }

    #[tokio::test]
    async fn detect_maps_4xx_to_provider_http_error() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(403).set_body_string("forbidden"))
            .mount(&server)
            .await;
        let d = detector_for(&server, json!({}));
        let err = d.detect("hello").await.unwrap_err();
        assert!(matches!(
            err,
            DetectorError::ProviderHttp {
                provider: "custom_webhook",
                status: 403,
                ..
            }
        ));
    }

    #[tokio::test]
    async fn detect_maps_bad_json_to_malformed_response() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(200).set_body_string("not json"))
            .mount(&server)
            .await;
        let d = detector_for(&server, json!({}));
        let err = d.detect("hello").await.unwrap_err();
        assert!(matches!(
            err,
            DetectorError::MalformedResponse {
                provider: "custom_webhook",
                ..
            }
        ));
    }

    #[tokio::test]
    async fn detect_retries_once_on_5xx_then_succeeds() {
        let server = MockServer::start().await;
        // First call 503, retry 200.
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(503))
            .up_to_n_times(1)
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({ "flagged": false })))
            .mount(&server)
            .await;
        let d = detector_for(&server, json!({}));
        assert_eq!(d.detect("hello").await.unwrap(), Detection::Pass);
    }
}
