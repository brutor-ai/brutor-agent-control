// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

use brutor_gateway_core::services::router_state::RouterState;
use criterion::{BenchmarkId, Criterion, criterion_group, criterion_main};
use std::time::Duration;

fn bench_router_state(c: &mut Criterion) {
    let mut group = c.benchmark_group("router_state");

    // is_cooled_down (miss — model not in cooldown)
    group.bench_function("is_cooled_down_miss", |b| {
        let state = RouterState::new(None);
        b.iter(|| state.is_cooled_down("model-1"))
    });

    // is_cooled_down (hit — model is in cooldown)
    group.bench_function("is_cooled_down_hit", |b| {
        let state = RouterState::new(None);
        state.set_cooldown("model-1", Duration::from_secs(60), 429, "rate limited");
        b.iter(|| state.is_cooled_down("model-1"))
    });

    // increment_rpm
    group.bench_function("increment_rpm", |b| {
        let state = RouterState::new(None);
        b.iter(|| state.increment_rpm("model-1"))
    });

    // add_tpm
    group.bench_function("add_tpm", |b| {
        let state = RouterState::new(None);
        b.iter(|| state.add_tpm("model-1", 500))
    });

    // check_rpm (below limit)
    group.bench_function("check_rpm", |b| {
        let state = RouterState::new(None);
        for _ in 0..50 {
            state.increment_rpm("model-1");
        }
        b.iter(|| state.check_rpm("model-1", 100))
    });

    // record_latency
    group.bench_function("record_latency", |b| {
        let state = RouterState::new(None);
        b.iter(|| state.record_latency("model-1", 150.0))
    });

    // get_avg_latency
    group.bench_function("get_avg_latency", |b| {
        let state = RouterState::new(None);
        for i in 0..100 {
            state.record_latency("model-1", 100.0 + i as f64);
        }
        b.iter(|| state.get_avg_latency("model-1"))
    });

    // begin_request + end_request cycle
    group.bench_function("begin_end_request", |b| {
        let state = RouterState::new(None);
        b.iter(|| {
            state.begin_request("model-1");
            state.end_request("model-1");
        })
    });

    // Group-concurrency (`max_parallel_requests`) enforcement moved to
    // `usage::LlmConcurrencyGuard`; the RouterState counter it benchmarked was
    // removed (it leaked slots and never released).

    // The exact-match response cache's store (`cache_hit`, `cache_miss`) is
    // the Enterprise Edition's ; its benchmarks are
    // `enterprise/benches/dispatch_store.rs`, under the same names.

    // cleanup with many entries
    for size in [100, 1000] {
        group.bench_with_input(BenchmarkId::new("cleanup", size), &size, |b, &size| {
            b.iter_batched(
                || {
                    let state = RouterState::new(None);
                    for i in 0..size {
                        state.increment_rpm(&format!("model-{i}"));
                        state.add_tpm(&format!("model-{i}"), 100);
                        if i % 3 == 0 {
                            state.set_cooldown(
                                &format!("model-{i}"),
                                Duration::from_millis(1), // already expired
                                429,
                                "test",
                            );
                        }
                    }
                    // Let cooldowns expire
                    std::thread::sleep(Duration::from_millis(2));
                    state
                },
                |state| state.cleanup(),
                criterion::BatchSize::SmallInput,
            )
        });
    }

    group.finish();
}

criterion_group!(benches, bench_router_state);
criterion_main!(benches);
