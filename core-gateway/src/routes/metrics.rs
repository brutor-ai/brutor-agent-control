// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Prometheus Metrics Endpoint
//!
//! Serves `/metrics` in Prometheus text exposition format when metrics are
//! enabled (`BRUTOR_METRICS_ENABLED=true`), on its **own listener**
//! (`BRUTOR_METRICS_LISTEN_ADDR`, default `0.0.0.0:9464`), never on the public
//! proxy port. Deployments keep that port off the internet; only the
//! scraper reaches it.
//!
//! ### Usage
//! ```yaml
//! # prometheus.yml
//! scrape_configs:
//!   - job_name: brutor-gateway-core
//!     scrape_interval: 15s
//!     static_configs:
//!       - targets: ['brutor-gateway-core:9464']
//! ```

use axum::Router;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::IntoResponse;

use crate::services::SharedState;

/// The metrics listener's whole router: `GET /metrics` and nothing else.
pub fn router() -> Router<SharedState> {
    Router::new().route("/metrics", axum::routing::get(prometheus_metrics))
}

/// Handler for `GET /metrics`.
/// Renders all registered metrics in Prometheus text format.
pub async fn prometheus_metrics(State(state): State<SharedState>) -> impl IntoResponse {
    match &state.telemetry.prometheus_handle {
        Some(handle) => {
            let body = handle.render();
            (
                StatusCode::OK,
                [("content-type", "text/plain; version=0.0.4; charset=utf-8")],
                body,
            )
                .into_response()
        }
        None => (StatusCode::NOT_FOUND, "Metrics not enabled").into_response(),
    }
}
