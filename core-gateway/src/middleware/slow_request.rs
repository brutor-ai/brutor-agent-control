// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Slow-request middleware
//!
//! Tower layer that wraps every proxy route and triggers a
//! `pg_stat_activity` capture when a request takes longer than the
//! per-route threshold. The capture (in
//! [`crate::services::diagnostics`]) runs on the dedicated diagnostic
//! pool so it's reachable even when the main pool is starved.
//!
//! ### Per-route thresholds
//!
//! Hard-coded route-pattern → threshold map. We don't expose this as
//! config because the right answer is *intrinsic to the route*:
//!   - admin/health endpoints should be sub-second; 2 s is the
//!     "something is wrong" signal.
//!   - chat completion supports streaming SSE that legitimately runs
//!     for minutes — only flag total durations that look pathological.
//!   - audio / video / image generation can take tens of seconds on
//!     the upstream — wide threshold.
//!
//! Routes not in the map fall back to a conservative default
//! ([`DEFAULT_SLOW_THRESHOLD_MS`]) so a future route still gets
//! basic coverage.
//!
//! ### Why a tower layer (not an axum middleware fn)
//!
//! Layers compose with the existing `TraceLayer` and tenant
//! middleware that the proxy already uses. Drop-in: register once in
//! `main.rs` after the diagnostic pool is built.

use std::sync::Arc;
use std::task::{Context, Poll};
use std::time::{Duration, Instant};

use axum::body::Body;
use axum::http::Request;
use axum::response::Response;
use futures::future::BoxFuture;
use sqlx::PgPool;
use tower::{Layer, Service};
use tracing::warn;

use crate::services::diagnostics;

/// Conservative default for routes not in [`route_threshold`].
/// 5 s catches obvious pathology without false-firing on normal
/// admin write paths.
pub const DEFAULT_SLOW_THRESHOLD_MS: u64 = 5_000;

/// Maximum number of `pg_stat_activity` rows captured per snapshot.
/// Bounded so a single slow-request log line fits comfortably in
/// Loki — under heavy load the table can have hundreds of entries.
pub const MAX_SNAPSHOT_ROWS: usize = 50;

/// Resolve the slow-request threshold (ms) for a given request URI
/// path. Patterns are checked longest-prefix-first so more specific
/// routes win.
///
/// Adding a new threshold here is intentional friction — the right
/// time to add a route is when an operator says "this route was slow
/// and we want to capture diagnostics for it specifically".
pub fn route_threshold(path: &str) -> u64 {
    // Streaming chat completion: legitimately long. Only flag when
    // total wall time exceeds 90 s, which is past every reasonable
    // single-token-stream latency budget.
    if path.starts_with("/v1/proxy/llm/chat/completions")
        || path.starts_with("/v1/proxy/anthropic/messages")
    {
        return 90_000;
    }
    // Multimodal / audio / image / video — upstream calls can be
    // slow but not minutes-slow.
    if path.starts_with("/v1/proxy/llm/audio/")
        || path.starts_with("/v1/proxy/llm/images/")
        || path.starts_with("/v1/proxy/llm/videos/")
    {
        return 30_000;
    }
    // Embeddings / responses: synchronous, expected sub-5 s.
    if path.starts_with("/v1/proxy/llm/embeddings") || path.starts_with("/v1/proxy/llm/responses") {
        return 5_000;
    }
    // Admin: this is the whole reason this middleware exists. The
    // reset-resilience hang we're chasing should fire here.
    if path.starts_with("/v1/proxy/llm/admin/") {
        return 2_000;
    }
    // MCP tool calls — variable; the bound is the upstream's
    // tolerance. 30 s captures pathology without flagging a slow
    // research-style tool.
    if path.starts_with("/v1/proxy/mcp/") {
        return 30_000;
    }
    // A2A inbound / outbound: typically fast handler dispatches plus
    // possibly an LLM call.
    if path.starts_with("/a2a/") {
        return 30_000;
    }
    // Auth / health probes / OpenAPI — tight budget.
    if path == "/health"
        || path == "/version"
        || path == "/v1/openapi.json"
        || path.starts_with("/v1/auth-info")
    {
        return 1_000;
    }
    // Catch-all: leave the default. Operators can correlate via the
    // route label on `proxy_slow_requests_total`.
    DEFAULT_SLOW_THRESHOLD_MS
}

/// Tower layer carrying the diagnostic pool reference. Construct
/// once at boot in `main.rs` and apply to the top-level router.
#[derive(Clone)]
pub struct SlowRequestLayer {
    diagnostic_pool: Option<Arc<PgPool>>,
}

impl SlowRequestLayer {
    pub fn new(diagnostic_pool: Option<Arc<PgPool>>) -> Self {
        Self { diagnostic_pool }
    }
}

impl<S> Layer<S> for SlowRequestLayer {
    type Service = SlowRequestService<S>;

    fn layer(&self, inner: S) -> Self::Service {
        SlowRequestService {
            inner,
            diagnostic_pool: self.diagnostic_pool.clone(),
        }
    }
}

#[derive(Clone)]
pub struct SlowRequestService<S> {
    inner: S,
    diagnostic_pool: Option<Arc<PgPool>>,
}

impl<S> Service<Request<Body>> for SlowRequestService<S>
where
    S: Service<Request<Body>, Response = Response> + Clone + Send + 'static,
    S::Future: Send + 'static,
    S::Error: Send + 'static,
{
    type Response = S::Response;
    type Error = S::Error;
    type Future = BoxFuture<'static, Result<Self::Response, Self::Error>>;

    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        self.inner.poll_ready(cx)
    }

    fn call(&mut self, req: Request<Body>) -> Self::Future {
        let path = req.uri().path().to_string();
        let threshold_ms = route_threshold(&path);
        // Pull the correlation id (if the upstream sets one). axum's
        // tower-http `TraceLayer` sets `x-request-id`; we read it but
        // do not require it.
        let correlation_id = req
            .headers()
            .get("x-request-id")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("unknown")
            .to_string();
        // Capture the W3C `traceparent` header if present.
        // The control plane injects one on outbound admin calls so
        // an operator can correlate the slow-request log with the
        // calling trace in Grafana, even without full OpenTelemetry
        // context propagation.
        let traceparent = req
            .headers()
            .get("traceparent")
            .and_then(|v| v.to_str().ok())
            .map(|s| s.to_string());
        let diagnostic_pool = self.diagnostic_pool.clone();

        // Tower services are typically borrow-by-mut; clone the
        // inner service for the future to take ownership safely
        // (standard pattern from tower docs).
        let mut inner = self.inner.clone();
        let started = Instant::now();
        Box::pin(async move {
            let response = inner.call(req).await?;

            let elapsed = started.elapsed();
            let threshold = Duration::from_millis(threshold_ms);
            if elapsed > threshold {
                let duration_ms = elapsed.as_secs_f64() * 1000.0;

                if let Some(pool) = diagnostic_pool {
                    // Spawn the capture so the response isn't blocked
                    // on it — the response is already on its way back
                    // to the client, this is purely diagnostic
                    // bookkeeping. Carries the traceparent into the
                    // log so an operator can step from a Grafana trace
                    // to the snapshot.
                    let cid = correlation_id.clone();
                    let route = path.clone();
                    let tp = traceparent.clone();
                    tokio::spawn(async move {
                        if let Some(tp) = &tp {
                            tracing::warn!(
                                target: "slow_request",
                                correlation_id = %cid,
                                traceparent = %tp,
                                route = %route,
                                duration_ms,
                                "Slow request — capturing diagnostics"
                            );
                        }
                        let captured = diagnostics::capture_and_log(
                            &pool,
                            &cid,
                            &route,
                            duration_ms,
                            MAX_SNAPSHOT_ROWS,
                        )
                        .await;
                        crate::services::proxy_metrics::record_slow_request(
                            &route,
                            if captured {
                                "captured_diagnostics"
                            } else {
                                "no_capture"
                            },
                        );
                    });
                } else {
                    // No diagnostic pool configured — still fire the
                    // counter so dashboards can see slow-request rate
                    // without the snapshot detail.
                    warn!(
                        target: "slow_request",
                        correlation_id = %correlation_id,
                        traceparent = ?traceparent,
                        route = %path,
                        duration_ms = %duration_ms,
                        threshold_ms = %threshold_ms,
                        "Slow request observed but no diagnostic pool configured"
                    );
                    crate::services::proxy_metrics::record_slow_request(&path, "no_pool");
                }
            }
            Ok(response)
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn admin_route_uses_2s_threshold() {
        // The reset-resilience hang we're chasing must trip the
        // middleware; pin the threshold.
        assert_eq!(
            route_threshold("/v1/proxy/llm/admin/some-id/reset-resilience"),
            2_000
        );
        assert_eq!(
            route_threshold("/v1/proxy/llm/admin/some-id/health-check"),
            2_000
        );
    }

    #[test]
    fn chat_completion_uses_long_threshold() {
        // Streaming SSE legitimately runs minutes; don't false-fire.
        assert_eq!(route_threshold("/v1/proxy/llm/chat/completions"), 90_000);
    }

    #[test]
    fn unknown_route_falls_back_to_default() {
        assert_eq!(
            route_threshold("/v1/some/future/route"),
            DEFAULT_SLOW_THRESHOLD_MS
        );
    }

    #[test]
    fn health_endpoint_has_tight_threshold() {
        // /health should never be slow; 1 s catches anything weird.
        assert_eq!(route_threshold("/health"), 1_000);
        assert_eq!(route_threshold("/version"), 1_000);
    }

    #[test]
    fn mcp_and_a2a_routes_get_30s() {
        assert_eq!(route_threshold("/v1/proxy/mcp/server/foo"), 30_000);
        assert_eq!(
            route_threshold("/a2a/default/hello-world-a2a/message:send"),
            30_000
        );
    }

    #[test]
    fn embeddings_route_5s() {
        assert_eq!(route_threshold("/v1/proxy/llm/embeddings"), 5_000);
    }
}
