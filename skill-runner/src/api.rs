// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! HTTP API: `POST /v1/runner/execute`, `POST /v1/runner/run_script`,
//! `GET /health`, `GET /metrics`.
//!
//! No other routes exist by design — every extra endpoint is a
//! potential attack surface and we want this binary to do exactly
//! one job.

use std::sync::Arc;
use std::time::Instant;

use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
};

use crate::auth::{AuthError, verify_bearer};
use crate::config::Config;
use crate::executor::{self, ExecutorTuning};
use crate::metrics::Metrics;
use crate::types::{ErrorEnvelope, ExecuteRequest, ExecuteResponse, RunScriptRequest};

#[derive(Clone)]
pub struct AppState {
    pub config: Arc<Config>,
    pub metrics: Arc<Metrics>,
    pub tuning: ExecutorTuning,
}

impl AppState {
    pub fn new(config: Config) -> Self {
        let tuning = ExecutorTuning {
            sandbox_timeout_secs: config.sandbox_timeout_secs,
            trusted_timeout_secs: config.trusted_timeout_secs,
            trusted_env: config.trusted_env.clone(),
        };
        Self {
            config: Arc::new(config),
            metrics: Arc::new(Metrics::new()),
            tuning,
        }
    }
}

/// Build the axum router. Public so the integration tests can mount
/// the same routes against a TestClient.
pub fn build_router(state: AppState) -> Router {
    let max_bytes = crate::config::MAX_REQUEST_BYTES;
    Router::new()
        .route("/v1/runner/execute", post(execute_handler))
        .route("/v1/runner/run_script", post(run_script_handler))
        .route("/health", get(health_handler))
        .route("/metrics", get(metrics_handler))
        // Body cap is a load-shedding control — refuses oversized
        // requests with HTTP 413 before they hit our executor.
        .layer(DefaultBodyLimit::max(max_bytes))
        .with_state(state)
}

// ============================================================================
// Handlers
// ============================================================================

async fn health_handler() -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "status": "ok",
        "service": "brutor-skill-runner",
        "version": env!("CARGO_PKG_VERSION"),
    }))
}

async fn metrics_handler(State(state): State<AppState>) -> impl IntoResponse {
    let (content_type, body) = state.metrics.encode_text();
    ([(axum::http::header::CONTENT_TYPE, content_type)], body)
}

async fn execute_handler(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(req): Json<ExecuteRequest>,
) -> Response {
    // Auth — bail before doing any work if the JWT is bad.
    let auth_header = match headers
        .get(axum::http::header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
    {
        Some(h) => h,
        None => return auth_failure(&state, AuthError::MissingHeader),
    };

    let claims_result = verify_bearer(
        auth_header,
        &state.config.jwt_secret,
        crate::config::JWT_LEEWAY_SECS,
        &req.execution_id,
    );
    let _claims = match claims_result {
        Ok(c) => c,
        Err(e) => return auth_failure(&state, e),
    };

    // Execute.
    let started = Instant::now();

    let response = executor::execute(req, &state.tuning, &state.config.workspace_root).await;

    observe_duration_ms(&state, "pending", started);

    let outcome_label = if response.success {
        "success"
    } else {
        "script_failure"
    };
    state
        .metrics
        .executions_total
        .with_label_values(&[outcome_label])
        .inc();

    for sr in response.script_results.values() {
        let label = if sr.success {
            "success"
        } else if sr.exit_code == -9 {
            "timeout"
        } else {
            "failure"
        };
        state
            .metrics
            .script_executions_total
            .with_label_values(&[sr.language.as_str(), label])
            .inc();
    }

    (StatusCode::OK, Json(response)).into_response()
}

/// L3 (act) — run ONE named script in a per-session workspace.
/// Same JWT model as `/v1/runner/execute` (the `exec` claim is bound to
/// `execution_id`); returns only the script's output, never its source.
async fn run_script_handler(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(req): Json<RunScriptRequest>,
) -> Response {
    let auth_header = match headers
        .get(axum::http::header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
    {
        Some(h) => h,
        None => return auth_failure(&state, AuthError::MissingHeader),
    };

    if let Err(e) = verify_bearer(
        auth_header,
        &state.config.jwt_secret,
        crate::config::JWT_LEEWAY_SECS,
        &req.execution_id,
    ) {
        return auth_failure(&state, e);
    }

    let started = Instant::now();

    let response = executor::run_named_script(
        req,
        &state.tuning,
        &state.config.workspace_root,
        crate::config::WORKSPACE_TTL_SECS,
    )
    .await;

    observe_duration_ms(&state, "run_script", started);

    let outcome = if response.success {
        "success"
    } else if response.exit_code == -9 {
        "timeout"
    } else {
        "script_failure"
    };
    state
        .metrics
        .executions_total
        .with_label_values(&[outcome])
        .inc();

    (StatusCode::OK, Json(response)).into_response()
}

/// Record how long a run took, in milliseconds: the unit the histogram's
/// name and buckets state. (The library's own timer observes seconds.)
fn observe_duration_ms(state: &AppState, label: &str, started: Instant) {
    state
        .metrics
        .execution_duration_ms
        .with_label_values(&[label])
        .observe(started.elapsed().as_secs_f64() * 1000.0);
}

fn auth_failure(state: &AppState, err: AuthError) -> Response {
    tracing::warn!(error = %err, "auth failure on /v1/runner/execute");
    state
        .metrics
        .executions_total
        .with_label_values(&["auth_error"])
        .inc();
    let body = ErrorEnvelope {
        error: "unauthorized".into(),
        message: err.to_string(),
    };
    (StatusCode::UNAUTHORIZED, Json(body)).into_response()
}

// Custom IntoResponse for the success path that lets us bypass the
// otherwise-correct default serializer. Used implicitly above —
// kept explicit for the type signature on `ExecuteResponse`.
impl IntoResponse for ExecuteResponse {
    fn into_response(self) -> Response {
        (StatusCode::OK, Json(self)).into_response()
    }
}
