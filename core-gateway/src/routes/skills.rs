// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
// Agent Skill Execution Proxy — REST endpoints for skill lifecycle.
//
// Port of Python `app/api/v1/endpoints/proxy/skill.py`.
//
// ============================================================================
// Two Execution Paths
// ============================================================================
//
// There are two paths to execute a skill:
//
// 1. MCP path (for LLM-driven agents):
//      MCP Client → POST /v1/proxy/mcp/{server_id} (JSON-RPC tools/call)
//                 → system_server::handle_tools_call()
//                 → skill_orchestrator::execute_skill()
//                 → skill_executor::execute()
//
//    The model sees skills as tools in tools/list, decides to call one,
//    and the proxy handles it transparently. The agent doesn't even know
//    it's calling a "skill" — it's just another MCP tool.
//
// 2. REST path (this file — for everything else):
//      App/Dashboard/CI → POST /v1/proxy/skills/execute
//                       → skill_orchestrator::execute_skill()
//
//    Direct programmatic execution by apps, CI/CD pipelines, webhooks,
//    admin dashboards ("Run this skill now"), and critically the approval
//    workflow (status polling + cancel). No MCP client is going to
//    POST /proxy/skills/executions/{id}/cancel — that's a human or
//    system actor.
//
// Both paths delegate to skill_orchestrator::execute_skill() for the
// actual business logic (the 8-step pipeline).
//
// ============================================================================
// This File Is a Thin HTTP Transport Layer
// ============================================================================
//
// - Extracts auth context from HTTP headers
// - Maps orchestrator SkillError to HTTP status codes
// - Fires proxy logging, metrics, and usage logs (HTTP-specific concerns)
// - Attaches usage warning headers (X-Brutor-Usage-*) when quota approaches limits
// - Returns JSON response models
//
// ============================================================================
// Endpoints
// ============================================================================
//
//     POST /v1/proxy/skills/execute                    → execute_skill()
//     GET  /v1/proxy/skills/executions/{execution_id}  → get_execution()
//     POST /v1/proxy/skills/executions/{execution_id}/cancel → cancel_execution()
//     GET  /v1/proxy/skills/catalog                    → get_catalog()
//
// Authentication: Required (API Key, Gateway JWT, or OAuth Token)
// Access Control: Group-based — skills must be assigned to the user's group
//
// Background writes (non-blocking, via BackgroundWriter):
//     - Proxy log with proxy_type "skill"
//     - Proxy metric with endpoint_category "skill_exec"
//     - Usage log with resource_type "skill" for quota tracking

use std::time::Instant;

use axum::Router;
use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Json, Response};
use axum::routing::{get, post};
use serde_json::json;

use crate::constants;
use crate::middleware::tenant::TenantContext;
use crate::models::auth::AuthType;
use crate::models::proxy::{SkillExecuteRequest, SkillExecuteResponse};
use crate::routes::route_helpers;
use crate::services::call_chain::{derive_tenant_key, parse_or_mint};
use crate::services::skill_orchestrator::{self, SkillError};
use crate::services::{SharedState, auth, guardrails, proxy_logger, proxy_metrics, usage};

/// Header getter for `parse_or_mint`'s closure-shaped contract.
/// Mirrors the same helper in `routes/llm.rs`, `routes/mcp.rs`,
/// and `routes/a2a.rs`.
fn header_value(headers: &HeaderMap, name: &str) -> Option<String> {
    headers
        .get(name)
        .and_then(|v| v.to_str().ok())
        .map(str::to_owned)
}

/// Tag the root span's kind for skill
/// executions. Mirrors `root_kind_for_auth` in `routes::mcp` +
/// `routes::llm`: only stamped on the very first step, so a skill
/// invoked from inside an LLM tool-call inherits the LLM's tag.
fn root_kind_for_auth(auth_type: &AuthType, is_root: bool) -> Option<&'static str> {
    if !is_root {
        return None;
    }
    match auth_type {
        AuthType::GatewayJwt => Some("skill_execution"),
        AuthType::ApiKey => Some("agent_invocation"),
        _ => Some("external_api"),
    }
}

pub fn router() -> Router<SharedState> {
    Router::new()
        .route("/execute", post(execute_skill))
        .route("/executions/{execution_id}", get(get_execution))
        .route("/executions/{execution_id}/cancel", post(cancel_execution))
        .route("/catalog", get(get_catalog))
}

// ============================================================================
// Helpers
// ============================================================================

/// What every row of one skill request carries.
struct SkillRowCtx<'a> {
    state: &'a SharedState,
    auth_ctx: &'a crate::models::auth::AuthContext,
    headers: &'a HeaderMap,
    correlation_id: &'a str,
    request_body: &'a str,
    start: Instant,
    call_chain: &'a crate::services::a2a_delegation::CallChain,
    current_task_id_str: &'a str,
    trace_continuity: crate::services::a2a_delegation::TraceContinuity,
    trace_root_kind: Option<&'a str>,
}

/// A refusal no policy made — the guardrail configs could not be read, so the
/// gateway would not run or return what it cannot check (fail closed). On the
/// record like every other refusal, sealed `engine_failure`
/// .
fn refuse_unchecked_skill(
    ctx: &SkillRowCtx<'_>,
    skill_id: Option<&str>,
    execution_id: Option<&str>,
    group_id: Option<&str>,
    message: &str,
) -> Response {
    let response = error_response(StatusCode::SERVICE_UNAVAILABLE, message);
    let mut log = proxy_logger::build_proxy_log(
        constants::proxy::SKILL,
        ctx.auth_ctx,
        "POST",
        "/v1/proxy/skills/execute",
        Some(crate::constants::system_server::agent_skill::BASE_URL),
        skill_id,
        response.status().as_u16(),
        ctx.start.elapsed().as_secs_f64() * 1000.0,
        false,
        Some(message),
        ctx.correlation_id,
    );
    log.skill_id = skill_id.map(str::to_owned);
    log.skill_execution_id = execution_id.map(str::to_owned);
    log.resource_group_id = group_id
        .map(str::to_owned)
        .or_else(|| ctx.auth_ctx.resolved_group_id.clone());
    log.proxy_subtype = Some(crate::constants::proxy_subtype::ENGINE_FAILURE.to_string());
    log.user_agent = proxy_logger::extract_user_agent(ctx.headers);
    log.request_body_preview = Some(proxy_logger::body_preview(ctx.request_body));
    log.request_size_bytes = Some(ctx.request_body.len() as i32);
    proxy_logger::apply_call_chain(
        &mut log,
        ctx.call_chain,
        ctx.current_task_id_str,
        ctx.trace_continuity,
        ctx.trace_root_kind,
    );
    ctx.state
        .background_writer
        .write_proxy_log(ctx.state.db.clone(), log);
    response
}

fn skill_error_to_response(e: SkillError) -> Response {
    let status = StatusCode::from_u16(e.status_code).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    // An approval outcome carries a JSON body (the 428/202 approval envelope)
    // that must be returned verbatim, not wrapped in the canonical error shape.
    if let Some(body) = e.body {
        return (status, axum::Json(body)).into_response();
    }
    error_response(status, &e.message)
}

fn error_response(status: StatusCode, message: &str) -> Response {
    crate::services::error_envelope::canonical_error_response(status, message)
}

/// Write a single **skill guardrail-block** proxy_log row with
/// `guardrail_blocked = true` (and `pii_detected` when the failing
/// check was PII). Mirrors the A2A `log_guardrail_block` helper in
/// `routes/a2a.rs`. Without this, skill guardrail blocks 403 without
/// leaving any audit trail, so they were invisible to Mission
/// Control's Governance "Guardrail Blocks" counter
/// (`count(proxy_logs where guardrail_blocked)`).
#[allow(clippy::too_many_arguments)]
fn write_skill_guardrail_block(
    state: &SharedState,
    auth_ctx: &crate::models::auth::AuthContext,
    correlation_id: &str,
    group_id: Option<&str>,
    skill_id: Option<&str>,
    check: &str,
    message: &str,
    request_json: Option<&str>,
    duration_ms: f64,
    call_chain: &crate::services::a2a_delegation::CallChain,
    current_task_id_str: &str,
    trace_continuity: crate::services::a2a_delegation::TraceContinuity,
    trace_root_kind: Option<&str>,
) {
    let mut log = proxy_logger::build_proxy_log(
        constants::proxy::SKILL,
        auth_ctx,
        "POST",
        "/v1/proxy/skills/execute",
        Some(crate::constants::system_server::agent_skill::BASE_URL),
        skill_id,
        403,
        duration_ms,
        false,
        Some(message),
        correlation_id,
    );
    log.skill_id = skill_id.map(str::to_owned);
    log.resource_group_id = group_id
        .map(str::to_owned)
        .or_else(|| auth_ctx.resolved_group_id.clone());
    if let Some(rj) = request_json {
        log.request_body_preview = Some(proxy_logger::body_preview(rj));
        log.request_size_bytes = Some(rj.len() as i32);
    }
    // The block message carries the failing check (e.g. "(check=pii)")
    // and doubles as the 403 body returned to the caller.
    let resp_json = serde_json::json!({ "error": { "message": message } }).to_string();
    log.response_body_preview = Some(proxy_logger::body_preview(&resp_json));
    log.response_size_bytes = Some(resp_json.len() as i32);
    // This path receives only a boolean across a boundary
    // (skill verdict / stream flag / helper arg), so the config id and
    // action are not available here. Recorded as a known gap rather than
    // stamped with a guess.
    proxy_logger::apply_guardrail_block(&mut log);
    if check.contains("pii") {
        proxy_logger::apply_pii_detected(&mut log);
    }
    proxy_logger::apply_call_chain(
        &mut log,
        call_chain,
        current_task_id_str,
        trace_continuity,
        trace_root_kind,
    );
    state
        .background_writer
        .write_proxy_log(state.db.clone(), log);
}

// ============================================================================
// Execute Skill
// ============================================================================

#[utoipa::path(
    post,
    path = "/v1/proxy/skills/execute",
    tag = "Skills",
    summary = "Execute an agent skill",
    description = "Resolves the skill by ID or name, applies group-based access control, \
                   checks permissions, creates an execution record, and runs the skill. \
                   Answers 200 when the run has ended (`execution_status` is `COMPLETED` or \
                   `FAILED`), and 202 when the call is held for a person: the body is then the \
                   approval envelope (`approval_required: true`, no `execution_id`), and the \
                   same call is retried with the approval token.",
    request_body = SkillExecuteRequest,
    responses(
        (status = 200, description = "The run has ended; `execution_status` says how", body = SkillExecuteResponse),
        (status = 202, description = "Held for approval (the approval envelope), or a run still in progress", body = Value),
        (status = 410, description = "The skill is in the progressive disclosure mode: use the Agent Skills MCP flow"),
        (status = 428, description = "Held for approval, for an interactive caller (the approval envelope)", body = Value),
        (status = 400, description = "Invalid request"),
        (status = 401, description = "Authentication required"),
        (status = 403, description = "Permission denied"),
        (status = 404, description = "Skill or group not found"),
    ),
    security(("api_key" = []), ("bearer" = []))
)]
pub(crate) async fn execute_skill(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Json(mut request): Json<SkillExecuteRequest>,
) -> Response {
    let start = Instant::now();

    // The one-shot REST execute is deprecated. A skill is meant to be
    // consumed via progressive disclosure over MCP (tools/list → skills__load →
    // skills__run_script); a one-shot "execute the skill" is really a *tool*.
    // This path still answers during the migration window; full removal is the
    // final step once demos + the live suite move to the MCP flow.
    tracing::warn!(
        target: "deprecation",
        "POST /v1/proxy/skills/execute is deprecated — use the Agent \
         Skills MCP progressive-disclosure flow (skills__load → skills__run_script)."
    );

    let tenant_id = match route_helpers::extract_tenant_id(tenant) {
        Ok(t) => t,
        Err(r) => return r,
    };
    let correlation_id = route_helpers::extract_correlation_id(&headers);

    let mut auth_ctx = match auth::authenticate_request(
        &headers,
        &tenant_id,
        &state.config.jwt_secret,
        &state.db,
        &state.credential_verifiers,
        client_addr.ip(),
        true,
    )
    .await
    {
        Ok(ctx) => ctx,
        Err(e) => return error_response(e.status_code(), &e.to_string()),
    };
    if let Err(e) = crate::services::identity_policy::judge_call(&state, &mut auth_ctx).await {
        return error_response(e.status_code(), &e.to_string());
    }

    // Resolve the skill's id up front for audit rows. Callers usually invoke by
    // NAME, so the by-name deny / guardrail-block log rows would otherwise carry
    // an empty skill_id even though the skill is known. Prefer an explicit id;
    // otherwise look it up by name (published wins). Cheap, and only the block
    // paths depend on it.
    let resolved_skill_id: Option<String> = match request.skill_id.clone() {
        Some(id) => Some(id),
        None => match request.skill_name.as_deref() {
            Some(name) => match sqlx::query_scalar::<_, String>(
                "SELECT id FROM agent_skills WHERE tenant_id = $1 AND name = $2 \
                 ORDER BY (status::text = 'PUBLISHED') DESC, created_at DESC LIMIT 1",
            )
            .bind(&tenant_id)
            .bind(name)
            .fetch_optional(&state.db)
            .await
            {
                Ok(id) => id,
                Err(e) => return crate::services::db_errors::unavailable("the skill", &e),
            },
            None => None,
        },
    };

    // The one-shot REST execute is REMOVED for progressive-disclosure
    // skills. A progressive skill is consumed only via the Agent Skills MCP flow
    // (tools/list → skills__load → skills__run_script), where every layer is
    // governed. A skill still in the legacy `execute` / `both` disclosure mode
    // keeps answering here during migration.
    if let Some(sid) = resolved_skill_id.as_deref() {
        let mode: Option<String> = match sqlx::query_scalar::<_, String>(
            "SELECT disclosure_mode::text FROM agent_skills WHERE tenant_id = $1 AND id = $2",
        )
        .bind(&tenant_id)
        .bind(sid)
        .fetch_optional(&state.db)
        .await
        {
            Ok(m) => m,
            // Read as "not progressive", a failed read let the removed
            // REST path execute a skill that must go through the MCP flow.
            Err(e) => {
                return crate::services::db_errors::unavailable("the skill's disclosure mode", &e);
            }
        };
        if mode.as_deref() == Some("PROGRESSIVE") {
            return error_response(
                axum::http::StatusCode::GONE,
                "REST skill execute has been removed. Consume this skill via the \
                 Agent Skills MCP progressive-disclosure flow: tools/list → skills__load → \
                 skills__run_script.",
            );
        }
    }

    // Normalized request body for the AUDIT rows: the client almost always calls
    // by name, so the raw request carries `skill_id: null`. Inject the resolved
    // id so every proxy-log's Request Body identifies the skill (the wire request
    // sent to the runner is unchanged — this is the logged/forensic copy only).
    let request_body_for_log: String = {
        let mut v = serde_json::to_value(&request).unwrap_or_else(|_| serde_json::json!({}));
        if v.get("skill_id").map(|x| x.is_null()).unwrap_or(false) {
            if let Some(id) = &resolved_skill_id {
                v["skill_id"] = serde_json::Value::String(id.clone());
            }
        }
        v.to_string()
    };

    // The approval tokens of this request: every `X-Approval-Token` header
    // and the body's `approval_token`, so a caller retrying an execution
    // that was held for a person is recognized by the orchestrator.
    request.approvals = std::sync::Arc::new(
        crate::services::agent_approval::RequestApprovals::from_headers(&headers)
            .with(request.approval_token.as_deref()),
    );

    // Mint or inherit the call chain at
    // skill route entry. A skill invoked by an LLM's tool_call
    // decision carries the parent chain in the inbound headers;
    // `parse_or_mint` extends it one depth. A direct operator
    // invocation mints fresh with `trace_root_kind=skill_execution`.
    let tenant_key = derive_tenant_key(state.config.encryption_key.as_bytes(), &tenant_id);
    let (call_chain, current_task_id, trace_continuity) =
        parse_or_mint(|n| header_value(&headers, n), &tenant_key);
    let current_task_id_str = current_task_id.to_string();
    let trace_root_kind = root_kind_for_auth(&auth_ctx.auth_type, call_chain.depth == 0);
    let row_ctx = SkillRowCtx {
        state: &state,
        auth_ctx: &auth_ctx,
        headers: &headers,
        correlation_id: &correlation_id,
        request_body: &request_body_for_log,
        start,
        call_chain: &call_chain,
        current_task_id_str: &current_task_id_str,
        trace_continuity,
        trace_root_kind,
    };

    // Skill input guardrails — surface "skill", direction Input.
    // Operator-visible since the seed shipped + the admin UI's
    // Surfaces tab has Check Skill Input on by default, but the
    // wire from the route → guardrails service has been missing
    // (the toggle was a UI lie). Now scans the user-supplied
    // `input_params` (recursive string leaves) so PII / banned
    // patterns are caught before the skill orchestrator dispatches
    // to the calling agent.
    if let Some(input_params) = request.input_params.as_ref() {
        let mut leaves = Vec::new();
        guardrails::body_redact::collect_string_leaves(input_params, &mut leaves);
        if !leaves.is_empty() {
            let configs = state
                .guardrails_cache
                .get_configs(
                    &tenant_id,
                    auth_ctx.guardrail_user_id().as_deref(),
                    auth_ctx.guardrail_group_id(),
                    &state.db,
                )
                .await;
            let configs = match configs {
                Ok(c) => c,
                Err(e) => {
                    // Fail closed. The cache serves the
                    // last known good set on a load failure, so nothing was
                    // cached at all here. "We cannot check" must not be
                    // served as "there is nothing to check".
                    tracing::error!(error = %e, "guardrail configs unavailable for skill input");
                    return refuse_unchecked_skill(
                        &row_ctx,
                        resolved_skill_id.as_deref(),
                        None,
                        None,
                        "guardrails_unavailable: refusing to run a skill we cannot check",
                    );
                }
            };
            if !configs.is_empty() {
                let combined = guardrails::body_redact::join_for_detector(&leaves);
                match guardrails::check_input(&combined, &configs, "skill", &state.http_client)
                    .await
                {
                    guardrails::CheckOutcome::Block(violation) => {
                        tracing::warn!(
                            check = %violation.check,
                            config = %violation.config_name,
                            "Skill input guardrail violation — 403 returned"
                        );
                        proxy_metrics::record_guardrail_block("skill", &violation.check);
                        let msg = format!(
                            "Skill input blocked by guardrail '{}' (check={}): {}",
                            violation.config_name, violation.check, violation.message
                        );
                        // Best-effort group resolution: the orchestrator hasn't
                        // resolved the skill's group yet at input time.
                        let group =
                            usage::resolve_target_group(&state.db, &auth_ctx, &tenant_id).await;
                        let req_json = Some(request_body_for_log.clone());
                        let duration_ms = start.elapsed().as_secs_f64() * 1000.0;
                        write_skill_guardrail_block(
                            &state,
                            &auth_ctx,
                            &correlation_id,
                            group.as_ref().map(|g| g.id.as_str()),
                            resolved_skill_id.as_deref(),
                            &violation.check,
                            &msg,
                            req_json.as_deref(),
                            duration_ms,
                            &call_chain,
                            &current_task_id_str,
                            trace_continuity,
                            trace_root_kind,
                        );
                        return error_response(axum::http::StatusCode::FORBIDDEN, &msg);
                    }
                    guardrails::CheckOutcome::Redacted {
                        content: redacted,
                        warnings,
                    } => {
                        // Splice the redacted text back into
                        // input_params using the same leaf-paths
                        // we collected on the way in. Same
                        // fallback as the other surfaces: on
                        // split-count mismatch, forward the
                        // original payload rather than corrupting
                        // it partially.
                        if let Some(parts) =
                            guardrails::body_redact::split_redacted(&redacted, leaves.len())
                        {
                            if let Some(input_mut) = request.input_params.as_mut() {
                                for (leaf, part) in leaves.iter().zip(parts.iter()) {
                                    guardrails::body_redact::set_string_at_path(
                                        input_mut, &leaf.path, part,
                                    );
                                }
                            }
                        } else {
                            tracing::warn!(
                                target: "guardrails",
                                surface = "skill",
                                "Skill input redaction split-count mismatch; forwarding original",
                            );
                        }
                        for w in &warnings {
                            tracing::warn!(target: "guardrails", check=%w.check, surface=%w.surface, "{}", w.message);
                        }
                    }
                    guardrails::CheckOutcome::Warn { warnings } => {
                        for w in &warnings {
                            tracing::warn!(target: "guardrails", check=%w.check, surface=%w.surface, "{}", w.message);
                        }
                    }
                    guardrails::CheckOutcome::Pass => {}
                }
            }
        }
    }

    // Semantic Governance Policy on the skill_input surface.
    {
        use crate::services::semantic_policy::{self, PolicyContext, PolicyDecision};
        let content = request
            .input_params
            .as_ref()
            .map(|p| serde_json::to_string(p).unwrap_or_default())
            .unwrap_or_default();
        if !content.is_empty() {
            let group_id = auth_ctx
                .api_key_group_id
                .clone()
                .or_else(|| auth_ctx.resolved_group_id.clone());
            let sgp_ctx = PolicyContext {
                surface: "skill_input".to_string(),
                actor_agent_id: auth_ctx.actor_agent_id.clone(),
                action_type: Some("skill_exec".to_string()),
                target: request
                    .skill_id
                    .clone()
                    .or_else(|| request.skill_name.clone()),
                subject: auth_ctx.gateway_user_id.clone(),
            };
            // The judge's call is a step of this execution's run.
            let auth_headers = crate::routes::llm_helpers::judge_self_call_headers(
                &headers,
                &call_chain,
                current_task_id,
                &tenant_key,
            );
            let outcome = semantic_policy::load_and_evaluate(
                &state.semantic_policy_cache,
                state.semantic_policy_judge.as_ref(),
                &state.db,
                &tenant_id,
                group_id.as_deref(),
                &content,
                &sgp_ctx,
                &auth_headers,
                state.config.judge_model.as_deref(),
            )
            .await;
            let dur = start.elapsed().as_secs_f64() * 1000.0;
            for (pid, reason) in &outcome.shadow_hits {
                let log = proxy_logger::build_semantic_policy_log(
                    &auth_ctx,
                    "skill",
                    "/v1/proxy/skills/execute",
                    sgp_ctx.target.as_deref(),
                    group_id.clone(),
                    &correlation_id,
                    dur,
                    pid,
                    false,
                    reason,
                );
                state
                    .background_writer
                    .write_proxy_log(state.db.clone(), log);
            }
            if outcome.decision == PolicyDecision::Block {
                let pid = outcome.matched_policy_id.clone().unwrap_or_default();
                let reason = outcome.reason.clone().unwrap_or_default();
                let log = proxy_logger::build_semantic_policy_log(
                    &auth_ctx,
                    "skill",
                    "/v1/proxy/skills/execute",
                    sgp_ctx.target.as_deref(),
                    group_id.clone(),
                    &correlation_id,
                    dur,
                    &pid,
                    true,
                    &reason,
                );
                state
                    .background_writer
                    .write_proxy_log(state.db.clone(), log);
                return error_response(
                    axum::http::StatusCode::FORBIDDEN,
                    &format!("semantic_policy_blocked: {reason}"),
                );
            }
        }
    }

    // POLICY-10 — the argument policy on the skill's input is evaluated by
    // the orchestrator (`skill_orchestrator::execute_skill`), on the group
    // the execution is authorised against, for every caller.
    // A `deny` comes back as the error below.

    // A skill request may declare (or try to re-declare) its run's
    // data scope like any other request of the run. The orchestrator judges
    // the skill's input against it.
    request.data_scope_conflicts = crate::services::data_scope::capture(
        &state.db,
        &tenant_id,
        &call_chain.root_task_id.to_string(),
        headers
            .get(crate::services::data_scope::SCOPE_HEADER)
            .and_then(|v| v.to_str().ok()),
    )
    .await;

    // Delegate to orchestrator
    let mut result = match skill_orchestrator::execute_skill(
        &state,
        &auth_ctx,
        &request,
        &correlation_id,
        &crate::services::run_controls::RunScope::from_chain(
            &call_chain,
            &current_task_id_str,
            trace_root_kind,
        )
        // An execution on a verified chain continues its run; any other
        // starts one for its credential if the run is new.
        .started_by(crate::services::run_controls::run_starter(
            &auth_ctx,
            trace_continuity == crate::services::a2a_delegation::TraceContinuity::Verified,
        )),
        // The skill route received the request: the execution is the action.
        crate::services::run_controls::StepKind::Action,
    )
    .await
    {
        Ok(r) => r,
        Err(e) => {
            // Audit the refusal. Orchestrator errors — access-denied (skill
            // not assigned), quota exceeded, disabled, approval-token — return
            // 4xx WITHOUT a success row, so they were invisible in Mission
            // Control and had no proxy_log to open. Stamp one so every refused
            // call is on the record with the same forensic detail (target,
            // client IP via auth, skill id, group, bodies).
            let duration_ms = start.elapsed().as_secs_f64() * 1000.0;
            let group = usage::resolve_target_group(&state.db, &auth_ctx, &tenant_id).await;
            let mut log = proxy_logger::build_proxy_log(
                constants::proxy::SKILL,
                &auth_ctx,
                "POST",
                "/v1/proxy/skills/execute",
                Some(crate::constants::system_server::agent_skill::BASE_URL),
                resolved_skill_id.as_deref(),
                e.status_code,
                duration_ms,
                false,
                Some(&e.message),
                &correlation_id,
            );
            log.skill_id = resolved_skill_id.clone();
            log.resource_group_id = group.as_ref().map(|g| g.id.clone());
            // Name the gate that refused, so the row is
            // sealed as the verdict it was (denied / awaiting_human /
            // engine_failure) and not as a failed execution.
            log.proxy_subtype = e.refusal.map(str::to_owned);
            log.request_body_preview = Some(proxy_logger::body_preview(&request_body_for_log));
            log.request_size_bytes = Some(request_body_for_log.len() as i32);
            let resp_str = e
                .body
                .as_ref()
                .map(|b| b.to_string())
                .unwrap_or_else(|| e.message.clone());
            log.response_body_preview = Some(proxy_logger::body_preview(&resp_str));
            log.response_size_bytes = Some(resp_str.len() as i32);
            // Compliance: an authorization/access refusal — skill not
            // assigned to the group, or execution disabled — is a SOC 2
            // logical-access-control event → soc2.cc6.6. (Quota refusals
            // carry "limit exceeded" and are excluded here; they're a
            // rate-limit surface, not access control.) This surfaces the
            // Ops "not assigned" 403 in Mission Control's Governance
            // "Policy Denials" and the compliance export.
            if e.argument_policy_denied || skill_refusal_is_access_denial(e.status_code, &e.message)
            {
                proxy_logger::apply_policy_deny(&mut log);
            }
            proxy_logger::apply_call_chain(
                &mut log,
                &call_chain,
                &current_task_id_str,
                trace_continuity,
                trace_root_kind,
            );
            state
                .background_writer
                .write_proxy_log(state.db.clone(), log);
            return skill_error_to_response(e);
        }
    };

    // Every gate has passed and the execution starts now. For a two-phase
    // system the `planned` record is sealed before the outcome.
    crate::services::evidence::planned::dispatching(
        &state.db,
        &tenant_id,
        Some(result.group_id.as_str()),
        constants::proxy::SKILL,
        format!("skill:{}", result.skill_id),
        Some(call_chain.root_task_id.to_string()),
    );

    // Skill output guardrails — surface "skill", direction Output.
    // The orchestrator returns `skill_prompt` (the prompt content
    // the calling agent will inject into the LLM) and
    // `skill_description`. Both are text the operator's policy may
    // want to scan / redact before it reaches the agent.
    let combined_output = {
        let mut s = String::new();
        if let Some(p) = &result.skill_prompt {
            s.push_str(p);
        }
        if let Some(d) = &result.skill_description {
            if !s.is_empty() {
                s.push('\n');
            }
            s.push_str(d);
        }
        s
    };
    if !combined_output.is_empty() {
        let configs = state
            .guardrails_cache
            .get_configs(
                &tenant_id,
                auth_ctx.guardrail_user_id().as_deref(),
                auth_ctx.guardrail_group_id(),
                &state.db,
            )
            .await;
        let configs = match configs {
            Ok(c) => c,
            Err(e) => {
                // Fail closed on the OUTPUT surface too: an
                // unchecked skill result is exactly the content output
                // guardrails exist to redact.
                tracing::error!(error = %e, "guardrail configs unavailable for skill output");
                // The execution was dispatched (RUNNING): settle it, or it
                // holds a concurrency slot for good.
                skill_orchestrator::settle_unrun(
                    &state.db,
                    &tenant_id,
                    &mut result,
                    "guardrails_unavailable",
                    "guardrail configs unavailable for the skill output check",
                )
                .await;
                return refuse_unchecked_skill(
                    &row_ctx,
                    Some(&result.skill_id),
                    Some(&result.execution_id),
                    Some(&result.group_id),
                    "guardrails_unavailable: refusing to return an unchecked skill result",
                );
            }
        };
        if !configs.is_empty() {
            match guardrails::check_output(&combined_output, &configs, "skill", &state.http_client)
                .await
            {
                guardrails::CheckOutcome::Block(violation) => {
                    tracing::warn!(
                        check = %violation.check,
                        config = %violation.config_name,
                        "Skill output guardrail violation — 403 returned"
                    );
                    proxy_metrics::record_guardrail_block("skill", &violation.check);
                    let msg = format!(
                        "Skill output blocked by guardrail '{}' (check={}): {}",
                        violation.config_name, violation.check, violation.message
                    );
                    let req_json = Some(request_body_for_log.clone());
                    let duration_ms = start.elapsed().as_secs_f64() * 1000.0;
                    write_skill_guardrail_block(
                        &state,
                        &auth_ctx,
                        &correlation_id,
                        Some(result.group_id.as_str()),
                        Some(result.skill_id.as_str()),
                        &violation.check,
                        &msg,
                        req_json.as_deref(),
                        duration_ms,
                        &call_chain,
                        &current_task_id_str,
                        trace_continuity,
                        trace_root_kind,
                    );
                    // Dispatched but refused before it ran: settle it.
                    skill_orchestrator::settle_unrun(
                        &state.db,
                        &tenant_id,
                        &mut result,
                        "guardrail_blocked",
                        &msg,
                    )
                    .await;
                    return error_response(axum::http::StatusCode::FORBIDDEN, &msg);
                }
                guardrails::CheckOutcome::Redacted {
                    content: redacted,
                    warnings,
                } => {
                    // Split the redacted blob back on the `\n`
                    // separator. Two slots: skill_prompt then
                    // skill_description (only the slots that
                    // were non-empty when we concatenated).
                    let mut iter = redacted.split('\n');
                    if result.skill_prompt.is_some() {
                        if let Some(p) = iter.next() {
                            result.skill_prompt = Some(p.to_string());
                        }
                    }
                    if result.skill_description.is_some() {
                        if let Some(d) = iter.next() {
                            result.skill_description = Some(d.to_string());
                        }
                    }
                    for w in &warnings {
                        tracing::warn!(target: "guardrails", check=%w.check, surface=%w.surface, "{}", w.message);
                    }
                }
                guardrails::CheckOutcome::Warn { warnings } => {
                    for w in &warnings {
                        tracing::warn!(target: "guardrails", check=%w.check, surface=%w.surface, "{}", w.message);
                    }
                }
                guardrails::CheckOutcome::Pass => {}
            }
        }
    }

    // --- Inline skill execution (REST parity with the MCP tools/call path) ---
    // The orchestrator only creates the RUNNING execution record and returns
    // the governance decision + skill prompt. `run_and_settle` runs the
    // skill's code, settles the execution and governs its output (the same
    // helper both inbound A2A paths use).
    let (runner_output, output_pii_redacted) = match skill_orchestrator::run_and_settle(
        &state,
        &auth_ctx,
        &tenant_id,
        &mut result,
        request.input_params.as_ref(),
    )
    .await
    {
        skill_orchestrator::Settlement::Ran {
            output,
            pii_redacted,
        } => (output, pii_redacted),
        skill_orchestrator::Settlement::GuardrailsUnavailable(e) => {
            tracing::error!(error = %e, "guardrail configs unavailable for skill output");
            return refuse_unchecked_skill(
                &row_ctx,
                Some(&result.skill_id),
                Some(&result.execution_id),
                Some(&result.group_id),
                "guardrails_unavailable: refusing to return an unchecked skill result",
            );
        }
        skill_orchestrator::Settlement::OutputBlocked { check, message } => {
            let req_json = Some(request_body_for_log.clone());
            let duration_ms = start.elapsed().as_secs_f64() * 1000.0;
            write_skill_guardrail_block(
                &state,
                &auth_ctx,
                &correlation_id,
                Some(result.group_id.as_str()),
                Some(result.skill_id.as_str()),
                &check,
                &message,
                req_json.as_deref(),
                duration_ms,
                &call_chain,
                &current_task_id_str,
                trace_continuity,
                trace_root_kind,
            );
            return error_response(axum::http::StatusCode::FORBIDDEN, &message);
        }
    };

    let duration_ms = start.elapsed().as_secs_f64() * 1000.0;
    let message = result.message();
    // 200 for a run that has ended (`execution_status` says how), 202 for
    // one that is still in progress. A held call never gets here: its
    // answer is the approval envelope, with 202 or 428.
    let answered = if result.execution_status == "RUNNING" {
        StatusCode::ACCEPTED
    } else {
        StatusCode::OK
    };

    // Background: proxy log
    let mut log = proxy_logger::build_proxy_log(
        constants::proxy::SKILL,
        &auth_ctx,
        "POST",
        "/v1/proxy/skills/execute",
        Some(crate::constants::system_server::agent_skill::BASE_URL),
        Some(&result.skill_id),
        answered.as_u16(),
        duration_ms,
        true,
        None,
        &correlation_id,
    );
    log.skill_id = Some(result.skill_id.clone());
    log.skill_execution_id = Some(result.execution_id.clone());
    log.user_agent = proxy_logger::extract_user_agent(&headers);
    // Stamp the resolved skill's group so RG-keyed compliance
    // frameworks tag skill success rows.
    log.resource_group_id = Some(result.group_id.clone());
    // Body previews so the skill success row carries the same forensic
    // detail (request input + returned output) as the LLM/MCP surfaces.
    log.request_body_preview = Some(proxy_logger::body_preview(&request_body_for_log));
    log.request_size_bytes = Some(request_body_for_log.len() as i32);
    let resp_preview = serde_json::json!({
        "execution_id": result.execution_id.clone(),
        "skill_id": result.skill_id.clone(),
        "skill_name": result.skill_name.clone(),
        "execution_status": result.execution_status.clone(),
        "message": message.clone(),
        "output": runner_output.clone(),
    })
    .to_string();
    log.response_body_preview = Some(proxy_logger::body_preview(&resp_preview));
    log.response_size_bytes = Some(resp_preview.len() as i32);
    // Output PII redaction is a governed event: flag the row so it surfaces in
    // Mission Control's Governance and the RG's PII compliance frameworks tag it
    // (a redact scrubs the output but is otherwise a success — without this
    // the audit row looked like an ordinary clean run).
    if output_pii_redacted {
        proxy_logger::apply_pii_detected(&mut log);
    }
    // Stamp the call chain on the
    // successful-execution audit row too. The skill's own LLM and
    // MCP calls (made by the agent the skill dispatches to) carry
    // this same root_task_id, so the trace timeline renders the
    // whole skill_execution → LLM → MCP tree as a single trace.
    proxy_logger::apply_call_chain(
        &mut log,
        &call_chain,
        &current_task_id_str,
        trace_continuity,
        trace_root_kind,
    );
    state
        .background_writer
        .write_proxy_log(state.db.clone(), log);

    // Background: proxy metric
    let metric = proxy_logger::build_proxy_metric(
        constants::proxy::SKILL,
        &auth_ctx,
        "POST",
        "/v1/proxy/skills/execute",
        "skill_exec",
        Some(&result.skill_id),
        answered.as_u16(),
        duration_ms,
        true,
        &correlation_id,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    );
    state
        .background_writer
        .write_proxy_metric(state.db.clone(), metric);

    // Background: usage log
    // Resolve group for usage logging (we already have the group info from the result)
    let group = usage::resolve_target_group(&state.db, &auth_ctx, &tenant_id).await;
    usage::write_skill_usage_log(
        &state.background_writer,
        state.db.clone(),
        &tenant_id,
        &auth_ctx,
        group.as_ref(),
        &result.skill_id,
        &result.skill_name,
        "/v1/proxy/skills/execute",
        &correlation_id,
        answered.as_u16(),
        duration_ms,
    );
    if let Some(ref g) = group {
        usage::spawn_alert_evaluation(
            state.db.clone(),
            tenant_id.clone(),
            g.clone(),
            "skill".into(),
            Some(correlation_id.clone()),
        );
    }

    let response = SkillExecuteResponse {
        execution_id: result.execution_id,
        skill_id: result.skill_id,
        skill_name: result.skill_name,
        skill_version: result.skill_version,
        execution_status: result.execution_status,
        correlation_id: result.correlation_id,
        message,
        group_id: result.group_id,
        group_name: result.group_name,
        allowed_tools: result.allowed_tools,
        recommended_model: result.recommended_model,
        skill_prompt: result.skill_prompt,
        skill_description: result.skill_description,
        max_tool_calls_per_execution: result.max_tool_calls_per_execution,
        output: runner_output,
    };

    // Attach usage warning headers if quota is approaching limits
    let mut resp = (answered, Json(response)).into_response();
    for (name, value) in &result.warning_headers {
        if let (Ok(n), Ok(v)) = (
            axum::http::HeaderName::from_bytes(name.as_bytes()),
            axum::http::HeaderValue::from_str(value),
        ) {
            resp.headers_mut().insert(n, v);
        }
    }
    resp
}

// ============================================================================
// Get Execution Status
// ============================================================================

#[utoipa::path(
    get,
    path = "/v1/proxy/skills/executions/{execution_id}",
    tag = "Skills",
    summary = "Get skill execution status",
    description = "The status of a skill execution, for the caller that started it (the end user, or the resource group a key without an end user or an agent acts in). Any other caller is answered as for an execution that does not exist.",
    responses(
        (status = 200, description = "Execution status", body = Value),
        (status = 404, description = "No such execution, or not the caller's")
    ),
    params(("execution_id" = String, Path, description = "Execution ID")),
    security(("api_key" = []), ("bearer" = []))
)]
pub(crate) async fn get_execution(
    State(state): State<SharedState>,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Path(execution_id): Path<String>,
) -> Response {
    let tenant_id = match route_helpers::extract_tenant_id(tenant) {
        Ok(t) => t,
        Err(r) => return r,
    };

    // The execution is read by the caller that started it.
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
        return error_response(e.status_code(), &e.to_string());
    }

    let result = match skill_orchestrator::get_execution_status(
        &state.db,
        &execution_id,
        &tenant_id,
        &auth_ctx,
    )
    .await
    {
        Ok(r) => r,
        Err(e) => return skill_error_to_response(e),
    };

    let body = json!({
        "execution_id": result.execution_id,
        "skill_id": result.skill_id,
        "skill_name": result.skill_name,
        "execution_status": result.execution_status,
        "correlation_id": result.correlation_id,
        "message": result.message,
    });

    (StatusCode::OK, Json(body)).into_response()
}

// ============================================================================
// Cancel Execution
// ============================================================================

#[utoipa::path(
    post,
    path = "/v1/proxy/skills/executions/{execution_id}/cancel",
    tag = "Skills",
    summary = "Cancel a skill execution",
    description = "Cancel a running skill execution. Only the caller that started it can.",
    params(("execution_id" = String, Path, description = "Execution ID")),
    responses(
        (status = 200, description = "Cancellation result", body = Value),
        (status = 404, description = "No such execution, or not the caller's"),
        (status = 409, description = "The execution is not running")
    ),
    security(("api_key" = []), ("bearer" = []))
)]
pub(crate) async fn cancel_execution(
    State(state): State<SharedState>,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Path(execution_id): Path<String>,
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
        return error_response(e.status_code(), &e.to_string());
    }

    let cancelled_by = auth_ctx.gateway_username.as_deref().unwrap_or("system");

    let result = match skill_orchestrator::cancel_execution(
        &state.db,
        &execution_id,
        &tenant_id,
        &auth_ctx,
        cancelled_by,
    )
    .await
    {
        Ok(r) => r,
        Err(e) => return skill_error_to_response(e),
    };

    let body = json!({
        "execution_id": result.execution_id,
        "skill_id": result.skill_id,
        "skill_name": result.skill_name,
        "execution_status": result.execution_status,
        "correlation_id": result.correlation_id,
        "message": result.message,
    });

    (StatusCode::OK, Json(body)).into_response()
}

// ============================================================================
// Skill Catalog
// ============================================================================

#[derive(serde::Deserialize)]
pub struct CatalogQuery {
    pub category: Option<String>,
    pub tag: Option<String>,
    /// Set to "llm" to get a compact text format optimized for LLM context injection.
    pub format: Option<String>,
}

#[utoipa::path(
    get,
    path = "/v1/proxy/skills/catalog",
    tag = "Skills",
    summary = "List available skills",
    description = "Lightweight catalog for skill discovery (Progressive Disclosure Level 1). \
                   Returns published, enabled skills accessible to the authenticated user's group. \
                   ~50-100 tokens per entry — designed for agent context injection.",
    responses((status = 200, description = "Skill catalog", body = Value)),
    security(("api_key" = []), ("bearer" = []))
)]
pub(crate) async fn get_catalog(
    State(state): State<SharedState>,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Query(params): Query<CatalogQuery>,
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
        return error_response(e.status_code(), &e.to_string());
    }

    let catalog = match skill_orchestrator::get_catalog(
        &state.db,
        &auth_ctx,
        &tenant_id,
        params.category.as_deref(),
        params.tag.as_deref(),
    )
    .await
    {
        Ok(c) => c,
        Err(e) => return skill_error_to_response(e),
    };

    // LLM-optimized text format: compact, one line per skill, max ~15K chars
    if params.format.as_deref() == Some("llm") {
        let mut text = String::from("Available Skills:\n");
        let mut budget = 15000usize;
        let total = catalog.skills.len();

        for (i, skill) in catalog.skills.iter().enumerate() {
            let name = skill.get("name").and_then(|v| v.as_str()).unwrap_or("?");
            let desc = skill
                .get("description")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let category = skill.get("category").and_then(|v| v.as_str());

            let mut line = format!("- {name}: {desc}");
            if let Some(cat) = category {
                line.push_str(&format!(" [category: {cat}]"));
            }
            line.push('\n');

            if line.len() > budget {
                text.push_str(&format!("... and {} more skills\n", total - i));
                break;
            }
            budget -= line.len();
            text.push_str(&line);
        }

        return (
            StatusCode::OK,
            [(
                axum::http::header::CONTENT_TYPE,
                "text/plain; charset=utf-8",
            )],
            text,
        )
            .into_response();
    }

    let body = json!({
        "skills": catalog.skills,
        "total": catalog.total,
        "group_id": catalog.group_id,
        "group_name": catalog.group_name,
    });

    (StatusCode::OK, Json(body)).into_response()
}

/// Classify a refused skill execution for compliance tagging.
///
/// An authorization/access refusal — the skill is not assigned to the target
/// group, or execution is disabled — surfaces at HTTP 403 and is a SOC 2
/// logical-access-control event (soc2.cc6.6). Quota refusals also return 403
/// but carry a "limit exceeded"/"quota" message; those belong to the
/// rate-limit surface, not access control, so they are excluded here.
fn skill_refusal_is_access_denial(status_code: u16, message: &str) -> bool {
    if status_code != 403 {
        return false;
    }
    let m = message.to_lowercase();
    !(m.contains("limit exceeded") || m.contains("quota"))
}

#[cfg(test)]
mod refusal_classification_tests {
    use super::skill_refusal_is_access_denial;

    #[test]
    fn not_assigned_403_is_access_denial() {
        assert!(skill_refusal_is_access_denial(
            403,
            "Skill is not assigned to the target group"
        ));
    }

    #[test]
    fn execution_disabled_403_is_access_denial() {
        assert!(skill_refusal_is_access_denial(
            403,
            "Skill execution is disabled for this group"
        ));
    }

    #[test]
    fn quota_403_is_not_access_denial() {
        assert!(!skill_refusal_is_access_denial(
            403,
            "Per-skill daily limit exceeded"
        ));
        assert!(!skill_refusal_is_access_denial(
            403,
            "Group skill quota reached"
        ));
    }

    #[test]
    fn non_403_is_not_access_denial() {
        assert!(!skill_refusal_is_access_denial(404, "Skill not found"));
        assert!(!skill_refusal_is_access_denial(400, "Bad request"));
        assert!(!skill_refusal_is_access_denial(202, "started"));
    }
}
