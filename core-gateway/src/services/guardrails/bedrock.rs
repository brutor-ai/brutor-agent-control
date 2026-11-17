// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # AWS Bedrock Guardrails detector
//!
//! Calls the Bedrock Runtime [`ApplyGuardrail`] API to screen content
//! against an AWS-managed guardrail (denied topics, content filters,
//! word filters, sensitive-information filters, contextual grounding).
//! It is a **standalone** detector — the orchestrator runs it over the
//! whole content as its own binding, alongside the built-in checks.
//!
//! [`ApplyGuardrail`]: https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ApplyGuardrail.html
//!
//! ## Authentication
//!
//! Requests are signed with **AWS Signature Version 4** by the gateway's
//! one signer (`anthropic_adapter::backend::bedrock::sigv4_headers_canonical`,
//! which signs every Bedrock request), with the credential and the region
//! of the guardrail's own `config` JSON (`aws_access_key_id` /
//! `aws_secret_access_key` / `aws_session_token`, `region`) and with nothing
//! else. This module reads no environment variable:
//! the gateway process's `AWS_*` variables are the operator's identity, and
//! a guardrail entry that named no credential ran as the operator, signing
//! a request to whatever `endpoint` the entry named. An entry without its
//! own credential or region builds no detector; the orchestrator then
//! treats the check as one that cannot be evaluated, and the policy's
//! `fail_open` decides the call.
//!
//! `aws_secret_access_key` and `aws_session_token` are stored encrypted
//! (the cipher and key of a provider entry's `api_key`) and arrive here
//! already decrypted by `GuardrailsCache` on load; the access key
//! id is an identifier and is stored in clear.
//!
//! ## Wire contract
//!
//! `POST https://bedrock-runtime.<region>.amazonaws.com/guardrail/<id>/version/<ver>/apply`,
//! the id and the version each one percent-encoded path segment (an
//! id may be the guardrail's ARN, which has `:` and `/`).
//!
//! ```json
//! { "source": "INPUT", "content": [ { "text": { "text": "<content>" } } ] }
//! ```
//!
//! A response with `action: "GUARDRAIL_INTERVENED"` is a hit. When the
//! response carries a masked `outputs[0].text`, it is attached as the
//! span redaction so a `redact` action scrubs the content in place.

use std::time::Duration;

use reqwest::Client as HttpClient;
use serde_json::Value;

use super::detector::{Detection, Detector, DetectorError, Span};
use crate::services::anthropic_adapter::backend::bedrock::{
    AwsCredentials, sigv4_headers_canonical, uri_encode,
};

const PROVIDER: &str = "bedrock_guardrail";
const SERVICE: &str = "bedrock";
const DEFAULT_TIMEOUT_MS: u64 = 4_000;
const DEFAULT_GUARDRAIL_VERSION: &str = "DRAFT";
const RETRY_BACKOFF_MS: u64 = 300;

/// AWS Bedrock Guardrails client.
pub struct BedrockGuardrailDetector {
    region: String,
    guardrail_id: String,
    guardrail_version: String,
    /// Base URL, e.g. `https://bedrock-runtime.us-east-1.amazonaws.com`.
    endpoint: String,
    credentials: AwsCredentials,
    timeout: Duration,
    http: HttpClient,
}

// Manual Debug: the credential is not rendered.
impl std::fmt::Debug for BedrockGuardrailDetector {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("BedrockGuardrailDetector")
            .field("region", &self.region)
            .field("guardrail_id", &self.guardrail_id)
            .field("guardrail_version", &self.guardrail_version)
            .field("endpoint", &self.endpoint)
            .field("credentials", &"<redacted>")
            .finish()
    }
}

impl BedrockGuardrailDetector {
    /// Build from a `providers[]` entry of type `bedrock`.
    ///
    /// Entry shape:
    /// ```json
    /// {
    ///   "provider": "bedrock",
    ///   "config": {
    ///     "guardrail_id": "abcd1234",
    ///     "guardrail_version": "DRAFT",
    ///     "region": "us-east-1",
    ///     "aws_access_key_id": "...",      // required: the guardrail's own
    ///     "aws_secret_access_key": "...",  // required: the guardrail's own
    ///     "aws_session_token": "...",      // optional
    ///     "timeout_ms": 4000
    ///   }
    /// }
    /// ```
    pub fn build(entry: &Value, http: HttpClient) -> Result<Self, DetectorError> {
        crate::services::guardrails::detector::refuse_unreadable_secret(
            entry,
            "bedrock_guardrail",
        )?;
        let cfg = entry.get("config");
        let cfg_str = |k: &str| {
            cfg.and_then(|c| c.get(k))
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty())
                .map(str::to_string)
        };

        let guardrail_id = cfg_str("guardrail_id").ok_or(DetectorError::MissingProviderConfig {
            provider: PROVIDER,
            field: "guardrail_id",
        })?;
        let guardrail_version =
            cfg_str("guardrail_version").unwrap_or_else(|| DEFAULT_GUARDRAIL_VERSION.to_string());

        // The region and the credential are the entry's own. There is no
        // other source: the process's environment is the gateway
        // operator's AWS identity, not this tenant's.
        let region = cfg_str("region").ok_or(DetectorError::MissingProviderConfig {
            provider: PROVIDER,
            field: "region",
        })?;
        let access_key_id =
            cfg_str("aws_access_key_id").ok_or(DetectorError::MissingProviderConfig {
                provider: PROVIDER,
                field: "aws_access_key_id",
            })?;
        let secret_access_key =
            cfg_str("aws_secret_access_key").ok_or(DetectorError::MissingProviderConfig {
                provider: PROVIDER,
                field: "aws_secret_access_key",
            })?;
        let session_token = cfg_str("aws_session_token");

        // The region is part of the host (when no endpoint is given) and of
        // the credential scope: a region name or no detector.
        let invalid_region = |_| DetectorError::Other {
            provider: PROVIDER,
            message: "the configured AWS region is not a region name".to_string(),
        };
        crate::services::cloud_hosts::aws_region(&region).map_err(invalid_region)?;
        let endpoint = match entry
            .get("endpoint")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
        {
            Some(endpoint) => endpoint.trim_end_matches('/').to_string(),
            None => crate::services::cloud_hosts::bedrock_runtime_base(&region)
                .map_err(invalid_region)?,
        };
        super::detector::check_endpoint(PROVIDER, &endpoint)?;

        let timeout_ms = cfg
            .and_then(|c| c.get("timeout_ms"))
            .and_then(|v| v.as_u64())
            .unwrap_or(DEFAULT_TIMEOUT_MS);

        Ok(Self {
            region,
            guardrail_id,
            guardrail_version,
            endpoint,
            credentials: AwsCredentials::new(access_key_id, secret_access_key, session_token),
            timeout: Duration::from_millis(timeout_ms),
            http,
        })
    }

    /// `/guardrail/<id>/version/<version>/apply`, the id and the version
    /// each ONE percent-encoded path segment.
    fn apply_path(&self) -> String {
        format!(
            "/guardrail/{}/version/{}/apply",
            uri_encode(&self.guardrail_id),
            uri_encode(&self.guardrail_version)
        )
    }

    async fn call_once(&self, content: &str) -> Result<Value, DetectorError> {
        let url = format!("{}{}", self.endpoint, self.apply_path());
        if host_for(&url).is_none() {
            return Err(DetectorError::Other {
                provider: PROVIDER,
                message: format!("could not parse host from endpoint `{}`", self.endpoint),
            });
        }

        let body = serde_json::json!({
            "source": "INPUT",
            "content": [ { "text": { "text": content } } ],
        });
        let body_bytes = serde_json::to_vec(&body).map_err(|e| DetectorError::Other {
            provider: PROVIDER,
            message: format!("request serialisation failed: {e}"),
        })?;

        // Signed over the request as it is sent: the encoded path, the host
        // with its port, the body bytes (the signer of every Bedrock call).
        let signed = sigv4_headers_canonical(
            &self.credentials,
            &self.region,
            SERVICE,
            &url,
            &body_bytes,
            chrono::Utc::now(),
        )
        .ok_or_else(|| DetectorError::Other {
            provider: PROVIDER,
            message: format!(
                "the endpoint `{}` cannot carry a signed request (a query or a fragment)",
                self.endpoint
            ),
        })?;

        let mut req = self
            .http
            .post(&url)
            .header("content-type", "application/json")
            .body(body_bytes)
            .timeout(self.timeout);
        for (name, value) in signed {
            req = req.header(name, value);
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
impl Detector for BedrockGuardrailDetector {
    fn name(&self) -> &'static str {
        "bedrock_guardrail"
    }

    async fn detect(&self, content: &str) -> Result<Detection, DetectorError> {
        let body = match self.call_once(content).await {
            Ok(b) => b,
            Err(DetectorError::ProviderHttp { status, .. }) if status >= 500 || status == 429 => {
                tokio::time::sleep(Duration::from_millis(RETRY_BACKOFF_MS)).await;
                self.call_once(content).await?
            }
            Err(DetectorError::Timeout { .. }) | Err(DetectorError::Other { .. }) => {
                tokio::time::sleep(Duration::from_millis(RETRY_BACKOFF_MS)).await;
                self.call_once(content).await?
            }
            Err(e) => return Err(e),
        };
        Ok(map_response(&body, content))
    }
}

/// `host[:port]` of a URL — the value AWS SigV4 signs and reqwest
/// sends as the `Host` header. The port is included only when the URL
/// carries an explicit non-default one (matches reqwest's behaviour).
fn host_for(url: &str) -> Option<String> {
    let parsed = url::Url::parse(url).ok()?;
    let host = parsed.host_str()?.to_string();
    match parsed.port() {
        Some(port) => Some(format!("{host}:{port}")),
        None => Some(host),
    }
}

/// Map an `ApplyGuardrail` response to a [`Detection`].
fn map_response(body: &Value, content: &str) -> Detection {
    let intervened = body
        .get("action")
        .and_then(|v| v.as_str())
        .map(|a| a == "GUARDRAIL_INTERVENED")
        .unwrap_or(false);
    if !intervened {
        return Detection::Pass;
    }

    let category = category_from_assessments(body);

    // Bedrock can return a masked rendition of the content under
    // `outputs[0].text`. When present, attach it as the span
    // redaction so a `redact` action scrubs the content in place.
    let masked = body
        .get("outputs")
        .and_then(|v| v.as_array())
        .and_then(|a| a.first())
        .and_then(|o| o.get("text"))
        .and_then(|v| v.as_str())
        .map(str::to_string);

    Detection::Hit {
        category,
        matches: vec![Span {
            start: 0,
            end: content.len(),
            redaction: masked,
        }],
        score: None,
    }
}

/// Derive a human-readable category from the first assessment's policy
/// block — `topicPolicy` → `topic`, `contentPolicy` → `content`, etc.
fn category_from_assessments(body: &Value) -> String {
    let first = body
        .get("assessments")
        .and_then(|v| v.as_array())
        .and_then(|a| a.first())
        .and_then(|v| v.as_object());
    if let Some(obj) = first {
        for key in obj.keys() {
            if let Some(stripped) = key.strip_suffix("Policy") {
                if !stripped.is_empty() {
                    return stripped.to_string();
                }
            }
        }
    }
    "guardrail".to_string()
}

// ===========================================================================
// AWS Signature Version 4
// ===========================================================================

/// A second, self-contained AWS Signature Version 4 implementation, pinned
/// to AWS's reference vectors. It signed the guardrail's requests until the
/// guardrail moved to the gateway's one signer; it stays as the
/// independent implementation that signer is checked against
/// (`the_gateways_signer_agrees_with_the_reference_implementation`).
#[cfg(test)]
pub mod sigv4 {
    use hmac::{Hmac, Mac};
    use sha2::{Digest, Sha256};

    type HmacSha256 = Hmac<Sha256>;

    /// Per-request signing inputs.
    pub struct SigningContext<'a> {
        pub access_key: &'a str,
        pub secret_key: &'a str,
        pub region: &'a str,
        pub service: &'a str,
        /// ISO-8601 basic UTC timestamp, `YYYYMMDDTHHMMSSZ`.
        pub amz_date: &'a str,
    }

    /// Lowercase hex SHA-256 of `data`.
    pub fn sha256_hex(data: &[u8]) -> String {
        hex::encode(Sha256::digest(data))
    }

    fn hmac(key: &[u8], data: &[u8]) -> Vec<u8> {
        let mut mac = HmacSha256::new_from_slice(key).expect("HMAC accepts any key length");
        mac.update(data);
        mac.finalize().into_bytes().to_vec()
    }

    /// The SigV4 signing key — the four-stage HMAC chain over the
    /// date, region, service and the `aws4_request` terminator.
    fn signing_key(secret: &str, date: &str, region: &str, service: &str) -> Vec<u8> {
        let k_date = hmac(format!("AWS4{secret}").as_bytes(), date.as_bytes());
        let k_region = hmac(&k_date, region.as_bytes());
        let k_service = hmac(&k_region, service.as_bytes());
        hmac(&k_service, b"aws4_request")
    }

    /// Compute the `Authorization` header value for a request.
    ///
    /// `signed_headers` must contain every header that will be sent
    /// and signed — at minimum `host` and `x-amz-date`. Names are
    /// lowercased and the set is sorted internally.
    pub fn authorization_header(
        ctx: &SigningContext,
        method: &str,
        canonical_uri: &str,
        canonical_query: &str,
        signed_headers: &[(String, String)],
        payload: &[u8],
    ) -> String {
        let payload_hash = sha256_hex(payload);

        let mut headers: Vec<(String, String)> = signed_headers
            .iter()
            .map(|(n, v)| (n.to_ascii_lowercase(), v.trim().to_string()))
            .collect();
        headers.sort_by(|a, b| a.0.cmp(&b.0));

        let canonical_headers: String = headers.iter().map(|(n, v)| format!("{n}:{v}\n")).collect();
        let signed_header_names: String = headers
            .iter()
            .map(|(n, _)| n.as_str())
            .collect::<Vec<_>>()
            .join(";");

        let canonical_request = format!(
            "{method}\n{canonical_uri}\n{canonical_query}\n{canonical_headers}\n{signed_header_names}\n{payload_hash}"
        );

        let date = &ctx.amz_date[..8]; // YYYYMMDD
        let scope = format!("{date}/{}/{}/aws4_request", ctx.region, ctx.service);
        let string_to_sign = format!(
            "AWS4-HMAC-SHA256\n{}\n{scope}\n{}",
            ctx.amz_date,
            sha256_hex(canonical_request.as_bytes())
        );

        let key = signing_key(ctx.secret_key, date, ctx.region, ctx.service);
        let signature = hex::encode(hmac(&key, string_to_sign.as_bytes()));

        format!(
            "AWS4-HMAC-SHA256 Credential={}/{scope}, SignedHeaders={signed_header_names}, Signature={signature}",
            ctx.access_key
        )
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        // ----- Primitives, pinned to known vectors -------------------------

        #[test]
        fn sha256_empty_string_vector() {
            assert_eq!(
                sha256_hex(b""),
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            );
        }

        #[test]
        fn sha256_abc_vector() {
            assert_eq!(
                sha256_hex(b"abc"),
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
            );
        }

        #[test]
        fn hmac_sha256_rfc4231_case1() {
            // RFC 4231 test case 1.
            let key = [0x0b_u8; 20];
            let out = hmac(&key, b"Hi There");
            assert_eq!(
                hex::encode(out),
                "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"
            );
        }

        // ----- Full assembly, pinned to the AWS SigV4 test suite -----------

        #[test]
        fn authorization_matches_aws_reference_signature() {
            // The `get-vanilla` request shape (GET /, host + x-amz-date,
            // empty body) signed for region `us-east-1` / service
            // `service` with the AWS example credentials. The expected
            // signature was produced by AWS's own reference signer —
            // `botocore.auth.SigV4Auth` — for the exact same inputs, so
            // this pins the full canonical-request → string-to-sign →
            // signing-key → signature chain against AWS's behaviour.
            let ctx = SigningContext {
                access_key: "AKIDEXAMPLE",
                secret_key: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                region: "us-east-1",
                service: "service",
                amz_date: "20150830T123600Z",
            };
            let headers = vec![
                ("host".to_string(), "example.amazonaws.com".to_string()),
                ("x-amz-date".to_string(), "20150830T123600Z".to_string()),
            ];
            let auth = authorization_header(&ctx, "GET", "/", "", &headers, b"");
            assert!(
                auth.contains(
                    "Signature=ea21d6f05e96a897f6000a1a293f0a5bf0f92a00343409e820dce329ca6365ea"
                ),
                "unexpected authorization header: {auth}"
            );
            assert!(
                auth.contains("Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request")
            );
            assert!(auth.contains("SignedHeaders=host;x-amz-date"));
        }

        #[test]
        fn header_order_is_normalised() {
            // Headers supplied out of order + mixed case still produce
            // a deterministic, sorted SignedHeaders list.
            let ctx = SigningContext {
                access_key: "AKIDEXAMPLE",
                secret_key: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                region: "us-east-1",
                service: "service",
                amz_date: "20150830T123600Z",
            };
            let headers = vec![
                ("X-Amz-Date".to_string(), "20150830T123600Z".to_string()),
                ("Host".to_string(), "example.amazonaws.com".to_string()),
            ];
            let auth = authorization_header(&ctx, "GET", "/", "", &headers, b"");
            assert!(auth.contains("SignedHeaders=host;x-amz-date"));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use wiremock::matchers::{header_exists, method};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    fn entry(server: &MockServer, extra_config: Value) -> Value {
        let mut config = json!({
            "guardrail_id": "gr-test",
            "region": "us-east-1",
            "aws_access_key_id": "AKIDEXAMPLE",
            "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        });
        if let Some(obj) = extra_config.as_object() {
            for (k, v) in obj {
                config[k] = v.clone();
            }
        }
        json!({ "provider": "bedrock", "endpoint": server.uri(), "config": config })
    }

    // ----- Constructor validation ------------------------------------------

    #[test]
    fn build_rejects_missing_guardrail_id() {
        let err = BedrockGuardrailDetector::build(
            &json!({ "provider": "bedrock", "config": { "region": "us-east-1" } }),
            reqwest::Client::new(),
        )
        .unwrap_err();
        assert!(matches!(
            err,
            DetectorError::MissingProviderConfig {
                provider: "bedrock_guardrail",
                field: "guardrail_id"
            }
        ));
    }

    /// An entry with no credential of its own builds no detector,
    /// whatever the process's environment holds (this module reads none of
    /// it: `nothing_here_reads_an_ambient_credential`).
    #[test]
    fn build_rejects_missing_credentials() {
        for (config, field) in [
            (
                json!({ "guardrail_id": "g", "region": "eu-west-1" }),
                "aws_access_key_id",
            ),
            (
                json!({ "guardrail_id": "g", "region": "eu-west-1", "aws_access_key_id": "a" }),
                "aws_secret_access_key",
            ),
            (
                json!({ "guardrail_id": "g", "aws_access_key_id": "a",
                        "aws_secret_access_key": "b" }),
                "region",
            ),
        ] {
            let err = BedrockGuardrailDetector::build(
                &json!({ "provider": "bedrock", "config": config }),
                reqwest::Client::new(),
            )
            .unwrap_err();
            assert!(
                matches!(
                    err,
                    DetectorError::MissingProviderConfig { provider: "bedrock_guardrail", field: f }
                        if f == field
                ),
                "{field}: {err}"
            );
        }
    }

    /// The credential, the region and the endpoint come from the entry and
    /// from nowhere else: this module names no environment variable, no
    /// home directory and no credential file.
    #[test]
    fn nothing_here_reads_an_ambient_credential() {
        let src = std::fs::read_to_string(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/src/services/guardrails/bedrock.rs"
        ))
        .expect("module source");
        let production = src.split("#[cfg(test)]").next().unwrap_or_default();
        for forbidden in [
            concat!("std::", "env"),
            concat!("env::", "var"),
            concat!("env_", "nonempty"),
            concat!("\"AWS", "_"),
            concat!(".a", "ws/"),
            concat!("home", "_dir"),
            concat!("169.254.", "170.2"),
        ] {
            assert!(
                !production.contains(forbidden),
                "guardrails/bedrock.rs mentions `{forbidden}`: a credential may only come \
                 from the guardrail's own configuration"
            );
        }
    }

    /// The guardrail's requests are signed by the gateway's one
    /// signer. The implementation in `sigv4`, pinned to AWS's reference
    /// vector above, computes the same signature for the same request: a
    /// POST whose path has an ARN as one encoded segment.
    #[test]
    fn the_gateways_signer_agrees_with_the_reference_implementation() {
        let creds = AwsCredentials::new(
            "AKIDEXAMPLE".to_string(),
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY".to_string(),
            None,
        );
        let d = BedrockGuardrailDetector::build(
            &json!({
                "provider": "bedrock",
                "config": {
                    "guardrail_id": "arn:aws:bedrock:us-east-1:123456789012:guardrail/abc123",
                    "guardrail_version": "7", "region": "us-east-1",
                    "aws_access_key_id": "AKIDEXAMPLE", "aws_secret_access_key": "x"
                }
            }),
            reqwest::Client::new(),
        )
        .unwrap();
        let path = d.apply_path();
        assert_eq!(
            path,
            "/guardrail/arn%3Aaws%3Abedrock%3Aus-east-1%3A123456789012%3Aguardrail%2Fabc123\
             /version/7/apply"
        );
        let url = format!("https://bedrock-runtime.us-east-1.amazonaws.com{path}");
        let body = br#"{"source":"INPUT"}"#;
        let now = chrono::DateTime::parse_from_rfc3339("2015-08-30T12:36:00Z")
            .unwrap()
            .with_timezone(&chrono::Utc);
        let signed = sigv4_headers_canonical(&creds, "us-east-1", SERVICE, &url, body, now)
            .expect("a signable URL");
        let authorization = &signed
            .iter()
            .find(|(name, _)| name == "Authorization")
            .expect("an Authorization header")
            .1;

        // The reference implementation, given the canonical URI the SigV4
        // reference defines: each segment of the path as sent, URI-encoded.
        let canonical_uri = path
            .split('/')
            .map(uri_encode)
            .collect::<Vec<_>>()
            .join("/");
        let ctx = sigv4::SigningContext {
            access_key: "AKIDEXAMPLE",
            secret_key: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            region: "us-east-1",
            service: SERVICE,
            amz_date: "20150830T123600Z",
        };
        let headers = vec![
            ("content-type".to_string(), "application/json".to_string()),
            (
                "host".to_string(),
                "bedrock-runtime.us-east-1.amazonaws.com".to_string(),
            ),
            ("x-amz-content-sha256".to_string(), sigv4::sha256_hex(body)),
            ("x-amz-date".to_string(), "20150830T123600Z".to_string()),
        ];
        assert_eq!(
            authorization,
            &sigv4::authorization_header(&ctx, "POST", &canonical_uri, "", &headers, body)
        );
    }

    #[test]
    fn build_defaults_guardrail_version_to_draft() {
        let d = BedrockGuardrailDetector::build(
            &json!({
                "provider": "bedrock",
                "config": {
                    "guardrail_id": "g", "region": "us-east-1",
                    "aws_access_key_id": "a", "aws_secret_access_key": "b"
                }
            }),
            reqwest::Client::new(),
        )
        .unwrap();
        assert_eq!(d.guardrail_version, "DRAFT");
        assert_eq!(d.apply_path(), "/guardrail/g/version/DRAFT/apply");
    }

    /// The region is part of the host. One that is not a region name
    /// builds no detector (it built `bedrock-runtime.x.attacker.example`).
    #[test]
    fn build_rejects_a_region_that_is_not_a_region() {
        for region in [
            "x.attacker.example/",
            "eu-west-1.attacker.example",
            "EU WEST",
        ] {
            let built = BedrockGuardrailDetector::build(
                &json!({
                    "provider": "bedrock",
                    "config": {
                        "guardrail_id": "g", "region": region,
                        "aws_access_key_id": "a", "aws_secret_access_key": "b"
                    }
                }),
                reqwest::Client::new(),
            );
            assert!(
                built.is_err(),
                "{region:?} built the endpoint {:?}",
                built.map(|d| d.endpoint).ok()
            );
        }
    }

    // ----- host_for --------------------------------------------------------

    #[test]
    fn host_for_omits_default_port() {
        assert_eq!(
            host_for("https://bedrock-runtime.us-east-1.amazonaws.com/x"),
            Some("bedrock-runtime.us-east-1.amazonaws.com".to_string())
        );
    }

    #[test]
    fn host_for_includes_explicit_port() {
        assert_eq!(
            host_for("http://127.0.0.1:8080/x"),
            Some("127.0.0.1:8080".to_string())
        );
    }

    // ----- Response mapping ------------------------------------------------

    #[test]
    fn map_response_pass_when_no_intervention() {
        assert_eq!(
            map_response(&json!({ "action": "NONE" }), "hello"),
            Detection::Pass
        );
    }

    #[test]
    fn map_response_hit_on_intervention() {
        let body = json!({
            "action": "GUARDRAIL_INTERVENED",
            "assessments": [ { "topicPolicy": { "topics": [] } } ]
        });
        match map_response(&body, "hello") {
            Detection::Hit {
                category, matches, ..
            } => {
                assert_eq!(category, "topic");
                assert_eq!(matches.len(), 1);
                assert!(matches[0].redaction.is_none());
            }
            _ => panic!("expected Hit"),
        }
    }

    #[test]
    fn map_response_attaches_masked_output_as_redaction() {
        let body = json!({
            "action": "GUARDRAIL_INTERVENED",
            "assessments": [ { "sensitiveInformationPolicy": {} } ],
            "outputs": [ { "text": "my email is {EMAIL}" } ]
        });
        match map_response(&body, "my email is jane@x.com") {
            Detection::Hit {
                category, matches, ..
            } => {
                assert_eq!(category, "sensitiveInformation");
                assert_eq!(matches[0].redaction.as_deref(), Some("my email is {EMAIL}"));
            }
            _ => panic!("expected Hit"),
        }
    }

    #[test]
    fn map_response_unknown_assessment_uses_generic_category() {
        let body = json!({ "action": "GUARDRAIL_INTERVENED", "assessments": [] });
        match map_response(&body, "x") {
            Detection::Hit { category, .. } => assert_eq!(category, "guardrail"),
            _ => panic!("expected Hit"),
        }
    }

    // ----- HTTP round-trip (signing is mocked, not verified vs AWS) --------

    #[tokio::test]
    async fn detect_clean_content_passes_and_signs_request() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(header_exists("authorization"))
            .and(header_exists("x-amz-date"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({ "action": "NONE" })))
            .expect(1)
            .mount(&server)
            .await;
        let d = BedrockGuardrailDetector::build(&entry(&server, json!({})), reqwest::Client::new())
            .unwrap();
        assert_eq!(d.detect("hello").await.unwrap(), Detection::Pass);
    }

    #[tokio::test]
    async fn detect_intervened_content_hits() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "action": "GUARDRAIL_INTERVENED",
                "assessments": [ { "contentPolicy": { "filters": [] } } ]
            })))
            .mount(&server)
            .await;
        let d = BedrockGuardrailDetector::build(&entry(&server, json!({})), reqwest::Client::new())
            .unwrap();
        match d.detect("bad").await.unwrap() {
            Detection::Hit { category, .. } => assert_eq!(category, "content"),
            _ => panic!("expected Hit"),
        }
    }

    #[tokio::test]
    async fn detect_maps_4xx_to_provider_http_error() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(403).set_body_string("AccessDenied"))
            .mount(&server)
            .await;
        let d = BedrockGuardrailDetector::build(&entry(&server, json!({})), reqwest::Client::new())
            .unwrap();
        let err = d.detect("x").await.unwrap_err();
        assert!(matches!(
            err,
            DetectorError::ProviderHttp {
                provider: "bedrock_guardrail",
                status: 403,
                ..
            }
        ));
    }

    #[tokio::test]
    async fn detect_sends_session_token_header_when_configured() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(header_exists("x-amz-security-token"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({ "action": "NONE" })))
            .expect(1)
            .mount(&server)
            .await;
        let d = BedrockGuardrailDetector::build(
            &entry(&server, json!({ "aws_session_token": "tok123" })),
            reqwest::Client::new(),
        )
        .unwrap();
        assert_eq!(d.detect("hello").await.unwrap(), Detection::Pass);
    }
}
