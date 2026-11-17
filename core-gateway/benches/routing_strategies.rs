// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

use brutor_gateway_core::models::db::LlmModel;
use brutor_gateway_core::services::llm_router::{
    select_cost_based, select_latency_based, select_least_busy, select_simple_shuffle,
    select_usage_based,
};
use brutor_gateway_core::services::router_state::RouterState;
use criterion::{BenchmarkId, Criterion, criterion_group, criterion_main};

fn make_models(count: usize) -> Vec<LlmModel> {
    (0..count)
        .map(|i| LlmModel {
            id: format!("model-{i}"),
            model_name: format!("gpt-4o-{i}"),
            provider: "openai".into(),
            enabled: true,
            routing_weight: Some((i as i32 % 5) + 1),
            rate_limit_rpm: Some(100),
            rate_limit_tpm: Some(100_000),
            cost_per_input_token: Some(0.001 * (i + 1) as f64),
            ..Default::default()
        })
        .collect()
}

fn bench_routing_strategies(c: &mut Criterion) {
    let mut group = c.benchmark_group("routing_strategies");

    for count in [3, 10, 50] {
        let models = make_models(count);

        group.bench_with_input(
            BenchmarkId::new("simple_shuffle", count),
            &models,
            |b, models| b.iter(|| select_simple_shuffle(models)),
        );

        // usage_based with pre-populated TPM data
        group.bench_with_input(
            BenchmarkId::new("usage_based", count),
            &models,
            |b, models| {
                let state = RouterState::new(None);
                for (i, m) in models.iter().enumerate() {
                    state.add_tpm(&m.id, (i as i64 + 1) * 1000);
                }
                b.iter(|| select_usage_based(models, &state))
            },
        );

        // least_busy with pre-populated active request counts
        group.bench_with_input(
            BenchmarkId::new("least_busy", count),
            &models,
            |b, models| {
                let state = RouterState::new(None);
                for (i, m) in models.iter().enumerate() {
                    for _ in 0..(i % 5) {
                        state.begin_request(&m.id);
                    }
                }
                b.iter(|| select_least_busy(models, &state))
            },
        );

        // latency_based with pre-populated latency data
        group.bench_with_input(
            BenchmarkId::new("latency_based", count),
            &models,
            |b, models| {
                let state = RouterState::new(None);
                for (i, m) in models.iter().enumerate() {
                    for _ in 0..10 {
                        state.record_latency(&m.id, 100.0 + (i as f64 * 20.0));
                    }
                }
                b.iter(|| select_latency_based(models, &state))
            },
        );

        // cost_based
        group.bench_with_input(
            BenchmarkId::new("cost_based", count),
            &models,
            // Bench with the production default blend, the same constant the
            // router falls back to when a group sets no cost_output_ratio.
            |b, models| {
                b.iter(|| {
                    select_cost_based(
                        models,
                        brutor_gateway_core::services::llm_router::DEFAULT_COST_OUTPUT_RATIO,
                    )
                })
            },
        );
    }

    group.finish();
}

criterion_group!(benches, bench_routing_strategies);
criterion_main!(benches);
