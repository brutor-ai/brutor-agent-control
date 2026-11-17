// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Continuous Assurance Checks — the control plane's evaluator surface.
//!
//! Called by the **control plane** over the internal `BRUTOR_GATEWAY_INTERNAL_URL`
//! path (service key + `X-Tenant-ID`), the same way the admin API already
//! reaches model health checks and audit-chain verification. The CEL
//! engine lives here and only here — author-time validation and runtime
//! evaluation share one implementation, so a spec the CP accepted can
//! never behave differently at the ledger.
//!
//! Two operations:
//!
//! * `POST /compile` — parse + profile-check an expression, then sample-
//!   evaluate it against synthetic run facts so the author sees a verdict
//!   and a result type immediately.
//! * `POST /backtest` — evaluate a published spec version against the last
//!   N days of recorded runs. Dispatches nothing, raises nothing; the
//!   returned counts + samples are the "what would this have flagged"
//!   evidence an operator reviews before enabling.

use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::post;
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::json;

use crate::middleware::tenant::TenantContext;
use crate::services::SharedState;
use crate::services::assurance_checks::{
    self, BACKTEST_MAX_RUNS, compile_expression, evaluate, unknown_fact_fields,
    value_matches_output,
};
use crate::services::service_auth::require_service_principal;

pub fn router() -> Router<SharedState> {
    Router::new()
        .route("/checks/compile", post(compile))
        .route("/checks/backtest", post(backtest))
}

/// SECURITY: a `TenantContext` alone is not a credential — the tenant
/// middleware builds one from a bare `X-Tenant-ID` header. These routes
/// read a tenant's run ledger (`backtest`) and evaluate caller-supplied
/// CEL, so the caller must also be the control plane's service key or an
/// admin session valid for that tenant.
async fn authorised_tenant(
    state: &SharedState,
    headers: &HeaderMap,
    tenant: Option<&axum::Extension<TenantContext>>,
) -> Result<String, Response> {
    let tenant_id = match tenant {
        Some(ctx) => ctx.0.tenant_id.clone(),
        None => {
            return Err((
                StatusCode::UNAUTHORIZED,
                Json(json!({ "error": "tenant context required" })),
            )
                .into_response());
        }
    };
    let (_ctx, principal) = require_service_principal(state, headers, &tenant_id, None).await?;
    principal.require_tenant_scope(&tenant_id)?;
    Ok(tenant_id)
}

#[derive(Deserialize)]
struct CompileRequest {
    expression: String,
    #[serde(default)]
    output: Option<String>,
}

/// `POST /v1/assurance/checks/compile`
async fn compile(
    State(state): State<SharedState>,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Json(req): Json<CompileRequest>,
) -> Response {
    if let Err(resp) = authorised_tenant(&state, &headers, tenant.as_ref()).await {
        return resp;
    }
    let program = match compile_expression(&req.expression) {
        Ok(p) => p,
        Err(errors) => {
            return Json(json!({ "ok": false, "errors": errors })).into_response();
        }
    };
    let mut errors: Vec<String> = unknown_fact_fields(&req.expression)
        .into_iter()
        .map(|f| format!("unknown run fact '{f}' — not in the pinned context schema"))
        .collect();

    // Sample evaluation: the author sees a verdict immediately, and the
    // declared output type is checked against a real result rather than
    // guessed statically (CEL is untyped until evaluated).
    let sample = match evaluate(&program, &crate::services::run_ledger::sample_run_facts()) {
        Ok(v) => {
            if let Some(output) = req.output.as_deref() {
                if !value_matches_output(&v, output) {
                    errors.push(format!(
                        "expression produced {v} which is not a valid '{output}' result"
                    ));
                }
            }
            Some(v)
        }
        Err(e) => {
            errors.push(format!("sample evaluation failed: {e}"));
            None
        }
    };

    Json(json!({
        "ok": errors.is_empty(),
        "errors": errors,
        "sample_value": sample,
        "context_version": crate::services::run_ledger::RUN_FACTS_VERSION,
    }))
    .into_response()
}

#[derive(Deserialize)]
struct BacktestRequest {
    check_id: String,
    version: i32,
    #[serde(default = "default_window_days")]
    window_days: i32,
}

fn default_window_days() -> i32 {
    7
}

/// `POST /v1/assurance/checks/backtest`
/// Backtests scan up to `BACKTEST_MAX_RUNS` rows each; a handful running
/// at once is a full-table read storm on `ai_system_runs`. Cap the
/// concurrency per process and answer 429 rather than queue.
const MAX_CONCURRENT_BACKTESTS: usize = 2;

fn backtest_slots() -> &'static tokio::sync::Semaphore {
    static SLOTS: std::sync::OnceLock<tokio::sync::Semaphore> = std::sync::OnceLock::new();
    SLOTS.get_or_init(|| tokio::sync::Semaphore::new(MAX_CONCURRENT_BACKTESTS))
}

async fn backtest(
    State(state): State<SharedState>,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Json(req): Json<BacktestRequest>,
) -> Response {
    let tenant_id = match authorised_tenant(&state, &headers, tenant.as_ref()).await {
        Ok(t) => t,
        Err(resp) => return resp,
    };
    let Ok(_slot) = backtest_slots().try_acquire() else {
        return (
            StatusCode::TOO_MANY_REQUESTS,
            [(axum::http::header::RETRY_AFTER, "5")],
            Json(json!({
                "ok": false,
                "error": format!("{MAX_CONCURRENT_BACKTESTS} backtests are already running; retry shortly")
            })),
        )
            .into_response();
    };
    let window_days = req.window_days.clamp(1, 90);
    match assurance_checks::backtest(
        &state.db,
        &tenant_id,
        &req.check_id,
        req.version,
        window_days,
    )
    .await
    {
        Ok(summary) => Json(json!({
            "ok": true,
            "evaluated": summary.evaluated,
            "breaches": summary.breaches,
            "errors": summary.errors,
            "window_days": summary.window_days,
            "max_runs": BACKTEST_MAX_RUNS,
            "samples": summary.samples,
        }))
        .into_response(),
        Err(e) => (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({ "ok": false, "error": e })),
        )
            .into_response(),
    }
}
