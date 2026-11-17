// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

use serde_json::{Value, json};

use super::{LlmAdapter, StreamNormalizer, content_to_blocks, filter_schema_constraints};
use crate::models::db::LlmModel;

// ---------------------------------------------------------------------------
// Anthropic adapter
// ---------------------------------------------------------------------------

pub struct AnthropicAdapter;

impl LlmAdapter for AnthropicAdapter {
    fn provider_name(&self) -> &str {
        "anthropic"
    }

    fn chat_endpoint(&self, _model: &LlmModel, _default: &str, _stream: bool) -> String {
        "/messages".to_string()
    }

    fn auth_headers(&self, model: &LlmModel) -> Vec<(String, String)> {
        let mut headers = Vec::new();
        if let Some(ref key) = model.api_key {
            headers.push(("x-api-key".into(), key.clone()));
        }
        headers.push(("anthropic-version".into(), "2023-06-01".into()));
        headers
    }

    fn transform_request(&self, obj: &mut serde_json::Map<String, Value>, model: &LlmModel) {
        // Remove null values — Anthropic rejects null for most fields
        let null_keys: Vec<String> = obj
            .iter()
            .filter(|(_, v)| v.is_null())
            .map(|(k, _)| k.clone())
            .collect();
        for k in null_keys {
            obj.remove(&k);
        }

        // max_completion_tokens → max_tokens
        if let Some(mct) = obj.remove("max_completion_tokens") {
            obj.insert("max_tokens".into(), mct);
        }

        // Ensure max_tokens is set (Anthropic requires it). Prefer the
        // model's configured `default_max_tokens` over a blanket 4096 so
        // operators who set e.g. 512 on the model actually see it on the
        // wire — matches the batch path (batch/providers/anthropic.rs).
        match obj.get("max_tokens") {
            Some(v) if !v.is_null() && v.as_i64().unwrap_or(0) > 0 => {}
            _ => {
                let fallback = model.default_max_tokens.unwrap_or(4096);
                obj.insert("max_tokens".into(), Value::from(fallback));
            }
        }

        // stop → stop_sequences
        if let Some(stop) = obj.remove("stop") {
            if !obj.contains_key("stop_sequences") {
                let sequences = match stop {
                    Value::Array(_) => stop,
                    Value::String(s) => Value::Array(vec![Value::String(s)]),
                    _ => Value::Array(vec![]),
                };
                if let Some(arr) = sequences.as_array() {
                    if !arr.is_empty() {
                        obj.insert("stop_sequences".into(), sequences);
                    }
                }
            }
        }

        // user → metadata.user_id
        if let Some(user) = obj.remove("user") {
            if !obj.contains_key("metadata") {
                if let Some(user_str) = user.as_str() {
                    if !user_str.is_empty() {
                        obj.insert("metadata".into(), json!({"user_id": user_str}));
                    }
                }
            }
        }

        // Extract system messages into the top-level `system` field as
        // an ARRAY of content blocks (one block per source message),
        // rather than concatenating them into a single string.
        //
        // ## Why array-of-blocks
        //
        // Anthropic accepts `system` as either a string OR an array of
        // content blocks. The array form lets the prompt-cache layer
        // attach a per-block `cache_control` marker — useful for fine-
        // grained caching when the operator's system prompt and the
        // caller's runtime instructions vary independently. The
        // single-string form collapses all sources into one cache
        // boundary; the array form preserves source granularity.
        //
        // ## Caller-supplied `cache_control` is stripped
        //
        // The Anthropic adapter is the authoritative emitter of cache
        // markers for its provider.
        // Any `cache_control` field a caller adds to a system message
        // gets dropped here so only the planner-driven markers survive.
        if let Some(messages) = obj.get_mut("messages") {
            if let Some(msgs) = messages.as_array_mut() {
                let mut system_blocks: Vec<Value> = Vec::new();
                msgs.retain(|m| {
                    if m.get("role").and_then(|r| r.as_str()) == Some("system") {
                        if let Some(content) = m.get("content").and_then(|c| c.as_str()) {
                            system_blocks.push(json!({
                                "type": "text",
                                "text": content,
                            }));
                        }
                        false
                    } else {
                        true
                    }
                });
                if !system_blocks.is_empty() && !obj.contains_key("system") {
                    obj.insert("system".into(), Value::Array(system_blocks));
                }
            }
        }

        // Strip caller-supplied `cache_control` keys from messages and
        // tools. The Anthropic-family cache adapter
        // (`llm_adapter::anthropic_cache`) is the authoritative emitter
        // of cache markers for this provider; allowing caller input to
        // persist would let an untrusted client either burn the
        // operator's cache budget or trigger 400s by placing markers
        // on positions Anthropic rejects.
        //
        // System-block stripping is handled implicitly above — we
        // rebuilt the `system` field from scratch with `{type, text}`
        // blocks only, so any caller cache_control on a system message
        // never makes it across.
        if let Some(msgs) = obj.get_mut("messages").and_then(|m| m.as_array_mut()) {
            for msg in msgs.iter_mut() {
                if let Some(mo) = msg.as_object_mut() {
                    mo.remove("cache_control");
                }
            }
        }
        if let Some(tools) = obj.get_mut("tools").and_then(|t| t.as_array_mut()) {
            for tool in tools.iter_mut() {
                if let Some(to) = tool.as_object_mut() {
                    to.remove("cache_control");
                }
            }
        }

        // Transform tool_choice
        self.transform_tool_choice(obj);

        // Transform tools from OpenAI to Anthropic format
        self.transform_tools(obj);

        // Remove empty tools array (Anthropic rejects tools: [])
        if let Some(tools) = obj.get("tools") {
            if tools.as_array().map_or(false, |a| a.is_empty()) {
                obj.remove("tools");
                obj.remove("tool_choice");
            }
        }

        // Convert OpenAI message roles/format to Anthropic format
        self.transform_messages(obj);

        // A caller's structured-output request:
        // OpenAI's `response_format: {type: "json_schema", json_schema:
        // {schema}}` is the Messages API's `output_config.format: {type:
        // "json_schema", schema}`. The schema was filtered here and then
        // removed with `response_format` below, so the request was lost
        // and the model answered free text with no error. Constraints the
        // API does not take are moved into the descriptions
        // (`filter_schema_constraints`). A caller's own `output_config`
        // (an effort) is kept. Not sent when the row says the model has no
        // structured output (`supports_response_schema: false`).
        //
        // `{type: "json_object"}` (OpenAI's JSON mode: any JSON object, no
        // schema) has no equivalent. Claude's JSON
        // output is `output_config.format` with `type: "json_schema"` and a
        // schema, and "`additionalProperties` must be `false`": for an
        // object with no listed keys "Claude can only return `{}`"
        // (<https://platform.claude.com/docs/en/build-with-claude/structured-outputs>,
        // "Objects with arbitrary keys"). So there is
        // nothing to send that means "any JSON object", and the gateway's
        // rule for a parameter a provider has no field for applies
        // (`README.md`, "Parameter dropping"): it is not sent. The drop is
        // logged, as the dropped schema above is; the caller gets Claude's
        // unconstrained answer, which a prompt asking for JSON usually
        // makes JSON and nothing guarantees.
        let row_refuses_schema = model
            .capabilities
            .as_ref()
            .and_then(|c| c.get("supports_response_schema"))
            .and_then(Value::as_bool)
            == Some(false);
        if let Some(schema) = obj
            .get("response_format")
            .and_then(|rf| rf.pointer("/json_schema/schema"))
            .cloned()
        {
            if row_refuses_schema {
                tracing::warn!(
                    model = %model.model_name,
                    "response_format json_schema dropped: the model's row states no structured output"
                );
            } else {
                let mut filtered = schema;
                filter_schema_constraints(&mut filtered);
                let format = json!({"type": "json_schema", "schema": filtered});
                match obj.get_mut("output_config").and_then(Value::as_object_mut) {
                    Some(config) => {
                        config.entry("format").or_insert(format);
                    }
                    None => {
                        obj.insert("output_config".into(), json!({"format": format}));
                    }
                }
            }
        }

        if obj
            .get("response_format")
            .and_then(|rf| rf.get("type"))
            .and_then(Value::as_str)
            == Some("json_object")
        {
            tracing::warn!(
                model = %model.model_name,
                "response_format json_object dropped: Claude has no schema-less JSON output; \
                 the answer is not constrained to JSON (send a json_schema to constrain it)"
            );
        }

        // Remove params Anthropic doesn't accept
        for param in [
            "n",
            "logprobs",
            "top_logprobs",
            "logit_bias",
            "frequency_penalty",
            "presence_penalty",
            "parallel_tool_calls",
            "response_format",
            "seed",
            "suffix",
            "stream_options",
        ] {
            obj.remove(param);
        }

        // ── Claude 4.x sampling-parameter rules ───────────────────
        // Three overlapping contracts the adapter has to enforce, in
        // order. Each one mirrors a real Anthropic 400 we've observed
        // on the live API.
        //
        //   (A) claude-opus-4-7 deprecates `temperature` outright.
        //       Sending it returns
        //       ``invalid_request_error: \`temperature\` is deprecated
        //       for this model.`` Strip unconditionally — even
        //       without thinking on.
        //
        //   (B) Claude 4.x family rejects `temperature` + `top_p` set
        //       *together*: ``temperature and top_p cannot both be
        //       specified for this model``. When both are present we
        //       drop `top_p` (keep temperature; it's the more
        //       commonly-set knob and the one our seed defaults).
        //
        //   (C) When thinking is on, temperature must be 1.0 (or
        //       absent) and top_p must be absent. For pre-4.x
        //       thinking-capable models this is the *only* rule that
        //       applies; for Sonnet 4.6 / Haiku 4.5 / older Opus 4.x
        //       it's also active alongside (B); for Opus 4.7
        //       temperature is already stripped via (A).
        //
        // Older Claude 3.x models hit none of these and pass through
        // untouched. The model-name gates are anchored on the
        // canonical Anthropic IDs.
        let name = &model.model_name;
        // On the first-party form of the id, so a Bedrock or Vertex id
        // (`us.anthropic.claude-opus-4-7`, `claude-opus-4-7@…`) meets the
        // same rules.
        let base = crate::services::providers::claude_base_name(name).unwrap_or_default();
        let is_opus_4_7 = base.starts_with("claude-opus-4-7");
        let is_claude_4 = base.starts_with("claude-opus-4")
            || base.starts_with("claude-sonnet-4")
            || base.starts_with("claude-haiku-4");

        // Rule (A): Opus 4.7 — temperature deprecated.
        if is_opus_4_7 {
            obj.remove("temperature");
        }
        // Rule (A'): from Claude 4.8 on (every Claude 5) `temperature` and
        // `top_p` are retired outright. For `provider = anthropic` the
        // request pipeline drops them after this adapter
        // (`providers::drop_unsupported_params`); a Claude model on another
        // platform (Bedrock, Vertex) reaches its upstream through this
        // adapter alone, and got both.
        if crate::services::providers::anthropic_sampling_params_retired(name) {
            obj.remove("temperature");
            obj.remove("top_p");
        }

        // Rule (B): Claude 4.x — can't ship both temperature and top_p.
        // After (A), Opus 4.7 won't have temperature, so this is a
        // no-op for it. For the other 4.x models we keep temperature
        // and drop top_p.
        if is_claude_4 && obj.contains_key("temperature") && obj.contains_key("top_p") {
            obj.remove("top_p");
        }

        // Rule (C) ── Thinking ──────────────────────────────────────
        // When the model row's `thinking_enabled` is true, inject the
        // thinking block the model accepts. Whoever asked for it, the API
        // requires temperature == 1.0 (or absent), and no top_p or top_k.
        //
        // The caller may have asked for thinking itself, and in that case a
        // temperature injected from the model's defaults earlier in
        // `prepare_request` is not one the caller chose: leaving it in turned
        // their request into a 400.
        if model.thinking_enabled.unwrap_or(false) && !obj.contains_key("thinking") {
            let block = if !crate::services::providers::anthropic_uses_adaptive_thinking(name) {
                // 10 000 token budget is Anthropic's documented sweet spot for
                // the models that still take a fixed budget.
                json!({ "type": "enabled", "budget_tokens": 10_000 })
            } else if crate::services::providers::anthropic_thinking_summary_is_opt_in(name) {
                // Without this the thinking text comes back empty, and there
                // is nothing to return to the caller or keep as evidence.
                json!({ "type": "adaptive", "display": "summarized" })
            } else {
                json!({ "type": "adaptive" })
            };
            obj.insert("thinking".into(), block);
        }
        // Where an output policy applies the route says so; the policy reads
        // the answer only, so the thinking must not come back readable.
        if obj
            .remove(crate::services::providers::OMIT_REASONING_MARKER)
            .is_some_and(|v| v == Value::Bool(true))
        {
            crate::services::providers::withhold_anthropic_thinking(obj, name);
        }
        let thinking_on = obj
            .get("thinking")
            .and_then(|t| t.get("type"))
            .and_then(|t| t.as_str())
            .is_some_and(|t| t != "disabled");
        if thinking_on {
            if is_opus_4_7 || crate::services::providers::anthropic_sampling_params_retired(name) {
                obj.remove("temperature");
            } else if obj.contains_key("temperature") {
                obj.insert("temperature".into(), json!(1.0));
            }
            obj.remove("top_p");
            obj.remove("top_k");
        }
    }

    fn needs_stream_normalization(&self) -> bool {
        true
    }

    fn create_stream_normalizer(&self) -> Box<dyn StreamNormalizer> {
        Box::new(AnthropicStreamNormalizer::new())
    }

    fn normalize_response(&self, body: &mut Value) {
        // Convert Anthropic non-streaming response to OpenAI chat completion format
        //
        // Anthropic format:
        //   { "id": "msg_...", "type": "message", "role": "assistant",
        //     "content": [{"type": "text", "text": "..."}],
        //     "model": "...", "stop_reason": "end_turn",
        //     "usage": {"input_tokens": N, "output_tokens": M} }
        //
        // OpenAI format:
        //   { "id": "chatcmpl-...", "object": "chat.completion",
        //     "choices": [{"index": 0, "message": {"role": "assistant", "content": "..."},
        //                  "finish_reason": "stop"}],
        //     "model": "...",
        //     "usage": {"prompt_tokens": N, "completion_tokens": M, "total_tokens": N+M} }

        let msg_type = body.get("type").and_then(|t| t.as_str()).unwrap_or("");
        if msg_type != "message" {
            return; // Not an Anthropic message response
        }

        let id = body
            .get("id")
            .and_then(|v| v.as_str())
            .unwrap_or("msg_proxy")
            .to_string();
        let model = body
            .get("model")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();

        // Extract text content and tool_calls from Anthropic content blocks
        let mut text_parts = Vec::new();
        let mut tool_calls = Vec::new();
        let mut reasoning_parts = Vec::new();
        let mut thinking_blocks = Vec::new();
        if let Some(content) = body.get("content").and_then(|c| c.as_array()) {
            for (idx, block) in content.iter().enumerate() {
                match block.get("type").and_then(|t| t.as_str()).unwrap_or("") {
                    "text" => {
                        if let Some(text) = block.get("text").and_then(|t| t.as_str()) {
                            text_parts.push(text.to_string());
                        }
                    }
                    "tool_use" => {
                        let tool_id = block.get("id").and_then(|v| v.as_str()).unwrap_or("");
                        let name = block.get("name").and_then(|v| v.as_str()).unwrap_or("");
                        let input = block.get("input").cloned().unwrap_or(json!({}));
                        tool_calls.push(json!({
                            "index": idx,
                            "id": tool_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": input.to_string()
                            }
                        }));
                    }
                    // `reasoning_content` is the field the rest of the proxy
                    // and OpenAI-shaped clients read. `thinking_blocks` keeps
                    // the signed originals, which a client must send back
                    // unchanged on the next turn of a tool loop.
                    "thinking" => {
                        if let Some(text) = block.get("thinking").and_then(|t| t.as_str()) {
                            if !text.is_empty() {
                                reasoning_parts.push(text.to_string());
                            }
                        }
                        thinking_blocks.push(block.clone());
                    }
                    "redacted_thinking" => thinking_blocks.push(block.clone()),
                    _ => {}
                }
            }
        }

        // Build OpenAI message
        let mut message = serde_json::Map::new();
        message.insert("role".into(), Value::String("assistant".into()));
        let combined_text = text_parts.join("");
        if !combined_text.is_empty() || tool_calls.is_empty() {
            message.insert("content".into(), Value::String(combined_text));
        } else {
            message.insert("content".into(), Value::Null);
        }
        if !reasoning_parts.is_empty() {
            message.insert(
                "reasoning_content".into(),
                Value::String(reasoning_parts.join("")),
            );
        }
        if !thinking_blocks.is_empty() {
            message.insert("thinking_blocks".into(), Value::Array(thinking_blocks));
        }
        if !tool_calls.is_empty() {
            message.insert("tool_calls".into(), Value::Array(tool_calls));
        }

        // Map stop_reason
        let stop_reason = body
            .get("stop_reason")
            .and_then(|r| r.as_str())
            .unwrap_or("stop");
        let finish_reason = match stop_reason {
            "end_turn" | "stop_sequence" => "stop",
            "max_tokens" => "length",
            "tool_use" => "tool_calls",
            other => other,
        };

        // Map usage. Preserve Anthropic's cache counters
        // (`cache_creation_input_tokens` / `cache_read_input_tokens`)
        // verbatim — the prompt-cache layer's `extract_usage` reads
        // them later from this same response, and an earlier version
        // of this function silently dropped them when it rebuilt the
        // body via `*body = json!(...)`. Result: every Anthropic
        // request would log `prompt_cache_*_tokens = NULL` even when
        // the cache had actually engaged, hiding cache-warming
        // behaviour and making the "is caching working?" question
        // unanswerable from `proxy_logs` alone.
        let usage = body.get("usage");
        let input_tokens = usage
            .and_then(|u| u.get("input_tokens"))
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let output_tokens = usage
            .and_then(|u| u.get("output_tokens"))
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let cache_creation_input_tokens = usage
            .and_then(|u| u.get("cache_creation_input_tokens"))
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let cache_read_input_tokens = usage
            .and_then(|u| u.get("cache_read_input_tokens"))
            .and_then(|v| v.as_i64())
            .unwrap_or(0);

        let mut usage_out = serde_json::Map::new();
        usage_out.insert("prompt_tokens".into(), json!(input_tokens));
        usage_out.insert("completion_tokens".into(), json!(output_tokens));
        usage_out.insert("total_tokens".into(), json!(input_tokens + output_tokens));
        // Only emit cache counters when the upstream actually
        // returned them, so logs / dashboards can still distinguish
        // "cache disabled" from "cache engaged but missed".
        if cache_creation_input_tokens > 0 {
            usage_out.insert(
                "cache_creation_input_tokens".into(),
                json!(cache_creation_input_tokens),
            );
        }
        if cache_read_input_tokens > 0 {
            usage_out.insert(
                "cache_read_input_tokens".into(),
                json!(cache_read_input_tokens),
            );
        }

        *body = json!({
            "id": format!("chatcmpl-{id}"),
            "object": "chat.completion",
            "created": chrono::Utc::now().timestamp(),
            "model": model,
            "choices": [{
                "index": 0,
                "message": Value::Object(message),
                "finish_reason": finish_reason,
            }],
            "usage": Value::Object(usage_out),
        });
    }
}

impl AnthropicAdapter {
    /// Transform OpenAI tool_choice format to Anthropic format.
    fn transform_tool_choice(&self, obj: &mut serde_json::Map<String, Value>) {
        let tc = match obj.get("tool_choice").cloned() {
            Some(tc) => tc,
            None => return,
        };

        let transformed = match tc {
            Value::String(ref s) => match s.as_str() {
                "auto" => Some(json!({"type": "auto"})),
                "none" => {
                    obj.remove("tools");
                    obj.remove("tool_choice");
                    None
                }
                "required" => Some(json!({"type": "any"})),
                _ => Some(json!({"type": "auto"})),
            },
            Value::Object(ref map) => {
                if let Some(func) = map.get("function") {
                    if let Some(name) = func.get("name").and_then(|n| n.as_str()) {
                        Some(json!({"type": "tool", "name": name}))
                    } else {
                        Some(json!({"type": "auto"}))
                    }
                } else if map.contains_key("type") {
                    None // Already Anthropic format
                } else {
                    Some(json!({"type": "auto"}))
                }
            }
            _ => {
                obj.remove("tool_choice");
                None
            }
        };

        if let Some(new_tc) = transformed {
            obj.insert("tool_choice".into(), new_tc);
        }
    }

    /// Transform OpenAI-format tools to Anthropic format.
    fn transform_tools(&self, obj: &mut serde_json::Map<String, Value>) {
        let tools = match obj.get_mut("tools") {
            Some(t) => t,
            None => return,
        };
        let tools_arr = match tools.as_array_mut() {
            Some(a) => a,
            None => return,
        };

        let transformed: Vec<Value> = tools_arr
            .iter()
            .filter_map(|tool| {
                if tool.get("type").and_then(|t| t.as_str()) == Some("function") {
                    if let Some(func) = tool.get("function") {
                        let mut at = serde_json::Map::new();
                        if let Some(name) = func.get("name") {
                            at.insert("name".into(), name.clone());
                        }
                        if let Some(desc) = func.get("description") {
                            at.insert("description".into(), desc.clone());
                        }
                        if let Some(params) = func.get("parameters") {
                            at.insert("input_schema".into(), params.clone());
                        } else {
                            at.insert(
                                "input_schema".into(),
                                json!({"type": "object", "properties": {}}),
                            );
                        }
                        return Some(Value::Object(at));
                    }
                }
                Some(tool.clone())
            })
            .collect();
        *tools = Value::Array(transformed);
    }

    /// Convert OpenAI message format to Anthropic message format:
    /// 1. Assistant "tool_calls" → "tool_use" content blocks
    /// 2. "role": "tool" → "role": "user" with "tool_result" content blocks
    /// 3. Merge consecutive same-role messages (Anthropic requires alternating)
    fn transform_messages(&self, obj: &mut serde_json::Map<String, Value>) {
        let messages = match obj.get_mut("messages") {
            Some(m) => m,
            None => return,
        };
        let msgs = match messages.as_array_mut() {
            Some(a) => a,
            None => return,
        };

        // Transform individual messages
        for msg in msgs.iter_mut() {
            let role = msg.get("role").and_then(|r| r.as_str()).unwrap_or("");

            if role == "assistant" {
                let thinking_blocks = take_thinking_blocks(msg);
                if let Some(tool_calls) = msg.get("tool_calls").cloned() {
                    if let Some(calls) = tool_calls.as_array() {
                        // Thinking comes first, unchanged: the API checks it.
                        let mut content_blocks: Vec<Value> = thinking_blocks;

                        // Preserve existing text content
                        if let Some(text) = msg.get("content").and_then(|c| c.as_str()) {
                            if !text.is_empty() {
                                content_blocks.push(json!({"type": "text", "text": text}));
                            }
                        } else if let Some(arr) = msg.get("content").and_then(|c| c.as_array()) {
                            content_blocks.extend(arr.iter().cloned());
                        }

                        for call in calls {
                            let id = call.get("id").and_then(|v| v.as_str()).unwrap_or("");
                            if let Some(func) = call.get("function") {
                                let name = func.get("name").and_then(|v| v.as_str()).unwrap_or("");
                                let input = func
                                    .get("arguments")
                                    .and_then(|v| v.as_str())
                                    .and_then(|s| serde_json::from_str::<Value>(s).ok())
                                    .unwrap_or(json!({}));
                                content_blocks.push(json!({
                                    "type": "tool_use",
                                    "id": id,
                                    "name": name,
                                    "input": input
                                }));
                            }
                        }

                        if let Some(obj) = msg.as_object_mut() {
                            obj.remove("tool_calls");
                            obj.insert("content".into(), Value::Array(content_blocks));
                        }
                    }
                } else if !thinking_blocks.is_empty() {
                    let mut content_blocks = thinking_blocks;
                    match msg.get("content") {
                        Some(Value::String(text)) if !text.is_empty() => {
                            content_blocks.push(json!({"type": "text", "text": text}));
                        }
                        Some(Value::Array(arr)) => content_blocks.extend(arr.iter().cloned()),
                        _ => {}
                    }
                    if let Some(obj) = msg.as_object_mut() {
                        obj.insert("content".into(), Value::Array(content_blocks));
                    }
                }
            } else if role == "tool" {
                let tool_call_id = msg
                    .get("tool_call_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let content = msg
                    .get("content")
                    .cloned()
                    .unwrap_or(Value::String(String::new()));
                let tool_result_content = if let Some(s) = content.as_str() {
                    s.to_string()
                } else {
                    content.to_string()
                };

                let tool_result = json!({
                    "type": "tool_result",
                    "tool_use_id": tool_call_id,
                    "content": tool_result_content
                });

                if let Some(obj) = msg.as_object_mut() {
                    obj.insert("role".into(), Value::String("user".into()));
                    obj.insert("content".into(), Value::Array(vec![tool_result]));
                    obj.remove("tool_call_id");
                    obj.remove("name");
                }
            }
        }

        // Merge consecutive same-role messages
        let mut merged: Vec<Value> = Vec::new();
        for msg in msgs.drain(..) {
            let role = msg
                .get("role")
                .and_then(|r| r.as_str())
                .unwrap_or("")
                .to_string();
            let prev_role = merged
                .last()
                .and_then(|m: &Value| m.get("role"))
                .and_then(|r| r.as_str())
                .map(String::from);

            if Some(&role) == prev_role.as_ref() {
                if let Some(prev) = merged.last_mut() {
                    let prev_content = prev.get("content").cloned().unwrap_or(Value::Null);
                    let cur_content = msg.get("content").cloned().unwrap_or(Value::Null);
                    let mut blocks = content_to_blocks(prev_content);
                    blocks.extend(content_to_blocks(cur_content));
                    if let Some(obj) = prev.as_object_mut() {
                        obj.insert("content".into(), Value::Array(blocks));
                    }
                }
            } else {
                merged.push(msg);
            }
        }
        *msgs = merged;
    }
}

// ---------------------------------------------------------------------------
// Anthropic stream normalizer
// ---------------------------------------------------------------------------

/// Converts Anthropic streaming events to OpenAI-compatible SSE format.
/// Take the proxy's two reasoning fields off an assistant message and return
/// the thinking blocks to replay. Anthropic rejects both fields as unknown, and
/// only its own block types may be replayed as thinking.
fn take_thinking_blocks(msg: &mut Value) -> Vec<Value> {
    let Some(obj) = msg.as_object_mut() else {
        return Vec::new();
    };
    obj.remove("reasoning_content");
    match obj.remove("thinking_blocks") {
        Some(Value::Array(blocks)) => blocks
            .into_iter()
            .filter(|b| {
                matches!(
                    b.get("type").and_then(|t| t.as_str()),
                    Some("thinking" | "redacted_thinking")
                )
            })
            .collect(),
        _ => Vec::new(),
    }
}

pub struct AnthropicStreamNormalizer {
    message_id: String,
    model: String,
    tool_index: i32,
    current_block_type: String,
    /// The thinking block being streamed, rebuilt so it can be returned whole
    /// when the block closes: the client has to replay it unchanged.
    thinking_block: Option<Value>,
}

impl AnthropicStreamNormalizer {
    pub fn new() -> Self {
        Self {
            message_id: String::new(),
            model: String::new(),
            tool_index: -1,
            current_block_type: String::new(),
            thinking_block: None,
        }
    }

    fn make_chunk(&self, choices: Value) -> String {
        json!({
            "id": format!("chatcmpl-{}", &self.message_id),
            "object": "chat.completion.chunk",
            "created": chrono::Utc::now().timestamp(),
            "model": self.model,
            "choices": choices
        })
        .to_string()
    }
}

impl StreamNormalizer for AnthropicStreamNormalizer {
    fn convert(&mut self, data: &str) -> Vec<String> {
        let parsed: Value = match serde_json::from_str(data) {
            Ok(v) => v,
            Err(_) => return vec![],
        };

        let event_type = parsed.get("type").and_then(|t| t.as_str()).unwrap_or("");

        match event_type {
            "message_start" => {
                let mut input_tokens: Option<i64> = None;
                let mut cache_write: Option<i64> = None;
                let mut cache_read: Option<i64> = None;
                if let Some(msg) = parsed.get("message") {
                    self.message_id = msg
                        .get("id")
                        .and_then(|v| v.as_str())
                        .unwrap_or("msg_proxy")
                        .to_string();
                    self.model = msg
                        .get("model")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string();
                    if let Some(usage) = msg.get("usage") {
                        input_tokens = usage.get("input_tokens").and_then(|v| v.as_i64());
                        // Preserve Anthropic's prompt-cache counters so
                        // downstream aggregation (Mission Control, group
                        // stats) can show "N tokens read from cache / $X
                        // saved". Without this pass-through, cache
                        // telemetry only works on non-streaming paths.
                        cache_write = usage
                            .get("cache_creation_input_tokens")
                            .and_then(|v| v.as_i64());
                        cache_read = usage
                            .get("cache_read_input_tokens")
                            .and_then(|v| v.as_i64());
                    }
                }
                let mut chunk_val: Value = json!({
                    "id": format!("chatcmpl-{}", &self.message_id),
                    "object": "chat.completion.chunk",
                    "created": chrono::Utc::now().timestamp(),
                    "model": self.model,
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": null
                    }]
                });
                if input_tokens.is_some() || cache_write.is_some() || cache_read.is_some() {
                    let mut usage = serde_json::Map::new();
                    if let Some(pt) = input_tokens {
                        usage.insert("prompt_tokens".into(), json!(pt));
                    }
                    if let Some(cw) = cache_write {
                        usage.insert("cache_creation_input_tokens".into(), json!(cw));
                    }
                    if let Some(cr) = cache_read {
                        usage.insert("cache_read_input_tokens".into(), json!(cr));
                    }
                    chunk_val["usage"] = Value::Object(usage);
                }
                vec![chunk_val.to_string()]
            }

            "content_block_start" => {
                let block = match parsed.get("content_block") {
                    Some(b) => b,
                    None => return vec![],
                };
                let block_type = block.get("type").and_then(|t| t.as_str()).unwrap_or("text");
                self.current_block_type = block_type.to_string();

                match block_type {
                    "thinking" => {
                        self.thinking_block =
                            Some(json!({"type": "thinking", "thinking": "", "signature": ""}));
                        vec![]
                    }
                    "redacted_thinking" => {
                        self.thinking_block = Some(block.clone());
                        vec![]
                    }
                    "tool_use" | "server_tool_use" => {
                        self.tool_index += 1;
                        let tool_id = block.get("id").and_then(|v| v.as_str()).unwrap_or("");
                        let tool_name = block.get("name").and_then(|v| v.as_str()).unwrap_or("");
                        vec![self.make_chunk(json!([{
                            "index": 0,
                            "delta": {
                                "tool_calls": [{
                                    "index": self.tool_index,
                                    "id": tool_id,
                                    "type": "function",
                                    "function": {
                                        "name": tool_name,
                                        "arguments": ""
                                    }
                                }]
                            },
                            "finish_reason": null
                        }]))]
                    }
                    _ => vec![],
                }
            }

            "content_block_delta" => {
                let delta = match parsed.get("delta") {
                    Some(d) => d,
                    None => return vec![],
                };
                let delta_type = delta.get("type").and_then(|t| t.as_str()).unwrap_or("");

                match delta_type {
                    "text_delta" => {
                        let text = delta.get("text").and_then(|t| t.as_str()).unwrap_or("");
                        if text.is_empty() {
                            return vec![];
                        }
                        vec![self.make_chunk(json!([{
                            "index": 0,
                            "delta": {"content": text},
                            "finish_reason": null
                        }]))]
                    }
                    "input_json_delta" => {
                        if !matches!(
                            self.current_block_type.as_str(),
                            "tool_use" | "server_tool_use"
                        ) {
                            return vec![];
                        }
                        let partial = delta
                            .get("partial_json")
                            .and_then(|p| p.as_str())
                            .unwrap_or("");
                        if partial.is_empty() {
                            return vec![];
                        }
                        vec![self.make_chunk(json!([{
                            "index": 0,
                            "delta": {
                                "tool_calls": [{
                                    "index": self.tool_index,
                                    "function": {
                                        "arguments": partial
                                    }
                                }]
                            },
                            "finish_reason": null
                        }]))]
                    }
                    "thinking_delta" => {
                        let text = delta.get("thinking").and_then(|t| t.as_str()).unwrap_or("");
                        if text.is_empty() {
                            return vec![];
                        }
                        if let Some(stored) = self
                            .thinking_block
                            .as_mut()
                            .and_then(|b| b.get_mut("thinking"))
                        {
                            let joined = format!("{}{text}", stored.as_str().unwrap_or(""));
                            *stored = Value::String(joined);
                        }
                        vec![self.make_chunk(json!([{
                            "index": 0,
                            "delta": {"reasoning_content": text},
                            "finish_reason": null
                        }]))]
                    }
                    "signature_delta" => {
                        if let (Some(block), Some(sig)) = (
                            self.thinking_block.as_mut(),
                            delta.get("signature").and_then(|s| s.as_str()),
                        ) {
                            block["signature"] = Value::String(sig.to_string());
                        }
                        vec![]
                    }
                    _ => vec![],
                }
            }

            "content_block_stop" => {
                self.current_block_type.clear();
                match self.thinking_block.take() {
                    Some(block) => vec![self.make_chunk(json!([{
                        "index": 0,
                        "delta": {"thinking_blocks": [block]},
                        "finish_reason": null
                    }]))],
                    None => vec![],
                }
            }

            "message_delta" => {
                let stop_reason = parsed
                    .get("delta")
                    .and_then(|d| d.get("stop_reason"))
                    .and_then(|r| r.as_str())
                    .unwrap_or("stop");
                let finish_reason = match stop_reason {
                    "end_turn" | "stop_sequence" => "stop",
                    "max_tokens" => "length",
                    "tool_use" => "tool_calls",
                    other => other,
                };

                let mut chunk_val: Value = json!({
                    "id": format!("chatcmpl-{}", &self.message_id),
                    "object": "chat.completion.chunk",
                    "created": chrono::Utc::now().timestamp(),
                    "model": self.model,
                    "choices": [{
                        "index": 0,
                        "delta": {},
                        "finish_reason": finish_reason
                    }]
                });
                if let Some(usage) = parsed.get("usage") {
                    let output_tokens = usage
                        .get("output_tokens")
                        .and_then(|v| v.as_i64())
                        .unwrap_or(0);
                    let input_tokens = usage
                        .get("input_tokens")
                        .and_then(|v| v.as_i64())
                        .unwrap_or(0);
                    let mut usage_out = serde_json::Map::new();
                    usage_out.insert("prompt_tokens".into(), json!(input_tokens));
                    usage_out.insert("completion_tokens".into(), json!(output_tokens));
                    usage_out.insert("total_tokens".into(), json!(input_tokens + output_tokens));
                    // Prompt-cache counters: occasionally only present on
                    // the closing delta (the `message_start` frame may
                    // omit them until the provider has finished scoring
                    // the request). Pass whatever is here through so the
                    // downstream log pipeline gets the final numbers.
                    if let Some(cw) = usage
                        .get("cache_creation_input_tokens")
                        .and_then(|v| v.as_i64())
                    {
                        usage_out.insert("cache_creation_input_tokens".into(), json!(cw));
                    }
                    if let Some(cr) = usage
                        .get("cache_read_input_tokens")
                        .and_then(|v| v.as_i64())
                    {
                        usage_out.insert("cache_read_input_tokens".into(), json!(cr));
                    }
                    chunk_val["usage"] = Value::Object(usage_out);
                }
                vec![chunk_val.to_string()]
            }

            "message_stop" => vec!["[DONE]".to_string()],

            _ => vec![],
        }
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::llm_adapter::make_test_model;
    use serde_json::json;

    // -----------------------------------------------------------------------
    // Anthropic adapter — chat_endpoint
    // -----------------------------------------------------------------------

    #[test]
    fn anthropic_chat_endpoint() {
        let model = make_test_model("anthropic", "claude-3-opus");
        let adapter = AnthropicAdapter;
        assert_eq!(
            adapter.chat_endpoint(&model, "/v1/chat/completions", false),
            "/messages"
        );
    }

    // -----------------------------------------------------------------------
    // Anthropic adapter — auth_headers
    // -----------------------------------------------------------------------

    #[test]
    fn anthropic_auth_headers_uses_x_api_key() {
        let model = make_test_model("anthropic", "claude-3-opus");
        let adapter = AnthropicAdapter;
        let headers = adapter.auth_headers(&model);

        let has_x_api_key = headers
            .iter()
            .any(|(k, v)| k == "x-api-key" && v == "sk-test-key");
        assert!(has_x_api_key, "expected x-api-key header");

        let has_no_bearer = headers.iter().all(|(k, _)| k != "Authorization");
        assert!(has_no_bearer, "should not have Authorization header");

        let has_version = headers
            .iter()
            .any(|(k, v)| k == "anthropic-version" && v == "2023-06-01");
        assert!(has_version, "expected anthropic-version header");
    }

    // -----------------------------------------------------------------------
    // Anthropic adapter — transform_request
    // -----------------------------------------------------------------------

    #[test]
    fn anthropic_removes_null_values() {
        let model = make_test_model("anthropic", "claude-3-opus");
        let adapter = AnthropicAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("model".into(), json!("claude-3-opus"));
        obj.insert(
            "messages".into(),
            json!([{"role": "user", "content": "hi"}]),
        );
        obj.insert("some_null_field".into(), Value::Null);

        adapter.transform_request(&mut obj, &model);

        assert!(!obj.contains_key("some_null_field"));
    }

    #[test]
    fn anthropic_max_completion_tokens_to_max_tokens() {
        let model = make_test_model("anthropic", "claude-3-opus");
        let adapter = AnthropicAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("model".into(), json!("claude-3-opus"));
        obj.insert(
            "messages".into(),
            json!([{"role": "user", "content": "hi"}]),
        );
        obj.insert("max_completion_tokens".into(), json!(2048));

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("max_tokens"), Some(&json!(2048)));
        assert!(!obj.contains_key("max_completion_tokens"));
    }

    #[test]
    fn anthropic_default_max_tokens_when_missing() {
        let model = make_test_model("anthropic", "claude-3-opus");
        let adapter = AnthropicAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("model".into(), json!("claude-3-opus"));
        obj.insert(
            "messages".into(),
            json!([{"role": "user", "content": "hi"}]),
        );

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("max_tokens"), Some(&json!(4096)));
    }

    #[test]
    fn anthropic_stop_array_to_stop_sequences() {
        let model = make_test_model("anthropic", "claude-3-opus");
        let adapter = AnthropicAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("model".into(), json!("claude-3-opus"));
        obj.insert(
            "messages".into(),
            json!([{"role": "user", "content": "hi"}]),
        );
        obj.insert("stop".into(), json!(["END", "STOP"]));

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("stop_sequences"), Some(&json!(["END", "STOP"])));
        assert!(!obj.contains_key("stop"));
    }

    #[test]
    fn anthropic_stop_string_to_stop_sequences_array() {
        let model = make_test_model("anthropic", "claude-3-opus");
        let adapter = AnthropicAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("model".into(), json!("claude-3-opus"));
        obj.insert(
            "messages".into(),
            json!([{"role": "user", "content": "hi"}]),
        );
        obj.insert("stop".into(), json!("END"));

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("stop_sequences"), Some(&json!(["END"])));
    }

    #[test]
    fn anthropic_user_to_metadata_user_id() {
        let model = make_test_model("anthropic", "claude-3-opus");
        let adapter = AnthropicAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("model".into(), json!("claude-3-opus"));
        obj.insert(
            "messages".into(),
            json!([{"role": "user", "content": "hi"}]),
        );
        obj.insert("user".into(), json!("user-123"));

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("metadata"), Some(&json!({"user_id": "user-123"})));
        assert!(!obj.contains_key("user"));
    }

    #[test]
    fn anthropic_extracts_system_messages_as_blocks() {
        // The Anthropic `system` field is emitted as an ARRAY
        // of content blocks ({type:"text", text:...}) rather than a
        // concatenated string. This preserves source granularity so
        // the prompt-cache layer can mark individual blocks with
        // cache_control.
        let model = make_test_model("anthropic", "claude-3-opus");
        let adapter = AnthropicAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("model".into(), json!("claude-3-opus"));
        obj.insert(
            "messages".into(),
            json!([
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "hi"}
            ]),
        );

        adapter.transform_request(&mut obj, &model);

        assert_eq!(
            obj.get("system"),
            Some(&json!([{"type": "text", "text": "You are helpful."}])),
            "single system message becomes a one-element block array"
        );
        // System messages should be removed from the messages array.
        let msgs = obj.get("messages").unwrap().as_array().unwrap();
        assert!(msgs.iter().all(|m| m.get("role").unwrap() != "system"));
    }

    #[test]
    fn anthropic_preserves_multiple_system_blocks_in_order() {
        // After the merge of server + caller system prompts into
        // multiple `role:"system"` messages, the Anthropic adapter
        // must emit each as its own block — in original order —
        // rather than collapsing them into one string. Per-block
        // identity matters for cache marker placement.
        let model = make_test_model("anthropic", "claude-3-opus");
        let adapter = AnthropicAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("model".into(), json!("claude-3-opus"));
        obj.insert(
            "messages".into(),
            json!([
                {"role": "system", "content": "[SERVER] core policy"},
                {"role": "system", "content": "[A] persona"},
                {"role": "system", "content": "[B] constraints"},
                {"role": "user", "content": "hi"}
            ]),
        );

        adapter.transform_request(&mut obj, &model);

        let system = obj.get("system").unwrap().as_array().unwrap();
        assert_eq!(system.len(), 3);
        assert_eq!(system[0]["text"], "[SERVER] core policy");
        assert_eq!(system[1]["text"], "[A] persona");
        assert_eq!(system[2]["text"], "[B] constraints");
        // Every block carries the {type: "text"} marker.
        assert!(system.iter().all(|b| b.get("type") == Some(&json!("text"))));
    }

    #[test]
    fn anthropic_strips_caller_supplied_cache_control_from_messages() {
        // The Anthropic adapter is the authoritative emitter of
        // cache markers for this provider. A caller passing
        // `cache_control` on a message should have it stripped so
        // only planner-driven markers survive.
        let model = make_test_model("anthropic", "claude-3-opus");
        let adapter = AnthropicAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("model".into(), json!("claude-3-opus"));
        obj.insert(
            "messages".into(),
            json!([
                {
                    "role": "user",
                    "content": "hi",
                    "cache_control": {"type": "ephemeral", "ttl": "1h"}
                }
            ]),
        );

        adapter.transform_request(&mut obj, &model);

        let msgs = obj.get("messages").unwrap().as_array().unwrap();
        // User message survives, but its caller-supplied cache_control
        // does not.
        assert_eq!(msgs[0]["role"], "user");
        assert!(
            msgs[0].get("cache_control").is_none(),
            "caller cache_control on a user message must be stripped"
        );
    }

    #[test]
    fn anthropic_strips_caller_supplied_cache_control_from_tools() {
        // Same authoritative-emitter rule applies to tools.
        let model = make_test_model("anthropic", "claude-3-opus");
        let adapter = AnthropicAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("model".into(), json!("claude-3-opus"));
        obj.insert(
            "messages".into(),
            json!([{"role": "user", "content": "hi"}]),
        );
        obj.insert(
            "tools".into(),
            json!([
                {
                    "type": "function",
                    "function": {"name": "do_thing"},
                    "cache_control": {"type": "ephemeral", "ttl": "1h"}
                }
            ]),
        );

        adapter.transform_request(&mut obj, &model);

        let tools = obj.get("tools").unwrap().as_array().unwrap();
        assert!(
            tools[0].get("cache_control").is_none(),
            "caller cache_control on a tool must be stripped"
        );
    }

    #[test]
    fn anthropic_strips_caller_supplied_cache_control_from_system_messages() {
        // System messages get rebuilt into `{type, text}` blocks during
        // extraction. Any caller cache_control on the source system
        // message MUST NOT survive into the resulting `system` array.
        let model = make_test_model("anthropic", "claude-3-opus");
        let adapter = AnthropicAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("model".into(), json!("claude-3-opus"));
        obj.insert(
            "messages".into(),
            json!([
                {
                    "role": "system",
                    "content": "be helpful",
                    "cache_control": {"type": "ephemeral", "ttl": "1h"}
                },
                {"role": "user", "content": "hi"}
            ]),
        );

        adapter.transform_request(&mut obj, &model);

        let system = obj.get("system").unwrap().as_array().unwrap();
        assert_eq!(system.len(), 1);
        assert!(
            system[0].get("cache_control").is_none(),
            "caller cache_control on a system message must not survive"
        );
        // Only `type` and `text` keys — nothing else carried over.
        let block = system[0].as_object().unwrap();
        assert_eq!(block.len(), 2);
        assert!(block.contains_key("type"));
        assert!(block.contains_key("text"));
    }

    #[test]
    fn anthropic_removes_openai_specific_params() {
        let model = make_test_model("anthropic", "claude-3-opus");
        let adapter = AnthropicAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("model".into(), json!("claude-3-opus"));
        obj.insert(
            "messages".into(),
            json!([{"role": "user", "content": "hi"}]),
        );
        obj.insert("n".into(), json!(2));
        obj.insert("logprobs".into(), json!(true));
        obj.insert("top_logprobs".into(), json!(5));
        obj.insert("logit_bias".into(), json!({}));
        obj.insert("frequency_penalty".into(), json!(0.5));
        obj.insert("presence_penalty".into(), json!(0.5));
        obj.insert("parallel_tool_calls".into(), json!(true));
        obj.insert("response_format".into(), json!({"type": "json_object"}));
        obj.insert("seed".into(), json!(42));
        obj.insert("suffix".into(), json!("test"));
        obj.insert("stream_options".into(), json!({}));

        adapter.transform_request(&mut obj, &model);

        for param in [
            "n",
            "logprobs",
            "top_logprobs",
            "logit_bias",
            "frequency_penalty",
            "presence_penalty",
            "parallel_tool_calls",
            "response_format",
            "seed",
            "suffix",
            "stream_options",
        ] {
            assert!(
                !obj.contains_key(param),
                "param '{}' should be removed",
                param
            );
        }
    }

    #[test]
    fn anthropic_empty_tools_array_removed() {
        let model = make_test_model("anthropic", "claude-3-opus");
        let adapter = AnthropicAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("model".into(), json!("claude-3-opus"));
        obj.insert(
            "messages".into(),
            json!([{"role": "user", "content": "hi"}]),
        );
        obj.insert("tools".into(), json!([]));
        obj.insert("tool_choice".into(), json!("auto"));

        adapter.transform_request(&mut obj, &model);

        assert!(!obj.contains_key("tools"));
        assert!(!obj.contains_key("tool_choice"));
    }

    // -----------------------------------------------------------------------
    // Anthropic adapter — normalize_response
    // -----------------------------------------------------------------------

    #[test]
    fn anthropic_normalize_response_text() {
        let adapter = AnthropicAdapter;
        let mut body = json!({
            "id": "msg_abc123",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello!"}],
            "model": "claude-3-opus-20240229",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5}
        });

        adapter.normalize_response(&mut body);

        assert_eq!(body["object"], "chat.completion");
        assert_eq!(body["id"], "chatcmpl-msg_abc123");
        assert_eq!(body["choices"][0]["message"]["role"], "assistant");
        assert_eq!(body["choices"][0]["message"]["content"], "Hello!");
        assert_eq!(body["choices"][0]["finish_reason"], "stop");
        assert_eq!(body["usage"]["prompt_tokens"], 10);
        assert_eq!(body["usage"]["completion_tokens"], 5);
        assert_eq!(body["usage"]["total_tokens"], 15);
        // Plain (no-cache) request: cache counter fields must NOT be
        // present — that's how downstream telemetry distinguishes
        // "caching disabled" from "caching engaged but no hit".
        assert!(body["usage"].get("cache_creation_input_tokens").is_none());
        assert!(body["usage"].get("cache_read_input_tokens").is_none());
    }

    #[test]
    fn anthropic_normalize_response_preserves_cache_counters() {
        // Regression for a real bug surfaced by the live prompt-caching
        // e2e suite: the non-streaming `normalize_response` was
        // rebuilding the entire body via `*body = json!(...)`, dropping
        // `cache_creation_input_tokens` and `cache_read_input_tokens`
        // from the usage object. Result: every Anthropic request would
        // log `prompt_cache_*_tokens = NULL` even when caching had
        // actually engaged, because the prompt-cache layer's
        // `extract_usage` reads from the normalized response — by
        // which point the upstream counters were gone.
        let adapter = AnthropicAdapter;
        let mut body = json!({
            "id": "msg_warm_turn",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Hi."}],
            "model": "claude-haiku-4-5",
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 15,
                "output_tokens": 4,
                "cache_creation_input_tokens": 3700,
                "cache_read_input_tokens": 0
            }
        });

        adapter.normalize_response(&mut body);

        // The warm counters must travel through verbatim.
        assert_eq!(body["usage"]["cache_creation_input_tokens"], 3700);
        // Read-side: zero on the warm turn, so absent (we only emit
        // when non-zero so observers can distinguish "engaged but
        // didn't hit" from "engaged and read N tokens").
        assert!(body["usage"].get("cache_read_input_tokens").is_none());
        // Non-cache fields still correct.
        assert_eq!(body["usage"]["prompt_tokens"], 15);
        assert_eq!(body["usage"]["completion_tokens"], 4);
        assert_eq!(body["usage"]["total_tokens"], 19);
    }

    #[test]
    fn anthropic_normalize_response_preserves_cache_read_on_hit() {
        // Turn-2 shape: cache_read > 0 (the doc came from the cache),
        // cache_creation = 0 (no new bytes warmed this turn).
        let adapter = AnthropicAdapter;
        let mut body = json!({
            "id": "msg_hit",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Sure."}],
            "model": "claude-haiku-4-5",
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 12,
                "output_tokens": 5,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 3700
            }
        });

        adapter.normalize_response(&mut body);

        assert_eq!(body["usage"]["cache_read_input_tokens"], 3700);
        // Creation absent (zero); avoids noise in the read-side row.
        assert!(body["usage"].get("cache_creation_input_tokens").is_none());
    }

    #[test]
    fn anthropic_normalize_response_stop_reason_mapping() {
        let adapter = AnthropicAdapter;

        let cases = vec![
            ("end_turn", "stop"),
            ("max_tokens", "length"),
            ("tool_use", "tool_calls"),
        ];

        for (anthropic_reason, expected_reason) in cases {
            let mut body = json!({
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "model": "claude-3-opus",
                "stop_reason": anthropic_reason,
                "usage": {"input_tokens": 1, "output_tokens": 1}
            });

            adapter.normalize_response(&mut body);

            assert_eq!(
                body["choices"][0]["finish_reason"], expected_reason,
                "stop_reason '{}' should map to '{}'",
                anthropic_reason, expected_reason
            );
        }
    }

    #[test]
    fn anthropic_normalize_response_tool_use() {
        let adapter = AnthropicAdapter;
        let mut body = json!({
            "id": "msg_tools",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_01", "name": "get_weather", "input": {"city": "London"}}
            ],
            "model": "claude-3-opus",
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 20, "output_tokens": 15}
        });

        adapter.normalize_response(&mut body);

        assert_eq!(body["choices"][0]["finish_reason"], "tool_calls");
        let tool_calls = &body["choices"][0]["message"]["tool_calls"];
        assert_eq!(tool_calls[0]["id"], "toolu_01");
        assert_eq!(tool_calls[0]["type"], "function");
        assert_eq!(tool_calls[0]["function"]["name"], "get_weather");
        // content should be null when only tool_use blocks
        assert_eq!(body["choices"][0]["message"]["content"], Value::Null);
    }

    // -----------------------------------------------------------------------
    // Anthropic adapter — needs_stream_normalization
    // -----------------------------------------------------------------------

    #[test]
    fn anthropic_needs_stream_normalization() {
        let adapter = AnthropicAdapter;
        assert!(adapter.needs_stream_normalization());
    }

    // -----------------------------------------------------------------------
    // Anthropic stream normalizer
    // -----------------------------------------------------------------------

    #[test]
    fn anthropic_stream_message_start() {
        let mut normalizer = AnthropicStreamNormalizer::new();
        let event = json!({
            "type": "message_start",
            "message": {
                "id": "msg_stream1",
                "model": "claude-3-opus",
                "usage": {"input_tokens": 25}
            }
        });

        let result = normalizer.convert(&event.to_string());
        assert_eq!(result.len(), 1);

        let chunk: Value = serde_json::from_str(&result[0]).unwrap();
        assert_eq!(chunk["id"], "chatcmpl-msg_stream1");
        assert_eq!(chunk["choices"][0]["delta"]["role"], "assistant");
        assert_eq!(chunk["usage"]["prompt_tokens"], 25);
    }

    #[test]
    fn anthropic_stream_text_delta() {
        let mut normalizer = AnthropicStreamNormalizer::new();
        // Initialize state
        normalizer.convert(
            &json!({"type": "message_start", "message": {"id": "msg_1", "model": "test"}})
                .to_string(),
        );

        let event = json!({
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "Hello"}
        });
        let result = normalizer.convert(&event.to_string());
        assert_eq!(result.len(), 1);

        let chunk: Value = serde_json::from_str(&result[0]).unwrap();
        assert_eq!(chunk["choices"][0]["delta"]["content"], "Hello");
        assert_eq!(chunk["choices"][0]["finish_reason"], Value::Null);
    }

    #[test]
    fn anthropic_stream_tool_call_delta() {
        let mut normalizer = AnthropicStreamNormalizer::new();
        normalizer.convert(
            &json!({"type": "message_start", "message": {"id": "msg_1", "model": "test"}})
                .to_string(),
        );

        // Start a tool_use block
        let block_start = json!({
            "type": "content_block_start",
            "content_block": {"type": "tool_use", "id": "toolu_01", "name": "get_weather"}
        });
        normalizer.convert(&block_start.to_string());

        // Send input_json_delta
        let delta = json!({
            "type": "content_block_delta",
            "delta": {"type": "input_json_delta", "partial_json": "{\"city\":"}
        });
        let result = normalizer.convert(&delta.to_string());
        assert_eq!(result.len(), 1);

        let chunk: Value = serde_json::from_str(&result[0]).unwrap();
        assert_eq!(
            chunk["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"],
            "{\"city\":"
        );
    }

    #[test]
    fn anthropic_stream_input_json_delta_ignored_outside_tool_use() {
        let mut normalizer = AnthropicStreamNormalizer::new();
        normalizer.convert(
            &json!({"type": "message_start", "message": {"id": "msg_1", "model": "test"}})
                .to_string(),
        );

        // current_block_type is empty (not in tool_use block)
        let delta = json!({
            "type": "content_block_delta",
            "delta": {"type": "input_json_delta", "partial_json": "{\"x\":1}"}
        });
        let result = normalizer.convert(&delta.to_string());
        assert!(result.is_empty());
    }

    #[test]
    fn anthropic_stream_message_delta_finish_reason() {
        let mut normalizer = AnthropicStreamNormalizer::new();
        normalizer.convert(
            &json!({"type": "message_start", "message": {"id": "msg_1", "model": "test"}})
                .to_string(),
        );

        let cases = vec![
            ("end_turn", "stop"),
            ("max_tokens", "length"),
            ("tool_use", "tool_calls"),
        ];

        for (stop_reason, expected) in cases {
            let event = json!({
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason},
                "usage": {"output_tokens": 10, "input_tokens": 0}
            });
            let result = normalizer.convert(&event.to_string());
            assert_eq!(result.len(), 1);

            let chunk: Value = serde_json::from_str(&result[0]).unwrap();
            assert_eq!(
                chunk["choices"][0]["finish_reason"], expected,
                "stream stop_reason '{}' should map to '{}'",
                stop_reason, expected
            );
        }
    }

    #[test]
    fn anthropic_stream_message_stop() {
        let mut normalizer = AnthropicStreamNormalizer::new();
        let event = json!({"type": "message_stop"});
        let result = normalizer.convert(&event.to_string());
        assert_eq!(result, vec!["[DONE]"]);
    }

    // -----------------------------------------------------------------------
    // Anthropic adapter — schema filtering in response_format
    // -----------------------------------------------------------------------

    #[test]
    fn anthropic_response_format_json_schema_gets_constraints_filtered() {
        let model = make_test_model("anthropic", "claude-3-opus");
        let adapter = AnthropicAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("model".into(), json!("claude-3-opus"));
        obj.insert(
            "messages".into(),
            json!([{"role": "user", "content": "hi"}]),
        );
        obj.insert(
            "response_format".into(),
            json!({
                "type": "json_schema",
                "json_schema": {
                    "name": "my_schema",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "count": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 100
                            }
                        }
                    }
                }
            }),
        );

        adapter.transform_request(&mut obj, &model);

        // `response_format` being gone is not enough to assert: when the schema is
        // filtered and then
        // dropped with it, so the caller's structured-output request was
        // lost and the model answers free text. It is sent as the
        // Messages API's `output_config.format`, the filtered schema in it.
        assert!(!obj.contains_key("response_format"));
        assert_eq!(
            obj.get("output_config"),
            Some(&json!({"format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "count": {
                            "type": "integer",
                            "description": "Constraints: minimum=0, maximum=100"
                        }
                    }
                }
            }}))
        );
    }

    /// A caller's own `output_config` (an effort) is kept beside the
    /// format; `json_object` has no Messages API equivalent and is dropped;
    /// a row that says the model has no structured output gets none.
    #[test]
    fn anthropic_response_format_merges_and_respects_the_row() {
        let adapter = AnthropicAdapter;
        let rf = json!({"type": "json_schema", "json_schema": {"name": "s", "schema": {"type": "object"}}});
        let base = |rf: Value| {
            let mut obj = serde_json::Map::new();
            obj.insert(
                "messages".into(),
                json!([{"role": "user", "content": "hi"}]),
            );
            obj.insert("response_format".into(), rf);
            obj
        };

        let model = make_test_model("anthropic", "claude-sonnet-5-5");
        let mut obj = base(rf.clone());
        obj.insert("output_config".into(), json!({"effort": "low"}));
        adapter.transform_request(&mut obj, &model);
        assert_eq!(
            obj.get("output_config"),
            Some(
                &json!({"effort": "low", "format": {"type": "json_schema", "schema": {"type": "object"}}})
            )
        );

        let mut obj = base(json!({"type": "json_object"}));
        adapter.transform_request(&mut obj, &model);
        assert!(!obj.contains_key("response_format") && !obj.contains_key("output_config"));

        let mut model = make_test_model("anthropic", "claude-old");
        model.capabilities = Some(json!({"supports_response_schema": false}));
        let mut obj = base(rf);
        adapter.transform_request(&mut obj, &model);
        assert!(!obj.contains_key("response_format") && !obj.contains_key("output_config"));
    }

    // -----------------------------------------------------------------------
    // Anthropic adapter — temperature / top_p / thinking
    // -----------------------------------------------------------------------
    //
    // Three rules layered, each pinned to a real 400 we've observed:
    //
    //   (A) claude-opus-4-7 deprecates `temperature` outright.
    //   (B) Claude 4.x family rejects `temperature` + `top_p` together.
    //   (C) Thinking on requires temperature == 1.0 (or absent) and no top_p.
    //
    // The tests below cover every combination that crosses a rule
    // boundary so a regression that re-introduces temperature on
    // Opus 4.7, or accidentally widens (A) to all of 4.x, fails fast.

    fn thinking_model() -> LlmModel {
        let mut m = make_test_model("anthropic", "claude-opus-4-7");
        m.thinking_enabled = Some(true);
        m
    }

    #[test]
    fn anthropic_thinking_enabled_injects_thinking_block() {
        let model = thinking_model();
        let mut obj = serde_json::Map::new();
        obj.insert("messages".into(), json!([]));
        AnthropicAdapter.transform_request(&mut obj, &model);
        let thinking = obj.get("thinking").expect("thinking block present");
        // Opus 4.7 answers a fixed budget with a 400.
        assert_eq!(
            thinking,
            &json!({"type": "adaptive", "display": "summarized"})
        );
    }

    #[test]
    fn thinking_uses_the_shape_each_model_accepts() {
        let block_for = |name: &str| {
            let mut model = make_test_model("anthropic", name);
            model.thinking_enabled = Some(true);
            let mut obj = serde_json::Map::new();
            obj.insert("messages".into(), json!([]));
            AnthropicAdapter.transform_request(&mut obj, &model);
            obj.get("thinking").cloned().unwrap()
        };
        let summarized = json!({"type": "adaptive", "display": "summarized"});
        for name in ["claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-4-8"] {
            assert_eq!(block_for(name), summarized, "{name}");
        }
        assert_eq!(block_for("claude-sonnet-4-6"), json!({"type": "adaptive"}));
        for name in ["claude-haiku-4-5", "claude-sonnet-4-5-20250929"] {
            assert_eq!(block_for(name)["type"], "enabled", "{name}");
            assert_eq!(block_for(name)["budget_tokens"], 10_000, "{name}");
        }
    }

    /// Under an output policy the route notes it on the body; the adapter asks
    /// for unreadable thinking where the model can omit it, whoever asked for
    /// thinking, and the note goes no further.
    #[test]
    fn the_omit_note_asks_for_omitted_thinking_where_the_model_can() {
        let run = |name: &str, thinking: Option<Value>, enabled: bool, note: bool| {
            let mut model = make_test_model("anthropic", name);
            model.thinking_enabled = Some(enabled);
            let mut obj = serde_json::Map::new();
            obj.insert("messages".into(), json!([]));
            if note {
                obj.insert(
                    crate::services::providers::OMIT_REASONING_MARKER.into(),
                    json!(true),
                );
            }
            if let Some(t) = thinking {
                obj.insert("thinking".into(), t);
            }
            AnthropicAdapter.transform_request(&mut obj, &model);
            assert!(!obj.contains_key(crate::services::providers::OMIT_REASONING_MARKER));
            obj
        };
        let transform = |name: &str, thinking: Option<Value>, enabled: bool| {
            run(name, thinking, enabled, true).get("thinking").cloned()
        };
        // A model that cannot omit keeps its request exactly as it would be
        // without the note: thinking stays on, and the response side clears
        // the text.
        let unchanged = |name: &str, thinking: Option<Value>, enabled: bool| {
            assert_eq!(
                run(name, thinking.clone(), enabled, true),
                run(name, thinking, enabled, false),
                "{name}"
            );
        };
        let omitted = json!({"type": "adaptive", "display": "omitted"});
        // Injected from the model row, and the caller's own summarized.
        assert_eq!(
            transform("claude-opus-4-7", None, true),
            Some(omitted.clone())
        );
        assert_eq!(
            transform(
                "claude-opus-5-5",
                Some(json!({"type": "adaptive", "display": "summarized"})),
                false
            ),
            Some(omitted)
        );
        // Off stays off; no thinking stays none.
        assert_eq!(
            transform("claude-opus-5-5", Some(json!({"type": "disabled"})), false),
            Some(json!({"type": "disabled"}))
        );
        assert_eq!(transform("claude-opus-5-5", None, false), None);
        unchanged("claude-sonnet-4-6", None, true);
        unchanged("claude-sonnet-4-5-20250929", None, true);
        let budget = json!({"type": "enabled", "budget_tokens": 4000});
        unchanged("claude-haiku-4-5", Some(budget.clone()), false);
        assert_eq!(
            transform("claude-haiku-4-5", Some(budget.clone()), false),
            Some(budget),
            "thinking stays on"
        );
        // Bedrock and Vertex ids meet the same rules as first-party ones.
        assert_eq!(
            transform("us.anthropic.claude-opus-4-7", None, true),
            Some(json!({"type": "adaptive", "display": "omitted"}))
        );
        unchanged("anthropic.claude-sonnet-4-5-20250929-v1:0", None, true);
        assert_eq!(
            transform("claude-opus-4-7@20260416", None, true),
            Some(json!({"type": "adaptive", "display": "omitted"}))
        );
    }

    #[test]
    fn a_callers_thinking_overrides_an_injected_default_temperature() {
        // The case: the model's default temperature was injected
        // and the caller's `thinking` then made Anthropic answer 400.
        let model = make_test_model("anthropic", "claude-sonnet-4-6");
        let mut obj = serde_json::Map::new();
        obj.insert("messages".into(), json!([]));
        obj.insert("temperature".into(), json!(0.7));
        obj.insert("top_p".into(), json!(0.9));
        obj.insert("top_k".into(), json!(40));
        obj.insert(
            "thinking".into(),
            json!({"type": "enabled", "budget_tokens": 2000}),
        );
        AnthropicAdapter.transform_request(&mut obj, &model);
        assert_eq!(obj.get("temperature"), Some(&json!(1.0)));
        assert!(!obj.contains_key("top_p"));
        assert!(!obj.contains_key("top_k"));
        assert_eq!(obj["thinking"]["budget_tokens"], 2000, "theirs, untouched");

        // Thinking explicitly off leaves sampling alone.
        let mut obj = serde_json::Map::new();
        obj.insert("messages".into(), json!([]));
        obj.insert("temperature".into(), json!(0.7));
        obj.insert("thinking".into(), json!({"type": "disabled"}));
        AnthropicAdapter.transform_request(&mut obj, &model);
        assert_eq!(obj.get("temperature"), Some(&json!(0.7)));

        // A model that retired sampling gets none, even with thinking on.
        let model = make_test_model("anthropic", "claude-opus-5-5");
        let mut obj = serde_json::Map::new();
        obj.insert("messages".into(), json!([]));
        obj.insert("temperature".into(), json!(0.7));
        obj.insert("thinking".into(), json!({"type": "adaptive"}));
        AnthropicAdapter.transform_request(&mut obj, &model);
        assert!(!obj.contains_key("temperature"));
    }

    #[test]
    fn thinking_survives_the_conversion_to_the_openai_shape() {
        let mut body = json!({
            "id": "msg_1", "type": "message", "role": "assistant",
            "model": "claude-opus-5-5", "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 2},
            "content": [
                {"type": "thinking", "thinking": "The ticket is about C7.", "signature": "sig-1"},
                {"type": "redacted_thinking", "data": "opaque"},
                {"type": "text", "text": "Checking."},
                {"type": "tool_use", "id": "t1", "name": "crm", "input": {}}
            ]
        });
        AnthropicAdapter.normalize_response(&mut body);
        let message = &body["choices"][0]["message"];
        assert_eq!(message["content"], "Checking.");
        assert_eq!(message["reasoning_content"], "The ticket is about C7.");
        assert_eq!(message["thinking_blocks"][0]["signature"], "sig-1");
        assert_eq!(message["thinking_blocks"][1]["data"], "opaque");
    }

    #[test]
    fn thinking_blocks_are_replayed_first_and_unchanged() {
        let model = make_test_model("anthropic", "claude-opus-5-5");
        let mut obj = serde_json::Map::new();
        obj.insert(
            "messages".into(),
            json!([
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": "Checking.",
                    "reasoning_content": "The ticket is about C7.",
                    "thinking_blocks": [
                        {"type": "thinking", "thinking": "The ticket is about C7.", "signature": "sig-1"},
                        {"type": "tool_use", "id": "forged", "name": "x", "input": {}}
                    ],
                    "tool_calls": [{"id": "t1", "type": "function",
                                    "function": {"name": "crm", "arguments": "{}"}}]
                },
                {"role": "tool", "tool_call_id": "t1", "content": "ok"},
                {"role": "assistant", "content": "Done.", "reasoning_content": "x",
                 "thinking_blocks": [{"type": "redacted_thinking", "data": "opaque"}]},
                {"role": "user", "content": "thanks"}
            ]),
        );
        AnthropicAdapter.transform_request(&mut obj, &model);
        let msgs = obj["messages"].as_array().unwrap();
        let first = &msgs[1];
        let blocks = first["content"].as_array().unwrap();
        assert_eq!(blocks[0]["signature"], "sig-1");
        assert_eq!(blocks[1]["type"], "text");
        assert_eq!(blocks[2]["id"], "t1", "the forged block is not replayed");
        assert_eq!(blocks.len(), 3);
        let second = msgs.iter().rfind(|m| m["role"] == "assistant").unwrap();
        assert_eq!(second["content"][0]["type"], "redacted_thinking");
        assert_eq!(second["content"][1]["text"], "Done.");
        for m in msgs {
            let m = m.as_object().unwrap();
            assert!(!m.contains_key("reasoning_content"));
            assert!(!m.contains_key("thinking_blocks"));
        }
    }

    #[test]
    fn a_streamed_thinking_block_reaches_the_client_whole() {
        let mut n = AnthropicStreamNormalizer::new();
        n.convert(
            &json!({"type": "message_start", "message": {"id": "m", "model": "x"}}).to_string(),
        );
        let start = json!({"type": "content_block_start", "index": 0,
                           "content_block": {"type": "thinking", "thinking": "", "signature": ""}});
        assert!(n.convert(&start.to_string()).is_empty());
        let mut reasoning = String::new();
        for t in ["The ticket ", "is about C7."] {
            let e = json!({"type": "content_block_delta", "index": 0,
                           "delta": {"type": "thinking_delta", "thinking": t}});
            let out = n.convert(&e.to_string());
            let c: Value = serde_json::from_str(&out[0]).unwrap();
            reasoning.push_str(
                c["choices"][0]["delta"]["reasoning_content"]
                    .as_str()
                    .unwrap(),
            );
        }
        assert_eq!(reasoning, "The ticket is about C7.");
        let sig = json!({"type": "content_block_delta", "index": 0,
                         "delta": {"type": "signature_delta", "signature": "sig-1"}});
        assert!(n.convert(&sig.to_string()).is_empty());
        let out = n.convert(&json!({"type": "content_block_stop", "index": 0}).to_string());
        let c: Value = serde_json::from_str(&out[0]).unwrap();
        assert_eq!(
            c["choices"][0]["delta"]["thinking_blocks"][0],
            json!({"type": "thinking", "thinking": "The ticket is about C7.", "signature": "sig-1"})
        );
        // An ordinary text block closing emits nothing extra.
        n.convert(
            &json!({"type": "content_block_start", "index": 1,
                    "content_block": {"type": "text", "text": ""}})
            .to_string(),
        );
        assert!(
            n.convert(&json!({"type": "content_block_stop", "index": 1}).to_string())
                .is_empty()
        );
    }

    // Rule (A) — opus-4-7 strips temperature, regardless of thinking.

    #[test]
    fn anthropic_opus_4_7_strips_temperature_thinking_off() {
        let mut model = make_test_model("anthropic", "claude-opus-4-7");
        model.thinking_enabled = Some(false);
        let mut obj = serde_json::Map::new();
        obj.insert("messages".into(), json!([]));
        obj.insert("temperature".into(), json!(0.2));
        AnthropicAdapter.transform_request(&mut obj, &model);
        assert!(
            !obj.contains_key("temperature"),
            "Opus 4.7 deprecation: temperature must be stripped"
        );
    }

    #[test]
    fn anthropic_opus_4_7_strips_temperature_thinking_on() {
        // Thinking on does NOT re-introduce temperature on Opus 4.7 —
        // rule (A) wins over rule (C)'s coerce-to-1.0 behaviour.
        let model = thinking_model();
        let mut obj = serde_json::Map::new();
        obj.insert("messages".into(), json!([]));
        obj.insert("temperature".into(), json!(0.2));
        AnthropicAdapter.transform_request(&mut obj, &model);
        assert!(
            !obj.contains_key("temperature"),
            "Opus 4.7: rule (A) strip wins over rule (C) coercion"
        );
    }

    // Rule (B) — Claude 4.x rejects temperature + top_p together.

    #[test]
    fn anthropic_sonnet_4_6_drops_top_p_when_temperature_present() {
        // Sonnet 4.6 keeps temperature but drops top_p when both are sent.
        let mut model = make_test_model("anthropic", "claude-sonnet-4-6");
        model.thinking_enabled = Some(false);
        let mut obj = serde_json::Map::new();
        obj.insert("messages".into(), json!([]));
        obj.insert("temperature".into(), json!(0.2));
        obj.insert("top_p".into(), json!(0.9));
        AnthropicAdapter.transform_request(&mut obj, &model);
        assert_eq!(
            obj.get("temperature"),
            Some(&json!(0.2)),
            "Sonnet 4.6 keeps temperature when thinking off"
        );
        assert!(
            !obj.contains_key("top_p"),
            "Sonnet 4.6 drops top_p when temperature is also present"
        );
    }

    #[test]
    fn anthropic_haiku_4_5_keeps_top_p_when_temperature_absent() {
        // Without temperature, top_p is fine on its own — rule (B)
        // only fires when both are present.
        let mut model = make_test_model("anthropic", "claude-haiku-4-5");
        model.thinking_enabled = Some(false);
        let mut obj = serde_json::Map::new();
        obj.insert("messages".into(), json!([]));
        obj.insert("top_p".into(), json!(0.9));
        AnthropicAdapter.transform_request(&mut obj, &model);
        assert_eq!(obj.get("top_p"), Some(&json!(0.9)));
    }

    // Rule (C) — thinking on coerces temperature to 1.0 + strips top_p.

    #[test]
    fn anthropic_sonnet_4_6_thinking_coerces_temperature() {
        // Sonnet 4.6 with thinking on: temperature is coerced to 1.0
        // (not stripped — Opus 4.7 is the only model where rule (A)
        // strips). top_p goes away regardless.
        let mut model = make_test_model("anthropic", "claude-sonnet-4-6");
        model.thinking_enabled = Some(true);
        let mut obj = serde_json::Map::new();
        obj.insert("messages".into(), json!([]));
        obj.insert("temperature".into(), json!(0.2));
        obj.insert("top_p".into(), json!(0.9));
        AnthropicAdapter.transform_request(&mut obj, &model);
        assert_eq!(
            obj.get("temperature"),
            Some(&json!(1.0)),
            "Sonnet 4.6 + thinking: temperature coerced to 1.0"
        );
        assert!(!obj.contains_key("top_p"));
    }

    #[test]
    fn anthropic_thinking_enabled_strips_top_p() {
        let model = thinking_model();
        let mut obj = serde_json::Map::new();
        obj.insert("messages".into(), json!([]));
        obj.insert("top_p".into(), json!(0.9));
        AnthropicAdapter.transform_request(&mut obj, &model);
        assert!(!obj.contains_key("top_p"));
    }

    // Negative tests — Claude 3.x and thinking-off paths must stay inert.

    #[test]
    fn anthropic_claude3_is_fully_inert() {
        // Claude 3.x predates all three rules. With thinking off,
        // temperature and top_p survive the adapter untouched.
        let mut model = make_test_model("anthropic", "claude-3-5-sonnet-20240620");
        model.thinking_enabled = Some(false);
        let mut obj = serde_json::Map::new();
        obj.insert("messages".into(), json!([]));
        obj.insert("temperature".into(), json!(0.2));
        obj.insert("top_p".into(), json!(0.9));
        AnthropicAdapter.transform_request(&mut obj, &model);
        assert_eq!(obj.get("temperature"), Some(&json!(0.2)));
        assert_eq!(obj.get("top_p"), Some(&json!(0.9)));
        assert!(!obj.contains_key("thinking"));
    }

    #[test]
    fn anthropic_thinking_disabled_does_not_inject_thinking_block() {
        // Even on Claude 4.x where rules (A)/(B) trigger, an off
        // thinking flag must NOT add the thinking block.
        let mut model = make_test_model("anthropic", "claude-opus-4-6");
        model.thinking_enabled = Some(false);
        let mut obj = serde_json::Map::new();
        obj.insert("messages".into(), json!([]));
        AnthropicAdapter.transform_request(&mut obj, &model);
        assert!(!obj.contains_key("thinking"));
    }

    #[test]
    fn anthropic_thinking_enabled_preserves_caller_thinking_block() {
        // If the caller already specified a `thinking` block (e.g. with a
        // custom budget_tokens), the adapter must not stomp on it — the
        // request body wins over the model-row default.
        let model = thinking_model();
        let mut obj = serde_json::Map::new();
        obj.insert("messages".into(), json!([]));
        obj.insert(
            "thinking".into(),
            json!({ "type": "enabled", "budget_tokens": 25_000 }),
        );
        AnthropicAdapter.transform_request(&mut obj, &model);
        assert_eq!(
            obj.get("thinking")
                .unwrap()
                .get("budget_tokens")
                .and_then(|v| v.as_i64()),
            Some(25_000),
        );
    }

    // -----------------------------------------------------------------------
    // Tool-result round-trip — agent skill / MCP-tool follow-up
    // -----------------------------------------------------------------------
    //
    // Skill execution from the portal goes:
    //   1. User asks LLM something
    //   2. LLM responds with `tool_calls` (assistant message)
    //   3. Portal executes the MCP tool / skill
    //   4. Portal sends a follow-up chat completion containing
    //      `[{role: "user"}, {role: "assistant", tool_calls: [...]},
    //        {role: "tool", tool_call_id: "...", content: "<result>"}]`
    //   5. LLM synthesises a final answer using the tool result
    //
    // Step 4 is where Anthropic differs hard from OpenAI: Anthropic
    // does not accept `role: "tool"` messages. The adapter must:
    //   * Convert `assistant.tool_calls` into a `tool_use` content
    //     block on the assistant message.
    //   * Convert `role: "tool"` into `role: "user"` with a
    //     `tool_result` content block carrying the *same*
    //     `tool_use_id` and the result text.
    //
    // If the conversion drops the tool_result content, the LLM never
    // sees the skill's output and either hangs or hallucinates a
    // response that ignores the data — exactly the symptom flagged in
    // the agent-skill follow-up regression. These tests pin the wire
    // shape so that regression can never reach prod silently.

    #[test]
    fn anthropic_tool_role_message_becomes_user_with_tool_result_block() {
        let model = make_test_model("anthropic", "claude-opus-4-7");
        let mut obj = serde_json::Map::new();
        obj.insert(
            "messages".into(),
            json!([
                { "role": "user", "content": "What's the weather in Stockholm?" },
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call_abc123",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": "{\"city\":\"Stockholm\"}"
                        }
                    }]
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_abc123",
                    "content": "[Server: weather-mcp]\n7°C, partly cloudy"
                },
            ]),
        );
        AnthropicAdapter.transform_request(&mut obj, &model);

        let msgs = obj
            .get("messages")
            .and_then(|m| m.as_array())
            .expect("messages still present after transform");

        // After the transform and the same-role merge, the
        // sequence is: user (original prompt) / assistant
        // (tool_use block) / user (tool_result block).
        assert_eq!(
            msgs.len(),
            3,
            "expected 3 messages after transform, got {msgs:#?}"
        );

        // Assistant message must carry the tool_use block with the
        // *original* call id — the tool_result references it later.
        let assistant = &msgs[1];
        assert_eq!(
            assistant.get("role").and_then(|r| r.as_str()),
            Some("assistant")
        );
        let assistant_blocks = assistant
            .get("content")
            .and_then(|c| c.as_array())
            .expect("assistant content must be a content-block array");
        let tool_use = assistant_blocks
            .iter()
            .find(|b| b.get("type").and_then(|t| t.as_str()) == Some("tool_use"))
            .expect("assistant must contain a tool_use block");
        assert_eq!(
            tool_use.get("id").and_then(|v| v.as_str()),
            Some("call_abc123")
        );
        assert_eq!(
            tool_use.get("name").and_then(|v| v.as_str()),
            Some("get_weather")
        );
        assert_eq!(
            tool_use.get("input"),
            Some(&json!({"city": "Stockholm"})),
            "tool_use.input must be the parsed arguments object",
        );

        // Tool message must have become a user message with a
        // tool_result block that:
        //   - references the same tool_use_id, AND
        //   - preserves the tool's output content verbatim. If either
        //     drops, the LLM can't tie the result back to the call
        //     and won't synthesise a response that uses the data.
        let tool_followup = &msgs[2];
        assert_eq!(
            tool_followup.get("role").and_then(|r| r.as_str()),
            Some("user"),
            "role:tool must be remapped to role:user for Anthropic",
        );
        let followup_blocks = tool_followup
            .get("content")
            .and_then(|c| c.as_array())
            .expect("user follow-up must have content-block array, not a string");
        let tool_result = followup_blocks
            .iter()
            .find(|b| b.get("type").and_then(|t| t.as_str()) == Some("tool_result"))
            .expect("follow-up must contain a tool_result block");
        assert_eq!(
            tool_result.get("tool_use_id").and_then(|v| v.as_str()),
            Some("call_abc123"),
            "tool_use_id must match the assistant's tool_use.id",
        );
        let result_text = tool_result
            .get("content")
            .and_then(|v| v.as_str())
            .expect("tool_result.content must be a string");
        assert!(
            result_text.contains("7°C, partly cloudy"),
            "tool result content must be preserved end-to-end; got: {result_text}",
        );
        assert!(
            result_text.contains("[Server: weather-mcp]"),
            "server attribution must survive the transform",
        );
    }

    #[test]
    fn anthropic_tool_result_content_object_serialises_verbatim() {
        // Some MCP tools return JSON; the portal flattens it before
        // sending, but as a defence the adapter should still preserve
        // whatever it gets — never drop the tool's output silently.
        let model = make_test_model("anthropic", "claude-sonnet-4-6");
        let mut obj = serde_json::Map::new();
        obj.insert(
            "messages".into(),
            json!([
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call_xyz",
                        "type": "function",
                        "function": { "name": "lookup", "arguments": "{}" }
                    }]
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_xyz",
                    "content": "{\"items\":[1,2,3]}"
                },
            ]),
        );
        AnthropicAdapter.transform_request(&mut obj, &model);

        let msgs = obj.get("messages").and_then(|m| m.as_array()).unwrap();
        let last = msgs.last().unwrap();
        let blocks = last.get("content").and_then(|c| c.as_array()).unwrap();
        let tool_result = blocks
            .iter()
            .find(|b| b.get("type").and_then(|t| t.as_str()) == Some("tool_result"))
            .expect("tool_result block missing");
        let s = tool_result
            .get("content")
            .and_then(|v| v.as_str())
            .expect("tool_result.content must be a string");
        assert!(
            s.contains("\"items\""),
            "JSON-shape tool result must survive verbatim; got: {s}",
        );
    }

    #[test]
    fn anthropic_tool_result_empty_content_does_not_crash() {
        // Edge case: tool returned no content (rare but observed when
        // a skill exits with success but empty stdout). We still want
        // a well-formed tool_result block referencing the tool_use_id
        // so the LLM can at least acknowledge the call.
        let model = make_test_model("anthropic", "claude-haiku-4-5");
        let mut obj = serde_json::Map::new();
        obj.insert(
            "messages".into(),
            json!([
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call_empty",
                        "type": "function",
                        "function": { "name": "noop", "arguments": "{}" }
                    }]
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_empty",
                    "content": ""
                },
            ]),
        );
        AnthropicAdapter.transform_request(&mut obj, &model);
        let msgs = obj.get("messages").and_then(|m| m.as_array()).unwrap();
        let last = msgs.last().unwrap();
        assert_eq!(last.get("role").and_then(|r| r.as_str()), Some("user"));
        let blocks = last.get("content").and_then(|c| c.as_array()).unwrap();
        let tool_result = blocks
            .iter()
            .find(|b| b.get("type").and_then(|t| t.as_str()) == Some("tool_result"))
            .expect("empty tool_result block must still exist");
        assert_eq!(
            tool_result.get("tool_use_id").and_then(|v| v.as_str()),
            Some("call_empty"),
        );
    }
}

// ===========================================================================
// OpenAI-compat shape conformance — Anthropic→OpenAI adapter
// ===========================================================================
//
// Every Claude response that reaches our `/v1/proxy/llm/chat/completions`
// endpoint passes through `normalize_response` (non-streaming) or
// `AnthropicStreamNormalizer::convert` (streaming). These are the
// highest-traffic OpenAI-shape emitters in the whole gateway.
//
// These tests pin the shape contract downstream SDKs rely on
// (openai-python, openai-node, LangChain). They sit alongside
// the happy-path tests above and exist specifically to catch silent
// drift — e.g., a field accidentally renamed to Anthropic's naming,
// snake_case → camelCase mixups, or streaming chunks that acquire a
// non-streaming `message` key.
//
// Reference: https://platform.openai.com/docs/api-reference/chat

#[cfg(test)]
mod openai_compat_conformance {
    use super::*;
    use serde_json::json;

    // ---- normalize_response (non-streaming) -------------------------------

    fn normalize(body: Value) -> Value {
        let adapter = AnthropicAdapter;
        let mut b = body;
        adapter.normalize_response(&mut b);
        b
    }

    fn minimal_anthropic_response() -> Value {
        json!({
            "id": "msg_xyz",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "hello"}],
            "model": "claude-3-opus-20240229",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5}
        })
    }

    #[test]
    fn normalized_has_all_documented_top_level_fields() {
        let out = normalize(minimal_anthropic_response());
        for key in ["id", "object", "created", "model", "choices", "usage"] {
            assert!(
                out.get(key).is_some(),
                "missing top-level `{key}` in {out:#}"
            );
        }
    }

    #[test]
    fn normalized_object_field_is_literal_chat_completion() {
        // SDKs dispatch on this. Off-by-one letter breaks every SDK.
        let out = normalize(minimal_anthropic_response());
        assert_eq!(out["object"], "chat.completion");
    }

    #[test]
    fn normalized_id_carries_chatcmpl_prefix() {
        // Telemetry aggregators bucket by the `chatcmpl-` prefix. The
        // adapter re-wraps Anthropic's `msg_XXX` id as
        // `chatcmpl-msg_XXX` to keep that grouping honest.
        let out = normalize(minimal_anthropic_response());
        let id = out["id"].as_str().unwrap();
        assert!(
            id.starts_with("chatcmpl-"),
            "id `{id}` must start with `chatcmpl-`",
        );
    }

    #[test]
    fn normalized_created_is_unix_seconds_integer() {
        // OpenAI emits created as integer seconds. Not ms, not an
        // ISO string. Clients use it for latency accounting.
        let out = normalize(minimal_anthropic_response());
        let created = out["created"].as_i64().expect("created is integer");
        // Sanity: must be in the last decade, not in the future more
        // than a minute.
        let now = chrono::Utc::now().timestamp();
        assert!(
            created > 1_600_000_000 && created <= now + 60,
            "created `{created}` out of plausible range (now={now})",
        );
    }

    #[test]
    fn normalized_choice_has_all_documented_fields() {
        let out = normalize(minimal_anthropic_response());
        let choice = &out["choices"][0];
        for key in ["index", "message", "finish_reason"] {
            assert!(
                choice.get(key).is_some(),
                "missing choice.{key} in {choice:#}",
            );
        }
    }

    #[test]
    fn normalized_message_role_is_assistant() {
        let out = normalize(minimal_anthropic_response());
        assert_eq!(out["choices"][0]["message"]["role"], "assistant");
    }

    #[test]
    fn normalized_usage_uses_openai_token_field_names() {
        // Critical drift guard: adapter must rename
        // input_tokens→prompt_tokens, output_tokens→completion_tokens,
        // AND compute total_tokens. Leaking Anthropic names makes
        // billing dashboards read 0.
        let out = normalize(minimal_anthropic_response());
        let usage = &out["usage"];
        assert_eq!(usage["prompt_tokens"], 10);
        assert_eq!(usage["completion_tokens"], 5);
        assert_eq!(usage["total_tokens"], 15);
        assert!(
            usage.get("input_tokens").is_none(),
            "leaked Anthropic field"
        );
        assert!(
            usage.get("output_tokens").is_none(),
            "leaked Anthropic field"
        );
    }

    #[test]
    fn normalized_tool_use_emits_openai_tool_calls_shape() {
        // The tool_use → tool_calls conversion is the sharpest part
        // of this path. Fields that must be present and correctly named.
        let out = normalize(json!({
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": "toolu_01ABC",
                "name": "get_weather",
                "input": {"city": "Oslo"}
            }],
            "model": "claude-3-opus",
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 5, "output_tokens": 2}
        }));
        let msg = &out["choices"][0]["message"];
        let tool_calls = msg["tool_calls"].as_array().expect("tool_calls is array");
        assert_eq!(tool_calls.len(), 1);
        let tc = &tool_calls[0];
        assert_eq!(tc["id"], "toolu_01ABC");
        assert_eq!(tc["type"], "function");
        assert_eq!(tc["function"]["name"], "get_weather");
        // OpenAI spec: function.arguments is a JSON-encoded string
        // (not an object). Clients parse it themselves.
        let args = tc["function"]["arguments"]
            .as_str()
            .expect("arguments must be a string, not an object");
        let parsed: Value = serde_json::from_str(args).unwrap();
        assert_eq!(parsed, json!({"city": "Oslo"}));
    }

    #[test]
    fn normalized_tool_calls_index_is_sequential() {
        // Each tool_call carries an `index` that SDKs use to stitch
        // streamed chunks back together (even though this is the
        // non-streaming path, the field must be present).
        let out = normalize(json!({
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "a", "input": {}},
                {"type": "tool_use", "id": "t2", "name": "b", "input": {}}
            ],
            "model": "claude-3-opus",
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1}
        }));
        let tcs = out["choices"][0]["message"]["tool_calls"]
            .as_array()
            .unwrap();
        assert_eq!(tcs[0]["index"], 0);
        assert_eq!(tcs[1]["index"], 1);
    }

    #[test]
    fn normalized_content_is_null_when_only_tool_calls() {
        // OpenAI spec: message.content is `null` (not empty string)
        // when the assistant returns only tool_calls. SDKs rely on
        // the null to know there's no textual reply.
        let out = normalize(json!({
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "a", "input": {}}],
            "model": "claude-3-opus",
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1}
        }));
        assert_eq!(
            out["choices"][0]["message"]["content"],
            Value::Null,
            "pure tool_use replies must carry null content",
        );
    }

    #[test]
    fn normalized_no_snake_to_camel_drift() {
        let out = normalize(minimal_anthropic_response());
        let s = serde_json::to_string(&out).unwrap();
        for camel in [
            "finishReason",
            "promptTokens",
            "completionTokens",
            "totalTokens",
            "toolCalls",
        ] {
            assert!(
                !s.contains(camel),
                "response leaked camelCase `{camel}`: {s}"
            );
        }
    }

    #[test]
    fn normalized_no_anthropic_field_leakage() {
        // No top-level keys from the Anthropic shape should survive
        // (stop_reason, stop_sequence, content array, type, etc.).
        let out = normalize(minimal_anthropic_response());
        for leak in ["type", "stop_reason", "stop_sequence"] {
            assert!(
                out.get(leak).is_none(),
                "Anthropic field `{leak}` leaked into OpenAI-shape output: {out:#}",
            );
        }
        // And content must not be at the top level — it moved to
        // choices[].message.content.
        assert!(
            !out["content"].is_array(),
            "Anthropic top-level content leaked"
        );
    }

    #[test]
    fn normalize_is_no_op_for_non_message_bodies() {
        // If upstream returned an error body or a different type, we
        // must not mangle it. The adapter should see `type != "message"`
        // and return without mutation so the error can bubble up cleanly.
        let untouched = json!({
            "type": "error",
            "error": {"type": "invalid_request_error", "message": "bad"}
        });
        let out = normalize(untouched.clone());
        assert_eq!(out, untouched, "non-message body must not be transformed");
    }

    // ---- AnthropicStreamNormalizer ---------------------------------------
    //
    // The normalizer is stateful. Each test constructs a fresh one
    // and feeds it a realistic sequence of Anthropic SSE events.

    fn collect(norm: &mut AnthropicStreamNormalizer, events: &[&str]) -> Vec<Value> {
        let mut out = Vec::new();
        for e in events {
            for chunk_str in norm.convert(e) {
                out.push(serde_json::from_str(&chunk_str).unwrap());
            }
        }
        out
    }

    #[test]
    fn stream_message_start_chunk_has_all_openai_fields() {
        let mut n = AnthropicStreamNormalizer::new();
        let chunks = collect(
            &mut n,
            &[
                r#"{"type":"message_start","message":{"id":"msg_abc","model":"claude-3-opus","usage":{"input_tokens":10}}}"#,
            ],
        );
        assert_eq!(chunks.len(), 1);
        let c = &chunks[0];
        for key in ["id", "object", "created", "model", "choices"] {
            assert!(c.get(key).is_some(), "message_start missing `{key}`: {c}");
        }
        assert_eq!(c["object"], "chat.completion.chunk");
        assert_eq!(c["model"], "claude-3-opus");
    }

    #[test]
    fn stream_message_start_first_chunk_carries_role_delta() {
        // OpenAI convention: the first streaming chunk carries
        // `delta: {"role": "assistant", ...}` so SDKs know which
        // turn they're consuming. Subsequent chunks have no role.
        let mut n = AnthropicStreamNormalizer::new();
        let chunks = collect(
            &mut n,
            &[r#"{"type":"message_start","message":{"id":"m","model":"c"}}"#],
        );
        let delta = &chunks[0]["choices"][0]["delta"];
        assert_eq!(delta["role"], "assistant");
    }

    #[test]
    fn stream_chunk_carries_delta_not_message() {
        // Drift guard: streaming chunks never have `message`. If
        // someone copies the non-streaming shape into the stream path,
        // SDKs silently render nothing.
        let mut n = AnthropicStreamNormalizer::new();
        let chunks = collect(
            &mut n,
            &[r#"{"type":"message_start","message":{"id":"m","model":"c"}}"#],
        );
        let choice = &chunks[0]["choices"][0];
        assert!(choice.get("delta").is_some(), "chunk must have delta");
        assert!(
            choice.get("message").is_none(),
            "chunk must NOT have message (that's for non-streaming)",
        );
    }

    #[test]
    fn stream_text_delta_emits_content_chunk() {
        let mut n = AnthropicStreamNormalizer::new();
        // message_start to seed id/model, then a text delta
        let chunks = collect(
            &mut n,
            &[
                r#"{"type":"message_start","message":{"id":"m","model":"c"}}"#,
                r#"{"type":"content_block_delta","delta":{"type":"text_delta","text":"hello"}}"#,
            ],
        );
        // Second chunk carries the text in choices[0].delta.content.
        assert_eq!(chunks[1]["choices"][0]["delta"]["content"], "hello");
        assert_eq!(chunks[1]["object"], "chat.completion.chunk");
    }

    #[test]
    fn stream_message_delta_carries_finish_reason_and_usage() {
        // On message_delta we emit a chunk with finish_reason set,
        // plus usage. OpenAI streams the final usage on the terminal
        // chunk (with stream_options).
        let mut n = AnthropicStreamNormalizer::new();
        let chunks = collect(
            &mut n,
            &[
                r#"{"type":"message_start","message":{"id":"m","model":"c"}}"#,
                r#"{"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"input_tokens":10,"output_tokens":5}}"#,
            ],
        );
        let last = chunks.last().unwrap();
        assert_eq!(last["choices"][0]["finish_reason"], "stop");
        let usage = &last["usage"];
        assert_eq!(usage["prompt_tokens"], 10);
        assert_eq!(usage["completion_tokens"], 5);
        assert_eq!(usage["total_tokens"], 15);
        // Must NOT leak Anthropic naming in the final usage chunk.
        assert!(usage.get("input_tokens").is_none());
        assert!(usage.get("output_tokens").is_none());
    }

    #[test]
    fn stream_stop_reason_mapping_matches_non_streaming_path() {
        // The mapping table must be identical to normalize_response.
        // Different mappings between paths would mean the same
        // Claude request, streamed or not, produces different
        // finish_reasons — a fun bug to debug.
        let cases = [
            ("end_turn", "stop"),
            ("stop_sequence", "stop"),
            ("max_tokens", "length"),
            ("tool_use", "tool_calls"),
        ];
        for (anthropic, openai) in cases {
            let mut n = AnthropicStreamNormalizer::new();
            let chunks = collect(
                &mut n,
                &[
                    r#"{"type":"message_start","message":{"id":"m","model":"c"}}"#,
                    &format!(
                        r#"{{"type":"message_delta","delta":{{"stop_reason":"{anthropic}"}}}}"#
                    ),
                ],
            );
            let last = chunks.last().unwrap();
            assert_eq!(
                last["choices"][0]["finish_reason"], openai,
                "`{anthropic}` must map to `{openai}`",
            );
        }
    }

    #[test]
    fn stream_tool_use_content_block_start_emits_tool_call_delta() {
        let mut n = AnthropicStreamNormalizer::new();
        let chunks = collect(
            &mut n,
            &[
                r#"{"type":"message_start","message":{"id":"m","model":"c"}}"#,
                r#"{"type":"content_block_start","content_block":{"type":"tool_use","id":"toolu_1","name":"weather"}}"#,
            ],
        );
        let tc_chunk = chunks.last().unwrap();
        let tool_calls = tc_chunk["choices"][0]["delta"]["tool_calls"]
            .as_array()
            .expect("tool_calls on delta");
        let tc = &tool_calls[0];
        assert_eq!(tc["index"], 0);
        assert_eq!(tc["id"], "toolu_1");
        assert_eq!(tc["type"], "function");
        assert_eq!(tc["function"]["name"], "weather");
        assert_eq!(tc["function"]["arguments"], "");
    }

    #[test]
    fn stream_input_json_delta_streams_argument_chunks() {
        // Streaming tool calls accumulate arguments via
        // `input_json_delta` — each chunk's `partial_json` is an
        // append-only string piece. SDKs stitch them together.
        //
        // Sequence expected:
        //   1. content_block_start  → `{arguments: ""}`  (init; SDK needs this)
        //   2. input_json_delta #1  → `{arguments: "{\"x\":"}`
        //   3. input_json_delta #2  → `{arguments: "1}"}`
        let mut n = AnthropicStreamNormalizer::new();
        let chunks = collect(
            &mut n,
            &[
                r#"{"type":"message_start","message":{"id":"m","model":"c"}}"#,
                r#"{"type":"content_block_start","content_block":{"type":"tool_use","id":"t","name":"f"}}"#,
                r#"{"type":"content_block_delta","delta":{"type":"input_json_delta","partial_json":"{\"x\":"}}"#,
                r#"{"type":"content_block_delta","delta":{"type":"input_json_delta","partial_json":"1}"}}"#,
            ],
        );
        // All chunks that carry a tool_calls arguments field.
        let arg_chunks: Vec<&Value> = chunks
            .iter()
            .filter(|c| {
                c["choices"][0]["delta"]["tool_calls"][0]["function"]
                    .get("arguments")
                    .is_some()
            })
            .collect();
        // Init chunk + two delta chunks = 3. Dropping the init breaks
        // openai-python's tool-call accumulator.
        assert_eq!(arg_chunks.len(), 3, "expected init + 2 delta chunks");
        assert_eq!(
            arg_chunks[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"], "",
            "content_block_start must seed an empty arguments field",
        );
        assert_eq!(
            arg_chunks[1]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"],
            "{\"x\":",
        );
        assert_eq!(
            arg_chunks[2]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"],
            "1}",
        );
        // Concatenation yields valid JSON.
        let joined: String = arg_chunks
            .iter()
            .map(|c| {
                c["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
                    .as_str()
                    .unwrap()
                    .to_string()
            })
            .collect();
        let parsed: Value = serde_json::from_str(&joined).expect("concatenated args parse");
        assert_eq!(parsed, json!({"x": 1}));
    }

    #[test]
    fn stream_thinking_delta_is_reasoning_not_content() {
        // Thinking used to be dropped, which lost it for the client AND the
        // audit row. It travels as `reasoning_content`, the field
        // OpenAI-shaped clients and the rest of the proxy read for reasoning,
        // and never as `content`: it is not the answer.
        let mut n = AnthropicStreamNormalizer::new();
        let chunks = collect(
            &mut n,
            &[
                r#"{"type":"message_start","message":{"id":"m","model":"c"}}"#,
                r#"{"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}"#,
                r#"{"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"hmm"}}"#,
            ],
        );
        assert_eq!(chunks.len(), 2);
        let delta = &chunks[1]["choices"][0]["delta"];
        assert_eq!(delta["reasoning_content"], "hmm");
        assert!(delta.get("content").is_none());
    }

    #[test]
    fn stream_unknown_event_does_not_panic() {
        // New Anthropic event types roll out periodically; a stream
        // normalizer that panics on an unknown type takes down the
        // whole response. Silent drop is the correct behavior.
        let mut n = AnthropicStreamNormalizer::new();
        let chunks = collect(&mut n, &[r#"{"type":"some_future_event","details":{}}"#]);
        assert_eq!(chunks.len(), 0, "unknown event types must not yield output");
    }

    #[test]
    fn stream_no_snake_to_camel_drift_across_all_events() {
        let mut n = AnthropicStreamNormalizer::new();
        let chunks = collect(
            &mut n,
            &[
                r#"{"type":"message_start","message":{"id":"m","model":"c","usage":{"input_tokens":10}}}"#,
                r#"{"type":"content_block_start","content_block":{"type":"tool_use","id":"t","name":"f"}}"#,
                r#"{"type":"content_block_delta","delta":{"type":"input_json_delta","partial_json":"{}"}}"#,
                r#"{"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"input_tokens":10,"output_tokens":5}}"#,
            ],
        );
        for c in chunks {
            let s = serde_json::to_string(&c).unwrap();
            for camel in [
                "finishReason",
                "promptTokens",
                "completionTokens",
                "totalTokens",
                "toolCalls",
            ] {
                assert!(
                    !s.contains(camel),
                    "streamed chunk leaked camelCase `{camel}`: {s}",
                );
            }
        }
    }
}
