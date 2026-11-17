// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

#![allow(
    clippy::redundant_closure,
    clippy::needless_borrow,
    clippy::needless_borrows_for_generic_args
)]

//! Benchmarks for the Anthropic Message Adapter pipeline.
//!
//! Covers all three layers:
//! - Layer 1: Client detection + preprocessing
//! - Layer 2: SSE observer + input text extraction
//! - Layer 3: Backend transforms + beta filtering + EventStream conversion

use axum::http::{HeaderMap, HeaderName, HeaderValue};
use brutor_gateway_core::models::db::LlmModel;
use brutor_gateway_core::services::anthropic_adapter::backend::{
    AnthropicDirectBackend, BackendAdapter, BedrockBackend, BetaFilter, VertexBackend,
};
use brutor_gateway_core::services::anthropic_adapter::client::{
    ClaudeCodeClient, ClientAdapter, GenericClient, detect_client,
};
use brutor_gateway_core::services::anthropic_adapter::extract_input_text;
use criterion::{Criterion, criterion_group, criterion_main};
use serde_json::{Value, json};

// ---------------------------------------------------------------------------
// Test data builders
// ---------------------------------------------------------------------------

fn claude_code_headers() -> HeaderMap {
    let mut h = HeaderMap::new();
    h.insert(
        HeaderName::from_static("anthropic-beta"),
        HeaderValue::from_static(
            "claude-code-20250219,interleaved-thinking-2025-01-24,extended-thinking-2025-01-24,advanced-tool-use-2025-11-20,structured-outputs-2025-02-19",
        ),
    );
    h.insert(
        HeaderName::from_static("anthropic-version"),
        HeaderValue::from_static("2023-06-01"),
    );
    h.insert(
        HeaderName::from_static("user-agent"),
        HeaderValue::from_static("claude-code/2.1.69"),
    );
    h
}

fn generic_sdk_headers() -> HeaderMap {
    let mut h = HeaderMap::new();
    h.insert(
        HeaderName::from_static("anthropic-beta"),
        HeaderValue::from_static("interleaved-thinking-2025-01-24"),
    );
    h.insert(
        HeaderName::from_static("anthropic-version"),
        HeaderValue::from_static("2023-06-01"),
    );
    h.insert(
        HeaderName::from_static("user-agent"),
        HeaderValue::from_static("python-anthropic/0.28.0"),
    );
    h
}

fn make_anthropic_model(provider: &str) -> LlmModel {
    LlmModel {
        id: format!("{provider}-bench-1"),
        model_name: "claude-sonnet-4-20250514".into(),
        provider: provider.into(),
        api_key: Some("AKIAIOSFODNN7EXAMPLE:wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY".into()),
        enabled: true,
        model_metadata: Some(json!({
            "aws_region": "us-west-2",
            "region": "us-central1",
            "project": "bench-project",
            "vertex_model": "claude-sonnet-4-6@20250514"
        })),
        ..Default::default()
    }
}

fn make_claude_code_request() -> Value {
    json!({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 8096,
        "stream": true,
        "system": "You are a helpful coding assistant.",
        "messages": [
            {"role": "user", "content": "Explain what a proxy server is in 3 sentences."},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "A proxy server is an intermediary."},
                    {
                        "type": "tool_use",
                        "id": "toolu_01A09q90qw90lq917835lqs",
                        "name": "Read",
                        "input": {"file_path": "/src/main.rs", "limit": 50}
                    }
                ]
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_01A09q90qw90lq917835lqs",
                        "content": "fn main() { println!(\"Hello\"); }"
                    },
                    {"type": "text", "text": "Now explain this code."},
                    {"type": "tool_reference", "tool_name": "Grep"}
                ]
            }
        ],
        "tools": [
            {
                "name": "Read",
                "description": "Read a file",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string"},
                        "limit": {"type": "integer"}
                    },
                    "required": ["file_path"]
                }
            },
            {
                "name": "Grep",
                "description": "Search file contents",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "path": {"type": "string"}
                    },
                    "required": ["pattern"]
                }
            }
        ],
        "thinking": {
            "type": "enabled",
            "budget_tokens": 10000
        }
    })
}

fn make_large_conversation() -> Value {
    let mut messages = Vec::new();
    for i in 0..20 {
        messages.push(json!({"role": "user", "content": format!("Message {i}: explain concept number {i} in detail with examples and references.")}));
        messages.push(json!({"role": "assistant", "content": [
            {"type": "text", "text": format!("Here is a detailed explanation of concept {i}. It involves multiple aspects that need careful consideration...")},
        ]}));
    }
    messages.push(json!({"role": "user", "content": "Summarize all the above."}));

    json!({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 4096,
        "system": [
            {"type": "text", "text": "You are a helpful assistant."},
            {"type": "text", "text": "Always be concise and accurate."}
        ],
        "messages": messages,
    })
}

// Beta header strings of varying complexity
const BETA_SHORT: &str = "claude-code-20250219";
const BETA_LONG: &str = "claude-code-20250219,interleaved-thinking-2025-01-24,extended-thinking-2025-01-24,advanced-tool-use-2025-11-20,structured-outputs-2025-02-19,tool-use-2024-11-15,effort-2025-03-01";

// ---------------------------------------------------------------------------
// Layer 1: Client detection + preprocessing
// ---------------------------------------------------------------------------

fn bench_client_detection(c: &mut Criterion) {
    let mut group = c.benchmark_group("anthropic_client_detection");

    group.bench_function("detect_claude_code_by_beta", |b| {
        let headers = claude_code_headers();
        b.iter(|| detect_client(&headers))
    });

    group.bench_function("detect_generic_sdk", |b| {
        let headers = generic_sdk_headers();
        b.iter(|| detect_client(&headers))
    });

    group.bench_function("detect_empty_headers", |b| {
        let headers = HeaderMap::new();
        b.iter(|| detect_client(&headers))
    });

    group.bench_function("claude_code_extract_headers", |b| {
        let headers = claude_code_headers();
        b.iter(|| ClaudeCodeClient.extract_client_headers(&headers))
    });

    group.bench_function("generic_extract_headers", |b| {
        let headers = generic_sdk_headers();
        b.iter(|| GenericClient.extract_client_headers(&headers))
    });

    // Preprocessing with tool_reference stripping (Bedrock path)
    group.bench_function("claude_code_preprocess_bedrock", |b| {
        let headers = claude_code_headers();
        let metadata = ClaudeCodeClient.extract_client_headers(&headers);
        b.iter_batched(
            || make_claude_code_request(),
            |mut body| ClaudeCodeClient.preprocess_request(&mut body, &metadata, "bedrock"),
            criterion::BatchSize::SmallInput,
        )
    });

    // Preprocessing passthrough (Anthropic Direct path — no stripping)
    group.bench_function("claude_code_preprocess_anthropic_direct", |b| {
        let headers = claude_code_headers();
        let metadata = ClaudeCodeClient.extract_client_headers(&headers);
        b.iter_batched(
            || make_claude_code_request(),
            |mut body| {
                ClaudeCodeClient.preprocess_request(&mut body, &metadata, "anthropic_direct")
            },
            criterion::BatchSize::SmallInput,
        )
    });

    group.finish();
}

// ---------------------------------------------------------------------------
// Layer 2: SSE observer + input text extraction
// ---------------------------------------------------------------------------

fn bench_input_extraction(c: &mut Criterion) {
    let mut group = c.benchmark_group("anthropic_input_extraction");

    // Small request
    group.bench_function("extract_input_text_small", |b| {
        let body = make_claude_code_request();
        b.iter(|| extract_input_text(&body))
    });

    // Large conversation (20 turns)
    group.bench_function("extract_input_text_large_20_turns", |b| {
        let body = make_large_conversation();
        b.iter(|| extract_input_text(&body))
    });

    group.finish();
}

// ---------------------------------------------------------------------------
// Layer 3: Backend transforms + beta filtering
// ---------------------------------------------------------------------------

fn bench_beta_filter(c: &mut Criterion) {
    let mut group = c.benchmark_group("anthropic_beta_filter");

    // Allow-all (Anthropic Direct — should be near-zero)
    group.bench_function("allow_all_short", |b| {
        let filter = BetaFilter::allow_all();
        b.iter(|| filter.filter(BETA_SHORT))
    });

    group.bench_function("allow_all_long", |b| {
        let filter = BetaFilter::allow_all();
        b.iter(|| filter.filter(BETA_LONG))
    });

    // Strip-all (Bedrock/Vertex)
    group.bench_function("strip_all_short", |b| {
        let filter = BetaFilter::strip_all();
        b.iter(|| filter.filter(BETA_SHORT))
    });

    group.bench_function("strip_all_long", |b| {
        let filter = BetaFilter::strip_all();
        b.iter(|| filter.filter(BETA_LONG))
    });

    // Allowlist with partial matches
    group.bench_function("allowlist_partial_match", |b| {
        let filter = BetaFilter::with_allowlist(vec![
            "interleaved-thinking".into(),
            "extended-thinking".into(),
        ]);
        b.iter(|| filter.filter(BETA_LONG))
    });

    group.finish();
}

fn bench_backend_transform(c: &mut Criterion) {
    let mut group = c.benchmark_group("anthropic_backend_transform");

    // Anthropic Direct — no-op transform
    group.bench_function("anthropic_direct_transform", |b| {
        let model = make_anthropic_model("anthropic");
        b.iter_batched(
            || make_claude_code_request(),
            |mut body| AnthropicDirectBackend.transform_request(&mut body, &model),
            criterion::BatchSize::SmallInput,
        )
    });

    // Bedrock — model mapping, effort→thinking, anthropic_version insertion
    group.bench_function("bedrock_transform", |b| {
        let model = make_anthropic_model("bedrock");
        b.iter_batched(
            || {
                let mut req = make_claude_code_request();
                // Add effort param to exercise effort→thinking mapping
                req.as_object_mut()
                    .unwrap()
                    .insert("effort".into(), json!("high"));
                // Remove existing thinking to test effort mapping
                req.as_object_mut().unwrap().remove("thinking");
                req
            },
            |mut body| BedrockBackend.transform_request(&mut body, &model),
            criterion::BatchSize::SmallInput,
        )
    });

    // Vertex — model mapping, anthropic_version, effort→thinking
    group.bench_function("vertex_transform", |b| {
        let model = make_anthropic_model("vertex_ai");
        b.iter_batched(
            || make_claude_code_request(),
            |mut body| VertexBackend.transform_request(&mut body, &model),
            criterion::BatchSize::SmallInput,
        )
    });

    // URL construction
    group.bench_function("anthropic_direct_url", |b| {
        let model = make_anthropic_model("anthropic");
        b.iter(|| AnthropicDirectBackend.messages_url(&model, true))
    });

    group.bench_function("bedrock_url_streaming", |b| {
        let model = make_anthropic_model("bedrock");
        b.iter(|| BedrockBackend.messages_url(&model, true))
    });

    group.bench_function("vertex_url_streaming", |b| {
        let model = make_anthropic_model("vertex_ai");
        b.iter(|| VertexBackend.messages_url(&model, true))
    });

    group.finish();
}

// ---------------------------------------------------------------------------
// EventStream frame parsing
// ---------------------------------------------------------------------------

fn build_eventstream_frame(event_type: &str, payload: &[u8]) -> Vec<u8> {
    // Build a minimal EventStream frame:
    // [4: total_length] [4: headers_length] [4: prelude_crc]
    // [headers] [payload] [4: message_crc]

    // Build headers: :event-type header
    let mut headers = Vec::new();
    let name = b":event-type";
    headers.push(name.len() as u8);
    headers.extend_from_slice(name);
    headers.push(7); // string type
    let event_bytes = event_type.as_bytes();
    headers.extend_from_slice(&(event_bytes.len() as u16).to_be_bytes());
    headers.extend_from_slice(event_bytes);

    // Also add :message-type = "event"
    let msg_name = b":message-type";
    headers.push(msg_name.len() as u8);
    headers.extend_from_slice(msg_name);
    headers.push(7);
    let msg_val = b"event";
    headers.extend_from_slice(&(msg_val.len() as u16).to_be_bytes());
    headers.extend_from_slice(msg_val);

    let headers_length = headers.len();
    let total_length = 12 + headers_length + payload.len() + 4; // prelude + headers + payload + crc

    let mut frame = Vec::with_capacity(total_length);
    frame.extend_from_slice(&(total_length as u32).to_be_bytes());
    frame.extend_from_slice(&(headers_length as u32).to_be_bytes());
    frame.extend_from_slice(&[0u8; 4]); // prelude CRC placeholder
    frame.extend_from_slice(&headers);
    frame.extend_from_slice(payload);
    frame.extend_from_slice(&[0u8; 4]); // message CRC placeholder

    frame
}

fn bench_eventstream_converter(c: &mut Criterion) {
    use brutor_gateway_core::services::anthropic_adapter::backend::BackendAdapter;

    let mut group = c.benchmark_group("anthropic_eventstream");

    // Single frame parse
    let payload = br#"{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello world, this is a test response from the model."}}"#;
    let frame = build_eventstream_frame("chunk", payload);

    group.bench_function("parse_single_frame", |b| {
        b.iter_batched(
            || {
                let converter = BedrockBackend.create_stream_converter().unwrap();
                (converter, frame.clone())
            },
            |(mut converter, frame)| converter.convert(&frame),
            criterion::BatchSize::SmallInput,
        )
    });

    // Simulate 50 frames arriving in a stream
    let mut multi_frames = Vec::new();
    for i in 0..50 {
        let p = format!(
            r#"{{"type":"content_block_delta","index":0,"delta":{{"type":"text_delta","text":"Token {i} "}}}}"#
        );
        multi_frames.extend(build_eventstream_frame("chunk", p.as_bytes()));
    }

    group.bench_function("parse_50_frames_batch", |b| {
        b.iter_batched(
            || {
                let converter = BedrockBackend.create_stream_converter().unwrap();
                (converter, multi_frames.clone())
            },
            |(mut converter, data)| converter.convert(&data),
            criterion::BatchSize::SmallInput,
        )
    });

    // Partial frame — tests buffering behavior
    group.bench_function("partial_frame_buffer", |b| {
        let full = build_eventstream_frame("chunk", payload);
        let half = full[..full.len() / 2].to_vec();
        let rest = full[full.len() / 2..].to_vec();

        b.iter_batched(
            || {
                let converter = BedrockBackend.create_stream_converter().unwrap();
                (converter, half.clone(), rest.clone())
            },
            |(mut converter, half, rest)| {
                let r1 = converter.convert(&half);
                let r2 = converter.convert(&rest);
                (r1, r2)
            },
            criterion::BatchSize::SmallInput,
        )
    });

    group.finish();
}

// ---------------------------------------------------------------------------
// Full pipeline simulation (all layers)
// ---------------------------------------------------------------------------

fn bench_full_pipeline(c: &mut Criterion) {
    let mut group = c.benchmark_group("anthropic_full_pipeline");

    // Simulate the full adapter pipeline for each backend (minus actual HTTP)
    for (provider, backend_name) in [
        ("anthropic", "anthropic_direct"),
        ("bedrock", "bedrock"),
        ("vertex_ai", "vertex"),
    ] {
        group.bench_function(&format!("pipeline_{provider}"), |b| {
            let headers = claude_code_headers();
            let model = make_anthropic_model(provider);

            b.iter_batched(
                || make_claude_code_request(),
                |mut body| {
                    // Layer 1: Client detection + header extraction
                    let client = detect_client(&headers);
                    let metadata = client.extract_client_headers(&headers);

                    // Layer 1: Client preprocessing
                    client.preprocess_request(&mut body, &metadata, backend_name);

                    // Layer 3: Backend transform
                    let backend =
                        brutor_gateway_core::services::anthropic_adapter::backend::get_backend(
                            provider,
                        )
                        .unwrap();
                    backend.transform_request(&mut body, &model);

                    // Layer 3: Beta header filtering
                    if let Some(ref beta) = metadata.anthropic_beta {
                        let _ = backend.filter_beta_headers(beta);
                    }

                    // Layer 2: Input text extraction (for guardrails)
                    let _ = extract_input_text(&body);
                },
                criterion::BatchSize::SmallInput,
            )
        });
    }

    group.finish();
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

criterion_group!(
    benches,
    bench_client_detection,
    bench_input_extraction,
    bench_beta_filter,
    bench_backend_transform,
    bench_eventstream_converter,
    bench_full_pipeline,
);
criterion_main!(benches);
