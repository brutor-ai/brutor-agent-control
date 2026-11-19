// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

use chrono::Utc;
use sha2::{Digest, Sha256};

use crate::models::auth::{AuthContext, AuthType};
use crate::models::db::{ProxyLogInsert, ProxyMetricInsert};
use crate::services::a2a_delegation::{CallChain, TraceContinuity};

/// Build a proxy log entry from request/response data.
pub fn build_proxy_log(
    proxy_type: &str,
    auth: &AuthContext,
    method: &str,
    endpoint: &str,
    target_url: Option<&str>,
    server_id: Option<&str>,
    status_code: u16,
    duration_ms: f64,
    success: bool,
    error: Option<&str>,
    correlation_id: &str,
) -> ProxyLogInsert {
    // Precedence: the client's asserted conversation (HMAC-derived, so it
    // cannot name another principal's session) beats the auth layer's own
    // session key, which beats the per-request correlation id.
    //
    // That last fallback is why this column used to be useless: `session_key`
    // is never set, so every row got its own correlation id and "session"
    // meant "request". A real conversation now groups its rows.
    let session_id = crate::middleware::run_end::resolved_session()
        .or_else(|| auth.session_key.clone())
        .unwrap_or_else(|| correlation_id.to_string());

    let asserted_turn = crate::middleware::run_end::asserted_turn();
    let asserted_step = crate::middleware::run_end::asserted_step();
    let guardrail = crate::middleware::run_end::guardrail_evidence();
    let trace = crate::middleware::run_end::asserted_trace();
    let provenance = crate::middleware::run_end::tool_provenance();

    let mut log = ProxyLogInsert {
        // A two-phase action reserved this row's id when it
        // sealed its `planned` record, so the outcome shares its action_id.
        id: crate::services::evidence::request::take_reserved_log_id(proxy_type)
            .unwrap_or_else(|| ulid::Ulid::new().to_string()),
        tenant_id: auth.tenant_id.clone(),
        timestamp: Utc::now(),
        proxy_type: proxy_type.to_string(),
        proxy_subtype: None,
        skill_id: None,
        skill_execution_id: None,
        correlation_id: Some(correlation_id.to_string()),
        parent_request_id: None,
        request_id: Some(correlation_id.to_string()),
        end_user_session_id: session_id,
        gateway_user_id: auth.gateway_user_id.clone(),
        gateway_user_username: auth.gateway_username.clone(),
        idp_user_id: auth.idp_user_id.clone(),
        idp_user_username: auth.idp_username.clone(),
        idp_user_email: auth.idp_email.clone(),
        client_ip: auth.client_ip.clone(),
        user_agent: None,
        server_id: server_id.map(String::from),
        idp_server_id: None,
        resource_group_id: None,
        // Per-key attribution from the auth context.
        api_key_id: auth.api_key_id.clone(),
        api_key_name: auth.api_key_name.clone(),
        http_method: method.to_string(),
        endpoint: endpoint.to_string(),
        target_url: target_url.map(String::from),
        request_size_bytes: None,
        request_headers: None,
        request_body_preview: None,
        http_status_code: Some(status_code as i32),
        response_size_bytes: None,
        response_headers: None,
        response_body_preview: None,
        success,
        error_message: error.map(String::from),
        error_type: if success {
            None
        } else {
            Some("proxy_error".into())
        },
        duration_ms: Some(duration_ms),
        mcp_method: None,
        mcp_request_id: None,
        mcp_capability_name: None,
        llm_model_requested: None,
        llm_model_used: None,
        llm_provider: None,
        llm_prompt_tokens: None,
        llm_completion_tokens: None,
        llm_total_tokens: None,
        llm_streaming: None,
        llm_temperature: None,
        llm_max_tokens: None,
        llm_function_calling: None,
        llm_used_fallback: None,
        llm_fallback_reason: None,
        llm_estimated_cost_usd: None,
        media_images_generated: None,
        media_audio_chars: None,
        media_audio_seconds_input: None,
        media_video_seconds_output: None,
        llm_cache_hit: None,
        llm_cache_layer: None,
        llm_tokens_saved: None,
        llm_cache_similarity_score: None,
        prompt_cache_write_tokens: None,
        prompt_cache_read_tokens: None,
        prompt_cache_savings_usd: None,
        rag_enabled: None,
        rag_chunks_retrieved: None,
        rag_context_tokens: None,
        rag_collection_ids: None,
        prompt_fingerprint: None,
        rag_top_similarity_score: None,
        rag_retrieval_duration_ms: None,
        oauth_flow_type: None,
        oauth_scope: None,
        root_task_id: None,
        delegation_depth: None,
        trace_root_kind: None,
        trace_continuity: None,
        // Read from the task-local the run-end
        // middleware scoped for this request, so every proxy surface that
        // builds a log through here (LLM, MCP, A2A, Skills, VMCP) honours
        // `X-Brutor-Run-End` without each one having to know about it.
        // `None` outside a request scope, which is what tests and background
        // work want.
        run_end_state: crate::middleware::run_end::requested_run_end(),
        // The outcome the client reported on this request.
        outcome_id: crate::middleware::run_end::requested_outcome_id(),
        source: crate::services::otel_ingest::SOURCE_GATEWAY.to_string(),
        // Data residency. Default to None
        // on every build; the route handler stamps both fields
        // after the residency check runs (Allowed → region +
        // residency_class="allowed"; Blocked → NULL region +
        // "blocked"; no-profile path → NULL region +
        // "not_evaluated").
        region_routed_to: None,
        residency_class: None,
        // Compliance tags. Default None so
        // the audit writer leaves the column NULL unless the
        // route handler calls `apply_compliance_tags` with a
        // resolved profile + context.
        compliance_tags: None,
        // Orchestrator outcome flags.
        // Default false. Helpers below (`apply_guardrail_block`
        // etc.) flip them when the corresponding orchestrator
        // event fires. `is_auth_event` defaults TRUE for OAuth
        // proxy rows because every OAuth flow is by definition
        // an auth event — saves per-site wiring across all four
        // OAuth audit-row builders. Other surfaces leave it
        // false and the route handler stamps via
        // `apply_auth_event(...)` when appropriate.
        guardrail_blocked: false,
        argument_policy_denied: false,
        pii_detected: false,
        is_auth_event: proxy_type == "oauth",
        current_task_id: None,
        parent_task_id: None,
        // Agent Identity. The actor (agent) + subject are auto-stamped
        // from the AuthContext on EVERY row (block + success, all surfaces) so
        // no per-site wiring is needed. The per-agent authz decision + matched
        // grant are set only where `decide_for_agent` runs (the enforcement
        // hook) via `apply_agent_authz_decision`.
        actor_agent_id: auth.actor_agent_id.clone(),
        subject_id: if auth.actor_agent_id.is_some() {
            auth.api_key_user_id.map(|u| u.to_string())
        } else {
            None
        },
        subject_kind: if auth.actor_agent_id.is_some() {
            Some(
                if auth.api_key_user_id.is_some() {
                    "user"
                } else {
                    "service"
                }
                .to_string(),
            )
        } else {
            None
        },
        agent_authz_decision: None,
        agent_grant_id: None,
        semantic_policy_id: None,
        // Client-asserted turn identity, read from the request-scoped
        // task-local set by the run_end middleware. None outside a request
        // scope (background tasks, tests) and when the client asserted
        // nothing, in which case the ledger infers turns and says so.
        // Stamped by audit_chain::stamp_batch at flush time, not
        // here: a row shed under backpressure must not consume a sequence
        // number, or the gap would read as a deletion.
        chain_id: None,
        chain_seq: None,
        prev_hash: None,
        row_hash: None,
        chain_version: None,
        step_id: asserted_step.as_ref().map(|(id, _)| id.clone()),
        step_name: asserted_step.as_ref().and_then(|(_, n)| n.clone()),
        turn_id: asserted_turn.as_ref().map(|(id, _)| id.clone()),
        turn_seq: asserted_turn.as_ref().and_then(|(_, seq)| *seq),
        // Decision record. NULL means "no such decision was taken on
        // this call" — the apply_* helpers below stamp them where one was.
        argument_policy_id: None,
        // Evidence recorded by the orchestrator when a check hit.
        // Block sites that hold the violation overwrite this with the same
        // values via `apply_guardrail_violation`; this is what the sites that
        // only receive a boolean get instead of nothing.
        guardrail_config_id: guardrail.as_ref().map(|(id, _, _)| id.clone()),
        guardrail_check: guardrail.as_ref().map(|(_, check, _)| check.clone()),
        guardrail_action: guardrail.as_ref().map(|(_, _, action)| action.clone()),
        // The checks that did NOT run. Recorded by the orchestrator at
        // the point it decided to skip, so every audit row carries it without
        // each block site having to know.
        guardrails_skipped: {
            let skipped = crate::middleware::run_end::guardrails_skipped();
            if skipped.is_empty() {
                None
            } else {
                Some(serde_json::Value::Array(
                    skipped
                        .into_iter()
                        .map(|(config, check, reason)| {
                            serde_json::json!({
                                "config": config,
                                "check": check,
                                "reason": reason,
                            })
                        })
                        .collect(),
                ))
            }
        },
        // Checks that RAN but on a lesser rung (retried / local
        // fallback). Separate from `guardrails_skipped` so a lower-fidelity
        // pass is never counted as a full one, nor as no check at all.
        guardrails_degraded: {
            let degraded = crate::middleware::run_end::guardrails_degraded();
            if degraded.is_empty() {
                None
            } else {
                Some(serde_json::Value::Array(
                    degraded
                        .into_iter()
                        .map(|(config, check, tier)| {
                            serde_json::json!({
                                "config": config,
                                "check": check,
                                "tier": tier,
                            })
                        })
                        .collect(),
                ))
            }
        },
        autonomy_level: None,
        autonomy_provenance: None,
        // The approval that authorised this call, recorded by the
        // approval path into a request-scoped task-local. This is what
        // makes approval -> action walkable forwards.
        consumed_approval_request_id: crate::middleware::run_end::consumed_approval(),
        // The provider's own request id, recorded from the upstream
        // response. The only join between our audit trail and the vendor's.
        upstream_request_id: crate::middleware::run_end::upstream_request_id(),
        // The state a quota/budget decision was taken against, so
        // "why was this allowed" is answerable and not just "it was".
        pre_action_state: crate::middleware::run_end::pre_action_state(),
        request_content_hash: None,
        response_content_hash: None,
        trace_id: trace.as_ref().map(|(t, _)| t.clone()),
        span_id: trace.as_ref().map(|(_, s)| s.clone()),
        server_version: provenance.as_ref().and_then(|(v, _)| v.clone()),
        tool_definition_hash: provenance.as_ref().and_then(|(_, h)| h.clone()),
        deciding_model: None,
        record_id: None,
        a2a_task_state: None,
        // Clear-safe request facts only: the portal's
        // conversation handle (validated ULID) and the approval this call
        // enqueued. Never identity.
        custom_metadata: crate::services::evidence::request::row_metadata(),
        tags: None,
        agent_impl_name: None,
        agent_impl_version: None,
        agent_impl_build: None,
        agent_impl_source: None,
        client_label: None,
        credential_kind: None,
        credential_assurance: None,
        credential_ref: None,
        credential_policy_outcome: None,
    };
    stamp_credential(&mut log, auth);
    // The caller's implementation release and client
    // label, on EVERY row of every surface. The carriers ride on the
    // AuthContext (snapshotted from the request scope when it was built), so
    // a row built after a spawn — a streaming tail — carries them too. This
    // is the only writer of those columns (`tests/agent_impl_guards.rs`).
    crate::services::agent_impl::stamp(&mut log, &auth.agent_impl);
    log
}

/// The credential that made the call, on every row of every surface: its
/// kind, level and reference as authentication stated them, the identity
/// judge's verdict when a surface judged the call, and the identity provider
/// of a workload credential. A 401 row records no credential: the context
/// it is built from is not one authentication accepted.
pub fn stamp_credential(log: &mut ProxyLogInsert, auth: &AuthContext) {
    if log.http_status_code == Some(401) {
        log.credential_kind = None;
        log.credential_assurance = None;
        log.credential_ref = None;
        log.credential_policy_outcome = None;
        return;
    }
    log.credential_kind = Some(auth.credential_kind.as_str().to_string());
    log.credential_assurance = auth.credential_assurance.map(|l| l.as_str().to_string());
    log.credential_ref = auth.credential_ref.clone();
    log.credential_policy_outcome = auth
        .identity_verdict
        .map(|verdict| verdict.outcome.as_str().to_string());
    if auth.credential_kind.is_workload()
        && let Some(idp) = &auth.credential_detail.idp_server_id
    {
        log.idp_server_id = Some(idp.clone());
    }
}

/// Stamp the per-agent runtime-authz decision + matched grant onto a proxy log
/// (the `agent_authz_block` audit row, or any row where a decision was made).
/// Actor/subject are already set by `build_proxy_log` from the AuthContext.
pub fn apply_agent_authz_decision(
    log: &mut ProxyLogInsert,
    outcome: &crate::services::agent_authz::Outcome,
    blocked: bool,
) {
    if blocked {
        log.proxy_subtype = Some(crate::constants::proxy_subtype::AGENT_AUTHZ_BLOCK.to_string());
    }
    log.agent_authz_decision = Some(outcome.decision.as_str().to_string());
    log.agent_grant_id = outcome.matched_grant_id.clone();
    // The authority the decision was taken under, and whether that
    // authority was current. `None` when the call was denied before the
    // autonomy gate ran, which is recorded as absent rather than guessed.
    log.autonomy_level = outcome.autonomy_level.map(str::to_string);
    log.autonomy_provenance = outcome.autonomy_provenance.map(str::to_string);
}

/// Stamp the autonomy decision for a caller that is **not** an agent principal.
///
/// The agent path carries this on its authz outcome; this is the same evidence
/// for the surfaces where autonomy is enforced standalone, so a suspension that
/// stopped a plain API key is as auditable as one that stopped an agent.
pub fn apply_autonomy(
    log: &mut ProxyLogInsert,
    resolution: &crate::services::autonomy::Resolution,
    blocked: bool,
) {
    if blocked {
        log.proxy_subtype = Some(crate::constants::proxy_subtype::AUTONOMY_BLOCK.to_string());
    }
    log.autonomy_level = Some(resolution.level.as_str().to_string());
    log.autonomy_provenance = Some(resolution.provenance.as_str().to_string());
}

/// Stamp a semantic-governance-policy outcome onto a proxy log. `blocked=true`
/// → an enforced block (`proxy_subtype="semantic_policy_block"`); `false` → a
/// shadow-mode would-block (`"semantic_policy_shadow"`). Stamps the policy id
/// for evidence either way.
pub fn apply_semantic_policy_decision(log: &mut ProxyLogInsert, policy_id: &str, blocked: bool) {
    log.proxy_subtype = Some(
        if blocked {
            crate::constants::proxy_subtype::SEMANTIC_POLICY_BLOCK
        } else {
            crate::constants::proxy_subtype::SEMANTIC_POLICY_SHADOW
        }
        .to_string(),
    );
    log.semantic_policy_id = Some(policy_id.to_string());
}

/// Build a complete SGP audit row for any surface — one helper so every call
/// site (LLM/MCP/A2A/skill, in/out) emits a consistent block/shadow row. The
/// caller writes it via the background writer.
#[allow(clippy::too_many_arguments)]
pub fn build_semantic_policy_log(
    auth: &AuthContext,
    proxy_type: &str,
    endpoint: &str,
    capability: Option<&str>,
    resource_group_id: Option<String>,
    correlation_id: &str,
    duration_ms: f64,
    policy_id: &str,
    blocked: bool,
    reason: &str,
) -> ProxyLogInsert {
    let mut log = build_proxy_log(
        proxy_type,
        auth,
        "POST",
        endpoint,
        None,
        None,
        if blocked { 403 } else { 200 },
        duration_ms,
        !blocked,
        Some(reason),
        correlation_id,
    );
    log.mcp_capability_name = capability.map(str::to_owned);
    log.resource_group_id = resource_group_id.or_else(|| auth.resolved_group_id.clone());
    apply_semantic_policy_decision(&mut log, policy_id, blocked);
    log
}

/// Return the body string for proxy log storage, stripped of null bytes
/// (PostgreSQL text columns reject `\0`).
/// Populate the call-chain audit columns on a `ProxyLogInsert`. Invoked
/// after `build_proxy_log` on every audit-row site in routes that have
/// already established a chain via `a2a_delegation::parse_or_mint` at
/// request entry.
///
/// - `chain` is the *parent* chain (returned by `parse_or_mint`); its
///   `root_task_id` groups every span in the agent flow; its
///   `parent_task_id` is this step's parent (only meaningful when
///   `chain.depth > 0`).
/// - `current_task_id` is the ULID this step is identified by — comes
///   from `parse_or_mint` as the freshly minted node id.
/// - `continuity` records how the chain was established (audit
///   surfaces fragmented vs verified vs spoofed traces).
/// - `root_kind` is set only on root spans (i.e. when `chain.depth == 0`)
///   — passing `None` on non-root spans is the documented contract.
pub fn apply_call_chain(
    log: &mut ProxyLogInsert,
    chain: &CallChain,
    current_task_id: &str,
    continuity: TraceContinuity,
    root_kind: Option<&str>,
) {
    log.root_task_id = Some(chain.root_task_id.to_string());
    log.current_task_id = Some(current_task_id.to_string());
    log.parent_task_id = if chain.depth > 0 {
        Some(chain.parent_task_id.to_string())
    } else {
        None
    };
    log.delegation_depth = Some(chain.depth as i32);
    log.trace_root_kind = root_kind.map(str::to_owned);
    log.trace_continuity = Some(continuity.as_str().to_owned());
}

/// Decide which `trace_root_kind` to stamp on a root span. Called once
/// per request, after preflight has populated `auth_ctx.auth_type`.
/// Only root rows carry the kind; all subsequent steps inherit via the
/// chain. Shared across LLM surfaces (OpenAI-compatible, Anthropic,
/// Gemini) so the wiring is identical. Mirrors the private copy in
/// `routes/llm.rs::root_kind_for_auth`.
pub fn root_kind_for_auth(auth_type: &AuthType, is_root: bool) -> Option<&'static str> {
    if !is_root {
        return None;
    }
    match auth_type {
        AuthType::GatewayJwt => Some("llm_user"),
        AuthType::ApiKey => Some("agent_invocation"),
        _ => Some("external_api"),
    }
}

/// Stamp the residency decision onto an
/// audit row. Mirrors `apply_call_chain` in shape (mutates the
/// row in place; no return value). Three audit shapes:
///
///   * `Allowed { region: Some(r) }` → region_routed_to=r, class=allowed
///   * `Allowed { region: None }`    → region_routed_to=NULL, class=allowed
///     (upstream had no region declared;
///     tenant profile allowed the
///     unspecified case)
///   * `Blocked { reason }`          → region_routed_to=NULL, class=blocked
///
/// For the "no profile / not evaluated" path callers should
/// instead use [`apply_residency_not_evaluated`].
pub fn apply_residency_audit(
    log: &mut ProxyLogInsert,
    decision: &crate::services::residency::ResidencyDecision,
) {
    use crate::services::residency::ResidencyDecision;
    match decision {
        ResidencyDecision::Allowed { region } => {
            log.region_routed_to = region.clone();
            log.residency_class = Some(crate::services::residency::CLASS_ALLOWED.to_string());
        }
        ResidencyDecision::Blocked { .. } => {
            log.region_routed_to = None;
            log.residency_class = Some(crate::services::residency::CLASS_BLOCKED.to_string());
        }
    }
}

/// Stamp the "no residency profile" audit
/// shape. Distinct from `apply_residency_audit(Allowed)` because
/// the dashboard distinguishes "tenant has no policy" (governance
/// gap to flag) from "policy ran and allowed" (everything fine).
pub fn apply_residency_not_evaluated(log: &mut ProxyLogInsert, provider_region: Option<&str>) {
    log.region_routed_to = provider_region.map(str::to_owned);
    log.residency_class = Some(crate::services::residency::CLASS_NOT_EVALUATED.to_string());
}

/// Stamp the compliance tags onto the audit
/// row.
///
/// The runtime invokes `compliance::derive_tags(ctx)` with the
/// tenant's profile + RG flags + the in-flight event flags
/// (guardrail blocked, residency blocked, etc.) gathered from
/// the route handler's existing state. Three outcomes:
///
///   * `derive_tags` returns `None` — tenant has no compliance
///     profile. Audit row stays `compliance_tags = NULL` (the
///     partial GIN index excludes these so dashboard queries
///     stay fast).
///   * `derive_tags` returns `Some(vec![])` — profile present
///     but no regime applied to this call. We stamp the empty
///     JSONB array `[]` so the dashboard can distinguish
///     "engine ran, quiet call" (compliant-and-uneventful) from
///     "engine never ran" (governance gap).
///   * `derive_tags` returns `Some(vec![...])` — stamp the
///     array as JSONB.
pub fn apply_compliance_tags(log: &mut ProxyLogInsert, tags: Option<Vec<String>>) {
    log.compliance_tags = tags.map(|t| {
        // serde_json::Value::Array preserves order — important so
        // the dashboard reads tags in the same order the engine
        // emitted them (test-friendly + readable in raw audit rows).
        serde_json::Value::Array(t.into_iter().map(serde_json::Value::String).collect())
    });
}

/// Set the guardrail_blocked flag
/// on the audit row. Called from the guardrail orchestrator
/// when ANY check returns BLOCK on this surface. Drives the
/// compliance engine's SOC 2 CC7.2 tag.
pub fn apply_guardrail_block(log: &mut ProxyLogInsert) {
    log.guardrail_blocked = true;
}

/// Stamp *which* guardrail did *what*, not just that one did.
///
/// `apply_guardrail_block` sets a boolean that answers "was this blocked by a
/// guardrail". It cannot answer "by which config, on which check, with what
/// action" — so a tenant with nine configs could see a block and not know
/// which policy to change, and a redaction (which altered the content sent
/// upstream) was indistinguishable from a warn (which altered nothing).
///
/// Sets `guardrail_blocked` only when the action actually blocked, so a
/// redact/warn row is evidence without being a false block in the metrics.
pub fn apply_guardrail_violation(
    log: &mut ProxyLogInsert,
    violation: &crate::services::guardrails::GuardrailViolation,
) {
    log.guardrail_config_id = Some(violation.config_id.clone());
    log.guardrail_check = Some(violation.check.clone());
    log.guardrail_action = Some(violation.action.clone());
    if violation.action == "block" {
        log.guardrail_blocked = true;
    }
}

/// Set the argument_policy_denied
/// flag. Called from the argument-policy evaluator when DENY
/// fires on `tool_calls`, `mcp tools/call`, or skill input.
/// Drives SOC 2 CC6.6.
pub fn apply_policy_deny(log: &mut ProxyLogInsert) {
    log.argument_policy_denied = true;
}

/// Set the pii_detected flag.
/// Called from the PII detector when at least one category
/// matched on this surface. Drives GDPR Article 30 (when
/// paired with an identifiable subject already on the log)
/// and HIPAA PHI (when the matched categories overlap the
/// PHI set — the engine reads category context itself).
pub fn apply_pii_detected(log: &mut ProxyLogInsert) {
    log.pii_detected = true;
}

/// Set the is_auth_event flag
/// on auth-related audit rows (token issue, API-key auth,
/// login / refresh). Drives SOC 2 CC6.1.
pub fn apply_auth_event(log: &mut ProxyLogInsert) {
    log.is_auth_event = true;
}

pub fn body_preview(s: &str) -> String {
    s.replace('\0', "")
}

/// Extract user-agent from request headers.
pub fn extract_user_agent(headers: &axum::http::HeaderMap) -> Option<String> {
    headers
        .get(axum::http::header::USER_AGENT)
        .and_then(|v| v.to_str().ok())
        .map(String::from)
}

/// Fingerprint the *instructions* in a chat request.
///
/// The paper names prompt edits as one of the doors that open with no
/// release; the drift cause ladder could not name that door because nothing
/// recorded what the instructions were. This hashes every system-role
/// message (OpenAI shape), the top-level `system` (Anthropic shape, string or
/// content blocks) and `systemInstruction.parts[].text` (Gemini shape), in
/// order, so a change in the dominant fingerprint across a change point is
/// evidence of a prompt change. It is a hash of the text — the text itself is
/// never stored by this path.
pub fn instruction_fingerprint(body: &serde_json::Value) -> Option<String> {
    let mut parts: Vec<String> = Vec::new();
    fn text_of(v: &serde_json::Value) -> Option<String> {
        match v {
            serde_json::Value::String(s) => Some(s.clone()),
            serde_json::Value::Array(items) => {
                let joined: Vec<String> = items
                    .iter()
                    .filter_map(|it| it.get("text").and_then(|t| t.as_str()).map(String::from))
                    .collect();
                if joined.is_empty() {
                    None
                } else {
                    Some(joined.join("\n"))
                }
            }
            _ => None,
        }
    }
    if let Some(msgs) = body.get("messages").and_then(|m| m.as_array()) {
        for m in msgs {
            let role = m.get("role").and_then(|r| r.as_str()).unwrap_or("");
            if role == "system" || role == "developer" {
                if let Some(t) = m.get("content").and_then(text_of) {
                    parts.push(t);
                }
            }
        }
    }
    if let Some(t) = body.get("system").and_then(text_of) {
        parts.push(t);
    }
    if let Some(si) = body.get("systemInstruction") {
        if let Some(t) = si.get("parts").and_then(text_of).or_else(|| text_of(si)) {
            parts.push(t);
        }
    }
    if parts.is_empty() {
        return None;
    }
    let mut hasher = Sha256::new();
    for (i, p) in parts.iter().enumerate() {
        if i > 0 {
            hasher.update(b"\x1e");
        }
        hasher.update(p.as_bytes());
    }
    Some(format!("{:x}", hasher.finalize()))
}

/// Populate LLM-specific fields on a proxy log from the request body.
pub fn populate_llm_request_fields(log: &mut ProxyLogInsert, body: &serde_json::Value) {
    log.prompt_fingerprint = instruction_fingerprint(body);
    log.llm_temperature = body.get("temperature").and_then(|v| v.as_f64());
    log.llm_max_tokens = body
        .get("max_tokens")
        .and_then(|v| v.as_i64())
        .map(|v| v as i32);
    log.llm_function_calling = Some(
        body.get("tools")
            .and_then(|v| v.as_array())
            .map_or(false, |a| !a.is_empty())
            || body
                .get("functions")
                .and_then(|v| v.as_array())
                .map_or(false, |a| !a.is_empty()),
    );
}

/// Build a proxy metric entry.
pub fn build_proxy_metric(
    proxy_type: &str,
    auth: &AuthContext,
    method: &str,
    endpoint: &str,
    endpoint_category: &str,
    server_id: Option<&str>,
    status_code: u16,
    duration_ms: f64,
    success: bool,
    correlation_id: &str,
    request_size_bytes: Option<i32>,
    response_size_bytes: Option<i32>,
    request_headers_count: Option<i32>,
    response_headers_count: Option<i32>,
    user_agent: Option<&str>,
    llm_data: Option<serde_json::Value>,
    mcp_data: Option<serde_json::Value>,
) -> ProxyMetricInsert {
    let now = Utc::now();
    use chrono::Datelike;
    use chrono::Timelike;

    let user_agent_hash = user_agent.map(|ua| {
        let mut hasher = Sha256::new();
        hasher.update(ua.as_bytes());
        format!("{:x}", hasher.finalize())
    });

    ProxyMetricInsert {
        id: ulid::Ulid::new().to_string(),
        tenant_id: auth.tenant_id.clone(),
        timestamp: now,
        proxy_type: proxy_type.to_string(),
        request_id: correlation_id.to_string(),
        correlation_id: Some(correlation_id.to_string()),
        server_id: server_id.map(String::from),
        http_method: method.to_string(),
        endpoint: endpoint.to_string(),
        endpoint_category: endpoint_category.to_string(),
        total_duration_ms: Some(duration_ms),
        http_status_code: Some(status_code as i32),
        success,
        error_type: None,
        request_size_bytes,
        response_size_bytes,
        request_headers_count,
        response_headers_count,
        gateway_user_id: auth.gateway_user_id.clone(),
        gateway_username: auth.gateway_username.clone(),
        end_user_type: Some(auth.auth_type.to_string()),
        client_ip: auth.client_ip.clone(),
        user_agent_hash,
        llm_data,
        mcp_data,
        request_hour: now.hour() as i32,
        request_day_of_week: now.weekday().num_days_from_monday() as i32,
    }
}

/// What stands in a logged body where an approval token was.
pub const APPROVAL_TOKEN_REDACTED: &str = "[REDACTED]";

/// Take every approval token out of what an audit row says.
///
/// A one-time approval token is a credential of its requester until it is
/// redeemed, and a request can carry one that is kept (the call was held
/// for another approval). It arrives in the `X-Approval-Token` header
/// ([`sanitize_headers`] redacts that), in the `approval_token` argument of
/// `skills__run_script`, in the skill route's body, and it leaves in the
/// answer of the approval poll. No row holds one: the value of every
/// `approval_token` key in the row's request and response previews is
/// replaced. The background writer calls this for every row, so no surface
/// has to remember to.
pub fn scrub_approval_tokens(log: &mut ProxyLogInsert) {
    for text in [
        &mut log.request_body_preview,
        &mut log.response_body_preview,
        &mut log.error_message,
    ] {
        if let Some(body) = text.as_mut() {
            if body.contains("approval_token") {
                *body = scrub_approval_token_values(body);
            }
        }
    }
}

/// `text` with the string value of every `approval_token` key replaced. It
/// works on the text, not on parsed JSON: a preview is cut to a length and
/// is often not a whole document, and a body may be JSON inside a JSON
/// string (its quotes escaped).
fn scrub_approval_token_values(text: &str) -> String {
    static VALUE: std::sync::LazyLock<regex_lite::Regex> = std::sync::LazyLock::new(|| {
        // "approval_token" : "<value>", the quotes plain or escaped. A
        // token holds no quote and no backslash, so its value runs to the
        // first of either, or to the end of a cut preview.
        regex_lite::Regex::new(r#"(?P<key>\\*"approval_token\\*"\s*:\s*\\*")[^"\\]*"#)
            .expect("the approval-token pattern")
    });
    VALUE
        .replace_all(text, |caps: &regex_lite::Captures<'_>| {
            format!("{}{APPROVAL_TOKEN_REDACTED}", &caps["key"])
        })
        .into_owned()
}

/// Sanitize request headers by redacting sensitive values.
pub fn sanitize_headers(headers: &axum::http::HeaderMap) -> String {
    use serde_json::json;
    let sensitive = [
        "authorization",
        "x-api-key",
        "cookie",
        "set-cookie",
        "x-gateway-authorization",
        "proxy-authorization",
        // Provider keys a caller may present, and AWS's session token.
        "api-key",
        "x-goog-api-key",
        "x-amz-security-token",
        // A one-time approval token is a credential of its requester for
        // as long as it is not redeemed.
        "x-approval-token",
        // The headers the gateway once copied cloud credentials into.
        // Nothing writes them now; a value under these names is a
        // credential all the same.
        "x-brutor-aws-",
        "x-brutor-azure-",
        "x-brutor-gcp-",
    ];
    let mut map = serde_json::Map::new();
    for (key, value) in headers.iter() {
        let k = key.as_str().to_lowercase();
        if sensitive.iter().any(|s| k.contains(s)) {
            map.insert(k, json!("[REDACTED]"));
        } else if let Ok(v) = value.to_str() {
            map.insert(k, json!(v));
        }
    }
    serde_json::to_string(&map).unwrap_or_default()
}

/// The context this row's call sent its model, or `None` when the row is not
/// a model reading a context: a response-cache hit never reached a model, and
/// an embedding is a retrieval inside a turn (the same rule as turn inference).
pub fn call_context_tokens(log: &ProxyLogInsert) -> Option<i32> {
    if log.proxy_type != "llm"
        || log.llm_cache_hit == Some(true)
        || log.proxy_subtype.as_deref() == Some("embeddings")
    {
        return None;
    }
    crate::services::prompt_cache_usage::context_tokens(
        log.llm_provider.as_deref(),
        log.llm_model_used.as_deref(),
        log.llm_prompt_tokens,
        log.prompt_cache_write_tokens,
        log.prompt_cache_read_tokens,
    )
}

#[cfg(test)]
mod tests {
    #[test]
    fn an_approval_token_value_is_replaced_wherever_the_key_is() {
        for (logged, scrubbed) in [
            (
                r#"{"skill_name":"s","approval_token":"tok-1,tok-2","args":{"a":1}}"#,
                r#"{"skill_name":"s","approval_token":"[REDACTED]","args":{"a":1}}"#,
            ),
            (
                r#"{"approval_token" : "tok-1"}"#,
                r#"{"approval_token" : "[REDACTED]"}"#,
            ),
            // JSON carried inside a JSON string.
            (
                r#"{"text":"{\"status\":\"approved\",\"approval_token\":\"tok-1\"}"}"#,
                r#"{"text":"{\"status\":\"approved\",\"approval_token\":\"[REDACTED]\"}"}"#,
            ),
            // A preview cut inside the value.
            (
                r#"{"approval_token":"tok-that-was-cu"#,
                r#"{"approval_token":"[REDACTED]"#,
            ),
            // Twice in one body.
            (
                r#"[{"approval_token":"a"},{"approval_token":"b"}]"#,
                r#"[{"approval_token":"[REDACTED]"},{"approval_token":"[REDACTED]"}]"#,
            ),
        ] {
            assert_eq!(super::scrub_approval_token_values(logged), scrubbed);
        }
        // A null, and a body that only mentions the word, are left alone.
        for untouched in [
            r#"{"approval_token":null,"skill_name":"s"}"#,
            "Submit with approval_token after approval.",
        ] {
            assert_eq!(super::scrub_approval_token_values(untouched), untouched);
        }
    }

    #[test]
    fn the_approval_token_header_is_redacted() {
        let mut headers = axum::http::HeaderMap::new();
        headers.insert("x-approval-token", "tok-1".parse().unwrap());
        let logged = super::sanitize_headers(&headers);
        assert!(
            !logged.contains("tok-1") && logged.contains("[REDACTED]"),
            "{logged}"
        );
    }

    use super::*;
    use crate::models::auth::{AuthContext, AuthType};

    fn test_auth() -> AuthContext {
        AuthContext {
            credential_kind: crate::models::auth::CredentialKind::ApiKey,
            credential_assurance: Some(crate::models::auth::CredentialAssurance::Key),
            credential_ref: None,
            credential_detail: Default::default(),
            identity_verdict: None,
            auth_type: AuthType::ApiKey,
            tenant_id: "test-tenant".to_string(),
            gateway_user_id: None,
            gateway_username: None,
            gateway_tenant_id: None,
            idp_user_id: None,
            idp_username: None,
            idp_email: None,
            api_key_id: None,
            api_key_name: None,
            api_key_scope: None,
            api_key_group_id: None,
            api_key_is_platform: false,
            api_key_user_id: None,
            allowed_skill_ids: None,
            session_key: None,
            client_ip: None,
            resolved_group_id: None,
            actor_agent_id: None,
            session_token_type: None,
            agent_impl: crate::services::agent_impl::request_carriers(),
            admission_hold: None,
        }
    }

    #[test]
    fn test_body_preview() {
        let input = "hello world this is a body";
        assert_eq!(body_preview(input), input.to_string());
    }

    #[test]
    fn test_extract_user_agent() {
        let mut headers = axum::http::HeaderMap::new();
        headers.insert(
            axum::http::header::USER_AGENT,
            "Mozilla/5.0 TestAgent".parse().unwrap(),
        );
        let result = extract_user_agent(&headers);
        assert_eq!(result, Some("Mozilla/5.0 TestAgent".to_string()));
    }

    #[test]
    fn test_extract_user_agent_missing() {
        let headers = axum::http::HeaderMap::new();
        let result = extract_user_agent(&headers);
        assert_eq!(result, None);
    }

    #[test]
    fn test_sanitize_headers_redacts_auth() {
        let mut headers = axum::http::HeaderMap::new();
        headers.insert("authorization", "Bearer secret-token".parse().unwrap());
        headers.insert("x-api-key", "sk-12345".parse().unwrap());
        headers.insert("cookie", "session=abc123".parse().unwrap());

        let result = sanitize_headers(&headers);
        let parsed: serde_json::Value = serde_json::from_str(&result).unwrap();
        assert_eq!(parsed["authorization"], "[REDACTED]");
        assert_eq!(parsed["x-api-key"], "[REDACTED]");
        assert_eq!(parsed["cookie"], "[REDACTED]");
    }

    #[test]
    fn test_sanitize_headers_preserves_normal() {
        let mut headers = axum::http::HeaderMap::new();
        headers.insert("content-type", "application/json".parse().unwrap());
        headers.insert("accept", "text/html".parse().unwrap());

        let result = sanitize_headers(&headers);
        let parsed: serde_json::Value = serde_json::from_str(&result).unwrap();
        assert_eq!(parsed["content-type"], "application/json");
        assert_eq!(parsed["accept"], "text/html");
    }

    #[test]
    fn test_sanitize_headers_case_insensitive() {
        // axum::http::HeaderMap normalizes header names to lowercase,
        // so "AUTHORIZATION" becomes "authorization" internally.
        let mut headers = axum::http::HeaderMap::new();
        headers.insert("authorization", "Bearer top-secret".parse().unwrap());

        let result = sanitize_headers(&headers);
        let parsed: serde_json::Value = serde_json::from_str(&result).unwrap();
        assert_eq!(parsed["authorization"], "[REDACTED]");
    }

    #[test]
    fn test_populate_llm_request_fields() {
        let auth = test_auth();
        let mut log = build_proxy_log(
            "llm",
            &auth,
            "POST",
            "/v1/chat/completions",
            None,
            None,
            200,
            100.0,
            true,
            None,
            "corr-1",
        );

        let body = serde_json::json!({
            "temperature": 0.7,
            "max_tokens": 1024,
            "tools": [{"type": "function", "function": {"name": "get_weather"}}],
        });
        populate_llm_request_fields(&mut log, &body);

        assert_eq!(log.llm_temperature, Some(0.7));
        assert_eq!(log.llm_max_tokens, Some(1024));
        assert_eq!(log.llm_function_calling, Some(true));
    }

    #[test]
    fn test_populate_llm_request_fields_no_tools() {
        let auth = test_auth();
        let mut log = build_proxy_log(
            "llm",
            &auth,
            "POST",
            "/v1/chat/completions",
            None,
            None,
            200,
            50.0,
            true,
            None,
            "corr-2",
        );

        let body = serde_json::json!({
            "temperature": 0.5,
            "max_tokens": 256,
        });
        populate_llm_request_fields(&mut log, &body);

        assert_eq!(log.llm_temperature, Some(0.5));
        assert_eq!(log.llm_max_tokens, Some(256));
        assert_eq!(log.llm_function_calling, Some(false));
    }

    #[test]
    fn test_build_proxy_metric_user_agent_hash() {
        let auth = test_auth();
        let metric = build_proxy_metric(
            "llm",
            &auth,
            "POST",
            "/v1/chat/completions",
            "chat",
            None,
            200,
            100.0,
            true,
            "corr-3",
            None,
            None,
            None,
            None,
            Some("Mozilla/5.0 TestAgent"),
            None,
            None,
        );

        // user_agent_hash should be the SHA256 of the user agent string
        let expected = {
            let mut hasher = Sha256::new();
            hasher.update(b"Mozilla/5.0 TestAgent");
            format!("{:x}", hasher.finalize())
        };
        assert_eq!(metric.user_agent_hash, Some(expected));
    }

    #[test]
    fn test_build_proxy_metric_fields() {
        let auth = test_auth();
        let metric = build_proxy_metric(
            "mcp",
            &auth,
            "POST",
            "/mcp/tools/call",
            "tools",
            Some("server-1"),
            200,
            55.5,
            true,
            "corr-4",
            Some(128),
            Some(256),
            Some(5),
            Some(3),
            None,
            None,
            None,
        );

        assert_eq!(metric.proxy_type, "mcp");
        assert_eq!(metric.http_method, "POST");
        assert_eq!(metric.endpoint, "/mcp/tools/call");
        assert_eq!(metric.http_status_code, Some(200));
        assert!(metric.success);
        assert_eq!(metric.server_id, Some("server-1".to_string()));
        assert_eq!(metric.tenant_id, "test-tenant");
        assert_eq!(metric.request_size_bytes, Some(128));
        assert_eq!(metric.response_size_bytes, Some(256));
        assert_eq!(metric.user_agent_hash, None);
    }

    // -----------------------------------------------------------------------
    // apply_call_chain
    //
    // `apply_call_chain` is the only writer of the six chain
    // columns on every audit row across LLM/MCP/Skill/A2A. A
    // typo here (wrong column, dropped Option wrapper, wrong
    // depth coercion) would silently corrupt traces — there's no
    // schema-level guard. These tests pin the contract.
    // -----------------------------------------------------------------------

    use crate::services::a2a_delegation::{CallChain, TraceContinuity};
    use ulid::Ulid;

    fn llm_log(provider: &str, model: &str) -> ProxyLogInsert {
        let mut log = empty_log();
        log.llm_provider = Some(provider.into());
        log.llm_model_used = Some(model.into());
        log.llm_prompt_tokens = Some(100);
        log.prompt_cache_read_tokens = Some(900);
        log
    }

    #[test]
    fn a_row_context_is_the_provider_normalised_size() {
        assert_eq!(
            call_context_tokens(&llm_log("anthropic", "claude")),
            Some(1_000)
        );
        assert_eq!(call_context_tokens(&llm_log("openai", "gpt-5")), Some(100));
    }

    #[test]
    fn a_row_that_never_read_a_context_has_none() {
        let mut hit = llm_log("openai", "gpt-5");
        hit.llm_cache_hit = Some(true);
        assert_eq!(
            call_context_tokens(&hit),
            None,
            "served from the response cache"
        );

        let mut embedding = llm_log("openai", "text-embedding-3-small");
        embedding.proxy_subtype = Some("embeddings".into());
        assert_eq!(call_context_tokens(&embedding), None);

        let mut tool = llm_log("openai", "gpt-5");
        tool.proxy_type = "mcp".into();
        assert_eq!(call_context_tokens(&tool), None);
    }

    fn empty_log() -> ProxyLogInsert {
        let auth = test_auth();
        build_proxy_log(
            "llm",
            &auth,
            "POST",
            "/v1/chat/completions",
            None,
            None,
            200,
            12.0,
            true,
            None,
            "corr-x",
        )
    }

    #[test]
    fn apply_call_chain_root_stamps_minted_no_parent() {
        let mut log = empty_log();
        let root_id = Ulid::new();
        let chain = CallChain::root(root_id);
        let current = Ulid::new().to_string();

        apply_call_chain(
            &mut log,
            &chain,
            &current,
            TraceContinuity::Minted,
            Some("llm_user"),
        );

        assert_eq!(log.root_task_id, Some(root_id.to_string()));
        assert_eq!(log.current_task_id, Some(current.clone()));
        // depth 0 → parent_task_id deliberately NULL so the
        // dashboard's "is this a root row?" predicate (parent IS
        // NULL) works without inspecting depth too.
        assert_eq!(log.parent_task_id, None);
        assert_eq!(log.delegation_depth, Some(0));
        assert_eq!(log.trace_root_kind.as_deref(), Some("llm_user"));
        assert_eq!(log.trace_continuity.as_deref(), Some("minted"));
    }

    #[test]
    fn apply_call_chain_verified_child_stamps_parent_and_depth() {
        let mut log = empty_log();
        let root_id = Ulid::new();
        let parent_id = Ulid::new();
        // Hand-construct a verified child chain at depth 2 to pin
        // that apply_call_chain reads depth and parent off the
        // chain (not from elsewhere).
        let chain = CallChain {
            root_task_id: root_id,
            parent_task_id: parent_id,
            depth: 2,
            actor_agent_id: None,
            subject: None,
        };
        let current = Ulid::new().to_string();

        apply_call_chain(
            &mut log,
            &chain,
            &current,
            TraceContinuity::Verified,
            Some("agent_invocation"),
        );

        assert_eq!(log.root_task_id, Some(root_id.to_string()));
        assert_eq!(log.parent_task_id, Some(parent_id.to_string()));
        assert_eq!(log.current_task_id, Some(current));
        assert_eq!(log.delegation_depth, Some(2));
        assert_eq!(log.trace_root_kind.as_deref(), Some("agent_invocation"));
        assert_eq!(log.trace_continuity.as_deref(), Some("verified"));
    }

    #[test]
    fn apply_call_chain_continuity_strings_match_enum_serialization() {
        // The trace_continuity column is a free-form String — pin
        // each enum variant's wire string here so a future rename
        // of `as_str` would fail this test before reaching the DB.
        let cases = [
            (TraceContinuity::Minted, "minted"),
            (TraceContinuity::Verified, "verified"),
            (TraceContinuity::ClientDropped, "client_dropped"),
            (TraceContinuity::SigFailed, "sig_failed"),
        ];
        for (cont, expected) in cases {
            let mut log = empty_log();
            apply_call_chain(
                &mut log,
                &CallChain::root(Ulid::new()),
                &Ulid::new().to_string(),
                cont,
                None,
            );
            assert_eq!(
                log.trace_continuity.as_deref(),
                Some(expected),
                "continuity {cont:?} should serialize as {expected:?}"
            );
        }
    }

    #[test]
    fn apply_call_chain_with_no_root_kind_leaves_field_null() {
        let mut log = empty_log();
        apply_call_chain(
            &mut log,
            &CallChain::root(Ulid::new()),
            &Ulid::new().to_string(),
            TraceContinuity::Minted,
            None,
        );
        // None propagation — the dashboard treats NULL as
        // "couldn't classify the originator" which is meaningfully
        // different from "external_api". Make sure we don't
        // accidentally stamp a placeholder string.
        assert_eq!(log.trace_root_kind, None);
    }
}

#[cfg(test)]
mod instruction_fingerprint_tests {
    use super::instruction_fingerprint;
    use serde_json::json;

    #[test]
    fn fingerprint_covers_the_three_instruction_shapes() {
        let openai = json!({"messages": [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}]});
        let anthropic =
            json!({"system": "Be brief.", "messages": [{"role": "user", "content": "hi"}]});
        let gemini =
            json!({"systemInstruction": {"parts": [{"text": "Be brief."}]}, "contents": []});
        let a = instruction_fingerprint(&openai).unwrap();
        assert_eq!(a, instruction_fingerprint(&anthropic).unwrap());
        assert_eq!(a, instruction_fingerprint(&gemini).unwrap());
        assert_eq!(a.len(), 64);
    }

    #[test]
    fn user_text_does_not_change_the_fingerprint_but_instructions_do() {
        let a = instruction_fingerprint(
            &json!({"messages": [{"role": "system", "content": "A"}, {"role": "user", "content": "x"}]}),
        );
        let b = instruction_fingerprint(
            &json!({"messages": [{"role": "system", "content": "A"}, {"role": "user", "content": "y"}]}),
        );
        let c = instruction_fingerprint(
            &json!({"messages": [{"role": "system", "content": "B"}, {"role": "user", "content": "x"}]}),
        );
        assert_eq!(a, b);
        assert_ne!(a, c);
    }

    #[test]
    fn no_instructions_means_no_fingerprint() {
        assert!(
            instruction_fingerprint(&json!({"messages": [{"role": "user", "content": "x"}]}))
                .is_none()
        );
        assert!(instruction_fingerprint(&json!({"prompt": "raw"})).is_none());
    }
}
