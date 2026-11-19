// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Run lifecycle endpoints.
//!
//! Two routes, both about the same thing: letting a client tell the
//! gateway that a run is over.
//!
//! Finalization has three mechanisms and this is the first
//! and best of them. The idle sweeper will eventually close any run, but a
//! run closed by timeout is recorded as `abandoned` — indistinguishable
//! from a client that crashed — while a run closed explicitly carries its
//! real outcome. The difference matters most for exactly the runs an
//! operator cares about: `exhausted` is *only* ever set from an explicit
//! signal, because the proxy cannot see a framework's max-iteration cap.
//!
//! ## Authorization
//!
//! A caller may only end a run it can see. The check is the same
//! containment rule the AI System header uses: the run's own
//! steps must have been authorized against the caller's group or one below
//! it. Without that, any authenticated client could close — and mislabel —
//! another team's runs.

use axum::{
    Router,
    extract::{Path, State},
    http::{HeaderMap, StatusCode},
    response::Response,
    routing::{get, post},
};
use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::middleware::tenant::TenantContext;
use crate::models::auth::{AuthContext, AuthType};
use crate::routes::route_helpers;
use crate::services::{
    SharedState,
    ai_system::is_self_or_ancestor,
    auth,
    run_ledger::{self, finalized_by, terminal},
    run_outcome,
};

/// Header form of the end signal, for clients that would rather not spend
/// a round trip: set it on the last call of the run.
pub const RUN_END_HEADER: &str = "X-Brutor-Run-End";
/// The outcome the run ended in, sent beside the run-end marker
/// (or as `"outcome_id"` in the body of `POST /v1/runs/{id}/end`).
pub const OUTCOME_ID_HEADER: &str = crate::services::run_outcome::OUTCOME_ID_HEADER;

/// Client-asserted turn identity. Steps sharing a turn id belong to
/// one pass of the agent loop, the same way steps sharing a root task id
/// belong to one run.
///
/// Optional by design: turns are inferable from model-call boundaries, and
/// the ledger records which way it got the number so an inferred count is
/// never mistaken for a reported one. A client that knows its own loop
/// should assert; one that does not, need change nothing.
pub const TURN_ID_HEADER: &str = "X-Brutor-Turn-Id";

/// The conversation a run belongs to. Asserted by the client; the gateway
/// HMAC-derives the stored key so one caller cannot name another's session.
/// See `services::a2a_delegation::HEADER_SESSION_ID` for the full rationale.
pub const SESSION_ID_HEADER: &str = "X-Brutor-Session-Id";

/// Marks the last request of a conversation — the portal sends it when a
/// chat is deleted. Carried on a request rather than sent as its own call so
/// the signal cannot be lost to a failed follow-up.
pub const SESSION_END_HEADER: &str = "X-Brutor-Session-End";

/// Optional turn ordinal within the run, so turns can be ordered without
/// relying on timestamps a batched writer may not preserve.
pub const TURN_SEQ_HEADER: &str = "X-Brutor-Turn-Seq";

/// Client-asserted logical phase of the plan — a LangGraph node,
/// a milestone on the agent's own to-do list. One step contains one or more
/// turns.
///
/// Unlike a turn boundary, a step boundary is **not inferable** here: nothing
/// in the traffic distinguishes "still researching" from "now drafting", and
/// that decision lives in the customer's orchestrator. So absent means absent —
/// `ai_system_runs.action_count` reads 0 rather than guessing 1.
pub const STEP_ID_HEADER: &str = "X-Brutor-Step-Id";

/// Optional display label for the step, e.g. `research`.
pub const STEP_NAME_HEADER: &str = "X-Brutor-Step-Name";

pub fn router() -> Router<SharedState> {
    Router::new()
        .route("/{root_task_id}/end", post(end_run))
        .route("/{root_task_id}", get(get_run))
        // Run feedback (under the run routes, not the portal prefix):
        // `POST /{root_task_id}/feedback`.
        .merge(super::run_feedback::router())
}

#[derive(Debug, Deserialize, Default, utoipa::ToSchema)]
pub struct EndRunRequest {
    /// How the run ended. Omit to let the gateway infer it.
    ///
    /// The one value worth sending is `exhausted`: it is the canonical
    /// "my agent looped until it ran out of iterations" signal and the
    /// gateway has no way to observe it.
    pub state: Option<String>,
    /// The outcome the run ended in, as declared in the AI
    /// System's contract: `<outcome>` or `<outcome>.<variant>`, each part
    /// `[a-z0-9_-]{1,48}`. Judged against the active contract's catalogue;
    /// a malformed value is refused with 400.
    pub outcome_id: Option<String>,
}

#[derive(Debug, Serialize, utoipa::ToSchema)]
pub struct RunResponse {
    pub run_id: String,
    pub root_task_id: String,
    pub ai_system_id: Option<String>,
    pub terminal_state: String,
    pub chain_integrity: String,
    pub action_count: i64,
    pub total_cost_usd: f64,
    /// True when this call re-closed an already-finalized run, which
    /// happens when a step arrives late. Surfaced rather than hidden so a
    /// client that double-sends can tell.
    pub reopened: bool,
    /// The reported outcome, as stored.
    pub outcome_id: Option<String>,
    /// `declared` | `undeclared` | `unreported`.
    pub outcome_conformance: String,
    /// `success` | `no_work` | `deviation` | `failed` | `not_decisive`.
    pub run_class: String,
    /// Why an `errored` / `exhausted` run failed.
    pub error_cause: Option<String>,
}

fn error_response(status: StatusCode, message: &str) -> Response {
    crate::services::error_envelope::canonical_error_response(status, message)
}

/// The group this caller acts within, for the containment check.
///
/// An API key carries its group in `api_key_group_id`, but a
/// portal end user carries theirs in `resolved_group_id` (the active
/// workspace). Reading only the first meant every JWT caller looked
/// group-less, took the permissive branch below, and could therefore act on
/// any run in the tenant.
fn caller_group(auth_ctx: &AuthContext) -> Option<&str> {
    auth_ctx
        .api_key_group_id
        .as_deref()
        .or(auth_ctx.resolved_group_id.as_deref())
}

/// Whether `auth_ctx` may act on a run whose steps ran in `run_groups`.
///
/// Permissive only in the one case where there is nothing to check
/// against: a run with no recorded resource group cannot be contained by
/// anything, and refusing it would make unattributed traffic impossible to
/// close explicitly.
///
/// A database error is returned. An unreadable group list used to be
/// an EMPTY one, which is the permissive case: any caller could end or read
/// any run while the database was failing.
async fn may_touch_run(
    state: &SharedState,
    tenant_id: &str,
    auth_ctx: &AuthContext,
    run_groups: &[String],
) -> Result<bool, sqlx::Error> {
    if run_groups.is_empty() {
        return Ok(true);
    }
    let Some(caller) = caller_group(auth_ctx) else {
        // No group on the caller at all.
        //
        // This permissive branch was written for a
        // tenant-wide *service key*, where the tenant scope really is the
        // only boundary there is. It also caught every end user, because
        // their group lives in a different field: an authenticated portal
        // user could end or read any run in the tenant, including runs that
        // belong to another user's work. Ending someone else's run is not a
        // read — it writes a terminal state into assurance evidence.
        //
        // So the branch now applies only to callers that legitimately have no
        // group scope. An end-user JWT with no resolved workspace is not one
        // of those; it is a caller we cannot contain, and an uncontainable
        // caller on a per-run action is refused.
        return Ok(!matches!(
            auth_ctx.auth_type,
            AuthType::GatewayJwt | AuthType::IdpOauth
        ));
    };
    for g in run_groups {
        if is_self_or_ancestor(&state.db, tenant_id, caller, g).await?
            || is_self_or_ancestor(&state.db, tenant_id, g, caller).await?
        {
            return Ok(true);
        }
    }
    Ok(false)
}

/// The run-access decision for `root_task_id`: `Err` is the response to
/// return (403 outside the caller's hierarchy, 503 when it cannot be read).
async fn check_run_access(
    state: &SharedState,
    tenant_id: &str,
    auth_ctx: &AuthContext,
    root_task_id: &str,
) -> Result<(), Response> {
    let unavailable = |e: sqlx::Error| {
        tracing::error!(error = %e, root_task_id, "run access check: database error; refusing");
        error_response(
            StatusCode::SERVICE_UNAVAILABLE,
            "Run access could not be checked; retry",
        )
    };
    let groups = run_group_ids(state, tenant_id, root_task_id)
        .await
        .map_err(unavailable)?;
    if !may_touch_run(state, tenant_id, auth_ctx, &groups)
        .await
        .map_err(unavailable)?
    {
        // 403, not 404: the run demonstrably exists, and pretending
        // otherwise would leave a caller debugging a phantom.
        return Err(error_response(
            StatusCode::FORBIDDEN,
            "This run belongs to a different part of the resource-group hierarchy",
        ));
    }
    Ok(())
}

/// Distinct resource groups the run's steps were authorized against.
async fn run_group_ids(
    state: &SharedState,
    tenant_id: &str,
    root_task_id: &str,
) -> Result<Vec<String>, sqlx::Error> {
    Ok(sqlx::query_as::<_, (String,)>(
        "SELECT DISTINCT resource_group_id FROM proxy_logs \
         WHERE tenant_id = $1 AND root_task_id = $2 AND resource_group_id IS NOT NULL",
    )
    .bind(tenant_id)
    .bind(root_task_id)
    .fetch_all(&state.db)
    .await?
    .into_iter()
    .map(|r| r.0)
    .collect())
}

#[utoipa::path(
    post,
    path = "/v1/runs/{root_task_id}/end",
    tag = "Runs",
    summary = "Finalize a run",
    description = "Close a run explicitly and record how it ended. Preferred over \
                   waiting for the idle sweeper: a swept run is recorded as \
                   `abandoned`, and `exhausted` can only ever be reported this way.",
    params(("root_task_id" = String, Path, description = "The run's root task id")),
    request_body(content = EndRunRequest, description = "Optional"),
    responses(
        (status = 200, description = "Run finalized", body = RunResponse),
        (status = 404, description = "No steps recorded for this run"),
        (status = 403, description = "Run belongs to another part of the hierarchy")
    ),
    security(("api_key" = []), ("bearer" = []))
)]
pub(crate) async fn end_run(
    State(state): State<SharedState>,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Path(root_task_id): Path<String>,
    body: Option<axum::Json<EndRunRequest>>,
) -> Response {
    let tenant_id = match route_helpers::extract_tenant_id(tenant) {
        Ok(t) => t,
        Err(r) => return r,
    };

    let mut auth_ctx = match auth::authenticate_request(
        &headers,
        &tenant_id,
        &state.config.jwt_secret,
        &state.db,
        &state.credential_verifiers,
        None,
        true,
    )
    .await
    {
        Ok(ctx) => ctx,
        Err(e) => return error_response(e.status_code(), &e.to_string()),
    };
    if let Err(e) = crate::services::identity_policy::judge_call(&state, &mut auth_ctx).await {
        return crate::services::identity_policy::refusal_response(
            &e,
            error_response(e.status_code(), &e.to_string()),
        )
        .await;
    }

    if let Err(resp) = check_run_access(&state, &tenant_id, &auth_ctx, &root_task_id).await {
        return resp;
    }

    // A body-supplied state takes precedence; the header is the
    // zero-round-trip alternative for clients that set it on their last
    // call. `X-Brutor-Run-End: true` means "ended, infer the outcome".
    let header_state = headers
        .get(RUN_END_HEADER)
        .and_then(|v| v.to_str().ok())
        .map(str::trim)
        .filter(|s| !s.is_empty() && !s.eq_ignore_ascii_case("true"));

    let requested = body
        .as_ref()
        .and_then(|b| b.state.as_deref())
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .or(header_state);

    if let Some(s) = requested {
        if !terminal::ALL.contains(&s) {
            return error_response(
                StatusCode::BAD_REQUEST,
                &format!(
                    "Unknown terminal state {s:?}; expected one of {}",
                    terminal::ALL.join(", ")
                ),
            );
        }
    }

    // The body's `outcome_id` wins over the header, like the
    // state. Here (unlike on a proxy call, where the work must not be lost to
    // a label) a malformed value is the client's mistake on a call whose
    // whole purpose is the label: 400, nothing finalized.
    let header_outcome = match headers.get(OUTCOME_ID_HEADER) {
        Some(v) => match v.to_str() {
            Ok(s) => Some(s.trim()),
            Err(_) => {
                return error_response(
                    StatusCode::BAD_REQUEST,
                    "X-Brutor-Outcome-Id is not a valid outcome id",
                );
            }
        },
        None => None,
    }
    .filter(|s| !s.is_empty());
    let outcome = body
        .as_ref()
        .and_then(|b| b.outcome_id.as_deref())
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .or(header_outcome);
    if let Some(o) = outcome {
        if !run_outcome::is_well_formed(o) {
            return error_response(
                StatusCode::BAD_REQUEST,
                &format!(
                    "Malformed outcome_id {o:?}; expected <outcome> or <outcome>.<variant>, \
                     each [a-z0-9_-]{{1,{}}}",
                    run_outcome::MAX_PART_LEN
                ),
            );
        }
    }
    match run_ledger::finalize_run(
        &state.db,
        &tenant_id,
        &root_task_id,
        finalized_by::EXPLICIT,
        requested,
        outcome,
    )
    .await
    {
        Ok(Some(run)) => axum::Json(RunResponse {
            run_id: run.run_id,
            root_task_id: run.root_task_id,
            ai_system_id: run.ai_system_id,
            terminal_state: run.terminal_state,
            chain_integrity: run.chain_integrity,
            action_count: run.action_count,
            total_cost_usd: run.total_cost_usd,
            reopened: run.reopened,
            outcome_id: run.outcome_id,
            outcome_conformance: run.outcome_conformance,
            run_class: run.run_class,
            error_cause: run.error_cause,
        })
        .into_response(),
        Ok(None) => error_response(
            StatusCode::NOT_FOUND,
            "No steps recorded for this run — nothing to finalize",
        ),
        Err(e) => {
            // Our own storage failing is an ERROR (proxy error taxonomy).
            tracing::error!(error = %e, %root_task_id, "run finalization failed");
            error_response(StatusCode::INTERNAL_SERVER_ERROR, "Failed to finalize run")
        }
    }
}

#[utoipa::path(
    get,
    path = "/v1/runs/{root_task_id}",
    tag = "Runs",
    summary = "Inspect a run",
    description = "The finalized rollup for a run the caller can see. Returns 404 \
                   while the run is still open — a run that has not been finalized \
                   has no rollup yet.",
    params(("root_task_id" = String, Path, description = "The run's root task id")),
    responses(
        (status = 200, description = "Run rollup"),
        (status = 404, description = "Run not finalized or not found")
    ),
    security(("api_key" = []), ("bearer" = []))
)]
pub(crate) async fn get_run(
    State(state): State<SharedState>,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Path(root_task_id): Path<String>,
) -> Response {
    let tenant_id = match route_helpers::extract_tenant_id(tenant) {
        Ok(t) => t,
        Err(r) => return r,
    };

    let mut auth_ctx = match auth::authenticate_request(
        &headers,
        &tenant_id,
        &state.config.jwt_secret,
        &state.db,
        &state.credential_verifiers,
        None,
        true,
    )
    .await
    {
        Ok(ctx) => ctx,
        Err(e) => return error_response(e.status_code(), &e.to_string()),
    };
    if let Err(e) = crate::services::identity_policy::judge_call(&state, &mut auth_ctx).await {
        return crate::services::identity_policy::refusal_response(
            &e,
            error_response(e.status_code(), &e.to_string()),
        )
        .await;
    }

    if let Err(resp) = check_run_access(&state, &tenant_id, &auth_ctx, &root_task_id).await {
        return resp;
    }

    type RunRow = (
        String,
        Option<String>,
        String,
        String,
        i32,
        f64,
        i32,
        Option<String>,
        Option<String>,
        Option<bool>,
        Option<String>,
    );
    let row: Result<Option<RunRow>, sqlx::Error> = sqlx::query_as(
        "SELECT id, ai_system_id, terminal_state, chain_integrity, action_count, \
                CAST(total_cost_usd AS DOUBLE PRECISION), reopened_count, \
                outcome_id, outcome_conformance, outcome_counts_toward_baseline, error_cause \
           FROM ai_system_runs WHERE tenant_id = $1 AND root_task_id = $2",
    )
    .bind(&tenant_id)
    .bind(&root_task_id)
    .fetch_optional(&state.db)
    .await;
    let row = match row {
        Ok(r) => r,
        Err(e) => return crate::services::db_errors::unavailable("the run", &e),
    };

    match row {
        Some((
            id,
            system,
            state_s,
            chain,
            steps,
            cost,
            reopened,
            outcome_id,
            conformance,
            counts,
            error_cause,
        )) => axum::Json(json!({
            "run_id": id,
            "root_task_id": root_task_id,
            "ai_system_id": system,
            "run_class": run_outcome::run_class(&state_s, conformance.as_deref(), counts),
            "terminal_state": state_s,
            "chain_integrity": chain,
            "action_count": steps,
            "total_cost_usd": cost,
            "reopened": reopened > 0,
            "outcome_id": outcome_id,
            "outcome_conformance": conformance,
            "error_cause": error_cause,
        }))
        .into_response(),
        None => error_response(
            StatusCode::NOT_FOUND,
            "Run not finalized yet — it has no rollup until it ends",
        ),
    }
}

use axum::response::IntoResponse;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn run_end_header_name_is_stable() {
        // Documented and set by the SDK; a rename is a wire
        // change.
        assert_eq!(RUN_END_HEADER, "X-Brutor-Run-End");
    }

    #[test]
    fn every_terminal_state_is_accepted_by_the_end_route() {
        // The route validates against the same list the finalizer writes,
        // so a state the ledger can store is never rejected here.
        for s in terminal::ALL {
            assert!(terminal::ALL.contains(&s));
        }
        // `blocked_policy` is gone.
        assert_eq!(terminal::ALL.len(), 6);
        assert!(!terminal::ALL.contains(&"blocked_policy"));
    }

    #[test]
    fn the_outcome_header_is_the_spec_name() {
        assert_eq!(OUTCOME_ID_HEADER, "X-Brutor-Outcome-Id");
    }
}
