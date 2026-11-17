// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! OpenAI Chat Completions to and from the Bedrock Runtime `Converse` and
//! `ConverseStream` operations.
//!
//! Request (`to_converse`), from the body `providers::prepare_request` left:
//!
//! | Chat Completions | Converse |
//! |---|---|
//! | `system` / `developer` messages | `system: [{text}]` |
//! | `user` text and `image_url` (base64 data URI) parts | `{text}`, `{image: {format, source: {bytes}}}` |
//! | assistant `tool_calls` | `{toolUse: {toolUseId, name, input}}` |
//! | `tool` messages | a `user` message with `{toolResult: {toolUseId, content: [{text}]}}` |
//! | `tools`, `tool_choice` | `toolConfig.tools[].toolSpec`, `toolConfig.toolChoice` (`auto`, `any`, `tool`) |
//! | `max_tokens` / `max_completion_tokens`, `temperature`, `top_p`, `stop` | `inferenceConfig` (`maxTokens`, `temperature`, `topP`, `stopSequences`) |
//! | `response_format` JSON schema, where the model has it | `outputConfig.textFormat` |
//!
//! Consecutive messages of one role are merged. A parameter with no
//! Converse field is not sent, the gateway's rule for a parameter a
//! provider does not take (`llm_adapter/README.md`, "Parameter dropping").
//! A feature the model does not support ([`super::Features`]) is an error
//! naming it: the request is refused, not sent without the feature.
//!
//! Response (`response_to_openai`, [`StreamTranslator`]): `text` blocks are
//! the content, `toolUse` blocks the tool calls, `stopReason` the finish
//! reason, `usage` the usage. `reasoningContent` blocks are not returned.

use futures::StreamExt;
use serde_json::{Map, Value, json};

use super::Features;
use crate::models::db::LlmModel;
use crate::services::anthropic_adapter::backend::bedrock::{
    EventStreamStep, next_event_stream_message,
};

// ---------------------------------------------------------------------------
// Request
// ---------------------------------------------------------------------------

fn refusal(model: &LlmModel, what: &str) -> String {
    format!(
        "model '{}' on Amazon Bedrock does not support {what} (as AWS documents the model for \
         the Converse API). The request was not sent.",
        model.model_name
    )
}

/// The reason a chat request cannot be served by the Converse row `model`
/// (a feature the model does not support, content Converse has no block
/// for); `None` when it can, and for a row Converse does not serve.
pub fn request_conflict(model: &LlmModel, request: &Value, stream: bool) -> Option<String> {
    if !super::serves_chat(model) {
        return None;
    }
    to_converse(request, model, stream).err()
}

/// One `image_url` part as a Converse image block.
fn image_block(part: &Value, model: &LlmModel, features: Features) -> Result<Value, String> {
    let url = part
        .get("image_url")
        .and_then(|i| i.get("url").or(Some(i)))
        .and_then(Value::as_str)
        .unwrap_or_default();
    let Some(rest) = url.strip_prefix("data:") else {
        return Err(
            "an image URL cannot be sent to Amazon Bedrock Converse, which takes the image \
             itself: send a base64 data URI (data:image/png;base64,…). The request was not sent."
                .to_string(),
        );
    };
    if !features.images {
        return Err(refusal(model, "image input"));
    }
    let (media, data) = rest
        .split_once(";base64,")
        .ok_or("an image data URI must be base64 (data:image/png;base64,…)")?;
    let format = match media {
        "image/png" => "png",
        "image/jpeg" | "image/jpg" => "jpeg",
        "image/gif" => "gif",
        "image/webp" => "webp",
        other => {
            return Err(format!(
                "image type '{other}' cannot be sent to Amazon Bedrock Converse (png, jpeg, gif \
                 and webp can). The request was not sent."
            ));
        }
    };
    let data: String = data.chars().filter(|c| !c.is_whitespace()).collect();
    if data.is_empty()
        || base64::Engine::decode(&base64::engine::general_purpose::STANDARD, &data).is_err()
    {
        return Err("an image data URI does not hold valid base64".to_string());
    }
    Ok(json!({"image": {"format": format, "source": {"bytes": data}}}))
}

/// Message content (a string, null, or an array of parts) as Converse
/// content blocks. `images` is false where Converse takes text only.
fn content_blocks(
    content: Option<&Value>,
    model: &LlmModel,
    features: Features,
    images: bool,
) -> Result<Vec<Value>, String> {
    let mut blocks = Vec::new();
    match content {
        None | Some(Value::Null) => {}
        Some(Value::String(s)) => {
            if !s.is_empty() {
                blocks.push(json!({"text": s}));
            }
        }
        Some(Value::Array(parts)) => {
            for part in parts {
                let kind = part.get("type").and_then(Value::as_str).unwrap_or("text");
                match kind {
                    "text" => {
                        let text = part.get("text").and_then(Value::as_str).unwrap_or_default();
                        if !text.is_empty() {
                            blocks.push(json!({"text": text}));
                        }
                    }
                    "image_url" if images => blocks.push(image_block(part, model, features)?),
                    other => {
                        return Err(format!(
                            "a content part of type '{other}' cannot be sent to Amazon Bedrock \
                             Converse here. The request was not sent."
                        ));
                    }
                }
            }
        }
        Some(other) => {
            return Err(format!(
                "message content must be a string or an array of parts, not {other}"
            ));
        }
    }
    Ok(blocks)
}

/// Append `blocks` as a message of `role`, merging into the last message
/// when it has the same role.
fn push_message(messages: &mut Vec<Value>, role: &str, blocks: Vec<Value>) {
    if blocks.is_empty() {
        return;
    }
    if let Some(last) = messages.last_mut()
        && last.get("role").and_then(Value::as_str) == Some(role)
        && let Some(content) = last.get_mut("content").and_then(Value::as_array_mut)
    {
        content.extend(blocks);
        return;
    }
    messages.push(json!({"role": role, "content": blocks}));
}

fn tool_config(
    obj: &Map<String, Value>,
    model: &LlmModel,
    features: Features,
    stream: bool,
) -> Result<Option<Value>, String> {
    let tools = match obj.get("tools").and_then(Value::as_array) {
        Some(t) if !t.is_empty() => t,
        _ => return Ok(None),
    };
    let choice = obj.get("tool_choice");
    // `none`: no tool may be called. Converse has no such choice; the tools
    // are not offered (the Anthropic adapter's rule).
    if choice.and_then(Value::as_str) == Some("none") {
        return Ok(None);
    }
    if !features.tools {
        return Err(refusal(model, "tools"));
    }
    if stream && !features.streaming_tools {
        return Err(refusal(model, "tools with streaming"));
    }
    let mut specs = Vec::with_capacity(tools.len());
    for tool in tools {
        let kind = tool
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or("function");
        let function = tool.get("function").filter(|_| kind == "function");
        let Some(function) = function else {
            return Err(format!(
                "a tool of type '{kind}' cannot be sent to Amazon Bedrock Converse (function \
                 tools can). The request was not sent."
            ));
        };
        let name = function
            .get("name")
            .and_then(Value::as_str)
            .filter(|n| !n.is_empty())
            .ok_or("a function tool needs a name")?;
        let mut spec = Map::new();
        spec.insert("name".into(), json!(name));
        if let Some(description) = function
            .get("description")
            .and_then(Value::as_str)
            .filter(|d| !d.is_empty())
        {
            spec.insert("description".into(), json!(description));
        }
        let schema = match function.get("parameters") {
            Some(p) if p.is_object() => p.clone(),
            _ => json!({"type": "object", "properties": {}}),
        };
        spec.insert("inputSchema".into(), json!({"json": schema}));
        specs.push(json!({"toolSpec": spec}));
    }
    let mut config = Map::new();
    config.insert("tools".into(), Value::Array(specs));
    let forced = |what: &str| -> Result<(), String> {
        if features.forced_tool_choice {
            Ok(())
        } else {
            Err(refusal(model, &format!("tool_choice {what}")))
        }
    };
    match choice {
        None | Some(Value::Null) => {}
        Some(Value::String(s)) if s == "auto" => {
            config.insert("toolChoice".into(), json!({"auto": {}}));
        }
        Some(Value::String(s)) if s == "required" => {
            forced("'required'")?;
            config.insert("toolChoice".into(), json!({"any": {}}));
        }
        Some(Value::Object(o)) => {
            let name = o
                .get("function")
                .and_then(|f| f.get("name"))
                .and_then(Value::as_str)
                .ok_or("tool_choice names no function")?;
            forced("naming a function")?;
            config.insert("toolChoice".into(), json!({"tool": {"name": name}}));
        }
        Some(other) => return Err(format!("tool_choice {other} is not one the gateway knows")),
    }
    Ok(Some(Value::Object(config)))
}

/// The Converse request body for an OpenAI chat completion body. `Err` is
/// the reason the request cannot be served by this model: the caller's to
/// fix, and nothing is sent.
pub fn to_converse(body: &Value, model: &LlmModel, stream: bool) -> Result<Value, String> {
    let features = super::features(model);
    let obj = body
        .as_object()
        .ok_or("the request body is not an object")?;
    let mut system: Vec<Value> = Vec::new();
    let mut messages: Vec<Value> = Vec::new();

    for message in obj
        .get("messages")
        .and_then(Value::as_array)
        .ok_or("the request has no messages")?
    {
        let role = message
            .get("role")
            .and_then(Value::as_str)
            .unwrap_or("user");
        let content = message.get("content");
        match role {
            "system" | "developer" => {
                system.extend(content_blocks(content, model, features, false)?);
            }
            "user" => {
                push_message(
                    &mut messages,
                    "user",
                    content_blocks(content, model, features, true)?,
                );
            }
            "assistant" => {
                let mut blocks = content_blocks(content, model, features, false)?;
                for call in message
                    .get("tool_calls")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                {
                    if !features.tools {
                        return Err(refusal(model, "tools (a tool call in the conversation)"));
                    }
                    let function = call.get("function").ok_or("a tool call has no function")?;
                    let arguments = function
                        .get("arguments")
                        .and_then(Value::as_str)
                        .unwrap_or("{}");
                    let input: Value = if arguments.trim().is_empty() {
                        json!({})
                    } else {
                        serde_json::from_str(arguments).map_err(|_| {
                            "the arguments of a tool call in the conversation are not JSON"
                                .to_string()
                        })?
                    };
                    blocks.push(json!({"toolUse": {
                        "toolUseId": call.get("id").and_then(Value::as_str).unwrap_or_default(),
                        "name": function.get("name").and_then(Value::as_str).unwrap_or_default(),
                        "input": input,
                    }}));
                }
                push_message(&mut messages, "assistant", blocks);
            }
            "tool" => {
                if !features.tools {
                    return Err(refusal(model, "tools (a tool result in the conversation)"));
                }
                let mut result = content_blocks(content, model, features, false)?;
                if result.is_empty() {
                    result.push(json!({"text": ""}));
                }
                push_message(
                    &mut messages,
                    "user",
                    vec![json!({"toolResult": {
                        "toolUseId": message
                            .get("tool_call_id")
                            .and_then(Value::as_str)
                            .unwrap_or_default(),
                        "content": result,
                    }})],
                );
            }
            other => {
                return Err(format!(
                    "a message of role '{other}' cannot be sent to Amazon Bedrock Converse. The \
                     request was not sent."
                ));
            }
        }
    }
    if messages.is_empty() {
        return Err("the request has no user or assistant message".to_string());
    }

    let mut out = Map::new();
    if !system.is_empty() {
        out.insert("system".into(), Value::Array(system));
    }
    out.insert("messages".into(), Value::Array(messages));
    if let Some(config) = tool_config(obj, model, features, stream)? {
        out.insert("toolConfig".into(), config);
    }

    let mut inference = Map::new();
    if let Some(max) = obj
        .get("max_tokens")
        .or_else(|| obj.get("max_completion_tokens"))
        .filter(|v| v.is_number())
    {
        inference.insert("maxTokens".into(), max.clone());
    }
    for (from, to) in [("temperature", "temperature"), ("top_p", "topP")] {
        if let Some(v) = obj.get(from).filter(|v| v.is_number()) {
            inference.insert(to.into(), v.clone());
        }
    }
    match obj.get("stop") {
        Some(Value::String(s)) if !s.is_empty() => {
            inference.insert("stopSequences".into(), json!([s]));
        }
        Some(Value::Array(a)) if !a.is_empty() => {
            inference.insert("stopSequences".into(), Value::Array(a.clone()));
        }
        _ => {}
    }
    if !inference.is_empty() {
        out.insert("inferenceConfig".into(), Value::Object(inference));
    }

    // A JSON-schema `response_format`, where the model has structured
    // output; otherwise it has no Converse field and is not sent (as the
    // Anthropic adapter does for a row without it).
    if features.structured_output
        && let Some(format) = obj.get("response_format")
        && format.get("type").and_then(Value::as_str) == Some("json_schema")
        && let Some(spec) = format.get("json_schema")
        && let Some(schema) = spec.get("schema")
    {
        let mut definition = Map::new();
        definition.insert("schema".into(), json!(schema.to_string()));
        for key in ["name", "description"] {
            if let Some(v) = spec.get(key).and_then(Value::as_str) {
                definition.insert(key.into(), json!(v));
            }
        }
        out.insert(
            "outputConfig".into(),
            json!({"textFormat": {"type": "json_schema",
                                  "structure": {"jsonSchema": definition}}}),
        );
    }
    Ok(Value::Object(out))
}

// ---------------------------------------------------------------------------
// Response
// ---------------------------------------------------------------------------

/// `stopReason` as `finish_reason`; `None` for a reason that is not an
/// answer (`malformed_model_output`, `malformed_tool_use`, or one the
/// gateway does not know).
fn finish_reason(stop_reason: &str) -> Option<&'static str> {
    match stop_reason {
        "end_turn" | "stop_sequence" => Some("stop"),
        "max_tokens" | "model_context_window_exceeded" => Some("length"),
        "tool_use" => Some("tool_calls"),
        "guardrail_intervened" | "content_filtered" => Some("content_filter"),
        _ => None,
    }
}

/// Converse `usage` as OpenAI `usage`. The three counts are required by
/// the API; a reply without them has no usage to record and is an error,
/// not a zero.
fn usage(usage: Option<&Value>) -> Result<Value, String> {
    let count = |key: &str| usage.and_then(|u| u.get(key)).and_then(Value::as_i64);
    let (Some(input), Some(output)) = (count("inputTokens"), count("outputTokens")) else {
        return Err("Amazon Bedrock's reply carries no token usage".to_string());
    };
    let mut out = Map::new();
    out.insert("prompt_tokens".into(), json!(input));
    out.insert("completion_tokens".into(), json!(output));
    out.insert(
        "total_tokens".into(),
        json!(count("totalTokens").unwrap_or(input + output)),
    );
    // The names the gateway's cache accounting reads (the Anthropic
    // adapter's).
    for (from, to) in [
        ("cacheReadInputTokens", "cache_read_input_tokens"),
        ("cacheWriteInputTokens", "cache_creation_input_tokens"),
    ] {
        if let Some(n) = count(from).filter(|n| *n > 0) {
            out.insert(to.into(), json!(n));
        }
    }
    Ok(Value::Object(out))
}

fn completion_id() -> String {
    format!("chatcmpl-{}", uuid::Uuid::new_v4().simple())
}

/// A `Converse` response as an OpenAI chat completion. `Err` for a body
/// that is not a Converse answer.
pub fn response_to_openai(response: &Value, model: &LlmModel) -> Result<Value, String> {
    let blocks = response
        .pointer("/output/message/content")
        .and_then(Value::as_array)
        .ok_or("Amazon Bedrock's reply is not a Converse response (no output.message)")?;
    let stop_reason = response
        .get("stopReason")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let finish = finish_reason(stop_reason).ok_or_else(|| {
        format!("Amazon Bedrock stopped with '{stop_reason}': the model gave no usable answer")
    })?;
    let mut text = String::new();
    let mut tool_calls = Vec::new();
    for block in blocks {
        if let Some(t) = block.get("text").and_then(Value::as_str) {
            text.push_str(t);
        } else if let Some(tool) = block.get("toolUse") {
            tool_calls.push(json!({
                "id": tool.get("toolUseId").and_then(Value::as_str).unwrap_or_default(),
                "type": "function",
                "function": {
                    "name": tool.get("name").and_then(Value::as_str).unwrap_or_default(),
                    "arguments": tool.get("input").unwrap_or(&json!({})).to_string(),
                },
            }));
        }
    }
    let mut message = Map::new();
    message.insert("role".into(), json!("assistant"));
    message.insert(
        "content".into(),
        if text.is_empty() && !tool_calls.is_empty() {
            Value::Null
        } else {
            json!(text)
        },
    );
    if !tool_calls.is_empty() {
        message.insert("tool_calls".into(), Value::Array(tool_calls));
    }
    Ok(json!({
        "id": completion_id(),
        "object": "chat.completion",
        "created": chrono::Utc::now().timestamp(),
        "model": model.model_name,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": usage(response.get("usage"))?,
    }))
}

// ---------------------------------------------------------------------------
// Stream
// ---------------------------------------------------------------------------

/// `ConverseStream` events in, OpenAI chat completion chunks out (SSE
/// `data` payloads). The stream ends with `[DONE]` only when Bedrock ended
/// the message (`messageStop`); an exception event becomes an `error`
/// payload and nothing follows it.
pub struct StreamTranslator {
    id: String,
    created: i64,
    model: String,
    /// The OpenAI tool-call index of each tool-use content block.
    tool_index: Vec<(i64, usize)>,
    stopped: bool,
    failed: bool,
}

impl StreamTranslator {
    pub fn new(model_name: &str) -> Self {
        Self {
            id: completion_id(),
            created: chrono::Utc::now().timestamp(),
            model: model_name.to_string(),
            tool_index: Vec::new(),
            stopped: false,
            failed: false,
        }
    }

    /// True once an error payload was produced: nothing more is.
    pub fn failed(&self) -> bool {
        self.failed
    }

    fn chunk(&self, delta: Value, finish: Option<&str>) -> String {
        json!({
            "id": self.id, "object": "chat.completion.chunk", "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        })
        .to_string()
    }

    fn error(&mut self, kind: &str, message: &str) -> Vec<String> {
        self.failed = true;
        vec![
            json!({"error": {
                "message": format!("Amazon Bedrock ({kind}): {message}"),
                "type": "upstream_error",
                "code": kind,
            }})
            .to_string(),
        ]
    }

    /// The stream broke in a way that is not an event: bytes that are not
    /// an event stream.
    pub fn malformed(&mut self) -> Vec<String> {
        if self.failed {
            return Vec::new();
        }
        self.error(
            "malformed_stream",
            "the response is not an event stream the gateway can read",
        )
    }

    /// One event-stream message.
    pub fn on_message(
        &mut self,
        message_type: Option<&str>,
        event_type: Option<&str>,
        exception_type: Option<&str>,
        payload: &[u8],
    ) -> Vec<String> {
        if self.failed {
            return Vec::new();
        }
        let payload: Value = serde_json::from_slice(payload).unwrap_or(Value::Null);
        if message_type.is_some_and(|t| t != "event") {
            let kind = exception_type.or(event_type).unwrap_or("exception");
            let message = ["message", "Message"]
                .iter()
                .find_map(|k| payload.get(k).and_then(Value::as_str))
                .unwrap_or("the stream ended with an exception")
                .to_string();
            return self.error(kind, &message);
        }
        let Some(event) = event_type else {
            return Vec::new();
        };
        // The event's own fields, whether or not they arrive wrapped in a
        // key of the event's name.
        let payload = match payload.get(event) {
            Some(inner) if inner.is_object() => inner,
            _ => &payload,
        };
        let block = payload.get("contentBlockIndex").and_then(Value::as_i64);
        match event {
            "messageStart" => vec![self.chunk(json!({"role": "assistant", "content": ""}), None)],
            "contentBlockStart" => {
                let Some(tool) = payload.pointer("/start/toolUse") else {
                    return Vec::new();
                };
                let index = self.tool_index.len();
                self.tool_index.push((block.unwrap_or(-1), index));
                vec![self.chunk(
                    json!({"tool_calls": [{
                        "index": index,
                        "id": tool.get("toolUseId").and_then(Value::as_str).unwrap_or_default(),
                        "type": "function",
                        "function": {
                            "name": tool.get("name").and_then(Value::as_str).unwrap_or_default(),
                            "arguments": "",
                        },
                    }]}),
                    None,
                )]
            }
            "contentBlockDelta" => {
                let delta = payload.get("delta");
                if let Some(text) = delta.and_then(|d| d.get("text")).and_then(Value::as_str) {
                    return vec![self.chunk(json!({"content": text}), None)];
                }
                if let Some(input) = delta
                    .and_then(|d| d.pointer("/toolUse/input"))
                    .and_then(Value::as_str)
                {
                    let index = self
                        .tool_index
                        .iter()
                        .find(|(b, _)| Some(*b) == block)
                        .map(|(_, i)| *i);
                    let Some(index) = index else {
                        return self.error(
                            "malformed_stream",
                            "a tool-call fragment for a tool call that was not started",
                        );
                    };
                    return vec![self.chunk(
                        json!({"tool_calls": [{"index": index,
                                               "function": {"arguments": input}}]}),
                        None,
                    )];
                }
                // `reasoningContent` and the block kinds the gateway does
                // not translate: nothing.
                Vec::new()
            }
            "messageStop" => {
                let reason = payload
                    .get("stopReason")
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                match finish_reason(reason) {
                    Some(finish) => {
                        self.stopped = true;
                        vec![self.chunk(json!({}), Some(finish))]
                    }
                    None => {
                        let reason = reason.to_string();
                        self.error(&reason, "the model gave no usable answer")
                    }
                }
            }
            "metadata" => match usage(payload.get("usage")) {
                Ok(usage) => vec![
                    json!({
                        "id": self.id, "object": "chat.completion.chunk",
                        "created": self.created, "model": self.model,
                        "choices": [], "usage": usage,
                    })
                    .to_string(),
                ],
                Err(reason) => self.error("malformed_stream", &reason),
            },
            _ => Vec::new(),
        }
    }

    /// The upstream closed: `[DONE]` when the message was ended, nothing
    /// for a stream that broke off.
    pub fn finish(&mut self) -> Vec<String> {
        if self.stopped && !self.failed {
            self.stopped = false;
            vec!["[DONE]".to_string()]
        } else {
            Vec::new()
        }
    }
}

/// Bytes of a `ConverseStream` response in, SSE text out.
struct SseAdapter {
    buffer: Vec<u8>,
    translator: StreamTranslator,
}

impl SseAdapter {
    fn sse(payloads: Vec<String>) -> String {
        payloads
            .into_iter()
            .map(|p| format!("data: {p}\n\n"))
            .collect()
    }

    fn push(&mut self, bytes: &[u8]) -> String {
        self.buffer.extend_from_slice(bytes);
        let mut out = Vec::new();
        loop {
            match next_event_stream_message(&self.buffer) {
                EventStreamStep::Incomplete => break,
                EventStreamStep::Malformed => {
                    self.buffer.clear();
                    out.extend(self.translator.malformed());
                    break;
                }
                EventStreamStep::Message(message, consumed) => {
                    self.buffer.drain(..consumed);
                    out.extend(self.translator.on_message(
                        message.message_type.as_deref(),
                        message.event_type.as_deref(),
                        message.exception_type.as_deref(),
                        &message.payload,
                    ));
                }
            }
        }
        Self::sse(out)
    }
}

/// A successful `ConverseStream` response as the response of an
/// OpenAI-compatible provider: the same status, an SSE body of chat
/// completion chunks. The chat route reads it as it reads any provider's
/// stream.
pub fn openai_sse_response(upstream: reqwest::Response, model: &LlmModel) -> reqwest::Response {
    let status = upstream.status();
    let adapter = SseAdapter {
        buffer: Vec::with_capacity(8192),
        translator: StreamTranslator::new(&model.model_name),
    };
    let bytes = Box::pin(upstream.bytes_stream());
    let body = futures::stream::unfold(
        (bytes, adapter, false),
        |(mut bytes, mut adapter, ended)| async move {
            if ended {
                return None;
            }
            loop {
                match bytes.next().await {
                    Some(Ok(chunk)) => {
                        let text = adapter.push(&chunk);
                        let failed = adapter.translator.failed();
                        if !text.is_empty() || failed {
                            return Some((Ok(text), (bytes, adapter, failed)));
                        }
                    }
                    Some(Err(e)) => return Some((Err(e), (bytes, adapter, true))),
                    None => {
                        // Bytes left over are a message that never
                        // completed: the stream broke off.
                        let tail = if adapter.buffer.is_empty() {
                            adapter.translator.finish()
                        } else {
                            adapter.translator.malformed()
                        };
                        return Some((Ok(SseAdapter::sse(tail)), (bytes, adapter, true)));
                    }
                }
            }
        },
    );
    let mut response = axum::http::Response::new(reqwest::Body::wrap_stream(body));
    *response.status_mut() = status;
    response.headers_mut().insert(
        axum::http::header::CONTENT_TYPE,
        axum::http::HeaderValue::from_static("text/event-stream"),
    );
    reqwest::Response::from(response)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::llm_adapter::make_test_model;

    fn nova() -> LlmModel {
        make_test_model("bedrock", "bedrock/amazon.nova-pro-v1:0")
    }

    fn user(text: &str) -> Value {
        json!({"messages": [{"role": "user", "content": text}]})
    }

    #[test]
    fn a_plain_request_is_messages_and_nothing_else() {
        assert_eq!(
            to_converse(&user("hi"), &nova(), false).unwrap(),
            json!({"messages": [{"role": "user", "content": [{"text": "hi"}]}]})
        );
    }

    #[test]
    fn parameters_with_no_converse_field_are_not_sent() {
        let mut body = user("hi");
        for (k, v) in [
            ("model", json!("bedrock/amazon.nova-pro-v1:0")),
            ("stream", json!(true)),
            ("stream_options", json!({"include_usage": true})),
            ("n", json!(2)),
            ("seed", json!(1)),
            ("presence_penalty", json!(1)),
            ("frequency_penalty", json!(1)),
            ("logprobs", json!(true)),
            ("top_logprobs", json!(3)),
            ("logit_bias", json!({"1": 1})),
            ("user", json!("u")),
            ("parallel_tool_calls", json!(false)),
            ("reasoning_effort", json!("high")),
            (
                "thinking",
                json!({"type": "enabled", "budget_tokens": 1024}),
            ),
            ("response_format", json!({"type": "json_object"})),
            ("top_k", json!(5)),
        ] {
            body[k] = v;
        }
        assert_eq!(
            to_converse(&body, &nova(), true).unwrap(),
            json!({"messages": [{"role": "user", "content": [{"text": "hi"}]}]})
        );
    }

    #[test]
    fn consecutive_tool_results_are_one_user_message() {
        let body = json!({"messages": [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "ok", "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "f", "arguments": "{}"}},
                {"id": "b", "type": "function", "function": {"name": "g", "arguments": ""}}
            ]},
            {"role": "tool", "tool_call_id": "a", "content": "1"},
            {"role": "tool", "tool_call_id": "b", "content": [{"type": "text", "text": "2"}]}
        ]});
        let out = to_converse(&body, &nova(), false).unwrap();
        assert_eq!(
            out["messages"],
            json!([
                {"role": "user", "content": [{"text": "go"}]},
                {"role": "assistant", "content": [
                    {"text": "ok"},
                    {"toolUse": {"toolUseId": "a", "name": "f", "input": {}}},
                    {"toolUse": {"toolUseId": "b", "name": "g", "input": {}}}
                ]},
                {"role": "user", "content": [
                    {"toolResult": {"toolUseId": "a", "content": [{"text": "1"}]}},
                    {"toolResult": {"toolUseId": "b", "content": [{"text": "2"}]}}
                ]}
            ])
        );
    }

    #[test]
    fn tool_choice_none_offers_no_tools_and_required_is_any() {
        let mut body = user("hi");
        body["tools"] = json!([{"type": "function", "function": {"name": "f"}}]);
        body["tool_choice"] = json!("none");
        assert!(
            to_converse(&body, &nova(), false)
                .unwrap()
                .get("toolConfig")
                .is_none()
        );
        body["tool_choice"] = json!("required");
        assert_eq!(
            to_converse(&body, &nova(), false).unwrap()["toolConfig"]["toolChoice"],
            json!({"any": {}})
        );
    }

    #[test]
    fn what_a_model_does_not_support_is_refused_by_name() {
        let deepseek = make_test_model("bedrock", "bedrock/deepseek.v3.2");
        let mut tools = user("hi");
        tools["tools"] = json!([{"type": "function", "function": {"name": "f"}}]);
        assert!(
            to_converse(&tools, &deepseek, false)
                .unwrap_err()
                .contains("tools")
        );
        let history = json!({"messages": [
            {"role": "user", "content": "x"},
            {"role": "assistant", "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "a", "content": "1"}
        ]});
        assert!(
            to_converse(&history, &deepseek, false)
                .unwrap_err()
                .contains("tools")
        );
        // A system prompt is a field of every Converse request.
        let system = json!({"messages": [{"role": "system", "content": "s"},
                                          {"role": "user", "content": "x"}]});
        assert_eq!(
            to_converse(&system, &deepseek, false).unwrap()["system"],
            json!([{"text": "s"}])
        );
        // An unknown member of a family has no feature beyond text.
        let future = make_test_model("bedrock", "bedrock/amazon.nova-9-v1:0");
        assert!(to_converse(&tools, &future, false).is_err());
        for bad in [
            json!({"messages": [{"role": "function", "name": "f", "content": "x"}]}),
            json!({"messages": []}),
            json!({"messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/bmp;base64,AAAA"}}]}]}),
            json!({"messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,***"}}]}]}),
            json!({"messages": [{"role": "user", "content": "x"}],
                   "tools": [{"type": "web_search"}]}),
        ] {
            assert!(to_converse(&bad, &nova(), false).is_err(), "{bad}");
        }
    }

    #[test]
    fn a_json_schema_is_output_config_where_the_model_has_it() {
        let mut body = user("hi");
        body["response_format"] = json!({"type": "json_schema", "json_schema": {
            "name": "verdict", "schema": {"type": "object"}}});
        let mistral = make_test_model("bedrock", "bedrock/mistral.mistral-large-3-675b-instruct");
        assert_eq!(
            to_converse(&body, &mistral, false).unwrap()["outputConfig"],
            json!({"textFormat": {"type": "json_schema", "structure": {"jsonSchema": {
                "schema": "{\"type\":\"object\"}", "name": "verdict"}}}})
        );
        assert!(
            to_converse(&body, &nova(), false)
                .unwrap()
                .get("outputConfig")
                .is_none()
        );
    }

    #[test]
    fn a_reply_without_usage_or_an_answer_is_an_error() {
        let ok = json!({"output": {"message": {"role": "assistant", "content": [{"text": "x"}]}},
                        "stopReason": "end_turn",
                        "usage": {"inputTokens": 1, "outputTokens": 2, "totalTokens": 3}});
        assert!(response_to_openai(&ok, &nova()).is_ok());
        let mut no_usage = ok.clone();
        no_usage.as_object_mut().unwrap().remove("usage");
        assert!(response_to_openai(&no_usage, &nova()).is_err());
        let mut malformed = ok.clone();
        malformed["stopReason"] = json!("malformed_tool_use");
        assert!(response_to_openai(&malformed, &nova()).is_err());
        assert!(response_to_openai(&json!({"choices": []}), &nova()).is_err());
        // Reasoning is not returned.
        let reasoning = json!({"output": {"message": {"role": "assistant", "content": [
            {"reasoningContent": {"reasoningText": {"text": "secret thoughts"}}},
            {"text": "answer"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 1, "outputTokens": 2, "totalTokens": 3}});
        let out = response_to_openai(&reasoning, &nova()).unwrap();
        assert!(!out.to_string().contains("secret thoughts"));
        assert_eq!(out["choices"][0]["message"]["content"], json!("answer"));
    }

    fn event(t: &mut StreamTranslator, kind: &str, payload: Value) -> Vec<String> {
        t.on_message(
            Some("event"),
            Some(kind),
            None,
            payload.to_string().as_bytes(),
        )
    }

    #[test]
    fn a_stream_translates_wrapped_and_bare_events_alike() {
        for wrapped in [false, true] {
            let mut t = StreamTranslator::new("m");
            let wrap = |kind: &str, v: Value| if wrapped { json!({kind: v}) } else { v };
            let mut out = Vec::new();
            for (kind, payload) in [
                ("messageStart", json!({"role": "assistant"})),
                (
                    "contentBlockDelta",
                    json!({"contentBlockIndex": 0, "delta": {"text": "hi"}}),
                ),
                ("contentBlockStop", json!({"contentBlockIndex": 0})),
                ("messageStop", json!({"stopReason": "end_turn"})),
                (
                    "metadata",
                    json!({"usage": {"inputTokens": 3, "outputTokens": 1,
                                              "totalTokens": 4}}),
                ),
            ] {
                out.extend(event(&mut t, kind, wrap(kind, payload)));
            }
            out.extend(t.finish());
            let text = out.join("\n");
            assert!(text.contains("\"content\":\"hi\""), "{text}");
            assert!(text.contains("\"finish_reason\":\"stop\""), "{text}");
            assert!(text.contains("\"prompt_tokens\":3"), "{text}");
            assert_eq!(out.last().map(String::as_str), Some("[DONE]"));
        }
    }

    #[test]
    fn a_stream_that_breaks_off_is_not_done() {
        let mut t = StreamTranslator::new("m");
        event(&mut t, "messageStart", json!({"role": "assistant"}));
        event(
            &mut t,
            "contentBlockDelta",
            json!({"contentBlockIndex": 0, "delta": {"text": "a"}}),
        );
        assert!(t.finish().is_empty());

        let mut t = StreamTranslator::new("m");
        let out = t.on_message(
            Some("exception"),
            None,
            Some("throttlingException"),
            br#"{"message":"slow down"}"#,
        );
        assert!(out[0].contains("throttlingException") && out[0].contains("slow down"));
        assert!(t.failed());
        assert!(event(&mut t, "messageStop", json!({"stopReason": "end_turn"})).is_empty());
        assert!(t.finish().is_empty());

        // Metadata without usage is not a zero.
        let mut t = StreamTranslator::new("m");
        let out = event(&mut t, "metadata", json!({"metrics": {"latencyMs": 1}}));
        assert!(out[0].contains("error"), "{out:?}");
    }

    #[test]
    fn bytes_that_are_not_an_event_stream_end_in_an_error() {
        let mut a = SseAdapter {
            buffer: Vec::new(),
            translator: StreamTranslator::new("m"),
        };
        // A total length of 3: no message is that short.
        let text = a.push(&[0, 0, 0, 3, 0, 0, 0, 0, 0, 0, 0, 0, 1, 2, 3]);
        assert!(text.contains("malformed_stream"), "{text}");
        assert!(a.translator.failed());
        // A headers length beyond the message.
        let mut a = SseAdapter {
            buffer: Vec::new(),
            translator: StreamTranslator::new("m"),
        };
        let text = a.push(&[
            0, 0, 0, 20, 0, 0, 0, 200, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        ]);
        assert!(text.contains("malformed_stream"), "{text}");
        // HTML, as a proxy in the way would answer.
        let mut a = SseAdapter {
            buffer: Vec::new(),
            translator: StreamTranslator::new("m"),
        };
        let text = a.push(b"<html><body>502 Bad Gateway</body></html>");
        assert!(text.contains("malformed_stream"), "{text}");
    }
}
