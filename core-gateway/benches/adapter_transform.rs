// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

#![allow(
    clippy::redundant_closure,
    clippy::needless_borrow,
    clippy::needless_borrows_for_generic_args
)]

use brutor_gateway_core::models::db::LlmModel;
use brutor_gateway_core::services::llm_adapter::get_adapter;
use criterion::{Criterion, criterion_group, criterion_main};
use serde_json::json;

fn make_chat_request() -> serde_json::Map<String, serde_json::Value> {
    let body = json!({
        "model": "gpt-4o",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Explain what a proxy server is in 3 sentences."}
        ],
        "temperature": 0.7,
        "max_tokens": 256,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather for a location",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "location": {"type": "string", "description": "City name"}
                        },
                        "required": ["location"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "search_docs",
                    "description": "Search documentation",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 100}
                        },
                        "required": ["query"]
                    }
                }
            }
        ],
        "tool_choice": "auto",
        "user": "bench-user-123",
        "stop": ["\n\n"],
        "n": 1,
        "logprobs": true,
        "top_logprobs": 3,
        "stream_options": {"include_usage": true}
    });
    body.as_object().unwrap().clone()
}

fn bench_adapter_transform(c: &mut Criterion) {
    let mut group = c.benchmark_group("adapter_transform");

    let providers = [
        ("openai", "gpt-4o"),
        ("anthropic", "claude-sonnet-4-20250514"),
        ("gemini", "gemini-2.0-flash"),
        ("mistral", "mistral-large-latest"),
        ("xai", "grok-3"),
        ("cohere", "command-r-plus"),
        ("bedrock", "anthropic.claude-3-sonnet"),
        ("ollama", "llama3"),
    ];

    for (provider, model_name) in providers {
        let model = LlmModel {
            id: format!("{provider}-1"),
            model_name: model_name.into(),
            provider: provider.into(),
            api_key: Some("sk-bench-key".into()),
            enabled: true,
            ..Default::default()
        };
        let adapter = get_adapter(provider);

        group.bench_function(&format!("transform_{provider}"), |b| {
            b.iter_batched(
                || make_chat_request(),
                |mut req| adapter.transform_request(&mut req, &model),
                criterion::BatchSize::SmallInput,
            )
        });
    }

    // Anthropic response normalization benchmark
    group.bench_function("normalize_anthropic_response", |b| {
        let adapter = get_adapter("anthropic");
        b.iter_batched(
            || json!({
                "id": "msg_01XFDUDYJgAACzvnptvVoYEL",
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "A proxy server acts as an intermediary between clients and other servers. It receives requests from clients, forwards them to the target server, and returns the responses back to the clients. Proxies are commonly used for caching, security filtering, load balancing, and anonymizing network traffic."}
                ],
                "model": "claude-sonnet-4-20250514",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 25, "output_tokens": 62}
            }),
            |mut body| adapter.normalize_response(&mut body),
            criterion::BatchSize::SmallInput,
        )
    });

    // Anthropic response normalization with tool_use
    group.bench_function("normalize_anthropic_tool_response", |b| {
        let adapter = get_adapter("anthropic");
        b.iter_batched(
            || {
                json!({
                    "id": "msg_01XFDUDYJgAACzvnptvVoYEL",
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Let me check the weather."},
                        {
                            "type": "tool_use",
                            "id": "toolu_01A09q90qw90lq917835lqs",
                            "name": "get_weather",
                            "input": {"location": "San Francisco"}
                        }
                    ],
                    "model": "claude-sonnet-4-20250514",
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 50, "output_tokens": 30}
                })
            },
            |mut body| adapter.normalize_response(&mut body),
            criterion::BatchSize::SmallInput,
        )
    });

    group.finish();
}

criterion_group!(benches, bench_adapter_transform);
criterion_main!(benches);
