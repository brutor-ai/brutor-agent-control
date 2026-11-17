// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

#![allow(clippy::redundant_closure)]

use brutor_gateway_core::services::llm_adapter::AnthropicStreamNormalizer;
use brutor_gateway_core::services::llm_adapter::StreamNormalizer;
use criterion::{Criterion, criterion_group, criterion_main};

const MESSAGE_START: &str = r#"{"type":"message_start","message":{"id":"msg_01XFDUDYJgAACzvnptvVoYEL","type":"message","role":"assistant","content":[],"model":"claude-sonnet-4-20250514","stop_reason":null,"usage":{"input_tokens":25,"output_tokens":0}}}"#;

const CONTENT_BLOCK_START_TEXT: &str =
    r#"{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}"#;

const TEXT_DELTA_SHORT: &str =
    r#"{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"A"}}"#;

const TEXT_DELTA_LONG: &str = r#"{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"A proxy server acts as an intermediary between clients and other servers, receiving requests"}}"#;

const CONTENT_BLOCK_START_TOOL: &str = r#"{"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_01A09q90qw90lq917835lqs","name":"get_weather"}}"#;

const INPUT_JSON_DELTA: &str = r#"{"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\"location\":\"San Francisco\"}"}}"#;

const CONTENT_BLOCK_STOP: &str = r#"{"type":"content_block_stop","index":0}"#;

const MESSAGE_DELTA: &str =
    r#"{"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":62}}"#;

const MESSAGE_STOP: &str = r#"{"type":"message_stop"}"#;

fn bench_stream_normalizer(c: &mut Criterion) {
    let mut group = c.benchmark_group("stream_normalizer");

    // message_start (first event)
    group.bench_function("anthropic_message_start", |b| {
        b.iter_batched(
            || AnthropicStreamNormalizer::new(),
            |mut norm| norm.convert(MESSAGE_START),
            criterion::BatchSize::SmallInput,
        )
    });

    // text_delta — the HOT PATH (called per token, hundreds of times per stream)
    group.bench_function("anthropic_text_delta_short", |b| {
        b.iter_batched(
            || {
                let mut norm = AnthropicStreamNormalizer::new();
                norm.convert(MESSAGE_START);
                norm.convert(CONTENT_BLOCK_START_TEXT);
                norm
            },
            |mut norm| norm.convert(TEXT_DELTA_SHORT),
            criterion::BatchSize::SmallInput,
        )
    });

    group.bench_function("anthropic_text_delta_long", |b| {
        b.iter_batched(
            || {
                let mut norm = AnthropicStreamNormalizer::new();
                norm.convert(MESSAGE_START);
                norm.convert(CONTENT_BLOCK_START_TEXT);
                norm
            },
            |mut norm| norm.convert(TEXT_DELTA_LONG),
            criterion::BatchSize::SmallInput,
        )
    });

    // tool_use flow: block_start → input_json_delta
    group.bench_function("anthropic_tool_call_delta", |b| {
        b.iter_batched(
            || {
                let mut norm = AnthropicStreamNormalizer::new();
                norm.convert(MESSAGE_START);
                norm.convert(CONTENT_BLOCK_START_TEXT);
                norm.convert(CONTENT_BLOCK_STOP);
                norm.convert(CONTENT_BLOCK_START_TOOL);
                norm
            },
            |mut norm| norm.convert(INPUT_JSON_DELTA),
            criterion::BatchSize::SmallInput,
        )
    });

    // message_delta (final event with usage + finish_reason)
    group.bench_function("anthropic_message_delta", |b| {
        b.iter_batched(
            || {
                let mut norm = AnthropicStreamNormalizer::new();
                norm.convert(MESSAGE_START);
                norm
            },
            |mut norm| norm.convert(MESSAGE_DELTA),
            criterion::BatchSize::SmallInput,
        )
    });

    // message_stop
    group.bench_function("anthropic_message_stop", |b| {
        b.iter_batched(
            || {
                let mut norm = AnthropicStreamNormalizer::new();
                norm.convert(MESSAGE_START);
                norm
            },
            |mut norm| norm.convert(MESSAGE_STOP),
            criterion::BatchSize::SmallInput,
        )
    });

    // Full stream simulation: message_start → 50 text_deltas → message_delta → message_stop
    group.bench_function("anthropic_full_stream_50_tokens", |b| {
        b.iter_batched(
            || AnthropicStreamNormalizer::new(),
            |mut norm| {
                norm.convert(MESSAGE_START);
                norm.convert(CONTENT_BLOCK_START_TEXT);
                for _ in 0..50 {
                    norm.convert(TEXT_DELTA_SHORT);
                }
                norm.convert(CONTENT_BLOCK_STOP);
                norm.convert(MESSAGE_DELTA);
                norm.convert(MESSAGE_STOP);
            },
            criterion::BatchSize::SmallInput,
        )
    });

    group.finish();
}

criterion_group!(benches, bench_stream_normalizer);
criterion_main!(benches);
