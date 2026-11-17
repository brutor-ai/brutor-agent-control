// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Prometheus metrics. Scraped via `GET /metrics`.

use prometheus::{
    Encoder, HistogramVec, IntCounterVec, Registry, TextEncoder, histogram_opts, opts,
    register_histogram_vec_with_registry, register_int_counter_vec_with_registry,
};

/// Lazily-built registry. We keep a single global so all metrics
/// share namespaces and the `/metrics` handler can encode them
/// without a per-request setup cost.
pub struct Metrics {
    pub registry: Registry,
    pub executions_total: IntCounterVec,
    pub execution_duration_ms: HistogramVec,
    pub script_executions_total: IntCounterVec,
}

impl Default for Metrics {
    fn default() -> Self {
        Self::new()
    }
}

impl Metrics {
    pub fn new() -> Self {
        let registry = Registry::new();

        let executions_total = register_int_counter_vec_with_registry!(
            opts!(
                "skill_runner_executions_total",
                "Total /execute requests handled. Labels distinguish success/auth/error outcomes."
            ),
            &["outcome"],
            registry
        )
        .expect("metric registration");

        let execution_duration_ms = register_histogram_vec_with_registry!(
            histogram_opts!(
                "skill_runner_execution_duration_ms",
                "Wall-clock duration of /execute (ms), labelled by outcome.",
                // Buckets tuned for skill workloads: most fast-path scripts
                // finish under 1s; slow PDF/parse skills go to 60s.
                vec![
                    10.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 5000.0, 30_000.0, 120_000.0
                ]
            ),
            &["outcome"],
            registry
        )
        .expect("metric registration");

        let script_executions_total = register_int_counter_vec_with_registry!(
            opts!(
                "skill_runner_script_executions_total",
                "Per-script subprocess executions, labelled by language and outcome."
            ),
            &["language", "outcome"],
            registry
        )
        .expect("metric registration");

        Self {
            registry,
            executions_total,
            execution_duration_ms,
            script_executions_total,
        }
    }

    /// Encode as Prometheus text format. Returns `(content_type, body)`.
    pub fn encode_text(&self) -> (String, String) {
        let encoder = TextEncoder::new();
        let metric_families = self.registry.gather();
        let mut buf = Vec::new();
        encoder
            .encode(&metric_families, &mut buf)
            .expect("text encode");
        (
            String::from(encoder.format_type()),
            String::from_utf8(buf).expect("UTF-8"),
        )
    }
}
