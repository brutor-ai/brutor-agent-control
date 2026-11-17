// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Anthropic Messages API Routes
//!
//! Provides Anthropic-native `/v1/messages` and `/v1/messages/count_tokens` endpoints
//! so that clients like Claude Code can connect directly via `ANTHROPIC_BASE_URL`.
//!
//! Internally these endpoints translate Anthropic ↔ OpenAI format and delegate
//! to the same chat-completion pipeline used by `/v1/proxy/llm/chat/completions`.

use std::time::Instant;

use axum::Router;
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::response::{IntoResponse, Json, Response};
use axum::routing::post;
use futures::stream::StreamExt;
use serde_json::{Value, json};
use tokio_stream::wrappers::ReceiverStream;

use crate::middleware::tenant::TenantContext;
use crate::routes::route_helpers;
use crate::services::call_chain::{derive_tenant_key, parse_or_mint};
use crate::services::llm_preflight::PreflightOptions;
use crate::services::proxy_logger::{
    self, body_preview, extract_user_agent, populate_llm_request_fields,
};
use crate::services::{
    SharedState, auth, llm_preflight, llm_router, providers, proxy_metrics, usage,
};

/// Header getter for `parse_or_mint`'s closure contract. Mirrors the
/// `llm.rs` and `mcp.rs` helpers — see `routes/llm.rs:header_value`.
fn header_value(headers: &HeaderMap, name: &str) -> Option<String> {
    headers
        .get(name)
        .and_then(|v| v.to_str().ok())
        .map(String::from)
}

/// Safety ceiling for streaming responses (64 MB).
const MAX_STREAM_BYTES: usize = 64 * 1024 * 1024;

pub fn router() -> Router<SharedState> {
    // `/messages` is a completion (AI-generated label);
    // `count_tokens` is not.
    Router::new()
        .route(
            "/messages",
            post(messages).layer(axum::middleware::from_fn(
                crate::services::evidence::notice::mark_ai_generated,
            )),
        )
        .route("/messages/count_tokens", post(count_tokens))
}

// ---------------------------------------------------------------------------
// Anthropic → OpenAI request translation
// ---------------------------------------------------------------------------

/// Convert an Anthropic Messages API request body into an OpenAI ChatCompletionRequest body.
fn anthropic_to_openai_request(body: &Value) -> Value {
    let obj = body.as_object();

    // messages — prepend system message if present
    let mut messages: Vec<Value> = Vec::new();

    if let Some(system) = obj.and_then(|o| o.get("system")) {
        match system {
            Value::String(s) => {
                messages.push(json!({"role": "system", "content": s}));
            }
            Value::Array(parts) => {
                // Anthropic allows system as array of content blocks
                let text: String = parts
                    .iter()
                    .filter_map(|p| p.get("text").and_then(|t| t.as_str()))
                    .collect::<Vec<_>>()
                    .join("\n");
                if !text.is_empty() {
                    messages.push(json!({"role": "system", "content": text}));
                }
            }
            _ => {}
        }
    }

    // Convert each Anthropic message to OpenAI format
    if let Some(msgs) = obj
        .and_then(|o| o.get("messages"))
        .and_then(|m| m.as_array())
    {
        for msg in msgs {
            let role = msg.get("role").and_then(|r| r.as_str()).unwrap_or("user");
            let content = msg.get("content");

            match content {
                Some(Value::String(s)) => {
                    let mut m = json!({"role": role, "content": s});
                    if let Some(tc_id) = msg.get("tool_use_id").and_then(|v| v.as_str()) {
                        // This is a tool result message
                        m["role"] = json!("tool");
                        m["tool_call_id"] = json!(tc_id);
                    }
                    messages.push(m);
                }
                Some(Value::Array(blocks)) => {
                    // Anthropic content blocks → OpenAI format
                    let mut text_parts: Vec<String> = Vec::new();
                    let mut tool_calls: Vec<Value> = Vec::new();
                    let mut tool_results: Vec<Value> = Vec::new();

                    for block in blocks {
                        let block_type = block.get("type").and_then(|t| t.as_str()).unwrap_or("");
                        match block_type {
                            "text" => {
                                if let Some(t) = block.get("text").and_then(|t| t.as_str()) {
                                    text_parts.push(t.to_string());
                                }
                            }
                            "tool_use" => {
                                let tc = json!({
                                    "id": block.get("id").cloned().unwrap_or(json!("")),
                                    "type": "function",
                                    "function": {
                                        "name": block.get("name").cloned().unwrap_or(json!("")),
                                        "arguments": block.get("input")
                                            .map(|v| serde_json::to_string(v).unwrap_or_default())
                                            .unwrap_or_default(),
                                    }
                                });
                                tool_calls.push(tc);
                            }
                            "tool_result" => {
                                let content_val = block.get("content");
                                let text = match content_val {
                                    Some(Value::String(s)) => s.clone(),
                                    Some(Value::Array(arr)) => arr
                                        .iter()
                                        .filter_map(|p| p.get("text").and_then(|t| t.as_str()))
                                        .collect::<Vec<_>>()
                                        .join("\n"),
                                    _ => String::new(),
                                };
                                tool_results.push(json!({
                                    "role": "tool",
                                    "tool_call_id": block.get("tool_use_id").cloned().unwrap_or(json!("")),
                                    "content": text,
                                }));
                            }
                            "image" => {
                                // Pass image blocks as-is in content array for multimodal
                                if let Some(source) = block.get("source") {
                                    text_parts.push(format!(
                                        "[image: {}]",
                                        source
                                            .get("type")
                                            .and_then(|t| t.as_str())
                                            .unwrap_or("base64")
                                    ));
                                }
                            }
                            _ => {}
                        }
                    }

                    // Build the OpenAI message(s)
                    if !tool_results.is_empty() {
                        // Tool result blocks become separate tool messages
                        for tr in tool_results {
                            messages.push(tr);
                        }
                    }

                    if !text_parts.is_empty() || !tool_calls.is_empty() {
                        let mut m = json!({"role": role});
                        if !text_parts.is_empty() {
                            m["content"] = json!(text_parts.join("\n"));
                        }
                        if !tool_calls.is_empty() {
                            m["tool_calls"] = json!(tool_calls);
                        }
                        messages.push(m);
                    }
                }
                _ => {
                    messages.push(json!({"role": role, "content": null}));
                }
            }
        }
    }

    // Build OpenAI request
    let mut openai = json!({
        "messages": messages,
        "stream": obj.and_then(|o| o.get("stream")).and_then(|v| v.as_bool()).unwrap_or(false),
    });
    let out = openai.as_object_mut().unwrap();

    // model
    if let Some(model) = obj.and_then(|o| o.get("model")) {
        out.insert("model".into(), model.clone());
    }

    // max_tokens
    if let Some(mt) = obj.and_then(|o| o.get("max_tokens")) {
        out.insert("max_tokens".into(), mt.clone());
    }

    // temperature
    if let Some(t) = obj.and_then(|o| o.get("temperature")) {
        out.insert("temperature".into(), t.clone());
    }

    // top_p
    if let Some(tp) = obj.and_then(|o| o.get("top_p")) {
        out.insert("top_p".into(), tp.clone());
    }

    // stop_sequences → stop
    if let Some(ss) = obj.and_then(|o| o.get("stop_sequences")) {
        out.insert("stop".into(), ss.clone());
    }

    // tools — Anthropic format → OpenAI format
    if let Some(tools) = obj.and_then(|o| o.get("tools")).and_then(|t| t.as_array()) {
        let openai_tools: Vec<Value> = tools
            .iter()
            .map(|tool| {
                json!({
                    "type": "function",
                    "function": {
                        "name": tool.get("name").cloned().unwrap_or(json!("")),
                        "description": tool.get("description").cloned().unwrap_or(json!("")),
                        "parameters": tool.get("input_schema").cloned().unwrap_or(json!({})),
                    }
                })
            })
            .collect();
        out.insert("tools".into(), json!(openai_tools));
    }

    // tool_choice — Anthropic format → OpenAI format
    if let Some(tc) = obj.and_then(|o| o.get("tool_choice")) {
        let tc_type = tc.get("type").and_then(|t| t.as_str()).unwrap_or("");
        let openai_tc = match tc_type {
            "auto" => json!("auto"),
            "any" => json!("required"),
            "tool" => {
                let name = tc.get("name").and_then(|n| n.as_str()).unwrap_or("");
                json!({"type": "function", "function": {"name": name}})
            }
            "none" => json!("none"),
            _ => json!("auto"),
        };
        out.insert("tool_choice".into(), openai_tc);
    }

    openai
}

// ---------------------------------------------------------------------------
// Governance chat-view adapters (G6 + G9)
// ---------------------------------------------------------------------------
//
// G6/G9 reuse the SAME helpers the chat path uses
// (`apply_governance`, `apply_response_governance`). Those operate on the
// chat-completion JSON shape, so these adapters build a chat-shaped *view*
// of the native Anthropic request/response, run the unchanged helper, then
// splice the governed result back into the native Anthropic shape. No
// helper is forked — only the text/param/tool-call extraction is adapted to
// the Anthropic Messages wire shape. Applied to the SHARED part of
// `messages()` so both the native and translated branches inherit them.

/// Build a chat-completion request *view* from a native Anthropic body so
/// `apply_governance` (RG param caps + tool filtering + banned-pattern +
/// mandatory system-prompt injection) can run unchanged. Reuses the
/// existing Anthropic→OpenAI request translation, then deserializes into
/// the typed `ChatCompletionRequest` the helper expects.
fn anthropic_request_to_chat_view(body: &Value) -> crate::models::proxy::ChatCompletionRequest {
    let openai = anthropic_to_openai_request(body);
    serde_json::from_value(openai).unwrap_or_else(|_| crate::models::proxy::ChatCompletionRequest {
        model: body.get("model").and_then(|m| m.as_str()).map(String::from),
        model_id: None,
        messages: Vec::new(),
        temperature: None,
        max_tokens: None,
        stream: None,
        top_p: None,
        n: None,
        stop: None,
        presence_penalty: None,
        frequency_penalty: None,
        tools: None,
        tool_choice: None,
        extra: serde_json::Map::new(),
    })
}

/// Splice the governed chat-view fields back into the native Anthropic
/// body after `apply_governance` mutated the view. Carries back: clamped
/// `temperature` / `top_p` / `max_tokens`, the filtered `tools` (translated
/// back to Anthropic `input_schema` shape), and the (possibly
/// mandatory-fragment-injected) system prompt → the native `system` slot.
fn apply_anthropic_chat_view_back(
    body: &mut Value,
    view: &crate::models::proxy::ChatCompletionRequest,
) {
    let Some(obj) = body.as_object_mut() else {
        return;
    };

    if let Some(t) = view.temperature {
        obj.insert("temperature".to_string(), json!(t));
    }
    if let Some(tp) = view.top_p {
        obj.insert("top_p".to_string(), json!(tp));
    }
    if let Some(mt) = view.max_tokens {
        obj.insert("max_tokens".to_string(), json!(mt));
    }

    // Tool filtering. The chat view holds OpenAI-shaped tools
    // (`function.{name,description,parameters}`); translate the filtered
    // set back to Anthropic shape (`{name,description,input_schema}`).
    // A `None` means governance stripped every tool.
    match &view.tools {
        Some(Value::Array(tools)) => {
            let anthropic_tools: Vec<Value> = tools
                .iter()
                .map(|t| {
                    let func = t.get("function");
                    json!({
                        "name": func.and_then(|f| f.get("name")).cloned().unwrap_or(json!("")),
                        "description": func
                            .and_then(|f| f.get("description"))
                            .cloned()
                            .unwrap_or(json!("")),
                        "input_schema": func
                            .and_then(|f| f.get("parameters"))
                            .cloned()
                            .unwrap_or(json!({})),
                    })
                })
                .collect();
            obj.insert("tools".to_string(), json!(anthropic_tools));
        }
        _ => {
            obj.remove("tools");
            obj.remove("tool_choice");
        }
    }

    // Mandatory system-prompt fragments were injected into the view's
    // leading system message → write the whole value back to the native
    // `system` slot (Anthropic's top-level system field).
    if let Some(sys) = view
        .messages
        .iter()
        .find(|m| m.role == "system")
        .and_then(|m| m.content.as_ref())
        .and_then(|c| c.as_str())
    {
        if !sys.is_empty() {
            obj.insert("system".to_string(), Value::String(sys.to_string()));
        }
    }
}

// ---------------------------------------------------------------------------
// OpenAI → Anthropic response translation
// ---------------------------------------------------------------------------

/// Convert an OpenAI chat completion response to Anthropic Messages format.
pub(crate) fn openai_to_anthropic_response(openai_resp: &Value) -> Value {
    let choice = openai_resp.get("choices").and_then(|c| c.get(0));
    let message = choice.and_then(|c| c.get("message"));

    // Build Anthropic content blocks
    let mut content: Vec<Value> = Vec::new();

    // Text content
    if let Some(text) = message
        .and_then(|m| m.get("content"))
        .and_then(|c| c.as_str())
    {
        if !text.is_empty() {
            content.push(json!({"type": "text", "text": text}));
        }
    }

    // Tool calls → tool_use blocks
    if let Some(tool_calls) = message
        .and_then(|m| m.get("tool_calls"))
        .and_then(|t| t.as_array())
    {
        for tc in tool_calls {
            let func = tc.get("function");
            let args_str = func
                .and_then(|f| f.get("arguments"))
                .and_then(|a| a.as_str())
                .unwrap_or("{}");
            let input: Value = serde_json::from_str(args_str).unwrap_or(json!({}));

            content.push(json!({
                "type": "tool_use",
                "id": tc.get("id").cloned().unwrap_or(json!("")),
                "name": func.and_then(|f| f.get("name")).cloned().unwrap_or(json!("")),
                "input": input,
            }));
        }
    }

    if content.is_empty() {
        content.push(json!({"type": "text", "text": ""}));
    }

    // Map finish_reason → stop_reason
    let finish_reason = choice
        .and_then(|c| c.get("finish_reason"))
        .and_then(|f| f.as_str())
        .unwrap_or("end_turn");
    let stop_reason = match finish_reason {
        "stop" => "end_turn",
        "length" => "max_tokens",
        "tool_calls" => "tool_use",
        "content_filter" => "end_turn",
        other => other,
    };

    // Map usage
    let usage = openai_resp.get("usage");
    let input_tokens = usage
        .and_then(|u| u.get("prompt_tokens"))
        .and_then(|v| v.as_i64())
        .unwrap_or(0);
    let output_tokens = usage
        .and_then(|u| u.get("completion_tokens"))
        .and_then(|v| v.as_i64())
        .unwrap_or(0);

    json!({
        "id": openai_resp.get("id").cloned().unwrap_or(json!("msg_proxy")),
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": openai_resp.get("model").cloned().unwrap_or(json!("")),
        "stop_reason": stop_reason,
        "stop_sequence": null,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
    })
}

/// Emit a [`StreamAction`](crate::services::stream_guard::StreamAction)
/// from the output guard onto the `/v1/messages` SSE channel. Converts
/// each post-guard (redacted) OpenAI frame to Anthropic events, and
/// accumulates the on-the-wire text into `wire_content` so the proxy log
/// mirrors exactly what the client received. Returns `true` when the
/// caller should stop forwarding (block emitted, or client disconnected).
async fn emit_anthropic_stream_action(
    tx: &tokio::sync::mpsc::Sender<Result<Event, axum::Error>>,
    action: crate::services::stream_guard::StreamAction,
    stream_state: &mut AnthropicStreamState,
    model_name: &str,
    total_chunks: &mut u32,
    guardrail_blocked: &mut bool,
    wire_content: &mut String,
) -> bool {
    use crate::services::stream_guard::StreamAction;
    match action {
        StreamAction::Block(reason) => {
            *guardrail_blocked = true;
            tracing::warn!(%reason, "Streaming output guardrail block (/v1/messages)");
            // Anthropic streaming error event — terminates the message.
            let err = serde_json::json!({
                "type": "error",
                "error": {
                    "type": "guardrail_block",
                    "message": "Output blocked by guardrail policy"
                }
            });
            let _ = tx
                .send(Ok(Event::default().event("error").data(err.to_string())))
                .await;
            true
        }
        StreamAction::Emit(payloads) => {
            for p in payloads {
                // Mirror the wire (redacted in sync mode) into the audit log.
                crate::services::stream_guard::append_chunk_loggable_text(&p, wire_content);
                for (event_type, event_data) in
                    openai_chunk_to_anthropic_events(&p, stream_state, model_name)
                {
                    if tx
                        .send(Ok(Event::default().event(event_type).data(event_data)))
                        .await
                        .is_err()
                    {
                        return true;
                    }
                    *total_chunks += 1;
                }
            }
            false
        }
    }
}

/// Streaming-translation state threaded across OpenAI→Anthropic chunk
/// conversions for a single response. The converter is otherwise stateless,
/// but Anthropic's content-block protocol requires remembering which blocks
/// we've opened so we can (a) emit exactly one `content_block_start` per tool
/// call carrying its `id`/`name`, (b) give each tool call a distinct block
/// index (text is block 0; tool blocks follow at 1, 2, …), and (c) close
/// every block we opened at finish.
#[derive(Default)]
pub(crate) struct AnthropicStreamState {
    /// True once `message_start` + the text `content_block_start` (index 0)
    /// have been emitted. (Replaces the old `is_first` flag, inverted.)
    started: bool,
    /// OpenAI `tool_calls[].index` → assigned Anthropic content-block index.
    /// Lets repeated argument deltas for one call target the same block, and
    /// records which blocks must be stopped at finish. BTreeMap for a
    /// deterministic stop order.
    tool_blocks: std::collections::BTreeMap<i64, i64>,
}

/// Convert an OpenAI streaming chunk (JSON string) to Anthropic SSE events.
/// Returns a vec of (event_type, data_json) pairs.
pub(crate) fn openai_chunk_to_anthropic_events(
    data: &str,
    state: &mut AnthropicStreamState,
    model_name: &str,
) -> Vec<(String, String)> {
    let mut events = Vec::new();

    let chunk: Value = match serde_json::from_str(data) {
        Ok(v) => v,
        Err(_) => return events,
    };

    let choice = chunk.get("choices").and_then(|c| c.get(0));
    let delta = choice.and_then(|c| c.get("delta"));
    let finish_reason = choice
        .and_then(|c| c.get("finish_reason"))
        .and_then(|f| f.as_str());

    // First chunk: emit message_start + the text content block (index 0).
    if !state.started {
        state.started = true;
        let msg_start = json!({
            "type": "message_start",
            "message": {
                "id": chunk.get("id").cloned().unwrap_or(json!("msg_proxy")),
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model_name,
                "stop_reason": null,
                "stop_sequence": null,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                }
            }
        });
        events.push((
            "message_start".into(),
            serde_json::to_string(&msg_start).unwrap(),
        ));

        // Also emit content_block_start for text
        let block_start = json!({
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "text",
                "text": "",
            }
        });
        events.push((
            "content_block_start".into(),
            serde_json::to_string(&block_start).unwrap(),
        ));
    }

    // Text delta
    if let Some(text) = delta
        .and_then(|d| d.get("content"))
        .and_then(|c| c.as_str())
    {
        let delta_event = json!({
            "type": "content_block_delta",
            "index": 0,
            "delta": {
                "type": "text_delta",
                "text": text,
            }
        });
        events.push((
            "content_block_delta".into(),
            serde_json::to_string(&delta_event).unwrap(),
        ));
    }

    // Tool call deltas. OpenAI streams a tool call across several chunks,
    // keyed by `tool_calls[].index`: the FIRST chunk for an index carries
    // `id` + `function.name`, later chunks carry only `function.arguments`
    // fragments. Anthropic instead needs one `content_block_start`
    // (`type:"tool_use"` with id + name) per call, then `input_json_delta`
    // frames on that block's index, then a `content_block_stop`. The old
    // code emitted only the deltas — with no block start and reusing the
    // text block's index 0 — so Anthropic SDK clients could not reconstruct
    // the tool call at all.
    if let Some(tool_calls) = delta
        .and_then(|d| d.get("tool_calls"))
        .and_then(|t| t.as_array())
    {
        for tc in tool_calls {
            let oai_index = tc.get("index").and_then(|i| i.as_i64()).unwrap_or(0);

            // Assign (first sighting) or look up this call's Anthropic block
            // index. Text holds block 0, so tool blocks start at 1.
            let anthropic_index = match state.tool_blocks.get(&oai_index) {
                Some(idx) => *idx,
                None => {
                    let idx = state.tool_blocks.len() as i64 + 1;
                    state.tool_blocks.insert(oai_index, idx);

                    // Open the tool_use block. id/name arrive on the first
                    // delta for this call; fall back defensively so a
                    // non-conforming upstream still yields a usable block.
                    let tool_id = tc
                        .get("id")
                        .and_then(|v| v.as_str())
                        .map(str::to_string)
                        .unwrap_or_else(|| format!("toolu_proxy_{idx}"));
                    let tool_name = tc
                        .get("function")
                        .and_then(|f| f.get("name"))
                        .and_then(|n| n.as_str())
                        .unwrap_or("")
                        .to_string();
                    let block_start = json!({
                        "type": "content_block_start",
                        "index": idx,
                        "content_block": {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": tool_name,
                            "input": {},
                        }
                    });
                    events.push((
                        "content_block_start".into(),
                        serde_json::to_string(&block_start).unwrap(),
                    ));
                    idx
                }
            };

            // Forward any argument fragment as input_json_delta. Skip empty
            // fragments — the opening delta usually carries "".
            if let Some(args) = tc
                .get("function")
                .and_then(|f| f.get("arguments"))
                .and_then(|a| a.as_str())
                .filter(|a| !a.is_empty())
            {
                let delta_event = json!({
                    "type": "content_block_delta",
                    "index": anthropic_index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": args,
                    }
                });
                events.push((
                    "content_block_delta".into(),
                    serde_json::to_string(&delta_event).unwrap(),
                ));
            }
        }
    }

    // Finish
    if let Some(fr) = finish_reason {
        let stop_reason = match fr {
            "stop" => "end_turn",
            "length" => "max_tokens",
            "tool_calls" => "tool_use",
            other => other,
        };

        // Close every block we opened: the text block (index 0, always
        // started on the first chunk) plus each tool_use block, in ascending
        // index order. The old code stopped only index 0, orphaning every
        // tool_use block.
        let block_stop = json!({"type": "content_block_stop", "index": 0});
        events.push((
            "content_block_stop".into(),
            serde_json::to_string(&block_stop).unwrap(),
        ));
        let mut tool_indices: Vec<i64> = state.tool_blocks.values().copied().collect();
        tool_indices.sort_unstable();
        for idx in tool_indices {
            let block_stop = json!({"type": "content_block_stop", "index": idx});
            events.push((
                "content_block_stop".into(),
                serde_json::to_string(&block_stop).unwrap(),
            ));
        }

        // Usage from final chunk if available
        let usage = chunk.get("usage");
        let output_tokens = usage
            .and_then(|u| u.get("completion_tokens"))
            .and_then(|v| v.as_i64())
            .unwrap_or(0);

        let msg_delta = json!({
            "type": "message_delta",
            "delta": {
                "stop_reason": stop_reason,
                "stop_sequence": null,
            },
            "usage": {
                "output_tokens": output_tokens,
            }
        });
        events.push((
            "message_delta".into(),
            serde_json::to_string(&msg_delta).unwrap(),
        ));

        let msg_stop = json!({"type": "message_stop"});
        events.push((
            "message_stop".into(),
            serde_json::to_string(&msg_stop).unwrap(),
        ));
    }

    events
}

// ---------------------------------------------------------------------------
// POST /v1/messages
// ---------------------------------------------------------------------------

#[utoipa::path(
    post,
    path = "/v1/messages",
    tag = "Anthropic",
    summary = "Messages (Anthropic-compatible)",
    description = "Anthropic Messages API endpoint. Translates to OpenAI format internally and routes through the same LLM pipeline. Supports streaming via SSE in Anthropic event format.",
    request_body = Value,
    responses(
        (status = 200, description = "Anthropic message response (or SSE stream if stream=true)", body = Value),
        (status = 401, description = "Authentication required"),
        (status = 429, description = "Rate/usage limit exceeded"),
    ),
    security(
        ("api_key" = []),
        ("bearer" = []),
    )
)]
pub(crate) async fn messages(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Json(mut anthropic_body): Json<Value>,
) -> Response {
    let start = Instant::now();
    let correlation_id = route_helpers::extract_correlation_id(&headers);
    let tenant_id = match extract_tenant_id(tenant) {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    // Extract model name from the Anthropic body BEFORE deciding pipeline
    let model_name = anthropic_body
        .get("model")
        .and_then(|m| m.as_str())
        .map(String::from);
    let wants_stream = anthropic_body
        .get("stream")
        .and_then(|s| s.as_bool())
        .unwrap_or(false);

    tracing::debug!(
        model_requested = ?model_name,
        stream = wants_stream,
        "Anthropic Messages API request"
    );

    // Preflight: auth → model resolution → access check → quota enforcement
    let pf_body = anthropic_body.to_string();
    // Mint the call chain BEFORE preflight so the run-level ceilings can
    // see which run this call belongs to and which delegation hop it is.
    let tenant_key = derive_tenant_key(state.config.encryption_key.as_bytes(), &tenant_id);
    let (call_chain, current_task_id, trace_continuity) =
        parse_or_mint(|n| header_value(&headers, n), &tenant_key);
    let current_task_id_str = current_task_id.to_string();

    // Where this call sits in its run — the run the pre-flight run controls
    // check, and the run every refusal or block row of this request lands on.
    let run_attr = crate::services::llm_refusal::RunAttribution::chain(
        &call_chain,
        &current_task_id_str,
        trace_continuity,
    );
    let pf = match llm_preflight::run_preflight(
        &state,
        &headers,
        &tenant_id,
        &correlation_id,
        model_name.as_deref(),
        None,
        &PreflightOptions::with_guardrails(run_attr.clone()),
        anthropic_error,
        client_addr.ip(),
        Some(llm_preflight::anthropic_usage_limit_response),
        Some(&pf_body),
    )
    .await
    {
        Ok(pf) => pf,
        Err(resp) => return resp,
    };

    let auth_ctx = pf.auth_ctx;
    let model = pf.model;
    let target_group = pf.target_group;
    // The output this call was admitted for (every token and spend cap's
    // room): bound `max_tokens` — and a `thinking` budget inside it — before
    // the body is forwarded or translated.
    if let Some(ceiling) = pf.out_ceiling {
        crate::services::llm_governance::apply_output_ceiling(&mut anthropic_body, ceiling);
    }

    // Call chain. Parse the inbound delegation
    // headers (or mint a fresh root) so this Anthropic-API request is
    // stitched into the same multi-step trace as the rest of the
    // gateway. The chain headers are then propagated onto every
    // upstream send via OUTBOUND_CHAIN_HEADERS.scope below.
    let trace_root_kind =
        proxy_logger::root_kind_for_auth(&auth_ctx.auth_type, call_chain.depth == 0);

    // Agent authorization, the autonomy kill switch and
    // semantic policy. This surface is mounted, takes the same API keys and
    // reaches the same models as /v1/chat/completions, so without this a
    // suspended AI System was still served here and a denied agent grant was
    // one endpoint away from irrelevant.
    if let Err(resp) = llm_preflight::enforce_actor_planes(
        &state,
        &auth_ctx,
        &tenant_id,
        &model,
        target_group.as_ref(),
        model_name.clone(),
        &serde_json::to_string(
            anthropic_body
                .get("messages")
                .unwrap_or(&serde_json::Value::Null),
        )
        .unwrap_or_default(),
        "/v1/messages",
        &headers,
        &correlation_id,
        start,
        &call_chain,
        &current_task_id_str,
        trace_continuity,
        trace_root_kind,
        anthropic_error,
    )
    .await
    {
        return resp;
    }
    let outbound_chain_owned: Vec<(&'static str, String)> = call_chain
        .to_header_pairs(&tenant_key)
        .into_iter()
        .collect();

    // Guardrails — extract text from Anthropic body directly (works for both paths)
    let input_text = crate::services::anthropic_adapter::extract_input_text(&anthropic_body);
    let guard_target =
        llm_preflight::LlmAuditTarget::from_model(&model, model_name.clone(), "/chat/completions");
    // Tracks whether an input guardrail redacted a PII category — stamped
    // on this request's success audit row(s) below (native + translated,
    // streaming + non-streaming).
    let mut input_pii_redacted = false;
    match llm_preflight::apply_guardrails_with_redaction(
        &state,
        &tenant_id,
        &crate::services::llm_refusal::RefusalScope::new(
            &auth_ctx,
            &headers,
            &correlation_id,
            "/v1/messages",
            &run_attr,
        )
        .started(start),
        &input_text,
        // Anthropic-native /v1/messages input. MUST be the per-mode token
        // `chat_input` — `"llm"` has no `surface_enabled` arm, so input
        // guardrails (prompt-injection, PII, banned words) would be silently
        // skipped on this path (the OpenAI chat path uses `chat_input`).
        crate::constants::guardrail_surface::CHAT_INPUT,
        Some(llm_preflight::anthropic_guardrail_violation_response),
        Some(&guard_target),
    )
    .await
    {
        Err(resp) => return resp,
        Ok(Some(redaction)) => {
            crate::services::anthropic_adapter::apply_redactions_to_input(
                &mut anthropic_body,
                &redaction.content,
            );
            input_pii_redacted = redaction.pii;
        }
        Ok(None) => {}
    }

    // Data-residency check (parity with routes/llm.rs:349). Applied to
    // the SHARED part so both the native and translated branches are gated.
    // A Blocked decision writes a single residency_violation row and 403s
    // before the upstream call; allowed/not-evaluated are stamped on the
    // success rows below (threaded into the native adapter; stamped inline
    // on the translated logs).
    // One shared implementation, see
    // `llm_preflight::enforce_residency`. The block that used to live here
    // was a verbatim copy of the one in routes/llm.rs and routes/gemini.rs,
    // which is how residency fixes kept landing on one surface and not its
    // neighbours.
    let residency = match llm_preflight::enforce_residency(
        &state,
        &auth_ctx,
        &tenant_id,
        &model,
        model_name.clone(),
        "/v1/messages",
        "Anthropic",
        &headers,
        &correlation_id,
        start,
        &call_chain,
        &current_task_id_str,
        trace_continuity,
        trace_root_kind,
        anthropic_error,
    )
    .await
    {
        Ok(o) => o,
        Err(resp) => return resp,
    };
    let provider_region_for_audit = residency.provider_region;
    let residency_decision = residency.decision;

    // RG governance enforcement (temperature/max_tokens/top_p caps,
    // tool filtering, banned-pattern, mandatory system-prompt injection) at
    // PARITY with chat_completions. Applied to the SHARED part so it covers
    // native + translated, streaming + non-streaming. `apply_governance`
    // operates on the chat-completion shape, so we build a chat *view* of
    // the Anthropic body, run the unchanged helper (a `reject` cap or a
    // banned pattern 403s here, before upstream), then splice the governed
    // params + injected `system` + filtered `tools` back into the native
    // body. Param caps are discarded — the Anthropic native pipeline has no
    // model-default-injection step that would need EFFECTIVE_PARAM_CAPS.
    {
        let mut gov_view = anthropic_request_to_chat_view(&anthropic_body);
        match llm_preflight::apply_governance(
            &state,
            &target_group,
            &model.id,
            &mut gov_view,
            anthropic_error,
            &crate::services::llm_refusal::RefusalScope {
                auth_ctx: &auth_ctx,
                headers: &headers,
                correlation_id: &correlation_id,
                endpoint: "/v1/messages",
                started: Some(start),
                model: Some((&model, model_name.clone())),
                group_id: None,
                run: Some(&crate::services::llm_refusal::RunAttribution::chain(
                    &call_chain,
                    &current_task_id_str,
                    trace_continuity,
                )),
                request_body: None,
            },
        )
        .await
        {
            Ok(_caps) => apply_anthropic_chat_view_back(&mut anthropic_body, &gov_view),
            Err(resp) => return resp,
        }
    }

    // ===================================================================
    // NATIVE PIPELINE — for providers that speak Anthropic Messages natively
    // (Anthropic Direct, Bedrock, Vertex AI)
    // Keeps the request in native Anthropic format end-to-end.
    // ===================================================================
    if crate::services::anthropic_adapter::is_native_anthropic_provider(&model.provider) {
        // Output checks read `text` blocks only. Where one can block or
        // redact, the thinking must not come back readable: omitted on a
        // model that can omit (`withhold_anthropic_thinking`), its text
        // cleared in the response on one that cannot.
        if crate::services::providers::is_claude_model(&model.model_name) {
            match llm_preflight::reasoning_must_be_withheld(
                &state,
                &tenant_id,
                &crate::services::llm_refusal::RefusalScope::new(
                    &auth_ctx,
                    &headers,
                    &correlation_id,
                    "/v1/messages",
                    &run_attr,
                )
                .started(start),
            )
            .await
            {
                Ok(true) => {
                    if let Some(obj) = anthropic_body.as_object_mut() {
                        crate::services::providers::withhold_anthropic_thinking(
                            obj,
                            &model.model_name,
                        );
                    }
                }
                Ok(false) => {}
                Err(resp) => return resp,
            }
        }
        tracing::debug!(
            provider = %model.provider,
            native = true,
            "Using native Anthropic pipeline"
        );

        if wants_stream {
            return crate::services::call_chain::OUTBOUND_CHAIN_HEADERS
                .scope(
                    outbound_chain_owned.clone(),
                    crate::services::anthropic_adapter::forward_native_streaming(
                        &state,
                        &headers,
                        anthropic_body,
                        &model,
                        &auth_ctx,
                        &tenant_id,
                        target_group.as_ref(),
                        &correlation_id,
                        start,
                        // Native /v1/messages client speaks Anthropic SSE —
                        // verbatim passthrough is what it wants.
                        false,
                        input_pii_redacted,
                        residency_decision.clone(),
                        provider_region_for_audit.clone(),
                        run_attr.clone(),
                    ),
                )
                .await;
        } else {
            return crate::services::call_chain::OUTBOUND_CHAIN_HEADERS
                .scope(
                    outbound_chain_owned.clone(),
                    crate::services::anthropic_adapter::forward_native_non_streaming(
                        &state,
                        &headers,
                        anthropic_body,
                        &model,
                        &auth_ctx,
                        &tenant_id,
                        target_group.as_ref(),
                        &correlation_id,
                        start,
                        input_pii_redacted,
                        residency_decision.clone(),
                        provider_region_for_audit.clone(),
                        run_attr.clone(),
                    ),
                )
                .await;
        }
    }

    // ===================================================================
    // FALLBACK PIPELINE — for non-Anthropic-family providers (OpenAI, etc.)
    // Translates Anthropic → OpenAI format and routes through the standard
    // LLM pipeline. This preserves backwards compatibility.
    // ===================================================================
    tracing::debug!(
        provider = %model.provider,
        native = false,
        "Using OpenAI translation pipeline (fallback)"
    );

    // Translate Anthropic request → OpenAI format
    let openai_body = anthropic_to_openai_request(&anthropic_body);

    // Parse into ChatCompletionRequest
    let request: crate::models::proxy::ChatCompletionRequest =
        match serde_json::from_value(openai_body) {
            Ok(r) => r,
            Err(e) => {
                return anthropic_error(
                    StatusCode::BAD_REQUEST,
                    &format!("Failed to parse request: {e}"),
                );
            }
        };

    if wants_stream {
        // ===== STREAMING PATH (Anthropic SSE format) =====
        // Cluster-wide RG / per-model concurrency gate for the stream —
        // guard moved into the SSE task below, released at stream-end via Drop.
        let stream_conc_guard = match crate::services::llm_refusal::acquire_concurrency(
            &state,
            target_group.as_ref(),
            &model,
            &crate::services::llm_refusal::RefusalScope {
                auth_ctx: &auth_ctx,
                headers: &headers,
                correlation_id: &correlation_id,
                endpoint: "/v1/messages",
                started: Some(start),
                model: Some((&model, None)),
                group_id: None,
                run: Some(&crate::services::llm_refusal::RunAttribution::chain(
                    &call_chain,
                    &current_task_id_str,
                    trace_continuity,
                )),
                request_body: None,
            },
        )
        .await
        {
            Ok(guard) => guard,
            Err(resp) => return resp,
        };
        // Task-local outbound chain
        // (read by llm_router's reqwest builder).
        match crate::services::call_chain::OUTBOUND_CHAIN_HEADERS
            .scope(
                outbound_chain_owned.clone(),
                llm_router::forward_streaming_with_model_fallback(&state, &model, &request),
            )
            .await
        {
            Ok(upstream_resp) => {
                let byte_stream = upstream_resp.bytes_stream();
                let (tx, rx) = tokio::sync::mpsc::channel::<Result<Event, axum::Error>>(32);

                let state_bg = state.clone();
                let auth_bg = auth_ctx.clone();
                let model_bg = model.clone();
                let corr_id = correlation_id.clone();
                let tenant_id_bg = tenant_id.clone();
                let target_group_bg = target_group.clone();
                let model_requested = request.model.clone();
                let user_agent = extract_user_agent(&headers);
                let sanitized_req_headers = proxy_logger::sanitize_headers(&headers);
                let req_headers_count = headers.len() as i32;
                // Residency outcome stamped on the translated streaming
                // success row.
                let residency_decision_bg = residency_decision.clone();
                let provider_region_bg = provider_region_for_audit.clone();
                // Call-chain stamping for the spawned streaming log task.
                // `trace_continuity` / `trace_root_kind` are Copy; clone the
                // chain + current-task-id string into the closure.
                let call_chain_bg = call_chain.clone();
                let current_task_id_bg = current_task_id_str.clone();
                let request_body_str = serde_json::to_string(&request).unwrap_or_default();
                let request_body_val = serde_json::to_value(&request).unwrap_or_default();
                let needs_normalization = providers::needs_stream_normalization(&model_bg.provider);
                let model_name_for_stream = model_bg.model_name.clone();

                let input_cost = model.cost_per_input_token.unwrap_or(0.0);
                let output_cost = model.cost_per_output_token.unwrap_or(0.0);

                // Output guardrails for the streamed response (chat_output
                // surface). Loaded on the request thread before the spawn so
                // the hot loop never blocks on config IO. /v1/messages is a
                // first-class chat surface, so it gets the same StreamGuard
                // protection as /chat/completions — without it, PII/secrets
                // would egress to native-Anthropic clients unredacted.
                let stream_guardrail_configs = match state
                    .guardrails_cache
                    .get_configs(
                        &tenant_id,
                        auth_ctx.guardrail_user_id().as_deref(),
                        auth_ctx.guardrail_group_id(),
                        &state.db,
                    )
                    .await
                {
                    Ok(c) => c,
                    // Fail closed — see `configs_unavailable`.
                    Err(e) => {
                        return crate::services::llm_refusal::guardrails_unavailable(
                            &state,
                            &crate::services::llm_refusal::RefusalScope::new(
                                &auth_ctx,
                                &headers,
                                &correlation_id,
                                "/v1/messages",
                                &run_attr,
                            )
                            .started(start),
                            crate::constants::guardrail_surface::CHAT_OUTPUT,
                            &e,
                        );
                    }
                };

                proxy_metrics::stream_opened();

                tokio::spawn(async move {
                    // Hold the RG concurrency slot for the whole stream.
                    let _stream_conc_guard = stream_conc_guard;
                    let mut sse_buf = crate::services::sse::SseByteBuffer::new();
                    let mut total_chunks = 0u32;
                    let mut total_response_bytes = 0usize;
                    // On-the-wire (post-guard / post-redaction) response text,
                    // accumulated from the frames we actually send to the
                    // client. This is what the proxy log records, so the audit
                    // body matches the wire exactly.
                    let mut accumulated_content = String::with_capacity(4096);
                    // Raw upstream text is discarded — we must not persist
                    // unredacted PII in the audit log. `extract_stream_chunk_data`
                    // still needs a content sink while it harvests finish/usage.
                    let mut upstream_text_sink = String::with_capacity(4096);
                    let mut finish_reason: Option<String> = None;
                    let mut prompt_tokens: Option<i32> = None;
                    let mut completion_tokens: Option<i32> = None;
                    // Provider prompt-cache counters harvested from the
                    // stream's usage events. See the OpenAI-side
                    // streaming path for the rationale — same shape.
                    let mut cache_write_tokens: i32 = 0;
                    let mut cache_read_tokens: i32 = 0;
                    let mut normalizer = providers::create_stream_normalizer(&model_bg.provider);
                    let mut anthropic_stream_state = AnthropicStreamState::default();
                    // Output-guardrail state machine for this stream (sync
                    // buffer-and-release redaction, else async-interrupt).
                    let mut stream_guard = crate::services::stream_guard::StreamGuard::new(
                        stream_guardrail_configs.into_vec(),
                        state_bg.http_client.clone(),
                    )
                    .for_run(tenant_id_bg.clone(), call_chain_bg.root_task_id.to_string());
                    let mut guardrail_blocked = false;

                    futures::pin_mut!(byte_stream);
                    'stream: while let Some(chunk_result) = byte_stream.next().await {
                        match chunk_result {
                            Ok(bytes) => {
                                total_response_bytes += bytes.len();
                                if total_response_bytes > MAX_STREAM_BYTES {
                                    tracing::warn!(
                                        total_bytes = total_response_bytes,
                                        "Stream exceeded safety ceiling, aborting"
                                    );
                                    break;
                                }
                                sse_buf.push(&bytes);

                                // UTF-8-safe (complete events only) + CRLF-aware
                                // SSE parsing. See services::sse.
                                while let Some(event_str) = sse_buf.next_event() {
                                    for line in event_str.lines() {
                                        if let Some(data) =
                                            crate::services::sse::sse_field(line, "data")
                                        {
                                            // First normalize to OpenAI format if needed
                                            let openai_chunks: Vec<String> = if needs_normalization
                                            {
                                                normalizer.convert(data)
                                            } else if data.trim() == "[DONE]" {
                                                vec!["[DONE]".to_string()]
                                            } else {
                                                vec![data.to_string()]
                                            };

                                            for chunk_str in openai_chunks {
                                                if chunk_str == "[DONE]" {
                                                    // No [DONE] in Anthropic format — message_stop is the terminal
                                                    continue;
                                                }

                                                // Harvest usage/finish for the audit log; raw content
                                                // goes to the discard sink because the logged body is
                                                // rebuilt from the post-guard frames below.
                                                extract_stream_chunk_data(
                                                    &chunk_str,
                                                    &mut upstream_text_sink,
                                                    &mut finish_reason,
                                                    &mut prompt_tokens,
                                                    &mut completion_tokens,
                                                    &mut cache_write_tokens,
                                                    &mut cache_read_tokens,
                                                );

                                                // An operator can stop the
                                                // run mid-stream; dropping the upstream is
                                                // what stops the provider billing.
                                                if stream_guard.run_aborted(&state_bg).await {
                                                    let _ = emit_anthropic_stream_action(
                                                        &tx,
                                                        crate::services::stream_guard::StreamAction::Block(
                                                            crate::services::run_intervention::ABORTED_MESSAGE
                                                                .to_string(),
                                                        ),
                                                        &mut anthropic_stream_state,
                                                        &model_name_for_stream,
                                                        &mut total_chunks,
                                                        &mut guardrail_blocked,
                                                        &mut accumulated_content,
                                                    )
                                                    .await;
                                                    break 'stream;
                                                }
                                                // Run the output guard, then convert the post-guard
                                                // (redacted) OpenAI frames → Anthropic SSE events.
                                                let action = stream_guard.process(&chunk_str).await;
                                                if emit_anthropic_stream_action(
                                                    &tx,
                                                    action,
                                                    &mut anthropic_stream_state,
                                                    &model_name_for_stream,
                                                    &mut total_chunks,
                                                    &mut guardrail_blocked,
                                                    &mut accumulated_content,
                                                )
                                                .await
                                                {
                                                    break 'stream;
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                            Err(e) => {
                                tracing::warn!("Stream error: {e}");
                                break;
                            }
                        }
                    }

                    // Flush any sync-mode held-back tail (no-op in the common
                    // case — the structural finish frame already flushed it).
                    if !guardrail_blocked {
                        let action = stream_guard.finish().await;
                        let _ = emit_anthropic_stream_action(
                            &tx,
                            action,
                            &mut anthropic_stream_state,
                            &model_name_for_stream,
                            &mut total_chunks,
                            &mut guardrail_blocked,
                            &mut accumulated_content,
                        )
                        .await;
                    }

                    // Background logging (same as OpenAI path)
                    let total_tokens = prompt_tokens.zip(completion_tokens).map(|(p, c)| p + c);

                    let duration = start.elapsed().as_millis() as f64;
                    let target_url = providers::build_url(&model_bg, "/chat/completions", true);
                    let mut log = proxy_logger::build_proxy_log(
                        "llm",
                        &auth_bg,
                        "POST",
                        "/v1/messages",
                        Some(&target_url),
                        Some(&model_bg.id),
                        200,
                        duration,
                        true,
                        None,
                        &corr_id,
                    );
                    crate::routes::llm_helpers::populate_group_id(&mut log, &target_group_bg);
                    log.proxy_subtype =
                        Some(crate::constants::proxy_subtype::ANTHROPIC_MESSAGES.to_string());
                    log.user_agent = user_agent.clone();
                    log.request_size_bytes = Some(request_body_str.len() as i32);
                    log.request_headers = Some(sanitized_req_headers.clone());
                    log.request_body_preview = Some(body_preview(&request_body_str));
                    log.response_size_bytes = Some(total_response_bytes as i32);
                    log.response_body_preview = Some(
                        serde_json::to_string(&json!({
                            "streaming": true,
                            "chunks": total_chunks,
                            "finish_reason": finish_reason,
                            "content": accumulated_content,
                        }))
                        .unwrap_or_default(),
                    );
                    log.llm_model_requested = model_requested.clone();
                    log.llm_model_used = Some(model_bg.model_name.clone());
                    log.llm_provider = Some(model_bg.provider.clone());
                    log.llm_prompt_tokens = prompt_tokens;
                    log.llm_completion_tokens = completion_tokens;
                    log.llm_total_tokens = total_tokens;
                    log.llm_streaming = Some(true);
                    log.llm_used_fallback = Some(false);
                    populate_llm_request_fields(&mut log, &request_body_val);

                    log.llm_estimated_cost_usd = crate::routes::llm_helpers::estimate_llm_cost(
                        prompt_tokens,
                        completion_tokens,
                        input_cost,
                        output_cost,
                    );
                    let estimated_cost = log.llm_estimated_cost_usd;

                    // Prompt cache telemetry (Anthropic native streaming).
                    // Same rationale as the OpenAI-compatible /chat
                    // path: populate the three cache columns so Mission
                    // Control can roll up per-group / per-tenant reads,
                    // writes, and $ saved.
                    let pc_mechanism = crate::services::prompt_cache_usage::mechanism_for_provider(
                        &model_bg.provider,
                        &model_bg.model_name,
                    );
                    let pc_usage = crate::services::prompt_cache_usage::usage_from_stream_totals(
                        cache_write_tokens,
                        cache_read_tokens,
                        pc_mechanism,
                        &model_bg,
                    );
                    if pc_usage.write_tokens > 0 || pc_usage.read_tokens > 0 {
                        log.prompt_cache_write_tokens = Some(pc_usage.write_tokens);
                        log.prompt_cache_read_tokens = Some(pc_usage.read_tokens);
                        log.prompt_cache_savings_usd = Some(pc_usage.savings_usd);
                        let mech_label =
                            crate::services::proxy_metrics::mechanism_label(pc_mechanism);
                        crate::services::proxy_metrics::record_prompt_cache_tokens(
                            "read",
                            mech_label,
                            &model_bg.provider,
                            pc_usage.read_tokens,
                        );
                        crate::services::proxy_metrics::record_prompt_cache_tokens(
                            "write",
                            mech_label,
                            &model_bg.provider,
                            pc_usage.write_tokens,
                        );
                        crate::services::proxy_metrics::record_prompt_cache_savings(
                            mech_label,
                            &model_bg.provider,
                            pc_usage.savings_usd,
                        );
                    }

                    proxy_logger::apply_call_chain(
                        &mut log,
                        &call_chain_bg,
                        &current_task_id_bg,
                        trace_continuity,
                        trace_root_kind,
                    );
                    // A streamed output-guardrail block is recorded on the
                    // stream's own outcome row — the row that names the run
                    // and step and carries the tokens the provider billed
                    // before the guard stopped it.
                    stream_guard.stamp_outcome(&mut log);
                    if input_pii_redacted {
                        proxy_logger::apply_pii_detected(&mut log);
                    }
                    // Stamp residency outcome on the translated streaming
                    // success row.
                    match &residency_decision_bg {
                        Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
                        None => proxy_logger::apply_residency_not_evaluated(
                            &mut log,
                            provider_region_bg.as_deref(),
                        ),
                    }
                    state_bg
                        .background_writer
                        .write_proxy_log(state_bg.db.clone(), log);

                    let llm_metric_data = serde_json::json!({
                        "provider": model_bg.provider,
                        "model_requested": model_requested,
                        "model_used": model_bg.model_name,
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": total_tokens,
                        "estimated_cost_usd": estimated_cost,
                        "streaming": true,
                    });
                    let metric = proxy_logger::build_proxy_metric(
                        "llm",
                        &auth_bg,
                        "POST",
                        "/v1/messages",
                        "anthropic_messages",
                        Some(&model_bg.id),
                        200,
                        duration,
                        true,
                        &corr_id,
                        Some(request_body_str.len() as i32),
                        Some(total_response_bytes as i32),
                        Some(req_headers_count),
                        None,
                        user_agent.as_deref(),
                        Some(llm_metric_data),
                        None,
                    );
                    state_bg
                        .background_writer
                        .write_proxy_metric(state_bg.db.clone(), metric);

                    usage::write_llm_usage_log(
                        &state_bg.usage_state,
                        &state_bg.background_writer,
                        &state_bg.model_metrics,
                        state_bg.db.clone(),
                        &tenant_id_bg,
                        &auth_bg,
                        target_group_bg.as_ref(),
                        &model_bg.id,
                        &model_bg.model_name,
                        &model_bg.provider,
                        prompt_tokens,
                        completion_tokens,
                        estimated_cost,
                        input_cost,
                        output_cost,
                        &target_url,
                        &corr_id,
                        200,
                        duration,
                    );
                    if let Some(ref g) = target_group_bg {
                        usage::spawn_alert_evaluation(
                            state_bg.db.clone(),
                            tenant_id_bg.clone(),
                            g.clone(),
                            "llm".into(),
                            Some(corr_id.clone()),
                        );
                    }

                    proxy_metrics::record_request("llm", "POST", 200, &model_bg.provider);
                    proxy_metrics::record_latency(
                        "llm",
                        "POST",
                        &model_bg.provider,
                        duration / 1000.0,
                    );
                    if let Some(pt) = prompt_tokens {
                        proxy_metrics::record_tokens(
                            "prompt",
                            &model_bg.provider,
                            &model_bg.model_name,
                            pt,
                        );
                    }
                    if let Some(ct) = completion_tokens {
                        proxy_metrics::record_tokens(
                            "completion",
                            &model_bg.provider,
                            &model_bg.model_name,
                            ct,
                        );
                    }
                    if let Some(cost) = estimated_cost {
                        proxy_metrics::record_cost(&model_bg.provider, &model_bg.model_name, cost);
                    }
                    proxy_metrics::stream_closed();

                    let stream_total_tokens = prompt_tokens
                        .zip(completion_tokens)
                        .map(|(p, c)| (p as i64) + (c as i64));
                    llm_router::record_stream_success(
                        &state_bg.router_state,
                        &model_bg.id,
                        duration,
                        stream_total_tokens,
                        model_bg.routed_via_group_id.as_deref(),
                    );
                });

                let stream = ReceiverStream::new(rx);
                let mut resp = Sse::new(stream)
                    .keep_alive(KeepAlive::default())
                    .into_response();

                resp.headers_mut().insert(
                    crate::constants::headers::X_CORRELATION_ID,
                    correlation_id.parse().unwrap(),
                );
                resp.headers_mut().insert(
                    crate::constants::headers::X_AUTH_TYPE,
                    auth_ctx.auth_type.to_string().parse().unwrap(),
                );
                crate::services::a2a_delegation::stamp_run_root(
                    resp.headers_mut(),
                    &call_chain.root_task_id,
                );

                resp
            }
            Err(e) => {
                let duration = start.elapsed().as_millis() as f64;
                let (status, body) = match &e {
                    llm_router::LlmRouterError::UpstreamHttpError { status, body } => (
                        StatusCode::from_u16(*status).unwrap_or(StatusCode::BAD_GATEWAY),
                        body.clone(),
                    ),
                    llm_router::LlmRouterError::AllModelsBusy { .. }
                    | llm_router::LlmRouterError::RateLimitExceeded(_) => (
                        StatusCode::TOO_MANY_REQUESTS,
                        json!({"error": e.to_string()}),
                    ),
                    llm_router::LlmRouterError::ConcurrencyLimitReached(_) => (
                        StatusCode::TOO_MANY_REQUESTS,
                        json!({"error": e.to_string()}),
                    ),
                    _ => (StatusCode::BAD_GATEWAY, json!({"error": e.to_string()})),
                };

                let target_url = providers::build_url(&model, "/chat/completions", false);
                let request_body_str = serde_json::to_string(&request).unwrap_or_default();
                let mut log = proxy_logger::build_proxy_log(
                    "llm",
                    &auth_ctx,
                    "POST",
                    "/v1/messages",
                    Some(&target_url),
                    Some(&model.id),
                    status.as_u16(),
                    duration,
                    false,
                    Some(&e.to_string()),
                    &correlation_id,
                );
                crate::routes::llm_helpers::populate_group_id(&mut log, &target_group);
                log.user_agent = extract_user_agent(&headers);
                log.request_size_bytes = Some(request_body_str.len() as i32);
                log.request_body_preview = Some(body_preview(&request_body_str));
                log.llm_model_requested = request.model.clone();
                log.llm_model_used = Some(model.model_name.clone());
                log.llm_provider = Some(model.provider.clone());
                log.llm_streaming = Some(true);
                // The gateway's own routing limit: this row is the refusal.
                if let Some(sub) = crate::services::llm_refusal::router_limit_subtype(&e) {
                    log.proxy_subtype = Some(sub);
                }
                populate_llm_request_fields(
                    &mut log,
                    &serde_json::to_value(&request).unwrap_or_default(),
                );
                proxy_logger::apply_call_chain(
                    &mut log,
                    &call_chain,
                    &current_task_id_str,
                    trace_continuity,
                    trace_root_kind,
                );
                state
                    .background_writer
                    .write_proxy_log(state.db.clone(), log);

                proxy_metrics::record_request("llm", "POST", status.as_u16(), &model.provider);
                proxy_metrics::record_latency("llm", "POST", &model.provider, duration / 1000.0);

                anthropic_error(status, &body.to_string())
            }
        }
    } else {
        // ===== NON-STREAMING PATH =====
        // The native /v1/messages endpoint skips prompt-cache policy
        // injection for now — clients hitting /v1/messages directly
        // are responsible for their own cache_control markers (it's
        // part of the native Anthropic API surface). Only the
        // OpenAI-compatible /v1/chat/completions path uses Brutor's
        // auto-inject behaviour.
        // Propagate chain to fallbacks.
        match crate::services::call_chain::OUTBOUND_CHAIN_HEADERS
            .scope(
                outbound_chain_owned,
                // Scope the response-cache identity, exactly
                // as the OpenAI surface does. Without it every /v1/messages
                // completion was cached under one tenant-wide bucket, so two
                // end users sending the same body to the same model were
                // served each other's answers.
                //
                // Unless the request said `X-Brutor-Cache: off`: with
                // no identity the router neither reads nor writes that cache.
                {
                    let forward = llm_router::forward_with_fallbacks(
                        &state.http_client,
                        &state.db,
                        &model,
                        &request,
                        false,
                        &state.router_state,
                        // No prompt-cache session: this surface in front
                        // of a model that is not Anthropic's has never
                        // asked for prompt caching.
                        None,
                        Some(&state),
                        target_group.as_ref(),
                    );
                    crate::services::response_cache::with_dispatch_cache(
                        state.response_cache.dispatch_cache(&headers, &auth_ctx),
                        forward,
                    )
                },
            )
            .await
        {
            Ok((mut resp_body, status_code, used_model, used_fallback)) => {
                let duration = start.elapsed().as_millis() as f64;
                let success = (200..300).contains(&status_code);

                if success {
                    state.router_state.record_latency(&used_model.id, duration);
                }

                tracing::debug!(
                    model = %used_model.model_name,
                    provider = %used_model.provider,
                    status_code,
                    duration_ms = duration,
                    used_fallback,
                    success,
                    "Anthropic Messages response"
                );

                let usage_obj = resp_body.get("usage");
                let prompt_tokens = usage_obj
                    .and_then(|u| u.get("prompt_tokens"))
                    .and_then(|v| v.as_i64())
                    .map(|v| v as i32);
                let completion_tokens = usage_obj
                    .and_then(|u| u.get("completion_tokens"))
                    .and_then(|v| v.as_i64())
                    .map(|v| v as i32);

                let request_body_str = serde_json::to_string(&request).unwrap_or_default();
                let response_body_str = serde_json::to_string(&resp_body).unwrap_or_default();

                // Background logging
                let target_url = providers::build_url(&used_model, "/chat/completions", false);
                let mut log = proxy_logger::build_proxy_log(
                    "llm",
                    &auth_ctx,
                    "POST",
                    "/v1/messages",
                    Some(&target_url),
                    Some(&used_model.id),
                    status_code,
                    duration,
                    success,
                    None,
                    &correlation_id,
                );
                crate::routes::llm_helpers::populate_group_id(&mut log, &target_group);
                let ua = extract_user_agent(&headers);
                log.proxy_subtype =
                    Some(crate::constants::proxy_subtype::ANTHROPIC_MESSAGES.to_string());
                log.user_agent = ua.clone();
                log.request_size_bytes = Some(request_body_str.len() as i32);
                log.request_headers = Some(proxy_logger::sanitize_headers(&headers));
                log.request_body_preview = Some(body_preview(&request_body_str));
                log.response_size_bytes = Some(response_body_str.len() as i32);
                log.response_body_preview = Some(body_preview(&response_body_str));
                log.llm_model_requested = request.model.clone();
                log.llm_model_used = Some(used_model.model_name.clone());
                log.llm_provider = Some(used_model.provider.clone());
                log.llm_prompt_tokens = prompt_tokens;
                log.llm_completion_tokens = completion_tokens;
                log.llm_total_tokens = prompt_tokens.zip(completion_tokens).map(|(p, c)| p + c);
                log.llm_streaming = Some(false);
                log.llm_used_fallback = Some(used_fallback);
                // Input guardrail redacted a PII category → stamp the
                // success row (output redaction is stamped separately in
                // the Redacted arm of the output-guardrail loop below).
                if input_pii_redacted {
                    proxy_logger::apply_pii_detected(&mut log);
                }
                // Stamp residency outcome on the translated non-streaming
                // success row.
                match &residency_decision {
                    Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
                    None => proxy_logger::apply_residency_not_evaluated(
                        &mut log,
                        provider_region_for_audit.as_deref(),
                    ),
                }
                if used_fallback {
                    log.llm_fallback_reason =
                        Some(format!("Fallback to {}", used_model.model_name));
                }
                populate_llm_request_fields(
                    &mut log,
                    &serde_json::to_value(&request).unwrap_or_default(),
                );

                let log_estimated_cost = crate::routes::llm_helpers::estimate_llm_cost(
                    prompt_tokens,
                    completion_tokens,
                    used_model.cost_per_input_token.unwrap_or(0.0),
                    used_model.cost_per_output_token.unwrap_or(0.0),
                );
                log.llm_estimated_cost_usd = log_estimated_cost;

                // Prompt cache telemetry (Anthropic native non-streaming).
                // Same as the other three hot paths. `resp_body` carries
                // Anthropic's native usage block (the adapter leaves
                // `cache_{creation,read}_input_tokens` intact).
                let pc_mechanism_ns = crate::services::prompt_cache_usage::mechanism_for_provider(
                    &used_model.provider,
                    &used_model.model_name,
                );
                let pc_usage_ns = crate::services::prompt_cache_usage::extract_usage_from_response(
                    &resp_body,
                    pc_mechanism_ns,
                    &used_model,
                );
                if pc_usage_ns.write_tokens > 0 || pc_usage_ns.read_tokens > 0 {
                    log.prompt_cache_write_tokens = Some(pc_usage_ns.write_tokens);
                    log.prompt_cache_read_tokens = Some(pc_usage_ns.read_tokens);
                    log.prompt_cache_savings_usd = Some(pc_usage_ns.savings_usd);
                    let mech_label =
                        crate::services::proxy_metrics::mechanism_label(pc_mechanism_ns);
                    crate::services::proxy_metrics::record_prompt_cache_tokens(
                        "read",
                        mech_label,
                        &used_model.provider,
                        pc_usage_ns.read_tokens,
                    );
                    crate::services::proxy_metrics::record_prompt_cache_tokens(
                        "write",
                        mech_label,
                        &used_model.provider,
                        pc_usage_ns.write_tokens,
                    );
                    crate::services::proxy_metrics::record_prompt_cache_savings(
                        mech_label,
                        &used_model.provider,
                        pc_usage_ns.savings_usd,
                    );
                }

                // Post-response governance (tool-call banned-arg patterns
                // + AST argument policies + tool-call rate counters) at PARITY
                // with chat. The translated path's `resp_body` is OpenAI-shaped
                // (`choices[].message.tool_calls[]`), so the helper reads it
                // directly. A block 403s; the helper owns its block audit row,
                // so we skip the deferred success write below on a block.
                let mut guardrail_block: Option<Response> = None;
                if let Err(resp) = llm_preflight::apply_response_governance(
                    &state,
                    &target_group,
                    &model.id,
                    &resp_body,
                    anthropic_error,
                    &crate::services::llm_refusal::RefusalScope::new(
                        &auth_ctx,
                        &headers,
                        &correlation_id,
                        "/v1/messages",
                        &run_attr,
                    )
                    .started(start),
                )
                .await
                {
                    guardrail_block = Some(resp);
                }

                // Output guardrails on the (OpenAI-shaped) response BEFORE
                // converting to Anthropic form. Mirrors routes/llm.rs: a Block
                // returns the Anthropic-shaped 403 error; a Redacted result is
                // spliced back into resp_body so the converted body — and the
                // audit preview — reflect the scrubbed content. Without this,
                // the Anthropic-native non-streaming path returned model output
                // with no PII/banned/secrets/toxic enforcement.
                if guardrail_block.is_none() {
                    'guard: {
                        let n = resp_body
                            .get("choices")
                            .and_then(|c| c.as_array())
                            .map(|a| a.len())
                            .unwrap_or(0);
                        for i in 0..n {
                            let content = resp_body
                                .pointer(&format!("/choices/{i}/message/content"))
                                .and_then(|v| v.as_str())
                                .map(|s| s.to_string());
                            let Some(content) = content else { continue };
                            if content.is_empty() {
                                continue;
                            }
                            match llm_preflight::apply_output_guardrails(
                                &state,
                                &tenant_id,
                                &crate::services::llm_refusal::RefusalScope::new(
                                    &auth_ctx,
                                    &headers,
                                    &correlation_id,
                                    "/v1/messages",
                                    &run_attr,
                                )
                                .started(start),
                                &content,
                                crate::constants::guardrail_surface::CHAT_OUTPUT,
                                Some(llm_preflight::anthropic_guardrail_violation_response),
                            )
                            .await
                            {
                                Err(resp) => {
                                    guardrail_block = Some(resp);
                                    break 'guard;
                                }
                                Ok(llm_preflight::OutputCheckResult::Pass) => {}
                                Ok(llm_preflight::OutputCheckResult::Redacted {
                                    content: new_text,
                                    pii,
                                }) => {
                                    if let Some(slot) = resp_body
                                        .pointer_mut(&format!("/choices/{i}/message/content"))
                                    {
                                        *slot = Value::String(new_text);
                                    }
                                    // PII redacted in the output → stamp the success
                                    // row so GDPR/HIPAA tagging fires.
                                    if pii {
                                        proxy_logger::apply_pii_detected(&mut log);
                                    }
                                }
                            }
                        }
                    }
                }
                // On a block, `apply_output_guardrails` /
                // `apply_response_governance` already wrote the
                // authoritative block audit row (with the compliance stamps),
                // so we SKIP the deferred write to avoid a duplicate row. On
                // pass/redact we write the deferred row with the (possibly
                // redacted) preview.
                if guardrail_block.is_none() {
                    let final_body_str = serde_json::to_string(&resp_body).unwrap_or_default();
                    log.response_size_bytes = Some(final_body_str.len() as i32);
                    log.response_body_preview = Some(body_preview(&final_body_str));
                    proxy_logger::apply_call_chain(
                        &mut log,
                        &call_chain,
                        &current_task_id_str,
                        trace_continuity,
                        trace_root_kind,
                    );
                    state
                        .background_writer
                        .write_proxy_log(state.db.clone(), log);
                }

                let llm_metric_data = serde_json::json!({
                    "provider": used_model.provider,
                    "model_requested": request.model,
                    "model_used": used_model.model_name,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens.zip(completion_tokens).map(|(p,c)| p+c),
                    "estimated_cost_usd": log_estimated_cost,
                    "streaming": false,
                    "used_fallback": used_fallback,
                });
                let req_hdr_count = headers.len() as i32;
                let metric = proxy_logger::build_proxy_metric(
                    "llm",
                    &auth_ctx,
                    "POST",
                    "/v1/messages",
                    "anthropic_messages",
                    Some(&used_model.id),
                    status_code,
                    duration,
                    success,
                    &correlation_id,
                    Some(request_body_str.len() as i32),
                    Some(response_body_str.len() as i32),
                    Some(req_hdr_count),
                    None,
                    ua.as_deref(),
                    Some(llm_metric_data),
                    None,
                );
                state
                    .background_writer
                    .write_proxy_metric(state.db.clone(), metric);

                if success {
                    usage::write_llm_usage_log(
                        &state.usage_state,
                        &state.background_writer,
                        &state.model_metrics,
                        state.db.clone(),
                        &tenant_id,
                        &auth_ctx,
                        target_group.as_ref(),
                        &used_model.id,
                        &used_model.model_name,
                        &used_model.provider,
                        prompt_tokens,
                        completion_tokens,
                        log_estimated_cost,
                        used_model.cost_per_input_token.unwrap_or(0.0),
                        used_model.cost_per_output_token.unwrap_or(0.0),
                        &target_url,
                        &correlation_id,
                        status_code,
                        duration,
                    );
                    if let Some(ref g) = target_group {
                        usage::spawn_alert_evaluation(
                            state.db.clone(),
                            tenant_id.clone(),
                            g.clone(),
                            "llm".into(),
                            Some(correlation_id.clone()),
                        );
                    }
                }

                proxy_metrics::record_request("llm", "POST", status_code, &used_model.provider);
                proxy_metrics::record_latency(
                    "llm",
                    "POST",
                    &used_model.provider,
                    duration / 1000.0,
                );
                if let Some(pt) = prompt_tokens {
                    proxy_metrics::record_tokens(
                        "prompt",
                        &used_model.provider,
                        &used_model.model_name,
                        pt,
                    );
                }
                if let Some(ct) = completion_tokens {
                    proxy_metrics::record_tokens(
                        "completion",
                        &used_model.provider,
                        &used_model.model_name,
                        ct,
                    );
                }
                if let Some(cost) = log_estimated_cost {
                    proxy_metrics::record_cost(&used_model.provider, &used_model.model_name, cost);
                }

                // Output guardrail blocked the response — return the
                // Anthropic-shaped 403 error instead of the model output.
                if let Some(resp) = guardrail_block {
                    return resp;
                }

                // Convert OpenAI response → Anthropic format
                let anthropic_resp = openai_to_anthropic_response(&resp_body);

                let status = StatusCode::from_u16(status_code).unwrap_or(StatusCode::OK);
                let mut resp = (status, Json(anthropic_resp)).into_response();
                resp.headers_mut().insert(
                    crate::constants::headers::X_CORRELATION_ID,
                    correlation_id.parse().unwrap(),
                );
                resp.headers_mut().insert(
                    crate::constants::headers::X_AUTH_TYPE,
                    auth_ctx.auth_type.to_string().parse().unwrap(),
                );
                crate::services::a2a_delegation::stamp_run_root(
                    resp.headers_mut(),
                    &call_chain.root_task_id,
                );

                resp
            }
            Err(e) => {
                let duration = start.elapsed().as_millis() as f64;
                let target_url = providers::build_url(&model, "/chat/completions", false);
                let request_body_str = serde_json::to_string(&request).unwrap_or_default();
                let mut log = proxy_logger::build_proxy_log(
                    "llm",
                    &auth_ctx,
                    "POST",
                    "/v1/messages",
                    Some(&target_url),
                    Some(&model.id),
                    502,
                    duration,
                    false,
                    Some(&e.to_string()),
                    &correlation_id,
                );
                crate::routes::llm_helpers::populate_group_id(&mut log, &target_group);
                log.user_agent = extract_user_agent(&headers);
                log.request_size_bytes = Some(request_body_str.len() as i32);
                log.request_body_preview = Some(body_preview(&request_body_str));
                log.llm_model_requested = request.model.clone();
                log.llm_model_used = Some(model.model_name.clone());
                log.llm_provider = Some(model.provider.clone());
                log.llm_streaming = Some(false);
                populate_llm_request_fields(
                    &mut log,
                    &serde_json::to_value(&request).unwrap_or_default(),
                );
                proxy_logger::apply_call_chain(
                    &mut log,
                    &call_chain,
                    &current_task_id_str,
                    trace_continuity,
                    trace_root_kind,
                );
                state
                    .background_writer
                    .write_proxy_log(state.db.clone(), log);

                proxy_metrics::record_request("llm", "POST", 502, &model.provider);
                proxy_metrics::record_latency("llm", "POST", &model.provider, duration / 1000.0);

                anthropic_error(StatusCode::BAD_GATEWAY, &e.to_string())
            }
        }
    }
}

// ---------------------------------------------------------------------------
// POST /v1/messages/count_tokens
// ---------------------------------------------------------------------------

#[utoipa::path(
    post,
    path = "/v1/messages/count_tokens",
    tag = "Anthropic",
    summary = "Count tokens (Anthropic-compatible)",
    description = "Token counting endpoint for Anthropic Messages API format.",
    request_body = Value,
    responses(
        (status = 200, description = "Token count", body = Value),
        (status = 401, description = "Authentication required"),
    ),
    security(
        ("api_key" = []),
        ("bearer" = []),
    )
)]
pub(crate) async fn count_tokens(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Json(anthropic_body): Json<Value>,
) -> Response {
    let tenant_id = match extract_tenant_id(tenant) {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    // Authenticate
    if let Err(e) = auth::authenticate_request(
        &headers,
        &tenant_id,
        &state.config.jwt_secret,
        &state.db,
        &state.credential_verifiers,
        client_addr.ip(),
        true,
    )
    .await
    {
        return anthropic_error(e.status_code(), &e.to_string());
    }

    // Resolve model to check if we can proxy count_tokens natively
    let model_name = anthropic_body
        .get("model")
        .and_then(|m| m.as_str())
        .map(String::from);

    if let Some(ref name) = model_name {
        if let Ok(model) = llm_router::resolve_model(
            &state.db,
            &tenant_id,
            Some(name),
            None,
            &state.router_state,
            Some(&state.encryption),
            Some(&state.model_cache),
            Some(&state.provider_cache),
        )
        .await
        {
            // Check if the backend supports count_tokens natively
            use crate::services::anthropic_adapter::backend;
            if let Some(be) = backend::get_backend(&model.provider) {
                if let Some(count_url) = be.count_tokens_url(&model) {
                    // Proxy to the backend's count_tokens endpoint
                    tracing::debug!(
                        url = %count_url,
                        provider = %model.provider,
                        "Proxying count_tokens to native backend"
                    );

                    let mut req_headers = be.auth_headers(&model);

                    // Forward anthropic-version
                    let version = headers
                        .get("anthropic-version")
                        .and_then(|v| v.to_str().ok())
                        .unwrap_or("2023-06-01");
                    req_headers.push(("anthropic-version".into(), version.to_string()));

                    // Forward filtered beta headers
                    if let Some(beta) = headers.get("anthropic-beta").and_then(|v| v.to_str().ok())
                    {
                        if let Some(filtered) = be.filter_beta_headers(beta) {
                            req_headers.push(("anthropic-beta".into(), filtered));
                        }
                    }

                    let mut req = state
                        .http_client
                        .post(&count_url)
                        .header("Content-Type", "application/json")
                        .timeout(std::time::Duration::from_secs(30));

                    for (k, v) in &req_headers {
                        req = req.header(k.as_str(), v.as_str());
                    }

                    match req.json(&anthropic_body).send().await {
                        Ok(resp) if resp.status().is_success() => {
                            let body: Value = resp.json().await.unwrap_or(json!({}));
                            return Json(body).into_response();
                        }
                        Ok(resp) => {
                            tracing::warn!(
                                status = resp.status().as_u16(),
                                "Native count_tokens failed, falling back to estimation"
                            );
                        }
                        Err(e) => {
                            tracing::warn!(
                                error = %e,
                                "Native count_tokens request failed, falling back to estimation"
                            );
                        }
                    }
                }
                // Backend doesn't support count_tokens — fall through to estimation
            }
        }
    }

    // Fallback: estimate token count from message content
    let input_text = crate::services::anthropic_adapter::extract_input_text(&anthropic_body);

    // Rough approximation: ~4 chars per token for English text
    let estimated_tokens = (input_text.len() as f64 / 4.0).ceil() as i64;

    Json(json!({
        "input_tokens": estimated_tokens,
    }))
    .into_response()
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn extract_tenant_id(tenant: Option<axum::Extension<TenantContext>>) -> Result<String, Response> {
    tenant
        .map(|t| t.tenant_id.clone())
        .ok_or_else(|| anthropic_error(StatusCode::BAD_REQUEST, "Missing tenant context"))
}

/// Return errors in Anthropic API error format.
fn anthropic_error(status: StatusCode, message: &str) -> Response {
    let error_type = match status {
        StatusCode::BAD_REQUEST => "invalid_request_error",
        StatusCode::UNAUTHORIZED => "authentication_error",
        StatusCode::FORBIDDEN => "permission_error",
        StatusCode::NOT_FOUND => "not_found_error",
        StatusCode::TOO_MANY_REQUESTS => "rate_limit_error",
        _ => "api_error",
    };
    let body = json!({
        "type": "error",
        "error": {
            "type": error_type,
            "message": message,
        }
    });
    (status, Json(body)).into_response()
}

/// Extract content, finish_reason, and usage from an OpenAI-format streaming chunk.
fn extract_stream_chunk_data(
    data: &str,
    accumulated: &mut String,
    finish_reason: &mut Option<String>,
    prompt_tokens: &mut Option<i32>,
    completion_tokens: &mut Option<i32>,
    cache_write_tokens: &mut i32,
    cache_read_tokens: &mut i32,
) {
    let chunk = match serde_json::from_str::<Value>(data) {
        Ok(v) => v,
        Err(_) => return,
    };

    let first_choice = chunk.get("choices").and_then(|c| c.get(0));
    let delta = first_choice.and_then(|c| c.get("delta"));

    if let Some(text) = delta
        .and_then(|d| d.get("content"))
        .and_then(|c| c.as_str())
    {
        accumulated.push_str(text);
    }

    if let Some(tool_calls) = delta
        .and_then(|d| d.get("tool_calls"))
        .and_then(|t| t.as_array())
    {
        for tc in tool_calls {
            let func = tc.get("function");
            if let Some(name) = func.and_then(|f| f.get("name")).and_then(|n| n.as_str()) {
                accumulated.push_str("[tool_call: ");
                accumulated.push_str(name);
                accumulated.push_str("] ");
            }
            if let Some(args) = func
                .and_then(|f| f.get("arguments"))
                .and_then(|a| a.as_str())
            {
                accumulated.push_str(args);
            }
        }
    }

    if let Some(fr) = first_choice
        .and_then(|c| c.get("finish_reason"))
        .and_then(|f| f.as_str())
    {
        *finish_reason = Some(fr.to_string());
    }

    // Usage + prompt-cache counters. Same rationale as the OpenAI-side
    // extractor: the normalizer preserves Anthropic's
    // cache_{creation,read}_input_tokens through to the OpenAI-style
    // chunk, and OpenAI-native reports its own cached_tokens in
    // prompt_tokens_details.
    if let Some(usage) = chunk.get("usage") {
        if let Some(pt) = usage.get("prompt_tokens").and_then(|v| v.as_i64()) {
            *prompt_tokens = Some(pt as i32);
        }
        if let Some(ct) = usage.get("completion_tokens").and_then(|v| v.as_i64()) {
            *completion_tokens = Some(ct as i32);
        }
        if let Some(cw) = usage
            .get("cache_creation_input_tokens")
            .and_then(|v| v.as_i64())
        {
            *cache_write_tokens = (*cache_write_tokens).max(cw as i32);
        }
        if let Some(cr) = usage
            .get("cache_read_input_tokens")
            .and_then(|v| v.as_i64())
        {
            *cache_read_tokens = (*cache_read_tokens).max(cr as i32);
        }
        if let Some(cr) = usage
            .get("prompt_tokens_details")
            .and_then(|d| d.get("cached_tokens"))
            .and_then(|v| v.as_i64())
        {
            *cache_read_tokens = (*cache_read_tokens).max(cr as i32);
        }
    }
}

// ===========================================================================
// Anthropic Messages API shape conformance
// ===========================================================================
//
// These tests pin the Messages API response + streaming shapes the
// gateway emits when an OpenAI-backed upstream is translated to an
// Anthropic-style `/v1/messages` reply. The documented shapes come
// from https://docs.anthropic.com/en/api/messages — if the adapter
// drifts away from them, SDKs (anthropic-sdk-python, anthropic-ts,
// Claude Code itself) break silently on the next release.
//
// Keep assertions field-by-field so a diff review makes "which field
// changed?" obvious.

#[cfg(test)]
mod messages_api_conformance {
    use super::*;
    use serde_json::json;

    // ---- openai_to_anthropic_response ------------------------------------

    #[test]
    fn response_has_all_documented_top_level_fields() {
        let openai_resp = json!({
            "id": "chatcmpl-abc",
            "object": "chat.completion",
            "model": "gpt-4o",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        });
        let out = openai_to_anthropic_response(&openai_resp);

        // Spec: every field in the Anthropic Messages response body.
        for key in [
            "id",
            "type",
            "role",
            "content",
            "model",
            "stop_reason",
            "stop_sequence",
            "usage",
        ] {
            assert!(out.get(key).is_some(), "missing top-level `{key}`: {out:?}");
        }
    }

    #[test]
    fn response_type_is_message() {
        let out = openai_to_anthropic_response(&json!({
            "choices": [{"message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}]
        }));
        assert_eq!(out["type"], "message");
    }

    #[test]
    fn response_role_is_assistant() {
        let out = openai_to_anthropic_response(&json!({
            "choices": [{"message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}]
        }));
        assert_eq!(out["role"], "assistant");
    }

    #[test]
    fn text_content_block_shape() {
        let out = openai_to_anthropic_response(&json!({
            "choices": [{
                "message": {"role": "assistant", "content": "the answer is 42"},
                "finish_reason": "stop"
            }]
        }));
        let blocks = out["content"].as_array().expect("content is an array");
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0]["type"], "text");
        assert_eq!(blocks[0]["text"], "the answer is 42");
    }

    #[test]
    fn empty_content_still_emits_one_text_block() {
        // Anthropic spec: `content` is always a non-empty array. If
        // the upstream gave us no text and no tool calls, we still
        // emit an empty text block rather than `[]` — otherwise
        // anthropic-sdk-python raises `IndexError`.
        let out = openai_to_anthropic_response(&json!({
            "choices": [{"message": {"role": "assistant"}, "finish_reason": "stop"}]
        }));
        let blocks = out["content"].as_array().unwrap();
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0]["type"], "text");
        assert_eq!(blocks[0]["text"], "");
    }

    #[test]
    fn tool_calls_render_as_tool_use_blocks() {
        let out = openai_to_anthropic_response(&json!({
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": null,
                    "tool_calls": [{
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": "{\"city\":\"Oslo\"}"
                        }
                    }]
                },
                "finish_reason": "tool_calls"
            }]
        }));
        let blocks = out["content"].as_array().unwrap();
        let tool_block = blocks
            .iter()
            .find(|b| b["type"] == "tool_use")
            .expect("tool_use block present");
        assert_eq!(tool_block["id"], "call-1");
        assert_eq!(tool_block["name"], "get_weather");
        assert_eq!(tool_block["input"], json!({"city": "Oslo"}));
    }

    #[test]
    fn tool_use_arguments_parse_json_not_passed_as_string() {
        // Anthropic spec: `tool_use.input` is a structured JSON
        // object, not a serialised string. OpenAI gives us a string;
        // we must parse it.
        let out = openai_to_anthropic_response(&json!({
            "choices": [{
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "c",
                        "type": "function",
                        "function": {"name": "f", "arguments": "{\"x\": 1}"}
                    }]
                },
                "finish_reason": "tool_calls"
            }]
        }));
        let block = out["content"]
            .as_array()
            .unwrap()
            .iter()
            .find(|b| b["type"] == "tool_use")
            .unwrap();
        assert!(
            block["input"].is_object(),
            "input must be an object, got {}",
            block["input"]
        );
    }

    #[test]
    fn stop_reason_mapping_matches_spec() {
        // Spec values: end_turn, max_tokens, stop_sequence, tool_use.
        let cases = [
            ("stop", "end_turn"),
            ("length", "max_tokens"),
            ("tool_calls", "tool_use"),
            ("content_filter", "end_turn"),
        ];
        for (openai_reason, anthropic_reason) in cases {
            let out = openai_to_anthropic_response(&json!({
                "choices": [{"message": {"role": "assistant", "content": "x"}, "finish_reason": openai_reason}]
            }));
            assert_eq!(
                out["stop_reason"], anthropic_reason,
                "`{openai_reason}` must map to `{anthropic_reason}`",
            );
        }
    }

    #[test]
    fn usage_uses_anthropic_token_field_names() {
        // Anthropic: `input_tokens` / `output_tokens`. OpenAI:
        // `prompt_tokens` / `completion_tokens`. Getting this wrong
        // makes the SDK bill 0 tokens.
        let out = openai_to_anthropic_response(&json!({
            "choices": [{"message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 42, "completion_tokens": 17}
        }));
        assert_eq!(out["usage"]["input_tokens"], 42);
        assert_eq!(out["usage"]["output_tokens"], 17);
        assert!(
            out["usage"].get("prompt_tokens").is_none(),
            "must not leak the OpenAI field name"
        );
        assert!(out["usage"].get("completion_tokens").is_none());
    }

    #[test]
    fn stop_sequence_is_null_when_not_set() {
        // The field is REQUIRED in the spec; when unset it's `null`,
        // not missing. Dropping the key entirely confuses strict parsers.
        let out = openai_to_anthropic_response(&json!({
            "choices": [{"message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}]
        }));
        assert_eq!(out["stop_sequence"], serde_json::Value::Null);
    }

    #[test]
    fn id_falls_back_to_msg_proxy_when_upstream_omits_it() {
        // OpenAI responses always carry `id`; self-hosted providers
        // (Ollama, vLLM) sometimes don't. We synthesise one rather
        // than emitting `null` so Anthropic SDKs don't choke.
        let out = openai_to_anthropic_response(&json!({
            "choices": [{"message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}]
        }));
        let id = out["id"].as_str().expect("id is a string");
        assert!(!id.is_empty(), "id must not be empty");
    }

    // ---- openai_chunk_to_anthropic_events --------------------------------

    #[test]
    fn first_chunk_emits_message_start() {
        let chunk = r#"{"id":"c1","model":"gpt-4o","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}"#;
        let mut state = AnthropicStreamState::default();
        let events = openai_chunk_to_anthropic_events(chunk, &mut state, "claude-3");
        // First non-empty delta must kick off a `message_start` event.
        let (event_type, body) = events
            .iter()
            .find(|(t, _)| t == "message_start")
            .expect("message_start should be the first event on a fresh stream");
        assert_eq!(event_type, "message_start");
        let parsed: serde_json::Value = serde_json::from_str(body).unwrap();
        assert_eq!(parsed["message"]["type"], "message");
        assert_eq!(parsed["message"]["role"], "assistant");
        assert_eq!(parsed["type"], "message_start");
        assert!(
            state.started,
            "state.started must be true after first chunk"
        );
    }

    #[test]
    fn content_delta_is_emitted_as_content_block_delta() {
        let chunk = r#"{"id":"c1","choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":null}]}"#;
        let mut state = AnthropicStreamState::default();
        let events = openai_chunk_to_anthropic_events(chunk, &mut state, "claude");
        // There should be a `content_block_delta` carrying a
        // `text_delta` inner type — the exact shape anthropic-sdk
        // readers expect.
        let (_, body) = events
            .iter()
            .find(|(t, _)| t == "content_block_delta")
            .expect("content_block_delta should fire for a text delta");
        let parsed: serde_json::Value = serde_json::from_str(body).unwrap();
        assert_eq!(parsed["type"], "content_block_delta");
        assert_eq!(parsed["delta"]["type"], "text_delta");
        assert_eq!(parsed["delta"]["text"], "hello");
    }

    #[test]
    fn content_block_start_precedes_deltas() {
        // Anthropic requires `content_block_start` before any deltas
        // in the same block. Test order explicitly so a regression in
        // state-machine logic shows up as a failing assertion.
        let chunk = r#"{"id":"c1","choices":[{"index":0,"delta":{"role":"assistant","content":"hi"},"finish_reason":null}]}"#;
        let mut state = AnthropicStreamState::default();
        let events = openai_chunk_to_anthropic_events(chunk, &mut state, "claude");
        let start_pos = events
            .iter()
            .position(|(t, _)| t == "content_block_start")
            .expect("content_block_start must be present");
        let delta_pos = events
            .iter()
            .position(|(t, _)| t == "content_block_delta")
            .expect("content_block_delta must be present");
        assert!(
            start_pos < delta_pos,
            "content_block_start ({start_pos}) must precede delta ({delta_pos})",
        );
    }

    #[test]
    fn subsequent_chunk_does_not_re_emit_message_start() {
        // Once the stream has started, we must not emit another
        // message_start — that would produce two "messages" in the
        // SDK's view of the stream and break state tracking.
        let first = r#"{"id":"c1","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}"#;
        let second = r#"{"id":"c1","choices":[{"index":0,"delta":{"content":"more"},"finish_reason":null}]}"#;

        let mut state = AnthropicStreamState::default();
        let _ = openai_chunk_to_anthropic_events(first, &mut state, "claude");
        let events = openai_chunk_to_anthropic_events(second, &mut state, "claude");
        assert!(
            events.iter().all(|(t, _)| t != "message_start"),
            "second chunk must not contain message_start: {events:?}",
        );
    }

    // ---- tool_use streaming translation (regression for the malformed
    //      Anthropic tool stream) -------------------------------------------

    /// Helper: parse an emitted (event_type, json) pair's body.
    fn body_of(events: &[(String, String)], event_type: &str) -> Vec<serde_json::Value> {
        events
            .iter()
            .filter(|(t, _)| t == event_type)
            .map(|(_, b)| serde_json::from_str(b).unwrap())
            .collect()
    }

    #[test]
    fn tool_call_opens_tool_use_block_with_id_and_name() {
        // OpenAI's first tool-call delta carries id + function.name.
        let mut state = AnthropicStreamState::default();
        let first = r#"{"id":"c1","choices":[{"index":0,"delta":{"role":"assistant","content":null,"tool_calls":[{"index":0,"id":"call_abc","type":"function","function":{"name":"get_weather","arguments":""}}]},"finish_reason":null}]}"#;
        let events = openai_chunk_to_anthropic_events(first, &mut state, "claude");

        // A tool_use content_block_start must be emitted with id + name, at
        // index 1 (text holds index 0).
        let starts = body_of(&events, "content_block_start");
        let tool_start = starts
            .iter()
            .find(|b| b["content_block"]["type"] == "tool_use")
            .expect("a tool_use content_block_start must be emitted");
        assert_eq!(
            tool_start["index"], 1,
            "tool block must not collide with text block 0"
        );
        assert_eq!(tool_start["content_block"]["id"], "call_abc");
        assert_eq!(tool_start["content_block"]["name"], "get_weather");
    }

    #[test]
    fn tool_call_arguments_stream_as_input_json_delta_on_tool_index() {
        let mut state = AnthropicStreamState::default();
        let first = r#"{"id":"c1","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_abc","type":"function","function":{"name":"f","arguments":""}}]},"finish_reason":null}]}"#;
        let _ = openai_chunk_to_anthropic_events(first, &mut state, "claude");
        // Subsequent chunk: only argument fragments, no id/name.
        let next = r#"{"id":"c1","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\"city\":\"NYC\"}"}}]},"finish_reason":null}]}"#;
        let events = openai_chunk_to_anthropic_events(next, &mut state, "claude");

        // No second content_block_start for the same call.
        assert!(
            body_of(&events, "content_block_start").is_empty(),
            "a repeated tool-call index must not re-open a block",
        );
        let deltas = body_of(&events, "content_block_delta");
        let json_delta = deltas
            .iter()
            .find(|b| b["delta"]["type"] == "input_json_delta")
            .expect("input_json_delta must be emitted for tool arguments");
        assert_eq!(
            json_delta["index"], 1,
            "delta must target the tool block index, not 0"
        );
        assert_eq!(json_delta["delta"]["partial_json"], "{\"city\":\"NYC\"}");
    }

    #[test]
    fn tool_call_finish_stops_text_and_tool_blocks() {
        let mut state = AnthropicStreamState::default();
        let first = r#"{"id":"c1","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_a","type":"function","function":{"name":"f","arguments":"{}"}}]},"finish_reason":null}]}"#;
        let _ = openai_chunk_to_anthropic_events(first, &mut state, "claude");
        let finish =
            r#"{"id":"c1","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}"#;
        let events = openai_chunk_to_anthropic_events(finish, &mut state, "claude");

        // Both block 0 (text) and block 1 (tool_use) must be stopped.
        let stops: Vec<i64> = body_of(&events, "content_block_stop")
            .iter()
            .map(|b| b["index"].as_i64().unwrap())
            .collect();
        assert!(
            stops.contains(&0),
            "text block (0) must be stopped: {stops:?}"
        );
        assert!(
            stops.contains(&1),
            "tool_use block (1) must be stopped: {stops:?}"
        );

        // stop_reason maps tool_calls → tool_use.
        let mdelta = body_of(&events, "message_delta");
        assert_eq!(mdelta[0]["delta"]["stop_reason"], "tool_use");
    }

    #[test]
    fn parallel_tool_calls_get_distinct_block_indices() {
        let mut state = AnthropicStreamState::default();
        // Two tool calls arrive in one chunk at indices 0 and 1.
        let chunk = r#"{"id":"c1","choices":[{"index":0,"delta":{"tool_calls":[
            {"index":0,"id":"call_a","type":"function","function":{"name":"f","arguments":""}},
            {"index":1,"id":"call_b","type":"function","function":{"name":"g","arguments":""}}
        ]},"finish_reason":null}]}"#;
        let events = openai_chunk_to_anthropic_events(chunk, &mut state, "claude");
        // Two distinct Anthropic block indices, 1 and 2.
        let mut idxs: Vec<i64> = body_of(&events, "content_block_start")
            .into_iter()
            .filter(|b| b["content_block"]["type"] == "tool_use")
            .map(|b| b["index"].as_i64().unwrap())
            .collect();
        idxs.sort_unstable();
        assert_eq!(
            idxs,
            vec![1, 2],
            "parallel tool calls must occupy distinct blocks"
        );
    }
}
