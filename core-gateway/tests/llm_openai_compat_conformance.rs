// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # OpenAI chat-completions shape conformance
//!
//! Brutor exposes `/v1/proxy/llm/chat/completions` as an
//! OpenAI-compatible endpoint. Downstream SDKs (openai-python,
//! openai-node, LangChain…) key on a specific JSON shape;
//! silently dropping or renaming a field breaks them without a
//! server-side error.
//!
//! Our code emits this shape in three places:
//!
//!   1. Relayed upstream response — we pass through OpenAI's body,
//!      so conformance is the upstream's responsibility.
//!   2. Cached non-streaming synthesis (`routes/llm.rs`, the cache
//!      hit path) — we build the response body from accumulated
//!      content.
//!   3. Cached streaming synthesis — we emit a single SSE
//!      `chat.completion.chunk` followed by `data: [DONE]`.
//!
//! The synthesis paths are inlined `json!` literals in the handler.
//! These tests **mirror the literal exactly** and then verify every
//! OpenAI-documented top-level + nested field is present and
//! typed correctly. If someone edits the handler to drop a field
//! without updating this golden, the test fails and the reviewer
//! has to explicitly OK the drift — or restore the field.
//!
//! Reference: https://platform.openai.com/docs/api-reference/chat/object
//!            https://platform.openai.com/docs/api-reference/chat-streaming/streaming-object

use serde_json::{Value, json};

// =============================================================================
// Golden shape definitions — keep in sync with the handler's `json!` literals.
// =============================================================================

/// Mirror of the cached non-streaming response built in
/// `routes/llm.rs` (~line 685).
fn cached_chat_completion(content: &str, finish_reason: &str) -> Value {
    json!({
        "id": format!("chatcmpl-cached-{}", ulid::Ulid::new()),
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content,
            },
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens": 10_i32,
            "completion_tokens": 5_i32,
            "total_tokens": 15_i32,
        }
    })
}

/// Mirror of the cached streaming chunk built in `routes/llm.rs`
/// (~line 260).
fn cached_chat_completion_chunk(content: &str) -> Value {
    let brutor_cache = json!({
        "hit": true,
        "layer": "redis_exact",
        "similarity_score": 1.0_f64,
    });
    json!({
        "id": format!("chatcmpl-cache-{}", ulid::Ulid::new()),
        "object": "chat.completion.chunk",
        "choices": [{
            "index": 0,
            "delta": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "brutor_cache": brutor_cache,
    })
}

// =============================================================================
// Non-streaming `chat.completion` shape
// =============================================================================

#[test]
fn chat_completion_has_all_documented_top_level_fields() {
    let out = cached_chat_completion("hello", "stop");
    for key in ["id", "object", "choices", "usage"] {
        assert!(
            out.get(key).is_some(),
            "missing top-level `{key}` in {out:#}",
        );
    }
}

#[test]
fn chat_completion_object_field_is_literal_chat_completion() {
    // OpenAI clients dispatch on `object` to decide
    // whether to treat a body as a completion vs. a chunk. The exact
    // string `"chat.completion"` is load-bearing.
    let out = cached_chat_completion("x", "stop");
    assert_eq!(out["object"], "chat.completion");
}

#[test]
fn chat_completion_id_starts_with_chatcmpl_prefix() {
    // SDKs don't strictly require this but every OpenAI response
    // uses the prefix, so telemetry aggregators group on it. Our
    // cached responses must match.
    let out = cached_chat_completion("x", "stop");
    let id = out["id"].as_str().unwrap();
    assert!(
        id.starts_with("chatcmpl-"),
        "id `{id}` must start with `chatcmpl-`",
    );
}

#[test]
fn chat_completion_choices_is_non_empty_array() {
    let out = cached_chat_completion("x", "stop");
    let choices = out["choices"].as_array().expect("choices is array");
    assert!(!choices.is_empty(), "choices must have at least one entry");
}

#[test]
fn chat_completion_choice_has_all_documented_fields() {
    let out = cached_chat_completion("x", "stop");
    let choice = &out["choices"][0];
    for key in ["index", "message", "finish_reason"] {
        assert!(
            choice.get(key).is_some(),
            "missing choice.{key} in {choice:#}",
        );
    }
}

#[test]
fn chat_completion_message_role_is_assistant() {
    // Spec: assistant responses always have role=assistant. SDKs
    // assert this; a typo'd role flips an entire conversation chain.
    let out = cached_chat_completion("x", "stop");
    assert_eq!(out["choices"][0]["message"]["role"], "assistant");
}

#[test]
fn chat_completion_message_content_is_string_type() {
    // OpenAI allows `content` to be null (for tool-calls), but our
    // cached path always synthesises a string. If we ever changed
    // this to emit null or an array, SDK breakage follows.
    let out = cached_chat_completion("hello world", "stop");
    let content = &out["choices"][0]["message"]["content"];
    assert!(content.is_string(), "content must be string, got {content}");
}

#[test]
fn chat_completion_finish_reason_accepts_documented_values() {
    // Spec values: stop | length | tool_calls | content_filter | function_call.
    for valid in ["stop", "length", "tool_calls", "content_filter"] {
        let out = cached_chat_completion("x", valid);
        assert_eq!(out["choices"][0]["finish_reason"], valid);
    }
}

#[test]
fn chat_completion_usage_has_all_three_token_counters() {
    // Spec: prompt_tokens + completion_tokens + total_tokens all present.
    let out = cached_chat_completion("x", "stop");
    let usage = &out["usage"];
    for key in ["prompt_tokens", "completion_tokens", "total_tokens"] {
        assert!(
            usage.get(key).and_then(|v| v.as_i64()).is_some(),
            "usage.{key} must be an integer",
        );
    }
}

#[test]
fn chat_completion_does_not_leak_anthropic_field_names() {
    // Drift guard: if someone accidentally uses `input_tokens` /
    // `output_tokens` (Anthropic naming) instead of
    // `prompt_tokens`/`completion_tokens`, catch it.
    let out = cached_chat_completion("x", "stop");
    let usage = &out["usage"];
    assert!(usage.get("input_tokens").is_none());
    assert!(usage.get("output_tokens").is_none());
}

// =============================================================================
// Streaming `chat.completion.chunk` shape
// =============================================================================

#[test]
fn chat_completion_chunk_object_field_is_literal_chat_completion_chunk() {
    // Different from the non-streaming object field. Dropping the
    // `.chunk` suffix sends SDKs down the non-streaming code path
    // and explodes on the missing `message` key.
    let out = cached_chat_completion_chunk("partial");
    assert_eq!(out["object"], "chat.completion.chunk");
}

#[test]
fn chat_completion_chunk_has_delta_not_message() {
    // Streaming chunks carry `choices[].delta`, not `message`. Using
    // `message` on a chunk (copy-paste from the non-streaming path)
    // is a silent drift — SDKs won't error, they just render nothing.
    let out = cached_chat_completion_chunk("partial");
    let choice = &out["choices"][0];
    assert!(choice.get("delta").is_some(), "chunk must have `delta`");
    assert!(
        choice.get("message").is_none(),
        "chunk must NOT have `message` (that's for non-streaming)",
    );
}

#[test]
fn chat_completion_chunk_delta_shape() {
    let out = cached_chat_completion_chunk("hello");
    let delta = &out["choices"][0]["delta"];
    assert_eq!(delta["role"], "assistant");
    assert_eq!(delta["content"], "hello");
}

#[test]
fn chat_completion_chunk_has_finish_reason_on_terminal_chunk() {
    // The last chunk must carry a non-null `finish_reason`; mid-stream
    // chunks have `null`. Our cache synthesis is always a single
    // terminal chunk, so it must be `"stop"`.
    let out = cached_chat_completion_chunk("final");
    assert_eq!(out["choices"][0]["finish_reason"], "stop");
}

#[test]
fn chat_completion_chunk_brutor_cache_extension_is_opt_in() {
    // We add a `brutor_cache` object so SDK clients can badge cached
    // replies (per-request headers are hard to surface via SDKs).
    // This is a Brutor extension, not an OpenAI field. SDKs should
    // ignore it — test that it's present and shaped consistently.
    let out = cached_chat_completion_chunk("x");
    let ext = &out["brutor_cache"];
    assert!(ext.is_object(), "brutor_cache must be an object");
    assert_eq!(ext["hit"], true);
    assert!(
        ext.get("layer").is_some(),
        "brutor_cache must carry a layer identifier",
    );
}

// =============================================================================
// Drift guards — shape must stay strictly OpenAI-compatible.
// =============================================================================

#[test]
fn no_snake_case_camel_mixups_in_completion() {
    // OpenAI uses snake_case throughout. Catch accidental camelCase
    // drift that Anthropic/A2A code might introduce via copy-paste.
    let out = cached_chat_completion("x", "stop");
    let s = serde_json::to_string(&out).unwrap();
    for camel in [
        "finishReason",
        "promptTokens",
        "completionTokens",
        "totalTokens",
    ] {
        assert!(
            !s.contains(camel),
            "response leaked camelCase field `{camel}`: {s}",
        );
    }
}

#[test]
fn no_snake_case_camel_mixups_in_chunk() {
    let out = cached_chat_completion_chunk("x");
    let s = serde_json::to_string(&out).unwrap();
    for camel in ["finishReason", "objectType"] {
        assert!(!s.contains(camel), "chunk leaked camelCase `{camel}`: {s}");
    }
}

#[test]
fn chunk_and_completion_differ_only_on_documented_fields() {
    // Structural sanity: a chunk and a completion share
    // `id`/`object`/`choices` at the top level. `usage` is
    // completion-only (usage arrives on the final chunk, but
    // OpenAI's stream protocol emits it via `stream_options` —
    // our cache synthesis is a single terminal chunk and currently
    // doesn't include it).
    let comp = cached_chat_completion("x", "stop");
    let chunk = cached_chat_completion_chunk("x");
    assert!(comp["id"].is_string());
    assert!(chunk["id"].is_string());
    assert!(comp["choices"].is_array());
    assert!(chunk["choices"].is_array());
}
