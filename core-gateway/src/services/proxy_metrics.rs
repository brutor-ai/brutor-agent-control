// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Proxy Metrics
//!
//! Thin wrapper around the [`metrics`] crate that records proxy-specific
//! counters, histograms, and gauges. Each function is a no-op if no
//! metrics recorder is installed (i.e. `BRUTOR_METRICS_ENABLED=false`), so
//! callers never need to check.
//!
//! ### Naming Convention
//! All metric names are prefixed with `proxy_` and use snake_case, following
//! the [Prometheus naming best practices](https://prometheus.io/docs/practices/naming/).
//!
//! ### Integration Points
//! - **Prometheus** — scraped via the `/metrics` HTTP endpoint.
//! - **Datadog / New Relic** — use the OpenTelemetry Collector with a
//!   Prometheus receiver, or point the OTLP metrics exporter directly.
//! - **Grafana** — connect Prometheus as a data source; import the
//!   bundled dashboard JSON (see `deploy/grafana/`).

use metrics::{counter, gauge, histogram};

/// Record a completed proxy request (any type: llm, mcp, oauth, skill).
///
/// Labels:
/// - `proxy_type`: "llm", "mcp", "oauth", "skill"
/// - `method`: HTTP method
/// - `status`: HTTP status code (string)
/// - `provider`: upstream provider name (or "n/a")
pub fn record_request(proxy_type: &str, method: &str, status: u16, provider: &str) {
    counter!(
        "proxy_requests_total",
        "proxy_type" => proxy_type.to_string(),
        "method" => method.to_string(),
        "status" => status.to_string(),
        "provider" => provider.to_string(),
    )
    .increment(1);
}

/// Record request latency in seconds.
///
/// Labels: `proxy_type`, `method`, `provider`
pub fn record_latency(proxy_type: &str, method: &str, provider: &str, duration_secs: f64) {
    histogram!(
        "proxy_request_duration_seconds",
        "proxy_type" => proxy_type.to_string(),
        "method" => method.to_string(),
        "provider" => provider.to_string(),
    )
    .record(duration_secs);
}

/// Record LLM token usage.
///
/// Labels: `direction` ("prompt" or "completion"), `provider`, `model`
pub fn record_tokens(direction: &str, provider: &str, model: &str, count: i32) {
    counter!(
        "proxy_llm_tokens_total",
        "direction" => direction.to_string(),
        "provider" => provider.to_string(),
        "model" => model.to_string(),
    )
    .increment(count as u64);
}

/// Record estimated LLM cost in microdollars (USD × 1,000,000).
///
/// Labels: `provider`, `model`
///
/// The Prometheus metric is `proxy_llm_estimated_cost_microdollars`.
/// To display in USD in Grafana, divide by 1,000,000:
///   `sum(increase(proxy_llm_estimated_cost_microdollars[1h])) / 1e6`
pub fn record_cost(provider: &str, model: &str, cost_usd: f64) {
    let microdollars = (cost_usd * 1_000_000.0) as u64;
    counter!(
        "proxy_llm_estimated_cost_microdollars",
        "provider" => provider.to_string(),
        "model" => model.to_string(),
    )
    .increment(microdollars);
}

/// Adjust the active SSE stream gauge (increment by +1 on open, -1 on close).
pub fn stream_opened() {
    gauge!("proxy_active_streams").increment(1.0);
}

pub fn stream_closed() {
    gauge!("proxy_active_streams").decrement(1.0);
}

/// Record a guardrail-blocked request.
///
/// Labels: `surface` ("llm", "mcp", "skill"), `check` (e.g. "banned_words")
pub fn record_guardrail_block(surface: &str, check: &str) {
    counter!(
        "proxy_guardrail_blocks_total",
        "surface" => surface.to_string(),
        "check" => check.to_string(),
    )
    .increment(1);
}

/// Record a *new* system alert being raised (first occurrence of a
/// dedup key — not a re-raise).
///
/// Labels: `category`, `severity`, `resource_type`
///
/// The metric is `proxy_system_alerts_raised_total`. Tenant ID is
/// intentionally omitted to keep label cardinality bounded; a
/// multi-tenant Grafana view is expected to aggregate across tenants
/// anyway. If per-tenant inspection is needed, query the
/// `system_alerts` table directly.
pub fn record_system_alert_raised(category: &str, severity: &str, resource_type: &str) {
    counter!(
        "proxy_system_alerts_raised_total",
        "category" => category.to_string(),
        "severity" => severity.to_string(),
        "resource_type" => resource_type.to_string(),
    )
    .increment(1);
}

/// Record a re-raise of an existing alert (dedup hit). Useful for
/// spotting alert storms in Grafana — a sudden spike in this counter
/// relative to `proxy_system_alerts_raised_total` means the same
/// downstream service is failing over and over.
///
/// Labels: `category`, `severity`, `resource_type`
pub fn record_system_alert_deduped(category: &str, severity: &str, resource_type: &str) {
    counter!(
        "proxy_system_alerts_deduped_total",
        "category" => category.to_string(),
        "severity" => severity.to_string(),
        "resource_type" => resource_type.to_string(),
    )
    .increment(1);
}

/// Record a system alert being auto-resolved by the background sweeper.
/// A spike here is also a useful signal (e.g. many alerts resolving
/// in the same minute probably means an outage ended).
///
/// Labels: `category`
pub fn record_system_alert_auto_resolved(category: &str) {
    counter!(
        "proxy_system_alerts_auto_resolved_total",
        "category" => category.to_string(),
    )
    .increment(1);
}

// ── Prompt cache metrics ─────────────────────────────────────────
//
// Counters paired with the policy decisions in
// `services::prompt_cache`. Decision counters cover the inject-time
// outcome (was the policy applied? if not, why?). Usage counters
// cover the response-time outcome (did the provider report cache
// reads? did we save tokens / dollars?).
//
// Label cardinality is kept low: `mechanism` ∈ {none, cache_control,
// auto_prefix, cached_contents}, `reason` is one of a fixed set of
// `&'static str` constants, and `provider` is a small enum of LLM
// providers. We intentionally don't label by tenant or model — that's
// what the proxy_logs table is for; a per-tenant breakdown via
// Prometheus would explode cardinality on large fleets.

/// Record a prompt-cache injection decision. Fires on every LLM
/// request whose group has a policy bound — `applied=true` when
/// markers were injected (or, for OpenAI, the policy's auto-prefix
/// path was active), and `applied=false` for skips.
///
/// `reason` is the `PromptCacheDecision::reason` field — already a
/// `&'static str` from the small fixed set.
pub fn record_prompt_cache_decision(mechanism: &str, applied: bool, reason: &str) {
    counter!(
        "proxy_prompt_cache_decisions_total",
        "mechanism" => mechanism.to_string(),
        "applied" => if applied { "true".to_string() } else { "false".to_string() },
        "reason" => reason.to_string(),
    )
    .increment(1);
}

/// Record provider-reported prompt-cache token usage. Fires once per
/// completed LLM request that surfaced non-zero counters; the metric
/// makes it possible to compute fleet-wide cache hit rate without
/// querying the `proxy_logs` table.
///
/// `direction` ∈ {"read", "write"}.
pub fn record_prompt_cache_tokens(direction: &str, mechanism: &str, provider: &str, count: i32) {
    if count <= 0 {
        return;
    }
    counter!(
        "proxy_prompt_cache_tokens_total",
        "direction" => direction.to_string(),
        "mechanism" => mechanism.to_string(),
        "provider" => provider.to_string(),
    )
    .increment(count as u64);
}

/// Record net $ saved on a single LLM call by the prompt cache. Stored
/// in microdollars (× 1 000 000) for the same reason as `record_cost`
/// — Prometheus counters take integers, and Grafana queries divide by
/// 1e6 to display USD.
pub fn record_prompt_cache_savings(mechanism: &str, provider: &str, savings_usd: f64) {
    if savings_usd <= 0.0 {
        return;
    }
    let microdollars = (savings_usd * 1_000_000.0) as u64;
    counter!(
        "proxy_prompt_cache_savings_microdollars_total",
        "mechanism" => mechanism.to_string(),
        "provider" => provider.to_string(),
    )
    .increment(microdollars);
}

/// Convert a `PromptCacheMechanism` discriminant to the lowercase
/// label string we emit on metrics. Centralised so the labels stay
/// stable across call sites — Grafana dashboards lock in label names.
pub fn mechanism_label(
    mechanism: crate::services::prompt_cache_usage::PromptCacheMechanism,
) -> &'static str {
    use crate::services::prompt_cache_usage::PromptCacheMechanism;
    match mechanism {
        PromptCacheMechanism::None => "none",
        PromptCacheMechanism::CacheControlBreakpoints => "cache_control",
        PromptCacheMechanism::AutomaticPrefix => "auto_prefix",
        PromptCacheMechanism::CachedContents => "cached_contents",
    }
}

// ── Operations-layer metrics ───────────────────────────
//
// The proxy's DB pool is the single most important saturation signal
// for the whole stack. These metrics let an operator answer "is the
// pool the bottleneck?" without poking at the proxy directly. Paired
// with the pool-saturation monitor which raises a
// system_alerts row when sustained saturation crosses a threshold.

/// Record current size of the main DB pool (number of established
/// connections, idle + in-use). Sampled every 5s by the pool monitor
/// task in `services::pool_saturation_monitor`.
pub fn record_db_pool_size(size: u32) {
    gauge!("proxy_db_pool_size").set(size as f64);
}

/// Record idle (available) connections in the main DB pool. When this
/// hits 0 sustained, the next request waits on `acquire`. Paired with
/// `proxy_db_pool_acquire_wait_seconds` so an operator can correlate
/// idle=0 windows with elevated wait times.
pub fn record_db_pool_idle(idle: usize) {
    gauge!("proxy_db_pool_idle").set(idle as f64);
}

/// Record how long a request waited to check out a DB connection.
/// Recorded for every successful acquire — buckets are picked to
/// resolve sub-100ms (healthy) and 100ms-5s (saturated) regimes.
///
/// Operators should expect p99 < 50ms in steady state; sustained
/// p99 > 500ms means traffic is hitting the pool ceiling.
pub fn record_db_pool_acquire_wait(wait_secs: f64) {
    histogram!("proxy_db_pool_acquire_wait_seconds").record(wait_secs);
}

/// Record a request that exceeded the per-route slow-request
/// threshold. Labels:
/// - `route`: the matched axum route pattern (e.g. "/v1/proxy/llm/admin/{id}/reset-resilience")
/// - `outcome`: "captured_diagnostics" | "no_capture" — captured when
///   we successfully wrote a `pg_stat_activity` snapshot via the
///   diagnostic pool, no_capture when the diagnostic pool was itself
///   busy (rare).
pub fn record_slow_request(route: &str, outcome: &str) {
    counter!(
        "proxy_slow_requests_total",
        "route" => route.to_string(),
        "outcome" => outcome.to_string(),
    )
    .increment(1);
}
