// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Gemini native client surface
//!
//! Exposes Google's native `/v1beta/models/{model}:generateContent`
//! endpoint at Brutor's edge so clients of the Google AI Studio /
//! Vertex SDK can point their base URL at Brutor and use the native
//! Gemini wire format end-to-end. Mirrors the Anthropic Messages
//! passthrough at `routes/anthropic.rs`.
//!
//! ## Why a native client surface
//!
//! The OpenAI-shape surface at `/v1/proxy/llm/chat/completions`
//! handles 90% of clients. But Google's SDKs (and tools like
//! Cline, Gemini CLI) speak the native Gemini wire shape and won't
//! accept OpenAI-shape responses. Exposing this route lets them point
//! `GOOGLE_API_BASE` at Brutor and inherit every governance / cache /
//! observability hook the standard pipeline runs — auth, quota,
//! group access, input/output guardrails, prompt cache, proxy_log,
//! usage_record, breaker signal.
//!
//! ## Path conventions
//!
//! Google's URL pattern bakes the model name into the path with a
//! `:action` suffix:
//!
//! ```text
//! POST /v1beta/models/{model}:generateContent
//! POST /v1beta/models/{model}:streamGenerateContent
//! ```
//!
//! Axum 0.8 / matchit handles mixed placeholder + literal segments,
//! so the route attribute below is the literal Google path.
//!
//! ## Pipeline
//!
//! Native-in, native-out. The body never crosses into OpenAI shape:
//!
//!   1. Preflight: tenant ctx → auth → model resolution → access
//!      check → quota enforcement (shared with the OpenAI route).
//!   2. Guardrails: extract input text from native body
//!      (`contents[].parts[].text` + `systemInstruction.parts[].text`).
//!   3. Prompt cache: `apply_to_request_async` dispatches to the
//!      `gemini_cache` adapter; the body is already in native shape so
//!      the existing lifecycle code matches without translation.
//!   4. Forward to Google's native endpoint with `x-goog-api-key`
//!      auth (the GeminiAdapter uses the same shape).
//!   5. Return native response.

use std::time::Instant;

use axum::Router;
use axum::extract::{Path, State};

// `Path` and `State` are imported above for the dispatcher's extractor
// list; the dispatched-to handlers receive plain args (the dispatcher
// has already done the extraction).
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Json, Response};
use axum::routing::post;
use serde_json::{Value, json};

use crate::middleware::tenant::TenantContext;
use crate::models::db::ProxyLogInsert;
use crate::routes::llm_helpers;
use crate::routes::route_helpers;
use crate::services::call_chain::{derive_tenant_key, parse_or_mint};
use crate::services::llm_preflight::PreflightOptions;
use crate::services::proxy_logger::{
    self, body_preview, extract_user_agent, populate_llm_request_fields,
};
use crate::services::{SharedState, llm_preflight, proxy_metrics, usage};

pub fn router() -> Router<SharedState> {
    // Axum 0.8's matchit router allows at most ONE placeholder per
    // path segment — `{model}:generateContent` is rejected at startup
    // with "Only one parameter is allowed per path segment". We
    // capture the entire `{model}:{action}` segment as one parameter
    // and dispatch in the handler.
    // Both actions are generations (AI-generated label).
    Router::new()
        .route("/v1beta/models/{model_action}", post(dispatch_model_action))
        .route_layer(axum::middleware::from_fn(
            crate::services::evidence::notice::mark_ai_generated,
        ))
}

/// Parse the captured `{model}:{action}` segment and route to the
/// appropriate generator. Unknown actions get a clean 404 with a
/// list of what we DO support — saves a few minutes of "why is my
/// SDK getting a 200 with garbage" debugging.
pub(crate) async fn dispatch_model_action(
    State(state): State<SharedState>,
    Path(model_action): Path<String>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Json(body): Json<Value>,
) -> Response {
    let Some((model_name, action)) = model_action.split_once(':') else {
        return gemini_error(
            StatusCode::NOT_FOUND,
            "Path must be /v1beta/models/{model}:{action}. \
             Supported actions: generateContent, streamGenerateContent.",
        );
    };
    match action {
        "generateContent" => {
            generate_content(
                state,
                model_name.to_string(),
                client_addr,
                headers,
                tenant,
                body,
            )
            .await
        }
        "streamGenerateContent" => {
            stream_generate_content(
                state,
                model_name.to_string(),
                client_addr,
                headers,
                tenant,
                body,
            )
            .await
        }
        other => gemini_error(
            StatusCode::NOT_FOUND,
            &format!(
                "Unsupported action ':{other}'. \
                 Supported actions: generateContent, streamGenerateContent."
            ),
        ),
    }
}

// ---------------------------------------------------------------------------
// POST /v1beta/models/{model}:generateContent
// ---------------------------------------------------------------------------

#[utoipa::path(
    post,
    path = "/v1beta/models/{model}:generateContent",
    tag = "Gemini",
    summary = "Generate content (Gemini-compatible)",
    description = "Gemini native Messages API endpoint. Preserves native body shape end-to-end and routes through the same governance / cache / observability pipeline as the OpenAI surface.",
    params(("model" = String, Path, description = "Model id or name")),
    request_body = Value,
    responses(
        (status = 200, description = "Gemini native response", body = Value),
        (status = 400, description = "Bad request"),
        (status = 401, description = "Authentication required"),
        (status = 429, description = "Rate/usage limit exceeded"),
    ),
    security(("api_key" = []), ("bearer" = []),)
)]
pub(crate) async fn generate_content(
    state: SharedState,
    model_path: String,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    mut gemini_body: Value,
) -> Response {
    let start = Instant::now();
    let correlation_id = route_helpers::extract_correlation_id(&headers);
    let tenant_id = match extract_tenant_id(tenant) {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    // Resolve the requested model — name lives in the URL path
    // (`/v1beta/models/{model}:generateContent`), not the body. We
    // strip a leading `models/` if the SDK qualified it.
    let model_name = model_path
        .strip_prefix("models/")
        .unwrap_or(&model_path)
        .to_string();

    tracing::debug!(
        model_requested = %model_name,
        "Gemini native generateContent request"
    );

    // Preflight: auth → model resolution → access check → quota.
    // The model name comes from the URL path, not the body — pass it
    // through to llm_preflight.
    // Call chain. Same shape as routes/llm.rs:
    // parse the inbound delegation headers (or mint a root) so this
    // Gemini-native request joins the cross-surface trace and emits
    // chain headers onto the upstream send.
    let tenant_key = derive_tenant_key(state.config.encryption_key.as_bytes(), &tenant_id);
    let (call_chain, current_task_id, trace_continuity) = parse_or_mint(
        |n| {
            headers
                .get(n)
                .and_then(|v| v.to_str().ok())
                .map(String::from)
        },
        &tenant_key,
    );
    let current_task_id_str = current_task_id.to_string();

    let pf_body = gemini_body.to_string();
    // Where this call sits in its run — the run the pre-flight run controls
    // check, and the run every refusal or block row of this request lands on.
    let run_attr = crate::services::llm_refusal::RunAttribution::chain(
        &call_chain,
        &current_task_id_str,
        trace_continuity,
    );
    let pf = match llm_preflight::run_preflight(
        &state,
        &headers,
        &tenant_id,
        &correlation_id,
        Some(&model_name),
        None,
        // Arm the run-level ceilings with the chain minted above.
        &PreflightOptions::with_guardrails(run_attr.clone()),
        gemini_error,
        client_addr.ip(),
        None,
        Some(&pf_body),
    )
    .await
    {
        Ok(pf) => pf,
        Err(resp) => return resp,
    };

    let auth_ctx = pf.auth_ctx;
    let model = pf.model;
    let target_group = pf.target_group;
    // The output this call was admitted for (every token and spend cap's
    // room): bound `maxOutputTokens` — and the thinking budget inside it.
    if let Some(ceiling) = pf.out_ceiling {
        crate::services::llm_governance::apply_output_ceiling(&mut gemini_body, ceiling);
    }

    let trace_root_kind =
        proxy_logger::root_kind_for_auth(&auth_ctx.auth_type, call_chain.depth == 0);

    // Agent authorization, the autonomy kill switch and
    // semantic policy, the same three planes the OpenAI surface runs. Without
    // them this mounted route was a way past a suspended AI System.
    if let Err(resp) = llm_preflight::enforce_actor_planes(
        &state,
        &auth_ctx,
        &tenant_id,
        &model,
        target_group.as_ref(),
        Some(model_name.clone()),
        &serde_json::to_string(gemini_body.get("contents").unwrap_or(&Value::Null))
            .unwrap_or_default(),
        &format!("/v1beta/models/{model_name}"),
        &headers,
        &correlation_id,
        start,
        &call_chain,
        &current_task_id_str,
        trace_continuity,
        trace_root_kind,
        gemini_error,
    )
    .await
    {
        return resp;
    }

    // Hard rule: the native client surface only handles Gemini-family
    // providers. Hitting it with a non-Gemini model is a misuse — the
    // OpenAI-shape route is the right surface for those. We don't
    // silently round-trip-translate here the way the Anthropic
    // fallback pipeline does, because Gemini's shape is too divergent
    // to safely auto-convert (no role:"system", contents not messages,
    // parts not content blocks, etc.).
    if !matches!(
        model.provider.as_str(),
        "gemini" | "google_ai_studio" | "vertex_ai" | "vertex"
    ) {
        return gemini_error(
            StatusCode::BAD_REQUEST,
            &format!(
                "Provider '{}' isn't a Gemini-family provider. Use the \
                 OpenAI surface at /v1/proxy/llm/chat/completions for \
                 cross-provider clients.",
                model.provider
            ),
        );
    }

    // Data-residency check (parity with routes/llm.rs:349). Look up
    // the resolved provider's region and intersect with the tenant
    // residency profile. A Blocked decision writes a single
    // residency_violation audit row and 403s before the prompt ever
    // reaches Google. Allowed/not-evaluated decisions are stamped on the
    // success row below.
    // One shared implementation, see
    // `llm_preflight::enforce_residency`.
    let residency = match llm_preflight::enforce_residency(
        &state,
        &auth_ctx,
        &tenant_id,
        &model,
        Some(model_name.clone()),
        &format!("/v1beta/models/{model_name}:generateContent"),
        "Gemini",
        &headers,
        &correlation_id,
        start,
        &call_chain,
        &current_task_id_str,
        trace_continuity,
        trace_root_kind,
        gemini_error,
    )
    .await
    {
        Ok(o) => o,
        Err(resp) => return resp,
    };
    let provider_region_for_audit = residency.provider_region;
    let residency_decision = residency.decision;

    // Guardrails over the native body. The Brutor-canonical
    // `apply_guardrails` takes a flat text — extract it from the
    // Gemini parts shape. On Redact we splice the rewritten text
    // back into systemInstruction.parts[].text / contents[].parts[].text
    // so a `pii_action: redact` policy actually masks the PII
    // before forwarding to Gemini.
    let input_text = extract_input_text(&gemini_body);
    let guard_target = llm_preflight::LlmAuditTarget::from_model(
        &model,
        Some(model_name.to_string()),
        "/chat/completions",
    );
    // Tracks whether an input guardrail redacted a PII category — stamped
    // on the success audit row below (output redaction is stamped
    // separately in the output-guardrail loop).
    let mut input_pii_redacted = false;
    match llm_preflight::apply_guardrails_with_redaction(
        &state,
        &tenant_id,
        &crate::services::llm_refusal::RefusalScope::new(
            &auth_ctx,
            &headers,
            &correlation_id,
            &format!("/v1beta/models/{model_name}:generateContent"),
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
            apply_redactions_to_input(&mut gemini_body, &redaction.content);
            input_pii_redacted = redaction.pii;
        }
        Ok(None) => {}
    }

    // RG governance enforcement (temperature/topP/maxOutputTokens
    // caps, tool filtering, banned-pattern, mandatory system-prompt
    // injection) at PARITY with chat_completions. `apply_governance`
    // operates on the chat-completion shape, so we build a chat *view* of
    // the Gemini body, run the unchanged helper over it (a `reject` cap or
    // a banned pattern 403s here, before the upstream call), then splice
    // the governed params + injected systemInstruction back into the
    // native body. Param caps are discarded — the native Gemini path has
    // no model-default-injection step that would need EFFECTIVE_PARAM_CAPS.
    {
        let mut gov_view = gemini_request_to_chat_view(&gemini_body);
        match llm_preflight::apply_governance(
            &state,
            &target_group,
            &model.id,
            &mut gov_view,
            gemini_error,
            &crate::services::llm_refusal::RefusalScope {
                auth_ctx: &auth_ctx,
                headers: &headers,
                correlation_id: &correlation_id,
                endpoint: &format!("/v1beta/models/{model_name}:generateContent"),
                started: Some(start),
                model: Some((&model, Some(model_name.clone()))),
                group_id: None,
                run: Some(&crate::services::llm_refusal::RunAttribution::chain(
                    &call_chain,
                    &current_task_id_str,
                    trace_continuity,
                )),
                request_body: None,
            },
        )
        .await
        {
            Ok(_caps) => apply_gemini_chat_view_back(&mut gemini_body, &gov_view),
            Err(resp) => return resp,
        }
    }

    // Build the outbound request body. The client already sent native
    // Gemini shape — minimal cleanup (strip caller-supplied
    // `cache_control` so only the prompt-cache layer's markers
    // survive, same authoritative-emitter rule the Anthropic adapter
    // enforces on its inbound path).
    let mut body = gemini_body;
    strip_caller_cache_control(&mut body);

    // Prompt-cache marker injection. `apply_to_request_async`
    // dispatches to the `gemini_cache` adapter which expects native
    // Gemini shape — the body is already in that shape because the
    // caller sent it. This is the whole point of the native surface:
    // skip the translate-roundtrip the OpenAI route incurs.
    let prompt_cache = state
        .prompt_cache_policy
        .session(&state, &tenant_id, target_group.as_ref(), &model)
        .await;
    if let Some(decision) = prompt_cache.mark(&mut body, &model).await {
        crate::services::prompt_caching::record_decision(&decision);
    }

    // Build upstream URL + auth. Branches on provider:
    //   * AI Studio  → `generativelanguage.googleapis.com` + `x-goog-api-key`
    //   * Vertex AI  → `{region}-aiplatform.googleapis.com/.../{model}:action`
    //                  + `Authorization: Bearer {the row's Google access token}`
    let (upstream_url, auth_header) =
        match resolve_gemini_endpoint(&model, GeminiAction::GenerateContent).await {
            Ok(v) => v,
            Err(resp) => return resp,
        };

    // Force stream=false off the body (clients sometimes paste it from
    // OpenAI examples; Google's native endpoint 400s on the field).
    if let Some(obj) = body.as_object_mut() {
        obj.remove("stream");
    }

    // Cluster-wide RG / per-model concurrency gate for native (non-streaming)
    // generateContent — held for the request (drops at fn return). Acquired
    // before begin_request so a saturation 429 needs no gauge cleanup.
    let _conc_guard = match crate::services::llm_refusal::acquire_concurrency(
        &state,
        target_group.as_ref(),
        &model,
        &crate::services::llm_refusal::RefusalScope {
            auth_ctx: &auth_ctx,
            headers: &headers,
            correlation_id: &correlation_id,
            endpoint: &format!("/v1beta/models/{model_name}:generateContent"),
            started: Some(start),
            model: Some((&model, Some(model_name.clone()))),
            group_id: None,
            run: Some(&crate::services::llm_refusal::RunAttribution::chain(
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

    state.router_state.increment_rpm(&model.id);
    state.router_state.begin_request(&model.id);

    let request_body_str = serde_json::to_string(&body).unwrap_or_default();
    let timeout_secs = model.timeout.map(|t| t as u64).unwrap_or(120);
    let mut req = state
        .http_client
        .post(&upstream_url)
        .header("Content-Type", "application/json")
        .header(auth_header.0.as_str(), auth_header.1.as_str())
        .timeout(std::time::Duration::from_secs(timeout_secs));
    // Chain headers on Gemini upstream.
    for (k, v) in call_chain.to_header_pairs(&tenant_key) {
        req = req.header(k, v);
    }
    let result = req.json(&body).send().await;
    state.router_state.end_request(&model.id);

    let upstream_resp = match result {
        Ok(r) => {
            // Capture the vendor's request id where one is offered.
            // Gemini does not currently send `x-request-id`/`request-id`, so
            // this is a no-op today; wiring it anyway means the surface is not
            // structurally incapable of carrying the handle if Google adds one.
            crate::middleware::run_end::record_upstream_response(r.status().as_u16(), r.headers());
            r
        }
        Err(e) => {
            return finalise_error(
                &state,
                &auth_ctx,
                &model,
                target_group.as_ref(),
                &tenant_id,
                &headers,
                &correlation_id,
                Some(model_name.clone()),
                &request_body_str,
                start,
                StatusCode::BAD_GATEWAY,
                &format!("Upstream call failed: {e}"),
                &call_chain,
                &current_task_id_str,
                trace_continuity,
                trace_root_kind,
            )
            .await;
        }
    };

    let status_code = upstream_resp.status();
    let response_headers = upstream_resp.headers().clone();
    let response_body_bytes = match upstream_resp.bytes().await {
        Ok(b) => b,
        Err(e) => {
            return finalise_error(
                &state,
                &auth_ctx,
                &model,
                target_group.as_ref(),
                &tenant_id,
                &headers,
                &correlation_id,
                Some(model_name.clone()),
                &request_body_str,
                start,
                StatusCode::BAD_GATEWAY,
                &format!("Failed to read upstream response: {e}"),
                &call_chain,
                &current_task_id_str,
                trace_continuity,
                trace_root_kind,
            )
            .await;
        }
    };

    let response_body_str = String::from_utf8_lossy(&response_body_bytes).to_string();
    let mut response_body_json: Value = serde_json::from_slice(&response_body_bytes)
        .unwrap_or(Value::String(response_body_str.clone()));

    // Log the request via the standard proxy_log pipeline — same row
    // shape as the OpenAI surface so dashboards and per-group stats
    // see both surfaces uniformly. Prompt-cache counters come straight
    // from Gemini's `usageMetadata.cachedContentTokenCount` via the
    // `gemini_cache::extract_usage` extractor that runs below.
    let duration_ms = start.elapsed().as_millis() as f64;
    let success = status_code.is_success();
    proxy_metrics::record_request("llm", "POST", status_code.as_u16(), &model.provider);
    proxy_metrics::record_latency("llm", "POST", &model.provider, duration_ms / 1000.0);

    let user_agent = extract_user_agent(&headers);
    let endpoint_path = format!("/v1beta/models/{model_name}:generateContent");
    let mut log = proxy_logger::build_proxy_log(
        "llm",
        &auth_ctx,
        "POST",
        &endpoint_path,
        Some(&upstream_url),
        Some(&model.id),
        status_code.as_u16(),
        duration_ms,
        success,
        None,
        &correlation_id,
    );
    log.proxy_subtype = Some(crate::constants::proxy_subtype::GEMINI_GENERATE_CONTENT.to_string());
    log.resource_group_id = target_group.as_ref().map(|g| g.id.clone());
    log.user_agent = user_agent.clone();
    log.request_size_bytes = Some(request_body_str.len() as i32);
    log.request_headers = Some(proxy_logger::sanitize_headers(&headers));
    log.request_body_preview = Some(body_preview(&request_body_str));
    log.response_size_bytes = Some(response_body_str.len() as i32);
    log.response_body_preview = Some(body_preview(&response_body_str));
    log.llm_provider = Some(model.provider.clone());
    log.llm_model_used = Some(model.model_name.clone());
    log.llm_model_requested = Some(model_name.clone());
    log.llm_streaming = Some(false);
    // Input guardrail redacted a PII category → stamp the success row
    // (output redaction is stamped separately in the output loop below).
    if input_pii_redacted {
        proxy_logger::apply_pii_detected(&mut log);
    }
    // Stamp the residency outcome (allowed / not-evaluated) on the
    // success row (blocked already returned a dedicated row above).
    match &residency_decision {
        Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
        None => proxy_logger::apply_residency_not_evaluated(
            &mut log,
            provider_region_for_audit.as_deref(),
        ),
    }

    // Populate token counters from Gemini's native `usageMetadata`
    // block. This is the native equivalent of the work
    // `populate_llm_request_fields` does on the OpenAI shape — both
    // routes need the per-call token counts on the proxy_log row.
    if let Some(usage_block) = response_body_json.get("usageMetadata") {
        log.llm_prompt_tokens = usage_block
            .get("promptTokenCount")
            .and_then(|v| v.as_i64())
            .map(|v| v as i32);
        log.llm_completion_tokens = usage_block
            .get("candidatesTokenCount")
            .and_then(|v| v.as_i64())
            .map(|v| v as i32);
        log.llm_total_tokens = usage_block
            .get("totalTokenCount")
            .and_then(|v| v.as_i64())
            .map(|v| v as i32);
    }

    // Estimated cost (same arithmetic the OpenAI route uses; we feed
    // it into the row so dashboards reconcile against `usage_records`
    // later).
    let log_estimated_cost = llm_helpers::estimate_llm_cost(
        log.llm_prompt_tokens,
        log.llm_completion_tokens,
        model.cost_per_input_token.unwrap_or(0.0),
        model.cost_per_output_token.unwrap_or(0.0),
    );
    log.llm_estimated_cost_usd = log_estimated_cost;

    // Pull request-side LLM fields (model params, message counts, etc).
    populate_llm_request_fields(
        &mut log,
        &serde_json::from_str::<Value>(&request_body_str).unwrap_or(Value::Null),
    );

    // Prompt-cache telemetry — mirrors the routes/anthropic.rs path.
    // Reads `usageMetadata.cachedContentTokenCount` for Gemini-mechanism
    // models; the per-group stats panel queries these columns.
    let pc_mechanism = crate::services::prompt_cache_usage::mechanism_for_provider(
        &model.provider,
        &model.model_name,
    );
    let pc_usage = crate::services::prompt_cache_usage::extract_usage_from_response(
        &response_body_json,
        pc_mechanism,
        &model,
    );
    if pc_usage.write_tokens > 0 || pc_usage.read_tokens > 0 {
        log.prompt_cache_write_tokens = Some(pc_usage.write_tokens);
        log.prompt_cache_read_tokens = Some(pc_usage.read_tokens);
        log.prompt_cache_savings_usd = Some(pc_usage.savings_usd);
        let mech_label = proxy_metrics::mechanism_label(pc_mechanism);
        proxy_metrics::record_prompt_cache_tokens(
            "read",
            mech_label,
            &model.provider,
            pc_usage.read_tokens,
        );
        proxy_metrics::record_prompt_cache_tokens(
            "write",
            mech_label,
            &model.provider,
            pc_usage.write_tokens,
        );
        proxy_metrics::record_prompt_cache_savings(
            mech_label,
            &model.provider,
            pc_usage.savings_usd,
        );
    }

    // Post-response governance (tool-call banned-arg patterns + AST
    // argument policies + tool-call rate counters) at PARITY with chat. We
    // normalize the Gemini `functionCall` parts into the chat tool-call view
    // the helper expects, run the unchanged helper, and 403 on a block.
    // `apply_response_governance` owns its own block audit row, so on a block
    // we skip the deferred success write below (single-audit-row contract).
    let mut guardrail_block: Option<Response> = None;
    {
        let tool_view = gemini_response_to_chat_tool_view(&response_body_json);
        if let Err(resp) = llm_preflight::apply_response_governance(
            &state,
            &target_group,
            &model.id,
            &tool_view,
            gemini_error,
            &crate::services::llm_refusal::RefusalScope::new(
                &auth_ctx,
                &headers,
                &correlation_id,
                &format!("/v1beta/models/{model_name}:generateContent"),
                &run_attr,
            )
            .started(start),
        )
        .await
        {
            guardrail_block = Some(resp);
        }
    }

    // Output guardrails on the Gemini-native response. Each candidate part's
    // text is checked independently (mirrors the per-choice loop in
    // routes/llm.rs): a Block returns the Gemini-shaped 403 error; a Redacted
    // result is spliced back into that part. Without this, the Gemini-native
    // non-streaming path returned model output with no PII/banned/secrets/toxic
    // enforcement (the streaming path already runs StreamGuard).
    if guardrail_block.is_none() {
        'guard: {
            let cand_count = response_body_json
                .get("candidates")
                .and_then(|c| c.as_array())
                .map(|a| a.len())
                .unwrap_or(0);
            for ci in 0..cand_count {
                let part_count = response_body_json
                    .pointer(&format!("/candidates/{ci}/content/parts"))
                    .and_then(|p| p.as_array())
                    .map(|a| a.len())
                    .unwrap_or(0);
                for pi in 0..part_count {
                    let text = response_body_json
                        .pointer(&format!("/candidates/{ci}/content/parts/{pi}/text"))
                        .and_then(|v| v.as_str())
                        .map(|s| s.to_string());
                    let Some(text) = text else { continue };
                    if text.is_empty() {
                        continue;
                    }
                    match llm_preflight::apply_output_guardrails(
                        &state,
                        &tenant_id,
                        &crate::services::llm_refusal::RefusalScope::new(
                            &auth_ctx,
                            &headers,
                            &correlation_id,
                            &format!("/v1beta/models/{model_name}:generateContent"),
                            &run_attr,
                        )
                        .started(start),
                        &text,
                        crate::constants::guardrail_surface::CHAT_OUTPUT,
                        Some(gemini_guardrail_violation_response),
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
                            if let Some(slot) = response_body_json
                                .pointer_mut(&format!("/candidates/{ci}/content/parts/{pi}/text"))
                            {
                                *slot = Value::String(new_text);
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
        }
    }
    // On a block, `apply_output_guardrails` already wrote the authoritative
    // block audit row (with compliance stamps), so SKIP the deferred write to
    // avoid a duplicate. On pass/redact, write the deferred row with the
    // (possibly redacted) preview. `usage::write_llm_usage_log` below still
    // reads `log` fields, so the row's token attribution survives either way.
    if guardrail_block.is_none() {
        let final_body_str = serde_json::to_string(&response_body_json).unwrap_or_default();
        log.response_size_bytes = Some(final_body_str.len() as i32);
        log.response_body_preview = Some(body_preview(&final_body_str));
        proxy_logger::apply_call_chain(
            &mut log,
            &call_chain,
            &current_task_id_str,
            trace_continuity,
            trace_root_kind,
        );
        state
            .background_writer
            .write_proxy_log(state.db.clone(), log.clone());
    }

    if success {
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
            log.llm_prompt_tokens,
            log.llm_completion_tokens,
            log_estimated_cost,
            model.cost_per_input_token.unwrap_or(0.0),
            model.cost_per_output_token.unwrap_or(0.0),
            &endpoint_path,
            &correlation_id,
            status_code.as_u16(),
            duration_ms,
        );
    }

    // Output guardrail blocked the response — return the Gemini-shaped 403
    // error instead of the model output.
    if let Some(resp) = guardrail_block {
        return resp;
    }

    // Build the client-facing response — pass through native shape.
    let mut response = (status_code, Json(response_body_json)).into_response();
    if let Some(ct) = response_headers.get(axum::http::header::CONTENT_TYPE) {
        response
            .headers_mut()
            .insert(axum::http::header::CONTENT_TYPE, ct.clone());
    }
    response
}

// ---------------------------------------------------------------------------
// OpenAI-surface bridge for Vertex-Gemini models
// ---------------------------------------------------------------------------
//
// Same translate-on-exit pattern as the Anthropic bridge in #681:
// the OpenAI surface routes Vertex-Gemini requests here. We translate
// the OpenAI body to Gemini native via `GeminiAdapter::transform_request`,
// then forward via `resolve_gemini_endpoint` (which knows about Vertex's
// regional URL + the row's Google access token). The response — JSON for non-streaming,
// SSE chunks for streaming — gets translated back to OpenAI shape via
// `GeminiAdapter::normalize_response` / `GeminiStreamNormalizer`
// before being handed to the client.
//
// PREFLIGHT ONLY has happened on the OpenAI-surface side: access checks,
// input guardrails and the quota gate all run before `chat_completions`
// dispatches here. Everything that can only be known AFTER the provider
// answers is this function's responsibility, because `chat_completions`
// `return`s into it and never regains control: output guardrails, the
// proxy_log row, the usage ledger, the throughput charge and the metrics.
//
// That tail used to be missing entirely. Both exit branches ended
// with `let _ = (auth_ctx, target_group, headers, correlation_id, start);`
// — an explicit discard of exactly the context the tail needs — so every
// Vertex-hosted Gemini call through the OpenAI surface produced no audit
// row (no evidence, no compliance tags, invisible to the run ledger and
// the Assurance Report), moved no token or cost counter (so $ and token
// quotas could not bind), and ran no output guardrail (so PII and secret
// redaction were silently off for this provider alone). The Anthropic
// sibling bridge in `services::anthropic_adapter` always did all of it;
// the asymmetry was the bug.

/// The chain identity a bridged call carries onto its audit row.
///
/// `chat_completions` owns these values; the bridge cannot re-derive them
/// (the delegation headers were consumed at request entry), and the
/// streaming branch needs to move them into its spawned task, so they are
/// captured as one owned bundle rather than five borrowed arguments.
#[derive(Clone)]
pub struct BridgeChain {
    pub chain: crate::services::a2a_delegation::CallChain,
    pub current_task_id: String,
    pub continuity: crate::services::a2a_delegation::TraceContinuity,
    pub root_kind: Option<String>,
}

/// What the bridge learned about one completed call, for the evidence tail.
struct BridgeOutcome {
    request_body_str: String,
    request_headers: Option<String>,
    openai_request: Value,
    response_body_str: String,
    model_requested: Option<String>,
    prompt_tokens: Option<i32>,
    completion_tokens: Option<i32>,
    total_tokens: Option<i32>,
    streaming: bool,
    status_code: u16,
    duration_ms: f64,
    user_agent: Option<String>,
    /// Input or output guardrail redacted a PII category on this call.
    pii_redacted: bool,
    /// False when a guardrail block already wrote the authoritative audit
    /// row — writing again would double-count the call (single-audit-row
    /// contract, same rule the native path follows).
    write_proxy_log: bool,
    /// How the guard ended a STREAMED response (operator abort / output
    /// guardrail block). A stream's block writes no row of its own: this
    /// outcome row is the block row.
    stream_outcome: crate::services::stream_guard::StreamOutcome,
}

/// Write the evidence a governed LLM call owes: the audit row, the usage
/// ledger entry, the run/spend charge and the Prometheus counters.
///
/// Mirrors the tail of the Gemini-native handler above and of the Anthropic
/// bridge, so a Vertex-Gemini call on the OpenAI surface reconciles against
/// `usage_records` and appears in the run ledger exactly like every other
/// surface. The run id is passed to the throughput charge (unlike the
/// Anthropic bridge, which still drops it) so per-run ceilings
/// accumulate for this path.
#[allow(clippy::too_many_arguments)]
async fn write_bridge_evidence(
    state: &SharedState,
    model: &crate::models::db::LlmModel,
    auth_ctx: &crate::models::auth::AuthContext,
    target_group: Option<&crate::models::db::ResourceGroup>,
    tenant_id: &str,
    correlation_id: &str,
    bridge_chain: &BridgeChain,
    outcome: BridgeOutcome,
) {
    let success = (200..300).contains(&outcome.status_code);
    let endpoint_path = crate::constants::routes::LLM_CHAT_COMPLETIONS;

    proxy_metrics::record_request("llm", "POST", outcome.status_code, &model.provider);
    proxy_metrics::record_latency("llm", "POST", &model.provider, outcome.duration_ms / 1000.0);
    if let Some(pt) = outcome.prompt_tokens {
        proxy_metrics::record_tokens("prompt", &model.provider, &model.model_name, pt);
    }
    if let Some(ct) = outcome.completion_tokens {
        proxy_metrics::record_tokens("completion", &model.provider, &model.model_name, ct);
    }

    // One definition of the formula, shared with the
    // native path and the OpenAI surface, so the three reconcile against
    // one another. This used to be a hand-rolled copy that voided the whole
    // estimate when either side was absent; the helper prices the side it
    // was given and charges zero for the one it wasn't.
    let estimated_cost = llm_helpers::estimate_llm_cost(
        outcome.prompt_tokens,
        outcome.completion_tokens,
        model.cost_per_input_token.unwrap_or(0.0),
        model.cost_per_output_token.unwrap_or(0.0),
    );
    if let Some(cost) = estimated_cost {
        proxy_metrics::record_cost(&model.provider, &model.model_name, cost);
    }

    if outcome.write_proxy_log {
        let mut log = proxy_logger::build_proxy_log(
            "llm",
            auth_ctx,
            "POST",
            endpoint_path,
            None,
            Some(&model.id),
            outcome.status_code,
            outcome.duration_ms,
            success,
            None,
            correlation_id,
        );
        log.proxy_subtype = Some(crate::constants::proxy_subtype::CHAT_COMPLETIONS.to_string());
        log.resource_group_id = target_group.map(|g| g.id.clone());
        log.user_agent = outcome.user_agent.clone();
        log.request_size_bytes = Some(outcome.request_body_str.len() as i32);
        log.request_headers = outcome.request_headers.clone();
        // The OUTBOUND body, per the proxy-log contract: what Vertex was
        // actually sent, after OpenAI→Gemini translation and prompt-cache
        // markers — not the client's original shape.
        log.request_body_preview = Some(body_preview(&outcome.request_body_str));
        log.response_size_bytes = Some(outcome.response_body_str.len() as i32);
        log.response_body_preview = Some(body_preview(&outcome.response_body_str));
        log.llm_provider = Some(model.provider.clone());
        log.llm_model_used = Some(model.model_name.clone());
        log.llm_model_requested = outcome.model_requested.clone();
        log.llm_streaming = Some(outcome.streaming);
        log.llm_prompt_tokens = outcome.prompt_tokens;
        log.llm_completion_tokens = outcome.completion_tokens;
        log.llm_total_tokens = outcome.total_tokens;
        log.llm_estimated_cost_usd = estimated_cost;
        if outcome.pii_redacted {
            proxy_logger::apply_pii_detected(&mut log);
        }
        outcome.stream_outcome.stamp(&mut log);
        populate_llm_request_fields(&mut log, &outcome.openai_request);
        proxy_logger::apply_call_chain(
            &mut log,
            &bridge_chain.chain,
            &bridge_chain.current_task_id,
            bridge_chain.continuity,
            bridge_chain.root_kind.as_deref(),
        );
        state
            .background_writer
            .write_proxy_log(state.db.clone(), log);
    }

    if success {
        usage::write_llm_usage_log(
            &state.usage_state,
            &state.background_writer,
            &state.model_metrics,
            state.db.clone(),
            tenant_id,
            auth_ctx,
            target_group,
            &model.id,
            &model.model_name,
            &model.provider,
            outcome.prompt_tokens,
            outcome.completion_tokens,
            estimated_cost,
            model.cost_per_input_token.unwrap_or(0.0),
            model.cost_per_output_token.unwrap_or(0.0),
            endpoint_path,
            correlation_id,
            outcome.status_code,
            outcome.duration_ms,
        );
        if let Some(g) = target_group {
            usage::spawn_alert_evaluation(
                state.db.clone(),
                tenant_id.to_string(),
                g.clone(),
                "llm".into(),
                Some(correlation_id.to_string()),
            );
        }
    }
}

/// Read Gemini's native `usageMetadata` block into `(prompt, completion, total)`.
///
/// Taken from the native response BEFORE `normalize_response` rewrites it
/// into OpenAI shape, so the counts come from the provider's own numbers
/// rather than from a translation.
fn gemini_usage_counts(v: &Value) -> (Option<i32>, Option<i32>, Option<i32>) {
    let Some(u) = v.get("usageMetadata") else {
        return (None, None, None);
    };
    let read = |k: &str| u.get(k).and_then(|x| x.as_i64()).map(|x| x as i32);
    (
        read("promptTokenCount"),
        read("candidatesTokenCount"),
        read("totalTokenCount"),
    )
}

#[allow(clippy::too_many_arguments)]
pub async fn forward_via_openai_surface(
    state: &SharedState,
    model: &crate::models::db::LlmModel,
    auth_ctx: &crate::models::auth::AuthContext,
    target_group: Option<&crate::models::db::ResourceGroup>,
    tenant_id: &str,
    correlation_id: &str,
    openai_body: Value,
    wants_stream: bool,
    headers: &HeaderMap,
    start: Instant,
    bridge_chain: &BridgeChain,
    input_pii_redacted: bool,
) -> Response {
    // Keep the client's original OpenAI-shape request: the audit row's
    // model-parameter columns are read from it, and it names the model the
    // caller asked for (which routing may have resolved to another).
    let openai_request = openai_body.clone();
    let model_requested = openai_body
        .get("model")
        .and_then(|v| v.as_str())
        .map(str::to_string);
    let user_agent = extract_user_agent(headers);
    let request_headers = Some(proxy_logger::sanitize_headers(headers));

    // ── Body translation: OpenAI → Gemini native ──────────────────
    let mut body = openai_body;
    if let Some(obj) = body.as_object_mut() {
        let adapter = crate::services::llm_adapter::get_adapter(&model.provider);
        adapter.transform_request(obj, model);
        // The OpenAI surface attaches `stream` here too; native Vertex
        // rejects it.
        obj.remove("stream");
        obj.remove("model_id");
    }

    // ── Apply prompt-cache markers ─────────────────────────────────
    // The OpenAI-surface chat handler already called the prompt-cache
    // dispatcher BEFORE arriving here, but on an OpenAI-shape body —
    // markers landed in the wrong place. Re-run on the post-translate
    // body so they land in the Vertex-native `systemInstruction`
    // shape that `gemini_cache` expects.
    let prompt_cache = state
        .prompt_cache_policy
        .session(state, tenant_id, target_group, model)
        .await;
    let _ = prompt_cache.mark(&mut body, model).await;

    // The outbound body is what the audit row records.
    let request_body_str = serde_json::to_string(&body).unwrap_or_default();

    let action = if wants_stream {
        GeminiAction::StreamGenerateContent
    } else {
        GeminiAction::GenerateContent
    };
    let (upstream_url, auth_header) = match resolve_gemini_endpoint(model, action).await {
        Ok(v) => v,
        Err(resp) => return resp,
    };

    // Cluster-wide RG / per-model concurrency gate — acquired before the
    // shared upstream send so it covers BOTH the non-streaming (held for fn
    // scope) and streaming (moved into the SSE task) branches. Saturation → 429
    // before begin_request, so no gauge cleanup is needed.
    let conc_guard = match crate::services::llm_refusal::acquire_concurrency(
        state,
        target_group,
        model,
        &crate::services::llm_refusal::RefusalScope {
            auth_ctx,
            headers,
            correlation_id,
            endpoint: "/chat/completions",
            started: Some(start),
            model: Some((model, None)),
            group_id: None,
            run: Some(&crate::services::llm_refusal::RunAttribution::chain(
                &bridge_chain.chain,
                &bridge_chain.current_task_id,
                bridge_chain.continuity,
            )),
            request_body: None,
        },
    )
    .await
    {
        Ok(guard) => guard,
        Err(resp) => return resp,
    };

    state.router_state.increment_rpm(&model.id);
    state.router_state.begin_request(&model.id);

    let timeout_secs = model.timeout.map(|t| t as u64).unwrap_or(120);
    let mut req = state
        .http_client
        .post(&upstream_url)
        .header("Content-Type", "application/json")
        .header(auth_header.0.as_str(), auth_header.1.as_str())
        .timeout(std::time::Duration::from_secs(timeout_secs));
    if wants_stream {
        req = req.header("Accept", "text/event-stream");
    }
    let result = req.json(&body).send().await;

    if !wants_stream {
        state.router_state.end_request(&model.id);
    }

    let upstream_resp = match result {
        Ok(r) => {
            // Capture the vendor's request id where one is offered.
            // Gemini does not currently send `x-request-id`/`request-id`, so
            // this is a no-op today; wiring it anyway means the surface is not
            // structurally incapable of carrying the handle if Google adds one.
            crate::middleware::run_end::record_upstream_response(r.status().as_u16(), r.headers());
            r
        }
        Err(e) => {
            if wants_stream {
                state.router_state.end_request(&model.id);
            }
            // A failed upstream call is still a governed call that happened:
            // record it, or the ledger shows a gap where an error belongs.
            write_bridge_evidence(
                state,
                model,
                auth_ctx,
                target_group,
                tenant_id,
                correlation_id,
                bridge_chain,
                BridgeOutcome {
                    request_body_str: request_body_str.clone(),
                    request_headers: request_headers.clone(),
                    openai_request: openai_request.clone(),
                    response_body_str: String::new(),
                    model_requested: model_requested.clone(),
                    prompt_tokens: None,
                    completion_tokens: None,
                    total_tokens: None,
                    streaming: wants_stream,
                    status_code: StatusCode::BAD_GATEWAY.as_u16(),
                    duration_ms: start.elapsed().as_millis() as f64,
                    user_agent: user_agent.clone(),
                    pii_redacted: input_pii_redacted,
                    write_proxy_log: true,
                    stream_outcome: crate::services::stream_guard::StreamOutcome::Completed,
                },
            )
            .await;
            return gemini_error(
                StatusCode::BAD_GATEWAY,
                &format!("Vertex upstream call failed: {e}"),
            );
        }
    };

    if !upstream_resp.status().is_success() {
        if wants_stream {
            state.router_state.end_request(&model.id);
        }
        let status_code = upstream_resp.status().as_u16();
        let err_text = upstream_resp
            .text()
            .await
            .unwrap_or_else(|_| "Unknown upstream error".into());
        write_bridge_evidence(
            state,
            model,
            auth_ctx,
            target_group,
            tenant_id,
            correlation_id,
            bridge_chain,
            BridgeOutcome {
                request_body_str: request_body_str.clone(),
                request_headers: request_headers.clone(),
                openai_request: openai_request.clone(),
                response_body_str: err_text.clone(),
                model_requested: model_requested.clone(),
                prompt_tokens: None,
                completion_tokens: None,
                total_tokens: None,
                streaming: wants_stream,
                status_code,
                duration_ms: start.elapsed().as_millis() as f64,
                user_agent: user_agent.clone(),
                pii_redacted: input_pii_redacted,
                write_proxy_log: true,
                stream_outcome: crate::services::stream_guard::StreamOutcome::Completed,
            },
        )
        .await;
        return gemini_error(
            StatusCode::from_u16(status_code).unwrap_or(StatusCode::BAD_GATEWAY),
            &err_text,
        );
    }

    if wants_stream {
        // Streaming: pump SSE chunks through GeminiStreamNormalizer
        // so the client sees OpenAI-shape chunks, with the same output
        // guardrail state machine the OpenAI streaming path runs.
        use axum::response::sse::{Event, KeepAlive, Sse};
        use futures::stream::StreamExt;
        use tokio_stream::wrappers::ReceiverStream;

        // Fetched before the spawn: the guard needs the tenant's configs and
        // the task has no request scope of its own.
        let stream_guardrail_configs = match state
            .guardrails_cache
            .get_configs(
                tenant_id,
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
                    state,
                    &crate::services::llm_refusal::RefusalScope::new(
                        auth_ctx,
                        headers,
                        correlation_id,
                        "/chat/completions",
                        &crate::services::llm_refusal::RunAttribution::chain(
                            &bridge_chain.chain,
                            &bridge_chain.current_task_id,
                            bridge_chain.continuity,
                        ),
                    )
                    .started(start),
                    crate::constants::guardrail_surface::CHAT_OUTPUT,
                    &e,
                );
            }
        };

        let byte_stream = upstream_resp.bytes_stream();
        let (tx, rx) = tokio::sync::mpsc::channel::<Result<Event, axum::Error>>(32);
        let model_bg = model.clone();
        let state_bg = state.clone();
        let auth_bg = auth_ctx.clone();
        let group_bg = target_group.cloned();
        let tenant_bg = tenant_id.to_string();
        let corr_bg = correlation_id.to_string();
        let chain_bg = bridge_chain.clone();
        let req_body_bg = request_body_str.clone();
        let req_headers_bg = request_headers.clone();
        let openai_req_bg = openai_request.clone();
        let model_requested_bg = model_requested.clone();
        let user_agent_bg = user_agent.clone();

        tokio::spawn(async move {
            // Hold the RG concurrency slot for the whole stream.
            let _stream_conc_guard = conc_guard;
            use crate::services::llm_adapter::StreamNormalizer;
            // Resolve the right SSE normalizer via the adapter — the
            // GeminiAdapter knows to return `GeminiStreamNormalizer`
            // which translates native Gemini SSE → OpenAI SSE chunks.
            let provider_adapter = crate::services::llm_adapter::get_adapter(&model_bg.provider);
            let mut normalizer: Box<dyn StreamNormalizer> =
                provider_adapter.create_stream_normalizer();

            let mut stream_guard = crate::services::stream_guard::StreamGuard::new(
                stream_guardrail_configs.into_vec(),
                state_bg.http_client.clone(),
            )
            .for_run(tenant_bg.clone(), chain_bg.chain.root_task_id.to_string());
            let mut guardrail_blocked = false;
            let mut total_chunks = 0u32;
            // What actually went on the wire, post-redaction — the audit body
            // must match what the client received, not the upstream tokens.
            let mut wire_content = String::with_capacity(4096);
            // Gemini reports usage on the native chunks; the last block wins.
            let mut prompt_tokens: Option<i32> = None;
            let mut completion_tokens: Option<i32> = None;
            let mut total_tokens: Option<i32> = None;

            let mut buffer = String::with_capacity(4096);
            futures::pin_mut!(byte_stream);
            'stream: while let Some(chunk_result) = byte_stream.next().await {
                match chunk_result {
                    Ok(bytes) => {
                        buffer.push_str(&String::from_utf8_lossy(&bytes));
                        while let Some(pos) = buffer.find("\n\n") {
                            let event_str = buffer[..pos].to_string();
                            buffer.drain(..pos + 2);
                            for line in event_str.lines() {
                                if let Some(data) = line.strip_prefix("data: ") {
                                    // Read the provider's own counts off the
                                    // native chunk before translation.
                                    if let Ok(native) = serde_json::from_str::<Value>(data) {
                                        let (p, c, t) = gemini_usage_counts(&native);
                                        if p.is_some() {
                                            prompt_tokens = p;
                                        }
                                        if c.is_some() {
                                            completion_tokens = c;
                                        }
                                        if t.is_some() {
                                            total_tokens = t;
                                        }
                                    }
                                    for converted in normalizer.convert(data) {
                                        // Mid-stream abort.
                                        if stream_guard.run_aborted(&state_bg).await {
                                            let _ = crate::routes::llm::emit_stream_action(
                                                &tx,
                                                crate::services::stream_guard::StreamAction::Block(
                                                    crate::services::run_intervention::ABORTED_MESSAGE
                                                        .to_string(),
                                                ),
                                                &mut total_chunks,
                                                &mut guardrail_blocked,
                                                &mut wire_content,
                                                &corr_bg,
                                            )
                                            .await;
                                            break 'stream;
                                        }
                                        let action = stream_guard.process(&converted).await;
                                        if crate::routes::llm::emit_stream_action(
                                            &tx,
                                            action,
                                            &mut total_chunks,
                                            &mut guardrail_blocked,
                                            &mut wire_content,
                                            &corr_bg,
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
                    Err(e) => {
                        tracing::warn!("Vertex-Gemini stream error: {e}");
                        break;
                    }
                }
            }

            // Flush any text the sync-mode segmenter is still holding.
            if !guardrail_blocked {
                let action = stream_guard.finish().await;
                let _ = crate::routes::llm::emit_stream_action(
                    &tx,
                    action,
                    &mut total_chunks,
                    &mut guardrail_blocked,
                    &mut wire_content,
                    &corr_bg,
                )
                .await;
            }

            state_bg.router_state.end_request(&model_bg.id);

            write_bridge_evidence(
                &state_bg,
                &model_bg,
                &auth_bg,
                group_bg.as_ref(),
                &tenant_bg,
                &corr_bg,
                &chain_bg,
                BridgeOutcome {
                    request_body_str: req_body_bg,
                    request_headers: req_headers_bg,
                    openai_request: openai_req_bg,
                    response_body_str: wire_content,
                    model_requested: model_requested_bg,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    streaming: true,
                    // A guardrail block still terminated a 200 stream; the
                    // block is stamped on this row (`stream_outcome`), which
                    // names the run and step.
                    status_code: StatusCode::OK.as_u16(),
                    duration_ms: start.elapsed().as_millis() as f64,
                    user_agent: user_agent_bg,
                    pii_redacted: input_pii_redacted,
                    write_proxy_log: true,
                    stream_outcome: stream_guard.outcome(),
                },
            )
            .await;
        });

        return Sse::new(ReceiverStream::new(rx))
            .keep_alive(KeepAlive::default())
            .into_response();
    }

    // Non-streaming: buffer the full response, translate native →
    // OpenAI shape, guard the output, then return.
    let response_bytes = match upstream_resp.bytes().await {
        Ok(b) => b,
        Err(e) => {
            return gemini_error(
                StatusCode::BAD_GATEWAY,
                &format!("Failed to read Vertex response body: {e}"),
            );
        }
    };
    let mut response_json: Value = match serde_json::from_slice(&response_bytes) {
        Ok(v) => v,
        Err(_) => return (StatusCode::OK, response_bytes.to_vec()).into_response(),
    };

    // Counts come off the NATIVE block, before translation rewrites it.
    let (prompt_tokens, completion_tokens, total_tokens) = gemini_usage_counts(&response_json);

    let provider_adapter = crate::services::llm_adapter::get_adapter(&model.provider);
    provider_adapter.normalize_response(&mut response_json);

    // Output guardrails on the translated (OpenAI-shape) choices, so a
    // block returns the same envelope this surface's clients already
    // handle. A Block short-circuits and its helper writes the
    // authoritative audit row; a Redaction is spliced back in.
    let mut guardrail_block: Option<Response> = None;
    let mut output_pii = false;
    let choice_count = response_json
        .get("choices")
        .and_then(|c| c.as_array())
        .map(|a| a.len())
        .unwrap_or(0);
    'guard: for ci in 0..choice_count {
        let text = response_json
            .pointer(&format!("/choices/{ci}/message/content"))
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        let Some(text) = text else { continue };
        if text.is_empty() {
            continue;
        }
        match llm_preflight::apply_output_guardrails(
            state,
            tenant_id,
            &crate::services::llm_refusal::RefusalScope::new(
                auth_ctx,
                headers,
                correlation_id,
                "/chat/completions",
                &crate::services::llm_refusal::RunAttribution::chain(
                    &bridge_chain.chain,
                    &bridge_chain.current_task_id,
                    bridge_chain.continuity,
                ),
            )
            .started(start),
            &text,
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
                if let Some(slot) =
                    response_json.pointer_mut(&format!("/choices/{ci}/message/content"))
                {
                    *slot = Value::String(new_text);
                }
                if pii {
                    output_pii = true;
                }
            }
        }
    }

    // Output checks read `message.content` only. The Gemini adapter maps no
    // reasoning into this body today; if it ever does, it goes no further
    // where a policy can block or redact.
    if guardrail_block.is_none()
        && let Err(resp) = llm_preflight::withhold_unscanned_reasoning(
            state,
            tenant_id,
            &crate::services::llm_refusal::RefusalScope::new(
                auth_ctx,
                headers,
                correlation_id,
                "/chat/completions",
                &crate::services::llm_refusal::RunAttribution::chain(
                    &bridge_chain.chain,
                    &bridge_chain.current_task_id,
                    bridge_chain.continuity,
                ),
            )
            .started(start),
            &mut response_json,
        )
        .await
    {
        guardrail_block = Some(resp);
    }

    let final_body_str = serde_json::to_string(&response_json).unwrap_or_default();
    write_bridge_evidence(
        state,
        model,
        auth_ctx,
        target_group,
        tenant_id,
        correlation_id,
        bridge_chain,
        BridgeOutcome {
            request_body_str,
            request_headers,
            openai_request,
            response_body_str: final_body_str,
            model_requested,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            streaming: false,
            status_code: StatusCode::OK.as_u16(),
            duration_ms: start.elapsed().as_millis() as f64,
            user_agent,
            pii_redacted: input_pii_redacted || output_pii,
            // On a block the guardrail helper already wrote the row.
            write_proxy_log: guardrail_block.is_none(),
            stream_outcome: crate::services::stream_guard::StreamOutcome::Completed,
        },
    )
    .await;

    if let Some(resp) = guardrail_block {
        return resp;
    }

    (StatusCode::OK, Json(response_json)).into_response()
}

// ---------------------------------------------------------------------------
// POST /v1beta/models/{model}:streamGenerateContent
// ---------------------------------------------------------------------------

type GeminiSseSender = tokio::sync::mpsc::Sender<Result<axum::response::sse::Event, axum::Error>>;

/// Build a minimal Gemini streaming chunk carrying a single text delta.
fn gemini_text_chunk(text: &str) -> String {
    json!({
        "candidates": [{
            "content": {"parts": [{"text": text}], "role": "model"},
            "index": 0
        }]
    })
    .to_string()
}

/// Emit redacted text segments (a [`TextAction`](crate::services::stream_guard::TextAction))
/// as Gemini SSE chunks. Accumulates the on-the-wire text into
/// `wire_content` for the audit log. Returns `true` to stop forwarding.
async fn emit_gemini_text_action(
    tx: &GeminiSseSender,
    action: crate::services::stream_guard::TextAction,
    total_chunks: &mut u32,
    guardrail_blocked: &mut bool,
    wire_content: &mut String,
    corr_id: &str,
) -> bool {
    use crate::services::stream_guard::TextAction;
    use axum::response::sse::Event;
    match action {
        TextAction::Block(reason) => {
            *guardrail_blocked = true;
            tracing::warn!(correlation_id = %corr_id, %reason, "Streaming output guardrail block (gemini native)");
            // Gemini has no error SSE event — emit a terminal finishReason
            // chunk so the client stops cleanly, then end the stream.
            let evt = json!({
                "candidates": [{
                    "content": {"parts": [], "role": "model"},
                    "finishReason": "OTHER",
                    "index": 0
                }]
            });
            let _ = tx.send(Ok(Event::default().data(evt.to_string()))).await;
            true
        }
        TextAction::Emit(segments) => {
            for s in segments {
                wire_content.push_str(&s);
                if tx
                    .send(Ok(Event::default().data(gemini_text_chunk(&s))))
                    .await
                    .is_err()
                {
                    return true;
                }
                *total_chunks += 1;
            }
            false
        }
    }
}

/// Redact (sync) or block-scan (async) one native Gemini SSE chunk,
/// re-injecting redacted text into `candidates[].content.parts[].text`
/// while preserving the envelope (finishReason / usageMetadata /
/// safetyRatings). Returns `true` to stop forwarding.
///
/// Multi-candidate chunks (`n > 1`, rare for streaming) are forwarded
/// verbatim with only a block-scan, to avoid dropping non-primary
/// candidates we don't reconstruct.
async fn redact_and_emit_gemini_event(
    tx: &GeminiSseSender,
    event_data: &str,
    stream_guard: &mut crate::services::stream_guard::StreamGuard,
    total_chunks: &mut u32,
    guardrail_blocked: &mut bool,
    wire_content: &mut String,
    corr_id: &str,
) -> bool {
    use crate::services::stream_guard::TextAction;
    use axum::response::sse::Event;

    let mut chunk: Value = match serde_json::from_str(event_data) {
        Ok(v) => v,
        Err(_) => {
            // Non-JSON frame — forward verbatim.
            if tx
                .send(Ok(Event::default().data(event_data)))
                .await
                .is_err()
            {
                return true;
            }
            *total_chunks += 1;
            return false;
        }
    };

    let n_candidates = chunk
        .get("candidates")
        .and_then(|c| c.as_array())
        .map(|a| a.len())
        .unwrap_or(0);

    // Primary-candidate text delta.
    let text: String = chunk
        .get("candidates")
        .and_then(|c| c.get(0))
        .and_then(|c| c.get("content"))
        .and_then(|c| c.get("parts"))
        .and_then(|p| p.as_array())
        .map(|parts| {
            parts
                .iter()
                .filter_map(|p| p.get("text").and_then(|t| t.as_str()))
                .collect::<String>()
        })
        .unwrap_or_default();

    let has_terminal = chunk
        .get("candidates")
        .and_then(|c| c.get(0))
        .and_then(|c| c.get("finishReason"))
        .is_some()
        || chunk.get("usageMetadata").is_some();

    // Async mode, or multi-candidate: forward verbatim, scan for Block only.
    if !stream_guard.is_sync() || n_candidates > 1 {
        if !text.is_empty() {
            if let TextAction::Block(reason) = stream_guard.process_text(&text).await {
                return emit_gemini_text_action(
                    tx,
                    TextAction::Block(reason),
                    total_chunks,
                    guardrail_blocked,
                    wire_content,
                    corr_id,
                )
                .await;
            }
            wire_content.push_str(&text);
        }
        if tx
            .send(Ok(Event::default().data(chunk.to_string())))
            .await
            .is_err()
        {
            return true;
        }
        *total_chunks += 1;
        return false;
    }

    // Sync mode: redact the text, hold back across frames.
    if !text.is_empty() {
        let action = stream_guard.process_text(&text).await;
        if emit_gemini_text_action(
            tx,
            action,
            total_chunks,
            guardrail_blocked,
            wire_content,
            corr_id,
        )
        .await
        {
            return true;
        }
    }

    if has_terminal {
        // Flush the held-back tail before the structural frame…
        let action = stream_guard.finish_text().await;
        if emit_gemini_text_action(
            tx,
            action,
            total_chunks,
            guardrail_blocked,
            wire_content,
            corr_id,
        )
        .await
        {
            return true;
        }
        // …then forward the terminal chunk with its (already-emitted) text
        // parts stripped so nothing unredacted is re-sent.
        if let Some(parts) = chunk
            .get_mut("candidates")
            .and_then(|c| c.get_mut(0))
            .and_then(|c| c.get_mut("content"))
            .and_then(|c| c.get_mut("parts"))
            .and_then(|p| p.as_array_mut())
        {
            parts.clear();
        }
        if tx
            .send(Ok(Event::default().data(chunk.to_string())))
            .await
            .is_err()
        {
            return true;
        }
        *total_chunks += 1;
    } else if text.is_empty() {
        // Non-terminal, non-text frame (e.g. promptFeedback) — forward as-is.
        if tx
            .send(Ok(Event::default().data(chunk.to_string())))
            .await
            .is_err()
        {
            return true;
        }
        *total_chunks += 1;
    }
    // Non-terminal text-only frame: text already emitted redacted above;
    // nothing further to forward.

    false
}

/// Streaming variant of `generate_content`. Same governance/cache/log
/// pipeline as the non-streaming path, but the upstream response is
/// pumped to the client as Server-Sent Events. Each chunk is
/// forwarded verbatim — both client and upstream speak Gemini native
/// SSE (the `?alt=sse` URL param tells Google to use SSE framing).
pub(crate) async fn stream_generate_content(
    state: SharedState,
    model_path: String,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    mut gemini_body: Value,
) -> Response {
    use axum::response::sse::{Event, KeepAlive, Sse};
    use futures::stream::StreamExt;
    use tokio_stream::wrappers::ReceiverStream;

    let start = Instant::now();
    let correlation_id = route_helpers::extract_correlation_id(&headers);
    let tenant_id = match extract_tenant_id(tenant) {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    let model_name = model_path
        .strip_prefix("models/")
        .unwrap_or(&model_path)
        .to_string();

    tracing::debug!(
        model_requested = %model_name,
        "Gemini native streamGenerateContent request"
    );

    // Preflight (shared shape with generate_content).
    // Call chain. Same shape as routes/llm.rs:
    // parse the inbound delegation headers (or mint a root) so this
    // Gemini-native request joins the cross-surface trace and emits
    // chain headers onto the upstream send.
    let tenant_key = derive_tenant_key(state.config.encryption_key.as_bytes(), &tenant_id);
    let (call_chain, current_task_id, trace_continuity) = parse_or_mint(
        |n| {
            headers
                .get(n)
                .and_then(|v| v.to_str().ok())
                .map(String::from)
        },
        &tenant_key,
    );
    let current_task_id_str = current_task_id.to_string();

    let pf_body = gemini_body.to_string();
    // Where this call sits in its run — the run the pre-flight run controls
    // check, and the run every refusal or block row of this request lands on.
    let run_attr = crate::services::llm_refusal::RunAttribution::chain(
        &call_chain,
        &current_task_id_str,
        trace_continuity,
    );
    let pf = match llm_preflight::run_preflight(
        &state,
        &headers,
        &tenant_id,
        &correlation_id,
        Some(&model_name),
        None,
        // Arm the run-level ceilings with the chain minted above.
        &PreflightOptions::with_guardrails(run_attr.clone()),
        gemini_error,
        client_addr.ip(),
        None,
        Some(&pf_body),
    )
    .await
    {
        Ok(pf) => pf,
        Err(resp) => return resp,
    };
    let auth_ctx = pf.auth_ctx;
    let model = pf.model;
    let target_group = pf.target_group;
    // The output this call was admitted for (every token and spend cap's
    // room): bound `maxOutputTokens` — and the thinking budget inside it.
    if let Some(ceiling) = pf.out_ceiling {
        crate::services::llm_governance::apply_output_ceiling(&mut gemini_body, ceiling);
    }

    let trace_root_kind =
        proxy_logger::root_kind_for_auth(&auth_ctx.auth_type, call_chain.depth == 0);

    // Agent authorization, the autonomy kill switch and
    // semantic policy, the same three planes the OpenAI surface runs. Without
    // them this mounted route was a way past a suspended AI System.
    if let Err(resp) = llm_preflight::enforce_actor_planes(
        &state,
        &auth_ctx,
        &tenant_id,
        &model,
        target_group.as_ref(),
        Some(model_name.clone()),
        &serde_json::to_string(gemini_body.get("contents").unwrap_or(&Value::Null))
            .unwrap_or_default(),
        &format!("/v1beta/models/{model_name}"),
        &headers,
        &correlation_id,
        start,
        &call_chain,
        &current_task_id_str,
        trace_continuity,
        trace_root_kind,
        gemini_error,
    )
    .await
    {
        return resp;
    }

    if !matches!(
        model.provider.as_str(),
        "gemini" | "google_ai_studio" | "vertex_ai" | "vertex"
    ) {
        return gemini_error(
            StatusCode::BAD_REQUEST,
            &format!(
                "Provider '{}' isn't a Gemini-family provider. Use the \
                 OpenAI surface at /v1/proxy/llm/chat/completions for \
                 cross-provider clients.",
                model.provider
            ),
        );
    }

    // Data-residency check (parity with generate_content / chat). A
    // Blocked decision writes a residency_violation row and 403s before the
    // upstream call; allowed/not-evaluated are stamped on the streaming
    // success row below.
    // One shared implementation, see
    // `llm_preflight::enforce_residency`.
    let residency = match llm_preflight::enforce_residency(
        &state,
        &auth_ctx,
        &tenant_id,
        &model,
        Some(model_name.clone()),
        &format!("/v1beta/models/{model_name}:streamGenerateContent"),
        "Gemini streaming",
        &headers,
        &correlation_id,
        start,
        &call_chain,
        &current_task_id_str,
        trace_continuity,
        trace_root_kind,
        gemini_error,
    )
    .await
    {
        Ok(o) => o,
        Err(resp) => return resp,
    };
    let provider_region_for_audit = residency.provider_region;
    let residency_decision = residency.decision;

    let input_text = extract_input_text(&gemini_body);
    let guard_target = llm_preflight::LlmAuditTarget::from_model(
        &model,
        Some(model_name.to_string()),
        "/chat/completions",
    );
    // Tracks whether an input guardrail redacted a PII category — stamped
    // on the streaming success audit row below.
    let mut input_pii_redacted = false;
    match llm_preflight::apply_guardrails_with_redaction(
        &state,
        &tenant_id,
        &crate::services::llm_refusal::RefusalScope::new(
            &auth_ctx,
            &headers,
            &correlation_id,
            &format!("/v1beta/models/{model_name}:streamGenerateContent"),
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
            apply_redactions_to_input(&mut gemini_body, &redaction.content);
            input_pii_redacted = redaction.pii;
        }
        Ok(None) => {}
    }

    // RG governance enforcement at PARITY with chat (see
    // generate_content for the chat-view rationale). Runs before upstream
    // dispatch so caps/tool-filtering/banned-pattern/system-prompt
    // injection cover the streaming path too.
    {
        let mut gov_view = gemini_request_to_chat_view(&gemini_body);
        match llm_preflight::apply_governance(
            &state,
            &target_group,
            &model.id,
            &mut gov_view,
            gemini_error,
            &crate::services::llm_refusal::RefusalScope {
                auth_ctx: &auth_ctx,
                headers: &headers,
                correlation_id: &correlation_id,
                endpoint: &format!("/v1beta/models/{model_name}:streamGenerateContent"),
                started: Some(start),
                model: Some((&model, Some(model_name.clone()))),
                group_id: None,
                run: Some(&crate::services::llm_refusal::RunAttribution::chain(
                    &call_chain,
                    &current_task_id_str,
                    trace_continuity,
                )),
                request_body: None,
            },
        )
        .await
        {
            Ok(_caps) => apply_gemini_chat_view_back(&mut gemini_body, &gov_view),
            Err(resp) => return resp,
        }
    }

    let mut body = gemini_body;
    strip_caller_cache_control(&mut body);

    // Prompt-cache marker injection (same as non-streaming).
    let prompt_cache = state
        .prompt_cache_policy
        .session(&state, &tenant_id, target_group.as_ref(), &model)
        .await;
    if let Some(decision) = prompt_cache.mark(&mut body, &model).await {
        crate::services::prompt_caching::record_decision(&decision);
    }

    let (upstream_url, auth_header) =
        match resolve_gemini_endpoint(&model, GeminiAction::StreamGenerateContent).await {
            Ok(v) => v,
            Err(resp) => return resp,
        };

    if let Some(obj) = body.as_object_mut() {
        obj.remove("stream");
    }

    state.router_state.increment_rpm(&model.id);
    state.router_state.begin_request(&model.id);

    let request_body_str = serde_json::to_string(&body).unwrap_or_default();
    let timeout_secs = model.timeout.map(|t| t as u64).unwrap_or(120);
    let mut req = state
        .http_client
        .post(&upstream_url)
        .header("Content-Type", "application/json")
        .header("Accept", "text/event-stream")
        .header(auth_header.0.as_str(), auth_header.1.as_str())
        .timeout(std::time::Duration::from_secs(timeout_secs));
    // Chain headers on Gemini streaming.
    for (k, v) in call_chain.to_header_pairs(&tenant_key) {
        req = req.header(k, v);
    }
    let result = req.json(&body).send().await;

    let upstream_resp = match result {
        Ok(r) => {
            // Capture the vendor's request id where one is offered.
            // Gemini does not currently send `x-request-id`/`request-id`, so
            // this is a no-op today; wiring it anyway means the surface is not
            // structurally incapable of carrying the handle if Google adds one.
            crate::middleware::run_end::record_upstream_response(r.status().as_u16(), r.headers());
            r
        }
        Err(e) => {
            state.router_state.end_request(&model.id);
            return finalise_error(
                &state,
                &auth_ctx,
                &model,
                target_group.as_ref(),
                &tenant_id,
                &headers,
                &correlation_id,
                Some(model_name.clone()),
                &request_body_str,
                start,
                StatusCode::BAD_GATEWAY,
                &format!("Upstream connection error: {e}"),
                &call_chain,
                &current_task_id_str,
                trace_continuity,
                trace_root_kind,
            )
            .await;
        }
    };

    if !upstream_resp.status().is_success() {
        state.router_state.end_request(&model.id);
        let status_code = upstream_resp.status().as_u16();
        let err_body = upstream_resp
            .text()
            .await
            .unwrap_or_else(|_| "Unknown upstream error".into());
        return finalise_error(
            &state,
            &auth_ctx,
            &model,
            target_group.as_ref(),
            &tenant_id,
            &headers,
            &correlation_id,
            Some(model_name),
            &request_body_str,
            start,
            StatusCode::from_u16(status_code).unwrap_or(StatusCode::BAD_GATEWAY),
            &err_body,
            &call_chain,
            &current_task_id_str,
            trace_continuity,
            trace_root_kind,
        )
        .await;
    }

    // Cluster-wide RG / per-model concurrency gate — guard moved into the SSE
    // task below, released at stream-end via Drop. Saturation → 429.
    let stream_conc_guard = match crate::services::llm_refusal::acquire_concurrency(
        &state,
        target_group.as_ref(),
        &model,
        &crate::services::llm_refusal::RefusalScope {
            auth_ctx: &auth_ctx,
            headers: &headers,
            correlation_id: &correlation_id,
            endpoint: &format!("/v1beta/models/{model_name}:streamGenerateContent"),
            started: Some(start),
            model: Some((&model, Some(model_name.clone()))),
            group_id: None,
            run: Some(&crate::services::llm_refusal::RunAttribution::chain(
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
        Err(resp) => {
            state.router_state.end_request(&model.id);
            return resp;
        }
    };

    let byte_stream = upstream_resp.bytes_stream();
    let (tx, rx) = tokio::sync::mpsc::channel::<Result<Event, axum::Error>>(32);

    // Background context for the post-stream proxy_log write.
    let state_bg = state.clone();
    let auth_bg = auth_ctx.clone();
    let model_bg = model.clone();
    let corr_id = correlation_id.clone();
    let tenant_id_bg = tenant_id.clone();
    let target_group_bg: Option<crate::models::db::ResourceGroup> = target_group.clone();
    let user_agent = extract_user_agent(&headers);
    // A post-response block inside the stream (tool-call governance) writes
    // its refusal row on this run and step.
    let run_attr_bg = run_attr.clone();
    let headers_bg = headers.clone();
    let model_name_bg = model_name.clone();
    let request_body_str_bg = request_body_str;
    let upstream_url_bg = upstream_url;
    // Call-chain stamping for the spawned streaming log task.
    // `trace_continuity` / `trace_root_kind` are Copy; clone the chain
    // and current-task-id string into the closure.
    let call_chain_bg = call_chain.clone();
    let current_task_id_bg = current_task_id_str.clone();
    // Residency outcome stamped on the streaming success row.
    let residency_decision_bg = residency_decision.clone();
    let provider_region_bg = provider_region_for_audit.clone();
    // Model id for the post-stream tool-call governance check.
    let model_id_bg = model.id.clone();

    // Output guardrails for the streamed response (chat_output surface).
    // The native Gemini `:streamGenerateContent` surface speaks Gemini SSE,
    // so redaction operates on `candidates[].content.parts[].text` in
    // place — without it, PII/secrets would egress unredacted here.
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
                    &format!("/v1beta/models/{model_name}:streamGenerateContent"),
                    &run_attr,
                )
                .started(start),
                crate::constants::guardrail_surface::CHAT_OUTPUT,
                &e,
            );
        }
    };

    proxy_metrics::stream_opened();

    tokio::spawn(async move {
        // Hold the RG concurrency slot for the whole stream.
        let _stream_conc_guard = stream_conc_guard;
        let mut buffer = String::with_capacity(4096);
        let mut total_chunks = 0u32;
        let mut total_response_bytes = 0usize;
        let mut last_usage_metadata: Option<Value> = None;
        // On-the-wire (post-guard / post-redaction) response text, recorded
        // in the proxy log so the audit body mirrors the wire.
        let mut accumulated_content = String::with_capacity(4096);
        // Output-guardrail state machine for this stream.
        let mut stream_guard = crate::services::stream_guard::StreamGuard::new(
            stream_guardrail_configs.into_vec(),
            state_bg.http_client.clone(),
        )
        .for_run(tenant_id_bg.clone(), call_chain_bg.root_task_id.to_string());
        let mut guardrail_blocked = false;
        // Accumulate emitted `functionCall` parts across the stream so
        // post-response governance (banned-arg patterns + AST argument
        // policies + tool-call rate counters) can scan them at stream end.
        // Gemini streams complete functionCall objects per part (not the
        // fragmented argument deltas OpenAI uses), so a simple collect works.
        let mut collected_tool_calls: Vec<Value> = Vec::new();
        // From the first chunk that asks for a tool, the rest of the turn is
        // held, unprocessed, until the calls are judged (data scope, argument
        // policies): a client runs a function call once it has it, so a
        // refusal sent after the call came too late. Replayed through the
        // same emit path if the calls pass; dropped if refused.
        let mut held_events: Vec<String> = Vec::new();

        futures::pin_mut!(byte_stream);
        'outer: while let Some(chunk_result) = byte_stream.next().await {
            match chunk_result {
                Ok(bytes) => {
                    total_response_bytes += bytes.len();
                    // Safety ceiling — same value the Anthropic native
                    // pipeline uses.
                    if total_response_bytes > 64 * 1024 * 1024 {
                        tracing::warn!(
                            total_bytes = total_response_bytes,
                            "Gemini native stream exceeded safety ceiling, aborting"
                        );
                        break;
                    }
                    buffer.push_str(&String::from_utf8_lossy(&bytes));

                    // Parse SSE frames: split on double newline. Each
                    // frame has a `data: {json}` line.
                    while let Some(pos) = buffer.find("\n\n") {
                        let event_str = buffer[..pos].to_string();
                        buffer.drain(..pos + 2);

                        let mut event_data = String::new();
                        for line in event_str.lines() {
                            if let Some(d) = line.strip_prefix("data: ") {
                                event_data = d.to_string();
                            }
                        }
                        if event_data.is_empty() {
                            continue;
                        }

                        // An operator can stop the run
                        // mid-stream; dropping the upstream connection is
                        // what stops the provider generating, and billing.
                        if stream_guard.run_aborted(&state_bg).await {
                            let _ = emit_gemini_text_action(
                                &tx,
                                crate::services::stream_guard::TextAction::Block(
                                    crate::services::run_intervention::ABORTED_MESSAGE.to_string(),
                                ),
                                &mut total_chunks,
                                &mut guardrail_blocked,
                                &mut accumulated_content,
                                &corr_id,
                            )
                            .await;
                            break 'outer;
                        }

                        // Observer: tap the final chunk's usage block
                        // for the post-stream proxy_log, and harvest any
                        // functionCall parts for G9 governance.
                        if let Ok(parsed) = serde_json::from_str::<Value>(&event_data) {
                            if let Some(u) = parsed.get("usageMetadata") {
                                last_usage_metadata = Some(u.clone());
                            }
                            if let Some(cands) = parsed.get("candidates").and_then(|c| c.as_array())
                            {
                                for cand in cands {
                                    if let Some(parts) =
                                        cand.pointer("/content/parts").and_then(|p| p.as_array())
                                    {
                                        for part in parts {
                                            if let Some(fc) = part.get("functionCall") {
                                                let name =
                                                    fc.get("name").cloned().unwrap_or(Value::Null);
                                                let args_str = fc
                                                    .get("args")
                                                    .map(|a| {
                                                        serde_json::to_string(a).unwrap_or_default()
                                                    })
                                                    .unwrap_or_default();
                                                collected_tool_calls.push(json!({
                                                    "id": Value::Null,
                                                    "type": "function",
                                                    "function": {
                                                        "name": name,
                                                        "arguments": args_str
                                                    }
                                                }));
                                            }
                                        }
                                    }
                                }
                            }
                        }

                        if !collected_tool_calls.is_empty() {
                            held_events.push(event_data);
                            continue;
                        }

                        // Run the output guard over this chunk's text,
                        // re-injecting redacted text into the Gemini envelope.
                        if redact_and_emit_gemini_event(
                            &tx,
                            &event_data,
                            &mut stream_guard,
                            &mut total_chunks,
                            &mut guardrail_blocked,
                            &mut accumulated_content,
                            &corr_id,
                        )
                        .await
                        {
                            break 'outer; // Block or client disconnect
                        }
                    }
                }
                Err(e) => {
                    tracing::warn!("Gemini native stream error: {e}");
                    break;
                }
            }
        }

        // Post-response tool-call governance on the accumulated
        // functionCall parts. Mirrors the non-streaming path: a banned-arg
        // pattern / argument-policy / rate-limit hit blocks the response.
        // `apply_response_governance` owns its own block audit row. On a
        // block we emit a terminal Gemini finishReason chunk so the client
        // stops cleanly (Gemini SSE has no error event), matching the
        // streaming output-guard block contract.
        let mut tool_governance_blocked = false;
        if !collected_tool_calls.is_empty() {
            let tool_view = json!({
                "choices": [{
                    "index": 0,
                    "message": { "role": "assistant", "tool_calls": collected_tool_calls }
                }]
            });
            if llm_preflight::apply_response_governance(
                &state_bg,
                &target_group_bg,
                &model_id_bg,
                &tool_view,
                gemini_error,
                &crate::services::llm_refusal::RefusalScope::new(
                    &auth_bg,
                    &headers_bg,
                    &corr_id,
                    &format!("/v1beta/models/{model_name_bg}:streamGenerateContent"),
                    &run_attr_bg,
                )
                .started(start),
            )
            .await
            .is_err()
            {
                tool_governance_blocked = true;
                tracing::warn!(
                    correlation_id = %corr_id,
                    "Streamed Gemini tool-call response blocked by output governance"
                );
                let evt = json!({
                    "candidates": [{
                        "content": {"parts": [], "role": "model"},
                        "finishReason": "OTHER",
                        "index": 0
                    }]
                });
                let _ = tx.send(Ok(Event::default().data(evt.to_string()))).await;
            }
        }
        // The calls passed: the held rest of the turn goes out now.
        if !tool_governance_blocked {
            for event_data in &held_events {
                if redact_and_emit_gemini_event(
                    &tx,
                    event_data,
                    &mut stream_guard,
                    &mut total_chunks,
                    &mut guardrail_blocked,
                    &mut accumulated_content,
                    &corr_id,
                )
                .await
                {
                    break; // Block or client disconnect
                }
            }
        }

        // Flush any sync-mode held-back tail (no-op in the common case —
        // the terminal chunk already flushed it). After the held turn, so
        // the text stays in order; not after a refusal's terminal chunk.
        if !guardrail_blocked && !tool_governance_blocked {
            let action = stream_guard.finish_text().await;
            let _ = emit_gemini_text_action(
                &tx,
                action,
                &mut total_chunks,
                &mut guardrail_blocked,
                &mut accumulated_content,
                &corr_id,
            )
            .await;
        }

        state_bg.router_state.end_request(&model_bg.id);
        proxy_metrics::stream_closed();

        // Post-stream proxy_log write. Mirrors the non-streaming path's
        // logging so dashboards see uniform rows for both modes.
        let duration_ms = start.elapsed().as_millis() as f64;
        proxy_metrics::record_request("llm", "POST", 200, &model_bg.provider);
        proxy_metrics::record_latency("llm", "POST", &model_bg.provider, duration_ms / 1000.0);

        let endpoint_path = format!("/v1beta/models/{model_name_bg}:streamGenerateContent");
        let mut log = proxy_logger::build_proxy_log(
            "llm",
            &auth_bg,
            "POST",
            &endpoint_path,
            Some(&upstream_url_bg),
            Some(&model_bg.id),
            200,
            duration_ms,
            true,
            None,
            &corr_id,
        );
        log.proxy_subtype =
            Some(crate::constants::proxy_subtype::GEMINI_STREAM_GENERATE_CONTENT.to_string());
        log.resource_group_id = target_group_bg.as_ref().map(|g| g.id.clone());
        log.user_agent = user_agent;
        log.request_size_bytes = Some(request_body_str_bg.len() as i32);
        log.request_body_preview = Some(body_preview(&request_body_str_bg));
        log.response_size_bytes = Some(total_response_bytes as i32);
        log.llm_provider = Some(model_bg.provider.clone());
        log.llm_model_used = Some(model_bg.model_name.clone());
        log.llm_model_requested = Some(model_name_bg);
        log.llm_streaming = Some(true);
        if input_pii_redacted {
            proxy_logger::apply_pii_detected(&mut log);
        }
        // Stamp residency outcome (allowed / not-evaluated) on the
        // streaming success row.
        match &residency_decision_bg {
            Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
            None => {
                proxy_logger::apply_residency_not_evaluated(&mut log, provider_region_bg.as_deref())
            }
        }
        log.response_body_preview = Some(
            serde_json::to_string(&json!({
                "streaming": true,
                "native_gemini": true,
                "chunks": total_chunks,
                "usage": last_usage_metadata.clone(),
                // Post-guard (redacted) text actually sent to the client.
                "content": accumulated_content,
            }))
            .unwrap_or_default(),
        );

        if let Some(ref u) = last_usage_metadata {
            log.llm_prompt_tokens = u
                .get("promptTokenCount")
                .and_then(|v| v.as_i64())
                .map(|v| v as i32);
            log.llm_completion_tokens = u
                .get("candidatesTokenCount")
                .and_then(|v| v.as_i64())
                .map(|v| v as i32);
            log.llm_total_tokens = u
                .get("totalTokenCount")
                .and_then(|v| v.as_i64())
                .map(|v| v as i32);
        }

        // Prompt-cache counter (same dispatcher as non-streaming).
        let pc_mechanism = crate::services::prompt_cache_usage::mechanism_for_provider(
            &model_bg.provider,
            &model_bg.model_name,
        );
        if let Some(ref u) = last_usage_metadata {
            let pc_usage = crate::services::prompt_cache_usage::extract_usage_from_response(
                &json!({ "usageMetadata": u }),
                pc_mechanism,
                &model_bg,
            );
            if pc_usage.write_tokens > 0 || pc_usage.read_tokens > 0 {
                log.prompt_cache_write_tokens = Some(pc_usage.write_tokens);
                log.prompt_cache_read_tokens = Some(pc_usage.read_tokens);
                log.prompt_cache_savings_usd = Some(pc_usage.savings_usd);
            }
        }

        let estimated_cost = llm_helpers::estimate_llm_cost(
            log.llm_prompt_tokens,
            log.llm_completion_tokens,
            model_bg.cost_per_input_token.unwrap_or(0.0),
            model_bg.cost_per_output_token.unwrap_or(0.0),
        );
        log.llm_estimated_cost_usd = estimated_cost;

        proxy_logger::apply_call_chain(
            &mut log,
            &call_chain_bg,
            &current_task_id_bg,
            trace_continuity,
            trace_root_kind,
        );
        // How the guard ended the stream (operator abort / guardrail block) is recorded on its own
        // outcome row — the row that names the run and step and carries the
        // tokens the provider billed before the guard stopped it.
        stream_guard.stamp_outcome(&mut log);
        state_bg
            .background_writer
            .write_proxy_log(state_bg.db.clone(), log.clone());

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
            log.llm_prompt_tokens,
            log.llm_completion_tokens,
            estimated_cost,
            model_bg.cost_per_input_token.unwrap_or(0.0),
            model_bg.cost_per_output_token.unwrap_or(0.0),
            &endpoint_path,
            &corr_id,
            200,
            duration_ms,
        );
    });

    Sse::new(ReceiverStream::new(rx))
        .keep_alive(KeepAlive::default())
        .into_response()
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Pull plaintext from a Gemini native body for guardrail input
/// scanning. Walks `systemInstruction.parts[].text` +
/// `contents[].parts[].text` only — multimodal parts (inline_data
/// images, file_data refs) and functionCall blocks are skipped here
/// because they're not what input guardrails check.
fn extract_input_text(body: &Value) -> String {
    let mut parts: Vec<String> = Vec::new();

    if let Some(si) = body.get("systemInstruction") {
        push_text_parts(si.get("parts"), &mut parts);
    }
    if let Some(si) = body.get("system_instruction") {
        push_text_parts(si.get("parts"), &mut parts);
    }

    if let Some(contents) = body.get("contents").and_then(|c| c.as_array()) {
        for entry in contents {
            push_text_parts(entry.get("parts"), &mut parts);
        }
    }

    parts.join("\n")
}

fn push_text_parts(parts_value: Option<&Value>, out: &mut Vec<String>) {
    if let Some(parts) = parts_value.and_then(|p| p.as_array()) {
        for part in parts {
            if let Some(t) = part.get("text").and_then(|t| t.as_str()) {
                if !t.is_empty() {
                    out.push(t.to_string());
                }
            }
        }
    }
}

/// Inverse of [`extract_input_text`] for Gemini: walks
/// `systemInstruction.parts[].text` then `contents[].parts[].text`
/// in the SAME order as the extractor and overwrites each non-
/// empty text slot with the next `\n`-separated segment of
/// `redacted`. Returns `false` on split-count mismatch — caller
/// then forwards the original body rather than partial-rewrite
/// into a corrupted Gemini envelope.
fn apply_redactions_to_input(body: &mut Value, redacted: &str) -> bool {
    fn count_text_parts(parts_value: Option<&Value>) -> usize {
        parts_value
            .and_then(|p| p.as_array())
            .map(|parts| {
                parts
                    .iter()
                    .filter(|p| {
                        p.get("text")
                            .and_then(|t| t.as_str())
                            .map(|s| !s.is_empty())
                            .unwrap_or(false)
                    })
                    .count()
            })
            .unwrap_or(0)
    }
    let mut expected = 0usize;
    if let Some(si) = body.get("systemInstruction") {
        expected += count_text_parts(si.get("parts"));
    }
    if let Some(si) = body.get("system_instruction") {
        expected += count_text_parts(si.get("parts"));
    }
    if let Some(contents) = body.get("contents").and_then(|c| c.as_array()) {
        for entry in contents {
            expected += count_text_parts(entry.get("parts"));
        }
    }
    if expected == 0 {
        return false;
    }
    let parts: Vec<&str> = redacted.split('\n').collect();
    if parts.len() != expected {
        tracing::warn!(
            target: "guardrails",
            surface = crate::constants::guardrail_surface::CHAT_INPUT,
            "Gemini redaction split-count mismatch (got {}, expected {}); forwarding original",
            parts.len(),
            expected,
        );
        return false;
    }
    fn replace_text_parts(parts_value: Option<&mut Value>, parts: &[&str], idx: &mut usize) {
        if let Some(arr) = parts_value.and_then(|p| p.as_array_mut()) {
            for part in arr.iter_mut() {
                if let Some(obj) = part.as_object_mut() {
                    let has_text = obj
                        .get("text")
                        .and_then(|t| t.as_str())
                        .map(|s| !s.is_empty())
                        .unwrap_or(false);
                    if has_text {
                        if let Some(p) = parts.get(*idx) {
                            obj.insert("text".to_string(), Value::String((*p).to_string()));
                        }
                        *idx += 1;
                    }
                }
            }
        }
    }
    let mut idx = 0usize;
    if let Some(si) = body.get_mut("systemInstruction") {
        replace_text_parts(si.get_mut("parts"), &parts, &mut idx);
    }
    if let Some(si) = body.get_mut("system_instruction") {
        replace_text_parts(si.get_mut("parts"), &parts, &mut idx);
    }
    if let Some(contents) = body.get_mut("contents").and_then(|c| c.as_array_mut()) {
        for entry in contents.iter_mut() {
            replace_text_parts(entry.get_mut("parts"), &parts, &mut idx);
        }
    }
    true
}

// ---------------------------------------------------------------------------
// Governance chat-view adapters (G6 + G9)
// ---------------------------------------------------------------------------
//
// G6/G9 reuse the SAME helpers the chat path uses
// (`apply_governance`, `apply_response_governance`) — those operate on
// the chat-completion JSON shape (`messages[]`,
// `choices[].message.tool_calls`). The Gemini-native surface carries its
// text/params/tools in a different shape, so these adapters build a
// chat-shaped *view* of the request/response, run the unchanged helper
// over the view, then splice the governed result back into the native
// Gemini shape. No helper is forked — only the text/tool-call/param
// extraction is adapted to the Gemini wire shape.

/// Build a chat-completion request *view* from a native Gemini body so
/// `apply_governance` (RG param caps + tool filtering + banned-pattern +
/// mandatory system-prompt injection) can run unchanged.
///
/// Mapping:
///   * `systemInstruction.parts[].text` → leading `system` message so
///     mandatory-fragment injection merges with the caller's system slot.
///   * `contents[]` (role + parts[].text) → `messages[]`. Gemini's
///     assistant role is `model`; we map it to `assistant` so the
///     banned-pattern user-message scan keys off the right role.
///   * `generationConfig.{temperature,topP,maxOutputTokens}` → the typed
///     chat fields so the caps apply.
///   * `tools` → the chat `tools` field so the tool filter applies.
fn gemini_request_to_chat_view(body: &Value) -> crate::models::proxy::ChatCompletionRequest {
    use crate::models::proxy::{ChatCompletionRequest, ChatMessage};

    let join_parts = |parts: Option<&Value>| -> Option<String> {
        let arr = parts?.as_array()?;
        let text: String = arr
            .iter()
            .filter_map(|p| p.get("text").and_then(|t| t.as_str()))
            .collect::<Vec<_>>()
            .join("\n");
        if text.is_empty() { None } else { Some(text) }
    };

    let mut messages: Vec<ChatMessage> = Vec::new();

    // systemInstruction (camelCase or snake_case) → leading system message.
    let sys_text = body
        .get("systemInstruction")
        .or_else(|| body.get("system_instruction"))
        .and_then(|si| join_parts(si.get("parts")));
    if let Some(sys) = sys_text {
        messages.push(ChatMessage {
            role: "system".to_string(),
            content: Some(Value::String(sys)),
            name: None,
            tool_calls: None,
            tool_call_id: None,
        });
    }

    if let Some(contents) = body.get("contents").and_then(|c| c.as_array()) {
        for entry in contents {
            let role = match entry.get("role").and_then(|r| r.as_str()) {
                Some("model") => "assistant",
                Some(r) => r,
                None => "user",
            };
            let content = join_parts(entry.get("parts")).map(Value::String);
            messages.push(ChatMessage {
                role: role.to_string(),
                content,
                name: None,
                tool_calls: None,
                tool_call_id: None,
            });
        }
    }

    let gen_cfg = body.get("generationConfig");
    ChatCompletionRequest {
        model: None,
        model_id: None,
        messages,
        temperature: gen_cfg
            .and_then(|g| g.get("temperature"))
            .and_then(|v| v.as_f64()),
        max_tokens: gen_cfg
            .and_then(|g| g.get("maxOutputTokens"))
            .and_then(|v| v.as_i64())
            .map(|v| v as i32),
        stream: None,
        top_p: gen_cfg.and_then(|g| g.get("topP")).and_then(|v| v.as_f64()),
        n: None,
        stop: None,
        presence_penalty: None,
        frequency_penalty: None,
        tools: body.get("tools").cloned(),
        tool_choice: None,
        extra: serde_json::Map::new(),
    }
}

/// Splice the governed chat-view fields back into the native Gemini body
/// after `apply_governance` mutated the view. Carries back: clamped
/// `temperature` / `topP` / `maxOutputTokens` (into `generationConfig`),
/// the filtered `tools`, and the (possibly mandatory-fragment-injected)
/// system prompt → `systemInstruction.parts[0].text`.
fn apply_gemini_chat_view_back(
    body: &mut Value,
    view: &crate::models::proxy::ChatCompletionRequest,
) {
    let Some(obj) = body.as_object_mut() else {
        return;
    };

    // Param clamps land in generationConfig.
    if view.temperature.is_some() || view.top_p.is_some() || view.max_tokens.is_some() {
        let gen_cfg = obj
            .entry("generationConfig".to_string())
            .or_insert_with(|| json!({}));
        if let Some(gen_obj) = gen_cfg.as_object_mut() {
            if let Some(t) = view.temperature {
                gen_obj.insert("temperature".to_string(), json!(t));
            }
            if let Some(tp) = view.top_p {
                gen_obj.insert("topP".to_string(), json!(tp));
            }
            if let Some(mt) = view.max_tokens {
                gen_obj.insert("maxOutputTokens".to_string(), json!(mt));
            }
        }
    }

    // Tool filtering: a `None` here means governance stripped every tool.
    match &view.tools {
        Some(tools) => {
            obj.insert("tools".to_string(), tools.clone());
        }
        None => {
            obj.remove("tools");
            obj.remove("toolConfig");
            obj.remove("tool_config");
        }
    }

    // Mandatory system-prompt fragments were injected into the view's
    // leading system message → write the whole value back as a single
    // text part in systemInstruction (Gemini's native system slot).
    if let Some(sys) = view
        .messages
        .iter()
        .find(|m| m.role == "system")
        .and_then(|m| m.content.as_ref())
        .and_then(|c| c.as_str())
    {
        if !sys.is_empty() {
            obj.insert(
                "systemInstruction".to_string(),
                json!({ "parts": [{ "text": sys }] }),
            );
            // Drop the snake_case variant so we don't leave two slots.
            obj.remove("system_instruction");
        }
    }
}

/// Normalize a Gemini-native response into a chat-shaped `Value`
/// (`choices[0].message.tool_calls[]`) so the unchanged
/// `apply_response_governance` (banned-arg patterns + AST argument
/// policies + tool-call rate counters) can scan emitted tool calls.
///
/// Gemini tool calls are `candidates[].content.parts[]` of shape
/// `{ "functionCall": { "name": …, "args": { … } } }`. The chat
/// governance reader expects `function.arguments` as a JSON-encoded
/// STRING, so we serialize `args`.
fn gemini_response_to_chat_tool_view(resp_body: &Value) -> Value {
    let mut tool_calls: Vec<Value> = Vec::new();
    if let Some(cands) = resp_body.get("candidates").and_then(|c| c.as_array()) {
        for cand in cands {
            if let Some(parts) = cand.pointer("/content/parts").and_then(|p| p.as_array()) {
                for part in parts {
                    if let Some(fc) = part.get("functionCall") {
                        let name = fc.get("name").cloned().unwrap_or(Value::Null);
                        let args_str = fc
                            .get("args")
                            .map(|a| serde_json::to_string(a).unwrap_or_default())
                            .unwrap_or_default();
                        tool_calls.push(json!({
                            "id": Value::Null,
                            "type": "function",
                            "function": { "name": name, "arguments": args_str }
                        }));
                    }
                }
            }
        }
    }
    json!({
        "choices": [{
            "index": 0,
            "message": { "role": "assistant", "tool_calls": tool_calls }
        }]
    })
}

/// Strip caller-supplied `cache_control` markers from anywhere they
/// could have leaked in. The `gemini_cache` adapter is the
/// authoritative emitter; trusting caller input would let an
/// untrusted client either burn the operator's cache budget or
/// trigger upstream 400s by marking positions Google doesn't accept.
fn strip_caller_cache_control(body: &mut Value) {
    fn walk(v: &mut Value) {
        match v {
            Value::Object(m) => {
                m.remove("cache_control");
                for (_, vv) in m.iter_mut() {
                    walk(vv);
                }
            }
            Value::Array(a) => {
                for vv in a.iter_mut() {
                    walk(vv);
                }
            }
            _ => {}
        }
    }
    if let Some(obj) = body.as_object_mut() {
        for key in [
            "contents",
            "tools",
            "systemInstruction",
            "system_instruction",
        ] {
            if let Some(v) = obj.get_mut(key) {
                walk(v);
            }
        }
    }
}

/// Distinguishes the two action suffixes Google emits for the
/// Gemini-family REST surfaces. Both AI Studio and Vertex use these
/// suffix names verbatim — only the host + auth differ.
#[derive(Debug, Clone, Copy)]
enum GeminiAction {
    GenerateContent,
    StreamGenerateContent,
}

impl GeminiAction {
    fn suffix(self) -> &'static str {
        match self {
            // Non-streaming.
            GeminiAction::GenerateContent => ":generateContent",
            // Streaming. `?alt=sse` forces the SSE-framed
            // transport — without it Vertex (and AI Studio) emits
            // newline-delimited JSON which most SDKs don't parse.
            GeminiAction::StreamGenerateContent => ":streamGenerateContent?alt=sse",
        }
    }
}

/// Resolve the upstream URL + auth header for a Gemini-family model.
/// Branches on provider:
///
///   * `gemini` / `google_ai_studio` → AI Studio host
///     (`generativelanguage.googleapis.com`) + plain API key in
///     `x-goog-api-key`. Cheapest path; one HTTP call per request.
///   * `vertex_ai` / `vertex` → Vertex regional host
///     (`{region}-aiplatform.googleapis.com`) + OAuth Bearer token
///     of the row's own service account, obtained via `gcp_auth`
///     (never the gateway's own Google identity). The token is
///     cached per service account, so the second-and-onward requests
///     skip the auth round-trip.
async fn resolve_gemini_endpoint(
    model: &crate::models::db::LlmModel,
    action: GeminiAction,
) -> Result<(String, (String, String)), Response> {
    let suffix = action.suffix();
    let bare_model = model
        .model_name
        .strip_prefix("models/")
        .unwrap_or(&model.model_name);

    match model.provider.as_str() {
        "gemini" | "google_ai_studio" => {
            let base = model
                .api_url
                .as_deref()
                .map(|u| u.trim_end_matches('/'))
                .unwrap_or("https://generativelanguage.googleapis.com");
            let url = format!("{base}/v1beta/models/{bare_model}{suffix}");

            let Some(api_key) = model.api_key.clone() else {
                return Err(gemini_error(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "Gemini model has no api_key configured",
                ));
            };
            Ok((url, ("x-goog-api-key".to_string(), api_key)))
        }
        "vertex_ai" | "vertex" => {
            // Vertex requires `{region}` + `{project}` to build the
            // URL. Pull from `provider_config` first (the UI-driven
            // path), then fall back to `model_metadata` for callers
            // that set them in the catalog row directly.
            // The host is built by the one builder of a Vertex host, from
            // the row's checked location and project. A row that has
            // neither is refused at dispatch; this is the backstop.
            let base = match crate::services::anthropic_adapter::backend::vertex::VertexBackend::publisher_model_url(model, "google", bare_model) {
                Ok(base) => base,
                Err(reason) => return Err(gemini_error(StatusCode::NOT_IMPLEMENTED, &reason)),
            };
            let url = format!("{base}{suffix}");

            // OAuth token via gcp_auth. The same crate the existing
            // anthropic_adapter::backend::vertex backend uses — token
            // caching is shared at the static provider level.
            match crate::services::anthropic_adapter::backend::vertex::get_access_token(model).await
            {
                Ok(token) => Ok((
                    url,
                    ("Authorization".to_string(), format!("Bearer {token}")),
                )),
                // A row with no service account of its own is refused at
                // dispatch; this is the backstop, the same answer.
                Err(e) if e.starts_with("provider_credential_missing") => {
                    Err(gemini_error(StatusCode::NOT_IMPLEMENTED, &e))
                }
                Err(e) => Err(gemini_error(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    &format!("Vertex auth failed: {e}"),
                )),
            }
        }
        other => Err(gemini_error(
            StatusCode::BAD_REQUEST,
            &format!(
                "Unsupported Gemini-family provider '{other}'. Expected \
                 `gemini`, `google_ai_studio`, or `vertex_ai`."
            ),
        )),
    }
}

/// `(region, project)` of a Vertex-Gemini model. Order: provider_config
/// (UI), then model_metadata.region/project. Neither has a default.
#[cfg(test)]
fn vertex_endpoint_parts(model: &crate::models::db::LlmModel) -> (Option<String>, Option<String>) {
    crate::services::anthropic_adapter::backend::vertex::VertexBackend::location_and_project(model)
}

fn extract_tenant_id(tenant: Option<axum::Extension<TenantContext>>) -> Result<String, Response> {
    match tenant {
        Some(axum::Extension(ctx)) => Ok(ctx.tenant_id.clone()),
        None => Err(gemini_error(
            StatusCode::BAD_REQUEST,
            "X-Tenant-ID header required",
        )),
    }
}

/// Native Gemini error envelope.
fn gemini_error(status: StatusCode, message: &str) -> Response {
    // Match Google's native error shape so SDKs surface it naturally.
    let body = json!({
        "error": {
            "code": status.as_u16(),
            "message": message,
            "status": status.canonical_reason().unwrap_or("ERROR"),
        }
    });
    (status, Json(body)).into_response()
}

/// Gemini-shaped 403 for an output guardrail block. A free `fn` (not a
/// closure) so it satisfies the `fn(&GuardrailViolation) -> Response` pointer
/// type `apply_output_guardrails` expects.
fn gemini_guardrail_violation_response(
    violation: &crate::services::guardrails::GuardrailViolation,
) -> Response {
    gemini_error(
        StatusCode::FORBIDDEN,
        &format!(
            "Guardrail violation ({}): {}",
            violation.check, violation.message
        ),
    )
}

#[allow(clippy::too_many_arguments)]
async fn finalise_error(
    state: &SharedState,
    auth_ctx: &crate::models::auth::AuthContext,
    model: &crate::models::db::LlmModel,
    target_group: Option<&crate::models::db::ResourceGroup>,
    _tenant_id: &str,
    headers: &HeaderMap,
    correlation_id: &str,
    model_requested: Option<String>,
    request_body_str: &str,
    start: Instant,
    status: StatusCode,
    message: &str,
    call_chain: &crate::services::a2a_delegation::CallChain,
    current_task_id: &str,
    trace_continuity: crate::services::a2a_delegation::TraceContinuity,
    trace_root_kind: Option<&'static str>,
) -> Response {
    let duration_ms = start.elapsed().as_millis() as f64;
    proxy_metrics::record_request("llm", "POST", status.as_u16(), &model.provider);
    proxy_metrics::record_latency("llm", "POST", &model.provider, duration_ms / 1000.0);

    let mut log: ProxyLogInsert = proxy_logger::build_proxy_log(
        "llm",
        auth_ctx,
        "POST",
        "/v1beta/models/{model}:generateContent",
        None,
        Some(&model.id),
        status.as_u16(),
        duration_ms,
        false,
        Some(message),
        correlation_id,
    );
    log.proxy_subtype = Some(crate::constants::proxy_subtype::GEMINI_GENERATE_CONTENT.to_string());
    log.resource_group_id = target_group.as_ref().map(|g| g.id.clone());
    log.user_agent = extract_user_agent(headers);
    log.llm_provider = Some(model.provider.clone());
    log.llm_model_used = Some(model.model_name.clone());
    log.llm_model_requested = model_requested;
    log.request_body_preview = Some(body_preview(request_body_str));
    proxy_logger::apply_call_chain(
        &mut log,
        call_chain,
        current_task_id,
        trace_continuity,
        trace_root_kind,
    );

    state
        .background_writer
        .write_proxy_log(state.db.clone(), log);

    gemini_error(status, message)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    /// The native Gemini streaming path redacts PII *inside* the Gemini
    /// envelope (`candidates[].content.parts[].text`) and records the
    /// redacted text as the on-the-wire content, while preserving the
    /// terminal `finishReason` frame. Uses the built-in PII detector
    /// (local, no network) via a sync StreamGuard.
    #[tokio::test]
    async fn gemini_streaming_redacts_pii_in_envelope() {
        let cfg = crate::models::db::GuardrailConfig {
            chat_output_enabled: Some(true),
            pii_detection_enabled: true,
            pii_action: Some("redact".into()),
            ..Default::default()
        };
        let mut guard =
            crate::services::stream_guard::StreamGuard::new(vec![cfg], reqwest::Client::new());
        assert!(
            guard.is_sync(),
            "redact on chat_output must derive sync mode"
        );

        let (tx, _rx) = tokio::sync::mpsc::channel(32);
        let mut total = 0u32;
        let mut blocked = false;
        let mut wire = String::new();

        let text_chunk = r#"{"candidates":[{"content":{"parts":[{"text":"mail alice@example.com now"}],"role":"model"},"index":0}]}"#;
        let stop1 = redact_and_emit_gemini_event(
            &tx,
            text_chunk,
            &mut guard,
            &mut total,
            &mut blocked,
            &mut wire,
            "corr",
        )
        .await;
        assert!(!stop1 && !blocked);

        // Terminal chunk: flushes the held-back (redacted) tail, then
        // forwards the structural frame with its text stripped.
        let fin_chunk = r#"{"candidates":[{"content":{"parts":[],"role":"model"},"finishReason":"STOP","index":0}],"usageMetadata":{"totalTokenCount":5}}"#;
        let stop2 = redact_and_emit_gemini_event(
            &tx,
            fin_chunk,
            &mut guard,
            &mut total,
            &mut blocked,
            &mut wire,
            "corr",
        )
        .await;
        assert!(!stop2 && !blocked);

        assert!(
            wire.contains("[REDACTED-EMAIL]"),
            "gemini-envelope redaction must fire, wire={wire:?}"
        );
        assert!(
            !wire.contains("alice@example.com"),
            "raw PII must not reach the wire, wire={wire:?}"
        );
        assert!(
            total >= 2,
            "expected a redacted text frame + the terminal frame, got {total}"
        );
    }

    /// Regression guard: Axum 0.8's matchit router rejects mixed
    /// placeholder + literal in a single path segment with
    /// "Only one parameter is allowed per path segment". The first
    /// version of this route used `/v1beta/models/{model}:generateContent`
    /// directly and panicked at server startup. We now capture the
    /// `{model}:{action}` segment as one parameter and split in the
    /// handler — this test asserts the router still constructs.
    #[test]
    fn router_constructs_without_panic() {
        // If matchit rejects the route, `Router::new().route(...)`
        // panics. Just calling `router()` is the test.
        let _r: Router<SharedState> = router();
    }

    #[test]
    fn gemini_action_suffixes() {
        assert_eq!(GeminiAction::GenerateContent.suffix(), ":generateContent");
        assert_eq!(
            GeminiAction::StreamGenerateContent.suffix(),
            ":streamGenerateContent?alt=sse"
        );
    }

    // Vertex's full `resolve_gemini_endpoint` path requires real
    // GCP credentials (gcp_auth crate initialises a rustls TLS
    // stack) — can't be cleanly unit-tested. The URL builder
    // `vertex_endpoint_parts` is exercised in its own tests below;
    // the full path is covered by live e2e tests gated on a Vertex
    // credential.

    #[tokio::test]
    async fn ai_studio_endpoint_builds_url_with_api_key_header() {
        let mut model = crate::services::llm_adapter::make_test_model("gemini", "gemini-2.5-flash");
        model.api_key = Some("AIza-test-key".into());
        let (url, (header_name, header_value)) =
            resolve_gemini_endpoint(&model, GeminiAction::GenerateContent)
                .await
                .expect("AI Studio path should succeed");
        assert!(url.contains("generativelanguage.googleapis.com"));
        assert!(url.ends_with(":generateContent"));
        assert!(url.contains("/models/gemini-2.5-flash"));
        assert_eq!(header_name, "x-goog-api-key");
        assert_eq!(header_value, "AIza-test-key");
    }

    #[tokio::test]
    async fn ai_studio_streaming_endpoint_uses_alt_sse() {
        let mut model = crate::services::llm_adapter::make_test_model("gemini", "gemini-2.5-pro");
        model.api_key = Some("AIza-test".into());
        let (url, _) = resolve_gemini_endpoint(&model, GeminiAction::StreamGenerateContent)
            .await
            .expect("AI Studio streaming path should succeed");
        assert!(url.ends_with(":streamGenerateContent?alt=sse"));
    }

    #[test]
    fn vertex_endpoint_parts_pull_project_region_from_provider_config() {
        let mut model =
            crate::services::llm_adapter::make_test_model("vertex_ai", "gemini-2.5-pro");
        model.provider_config = Some(
            serde_json::json!({
                "gcp_project_id": "ops-prod",
                "gcp_location": "asia-southeast1"
            })
            .to_string(),
        );
        let (region, project) = vertex_endpoint_parts(&model);
        assert_eq!(region.as_deref(), Some("asia-southeast1"));
        assert_eq!(project.as_deref(), Some("ops-prod"));
    }

    #[test]
    fn vertex_endpoint_parts_fall_back_to_model_metadata() {
        let mut model =
            crate::services::llm_adapter::make_test_model("vertex_ai", "gemini-2.5-pro");
        model.model_metadata = Some(serde_json::json!({
            "region": "us-east4",
            "project": "alt-project"
        }));
        let (region, project) = vertex_endpoint_parts(&model);
        assert_eq!(region.as_deref(), Some("us-east4"));
        assert_eq!(project.as_deref(), Some("alt-project"));
    }

    #[test]
    fn vertex_endpoint_parts_have_no_default_location_or_project() {
        // A row that names no project has none. The default was
        // `my-project`: somebody's project, called with the row's token.
        let model = crate::services::llm_adapter::make_test_model("vertex_ai", "gemini-2.5-pro");
        let (region, project) = vertex_endpoint_parts(&model);
        assert_eq!(region, None, "no default location");
        assert_eq!(project, None);
    }

    // ── apply_redactions_to_input ───────────────────────────────────

    #[test]
    fn gemini_redact_system_and_contents_text_parts() {
        let mut body = serde_json::json!({
            "systemInstruction": {
                "parts": [{"text": "you handle PII carefully"}]
            },
            "contents": [
                {"parts": [{"text": "my ssn is 123-45-6789"}]}
            ]
        });
        // Two text leaves: systemInstruction.parts[0].text, then
        // contents[0].parts[0].text. Extractor order matches.
        let redacted = "you handle PII carefully\nmy ssn is [REDACTED-SSN]";
        assert!(apply_redactions_to_input(&mut body, redacted));
        assert_eq!(
            body["systemInstruction"]["parts"][0]["text"],
            "you handle PII carefully"
        );
        assert_eq!(
            body["contents"][0]["parts"][0]["text"],
            "my ssn is [REDACTED-SSN]"
        );
    }

    #[test]
    fn gemini_redact_handles_snake_case_system_instruction() {
        // Some Gemini clients send `system_instruction` instead of
        // `systemInstruction`. The extractor honors both; the
        // splicer must too.
        let mut body = serde_json::json!({
            "system_instruction": {"parts": [{"text": "be careful"}]},
            "contents": [{"parts": [{"text": "credit-card 4111-1111-1111-1111"}]}]
        });
        let redacted = "be careful\ncredit-card [REDACTED-CC]";
        assert!(apply_redactions_to_input(&mut body, redacted));
        assert_eq!(body["system_instruction"]["parts"][0]["text"], "be careful");
        assert_eq!(
            body["contents"][0]["parts"][0]["text"],
            "credit-card [REDACTED-CC]"
        );
    }

    #[test]
    fn gemini_redact_skips_non_text_parts() {
        // Multimodal parts (inline_data, file_data, functionCall)
        // aren't text-bearing; the extractor skips them and so
        // should the splicer.
        let mut body = serde_json::json!({
            "contents": [
                {"parts": [
                    {"text": "first text"},
                    {"inline_data": {"mime_type": "image/png", "data": "..."}},
                    {"text": "second text"}
                ]}
            ]
        });
        // Two text leaves.
        let redacted = "FIRST TEXT\nSECOND TEXT";
        assert!(apply_redactions_to_input(&mut body, redacted));
        assert_eq!(body["contents"][0]["parts"][0]["text"], "FIRST TEXT");
        // inline_data part unchanged.
        assert!(body["contents"][0]["parts"][1]["inline_data"].is_object());
        assert_eq!(body["contents"][0]["parts"][2]["text"], "SECOND TEXT");
    }

    #[test]
    fn gemini_redact_no_op_on_count_mismatch() {
        let mut body = serde_json::json!({
            "contents": [
                {"parts": [{"text": "a"}, {"text": "b"}]}
            ]
        });
        let original = body.clone();
        assert!(!apply_redactions_to_input(&mut body, "single-line"));
        assert_eq!(body, original);
    }

    #[test]
    fn gemini_redact_no_op_on_empty_payload() {
        let mut body = serde_json::json!({"contents": []});
        let original = body.clone();
        assert!(!apply_redactions_to_input(&mut body, "anything"));
        assert_eq!(body, original);
    }

    // ── usage extraction for the OpenAI-surface bridge ──────────
    //
    // The bridge reads Gemini's own counts off the NATIVE body before
    // `normalize_response` rewrites it into OpenAI shape. Getting this
    // wrong is silent: the row still writes, the numbers are just absent,
    // and every $ and token ceiling then measures nothing.

    #[test]
    fn usage_counts_come_from_the_native_metadata_block() {
        let v = serde_json::json!({
            "candidates": [],
            "usageMetadata": {
                "promptTokenCount": 1200,
                "candidatesTokenCount": 350,
                "totalTokenCount": 1550
            }
        });
        assert_eq!(
            super::gemini_usage_counts(&v),
            (Some(1200), Some(350), Some(1550))
        );
    }

    #[test]
    fn a_response_with_no_usage_block_reports_absence_not_zero() {
        // Zero would read as "this call cost nothing", which is a claim.
        // Absent reads as "not measured", which is the truth.
        let v = serde_json::json!({"candidates": []});
        assert_eq!(super::gemini_usage_counts(&v), (None, None, None));
    }

    #[test]
    fn a_partial_usage_block_keeps_the_fields_it_has() {
        let v = serde_json::json!({"usageMetadata": {"promptTokenCount": 42}});
        assert_eq!(super::gemini_usage_counts(&v), (Some(42), None, None));
    }

    #[test]
    fn the_openai_shape_is_not_mistaken_for_the_native_one() {
        // After translation the counts live under `usage`, not
        // `usageMetadata`; reading the wrong one would silently zero the
        // ledger for every bridged call.
        let v = serde_json::json!({
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        });
        assert_eq!(super::gemini_usage_counts(&v), (None, None, None));
    }
}
