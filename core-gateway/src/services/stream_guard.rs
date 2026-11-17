// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Sync buffer-and-release output guardrails for **streamed** LLM
//! responses (the `chat_output` surface over SSE).
//!
//! `routes/llm.rs` has *async-interrupt* streaming: scan
//! the accumulated text on a debounce and **terminate** the stream on a
//! Block verdict. That stops future tokens but can't recall already-sent
//! ones, and can't *redact* — you can't un-send a token.
//!
//! This module adds the **sync buffer-and-release** posture
//! that the industry standard (AWS Bedrock "synchronous", NVIDIA NeMo
//! streaming output rails) uses for redaction: hold the output back in
//! small *segments*, run the full output orchestrator on each segment
//! *before* releasing it, and emit a **reconstructed** SSE chunk
//! carrying the clean-or-redacted text. The cost is ~one segment of
//! added latency; the payoff is that PII/secrets never reach the client.
//!
//! The hard logic — segmentation with an overlap hold-back so an entity
//! split across token frames (a card number arriving as `4242 ` then
//! `4242 4242 4242`) is never cut mid-entity, plus chunk reconstruction
//! — lives here as pure, unit-tested functions. [`StreamGuard::process`]
//! is the one entry point both SSE branches call, so the per-frame
//! policy lives in exactly one place.

use serde_json::{Value, json};

use crate::models::db::GuardrailConfig;
use crate::services::guardrails::{self, CheckOutcome};

/// Append the human-readable, on-the-wire text of one parsed OpenAI
/// streaming chunk — assistant `content` plus any tool-call name /
/// arguments — to `out`.
///
/// Shared by every streaming route so the proxy-log response body is
/// rebuilt from the **post-guard** frames actually sent to the client
/// (redacted where redaction fired), and therefore mirrors the wire
/// exactly instead of the unredacted upstream tokens. Centralised here
/// (rather than copied per route) so the renderers can't drift.
pub fn append_chunk_loggable_text_value(chunk: &Value, out: &mut String) {
    let delta = chunk
        .get("choices")
        .and_then(|c| c.get(0))
        .and_then(|c| c.get("delta"));

    if let Some(text) = delta
        .and_then(|d| d.get("content"))
        .and_then(|c| c.as_str())
    {
        out.push_str(text);
    }

    if let Some(tool_calls) = delta
        .and_then(|d| d.get("tool_calls"))
        .and_then(|t| t.as_array())
    {
        for tc in tool_calls {
            let func = tc.get("function");
            if let Some(name) = func.and_then(|f| f.get("name")).and_then(|n| n.as_str()) {
                out.push_str("[tool_call: ");
                out.push_str(name);
                out.push_str("] ");
            }
            if let Some(args) = func
                .and_then(|f| f.get("arguments"))
                .and_then(|a| a.as_str())
            {
                out.push_str(args);
            }
        }
    }
}

/// Render one already-serialized OpenAI SSE `data:` payload's loggable
/// text into `out` (parses then delegates to the value renderer).
pub fn append_chunk_loggable_text(data: &str, out: &mut String) {
    if let Ok(v) = serde_json::from_str::<Value>(data) {
        append_chunk_loggable_text_value(&v, out);
    }
}

/// Keep at least this many trailing characters buffered (un-emitted) so
/// a structured-PII entity (card ≤19, email, phone, SSN, IBAN — all
/// shorter than this) that straddles a segment boundary stays whole
/// until it's been checked. Larger = safer against boundary splits,
/// but adds latency; 48 comfortably covers every built-in PII category.
const OVERLAP_CHARS: usize = 48;

/// Release a segment once the emittable region reaches roughly this many
/// bytes (or a newline appears). Bounds the added "time to first
/// released token" to about one segment's worth of generation.
const SEGMENT_TARGET: usize = 200;

/// Pure text segmenter: accumulates streamed text deltas and yields
/// segments at safe boundaries, always holding back an `OVERLAP_CHARS`
/// trailing window so a boundary-spanning entity is never split before
/// it has been checked.
#[derive(Debug, Default)]
pub struct StreamSegmenter {
    pending: String,
}

impl StreamSegmenter {
    pub fn new() -> Self {
        Self {
            pending: String::new(),
        }
    }

    /// Push a text delta; return zero or more segments now safe to
    /// check + emit (the trailing overlap window is retained).
    pub fn push(&mut self, delta: &str) -> Vec<String> {
        if !delta.is_empty() {
            self.pending.push_str(delta);
        }
        let mut segments = Vec::new();
        while let Some(seg) = self.take_one() {
            segments.push(seg);
        }
        segments
    }

    /// At end-of-stream, return whatever remains (no hold-back — there
    /// is no more text coming, so the tail is safe to check + emit).
    pub fn flush(&mut self) -> Option<String> {
        if self.pending.is_empty() {
            None
        } else {
            Some(std::mem::take(&mut self.pending))
        }
    }

    fn take_one(&mut self) -> Option<String> {
        let char_count = self.pending.chars().count();
        if char_count <= OVERLAP_CHARS {
            return None;
        }
        // Byte index where the held-back overlap tail begins.
        let keep_from = self
            .pending
            .char_indices()
            .nth(char_count - OVERLAP_CHARS)
            .map(|(i, _)| i)
            .unwrap_or(self.pending.len());
        let emittable = &self.pending[..keep_from];
        let has_newline = emittable.contains('\n');
        // Only release once there's a worthwhile chunk OR a hard line
        // boundary (keeps latency low for short, chatty replies).
        if emittable.len() < SEGMENT_TARGET && !has_newline {
            return None;
        }
        // Cut at the last whitespace so we never split a word/entity in
        // the middle; include the whitespace char in the released
        // segment. If there's no whitespace (one very long token —
        // never a PII entity), release the whole emittable region.
        let cut = match emittable.rfind(|c: char| c.is_whitespace()) {
            Some(i) => {
                let ws_len = emittable[i..]
                    .chars()
                    .next()
                    .map(|c| c.len_utf8())
                    .unwrap_or(1);
                i + ws_len
            }
            None => emittable.len(),
        };
        if cut == 0 {
            return None;
        }
        let seg: String = self.pending[..cut].to_string();
        self.pending.drain(..cut);
        Some(seg)
    }
}

/// Stable header fields harvested from the first upstream chunk so
/// reconstructed content chunks carry the same `id` / `model` /
/// `created` the client already saw (some SDKs assert on a stable id).
#[derive(Debug, Clone)]
pub struct ChunkTemplate {
    pub id: String,
    pub created: i64,
    pub model: String,
}

impl ChunkTemplate {
    pub fn from_chunk(json: &str) -> Option<Self> {
        let v: Value = serde_json::from_str(json).ok()?;
        Some(Self {
            id: v
                .get("id")
                .and_then(|x| x.as_str())
                .unwrap_or("chatcmpl-stream")
                .to_string(),
            created: v.get("created").and_then(|x| x.as_i64()).unwrap_or(0),
            model: v
                .get("model")
                .and_then(|x| x.as_str())
                .unwrap_or("")
                .to_string(),
        })
    }

    fn fallback() -> Self {
        Self {
            id: "chatcmpl-stream".to_string(),
            created: 0,
            model: String::new(),
        }
    }
}

/// Build the SSE `data:` payload (JSON only — caller wraps in the SSE
/// event) for a reconstructed OpenAI `chat.completion.chunk` carrying
/// `text` as the content delta.
pub fn content_chunk(template: &ChunkTemplate, text: &str) -> String {
    json!({
        "id": template.id,
        "object": "chat.completion.chunk",
        "created": template.created,
        "model": template.model,
        "choices": [{
            "index": 0,
            "delta": { "content": text },
            "finish_reason": Value::Null,
        }],
    })
    .to_string()
}

/// What a single upstream chunk carries, for routing decisions.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct ChunkParts {
    /// `choices[0].delta.content` text (empty if none).
    pub content: String,
    /// The chunk streams tool-call argument deltas — out of scope for
    /// text redaction, so the guard falls back to verbatim passthrough.
    pub has_tool_calls: bool,
    /// The chunk carries structural fields that must be forwarded even
    /// in sync mode (finish_reason set, role-only first delta, usage).
    pub structural: bool,
}

/// Inspect an OpenAI-shaped chunk's `choices[0]`.
pub fn analyze_chunk(json: &str) -> ChunkParts {
    let v: Value = match serde_json::from_str(json) {
        Ok(v) => v,
        Err(_) => return ChunkParts::default(),
    };
    let choice = v
        .get("choices")
        .and_then(|c| c.as_array())
        .and_then(|a| a.first());
    let Some(choice) = choice else {
        // No choices — e.g. a usage-only chunk. Treat as structural.
        let has_usage = v.get("usage").map(|u| !u.is_null()).unwrap_or(false);
        return ChunkParts {
            content: String::new(),
            has_tool_calls: false,
            structural: has_usage,
        };
    };
    let delta = choice.get("delta");
    let content = delta
        .and_then(|d| d.get("content"))
        .and_then(|c| c.as_str())
        .unwrap_or("")
        .to_string();
    let has_tool_calls = delta
        .and_then(|d| d.get("tool_calls"))
        .map(|t| !t.is_null())
        .unwrap_or(false);
    let has_role = delta
        .and_then(|d| d.get("role"))
        .map(|r| !r.is_null())
        .unwrap_or(false);
    let has_finish = choice
        .get("finish_reason")
        .map(|f| !f.is_null())
        .unwrap_or(false);
    let has_usage = v.get("usage").map(|u| !u.is_null()).unwrap_or(false);
    ChunkParts {
        content,
        has_tool_calls,
        structural: has_role || has_finish || has_usage || has_tool_calls,
    }
}

/// Return the chunk with `choices[].delta.content` removed, so the
/// structural fields (role / finish_reason / tool_calls / usage) can be
/// forwarded without re-emitting content the segmenter already handled.
pub fn strip_content(json: &str) -> String {
    let mut v: Value = match serde_json::from_str(json) {
        Ok(v) => v,
        Err(_) => return json.to_string(),
    };
    if let Some(choices) = v.get_mut("choices").and_then(|c| c.as_array_mut()) {
        for choice in choices {
            if let Some(delta) = choice.get_mut("delta").and_then(|d| d.as_object_mut()) {
                delta.remove("content");
            }
        }
    }
    v.to_string()
}

/// Remove reasoning (`reasoning_content`, `thinking_blocks`) from a chunk's
/// deltas. `None` when it carries none, which is nearly every chunk, so the
/// substring test keeps a token stream from parsing each one twice.
///
/// Thinking blocks with no readable text (`display: "omitted"`, redacted
/// thinking) are kept: the client cannot read them and needs them unchanged to
/// replay the turn in a tool loop.
pub fn strip_reasoning(json: &str) -> Option<String> {
    if !json.contains("reasoning_content") && !json.contains("thinking_blocks") {
        return None;
    }
    let mut v: Value = serde_json::from_str(json).ok()?;
    let mut found = false;
    for choice in v.get_mut("choices")?.as_array_mut()? {
        if let Some(delta) = choice.get_mut("delta").and_then(|d| d.as_object_mut()) {
            found |= delta.remove("reasoning_content").is_some();
            if delta
                .get("thinking_blocks")
                .is_some_and(thinking_blocks_are_readable)
            {
                delta.remove("thinking_blocks");
                found = true;
            }
        }
    }
    found.then(|| v.to_string())
}

/// Whether any of these Anthropic thinking blocks has readable text. A block
/// with empty text (`display: "omitted"`) or a `redacted_thinking` block
/// carries only what the client cannot read.
pub fn thinking_blocks_are_readable(blocks: &Value) -> bool {
    blocks.as_array().is_some_and(|blocks| {
        blocks.iter().any(|b| {
            b.get("thinking")
                .and_then(Value::as_str)
                .is_some_and(|t| !t.is_empty())
        })
    })
}

/// Responses API stream events whose payload is the model's reasoning
/// (`response.reasoning_summary_part.*`, `response.reasoning_summary_text.*`,
/// `response.reasoning_text.*`). Output checks never see them.
pub fn is_responses_reasoning_event(event_name: &str) -> bool {
    event_name.starts_with("response.reasoning")
}

/// Whether a Responses API body has a `reasoning` item with readable reasoning.
pub fn responses_carries_reasoning(body: &Value) -> bool {
    body.get("output")
        .and_then(Value::as_array)
        .is_some_and(|output| {
            output.iter().any(|item| {
                item.get("type").and_then(Value::as_str) == Some("reasoning")
                    && (item.get("content").is_some()
                        || item
                            .get("summary")
                            .and_then(Value::as_array)
                            .is_some_and(|s| !s.is_empty()))
            })
        })
}

/// Remove the readable reasoning from a Responses API value: a response body
/// (`output[]`), a stream event that carries the response (`response.output[]`)
/// or one output item (`item`). A `reasoning` item keeps its `id` and
/// `encrypted_content`, which the client cannot read and needs to send the
/// item back on the next turn; its `summary` becomes `[]` and its `content`
/// goes. A response that lost reasoning says so (`reasoning_withheld`).
/// Returns whether anything was removed.
pub fn strip_responses_reasoning(value: &mut Value) -> bool {
    fn strip_item(item: &mut Value) -> bool {
        let Some(item) = item.as_object_mut() else {
            return false;
        };
        if item.get("type").and_then(Value::as_str) != Some("reasoning") {
            return false;
        }
        let had_summary = item
            .get("summary")
            .and_then(Value::as_array)
            .is_some_and(|s| !s.is_empty());
        if had_summary {
            item.insert("summary".into(), Value::Array(Vec::new()));
        }
        let had_content = item.remove("content").is_some();
        had_summary || had_content
    }
    fn strip_response(response: &mut Value) -> bool {
        let Some(output) = response.get_mut("output").and_then(Value::as_array_mut) else {
            return false;
        };
        let mut found = false;
        for item in output.iter_mut() {
            found |= strip_item(item);
        }
        if found && let Some(obj) = response.as_object_mut() {
            obj.insert("reasoning_withheld".into(), Value::Bool(true));
        }
        found
    }
    let mut found = strip_response(value);
    if let Some(response) = value.get_mut("response") {
        found |= strip_response(response);
    }
    if let Some(item) = value.get_mut("item") {
        found |= strip_item(item);
    }
    found
}

/// The reasoning text a chunk carries, if any.
fn chunk_reasoning(json: &str) -> Option<String> {
    if !json.contains("reasoning_content") {
        return None;
    }
    let v: Value = serde_json::from_str(json).ok()?;
    v.pointer("/choices/0/delta/reasoning_content")?
        .as_str()
        .map(str::to_string)
}

/// Visit, in a fixed order, every place a Responses stream event repeats
/// the answer's text: the `text` of each `output_text` content part, at any
/// depth (`response.completed`, `output_item.done`, `content_part.done`),
/// and the top-level `text` of an `output_text.done` event.
fn visit_responses_repeated_texts(value: &mut Value, visit: &mut dyn FnMut(&mut String)) {
    match value {
        Value::Object(map) => {
            let kind = map.get("type").and_then(Value::as_str);
            let is_text_holder = matches!(kind, Some("output_text" | "response.output_text.done"));
            if is_text_holder && let Some(Value::String(text)) = map.get_mut("text") {
                visit(text);
            }
            for (_, child) in map.iter_mut() {
                visit_responses_repeated_texts(child, visit);
            }
        }
        Value::Array(items) => {
            for item in items.iter_mut() {
                visit_responses_repeated_texts(item, visit);
            }
        }
        _ => {}
    }
}

/// The answer texts a Responses stream event repeats, in visiting order.
/// Empty for an event that repeats none (the common case: a delta).
pub fn responses_repeated_texts(value: &Value) -> Vec<String> {
    let mut texts = Vec::new();
    // The visitor needs `&mut`; the clone is of one event that carries text.
    let mut scratch = value.clone();
    visit_responses_repeated_texts(&mut scratch, &mut |t| texts.push(t.clone()));
    texts
}

/// Put `replacements` where [`responses_repeated_texts`] found the texts,
/// in the same order. `false`, and the event is left untouched, when the
/// number does not match what the event holds.
pub fn replace_responses_repeated_texts(value: &mut Value, replacements: Vec<String>) -> bool {
    if responses_repeated_texts(value).len() != replacements.len() {
        return false;
    }
    let mut next = replacements.into_iter();
    visit_responses_repeated_texts(value, &mut |t| {
        if let Some(replacement) = next.next() {
            *t = replacement;
        }
    });
    true
}

/// Decide the streaming posture for the whole config set.
///
/// A config that can **withhold or modify** content on `chat_output` (any
/// block-OR-redact output check) ALWAYS runs sync — this is not
/// operator-overridable. Async-interrupt forwards each content chunk
/// verbatim and only scans on a 256-byte debounce (with no final flush
/// check), so a block-only check leaks the offending chunk — and never even
/// inspects content shorter than the debounce. A block that leaks is not a
/// block, so an explicit `streaming_output_mode: async` on such a config is
/// ignored (SECURITY: this was the downgrade path).
///
/// For configs with nothing to withhold, the per-config
/// `streaming_output_mode` selects the posture: `sync` forces
/// buffer-and-release; `async` / `auto` / None stay on the faster
/// async-interrupt path. The set runs sync if ANY config requires it.
pub fn needs_sync(configs: &[GuardrailConfig]) -> bool {
    configs.iter().any(|c| {
        // A config that can withhold or modify content on output MUST run
        // sync — async-interrupt forwards each chunk verbatim before the
        // verdict, so the offending token is already on the wire. A block
        // that leaks is not a block, so this is not operator-overridable:
        // an explicit `async` on a block/redact output check is ignored.
        let enforces_on_output =
            c.chat_output_enabled.unwrap_or(false) && config_enforces_on_output(c);
        if enforces_on_output {
            return true;
        }
        // Otherwise the mode selects the posture. `async` opts a warn-only
        // config out of buffering; `sync` forces it on; `auto`/None stay on
        // the faster async-interrupt path (nothing to withhold).
        matches!(c.streaming_output_mode.as_deref(), Some("sync"))
    })
}

/// True when the config has any output check that would **withhold (block)
/// or modify (redact)** content on `chat_output` — the cases that require
/// buffer-before-release in streaming. Warn-only checks don't alter the
/// stream, so they stay on the faster async-interrupt path.
fn config_enforces_on_output(c: &GuardrailConfig) -> bool {
    let nonempty = |v: &Option<serde_json::Value>| {
        v.as_ref()
            .and_then(|x| x.as_array())
            .is_some_and(|a| !a.is_empty())
    };
    let enforces = |a: &Option<String>| matches!(a.as_deref(), Some("block") | Some("redact"));

    // Inherently blocking checks (match = deny; no per-check action field).
    nonempty(&c.banned_words)
        || nonempty(&c.banned_patterns)
        || c.secrets_detection_enabled
        // Action-driven checks: only when set to block/redact (not warn).
        || (c.pii_detection_enabled && enforces(&c.pii_action))
        || (c.toxic_content_enabled && enforces(&c.toxic_content_action))
        || (c.prompt_injection_enabled && enforces(&c.prompt_injection_action))
        || (c.jailbreak_detection_enabled && enforces(&c.jailbreak_action))
}

/// What the caller should do with a processed chunk.
#[derive(Debug)]
pub enum StreamAction {
    /// Forward these `data:` payloads (already reconstructed where
    /// needed), in order, then continue.
    Emit(Vec<String>),
    /// A Block verdict fired — emit the guardrail error event + `[DONE]`
    /// and terminate the stream.
    Block(String),
}

/// Result of [`StreamGuard::process_text`] / [`StreamGuard::finish_text`]
/// for non-OpenAI wire formats — carries **plain text** segments (the
/// caller re-injects them into its own envelope) rather than
/// reconstructed OpenAI frames.
pub enum TextAction {
    /// Redacted text segments to emit (sync mode); empty in async mode,
    /// where the caller forwards the original event verbatim.
    Emit(Vec<String>),
    /// A Block verdict fired — terminate the stream.
    Block(String),
}

/// How a guarded stream ended, as its outcome row must record it.
#[derive(Debug, Clone, PartialEq)]
pub enum StreamOutcome {
    /// Ran to its end (or the client left): nothing to stamp.
    Completed,
    /// An operator stopped the run mid-stream.
    RunAborted,
    /// An output guardrail stopped the content.
    GuardrailBlocked(crate::services::guardrails::GuardrailViolation),
}

impl StreamOutcome {
    /// Stamp the outcome on the stream's own outcome row — the row that
    /// names the run and step and carries the tokens billed before the
    /// stream stopped. An abort is `run_aborted` (sealed `denied`, the same
    /// subtype and verdict as the pre-flight abort); a guardrail block is the
    /// violation (sealed `blocked`).
    pub fn stamp(&self, log: &mut crate::models::db::ProxyLogInsert) {
        match self {
            StreamOutcome::Completed => {}
            StreamOutcome::RunAborted => {
                log.proxy_subtype = Some(crate::constants::proxy_subtype::RUN_ABORTED.to_string());
                log.success = false;
                log.http_status_code = Some(403);
                log.error_message =
                    Some(crate::services::run_intervention::ABORTED_MESSAGE.to_string());
            }
            StreamOutcome::GuardrailBlocked(v) => {
                crate::services::llm_refusal::stamp_guardrail_block(log, v)
            }
        }
    }
}

/// Per-stream output-guardrail state machine. One instance per streamed
/// response; both SSE branches (native OpenAI + Anthropic-normalized,
/// which is already OpenAI-shaped) route every content chunk through
/// [`process`](Self::process).
pub struct StreamGuard {
    configs: Vec<GuardrailConfig>,
    http_client: reqwest::Client,
    sync: bool,
    seg: StreamSegmenter,
    template: Option<ChunkTemplate>,
    /// Once a tool-call stream is seen we fall back to verbatim
    /// passthrough for the rest of the response (redacting interleaved
    /// tool-call argument deltas is out of scope for this phase).
    fell_back: bool,
    /// Byte length of accumulated text at the last async-mode check
    /// (debounce so external detectors aren't called per token).
    last_check_len: usize,
    /// Running accumulated text (async-mode checks scan this).
    accumulated: String,
    /// The violation that stopped the stream, once one has. The stream's
    /// own outcome row is its block row (the gateway stopped the content
    /// after the provider started billing it), so the surface stamps this
    /// on that row ([`StreamOutcome::stamp`]) — which config, which check,
    /// on the run the row already names.
    blocked: std::sync::OnceLock<crate::services::guardrails::GuardrailViolation>,
    /// `(tenant_id, root_task_id)` of the run this stream is a step of —
    /// armed by [`for_run`](Self::for_run). An operator can stop a run
    /// mid-stream; [`run_aborted`](Self::run_aborted) is the
    /// one poll every streaming surface asks.
    run: Option<(String, String)>,
    /// Events seen by [`run_aborted`](Self::run_aborted), for its cadence.
    abort_polls: usize,
    /// The run was aborted while this stream was open. Recorded on the
    /// stream's outcome row by [`stamp_outcome`](Self::stamp_outcome).
    aborted: bool,
    /// Reasoning text actually put on the wire, for the audit row. Kept apart
    /// from the answer: the answer also feeds the semantic cache.
    reasoning: String,
    /// Reasoning was removed because an output policy can block or redact.
    reasoning_withheld: bool,
}

/// How often the streaming loop asks whether the run has been aborted.
///
/// A token stream emits hundreds of chunks a second and the check is a
/// lock-free map read, so asking every chunk would be free but pointless.
/// Every 32 chunks bounds the delay to well under a second on any real
/// stream while keeping the cost invisible.
pub const ABORT_POLL_CHUNKS: usize = 32;

/// Async-mode debounce: re-scan after this many new bytes.
const ASYNC_CHECK_INTERVAL: usize = 256;

/// How much of the accumulated response one async-mode scan covers.
///
/// The debounce used to hand the detectors the *whole* accumulation every
/// 256 bytes, so an N-byte answer cost N/256 scans of ~N/2 bytes each —
/// quadratic, and for a Presidio/Bedrock/moderation config that is one
/// outbound HTTP call per scan carrying an ever-growing body. A 100 KB
/// answer shipped ~20 MB to the detector to look at 100 KB of text.
///
/// A response that fits in this window is still scanned exactly as before
/// (the window covers all of it), so nothing changes for the overwhelming
/// majority of completions. Past it, each scan sees the trailing 8 KB
/// instead of everything: an entity or phrase is still whole in some
/// window — see [`scan_window_start`] for the overlap that guarantees it —
/// but a detector that only fires on content correlated across more than
/// 8 KB of prose no longer sees both ends at once. That is the trade, and
/// it is the async posture only: the sync (block/redact) path segments
/// independently and is untouched.
const ASYNC_SCAN_WINDOW: usize = 8192;

/// Where the next async-mode scan should start in `text`.
///
/// Two floors, whichever reaches further back:
///
/// * `last_check_len - OVERLAP_CHARS×4` — the previous scan's tail, so a
///   pattern that straddles the boundary between two scans is whole in the
///   second one. `OVERLAP_CHARS` is the module's stated bound on a
///   structured entity (card, IBAN, SSN…); ×4 makes it a byte bound that
///   holds for multi-byte UTF-8. This is what bites when a single delta is
///   larger than the window.
/// * `len - ASYNC_SCAN_WINDOW` — keep enough preceding context that a
///   detector is judging prose rather than a 256-byte sliver.
///
/// The result is walked back to a char boundary so slicing is safe.
fn scan_window_start(text: &str, last_check_len: usize) -> usize {
    let overlap_floor = last_check_len.saturating_sub(OVERLAP_CHARS * 4);
    let window_floor = text.len().saturating_sub(ASYNC_SCAN_WINDOW);
    let mut start = overlap_floor.min(window_floor);
    while start > 0 && !text.is_char_boundary(start) {
        start -= 1;
    }
    start
}

impl StreamGuard {
    pub fn new(configs: Vec<GuardrailConfig>, http_client: reqwest::Client) -> Self {
        let sync = needs_sync(&configs);
        Self {
            configs,
            http_client,
            sync,
            seg: StreamSegmenter::new(),
            template: None,
            fell_back: false,
            last_check_len: 0,
            accumulated: String::new(),
            blocked: std::sync::OnceLock::new(),
            run: None,
            abort_polls: 0,
            aborted: false,
            reasoning: String::new(),
            reasoning_withheld: false,
        }
    }

    /// What the audit row says about reasoning: the text that reached the
    /// client, or that it was withheld. Reasoning is not scanned, so where an
    /// output policy can block or redact it reaches neither the client nor the
    /// log, which mirrors the wire.
    pub fn reasoning_record(&self) -> (Option<&str>, bool) {
        (
            (!self.reasoning.is_empty()).then_some(self.reasoning.as_str()),
            self.reasoning_withheld,
        )
    }

    /// Arm the mid-stream abort poll for the run this stream is a step of.
    pub fn for_run(mut self, tenant_id: impl Into<String>, run_id: impl Into<String>) -> Self {
        self.run = Some((tenant_id.into(), run_id.into()));
        self
    }

    /// Has an operator stopped this stream's run? Ask once per upstream
    /// event, before forwarding it. Polls on the first event (an abort that
    /// landed between the pre-flight check and the first byte) and then
    /// every [`ABORT_POLL_CHUNKS`] events. On a hit it records the block on
    /// the intervention ("it actually bit") — the same record a pre-flight
    /// abort makes — and stays `true`: the caller emits its terminal event
    /// and drops the upstream connection, which is what stops the provider
    /// generating, and billing.
    pub async fn run_aborted(&mut self, state: &crate::services::SharedState) -> bool {
        if self.aborted {
            return true;
        }
        let Some((tenant_id, run_id)) = self.run.as_ref() else {
            return false;
        };
        self.abort_polls += 1;
        if self.abort_polls != 1 && !self.should_poll_abort(self.abort_polls) {
            return false;
        }
        if crate::services::run_intervention::check(
            &state.db,
            Some(&state.run_abort_cache),
            tenant_id,
            run_id,
        )
        .await
        .is_some()
        {
            crate::services::run_intervention::record_block(&state.db, tenant_id, run_id).await;
            tracing::warn!(run = %run_id, "run aborted mid-stream — terminating");
            self.aborted = true;
        }
        self.aborted
    }

    /// How the guard ended the stream (see [`StreamOutcome`]).
    pub fn outcome(&self) -> StreamOutcome {
        if self.aborted {
            StreamOutcome::RunAborted
        } else if let Some(v) = self.blocked.get() {
            StreamOutcome::GuardrailBlocked(v.clone())
        } else {
            StreamOutcome::Completed
        }
    }

    /// Record how the guard ended the stream on the stream's own outcome row
    /// ([`StreamOutcome::stamp`]).
    pub fn stamp_outcome(&self, log: &mut crate::models::db::ProxyLogInsert) {
        self.outcome().stamp(log);
    }

    /// Test-only constructor to force a posture regardless of config.
    #[cfg(test)]
    pub fn with_sync(
        sync: bool,
        configs: Vec<GuardrailConfig>,
        http_client: reqwest::Client,
    ) -> Self {
        let mut g = Self::new(configs, http_client);
        g.sync = sync;
        g
    }

    pub fn is_sync(&self) -> bool {
        self.sync
    }

    /// True when there are no output guardrails at all — the caller can
    /// skip the per-chunk machinery entirely and forward verbatim.
    pub fn is_noop(&self) -> bool {
        self.configs.is_empty()
    }

    /// Process one OpenAI-shaped SSE chunk (`data:` JSON, never
    /// `[DONE]`). Returns the payloads to forward, or a Block.
    /// Has an operator stopped this run mid-stream?
    ///
    /// Polled from the streaming loop rather than checked per chunk: the
    /// lookup is a lock-free map read, but a token stream can emit hundreds
    /// of chunks a second and there is no value in asking that often. Every
    /// `ABORT_POLL_CHUNKS` chunks bounds the delay to a fraction of a
    /// second while keeping the cost invisible.
    ///
    /// On a trip the caller emits a terminal event and drops the upstream
    /// connection, which is what stops the provider generating — and
    /// billing. This is the one place abort *can* interrupt work already in
    /// flight, because a stream is a sequence of actions rather than one.
    pub fn should_poll_abort(&self, chunks_seen: usize) -> bool {
        chunks_seen > 0 && chunks_seen.is_multiple_of(ABORT_POLL_CHUNKS)
    }

    pub async fn process(&mut self, chunk_json: &str) -> StreamAction {
        // Reasoning is not scanned, and the verbatim branch below would
        // forward it past a policy that can block or redact the answer.
        let without_reasoning = if self.sync {
            strip_reasoning(chunk_json)
        } else {
            None
        };
        let chunk_json = match without_reasoning.as_deref() {
            Some(stripped) => {
                self.reasoning_withheld = true;
                stripped
            }
            None => {
                if let Some(text) = chunk_reasoning(chunk_json) {
                    self.reasoning.push_str(&text);
                }
                chunk_json
            }
        };
        if self.template.is_none() {
            self.template = ChunkTemplate::from_chunk(chunk_json);
        }
        let parts = analyze_chunk(chunk_json);
        self.accumulated.push_str(&parts.content);

        // No redaction configured (or we've fallen back to passthrough
        // after a tool-call stream): async-interrupt — forward verbatim,
        // scan on a debounce, Block to terminate.
        if !self.sync || self.fell_back || parts.has_tool_calls {
            if parts.has_tool_calls {
                self.fell_back = true;
            }
            let mut payloads = Vec::new();
            // Flush any buffered (sync-mode) content before we switch to
            // verbatim, so nothing is silently dropped.
            if let Some(rem) = self.seg.flush() {
                match self.check(&rem).await {
                    CheckResult::Block(r) => return StreamAction::Block(r),
                    CheckResult::Text(t) => payloads.push(content_chunk(self.template_ref(), &t)),
                }
            }
            payloads.push(chunk_json.to_string());
            if !self.configs.is_empty()
                && self.accumulated.len().saturating_sub(self.last_check_len)
                    >= ASYNC_CHECK_INTERVAL
            {
                let start = scan_window_start(&self.accumulated, self.last_check_len);
                self.last_check_len = self.accumulated.len();
                // Borrowed, not cloned: the old full copy of the response
                // was allocated afresh on every debounce tick.
                if let CheckResult::Block(r) = self.check(&self.accumulated[start..]).await {
                    return StreamAction::Block(r);
                }
            }
            return StreamAction::Emit(payloads);
        }

        // Sync buffer-and-release.
        let mut payloads = Vec::new();
        for seg in self.seg.push(&parts.content) {
            match self.check(&seg).await {
                CheckResult::Block(r) => return StreamAction::Block(r),
                CheckResult::Text(t) => payloads.push(content_chunk(self.template_ref(), &t)),
            }
        }
        // Structural frame (finish/role/usage): flush buffered content
        // first (so all content precedes the finish), then forward the
        // structural fields with content stripped.
        if parts.structural {
            if let Some(rem) = self.seg.flush() {
                match self.check(&rem).await {
                    CheckResult::Block(r) => return StreamAction::Block(r),
                    CheckResult::Text(t) => payloads.push(content_chunk(self.template_ref(), &t)),
                }
            }
            payloads.push(strip_content(chunk_json));
        }
        StreamAction::Emit(payloads)
    }

    /// Flush the segmenter at end-of-stream (covers the held-back tail
    /// that never reached a boundary). Returns trailing payloads or a
    /// Block. Caller invokes this once after the upstream loop ends, in
    /// sync mode, before emitting the final `[DONE]`.
    pub async fn finish(&mut self) -> StreamAction {
        if !self.sync || self.fell_back {
            if let CheckResult::Block(r) = self.check_unscanned_tail().await {
                return StreamAction::Block(r);
            }
            return StreamAction::Emit(vec![]);
        }
        if let Some(rem) = self.seg.flush() {
            match self.check(&rem).await {
                CheckResult::Block(r) => return StreamAction::Block(r),
                CheckResult::Text(t) => {
                    return StreamAction::Emit(vec![content_chunk(self.template_ref(), &t)]);
                }
            }
        }
        StreamAction::Emit(vec![])
    }

    /// Text-level counterpart of [`process`](Self::process) for streaming
    /// wire formats that are **not** OpenAI-shaped — native Anthropic
    /// `content_block_delta.delta.text` and native Gemini
    /// `candidates[].content.parts[].text`. Feeds raw assistant text
    /// through the same segmenter + orchestrator and returns the
    /// **redacted** text segments for the caller to re-inject into its own
    /// envelope. In sync mode this redacts (held-back, segment-boundary);
    /// in async mode it forwards verbatim and scans on a debounce. Returns
    /// [`TextAction::Block`] to terminate.
    ///
    /// In **async** mode the returned `Emit` is empty — the caller already
    /// holds the original event and should forward it verbatim; this call
    /// exists only to drive Block detection. In **sync** mode the caller
    /// must emit one envelope per returned segment (and nothing when the
    /// segment list is empty, i.e. the text is being held back).
    pub async fn process_text(&mut self, text: &str) -> TextAction {
        self.accumulated.push_str(text);

        // No redaction (or fell back): async-interrupt. Caller forwards the
        // original event verbatim; we only scan to decide whether to Block.
        if !self.sync || self.fell_back {
            if !self.configs.is_empty()
                && self.accumulated.len().saturating_sub(self.last_check_len)
                    >= ASYNC_CHECK_INTERVAL
            {
                let start = scan_window_start(&self.accumulated, self.last_check_len);
                self.last_check_len = self.accumulated.len();
                // Sliding window, borrowed — see `process`.
                if let CheckResult::Block(r) = self.check(&self.accumulated[start..]).await {
                    return TextAction::Block(r);
                }
            }
            return TextAction::Emit(vec![]);
        }

        // Sync buffer-and-release: redact each released segment.
        let mut out = Vec::new();
        for seg in self.seg.push(text) {
            match self.check(&seg).await {
                CheckResult::Block(r) => return TextAction::Block(r),
                CheckResult::Text(t) => out.push(t),
            }
        }
        TextAction::Emit(out)
    }

    /// Flush the held-back tail for the text API (sync mode). Mirrors
    /// [`finish`](Self::finish) but returns plain text segments.
    pub async fn finish_text(&mut self) -> TextAction {
        if !self.sync || self.fell_back {
            if let CheckResult::Block(r) = self.check_unscanned_tail().await {
                return TextAction::Block(r);
            }
            return TextAction::Emit(vec![]);
        }
        if let Some(rem) = self.seg.flush() {
            match self.check(&rem).await {
                CheckResult::Block(r) => return TextAction::Block(r),
                CheckResult::Text(t) => return TextAction::Emit(vec![t]),
            }
        }
        TextAction::Emit(vec![])
    }

    /// Check, whole, a text the upstream repeats outside the deltas.
    ///
    /// A Responses stream says the answer more than once: in the
    /// `output_text.delta` events, and again in `output_text.done`,
    /// `content_part.done`, `output_item.done` and `response.completed`.
    /// Where the deltas are being redacted (sync mode) the repeats must
    /// carry the same redactions, or the client receives the original text
    /// one event after its redacted form. Returns the text to send in its
    /// place, or `Block`. Outside sync mode the deltas go out verbatim, so
    /// the repeat does too.
    pub async fn check_repeated_text(&self, text: &str) -> TextAction {
        if !self.sync || self.fell_back {
            return TextAction::Emit(vec![text.to_string()]);
        }
        match self.check(text).await {
            CheckResult::Block(r) => TextAction::Block(r),
            CheckResult::Text(t) => TextAction::Emit(vec![t]),
        }
    }

    fn template_ref(&self) -> &ChunkTemplate {
        // `process` sets `template` on the first chunk; this is only
        // reached after at least one chunk, but stay defensive.
        static FALLBACK: std::sync::OnceLock<ChunkTemplate> = std::sync::OnceLock::new();
        self.template
            .as_ref()
            .unwrap_or_else(|| FALLBACK.get_or_init(ChunkTemplate::fallback))
    }

    /// Async mode scans on a debounce: the text after the last scan has
    /// been forwarded but not checked. At the end of the stream it is, so a
    /// check that only warns sees the whole answer (and what it finds is
    /// recorded, `guardrails::flags`).
    async fn check_unscanned_tail(&mut self) -> CheckResult {
        if self.configs.is_empty() || self.accumulated.len() <= self.last_check_len {
            return CheckResult::Text(String::new());
        }
        let start = scan_window_start(&self.accumulated, self.last_check_len);
        self.last_check_len = self.accumulated.len();
        self.check(&self.accumulated[start..]).await
    }

    async fn check(&self, text: &str) -> CheckResult {
        if self.configs.is_empty() || text.is_empty() {
            return CheckResult::Text(text.to_string());
        }
        match guardrails::check_output(
            text,
            &self.configs,
            crate::constants::guardrail_surface::CHAT_OUTPUT,
            &self.http_client,
        )
        .await
        {
            CheckOutcome::Block(v) => {
                let message = v.message.clone();
                // First block wins: it is the one that stopped the stream.
                let _ = self.blocked.set(v);
                CheckResult::Block(message)
            }
            CheckOutcome::Redacted { content, .. } => CheckResult::Text(content),
            CheckOutcome::Pass | CheckOutcome::Warn { .. } => CheckResult::Text(text.to_string()),
        }
    }
}

enum CheckResult {
    /// Forward this (possibly redacted) text.
    Text(String),
    /// Terminate with this reason.
    Block(String),
}

/// Accumulates OpenAI streaming `tool_calls` deltas into complete tool calls
/// so the post-stream output governance (banned-argument patterns, argument
/// policies, tool-call rate counters) can run on STREAMED responses too.
///
/// Streaming previously bypassed all tool-call governance: `delta.tool_calls`
/// fragments were forwarded verbatim and never scanned/counted, so a client
/// could stream to evade per-group tool-call limits or banned-argument rules.
/// The caller ingests every chunk, then at finish reconstructs a
/// chat-completion response body via [`ToolCallAccumulator::to_response_body`]
/// and feeds it to the same `apply_response_governance` the non-streaming path
/// uses.
#[derive(Default)]
pub struct ToolCallAccumulator {
    /// Indexed by the OpenAI `tool_calls[].index`; grown as needed.
    calls: Vec<ToolCallParts>,
}

#[derive(Default, Clone)]
struct ToolCallParts {
    id: String,
    name: String,
    arguments: String,
}

impl ToolCallAccumulator {
    /// Merge one OpenAI streaming chunk's `delta.tool_calls` into the
    /// accumulator. Non-tool chunks (text deltas, usage, finish) are ignored.
    pub fn ingest_chunk_str(&mut self, data: &str) {
        let Ok(chunk) = serde_json::from_str::<Value>(data) else {
            return;
        };
        self.ingest_chunk(&chunk);
    }

    pub fn ingest_chunk(&mut self, chunk: &Value) {
        let Some(tool_calls) = chunk
            .get("choices")
            .and_then(|c| c.get(0))
            .and_then(|c| c.get("delta"))
            .and_then(|d| d.get("tool_calls"))
            .and_then(|t| t.as_array())
        else {
            return;
        };
        for (pos, tc) in tool_calls.iter().enumerate() {
            // `index` identifies the call across chunks; fall back to array
            // position if a provider omits it.
            let idx = tc
                .get("index")
                .and_then(|i| i.as_u64())
                .map(|i| i as usize)
                .unwrap_or(pos);
            if idx >= self.calls.len() {
                self.calls.resize(idx + 1, ToolCallParts::default());
            }
            let slot = &mut self.calls[idx];
            if let Some(id) = tc.get("id").and_then(|v| v.as_str()) {
                if !id.is_empty() {
                    slot.id = id.to_string();
                }
            }
            if let Some(name) = tc
                .get("function")
                .and_then(|f| f.get("name"))
                .and_then(|n| n.as_str())
            {
                if !name.is_empty() {
                    slot.name = name.to_string();
                }
            }
            if let Some(args) = tc
                .get("function")
                .and_then(|f| f.get("arguments"))
                .and_then(|a| a.as_str())
            {
                slot.arguments.push_str(args);
            }
        }
    }

    pub fn is_empty(&self) -> bool {
        self.calls.is_empty()
    }

    /// Reconstruct a minimal OpenAI chat-completion response body carrying the
    /// accumulated tool calls, shaped exactly like the non-streaming response
    /// so `apply_response_governance` / `count_response_tool_calls` see the
    /// same structure.
    pub fn to_response_body(&self, finish_reason: Option<&str>) -> Value {
        let tool_calls: Vec<Value> = self
            .calls
            .iter()
            .enumerate()
            .map(|(i, c)| {
                json!({
                    "index": i,
                    "id": c.id,
                    "type": "function",
                    "function": { "name": c.name, "arguments": c.arguments },
                })
            })
            .collect();
        json!({
            "choices": [{
                "index": 0,
                "message": { "role": "assistant", "content": null, "tool_calls": tool_calls },
                "finish_reason": finish_reason.unwrap_or("tool_calls"),
            }]
        })
    }
}

#[cfg(test)]
mod tool_acc_tests {
    use super::*;

    #[test]
    fn empty_when_no_tool_calls() {
        let mut acc = ToolCallAccumulator::default();
        acc.ingest_chunk_str(r#"{"choices":[{"delta":{"content":"hi"}}]}"#);
        acc.ingest_chunk_str(r#"{"choices":[{"delta":{},"finish_reason":"stop"}]}"#);
        assert!(acc.is_empty());
    }

    #[test]
    fn accumulates_split_argument_fragments() {
        let mut acc = ToolCallAccumulator::default();
        // First delta carries id + name + partial args; later deltas append.
        acc.ingest_chunk_str(
            r#"{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_a","type":"function","function":{"name":"send_email","arguments":"{\"to\":"}}]}}]}"#,
        );
        acc.ingest_chunk_str(
            r#"{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\"x@y.z\"}"}}]}}]}"#,
        );
        assert!(!acc.is_empty());
        let body = acc.to_response_body(Some("tool_calls"));
        let tc = &body["choices"][0]["message"]["tool_calls"][0];
        assert_eq!(tc["id"], "call_a");
        assert_eq!(tc["function"]["name"], "send_email");
        assert_eq!(tc["function"]["arguments"], "{\"to\":\"x@y.z\"}");
        assert_eq!(body["choices"][0]["finish_reason"], "tool_calls");
    }

    #[test]
    fn handles_parallel_tool_calls_by_index() {
        let mut acc = ToolCallAccumulator::default();
        acc.ingest_chunk_str(
            r#"{"choices":[{"delta":{"tool_calls":[
                {"index":0,"id":"a","type":"function","function":{"name":"f","arguments":"{}"}},
                {"index":1,"id":"b","type":"function","function":{"name":"g","arguments":"{}"}}
            ]}}]}"#,
        );
        let body = acc.to_response_body(Some("tool_calls"));
        let calls = body["choices"][0]["message"]["tool_calls"]
            .as_array()
            .unwrap();
        assert_eq!(calls.len(), 2);
        assert_eq!(calls[0]["id"], "a");
        assert_eq!(calls[1]["id"], "b");
    }

    #[test]
    fn reconstructed_body_is_countable_by_governance() {
        // Shape must match what count_response_tool_calls / the banned-arg
        // check expect (choices[].message.tool_calls[].function.arguments).
        let mut acc = ToolCallAccumulator::default();
        acc.ingest_chunk_str(
            r#"{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"x","function":{"name":"t","arguments":"{\"a\":1}"}}]}}]}"#,
        );
        let body = acc.to_response_body(None);
        let args = body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
            .as_str()
            .unwrap();
        assert_eq!(args, "{\"a\":1}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn seg_all(deltas: &[&str]) -> (Vec<String>, Option<String>) {
        let mut s = StreamSegmenter::new();
        let mut out = Vec::new();
        for d in deltas {
            out.extend(s.push(d));
        }
        let tail = s.flush();
        (out, tail)
    }

    #[test]
    fn short_text_held_until_flush() {
        // Below the overlap threshold: nothing released until flush.
        let (segs, tail) = seg_all(&["Hi ", "there"]);
        assert!(segs.is_empty(), "short text should not release early");
        assert_eq!(tail.as_deref(), Some("Hi there"));
    }

    #[test]
    fn flush_reassembles_full_text() {
        let words: Vec<String> = (0..60).map(|i| format!("word{i} ")).collect();
        let refs: Vec<&str> = words.iter().map(|s| s.as_str()).collect();
        let (segs, tail) = seg_all(&refs);
        let mut joined = segs.concat();
        if let Some(t) = tail {
            joined.push_str(&t);
        }
        assert_eq!(joined, words.concat(), "no text may be lost or duplicated");
    }

    #[test]
    fn segments_cut_on_whitespace_not_mid_word() {
        let big = "alpha ".repeat(60); // > SEGMENT_TARGET
        let (segs, _tail) = seg_all(&[&big]);
        assert!(!segs.is_empty(), "long text must release segments");
        for s in &segs {
            // every released segment ends at a whitespace boundary
            assert!(
                s.ends_with(char::is_whitespace),
                "segment {s:?} was cut mid-token"
            );
        }
    }

    #[test]
    fn entity_split_across_deltas_is_not_cut() {
        // A card number arriving in two frames must end up whole in one
        // released unit (segment or flush tail), never split.
        let prefix = "Your card on file is ".to_string() + &"padding ".repeat(40);
        let mut s = StreamSegmenter::new();
        let mut released = String::new();
        for seg in s.push(&prefix) {
            released.push_str(&seg);
        }
        for seg in s.push("4242 4242 ") {
            released.push_str(&seg);
        }
        for seg in s.push("4242 4242 thanks") {
            released.push_str(&seg);
        }
        if let Some(t) = s.flush() {
            released.push_str(&t);
        }
        assert!(
            released.contains("4242 4242 4242 4242"),
            "the full card must survive intact, got: {released:?}"
        );
    }

    #[test]
    fn content_chunk_is_valid_openai_shape() {
        let t = ChunkTemplate {
            id: "chatcmpl-x".into(),
            created: 123,
            model: "claude-haiku".into(),
        };
        let s = content_chunk(&t, "hello [REDACTED-EMAIL]");
        let v: Value = serde_json::from_str(&s).unwrap();
        assert_eq!(v["object"], "chat.completion.chunk");
        assert_eq!(v["id"], "chatcmpl-x");
        assert_eq!(
            v["choices"][0]["delta"]["content"],
            "hello [REDACTED-EMAIL]"
        );
        assert!(v["choices"][0]["finish_reason"].is_null());
    }

    #[test]
    fn analyze_chunk_extracts_content_and_flags() {
        let c = analyze_chunk(r#"{"choices":[{"delta":{"content":"hi"},"finish_reason":null}]}"#);
        assert_eq!(c.content, "hi");
        assert!(!c.has_tool_calls);
        assert!(!c.structural);

        let f = analyze_chunk(r#"{"choices":[{"delta":{},"finish_reason":"stop"}]}"#);
        assert!(f.structural);
        assert_eq!(f.content, "");

        let tc = analyze_chunk(
            r#"{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{"}}]},"finish_reason":null}]}"#,
        );
        assert!(tc.has_tool_calls);
        assert!(tc.structural);

        let role = analyze_chunk(r#"{"choices":[{"delta":{"role":"assistant"}}]}"#);
        assert!(role.structural);
    }

    #[test]
    fn strip_content_keeps_structure_drops_content() {
        let out = strip_content(
            r#"{"id":"x","choices":[{"delta":{"content":"leak","role":"assistant"},"finish_reason":"stop"}]}"#,
        );
        let v: Value = serde_json::from_str(&out).unwrap();
        assert!(v["choices"][0]["delta"].get("content").is_none());
        assert_eq!(v["choices"][0]["delta"]["role"], "assistant");
        assert_eq!(v["choices"][0]["finish_reason"], "stop");
    }

    #[test]
    fn responses_reasoning_is_emptied_in_every_shape_it_arrives_in() {
        let item = || {
            serde_json::json!({"type": "reasoning", "id": "rs_1",
                "summary": [{"type": "summary_text", "text": "why"}],
                "content": [{"type": "reasoning_text", "text": "raw why"}],
                "encrypted_content": "blob"})
        };
        let message = serde_json::json!({"type": "message", "content": [{"text": "answer"}]});

        let mut body = serde_json::json!({"output": [item(), message.clone()]});
        assert!(responses_carries_reasoning(&body));
        assert!(strip_responses_reasoning(&mut body));
        assert!(!responses_carries_reasoning(&body));
        assert_eq!(body["output"][0]["summary"], serde_json::json!([]));
        assert!(body["output"][0].get("content").is_none());
        assert_eq!(body["output"][0]["encrypted_content"], "blob");
        assert_eq!(body["output"][0]["id"], "rs_1");
        assert_eq!(body["output"][1], message);
        assert_eq!(body["reasoning_withheld"], true);

        let mut completed = serde_json::json!({"type": "response.completed",
            "response": {"output": [item()]}});
        assert!(strip_responses_reasoning(&mut completed));
        assert_eq!(
            completed["response"]["output"][0]["summary"],
            serde_json::json!([])
        );
        assert_eq!(completed["response"]["reasoning_withheld"], true);

        let mut done = serde_json::json!({"type": "response.output_item.done", "item": item()});
        assert!(strip_responses_reasoning(&mut done));
        assert_eq!(done["item"]["summary"], serde_json::json!([]));

        // Nothing readable: untouched, no marker.
        let mut empty =
            serde_json::json!({"output": [{"type": "reasoning", "summary": []}, message]});
        let before = empty.clone();
        assert!(!strip_responses_reasoning(&mut empty));
        assert_eq!(empty, before);
    }

    #[test]
    fn every_responses_reasoning_event_is_recognised_and_answer_text_is_not() {
        for name in [
            "response.reasoning_summary_part.added",
            "response.reasoning_summary_part.done",
            "response.reasoning_summary_text.delta",
            "response.reasoning_summary_text.done",
            "response.reasoning_text.delta",
            "response.reasoning_text.done",
        ] {
            assert!(is_responses_reasoning_event(name), "{name}");
        }
        for name in [
            "response.output_text.delta",
            "response.output_item.done",
            "response.completed",
            "response.function_call_arguments.delta",
        ] {
            assert!(!is_responses_reasoning_event(name), "{name}");
        }
    }

    #[test]
    fn needs_sync_when_an_enforcing_output_check_is_active() {
        let mut c = GuardrailConfig::default();
        assert!(!needs_sync(std::slice::from_ref(&c)));

        // A block-capable check on chat_output MUST run sync — async-interrupt
        // leaks the offending chunk before the verdict (regression: banned
        // words / secrets / pii-block previously stayed async and leaked).
        c.chat_output_enabled = Some(true);
        c.pii_detection_enabled = true;
        c.pii_action = Some("block".into());
        assert!(
            needs_sync(std::slice::from_ref(&c)),
            "pii block on output now forces sync (was leaky on async)"
        );

        // Redact still forces sync.
        c.pii_action = Some("redact".into());
        assert!(needs_sync(std::slice::from_ref(&c)), "redact forces sync");

        // Warn-only doesn't alter the stream → stays on the faster async path.
        c.pii_action = Some("warn".into());
        assert!(
            !needs_sync(std::slice::from_ref(&c)),
            "warn-only stays async"
        );

        // Inherently-blocking checks force sync purely by being configured.
        let mut c = GuardrailConfig {
            chat_output_enabled: Some(true),
            banned_words: Some(serde_json::json!(["forbidden"])),
            ..Default::default()
        };
        assert!(
            needs_sync(std::slice::from_ref(&c)),
            "banned_words on output forces sync"
        );
        c.banned_words = None;
        c.secrets_detection_enabled = true;
        assert!(
            needs_sync(std::slice::from_ref(&c)),
            "secrets detection on output forces sync"
        );

        // Enforcement configured but output surface off → still async.
        c.chat_output_enabled = Some(false);
        assert!(!needs_sync(std::slice::from_ref(&c)));
    }

    #[test]
    fn streaming_output_mode_override_wins_over_auto_derivation() {
        // 'sync' forces sync even with no redact check configured.
        let mut c = GuardrailConfig {
            streaming_output_mode: Some("sync".into()),
            ..Default::default()
        };
        assert!(
            needs_sync(std::slice::from_ref(&c)),
            "explicit sync forces sync"
        );

        // 'async' opts a WARN-ONLY config out of buffering (nothing to
        // withhold, so no leak).
        c.streaming_output_mode = Some("async".into());
        c.chat_output_enabled = Some(true);
        c.pii_detection_enabled = true;
        c.pii_action = Some("warn".into());
        assert!(
            !needs_sync(std::slice::from_ref(&c)),
            "async opts a warn-only output check out of sync"
        );

        // 'auto' falls back to derivation (redact → sync).
        c.streaming_output_mode = Some("auto".into());
        c.pii_action = Some("redact".into());
        assert!(
            needs_sync(std::slice::from_ref(&c)),
            "auto derives sync from redact"
        );
    }

    #[test]
    fn async_override_cannot_downgrade_a_blocking_output_check() {
        // SECURITY REGRESSION GUARD: an explicit `streaming_output_mode:
        // async` MUST NOT be able to turn a block/redact output check into a
        // leaky async-interrupt check. A block that leaks is not a block.
        for action in ["block", "redact"] {
            let c = GuardrailConfig {
                streaming_output_mode: Some("async".into()),
                chat_output_enabled: Some(true),
                pii_detection_enabled: true,
                pii_action: Some(action.into()),
                ..Default::default()
            };
            assert!(
                needs_sync(std::slice::from_ref(&c)),
                "async override must be ignored for a {action} output check"
            );
        }

        // Same for the inherently-blocking checks (no action field).
        let c = GuardrailConfig {
            streaming_output_mode: Some("async".into()),
            chat_output_enabled: Some(true),
            secrets_detection_enabled: true,
            ..Default::default()
        };
        assert!(
            needs_sync(std::slice::from_ref(&c)),
            "async override must be ignored for secrets detection on output"
        );
    }

    #[tokio::test]
    async fn process_passthrough_when_no_configs() {
        let mut g = StreamGuard::new(vec![], reqwest::Client::new());
        assert!(g.is_noop());
        let chunk = r#"{"choices":[{"delta":{"content":"hello"},"finish_reason":null}]}"#;
        match g.process(chunk).await {
            StreamAction::Emit(p) => {
                assert_eq!(p.len(), 1);
                assert_eq!(p[0], chunk, "no-config path forwards verbatim");
            }
            StreamAction::Block(_) => panic!("must not block with no configs"),
        }
    }

    #[tokio::test]
    async fn sync_mode_reconstructs_content_and_forwards_finish() {
        // Forced sync, empty configs → check() is a Pass no-op, so this
        // exercises the buffer-and-release + reconstruction path without
        // any external detector.
        let mut g = StreamGuard::with_sync(true, vec![], reqwest::Client::new());
        let mut emitted = Vec::new();
        // Stream enough content to cross a segment boundary.
        let body = "lorem ipsum dolor sit amet ".repeat(20);
        let chunk = format!(
            r#"{{"id":"x","model":"m","choices":[{{"delta":{{"content":"{}"}},"finish_reason":null}}]}}"#,
            body.trim_end()
        );
        match g.process(&chunk).await {
            StreamAction::Emit(p) => emitted.extend(p),
            StreamAction::Block(_) => panic!(),
        }
        // finish chunk
        let fin = r#"{"id":"x","model":"m","choices":[{"delta":{},"finish_reason":"stop"}]}"#;
        match g.process(fin).await {
            StreamAction::Emit(p) => emitted.extend(p),
            StreamAction::Block(_) => panic!(),
        }
        match g.finish().await {
            StreamAction::Emit(p) => emitted.extend(p),
            StreamAction::Block(_) => panic!(),
        }
        // Reassemble all emitted content deltas → must equal the input
        // text (nothing lost), and a finish chunk must be present.
        let mut reassembled = String::new();
        let mut saw_finish = false;
        for payload in &emitted {
            let v: Value = serde_json::from_str(payload).unwrap();
            if let Some(t) = v["choices"][0]["delta"]["content"].as_str() {
                reassembled.push_str(t);
            }
            if v["choices"][0]["finish_reason"].as_str() == Some("stop") {
                saw_finish = true;
            }
        }
        assert_eq!(reassembled, body.trim_end(), "content must round-trip");
        assert!(saw_finish, "finish_reason must be forwarded");
    }

    #[tokio::test]
    async fn process_text_sync_roundtrips_without_loss() {
        // The text-level API (used by native Anthropic /v1/messages and
        // native Gemini, where the wire isn't OpenAI-shaped) must release
        // every input character exactly once across process_text + the
        // finish_text flush — no loss, no duplication.
        let mut g = StreamGuard::with_sync(true, vec![], reqwest::Client::new());
        let body = "lorem ipsum dolor sit amet consectetur ".repeat(20);

        let mut released = String::new();
        // Feed the body in a few deltas, as a real stream would.
        for piece in body.as_bytes().chunks(37) {
            let s = std::str::from_utf8(piece).unwrap();
            match g.process_text(s).await {
                TextAction::Emit(segs) => released.extend(segs),
                TextAction::Block(_) => panic!("must not block with no configs"),
            }
        }
        match g.finish_text().await {
            TextAction::Emit(segs) => released.extend(segs),
            TextAction::Block(_) => panic!(),
        }
        assert_eq!(released, body, "text must round-trip with no loss");
    }

    #[tokio::test]
    async fn process_text_async_passes_through_without_emitting() {
        // With no configs the guard is async/no-op: process_text returns an
        // empty Emit (the caller forwards the original event verbatim) and
        // never blocks.
        let mut g = StreamGuard::new(vec![], reqwest::Client::new());
        match g.process_text("hello world").await {
            TextAction::Emit(segs) => assert!(
                segs.is_empty(),
                "async/no-op must not re-emit text; caller forwards verbatim"
            ),
            TextAction::Block(_) => panic!("must not block with no configs"),
        }
        match g.finish_text().await {
            TextAction::Emit(segs) => assert!(segs.is_empty()),
            TextAction::Block(_) => panic!(),
        }
    }

    // --- End-to-end redaction/block with a REAL local detector ---------
    //
    // The tests above use empty configs (the orchestrator is a Pass no-op),
    // so they only prove the segmentation/plumbing. These drive StreamGuard
    // with the built-in PII detector — which redacts/blocks locally with no
    // network — so they prove the guard actually scrubs and terminates a
    // stream. This is the engine shared by every streaming route
    // (llm/anthropic/gemini/adapter via process/process_text), so it covers
    // the redaction behaviour of all of them at one deterministic point.

    /// A config that redacts PII on `chat_output` (built-in detector,
    /// local). `needs_sync` derives sync from the redact action.
    fn pii_redact_chat_output() -> GuardrailConfig {
        GuardrailConfig {
            chat_output_enabled: Some(true),
            pii_detection_enabled: true,
            pii_action: Some("redact".into()),
            ..Default::default()
        }
    }

    /// A config that blocks PII on `chat_output` (built-in detector).
    /// Block-only ⇒ async posture.
    fn pii_block_chat_output() -> GuardrailConfig {
        GuardrailConfig {
            chat_output_enabled: Some(true),
            pii_detection_enabled: true,
            pii_action: Some("block".into()),
            ..Default::default()
        }
    }

    fn delta_content(payload: &str) -> Option<String> {
        let v: Value = serde_json::from_str(payload).ok()?;
        v["choices"][0]["delta"]["content"]
            .as_str()
            .map(String::from)
    }

    #[tokio::test]
    async fn sync_mode_actually_redacts_pii_on_the_stream() {
        let mut g = StreamGuard::new(vec![pii_redact_chat_output()], reqwest::Client::new());
        assert!(
            g.is_sync(),
            "a redact chat_output check must derive sync mode"
        );

        let mut emitted = Vec::new();
        let chunk = r#"{"id":"x","model":"m","choices":[{"delta":{"content":"please reach alice@example.com today"},"finish_reason":null}]}"#;
        if let StreamAction::Emit(p) = g.process(chunk).await {
            emitted.extend(p);
        }
        if let StreamAction::Emit(p) = g.finish().await {
            emitted.extend(p);
        }
        let content: String = emitted.iter().filter_map(|p| delta_content(p)).collect();
        assert!(
            content.contains("[REDACTED-EMAIL]"),
            "redaction must fire on the stream, got: {content:?}"
        );
        assert!(
            !content.contains("alice@example.com"),
            "raw PII must never reach the wire, got: {content:?}"
        );
    }

    #[tokio::test]
    async fn process_text_actually_redacts_pii_in_sync() {
        let mut g = StreamGuard::new(vec![pii_redact_chat_output()], reqwest::Client::new());
        assert!(g.is_sync());

        let mut released = String::new();
        if let TextAction::Emit(segs) = g.process_text("please reach alice@example.com today").await
        {
            released.extend(segs);
        }
        if let TextAction::Emit(segs) = g.finish_text().await {
            released.extend(segs);
        }
        assert!(
            released.contains("[REDACTED-EMAIL]"),
            "text-API redaction must fire (native Anthropic/Gemini paths), got: {released:?}"
        );
        assert!(
            !released.contains("alice@example.com"),
            "raw PII leaked: {released:?}"
        );
    }

    #[test]
    fn the_texts_a_responses_event_repeats_are_found_and_replaced_in_order() {
        let mut completed = serde_json::json!({
            "type": "response.completed",
            "response": {"output": [
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": "not the answer"}]},
                {"type": "message", "content": [
                    {"type": "output_text", "text": "first"},
                    {"type": "refusal", "refusal": "no"},
                    {"type": "output_text", "text": "second"}
                ]}
            ]}
        });
        assert_eq!(responses_repeated_texts(&completed), ["first", "second"]);
        assert!(replace_responses_repeated_texts(
            &mut completed,
            vec!["FIRST".into(), "SECOND".into()]
        ));
        assert_eq!(responses_repeated_texts(&completed), ["FIRST", "SECOND"]);
        assert_eq!(
            completed["response"]["output"][0]["summary"][0]["text"],
            "not the answer"
        );

        let mut done = serde_json::json!({"type": "response.output_text.done", "text": "whole"});
        assert_eq!(responses_repeated_texts(&done), ["whole"]);
        assert!(replace_responses_repeated_texts(
            &mut done,
            vec!["WHOLE".into()]
        ));
        assert_eq!(done["text"], "WHOLE");

        let mut part = serde_json::json!({
            "type": "response.content_part.done",
            "part": {"type": "output_text", "text": "part"}
        });
        assert_eq!(responses_repeated_texts(&part), ["part"]);
        // The wrong number of replacements changes nothing.
        assert!(!replace_responses_repeated_texts(&mut part, vec![]));
        assert_eq!(part["part"]["text"], "part");

        let delta = serde_json::json!({"type": "response.output_text.delta", "delta": "x"});
        assert!(responses_repeated_texts(&delta).is_empty());
    }

    #[tokio::test]
    async fn a_repeated_text_is_redacted_in_sync_and_untouched_otherwise() {
        let g = StreamGuard::new(vec![pii_redact_chat_output()], reqwest::Client::new());
        assert!(g.is_sync());
        match g.check_repeated_text("reach alice@example.com today").await {
            TextAction::Emit(t) => {
                assert_eq!(t.len(), 1);
                assert!(t[0].contains("[REDACTED-EMAIL]"), "{t:?}");
                assert!(!t[0].contains("alice@example.com"), "{t:?}");
            }
            TextAction::Block(r) => panic!("redact, not block: {r}"),
        }

        let blocking = StreamGuard::new(vec![pii_block_chat_output()], reqwest::Client::new());
        if blocking.is_sync() {
            assert!(matches!(
                blocking
                    .check_repeated_text("reach alice@example.com")
                    .await,
                TextAction::Block(_)
            ));
        }

        let none = StreamGuard::new(vec![], reqwest::Client::new());
        match none.check_repeated_text("reach alice@example.com").await {
            TextAction::Emit(t) => assert_eq!(t, ["reach alice@example.com"]),
            TextAction::Block(r) => panic!("no policy, no block: {r}"),
        }
    }

    #[tokio::test]
    async fn async_mode_blocks_the_stream_on_pii() {
        // Block-capable checks now auto-derive sync (the leak-safe default),
        // so to exercise the async-interrupt path we force it — the posture an
        // operator opts into explicitly via streaming_output_mode='async'
        // (lower latency, accepting that the triggering chunk may already be
        // in flight).
        let mut g =
            StreamGuard::with_sync(false, vec![pii_block_chat_output()], reqwest::Client::new());
        assert!(!g.is_sync(), "forced async posture");

        // Below ASYNC_CHECK_INTERVAL: the detector hasn't been run yet, so a
        // short clean delta is forwarded, not blocked.
        let short = format!(
            r#"{{"choices":[{{"delta":{{"content":"{}"}}}}]}}"#,
            "ok ".repeat(10)
        );
        match g.process(&short).await {
            StreamAction::Emit(_) => {}
            StreamAction::Block(_) => panic!("must not block before the debounce interval"),
        }

        // Cross the interval with PII in the accumulated text → Block.
        let big = format!(
            r#"{{"choices":[{{"delta":{{"content":"{} now email bob@example.com"}}}}]}}"#,
            "filler ".repeat(60)
        );
        match g.process(&big).await {
            StreamAction::Block(_) => {}
            StreamAction::Emit(_) => panic!("must block once PII is scanned past the interval"),
        }
    }

    #[tokio::test]
    async fn process_text_blocks_in_async_on_pii() {
        // Force the async-interrupt posture (block-capable checks otherwise
        // auto-derive sync now — see async_mode_blocks_the_stream_on_pii).
        let mut g =
            StreamGuard::with_sync(false, vec![pii_block_chat_output()], reqwest::Client::new());
        assert!(!g.is_sync());

        // One delta large enough to trip the debounce, carrying PII.
        let body = format!("{} contact carol@example.com", "filler ".repeat(60));
        match g.process_text(&body).await {
            TextAction::Block(_) => {}
            TextAction::Emit(_) => {
                panic!("text-API async path must block on PII past the interval")
            }
        }
    }

    #[test]
    fn a_response_that_fits_the_window_is_scanned_whole() {
        // The windowing must be invisible for ordinary completions: below
        // ASYNC_SCAN_WINDOW every scan still covers the entire response,
        // exactly as the full-rescan version did.
        let text = "a".repeat(ASYNC_SCAN_WINDOW);
        assert_eq!(scan_window_start(&text, 0), 0);
        assert_eq!(scan_window_start(&text, 4000), 0);
    }

    #[test]
    fn the_window_never_starts_mid_character() {
        // Slicing a String at a non-boundary panics, and a stream of CJK
        // or emoji lands on one routinely.
        let text = "é".repeat(ASYNC_SCAN_WINDOW); // 2 bytes each
        let start = scan_window_start(&text, text.len());
        assert!(text.is_char_boundary(start));
        let _ = &text[start..]; // must not panic
    }

    #[tokio::test]
    async fn async_scan_catches_pii_straddling_a_scan_boundary() {
        // The regression this windowing could have introduced: an entity
        // whose first half arrived before a scan and whose second half
        // arrives after it. The overlap floor is what keeps it whole, and
        // it only bites when a single delta is larger than the window —
        // hence the deliberately oversized deltas here.
        let mut g =
            StreamGuard::with_sync(false, vec![pii_block_chat_output()], reqwest::Client::new());

        // Scan 1: no PII — the address is cut short of a TLD, so nothing
        // matches yet.
        let head = format!("{}dave@exa", "filler ".repeat(3000));
        match g.process_text(&head).await {
            TextAction::Emit(_) => {}
            TextAction::Block(_) => panic!("half an address is not PII"),
        }

        // Scan 2: completes the address, then buries it under more than a
        // window's worth of prose. Without the overlap the next window
        // would begin after `dave@exa` and the address would never be seen
        // whole by any scan.
        let tail = format!("mple.com {}", "filler ".repeat(2000));
        match g.process_text(&tail).await {
            TextAction::Block(_) => {}
            TextAction::Emit(_) => {
                panic!("PII straddling a scan boundary must still block")
            }
        }
    }

    // ---- mid-stream abort polling --------------------

    #[test]
    fn the_abort_poll_fires_on_the_interval_and_never_on_the_first_chunk() {
        let g = StreamGuard::new(Vec::new(), reqwest::Client::new());
        // Nothing has streamed yet: an abort check before the first chunk
        // would duplicate the preflight gate that already ran.
        assert!(!g.should_poll_abort(0));
        assert!(!g.should_poll_abort(1));
        assert!(g.should_poll_abort(ABORT_POLL_CHUNKS));
        assert!(g.should_poll_abort(ABORT_POLL_CHUNKS * 3));
        assert!(!g.should_poll_abort(ABORT_POLL_CHUNKS + 1));
    }

    // Per-chunk would be free but pointless on a stream emitting hundreds
    // of chunks a second; too coarse and an operator watches a stopped run
    // keep talking. Checked at compile time — the bound is a constant.
    const _: () = assert!(ABORT_POLL_CHUNKS >= 8, "too chatty to be worth it");
    const _: () = assert!(ABORT_POLL_CHUNKS <= 128, "too coarse to feel responsive");

    // -- reasoning -----------------------------------------------------------

    #[test]
    fn reasoning_is_found_and_removed_only_where_it_exists() {
        let plain = r#"{"choices":[{"index":0,"delta":{"content":"hi"}}]}"#;
        assert_eq!(strip_reasoning(plain), None);
        assert_eq!(chunk_reasoning(plain), None);

        let reasoning = r#"{"choices":[{"index":0,"delta":{"reasoning_content":"why"}}]}"#;
        assert_eq!(chunk_reasoning(reasoning).as_deref(), Some("why"));
        let stripped = strip_reasoning(reasoning).unwrap();
        assert!(!stripped.contains("why"));

        let blocks = r#"{"choices":[{"index":0,"delta":{"thinking_blocks":[{"type":"thinking","thinking":"why","signature":"s"}]}}]}"#;
        assert!(!strip_reasoning(blocks).unwrap().contains("signature"));

        // Signed blocks with no readable text replay the turn; they stay.
        let omitted = r#"{"choices":[{"index":0,"delta":{"thinking_blocks":[{"type":"thinking","thinking":"","signature":"s"},{"type":"redacted_thinking","data":"d"}]}}]}"#;
        assert_eq!(strip_reasoning(omitted), None);

        // The words appearing in the answer are not reasoning.
        let answer = r#"{"choices":[{"index":0,"delta":{"content":"reasoning_content"}}]}"#;
        assert_eq!(strip_reasoning(answer), None);
    }

    #[tokio::test]
    async fn a_policy_that_can_block_keeps_reasoning_off_the_wire() {
        let mut guard = StreamGuard::with_sync(true, Vec::new(), reqwest::Client::new());
        let chunk = r#"{"id":"x","choices":[{"index":0,"delta":{"reasoning_content":"secret"}}]}"#;
        let StreamAction::Emit(out) = guard.process(chunk).await else {
            panic!("no block expected");
        };
        assert!(out.iter().all(|p| !p.contains("secret")), "{out:?}");
        assert_eq!(guard.reasoning_record(), (None, true));

        // The verbatim branch a tool-call stream falls back to is covered too.
        let tool = r#"{"id":"x","choices":[{"index":0,"delta":{"tool_calls":[{"index":0}]}}]}"#;
        guard.process(tool).await;
        let StreamAction::Emit(out) = guard.process(chunk).await else {
            panic!("no block expected");
        };
        assert!(out.iter().all(|p| !p.contains("secret")), "{out:?}");
    }

    #[tokio::test]
    async fn without_such_a_policy_reasoning_passes_and_is_recorded() {
        let mut guard = StreamGuard::with_sync(false, Vec::new(), reqwest::Client::new());
        let chunk = r#"{"id":"x","choices":[{"index":0,"delta":{"reasoning_content":"why"}}]}"#;
        let StreamAction::Emit(out) = guard.process(chunk).await else {
            panic!("no block expected");
        };
        assert_eq!(out, vec![chunk.to_string()]);
        assert_eq!(guard.reasoning_record(), (Some("why"), false));
    }
}
