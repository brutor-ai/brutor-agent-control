// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Gemini adapter — native REST surface
//!
//! Translates between Brutor's canonical OpenAI chat-completion shape
//! and Google's native Gemini REST API
//! (`/v1beta/models/{model}:generateContent`,
//! `/v1beta/models/{model}:streamGenerateContent`).
//!
//! ## Why native, not the OpenAI-compat layer
//!
//! Google exposes `/v1beta/openai/chat/completions` as a compatibility
//! shim. Convenient for drop-in clients, but it blocks every Gemini-
//! specific capability:
//!
//!   * `cachedContents` — the only field that triggers explicit
//!     prompt-cache lookups (the OpenAI-compat endpoint doesn't accept
//!     `cachedContent`).
//!   * Code execution as a first-class tool (`codeExecution`).
//!   * Google Search grounding with citation metadata.
//!   * Per-category `safetySettings`.
//!   * Multimodal `Part` objects (inline images / video / audio / PDFs
//!     via `inline_data`, references via `file_data`).
//!   * `responseSchema` richer than OpenAI's `response_format.json_schema`.
//!   * Native `usageMetadata.cachedContentTokenCount` counter.
//!
//! Targeting native at the wire edge preserves all those affordances
//! while keeping Brutor's internal pipeline OpenAI-shape (same pattern
//! the Anthropic adapter follows). Clients on Brutor's own surface
//! continue to see OpenAI shape — the translation is invisible to
//! them.

use std::collections::HashMap;

use serde_json::{Map, Value, json};

use super::{LlmAdapter, StreamNormalizer};
use crate::models::db::LlmModel;

// ---------------------------------------------------------------------------
// Adapter
// ---------------------------------------------------------------------------

pub struct GeminiAdapter;

impl LlmAdapter for GeminiAdapter {
    fn provider_name(&self) -> &str {
        "gemini"
    }

    /// Native Gemini uses model-in-path endpoints. The path differs
    /// between non-streaming and streaming, so the `stream` flag
    /// determines the suffix.
    ///
    /// Examples:
    ///   * non-streaming: `/v1beta/models/gemini-2.5-flash:generateContent`
    ///   * streaming:     `/v1beta/models/gemini-2.5-flash:streamGenerateContent?alt=sse`
    ///
    /// The `default` argument (typically `/chat/completions`) is
    /// ignored — Gemini's API doesn't share its shape with OpenAI's
    /// path scheme.
    fn chat_endpoint(&self, model: &LlmModel, default: &str, stream: bool) -> String {
        // Embeddings go to Google's OpenAI-compatible endpoint (the
        // body is OpenAI-shaped already); every other call is native.
        if default == "/embeddings" {
            return "/v1beta/openai/embeddings".to_string();
        }
        let suffix = if stream {
            ":streamGenerateContent?alt=sse"
        } else {
            ":generateContent"
        };
        // Strip a leading `models/` if the operator already qualified
        // it — we re-add the prefix below and `models/models/...` would
        // be rejected.
        let name = model
            .model_name
            .strip_prefix("models/")
            .unwrap_or(&model.model_name);
        format!("/v1beta/models/{name}{suffix}")
    }

    /// Gemini accepts the API key either as a `x-goog-api-key` header
    /// or as a `?key=...` query parameter. We use the header — it
    /// doesn't get stamped into request logs the way query strings do.
    fn auth_headers(&self, model: &LlmModel) -> Vec<(String, String)> {
        let mut headers = Vec::new();
        if let Some(ref key) = model.api_key {
            headers.push(("x-goog-api-key".into(), key.clone()));
        }
        headers
    }

    /// The OpenAI-compatible endpoint authenticates with a Bearer key; the
    /// native one refuses Bearer for an API key (it means OAuth there).
    fn auth_headers_for_endpoint(&self, model: &LlmModel, endpoint: &str) -> Vec<(String, String)> {
        if endpoint != "/embeddings" {
            return self.auth_headers(model);
        }
        let mut headers = Vec::new();
        if let Some(ref key) = model.api_key {
            headers.push(("Authorization".into(), format!("Bearer {key}")));
        }
        headers
    }

    fn transform_request(&self, obj: &mut serde_json::Map<String, Value>, model: &LlmModel) {
        // Order of operations matters here because each step assumes
        // the previous one has run.
        //
        //  1. messages[] → contents[] (with role mapping + system
        //     extraction into top-level systemInstruction).
        //  2. tools (OpenAI) → tools[{functionDeclarations: [...]}].
        //  3. tool_choice → toolConfig.functionCallingConfig.
        //  4. response_format → responseSchema + responseMimeType.
        //  5. Sampling params (temperature, top_p, max_tokens, n, stop)
        //     → generationConfig.
        //  6. Misc Gemini-specific affordances: thinking, web search,
        //     audio output, safety settings passthrough.
        //  7. Strip OpenAI-only fields the native API rejects.
        //  8. Strip caller-supplied `cache_control` from messages/tools
        //     — same authoritative-emitter rule as the Anthropic
        //     adapter (the prompt-cache layer owns cache markers).

        transform_messages_to_contents(obj);
        transform_tools(obj);
        transform_tool_choice(obj);
        transform_response_format(obj);
        transform_generation_config(obj);
        transform_thinking(obj, model);
        transform_web_search(obj);
        transform_audio_output(obj);

        // Strip caller-supplied `cache_control` from anywhere it could
        // have leaked in. The prompt-cache layer
        // (`services::prompt_cache::plan_breakpoints` →
        // `llm_adapter::gemini_cache`) is the authoritative emitter of
        // cache markers for this provider.
        strip_caller_cache_control(obj);

        // OpenAI-only / not-supported params.
        for param in [
            "logit_bias",
            "parallel_tool_calls",
            "user",
            "suffix",
            "stream_options",
            "frequency_penalty",
            "presence_penalty",
            "logprobs",
            "top_logprobs",
            "response_format",
            "model",
            "stream",
            "temperature",
            "top_p",
            "top_k",
            "max_tokens",
            "max_completion_tokens",
            "n",
            "stop",
            "thinking_config",
            // Brutor-internal fields that must never reach the provider.
            "model_id",
        ] {
            obj.remove(param);
        }
    }

    fn needs_stream_normalization(&self) -> bool {
        true
    }

    fn create_stream_normalizer(&self) -> Box<dyn StreamNormalizer> {
        Box::new(GeminiStreamNormalizer::new())
    }

    fn normalize_response(&self, body: &mut Value) {
        normalize_native_response(body);
    }
}

// ---------------------------------------------------------------------------
// Request translation: OpenAI → Gemini native
// ---------------------------------------------------------------------------

/// Move `role:"system"` messages from `messages` into the top-level
/// `systemInstruction` field, and convert the remaining `messages`
/// array into Gemini's `contents` array (with role + part shape).
///
/// Gemini roles:
///   * `user`    — user-authored turn
///   * `model`   — assistant-authored turn (Gemini calls it "model")
///
/// System messages have no role in `contents`; they sit at the top
/// level as `systemInstruction: { parts: [...] }` (single block; we
/// merge multiple input system messages into one block joined by
/// `\n\n`, the same way the Anthropic adapter does).
fn transform_messages_to_contents(obj: &mut Map<String, Value>) {
    let Some(Value::Array(msgs)) = obj.remove("messages") else {
        return;
    };

    let mut system_parts: Vec<String> = Vec::new();
    // tool_call_id → function name. Required to convert OpenAI `role:"tool"`
    // results back into Gemini's `functionResponse` part — the OpenAI
    // shape carries the function name only on the assistant message that
    // emitted the call, not on the tool-result message. We snapshot a
    // map while walking the array.
    let mut tool_call_name_by_id: HashMap<String, String> = HashMap::new();
    let mut contents: Vec<Value> = Vec::new();

    for msg in msgs {
        let role = msg.get("role").and_then(|r| r.as_str()).unwrap_or("user");
        let content_value = msg.get("content").cloned().unwrap_or(Value::Null);

        match role {
            "system" => {
                // Extract text — only string content survives. Multimodal
                // system instructions aren't supported by Gemini.
                if let Some(text) = content_value.as_str() {
                    system_parts.push(text.to_string());
                }
                continue;
            }
            "user" => {
                let parts = openai_content_to_gemini_parts(&content_value);
                if !parts.is_empty() {
                    contents.push(json!({ "role": "user", "parts": parts }));
                }
            }
            "assistant" => {
                // Two payload shapes:
                //   (a) text content + optional tool_calls
                //   (b) only tool_calls, content null/empty
                let mut parts: Vec<Value> = Vec::new();
                let text_parts = openai_content_to_gemini_parts(&content_value);
                for p in text_parts {
                    parts.push(p);
                }
                if let Some(tool_calls) = msg.get("tool_calls").and_then(|tc| tc.as_array()) {
                    for call in tool_calls {
                        let id = call.get("id").and_then(|v| v.as_str()).unwrap_or("");
                        let func = call.get("function");
                        let name = func
                            .and_then(|f| f.get("name"))
                            .and_then(|n| n.as_str())
                            .unwrap_or("");
                        let args = func
                            .and_then(|f| f.get("arguments"))
                            .and_then(|a| a.as_str())
                            .and_then(|s| serde_json::from_str::<Value>(s).ok())
                            .unwrap_or(json!({}));
                        if !id.is_empty() {
                            tool_call_name_by_id.insert(id.to_string(), name.to_string());
                        }
                        parts.push(json!({
                            "functionCall": {
                                "name": name,
                                "args": args,
                            }
                        }));
                    }
                }
                if !parts.is_empty() {
                    contents.push(json!({ "role": "model", "parts": parts }));
                }
            }
            "tool" => {
                // OpenAI: { role:"tool", tool_call_id, content }
                // Gemini: { role:"user", parts:[{functionResponse:{name, response}}] }
                let tool_call_id = msg
                    .get("tool_call_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let func_name = tool_call_name_by_id
                    .get(tool_call_id)
                    .cloned()
                    .unwrap_or_else(|| {
                        // Fallback: callers sometimes pass `name` on
                        // the tool-result message itself.
                        msg.get("name")
                            .and_then(|n| n.as_str())
                            .unwrap_or("")
                            .to_string()
                    });
                // Wrap content into a Gemini-compatible response object.
                // Gemini expects `response` to be a struct (object); if
                // the tool returned plain text, wrap it so the API
                // doesn't 400.
                let response_value = match &content_value {
                    Value::Object(_) => content_value.clone(),
                    Value::String(s) => {
                        // Try to parse JSON first — many tools return
                        // serialised JSON strings; passing those as
                        // {result: "..."} buries the structure. Falls
                        // back to {result: <string>} only when parse
                        // fails.
                        serde_json::from_str::<Value>(s)
                            .ok()
                            .filter(|v| v.is_object())
                            .unwrap_or_else(|| json!({ "result": s }))
                    }
                    Value::Null => json!({ "result": null }),
                    other => json!({ "result": other.clone() }),
                };
                contents.push(json!({
                    "role": "user",
                    "parts": [{
                        "functionResponse": {
                            "name": func_name,
                            "response": response_value,
                        }
                    }],
                }));
            }
            _ => {
                // Unknown role — drop quietly. Better than 400ing.
            }
        }
    }

    if !system_parts.is_empty() {
        obj.insert(
            "systemInstruction".into(),
            json!({ "parts": [{ "text": system_parts.join("\n\n") }] }),
        );
    }
    obj.insert("contents".into(), Value::Array(contents));
}

/// Convert OpenAI message `content` (string OR array of blocks) into
/// Gemini's `parts` array.
fn openai_content_to_gemini_parts(content: &Value) -> Vec<Value> {
    match content {
        Value::String(s) if s.is_empty() => Vec::new(),
        Value::String(s) => vec![json!({ "text": s })],
        Value::Array(blocks) => blocks
            .iter()
            .filter_map(openai_content_block_to_gemini_part)
            .collect(),
        Value::Null => Vec::new(),
        other => vec![json!({ "text": other.to_string() })],
    }
}

/// Translate a single OpenAI content block to a Gemini Part.
fn openai_content_block_to_gemini_part(block: &Value) -> Option<Value> {
    let ty = block.get("type").and_then(|t| t.as_str()).unwrap_or("text");
    match ty {
        "text" => block
            .get("text")
            .and_then(|t| t.as_str())
            .map(|s| json!({ "text": s })),
        "image_url" => {
            // OpenAI: { type:"image_url", image_url:{ url:"data:image/png;base64,..." } }
            // Gemini: { inline_data:{ mime_type:"image/png", data:"<base64>" } }
            let url = block
                .get("image_url")
                .and_then(|iu| iu.get("url"))
                .and_then(|u| u.as_str())?;
            data_url_to_inline_data(url)
        }
        "input_audio" => block
            .get("input_audio")
            .and_then(|ia| ia.get("data"))
            .and_then(|d| d.as_str())
            .map(|data| {
                json!({
                    "inline_data": {
                        "mime_type": "audio/wav",
                        "data": data,
                    }
                })
            }),
        _ => None,
    }
}

/// Best-effort parse of an OpenAI-style data URL into a Gemini
/// `inline_data` part. Returns `None` for non-data URLs (Gemini accepts
/// hosted URLs via `file_data` but Brutor doesn't surface that today).
fn data_url_to_inline_data(url: &str) -> Option<Value> {
    let rest = url.strip_prefix("data:")?;
    let (mime, data_segment) = rest.split_once(';')?;
    let data = data_segment.strip_prefix("base64,")?;
    Some(json!({
        "inline_data": {
            "mime_type": mime,
            "data": data,
        }
    }))
}

/// Convert OpenAI `tools` array to Gemini `tools: [{functionDeclarations: [...]}]`.
///
/// Brutor receives:
///   ```json
///   "tools": [
///     {"type": "function", "function": {"name": "x", "description": "...", "parameters": {...}}}
///   ]
///   ```
/// Gemini wants:
///   ```json
///   "tools": [
///     {"functionDeclarations": [
///       {"name": "x", "description": "...", "parameters": {...}}
///     ]}
///   ]
///   ```
///
/// `googleSearch` and `codeExecution` aren't function tools — they
/// pass through as separate top-level tool entries.
/// Strip keys Google's native Gemini REST surface rejects on
/// `tools[].function_declarations[].parameters`. Google validates
/// against a subset of OpenAPI 3.0 Schema; emitting standard JSON
/// Schema keys produces 400s like
/// `Unknown name 'additionalProperties' ...: Cannot find field`.
///
/// The recursion walks both `properties` and `items` so nested object
/// / array schemas get the same treatment.
fn sanitise_gemini_schema(schema: &mut Value) {
    // Keys Google rejects. List grows as we discover more; keep in
    // sync with anything that surfaces in proxy_logs as
    // "Unknown name 'X' at 'tools[0].function_declarations...'".
    const UNSUPPORTED: &[&str] = &[
        "additionalProperties",
        "$schema",
        "$id",
        "$ref",
        "$defs",
        "definitions",
        // Some Gemini variants (older models) also reject these —
        // keeping the list aggressive errs on the side of "the
        // health check stays green" rather than "the schema is
        // maximally expressive".
        "patternProperties",
        "dependentRequired",
        "dependentSchemas",
        "unevaluatedProperties",
        "unevaluatedItems",
        "examples",
        "readOnly",
        "writeOnly",
        "discriminator",
        "externalDocs",
        "xml",
    ];

    match schema {
        Value::Object(map) => {
            for &key in UNSUPPORTED {
                map.remove(key);
            }
            // Recurse into nested schema-holders.
            if let Some(properties) = map.get_mut("properties") {
                if let Some(props_obj) = properties.as_object_mut() {
                    for (_, v) in props_obj.iter_mut() {
                        sanitise_gemini_schema(v);
                    }
                }
            }
            if let Some(items) = map.get_mut("items") {
                sanitise_gemini_schema(items);
            }
            for combo_key in ["anyOf", "allOf", "oneOf"] {
                if let Some(arr) = map.get_mut(combo_key).and_then(|v| v.as_array_mut()) {
                    for item in arr.iter_mut() {
                        sanitise_gemini_schema(item);
                    }
                }
            }
        }
        Value::Array(arr) => {
            for item in arr.iter_mut() {
                sanitise_gemini_schema(item);
            }
        }
        _ => {}
    }
}

fn transform_tools(obj: &mut Map<String, Value>) {
    let Some(tools_value) = obj.remove("tools") else {
        return;
    };
    // Brutor's `ChatCompletionRequest` serializes unset `Option<Value>`
    // fields as `null` rather than dropping them, so we routinely see
    // `tools: null` on requests with no tools — short-circuit before
    // it gets parsed as an empty array (Google rejects null-typed
    // fields with 400).
    if tools_value.is_null() {
        return;
    }
    let Value::Array(tools) = tools_value else {
        // Already a struct? Pass through unchanged.
        obj.insert("tools".into(), tools_value);
        return;
    };
    if tools.is_empty() {
        return;
    }

    let mut function_decls: Vec<Value> = Vec::new();
    let mut native_tools: Vec<Value> = Vec::new();

    for tool in tools {
        // Native Gemini tool shapes (no `type` field, just the
        // tool-name as the top-level key) pass through verbatim —
        // operators sometimes encode `googleSearch` / `codeExecution`
        // directly on the request.
        if tool.get("googleSearch").is_some()
            || tool.get("codeExecution").is_some()
            || tool.get("googleSearchRetrieval").is_some()
        {
            native_tools.push(tool);
            continue;
        }

        // OpenAI `{type:"function", function:{...}}`. Default `type` to
        // "function" so operators who omit it still hit this branch
        // (the OpenAI spec doesn't strictly require `type` when the
        // sibling `function` field is present).
        let ty = tool
            .get("type")
            .and_then(|t| t.as_str())
            .unwrap_or("function");
        if ty == "function" {
            if let Some(func) = tool.get("function") {
                let mut decl = serde_json::Map::new();
                if let Some(name) = func.get("name") {
                    decl.insert("name".into(), name.clone());
                }
                if let Some(desc) = func.get("description") {
                    decl.insert("description".into(), desc.clone());
                }
                if let Some(params) = func.get("parameters") {
                    // Google's native REST validates `parameters`
                    // against a SUBSET of OpenAPI 3.0 Schema and 400s
                    // on unknown fields. The Brutor pipeline can
                    // receive parameters generated by SDKs that emit
                    // standard JSON Schema (Pydantic, etc.) which
                    // include keys Gemini's subset doesn't recognise.
                    // Strip them before sending — failing health
                    // checks on every Gemini model (`Unknown name
                    // 'additionalProperties'...`) is what surfaced
                    // this gap.
                    let mut sanitised = params.clone();
                    sanitise_gemini_schema(&mut sanitised);
                    decl.insert("parameters".into(), sanitised);
                }
                if !decl.is_empty() {
                    function_decls.push(Value::Object(decl));
                }
            }
        }
        // Unknown tool shape: drop quietly to avoid 400ing.
    }

    let mut grouped: Vec<Value> = Vec::new();
    if !function_decls.is_empty() {
        grouped.push(json!({ "functionDeclarations": function_decls }));
    }
    grouped.extend(native_tools);

    if !grouped.is_empty() {
        obj.insert("tools".into(), Value::Array(grouped));
    }
}

/// OpenAI `tool_choice` → Gemini `toolConfig.functionCallingConfig`.
///
///   * `"auto"` / unset → `AUTO`
///   * `"none"` → `NONE`
///   * `"required"` → `ANY`
///   * `{type:"function", function:{name:"x"}}` → `ANY` + allowedFunctionNames
fn transform_tool_choice(obj: &mut Map<String, Value>) {
    let Some(tc) = obj.remove("tool_choice") else {
        return;
    };
    // ChatCompletionRequest serializes the unset `Option<Value>` as
    // `null`, which would otherwise fall into the catchall arm below
    // and synthesize a `toolConfig.functionCallingConfig.mode = AUTO`
    // — Gemini then 400s with "Function calling config is set
    // without function_declarations" when the request has no actual
    // tools. Skip the synthesis on null.
    if tc.is_null() {
        return;
    }
    // Also skip if the body has no tools at this point — Gemini
    // rejects toolConfig without functionDeclarations even when the
    // caller explicitly set tool_choice. The OpenAI shape allows the
    // pairing; Gemini does not.
    let has_function_tools = obj
        .get("tools")
        .and_then(|t| t.as_array())
        .map(|arr| !arr.is_empty())
        .unwrap_or(false);
    if !has_function_tools {
        return;
    }

    let (mode, allowed): (&str, Option<Vec<String>>) = match &tc {
        Value::String(s) => match s.as_str() {
            "auto" => ("AUTO", None),
            "none" => ("NONE", None),
            "required" | "any" => ("ANY", None),
            _ => ("AUTO", None),
        },
        Value::Object(map) => {
            if let Some(func) = map.get("function") {
                let name = func.get("name").and_then(|n| n.as_str()).unwrap_or("");
                if name.is_empty() {
                    ("AUTO", None)
                } else {
                    ("ANY", Some(vec![name.to_string()]))
                }
            } else {
                ("AUTO", None)
            }
        }
        _ => ("AUTO", None),
    };

    let mut function_calling_config = serde_json::Map::new();
    function_calling_config.insert("mode".into(), json!(mode));
    if let Some(names) = allowed {
        function_calling_config.insert("allowedFunctionNames".into(), json!(names));
    }
    obj.insert(
        "toolConfig".into(),
        json!({ "functionCallingConfig": Value::Object(function_calling_config) }),
    );
}

/// OpenAI `response_format` → Gemini `responseMimeType` + optional
/// `responseSchema` inside `generationConfig`. We stash these on a
/// temporary key and `transform_generation_config` moves them into
/// the final `generationConfig` object.
fn transform_response_format(obj: &mut Map<String, Value>) {
    let Some(rf) = obj.remove("response_format") else {
        return;
    };
    let rf_type = rf.get("type").and_then(|t| t.as_str()).unwrap_or("");
    match rf_type {
        "json_schema" => {
            let mut hint = serde_json::Map::new();
            hint.insert(
                "response_mime_type".into(),
                Value::String("application/json".into()),
            );
            if let Some(schema) = rf.get("json_schema").and_then(|js| js.get("schema")) {
                hint.insert("response_schema".into(), schema.clone());
            }
            obj.insert("__gemini_response_format".into(), Value::Object(hint));
        }
        "json_object" => {
            obj.insert(
                "__gemini_response_format".into(),
                json!({ "response_mime_type": "application/json" }),
            );
        }
        _ => {}
    }
}

/// Build the `generationConfig` block from OpenAI top-level sampling
/// params. Gemini rejects most of these at the top level — they must
/// be nested under `generationConfig`.
fn transform_generation_config(obj: &mut Map<String, Value>) {
    let mut config = serde_json::Map::new();

    if let Some(t) = obj.get("temperature").cloned() {
        config.insert("temperature".into(), t);
    }
    if let Some(p) = obj.get("top_p").cloned() {
        config.insert("topP".into(), p);
    }
    if let Some(k) = obj.get("top_k").cloned() {
        config.insert("topK".into(), k);
    }
    // OpenAI sends max_tokens; Responses API sends max_completion_tokens.
    // Both map to Gemini's maxOutputTokens.
    if let Some(m) = obj
        .get("max_tokens")
        .cloned()
        .or_else(|| obj.get("max_completion_tokens").cloned())
    {
        config.insert("maxOutputTokens".into(), m);
    }
    if let Some(n) = obj.get("n").cloned() {
        config.insert("candidateCount".into(), n);
    }
    if let Some(stop) = obj.get("stop").cloned() {
        // OpenAI accepts either string or array. Gemini accepts array.
        let stops = match stop {
            Value::String(s) => json!([s]),
            Value::Array(a) => Value::Array(a),
            other => other,
        };
        config.insert("stopSequences".into(), stops);
    }
    if let Some(Value::Object(hint)) = obj.remove("__gemini_response_format") {
        for (k, v) in hint {
            config.insert(k, v);
        }
    }

    if !config.is_empty() {
        // Preserve a caller-supplied generationConfig and merge our
        // computed values on top — that lets operators override
        // anything we synthesise via the model row.
        let existing = obj.remove("generationConfig").unwrap_or(json!({}));
        let mut merged = existing.as_object().cloned().unwrap_or_default();
        for (k, v) in config {
            merged.entry(k).or_insert(v);
        }
        obj.insert("generationConfig".into(), Value::Object(merged));
    }
}

/// Lift `thinking_config` (Brutor-canonical Gemini shape) into
/// `generationConfig.thinkingConfig` (Gemini-native shape). Triggered
/// by either an explicit `thinking_config` field on the request OR the
/// model row's `thinking_enabled` flag.
fn transform_thinking(obj: &mut Map<String, Value>, model: &LlmModel) {
    let user_supplied = obj.remove("thinking_config");

    let thinking_block = if let Some(tc) = user_supplied {
        // Translate snake_case → camelCase. Caller may have sent
        // either; Brutor's docs use snake_case.
        let mut tc_map = tc.as_object().cloned().unwrap_or_default();
        if let Some(budget) = tc_map.remove("thinking_budget") {
            tc_map
                .entry(String::from("thinkingBudget"))
                .or_insert(budget);
        }
        Some(Value::Object(tc_map))
    } else if model.thinking_enabled.unwrap_or(false) {
        // Default budget parallels the Anthropic adapter's default
        // (operators flipping the generic flag get comparable behaviour
        // across providers).
        Some(json!({ "thinkingBudget": 10_000 }))
    } else {
        None
    };

    if let Some(thinking) = thinking_block {
        let existing = obj
            .remove("generationConfig")
            .unwrap_or_else(|| Value::Object(serde_json::Map::new()));
        let mut merged = existing.as_object().cloned().unwrap_or_default();
        merged.insert("thinkingConfig".into(), thinking);
        obj.insert("generationConfig".into(), Value::Object(merged));
    }
}

/// OpenAI's `web_search_options` → Gemini's native `googleSearch` tool.
fn transform_web_search(obj: &mut Map<String, Value>) {
    if obj.remove("web_search_options").is_some() {
        let google_search = json!({ "googleSearch": {} });
        if let Some(Value::Array(tools)) = obj.get_mut("tools") {
            tools.push(google_search);
        } else {
            obj.insert("tools".into(), json!([google_search]));
        }
    }
}

/// Translate `audio: {voice: "Kore"}` (OpenAI-style) into Gemini's
/// native `speechConfig` + `responseModalities` shape.
fn transform_audio_output(obj: &mut Map<String, Value>) {
    let Some(audio) = obj.remove("audio") else {
        return;
    };
    let Some(voice) = audio.get("voice").and_then(|v| v.as_str()) else {
        return;
    };
    obj.insert(
        "speechConfig".into(),
        json!({
            "voiceConfig": {
                "prebuiltVoiceConfig": {
                    "voiceName": voice
                }
            }
        }),
    );
    if let Some(rm) = obj
        .get_mut("responseModalities")
        .and_then(|v| v.as_array_mut())
    {
        if !rm.iter().any(|v| v.as_str() == Some("AUDIO")) {
            rm.push(Value::String("AUDIO".into()));
        }
    } else {
        obj.insert("responseModalities".into(), json!(["AUDIO", "TEXT"]));
    }
}

/// Strip caller-supplied `cache_control` keys from messages,
/// systemInstruction, contents, and tools. The Gemini cache adapter
/// (`llm_adapter::gemini_cache`) is the authoritative emitter of
/// cache markers for this provider.
fn strip_caller_cache_control(obj: &mut Map<String, Value>) {
    fn strip(v: &mut Value) {
        match v {
            Value::Object(m) => {
                m.remove("cache_control");
                for (_, vv) in m.iter_mut() {
                    strip(vv);
                }
            }
            Value::Array(a) => {
                for vv in a.iter_mut() {
                    strip(vv);
                }
            }
            _ => {}
        }
    }
    // Walk only the fields we know callers could plant markers on —
    // avoid trivializing the whole body unnecessarily.
    for key in ["contents", "tools", "systemInstruction"] {
        if let Some(v) = obj.get_mut(key) {
            strip(v);
        }
    }
}

// ---------------------------------------------------------------------------
// Response translation: Gemini native → OpenAI
// ---------------------------------------------------------------------------

/// In-place transform a Gemini native response body to OpenAI
/// chat-completion shape, preserving cache counters that
/// `services::prompt_cache::extract_usage_from_response` reads back
/// later in the request flow.
fn normalize_native_response(body: &mut Value) {
    // Only act on a Gemini-shaped body. Distinguishing fingerprint:
    // top-level `candidates` array. If we already have `choices`,
    // someone (a previous pass, a test, a passthrough) already
    // normalized.
    if body.get("choices").is_some() {
        return;
    }
    let candidates_value = match body.get("candidates") {
        Some(Value::Array(_)) => body.get("candidates").cloned(),
        _ => return,
    };
    let candidates = candidates_value
        .as_ref()
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();

    let model = body
        .get("modelVersion")
        .or_else(|| body.get("model"))
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let id = body
        .get("responseId")
        .and_then(|v| v.as_str())
        .unwrap_or("gemini_proxy")
        .to_string();

    let mut choices: Vec<Value> = Vec::with_capacity(candidates.len());
    for (idx, cand) in candidates.into_iter().enumerate() {
        let finish_reason = match cand
            .get("finishReason")
            .and_then(|f| f.as_str())
            .unwrap_or("STOP")
        {
            "STOP" => "stop",
            "MAX_TOKENS" => "length",
            "SAFETY" => "content_filter",
            "RECITATION" => "content_filter",
            "OTHER" => "stop",
            other => other,
        };

        let parts = cand
            .get("content")
            .and_then(|c| c.get("parts"))
            .and_then(|p| p.as_array())
            .cloned()
            .unwrap_or_default();
        let mut text_buf = String::new();
        let mut tool_calls: Vec<Value> = Vec::new();
        for (call_idx, part) in parts.iter().enumerate() {
            if let Some(text) = part.get("text").and_then(|t| t.as_str()) {
                text_buf.push_str(text);
            } else if let Some(fc) = part.get("functionCall") {
                let name = fc.get("name").and_then(|n| n.as_str()).unwrap_or("");
                let args = fc.get("args").cloned().unwrap_or(json!({}));
                tool_calls.push(json!({
                    "index": call_idx,
                    // Synthesise a stable id; Gemini doesn't return one,
                    // but downstream clients (and the matching tool
                    // result on the next turn) need one.
                    "id": format!("call_{idx}_{call_idx}"),
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": args.to_string(),
                    }
                }));
            }
            // Other part types (inline_data, executableCode, codeExecutionResult)
            // are NOT yet surfaced through the OpenAI shape; future
            // multimodal-response support lands here.
        }

        let mut message = serde_json::Map::new();
        message.insert("role".into(), Value::String("assistant".into()));
        if !text_buf.is_empty() || tool_calls.is_empty() {
            message.insert("content".into(), Value::String(text_buf));
        } else {
            message.insert("content".into(), Value::Null);
        }
        if !tool_calls.is_empty() {
            message.insert("tool_calls".into(), Value::Array(tool_calls));
        }

        choices.push(json!({
            "index": idx,
            "message": Value::Object(message),
            "finish_reason": finish_reason,
        }));
    }

    // Usage. Preserve cachedContentTokenCount alongside the standard
    // counters; the prompt-cache layer reads it on its way back through
    // `extract_usage_from_response`.
    let usage_in = body.get("usageMetadata");
    let prompt_tokens = usage_in
        .and_then(|u| u.get("promptTokenCount"))
        .and_then(|v| v.as_i64())
        .unwrap_or(0);
    let completion_tokens = usage_in
        .and_then(|u| u.get("candidatesTokenCount"))
        .and_then(|v| v.as_i64())
        .unwrap_or(0);
    let cached_content_tokens = usage_in
        .and_then(|u| u.get("cachedContentTokenCount"))
        .and_then(|v| v.as_i64())
        .unwrap_or(0);

    let mut usage_out = serde_json::Map::new();
    usage_out.insert("prompt_tokens".into(), json!(prompt_tokens));
    usage_out.insert("completion_tokens".into(), json!(completion_tokens));
    usage_out.insert(
        "total_tokens".into(),
        json!(prompt_tokens + completion_tokens),
    );
    if cached_content_tokens > 0 {
        // Two forms — keep the native counter for the prompt-cache
        // extractor (which reads via `usageMetadata.cachedContentTokenCount`),
        // and surface the OpenAI-shape `prompt_tokens_details.cached_tokens`
        // so OpenAI-style observers see something familiar.
        usage_out.insert(
            "cachedContentTokenCount".into(),
            json!(cached_content_tokens),
        );
        usage_out.insert(
            "prompt_tokens_details".into(),
            json!({ "cached_tokens": cached_content_tokens }),
        );
    }

    *body = json!({
        "id": format!("chatcmpl-{id}"),
        "object": "chat.completion",
        "created": chrono::Utc::now().timestamp(),
        "model": model,
        "choices": choices,
        "usage": Value::Object(usage_out.clone()),
        // `extract_gemini_usage` reads from `usageMetadata` —
        // keep it on the normalized body so the prompt-cache layer
        // can still find the native shape.
        "usageMetadata": {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": completion_tokens,
            "totalTokenCount": prompt_tokens + completion_tokens,
            "cachedContentTokenCount": cached_content_tokens,
        },
    });
}

// ---------------------------------------------------------------------------
// Streaming normalizer: Gemini SSE → OpenAI SSE
// ---------------------------------------------------------------------------

/// Stateful converter for Gemini's `:streamGenerateContent?alt=sse`
/// stream into OpenAI-shape chat-completion SSE chunks. The shape
/// invariants mirror what `BrutorChatTransport.processResponseStream`
/// expects on the client side: chunks with `{choices: [{delta:...}]}`
/// and a final `[DONE]` sentinel.
pub struct GeminiStreamNormalizer {
    chat_id: String,
    /// Per-candidate tool-call indices so deltas can update the same
    /// tool_call across multiple chunks.
    tool_call_indices: HashMap<String, usize>,
}

impl GeminiStreamNormalizer {
    pub fn new() -> Self {
        Self {
            chat_id: format!(
                "chatcmpl-gemini-{}",
                chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
            ),
            tool_call_indices: HashMap::new(),
        }
    }

    fn next_tool_index(&mut self, candidate_idx: usize) -> usize {
        let key = candidate_idx.to_string();
        let n = self.tool_call_indices.entry(key).or_insert(0);
        let v = *n;
        *n += 1;
        v
    }
}

impl Default for GeminiStreamNormalizer {
    fn default() -> Self {
        Self::new()
    }
}

impl StreamNormalizer for GeminiStreamNormalizer {
    fn convert(&mut self, data: &str) -> Vec<String> {
        if data.trim() == "[DONE]" {
            return vec!["[DONE]".to_string()];
        }
        let Ok(value): Result<Value, _> = serde_json::from_str(data) else {
            // Forward malformed payloads unchanged so debuggers can see
            // them. The client's processResponseStream skips frames it
            // can't parse.
            return vec![data.to_string()];
        };

        let candidates = value
            .get("candidates")
            .and_then(|c| c.as_array())
            .cloned()
            .unwrap_or_default();
        let usage = value.get("usageMetadata");
        let model = value
            .get("modelVersion")
            .or_else(|| value.get("model"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();

        let mut out = Vec::new();
        for cand in candidates {
            let idx = cand.get("index").and_then(|i| i.as_u64()).unwrap_or(0) as usize;
            let parts = cand
                .get("content")
                .and_then(|c| c.get("parts"))
                .and_then(|p| p.as_array())
                .cloned()
                .unwrap_or_default();
            let mut text_delta = String::new();
            let mut tool_call_deltas: Vec<Value> = Vec::new();
            for part in parts {
                if let Some(text) = part.get("text").and_then(|t| t.as_str()) {
                    text_delta.push_str(text);
                } else if let Some(fc) = part.get("functionCall") {
                    let name = fc.get("name").and_then(|n| n.as_str()).unwrap_or("");
                    let args = fc.get("args").cloned().unwrap_or(json!({}));
                    let call_idx = self.next_tool_index(idx);
                    tool_call_deltas.push(json!({
                        "index": call_idx,
                        "id": format!("call_{idx}_{call_idx}"),
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": args.to_string(),
                        }
                    }));
                }
            }

            let finish_reason_str = cand.get("finishReason").and_then(|f| f.as_str());
            let finish_reason = finish_reason_str.map(|fr| match fr {
                "STOP" => "stop",
                "MAX_TOKENS" => "length",
                "SAFETY" | "RECITATION" => "content_filter",
                "OTHER" => "stop",
                other => other,
            });

            let mut delta = serde_json::Map::new();
            if !text_delta.is_empty() {
                delta.insert("content".into(), Value::String(text_delta));
            }
            if !tool_call_deltas.is_empty() {
                delta.insert("tool_calls".into(), Value::Array(tool_call_deltas));
            }
            // The first chunk for a stream MUST carry role:"assistant" — the
            // SDK uses it to seed the message shape.
            delta.insert("role".into(), Value::String("assistant".into()));

            let mut choice = serde_json::Map::new();
            choice.insert("index".into(), json!(idx));
            choice.insert("delta".into(), Value::Object(delta));
            if let Some(fr) = finish_reason {
                choice.insert("finish_reason".into(), Value::String(fr.to_string()));
            }

            let chunk = json!({
                "id": self.chat_id,
                "object": "chat.completion.chunk",
                "created": chrono::Utc::now().timestamp(),
                "model": model,
                "choices": [Value::Object(choice)],
            });

            out.push(serde_json::to_string(&chunk).unwrap_or_default());
        }

        // Emit a terminal chunk carrying usage when Gemini surfaces it.
        // OpenAI's SSE convention is a final chunk with `usage` set;
        // the prompt-cache extractor accumulates these via
        // `usage_from_stream_totals`.
        if let Some(u) = usage {
            let prompt_tokens = u
                .get("promptTokenCount")
                .and_then(|v| v.as_i64())
                .unwrap_or(0);
            let completion_tokens = u
                .get("candidatesTokenCount")
                .and_then(|v| v.as_i64())
                .unwrap_or(0);
            let cached_content_tokens = u
                .get("cachedContentTokenCount")
                .and_then(|v| v.as_i64())
                .unwrap_or(0);

            let mut usage_block = serde_json::Map::new();
            usage_block.insert("prompt_tokens".into(), json!(prompt_tokens));
            usage_block.insert("completion_tokens".into(), json!(completion_tokens));
            usage_block.insert(
                "total_tokens".into(),
                json!(prompt_tokens + completion_tokens),
            );
            if cached_content_tokens > 0 {
                usage_block.insert(
                    "cachedContentTokenCount".into(),
                    json!(cached_content_tokens),
                );
                usage_block.insert(
                    "prompt_tokens_details".into(),
                    json!({ "cached_tokens": cached_content_tokens }),
                );
            }
            let usage_chunk = json!({
                "id": self.chat_id,
                "object": "chat.completion.chunk",
                "created": chrono::Utc::now().timestamp(),
                "model": model,
                "choices": [],
                "usage": Value::Object(usage_block),
            });
            out.push(serde_json::to_string(&usage_chunk).unwrap_or_default());
        }

        out
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
// Test names embed Gemini's wire field names verbatim — `systemInstruction`,
// `functionCall`, `functionDeclarations`, `additionalProperties`. Those are
// camelCase in the API, and snake_casing them here would break the link
// between each test and the exact field it pins.
#[allow(non_snake_case)]
mod tests {
    use super::*;
    use crate::services::llm_adapter::make_test_model;
    use serde_json::json;

    // ── chat_endpoint / auth_headers ─────────────────────────────────

    #[test]
    fn chat_endpoint_non_streaming() {
        let model = make_test_model("gemini", "gemini-2.5-flash");
        let adapter = GeminiAdapter;
        assert_eq!(
            adapter.chat_endpoint(&model, "/chat/completions", false),
            "/v1beta/models/gemini-2.5-flash:generateContent"
        );
    }

    #[test]
    fn chat_endpoint_streaming() {
        let model = make_test_model("gemini", "gemini-2.5-pro");
        let adapter = GeminiAdapter;
        assert_eq!(
            adapter.chat_endpoint(&model, "/chat/completions", true),
            "/v1beta/models/gemini-2.5-pro:streamGenerateContent?alt=sse"
        );
    }

    #[test]
    fn chat_endpoint_strips_double_models_prefix() {
        // Operators occasionally save the fully-qualified name
        // `models/gemini-2.5-pro`. The adapter must not produce
        // `/v1beta/models/models/...`.
        let model = make_test_model("gemini", "models/gemini-2.5-pro");
        let adapter = GeminiAdapter;
        assert_eq!(
            adapter.chat_endpoint(&model, "/chat/completions", false),
            "/v1beta/models/gemini-2.5-pro:generateContent"
        );
    }

    #[test]
    fn auth_headers_uses_x_goog_api_key() {
        let mut model = make_test_model("gemini", "gemini-2.5-flash");
        model.api_key = Some("AIza-test".to_string());
        let adapter = GeminiAdapter;
        let h = adapter.auth_headers(&model);
        assert_eq!(h.len(), 1);
        assert_eq!(h[0].0, "x-goog-api-key");
        assert_eq!(h[0].1, "AIza-test");
    }

    #[test]
    fn auth_headers_empty_when_no_api_key() {
        let mut model = make_test_model("gemini", "gemini-2.5-flash");
        model.api_key = None;
        let adapter = GeminiAdapter;
        assert!(adapter.auth_headers(&model).is_empty());
    }

    // ── transform_messages_to_contents ───────────────────────────────

    #[test]
    fn extracts_system_message_into_systemInstruction() {
        let adapter = GeminiAdapter;
        let model = make_test_model("gemini", "gemini-2.5-flash");
        let mut obj = serde_json::Map::new();
        obj.insert(
            "messages".into(),
            json!([
                {"role": "system", "content": "be helpful"},
                {"role": "user", "content": "hi"}
            ]),
        );
        adapter.transform_request(&mut obj, &model);
        assert_eq!(
            obj.get("systemInstruction"),
            Some(&json!({"parts": [{"text": "be helpful"}]})),
        );
        let contents = obj.get("contents").unwrap().as_array().unwrap();
        assert_eq!(contents.len(), 1);
        assert_eq!(contents[0]["role"], "user");
        assert_eq!(contents[0]["parts"][0]["text"], "hi");
    }

    #[test]
    fn merges_multiple_system_messages_with_newline_separator() {
        // The merge puts server's system_prompt + caller's system at the
        // front of messages. The Gemini adapter joins multi-system into
        // a single systemInstruction block — matching the Anthropic
        // pattern for cross-provider consistency.
        let adapter = GeminiAdapter;
        let model = make_test_model("gemini", "gemini-2.5-flash");
        let mut obj = serde_json::Map::new();
        obj.insert(
            "messages".into(),
            json!([
                {"role": "system", "content": "[server] core policy"},
                {"role": "system", "content": "[caller] persona"},
                {"role": "user", "content": "hi"}
            ]),
        );
        adapter.transform_request(&mut obj, &model);
        let si = obj.get("systemInstruction").unwrap();
        let text = si["parts"][0]["text"].as_str().unwrap();
        assert!(text.contains("[server] core policy"));
        assert!(text.contains("[caller] persona"));
        // Server's prompt appears first → stable, cacheable prefix.
        assert!(text.find("[server]").unwrap() < text.find("[caller]").unwrap());
    }

    #[test]
    fn maps_assistant_role_to_model() {
        let adapter = GeminiAdapter;
        let model = make_test_model("gemini", "gemini-2.5-flash");
        let mut obj = serde_json::Map::new();
        obj.insert(
            "messages".into(),
            json!([
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "a"},
                {"role": "user", "content": "q2"}
            ]),
        );
        adapter.transform_request(&mut obj, &model);
        let contents = obj.get("contents").unwrap().as_array().unwrap();
        assert_eq!(contents.len(), 3);
        assert_eq!(contents[0]["role"], "user");
        assert_eq!(contents[1]["role"], "model");
        assert_eq!(contents[2]["role"], "user");
    }

    #[test]
    fn translates_assistant_tool_calls_to_functionCall_parts() {
        let adapter = GeminiAdapter;
        let model = make_test_model("gemini", "gemini-2.5-flash");
        let mut obj = serde_json::Map::new();
        obj.insert(
            "messages".into(),
            json!([
                {"role": "user", "content": "do thing"},
                {
                    "role": "assistant",
                    "content": null,
                    "tool_calls": [{
                        "id": "tc1",
                        "type": "function",
                        "function": {
                            "name": "do_thing",
                            "arguments": "{\"x\": 1}"
                        }
                    }]
                },
                {
                    "role": "tool",
                    "tool_call_id": "tc1",
                    "content": "{\"result\": \"done\"}"
                }
            ]),
        );
        adapter.transform_request(&mut obj, &model);
        let contents = obj.get("contents").unwrap().as_array().unwrap();
        // user, model (functionCall), user (functionResponse)
        assert_eq!(contents.len(), 3);
        assert_eq!(contents[1]["role"], "model");
        assert_eq!(contents[1]["parts"][0]["functionCall"]["name"], "do_thing");
        assert_eq!(contents[1]["parts"][0]["functionCall"]["args"]["x"], 1);
        // Tool result becomes a `user` role with functionResponse part,
        // carrying the function name reconstructed from tool_call_id.
        assert_eq!(contents[2]["role"], "user");
        let fr = &contents[2]["parts"][0]["functionResponse"];
        assert_eq!(fr["name"], "do_thing");
        assert_eq!(fr["response"]["result"], "done");
    }

    // ── transform_tools ──────────────────────────────────────────────

    #[test]
    fn groups_function_tools_under_functionDeclarations() {
        let adapter = GeminiAdapter;
        let model = make_test_model("gemini", "gemini-2.5-flash");
        let mut obj = serde_json::Map::new();
        obj.insert("messages".into(), json!([{"role": "user", "content": "x"}]));
        obj.insert(
            "tools".into(),
            json!([
                {"type": "function", "function": {"name": "a", "description": "A", "parameters": {"type":"object"}}},
                {"type": "function", "function": {"name": "b", "parameters": {"type":"object"}}}
            ]),
        );
        adapter.transform_request(&mut obj, &model);
        let tools = obj.get("tools").unwrap().as_array().unwrap();
        assert_eq!(tools.len(), 1);
        let decls = tools[0]["functionDeclarations"].as_array().unwrap();
        assert_eq!(decls.len(), 2);
        assert_eq!(decls[0]["name"], "a");
        assert_eq!(decls[1]["name"], "b");
    }

    #[test]
    fn function_parameters_strip_additionalProperties() {
        // Regression for the health-check incident where every
        // Gemini model went unhealthy with
        // `Unknown name "additionalProperties" at tools[0].function_declarations[0].parameters`.
        // Pydantic and similar OpenAI-shape tool builders emit
        // `additionalProperties: false` on every object schema;
        // Google's native REST rejects it. The adapter MUST strip.
        let adapter = GeminiAdapter;
        let model = make_test_model("gemini", "gemini-2.5-flash");
        let mut obj = serde_json::Map::new();
        obj.insert("messages".into(), json!([{"role": "user", "content": "x"}]));
        obj.insert(
            "tools".into(),
            json!([{
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "Search the docs",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "The search query"
                            },
                            "filters": {
                                "type": "object",
                                "additionalProperties": false,
                                "properties": {
                                    "tag": {"type": "string"}
                                }
                            }
                        },
                        "additionalProperties": false,
                        "required": ["query"]
                    }
                }
            }]),
        );
        adapter.transform_request(&mut obj, &model);

        let params = &obj.get("tools").unwrap()[0]["functionDeclarations"][0]["parameters"];
        // Top-level `additionalProperties` must be gone.
        assert!(
            params.get("additionalProperties").is_none(),
            "top-level additionalProperties must be stripped: {params}"
        );
        // Nested `additionalProperties` inside `properties.filters`
        // must also be stripped — the recursion has to walk in.
        let nested = &params["properties"]["filters"];
        assert!(
            nested.get("additionalProperties").is_none(),
            "nested additionalProperties must be stripped: {nested}"
        );
        // Functional fields survive — we only strip the unsupported keys.
        assert!(params.get("type").is_some());
        assert!(params.get("properties").is_some());
        assert!(params.get("required").is_some());
    }

    #[test]
    fn function_parameters_strip_other_json_schema_isms() {
        // Same regression class — these other JSON Schema keys also
        // 400 from Google's native API. Bundled in one test so the
        // unsupported-keys list stays in sync.
        let adapter = GeminiAdapter;
        let model = make_test_model("gemini", "gemini-2.5-flash");
        let mut obj = serde_json::Map::new();
        obj.insert("messages".into(), json!([{"role": "user", "content": "x"}]));
        obj.insert(
            "tools".into(),
            json!([{
                "type": "function",
                "function": {
                    "name": "x",
                    "parameters": {
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "$id": "https://example.com/schemas/x",
                        "$defs": {"Foo": {"type": "string"}},
                        "type": "object",
                        "properties": {"q": {"type": "string"}}
                    }
                }
            }]),
        );
        adapter.transform_request(&mut obj, &model);
        let params = &obj.get("tools").unwrap()[0]["functionDeclarations"][0]["parameters"];
        for forbidden in ["$schema", "$id", "$defs", "definitions"] {
            assert!(
                params.get(forbidden).is_none(),
                "{forbidden:?} must be stripped from parameters: {params}"
            );
        }
    }

    #[test]
    fn passes_through_native_googleSearch_and_codeExecution() {
        let adapter = GeminiAdapter;
        let model = make_test_model("gemini", "gemini-2.5-flash");
        let mut obj = serde_json::Map::new();
        obj.insert("messages".into(), json!([{"role": "user", "content": "x"}]));
        obj.insert(
            "tools".into(),
            json!([
                {"type": "function", "function": {"name": "a", "parameters": {"type":"object"}}},
                {"googleSearch": {}},
                {"codeExecution": {}}
            ]),
        );
        adapter.transform_request(&mut obj, &model);
        let tools = obj.get("tools").unwrap().as_array().unwrap();
        assert_eq!(tools.len(), 3);
        // First entry is the functionDeclarations group; the others
        // are native tools as-is.
        assert!(tools[0].get("functionDeclarations").is_some());
        assert!(tools.iter().any(|t| t.get("googleSearch").is_some()));
        assert!(tools.iter().any(|t| t.get("codeExecution").is_some()));
    }

    // ── transform_tool_choice ────────────────────────────────────────

    #[test]
    fn tool_choice_auto_to_AUTO_mode() {
        let adapter = GeminiAdapter;
        let model = make_test_model("gemini", "gemini-2.5-flash");
        let mut obj = serde_json::Map::new();
        obj.insert("messages".into(), json!([{"role": "user", "content": "x"}]));
        obj.insert("tool_choice".into(), json!("auto"));
        // Gemini rejects toolConfig without functionDeclarations, so
        // toolConfig only gets emitted when the body actually has
        // tools to pair it with.
        obj.insert(
            "tools".into(),
            json!([{"type": "function", "function": {"name": "weather", "parameters": {"type":"object"}}}]),
        );
        adapter.transform_request(&mut obj, &model);
        assert_eq!(
            obj.get("toolConfig").unwrap()["functionCallingConfig"]["mode"],
            "AUTO"
        );
    }

    #[test]
    fn tool_choice_null_does_not_synthesize_toolConfig() {
        // ChatCompletionRequest serializes the unset `Option<Value>`
        // tool_choice as JSON null. Without the explicit null-skip in
        // `transform_tool_choice`, the catchall arm synthesises a
        // toolConfig with mode=AUTO — and Google's native endpoint
        // 400s with "Function calling config is set without
        // function_declarations" when the body has no tools to
        // declare. This pins the fix.
        let adapter = GeminiAdapter;
        let model = make_test_model("gemini", "gemini-2.5-flash");
        let mut obj = serde_json::Map::new();
        obj.insert("messages".into(), json!([{"role": "user", "content": "x"}]));
        obj.insert("tools".into(), Value::Null);
        obj.insert("tool_choice".into(), Value::Null);
        adapter.transform_request(&mut obj, &model);
        assert!(
            !obj.contains_key("toolConfig"),
            "null tool_choice must NOT produce toolConfig: body = {:?}",
            obj
        );
        // Also: null tools must not survive as a top-level field.
        assert!(!obj.contains_key("tools"));
    }

    #[test]
    fn tool_choice_auto_without_tools_does_not_synthesize_toolConfig() {
        // Even when the caller explicitly sets tool_choice="auto",
        // Gemini rejects toolConfig if there are no functionDeclarations.
        // Skip in that case.
        let adapter = GeminiAdapter;
        let model = make_test_model("gemini", "gemini-2.5-flash");
        let mut obj = serde_json::Map::new();
        obj.insert("messages".into(), json!([{"role": "user", "content": "x"}]));
        obj.insert("tool_choice".into(), json!("auto"));
        // no tools field at all
        adapter.transform_request(&mut obj, &model);
        assert!(!obj.contains_key("toolConfig"));
    }

    #[test]
    fn tool_choice_specific_function_to_ANY_with_allowedFunctionNames() {
        let adapter = GeminiAdapter;
        let model = make_test_model("gemini", "gemini-2.5-flash");
        let mut obj = serde_json::Map::new();
        obj.insert("messages".into(), json!([{"role": "user", "content": "x"}]));
        obj.insert(
            "tools".into(),
            json!([{"type": "function", "function": {"name": "weather", "parameters": {"type":"object"}}}]),
        );
        obj.insert(
            "tool_choice".into(),
            json!({"type": "function", "function": {"name": "weather"}}),
        );
        adapter.transform_request(&mut obj, &model);
        let cfg = &obj.get("toolConfig").unwrap()["functionCallingConfig"];
        assert_eq!(cfg["mode"], "ANY");
        assert_eq!(cfg["allowedFunctionNames"], json!(["weather"]));
    }

    // ── transform_generation_config ─────────────────────────────────

    #[test]
    fn moves_sampling_params_into_generationConfig() {
        let adapter = GeminiAdapter;
        let model = make_test_model("gemini", "gemini-2.5-flash");
        let mut obj = serde_json::Map::new();
        obj.insert("messages".into(), json!([{"role": "user", "content": "x"}]));
        obj.insert("temperature".into(), json!(0.3));
        obj.insert("top_p".into(), json!(0.9));
        obj.insert("max_tokens".into(), json!(512));
        obj.insert("n".into(), json!(2));
        obj.insert("stop".into(), json!(["END"]));
        adapter.transform_request(&mut obj, &model);

        // Top-level params have moved out — Gemini rejects them at top level.
        assert!(!obj.contains_key("temperature"));
        assert!(!obj.contains_key("top_p"));
        assert!(!obj.contains_key("max_tokens"));
        assert!(!obj.contains_key("n"));
        assert!(!obj.contains_key("stop"));

        let cfg = obj.get("generationConfig").unwrap();
        assert_eq!(cfg["temperature"], 0.3);
        assert_eq!(cfg["topP"], 0.9);
        assert_eq!(cfg["maxOutputTokens"], 512);
        assert_eq!(cfg["candidateCount"], 2);
        assert_eq!(cfg["stopSequences"], json!(["END"]));
    }

    #[test]
    fn response_format_json_schema_routes_to_responseSchema() {
        let adapter = GeminiAdapter;
        let model = make_test_model("gemini", "gemini-2.5-flash");
        let mut obj = serde_json::Map::new();
        obj.insert("messages".into(), json!([{"role": "user", "content": "x"}]));
        obj.insert(
            "response_format".into(),
            json!({
                "type": "json_schema",
                "json_schema": {"schema": {"type": "object"}}
            }),
        );
        adapter.transform_request(&mut obj, &model);
        let cfg = obj.get("generationConfig").unwrap();
        assert_eq!(cfg["response_mime_type"], "application/json");
        assert_eq!(cfg["response_schema"], json!({"type": "object"}));
    }

    // ── transform_thinking ──────────────────────────────────────────

    #[test]
    fn thinking_enabled_injects_default_thinkingConfig() {
        let adapter = GeminiAdapter;
        let mut model = make_test_model("gemini", "gemini-2.5-pro");
        model.thinking_enabled = Some(true);
        let mut obj = serde_json::Map::new();
        obj.insert("messages".into(), json!([{"role": "user", "content": "x"}]));
        adapter.transform_request(&mut obj, &model);
        let cfg = obj.get("generationConfig").unwrap();
        assert!(cfg["thinkingConfig"]["thinkingBudget"].as_i64().unwrap() > 0);
    }

    #[test]
    fn caller_thinking_config_snake_case_normalized_to_camelCase() {
        let adapter = GeminiAdapter;
        let model = make_test_model("gemini", "gemini-2.5-pro");
        let mut obj = serde_json::Map::new();
        obj.insert("messages".into(), json!([{"role": "user", "content": "x"}]));
        obj.insert("thinking_config".into(), json!({"thinking_budget": 5000}));
        adapter.transform_request(&mut obj, &model);
        let cfg = obj.get("generationConfig").unwrap();
        assert_eq!(cfg["thinkingConfig"]["thinkingBudget"], 5000);
    }

    // ── strips OpenAI-only params ────────────────────────────────────

    #[test]
    fn drops_openai_only_penalty_params() {
        let adapter = GeminiAdapter;
        let model = make_test_model("gemini", "gemini-2.5-flash");
        let mut obj = serde_json::Map::new();
        obj.insert("messages".into(), json!([{"role": "user", "content": "x"}]));
        obj.insert("frequency_penalty".into(), json!(0.5));
        obj.insert("presence_penalty".into(), json!(0.5));
        obj.insert("logit_bias".into(), json!({}));
        obj.insert("parallel_tool_calls".into(), json!(true));
        adapter.transform_request(&mut obj, &model);
        assert!(!obj.contains_key("frequency_penalty"));
        assert!(!obj.contains_key("presence_penalty"));
        assert!(!obj.contains_key("logit_bias"));
        assert!(!obj.contains_key("parallel_tool_calls"));
    }

    // ── strip_caller_cache_control ──────────────────────────────────

    #[test]
    fn strips_caller_supplied_cache_control() {
        let adapter = GeminiAdapter;
        let model = make_test_model("gemini", "gemini-2.5-flash");
        let mut obj = serde_json::Map::new();
        obj.insert(
            "messages".into(),
            json!([{
                "role": "system",
                "content": "be helpful",
                "cache_control": {"type": "ephemeral"}
            }, {
                "role": "user",
                "content": "x"
            }]),
        );
        obj.insert(
            "tools".into(),
            json!([{
                "type": "function",
                "function": {"name": "t1", "parameters": {"type":"object"}},
                "cache_control": {"type": "ephemeral"}
            }]),
        );
        adapter.transform_request(&mut obj, &model);
        // Neither tools nor systemInstruction should carry caller markers.
        let tools_str = serde_json::to_string(obj.get("tools").unwrap()).unwrap();
        assert!(!tools_str.contains("cache_control"));
        let si_str = serde_json::to_string(obj.get("systemInstruction").unwrap()).unwrap();
        assert!(!si_str.contains("cache_control"));
    }

    // ── normalize_response ──────────────────────────────────────────

    #[test]
    fn normalize_response_translates_text_to_choices() {
        let adapter = GeminiAdapter;
        let mut body = json!({
            "candidates": [{
                "content": {
                    "role": "model",
                    "parts": [{"text": "Hello world."}]
                },
                "finishReason": "STOP",
                "index": 0
            }],
            "usageMetadata": {
                "promptTokenCount": 12,
                "candidatesTokenCount": 4,
                "totalTokenCount": 16
            },
            "modelVersion": "gemini-2.5-flash"
        });
        adapter.normalize_response(&mut body);
        assert_eq!(body["object"], "chat.completion");
        assert_eq!(body["model"], "gemini-2.5-flash");
        assert_eq!(body["choices"][0]["message"]["role"], "assistant");
        assert_eq!(body["choices"][0]["message"]["content"], "Hello world.");
        assert_eq!(body["choices"][0]["finish_reason"], "stop");
        assert_eq!(body["usage"]["prompt_tokens"], 12);
        assert_eq!(body["usage"]["completion_tokens"], 4);
        assert_eq!(body["usage"]["total_tokens"], 16);
    }

    #[test]
    fn normalize_response_preserves_cachedContentTokenCount() {
        // Regression for the bug that hid Gemini caching from
        // proxy_logs — the native counter must survive normalization
        // so `gemini_cache::extract_usage` can read it.
        let adapter = GeminiAdapter;
        let mut body = json!({
            "candidates": [{
                "content": {"role": "model", "parts": [{"text": "ok"}]},
                "finishReason": "STOP",
                "index": 0
            }],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 5,
                "cachedContentTokenCount": 3500
            },
            "modelVersion": "gemini-2.5-pro"
        });
        adapter.normalize_response(&mut body);
        assert_eq!(body["usageMetadata"]["cachedContentTokenCount"], 3500);
        // OpenAI-shape mirror also present so OpenAI-aware observers
        // see a familiar field.
        assert_eq!(
            body["usage"]["prompt_tokens_details"]["cached_tokens"],
            3500,
        );
    }

    #[test]
    fn normalize_response_translates_functionCall_to_tool_calls() {
        let adapter = GeminiAdapter;
        let mut body = json!({
            "candidates": [{
                "content": {
                    "role": "model",
                    "parts": [{
                        "functionCall": {
                            "name": "get_weather",
                            "args": {"city": "Stockholm"}
                        }
                    }]
                },
                "finishReason": "STOP",
                "index": 0
            }],
            "usageMetadata": {
                "promptTokenCount": 50,
                "candidatesTokenCount": 12
            },
            "modelVersion": "gemini-2.5-flash"
        });
        adapter.normalize_response(&mut body);
        let tc = &body["choices"][0]["message"]["tool_calls"][0];
        assert_eq!(tc["type"], "function");
        assert_eq!(tc["function"]["name"], "get_weather");
        // OpenAI shape requires `arguments` as a JSON string.
        assert_eq!(tc["function"]["arguments"], "{\"city\":\"Stockholm\"}");
    }

    #[test]
    fn normalize_response_passes_through_already_normalized_body() {
        // Defensive: if upstream returned an OpenAI-shape body (e.g. a
        // future Vertex passthrough that already did the normalization),
        // don't double-normalize.
        let adapter = GeminiAdapter;
        let original = json!({
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        });
        let mut body = original.clone();
        adapter.normalize_response(&mut body);
        assert_eq!(body, original);
    }

    // ── Streaming normalizer ────────────────────────────────────────

    #[test]
    fn stream_normalizer_emits_openai_chunk_for_text_delta() {
        let mut norm = GeminiStreamNormalizer::new();
        let frame = json!({
            "candidates": [{
                "content": {"role": "model", "parts": [{"text": "hello"}]},
                "index": 0
            }],
            "modelVersion": "gemini-2.5-flash"
        });
        let out = norm.convert(&frame.to_string());
        assert_eq!(out.len(), 1);
        let chunk: Value = serde_json::from_str(&out[0]).unwrap();
        assert_eq!(chunk["object"], "chat.completion.chunk");
        assert_eq!(chunk["choices"][0]["delta"]["content"], "hello");
        assert_eq!(chunk["choices"][0]["delta"]["role"], "assistant");
    }

    #[test]
    fn stream_normalizer_emits_usage_chunk_when_present() {
        let mut norm = GeminiStreamNormalizer::new();
        let frame = json!({
            "candidates": [],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 50,
                "cachedContentTokenCount": 80
            },
            "modelVersion": "gemini-2.5-flash"
        });
        let out = norm.convert(&frame.to_string());
        // Just the usage chunk (no candidates → no content chunks).
        assert_eq!(out.len(), 1);
        let chunk: Value = serde_json::from_str(&out[0]).unwrap();
        assert_eq!(chunk["usage"]["prompt_tokens"], 100);
        assert_eq!(chunk["usage"]["completion_tokens"], 50);
        assert_eq!(chunk["usage"]["cachedContentTokenCount"], 80);
        assert_eq!(chunk["usage"]["prompt_tokens_details"]["cached_tokens"], 80,);
    }

    #[test]
    fn stream_normalizer_passes_through_done() {
        let mut norm = GeminiStreamNormalizer::new();
        let out = norm.convert("[DONE]");
        assert_eq!(out, vec!["[DONE]"]);
    }

    #[test]
    fn stream_normalizer_handles_finish_reason() {
        let mut norm = GeminiStreamNormalizer::new();
        let frame = json!({
            "candidates": [{
                "content": {"role": "model", "parts": [{"text": "."}]},
                "index": 0,
                "finishReason": "MAX_TOKENS"
            }],
            "modelVersion": "gemini-2.5-flash"
        });
        let out = norm.convert(&frame.to_string());
        let chunk: Value = serde_json::from_str(&out[0]).unwrap();
        assert_eq!(chunk["choices"][0]["finish_reason"], "length");
    }
}
