// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # LLM Proxy Routes (OpenAI Compatible)
//!
//! Provides a standardized API surface for Chat Completions, Embeddings, and
//! the modern Responses API, regardless of the upstream provider.
//!
//! ### Key Features:
//! - **SSE Streaming**: Full support for Server-Sent Events with zero-copy forwarding.
//! - **Provider Normalization**: Translates Anthropic/Gemini/Azure formats into OpenAI JSON.
//! - **Pre-emptive Guardrails**: Validates prompt safety before sending requests upstream.
//! - **Usage Tracking**: Background tasks calculate token costs and enforce tenant quotas.

use std::time::Instant;

use axum::Router;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::response::{IntoResponse, Json, Response};
use axum::routing::{get, post};
use futures::stream::StreamExt;
use serde_json::{Value, json};
use tokio_stream::wrappers::ReceiverStream;

use crate::middleware::tenant::TenantContext;
use crate::models::auth::AuthType;
use crate::models::proxy::{
    ChatCompletionRequest, CompletionRequest, EmbeddingRequest, ResponsesRequest,
};
use crate::routes::{llm_helpers, route_helpers};
use std::sync::Arc;

use crate::services::a2a_delegation::{
    CallChain, TraceContinuity, derive_tenant_key, parse_or_mint,
};
use crate::services::llm_preflight::{self, PreflightOptions};
use crate::services::llm_refusal::{self, Refusal, RefusalScope};
use crate::services::proxy_logger::{
    self, body_preview, extract_user_agent, populate_llm_request_fields,
};
use crate::services::response_cache::{
    self, CacheBody, CacheKey, ResponseCacheSession, StoreFacts, StorePlan,
};
use crate::services::system_alerts::{SystemAlertInput, spawn_raise};
use crate::services::{SharedState, auth, llm_router, providers, proxy_metrics, usage};

/// Read a header as an owned `Option<String>` for `parse_or_mint`'s
/// closure-shaped contract. Duplicates the small helper used in
/// `routes/a2a.rs` so the wiring sites are symmetric across surfaces.
fn header_value(headers: &HeaderMap, name: &str) -> Option<String> {
    headers
        .get(name)
        .and_then(|v| v.to_str().ok())
        .map(str::to_owned)
}

/// Decide which `trace_root_kind` to stamp on a root span. Called once
/// per request, after preflight has populated `auth_ctx.auth_type`.
/// is that only root rows carry the kind, all subsequent steps inherit
/// via the chain.
fn root_kind_for_auth(auth_type: &AuthType, is_root: bool) -> Option<&'static str> {
    if !is_root {
        return None;
    }
    match auth_type {
        AuthType::GatewayJwt => Some("llm_user"),
        AuthType::ApiKey => Some("agent_invocation"),
        _ => Some("external_api"),
    }
}

/// Safety ceiling for streaming responses (64 MB).
/// Prevents runaway streams from exhausting server memory.
const MAX_STREAM_BYTES: usize = 64 * 1024 * 1024;

/// Emit the result of a [`StreamGuard`](crate::services::stream_guard::StreamGuard)
/// decision to the SSE client. Returns `true` if the caller should
/// `break 'stream` (a Block fired, or the client disconnected).
///
/// Shared by both SSE branches and the end-of-stream flush so the
/// emit/terminate policy lives in exactly one place.
pub(crate) async fn emit_stream_action(
    tx: &tokio::sync::mpsc::Sender<Result<axum::response::sse::Event, axum::Error>>,
    action: crate::services::stream_guard::StreamAction,
    total_chunks: &mut u32,
    guardrail_blocked: &mut bool,
    // The exact text put on the wire (post-guard / post-redaction). The
    // proxy-log response body is built from this so it matches what the
    // client actually received — not the unredacted upstream tokens.
    wire_content: &mut String,
    corr_id: &str,
) -> bool {
    use crate::services::stream_guard::StreamAction;
    use axum::response::sse::Event;
    match action {
        StreamAction::Block(reason) => {
            *guardrail_blocked = true;
            tracing::warn!(correlation_id = %corr_id, %reason, "Streaming output guardrail block");
            let _ = tx
                .send(Ok(Event::default().data(
                    "{\"error\":{\"type\":\"guardrail_block\",\"message\":\"Output blocked by guardrail policy\"}}",
                )))
                .await;
            let _ = tx.send(Ok(Event::default().data("[DONE]"))).await;
            true
        }
        StreamAction::Emit(payloads) => {
            for p in payloads {
                // Record what we're about to send so the audit log mirrors
                // the wire exactly (redacted content in sync mode, verbatim
                // in async/passthrough mode).
                crate::services::stream_guard::append_chunk_loggable_text(&p, wire_content);
                if tx.send(Ok(Event::default().data(p))).await.is_err() {
                    tracing::debug!(
                        correlation_id = %corr_id,
                        chunks = *total_chunks,
                        "Client disconnected mid-stream; aborting forwarder"
                    );
                    return true;
                }
                *total_chunks += 1;
            }
            false
        }
    }
}

/// Emit a [`TextAction`](crate::services::stream_guard::TextAction) from
/// the Responses-API output guardrail to the SSE client.
///
/// Sync (redaction) mode: each held-back segment is re-emitted as a
/// synthetic `response.output_text.delta` event so the client still sees
/// streamed text — just the redacted form. Block: emit a guardrail error
/// event and signal termination. Returns `true` when the caller should
/// stop forwarding (a Block fired, or the client disconnected).
async fn emit_responses_text_action(
    sink: &mut ResponsesSink<'_>,
    action: crate::services::stream_guard::TextAction,
    guardrail_blocked: &mut bool,
    corr_id: &str,
) -> bool {
    use crate::services::stream_guard::TextAction;
    use axum::response::sse::Event;
    match action {
        TextAction::Block(reason) => {
            *guardrail_blocked = true;
            tracing::warn!(correlation_id = %corr_id, %reason, "Streaming output guardrail block (responses)");
            let _ = sink
                .send(Event::default().event("error").data(
                    "{\"error\":{\"type\":\"guardrail_block\",\"message\":\"Output blocked by guardrail policy\"}}",
                ))
                .await;
            true
        }
        TextAction::Emit(segments) => {
            for s in segments {
                let payload = serde_json::json!({ "delta": s }).to_string();
                if sink
                    .send(
                        Event::default()
                            .event("response.output_text.delta")
                            .data(payload),
                    )
                    .await
                {
                    return true;
                }
            }
            false
        }
    }
}

/// Where a Responses stream's frames go: to the client, or, once the answer
/// starts a function call, held until the turn's calls are judged (data scope,
/// argument policies). A client runs a function call as soon
/// as it has it, so the check must come first. Held frames are released in
/// order when the calls pass and dropped when they are refused.
struct ResponsesSink<'a> {
    tx: &'a tokio::sync::mpsc::Sender<Result<axum::response::sse::Event, axum::Error>>,
    held: Option<Vec<axum::response::sse::Event>>,
}

impl<'a> ResponsesSink<'a> {
    fn new(
        tx: &'a tokio::sync::mpsc::Sender<Result<axum::response::sse::Event, axum::Error>>,
    ) -> Self {
        Self { tx, held: None }
    }

    /// Hold every frame from now on.
    fn hold(&mut self) {
        self.held.get_or_insert_with(Vec::new);
    }

    /// Send or hold one frame. `true` when the client has gone.
    async fn send(&mut self, event: axum::response::sse::Event) -> bool {
        match self.held.as_mut() {
            Some(held) => {
                held.push(event);
                false
            }
            None => self.tx.send(Ok(event)).await.is_err(),
        }
    }

    /// The held frames, if any, in order.
    fn release(&mut self) -> Vec<axum::response::sse::Event> {
        self.held.take().unwrap_or_default()
    }
}

/// Whether a Responses stream event is part of a function call the model is
/// asking for (`response.output_item.added` / `.done` of a `function_call`
/// item, `response.function_call_arguments.*`). The type is on the `event:`
/// line, or only in the payload when an upstream leaves that line out.
fn is_responses_function_call_event(event_name: Option<&str>, data: &str) -> bool {
    if !data.contains("function_call") {
        return false;
    }
    let Ok(v) = serde_json::from_str::<Value>(data) else {
        return false;
    };
    let kind = event_name
        .or_else(|| v.get("type").and_then(Value::as_str))
        .unwrap_or_default();
    kind.starts_with("response.function_call_arguments")
        || (kind.starts_with("response.output_item.")
            && v.pointer("/item/type").and_then(Value::as_str) == Some("function_call"))
}

/// Whether the `/v1/proxy/llm/responses` handler should forward to the
/// upstream `/v1/responses` endpoint instead of converting to
/// `/v1/chat/completions`.
///
/// OpenAI and Azure host their reasoning models behind /responses;
/// xAI does the same and additionally requires it for
/// `grok-4.20-multi-agent-0309` ("Multi Agent requests are not allowed
/// on chat completions"). Keep this list narrow — providers added here
/// must accept the OpenAI Responses request shape (`input` field rather
/// than `messages`) verbatim, since `prepare_responses` doesn't run
/// per-provider transformations.
fn provider_uses_responses_api(provider: &str) -> bool {
    matches!(provider, "openai" | "azure" | "azure_ai" | "xai")
}

pub fn router() -> Router<SharedState> {
    // Completions (streaming and not) are labelled
    // `X-Brutor-AI-Generated: true`; embeddings and listings are not
    // generated content and stay unlabelled.
    let generation = Router::new()
        .route("/chat/completions", post(chat_completions))
        .route("/responses", post(responses))
        .route("/completions", post(completions))
        .route_layer(axum::middleware::from_fn(
            crate::services::evidence::notice::mark_ai_generated,
        ))
        // What the guardrails flag on a request, for the response cache.
        .route_layer(axum::middleware::from_fn(
            crate::services::guardrails::flags::layer,
        ));
    Router::new()
        .merge(generation)
        .route("/embeddings", post(embeddings))
        .route("/models", get(list_models))
        .route("/models/{model_id}", get(get_model))
        .route("/health", get(health))
        .route("/admin/{model_id}/reset-resilience", post(reset_resilience))
        .route(
            "/admin/{model_id}/invalidate-cache",
            post(invalidate_model_cache),
        )
        .route("/admin/{model_id}/probe-catalog", post(probe_catalog))
        .route(
            "/admin/{model_id}/record-probe-result",
            post(record_probe_result),
        )
}

#[utoipa::path(
    post,
    path = "/v1/proxy/llm/chat/completions",
    tag = "LLM",
    summary = "Chat completions (OpenAI-compatible)",
    description = "OpenAI-compatible chat completion endpoint with full SSE streaming support. Supports all major providers via automatic format translation.",
    request_body = ChatCompletionRequest,
    responses(
        (status = 200, description = "Chat completion response (or SSE stream if stream=true)", body = Value),
        (status = 401, description = "Authentication required"),
        (status = 429, description = "Rate/usage limit exceeded"),
    ),
    security(
        ("api_key" = []),
        ("bearer" = []),
    )
)]
pub(crate) async fn chat_completions(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Json(mut request): Json<ChatCompletionRequest>,
) -> Response {
    let start = Instant::now();
    let correlation_id = route_helpers::extract_correlation_id(&headers);
    // Health-probe traffic skips ALL metric writes (proxy_metrics,
    // usage_logs, model_metrics) inside this handler. Probe accounting
    // is centralized in `services::catalog_probe::record_probe_outcome`,
    // called by gateway-control via `record-probe-result` after every
    // probe regardless of outcome. Without this skip, probes would
    // double-count: once from the route's success-path metric writes,
    // once from the central record-probe path.
    let tenant_id = match route_helpers::extract_tenant_id(tenant) {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    // Establish the call chain at request entry,
    // before any audit row is written. If the caller supplied the four
    // `x-brutor-delegation-*` headers we verify the HMAC and adopt their
    // chain; otherwise we mint a fresh root. Either way every audit
    // row this handler produces below will carry the same
    // root_task_id / current_task_id / parent_task_id / depth so an
    // operator can grep one ULID and reconstruct the entire LLM ↔ MCP
    // ↔ Skill agent flow.
    let tenant_key = derive_tenant_key(state.config.encryption_key.as_bytes(), &tenant_id);
    // Resolve the conversation this request belongs to, if the client
    // asserted one. Derived here rather than in the middleware because the
    // HMAC needs the tenant key, which only exists after auth.
    if let Some(sess) = crate::services::a2a_delegation::resolve_asserted_session(
        |h| {
            headers
                .get(h)
                .and_then(|v| v.to_str().ok())
                .map(str::to_string)
        },
        &tenant_key,
    ) {
        crate::middleware::run_end::set_resolved_session(sess.to_string());
    }
    let (call_chain, current_task_id, trace_continuity) =
        parse_or_mint(|n| header_value(&headers, n), &tenant_key);
    // Captured up front: the chain is moved into the telemetry task
    // further down, and the response headers are stamped after that.
    let run_id_header = call_chain.root_task_id.to_string();
    let current_task_id_str = current_task_id.to_string();
    // Where this call sits in its run — every refusal or block row of this
    // request (pre-flight, guardrail, post-response governance) lands on it.
    let run_attr =
        llm_refusal::RunAttribution::chain(&call_chain, &current_task_id_str, trace_continuity);

    tracing::debug!(
        model_requested = ?request.model,
        stream = ?request.stream,
        message_count = request.messages.len(),
        has_tools = request.tools.is_some(),
        "Chat completion request"
    );

    // Preflight: auth → model resolution → access check → quota enforcement
    let pf_body = serde_json::to_string(&request).unwrap_or_default();
    let pf = match llm_preflight::run_preflight(
        &state,
        &headers,
        &tenant_id,
        &correlation_id,
        request.model.as_deref(),
        request.model_id.as_deref(),
        // Arm the run-level caps: the chain minted above gives both the run
        // this call belongs to and which delegation hop it is.
        &PreflightOptions::full(run_attr.clone()),
        error_response,
        client_addr.ip(),
        None,
        Some(&pf_body),
    )
    .await
    {
        Ok(pf) => pf,
        Err(resp) => return resp,
    };
    let llm_preflight::PreflightResult {
        auth_ctx,
        model,
        target_group,
        enforcement,
        out_ceiling,
        ..
    } = pf;
    // A probe's metric is written by the central probe recorder; its usage
    // (real tokens and cost, billed to Unbound) is written here. Only
    // the platform service key's probe counts.
    let is_probe = crate::services::catalog_probe::is_health_probe_request(&headers, &auth_ctx);

    // Determine the chain root-kind tag for this request. Only root
    // spans (depth == 0) get a tag; downstream steps inherit via the
    // chain's root_task_id rather than re-tagging.
    let trace_root_kind = root_kind_for_auth(&auth_ctx.auth_type, call_chain.depth == 0);

    // Data residency check.
    //
    // Look up the resolved model's provider region via the
    // ProviderCache (one cache step, no DB round-trip in the warm
    // case) and intersect with the tenant's residency profile.
    // Three outcomes:
    //
    //   * No profile           → audit rows stamp not_evaluated;
    //                            request proceeds.
    //   * Allowed              → audit rows stamp allowed +
    //                            region_routed_to.
    //   * Blocked              → write a single residency_violation
    //                            audit row + return 403
    //                            region_not_allowed. Upstream LLM
    //                            never sees the prompt.
    //
    // Fallback-chain integration (RFC Feature 7.5) is a follow-up:
    // when the primary model's region is blocked, the chain should
    // try the next model. Today the block is strict — the first
    // model's region decides.
    // Data residency: one shared
    // implementation, see `llm_preflight::enforce_residency`. This used to be
    // ~55 lines copied here and at every other LLM surface, which is how the
    // sticky prompt-cache pin and the Gemini bridge each
    // came to skip the check independently — there was no single place to add
    // one.
    let residency = match llm_preflight::enforce_residency(
        &state,
        &auth_ctx,
        &tenant_id,
        &model,
        request.model.clone(),
        "/v1/chat/completions",
        "Chat",
        &headers,
        &correlation_id,
        start,
        &call_chain,
        &current_task_id_str,
        trace_continuity,
        trace_root_kind,
        error_response,
    )
    .await
    {
        Ok(o) => o,
        Err(resp) => return resp,
    };
    let provider_region_for_audit = residency.provider_region;
    let residency_decision = residency.decision;

    // The three actor planes (agent authorization, the AI
    // System autonomy gate, semantic policy on the input) now live in one
    // helper so every LLM surface runs them. They used to be inline here and
    // nowhere else, which made `/v1/messages` and the Gemini native route a
    // way around a suspended system, a denied agent grant, and every
    // enforcing policy.
    if let Err(resp) = llm_preflight::enforce_actor_planes(
        &state,
        &auth_ctx,
        &tenant_id,
        &model,
        target_group.as_ref(),
        request.model.clone(),
        &serde_json::to_string(&request.messages).unwrap_or_default(),
        "/v1/chat/completions",
        &headers,
        &correlation_id,
        start,
        &call_chain,
        &current_task_id_str,
        trace_continuity,
        trace_root_kind,
        error_response,
    )
    .await
    {
        return resp;
    }

    // Capability flag: ``requires_responses_api``. Some upstream models
    // (notably xAI's `grok-4.20-multi-agent-0309`) only respond on the
    // Responses endpoint and reject /chat/completions with a hard
    // 400. The seed flags these so the proxy can tell the caller to
    // hit the Responses route directly rather than falling through to
    // an opaque provider-side error.
    if let Some(caps) = model.capabilities.as_ref().and_then(|v| v.as_object()) {
        if caps
            .get("requires_responses_api")
            .and_then(|v| v.as_bool())
            .unwrap_or(false)
        {
            tracing::info!(
                model = %model.model_name,
                provider = %model.provider,
                "Chat completions blocked: model is flagged requires_responses_api"
            );
            return error_response(
                StatusCode::BAD_REQUEST,
                &format!(
                    "Model '{}' only supports the Responses endpoint. \
                     POST to /v1/proxy/llm/responses with this model_name instead.",
                    model.model_name
                ),
            );
        }
    }

    // A caller's own choice the model is known to refuse (the catalog's
    // GPT-6 Sol / Luna take tools on Chat Completions only with
    // `reasoning_effort: "none"`). What the caller left open the adapter
    // fills; what the caller chose is not replaced, and a request the
    // provider refuses is not sent: the caller is told what to change.
    if let Some(reason) = crate::services::llm_adapter::openai_family::chat_request_conflict(
        &model,
        request.tools.as_ref(),
        request.extra.get("reasoning_effort"),
    ) {
        tracing::info!(
            model = %model.model_name,
            provider = %model.provider,
            "Chat completions refused: reasoning_effort conflicts with tools for this model"
        );
        return error_response(StatusCode::BAD_REQUEST, &reason);
    }

    // A Bedrock row served through Converse, asked for something the model
    // does not have there (tools, an image, …) or for content Converse has
    // no block for: refused by name, not sent without it.
    if crate::services::bedrock_runtime::serves_chat(&model)
        && let Some(reason) = crate::services::bedrock_runtime::converse::request_conflict(
            &model,
            &serde_json::to_value(&request).unwrap_or_default(),
            request.stream.unwrap_or(false),
        )
    {
        tracing::info!(
            model = %model.model_name,
            provider = %model.provider,
            "Chat completions refused: the request needs what this Bedrock model does not support"
        );
        return error_response(StatusCode::BAD_REQUEST, &reason);
    }

    // Snapshot the governed params BEFORE `apply_governance` clamps them in
    // place, so we can emit an additive `brutor_governance` envelope on the
    // response telling the caller what the gateway changed (Scene 6 of the LLM
    // demo). `apply_governance` mutates `request.{max_tokens,temperature}` and
    // may inject a mandatory system-prompt fragment; we detect fragment
    // injection by the total system-message length growing (RAG injection
    // happens later, so this window only sees the governance fragment).
    let gov_requested_max_tokens = request.max_tokens.map(|v| v as i64);
    let gov_requested_temperature = request.temperature;
    let sys_content_len = |msgs: &[crate::models::proxy::ChatMessage]| -> usize {
        msgs.iter()
            .filter(|m| m.role == "system")
            .filter_map(|m| m.content.as_ref().and_then(|c| c.as_str()))
            .map(|s| s.len())
            .sum()
    };
    let gov_sys_len_before = sys_content_len(&request.messages);

    // LLM governance enforcement (temperature, tools, banned patterns, system prompt injection)
    // Returns the effective output-param ceilings (max_tokens /
    // temperature) so we can bind model-injected defaults to the same
    // caps the caller-supplied values were just clamped against — see
    // the `EFFECTIVE_PARAM_CAPS.scope(...)` wraps around the forward
    // calls below.
    let model_requested_for_audit = request.model.clone();
    let param_caps = match llm_preflight::apply_governance(
        &state,
        &target_group,
        &model.id,
        &mut request,
        error_response,
        &RefusalScope {
            auth_ctx: &auth_ctx,
            headers: &headers,
            correlation_id: &correlation_id,
            endpoint: "/chat/completions",
            started: Some(start),
            model: Some((&model, model_requested_for_audit)),
            group_id: None,
            run: Some(&llm_refusal::RunAttribution::chain(
                &call_chain,
                &current_task_id_str,
                trace_continuity,
            )),
            request_body: None,
        },
    )
    .await
    {
        Ok(caps) => caps,
        Err(resp) => return resp,
    };
    // The output this call was admitted for: what every token and spend cap
    // still has room for. Bound here, on the request, and again after the
    // provider translation (`ParamCaps::output_ceiling`), which also binds a
    // model default injected later.
    if let Some(ceiling) = out_ceiling {
        crate::services::llm_governance::apply_output_ceiling_to_chat(&mut request, ceiling);
    }
    let param_caps =
        crate::services::llm_governance::ParamCaps::with_output_ceiling(param_caps, out_ceiling);

    // Build the response envelope from what governance actually changed. `None`
    // when nothing was clamped/injected → no envelope is attached and the
    // response stays byte-for-byte OpenAI-compatible.
    let governance_envelope = crate::services::llm_governance::build_governance_envelope(
        gov_requested_max_tokens,
        request.max_tokens.map(|v| v as i64),
        gov_requested_temperature,
        request.temperature,
        sys_content_len(&request.messages) > gov_sys_len_before,
        Some("model_profile"),
    );

    // Guardrails — check input. On Redact we splice the rewritten
    // text back into request.messages[*].content so a `pii_action:
    // redact` policy actually masks the PII before forwarding to
    // OpenAI / the upstream — otherwise the operator's policy is
    // just a warning log.
    // Flatten each message's content with the array-aware walk so the
    // multi-part / vision form (`content: [{type:"text",text:…},
    // {type:"image_url",…}]`) is scanned too. Reading only `c.as_str()`
    // here let any client bypass input guardrails (PII, jailbreak,
    // prompt-injection) simply by sending array-form content — the text
    // leaves were invisible to the scanner. `flatten_input_text` pulls the
    // `text` parts and ignores image parts; `apply_redactions_to_openai_messages`
    // walks the identical shape so redaction splices back into the same leaves.
    // User turns only — see `is_input_scannable_role`. ALL of them, not just
    // the newest: an instruction split across several messages only reads as
    // an attack once they are put back together.
    let input_text = llm_preflight::scan_text_from_openai_messages(&request.messages);

    // Per-mode surface dispatch for chat completions.
    // Capture the resolved upstream so a guardrail block records which
    // model/provider/URL the request was aimed at (the block returns
    // before we forward, so the row would otherwise show N/A target).
    let guard_target = llm_preflight::LlmAuditTarget::from_model(
        &model,
        request.model.clone(),
        "/chat/completions",
    );
    // Tracks whether an input guardrail redacted a PII category — set in
    // the redaction arm below, stamped on this request's success audit
    // row(s) (the success log is built much later than this early check).
    let mut input_pii_redacted = false;
    // Skip input guardrails for the semantic-policy judge self-call (marked by
    // `X-Brutor-Semantic-Policy`). The judge's whole job is to read the governed
    // content and rule on it, so re-screening the judge prompt through the same
    // input guardrails is both self-defeating (the judge can never see a prompt
    // that trips a detector) and actively harmful: the model-id echoed in the
    // judge envelope can itself false-positive a PII/pattern check, blocking the
    // self-call and forcing every semantic policy on that group to fail open.
    // Verified internal origin — NOT mere header presence. Keying this on
    // presence let any caller with a valid key add the header and skip
    // every input guardrail (PII, prompt-injection, secrets, toxicity).
    let is_judge_self_call = crate::services::internal_call::is_internal(
        &headers,
        crate::constants::headers::X_BRUTOR_SEMANTIC_POLICY,
    );
    if !is_judge_self_call {
        match llm_preflight::apply_guardrails_with_redaction(
            &state,
            &tenant_id,
            &RefusalScope::new(
                &auth_ctx,
                &headers,
                &correlation_id,
                "/chat/completions",
                &run_attr,
            )
            .started(start),
            &input_text,
            crate::constants::guardrail_surface::CHAT_INPUT,
            None,
            Some(&guard_target),
        )
        .await
        {
            Err(resp) => return resp,
            Ok(Some(redaction)) => {
                llm_preflight::apply_redactions_to_openai_messages(
                    &mut request.messages,
                    &redaction.content,
                );
                // Set when a PII-category input check redacted the prompt —
                // stamped on this request's success audit row(s) so the
                // compliance tagger derives has_pii/has_phi (GDPR/HIPAA).
                input_pii_redacted = redaction.pii;
            }
            Ok(None) => {}
        }
    }

    // What the platform's self-calls made for this request forward (the
    // semantic cache's and the Knowledge Base's embedding calls): the
    // caller's credential and tenant, and this step's delegation chain, so
    // each is governed, billed and recorded as a step of the caller's run.
    let self_call_headers = (state.response_cache.makes_self_calls()
        || state.context_enricher.makes_self_calls())
    .then(|| llm_helpers::self_call_headers(&headers, &call_chain, current_task_id, &tenant_key));
    let caller_headers = self_call_headers.as_ref().unwrap_or(&headers);
    let cache_auth_headers = llm_helpers::embedding_self_call_headers(caller_headers);

    // --- Semantic cache lookup ---
    // Load per-group cache config from EffectiveGroupData (or skip if no config assigned)
    // The response cache's view of this request: the opt-out
    // header, the group's cache policy and the cache's own gating live
    // behind the hook, and so does the router's exact-match cache (R10b:
    // `dispatch_cache`, asked where the request is forwarded).
    let cache_session = state
        .response_cache
        .session(&state, &tenant_id, target_group.as_ref(), caller_headers)
        .await;

    // A group whose answers are grounded in a knowledge base
    // must not share them through the response cache.
    //
    // The cache is consulted here, on the PRE-RAG request, and its key is
    // (tenant, group, model, body) plus the caller identity — but the answer
    // it stores was synthesised from documents the RAG path resolved per USER,
    // against that user's ACL principals, on a code path that deliberately
    // fails closed because "a transient DB error must not expose every
    // connector document in the tenant". Two people in one group asking the
    // same question have different entitlements, so serving the second a
    // cached answer built from the first's documents leaks exactly what those
    // ACLs exist to protect.
    //
    // Folding the ACL principal set into the key would preserve caching, but
    // it has to be right for every principal source or it silently re-opens
    // this; declining to cache for RAG-enabled groups is the conservative
    // choice and costs only latency.
    //
    // Whether the group is grounded is the context enricher's answer.
    // None installed, no group is.
    let enrichment = state
        .context_enricher
        .session(&state, &tenant_id, target_group.as_ref())
        .await;
    let kb_grounded = enrichment.grounded();

    let cache_key = CacheKey {
        tenant_id: &tenant_id,
        group_id: target_group.as_ref().map(|g| g.id.as_str()),
        model_name: &model.model_name,
        body: CacheBody::Chat(&request),
    };
    // A grounded group neither reads nor writes any response cache:
    // the rule is about grounded answers, so the cache is not asked.
    let cached = if kb_grounded {
        None
    } else {
        cache_session.lookup(&cache_key).await
    };
    if let Some(result) = cached {
        let duration = start.elapsed().as_millis() as f64;
        let cache_layer = result.layer.as_str().to_string();

        // Log cache hit — shared builder with the /responses hit
        // path so both surfaces record the same evidence.
        let mut log = llm_helpers::build_semantic_cache_hit_log(
            "/chat/completions",
            crate::constants::proxy_subtype::CHAT_COMPLETIONS,
            &auth_ctx,
            &target_group,
            &headers,
            &model,
            request.model.clone(),
            &serde_json::to_value(&request).unwrap_or_default(),
            &result,
            request.stream.unwrap_or(false),
            duration,
            &correlation_id,
        );
        proxy_logger::apply_call_chain(
            &mut log,
            &call_chain,
            &current_task_id_str,
            trace_continuity,
            trace_root_kind,
        );
        match &residency_decision {
            Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
            None => proxy_logger::apply_residency_not_evaluated(
                &mut log,
                provider_region_for_audit.as_deref(),
            ),
        }
        if input_pii_redacted {
            proxy_logger::apply_pii_detected(&mut log);
        }
        state
            .background_writer
            .write_proxy_log(state.db.clone(), log);

        // Return cached response.
        // If the client requested streaming, wrap the cached response
        // as an SSE stream so the portal's SSE client doesn't hang.
        if request.stream.unwrap_or(false) {
            // Convert cached response to SSE stream format
            let content = result
                .response
                .get("choices")
                .and_then(|c| c.get(0))
                .and_then(|c| c.get("message"))
                .and_then(|m| m.get("content"))
                .and_then(|c| c.as_str())
                .unwrap_or("");

            // `brutor_cache` is an extension field SDK clients
            // preserve on unknown fields — lets the portal
            // badge the message as "Cached response" without
            // parsing response headers (which OpenAI SDK
            // doesn't expose on streaming/non-streaming
            // calls uniformly). Also mirrored to the
            // `X-Cache-Hit` header below for direct HTTP
            // consumers.
            let brutor_cache = json!({
                "hit": true,
                "layer": cache_layer,
                "similarity_score": result.similarity_score,
            });
            let chunk = json!({
                "id": format!("chatcmpl-cache-{}", ulid::Ulid::new()),
                "object": "chat.completion.chunk",
                "choices": [{
                    "index": 0,
                    "delta": { "role": "assistant", "content": content },
                    "finish_reason": "stop"
                }],
                "brutor_cache": brutor_cache,
            });

            let (tx, rx) = tokio::sync::mpsc::channel::<Result<Event, axum::Error>>(4);
            tokio::spawn(async move {
                let _ = tx
                    .send(Ok(
                        Event::default().data(serde_json::to_string(&chunk).unwrap_or_default())
                    ))
                    .await;
                let _ = tx.send(Ok(Event::default().data("[DONE]"))).await;
            });

            let stream = ReceiverStream::new(rx);
            let mut resp = Sse::new(stream)
                .keep_alive(KeepAlive::default())
                .into_response();
            resp.headers_mut().insert(
                "X-Cache-Hit",
                cache_layer
                    .parse()
                    .unwrap_or_else(|_| "hit".parse().unwrap()),
            );
            resp.headers_mut().insert(
                crate::constants::headers::X_CORRELATION_ID,
                correlation_id.parse().unwrap(),
            );
            // The run this response belongs to, so a client can attribute
            // feedback to the task it just saw rather than the server having
            // to infer it from timing. Counterpart of the inbound header.
            if let Ok(v) = run_id_header.parse() {
                resp.headers_mut()
                    .insert(crate::constants::headers::X_BRUTOR_RUN_ID, v);
            }
            return resp;
        } else {
            // Merge a `brutor_cache` field into the cached
            // response body so SDK clients (which don't easily
            // surface response headers) can still badge the
            // reply. The `X-Cache-Hit` header below is the
            // direct-HTTP equivalent.
            let mut response_body = result.response.clone();
            if let Some(obj) = response_body.as_object_mut() {
                obj.insert(
                    "brutor_cache".into(),
                    json!({
                        "hit": true,
                        "layer": cache_layer,
                        "similarity_score": result.similarity_score,
                    }),
                );
            }
            let mut resp = Json(response_body).into_response();
            resp.headers_mut().insert(
                "X-Cache-Hit",
                cache_layer
                    .parse()
                    .unwrap_or_else(|_| "hit".parse().unwrap()),
            );
            resp.headers_mut().insert(
                crate::constants::headers::X_CORRELATION_ID,
                correlation_id.parse().unwrap(),
            );
            // The run this response belongs to, so a client can attribute
            // feedback to the task it just saw rather than the server having
            // to infer it from timing. Counterpart of the inbound header.
            if let Ok(v) = run_id_header.parse() {
                resp.headers_mut()
                    .insert(crate::constants::headers::X_BRUTOR_RUN_ID, v);
            }
            return resp;
        }
    }

    // --- Context enrichment (RAG) ---
    // The enricher retrieves what the asker may see; `enrich_chat` places it
    // in the request, cut to what the call's admission reserved for it.
    let rag_result = match crate::services::context_enrichment::enrich_chat(
        enrichment.as_ref(),
        &mut request,
        &auth_ctx,
        &cache_auth_headers,
    )
    .await
    {
        Ok(record) => record,
        // The group is grounded and its context could not be retrieved:
        // the request is refused. It
        // used to be answered from the model's own knowledge, with the
        // audit row of a request nothing was retrieved for — a control that
        // could not decide, deciding "allow". Nothing is sent to the model;
        // the refusal is recorded with its reason. (A retrieval that
        // succeeds and finds nothing is `Ok(None)`: answered as written.)
        Err(unavailable) => {
            tracing::warn!(
                tenant_id = %tenant_id,
                group_id = target_group.as_ref().map(|g| g.id.as_str()),
                "{unavailable}; refusing the request"
            );
            let message = format!("knowledge_base_unavailable: {unavailable}");
            return llm_refusal::refuse(
                &state,
                &RefusalScope {
                    auth_ctx: &auth_ctx,
                    headers: &headers,
                    correlation_id: &correlation_id,
                    endpoint: "/chat/completions",
                    started: Some(start),
                    model: Some((&model, request.model.clone())),
                    group_id: target_group.as_ref().map(|g| g.id.clone()),
                    run: Some(&run_attr),
                    request_body: None,
                },
                Refusal::new(
                    crate::constants::proxy_subtype::ENGINE_FAILURE,
                    message.as_str(),
                )
                .error_type("knowledge_base_unavailable"),
                error_response(StatusCode::SERVICE_UNAVAILABLE, &message),
            );
        }
    };

    // Vertex-Gemini OpenAI-surface bridge. Same translate-on-exit
    // pattern as Bedrock-Claude: the OpenAI client sends
    // OpenAI shape, GeminiAdapter translates to native, the native
    // pipeline hits Vertex with the row's Google access token, and the response is
    // translated back. Routes the request through the same native
    // /v1beta/models/{m}:generateContent code path the dedicated
    // Gemini client surface uses — they share auth + URL + cache
    // hooks via `resolve_gemini_endpoint`.
    let is_vertex_gemini = matches!(model.provider.as_str(), "vertex_ai" | "vertex")
        && model.model_name.to_ascii_lowercase().contains("gemini");
    if is_vertex_gemini {
        let openai_body = serde_json::to_value(&request).unwrap_or_default();
        let wants_stream_now = request.stream.unwrap_or(false);
        return crate::services::llm_governance::EFFECTIVE_PARAM_CAPS
            .scope(
                param_caps.clone().unwrap_or_default(),
                crate::routes::gemini::forward_via_openai_surface(
                    &state,
                    &model,
                    &auth_ctx,
                    target_group.as_ref(),
                    &tenant_id,
                    &correlation_id,
                    openai_body,
                    wants_stream_now,
                    &headers,
                    start,
                    // The bridge writes its own evidence tail (audit
                    // row, usage ledger, run charge, metrics) because this is
                    // a `return`: nothing here runs after it. It needs the
                    // chain identity and the input-redaction flag to do that.
                    &crate::routes::gemini::BridgeChain {
                        chain: call_chain.clone(),
                        current_task_id: current_task_id_str.clone(),
                        continuity: trace_continuity,
                        root_kind: trace_root_kind.map(str::to_string),
                    },
                    input_pii_redacted,
                ),
            )
            .await;
    }

    // Output checks read the answer only. Where one can block or redact,
    // Claude is asked for unreadable thinking, so its signed blocks still
    // reach the client and replay (both send paths below read this note).
    if let Err(resp) = llm_preflight::note_reasoning_to_omit(
        &state,
        &tenant_id,
        &RefusalScope::new(
            &auth_ctx,
            &headers,
            &correlation_id,
            "/chat/completions",
            &run_attr,
        )
        .started(start),
        &model.model_name,
        &mut request.extra,
    )
    .await
    {
        return resp;
    }

    // Determine if streaming
    let wants_stream = request.stream.unwrap_or(false);

    tracing::debug!(wants_stream, "Routing to streaming/non-streaming path");

    // Open the request's prompt-cache session ONCE here so both branches
    // can use it (`services::prompt_caching`). Streaming uses it only to
    // report a warmed cache after the stream (markers come from the client
    // today); non-streaming hands it to the router, which asks it to mark
    // the outbound request and which model the conversation is pinned to.
    let prompt_cache = state
        .prompt_cache_policy
        .session(&state, &tenant_id, target_group.as_ref(), &model)
        .await;

    // Cloud-hosted Anthropic models (Bedrock-Claude, Vertex-Claude)
    // need to be routed through the native Anthropic pipeline because
    // the standard forward path doesn't translate OpenAI shape →
    // Anthropic native, doesn't run SigV4 / the Google access token, and doesn't know
    // the provider-specific URL. The dispatcher below mirrors the
    // Anthropic-direct translate-on-exit pattern but routes upstream
    // through `services::anthropic_adapter::backend::{bedrock,vertex}`
    // (where the SigV4 / OAuth signing already lives).
    if crate::services::anthropic_adapter::should_route_via_openai_surface(&model) {
        let openai_body = serde_json::to_value(&request).unwrap_or_default();
        let caps_for_scope = param_caps.clone().unwrap_or_default();
        return if wants_stream {
            crate::services::llm_governance::EFFECTIVE_PARAM_CAPS
                .scope(
                    caps_for_scope,
                    crate::services::anthropic_adapter::forward_via_openai_surface_streaming(
                        &state,
                        &headers,
                        openai_body,
                        &model,
                        &auth_ctx,
                        &tenant_id,
                        target_group.as_ref(),
                        &correlation_id,
                        start,
                        input_pii_redacted,
                        run_attr.clone(),
                    ),
                )
                .await
        } else {
            crate::services::llm_governance::EFFECTIVE_PARAM_CAPS
                .scope(
                    caps_for_scope,
                    crate::services::anthropic_adapter::forward_via_openai_surface_non_streaming(
                        &state,
                        &headers,
                        openai_body,
                        &model,
                        &auth_ctx,
                        &tenant_id,
                        target_group.as_ref(),
                        &correlation_id,
                        start,
                        input_pii_redacted,
                        run_attr.clone(),
                    ),
                )
                .await
        };
    }

    if wants_stream {
        // ===== STREAMING PATH =====
        // Cluster-wide RG / per-model concurrency gate for the STREAM
        // (`llm_global_limits.concurrency.max_concurrent`, group + model
        // scope, restrictive/stacking like every other limit). The guard is
        // MOVED into the SSE-forwarding task below, so the slot is held for
        // the whole stream and released on completion / client-disconnect /
        // error via its Drop. Saturation → 429 before we open the upstream
        // connection. `None` group (legacy/service paths) → skipped.
        let stream_conc_guard = match crate::services::llm_refusal::acquire_concurrency(
            &state,
            target_group.as_ref(),
            &model,
            &RefusalScope {
                auth_ctx: &auth_ctx,
                headers: &headers,
                correlation_id: &correlation_id,
                endpoint: "/chat/completions",
                started: Some(start),
                model: Some((&model, None)),
                group_id: None,
                run: Some(&llm_refusal::RunAttribution::chain(
                    &call_chain,
                    &current_task_id_str,
                    trace_continuity,
                )),
                request_body: None,
            },
        )
        .await
        {
            Ok(guard) => guard,
            Err(resp) => return resp,
        };
        // Set the outbound chain
        // header pairs as a task-local around the streaming send.
        // The depth-N reqwest builder in `llm_router::forward_*`
        // reads them and emits the four `x-brutor-delegation-*`
        // headers on every outbound provider request.
        let outbound_chain = call_chain.to_header_pairs(&tenant_key);
        let outbound_chain_owned: Vec<(&'static str, String)> =
            outbound_chain.into_iter().collect();
        // Scope a cell the router fills with the EXACT body POSTed
        // upstream (resolved model, provider-translated shape, injected
        // system prompt, prompt-cache markers) so the proxy log records
        // what the provider actually received, not the inbound client body.
        let (fwd_outcome, outbound_body_logged) = llm_router::OUTBOUND_REQUEST_BODY
            .scope(std::cell::RefCell::new(None), async {
                let r = crate::services::llm_governance::EFFECTIVE_PARAM_CAPS
                    .scope(
                        param_caps.clone().unwrap_or_default(),
                        crate::services::call_chain::OUTBOUND_CHAIN_HEADERS.scope(
                            outbound_chain_owned,
                            llm_router::forward_streaming_with_model_fallback(
                                &state, &model, &request,
                            ),
                        ),
                    )
                    .await;
                let captured = llm_router::OUTBOUND_REQUEST_BODY.with(|c| c.borrow().clone());
                (r, captured)
            })
            .await;
        match fwd_outcome {
            Ok(upstream_resp) => {
                let byte_stream = upstream_resp.bytes_stream();
                let (tx, rx) = tokio::sync::mpsc::channel::<Result<Event, axum::Error>>(32);

                let state_bg = state.clone();
                let auth_bg = auth_ctx.clone();
                let model_bg = model.clone();
                // The run identity the mid-stream abort poll
                // asks about. The chain root, matching what the audit rows
                // carry and what the operator sees in the live-run list.
                let run_id_for_abort = call_chain.root_task_id.to_string();
                let corr_id = correlation_id.clone();
                let tenant_id_bg = tenant_id.clone();
                let target_group_bg = target_group.clone();
                let rag_result_bg = rag_result.clone();
                let cache_session_bg = Arc::clone(&cache_session);
                let model_requested = request.model.clone();
                let user_agent = extract_user_agent(&headers);
                let sanitized_req_headers = proxy_logger::sanitize_headers(&headers);
                let req_headers_count = headers.len() as i32;
                // Displayed/audited request body == what was sent upstream;
                // fall back to the inbound request only if capture didn't run.
                let request_body_str = outbound_body_logged
                    .clone()
                    .unwrap_or_else(|| serde_json::to_string(&request).unwrap_or_default());
                let request_body_val = serde_json::to_value(&request).unwrap_or_default();
                let request_for_cache = request.clone();
                let needs_normalization = providers::needs_stream_normalization(&model_bg.provider);
                // The prompt-cache session goes into the task so that a
                // stream whose provider reports a warmed cache can say so
                // after it closed.
                let prompt_cache_bg = Arc::clone(&prompt_cache);
                // A post-response block inside the stream (tool-call
                // governance) writes its refusal row on this run and step.
                let run_attr_bg = run_attr.clone();
                let headers_bg = headers.clone();

                tracing::debug!(
                    provider = %model_bg.provider,
                    needs_normalization,
                    "Streaming response started"
                );

                // Spawn task to forward upstream SSE chunks to client
                let input_cost = model.cost_per_input_token.unwrap_or(0.0);
                let output_cost = model.cost_per_output_token.unwrap_or(0.0);

                proxy_metrics::stream_opened();

                // Pre-load the tenant's guardrail configs once, before the
                // streaming closure starts (the cache read reaches into
                // Redis/DB — we don't want it on the per-chunk hot path).
                // The streamed output is then guarded incrementally:
                // `check_output` runs the FULL configured output checks
                // (PII, secrets, toxic, prompt-injection, banned) on the
                // accumulated content at segment boundaries (debounced by
                // STREAM_OUTPUT_CHECK_INTERVAL so external detectors like
                // Presidio aren't called per token). On a Block verdict we
                // emit one error event and terminate the stream — the
                // async-interrupt posture: future tokens are stopped, the
                // few already emitted can't be recalled (an inherent limit
                // of streaming; sync buffer-and-release for redaction is
                // the follow-up tracked separately). Replaces the earlier
                // banned-words-only check, which `check_output` subsumes.
                let stream_guardrail_configs = match state
                    .guardrails_cache
                    .get_configs(
                        &tenant_id,
                        auth_ctx.guardrail_user_id().as_deref(),
                        auth_ctx.guardrail_group_id(),
                        &state.db,
                    )
                    .await
                {
                    Ok(c) => c,
                    // Fail closed — see `configs_unavailable`.
                    Err(e) => {
                        return crate::services::llm_refusal::guardrails_unavailable(
                            &state,
                            &crate::services::llm_refusal::RefusalScope::new(
                                &auth_ctx,
                                &headers,
                                &correlation_id,
                                "/chat/completions",
                                &run_attr,
                            )
                            .started(start),
                            crate::constants::guardrail_surface::CHAT_OUTPUT,
                            &e,
                        );
                    }
                };

                tokio::spawn(crate::services::guardrails::flags::carried(async move {
                    // Hold the RG concurrency slot for the whole stream —
                    // dropped when this task ends (normal completion, client
                    // disconnect, or error), releasing the cluster-wide slot.
                    let _stream_conc_guard = stream_conc_guard;
                    // Output-guardrail state machine for this stream.
                    // `process()` runs the full orchestrator over the
                    // streamed content — sync buffer-and-release with
                    // redaction when a redact-action output check is
                    // active (`stream_guard::needs_sync`), else
                    // async-interrupt (forward verbatim, terminate on a
                    // Block). Both SSE branches route every content chunk
                    // through it so the policy lives in one place.
                    let mut stream_guard = crate::services::stream_guard::StreamGuard::new(
                        stream_guardrail_configs.into_vec(),
                        state_bg.http_client.clone(),
                    )
                    .for_run(tenant_id_bg.clone(), run_id_for_abort.clone());
                    // `guardrail_blocked` guards the check from re-emitting
                    // the error event if banned content keeps streaming
                    // past the first hit. After we set it and `break
                    // 'stream`, the loop never re-enters — but Clippy
                    // doesn't know that, so we read it back at end of
                    // scope via the `tracing::debug` below to be explicit.
                    let mut guardrail_blocked = false;
                    let mut sse_buf = crate::services::sse::SseByteBuffer::new();
                    let mut total_chunks = 0u32;
                    let mut total_response_bytes = 0usize;
                    // The on-the-wire response text (post-guard / post-redaction),
                    // accumulated by `emit_stream_action` from the frames we
                    // actually send to the client. This is what the proxy log
                    // records, so the audit body matches the wire exactly.
                    let mut accumulated_content = String::with_capacity(4096);
                    // Streamed tool-call output governance: accumulate the
                    // tool calls and HOLD the terminal finish chunk until
                    // governance runs at stream end (banned-arg patterns,
                    // argument policies, per-group tool-call counters). Without
                    // this, streamed tool calls bypassed governance entirely.
                    let mut tool_acc =
                        crate::services::stream_guard::ToolCallAccumulator::default();
                    let mut held_finish_chunk: Option<String> = None;
                    // Raw upstream text is deliberately discarded: we must not
                    // persist unredacted PII in the audit log. `extract_stream_chunk_data`
                    // still needs a sink for its content pass while it harvests
                    // finish_reason / usage / cache counters from each chunk.
                    let mut upstream_text_sink = String::with_capacity(4096);
                    let mut finish_reason: Option<String> = None;
                    let mut prompt_tokens: Option<i32> = None;
                    let mut completion_tokens: Option<i32> = None;
                    // Provider-side prompt-cache counters harvested from
                    // the stream's usage events. 0 + 0 means either the
                    // provider doesn't expose cache telemetry or the
                    // request simply didn't hit.
                    let mut cache_write_tokens: i32 = 0;
                    let mut cache_read_tokens: i32 = 0;
                    let mut normalizer = providers::create_stream_normalizer(&model_bg.provider);

                    futures::pin_mut!(byte_stream);
                    // When the client disconnects mid-stream, the SSE receiver
                    // is dropped and `tx.send()` starts returning Err. The
                    // mpsc is bounded (cap 32), so silently ignoring that Err
                    // with `let _ =` previously let the task keep buffering
                    // upstream bytes into a dead channel until the next
                    // `.await` on a full buffer blocked forever — the stream
                    // appeared to hang with no logs. Break immediately on
                    // send failure and surface it in tracing.
                    'stream: while let Some(chunk_result) = byte_stream.next().await {
                        match chunk_result {
                            Ok(bytes) => {
                                total_response_bytes += bytes.len();
                                if total_response_bytes > MAX_STREAM_BYTES {
                                    tracing::warn!(
                                        total_bytes = total_response_bytes,
                                        "Stream exceeded safety ceiling ({} MB), aborting",
                                        MAX_STREAM_BYTES / (1024 * 1024)
                                    );
                                    break;
                                }
                                sse_buf.push(&bytes);

                                // Parse SSE events — UTF-8-safe (decodes only
                                // complete events) and CRLF-aware. See
                                // services::sse.
                                while let Some(event_str) = sse_buf.next_event() {
                                    for line in event_str.lines() {
                                        if let Some(data) =
                                            crate::services::sse::sse_field(line, "data")
                                        {
                                            if needs_normalization {
                                                // Convert Anthropic SSE → OpenAI SSE
                                                for converted in normalizer.convert(data) {
                                                    if converted == "[DONE]" {
                                                        // Flush any sync-mode held-back content
                                                        // before [DONE] so the tail precedes it.
                                                        let action = stream_guard.finish().await;
                                                        if emit_stream_action(
                                                            &tx,
                                                            action,
                                                            &mut total_chunks,
                                                            &mut guardrail_blocked,
                                                            &mut accumulated_content,
                                                            &corr_id,
                                                        )
                                                        .await
                                                        {
                                                            break 'stream;
                                                        }
                                                        // Gate any held tool-call finish chunk on
                                                        // output governance before the terminal.
                                                        if finalize_streamed_tool_governance(
                                                            &tx,
                                                            &state_bg,
                                                            &target_group_bg,
                                                            &model_bg.id,
                                                            &tool_acc,
                                                            &mut held_finish_chunk,
                                                            finish_reason.as_deref(),
                                                            &RefusalScope::new(
                                                                &auth_bg,
                                                                &headers_bg,
                                                                &corr_id,
                                                                "/chat/completions",
                                                                &run_attr_bg,
                                                            )
                                                            .started(start),
                                                        )
                                                        .await
                                                        {
                                                            guardrail_blocked = true;
                                                            break 'stream;
                                                        }
                                                        if tx
                                                            .send(Ok(
                                                                Event::default().data("[DONE]")
                                                            ))
                                                            .await
                                                            .is_err()
                                                        {
                                                            tracing::debug!(
                                                                correlation_id = %corr_id,
                                                                chunks = total_chunks,
                                                                "Client disconnected mid-stream; aborting forwarder"
                                                            );
                                                            break 'stream;
                                                        }
                                                    } else {
                                                        // Extract content/usage for the audit log,
                                                        // then route the chunk through the output
                                                        // guard (sync redaction or async-interrupt).
                                                        extract_stream_chunk_data(
                                                            &converted,
                                                            &mut upstream_text_sink,
                                                            &mut finish_reason,
                                                            &mut prompt_tokens,
                                                            &mut completion_tokens,
                                                            &mut cache_write_tokens,
                                                            &mut cache_read_tokens,
                                                        );
                                                        // Accumulate tool calls; hold the finish
                                                        // chunk for a tool-call response so it can
                                                        // be gated on governance at [DONE].
                                                        if !ingest_and_maybe_hold_finish(
                                                            &converted,
                                                            &mut tool_acc,
                                                            &mut held_finish_chunk,
                                                        ) {
                                                            let action = stream_guard
                                                                .process(&converted)
                                                                .await;
                                                            if emit_stream_action(
                                                                &tx,
                                                                action,
                                                                &mut total_chunks,
                                                                &mut guardrail_blocked,
                                                                &mut accumulated_content,
                                                                &corr_id,
                                                            )
                                                            .await
                                                            {
                                                                break 'stream;
                                                            }
                                                        }
                                                    }
                                                }
                                            } else if data.trim() == "[DONE]" {
                                                // Flush any sync-mode held-back content before
                                                // [DONE] so the tail precedes it.
                                                let action = stream_guard.finish().await;
                                                if emit_stream_action(
                                                    &tx,
                                                    action,
                                                    &mut total_chunks,
                                                    &mut guardrail_blocked,
                                                    &mut accumulated_content,
                                                    &corr_id,
                                                )
                                                .await
                                                {
                                                    break 'stream;
                                                }
                                                // Gate any held tool-call finish chunk on output
                                                // governance before the terminal [DONE].
                                                if finalize_streamed_tool_governance(
                                                    &tx,
                                                    &state_bg,
                                                    &target_group_bg,
                                                    &model_bg.id,
                                                    &tool_acc,
                                                    &mut held_finish_chunk,
                                                    finish_reason.as_deref(),
                                                    &RefusalScope::new(
                                                        &auth_bg,
                                                        &headers_bg,
                                                        &corr_id,
                                                        "/chat/completions",
                                                        &run_attr_bg,
                                                    )
                                                    .started(start),
                                                )
                                                .await
                                                {
                                                    guardrail_blocked = true;
                                                    break 'stream;
                                                }
                                                if tx
                                                    .send(Ok(Event::default().data("[DONE]")))
                                                    .await
                                                    .is_err()
                                                {
                                                    tracing::debug!(
                                                        correlation_id = %corr_id,
                                                        chunks = total_chunks,
                                                        "Client disconnected mid-stream; aborting forwarder"
                                                    );
                                                    break 'stream;
                                                }
                                            } else {
                                                // Extract content/usage for the audit log, then
                                                // route the chunk through the output guard.
                                                extract_stream_chunk_data(
                                                    data,
                                                    &mut upstream_text_sink,
                                                    &mut finish_reason,
                                                    &mut prompt_tokens,
                                                    &mut completion_tokens,
                                                    &mut cache_write_tokens,
                                                    &mut cache_read_tokens,
                                                );
                                                // Accumulate tool calls; hold the finish chunk for
                                                // a tool-call response (gated at [DONE]).
                                                if !ingest_and_maybe_hold_finish(
                                                    data,
                                                    &mut tool_acc,
                                                    &mut held_finish_chunk,
                                                ) {
                                                    // An
                                                    // operator can stop a run
                                                    // mid-stream. Dropping
                                                    // the upstream connection
                                                    // is what stops the
                                                    // provider generating,
                                                    // and billing.
                                                    if stream_guard.run_aborted(&state).await {
                                                        let _ = emit_stream_action(
                                                            &tx,
                                                            crate::services::stream_guard::StreamAction::Block(
                                                                crate::services::run_intervention::ABORTED_MESSAGE
                                                                    .to_string(),
                                                            ),
                                                            &mut total_chunks,
                                                            &mut guardrail_blocked,
                                                            &mut accumulated_content,
                                                            &corr_id,
                                                        )
                                                        .await;
                                                        break 'stream;
                                                    }
                                                    let action = stream_guard.process(data).await;
                                                    if emit_stream_action(
                                                        &tx,
                                                        action,
                                                        &mut total_chunks,
                                                        &mut guardrail_blocked,
                                                        &mut accumulated_content,
                                                        &corr_id,
                                                    )
                                                    .await
                                                    {
                                                        break 'stream;
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                            Err(e) => {
                                tracing::warn!("Stream error: {e}");
                                break;
                            }
                        }
                    }

                    // Flush any sync-mode held-back tail for providers that
                    // end the stream without a `[DONE]` sentinel (the
                    // in-loop `[DONE]` handlers already flush; this is the
                    // no-`[DONE]` safety net — the segmenter is empty after
                    // an earlier flush, so it's a no-op in the common case).
                    if !guardrail_blocked {
                        let action = stream_guard.finish().await;
                        let _ = emit_stream_action(
                            &tx,
                            action,
                            &mut total_chunks,
                            &mut guardrail_blocked,
                            &mut accumulated_content,
                            &corr_id,
                        )
                        .await;
                    }

                    // No-`[DONE]` providers: a tool-call finish chunk may still
                    // be held (the in-loop [DONE] handlers already finalised
                    // when a [DONE] arrived — `held_finish_chunk` is then None,
                    // so this is a no-op in that case). Gate it on governance
                    // here so streamed tool calls are never released ungoverned.
                    if !guardrail_blocked && held_finish_chunk.is_some() {
                        if finalize_streamed_tool_governance(
                            &tx,
                            &state_bg,
                            &target_group_bg,
                            &model_bg.id,
                            &tool_acc,
                            &mut held_finish_chunk,
                            finish_reason.as_deref(),
                            &RefusalScope::new(
                                &auth_bg,
                                &headers_bg,
                                &corr_id,
                                "/chat/completions",
                                &run_attr_bg,
                            )
                            .started(start),
                        )
                        .await
                        {
                            guardrail_blocked = true;
                        }
                    }

                    // accumulated_content is already built incrementally
                    let (reasoning_logged, reasoning_withheld) = stream_guard.reasoning_record();
                    let reasoning_logged = reasoning_logged.map(str::to_owned);
                    tracing::debug!(
                        correlation_id = %corr_id,
                        chunks = total_chunks,
                        guardrail_blocked,
                        "Stream loop exited"
                    );
                    let total_tokens = prompt_tokens.zip(completion_tokens).map(|(p, c)| p + c);

                    // Background logging after stream completes
                    let duration = start.elapsed().as_millis() as f64;
                    // Background logging after a streaming call — pass
                    // stream=true so Gemini's URL is logged as the
                    // `:streamGenerateContent` variant we actually hit.
                    let target_url = providers::build_url(&model_bg, "/chat/completions", true);
                    // Reflect a guardrail/governance block in the audit row.
                    // Previously the streaming log was hard-coded 200/success,
                    // so a blocked stream (content OR tool-call governance) was
                    // mis-recorded as a successful 200.
                    let (log_status, log_success, log_error): (u16, bool, Option<&str>) =
                        if guardrail_blocked {
                            (
                                403,
                                false,
                                Some("Response blocked by output guardrail / governance policy"),
                            )
                        } else {
                            (200, true, None)
                        };
                    let mut log = proxy_logger::build_proxy_log(
                        "llm",
                        &auth_bg,
                        "POST",
                        "/chat/completions",
                        Some(&target_url),
                        Some(&model_bg.id),
                        log_status,
                        duration,
                        log_success,
                        log_error,
                        &corr_id,
                    );
                    log.proxy_subtype =
                        Some(crate::constants::proxy_subtype::CHAT_COMPLETIONS.to_string());
                    llm_helpers::populate_group_id(&mut log, &target_group_bg);
                    log.user_agent = user_agent.clone();
                    log.request_size_bytes = Some(request_body_str.len() as i32);
                    log.request_headers = Some(sanitized_req_headers.clone());
                    log.request_body_preview = Some(body_preview(&request_body_str));
                    log.response_size_bytes = Some(total_response_bytes as i32);
                    log.response_body_preview = Some(
                        serde_json::to_string(&json!({
                            "streaming": true,
                            "chunks": total_chunks,
                            "finish_reason": finish_reason,
                            "content": accumulated_content,
                            "reasoning_content": reasoning_logged,
                            "reasoning_withheld": reasoning_withheld,
                        }))
                        .unwrap_or_default(),
                    );
                    log.llm_model_requested = model_requested.clone();
                    log.llm_model_used = Some(model_bg.model_name.clone());
                    log.llm_provider = Some(model_bg.provider.clone());
                    log.llm_prompt_tokens = prompt_tokens;
                    log.llm_completion_tokens = completion_tokens;
                    log.llm_total_tokens = total_tokens;
                    log.llm_streaming = Some(true);
                    log.llm_used_fallback = Some(false);
                    populate_llm_request_fields(&mut log, &request_body_val);

                    // Estimate cost
                    log.llm_estimated_cost_usd = llm_helpers::estimate_llm_cost(
                        prompt_tokens,
                        completion_tokens,
                        input_cost,
                        output_cost,
                    );

                    // Prompt cache telemetry (streaming). We accumulated
                    // the provider's cache counters off the usage events
                    // as the stream ran. Mission Control reads these to
                    // show per-group / per-tenant cache hit rate and $
                    // saved. Zero counters → skip the fields so we don't
                    // pollute aggregates with rows that look like "cache
                    // supported but not hitting" when the provider
                    // doesn't report cache usage at all.
                    let pc_mechanism = crate::services::prompt_cache_usage::mechanism_for_provider(
                        &model_bg.provider,
                        &model_bg.model_name,
                    );
                    let pc_usage = crate::services::prompt_cache_usage::usage_from_stream_totals(
                        cache_write_tokens,
                        cache_read_tokens,
                        pc_mechanism,
                        &model_bg,
                    );
                    if pc_usage.write_tokens > 0 || pc_usage.read_tokens > 0 {
                        log.prompt_cache_write_tokens = Some(pc_usage.write_tokens);
                        log.prompt_cache_read_tokens = Some(pc_usage.read_tokens);
                        log.prompt_cache_savings_usd = Some(pc_usage.savings_usd);
                        let mech_label =
                            crate::services::proxy_metrics::mechanism_label(pc_mechanism);
                        crate::services::proxy_metrics::record_prompt_cache_tokens(
                            "read",
                            mech_label,
                            &model_bg.provider,
                            pc_usage.read_tokens,
                        );
                        crate::services::proxy_metrics::record_prompt_cache_tokens(
                            "write",
                            mech_label,
                            &model_bg.provider,
                            pc_usage.write_tokens,
                        );
                        crate::services::proxy_metrics::record_prompt_cache_savings(
                            mech_label,
                            &model_bg.provider,
                            pc_usage.savings_usd,
                        );
                        // The answer warmed this model's cache (or read
                        // from it): tell the session, which may pin the
                        // conversation to the model so the next request
                        // with the same prefix picks up the hit.
                        prompt_cache_bg
                            .record_warm(&request_for_cache, &model_bg, pc_usage.write_tokens)
                            .await;
                    }

                    // RAG fields
                    llm_helpers::populate_rag_log_fields(&mut log, &rag_result_bg);

                    let estimated_cost = log.llm_estimated_cost_usd;

                    proxy_logger::apply_call_chain(
                        &mut log,
                        &call_chain,
                        &current_task_id_str,
                        trace_continuity,
                        trace_root_kind,
                    );
                    // How the guard ended the stream (operator abort / guardrail block) is recorded on its own
                    // outcome row — the row that names the run and step and carries the
                    // tokens the provider billed before the guard stopped it.
                    stream_guard.stamp_outcome(&mut log);
                    match &residency_decision {
                        Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
                        None => proxy_logger::apply_residency_not_evaluated(
                            &mut log,
                            provider_region_for_audit.as_deref(),
                        ),
                    }
                    if input_pii_redacted {
                        proxy_logger::apply_pii_detected(&mut log);
                    }
                    state_bg
                        .background_writer
                        .write_proxy_log(state_bg.db.clone(), log);

                    // Store in semantic cache (streaming path).
                    //
                    // Contract reminder — both layers cache or both skip,
                    // gated by `should_cache_tool_response`:
                    //   * no tool invoked → always cache
                    //   * tool invoked + master switch on → skip
                    //   * tool invoked + master off + per-tool rules → allow/skip
                    {
                        // The answer's side of the tool gate. The streaming
                        // path does not accumulate whole tool-call objects,
                        // so a tool call is one unnamed entry: enough for
                        // "a tool was invoked" to apply.
                        let invoked_tools: Vec<(String, String)> =
                            if llm_helpers::finish_reason_is_tool_call(finish_reason.as_deref()) {
                                vec![(String::new(), String::new())]
                            } else {
                                Vec::new()
                            };
                        let total = prompt_tokens.unwrap_or(0) + completion_tokens.unwrap_or(0);
                        let cache_key = CacheKey {
                            tenant_id: &tenant_id_bg,
                            group_id: target_group_bg.as_ref().map(|g| g.id.as_str()),
                            model_name: &model_bg.model_name,
                            body: CacheBody::Chat(&request_for_cache),
                        };
                        // A stream the output guardrail stopped must
                        // not be cached. `accumulated_content` is built
                        // incrementally and is NOT cleared on a block, so at this
                        // point it holds whatever got through before the guard
                        // fired: the refused text, truncated. Storing it converts
                        // a one-off block into a permanent cached answer, because
                        // the lookup path returns cached bodies without a
                        // guardrail pass. The audit row above already records the
                        // block as 403; this is the other half of honouring it.
                        //
                        // Nor is a RAG-grounded answer.
                        if !guardrail_blocked
                            && !kb_grounded
                            && let Some(plan) = cache_session_bg.plan_store(
                                &cache_key,
                                StoreFacts {
                                    invoked_tools,
                                    token_count: total,
                                },
                            )
                        {
                            // A non-streaming response built from the
                            // accumulated content.
                            plan.commit(serde_json::json!({
                                "id": format!("chatcmpl-cached-{}", ulid::Ulid::new()),
                                "object": "chat.completion",
                                "choices": [{
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": accumulated_content
                                    },
                                    "finish_reason": finish_reason
                                }],
                                "usage": {
                                    "prompt_tokens": prompt_tokens,
                                    "completion_tokens": completion_tokens,
                                    "total_tokens": total
                                }
                            }));
                        }
                    }

                    // Proxy metric
                    let llm_metric_data = serde_json::json!({
                        "provider": model_bg.provider,
                        "model_requested": model_requested,
                        "model_used": model_bg.model_name,
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": total_tokens,
                        "estimated_cost_usd": estimated_cost,
                        "streaming": true,
                    });
                    if !is_probe {
                        let metric = proxy_logger::build_proxy_metric(
                            "llm",
                            &auth_bg,
                            "POST",
                            "/chat/completions",
                            "chat_completions",
                            Some(&model_bg.id),
                            200,
                            duration,
                            true,
                            &corr_id,
                            Some(request_body_str.len() as i32),
                            Some(total_response_bytes as i32),
                            Some(req_headers_count),
                            None,
                            user_agent.as_deref(),
                            Some(llm_metric_data),
                            None,
                        );
                        state_bg
                            .background_writer
                            .write_proxy_metric(state_bg.db.clone(), metric);
                    }
                    // A probe records its real usage (billed to Unbound);
                    // only the metric is left to the central probe recorder.

                    // Usage log
                    usage::write_llm_usage_log(
                        &state_bg.usage_state,
                        &state_bg.background_writer,
                        &state_bg.model_metrics,
                        state_bg.db.clone(),
                        &tenant_id_bg,
                        &auth_bg,
                        target_group_bg.as_ref(),
                        &model_bg.id,
                        &model_bg.model_name,
                        &model_bg.provider,
                        prompt_tokens,
                        completion_tokens,
                        estimated_cost,
                        input_cost,
                        output_cost,
                        &target_url,
                        &corr_id,
                        200,
                        duration,
                    );
                    if let Some(ref g) = target_group_bg {
                        usage::spawn_alert_evaluation(
                            state_bg.db.clone(),
                            tenant_id_bg.clone(),
                            g.clone(),
                            "llm".into(),
                            Some(corr_id.clone()),
                        );
                    }

                    // Prometheus metrics
                    proxy_metrics::record_request("llm", "POST", 200, &model_bg.provider);
                    proxy_metrics::record_latency(
                        "llm",
                        "POST",
                        &model_bg.provider,
                        duration / 1000.0,
                    );
                    if let Some(pt) = prompt_tokens {
                        proxy_metrics::record_tokens(
                            "prompt",
                            &model_bg.provider,
                            &model_bg.model_name,
                            pt,
                        );
                    }
                    if let Some(ct) = completion_tokens {
                        proxy_metrics::record_tokens(
                            "completion",
                            &model_bg.provider,
                            &model_bg.model_name,
                            ct,
                        );
                    }
                    if let Some(cost) = estimated_cost {
                        proxy_metrics::record_cost(&model_bg.provider, &model_bg.model_name, cost);
                    }
                    proxy_metrics::stream_closed();

                    // Router state: record stream completion
                    let stream_total_tokens = prompt_tokens
                        .zip(completion_tokens)
                        .map(|(p, c)| (p as i64) + (c as i64));
                    llm_router::record_stream_success(
                        &state_bg.router_state,
                        &model_bg.id,
                        duration,
                        stream_total_tokens,
                        model_bg.routed_via_group_id.as_deref(),
                    );

                    // Live health status update for streaming completion
                    state_bg.mcp_health.record_llm_success(
                        state_bg.db.clone(),
                        &tenant_id_bg,
                        &model_bg.id,
                    );
                }));

                let stream = ReceiverStream::new(rx);
                let mut resp = Sse::new(stream)
                    .keep_alive(KeepAlive::default())
                    .into_response();

                resp.headers_mut().insert(
                    crate::constants::headers::X_CORRELATION_ID,
                    correlation_id.parse().unwrap(),
                );
                // The run this response belongs to, so a client can attribute
                // feedback to the task it just saw rather than the server having
                // to infer it from timing. Counterpart of the inbound header.
                if let Ok(v) = run_id_header.parse() {
                    resp.headers_mut()
                        .insert(crate::constants::headers::X_BRUTOR_RUN_ID, v);
                }
                resp.headers_mut().insert(
                    crate::constants::headers::X_AUTH_TYPE,
                    auth_ctx.auth_type.to_string().parse().unwrap(),
                );

                resp
            }
            Err(e) => {
                let duration = start.elapsed().as_millis() as f64;

                // Live health status update on streaming error
                state.mcp_health.record_llm_failure(
                    state.db.clone(),
                    &tenant_id,
                    &model.id,
                    &e.to_string(),
                );

                let (status, body) = match &e {
                    llm_router::LlmRouterError::UpstreamHttpError { status, body } => (
                        StatusCode::from_u16(*status).unwrap_or(StatusCode::BAD_GATEWAY),
                        body.clone(),
                    ),
                    llm_router::LlmRouterError::AllModelsBusy { .. }
                    | llm_router::LlmRouterError::RateLimitExceeded(_) => (
                        StatusCode::TOO_MANY_REQUESTS,
                        json!({"error": e.to_string()}),
                    ),
                    llm_router::LlmRouterError::ConcurrencyLimitReached(_) => (
                        StatusCode::TOO_MANY_REQUESTS,
                        json!({"error": e.to_string()}),
                    ),
                    _ => (StatusCode::BAD_GATEWAY, json!({"error": e.to_string()})),
                };

                let target_url = providers::build_url(&model, "/chat/completions", false);
                let request_body_str = serde_json::to_string(&request).unwrap_or_default();
                let mut log = proxy_logger::build_proxy_log(
                    "llm",
                    &auth_ctx,
                    "POST",
                    "/chat/completions",
                    Some(&target_url),
                    Some(&model.id),
                    status.as_u16(),
                    duration,
                    false,
                    Some(&e.to_string()),
                    &correlation_id,
                );
                stamp_judge_self_call(&mut log, is_judge_self_call);
                llm_helpers::populate_group_id(&mut log, &target_group);
                log.user_agent = extract_user_agent(&headers);
                log.request_size_bytes = Some(request_body_str.len() as i32);
                log.request_body_preview = Some(body_preview(&request_body_str));
                log.llm_model_requested = request.model.clone();
                log.llm_model_used = Some(model.model_name.clone());
                log.llm_provider = Some(model.provider.clone());
                log.llm_streaming = Some(true);
                // The gateway's own routing limit: this row is the refusal.
                if let Some(sub) = llm_refusal::router_limit_subtype(&e) {
                    log.proxy_subtype = Some(sub);
                }
                populate_llm_request_fields(
                    &mut log,
                    &serde_json::to_value(&request).unwrap_or_default(),
                );
                proxy_logger::apply_call_chain(
                    &mut log,
                    &call_chain,
                    &current_task_id_str,
                    trace_continuity,
                    trace_root_kind,
                );
                match &residency_decision {
                    Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
                    None => proxy_logger::apply_residency_not_evaluated(
                        &mut log,
                        provider_region_for_audit.as_deref(),
                    ),
                }
                state
                    .background_writer
                    .write_proxy_log(state.db.clone(), log);

                proxy_metrics::record_request("llm", "POST", status.as_u16(), &model.provider);
                proxy_metrics::record_latency("llm", "POST", &model.provider, duration / 1000.0);

                // Fire a downstream alert for provider-side failures —
                // streaming path only preserves the provider's own JSON
                // error body, so we alert-then-return without reshaping.
                if let Some(reason) = classify_router_error(&e) {
                    spawn_raise(
                        state.db.clone(),
                        SystemAlertInput::downstream_provider(
                            Some(tenant_id.clone()),
                            &model.provider,
                            Some(model.id.clone()),
                            Some(model.model_name.clone()),
                            reason.error_code,
                            e.to_string(),
                        ),
                    );
                }
                error_response(status, &body.to_string())
            }
        }
    } else {
        // ===== NON-STREAMING PATH (with fallback support) =====
        //
        // The prompt-cache policy is loaded at the start of the
        // handler (so streaming and non-streaming share the same
        // resolved value). Passed into the primary-model call so
        // Anthropic-mechanism requests get `cache_control` markers
        // injected; fallback providers skip caching (different KV
        // cache, different billing).

        // Cluster-wide RG / per-model concurrency gate
        // (`llm_global_limits.concurrency.max_concurrent`, group + model scope,
        // restrictive/stacking). The STREAMING path already acquires this; the
        // non-streaming path did NOT, so `max_concurrent` was silently
        // unenforced for ordinary (non-SSE) chat completions — 10 parallel
        // calls all returned 200. Held in `_nonstream_conc_guard` for the whole
        // upstream round-trip + response build; released on Drop when this
        // branch returns. Saturation → 429 before we open the connection.
        let _nonstream_conc_guard = match crate::services::llm_refusal::acquire_concurrency(
            &state,
            target_group.as_ref(),
            &model,
            &RefusalScope {
                auth_ctx: &auth_ctx,
                headers: &headers,
                correlation_id: &correlation_id,
                endpoint: "/chat/completions",
                started: Some(start),
                model: Some((&model, None)),
                group_id: None,
                run: Some(&llm_refusal::RunAttribution::chain(
                    &call_chain,
                    &current_task_id_str,
                    trace_continuity,
                )),
                request_body: None,
            },
        )
        .await
        {
            Ok(guard) => guard,
            Err(resp) => return resp,
        };

        // Propagate the verified
        // call chain into every fallback provider request via the
        // task-local read by `llm_router`'s reqwest builder.
        let outbound_chain = call_chain.to_header_pairs(&tenant_key);
        let outbound_chain_owned: Vec<(&'static str, String)> =
            outbound_chain.into_iter().collect();
        // Capture the exact body POSTed upstream (resolved model,
        // provider-translated shape, system prompt, prompt-cache markers)
        // so the proxy log records what the provider actually received.
        let (fwd_outcome, outbound_body_logged) = llm_router::OUTBOUND_REQUEST_BODY
            .scope(std::cell::RefCell::new(None), async {
                let r = crate::services::llm_governance::EFFECTIVE_PARAM_CAPS
                    .scope(
                        param_caps.clone().unwrap_or_default(),
                        crate::services::call_chain::OUTBOUND_CHAIN_HEADERS.scope(
                            outbound_chain_owned,
                            // Scope the response-cache identity so the exact-
                            // match cache is isolated per (tenant, caller,
                            // group) — never shared across users.
                            //
                            // Unless the request said `X-Brutor-Cache:
                            // off`. The router neither reads nor writes its
                            // exact-match cache without an identity, so not
                            // scoping one is how "bypass the cache entirely"
                            // reaches the cache the handler cannot see.
                            {
                                let forward = llm_router::forward_with_fallbacks(
                                    &state.http_client,
                                    &state.db,
                                    &model,
                                    &request,
                                    false,
                                    &state.router_state,
                                    Some(prompt_cache.as_ref()),
                                    Some(&state),
                                    target_group.as_ref(),
                                );
                                response_cache::with_dispatch_cache(
                                    state.response_cache.dispatch_cache(&headers, &auth_ctx),
                                    forward,
                                )
                            },
                        ),
                    )
                    .await;
                let captured = llm_router::OUTBOUND_REQUEST_BODY.with(|c| c.borrow().clone());
                (r, captured)
            })
            .await;
        match fwd_outcome {
            Ok((mut resp_body, status_code, used_model, used_fallback)) => {
                let duration = start.elapsed().as_millis() as f64;
                let success = (200..300).contains(&status_code);

                // Live health status update
                if success {
                    state.mcp_health.record_llm_success(
                        state.db.clone(),
                        &tenant_id,
                        &used_model.id,
                    );
                } else {
                    // Include the upstream's response body in the
                    // recorded error so operators can read the actual
                    // failure reason in the Admin Console (e.g. HF's
                    // `model_not_supported`, OpenAI's
                    // `insufficient_quota`). Without this, every 4xx
                    // surfaces as a bare "HTTP 400" badge with no clue
                    // why the model is unhealthy. Capped to keep the
                    // `health_check_error` column compact — full
                    // body is preserved in proxy_logs for forensics.
                    let body_excerpt = serde_json::to_string(&resp_body)
                        .ok()
                        .map(|s| {
                            if s.len() > 500 {
                                format!("{}…", &s[..500])
                            } else {
                                s
                            }
                        })
                        .unwrap_or_default();
                    let error_msg = if body_excerpt.is_empty() {
                        format!("HTTP {status_code}")
                    } else {
                        format!("HTTP {status_code}: {body_excerpt}")
                    };
                    state.mcp_health.record_llm_failure(
                        state.db.clone(),
                        &tenant_id,
                        &used_model.id,
                        &error_msg,
                    );
                }

                // Record latency in router state
                if success {
                    state.router_state.record_latency(&used_model.id, duration);
                }

                tracing::debug!(
                    model = %used_model.model_name,
                    provider = %used_model.provider,
                    status_code,
                    duration_ms = duration,
                    used_fallback,
                    success,
                    "Chat completion response"
                );

                // Extract usage for logging
                let usage_obj = resp_body.get("usage");
                let prompt_tokens = usage_obj
                    .and_then(|u| u.get("prompt_tokens"))
                    .and_then(|v| v.as_i64())
                    .map(|v| v as i32);
                let completion_tokens = usage_obj
                    .and_then(|u| u.get("completion_tokens"))
                    .and_then(|v| v.as_i64())
                    .map(|v| v as i32);

                // Capture request/response body previews. The request body
                // is what was actually sent upstream (captured in the
                // router), falling back to the inbound request if capture
                // didn't run.
                let request_body_str = outbound_body_logged
                    .clone()
                    .unwrap_or_else(|| serde_json::to_string(&request).unwrap_or_default());
                let response_body_str = serde_json::to_string(&resp_body).unwrap_or_default();

                // Background: proxy log
                let target_url = providers::build_url(&used_model, "/chat/completions", false);
                let mut log = proxy_logger::build_proxy_log(
                    "llm",
                    &auth_ctx,
                    "POST",
                    "/chat/completions",
                    Some(&target_url),
                    Some(&used_model.id),
                    status_code,
                    duration,
                    success,
                    None,
                    &correlation_id,
                );
                stamp_judge_self_call(&mut log, is_judge_self_call);
                let ua = extract_user_agent(&headers);
                log.proxy_subtype =
                    Some(crate::constants::proxy_subtype::CHAT_COMPLETIONS.to_string());
                llm_helpers::populate_group_id(&mut log, &target_group);
                log.user_agent = ua.clone();
                log.request_size_bytes = Some(request_body_str.len() as i32);
                log.request_headers = Some(proxy_logger::sanitize_headers(&headers));
                log.request_body_preview = Some(body_preview(&request_body_str));
                log.response_size_bytes = Some(response_body_str.len() as i32);
                log.response_body_preview = Some(body_preview(&response_body_str));
                log.llm_model_requested = request.model.clone();
                log.llm_model_used = Some(used_model.model_name.clone());
                log.llm_provider = Some(used_model.provider.clone());
                log.llm_prompt_tokens = prompt_tokens;
                log.llm_completion_tokens = completion_tokens;
                log.llm_total_tokens = prompt_tokens.zip(completion_tokens).map(|(p, c)| p + c);
                log.llm_streaming = Some(false);
                log.llm_used_fallback = Some(used_fallback);
                // Input guardrail redacted a PII category → stamp the
                // success row (output redaction is stamped separately in
                // the Redacted arm of the output-guardrail loop below).
                if input_pii_redacted {
                    proxy_logger::apply_pii_detected(&mut log);
                }
                if used_fallback {
                    log.llm_fallback_reason =
                        Some(format!("Fallback to {}", used_model.model_name));
                }
                populate_llm_request_fields(
                    &mut log,
                    &serde_json::to_value(&request).unwrap_or_default(),
                );

                // Estimate cost
                let log_estimated_cost = llm_helpers::estimate_llm_cost(
                    prompt_tokens,
                    completion_tokens,
                    used_model.cost_per_input_token.unwrap_or(0.0),
                    used_model.cost_per_output_token.unwrap_or(0.0),
                );
                log.llm_estimated_cost_usd = log_estimated_cost;

                // Prompt cache telemetry — extract whatever the
                // provider reported. Non-cached calls and non-reporting
                // providers return zeros; we still write the row so
                // Mission Control can tell apart "prompt cache not
                // supported" from "supported but not hitting."
                let pc_mechanism = crate::services::prompt_cache_usage::mechanism_for_provider(
                    &used_model.provider,
                    &used_model.model_name,
                );
                let pc_usage = crate::services::prompt_cache_usage::extract_usage_from_response(
                    &resp_body,
                    pc_mechanism,
                    &used_model,
                );
                if pc_usage.write_tokens > 0 || pc_usage.read_tokens > 0 {
                    log.prompt_cache_write_tokens = Some(pc_usage.write_tokens);
                    log.prompt_cache_read_tokens = Some(pc_usage.read_tokens);
                    log.prompt_cache_savings_usd = Some(pc_usage.savings_usd);
                    let mech_label = crate::services::proxy_metrics::mechanism_label(pc_mechanism);
                    crate::services::proxy_metrics::record_prompt_cache_tokens(
                        "read",
                        mech_label,
                        &used_model.provider,
                        pc_usage.read_tokens,
                    );
                    crate::services::proxy_metrics::record_prompt_cache_tokens(
                        "write",
                        mech_label,
                        &used_model.provider,
                        pc_usage.write_tokens,
                    );
                    crate::services::proxy_metrics::record_prompt_cache_savings(
                        mech_label,
                        &used_model.provider,
                        pc_usage.savings_usd,
                    );
                    // Only an answer pins. This branch also handles an
                    // upstream error, and an error body that carries a usage
                    // block must not pin the model that just failed (the
                    // router dropped that pin a moment ago).
                    if success {
                        prompt_cache
                            .record_warm(&request, &used_model, pc_usage.write_tokens)
                            .await;
                    }
                }

                // RAG fields
                llm_helpers::populate_rag_log_fields(&mut log, &rag_result);

                proxy_logger::apply_call_chain(
                    &mut log,
                    &call_chain,
                    &current_task_id_str,
                    trace_continuity,
                    trace_root_kind,
                );
                match &residency_decision {
                    Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
                    None => proxy_logger::apply_residency_not_evaluated(
                        &mut log,
                        provider_region_for_audit.as_deref(),
                    ),
                }
                // NOTE: `log` is finalised here but WRITTEN below, after the
                // output governance/guardrails run, so its status reflects a
                // guardrail block. It previously logged 200/success before a
                // subsequent 403 block, mis-recording blocked responses as
                // successful in the audit trail.

                // Store in semantic cache (fire-and-forget).
                // Both Redis exact and Qdrant semantic layers go together —
                // `should_cache_tool_response` decides for both.
                // The deferred semantic-cache store.
                //
                // Carried across the guardrail loop so the write happens only
                // after the output verdict is known, and only on the redacted
                // body. Declared here because the decision needs inputs that
                // are in scope inside the `success` branch, while the write
                // must happen after it.
                let mut cache_store_plan: Option<StorePlan> = None;
                // Never store a RAG-grounded answer; see the
                // lookup. The cache is not asked.
                if success && !kb_grounded {
                    // The answer's side of the tool gate: `tool_calls`
                    // populated means the model is asking for a tool. The
                    // request's side (this request being the second half of a
                    // tool round trip) and continuations are the cache's to
                    // judge from the key.
                    //
                    // Decide here, STORE LATER. This used to
                    // store on the spot, about 130 lines before the output
                    // guardrails run, so the raw upstream body was persisted
                    // unredacted and a completion a `block` verdict was about
                    // to 403 was cached anyway. The plan is made here, where
                    // the inputs are in scope; it is committed after the
                    // verdict, with the body that is returned.
                    cache_store_plan = cache_session.plan_store(
                        &CacheKey {
                            tenant_id: &tenant_id,
                            group_id: target_group.as_ref().map(|g| g.id.as_str()),
                            model_name: &used_model.model_name,
                            body: CacheBody::Chat(&request),
                        },
                        StoreFacts {
                            invoked_tools: llm_helpers::extract_chat_tool_calls(&resp_body),
                            token_count: prompt_tokens
                                .zip(completion_tokens)
                                .map(|(p, c)| p + c)
                                .unwrap_or(0),
                        },
                    );
                }

                // Background: proxy metric
                let llm_metric_data = serde_json::json!({
                    "provider": used_model.provider,
                    "model_requested": request.model,
                    "model_used": used_model.model_name,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens.zip(completion_tokens).map(|(p,c)| p+c),
                    "estimated_cost_usd": log_estimated_cost,
                    "streaming": false,
                    "used_fallback": used_fallback,
                });
                let req_hdr_count = headers.len() as i32;
                if !is_probe {
                    let metric = proxy_logger::build_proxy_metric(
                        "llm",
                        &auth_ctx,
                        "POST",
                        "/chat/completions",
                        "llm_chat",
                        Some(&used_model.id),
                        status_code,
                        duration,
                        success,
                        &correlation_id,
                        Some(request_body_str.len() as i32),
                        Some(response_body_str.len() as i32),
                        Some(req_hdr_count),
                        None,
                        ua.as_deref(),
                        Some(llm_metric_data),
                        None,
                    );
                    state
                        .background_writer
                        .write_proxy_metric(state.db.clone(), metric);
                }
                // A probe records its real usage (billed to Unbound);
                // only the metric is left to the central probe recorder.

                // Background: usage log
                if success {
                    usage::write_llm_usage_log(
                        &state.usage_state,
                        &state.background_writer,
                        &state.model_metrics,
                        state.db.clone(),
                        &tenant_id,
                        &auth_ctx,
                        target_group.as_ref(),
                        &used_model.id,
                        &used_model.model_name,
                        &used_model.provider,
                        prompt_tokens,
                        completion_tokens,
                        log_estimated_cost,
                        used_model.cost_per_input_token.unwrap_or(0.0),
                        used_model.cost_per_output_token.unwrap_or(0.0),
                        &target_url,
                        &correlation_id,
                        status_code,
                        duration,
                    );
                    if let Some(ref g) = target_group {
                        usage::spawn_alert_evaluation(
                            state.db.clone(),
                            tenant_id.clone(),
                            g.clone(),
                            "llm".into(),
                            Some(correlation_id.clone()),
                        );
                    }
                }

                // Prometheus metrics
                proxy_metrics::record_request("llm", "POST", status_code, &used_model.provider);
                proxy_metrics::record_latency(
                    "llm",
                    "POST",
                    &used_model.provider,
                    duration / 1000.0,
                );
                if let Some(pt) = prompt_tokens {
                    proxy_metrics::record_tokens(
                        "prompt",
                        &used_model.provider,
                        &used_model.model_name,
                        pt,
                    );
                }
                if let Some(ct) = completion_tokens {
                    proxy_metrics::record_tokens(
                        "completion",
                        &used_model.provider,
                        &used_model.model_name,
                        ct,
                    );
                }
                if let Some(cost) = log_estimated_cost {
                    proxy_metrics::record_cost(&used_model.provider, &used_model.model_name, cost);
                }

                // Post-response governance + output guardrails. We CAPTURE a
                // block into `guardrail_block` rather than returning
                // immediately, so the deferred audit-log write below records
                // the FINAL status (403) instead of the upstream 200. The
                // captured block response is returned after the row is written.
                let mut guardrail_block: Option<Response> = None;

                // Post-response governance: scan emitted tool calls against
                // banned_argument_patterns + increment the per-group
                // hourly/daily tool-call counters the pre-flight check reads
                // on the NEXT request. A block 403s the response.
                if let Err(resp) = llm_preflight::apply_response_governance(
                    &state,
                    &target_group,
                    &model.id,
                    &resp_body,
                    error_response,
                    &RefusalScope::new(
                        &auth_ctx,
                        &headers,
                        &correlation_id,
                        "/chat/completions",
                        &run_attr,
                    )
                    .started(start),
                )
                .await
                {
                    guardrail_block = Some(resp);
                }

                // Output-side guardrails — scan every assistant message
                // emitted by the upstream and either splice the redacted
                // text back into the body or 403 the response on a Block
                // verdict. Runs AFTER upstream call (tokens are already
                // burned) but BEFORE the body reaches the client. Each
                // `choices[*].message.content` is checked independently;
                // a Block on any choice aborts the entire response (we
                // don't ship a partial answer with one redacted choice).
                if guardrail_block.is_none() {
                    let choices_count = resp_body
                        .get("choices")
                        .and_then(|c| c.as_array())
                        .map(|a| a.len())
                        .unwrap_or(0);
                    'guard: for i in 0..choices_count {
                        let content_text = resp_body
                            .pointer(&format!("/choices/{i}/message/content"))
                            .and_then(|v| v.as_str())
                            .map(|s| s.to_string());
                        let Some(content) = content_text else {
                            // null content (e.g. tool-call only message) — nothing to scan
                            continue;
                        };
                        if content.is_empty() {
                            continue;
                        }
                        match llm_preflight::apply_output_guardrails(
                            &state,
                            &tenant_id,
                            &RefusalScope::new(
                                &auth_ctx,
                                &headers,
                                &correlation_id,
                                "/chat/completions",
                                &run_attr,
                            )
                            .started(start),
                            &content,
                            // Per-mode output surface — MUST match a
                            // `surface_enabled` arm (chat_output), else the
                            // config-level gate returns false and EVERY output
                            // check (PII redact, banned words, toxic, secrets)
                            // is silently skipped. `"llm"` has no arm.
                            crate::constants::guardrail_surface::CHAT_OUTPUT,
                            None,
                        )
                        .await
                        {
                            Err(resp) => {
                                guardrail_block = Some(resp);
                                break 'guard;
                            }
                            Ok(llm_preflight::OutputCheckResult::Pass) => {}
                            Ok(llm_preflight::OutputCheckResult::Redacted {
                                content: new_text,
                                pii,
                            }) => {
                                if let Some(content_slot) =
                                    resp_body.pointer_mut(&format!("/choices/{i}/message/content"))
                                {
                                    *content_slot = Value::String(new_text);
                                }
                                // PII redacted in the output → stamp the success
                                // row so GDPR/HIPAA tagging fires.
                                if pii {
                                    proxy_logger::apply_pii_detected(&mut log);
                                }
                            }
                        }
                    }
                }

                // Output checks cover the answer, not the model's reasoning, so
                // reasoning goes no further where a policy can block or redact.
                if guardrail_block.is_none() {
                    if let Err(resp) = llm_preflight::withhold_unscanned_reasoning(
                        &state,
                        &tenant_id,
                        &RefusalScope::new(
                            &auth_ctx,
                            &headers,
                            &correlation_id,
                            "/chat/completions",
                            &run_attr,
                        )
                        .started(start),
                        &mut resp_body,
                    )
                    .await
                    {
                        guardrail_block = Some(resp);
                    }
                }

                // On a guardrail block, `apply_output_guardrails` /
                // `apply_response_governance` already wrote the authoritative
                // block audit row (with the compliance stamps — soc2.cc7.2,
                // eu_ai_act, PII). Return before the deferred write so we don't
                // emit a SECOND row for the same blocked request.
                if let Some(resp) = guardrail_block {
                    return resp;
                }

                // Attach the parameter-governance envelope (additive top-level
                // `brutor_governance`) so the caller can SEE what was clamped /
                // injected. No-op when nothing changed. Done before the audit
                // preview is taken so the logged body matches what the client gets.
                crate::services::llm_governance::attach_governance_envelope(
                    &mut resp_body,
                    governance_envelope.clone(),
                );

                // Store the response we are actually returning.
                //
                // Past the block check, so a refused completion is never
                // cached, and after the redaction loop, so what lands in Redis
                // and Qdrant is the post-redaction body. The lookup path
                // returns cached bodies without a guardrail pass, which is
                // exactly why what goes in has to be already clean.
                if let Some(plan) = cache_store_plan.take() {
                    plan.commit(resp_body.clone());
                }

                // Not blocked — write the deferred audit row. Any redactions are
                // reflected in the preview so the log matches what the client
                // received.
                let final_body_str = serde_json::to_string(&resp_body).unwrap_or_default();
                log.response_size_bytes = Some(final_body_str.len() as i32);
                log.response_body_preview = Some(body_preview(&final_body_str));
                state
                    .background_writer
                    .write_proxy_log(state.db.clone(), log);

                // Build response with custom headers
                let status = StatusCode::from_u16(status_code).unwrap_or(StatusCode::OK);
                let mut resp = (status, Json(resp_body)).into_response();
                resp.headers_mut().insert(
                    crate::constants::headers::X_CORRELATION_ID,
                    correlation_id.parse().unwrap(),
                );
                // The run this response belongs to, so a client can attribute
                // feedback to the task it just saw rather than the server having
                // to infer it from timing. Counterpart of the inbound header.
                if let Ok(v) = run_id_header.parse() {
                    resp.headers_mut()
                        .insert(crate::constants::headers::X_BRUTOR_RUN_ID, v);
                }
                resp.headers_mut().insert(
                    crate::constants::headers::X_AUTH_TYPE,
                    auth_ctx.auth_type.to_string().parse().unwrap(),
                );

                if used_fallback {
                    if let Ok(val) = used_model.model_name.parse() {
                        resp.headers_mut().insert("x-fallback-model", val);
                    }
                }

                // Add usage warning headers
                for (k, v) in &enforcement.warning_headers {
                    if let (Ok(name), Ok(val)) = (
                        k.parse::<axum::http::header::HeaderName>(),
                        v.parse::<axum::http::header::HeaderValue>(),
                    ) {
                        resp.headers_mut().insert(name, val);
                    }
                }

                resp
            }
            Err(e) => {
                let duration = start.elapsed().as_millis() as f64;

                // Live health status update on failure
                state.mcp_health.record_llm_failure(
                    state.db.clone(),
                    &tenant_id,
                    &model.id,
                    &e.to_string(),
                );

                let target_url = providers::build_url(&model, "/chat/completions", false);
                let request_body_str = serde_json::to_string(&request).unwrap_or_default();
                // A gateway routing limit is a refusal (429, sealed `denied`);
                // anything else is a downstream failure (502).
                let limit_subtype = llm_refusal::router_limit_subtype(&e);
                let row_status: u16 = if limit_subtype.is_some() { 429 } else { 502 };
                let mut log = proxy_logger::build_proxy_log(
                    "llm",
                    &auth_ctx,
                    "POST",
                    "/chat/completions",
                    Some(&target_url),
                    Some(&model.id),
                    row_status,
                    duration,
                    false,
                    Some(&e.to_string()),
                    &correlation_id,
                );
                stamp_judge_self_call(&mut log, is_judge_self_call);
                llm_helpers::populate_group_id(&mut log, &target_group);
                log.proxy_subtype = limit_subtype;
                log.user_agent = extract_user_agent(&headers);
                log.request_size_bytes = Some(request_body_str.len() as i32);
                log.request_body_preview = Some(body_preview(&request_body_str));
                log.llm_model_requested = request.model.clone();
                log.llm_model_used = Some(model.model_name.clone());
                log.llm_provider = Some(model.provider.clone());
                log.llm_streaming = Some(false);
                populate_llm_request_fields(
                    &mut log,
                    &serde_json::to_value(&request).unwrap_or_default(),
                );
                proxy_logger::apply_call_chain(
                    &mut log,
                    &call_chain,
                    &current_task_id_str,
                    trace_continuity,
                    trace_root_kind,
                );
                match &residency_decision {
                    Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
                    None => proxy_logger::apply_residency_not_evaluated(
                        &mut log,
                        provider_region_for_audit.as_deref(),
                    ),
                }
                state
                    .background_writer
                    .write_proxy_log(state.db.clone(), log);

                proxy_metrics::record_request("llm", "POST", row_status, &model.provider);
                proxy_metrics::record_latency("llm", "POST", &model.provider, duration / 1000.0);

                router_error_response(&state, &tenant_id, &model, &e)
            }
        }
    }
}

#[utoipa::path(
    post,
    path = "/v1/proxy/llm/responses",
    tag = "LLM",
    summary = "Responses API (structured output)",
    request_body = ResponsesRequest,
    responses(
        (status = 200, description = "Structured response", body = Value),
        (status = 401, description = "Authentication required"),
    ),
    security(("api_key" = []), ("bearer" = []))
)]
pub(crate) async fn responses(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Json(mut request): Json<ResponsesRequest>,
) -> Response {
    let tenant_id = match route_helpers::extract_tenant_id(tenant) {
        Ok(id) => id,
        Err(resp) => return resp,
    };
    let correlation_id = route_helpers::extract_correlation_id(&headers);
    // Probe traffic skips metric writes — see chat_completions doc.
    // Request-entry clock, for gates that refuse before dispatch. The `start`
    // further down times the upstream call itself and feeds `record_latency`;
    // widening that one to cover preflight would quietly change what the
    // latency dashboards mean.
    let request_start = Instant::now();

    // Establish the call chain at request entry,
    // same pattern as chat_completions. See chat_completions for the
    // detailed rationale; identical contract here.
    let tenant_key = derive_tenant_key(state.config.encryption_key.as_bytes(), &tenant_id);
    // Resolve the conversation this request belongs to, if the client
    // asserted one. Derived here rather than in the middleware because the
    // HMAC needs the tenant key, which only exists after auth.
    if let Some(sess) = crate::services::a2a_delegation::resolve_asserted_session(
        |h| {
            headers
                .get(h)
                .and_then(|v| v.to_str().ok())
                .map(str::to_string)
        },
        &tenant_key,
    ) {
        crate::middleware::run_end::set_resolved_session(sess.to_string());
    }
    let (call_chain, current_task_id, trace_continuity) =
        parse_or_mint(|n| header_value(&headers, n), &tenant_key);
    let current_task_id_str = current_task_id.to_string();

    // Preflight: auth → model resolution → access check → quota enforcement
    let pf_body = serde_json::to_string(&request).unwrap_or_default();
    // Where this call sits in its run — the run the pre-flight run controls
    // check, and the run every refusal or block row of this request lands on.
    let run_attr =
        llm_refusal::RunAttribution::chain(&call_chain, &current_task_id_str, trace_continuity);
    let pf = match llm_preflight::run_preflight(
        &state,
        &headers,
        &tenant_id,
        &correlation_id,
        request.model.as_deref(),
        request.model_id.as_deref(),
        // Arm the run-scoped controls (run caps + abort) — every governed
        // LLM surface decides them in preflight, for agent and non-agent
        // callers alike. This surface used to run neither.
        &PreflightOptions::quota_only(run_attr.clone()),
        error_response,
        client_addr.ip(),
        None,
        Some(&pf_body),
    )
    .await
    {
        Ok(pf) => pf,
        Err(resp) => return resp,
    };
    let llm_preflight::PreflightResult {
        auth_ctx,
        model,
        target_group,
        out_ceiling,
        ..
    } = pf;

    let trace_root_kind = root_kind_for_auth(&auth_ctx.auth_type, call_chain.depth == 0);

    // Data residency check. Same shape as
    // chat_completions; see that handler for the semantics.
    // Data residency: one shared
    // implementation, see `llm_preflight::enforce_residency`. This used to be
    // ~55 lines copied here and at every other LLM surface, which is how the
    // sticky prompt-cache pin and the Gemini bridge each
    // came to skip the check independently — there was no single place to add
    // one.
    let residency = match llm_preflight::enforce_residency(
        &state,
        &auth_ctx,
        &tenant_id,
        &model,
        request.model.clone(),
        "/v1/responses",
        "Responses",
        &headers,
        &correlation_id,
        request_start,
        &call_chain,
        &current_task_id_str,
        trace_continuity,
        trace_root_kind,
        error_response,
    )
    .await
    {
        Ok(o) => o,
        Err(resp) => return resp,
    };
    let provider_region_for_audit = residency.provider_region;
    let residency_decision = residency.decision;

    // The actor planes (agent authorization, the AI System autonomy gate,
    // semantic policy on the input), as on `/chat/completions`. This route
    // takes the same keys and reaches the same models and ran none of them:
    // a default-deny agent with no grant and a key
    // of a suspended AI System were refused on chat and served here.
    if let Err(resp) = llm_preflight::enforce_actor_planes(
        &state,
        &auth_ctx,
        &tenant_id,
        &model,
        target_group.as_ref(),
        request.model.clone(),
        &llm_preflight::flatten_input_text(&request.input),
        "/v1/responses",
        &headers,
        &correlation_id,
        request_start,
        &call_chain,
        &current_task_id_str,
        trace_continuity,
        trace_root_kind,
        error_response,
    )
    .await
    {
        return resp;
    }

    tracing::debug!(
        model_name = %model.model_name,
        provider = %model.provider,
        "Responses API request"
    );

    // Input guardrails — scan the user-supplied prompt before
    // forwarding upstream. Catches PII / prompt-injection / banned
    // content per the tenant's guardrail configs. Same shape the
    // chat-completions handler uses; differs only in how we pull
    // the text out of the request (Responses API takes
    // `input: string | array<turn>`).
    let input_text = llm_preflight::flatten_input_text(&request.input);
    // Tracks whether an input guardrail redacted a PII category — stamped
    // on this request's success audit row(s) below.
    let mut input_pii_redacted = false;
    if !input_text.is_empty() {
        // Per-mode surface dispatch. The Responses API is the
        // structured-output sibling of chat completions; same policy.
        let guard_target =
            llm_preflight::LlmAuditTarget::from_model(&model, request.model.clone(), "/responses");
        match llm_preflight::apply_guardrails_with_redaction(
            &state,
            &tenant_id,
            &RefusalScope::new(
                &auth_ctx,
                &headers,
                &correlation_id,
                "/responses",
                &run_attr,
            )
            .started(request_start),
            &input_text,
            crate::constants::guardrail_surface::CHAT_INPUT,
            None,
            Some(&guard_target),
        )
        .await
        {
            Err(resp) => return resp,
            Ok(Some(redaction)) => {
                llm_preflight::apply_redactions_to_flattened_input(
                    &mut request.input,
                    &redaction.content,
                );
                input_pii_redacted = redaction.pii;
            }
            Ok(None) => {}
        }
    }

    // RG governance enforcement (temperature/max_output_tokens caps,
    // tool filtering, banned-pattern, mandatory system-prompt injection)
    // at PARITY with chat_completions. `apply_governance` operates on the
    // chat-completion shape, so we build a chat *view* of the responses
    // request, run the unchanged helper over it (a `reject` cap or a
    // banned pattern 403s here, before the upstream call), then splice
    // the governed params + injected `instructions` back into the native
    // ResponsesRequest. The returned param caps are intentionally
    // discarded — the responses path doesn't run the chat path's
    // EFFECTIVE_PARAM_CAPS model-default binding (it has no equivalent
    // default-injection step), and the caps have already been applied to
    // the request via the view splice-back.
    {
        let mut gov_view = responses_request_to_chat_view(&request);
        match llm_preflight::apply_governance(
            &state,
            &target_group,
            &model.id,
            &mut gov_view,
            error_response,
            &RefusalScope {
                auth_ctx: &auth_ctx,
                headers: &headers,
                correlation_id: &correlation_id,
                endpoint: "/responses",
                started: Some(request_start),
                model: Some((&model, None)),
                group_id: None,
                run: Some(&llm_refusal::RunAttribution::chain(
                    &call_chain,
                    &current_task_id_str,
                    trace_continuity,
                )),
                request_body: None,
            },
        )
        .await
        {
            Ok(_caps) => apply_responses_chat_view_back(&mut request, &gov_view),
            Err(resp) => return resp,
        }
    }

    // The response cache's view of this request, with the
    // headers its self-calls forward (see `chat_completions`).
    let self_call_headers = state.response_cache.makes_self_calls().then(|| {
        llm_helpers::self_call_headers(&headers, &call_chain, current_task_id, &tenant_key)
    });
    let cache_session = state
        .response_cache
        .session(
            &state,
            &tenant_id,
            target_group.as_ref(),
            self_call_headers.as_ref().unwrap_or(&headers),
        )
        .await;
    let request_body_for_cache = serde_json::to_value(&request).unwrap_or_default();
    let cache_key = CacheKey {
        tenant_id: &tenant_id,
        group_id: target_group.as_ref().map(|g| g.id.as_str()),
        model_name: &model.model_name,
        body: CacheBody::Value(&request_body_for_cache),
    };
    if let Some(result) = cache_session.lookup(&cache_key).await {
        // Audit row for the hit — same evidence the chat path
        // records (layer, tokens saved, cost avoided, previews).
        // Before this the /responses short-circuit wrote no row
        // at all, so a cached answer left no trace in proxy_logs.
        let mut log = llm_helpers::build_semantic_cache_hit_log(
            "/responses",
            crate::constants::proxy_subtype::RESPONSES,
            &auth_ctx,
            &target_group,
            &headers,
            &model,
            request.model.clone(),
            &request_body_for_cache,
            &result,
            request.stream.unwrap_or(false),
            request_start.elapsed().as_millis() as f64,
            &correlation_id,
        );
        proxy_logger::apply_call_chain(
            &mut log,
            &call_chain,
            &current_task_id_str,
            trace_continuity,
            trace_root_kind,
        );
        match &residency_decision {
            Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
            None => proxy_logger::apply_residency_not_evaluated(
                &mut log,
                provider_region_for_audit.as_deref(),
            ),
        }
        if input_pii_redacted {
            proxy_logger::apply_pii_detected(&mut log);
        }
        state
            .background_writer
            .write_proxy_log(state.db.clone(), log);

        let mut response_body = result.response.clone();
        if let Some(obj) = response_body.as_object_mut() {
            obj.insert(
                "brutor_cache".into(),
                json!({
                    "hit": true,
                    "layer": result.layer.as_str(),
                    "similarity_score": result.similarity_score,
                }),
            );
        }
        let mut resp = Json(response_body).into_response();
        resp.headers_mut()
            .insert("X-Cache-Hit", result.layer.as_str().parse().unwrap());
        resp.headers_mut().insert(
            crate::constants::headers::X_CORRELATION_ID,
            correlation_id.parse().unwrap(),
        );
        // The run this response belongs to, so a client can attribute
        // feedback to the task it just saw rather than the server having
        // to infer it from timing. Counterpart of the inbound header.
        if let Ok(v) = call_chain.root_task_id.to_string().parse() {
            resp.headers_mut()
                .insert(crate::constants::headers::X_BRUTOR_RUN_ID, v);
        }
        return resp;
    }

    let use_responses_api = provider_uses_responses_api(&model.provider);
    let endpoint = if use_responses_api {
        "/responses"
    } else {
        "/chat/completions"
    };
    let url = providers::build_url(&model, endpoint, false);
    let auth_headers = providers::build_headers(&model);

    let mut body = serde_json::to_value(&request).unwrap_or_default();
    if use_responses_api {
        providers::prepare_responses(&mut body, &model);
    } else {
        // Convert Responses API format → chat completion format for non-OpenAI providers
        convert_responses_to_chat(&mut body);
        // Same note as chat completions: unreadable Claude thinking where
        // an output check applies.
        if let Some(obj) = body.as_object_mut()
            && let Err(resp) = llm_preflight::note_reasoning_to_omit(
                &state,
                &tenant_id,
                &RefusalScope::new(
                    &auth_ctx,
                    &headers,
                    &correlation_id,
                    "/responses",
                    &run_attr,
                )
                .started(request_start),
                &model.model_name,
                obj,
            )
            .await
        {
            return resp;
        }
        providers::prepare_request(&mut body, &model);
    }
    // The output this call was admitted for (every token and spend cap's
    // room): bound the provider body to it, in the spelling of the API it
    // goes to — the Responses API takes only `max_output_tokens`.
    if let Some(ceiling) = out_ceiling {
        if use_responses_api {
            crate::services::llm_governance::apply_output_ceiling_to_responses(&mut body, ceiling);
        } else {
            providers::apply_output_ceiling_for_model(&mut body, &model, ceiling);
        }
    }

    // Streaming branch — pipe upstream SSE bytes through to the
    // client so the portal (or any AI Agent calling the proxy)
    // gets native OpenAI Responses-API events
    // (`response.output_text.delta`, `response.completed`, etc.).
    // We don't translate to chat-completion SSE — the API is
    // exposed *as the Responses API*; clients pick the endpoint
    // they want. Streaming is only supported when the upstream
    // provider speaks Responses (OpenAI / Azure today). For the
    // non-Responses providers we already convert the body shape
    // and call /chat/completions upstream — streaming through
    // that path would require SSE-format translation we
    // explicitly chose not to do, so we fall through to the
    // single-shot JSON path below for those.
    let stream_requested = body
        .get("stream")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    if stream_requested && use_responses_api {
        return responses_streaming(
            state,
            auth_ctx,
            target_group,
            model,
            request,
            &headers,
            url,
            auth_headers,
            body,
            correlation_id,
            tenant_id,
            request_body_for_cache,
            Arc::clone(&cache_session),
            // Thread the call chain through so the
            // streaming handler's audit rows join the same trace as
            // this entry handler's. Without this, the trace would
            // fragment at the SSE boundary.
            call_chain.clone(),
            current_task_id_str.clone(),
            trace_continuity,
            trace_root_kind,
            // Same idea for residency: thread the
            // already-evaluated decision so the streaming audit rows
            // stamp the same outcome.
            residency_decision.clone(),
            provider_region_for_audit.clone(),
            input_pii_redacted,
        )
        .await;
    }
    if stream_requested && !use_responses_api {
        // Non-OpenAI providers on /responses with stream=true:
        // the upstream is chat-completions and would emit chat-
        // completion SSE, but the client is asking for Responses
        // SSE. Rather than silently swap formats, refuse so the
        // client makes an explicit choice. They can either drop
        // `stream` or hit /chat/completions directly.
        if let Some(obj) = body.as_object_mut() {
            obj.insert("stream".into(), serde_json::Value::Bool(false));
        }
    }

    let mut req = state
        .http_client
        .post(&url)
        .header("Content-Type", "application/json");
    for (k, v) in &auth_headers {
        req = req.header(k.as_str(), v.as_str());
    }

    let start = Instant::now();
    let request_body_str = serde_json::to_string(&body).unwrap_or_default();

    match req.json(&body).send().await {
        Ok(resp) => {
            let status = resp.status().as_u16();
            crate::middleware::run_end::record_upstream_response(status, resp.headers());
            let duration = start.elapsed().as_millis() as f64;
            let mut resp_body: Value = crate::services::providers::upstream_json(resp).await;
            let success = (200..300).contains(&status);

            let mut log = proxy_logger::build_proxy_log(
                "llm",
                &auth_ctx,
                "POST",
                endpoint,
                Some(&url),
                Some(&model.id),
                status,
                duration,
                success,
                None,
                &correlation_id,
            );
            log.proxy_subtype = Some(crate::constants::proxy_subtype::RESPONSES.to_string());
            llm_helpers::populate_group_id(&mut log, &target_group);
            log.user_agent = extract_user_agent(&headers);
            log.request_size_bytes = Some(request_body_str.len() as i32);
            log.request_body_preview = Some(body_preview(&request_body_str));
            log.llm_model_requested = request.model.clone();
            log.llm_model_used = Some(model.model_name.clone());
            log.llm_provider = Some(model.provider.clone());
            log.llm_streaming = Some(false);
            if input_pii_redacted {
                proxy_logger::apply_pii_detected(&mut log);
            }
            proxy_logger::apply_call_chain(
                &mut log,
                &call_chain,
                &current_task_id_str,
                trace_continuity,
                trace_root_kind,
            );
            match &residency_decision {
                Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
                None => proxy_logger::apply_residency_not_evaluated(
                    &mut log,
                    provider_region_for_audit.as_deref(),
                ),
            }

            // Post-response governance + output guardrails at
            // PARITY with chat_completions. We CAPTURE a block rather than
            // returning immediately, so the deferred audit row records the
            // FINAL status (403). The block helper owns its own
            // authoritative audit row, so on a block we return BEFORE the
            // deferred success-log write to avoid double-logging.
            let mut guardrail_block: Option<Response> = None;

            if success {
                // Tool-call arg policies + banned-arg patterns +
                // tool-call rate counters. The Responses upstream emits
                // tool calls as `output[].type=="function_call"`; the
                // governance readers expect the chat shape, so normalize
                // into a synthetic chat-shaped view first. Non-Responses
                // upstreams already return chat shape (`convert_responses`
                // wasn't applied to the response) — pass the raw body.
                let gov_body = responses_output_to_chat_tool_view(&resp_body);
                let gov_target = gov_body.as_ref().unwrap_or(&resp_body);
                if let Err(resp) = llm_preflight::apply_response_governance(
                    &state,
                    &target_group,
                    &model.id,
                    gov_target,
                    error_response,
                    &RefusalScope::new(
                        &auth_ctx,
                        &headers,
                        &correlation_id,
                        "/responses",
                        &run_attr,
                    )
                    .started(request_start),
                )
                .await
                {
                    guardrail_block = Some(resp);
                }
            }

            // Output guardrails on the assistant text. Mirrors the
            // chat non-stream path: scan each emitted text leaf on the
            // `chat_output` surface, splice redactions back into the
            // response JSON, 403 on a Block. Handles both shapes — the
            // Responses-native `output[].content[].text` and the chat
            // shape (`choices[].message.content`) for non-Responses
            // upstreams.
            if success && guardrail_block.is_none() {
                // Responses-native shape.
                let leaves = extract_responses_output_text(&resp_body);
                if !leaves.is_empty() {
                    'guard_responses: for (i, j, content) in leaves {
                        match llm_preflight::apply_output_guardrails(
                            &state,
                            &tenant_id,
                            &RefusalScope::new(
                                &auth_ctx,
                                &headers,
                                &correlation_id,
                                "/responses",
                                &run_attr,
                            )
                            .started(request_start),
                            &content,
                            crate::constants::guardrail_surface::CHAT_OUTPUT,
                            None,
                        )
                        .await
                        {
                            Err(resp) => {
                                guardrail_block = Some(resp);
                                break 'guard_responses;
                            }
                            Ok(llm_preflight::OutputCheckResult::Pass) => {}
                            Ok(llm_preflight::OutputCheckResult::Redacted {
                                content: new_text,
                                pii,
                            }) => {
                                splice_responses_output_text(&mut resp_body, i, j, new_text);
                                if pii {
                                    proxy_logger::apply_pii_detected(&mut log);
                                }
                            }
                        }
                    }
                } else {
                    // Chat shape (non-Responses upstream) — scan
                    // `choices[].message.content`, same as chat path.
                    let choices_count = resp_body
                        .get("choices")
                        .and_then(|c| c.as_array())
                        .map(|a| a.len())
                        .unwrap_or(0);
                    'guard_chat: for i in 0..choices_count {
                        let content_text = resp_body
                            .pointer(&format!("/choices/{i}/message/content"))
                            .and_then(|v| v.as_str())
                            .map(|s| s.to_string());
                        let Some(content) = content_text else {
                            continue;
                        };
                        if content.is_empty() {
                            continue;
                        }
                        match llm_preflight::apply_output_guardrails(
                            &state,
                            &tenant_id,
                            &RefusalScope::new(
                                &auth_ctx,
                                &headers,
                                &correlation_id,
                                "/responses",
                                &run_attr,
                            )
                            .started(request_start),
                            &content,
                            crate::constants::guardrail_surface::CHAT_OUTPUT,
                            None,
                        )
                        .await
                        {
                            Err(resp) => {
                                guardrail_block = Some(resp);
                                break 'guard_chat;
                            }
                            Ok(llm_preflight::OutputCheckResult::Pass) => {}
                            Ok(llm_preflight::OutputCheckResult::Redacted {
                                content: new_text,
                                pii,
                            }) => {
                                if let Some(slot) =
                                    resp_body.pointer_mut(&format!("/choices/{i}/message/content"))
                                {
                                    *slot = Value::String(new_text);
                                }
                                if pii {
                                    proxy_logger::apply_pii_detected(&mut log);
                                }
                            }
                        }
                    }
                }
            }

            // Output checks cover the answer, not the reasoning summary, so
            // reasoning goes no further where a policy can block or redact.
            // Before the log and the semantic-cache store, which both read
            // this body.
            if success && guardrail_block.is_none() {
                if let Err(resp) = llm_preflight::withhold_unscanned_reasoning(
                    &state,
                    &tenant_id,
                    &RefusalScope::new(
                        &auth_ctx,
                        &headers,
                        &correlation_id,
                        "/responses",
                        &run_attr,
                    )
                    .started(request_start),
                    &mut resp_body,
                )
                .await
                {
                    guardrail_block = Some(resp);
                }
            }

            // On a block, the helper already wrote the authoritative 403
            // audit row — return before the deferred success-log write so
            // we emit exactly ONE row for the blocked request.
            if let Some(resp) = guardrail_block {
                return resp;
            }

            // No block — finalize the deferred audit row. response body /
            // preview reflect any spliced redactions so the log matches
            // what the client receives.
            let response_body_str = serde_json::to_string(&resp_body).unwrap_or_default();
            log.response_size_bytes = Some(response_body_str.len() as i32);
            log.response_body_preview = Some(body_preview(&response_body_str));

            // Usage + cost on the audit row, BEFORE it is written. Until
            // 0.10.94 the row went to the writer here with no token or
            // cost fields, and the usage was only read further down for
            // `usage_logs` — so the budget counter could refuse a run
            // ("Daily budget exhausted") whose ledger, assurance cost
            // signals and Mission Control run cost all said $0. One
            // parse feeds both ledgers so they cannot disagree.
            let usage = llm_helpers::ResponsesUsage::from_body(&resp_body);
            let estimated_cost = llm_helpers::populate_llm_usage_fields(&mut log, &usage, &model);
            populate_llm_request_fields(&mut log, &body);
            let pc_mechanism = crate::services::prompt_cache_usage::mechanism_for_provider(
                &model.provider,
                &model.model_name,
            );
            let pc_usage = crate::services::prompt_cache_usage::extract_usage_from_response(
                &resp_body,
                pc_mechanism,
                &model,
            );
            llm_helpers::apply_prompt_cache_usage(
                &mut log,
                &pc_usage,
                pc_mechanism,
                &model.provider,
            );
            state
                .background_writer
                .write_proxy_log(state.db.clone(), log);

            // Store in semantic cache (/responses endpoint).
            if success {
                let usage_obj = resp_body.get("usage");
                let total = usage_obj
                    .and_then(|u| u.get("total_tokens").or_else(|| u.get("prompt_tokens")))
                    .and_then(|v| v.as_i64())
                    .unwrap_or(0) as i32;
                // Past the guardrail block's return above, so a refused
                // answer never reaches this; `resp_body` carries the
                // redactions.
                if let Some(plan) = cache_session.plan_store(
                    &CacheKey {
                        tenant_id: &tenant_id,
                        group_id: target_group.as_ref().map(|g| g.id.as_str()),
                        model_name: &model.model_name,
                        body: CacheBody::Value(&request_body_for_cache),
                    },
                    StoreFacts {
                        invoked_tools: llm_helpers::extract_responses_tool_calls(&resp_body),
                        token_count: total,
                    },
                ) {
                    plan.commit(resp_body.clone());
                }
            }

            // Usage log for responses — the same token counts and cost
            // estimate that went on the proxy_logs row above.
            if success {
                let (prompt_tokens, completion_tokens) = (usage.input_tokens, usage.output_tokens);
                // A probe records its real usage (billed to Unbound).
                usage::write_llm_usage_log(
                    &state.usage_state,
                    &state.background_writer,
                    &state.model_metrics,
                    state.db.clone(),
                    &tenant_id,
                    &auth_ctx,
                    target_group.as_ref(),
                    &model.id,
                    &model.model_name,
                    &model.provider,
                    prompt_tokens,
                    completion_tokens,
                    estimated_cost,
                    model.cost_per_input_token.unwrap_or(0.0),
                    model.cost_per_output_token.unwrap_or(0.0),
                    &url,
                    &correlation_id,
                    status,
                    duration,
                );

                // proxy_metrics row — Mission Control's
                // `get_performance_metrics` reads from `proxy_metrics`,
                // so without this write every successful /responses
                // call (used by reasoning models like o1 / o3 and
                // every Responses-API client) is invisible to the
                // dashboard while resource-group usage (which reads
                // `usage_logs`) sees it.
                let llm_metric_data = serde_json::json!({
                    "provider": model.provider,
                    "model_requested": request.model,
                    "model_used": model.model_name,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens.zip(completion_tokens).map(|(p, c)| p + c),
                    "estimated_cost_usd": estimated_cost,
                    "streaming": false,
                });
                let metric = proxy_logger::build_proxy_metric(
                    "llm",
                    &auth_ctx,
                    "POST",
                    "/responses",
                    "responses",
                    Some(&model.id),
                    status,
                    duration,
                    success,
                    &correlation_id,
                    Some(request_body_str.len() as i32),
                    Some(response_body_str.len() as i32),
                    None,
                    None,
                    extract_user_agent(&headers).as_deref(),
                    Some(llm_metric_data),
                    None,
                );
                state
                    .background_writer
                    .write_proxy_metric(state.db.clone(), metric);
                // A probe records its real usage (billed to Unbound);
                // only the metric is left to the central probe recorder.

                if let Some(ref g) = target_group {
                    usage::spawn_alert_evaluation(
                        state.db.clone(),
                        tenant_id.clone(),
                        g.clone(),
                        "llm".into(),
                        Some(correlation_id.clone()),
                    );
                }
            }

            proxy_metrics::record_request("llm", "POST", status, &model.provider);
            proxy_metrics::record_latency("llm", "POST", &model.provider, duration / 1000.0);

            let axum_status = StatusCode::from_u16(status).unwrap_or(StatusCode::OK);
            let mut response = (axum_status, Json(resp_body)).into_response();
            response.headers_mut().insert(
                crate::constants::headers::X_CORRELATION_ID,
                correlation_id.parse().unwrap(),
            );
            // The run this response belongs to, so a client can attribute
            // feedback to the task it just saw rather than the server having
            // to infer it from timing. Counterpart of the inbound header.
            if let Ok(v) = call_chain.root_task_id.to_string().parse() {
                response
                    .headers_mut()
                    .insert(crate::constants::headers::X_BRUTOR_RUN_ID, v);
            }
            response
        }
        Err(e) => {
            let duration = start.elapsed().as_millis() as f64;
            let mut log = proxy_logger::build_proxy_log(
                "llm",
                &auth_ctx,
                "POST",
                endpoint,
                Some(&url),
                Some(&model.id),
                502,
                duration,
                false,
                Some(&e.to_string()),
                &correlation_id,
            );
            log.proxy_subtype = Some(crate::constants::proxy_subtype::RESPONSES.to_string());
            llm_helpers::populate_group_id(&mut log, &target_group);
            log.user_agent = extract_user_agent(&headers);
            log.request_size_bytes = Some(request_body_str.len() as i32);
            log.request_body_preview = Some(body_preview(&request_body_str));
            log.llm_model_requested = request.model.clone();
            log.llm_model_used = Some(model.model_name.clone());
            log.llm_provider = Some(model.provider.clone());
            proxy_logger::apply_call_chain(
                &mut log,
                &call_chain,
                &current_task_id_str,
                trace_continuity,
                trace_root_kind,
            );
            match &residency_decision {
                Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
                None => proxy_logger::apply_residency_not_evaluated(
                    &mut log,
                    provider_region_for_audit.as_deref(),
                ),
            }
            state
                .background_writer
                .write_proxy_log(state.db.clone(), log);

            proxy_metrics::record_request("llm", "POST", 502, &model.provider);
            proxy_metrics::record_latency("llm", "POST", &model.provider, duration / 1000.0);

            reqwest_error_response(&state, &tenant_id, &model, &e)
        }
    }
}

#[utoipa::path(
    post,
    path = "/v1/proxy/llm/completions",
    tag = "LLM",
    summary = "Text completions (legacy)",
    request_body = CompletionRequest,
    responses(
        (status = 200, description = "Completion response", body = Value),
        (status = 401, description = "Authentication required"),
    ),
    security(("api_key" = []), ("bearer" = []))
)]
pub(crate) async fn completions(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Json(mut request): Json<CompletionRequest>,
) -> Response {
    let start = Instant::now();
    let tenant_id = match route_helpers::extract_tenant_id(tenant) {
        Ok(id) => id,
        Err(resp) => return resp,
    };
    let correlation_id = route_helpers::extract_correlation_id(&headers);
    // Probe traffic skips metric writes — see chat_completions doc.

    // Call chain established at request entry so
    // outbound calls this handler makes can carry the four
    // `x-brutor-delegation-*` headers. Bindings are `_`-prefixed here
    // because the legacy `/completions` route in its current shape
    // doesn't emit proxy_log rows (it delegates the per-call audit to
    // chat_completions internally); when outbound header emission
    // lands here we'll drop the underscores.
    let tenant_key = derive_tenant_key(state.config.encryption_key.as_bytes(), &tenant_id);
    // Resolve the conversation this request belongs to, if the client
    // asserted one. Derived here rather than in the middleware because the
    // HMAC needs the tenant key, which only exists after auth.
    if let Some(sess) = crate::services::a2a_delegation::resolve_asserted_session(
        |h| {
            headers
                .get(h)
                .and_then(|v| v.to_str().ok())
                .map(str::to_string)
        },
        &tenant_key,
    ) {
        crate::middleware::run_end::set_resolved_session(sess.to_string());
    }
    let (_call_chain, _current_task_id, _trace_continuity) =
        parse_or_mint(|n| header_value(&headers, n), &tenant_key);
    let run_id_header = _call_chain.root_task_id.to_string();

    // Preflight: auth → model resolution → access check → quota enforcement
    let pf_body = serde_json::to_string(&request).unwrap_or_default();
    // Where this call sits in its run — the run the pre-flight run controls
    // check, and the run every refusal or block row of this request lands on.
    let run_attr = llm_refusal::RunAttribution::chain(
        &_call_chain,
        &_current_task_id.to_string(),
        _trace_continuity,
    );
    let pf = match llm_preflight::run_preflight(
        &state,
        &headers,
        &tenant_id,
        &correlation_id,
        request.model.as_deref(),
        request.model_id.as_deref(),
        // Run-scoped controls (run caps + abort), as on every LLM surface.
        &PreflightOptions::with_guardrails(run_attr.clone()),
        error_response,
        client_addr.ip(),
        None,
        Some(&pf_body),
    )
    .await
    {
        Ok(pf) => pf,
        Err(resp) => return resp,
    };
    let llm_preflight::PreflightResult {
        auth_ctx,
        model,
        target_group,
        out_ceiling,
        ..
    } = pf;

    // Data residency, as on `/chat/completions`, `/responses` and
    // `/embeddings`: this route reaches the same
    // providers with the same keys and ran no residency check, so a tenant
    // whose profile forbids a provider's region had its prompt sent there by
    // changing endpoint. A block writes its one `residency_violation` row
    // and returns before anything is sent.
    if let Err(resp) = llm_preflight::enforce_residency(
        &state,
        &auth_ctx,
        &tenant_id,
        &model,
        request.model.clone(),
        "/v1/completions",
        "Completions",
        &headers,
        &correlation_id,
        start,
        &_call_chain,
        &_current_task_id.to_string(),
        _trace_continuity,
        root_kind_for_auth(&auth_ctx.auth_type, _call_chain.depth == 0),
        error_response,
    )
    .await
    {
        return resp;
    }

    // The actor planes, as on `/chat/completions`
    // (see `responses`).
    if let Err(resp) = llm_preflight::enforce_actor_planes(
        &state,
        &auth_ctx,
        &tenant_id,
        &model,
        target_group.as_ref(),
        request.model.clone(),
        &request.prompt,
        "/completions",
        &headers,
        &correlation_id,
        start,
        &_call_chain,
        &_current_task_id.to_string(),
        _trace_continuity,
        root_kind_for_auth(&auth_ctx.auth_type, _call_chain.depth == 0),
        error_response,
    )
    .await
    {
        return resp;
    }

    // Guardrails — check input. CompletionRequest carries a
    // single `prompt: String`, so the splice on Redacted is a
    // straight overwrite — no walk + join needed.
    //
    // Per-mode surface dispatch. Legacy `/completions` is the
    // pre-chat model class; we treat it as `chat_input` so it carries
    // the same policy.
    let guard_target =
        llm_preflight::LlmAuditTarget::from_model(&model, request.model.clone(), "/completions");
    match llm_preflight::apply_guardrails_with_redaction(
        &state,
        &tenant_id,
        &RefusalScope::new(
            &auth_ctx,
            &headers,
            &correlation_id,
            "/completions",
            &run_attr,
        )
        .started(start),
        &request.prompt,
        crate::constants::guardrail_surface::CHAT_INPUT,
        None,
        Some(&guard_target),
    )
    .await
    {
        Ok(Some(redaction)) => {
            // NOTE: the legacy /completions handler writes only a
            // proxy_metric + usage_log on success — it never emits a
            // proxy_logs row, so there is no audit row here to carry
            // `pii_detected`. `redaction.pii` is intentionally not
            // threaded; see the task report's "remaining gap" note.
            request.prompt = redaction.content;
        }
        Ok(None) => {}
        Err(resp) => return resp,
    }

    tracing::debug!(
        model_name = %model.model_name,
        provider = %model.provider,
        "Text completion request"
    );

    // RG governance enforcement (temperature / max_tokens caps +
    // banned-pattern) at PARITY with chat_completions. `apply_governance`
    // operates on the chat shape, so we run it over a chat *view* of the
    // prompt-only request (a `reject` cap or banned pattern 403s here,
    // before upstream) then copy the clamped params back. Mandatory
    // system-prompt injection is N/A on this prompt-only surface — see
    // `completion_request_to_chat_view`.
    {
        let mut gov_view = completion_request_to_chat_view(&request);
        match llm_preflight::apply_governance(
            &state,
            &target_group,
            &model.id,
            &mut gov_view,
            error_response,
            &RefusalScope {
                auth_ctx: &auth_ctx,
                headers: &headers,
                correlation_id: &correlation_id,
                endpoint: "/completions",
                started: Some(start),
                model: Some((&model, None)),
                group_id: None,
                run: Some(&llm_refusal::RunAttribution::chain(
                    &_call_chain,
                    &_current_task_id.to_string(),
                    _trace_continuity,
                )),
                request_body: None,
            },
        )
        .await
        {
            Ok(_caps) => {
                request.temperature = gov_view.temperature;
                request.max_tokens = gov_view.max_tokens;
            }
            Err(resp) => return resp,
        }
    }

    // The response cache's view of this request, with the
    // headers its self-calls forward (see `chat_completions`).
    let self_call_headers = state.response_cache.makes_self_calls().then(|| {
        llm_helpers::self_call_headers(&headers, &_call_chain, _current_task_id, &tenant_key)
    });
    let cache_session = state
        .response_cache
        .session(
            &state,
            &tenant_id,
            target_group.as_ref(),
            self_call_headers.as_ref().unwrap_or(&headers),
        )
        .await;
    let compl_body_for_cache = serde_json::to_value(&request).unwrap_or_default();
    let cache_key = CacheKey {
        tenant_id: &tenant_id,
        group_id: target_group.as_ref().map(|g| g.id.as_str()),
        model_name: &model.model_name,
        body: CacheBody::Value(&compl_body_for_cache),
    };
    if let Some(result) = cache_session.lookup(&cache_key).await {
        let mut response_body = result.response.clone();
        if let Some(obj) = response_body.as_object_mut() {
            obj.insert(
                "brutor_cache".into(),
                json!({
                    "hit": true,
                    "layer": result.layer.as_str(),
                    "similarity_score": result.similarity_score,
                }),
            );
        }
        let mut resp = Json(response_body).into_response();
        resp.headers_mut()
            .insert("X-Cache-Hit", result.layer.as_str().parse().unwrap());
        resp.headers_mut().insert(
            crate::constants::headers::X_CORRELATION_ID,
            correlation_id.parse().unwrap(),
        );
        // The run this response belongs to, so a client can attribute
        // feedback to the task it just saw rather than the server having
        // to infer it from timing. Counterpart of the inbound header.
        if let Ok(v) = run_id_header.parse() {
            resp.headers_mut()
                .insert(crate::constants::headers::X_BRUTOR_RUN_ID, v);
        }
        return resp;
    }

    let mut body = serde_json::to_value(&request).unwrap_or_default();
    let converted_to_chat = providers::prepare_text_completion(&mut body, &model);
    // The output this call was admitted for: bound the provider body to it,
    // in the provider's own spelling.
    if let Some(ceiling) = out_ceiling {
        providers::apply_output_ceiling_for_model(&mut body, &model, ceiling);
    }

    // If converted to chat format, use chat completions endpoint
    let endpoint = if converted_to_chat {
        "/chat/completions"
    } else {
        "/completions"
    };
    tracing::debug!(converted_to_chat, endpoint, "Text completion routing");
    let url = providers::build_url(&model, endpoint, false);
    let auth_headers = providers::build_headers(&model);

    let mut req = state
        .http_client
        .post(&url)
        .header("Content-Type", "application/json");
    for (k, v) in &auth_headers {
        req = req.header(k.as_str(), v.as_str());
    }

    match req.json(&body).send().await {
        Ok(resp) => {
            let status = resp.status().as_u16();
            crate::middleware::run_end::record_upstream_response(status, resp.headers());
            let duration = start.elapsed().as_millis() as f64;
            let resp_body: Value = crate::services::providers::upstream_json(resp).await;
            let success = (200..300).contains(&status);

            // If we converted to chat format, extract the text from the chat response
            let mut final_body = if converted_to_chat {
                convert_chat_response_to_completion(&resp_body)
            } else {
                resp_body
            };

            // Output guardrails on the completion text at PARITY
            // with chat_completions. Legacy completions carry the
            // assistant text in `choices[].text` (no messages, no tool
            // calls — so `apply_response_governance` is N/A here). Each
            // choice is scanned on the `chat_output` surface; a Redact
            // splices the masked text back, a Block 403s the response.
            // The helper owns the block audit row, so we return it
            // directly (this handler writes no proxy_log success row of
            // its own — only usage + metric — so there's no deferred row
            // to double-log). This handler never streams (it always
            // single-shots `resp.json()`), so there is no streaming G4
            // variant to wire.
            if success {
                let choices_count = final_body
                    .get("choices")
                    .and_then(|c| c.as_array())
                    .map(|a| a.len())
                    .unwrap_or(0);
                for i in 0..choices_count {
                    let text = final_body
                        .pointer(&format!("/choices/{i}/text"))
                        .and_then(|v| v.as_str())
                        .map(|s| s.to_string());
                    let Some(text) = text else { continue };
                    if text.is_empty() {
                        continue;
                    }
                    match llm_preflight::apply_output_guardrails(
                        &state,
                        &tenant_id,
                        &RefusalScope::new(
                            &auth_ctx,
                            &headers,
                            &correlation_id,
                            "/completions",
                            &run_attr,
                        )
                        .started(start),
                        &text,
                        crate::constants::guardrail_surface::CHAT_OUTPUT,
                        None,
                    )
                    .await
                    {
                        Err(resp) => return resp,
                        Ok(llm_preflight::OutputCheckResult::Pass) => {}
                        Ok(llm_preflight::OutputCheckResult::Redacted {
                            content: new_text,
                            ..
                        }) => {
                            if let Some(slot) =
                                final_body.pointer_mut(&format!("/choices/{i}/text"))
                            {
                                *slot = Value::String(new_text);
                            }
                        }
                    }
                }
            }

            // Store in semantic cache (/completions endpoint).
            // /completions technically doesn't surface tool calls, but
            // we still defer to `should_cache_tool_response` for the
            // (rare) cases where a provider adapter wraps a tool-capable
            // chat model behind /completions.
            if success {
                let finish_reason = final_body
                    .get("choices")
                    .and_then(|c| c.get(0))
                    .and_then(|c| c.get("finish_reason"))
                    .and_then(|fr| fr.as_str());
                let invoked_tools: Vec<(String, String)> =
                    if llm_helpers::finish_reason_is_tool_call(finish_reason) {
                        vec![(String::new(), String::new())]
                    } else {
                        Vec::new()
                    };
                let total = final_body
                    .get("usage")
                    .and_then(|u| u.get("total_tokens"))
                    .and_then(|v| v.as_i64())
                    .unwrap_or(0) as i32;
                if let Some(plan) = cache_session.plan_store(
                    &CacheKey {
                        tenant_id: &tenant_id,
                        group_id: target_group.as_ref().map(|g| g.id.as_str()),
                        model_name: &model.model_name,
                        body: CacheBody::Value(&compl_body_for_cache),
                    },
                    StoreFacts {
                        invoked_tools,
                        token_count: total,
                    },
                ) {
                    plan.commit(final_body.clone());
                }
            }

            // Usage log for completions
            if success {
                let (prompt_tokens, completion_tokens) =
                    llm_helpers::extract_token_counts(&final_body);
                let estimated_cost = llm_helpers::estimate_llm_cost(
                    prompt_tokens,
                    completion_tokens,
                    model.cost_per_input_token.unwrap_or(0.0),
                    model.cost_per_output_token.unwrap_or(0.0),
                );
                // A probe records its real usage (billed to Unbound).
                usage::write_llm_usage_log(
                    &state.usage_state,
                    &state.background_writer,
                    &state.model_metrics,
                    state.db.clone(),
                    &tenant_id,
                    &auth_ctx,
                    target_group.as_ref(),
                    &model.id,
                    &model.model_name,
                    &model.provider,
                    prompt_tokens,
                    completion_tokens,
                    estimated_cost,
                    model.cost_per_input_token.unwrap_or(0.0),
                    model.cost_per_output_token.unwrap_or(0.0),
                    &url,
                    &correlation_id,
                    status,
                    duration,
                );

                // proxy_metrics row — Mission Control reads
                // exclusively from `proxy_metrics`. Without this
                // write, every legacy /completions call (still used
                // by classic OpenAI completion clients and the
                // text-davinci-style adapters) is invisible to the
                // dashboard while resource-group usage sees it.
                let llm_metric_data = serde_json::json!({
                    "provider": model.provider,
                    "model_requested": request.model,
                    "model_used": model.model_name,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens.zip(completion_tokens).map(|(p, c)| p + c),
                    "estimated_cost_usd": estimated_cost,
                    "streaming": false,
                    "converted_to_chat": converted_to_chat,
                });
                let metric = proxy_logger::build_proxy_metric(
                    "llm",
                    &auth_ctx,
                    "POST",
                    "/completions",
                    "completions",
                    Some(&model.id),
                    status,
                    duration,
                    success,
                    &correlation_id,
                    None,
                    None,
                    None,
                    None,
                    extract_user_agent(&headers).as_deref(),
                    Some(llm_metric_data),
                    None,
                );
                state
                    .background_writer
                    .write_proxy_metric(state.db.clone(), metric);
                // A probe records its real usage (billed to Unbound);
                // only the metric is left to the central probe recorder.

                if let Some(ref g) = target_group {
                    usage::spawn_alert_evaluation(
                        state.db.clone(),
                        tenant_id.clone(),
                        g.clone(),
                        "llm".into(),
                        Some(correlation_id.clone()),
                    );
                }
            }

            proxy_metrics::record_request("llm", "POST", status, &model.provider);
            proxy_metrics::record_latency("llm", "POST", &model.provider, duration / 1000.0);

            let axum_status = StatusCode::from_u16(status).unwrap_or(StatusCode::OK);
            let mut response = (axum_status, Json(final_body)).into_response();
            response.headers_mut().insert(
                crate::constants::headers::X_CORRELATION_ID,
                correlation_id.parse().unwrap(),
            );
            // The run this response belongs to, so a client can attribute
            // feedback to the task it just saw rather than the server having
            // to infer it from timing. Counterpart of the inbound header.
            if let Ok(v) = run_id_header.parse() {
                response
                    .headers_mut()
                    .insert(crate::constants::headers::X_BRUTOR_RUN_ID, v);
            }
            response
        }
        Err(e) => {
            let duration = start.elapsed().as_millis() as f64;
            proxy_metrics::record_request("llm", "POST", 502, &model.provider);
            proxy_metrics::record_latency("llm", "POST", &model.provider, duration / 1000.0);
            reqwest_error_response(&state, &tenant_id, &model, &e)
        }
    }
}

/// Streaming branch of the `/v1/proxy/llm/responses` handler.
///
/// Pipes the upstream OpenAI Responses-API SSE stream straight
/// through to the client. The wire format on both sides is
/// identical (e.g. `event: response.output_text.delta`,
/// `data: {...}`) so this is an unmodified passthrough — clients
/// (portal, AI agents) decode native Responses-API events.
///
/// Why not translate to chat-completion SSE: the Responses API
/// surface is intentionally distinct (reasoning chains, tools
/// with state, structured output via `text_format`, response-id
/// continuations) — clients that adopt it should consume it
/// natively rather than through a lossy translation layer. The
/// chat-completion-only clients keep using `/chat/completions`.
///
/// Pre-conditions enforced by the caller (`responses` handler):
/// - `provider_uses_responses_api(model.provider)` is true
/// - request body has `stream: true`
/// - body has been transformed by `prepare_responses` already
#[allow(clippy::too_many_arguments)]
async fn responses_streaming(
    state: SharedState,
    auth_ctx: crate::models::auth::AuthContext,
    target_group: Option<crate::models::db::ResourceGroup>,
    model: crate::models::db::LlmModel,
    request: ResponsesRequest,
    headers: &HeaderMap,
    url: String,
    auth_headers: Vec<(String, String)>,
    body: Value,
    correlation_id: String,
    tenant_id: String,
    request_body_for_cache: Value,
    cache_session: Arc<dyn ResponseCacheSession>,
    // Chain inherited from the caller (`responses`).
    // See chat_completions for the parse-or-mint semantics; here the
    // chain is always pre-established because this fn is only reached
    // via `responses`, which has already done parse_or_mint at entry.
    call_chain: CallChain,
    current_task_id_str: String,
    trace_continuity: TraceContinuity,
    trace_root_kind: Option<&'static str>,
    // Residency decision inherited from
    // `responses`. The check already ran at entry; streaming path
    // just stamps each audit row with the same outcome.
    residency_decision: Option<crate::services::residency::ResidencyDecision>,
    provider_region_for_audit: Option<String>,
    // True when an input guardrail redacted a PII category from the
    // prompt — stamped on the streaming success audit row so the
    // compliance tagger derives has_pii/has_phi (GDPR/HIPAA).
    input_pii_redacted: bool,
) -> Response {
    let start = Instant::now();
    let request_body_str = serde_json::to_string(&body).unwrap_or_default();
    // Probe traffic skips metric writes — see chat_completions doc.
    let is_probe = crate::services::catalog_probe::is_health_probe_request(headers, &auth_ctx);

    let mut req = state
        .http_client
        .post(&url)
        .header("Content-Type", "application/json");
    for (k, v) in &auth_headers {
        req = req.header(k.as_str(), v.as_str());
    }

    let upstream_resp = match req.json(&body).send().await {
        Ok(r) => r,
        Err(e) => {
            let duration = start.elapsed().as_millis() as f64;
            proxy_metrics::record_request("llm", "POST", 502, &model.provider);
            proxy_metrics::record_latency("llm", "POST", &model.provider, duration / 1000.0);
            return reqwest_error_response(&state, &tenant_id, &model, &e);
        }
    };

    let status = upstream_resp.status().as_u16();
    crate::middleware::run_end::record_upstream_response(status, upstream_resp.headers());
    let success = (200..300).contains(&status);

    // Non-2xx upstream — log and return as JSON. Clients can
    // detect by the absence of an SSE Content-Type and surface
    // the payload to the user. This matches what the chat-
    // completions handler does for upstream errors.
    if !success {
        let body_text = upstream_resp.text().await.unwrap_or_default();
        let duration = start.elapsed().as_millis() as f64;
        let mut log = proxy_logger::build_proxy_log(
            "llm",
            &auth_ctx,
            "POST",
            "/responses",
            Some(&url),
            Some(&model.id),
            status,
            duration,
            false,
            Some(&format!("upstream {status}")),
            &correlation_id,
        );
        log.proxy_subtype = Some(crate::constants::proxy_subtype::RESPONSES.to_string());
        llm_helpers::populate_group_id(&mut log, &target_group);
        log.user_agent = extract_user_agent(headers);
        log.request_size_bytes = Some(request_body_str.len() as i32);
        log.request_body_preview = Some(body_preview(&request_body_str));
        log.response_size_bytes = Some(body_text.len() as i32);
        log.response_body_preview = Some(body_preview(&body_text));
        log.llm_model_requested = request.model.clone();
        log.llm_model_used = Some(model.model_name.clone());
        log.llm_provider = Some(model.provider.clone());
        log.llm_streaming = Some(true);
        proxy_logger::apply_call_chain(
            &mut log,
            &call_chain,
            &current_task_id_str,
            trace_continuity,
            trace_root_kind,
        );
        match &residency_decision {
            Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
            None => proxy_logger::apply_residency_not_evaluated(
                &mut log,
                provider_region_for_audit.as_deref(),
            ),
        }
        state
            .background_writer
            .write_proxy_log(state.db.clone(), log);

        let axum_status = StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_GATEWAY);
        let parsed: Value = serde_json::from_str(&body_text)
            .unwrap_or_else(|_| serde_json::json!({"error": {"message": body_text}}));
        let mut response = (axum_status, Json(parsed)).into_response();
        response.headers_mut().insert(
            crate::constants::headers::X_CORRELATION_ID,
            correlation_id.parse().unwrap(),
        );
        crate::services::a2a_delegation::stamp_run_root(
            response.headers_mut(),
            &call_chain.root_task_id,
        );
        return response;
    }

    let byte_stream = upstream_resp.bytes_stream();
    let (tx, rx) = tokio::sync::mpsc::channel::<Result<Event, axum::Error>>(32);

    proxy_metrics::stream_opened();

    let state_bg = state.clone();
    let auth_bg = auth_ctx.clone();
    let model_bg = model.clone();
    let corr_id_bg = correlation_id.clone();
    let tenant_id_bg = tenant_id.clone();
    let target_group_bg = target_group.clone();
    let user_agent = extract_user_agent(headers);
    let req_body_str_bg = request_body_str.clone();
    let request_for_cache = request_body_for_cache.clone();
    let model_requested = request.model.clone();
    let outbound_body_bg = body.clone();

    // Pre-load the tenant's guardrail configs once, before the
    // streaming closure starts (the cache read reaches into Redis/DB —
    // not the per-chunk hot path). The Responses SSE output is then
    // guarded incrementally on the `chat_output` surface, mirroring the
    // chat streaming path. Redaction (sync) re-emits the held text as
    // synthetic `response.output_text.delta` events; block-only (async)
    // forwards verbatim and terminates on a Block verdict.
    let stream_guardrail_configs = match state
        .guardrails_cache
        .get_configs(
            &tenant_id,
            auth_ctx.guardrail_user_id().as_deref(),
            auth_ctx.guardrail_group_id(),
            &state.db,
        )
        .await
    {
        Ok(c) => c,
        // Fail closed — see `configs_unavailable`.
        Err(e) => {
            return crate::services::llm_refusal::guardrails_unavailable(
                &state,
                &crate::services::llm_refusal::RefusalScope::new(
                    &auth_ctx,
                    headers,
                    &correlation_id,
                    "/responses",
                    &crate::services::llm_refusal::RunAttribution::chain(
                        &call_chain,
                        &current_task_id_str,
                        trace_continuity,
                    ),
                ),
                crate::constants::guardrail_surface::CHAT_OUTPUT,
                &e,
            );
        }
    };

    // The run the mid-stream abort poll watches.
    // The run root, kept for the response header: `call_chain` moves into the stream task.
    let response_run_root = call_chain.root_task_id.to_string();
    let abort_run_id = call_chain.root_task_id.to_string();
    // The run and request the post-stream function-call check records on.
    let headers_bg = headers.clone();
    let run_attr_bg = crate::services::llm_refusal::RunAttribution::chain(
        &call_chain,
        &current_task_id_str,
        trace_continuity,
    );
    tokio::spawn(crate::services::guardrails::flags::carried(async move {
        let mut sse_buf = crate::services::sse::SseByteBuffer::new();
        let mut sink = ResponsesSink::new(&tx);
        // Complete function-call items seen in the stream, for a stream that
        // ends without `response.completed`.
        let mut completed_calls: Vec<Value> = Vec::new();
        let mut total_response_bytes = 0usize;
        let mut accumulated_text = String::with_capacity(4096);
        // Usage arrives once, on the terminal `response.completed` event
        // (`response.created` / `in_progress` carry `usage: null`, which
        // parses as empty and is ignored).
        let mut stream_usage = llm_helpers::ResponsesUsage::default();
        // Output-guardrail state machine for this Responses stream.
        let mut stream_guard = crate::services::stream_guard::StreamGuard::new(
            stream_guardrail_configs.into_vec(),
            state_bg.http_client.clone(),
        )
        .for_run(tenant_id_bg.clone(), abort_run_id);
        let mut guardrail_blocked = false;
        // Final upstream `response` object harvested from
        // `event: response.completed` so we can store the full
        // response in the semantic cache.
        let mut final_response: Option<Value> = None;

        futures::pin_mut!(byte_stream);
        'outer: while let Some(chunk_result) = byte_stream.next().await {
            match chunk_result {
                Ok(bytes) => {
                    total_response_bytes += bytes.len();
                    if total_response_bytes > MAX_STREAM_BYTES {
                        tracing::warn!(
                            total_bytes = total_response_bytes,
                            "Responses stream exceeded safety ceiling, aborting"
                        );
                        break;
                    }
                    sse_buf.push(&bytes);

                    // Drain SSE events — UTF-8-safe (complete events only) and
                    // CRLF-aware. See services::sse.
                    while let Some(event_block) = sse_buf.next_event() {
                        // An operator can stop the run
                        // mid-stream; dropping the upstream connection is
                        // what stops the provider generating, and billing.
                        if stream_guard.run_aborted(&state_bg).await {
                            emit_responses_text_action(
                                &mut sink,
                                crate::services::stream_guard::TextAction::Block(
                                    crate::services::run_intervention::ABORTED_MESSAGE.to_string(),
                                ),
                                &mut guardrail_blocked,
                                &corr_id_bg,
                            )
                            .await;
                            break 'outer;
                        }
                        // Forward the event verbatim. SSE wire format
                        // is line-oriented; axum's `Event::default()
                        // .data(...)` would re-prefix with `data:` and
                        // strip our `event:` line, so we use the
                        // builder explicitly to preserve named events.
                        let mut event_name: Option<String> = None;
                        let mut data_lines: Vec<&str> = Vec::new();
                        for line in event_block.lines() {
                            if let Some(name) = crate::services::sse::sse_field(line, "event") {
                                event_name = Some(name.to_string());
                            } else if let Some(d) = crate::services::sse::sse_field(line, "data") {
                                data_lines.push(d);
                            }
                            // ignore comments / id lines
                        }
                        let mut data_joined = data_lines.join("\n");

                        // Output checks cover the answer, not the reasoning
                        // summary, so where a policy can block or redact
                        // (sync) reasoning events are dropped and reasoning
                        // items emptied, before the log and the cache read
                        // the final response below.
                        if stream_guard.is_sync() {
                            // The type is on the `event:` line, or only in the
                            // payload when an upstream leaves that line out.
                            let reasoning_event = match event_name.as_deref() {
                                Some(name) => {
                                    crate::services::stream_guard::is_responses_reasoning_event(
                                        name,
                                    )
                                }
                                None => {
                                    data_joined.contains("response.reasoning")
                                        && serde_json::from_str::<Value>(&data_joined)
                                            .ok()
                                            .and_then(|v| {
                                                v.get("type")
                                                    .and_then(Value::as_str)
                                                    .map(crate::services::stream_guard::is_responses_reasoning_event)
                                            })
                                            .unwrap_or(false)
                                }
                            };
                            if reasoning_event {
                                continue;
                            }
                            if data_joined.contains("\"reasoning\"")
                                && let Ok(mut v) = serde_json::from_str::<Value>(&data_joined)
                                && crate::services::stream_guard::strip_responses_reasoning(&mut v)
                            {
                                data_joined = v.to_string();
                            }
                        }

                        // The events that repeat the answer's text
                        // (`output_text.done`, `content_part.done`,
                        // `output_item.done`, `response.completed`) carry the
                        // same redactions as the deltas did. Done here, before
                        // the final response is taken for the log and the
                        // cache below, so those hold the redacted text too.
                        if stream_guard.is_sync()
                            && data_joined.contains("output_text")
                            && let Ok(mut v) = serde_json::from_str::<Value>(&data_joined)
                        {
                            let repeated =
                                crate::services::stream_guard::responses_repeated_texts(&v);
                            if !repeated.is_empty() {
                                use crate::services::stream_guard::TextAction;
                                let mut checked = Vec::with_capacity(repeated.len());
                                let mut refused: Option<String> = None;
                                for text in &repeated {
                                    match stream_guard.check_repeated_text(text).await {
                                        TextAction::Emit(t) => checked.extend(t),
                                        TextAction::Block(reason) => {
                                            refused = Some(reason);
                                            break;
                                        }
                                    }
                                }
                                // Fail closed: a repeat that is refused, or
                                // that cannot be put back where it came from,
                                // ends the stream rather than going out as it
                                // arrived.
                                let replaced = refused.is_none()
                                    && crate::services::stream_guard::replace_responses_repeated_texts(
                                        &mut v, checked,
                                    );
                                if !replaced {
                                    emit_responses_text_action(
                                        &mut sink,
                                        TextAction::Block(refused.unwrap_or_else(|| {
                                            "a repeated answer text could not be checked"
                                                .to_string()
                                        })),
                                        &mut guardrail_blocked,
                                        &corr_id_bg,
                                    )
                                    .await;
                                    break 'outer;
                                }
                                data_joined = v.to_string();
                            }
                        }

                        // From the first frame of a function
                        // call, the turn is held until its calls are judged.
                        if is_responses_function_call_event(event_name.as_deref(), &data_joined) {
                            sink.hold();
                            if let Ok(v) = serde_json::from_str::<Value>(&data_joined)
                                && event_name
                                    .as_deref()
                                    .or_else(|| v.get("type").and_then(Value::as_str))
                                    == Some("response.output_item.done")
                                && let Some(item) = v.get("item")
                            {
                                completed_calls.push(item.clone());
                            }
                        }

                        // Sniff event payload for telemetry —
                        // text deltas and the final response so we
                        // can populate proxy_log + cache after the
                        // stream closes. Best-effort JSON parse;
                        // unparseable events still get forwarded.
                        // `delta_text` is set when this event carries an
                        // `response.output_text.delta` assistant text
                        // chunk — the slice the output guardrails scan.
                        let mut delta_text: Option<String> = None;
                        if let Ok(v) = serde_json::from_str::<Value>(&data_joined) {
                            if let Some(delta) = v.get("delta").and_then(|d| d.as_str()) {
                                // Only treat `output_text.delta` as guardable
                                // assistant text; reasoning / refusal / args
                                // deltas use other event types and aren't the
                                // user-visible answer text.
                                let is_text_delta = event_name
                                    .as_deref()
                                    .map(|n| n == "response.output_text.delta")
                                    .unwrap_or(true);
                                if is_text_delta {
                                    delta_text = Some(delta.to_string());
                                }
                            }
                            if let Some(resp) = v.get("response") {
                                let u = llm_helpers::ResponsesUsage::from_body(resp);
                                if !u.is_empty() {
                                    stream_usage = u;
                                }
                                // The `response.completed` event
                                // carries the full response object.
                                final_response = Some(resp.clone());
                            }
                        }

                        // Route assistant text deltas through the
                        // output-guardrail state machine. Non-text events
                        // (created / item.added / completed / tool args /
                        // reasoning) are forwarded verbatim.
                        if let Some(text) = delta_text {
                            accumulated_text.push_str(&text);
                            if stream_guard.is_sync() {
                                // Sync (redaction): suppress the original
                                // delta; emit redacted segments as synthetic
                                // output_text.delta events (held back across
                                // boundaries by the segmenter).
                                let action = stream_guard.process_text(&text).await;
                                if emit_responses_text_action(
                                    &mut sink,
                                    action,
                                    &mut guardrail_blocked,
                                    &corr_id_bg,
                                )
                                .await
                                {
                                    break 'outer;
                                }
                                // original delta event intentionally dropped
                                continue;
                            } else {
                                // Async (block-only): scan, forward verbatim,
                                // terminate on a Block verdict.
                                use crate::services::stream_guard::TextAction;
                                if let TextAction::Block(reason) =
                                    stream_guard.process_text(&text).await
                                {
                                    emit_responses_text_action(
                                        &mut sink,
                                        TextAction::Block(reason),
                                        &mut guardrail_blocked,
                                        &corr_id_bg,
                                    )
                                    .await;
                                    break 'outer;
                                }
                            }
                        } else if event_name.as_deref() == Some("response.completed") {
                            // Flush any sync-mode held-back tail as a final
                            // synthetic text delta BEFORE the terminal event,
                            // so the redacted tail precedes `completed`.
                            let action = stream_guard.finish_text().await;
                            if emit_responses_text_action(
                                &mut sink,
                                action,
                                &mut guardrail_blocked,
                                &corr_id_bg,
                            )
                            .await
                            {
                                break 'outer;
                            }
                        }

                        let mut sse_event = Event::default();
                        if let Some(ref name) = event_name {
                            sse_event = sse_event.event(name.clone());
                        }
                        if sink.send(sse_event.data(data_joined)).await {
                            break 'outer;
                        }
                    }
                }
                Err(e) => {
                    tracing::warn!(error = %e, "Responses upstream stream error");
                    break;
                }
            }
        }

        // The function calls the answer asked for are
        // judged (data scope, argument policies, tool-call rate) before the
        // held rest of the turn goes out. Judged on the completed response,
        // or, when the stream ended without one, on the complete call items
        // it carried. A refusal writes its own row; the client gets an error
        // event and never the turn's end.
        let held = sink.release();
        let tool_view = match final_response.as_ref() {
            Some(resp) => responses_output_to_chat_tool_view(resp),
            None => responses_output_to_chat_tool_view(&json!({ "output": completed_calls })),
        };
        let refused = match tool_view {
            Some(view) if !held.is_empty() => llm_preflight::apply_response_governance(
                &state_bg,
                &target_group_bg,
                &model_bg.id,
                &view,
                error_response,
                &RefusalScope::new(
                    &auth_bg,
                    &headers_bg,
                    &corr_id_bg,
                    "/responses",
                    &run_attr_bg,
                )
                .started(start),
            )
            .await
            .is_err(),
            _ => false,
        };
        if refused {
            tracing::warn!(
                correlation_id = %corr_id_bg,
                "Streamed Responses function call refused by output governance"
            );
            let _ = tx
                .send(Ok(Event::default().event("error").data(
                    "{\"error\":{\"type\":\"guardrail_block\",\"message\":\"Response blocked by output governance policy\"}}",
                )))
                .await;
        } else {
            for event in held {
                if tx.send(Ok(event)).await.is_err() {
                    break;
                }
            }
        }

        // Send a final keepalive-style done marker. OpenAI's
        // Responses API doesn't send `[DONE]` — `response.completed`
        // is the terminal event — so we don't fabricate one here.

        proxy_metrics::stream_closed();

        let duration = start.elapsed().as_millis() as f64;
        let response_body_str = final_response
            .as_ref()
            .map(|r| serde_json::to_string(r).unwrap_or_default())
            .unwrap_or_else(|| accumulated_text.clone());
        let prompt_tokens = stream_usage.input_tokens;
        let completion_tokens = stream_usage.output_tokens;

        // Reflect a streaming output-guardrail block in the audit
        // row. Without this a blocked stream would be mis-recorded as a
        // successful 200 (same fix the chat streaming path carries).
        let (log_status, log_success, log_error): (u16, bool, Option<&str>) = if guardrail_blocked {
            (
                403,
                false,
                Some("Response blocked by output guardrail policy"),
            )
        } else {
            (200, true, None)
        };
        let mut log = proxy_logger::build_proxy_log(
            "llm",
            &auth_bg,
            "POST",
            "/responses",
            Some(&url),
            Some(&model_bg.id),
            log_status,
            duration,
            log_success,
            log_error,
            &corr_id_bg,
        );
        log.proxy_subtype = Some(crate::constants::proxy_subtype::RESPONSES.to_string());
        llm_helpers::populate_group_id(&mut log, &target_group_bg);
        log.user_agent = user_agent;
        log.request_size_bytes = Some(req_body_str_bg.len() as i32);
        log.request_body_preview = Some(body_preview(&req_body_str_bg));
        log.response_size_bytes = Some(response_body_str.len() as i32);
        log.response_body_preview = Some(body_preview(&response_body_str));
        log.llm_model_requested = model_requested.clone();
        log.llm_model_used = Some(model_bg.model_name.clone());
        log.llm_provider = Some(model_bg.provider.clone());
        log.llm_streaming = Some(true);
        if input_pii_redacted {
            proxy_logger::apply_pii_detected(&mut log);
        }
        // Same parser and cost formula as the non-streaming branch and as
        // usage_logs below, so the three agree for a streamed call.
        let estimated_cost =
            llm_helpers::populate_llm_usage_fields(&mut log, &stream_usage, &model_bg);
        populate_llm_request_fields(&mut log, &outbound_body_bg);
        // Prompt cache telemetry — the Responses stream reports
        // `input_tokens_details.cached_tokens` on the terminal event.
        let pc_mechanism = crate::services::prompt_cache_usage::mechanism_for_provider(
            &model_bg.provider,
            &model_bg.model_name,
        );
        let pc_usage = crate::services::prompt_cache_usage::usage_from_stream_totals(
            0,
            stream_usage.cached_input_tokens,
            pc_mechanism,
            &model_bg,
        );
        llm_helpers::apply_prompt_cache_usage(
            &mut log,
            &pc_usage,
            pc_mechanism,
            &model_bg.provider,
        );
        proxy_logger::apply_call_chain(
            &mut log,
            &call_chain,
            &current_task_id_str,
            trace_continuity,
            trace_root_kind,
        );
        // How the guard ended the stream (operator abort / guardrail block) is recorded on its own
        // outcome row — the row that names the run and step and carries the
        // tokens the provider billed before the guard stopped it.
        stream_guard.stamp_outcome(&mut log);
        match &residency_decision {
            Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
            None => proxy_logger::apply_residency_not_evaluated(
                &mut log,
                provider_region_for_audit.as_deref(),
            ),
        }
        state_bg
            .background_writer
            .write_proxy_log(state_bg.db.clone(), log);

        // proxy_metrics row — Mission Control reads exclusively
        // from `proxy_metrics`, so without this write every
        // streaming /responses call (used by reasoning models like
        // o1 / o3 in Responses-API mode) is invisible to the
        // dashboard while resource-group usage sees it.
        let llm_metric_data = serde_json::json!({
            "provider": model_bg.provider,
            "model_requested": model_requested,
            "model_used": model_bg.model_name,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens.zip(completion_tokens).map(|(p, c)| p + c),
            "estimated_cost_usd": estimated_cost,
            "streaming": true,
        });
        if !is_probe {
            let metric = proxy_logger::build_proxy_metric(
                "llm",
                &auth_bg,
                "POST",
                "/responses",
                "responses",
                Some(&model_bg.id),
                200,
                duration,
                true,
                &corr_id_bg,
                Some(req_body_str_bg.len() as i32),
                Some(response_body_str.len() as i32),
                None,
                None,
                None,
                Some(llm_metric_data),
                None,
            );
            state_bg
                .background_writer
                .write_proxy_metric(state_bg.db.clone(), metric);
        } // end if !is_probe

        if let Some(pt) = prompt_tokens {
            proxy_metrics::record_tokens("prompt", &model_bg.provider, &model_bg.model_name, pt);
        }
        if let Some(ct) = completion_tokens {
            proxy_metrics::record_tokens(
                "completion",
                &model_bg.provider,
                &model_bg.model_name,
                ct,
            );
        }

        // A probe records its real usage (billed to Unbound);
        // only the metric is left to the central probe recorder.
        // Usage record for cost / quota / limits. Same helper
        // the chat-completions streaming path uses — fields
        // identical, just the endpoint string changes.
        usage::write_llm_usage_log(
            &state_bg.usage_state,
            &state_bg.background_writer,
            &state_bg.model_metrics,
            state_bg.db.clone(),
            &tenant_id_bg,
            &auth_bg,
            target_group_bg.as_ref(),
            &model_bg.id,
            &model_bg.model_name,
            &model_bg.provider,
            prompt_tokens,
            completion_tokens,
            estimated_cost,
            model_bg.cost_per_input_token.unwrap_or(0.0),
            model_bg.cost_per_output_token.unwrap_or(0.0),
            &url,
            &corr_id_bg,
            200,
            duration,
        );
        if let Some(ref g) = target_group_bg {
            usage::spawn_alert_evaluation(
                state_bg.db.clone(),
                tenant_id_bg.clone(),
                g.clone(),
                "llm".into(),
                Some(corr_id_bg.clone()),
            );
        }

        // Semantic-cache store. We cache the final `response`
        // object — a re-issuance of the same prompt by another
        // user can later be served from cache as a single-shot
        // JSON without re-streaming. Tools / continuations
        // gating mirrors the non-streaming /responses path.
        if let Some(resp_value) = final_response {
            let total = match (prompt_tokens, completion_tokens) {
                (Some(p), Some(c)) => p + c,
                _ => prompt_tokens.unwrap_or(0),
            };
            let cache_key = CacheKey {
                tenant_id: &tenant_id_bg,
                group_id: target_group_bg.as_ref().map(|g| g.id.as_str()),
                model_name: &model_bg.model_name,
                body: CacheBody::Value(&request_for_cache),
            };
            // A stream the output guardrail stopped, or whose function
            // call output governance refused, is not cached: the lookup
            // returns a cached body with no guardrail pass, so storing it
            // would turn one refusal into an answer for everyone who asks
            // next (as on the streamed chat store).
            if !guardrail_blocked
                && !refused
                && let Some(plan) = cache_session.plan_store(
                    &cache_key,
                    StoreFacts {
                        invoked_tools: llm_helpers::extract_responses_tool_calls(&resp_value),
                        token_count: total,
                    },
                )
            {
                plan.commit(resp_value);
            }
        }
    }));

    let stream = ReceiverStream::new(rx);
    let mut response = Sse::new(stream)
        .keep_alive(KeepAlive::default())
        .into_response();
    response.headers_mut().insert(
        crate::constants::headers::X_CORRELATION_ID,
        correlation_id.parse().unwrap(),
    );
    crate::services::a2a_delegation::stamp_run_root(response.headers_mut(), &response_run_root);
    response
}

/// Number of distinct inputs in an OpenAI-style embeddings `input` field, used
/// to enforce `max_batch_size` embedding governance. Per the OpenAI contract
/// `input` may be a single string, an array of strings, a single pre-tokenized
/// input (array of ints), or a batch of pre-tokenized inputs (array of arrays).
/// An array whose elements are all numbers is ONE tokenized input; any other
/// non-empty array is a batch of its length; a scalar is one; null/empty is 0.
fn embedding_batch_size(input: &serde_json::Value) -> u32 {
    match input {
        serde_json::Value::Array(arr) => {
            if arr.is_empty() {
                0
            } else if arr.iter().all(|v| v.is_number()) {
                1
            } else {
                arr.len() as u32
            }
        }
        serde_json::Value::Null => 0,
        _ => 1,
    }
}

/// Mark a chat row as the semantic-policy judge's own call
/// (`tags.internal_call`), as an embeddings row is marked as the platform's
/// (`stamp_internal_embedding`). `is_judge` is the verified origin
/// (`internal_call::is_internal`), never header presence. The row hangs off
/// the run of the call the judge rules on.
fn stamp_judge_self_call(log: &mut crate::models::db::ProxyLogInsert, is_judge: bool) {
    if is_judge {
        log.tags = Some(json!({ "internal_call": "semantic_policy_judge" }));
    }
}

/// Mark an embeddings row as the platform's own call for the caller: which
/// feature made it (`tags.internal_embedding`: `semantic_cache` or
/// `knowledge_base`) and, for the semantic cache, the operation
/// (`llm_cache_layer`: `lookup` or `store`). The row is otherwise the
/// caller's: tenant, group, key, agent, end user, run, tokens and cost.
fn stamp_internal_embedding(
    log: &mut crate::models::db::ProxyLogInsert,
    internal: Option<crate::services::internal_call::InternalEmbedding>,
) {
    let Some(internal) = internal else { return };
    log.llm_cache_layer = internal.cache_op().map(str::to_string);
    log.tags = Some(json!({ "internal_embedding": internal.feature() }));
}

#[utoipa::path(
    post,
    path = "/v1/proxy/llm/embeddings",
    tag = "LLM",
    summary = "Generate embeddings",
    request_body = EmbeddingRequest,
    responses(
        (status = 200, description = "Embedding vectors", body = Value),
        (status = 401, description = "Authentication required"),
    ),
    security(("api_key" = []), ("bearer" = []))
)]
pub(crate) async fn embeddings(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Json(mut request): Json<EmbeddingRequest>,
) -> Response {
    let start = Instant::now();
    let tenant_id = match route_helpers::extract_tenant_id(tenant) {
        Ok(id) => id,
        Err(resp) => return resp,
    };
    let correlation_id = route_helpers::extract_correlation_id(&headers);
    // Whether the platform's own code makes this call for the caller (the
    // semantic cache, the Knowledge Base). Verified
    // by this process's token: a caller cannot assert it.
    let internal_embedding = crate::services::internal_call::internal_embedding(&headers);
    // Probe traffic skips metric writes — see chat_completions doc.

    // Establish the call chain at request entry.
    // See chat_completions for the detailed rationale.
    let tenant_key = derive_tenant_key(state.config.encryption_key.as_bytes(), &tenant_id);
    // Resolve the conversation this request belongs to, if the client
    // asserted one. Derived here rather than in the middleware because the
    // HMAC needs the tenant key, which only exists after auth.
    if let Some(sess) = crate::services::a2a_delegation::resolve_asserted_session(
        |h| {
            headers
                .get(h)
                .and_then(|v| v.to_str().ok())
                .map(str::to_string)
        },
        &tenant_key,
    ) {
        crate::middleware::run_end::set_resolved_session(sess.to_string());
    }
    let (call_chain, current_task_id, trace_continuity) =
        parse_or_mint(|n| header_value(&headers, n), &tenant_key);
    let current_task_id_str = current_task_id.to_string();

    // Preflight: auth → model resolution → access check → quota enforcement
    let pf_body = serde_json::to_string(&request).unwrap_or_default();
    // Where this call sits in its run — the run the pre-flight run controls
    // check, and the run every refusal or block row of this request lands on.
    let run_attr =
        llm_refusal::RunAttribution::chain(&call_chain, &current_task_id_str, trace_continuity);
    let pf = match llm_preflight::run_preflight(
        &state,
        &headers,
        &tenant_id,
        &correlation_id,
        request.model.as_deref(),
        request.model_id.as_deref(),
        // Refusals land on this call's run. Run caps are not armed here:
        // embeddings are also the semantic cache's own self-calls.
        &PreflightOptions::quota_only(run_attr.clone())
            .not_run_accounted()
            .no_output(),
        error_response,
        client_addr.ip(),
        None,
        Some(&pf_body),
    )
    .await
    {
        Ok(pf) => pf,
        Err(resp) => return resp,
    };
    let llm_preflight::PreflightResult {
        auth_ctx,
        model,
        target_group,
        ..
    } = pf;
    // A probe's metric is written by the central probe recorder; its usage
    // (real tokens and cost, billed to Unbound) is written here. Only
    // the platform service key's probe counts.
    let is_probe = crate::services::catalog_probe::is_health_probe_request(&headers, &auth_ctx);

    let trace_root_kind = root_kind_for_auth(&auth_ctx.auth_type, call_chain.depth == 0);

    // Data residency check.
    // Data residency: one shared
    // implementation, see `llm_preflight::enforce_residency`. This used to be
    // ~55 lines copied here and at every other LLM surface, which is how the
    // sticky prompt-cache pin and the Gemini bridge each
    // came to skip the check independently — there was no single place to add
    // one.
    let residency = match llm_preflight::enforce_residency(
        &state,
        &auth_ctx,
        &tenant_id,
        &model,
        request.model.clone(),
        "/v1/embeddings",
        "Embeddings",
        &headers,
        &correlation_id,
        start,
        &call_chain,
        &current_task_id_str,
        trace_continuity,
        trace_root_kind,
        error_response,
    )
    .await
    {
        Ok(o) => o,
        Err(resp) => return resp,
    };
    let provider_region_for_audit = residency.provider_region;
    let residency_decision = residency.decision;

    // The actor planes, as on `/chat/completions`
    // (see `responses`). An embedding is a model call made with the caller's
    // credential, also when the gateway makes it for the caller (the
    // semantic cache's and the knowledge base's self-calls carry the
    // caller's own credential and are judged as the caller's), with one
    // exception the planes make themselves: the platform's own call of the
    // tenant's embedding model needs no grant of the agent's: it is not the
    // agent's call (`llm_preflight::internal_embedding_grant`).
    if let Err(resp) = llm_preflight::enforce_actor_planes(
        &state,
        &auth_ctx,
        &tenant_id,
        &model,
        target_group.as_ref(),
        request.model.clone(),
        &llm_preflight::flatten_input_text(&request.input),
        llm_preflight::EMBEDDINGS_ACTOR_ENDPOINT,
        &headers,
        &correlation_id,
        start,
        &call_chain,
        &current_task_id_str,
        trace_continuity,
        trace_root_kind,
        error_response,
    )
    .await
    {
        return resp;
    }

    tracing::debug!(
        model_name = %model.model_name,
        provider = %model.provider,
        "Embedding request"
    );

    // Media governance for embeddings — dim whitelist / max batch size.
    // Embeddings are a media mode; enforce the per-mode governance the same
    // way the image/audio/video routes do (this path was previously unwired,
    // so an operator's embedding policy was silently ignored). Runs before the
    // upstream call so blocked requests never reach the provider.
    {
        use crate::services::media_governance::{
            EmbeddingRequest as EmbeddingGovRequest, enforce_embedding, governance_for_mode,
        };
        let effective_media_gov = match target_group.as_ref() {
            Some(g) => {
                crate::services::usage::collect_effective_media_governance_for_model(
                    &state.db,
                    g,
                    "embedding",
                    &model.id,
                )
                .await
            }
            None => Ok(None),
        };
        // `(unreadable, message)` of a refusal: the policy refused (403), or
        // it could not be read (503; fail closed — never read as "no
        // policy").
        let refusal = match effective_media_gov {
            Ok(gov) => {
                let mode_gov = governance_for_mode(gov.as_ref(), "embedding");
                let check = EmbeddingGovRequest {
                    dim: request.dimensions.and_then(|d| u32::try_from(d).ok()),
                    batch_size: embedding_batch_size(&request.input),
                };
                enforce_embedding(mode_gov, &check).err().map(|e| {
                    tracing::warn!(
                        tenant_id = %tenant_id,
                        model = %model.model_name,
                        "embedding governance block: {e}",
                    );
                    (false, format!("policy: {e}"))
                })
            }
            Err(e) => {
                tracing::error!(error = %e, "media governance unreadable; refusing (fail closed)");
                Some((
                    true,
                    "Refused: this group's media governance cannot be read right now".to_string(),
                ))
            }
        };
        if let Some((unreadable, message)) = refusal {
            let run = llm_refusal::RunAttribution::chain(
                &call_chain,
                &current_task_id_str,
                trace_continuity,
            );
            let scope = RefusalScope {
                auth_ctx: &auth_ctx,
                headers: &headers,
                correlation_id: &correlation_id,
                endpoint: "/embeddings",
                started: Some(start),
                model: Some((&model, request.model.clone())),
                group_id: target_group.as_ref().map(|g| g.id.clone()),
                run: Some(&run),
                request_body: Some(&pf_body),
            };
            let refusal = Refusal::new(
                crate::constants::proxy_subtype::LLM_GOVERNANCE_BLOCK,
                message.as_str(),
            )
            .error_type("embedding_governance");
            let audit = |log: &mut crate::models::db::ProxyLogInsert| match &residency_decision {
                Some(d) => proxy_logger::apply_residency_audit(log, d),
                None => proxy_logger::apply_residency_not_evaluated(
                    log,
                    provider_region_for_audit.as_deref(),
                ),
            };
            if unreadable {
                return llm_refusal::refuse_with(
                    &state,
                    &scope,
                    refusal,
                    audit,
                    error_response(StatusCode::SERVICE_UNAVAILABLE, &message),
                );
            }
            return llm_refusal::refuse_with(
                &state,
                &scope,
                refusal,
                audit,
                error_response(StatusCode::FORBIDDEN, &message),
            );
        }
    }

    // Input guardrails — embedding inputs are commonly free-form
    // documents (RAG indexing, doc Q&A) and are exactly the
    // surface where PII tends to leave the company. Scan before
    // hitting the upstream so the embedding bytes never get
    // generated for blocked content.
    let embed_input_text = llm_preflight::flatten_input_text(&request.input);
    // Tracks whether an input guardrail redacted a PII category — stamped
    // on the embeddings success audit row below.
    let mut input_pii_redacted = false;
    if !embed_input_text.is_empty() {
        // Per-mode surface dispatch. Embeddings ingest is
        // operator-controlled corpus (RAG indexing, doc Q&A).
        // Jailbreak / prompt_injection / toxic_content are
        // false-positive prone here and the new audit-recommended
        // seed defaults narrow them away from this surface.
        let guard_target =
            llm_preflight::LlmAuditTarget::from_model(&model, request.model.clone(), "/embeddings");
        match llm_preflight::apply_guardrails_with_redaction(
            &state,
            &tenant_id,
            &RefusalScope::new(
                &auth_ctx,
                &headers,
                &correlation_id,
                "/embeddings",
                &run_attr,
            )
            .started(start),
            &embed_input_text,
            crate::constants::guardrail_surface::EMBEDDINGS_INPUT,
            None,
            Some(&guard_target),
        )
        .await
        {
            Err(resp) => return resp,
            Ok(Some(redaction)) => {
                llm_preflight::apply_redactions_to_flattened_input(
                    &mut request.input,
                    &redaction.content,
                );
                input_pii_redacted = redaction.pii;
            }
            Ok(None) => {}
        }
    }

    // Model-level failover: try this model, then its ordered
    // `fallback_models` chain (same-mode models), until one returns 2xx. The
    // helper resolves + decrypts each candidate and records per-model cooldown.
    let fb_chain = llm_router::resolve_fallback_chain(
        &state.db,
        &tenant_id,
        &model,
        &state.router_state,
        Some(&state.encryption),
        Some(&state.model_cache),
        Some(&state.provider_cache),
        providers::ProviderApi::Embeddings,
    )
    .await;
    let mut candidates: Vec<crate::models::db::LlmModel> = Vec::with_capacity(1 + fb_chain.len());
    candidates.push(model);
    candidates.extend(fb_chain);

    // What the successful calls of a failed multi-call Bedrock request had
    // used: recorded below whichever candidate answers in the end.
    let partial_usage = crate::services::bedrock_runtime::embeddings::PartialUsageSink::default();
    let send_outcome = llm_router::send_first_success_with(&candidates, &state.router_state, |m| {
        // A Bedrock embedding row is InvokeModel in the model's own
        // shape, signed, one call per text for Titan
        // (`bedrock_runtime::embeddings`); its answer comes back as an
        // OpenAI-compatible provider's would.
        if crate::services::bedrock_runtime::serves_embeddings(m) {
            let (state, model, input) = (state.clone(), m.clone(), request.input.clone());
            let dimensions = request.dimensions;
            let partial_usage = partial_usage.clone();
            return llm_router::CandidateSend::Deferred(Box::pin(async move {
                crate::services::bedrock_runtime::embeddings::embed(
                    &state.http_client,
                    &model,
                    &input,
                    dimensions,
                    &partial_usage,
                )
                .await
            }));
        }
        let mut b = json!({
            "model": m.model_name,
            "input": request.input.clone(),
            "encoding_format": request.encoding_format,
            "dimensions": request.dimensions,
        });
        providers::prepare_embedding(&mut b, m);
        let ah = providers::build_headers_for(m, "/embeddings");
        let mut req = state
            .http_client
            .post(providers::build_url(m, "/embeddings", false))
            .header("Content-Type", "application/json");
        for (k, v) in &ah {
            req = req.header(k.as_str(), v.as_str());
        }
        llm_router::CandidateSend::Request(Box::new(req.json(&b)))
    })
    .await;

    // Rebind primary-scoped locals to the model that actually served (or last
    // failed) so the existing logging/usage blocks below are unchanged.
    let used_fallback = matches!(
        &send_outcome,
        llm_router::FallbackSend::Response {
            used_fallback: true,
            ..
        } | llm_router::FallbackSend::Transport {
            used_fallback: true,
            ..
        }
    );
    let model = match &send_outcome {
        llm_router::FallbackSend::Response { model, .. }
        | llm_router::FallbackSend::Transport { model, .. } => model.clone(),
    };
    if used_fallback {
        tracing::info!(model = %model.model_name, "embeddings: served via model-level fallback");
    }
    let url = providers::build_url(&model, "/embeddings", false);
    let mut body = json!({
        "model": model.model_name,
        "input": request.input,
        "encoding_format": request.encoding_format,
        "dimensions": request.dimensions,
    });
    providers::prepare_embedding(&mut body, &model);

    // The calls of a failed multi-call Bedrock request that succeeded
    // are billed by AWS. Their tokens are a usage row of the row they were
    // made to, costed, with the status the request failed with; the audit
    // row of the failed request carries the same tokens and cost (the error
    // body has them as `usage`).
    let partials = partial_usage
        .lock()
        .map(|mut sink| std::mem::take(&mut *sink))
        .unwrap_or_default();
    for partial in partials {
        let tokens = i32::try_from(partial.tokens).unwrap_or(i32::MAX);
        let input_cost = partial.model.cost_per_input_token.unwrap_or(0.0);
        usage::write_embedding_usage_log(
            &state.usage_state,
            &state.background_writer,
            &state.model_metrics,
            state.db.clone(),
            &tenant_id,
            &auth_ctx,
            target_group.as_ref(),
            &partial.model.id,
            &partial.model.model_name,
            &partial.model.provider,
            Some(tokens),
            llm_helpers::estimate_llm_cost(
                Some(tokens),
                None,
                input_cost,
                partial.model.cost_per_output_token.unwrap_or(0.0),
            ),
            input_cost,
            &providers::build_url(&partial.model, "/embeddings", false),
            &correlation_id,
            partial.status,
            start.elapsed().as_millis() as f64,
        );
    }

    match send_outcome {
        llm_router::FallbackSend::Response { response: resp, .. } => {
            let status = resp.status().as_u16();
            let duration = start.elapsed().as_millis() as f64;
            let resp_body: Value = crate::services::providers::upstream_json(resp).await;
            let success = (200..300).contains(&status);

            // Extract token usage for embeddings
            let prompt_tokens = resp_body
                .get("usage")
                .and_then(|u| u.get("prompt_tokens"))
                .and_then(|v| v.as_i64())
                .map(|v| v as i32);

            // Proxy log for embeddings
            {
                let request_body_str = serde_json::to_string(&body).unwrap_or_default();
                let response_body_str = serde_json::to_string(&resp_body).unwrap_or_default();
                let mut log = proxy_logger::build_proxy_log(
                    "llm",
                    &auth_ctx,
                    "POST",
                    "/embeddings",
                    Some(&url),
                    Some(&model.id),
                    status,
                    duration,
                    success,
                    None,
                    &correlation_id,
                );
                log.proxy_subtype = Some(crate::constants::proxy_subtype::EMBEDDINGS.to_string());
                // An embedding the platform made for this caller (the
                // semantic cache, the Knowledge Base) says so on its row.
                stamp_internal_embedding(&mut log, internal_embedding);
                llm_helpers::populate_group_id(&mut log, &target_group);
                log.user_agent = extract_user_agent(&headers);
                log.request_size_bytes = Some(request_body_str.len() as i32);
                log.request_body_preview = Some(body_preview(&request_body_str));
                log.response_size_bytes = Some(response_body_str.len() as i32);
                log.response_body_preview = Some(body_preview(&response_body_str));
                log.llm_model_requested = request.model.clone();
                log.llm_model_used = Some(model.model_name.clone());
                log.llm_provider = Some(model.provider.clone());
                log.llm_streaming = Some(false);
                log.llm_prompt_tokens = prompt_tokens;
                log.llm_total_tokens = prompt_tokens;
                log.llm_estimated_cost_usd = llm_helpers::estimate_llm_cost(
                    prompt_tokens,
                    None,
                    model.cost_per_input_token.unwrap_or(0.0),
                    model.cost_per_output_token.unwrap_or(0.0),
                );
                if input_pii_redacted {
                    proxy_logger::apply_pii_detected(&mut log);
                }
                populate_llm_request_fields(&mut log, &body);
                proxy_logger::apply_call_chain(
                    &mut log,
                    &call_chain,
                    &current_task_id_str,
                    trace_continuity,
                    trace_root_kind,
                );
                match &residency_decision {
                    Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
                    None => proxy_logger::apply_residency_not_evaluated(
                        &mut log,
                        provider_region_for_audit.as_deref(),
                    ),
                }
                state
                    .background_writer
                    .write_proxy_log(state.db.clone(), log);

                // proxy_metrics row — Mission Control's
                // `get_performance_metrics` reads exclusively from
                // `proxy_metrics`, so without this write every
                // embedding call (whether direct from a client or
                // a self-call from the semantic-cache layer) is
                // invisible to the dashboard while resource-group
                // usage (which reads `usage_logs`) sees it. The
                // visible symptom: Mission Control under-counts
                // requests vs group usage by exactly the volume of
                // embedding traffic — and on every chat request the
                // semantic cache fires one or more embedding self-
                // calls, so the gap grows linearly with chat load.
                let estimated_cost_for_metric = llm_helpers::estimate_llm_cost(
                    prompt_tokens,
                    None,
                    model.cost_per_input_token.unwrap_or(0.0),
                    model.cost_per_output_token.unwrap_or(0.0),
                );
                if !is_probe {
                    let llm_metric_data = serde_json::json!({
                        "provider": model.provider,
                        "model_requested": request.model,
                        "model_used": model.model_name,
                        "prompt_tokens": prompt_tokens,
                        "estimated_cost_usd": estimated_cost_for_metric,
                        "streaming": false,
                    });
                    let metric = proxy_logger::build_proxy_metric(
                        "llm",
                        &auth_ctx,
                        "POST",
                        "/embeddings",
                        "embeddings",
                        Some(&model.id),
                        status,
                        duration,
                        success,
                        &correlation_id,
                        Some(request_body_str.len() as i32),
                        Some(response_body_str.len() as i32),
                        None,
                        None,
                        extract_user_agent(&headers).as_deref(),
                        Some(llm_metric_data),
                        None,
                    );
                    state
                        .background_writer
                        .write_proxy_metric(state.db.clone(), metric);
                } // end if !is_probe
            }

            // Usage log for embeddings
            if success {
                // Probes record their real usage too
                let estimated_cost = llm_helpers::estimate_llm_cost(
                    prompt_tokens,
                    None,
                    model.cost_per_input_token.unwrap_or(0.0),
                    model.cost_per_output_token.unwrap_or(0.0),
                );
                usage::write_embedding_usage_log(
                    &state.usage_state,
                    &state.background_writer,
                    &state.model_metrics,
                    state.db.clone(),
                    &tenant_id,
                    &auth_ctx,
                    target_group.as_ref(),
                    &model.id,
                    &model.model_name,
                    &model.provider,
                    prompt_tokens,
                    estimated_cost,
                    model.cost_per_input_token.unwrap_or(0.0),
                    &url,
                    &correlation_id,
                    status,
                    duration,
                );
                if let Some(ref g) = target_group {
                    usage::spawn_alert_evaluation(
                        state.db.clone(),
                        tenant_id.clone(),
                        g.clone(),
                        "llm".into(),
                        Some(correlation_id.clone()),
                    );
                }
            }

            proxy_metrics::record_request("llm", "POST", status, &model.provider);
            proxy_metrics::record_latency("llm", "POST", &model.provider, duration / 1000.0);
            if let Some(pt) = prompt_tokens {
                proxy_metrics::record_tokens("prompt", &model.provider, &model.model_name, pt);
            }

            let axum_status = StatusCode::from_u16(status).unwrap_or(StatusCode::OK);
            let mut response = (axum_status, Json(resp_body)).into_response();
            response.headers_mut().insert(
                crate::constants::headers::X_CORRELATION_ID,
                correlation_id.parse().unwrap(),
            );
            crate::services::a2a_delegation::stamp_run_root(
                response.headers_mut(),
                &call_chain.root_task_id,
            );
            response
        }
        llm_router::FallbackSend::Transport { error: e, .. } => {
            let duration = start.elapsed().as_millis() as f64;
            let mut log = proxy_logger::build_proxy_log(
                "llm",
                &auth_ctx,
                "POST",
                "/embeddings",
                Some(&url),
                Some(&model.id),
                502,
                duration,
                false,
                Some(&e.to_string()),
                &correlation_id,
            );
            log.proxy_subtype = Some(crate::constants::proxy_subtype::EMBEDDINGS.to_string());
            stamp_internal_embedding(&mut log, internal_embedding);
            llm_helpers::populate_group_id(&mut log, &target_group);
            log.user_agent = extract_user_agent(&headers);
            log.llm_model_requested = request.model.clone();
            log.llm_model_used = Some(model.model_name.clone());
            log.llm_provider = Some(model.provider.clone());
            proxy_logger::apply_call_chain(
                &mut log,
                &call_chain,
                &current_task_id_str,
                trace_continuity,
                trace_root_kind,
            );
            match &residency_decision {
                Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
                None => proxy_logger::apply_residency_not_evaluated(
                    &mut log,
                    provider_region_for_audit.as_deref(),
                ),
            }
            state
                .background_writer
                .write_proxy_log(state.db.clone(), log);

            // proxy_metrics row on the upstream-error path so
            // Mission Control's error rate reflects embedding
            // failures the same way it reflects chat failures.
            let llm_metric_data = serde_json::json!({
                "provider": model.provider,
                "model_requested": request.model,
                "model_used": model.model_name,
                "error": e.to_string(),
                "streaming": false,
            });
            if !is_probe {
                let metric = proxy_logger::build_proxy_metric(
                    "llm",
                    &auth_ctx,
                    "POST",
                    "/embeddings",
                    "embeddings",
                    Some(&model.id),
                    502,
                    duration,
                    false,
                    &correlation_id,
                    None,
                    None,
                    None,
                    None,
                    extract_user_agent(&headers).as_deref(),
                    Some(llm_metric_data),
                    None,
                );
                state
                    .background_writer
                    .write_proxy_metric(state.db.clone(), metric);
            } // end if !is_probe

            proxy_metrics::record_request("llm", "POST", 502, &model.provider);
            proxy_metrics::record_latency("llm", "POST", &model.provider, duration / 1000.0);
            reqwest_error_response(&state, &tenant_id, &model, &e)
        }
    }
}

/// GET /v1/proxy/llm/health
async fn health(State(state): State<SharedState>) -> Json<Value> {
    let db_ok = sqlx::query("SELECT 1").execute(&state.db).await.is_ok();

    Json(json!({
        "status": if db_ok { "healthy" } else { "degraded" },
        "service": "brutor-gateway-core-llm",
        "components": {
            "database": if db_ok { "up" } else { "down" },
        }
    }))
}

// ---------------------------------------------------------------------------
// POST /admin/{model_id}/reset-resilience — Clear all in-memory
// resilience state for a model after an operator confirms it is
// healthy via the admin "Test Connection" flow. Wipes the circuit
// breaker counters, probe throttle, and router cooldown, and writes
// `health_status='healthy'` to the DB as a belt-and-suspenders.
//
// Auth: requires the tenant context to match the model's tenant_id.
// Since the Python admin side calls this with BRUTOR_GATEWAY_SERVICE_API_KEY
// (global scope) + X-Tenant-ID, the tenant middleware populates
// TenantContext and we enforce the match ourselves.
// ---------------------------------------------------------------------------

#[utoipa::path(
    post,
    path = "/v1/proxy/llm/admin/{model_id}/reset-resilience",
    tag = "LLM",
    summary = "Reset resilience state for a model (admin only)",
    description = "Clears the in-memory circuit breaker, probe throttle, \
    and router cooldown for the given model, and flips DB health_status \
    back to 'healthy'. Used by the admin Test Connection flow so an \
    operator can immediately unstick a model after verifying it's up.",
    params(("model_id" = String, Path, description = "LLModel id")),
    responses(
        (status = 200, description = "Resilience state reset"),
        (status = 401, description = "No credential, or one that does not stand"),
        (status = 403, description = "Not the platform service key"),
        (status = 404, description = "Model not found for the current tenant"),
    ),
    security(
        ("api_key" = []),
        ("bearer" = []),
    )
)]
#[tracing::instrument(
    name = "admin.reset_resilience",
    skip_all,
    fields(model_id = %model_id, tenant_id = tracing::field::Empty)
)]
pub(crate) async fn reset_resilience(
    State(state): State<SharedState>,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Path(model_id): Path<String>,
) -> Response {
    let tenant_id = match tenant {
        Some(axum::Extension(ctx)) => ctx.tenant_id,
        None => {
            return error_response(StatusCode::UNAUTHORIZED, "tenant context required");
        }
    };
    // The control plane's route: the
    // platform service key, or nothing is done. It read no credential, so
    // anyone who could name a tenant declared its failing models healthy.
    if let Err(resp) =
        crate::services::service_auth::require_platform_key(&state, &headers, &tenant_id, None)
            .await
    {
        return resp;
    }
    // Record tenant_id on the span so a slow-request capture can
    // correlate the snapshot to the right tenant.
    tracing::Span::current().record("tenant_id", tracing::field::display(&tenant_id));
    tracing::debug!(target: "diagnostics", "reset_resilience: acquiring DB connection");
    let _query_start = std::time::Instant::now();

    // Single round-trip to the DB: the UPDATE's WHERE clause both
    // tenant-scopes the operation AND stands in for the existence
    // check. `rows_affected == 0` means the row didn't exist for this
    // tenant — reply 404 without burning a second connection on a
    // pre-flight SELECT.
    //
    // Why this matters: the admin "Test Connection" sweep fans out
    // across every seeded model in parallel (70+ in a default seed).
    // The previous SELECT + spawned-UPDATE shape demanded two pool
    // slots per call, so a default pool of 20 saturated and every
    // request hit the 5s acquire-timeout, returning 500. Collapsing
    // to one UPDATE halves the contention and removes the blocking
    // pre-flight read entirely.
    let update_sql = "UPDATE llm_model SET \
                      health_status = 'healthy', \
                      consecutive_health_failures = 0, \
                      health_check_error = NULL, \
                      last_health_success = NOW(), \
                      last_health_check = NOW() \
                      WHERE id = $1 AND tenant_id = $2";
    let rows_affected = match sqlx::query(update_sql)
        .bind(&model_id)
        .bind(&tenant_id)
        .execute(&state.db)
        .await
    {
        Ok(result) => {
            // Record SQL wall time so the slow-request middleware
            // can attribute "where did the 10s go?" between the
            // tenant middleware (DB lookup x2), this UPDATE, and
            // request setup overhead.
            tracing::debug!(
                target: "diagnostics",
                sql_ms = _query_start.elapsed().as_millis() as u64,
                rows_affected = result.rows_affected(),
                "reset_resilience: UPDATE complete"
            );
            result.rows_affected()
        }
        Err(e) => {
            tracing::error!(
                model_id = %model_id,
                tenant_id = %tenant_id,
                error = %e,
                sql_ms = _query_start.elapsed().as_millis() as u64,
                "reset_resilience: DB update failed"
            );
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "update failed"})),
            )
                .into_response();
        }
    };

    if rows_affected == 0 {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "model not found for tenant"})),
        )
            .into_response();
    }

    // In-memory reset only — the DB row is already up to date from
    // the UPDATE above, so we don't need the spawned UPDATE that
    // `reset_model_resilience` would issue. Calling the in-memory
    // helper directly keeps the work to a single DB hit per request.
    crate::services::llm_resilience::reset_model_resilience_in_memory(&state, &model_id);

    (
        StatusCode::OK,
        Json(json!({
            "status": "reset",
            "model_id": model_id,
            "tenant_id": tenant_id,
            "cleared": [
                "circuit_breaker",
                "probe_throttle",
                "router_cooldown",
                "db_health_status",
            ]
        })),
    )
        .into_response()
}

// ---------------------------------------------------------------------------
// POST /admin/{model_id}/invalidate-cache — Synchronous cache bust.
//
// Called from the admin control plane after any mutation of an
// llm_model row (create/update/copy/group-reassign). The existing
// Redis pub/sub invalidation is fire-and-forget from the admin's
// perspective — there's no receipt that any proxy pod has actually
// processed the message. That opens a short race when the admin UI
// immediately dispatches a follow-up call (e.g. health-check) that
// races past the invalidation and hits a stale model in cache
// (symptom: "API key not configured" right after the operator just
// saved it).
//
// Calling this endpoint synchronously from the admin guarantees THIS
// proxy pod has cleared its model/group cache entries before the
// admin's response returns. Redis pub/sub still fans out to the
// other pods in multi-pod deployments.
// ---------------------------------------------------------------------------

#[utoipa::path(
    post,
    path = "/v1/proxy/llm/admin/{model_id}/invalidate-cache",
    tag = "LLM",
    summary = "Synchronously invalidate the in-memory model cache (admin only)",
    description = "Drops the given model from the gateway's in-process \
    model cache and flushes the tenant's group cache so the next \
    request reloads fresh DB state. Called by the admin after \
    create/update/copy/assign flows to eliminate the pub/sub race \
    against immediate follow-up calls like health-check.",
    params(("model_id" = String, Path, description = "LLModel id")),
    responses(
        (status = 200, description = "Cache entries cleared"),
        (status = 401, description = "Missing tenant context"),
        (status = 404, description = "Model not found for tenant"),
    ),
    security(
        ("api_key" = []),
        ("bearer" = []),
    )
)]
pub(crate) async fn invalidate_model_cache(
    State(state): State<SharedState>,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Path(model_id): Path<String>,
) -> Response {
    let tenant_id = match tenant {
        Some(axum::Extension(ctx)) => ctx.tenant_id,
        None => {
            return error_response(StatusCode::UNAUTHORIZED, "tenant context required");
        }
    };
    // The control plane's route: the platform service key.
    if let Err(resp) =
        crate::services::service_auth::require_platform_key(&state, &headers, &tenant_id, None)
            .await
    {
        return resp;
    }

    // The invalidate/invalidate_tenant methods live on the
    // ResourceCache trait — bring it into scope so dot-notation
    // resolves to the right impl on ModelCache / ResourceGroupCache.
    use crate::services::cache::ResourceCache;

    // Drop the model entry for this tenant — next request will
    // reload from Postgres with the fresh row.
    state.model_cache.invalidate(&tenant_id, &model_id).await;

    // Group cache caches the tenant's effective-model lists. A model
    // update can shift those lists (groups added / removed, enabled
    // toggle, routing group membership), so flush the whole tenant
    // group cache too — cheap and keeps the next group lookup honest.
    state.group_cache.invalidate_tenant(&tenant_id).await;

    tracing::debug!(
        tenant_id = %tenant_id,
        model_id = %model_id,
        "Synchronous cache invalidation completed for model update"
    );

    (
        StatusCode::OK,
        Json(json!({
            "status": "invalidated",
            "model_id": model_id,
            "tenant_id": tenant_id,
            "cleared": ["model_cache", "group_cache"],
        })),
    )
        .into_response()
}

// ---------------------------------------------------------------------------
// GET /models — List models accessible to the authenticated user
// ---------------------------------------------------------------------------

#[utoipa::path(
    get,
    path = "/v1/proxy/llm/models",
    tag = "LLM",
    summary = "List available models (OpenAI-compatible)",
    description = "Returns the list of models available to the authenticated user based on their resource group assignments. Compatible with the OpenAI GET /v1/models endpoint.",
    responses(
        (status = 200, description = "List of available models", body = Value),
        (status = 401, description = "Authentication required"),
    ),
    security(
        ("api_key" = []),
        ("bearer" = []),
    )
)]
pub(crate) async fn list_models(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
) -> Response {
    let tenant_id = match route_helpers::extract_tenant_id(tenant) {
        Ok(id) => id,
        Err(resp) => return resp,
    };

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
        return crate::services::identity_policy::refusal_response(
            &e,
            error_response(e.status_code(), &e.to_string()),
        )
        .await;
    }

    // Resolve which resource groups the user belongs to
    // An unreadable membership list is a 503, not an empty catalogue.
    let group_ids = match resolve_user_group_ids(&state.db, &auth_ctx, &tenant_id).await {
        Ok(g) => g,
        Err(e) => return crate::services::db_errors::unavailable("your resource groups", &e),
    };

    if group_ids.is_empty() {
        return Json(json!({
            "object": "list",
            "data": []
        }))
        .into_response();
    }

    // Get all chat-capable models accessible through these groups.
    // Embedding and other non-chat models are excluded — they are used internally
    // (e.g., semantic cache) but should not appear as selectable models in the portal.
    //
    // Use EXISTS rather than `JOIN … SELECT DISTINCT m.*`: a model bound to
    // several of the user's groups would otherwise appear once per binding,
    // and `DISTINCT m.*` over the whole row fails on Postgres because
    // `llm_model` carries `json`-typed columns (e.g. `features`, `settings`)
    // and the `json` type has no equality operator (only `jsonb` does).
    // EXISTS yields at most one row per model, so no DISTINCT is needed.
    let models: Vec<crate::models::db::LlmModel> = match sqlx::query_as(concat!(
        "SELECT m.* FROM llm_model m WHERE m.tenant_id = $2 AND ",
        crate::model_available_sql!("m."),
        " AND (m.mode IS NULL OR m.mode IN ('chat', 'responses', 'completion')) \
             AND EXISTS ( \
                 SELECT 1 FROM resource_group_llm_models rglm \
                 WHERE rglm.llm_model_id = m.id AND rglm.resource_group_id = ANY($1) \
             ) \
             ORDER BY m.name ASC"
    ))
    .bind(&group_ids)
    .bind(&tenant_id)
    .fetch_all(&state.db)
    .await
    {
        Ok(rows) => rows,
        Err(e) => {
            // Don't silently return an empty model list on a query
            // failure — that masks the bug as "user has no models".
            tracing::error!(error = %e, "list_models: group-accessible model query failed");
            return error_response(
                axum::http::StatusCode::INTERNAL_SERVER_ERROR,
                "Failed to list models",
            );
        }
    };

    // Also include models from parent groups in the chain
    let mut all_model_ids: std::collections::HashSet<String> =
        models.iter().map(|m| m.id.clone()).collect();
    let mut all_models = models;

    // Walk parent chains for each group
    for gid in &group_ids {
        let parent_models = match get_inherited_models(&state.db, gid, &tenant_id).await {
            Ok(m) => m,
            Err(e) => return crate::services::db_errors::unavailable("inherited models", &e),
        };
        for m in parent_models {
            if all_model_ids.insert(m.id.clone()) {
                all_models.push(m);
            }
        }
    }

    all_models.sort_by(|a, b| a.name.cmp(&b.name));

    let data: Vec<Value> = all_models.iter().map(model_to_openai_object).collect();

    Json(json!({
        "object": "list",
        "data": data
    }))
    .into_response()
}

// ---------------------------------------------------------------------------
// GET /models/{model_id} — Get a specific model
// ---------------------------------------------------------------------------

#[utoipa::path(
    get,
    path = "/v1/proxy/llm/models/{model_id}",
    tag = "LLM",
    summary = "Retrieve a model (OpenAI-compatible)",
    description = "Returns details for a specific model if the authenticated user has access to it via their resource group. Compatible with the OpenAI GET /v1/models/{model} endpoint.",
    params(
        ("model_id" = String, Path, description = "Model ID or model name"),
    ),
    responses(
        (status = 200, description = "Model details", body = Value),
        (status = 401, description = "Authentication required"),
        (status = 404, description = "Model not found or not accessible"),
    ),
    security(
        ("api_key" = []),
        ("bearer" = []),
    )
)]
pub(crate) async fn get_model(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Path(model_id): Path<String>,
) -> Response {
    let tenant_id = match route_helpers::extract_tenant_id(tenant) {
        Ok(id) => id,
        Err(resp) => return resp,
    };

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
        return crate::services::identity_policy::refusal_response(
            &e,
            error_response(e.status_code(), &e.to_string()),
        )
        .await;
    }

    // Resolve the model by ID or name (slug stamped post-load).
    let model: Result<Option<crate::models::db::LlmModel>, sqlx::Error> = sqlx::query_as(concat!(
        "SELECT * FROM llm_model \
             WHERE (id = $1 OR model_name = $1 OR name = $1) AND tenant_id = $2 AND ",
        crate::model_available_sql!(""),
        " LIMIT 1"
    ))
    .bind(&model_id)
    .bind(&tenant_id)
    .fetch_optional(&state.db)
    .await;
    let model = match model {
        Ok(m) => m,
        Err(e) => return crate::services::db_errors::unavailable("the model", &e),
    };

    let mut model = match model {
        Some(m) => m,
        None => {
            return error_response(
                StatusCode::NOT_FOUND,
                &format!("Model '{model_id}' not found"),
            );
        }
    };
    state.provider_cache.stamp_one(&mut model).await;

    // Verify the user has access to this model via their resource groups
    let access =
        match usage::resolve_model_access(&state.db, &auth_ctx, &tenant_id, &model.id).await {
            Ok(a) => a,
            Err(e) => return crate::services::db_errors::unavailable("model access", &e),
        };

    if access.is_none() {
        return error_response(
            StatusCode::NOT_FOUND,
            &format!("Model '{model_id}' not found"),
        );
    }

    Json(model_to_openai_object(&model)).into_response()
}

// ---------------------------------------------------------------------------
// Model response helpers
// ---------------------------------------------------------------------------

/// Convert an `LlmModel` to the OpenAI `/v1/models` response object format.
fn model_to_openai_object(model: &crate::models::db::LlmModel) -> Value {
    json!({
        "id": model.model_name,
        "object": "model",
        "created": model.created_at.map(|t| t.timestamp()).unwrap_or(0),
        "owned_by": model.provider,
    })
}

/// Resolve all resource group IDs for the authenticated user.
async fn resolve_user_group_ids(
    db: &sqlx::PgPool,
    auth: &crate::models::auth::AuthContext,
    tenant_id: &str,
) -> Result<Vec<String>, sqlx::Error> {
    // API key with bound group
    if let Some(ref gid) = auth.api_key_group_id {
        return Ok(vec![gid.clone()]);
    }

    // Gateway user — get all group memberships, keyed by the integer user id.
    // `gateway_user_id` is only numeric for a real Gateway-JWT user or a
    // user-scoped API key. A GLOBAL API key gets a synthetic subject
    // (`api_key_<id>`); it has no user/end-user memberships, so skip the query
    // rather than feed a non-integer into the `$1::int` cast (which would error
    // and 500 the request). Group-scoped keys never reach here — they return
    // early via `api_key_group_id` above.
    let user_id: i32 = match auth.gateway_user_id.as_deref().and_then(|u| u.parse().ok()) {
        Some(uid) => uid,
        None => return Ok(vec![]),
    };

    // Union direct + EndUserGroup-mediated access.
    let groups: Vec<(String,)> = sqlx::query_as(
        r#"SELECT rg.id
           FROM resource_groups rg
           WHERE rg.tenant_id = $2
             AND rg.is_active = true
             AND rg.id IN (
                 SELECT group_id
                 FROM resource_group_memberships
                 WHERE user_id = $1::int
                   AND tenant_id = $2
                 UNION
                 SELECT rgeug.resource_group_id
                 FROM resource_group_end_user_groups rgeug
                 JOIN end_user_group_members eugm
                   ON eugm.end_user_group_id = rgeug.end_user_group_id
                 WHERE eugm.end_user_id = $1::int
             )"#,
    )
    .bind(user_id)
    .bind(tenant_id)
    .fetch_all(db)
    .await?;

    Ok(groups.into_iter().map(|(id,)| id).collect())
}

/// Get models inherited from parent groups in the hierarchy chain.
async fn get_inherited_models(
    db: &sqlx::PgPool,
    group_id: &str,
    tenant_id: &str,
) -> Result<Vec<crate::models::db::LlmModel>, sqlx::Error> {
    // Walk the parent chain (max 10 levels to prevent infinite loops)
    let mut current_id = group_id.to_string();
    let mut inherited = Vec::new();
    let mut seen_ids: std::collections::HashSet<String> = std::collections::HashSet::new();

    for _ in 0..10 {
        let parent: Option<(Option<String>,)> = sqlx::query_as(
            "SELECT parent_group_id FROM resource_groups WHERE id = $1 AND tenant_id = $2",
        )
        .bind(&current_id)
        .bind(tenant_id)
        .fetch_optional(db)
        .await?;

        match parent {
            Some((Some(pid),)) => {
                // Get chat-capable models from this parent group
                let models: Vec<crate::models::db::LlmModel> = sqlx::query_as(concat!(
                    "SELECT m.* FROM llm_model m \
                         JOIN resource_group_llm_models rglm ON m.id = rglm.llm_model_id \
                         WHERE rglm.resource_group_id = $1 AND m.tenant_id = $2 AND ",
                    crate::model_available_sql!("m."),
                    " AND (m.mode IS NULL OR m.mode IN ('chat', 'responses', 'completion'))"
                ))
                .bind(&pid)
                .bind(tenant_id)
                .fetch_all(db)
                .await?;

                for m in models {
                    if seen_ids.insert(m.id.clone()) {
                        inherited.push(m);
                    }
                }

                current_id = pid;
            }
            _ => break,
        }
    }

    Ok(inherited)
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Thin alias around the shared canonical envelope helper. See
/// `services::error_envelope::canonical_error_response` for the
/// unwrap rules; this file kept the local name so the existing
/// call-sites (~30 in this route alone) don't need touching.
fn error_response(status: StatusCode, message: &str) -> Response {
    crate::services::error_envelope::canonical_error_response(status, message)
}

/// Classify an `LlmRouterError` as **downstream** (the provider failed —
/// network, timeout, 5xx, rate-limited by the provider) or **local**
/// (our own code, configuration, or a per-tenant limit). This matters
/// because only downstream failures should raise a system alert —
/// local ones are either normal control flow (caller hit their quota)
/// or our own bugs that belong to ERROR logs, not service alerts.
///
/// Returns `None` for local errors.
fn classify_router_error(err: &llm_router::LlmRouterError) -> Option<DownstreamReason> {
    use llm_router::LlmRouterError as E;
    match err {
        // Network/transport failure on the call to the upstream provider.
        E::UpstreamError(_) => Some(DownstreamReason {
            error_code: Some("upstream_transport".into()),
        }),
        E::UpstreamAmbiguous(_) => Some(DownstreamReason {
            error_code: Some("upstream_outcome_unknown".into()),
        }),
        // Provider returned a status code. Only 5xx (and the provider's
        // own 429) count as a service-level failure worth alerting on;
        // 4xx responses are caller problems (bad request, invalid model
        // options) and would spam alerts if we treated them as downstream.
        E::UpstreamHttpError { status, .. } => {
            if *status >= 500 || *status == 429 {
                Some(DownstreamReason {
                    error_code: Some(status.to_string()),
                })
            } else {
                None
            }
        }
        // All configured fallback models in the group are in cooldown.
        // This means every provider in the fallback set is currently
        // failing — definitively downstream.
        E::AllModelsBusy { .. } => Some(DownstreamReason {
            error_code: Some("all_models_busy".into()),
        }),
        // Local per-tenant limits, config issues, or bugs.
        E::NoModelSpecified
        | E::ModelNotFound(_)
        | E::DbError(_)
        | E::SerializationError(_)
        | E::RateLimitExceeded(_)
        | E::ConcurrencyLimitReached(_)
        // A policy refusal: nothing was sent upstream.
        | E::OutboundDenied(_) => None,
    }
}

struct DownstreamReason {
    error_code: Option<String>,
}

/// Error response for a downstream LLM provider failure (network error,
/// timeout, 5xx from the upstream API). Fires a `downstream_provider`
/// system alert **before** returning so the on-call engineer sees a
/// single deduped row regardless of how many requests hit the same
/// broken provider simultaneously.
///
/// The dedup key is keyed on `(tenant, provider, error_code)`, so
/// different failure modes of the same provider stay as separate
/// alerts (useful for triage) while repeated occurrences of the
/// same failure bump the counter on one row.
fn downstream_provider_error_response(
    state: &SharedState,
    tenant_id: &str,
    model_id: &str,
    model_name: &str,
    provider: &str,
    status: StatusCode,
    error_code: Option<String>,
    message: &str,
) -> Response {
    spawn_raise(
        state.db.clone(),
        SystemAlertInput::downstream_provider(
            Some(tenant_id.to_string()),
            provider,
            Some(model_id.to_string()),
            Some(model_name.to_string()),
            error_code,
            message.to_string(),
        ),
    );
    error_response(status, message)
}

/// Classify a `reqwest::Error` from a direct provider call. All of these
/// categories are *always* downstream — the request never reached the
/// provider's app logic (transport/connect/timeout) or the provider
/// itself responded with a network-level failure. Returning a code
/// keeps each failure mode a separate dedup row.
fn classify_reqwest_error(e: &reqwest::Error) -> String {
    if e.is_timeout() {
        "timeout".into()
    } else if e.is_connect() {
        "connect_refused".into()
    } else if e.is_status() {
        e.status()
            .map(|s| s.as_u16().to_string())
            .unwrap_or_else(|| "http_error".into())
    } else if e.is_decode() {
        "decode_error".into()
    } else if e.is_body() {
        "body_error".into()
    } else {
        "transport".into()
    }
}

/// Error response for a direct `reqwest` call to an LLM provider
/// (the `responses`, `completions`, and `embeddings` handlers don't go
/// through `llm_router`; they hit the provider themselves). All such
/// failures are downstream by construction — we'd already have bailed
/// out before this point if the problem were local.
fn reqwest_error_response(
    state: &SharedState,
    tenant_id: &str,
    model: &crate::models::db::LlmModel,
    err: &reqwest::Error,
) -> Response {
    downstream_provider_error_response(
        state,
        tenant_id,
        &model.id,
        &model.model_name,
        &model.provider,
        StatusCode::BAD_GATEWAY,
        Some(classify_reqwest_error(err)),
        &err.to_string(),
    )
}

/// Build an error response from an `LlmRouterError`, raising a downstream
/// alert iff the error is classified as downstream. Replaces the bare
/// `error_response(BAD_GATEWAY, ...)` calls that silently handed every
/// failure mode back to the caller with zero operator visibility.
fn router_error_response(
    state: &SharedState,
    tenant_id: &str,
    model: &crate::models::db::LlmModel,
    err: &llm_router::LlmRouterError,
) -> Response {
    // Match existing mapping: upstream HTTP errors pass through the
    // provider's status; everything else becomes 502 BAD_GATEWAY or 429
    // for rate/concurrency issues. This keeps the HTTP contract stable.
    use llm_router::LlmRouterError as E;
    let status = match err {
        E::UpstreamHttpError { status, .. } => {
            StatusCode::from_u16(*status).unwrap_or(StatusCode::BAD_GATEWAY)
        }
        E::AllModelsBusy { .. } | E::RateLimitExceeded(_) | E::ConcurrencyLimitReached(_) => {
            StatusCode::TOO_MANY_REQUESTS
        }
        _ => StatusCode::BAD_GATEWAY,
    };

    match classify_router_error(err) {
        Some(reason) => downstream_provider_error_response(
            state,
            tenant_id,
            &model.id,
            &model.model_name,
            &model.provider,
            status,
            reason.error_code,
            &err.to_string(),
        ),
        None => error_response(status, &err.to_string()),
    }
}

/// Finalise output governance for a STREAMED chat response that emitted tool
/// calls. Runs the SAME `apply_response_governance` as the non-streaming path
/// (banned-argument patterns, argument policies, per-group tool-call rate
/// counters) on the reconstructed tool calls, then either RELEASES the held
/// finish chunk (governance passed — the client now sees finish_reason and may
/// act on the tool call) or emits an `error` event and drops it (blocked).
///
/// Returns `true` when the response was blocked. No-op (false) when no tool
/// calls were accumulated. We hold only the FINISH chunk, not every argument
/// delta: well-behaved SDKs act on a tool call only once `finish_reason`
/// arrives, so withholding it until governance passes blocks execution without
/// buffering the whole stream. This closes the gap where streamed tool calls
/// bypassed governance entirely.
#[allow(clippy::too_many_arguments)]
async fn finalize_streamed_tool_governance(
    tx: &tokio::sync::mpsc::Sender<Result<axum::response::sse::Event, axum::Error>>,
    state: &SharedState,
    target_group: &Option<crate::models::db::ResourceGroup>,
    model_id: &str,
    tool_acc: &crate::services::stream_guard::ToolCallAccumulator,
    held_finish_chunk: &mut Option<String>,
    finish_reason: Option<&str>,
    scope: &RefusalScope<'_>,
) -> bool {
    if tool_acc.is_empty() {
        return false;
    }
    let correlation_id = scope.correlation_id;
    let reconstructed = tool_acc.to_response_body(finish_reason);
    let blocked = llm_preflight::apply_response_governance(
        state,
        target_group,
        model_id,
        &reconstructed,
        error_response,
        scope,
    )
    .await
    .is_err();

    if blocked {
        tracing::warn!(
            correlation_id = %correlation_id,
            "Streamed tool-call response blocked by output governance"
        );
        let err = serde_json::json!({
            "error": {
                "type": "guardrail_block",
                "message": "Response blocked by output governance policy"
            }
        });
        let _ = tx
            .send(Ok(axum::response::sse::Event::default()
                .event("error")
                .data(err.to_string())))
            .await;
        // Drop the held finish chunk — the client must NOT receive
        // finish_reason (and thus must not execute the tool) on a block.
        held_finish_chunk.take();
        return true;
    }

    // Passed — release the held finish chunk so the client sees finish_reason.
    if let Some(chunk) = held_finish_chunk.take() {
        let _ = tx
            .send(Ok(axum::response::sse::Event::default().data(chunk)))
            .await;
    }
    false
}

/// Ingest a streamed chunk into the tool-call accumulator and, if it is the
/// terminal (`finish_reason`) chunk of a tool-call response, HOLD it (returns
/// `true` so the caller does NOT forward it yet). The held finish chunk is
/// released — or replaced with an error — by
/// [`finalize_streamed_tool_governance`] once governance has run. Non-tool
/// chunks are never held (returns `false`).
fn ingest_and_maybe_hold_finish(
    chunk_str: &str,
    tool_acc: &mut crate::services::stream_guard::ToolCallAccumulator,
    held_finish_chunk: &mut Option<String>,
) -> bool {
    tool_acc.ingest_chunk_str(chunk_str);
    if tool_acc.is_empty() {
        return false;
    }
    let has_finish = serde_json::from_str::<Value>(chunk_str)
        .ok()
        .and_then(|v| {
            v.get("choices")
                .and_then(|c| c.get(0))
                .and_then(|c| c.get("finish_reason"))
                .map(|fr| !fr.is_null())
        })
        .unwrap_or(false);
    if has_finish {
        *held_finish_chunk = Some(chunk_str.to_string());
        true
    } else {
        false
    }
}

/// Extract content, finish_reason, and usage from an OpenAI-format streaming chunk.
/// Appends text directly to `accumulated` to avoid Vec<String> reallocation overhead.
fn extract_stream_chunk_data(
    data: &str,
    accumulated: &mut String,
    finish_reason: &mut Option<String>,
    prompt_tokens: &mut Option<i32>,
    completion_tokens: &mut Option<i32>,
    cache_write_tokens: &mut i32,
    cache_read_tokens: &mut i32,
) {
    let chunk = match serde_json::from_str::<Value>(data) {
        Ok(v) => v,
        Err(_) => return,
    };

    let first_choice = chunk.get("choices").and_then(|c| c.get(0));

    // Assistant content + tool-call text → the accumulation buffer.
    crate::services::stream_guard::append_chunk_loggable_text_value(&chunk, accumulated);

    // Finish reason
    if let Some(fr) = first_choice
        .and_then(|c| c.get("finish_reason"))
        .and_then(|f| f.as_str())
    {
        *finish_reason = Some(fr.to_string());
    }

    // Usage (OpenAI includes this in final chunk with stream_options.include_usage).
    // We also pick up prompt-cache counters so Mission Control can tell
    // "cache not supported" apart from "supported but not hitting":
    //   * Anthropic (via our stream normalizer) forwards
    //     `usage.cache_creation_input_tokens` (write) and
    //     `usage.cache_read_input_tokens` (read) on the final delta.
    //   * OpenAI emits `usage.prompt_tokens_details.cached_tokens`
    //     (read only — writes are free and invisible on OpenAI).
    // max() is used because the counters can appear on multiple
    // events during a stream; we want the final figure, not a sum.
    if let Some(usage) = chunk.get("usage") {
        if let Some(pt) = usage.get("prompt_tokens").and_then(|v| v.as_i64()) {
            *prompt_tokens = Some(pt as i32);
        }
        if let Some(ct) = usage.get("completion_tokens").and_then(|v| v.as_i64()) {
            *completion_tokens = Some(ct as i32);
        }
        if let Some(cw) = usage
            .get("cache_creation_input_tokens")
            .and_then(|v| v.as_i64())
        {
            *cache_write_tokens = (*cache_write_tokens).max(cw as i32);
        }
        if let Some(cr) = usage
            .get("cache_read_input_tokens")
            .and_then(|v| v.as_i64())
        {
            *cache_read_tokens = (*cache_read_tokens).max(cr as i32);
        }
        if let Some(cr) = usage
            .get("prompt_tokens_details")
            .and_then(|d| d.get("cached_tokens"))
            .and_then(|v| v.as_i64())
        {
            *cache_read_tokens = (*cache_read_tokens).max(cr as i32);
        }
    }
}

// ---------------------------------------------------------------------------
// Governance / output-guardrail surface adapters for /responses + /completions
// ---------------------------------------------------------------------------
//
// G6/G9 reuse the SAME helpers the chat path uses
// (`apply_governance`, `apply_response_governance`) — those operate on
// the chat-completion JSON shape (`messages[]`,
// `choices[].message.tool_calls`). The Responses / Completions surfaces
// carry their text in different fields, so these adapters build a
// chat-shaped *view* of the request/response, run the unchanged helper
// over the view, then splice the governed result back into the native
// shape. No helper is forked — only the text/tool-call extraction is
// adapted per the task scope notes.

/// Build a chat-completion request *view* from a `ResponsesRequest` so
/// `apply_governance` (RG param caps + tool filtering + banned-pattern +
/// mandatory system-prompt injection) can run unchanged.
///
/// Mapping:
///   * `input` (string | array<turn>) → `messages[]` (same shape
///     `convert_responses_to_chat` uses).
///   * `instructions` (Responses native system slot, in `extra`) →
///     leading `system` message so mandatory-fragment injection merges
///     with it.
///   * `max_output_tokens` → `max_tokens` (the cap the governance reads).
///   * `temperature` / `top_p` / `tools` / `tool_choice` (in `extra`) →
///     the typed `ChatCompletionRequest` fields so the caps + tool
///     filters apply.
fn responses_request_to_chat_view(
    request: &ResponsesRequest,
) -> crate::models::proxy::ChatCompletionRequest {
    use crate::models::proxy::{ChatCompletionRequest, ChatMessage};

    let mut messages: Vec<ChatMessage> = Vec::new();
    // instructions → leading system message
    if let Some(instr) = request.extra.get("instructions").and_then(|v| v.as_str()) {
        messages.push(ChatMessage {
            role: "system".to_string(),
            content: Some(Value::String(instr.to_string())),
            name: None,
            tool_calls: None,
            tool_call_id: None,
        });
    }
    match &request.input {
        Value::String(s) => messages.push(ChatMessage {
            role: "user".to_string(),
            content: Some(Value::String(s.clone())),
            name: None,
            tool_calls: None,
            tool_call_id: None,
        }),
        Value::Array(arr) => {
            for item in arr {
                if let Some(role) = item.get("role").and_then(|r| r.as_str()) {
                    messages.push(ChatMessage {
                        role: role.to_string(),
                        content: item.get("content").cloned(),
                        name: None,
                        tool_calls: None,
                        tool_call_id: None,
                    });
                } else if let Some(s) = item.as_str() {
                    messages.push(ChatMessage {
                        role: "user".to_string(),
                        content: Some(Value::String(s.to_string())),
                        name: None,
                        tool_calls: None,
                        tool_call_id: None,
                    });
                }
            }
        }
        _ => {}
    }

    ChatCompletionRequest {
        model: request.model.clone(),
        model_id: request.model_id.clone(),
        messages,
        temperature: request.extra.get("temperature").and_then(|v| v.as_f64()),
        max_tokens: request.max_output_tokens,
        stream: request.stream,
        top_p: request.extra.get("top_p").and_then(|v| v.as_f64()),
        n: None,
        stop: None,
        presence_penalty: None,
        frequency_penalty: None,
        tools: request.extra.get("tools").cloned(),
        tool_choice: request.extra.get("tool_choice").cloned(),
        extra: serde_json::Map::new(),
    }
}

/// Splice the governed chat-view fields back into the native
/// `ResponsesRequest` after `apply_governance` mutated the view.
///
/// Carries back: clamped `temperature` / `top_p`, clamped
/// `max_output_tokens` (from the view's `max_tokens`), the filtered
/// `tools` / `tool_choice`, and the (possibly mandatory-fragment-
/// injected) system prompt → `instructions` (the Responses native
/// system slot). The first `system` message in the governed view is the
/// authoritative instructions value.
fn apply_responses_chat_view_back(
    request: &mut ResponsesRequest,
    view: &crate::models::proxy::ChatCompletionRequest,
) {
    if let Some(t) = view.temperature {
        request.extra.insert("temperature".to_string(), json!(t));
    }
    if let Some(tp) = view.top_p {
        request.extra.insert("top_p".to_string(), json!(tp));
    }
    // max_tokens (view) → max_output_tokens (native). A clamp lowers it.
    if let Some(mt) = view.max_tokens {
        request.max_output_tokens = Some(mt);
    }
    // Tool filtering: a `None` here means governance stripped every tool.
    match &view.tools {
        Some(tools) => {
            request.extra.insert("tools".to_string(), tools.clone());
        }
        None => {
            request.extra.remove("tools");
            request.extra.remove("tool_choice");
        }
    }
    if let Some(tc) = &view.tool_choice {
        request.extra.insert("tool_choice".to_string(), tc.clone());
    }
    // Mandatory system-prompt fragments were injected into the view's
    // leading system message → write it back to `instructions`.
    if let Some(sys) = view
        .messages
        .iter()
        .find(|m| m.role == "system")
        .and_then(|m| m.content.as_ref())
        .and_then(|c| c.as_str())
    {
        if !sys.is_empty() {
            request
                .extra
                .insert("instructions".to_string(), Value::String(sys.to_string()));
        }
    }
}

/// Normalize a `/responses` upstream response into a chat-shaped
/// `Value` (`choices[0].message.tool_calls[]`) so the unchanged
/// `apply_response_governance` (banned-arg patterns + AST argument
/// policies + tool-call rate counters) can scan emitted tool calls.
///
/// Responses-API tool calls are `output[]` items of
/// `type == "function_call"` with top-level `name` + `arguments`
/// (a JSON-encoded string). We re-shape them into the chat
/// `choices[].message.tool_calls[].function.{name,arguments}` form the
/// governance readers expect. Returns `None` when the body is already
/// chat-shaped (non-Responses upstream) — the caller passes the raw
/// body in that case.
fn responses_output_to_chat_tool_view(resp_body: &Value) -> Option<Value> {
    let output = resp_body.get("output").and_then(|o| o.as_array())?;
    let tool_calls: Vec<Value> = output
        .iter()
        .filter(|i| i.get("type").and_then(|t| t.as_str()) == Some("function_call"))
        .map(|i| {
            json!({
                "id": i.get("call_id").or_else(|| i.get("id")).cloned().unwrap_or(Value::Null),
                "type": "function",
                "function": {
                    "name": i.get("name").cloned().unwrap_or(Value::Null),
                    "arguments": i.get("arguments").cloned().unwrap_or(Value::Null),
                }
            })
        })
        .collect();
    Some(json!({
        "choices": [{
            "index": 0,
            "message": { "role": "assistant", "tool_calls": tool_calls }
        }]
    }))
}

/// Extract the assistant text emitted by a `/responses` upstream,
/// per top-level message item, for output-guardrail scanning.
///
/// Returns `(item_index, content_index, text)` triples so a redaction
/// can be spliced back into the exact `output[i].content[j].text` leaf.
/// Handles both the structured shape (`output[].content[].text` on
/// items of `type == "message"`) and the `output_text` convenience
/// string (mapped to a synthetic location the splicer recognises).
fn extract_responses_output_text(resp_body: &Value) -> Vec<(usize, usize, String)> {
    let mut out = Vec::new();
    if let Some(items) = resp_body.get("output").and_then(|o| o.as_array()) {
        for (i, item) in items.iter().enumerate() {
            if item.get("type").and_then(|t| t.as_str()) != Some("message") {
                continue;
            }
            if let Some(parts) = item.get("content").and_then(|c| c.as_array()) {
                for (j, part) in parts.iter().enumerate() {
                    // output_text parts carry the assistant text.
                    if let Some(txt) = part.get("text").and_then(|t| t.as_str()) {
                        if !txt.is_empty() {
                            out.push((i, j, txt.to_string()));
                        }
                    }
                }
            }
        }
    }
    out
}

/// Splice a redacted string back into `output[i].content[j].text`.
fn splice_responses_output_text(resp_body: &mut Value, i: usize, j: usize, new_text: String) {
    if let Some(slot) = resp_body.pointer_mut(&format!("/output/{i}/content/{j}/text")) {
        *slot = Value::String(new_text);
    }
}

/// Build a chat-completion request *view* from a legacy
/// `CompletionRequest` so `apply_governance` (RG param caps +
/// banned-pattern) can run unchanged.
///
/// The legacy completion surface is prompt-only — no message array, no
/// tools, no system slot. We map `prompt` → a single `user` message so
/// the banned-pattern user-message scan and temperature/max_tokens caps
/// apply. Mandatory system-prompt injection is intentionally N/A here:
/// the shape has nowhere to carry a system role, and the helper only
/// injects a system message into a `messages[]` array we don't surface
/// back to the prompt-only wire format.
fn completion_request_to_chat_view(
    request: &CompletionRequest,
) -> crate::models::proxy::ChatCompletionRequest {
    use crate::models::proxy::{ChatCompletionRequest, ChatMessage};
    ChatCompletionRequest {
        model: request.model.clone(),
        model_id: request.model_id.clone(),
        messages: vec![ChatMessage {
            role: "user".to_string(),
            content: Some(Value::String(request.prompt.clone())),
            name: None,
            tool_calls: None,
            tool_call_id: None,
        }],
        temperature: request.temperature,
        max_tokens: request.max_tokens,
        stream: request.stream,
        top_p: request.extra.get("top_p").and_then(|v| v.as_f64()),
        n: None,
        stop: None,
        presence_penalty: None,
        frequency_penalty: None,
        tools: None,
        tool_choice: None,
        extra: serde_json::Map::new(),
    }
}

/// Convert a chat completion response back to text completion format.
/// Used when we converted a text completion prompt to chat format for non-OpenAI providers.
fn convert_chat_response_to_completion(chat_resp: &Value) -> Value {
    let text = chat_resp
        .get("choices")
        .and_then(|c| c.get(0))
        .and_then(|c| c.get("message"))
        .and_then(|m| m.get("content"))
        .and_then(|c| c.as_str())
        .unwrap_or("");

    let finish_reason = chat_resp
        .get("choices")
        .and_then(|c| c.get(0))
        .and_then(|c| c.get("finish_reason"))
        .cloned()
        .unwrap_or(Value::String("stop".into()));

    json!({
        "id": chat_resp.get("id").cloned().unwrap_or(Value::String("cmpl-proxy".into())),
        "object": "text_completion",
        "created": chat_resp.get("created").cloned().unwrap_or(Value::from(0)),
        "model": chat_resp.get("model").cloned().unwrap_or_default(),
        "choices": [{
            "text": text,
            "index": 0,
            "logprobs": null,
            "finish_reason": finish_reason,
        }],
        "usage": chat_resp.get("usage").cloned().unwrap_or_default(),
    })
}

/// Convert a Responses API request to chat completion format for non-OpenAI providers.
fn convert_responses_to_chat(body: &mut Value) {
    let obj = match body.as_object_mut() {
        Some(o) => o,
        None => return,
    };

    // input → messages
    if let Some(input) = obj.remove("input") {
        let messages = match &input {
            Value::String(s) => {
                serde_json::json!([{"role": "user", "content": s}])
            }
            Value::Array(arr) => {
                // Already in message format or needs conversion
                let mut msgs = Vec::new();
                for item in arr {
                    if item.get("role").is_some() {
                        // Already a message object
                        msgs.push(item.clone());
                    } else if let Some(s) = item.as_str() {
                        msgs.push(serde_json::json!({"role": "user", "content": s}));
                    } else {
                        msgs.push(item.clone());
                    }
                }
                Value::Array(msgs)
            }
            _ => serde_json::json!([{"role": "user", "content": input.to_string()}]),
        };
        obj.insert("messages".into(), messages);
    }

    // instructions → system message (prepended)
    if let Some(instructions) = obj.remove("instructions") {
        if let Some(instr_str) = instructions.as_str() {
            if let Some(messages) = obj.get_mut("messages").and_then(|m| m.as_array_mut()) {
                messages.insert(
                    0,
                    serde_json::json!({"role": "system", "content": instr_str}),
                );
            }
        }
    }

    // max_output_tokens → max_tokens
    if let Some(mot) = obj.remove("max_output_tokens") {
        if !obj.contains_key("max_tokens") {
            obj.insert("max_tokens".into(), mot);
        }
    }

    // text_format → response_format
    if let Some(tf) = obj.remove("text_format") {
        if !obj.contains_key("response_format") {
            obj.insert("response_format".into(), tf);
        }
    }

    // Remove Responses-API-only params that chat completions doesn't understand
    obj.remove("store");
    obj.remove("truncation");
    obj.remove("previous_response_id");
    obj.remove("reasoning");
}

// ---------------------------------------------------------------------------
// Catalog-probe admin route — POST /admin/{model_id}/probe-catalog
// ---------------------------------------------------------------------------
//
// Fires a free, honest health check against the upstream provider's
// /models endpoint. Verifies API key validity + reachability + that
// the configured model_name is in the provider's catalog. Replaces
// the synthetic-validation-400 trick the Python health checker used
// for image / video / TTS / STT, which only proved the proxy's own
// validation worked and never reached the upstream.

#[utoipa::path(
    post,
    path = "/v1/proxy/llm/admin/{model_id}/probe-catalog",
    tag = "LLM",
    summary = "Catalog-probe health check",
    description = "Hits the provider's /models endpoint with the model's API key and verifies the configured model_name appears. Used by the Python health checker. Returns a structured CatalogProbeResult.",
    params(
        ("model_id" = String, Path, description = "LLModel id"),
    ),
    responses(
        (status = 200, description = "Probe ran (whether or not the provider succeeded)", body = Value),
        (status = 401, description = "Tenant context required"),
        (status = 404, description = "Model not found for this tenant"),
        (status = 422, description = "Provider has no models_api_url configured (caller should fall back to real-inference probe)"),
    ),
    security(("api_key" = []), ("bearer" = []))
)]
#[tracing::instrument(
    name = "admin.probe_catalog",
    skip_all,
    fields(model_id = %model_id, tenant_id = tracing::field::Empty)
)]
pub(crate) async fn probe_catalog(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Path(model_id): Path<String>,
) -> Response {
    let tenant_id = match tenant.as_ref() {
        Some(axum::Extension(ctx)) => ctx.tenant_id.clone(),
        None => {
            return error_response(StatusCode::UNAUTHORIZED, "tenant context required");
        }
    };
    tracing::Span::current().record("tenant_id", tracing::field::display(&tenant_id));
    // The control plane's route: the platform service key.
    // Unauthenticated, it made the gateway call a provider's catalogue with
    // the tenant's stored key for anyone who named the tenant and a model.
    if let Err(resp) = crate::services::service_auth::require_platform_key(
        &state,
        &headers,
        &tenant_id,
        client_addr.ip(),
    )
    .await
    {
        return resp;
    }

    // Single round-trip JOIN: model row + provider's models_api_url
    // and key. Tenant-scoped — the row either exists for this tenant
    // or it doesn't, no separate existence pre-check.
    let row_opt: Option<(
        String,
        Option<String>,
        Option<String>,
        String,
        Option<String>,
    )> = match sqlx::query_as(
        r#"SELECT m.model_name,
                      m.api_url,
                      m.api_key,
                      p.key as provider_key,
                      p.models_api_url
               FROM llm_model m
               LEFT JOIN llm_providers p ON p.id = m.provider_id
               WHERE m.id = $1 AND m.tenant_id = $2"#,
    )
    .bind(&model_id)
    .bind(&tenant_id)
    .fetch_optional(&state.db)
    .await
    {
        Ok(r) => r,
        Err(e) => {
            tracing::error!(model_id = %model_id, "probe_catalog DB error: {e}");
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": format!("db error: {e}")})),
            )
                .into_response();
        }
    };
    let (model_name, model_api_url, encrypted_key, provider_key, models_api_url) = match row_opt {
        Some(r) => r,
        None => {
            return (
                StatusCode::NOT_FOUND,
                Json(json!({"error": format!("model {model_id} not found for this tenant")})),
            )
                .into_response();
        }
    };

    // Decrypt the model's api_key. Same wrapper the LLM router
    // uses, so failures behave consistently (blank → no auth
    // header rather than ciphertext-as-bearer).
    let decrypted_key: Option<String> = encrypted_key.and_then(|ct| {
        if ct.is_empty() {
            return None;
        }
        match state.encryption.decrypt(&ct) {
            Ok(plain) => Some(plain),
            Err(e) => {
                tracing::warn!(model_id = %model_id, "probe_catalog: api_key decrypt failed: {e}");
                None
            }
        }
    });

    let inputs = crate::services::catalog_probe::CatalogProbeInputs {
        provider_key: provider_key.clone(),
        models_api_url: models_api_url.clone().unwrap_or_default(),
        model_api_url: model_api_url.clone(),
        model_name: Some(model_name.clone()),
        api_key: decrypted_key,
    };

    let result = match crate::services::catalog_probe::run(&state.http_client, &inputs).await {
        Ok(r) => r,
        Err(crate::services::catalog_probe::CatalogProbeError::NoCatalogUrl) => {
            return (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({
                    "error": "no_catalog_url",
                    "detail": "Provider has no models_api_url configured. Caller should fall back to a real-inference probe.",
                    "provider_key": provider_key.clone(),
                })),
            )
                .into_response();
        }
        Err(crate::services::catalog_probe::CatalogProbeError::SentinelUnresolvable) => {
            return (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({
                    "error": "sentinel_unresolvable",
                    "detail": "Provider catalog URL uses <MODEL_API_URL> sentinel but the model row's api_url is unset.",
                    "provider_key": provider_key.clone(),
                })),
            )
                .into_response();
        }
    };

    // Probe accounting (proxy_metrics + usage_logs + model_metrics)
    // is the gateway-control's responsibility — it posts the
    // outcome to `record-probe-result` after this handler returns.
    // We deliberately do NOT write counters here; doing so caused
    // double-counting and inconsistent per-view tallies in the
    // first iteration of this work. See
    // `services::catalog_probe::record_probe_outcome` for the
    // single source of truth.
    let _ = client_addr;
    let _ = headers;

    (
        StatusCode::OK,
        Json(serde_json::to_value(&result).unwrap_or(json!({}))),
    )
        .into_response()
}

// ---------------------------------------------------------------------------
// Probe-outcome recording — POST /admin/{model_id}/record-probe-result
// ---------------------------------------------------------------------------
//
// Single-source-of-truth probe accounting. Gateway-control runs the
// probe (via probe-catalog, the embedding endpoint, chat completions,
// audio_speech, audio_transcribe, images, video, etc.) and posts the
// outcome here. This handler writes EXACTLY ONCE to all three
// counters that feed the operator-facing views:
//
//   * proxy_metrics      → Mission Control "Total Requests"
//   * usage_logs         → per-group Usage view
//   * model_metrics      → per-model row "Requests" column
//
// Before this endpoint existed, each probe path tried to write
// metrics from its own success/failure branches, and the failure
// paths (no api_key, circuit-open, fallback-rejected, synthetic 400)
// had inconsistent coverage — operators saw 8/8/8/0/0/0/7/7 across
// rows that all received the same number of probes. Now: one probe
// → one record-probe call → +1 in every view, every time.
//
// Existing route metric writes are skipped on probe traffic via
// `services::catalog_probe::is_health_probe_request(&headers, &auth)` —
// the `X-Brutor-Internal-Health-Check` header gateway-control
// already stamps. So this single record-probe path is the only
// place probes count, no double-counting from routes.

#[derive(serde::Deserialize, utoipa::ToSchema)]
pub(crate) struct RecordProbeResultBody {
    pub success: bool,
    pub status_code: u16,
    pub latency_ms: f64,
    /// One of `"catalog"`, `"endpoint"`, `"synthetic"`. Drives the
    /// `endpoint_category` tag on the proxy_metric row so the
    /// per-category breakdown attributes probe traffic correctly.
    pub probe_type: String,
}

#[utoipa::path(
    post,
    path = "/v1/proxy/llm/admin/{model_id}/record-probe-result",
    tag = "LLM",
    summary = "Record a probe outcome",
    description = "Centralized probe accounting — writes proxy_metrics + usage_logs + model_metrics exactly once for the supplied model_id. Called by gateway-control after every health probe.",
    params(("model_id" = String, Path, description = "LLModel id")),
    request_body = RecordProbeResultBody,
    responses(
        (status = 200, description = "Recorded"),
        (status = 401, description = "Tenant context required"),
        (status = 404, description = "Model not found for this tenant"),
    ),
    security(("api_key" = []), ("bearer" = []))
)]
#[tracing::instrument(
    name = "admin.record_probe_result",
    skip_all,
    fields(model_id = %model_id, tenant_id = tracing::field::Empty)
)]
pub(crate) async fn record_probe_result(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Path(model_id): Path<String>,
    Json(body): Json<RecordProbeResultBody>,
) -> Response {
    let tenant_id = match tenant.as_ref() {
        Some(axum::Extension(ctx)) => ctx.tenant_id.clone(),
        None => {
            return error_response(StatusCode::UNAUTHORIZED, "tenant context required");
        }
    };
    tracing::Span::current().record("tenant_id", tracing::field::display(&tenant_id));

    // Lookup model + provider so the metric/usage rows carry the
    // right model_name and provider slug (consumers downstream
    // filter by both).
    let row_opt: Option<(String, String)> = match sqlx::query_as(
        r#"SELECT m.model_name, COALESCE(p.key, '') as provider_key
           FROM llm_model m
           LEFT JOIN llm_providers p ON p.id = m.provider_id
           WHERE m.id = $1 AND m.tenant_id = $2"#,
    )
    .bind(&model_id)
    .bind(&tenant_id)
    .fetch_optional(&state.db)
    .await
    {
        Ok(r) => r,
        Err(e) => {
            tracing::error!(model_id = %model_id, "record_probe_result DB error: {e}");
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": format!("db error: {e}")})),
            )
                .into_response();
        }
    };
    let (model_name, provider_key) = match row_opt {
        Some(r) => r,
        None => {
            return (
                StatusCode::NOT_FOUND,
                Json(json!({"error": format!("model {model_id} not found for this tenant")})),
            )
                .into_response();
        }
    };

    // Resolve the auth context + target group via the same path
    // every other endpoint uses. The probe header doesn't matter
    // here (this IS the probe-recording endpoint); we want the
    // standard auth → group resolution so the usage_log lands in
    // the right group.
    let mut auth = match crate::services::auth::authenticate_request(
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
        Err(e) => {
            tracing::warn!(model_id = %model_id, "record_probe_result auth failed: {e}");
            return error_response(StatusCode::UNAUTHORIZED, "authentication failed");
        }
    };
    if let Err(e) = crate::services::identity_policy::judge_call(&state, &mut auth).await {
        return crate::services::identity_policy::refusal_response(
            &e,
            error_response(e.status_code(), &e.to_string()),
        )
        .await;
    }
    // Probe results feed a model's health counters and the usage
    // ledger. Only the platform's own prober may write them: any other key
    // could fake a model's health or write usage rows for it.
    if !auth.api_key_is_platform {
        // Written through the one refusal writer: a tenant key trying to fake
        // a model's health is exactly what a security review needs to find.
        let msg = "only the platform service key may record probe results";
        let correlation_id = uuid::Uuid::new_v4().to_string();
        return crate::services::llm_refusal::refuse(
            &state,
            &crate::services::llm_refusal::RefusalScope {
                auth_ctx: &auth,
                headers: &headers,
                correlation_id: &correlation_id,
                endpoint: "record_probe_result",
                started: None,
                model: None,
                group_id: auth.api_key_group_id.clone(),
                run: None,
                request_body: None,
            },
            crate::services::llm_refusal::Refusal::new(
                crate::constants::proxy_subtype::ACCESS_DENIED,
                msg,
            )
            .error_type("probe_result_forbidden"),
            error_response(StatusCode::FORBIDDEN, msg),
        );
    }
    let target_group =
        crate::services::usage::resolve_target_group(&state.db, &auth, &tenant_id).await;

    let correlation_id = uuid::Uuid::new_v4().to_string();
    crate::services::catalog_probe::record_probe_outcome(
        &state,
        &auth,
        target_group.as_ref(),
        &tenant_id,
        &model_id,
        &model_name,
        if provider_key.is_empty() {
            "unknown"
        } else {
            &provider_key
        },
        body.success,
        body.status_code,
        body.latency_ms,
        &body.probe_type,
        &correlation_id,
    );

    (StatusCode::OK, Json(json!({"recorded": true}))).into_response()
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    // `error_response` itself is now a thin alias around the
    // shared `services::error_envelope::canonical_error_response`.
    // Behaviour tests live with the implementation in that module.

    #[test]
    fn model_to_openai_object_basic() {
        let model = crate::models::db::LlmModel {
            model_name: "claude-sonnet-4-20250514".into(),
            provider: "anthropic".into(),
            created_at: Some(chrono::Utc.with_ymd_and_hms(2025, 5, 14, 0, 0, 0).unwrap()),
            ..Default::default()
        };

        let obj = model_to_openai_object(&model);
        assert_eq!(obj["id"], "claude-sonnet-4-20250514");
        assert_eq!(obj["object"], "model");
        assert_eq!(obj["owned_by"], "anthropic");
        assert!(obj["created"].as_i64().unwrap() > 0);
    }

    #[test]
    fn model_to_openai_object_no_created_at() {
        let model = crate::models::db::LlmModel {
            model_name: "gpt-4o".into(),
            provider: "openai".into(),
            created_at: None,
            ..Default::default()
        };

        let obj = model_to_openai_object(&model);
        assert_eq!(obj["id"], "gpt-4o");
        assert_eq!(obj["created"], 0);
    }

    #[test]
    fn model_to_openai_object_uses_model_name_not_display_name() {
        let model = crate::models::db::LlmModel {
            name: "My Custom Claude".into(),
            model_name: "claude-sonnet-4-20250514".into(),
            provider: "anthropic".into(),
            ..Default::default()
        };

        let obj = model_to_openai_object(&model);
        // OpenAI clients expect `id` to be the model identifier they pass in requests
        assert_eq!(obj["id"], "claude-sonnet-4-20250514");
    }

    #[test]
    fn list_models_response_format() {
        let models = [
            crate::models::db::LlmModel {
                model_name: "claude-sonnet-4-20250514".into(),
                provider: "anthropic".into(),
                ..Default::default()
            },
            crate::models::db::LlmModel {
                model_name: "gpt-4o".into(),
                provider: "openai".into(),
                ..Default::default()
            },
        ];

        let data: Vec<Value> = models.iter().map(model_to_openai_object).collect();
        let response = json!({
            "object": "list",
            "data": data
        });

        assert_eq!(response["object"], "list");
        let data = response["data"].as_array().unwrap();
        assert_eq!(data.len(), 2);
        assert_eq!(data[0]["id"], "claude-sonnet-4-20250514");
        assert_eq!(data[1]["id"], "gpt-4o");

        // Verify each entry has all required OpenAI fields
        for entry in data {
            assert!(entry.get("id").is_some());
            assert_eq!(entry["object"], "model");
            assert!(entry.get("created").is_some());
            assert!(entry.get("owned_by").is_some());
        }
    }

    #[test]
    fn empty_models_list_response() {
        let data: Vec<Value> = vec![];
        let response = json!({
            "object": "list",
            "data": data
        });

        assert_eq!(response["object"], "list");
        assert!(response["data"].as_array().unwrap().is_empty());
    }

    // ── Mode filtering tests ─────────────────────────────────────────
    // The /models endpoint must only return chat-capable models.
    // Embedding, image, and audio models must be excluded.

    /// Helper: simulate the SQL mode filter logic in Rust for unit testing.
    /// Matches: `AND (m.mode IS NULL OR m.mode IN ('chat', 'responses', 'completion'))`
    fn is_chat_mode(mode: &Option<String>) -> bool {
        matches!(
            mode.as_deref(),
            None | Some("chat") | Some("responses") | Some("completion")
        )
    }

    #[test]
    fn mode_filter_allows_chat() {
        assert!(is_chat_mode(&Some("chat".into())));
    }

    #[test]
    fn mode_filter_allows_responses() {
        assert!(is_chat_mode(&Some("responses".into())));
    }

    #[test]
    fn mode_filter_allows_completion() {
        assert!(is_chat_mode(&Some("completion".into())));
    }

    #[test]
    fn mode_filter_allows_null() {
        assert!(is_chat_mode(&None));
    }

    #[test]
    fn mode_filter_excludes_embedding() {
        assert!(!is_chat_mode(&Some("embedding".into())));
    }

    #[test]
    fn mode_filter_excludes_image() {
        assert!(!is_chat_mode(&Some("image".into())));
    }

    #[test]
    fn mode_filter_excludes_audio() {
        assert!(!is_chat_mode(&Some("audio".into())));
    }

    #[test]
    fn mode_filter_excludes_moderation() {
        assert!(!is_chat_mode(&Some("moderation".into())));
    }

    #[test]
    fn mode_filter_excludes_tts() {
        assert!(!is_chat_mode(&Some("tts".into())));
    }

    #[test]
    fn mode_filter_applied_to_model_list() {
        // Simulate a model list with mixed modes
        let all_models = [
            crate::models::db::LlmModel {
                model_name: "gpt-4o".into(),
                provider: "openai".into(),
                mode: Some("chat".into()),
                ..Default::default()
            },
            crate::models::db::LlmModel {
                model_name: "text-embedding-3-small".into(),
                provider: "openai".into(),
                mode: Some("embedding".into()),
                ..Default::default()
            },
            crate::models::db::LlmModel {
                model_name: "claude-sonnet-4".into(),
                provider: "anthropic".into(),
                mode: None, // legacy model with no mode set
                ..Default::default()
            },
            crate::models::db::LlmModel {
                model_name: "dall-e-3".into(),
                provider: "openai".into(),
                mode: Some("image".into()),
                ..Default::default()
            },
        ];

        let filtered: Vec<_> = all_models
            .iter()
            .filter(|m| is_chat_mode(&m.mode))
            .collect();

        assert_eq!(
            filtered.len(),
            2,
            "Should only include chat + legacy models"
        );
        assert_eq!(filtered[0].model_name, "gpt-4o");
        assert_eq!(filtered[1].model_name, "claude-sonnet-4");
    }

    #[test]
    fn embedding_model_never_in_models_list() {
        // Regression test: embedding models assigned to parent groups for
        // semantic cache must NOT appear in /models or /portal/models.
        let embedding = crate::models::db::LlmModel {
            model_name: "text-embedding-3-small".into(),
            provider: "openai".into(),
            mode: Some("embedding".into()),
            ..Default::default()
        };
        assert!(
            !is_chat_mode(&embedding.mode),
            "Embedding models must be filtered from /models endpoint"
        );
    }

    #[test]
    fn responses_mode_included() {
        // OpenAI Responses API uses mode: "responses"
        let model = crate::models::db::LlmModel {
            model_name: "gpt-4o".into(),
            provider: "openai".into(),
            mode: Some("responses".into()),
            ..Default::default()
        };
        assert!(
            is_chat_mode(&model.mode),
            "Responses API models must appear in /models"
        );
    }

    // ── /v1/responses provider routing ─────────────────────────────
    // The `responses()` handler routes to the upstream's native
    // /v1/responses endpoint for providers that accept the OpenAI
    // Responses request shape, and falls back to /v1/chat/completions
    // for everyone else. These tests pin which providers are on the
    // happy path, because forgetting to add a provider here regresses
    // grok-4.20 multi-agent (which hard-fails on chat completions) or,
    // in the other direction, would silently route a chat-only
    // provider to a 404 endpoint.

    #[test]
    fn provider_uses_responses_api_openai() {
        assert!(provider_uses_responses_api("openai"));
    }

    #[test]
    fn provider_uses_responses_api_azure() {
        assert!(provider_uses_responses_api("azure"));
        assert!(provider_uses_responses_api("azure_ai"));
    }

    #[test]
    fn provider_uses_responses_api_xai() {
        // grok-4.20-multi-agent-0309 only works on /v1/responses;
        // /v1/chat/completions returns "Multi Agent requests are not
        // allowed on chat completions".
        assert!(provider_uses_responses_api("xai"));
    }

    #[test]
    fn provider_uses_responses_api_excludes_chat_only_providers() {
        // Anthropic / Google / Mistral / etc. do not accept the
        // OpenAI Responses shape — they need the chat/completions
        // fallback path with body shape conversion.
        assert!(!provider_uses_responses_api("anthropic"));
        assert!(!provider_uses_responses_api("google_ai_studio"));
        assert!(!provider_uses_responses_api("mistral"));
        assert!(!provider_uses_responses_api("groq"));
        assert!(!provider_uses_responses_api("cohere"));
    }

    #[test]
    fn provider_uses_responses_api_unknown_provider_falls_back() {
        // Conservative default: any provider not on the explicit
        // allowlist routes through chat/completions.
        assert!(!provider_uses_responses_api(""));
        assert!(!provider_uses_responses_api("brand-new-provider"));
    }

    // ── Embedding endpoint tests ────────────────────────────────────

    #[test]
    fn embedding_request_deserializes_array() {
        let json = r#"{"model": "text-embedding-3-small", "input": ["hello", "world"]}"#;
        let req: EmbeddingRequest = serde_json::from_str(json).unwrap();
        assert_eq!(req.model.as_deref(), Some("text-embedding-3-small"));
        assert_eq!(req.input.as_array().unwrap().len(), 2);
    }

    #[test]
    fn embedding_request_with_model_id() {
        let json = r#"{"model_id": "llm-abc123", "input": ["test"]}"#;
        let req: EmbeddingRequest = serde_json::from_str(json).unwrap();
        assert!(req.model.is_none());
        assert_eq!(req.model_id.as_deref(), Some("llm-abc123"));
    }

    #[test]
    fn embedding_request_string_input() {
        // OpenAI also accepts a single string as input
        let json = r#"{"model": "text-embedding-3-small", "input": "single text"}"#;
        let req: EmbeddingRequest = serde_json::from_str(json).unwrap();
        assert!(req.input.is_string());
        assert_eq!(req.input.as_str().unwrap(), "single text");
    }

    #[test]
    fn embedding_request_optional_fields() {
        let json =
            r#"{"model": "m", "input": ["x"], "encoding_format": "float", "dimensions": 256}"#;
        let req: EmbeddingRequest = serde_json::from_str(json).unwrap();
        assert_eq!(req.encoding_format.as_deref(), Some("float"));
        assert_eq!(req.dimensions, Some(256));
    }

    #[test]
    fn embedding_batch_size_counts_inputs() {
        use serde_json::json;
        // Single string → one input.
        assert_eq!(super::embedding_batch_size(&json!("hello")), 1);
        // Array of strings → one per element.
        assert_eq!(super::embedding_batch_size(&json!(["a", "b", "c"])), 3);
        // A single pre-tokenized input (array of ints) → ONE input, not len.
        assert_eq!(super::embedding_batch_size(&json!([1, 2, 3, 4])), 1);
        // Batch of pre-tokenized inputs (array of arrays) → one per element.
        assert_eq!(super::embedding_batch_size(&json!([[1, 2], [3, 4]])), 2);
        // Empty array / null → zero.
        assert_eq!(super::embedding_batch_size(&json!([])), 0);
        assert_eq!(super::embedding_batch_size(&serde_json::Value::Null), 0);
    }

    #[test]
    fn embedding_model_is_embedding_mode() {
        // Verify embedding models have mode=embedding, which is used
        // for filtering in both /models endpoint and KB collection dropdown
        let model = crate::models::db::LlmModel {
            model_name: "text-embedding-3-small".into(),
            provider: "openai".into(),
            mode: Some("embedding".into()),
            ..Default::default()
        };

        // Must NOT appear in chat /models list
        assert!(!is_chat_mode(&model.mode));

        // But should be selectable as KB collection embedding model
        assert_eq!(model.mode.as_deref(), Some("embedding"));
    }

    #[test]
    fn embedding_model_with_cost_tracking() {
        // Verify the model struct supports cost fields for embedding usage tracking
        let model = crate::models::db::LlmModel {
            model_name: "text-embedding-3-small".into(),
            provider: "openai".into(),
            mode: Some("embedding".into()),
            cost_per_input_token: Some(0.00000002), // $0.02/1M tokens
            cost_per_output_token: None,            // Embeddings have no output tokens
            ..Default::default()
        };
        assert_eq!(model.cost_per_input_token, Some(0.00000002));
        assert!(model.cost_per_output_token.is_none());
    }

    // =====================================================================
    // Error classifiers — keep in sync with system_alerts wiring.
    //
    // These assertions are load-bearing: the whole alert taxonomy rests on
    // `classify_router_error` / `classify_reqwest_error` returning the
    // correct "is this downstream?" answer. A regression that quietly
    // flips a variant from Some(...) → None would make the alert
    // disappear for that failure mode, with no visible compile error.
    // =====================================================================

    use llm_router::LlmRouterError;

    #[test]
    fn classify_router_error_upstream_transport_is_downstream() {
        let c = classify_router_error(&LlmRouterError::UpstreamError("connection reset".into()));
        let c = c.expect("transport errors are downstream");
        assert_eq!(c.error_code.as_deref(), Some("upstream_transport"));
    }

    #[test]
    fn classify_router_error_5xx_is_downstream() {
        let c = classify_router_error(&LlmRouterError::UpstreamHttpError {
            status: 503,
            body: serde_json::json!({}),
        });
        assert_eq!(c.unwrap().error_code.as_deref(), Some("503"));
    }

    #[test]
    fn classify_router_error_429_is_downstream() {
        // Provider-side rate limiting means the provider itself is
        // degraded for this tenant — treat as downstream.
        let c = classify_router_error(&LlmRouterError::UpstreamHttpError {
            status: 429,
            body: serde_json::json!({}),
        });
        assert_eq!(c.unwrap().error_code.as_deref(), Some("429"));
    }

    #[test]
    fn classify_router_error_4xx_is_local() {
        // Caller sent a bad request (bad prompt shape, invalid model
        // option). Alerting on these would spam on every client bug.
        for status in [400u16, 401, 403, 404, 409, 422] {
            assert!(
                classify_router_error(&LlmRouterError::UpstreamHttpError {
                    status,
                    body: serde_json::json!({}),
                })
                .is_none(),
                "status {status} must NOT be classified as downstream"
            );
        }
    }

    #[test]
    fn classify_router_error_all_models_busy_is_downstream() {
        let c = classify_router_error(&LlmRouterError::AllModelsBusy {
            group_id: "g1".into(),
            group: "grp".into(),
            retry_after_secs: None,
        });
        assert_eq!(c.unwrap().error_code.as_deref(), Some("all_models_busy"));
    }

    #[test]
    fn classify_router_error_local_variants_return_none() {
        // These are all our-side failures, either config drift or a
        // caller hitting their own quota. None should raise a
        // downstream_provider alert — they either belong at ERROR
        // (NoModelSpecified / ModelNotFound / DbError / Serialization)
        // or are normal control flow (RateLimit / Concurrency).
        let cases: &[LlmRouterError] = &[
            LlmRouterError::NoModelSpecified,
            LlmRouterError::ModelNotFound("m-x".into()),
            LlmRouterError::DbError("oops".into()),
            LlmRouterError::SerializationError("bad json".into()),
            LlmRouterError::RateLimitExceeded("tenant limit".into()),
            LlmRouterError::ConcurrencyLimitReached("grp".into()),
        ];
        for err in cases {
            assert!(
                classify_router_error(err).is_none(),
                "{err:?} must be local, not downstream"
            );
        }
    }

    #[test]
    fn classify_reqwest_error_returns_stable_codes() {
        // We can't easily construct each `reqwest::Error` variant from
        // outside the crate, but we can exercise the real error types
        // via a URL builder + request. The critical invariant is:
        //
        // * every branch returns a non-empty, lowercase snake_case code
        // * none of the branches panic
        //
        // String value is pinned so a downstream DB migration keyed on
        // these codes stays stable.
        //
        // We go through a guaranteed-to-fail connect attempt to
        // produce a real `reqwest::Error` whose `is_connect()` is true.
        use tokio::runtime::Builder;
        let rt = Builder::new_current_thread().enable_all().build().unwrap();
        let err = rt.block_on(async {
            reqwest::Client::new()
                // Port 1 is reserved; connect must fail.
                .get("http://127.0.0.1:1/")
                .timeout(std::time::Duration::from_millis(50))
                .send()
                .await
                .expect_err("connect must fail")
        });
        let code = classify_reqwest_error(&err);
        // Must be one of the known codes — not the empty string, and
        // not one we didn't author.
        assert!(
            [
                "timeout",
                "connect_refused",
                "transport",
                "decode_error",
                "body_error",
                "http_error",
            ]
            .iter()
            .any(|&known| known == code),
            "unexpected classifier output: {code:?}"
        );
    }

    // ── Streaming prompt-cache accumulator (Gap #8) ────────────────
    //
    // The streaming path collects `cache_creation_input_tokens` /
    // `cache_read_input_tokens` (Anthropic) and
    // `prompt_tokens_details.cached_tokens` (OpenAI) from SSE
    // events. The route handler later turns those into proxy_logs
    // columns + Prometheus counters + sticky pin writes — all of
    // those depend on the totals being right. These tests pin the
    // accumulator to its expected behaviour.

    fn run_chunks(payloads: &[&str]) -> (i32, i32, Option<i32>, Option<i32>) {
        let mut acc = String::new();
        let mut fr: Option<String> = None;
        let mut pt: Option<i32> = None;
        let mut ct: Option<i32> = None;
        let mut cw = 0i32;
        let mut cr = 0i32;
        for p in payloads {
            extract_stream_chunk_data(p, &mut acc, &mut fr, &mut pt, &mut ct, &mut cw, &mut cr);
        }
        (cw, cr, pt, ct)
    }

    #[test]
    fn stream_anthropic_warm_chunk_is_captured() {
        // Anthropic emits cache tokens on the final delta with
        // `cache_creation_input_tokens` (write) on a warm call.
        let payload = serde_json::json!({
            "choices": [{"delta": {"content": ""}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 5000,
                "completion_tokens": 100,
                "cache_creation_input_tokens": 4500,
                "cache_read_input_tokens": 0
            }
        })
        .to_string();
        let (cw, cr, pt, ct) = run_chunks(&[&payload]);
        assert_eq!(cw, 4500);
        assert_eq!(cr, 0);
        assert_eq!(pt, Some(5000));
        assert_eq!(ct, Some(100));
    }

    #[test]
    fn stream_anthropic_read_chunk_is_captured() {
        let payload = serde_json::json!({
            "choices": [{"delta": {"content": ""}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 5050,
                "completion_tokens": 80,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 4500
            }
        })
        .to_string();
        let (cw, cr, _, _) = run_chunks(&[&payload]);
        assert_eq!(cw, 0);
        assert_eq!(cr, 4500);
    }

    #[test]
    fn stream_uses_max_when_counters_appear_on_multiple_events() {
        // The accumulator uses max() because providers may surface
        // partial counts on intermediate events. A later event with
        // a smaller value must NOT decrease the running total.
        let p1 = serde_json::json!({
            "choices": [{"delta": {"content": "a"}}],
            "usage": {"cache_read_input_tokens": 1000}
        })
        .to_string();
        let p2 = serde_json::json!({
            "choices": [{"delta": {"content": "b"}}],
            "usage": {"cache_read_input_tokens": 4500}
        })
        .to_string();
        let p3 = serde_json::json!({
            "choices": [{"delta": {"content": "c"}, "finish_reason": "stop"}],
            "usage": {"cache_read_input_tokens": 0}
        })
        .to_string();
        let (_, cr, _, _) = run_chunks(&[&p1, &p2, &p3]);
        assert_eq!(cr, 4500, "must keep the max, not the last");
    }

    #[test]
    fn stream_openai_cached_tokens_via_prompt_tokens_details() {
        // OpenAI uses `prompt_tokens_details.cached_tokens` (no
        // separate write counter — auto-prefix is invisible on the
        // write side).
        let payload = serde_json::json!({
            "choices": [{"delta": {"content": ""}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 2048,
                "completion_tokens": 64,
                "prompt_tokens_details": {"cached_tokens": 1536}
            }
        })
        .to_string();
        let (cw, cr, pt, _) = run_chunks(&[&payload]);
        assert_eq!(cw, 0, "OpenAI emits no write counter");
        assert_eq!(cr, 1536);
        assert_eq!(pt, Some(2048));
    }

    #[test]
    fn stream_chunk_without_usage_does_not_touch_counters() {
        // Most chunks in a stream are content deltas. They must not
        // overwrite the accumulator (which max() would naturally
        // protect against, but defence in depth — testing the no-op).
        let p1 = serde_json::json!({
            "choices": [{"delta": {"content": "hello "}}]
        })
        .to_string();
        let p2 = serde_json::json!({
            "choices": [{"delta": {"content": "world"}}]
        })
        .to_string();
        let (cw, cr, pt, ct) = run_chunks(&[&p1, &p2]);
        assert_eq!(cw, 0);
        assert_eq!(cr, 0);
        assert_eq!(pt, None);
        assert_eq!(ct, None);
    }

    #[test]
    fn stream_malformed_chunk_does_not_panic() {
        // SSE produces lots of `data: ` lines — including pings,
        // [DONE] sentinels, and partial JSON when the upstream
        // disconnects mid-message. The accumulator must tolerate
        // garbage without panicking.
        let (cw, cr, pt, ct) = run_chunks(&["", "{}", "not-json", r#"{"foo": [unterminated"#]);
        assert_eq!(cw, 0);
        assert_eq!(cr, 0);
        assert_eq!(pt, None);
        assert_eq!(ct, None);
    }

    #[test]
    fn stream_combined_anthropic_warm_then_read_keeps_both_counters() {
        // Hypothetical multi-call recovery scenario: a stream-restart
        // could emit a warm event then a read event. The accumulator
        // must hold both counters independently.
        let warm = serde_json::json!({
            "choices": [{"delta": {"content": ""}}],
            "usage": {"cache_creation_input_tokens": 4500}
        })
        .to_string();
        let read = serde_json::json!({
            "choices": [{"delta": {"content": ""}}],
            "usage": {"cache_read_input_tokens": 1200}
        })
        .to_string();
        let (cw, cr, _, _) = run_chunks(&[&warm, &read]);
        assert_eq!(cw, 4500);
        assert_eq!(cr, 1200);
    }
}
