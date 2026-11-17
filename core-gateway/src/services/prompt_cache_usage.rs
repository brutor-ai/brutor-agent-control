// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Cached-token accounting (Community)
//!
//! Providers cache prompt prefixes and say so in the usage they report:
//! Anthropic counts cache writes and reads beside the input tokens, OpenAI
//! and Gemini count the cached part inside the prompt. They report it
//! whoever asked for the caching — OpenAI caches by itself, and a caller may
//! send its own `cache_control` — so reading those figures is accounting,
//! not the prompt-caching feature: they go to the audit row
//! (`prompt_cache_read_tokens` / `prompt_cache_write_tokens`, which are in
//! the hash-chained evidence record), to the metrics, and into the size of
//! the context a call sent ([`context_tokens`]).
//!
//! The feature — choosing what to mark for the cache and pinning a
//! conversation to the model whose cache is warm — is behind the hook in
//! [`crate::services::prompt_caching`]. Until that step this
//! file's contents lived inside the feature's module; they were split out so
//! that an installation without the feature still accounts for what its
//! providers report.
//!
//! [`PromptCacheMechanism`] is how a provider caches. It is a fact about the
//! provider (it decides where the usage is read from and what a cached token
//! costs), so it lives here, and the feature reads it to choose how to mark.
//! The saving ([`PromptCacheUsage::savings_usd`]) is computed from the
//! provider's published discount for a cached token and the model's price,
//! never from a policy.

use serde_json::Value;

use crate::models::db::LlmModel;

// ---------------------------------------------------------------------------
// Mechanism (derived from provider name — model-intrinsic, not operator-set)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PromptCacheMechanism {
    /// No prompt caching available for this provider.
    None,
    /// Anthropic / Bedrock Claude / Vertex Claude: explicit markers.
    CacheControlBreakpoints,
    /// OpenAI / Azure OpenAI: implicit, prefix ≥ 1024 tokens.
    AutomaticPrefix,
    /// Google Gemini: separate `cachedContents` lifecycle.
    CachedContents,
}

impl PromptCacheMechanism {
    /// Maximum `cache_control` breakpoints the provider accepts per
    /// request. Anthropic: 4. Others: N/A.
    pub fn max_breakpoints(&self) -> usize {
        match self {
            Self::CacheControlBreakpoints => 4,
            _ => 0,
        }
    }

    /// Read-side pricing multiplier vs uncached input. Approximate
    /// and forward-looking — providers shift these; final $-saved is
    /// computed from the catalog's cost_per_input_token and the
    /// returned counters, so this is only used for a rough estimate
    /// when the provider doesn't surface explicit cached-tokens.
    pub fn read_multiplier(&self) -> f64 {
        match self {
            Self::CacheControlBreakpoints => 0.10,
            Self::AutomaticPrefix => 0.50,
            Self::CachedContents => 0.25,
            Self::None => 1.0,
        }
    }

    /// Write-side pricing multiplier. Only applies to `creation`
    /// tokens — the one-time cost of warming the cache.
    pub fn write_multiplier(&self) -> f64 {
        match self {
            Self::CacheControlBreakpoints => 1.25,
            // OpenAI & Gemini don't charge a write premium today.
            Self::AutomaticPrefix => 1.0,
            Self::CachedContents => 1.0,
            Self::None => 1.0,
        }
    }
}

/// Resolve the prompt-cache mechanism for a (provider, model_name)
/// pair. The model name matters for Vertex AI, which serves both Claude
/// (cache_control breakpoints) and Gemini (cachedContents). Pass the
/// raw `LlmModel.model_name` here — case is preserved but the prefix
/// match is case-insensitive on the gemini detection.
pub fn mechanism_for_provider(provider: &str, model_name: &str) -> PromptCacheMechanism {
    let model_lc = model_name.to_ascii_lowercase();
    match provider {
        // Anthropic family.
        "anthropic" => PromptCacheMechanism::CacheControlBreakpoints,

        // Bedrock — dispatch on model family.
        //
        // Brutor's current Bedrock adapter emits Anthropic-native
        // InvokeModel body shape, so `cache_control` markers work
        // end-to-end for Claude models on Bedrock. Non-Claude Bedrock
        // models (Amazon Nova, Titan, Mistral-on-Bedrock, etc.) use
        // the Converse API with a different cache vocabulary
        // (`cachePoint` blocks). The Converse adapter isn't wired
        // yet, so emitting `cache_control` for those families would
        // produce invalid markers the provider rejects — safer to
        // disable caching for them until the Converse path lands.
        //
        // When Bedrock Converse support is added, this dispatch
        // should branch to `BedrockCachePoint` and a new
        // `llm_adapter::bedrock_cache` module handles the emission.
        p if p.starts_with("bedrock") => {
            if model_lc.contains("anthropic") || model_lc.contains("claude") {
                PromptCacheMechanism::CacheControlBreakpoints
            } else {
                PromptCacheMechanism::None
            }
        }

        // Vertex AI: Claude (cache_control) vs Gemini (cachedContents).
        // We dispatch on the model name because the provider field
        // alone is ambiguous — `vertex_ai/claude-opus-4` and
        // `vertex_ai/gemini-2.5-pro` arrive with the same `provider`.
        p if p.starts_with("vertex_ai") || p == "vertex" => {
            if model_lc.contains("gemini") {
                PromptCacheMechanism::CachedContents
            } else {
                // Default to Claude-style breakpoints; covers
                // claude-*, anthropic.* and any future Anthropic models
                // surfaced through Vertex.
                PromptCacheMechanism::CacheControlBreakpoints
            }
        }

        // OpenAI family.
        "openai" | "azure" | "azure_ai" => PromptCacheMechanism::AutomaticPrefix,

        // Google Gemini — cachedContents lifecycle. Gemini's minimum
        // cacheable size is 32 768 tokens (model-dependent), so this
        // mechanism only lights up on heavy tool/system-prompt-heavy
        // deployments. For lighter traffic the lookup-miss fast path
        // returns immediately without trying to create a cache.
        "gemini" | "google_ai_studio" => PromptCacheMechanism::CachedContents,

        // Everything else (Ollama, Replicate, Cohere, Mistral,
        // Together, Fireworks, Groq, Perplexity, xAI, Deepseek,
        // Cloudflare, AI21, Databricks) either doesn't support prompt
        // caching or doesn't publish a stable interface.
        _ => PromptCacheMechanism::None,
    }
}

/// How many tokens of context one LLM call sent the model, cached or not.
///
/// The providers report this differently, and the difference is the whole
/// reason this function exists. Anthropic's `input_tokens` (direct, Bedrock,
/// Vertex) counts only the uncached part: cache reads and writes are reported
/// BESIDE it. OpenAI's and Gemini's prompt count already INCLUDES the cached
/// part. Reading `llm_prompt_tokens` alone would show a well-cached Anthropic
/// agent a tiny context, shrinking further as its caching improves.
///
/// `None` means not measured (no usage reported), never zero: a stream closed
/// without usage has an unknown context, not an empty one.
pub fn context_tokens(
    provider: Option<&str>,
    model: Option<&str>,
    prompt_tokens: Option<i32>,
    cache_write_tokens: Option<i32>,
    cache_read_tokens: Option<i32>,
) -> Option<i32> {
    let reported_beside_prompt = matches!(
        mechanism_for_provider(provider.unwrap_or(""), model.unwrap_or("")),
        PromptCacheMechanism::CacheControlBreakpoints
    );
    let total = if reported_beside_prompt {
        prompt_tokens
            .unwrap_or(0)
            .saturating_add(cache_write_tokens.unwrap_or(0))
            .saturating_add(cache_read_tokens.unwrap_or(0))
    } else {
        prompt_tokens.unwrap_or(0)
    };
    (total > 0).then_some(total)
}

#[derive(Debug, Clone, Default)]
pub struct PromptCacheUsage {
    /// Tokens written to provider cache this request (one-time
    /// warming cost).
    pub write_tokens: i32,
    /// Tokens served from provider cache this request.
    pub read_tokens: i32,
    /// Net $ saved vs uncached. Positive = we came out ahead.
    pub savings_usd: f64,
}

// ---------------------------------------------------------------------------
// Response-side: extract usage
// ---------------------------------------------------------------------------

/// Pull prompt-cache usage counters from a provider response. Works
/// across all mechanisms — each provider exposes these under a
/// different path but semantically they're the same three numbers.
pub fn extract_usage_from_response(
    response: &Value,
    mechanism: PromptCacheMechanism,
    model: &LlmModel,
) -> PromptCacheUsage {
    match mechanism {
        PromptCacheMechanism::CacheControlBreakpoints => anthropic_usage(response, model),
        PromptCacheMechanism::AutomaticPrefix => openai_usage(response, model),
        PromptCacheMechanism::CachedContents => gemini_usage(response, model),
        PromptCacheMechanism::None => PromptCacheUsage::default(),
    }
}

/// Build a `PromptCacheUsage` from totals accumulated over a streaming
/// response. Streaming handlers walk the SSE events themselves and
/// aggregate the cache counters as they pass — this helper attaches
/// the provider-specific savings computation so callers don't have to
/// know the multipliers.
///
/// Returns the zero-usage when the mechanism is `None` or both
/// counters are zero (non-caching call); callers can use that to skip
/// writing the prompt-cache log fields entirely.
pub fn usage_from_stream_totals(
    write_tokens: i32,
    read_tokens: i32,
    mechanism: PromptCacheMechanism,
    model: &LlmModel,
) -> PromptCacheUsage {
    if matches!(mechanism, PromptCacheMechanism::None) || (write_tokens == 0 && read_tokens == 0) {
        return PromptCacheUsage::default();
    }
    PromptCacheUsage {
        write_tokens,
        read_tokens,
        savings_usd: compute_savings_usd(write_tokens, read_tokens, mechanism, model),
    }
}

// Per-provider reading of the usage block. `extract_usage_from_response`
// above dispatches by mechanism.

/// Pull Anthropic-family cache usage counters from a response. Works
/// whether the response is in Anthropic's native shape or has already
/// been normalized to OpenAI shape — both preserve the same
/// `cache_creation_input_tokens` / `cache_read_input_tokens` keys.
fn anthropic_usage(response: &Value, model: &LlmModel) -> PromptCacheUsage {
    let usage = match response.get("usage") {
        Some(u) => u,
        None => return PromptCacheUsage::default(),
    };

    let write_tokens = usage
        .get("cache_creation_input_tokens")
        .and_then(|v| v.as_i64())
        .unwrap_or(0) as i32;
    let read_tokens = usage
        .get("cache_read_input_tokens")
        .and_then(|v| v.as_i64())
        .unwrap_or(0) as i32;

    PromptCacheUsage {
        write_tokens,
        read_tokens,
        savings_usd: compute_savings_usd(
            write_tokens,
            read_tokens,
            PromptCacheMechanism::CacheControlBreakpoints,
            model,
        ),
    }
}

/// Pull OpenAI-family cache usage counters from a response.
/// `cached_tokens` is the only signal — OpenAI doesn't charge a write
/// premium so `write_tokens` is always 0 for this mechanism.
fn openai_usage(response: &Value, model: &LlmModel) -> PromptCacheUsage {
    // Chat Completions reports the hit under
    // `usage.prompt_tokens_details.cached_tokens`; the Responses API
    // reports the same number under `usage.input_tokens_details.cached_tokens`.
    // Both come through this adapter, so read whichever shape is present.
    let read_tokens = response
        .get("usage")
        .and_then(|u| {
            u.pointer("/prompt_tokens_details/cached_tokens")
                .or_else(|| u.pointer("/input_tokens_details/cached_tokens"))
        })
        .and_then(|v| v.as_i64())
        .unwrap_or(0) as i32;

    PromptCacheUsage {
        write_tokens: 0,
        read_tokens,
        savings_usd: compute_savings_usd(
            0,
            read_tokens,
            PromptCacheMechanism::AutomaticPrefix,
            model,
        ),
    }
}

/// Pull Gemini cache usage counters from a response. The
/// `cachedContentTokenCount` field only appears when the request
/// referenced a cached resource.
fn gemini_usage(response: &Value, model: &LlmModel) -> PromptCacheUsage {
    let read_tokens = response
        .get("usageMetadata")
        .and_then(|u| u.get("cachedContentTokenCount"))
        .and_then(|v| v.as_i64())
        .unwrap_or(0) as i32;

    PromptCacheUsage {
        write_tokens: 0,
        read_tokens,
        savings_usd: compute_savings_usd(
            0,
            read_tokens,
            PromptCacheMechanism::CachedContents,
            model,
        ),
    }
}

fn compute_savings_usd(
    write_tokens: i32,
    read_tokens: i32,
    mechanism: PromptCacheMechanism,
    model: &LlmModel,
) -> f64 {
    // `cost_per_input_token` in the catalog is **USD per 1 MILLION
    // input tokens** — the column comment in `llm_model.py:220` is
    // authoritative ("USD per 1M input tokens"), the seed values
    // in `provider_data/*.py` are in dollars-per-million (e.g.
    // Vertex AI seeds 1.25), and the regular cost path uses
    // `llm_helpers::estimate_llm_cost` which divides by
    // 1_000_000. An earlier version of this function treated the
    // column as per-token and produced savings figures inflated
    // by 1,000,000× — Mission Control's "$ saved" KPI showed
    // $45.4K for ~52K cached tokens, when the real number is
    // ~$0.045. Fixed by dividing by 1e6 to align with every other
    // cost calculation in the gateway.
    let per_million_tokens = model.cost_per_input_token.unwrap_or(0.0);
    if per_million_tokens <= 0.0 {
        return 0.0;
    }
    // Net = (what we'd have paid uncached) - (what we paid with cache).
    // Uncached cost for read_tokens = read_tokens × 1.0
    // Cached cost     for read_tokens = read_tokens × read_multiplier
    // Write premium penalty          = write_tokens × (write_multiplier - 1.0)
    let read_savings_tokens = read_tokens as f64 * (1.0 - mechanism.read_multiplier());
    let write_penalty_tokens = write_tokens as f64 * (mechanism.write_multiplier() - 1.0);
    (read_savings_tokens - write_penalty_tokens) * per_million_tokens / 1_000_000.0
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::llm_adapter::make_test_model;
    use serde_json::json;

    /// A model priced like Claude Sonnet: $3 per million input tokens (the
    /// column is per million, not per token).
    fn fake_model(provider: &str) -> LlmModel {
        let mut model = make_test_model(provider, "test");
        model.cost_per_input_token = Some(3.0);
        model.cost_per_output_token = Some(15.0);
        model
    }

    #[test]
    fn context_adds_the_cache_only_where_the_provider_reports_it_apart() {
        // (provider, model) with Anthropic's shape: `input_tokens` is the
        // uncached part. 100 + 300 written + 5,000 read = 5,400.
        for (provider, model) in [
            ("anthropic", "claude-sonnet-4-5"),
            ("bedrock", "anthropic.claude-sonnet-4-5"),
            ("vertex_ai", "claude-sonnet-4-5"),
        ] {
            assert_eq!(
                context_tokens(
                    Some(provider),
                    Some(model),
                    Some(100),
                    Some(300),
                    Some(5_000)
                ),
                Some(5_400),
                "{provider}"
            );
        }
        // OpenAI's and Gemini's prompt count already includes the cached part.
        for (provider, model) in [
            ("openai", "gpt-5"),
            ("azure", "gpt-5"),
            ("gemini", "gemini-2.5-pro"),
            ("vertex_ai", "gemini-2.5-pro"),
            ("mistral", "mistral-large"),
        ] {
            assert_eq!(
                context_tokens(Some(provider), Some(model), Some(5_400), None, Some(5_000)),
                Some(5_400),
                "{provider}"
            );
        }
    }

    #[test]
    fn a_well_cached_context_reads_the_same_as_an_uncached_one() {
        // The class of bug this function exists for: better caching must not
        // look like a smaller context.
        let cold = context_tokens(Some("anthropic"), Some("claude"), Some(5_400), None, None);
        let warm = context_tokens(
            Some("anthropic"),
            Some("claude"),
            Some(10),
            None,
            Some(5_390),
        );
        assert_eq!(cold, warm);
    }

    #[test]
    fn no_reported_usage_is_not_measured_rather_than_zero() {
        assert_eq!(
            context_tokens(Some("openai"), Some("gpt-5"), None, None, None),
            None
        );
        assert_eq!(
            context_tokens(Some("openai"), Some("gpt-5"), Some(0), None, None),
            None
        );
        assert_eq!(
            context_tokens(Some("anthropic"), Some("claude"), None, None, None),
            None
        );
        assert_eq!(context_tokens(None, None, None, None, None), None);
    }

    #[test]
    fn a_closing_frame_with_only_cache_counters_is_still_measured() {
        assert_eq!(
            context_tokens(Some("anthropic"), Some("claude"), None, None, Some(700)),
            Some(700)
        );
    }

    #[test]
    fn context_cannot_overflow_into_a_negative_size() {
        assert_eq!(
            context_tokens(
                Some("anthropic"),
                Some("claude"),
                Some(i32::MAX),
                Some(i32::MAX),
                None
            ),
            Some(i32::MAX)
        );
    }

    #[test]
    fn mechanism_detection() {
        assert_eq!(
            mechanism_for_provider("anthropic", "claude-opus-4"),
            PromptCacheMechanism::CacheControlBreakpoints,
        );
        assert_eq!(
            mechanism_for_provider("bedrock", "anthropic.claude-3-5-sonnet"),
            PromptCacheMechanism::CacheControlBreakpoints,
        );
        // Bedrock Claude variants (different model_name prefixes
        // operators use) — all should resolve to cache_control.
        assert_eq!(
            mechanism_for_provider("bedrock", "claude-3-haiku"),
            PromptCacheMechanism::CacheControlBreakpoints,
        );
        // Bedrock non-Claude (Nova, Titan, etc.) — Converse API uses
        // a different cache vocabulary (cachePoint blocks). Since the
        // Converse adapter isn't wired yet, we safely disable caching
        // for these families rather than emit markers the provider
        // rejects.
        assert_eq!(
            mechanism_for_provider("bedrock", "amazon.nova-pro-v1:0"),
            PromptCacheMechanism::None,
        );
        assert_eq!(
            mechanism_for_provider("bedrock", "amazon.titan-text-express"),
            PromptCacheMechanism::None,
        );
        assert_eq!(
            mechanism_for_provider("bedrock_converse", "mistral.mistral-large"),
            PromptCacheMechanism::None,
        );
        // Vertex Claude → cache_control breakpoints.
        assert_eq!(
            mechanism_for_provider("vertex_ai", "claude-opus-4"),
            PromptCacheMechanism::CacheControlBreakpoints,
        );
        assert_eq!(
            mechanism_for_provider("openai", "gpt-5"),
            PromptCacheMechanism::AutomaticPrefix,
        );
        assert_eq!(
            mechanism_for_provider("azure", "gpt-4o"),
            PromptCacheMechanism::AutomaticPrefix,
        );
        assert_eq!(
            mechanism_for_provider("ollama", "llama3"),
            PromptCacheMechanism::None,
        );
        assert_eq!(
            mechanism_for_provider("gemini", "gemini-2.5-pro"),
            PromptCacheMechanism::CachedContents,
        );
        assert_eq!(
            mechanism_for_provider("google_ai_studio", "gemini-2.5-flash"),
            PromptCacheMechanism::CachedContents,
        );
    }

    #[test]
    fn vertex_gemini_routes_to_cached_contents() {
        // Regression for Gap #3: gemini-* on vertex_ai must use the
        // cachedContents lifecycle, not Anthropic's cache_control
        // breakpoints. Without the model_name dispatch we'd silently
        // inject Anthropic markers into a Gemini request and report
        // zero cache savings every time.
        assert_eq!(
            mechanism_for_provider("vertex_ai", "gemini-2.5-pro"),
            PromptCacheMechanism::CachedContents,
        );
        assert_eq!(
            mechanism_for_provider("vertex_ai", "gemini-2.5-flash"),
            PromptCacheMechanism::CachedContents,
        );
        // Case-insensitive match on the model substring.
        assert_eq!(
            mechanism_for_provider("vertex_ai", "publishers/google/models/Gemini-2.5-Pro"),
            PromptCacheMechanism::CachedContents,
        );
        // Vertex Claude must still resolve to breakpoints.
        assert_eq!(
            mechanism_for_provider("vertex_ai", "claude-opus-4-1@20250805"),
            PromptCacheMechanism::CacheControlBreakpoints,
        );
        assert_eq!(
            mechanism_for_provider("vertex", "claude-3-5-sonnet"),
            PromptCacheMechanism::CacheControlBreakpoints,
        );
    }

    #[test]
    fn extract_anthropic_usage_from_normalised_response() {
        let model = fake_model("anthropic");
        let resp = json!({
            "choices": [{"message": {"content": "hi"}}],
            "usage": {
                "prompt_tokens": 523,
                "completion_tokens": 42,
                "cache_creation_input_tokens": 2847,
                "cache_read_input_tokens": 18203
            }
        });
        let usage = extract_usage_from_response(
            &resp,
            PromptCacheMechanism::CacheControlBreakpoints,
            &model,
        );
        assert_eq!(usage.write_tokens, 2847);
        assert_eq!(usage.read_tokens, 18203);
        // 18203 × (1 - 0.10) − 2847 × (1.25 - 1.0) = 16382.7 - 711.75 ≈ 15670.95
        // × 0.000003 = 0.047...
        assert!(usage.savings_usd > 0.04 && usage.savings_usd < 0.05);
    }

    #[test]
    fn extract_openai_usage_from_cached_tokens() {
        let model = fake_model("openai");
        let resp = json!({
            "usage": {
                "prompt_tokens": 21050,
                "prompt_tokens_details": {"cached_tokens": 18432},
                "completion_tokens": 312
            }
        });
        let usage =
            extract_usage_from_response(&resp, PromptCacheMechanism::AutomaticPrefix, &model);
        assert_eq!(usage.write_tokens, 0);
        assert_eq!(usage.read_tokens, 18432);
        assert!(usage.savings_usd > 0.0);
    }

    #[test]
    fn extract_gemini_usage_via_dispatcher() {
        let model = fake_model("gemini");
        let resp = json!({
            "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
            "usageMetadata": {
                "promptTokenCount": 40_000,
                "cachedContentTokenCount": 32_500,
                "candidatesTokenCount": 412
            }
        });
        let usage =
            extract_usage_from_response(&resp, PromptCacheMechanism::CachedContents, &model);
        assert_eq!(usage.write_tokens, 0);
        assert_eq!(usage.read_tokens, 32_500);
        assert!(usage.savings_usd > 0.0);
    }

    #[test]
    fn openai_zero_cached_tokens_returns_zero_usage() {
        // Sanity: OpenAI prefix caching reports zero on cold prompts.
        // Make sure the extractor handles that without producing
        // negative or NaN savings.
        let model = fake_model("openai");
        let resp = json!({
            "usage": {
                "prompt_tokens": 1024,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens": 128
            }
        });
        let usage =
            extract_usage_from_response(&resp, PromptCacheMechanism::AutomaticPrefix, &model);
        assert_eq!(usage.read_tokens, 0);
        assert_eq!(usage.write_tokens, 0);
        assert_eq!(usage.savings_usd, 0.0);
    }

    #[test]
    fn openai_missing_prompt_tokens_details_returns_zero() {
        // Older OpenAI-compatible providers don't include
        // `prompt_tokens_details` at all. The extractor must tolerate
        // the missing key — not panic.
        let model = fake_model("openai");
        let resp = json!({
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50
            }
        });
        let usage =
            extract_usage_from_response(&resp, PromptCacheMechanism::AutomaticPrefix, &model);
        assert_eq!(usage.read_tokens, 0);
    }

    #[test]
    fn anthropic_missing_cache_fields_returns_zero() {
        let model = fake_model("anthropic");
        let resp = json!({
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50
            }
        });
        let usage = extract_usage_from_response(
            &resp,
            PromptCacheMechanism::CacheControlBreakpoints,
            &model,
        );
        assert_eq!(usage.read_tokens, 0);
        assert_eq!(usage.write_tokens, 0);
    }

    #[test]
    fn savings_computation_handles_write_only_warm_call() {
        // First call ever — only write_tokens reported. Net savings
        // should be NEGATIVE (we paid the write premium to set up
        // the cache, no reads to amortise yet) but bounded.
        let model = fake_model("anthropic");
        let resp = json!({
            "usage": {
                "cache_creation_input_tokens": 5000,
                "cache_read_input_tokens": 0
            }
        });
        let usage = extract_usage_from_response(
            &resp,
            PromptCacheMechanism::CacheControlBreakpoints,
            &model,
        );
        assert_eq!(usage.write_tokens, 5000);
        assert_eq!(usage.read_tokens, 0);
        // Anthropic write-multiplier is 1.25. Per the seed:
        // cost_per_input_token = $3 per million tokens.
        //   penalty = 5000 × (1.25 - 1.0) × $3 / 1_000_000
        //           = 5000 × 0.25 × 3 / 1e6
        //           = $0.00375
        // → savings is negative (we paid the write premium and
        // have nothing to amortise it against yet), small in
        // absolute terms.
        assert!(
            usage.savings_usd < 0.0 && usage.savings_usd > -0.01,
            "expected small negative savings on warm-only call, got {}",
            usage.savings_usd,
        );
    }

    #[test]
    fn no_cost_per_token_means_zero_savings() {
        // Catalog row with cost_per_input_token=None. The savings
        // computation must short-circuit to 0.0 rather than dividing
        // by zero.
        let mut model = fake_model("anthropic");
        model.cost_per_input_token = None;
        let resp = json!({
            "usage": {
                "cache_creation_input_tokens": 1000,
                "cache_read_input_tokens": 5000
            }
        });
        let usage = extract_usage_from_response(
            &resp,
            PromptCacheMechanism::CacheControlBreakpoints,
            &model,
        );
        assert_eq!(usage.savings_usd, 0.0);
    }

    #[test]
    fn savings_uses_per_million_pricing_not_per_token() {
        // Regression for the "Mission Control shows $45.4K saved
        // for 52K cached tokens" bug. The catalog stores
        // `cost_per_input_token` as **USD per 1M tokens**; this
        // function previously treated it as USD per single token,
        // inflating savings by 1,000,000×.
        //
        // Realistic shape: Anthropic Claude Sonnet on a warm
        // request. Write a 1k-token cache block, then read 50k
        // tokens from cache.
        //   - Cost-per-million input: $3
        //   - read_multiplier:   0.1 (90% off cached reads)
        //   - write_multiplier:  1.25 (25% premium on writes)
        //
        // Net savings (per the function):
        //   read_savings_tokens  = 50000 × (1.0 - 0.1) = 45000
        //   write_penalty_tokens =  1000 × (1.25 - 1.0) =   250
        //   net_tokens           = 44750
        //   savings_usd          = 44750 × $3 / 1_000_000
        //                        = $0.13425
        //
        // The pre-fix code computed 44750 × 3 = $134,250 — three
        // orders of magnitude off our $0.045 production observation
        // but the SHAPE of the bug. The test pins the realistic
        // result so a future regression flips it back loudly.
        let model = fake_model("anthropic");
        let resp = json!({
            "usage": {
                "cache_creation_input_tokens": 1000,
                "cache_read_input_tokens": 50000
            }
        });
        let usage = extract_usage_from_response(
            &resp,
            PromptCacheMechanism::CacheControlBreakpoints,
            &model,
        );

        // Should be a small positive number, NOT thousands.
        // Bounds check: any plausible per-million pricing puts
        // the answer well under $1 for this volume.
        assert!(
            usage.savings_usd > 0.0,
            "expected positive savings on cache-read-heavy call, got {}",
            usage.savings_usd
        );
        assert!(
            usage.savings_usd < 1.0,
            "savings should be cents-scale for 50k cached tokens at $3/M pricing, got {} \
             — looks like the /1M divisor regressed",
            usage.savings_usd
        );

        // Tight assertion on the exact value (44750 × 3 / 1e6 = $0.13425).
        let expected = (50000.0 * 0.9 - 1000.0 * 0.25) * 3.0 / 1_000_000.0;
        assert!(
            (usage.savings_usd - expected).abs() < 1e-9,
            "expected {expected} (per-million pricing), got {}",
            usage.savings_usd
        );
    }

    #[test]
    fn savings_for_openai_auto_prefix_matches_mission_control_scale() {
        // The reported production case: 51.8K read tokens, 0 write.
        // OpenAI's auto-prefix charges 0.5× for cached reads
        // (50% discount). At GPT-4o pricing of $2.50 per million
        // input tokens, savings should be:
        //   51800 × (1.0 - 0.5) × $2.50 / 1_000_000 = $0.064750
        // The dashboard's $45.4K reading was 1,000,000× too high.
        let mut model = fake_model("openai");
        model.cost_per_input_token = Some(2.50); // GPT-4o
        let resp = json!({
            "usage": {
                "prompt_tokens_details": {
                    "cached_tokens": 51800
                }
            }
        });
        let usage =
            extract_usage_from_response(&resp, PromptCacheMechanism::AutomaticPrefix, &model);
        assert!(
            usage.savings_usd < 1.0,
            "OpenAI auto-prefix savings for 51.8K tokens should be cents-scale, got {}",
            usage.savings_usd
        );
        let expected = 51800.0 * 0.5 * 2.50 / 1_000_000.0;
        assert!(
            (usage.savings_usd - expected).abs() < 1e-9,
            "expected ~${expected:.4}, got {}",
            usage.savings_usd
        );
    }

    #[test]
    fn stream_totals_skips_zeros() {
        let model = fake_model("anthropic");
        let usage =
            usage_from_stream_totals(0, 0, PromptCacheMechanism::CacheControlBreakpoints, &model);
        // Both zero → caller should be able to skip writing log fields.
        assert_eq!(usage.write_tokens, 0);
        assert_eq!(usage.read_tokens, 0);
        assert_eq!(usage.savings_usd, 0.0);
    }

    #[test]
    fn anthropic_extract_usage_reads_anthropic_keys() {
        let model = make_test_model("anthropic", "claude-opus-4");
        let resp = json!({
            "usage": {
                "cache_creation_input_tokens": 1000,
                "cache_read_input_tokens": 5000
            }
        });
        let usage = anthropic_usage(&resp, &model);
        assert_eq!(usage.write_tokens, 1000);
        assert_eq!(usage.read_tokens, 5000);
    }

    #[test]
    fn anthropic_extract_usage_zero_on_missing_keys() {
        let model = make_test_model("anthropic", "claude-opus-4");
        let resp = json!({"usage": {"prompt_tokens": 100}});
        let usage = anthropic_usage(&resp, &model);
        assert_eq!(usage.write_tokens, 0);
        assert_eq!(usage.read_tokens, 0);
    }

    #[test]
    fn openai_reads_cached_tokens_from_responses_input_tokens_details() {
        // OpenAI Responses API shape (`/v1/responses`): the cache hit is
        // reported under `input_tokens_details`, not `prompt_tokens_details`.
        let model = make_test_model("openai", "gpt-5.5");
        let resp = json!({
            "usage": {
                "input_tokens": 1500,
                "input_tokens_details": {"cached_tokens": 1024},
                "output_tokens": 20,
                "total_tokens": 1520
            }
        });
        let usage = openai_usage(&resp, &model);
        assert_eq!(usage.read_tokens, 1024);
        assert_eq!(usage.write_tokens, 0);
    }

    #[test]
    fn openai_reads_cached_tokens_from_prompt_tokens_details() {
        let model = make_test_model("openai", "gpt-5");
        let resp = json!({
            "usage": {
                "prompt_tokens": 1500,
                "prompt_tokens_details": {"cached_tokens": 1024}
            }
        });
        let usage = openai_usage(&resp, &model);
        assert_eq!(usage.read_tokens, 1024);
        assert_eq!(usage.write_tokens, 0);
    }

    #[test]
    fn openai_zero_when_prompt_tokens_details_missing() {
        let model = make_test_model("openai", "gpt-5");
        let resp = json!({"usage": {"prompt_tokens": 100}});
        let usage = openai_usage(&resp, &model);
        assert_eq!(usage.read_tokens, 0);
        assert_eq!(usage.write_tokens, 0);
    }

    #[test]
    fn openai_zero_when_cached_tokens_field_absent() {
        let model = make_test_model("openai", "gpt-5");
        let resp = json!({
            "usage": {
                "prompt_tokens": 100,
                "prompt_tokens_details": {"audio_tokens": 0}
            }
        });
        let usage = openai_usage(&resp, &model);
        assert_eq!(usage.read_tokens, 0);
    }

    #[test]
    fn gemini_extract_usage_reads_gemini_cached_content_token_count() {
        let model = make_test_model("gemini", "gemini-2.5-pro");
        let resp = json!({
            "usageMetadata": {
                "cachedContentTokenCount": 30000,
                "promptTokenCount": 31000
            }
        });
        let usage = gemini_usage(&resp, &model);
        assert_eq!(usage.read_tokens, 30000);
        assert_eq!(usage.write_tokens, 0);
    }
}
