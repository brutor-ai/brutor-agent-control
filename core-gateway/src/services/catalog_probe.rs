// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Catalog-probe health check.
//!
//! Honest health check for image / video / TTS / STT / chat models that
//! actually verifies the upstream provider — at zero cost. Hits the
//! provider's `/models` listing endpoint with the model's API key,
//! optionally checks that the configured `model_name` appears in the
//! response, and returns a structured result.
//!
//! The motivating bug: synthetic-validation probes (e.g. image probe
//! sending `n=0`) bounced off Brutor's own validation 400 and never
//! reached the provider, so a Healthy badge meant nothing about the
//! provider's API key, network, or model availability. This probe
//! does the round-trip honestly.
//!
//! Per-provider URL + auth conventions live in `auth_for_provider`
//! and `resolve_catalog_url` below. Provider key (lowercase slug) is
//! the dispatch key, matching the rest of the proxy.

use std::time::{Duration, Instant};

use serde::Serialize;
use sqlx::PgPool;

/// Sentinel embedded in `models_api_url` for self-hosted providers
/// where the catalog URL depends on the model's own `api_url` (vLLM,
/// LM Studio). The probe substitutes this prefix with the model
/// row's `api_url`, stripped of any trailing slash.
const MODEL_API_URL_SENTINEL: &str = "<MODEL_API_URL>";

/// HTTP timeout for the probe. Provider /models endpoints are fast
/// (small JSON response, no inference). 10 s is generous and gives
/// us room to retry once if the first try is blocked behind TLS
/// handshake on a cold connection.
const PROBE_TIMEOUT_SECS: u64 = 10;

/// Outcome the API endpoint hands back to gateway-control. Mirrors
/// the shape that `llm_models_manager.py` expects — `success`,
/// `latency_ms`, `error_message` are the load-bearing fields; the
/// rest are diagnostics that surface in the admin UI on hover.
#[derive(Debug, Clone, Serialize)]
pub struct CatalogProbeResult {
    /// Whether the probe came back with a 2xx that looks like a
    /// model list. False on 4xx, 5xx, network error, JSON parse
    /// error, or missing model name when checking is enabled.
    pub success: bool,
    /// HTTP status from the provider, or `None` if we never got
    /// that far (network error, TLS failure).
    pub status_code: Option<u16>,
    /// Wall-clock time from request start to response end, in
    /// milliseconds. `None` only if the dispatch failed before
    /// it started.
    pub latency_ms: Option<f64>,
    /// Whether the configured `model_name` appears in the
    /// returned listing. `None` means the listing was returned
    /// but we didn't recognise its shape well enough to scan.
    pub model_name_found: Option<bool>,
    /// Number of models in the returned listing. `None` if the
    /// shape didn't match anything we know how to count.
    pub models_count: Option<usize>,
    /// Resolved URL we actually hit (after sentinel substitution).
    /// Surfaced to the admin UI so an operator can copy/paste into
    /// curl to debug a failure.
    pub resolved_url: String,
    /// Free-form error message on failure. Empty on success.
    pub error_message: String,
    /// Provider key dispatched on (`openai`, `anthropic`, `gemini`,
    /// …). Repeats `provider_key` from the request — useful for
    /// correlating logs.
    pub provider_key: String,
}

/// Inputs the probe needs. Pulled from the DB by the caller (route
/// handler) and passed in so this module is testable without a DB.
#[derive(Debug, Clone)]
pub struct CatalogProbeInputs {
    /// Lowercase provider slug — drives auth style.
    pub provider_key: String,
    /// Provider's `models_api_url` column. May contain the
    /// `<MODEL_API_URL>` sentinel.
    pub models_api_url: String,
    /// Model row's own `api_url` — used to resolve the sentinel.
    pub model_api_url: Option<String>,
    /// The model's display name on the provider side. Optional —
    /// when set, the probe verifies the name appears in the
    /// listing.
    pub model_name: Option<String>,
    /// The model's API key, ALREADY DECRYPTED. The route handler
    /// runs the decryption; this module never sees ciphertext.
    pub api_key: Option<String>,
}

#[derive(Debug)]
pub enum CatalogProbeError {
    /// Provider doesn't have `models_api_url` configured — the
    /// catalog probe isn't applicable. Caller should fall back to
    /// a real-inference probe.
    NoCatalogUrl,
    /// Provider has the sentinel but the model row's `api_url` is
    /// unset, so we can't resolve it. Either the model is
    /// misconfigured or the provider entry shouldn't have used the
    /// sentinel.
    SentinelUnresolvable,
}

/// Dispatch entry point. Returns a `CatalogProbeResult` even on
/// HTTP-layer failures (e.g. 401, 5xx, network) — the route
/// handler turns this into a JSON response and writes a
/// proxy_metric row. `Err` is reserved for "this probe doesn't
/// apply" outcomes the caller has to react to.
pub async fn run(
    http_client: &reqwest::Client,
    inputs: &CatalogProbeInputs,
) -> Result<CatalogProbeResult, CatalogProbeError> {
    if inputs.models_api_url.trim().is_empty() {
        return Err(CatalogProbeError::NoCatalogUrl);
    }
    let resolved_url =
        resolve_catalog_url(&inputs.models_api_url, inputs.model_api_url.as_deref())?;
    // The probe URL comes from the model's tenant-configured api_url
    // (or the provider's catalog URL); an IP literal the outbound policy
    // denies is never fetched. Reported as a failed probe.
    if let Err(denied) = crate::services::outbound_policy::check_url(&resolved_url) {
        tracing::warn!(host = %denied.host, reason = denied.reason, "catalog probe URL refused by outbound policy");
        return Ok(CatalogProbeResult {
            success: false,
            status_code: None,
            latency_ms: None,
            model_name_found: None,
            models_count: None,
            resolved_url,
            error_message: denied.to_string(),
            provider_key: inputs.provider_key.clone(),
        });
    }

    let mut req = http_client
        .get(&resolved_url)
        .timeout(Duration::from_secs(PROBE_TIMEOUT_SECS));
    req = apply_auth(req, &inputs.provider_key, inputs.api_key.as_deref());

    let started = Instant::now();
    let outcome = req.send().await;
    let latency_ms = started.elapsed().as_secs_f64() * 1000.0;

    match outcome {
        Err(e) => Ok(CatalogProbeResult {
            success: false,
            status_code: e.status().map(|s| s.as_u16()),
            latency_ms: Some(latency_ms),
            model_name_found: None,
            models_count: None,
            resolved_url,
            error_message: format!("network/dispatch error: {e}"),
            provider_key: inputs.provider_key.clone(),
        }),
        Ok(resp) => {
            let status = resp.status();
            let status_code = status.as_u16();
            let body = resp.text().await.unwrap_or_default();

            if !status.is_success() {
                return Ok(CatalogProbeResult {
                    success: false,
                    status_code: Some(status_code),
                    latency_ms: Some(latency_ms),
                    model_name_found: None,
                    models_count: None,
                    resolved_url,
                    error_message: format!(
                        "provider returned {} — body: {}",
                        status_code,
                        truncate(&body, 500)
                    ),
                    provider_key: inputs.provider_key.clone(),
                });
            }

            // Parse the body and count / scan models.
            let parsed: serde_json::Value =
                serde_json::from_str(&body).unwrap_or(serde_json::Value::Null);
            let (models_count, model_name_found) =
                summarise_listing(&parsed, inputs.model_name.as_deref());

            // If the operator passed a model_name, treat absence as
            // failure — that's the catalog probe's whole point: confirm
            // the configured name resolves on the provider side.
            let success = !matches!(
                (model_name_found, inputs.model_name.as_deref()),
                (Some(false), Some(_))
            );
            let error_message = if success {
                String::new()
            } else {
                format!(
                    "model {:?} not found in provider listing of {} models",
                    inputs.model_name.as_deref().unwrap_or(""),
                    models_count.unwrap_or(0)
                )
            };

            Ok(CatalogProbeResult {
                success,
                status_code: Some(status_code),
                latency_ms: Some(latency_ms),
                model_name_found,
                models_count,
                resolved_url,
                error_message,
                provider_key: inputs.provider_key.clone(),
            })
        }
    }
}

/// Substitute the `<MODEL_API_URL>` sentinel if present.
fn resolve_catalog_url(
    template: &str,
    model_api_url: Option<&str>,
) -> Result<String, CatalogProbeError> {
    if !template.contains(MODEL_API_URL_SENTINEL) {
        return Ok(template.to_string());
    }
    let base = model_api_url
        .map(|s| s.trim_end_matches('/').to_string())
        .ok_or(CatalogProbeError::SentinelUnresolvable)?;
    Ok(template.replace(MODEL_API_URL_SENTINEL, &base))
}

/// Apply provider-specific auth. Most providers take Bearer; a few
/// stragglers use header / query forms unique to them.
fn apply_auth(
    req: reqwest::RequestBuilder,
    provider_key: &str,
    api_key: Option<&str>,
) -> reqwest::RequestBuilder {
    let Some(key) = api_key else {
        // Some self-hosted providers (Ollama, vLLM, LM Studio)
        // don't require auth. We pass the request through — if the
        // remote demands it the GET will 401 and we surface that.
        return req;
    };
    if key.is_empty() {
        return req;
    }

    match provider_key {
        // Anthropic doesn't use Bearer; their docs require x-api-key.
        // Their /v1/models endpoint additionally needs an
        // anthropic-version header — without it the server 400s
        // before it even authenticates.
        "anthropic" => req
            .header("x-api-key", key)
            .header("anthropic-version", "2023-06-01"),
        // Gemini's Generative Language API uses ?key= query auth.
        // The api_url already includes /v1beta/models, so we just
        // append the query parameter.
        "gemini" => req.query(&[("key", key)]),
        // Replicate uses `Authorization: Token <key>` rather than
        // Bearer. Different prefix, same header.
        "replicate" => req.header("authorization", format!("Token {key}")),
        // ElevenLabs uses xi-api-key.
        "elevenlabs" => req.header("xi-api-key", key),
        // Fal.ai (when present) uses `Authorization: Key <key>`.
        // We don't ship a catalog URL for fal_ai today but list it
        // here for symmetry with the operator-editable flow.
        "fal_ai" => req.header("authorization", format!("Key {key}")),
        // Default: OpenAI-style Bearer. Covers OpenAI, OpenRouter,
        // Mistral, Groq, Together, Fireworks, DeepInfra, xAI,
        // Cohere, AI21, Cerebras, SambaNova, DeepSeek, Moonshot,
        // Ollama (when an operator put a key on it), vLLM, LM Studio.
        _ => req.bearer_auth(key),
    }
}

/// Best-effort listing summary. Recognises the common shapes:
///
///   * OpenAI / most: `{ "data": [ { "id": "..." }, … ] }`
///   * Gemini: `{ "models": [ { "name": "models/..." }, … ] }`
///   * Anthropic: `{ "data": [ { "id": "...", "display_name": "..." } ] }`
///
/// When the shape doesn't match anything we know, returns `(None, None)`
/// — caller treats this as "succeeded but couldn't verify the name".
fn summarise_listing(
    body: &serde_json::Value,
    model_name: Option<&str>,
) -> (Option<usize>, Option<bool>) {
    // Pick whichever array key the provider used.
    let array = body
        .get("data")
        .or_else(|| body.get("models"))
        .and_then(|v| v.as_array());
    let Some(items) = array else {
        return (None, None);
    };
    let count = items.len();
    let Some(needle) = model_name else {
        return (Some(count), None);
    };
    let needle_norm = normalise_model_id(needle);
    let found = items.iter().any(|item| {
        // Try a handful of likely id keys. `id` covers OpenAI and
        // most clones; `name` covers Gemini's `models/<id>` form;
        // `model` is rare but seen in some providers.
        let candidates: [Option<&str>; 3] = [
            item.get("id").and_then(|v| v.as_str()),
            item.get("name").and_then(|v| v.as_str()),
            item.get("model").and_then(|v| v.as_str()),
        ];
        candidates
            .iter()
            .filter_map(|c| c.as_ref())
            .any(|raw| normalise_model_id(raw) == needle_norm)
    });
    (Some(count), Some(found))
}

/// Strip provider-specific prefixes when comparing model IDs.
/// Gemini returns `models/gemini-1.5-pro`; the model row stores
/// just `gemini-1.5-pro`. Without the trim, presence checks
/// false-negative on Gemini.
fn normalise_model_id(raw: &str) -> String {
    raw.trim_start_matches("models/").to_lowercase()
}

fn truncate(s: &str, n: usize) -> String {
    if s.len() <= n {
        s.to_string()
    } else {
        format!("{}…", &s[..n])
    }
}

/// Convenience: write a `proxy_metric` row tagged
/// `endpoint_category="health_probe_catalog"`. The route handler
/// calls this after the probe so Mission Control's per-category
/// breakdown can attribute probe traffic correctly. Background
/// fire-and-forget — failure to write a metric must not affect
/// the probe response the operator sees.
pub fn write_probe_metric(
    background_writer: &crate::services::background_writer::BackgroundWriter,
    db: &PgPool,
    auth: &crate::models::auth::AuthContext,
    model_id: &str,
    result: &CatalogProbeResult,
    correlation_id: &str,
) {
    use crate::services::proxy_logger;
    let metric = proxy_logger::build_proxy_metric(
        "llm",
        auth,
        "GET",
        "/v1/proxy/llm/admin/probe-catalog",
        "health_probe_catalog",
        Some(model_id),
        result.status_code.unwrap_or(0),
        result.latency_ms.unwrap_or(0.0),
        result.success,
        correlation_id,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    );
    background_writer.write_proxy_metric(db.clone(), metric);
}

/// Centralized probe outcome writer — writes to ALL three counters
/// (proxy_metrics, usage_logs, model_metrics) for a single probe in
/// one place. The whole point of this is so that a probe (catalog,
/// endpoint, or synthetic) increments the per-model row counter,
/// Mission Control's total, and the per-group Usage view by exactly
/// `+1` apiece, no matter which probe path was taken or whether the
/// underlying upstream returned 200 / 401 / 502 / circuit-open.
///
/// Before this helper existed, each route had its own metric-write
/// fan-out and the failure paths (no api_key, circuit open, fallback
/// rejected, synthetic 400 short-circuit) all skipped one or more
/// of the writes inconsistently. Operators saw 8/8/8/0/0/0/7/7
/// across rows that had all received the same number of probe
/// cycles — a usability disaster.
///
/// Now: every probe path returns from gateway-control, gateway-
/// control posts the outcome to
/// `POST /v1/proxy/llm/admin/{model_id}/record-probe-result`, and
/// THIS helper writes all three counters atomically. One probe →
/// one record-probe call → one +1 in every view, every time.
pub fn record_probe_outcome(
    state: &crate::services::SharedState,
    auth: &crate::models::auth::AuthContext,
    target_group: Option<&crate::models::db::ResourceGroup>,
    tenant_id: &str,
    model_id: &str,
    model_name: &str,
    provider: &str,
    success: bool,
    status_code: u16,
    latency_ms: f64,
    probe_type: &str,
    correlation_id: &str,
) {
    use crate::services::proxy_logger;

    // 1. proxy_metrics — Mission Control's "Total Requests" KPI.
    //    Tagged with endpoint_category="health_probe_<type>" so the
    //    per-category breakdown chart attributes probe traffic
    //    correctly without polluting LLM/MCP/A2A/Portal counts.
    let category = match probe_type {
        "catalog" => "health_probe_catalog",
        "endpoint" => "health_probe_endpoint",
        "synthetic" => "health_probe_synthetic",
        _ => "health_probe",
    };
    let endpoint_path = format!("/v1/proxy/llm/admin/{model_id}/probe");
    let metric = proxy_logger::build_proxy_metric(
        "llm",
        auth,
        "POST",
        &endpoint_path,
        category,
        Some(model_id),
        status_code,
        latency_ms,
        success,
        correlation_id,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    );
    state
        .background_writer
        .write_proxy_metric(state.db.clone(), metric);

    // 2. usage_logs + 3. model_metrics — write_llm_usage_log handles
    //    BOTH as a side effect (the usage row goes to the
    //    background writer; the counter increment goes to the
    //    in-process aggregator that flushes to llm_model.total_requests
    //    every 5 s).
    //
    //    A SUCCESSFUL endpoint probe was a real generation, and the
    //    route that served it already wrote its usage row with the real
    //    tokens and cost (billed to Unbound). Writing a zero-cost row here
    //    as well is what made platform spend read $0 while the provider
    //    billed every probe. Catalog and synthetic probes, and failed
    //    endpoint probes (routes record usage on success only), keep this
    //    one free row so every probe is still counted.
    if probe_type == "endpoint" && success {
        return;
    }
    crate::services::usage::write_llm_usage_log(
        &state.usage_state,
        &state.background_writer,
        &state.model_metrics,
        state.db.clone(),
        tenant_id,
        auth,
        target_group,
        model_id,
        model_name,
        provider,
        Some(0),
        Some(0),
        Some(0.0),
        0.0,
        0.0,
        &endpoint_path,
        correlation_id,
        status_code,
        latency_ms,
    );
}

/// Header sentinel that gateway-control stamps on every probe
/// request. Routes use this to skip their own metric-write
/// fan-out when a request is a probe — `record_probe_outcome`
/// (called centrally from gateway-control) is the single source
/// of truth for probe accounting.
pub const HEALTH_PROBE_HEADER: &str = "x-brutor-internal-health-check";

/// Whether a request is the platform's own health probe.
///
/// The header alone proves nothing: any caller can set it, and a probe
/// skips metric and usage-ledger writes (multimodal spend went unrecorded for
/// a caller that added it). It counts only on the platform service key, which
/// a tenant admin cannot mint.
pub fn is_health_probe_request(
    headers: &axum::http::HeaderMap,
    auth: &crate::models::auth::AuthContext,
) -> bool {
    auth.api_key_is_platform && has_probe_header(headers)
}

fn has_probe_header(headers: &axum::http::HeaderMap) -> bool {
    headers
        .get(HEALTH_PROBE_HEADER)
        .and_then(|h| h.to_str().ok())
        .map(|v| !v.trim().is_empty() && v.trim() != "0")
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn resolves_url_passes_through_when_no_sentinel() {
        let out = resolve_catalog_url("https://api.openai.com/v1/models", None).unwrap();
        assert_eq!(out, "https://api.openai.com/v1/models");
    }

    #[test]
    fn resolves_sentinel_with_model_api_url() {
        let out = resolve_catalog_url(
            "<MODEL_API_URL>/v1/models",
            Some("https://vllm.example/v1/"),
        )
        .unwrap();
        // Trailing slash on the model URL stripped — would otherwise
        // produce //v1/models.
        assert_eq!(out, "https://vllm.example/v1/v1/models");
    }

    #[test]
    fn rejects_sentinel_without_model_api_url() {
        let err = resolve_catalog_url("<MODEL_API_URL>/v1/models", None).unwrap_err();
        matches!(err, CatalogProbeError::SentinelUnresolvable);
    }

    #[test]
    fn summarise_openai_style_body_finds_known_model() {
        let body = json!({
            "object": "list",
            "data": [
                { "id": "gpt-4o" },
                { "id": "gpt-4o-mini" },
                { "id": "dall-e-3" },
            ],
        });
        let (count, found) = summarise_listing(&body, Some("gpt-4o"));
        assert_eq!(count, Some(3));
        assert_eq!(found, Some(true));
    }

    #[test]
    fn summarise_openai_style_body_misses_unknown_model() {
        let body = json!({"data": [{"id": "gpt-3.5-turbo"}]});
        let (count, found) = summarise_listing(&body, Some("gpt-4o"));
        assert_eq!(count, Some(1));
        assert_eq!(found, Some(false));
    }

    #[test]
    fn summarise_gemini_style_body_strips_prefix() {
        // Gemini's listing returns `name: "models/gemini-1.5-pro"`.
        // The model row stores `gemini-1.5-pro`. Probe must
        // normalise both sides for the presence check.
        let body = json!({
            "models": [
                { "name": "models/gemini-1.5-pro" },
                { "name": "models/gemini-1.5-flash" },
            ],
        });
        let (count, found) = summarise_listing(&body, Some("gemini-1.5-pro"));
        assert_eq!(count, Some(2));
        assert_eq!(found, Some(true));
    }

    #[test]
    fn summarise_returns_count_only_when_no_needle_provided() {
        let body = json!({"data": [{"id": "a"}, {"id": "b"}]});
        let (count, found) = summarise_listing(&body, None);
        assert_eq!(count, Some(2));
        assert_eq!(found, None);
    }

    #[test]
    fn summarise_returns_nones_for_unknown_shape() {
        let body = json!({"completely": "unexpected"});
        let (count, found) = summarise_listing(&body, Some("anything"));
        assert_eq!(count, None);
        assert_eq!(found, None);
    }

    #[test]
    fn truncate_short_string_unchanged() {
        assert_eq!(truncate("abc", 10), "abc");
    }

    #[test]
    fn truncate_long_string_appended_ellipsis() {
        assert_eq!(truncate("abcdefghij", 5), "abcde…");
    }

    #[test]
    fn normalise_model_id_strips_models_prefix_and_lowercases() {
        assert_eq!(
            normalise_model_id("models/Gemini-1.5-PRO"),
            "gemini-1.5-pro"
        );
        assert_eq!(normalise_model_id("gpt-4o"), "gpt-4o");
    }
}
