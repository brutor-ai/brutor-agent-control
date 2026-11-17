// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
// LLM provider adapter trait and implementations.
// Each adapter normalizes between OpenAI-compatible API format and
// a specific provider's native format (request transform, headers,
// URL routing, streaming, and response normalization).

use serde_json::{Value, json};

use crate::models::db::LlmModel;

mod ai21;
mod anthropic;
mod azure;
mod bedrock;
mod cloudflare;
mod cohere;
mod databricks;
mod deepseek;
mod gemini;
mod groq;
mod minimax;
mod mistral;
mod moonshot;
mod ollama;
mod openai;
pub(crate) mod openai_family;
mod openrouter;
mod perplexity;
mod replicate;
mod together;
mod xai;

pub use anthropic::{AnthropicAdapter, AnthropicStreamNormalizer};

// ---------------------------------------------------------------------------
// Traits
// ---------------------------------------------------------------------------

/// Adapter interface for LLM provider differences.
/// OpenAI format is the canonical wire format; adapters translate to/from
/// each provider's native format.
pub trait LlmAdapter: Send + Sync {
    /// Provider identifier (e.g. "openai", "anthropic").
    fn provider_name(&self) -> &str;

    /// Override the chat completions endpoint path.
    /// Default: returns the caller-supplied endpoint unchanged.
    ///
    /// `stream` indicates whether the caller is asking for a streamed
    /// response — most providers use the same URL for both (Anthropic
    /// /v1/messages, OpenAI /chat/completions), so the default impl
    /// ignores the flag. Gemini is the outlier: its native REST has
    /// distinct URL paths (`:generateContent` vs `:streamGenerateContent`).
    fn chat_endpoint(&self, _model: &LlmModel, default: &str, _stream: bool) -> String {
        default.to_string()
    }

    /// Authentication for a specific endpoint. Most providers authenticate
    /// every endpoint the same way; Gemini's OpenAI-compatible embeddings
    /// endpoint wants a Bearer key where its native API wants
    /// `x-goog-api-key`.
    fn auth_headers_for_endpoint(
        &self,
        model: &LlmModel,
        _endpoint: &str,
    ) -> Vec<(String, String)> {
        self.auth_headers(model)
    }

    /// Build provider-specific authentication and API version headers.
    fn auth_headers(&self, model: &LlmModel) -> Vec<(String, String)> {
        // Default: Bearer token
        let mut headers = Vec::new();
        if let Some(ref key) = model.api_key {
            headers.push(("Authorization".into(), format!("Bearer {key}")));
        }
        headers
    }

    /// Transform the request body from OpenAI format to the provider's native
    /// format. Called after model defaults and system prompt injection.
    fn transform_request(&self, _obj: &mut serde_json::Map<String, Value>, _model: &LlmModel) {}

    /// Parameter names that this provider does not accept.
    /// These are dropped when `drop_params` is true on the model.
    fn unsupported_params(&self) -> &'static [&'static str] {
        &[]
    }

    /// Whether streaming responses from this provider need normalization
    /// to OpenAI SSE format.
    fn needs_stream_normalization(&self) -> bool {
        false
    }

    /// Create a stateful stream normalizer for converting provider SSE events
    /// to OpenAI-compatible SSE format.
    fn create_stream_normalizer(&self) -> Box<dyn StreamNormalizer> {
        Box::new(NoopStreamNormalizer)
    }

    /// Normalize a non-streaming response body from the provider's native
    /// format to OpenAI chat completion format. No-op by default.
    fn normalize_response(&self, _body: &mut Value) {}
}

/// Stateful converter for streaming SSE events.
pub trait StreamNormalizer: Send {
    /// Convert a single provider SSE data payload to zero or more
    /// OpenAI-format SSE data strings. Return `["[DONE]"]` for end-of-stream.
    fn convert(&mut self, data: &str) -> Vec<String>;
}

/// No-op stream normalizer — passes data through unchanged.
struct NoopStreamNormalizer;

impl StreamNormalizer for NoopStreamNormalizer {
    fn convert(&mut self, data: &str) -> Vec<String> {
        vec![data.to_string()]
    }
}

// ---------------------------------------------------------------------------
// Adapter lookup
// ---------------------------------------------------------------------------

use ai21::Ai21Adapter;
// AnthropicAdapter is `pub use`-d above; no need to re-import here.
use azure::AzureAdapter;
use bedrock::BedrockAdapter;
use cloudflare::CloudflareAdapter;
use cohere::CohereAdapter;
use databricks::DatabricksAdapter;
use deepseek::DeepseekAdapter;
use gemini::GeminiAdapter;
use groq::GroqAdapter;
use minimax::MinimaxAdapter;
use mistral::MistralAdapter;
use moonshot::MoonshotAdapter;
use ollama::OllamaAdapter;
use openai::OpenAiAdapter;
use openrouter::OpenRouterAdapter;
use perplexity::PerplexityAdapter;
use replicate::ReplicateAdapter;
use together::TogetherFireworksAdapter;
use xai::XaiAdapter;

/// Look up the adapter for a provider name.
/// Returns the OpenAI adapter as the default for unknown providers.
pub fn get_adapter(provider: &str) -> &'static dyn LlmAdapter {
    match provider {
        "anthropic" => &AnthropicAdapter,
        "gemini" | "google_ai_studio" | "vertex_ai" | "vertex" => &GeminiAdapter,
        "cohere" => &CohereAdapter,
        "perplexity" => &PerplexityAdapter,
        "deepseek" => &DeepseekAdapter,
        "groq" => &GroqAdapter,
        "together_ai" | "fireworks_ai" | "deepinfra" => &TogetherFireworksAdapter,
        "ollama" | "lm_studio" => &OllamaAdapter,
        "bedrock" => &BedrockAdapter,
        "azure" | "azure_ai" => &AzureAdapter,
        "mistral" | "codestral" => &MistralAdapter,
        "xai" => &XaiAdapter,
        "moonshot" => &MoonshotAdapter,
        "minimax" => &MinimaxAdapter,
        "ai21" => &Ai21Adapter,
        "openrouter" => &OpenRouterAdapter,
        "cloudflare" => &CloudflareAdapter,
        "replicate" => &ReplicateAdapter,
        "databricks" => &DatabricksAdapter,
        _ => &OpenAiAdapter,
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Recursively strip JSON schema constraints unsupported by Anthropic.
/// Moves stripped constraints into the field's "description" for context.
pub(crate) fn filter_schema_constraints(schema: &mut Value) {
    const UNSUPPORTED: &[&str] = &[
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
    ];

    if let Some(obj) = schema.as_object_mut() {
        // Collect constraint descriptions
        let mut extras = Vec::new();
        for &key in UNSUPPORTED {
            if let Some(val) = obj.remove(key) {
                extras.push(format!("{key}={val}"));
            }
        }
        // Append to description
        if !extras.is_empty() {
            let desc = obj
                .get("description")
                .and_then(|d| d.as_str())
                .unwrap_or("")
                .to_string();
            let suffix = extras.join(", ");
            let new_desc = if desc.is_empty() {
                format!("Constraints: {suffix}")
            } else {
                format!("{desc} (Constraints: {suffix})")
            };
            obj.insert("description".into(), Value::String(new_desc));
        }

        // Recurse into properties
        if let Some(props) = obj.get_mut("properties") {
            if let Some(props_obj) = props.as_object_mut() {
                for (_, v) in props_obj.iter_mut() {
                    filter_schema_constraints(v);
                }
            }
        }
        // Recurse into items
        if let Some(items) = obj.get_mut("items") {
            filter_schema_constraints(items);
        }
        // Recurse into $defs
        if let Some(defs) = obj.get_mut("$defs") {
            if let Some(defs_obj) = defs.as_object_mut() {
                for (_, v) in defs_obj.iter_mut() {
                    filter_schema_constraints(v);
                }
            }
        }
        // Recurse into anyOf/allOf/oneOf
        for combo_key in ["anyOf", "allOf", "oneOf"] {
            if let Some(arr) = obj.get_mut(combo_key).and_then(|v| v.as_array_mut()) {
                for item in arr.iter_mut() {
                    filter_schema_constraints(item);
                }
            }
        }
    }
}

/// Strip `name` field from all messages (for providers that don't support it).
pub(crate) fn strip_name_from_messages(obj: &mut serde_json::Map<String, Value>) {
    if let Some(messages) = obj.get_mut("messages").and_then(|m| m.as_array_mut()) {
        for msg in messages.iter_mut() {
            if let Some(msg_obj) = msg.as_object_mut() {
                msg_obj.remove("name");
            }
        }
    }
}

/// Remove `strict` field from tool definitions.
pub(crate) fn strip_strict_from_tools(obj: &mut serde_json::Map<String, Value>) {
    if let Some(tools) = obj.get_mut("tools").and_then(|t| t.as_array_mut()) {
        for tool in tools.iter_mut() {
            if let Some(tool_obj) = tool.as_object_mut() {
                tool_obj.remove("strict");
                if let Some(func) = tool_obj.get_mut("function") {
                    if let Some(func_obj) = func.as_object_mut() {
                        func_obj.remove("strict");
                    }
                }
            }
        }
    }
}

/// Normalize message content to an array of content blocks for merging.
pub(crate) fn content_to_blocks(content: Value) -> Vec<Value> {
    match content {
        Value::Array(arr) => arr,
        Value::String(s) => vec![json!({"type": "text", "text": s})],
        Value::Null => vec![],
        other => vec![json!({"type": "text", "text": other.to_string()})],
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
pub(crate) fn make_test_model(provider: &str, model_name: &str) -> LlmModel {
    LlmModel {
        id: "test-model-1".to_string(),
        tenant_id: "tenant-1".to_string(),
        name: "Test Model".to_string(),
        description: None,
        usage: None,
        provider_id: format!("prov-{provider}"),
        provider: provider.to_string(),
        provider_additional_drop_params: None,
        model_name: model_name.to_string(),
        mode: None,
        parameters: None,
        tags: None,
        api_url_auto_discovered: None,
        api_url: None,
        auth_type: None,
        api_key: Some("sk-test-key".to_string()),
        default_temperature: None,
        default_max_tokens: None,
        system_prompt: None,
        top_p: None,
        n: None,
        stop_sequences: None,
        seed: None,
        presence_penalty: None,
        frequency_penalty: None,
        logit_bias: None,
        response_format: None,
        logprobs: None,
        top_logprobs: None,
        stream_options: None,
        supports_streaming: None,
        supports_function_calling: None,
        supports_vision: None,
        tools: None,
        tool_choice: None,
        parallel_tool_calls: None,
        user_identifier: None,
        api_version: None,
        extra_headers: None,
        model_metadata: None,
        drop_params: None,
        additional_drop_params: None,
        thinking_enabled: None,
        routing_group_id: None,
        routed_via_group_id: None,
        routed_stream_timeout: None,
        routed_via_tier: None,
        routed_tier_signals: None,
        routing_weight: None,
        fallback_models: None,
        rate_limit_rpm: None,
        rate_limit_tpm: None,
        cost_per_input_token: None,
        cost_per_output_token: None,
        cost_per_image: None,
        supported_image_sizes: None,
        supported_image_qualities: None,
        max_images_per_request: None,
        cost_per_audio_char: None,
        supported_voices: None,
        supported_audio_formats: None,
        cost_per_audio_minute: None,
        max_audio_duration_seconds: None,
        supported_input_formats: None,
        supported_response_formats: None,
        cost_per_video_second: None,
        supported_video_resolutions: None,
        supported_video_durations: None,
        max_video_duration_seconds: None,
        timeout: None,
        max_concurrent_requests: None,
        allowed_fails: None,
        cooldown_time: None,
        enabled: true,
        capabilities: None,
        health_status: None,
        last_health_check: None,
        last_health_success: None,
        consecutive_health_failures: None,
        health_check_error: None,
        total_requests: None,
        failed_requests: None,
        total_tokens_consumed: None,
        avg_latency_ms: None,
        supports_batch: None,
        provider_config: None,
        credential_unreadable: false,
        credentials_opened: false,
        created_at: None,
        updated_at: None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    // -----------------------------------------------------------------------
    // get_adapter tests
    // -----------------------------------------------------------------------

    #[test]
    fn get_adapter_anthropic() {
        assert_eq!(get_adapter("anthropic").provider_name(), "anthropic");
    }

    #[test]
    fn get_adapter_gemini_variants() {
        assert_eq!(get_adapter("gemini").provider_name(), "gemini");
        assert_eq!(get_adapter("google_ai_studio").provider_name(), "gemini");
        assert_eq!(get_adapter("vertex_ai").provider_name(), "gemini");
        assert_eq!(get_adapter("vertex").provider_name(), "gemini");
    }

    #[test]
    fn get_adapter_cohere() {
        assert_eq!(get_adapter("cohere").provider_name(), "cohere");
    }

    #[test]
    fn get_adapter_perplexity() {
        assert_eq!(get_adapter("perplexity").provider_name(), "perplexity");
    }

    #[test]
    fn get_adapter_deepseek() {
        assert_eq!(get_adapter("deepseek").provider_name(), "deepseek");
    }

    #[test]
    fn get_adapter_groq() {
        assert_eq!(get_adapter("groq").provider_name(), "groq");
    }

    #[test]
    fn get_adapter_together_fireworks_deepinfra() {
        assert_eq!(get_adapter("together_ai").provider_name(), "together_ai");
        assert_eq!(get_adapter("fireworks_ai").provider_name(), "together_ai");
        assert_eq!(get_adapter("deepinfra").provider_name(), "together_ai");
    }

    #[test]
    fn get_adapter_ollama_lm_studio() {
        assert_eq!(get_adapter("ollama").provider_name(), "ollama");
        assert_eq!(get_adapter("lm_studio").provider_name(), "ollama");
    }

    #[test]
    fn get_adapter_bedrock() {
        assert_eq!(get_adapter("bedrock").provider_name(), "bedrock");
    }

    #[test]
    fn get_adapter_unknown_falls_back_to_openai() {
        assert_eq!(get_adapter("unknown_provider").provider_name(), "openai");
        assert_eq!(get_adapter("openai").provider_name(), "openai");
        assert_eq!(get_adapter("azure").provider_name(), "azure");
    }

    #[test]
    fn get_adapter_azure() {
        assert_eq!(get_adapter("azure").provider_name(), "azure");
        assert_eq!(get_adapter("azure_ai").provider_name(), "azure");
    }

    #[test]
    fn get_adapter_mistral() {
        assert_eq!(get_adapter("mistral").provider_name(), "mistral");
        assert_eq!(get_adapter("codestral").provider_name(), "mistral");
    }

    #[test]
    fn get_adapter_xai() {
        assert_eq!(get_adapter("xai").provider_name(), "xai");
    }

    #[test]
    fn get_adapter_ai21() {
        assert_eq!(get_adapter("ai21").provider_name(), "ai21");
    }

    #[test]
    fn get_adapter_openrouter() {
        assert_eq!(get_adapter("openrouter").provider_name(), "openrouter");
    }

    #[test]
    fn get_adapter_cloudflare() {
        assert_eq!(get_adapter("cloudflare").provider_name(), "cloudflare");
    }

    #[test]
    fn get_adapter_replicate_databricks() {
        assert_eq!(get_adapter("replicate").provider_name(), "replicate");
        assert_eq!(get_adapter("databricks").provider_name(), "databricks");
    }

    // -----------------------------------------------------------------------
    // content_to_blocks helper
    // -----------------------------------------------------------------------

    #[test]
    fn content_to_blocks_array_passthrough() {
        let input = json!([{"type": "text", "text": "hello"}, {"type": "image_url", "url": "x"}]);
        let blocks = content_to_blocks(input.clone());
        assert_eq!(blocks, input.as_array().unwrap().clone());
    }

    #[test]
    fn content_to_blocks_string_to_text_block() {
        let blocks = content_to_blocks(json!("hello world"));
        assert_eq!(blocks, vec![json!({"type": "text", "text": "hello world"})]);
    }

    #[test]
    fn content_to_blocks_null_to_empty_vec() {
        let blocks = content_to_blocks(Value::Null);
        assert!(blocks.is_empty());
    }

    #[test]
    fn content_to_blocks_number_to_text_block() {
        let blocks = content_to_blocks(json!(42));
        assert_eq!(blocks, vec![json!({"type": "text", "text": "42"})]);
    }

    // -----------------------------------------------------------------------
    // filter_schema_constraints helper
    // -----------------------------------------------------------------------

    #[test]
    fn filter_schema_constraints_removes_and_appends_to_description() {
        let mut schema = json!({
            "type": "string",
            "description": "A name",
            "minLength": 1,
            "maxLength": 100
        });

        filter_schema_constraints(&mut schema);

        assert!(!schema.as_object().unwrap().contains_key("minLength"));
        assert!(!schema.as_object().unwrap().contains_key("maxLength"));
        let desc = schema.get("description").unwrap().as_str().unwrap();
        assert!(desc.contains("minLength=1"));
        assert!(desc.contains("maxLength=100"));
        assert!(desc.starts_with("A name"));
    }

    #[test]
    fn filter_schema_constraints_recursively_filters_nested() {
        let mut schema = json!({
            "type": "object",
            "properties": {
                "age": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 150
                },
                "name": {
                    "type": "string",
                    "minLength": 1
                }
            }
        });

        filter_schema_constraints(&mut schema);

        let age = schema.get("properties").unwrap().get("age").unwrap();
        assert!(!age.as_object().unwrap().contains_key("minimum"));
        assert!(!age.as_object().unwrap().contains_key("maximum"));
        let age_desc = age.get("description").unwrap().as_str().unwrap();
        assert!(age_desc.contains("minimum=0"));
        assert!(age_desc.contains("maximum=150"));

        let name = schema.get("properties").unwrap().get("name").unwrap();
        assert!(!name.as_object().unwrap().contains_key("minLength"));
        let name_desc = name.get("description").unwrap().as_str().unwrap();
        assert!(name_desc.contains("minLength=1"));
    }

    // -----------------------------------------------------------------------
    // strip_name_from_messages helper
    // -----------------------------------------------------------------------

    #[test]
    fn strip_name_from_messages_removes_name() {
        let mut obj = serde_json::Map::new();
        obj.insert(
            "messages".into(),
            json!([
                {"role": "user", "content": "hi", "name": "alice"},
                {"role": "assistant", "content": "hello", "name": "bot"}
            ]),
        );

        strip_name_from_messages(&mut obj);

        let msgs = obj.get("messages").unwrap().as_array().unwrap();
        for msg in msgs {
            assert!(!msg.as_object().unwrap().contains_key("name"));
        }
    }

    // -----------------------------------------------------------------------
    // strip_strict_from_tools helper
    // -----------------------------------------------------------------------

    #[test]
    fn strip_strict_from_tools_removes_strict() {
        let mut obj = serde_json::Map::new();
        obj.insert(
            "tools".into(),
            json!([{
                "type": "function",
                "strict": true,
                "function": {
                    "name": "test",
                    "strict": true,
                    "parameters": {"type": "object"}
                }
            }]),
        );

        strip_strict_from_tools(&mut obj);

        let tool = &obj.get("tools").unwrap().as_array().unwrap()[0];
        assert!(!tool.as_object().unwrap().contains_key("strict"));
        assert!(
            !tool
                .get("function")
                .unwrap()
                .as_object()
                .unwrap()
                .contains_key("strict")
        );
    }
}
