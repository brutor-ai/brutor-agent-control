// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The MCP proxy's reading of a request body.
//!
//! Before: the body was parsed by `serde_json::from_slice::<Value>` three
//! times before the upstream call (the classifier, the handler, the
//! capability gate). Now: once, by `jsonrpc_body::read`, which also refuses
//! a repeated key. `before_three_parses` and `after_one_read` are the two
//! costs for the same `tools/call`.

use brutor_gateway_core::services::jsonrpc_body::{RpcBody, read};
use criterion::{Criterion, black_box, criterion_group, criterion_main};
use serde_json::{Value, json};

fn tool_call(argument_bytes: usize) -> Vec<u8> {
    json!({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {
            "name": "search_orders",
            "arguments": {
                "customer_id": "c-10293", "limit": 25, "include": ["lines", "payments"],
                "filter": {"status": "open", "min_total": 120.5, "region": "eu-west"},
                "note": "x".repeat(argument_bytes),
            },
            "_meta": {"progressToken": "p-1"}
        }
    })
    .to_string()
    .into_bytes()
}

fn bench(c: &mut Criterion) {
    for (label, size) in [("tools_call_300B", 64), ("tools_call_8KB", 8 * 1024)] {
        let body = tool_call(size);
        c.bench_function(&format!("{label}/before_three_parses"), |b| {
            b.iter(|| {
                for _ in 0..3 {
                    black_box(serde_json::from_slice::<Value>(black_box(&body)).unwrap());
                }
            })
        });
        c.bench_function(&format!("{label}/after_one_read"), |b| {
            b.iter(|| match read(black_box(&body)) {
                RpcBody::Message { value, .. } => {
                    black_box(value);
                }
                _ => unreachable!(),
            })
        });
    }
}

criterion_group!(benches, bench);
criterion_main!(benches);
