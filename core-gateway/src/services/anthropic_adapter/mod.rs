// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Anthropic Message Adapter
//!
//! Three-layer adapter stack for routing Anthropic Messages API requests
//! through the gateway to different backend providers while staying in
//! native Anthropic format end-to-end.
//!
//! ## Architecture
//!
//! ```text
//! Layer 1: Client Adapters  — handle protocol differences between dev tools
//!                              (Claude Code, Cursor, generic SDK, etc.)
//! Layer 2: Core Orchestrator — shared governance (auth, guardrails, logging,
//!                              usage tracking, quota enforcement)
//! Layer 3: Backend Adapters  — handle provider differences (Anthropic Direct,
//!                              AWS Bedrock, Google Vertex AI)
//! ```
//!
//! The key design principle: requests stay in native Anthropic Messages format
//! throughout the pipeline. No double-translation through OpenAI format, which
//! preserves Claude Code-specific features (beta headers, extended thinking,
//! effort, tool_reference blocks, interleaved thinking streams).

pub mod backend;
pub mod client;

use std::time::Instant;

use axum::http::{HeaderMap, StatusCode};
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::response::{IntoResponse, Json, Response};
use futures::stream::StreamExt;
use serde_json::{Value, json};
use tokio_stream::wrappers::ReceiverStream;

use crate::models::auth::AuthContext;
use crate::models::db::{LlmModel, ResourceGroup};
use crate::services::proxy_logger::{
    self, body_preview, extract_user_agent, populate_llm_request_fields,
};
use crate::services::{SharedState, llm_router, proxy_metrics, usage};

pub use backend::is_native_anthropic_provider;

/// Safety ceiling for streaming responses (64 MB).
const MAX_STREAM_BYTES: usize = 64 * 1024 * 1024;

// ---------------------------------------------------------------------------
// Provider-specific auth resolution
// ---------------------------------------------------------------------------

/// Resolve provider-specific auth headers after the request body is finalized.
///
/// This is separate from `BackendAdapter::auth_headers()` because:
/// - **Bedrock**: SigV4 signing requires the full serialized request body
/// - **Vertex**: Token fetch is async (needs `.await`)
/// - **Anthropic Direct**: Simple `x-api-key` (handled by `auth_headers()`, this is a no-op)
async fn resolve_provider_auth(
    model: &LlmModel,
    url: &str,
    body_bytes: &[u8],
) -> Result<Vec<(String, String)>, String> {
    match model.provider.as_str() {
        // Signed with the row's own credential or not sent: a row
        // without one is refused at dispatch, and here again.
        "bedrock" | "bedrock_converse" => backend::bedrock::sign_request(model, url, body_bytes),
        "vertex_ai" | "vertex" => {
            let token = backend::vertex::get_access_token(model).await?;
            Ok(vec![("Authorization".into(), format!("Bearer {token}"))])
        }
        _ => {
            // Anthropic Direct and others: auth_headers() already returned everything
            Ok(vec![])
        }
    }
}

// ---------------------------------------------------------------------------
// Native forwarding — streaming path
// ---------------------------------------------------------------------------

/// Frames the end of one turn produces (`message_delta`, `message_stop`, and
/// any text flushed before them), with room to spare.
const HELD_TURN_END_FRAMES: usize = 64;

/// Forward an Anthropic Messages request natively to a backend that speaks
/// the Anthropic Messages API (Anthropic Direct, Bedrock, Vertex).
///
/// This is called from `routes/anthropic.rs` after auth, model resolution,
/// access checks, quota enforcement, and guardrails have already been applied.
///
/// The request stays in native Anthropic format end-to-end. No OpenAI translation.
/// `force_openai_normalization` flips the per-event SSE emission to
/// run each Anthropic event through `AnthropicStreamNormalizer` before
/// forwarding to the client. Set this when the OpenAI-shape surface
/// bridge is calling — the client speaks OpenAI shape and won't
/// understand native Anthropic SSE event types
/// (`message_start`, `content_block_delta`, etc.). Native callers
/// (Claude Code via `/v1/messages`) pass `false` for verbatim
/// passthrough.
#[allow(clippy::too_many_arguments)]
pub async fn forward_native_streaming(
    state: &SharedState,
    headers: &HeaderMap,
    body: Value,
    model: &LlmModel,
    auth_ctx: &AuthContext,
    tenant_id: &str,
    target_group: Option<&ResourceGroup>,
    correlation_id: &str,
    start: Instant,
    force_openai_normalization: bool,
    // True when an input guardrail redacted a PII category from the
    // prompt — stamped on the success audit row so the compliance
    // tagger derives has_pii/has_phi (GDPR/HIPAA).
    input_pii_redacted: bool,
    // Data-residency decision computed by the caller. Stamped on the
    // streaming success row (allowed / not-evaluated). A Blocked decision
    // never reaches here (the caller 403s before dispatch).
    residency_decision: Option<crate::services::residency::ResidencyDecision>,
    provider_region: Option<String>,
    // The run this call belongs to. Without it the
    // throughput charge moves the group and tenant counters but never the
    // run's, so per-run ceilings read zero forever and the gate
    // that exists to stop a runaway loop has nothing to compare against.
    run: crate::services::llm_refusal::RunAttribution,
) -> Response {
    // 1. Detect client and select backend
    let client_adapter = client::detect_client(headers);
    let metadata = client_adapter.extract_client_headers(headers);

    let backend = match backend::get_backend(&model.provider) {
        Some(b) => b,
        None => {
            return anthropic_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                &format!("No native backend for provider: {}", model.provider),
            );
        }
    };

    // 2. Client preprocessing (backend-aware body fixups)
    let mut body = body;
    client_adapter.preprocess_request(&mut body, &metadata, backend.backend_name());

    // 3. Backend request transformation
    backend.transform_request(&mut body, model);

    // 3.5. Prompt-cache marker injection — see the matching block in
    // `forward_native_non_streaming` for the rationale. Skipping this
    // step here would mean Claude-Code-style streaming clients lose
    // cache savings the OpenAI-shape route gets for free.
    let prompt_cache = state
        .prompt_cache_policy
        .session(state, tenant_id, target_group, model)
        .await;
    if let Some(decision) = prompt_cache.mark(&mut body, model).await {
        crate::services::prompt_caching::record_decision(&decision);
    }

    // 4. Build upstream URL (no ?beta=true)
    let upstream_url = backend.messages_url(model, true);

    // 5. Build headers
    let mut req_headers: Vec<(String, String)> = backend.auth_headers(model);

    // Forward anthropic-version header
    if let Some(ref ver) = metadata.anthropic_version {
        req_headers.push(("anthropic-version".into(), ver.clone()));
    } else {
        req_headers.push(("anthropic-version".into(), "2023-06-01".into()));
    }

    // Filter and forward anthropic-beta headers
    if let Some(ref beta) = metadata.anthropic_beta {
        if let Some(filtered) = backend.filter_beta_headers(beta) {
            req_headers.push(("anthropic-beta".into(), filtered));
        }
    }

    // Force stream=true in body
    if let Some(obj) = body.as_object_mut() {
        obj.insert("stream".into(), Value::Bool(true));
    }

    // 6. Resolve provider-specific auth (SigV4 for Bedrock, OAuth for Vertex)
    let body_bytes = serde_json::to_vec(&body).unwrap_or_default();
    match resolve_provider_auth(model, &upstream_url, &body_bytes).await {
        Ok(extra_headers) => req_headers.extend(extra_headers),
        Err(e) => {
            return anthropic_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                &format!("Provider auth failed: {e}"),
            );
        }
    }

    // 7. Track request in router state
    state.router_state.increment_rpm(&model.id);
    state.router_state.begin_request(&model.id);

    // 8. Send upstream request
    let timeout_secs = model.timeout.map(|t| t as u64).unwrap_or(120);
    let mut req = state
        .http_client
        .post(&upstream_url)
        .header("Content-Type", "application/json")
        .timeout(std::time::Duration::from_secs(timeout_secs));

    for (k, v) in &req_headers {
        req = req.header(k.as_str(), v.as_str());
    }

    // Propagate the gateway's call
    // chain onto the native Anthropic upstream. Set by the route
    // handler via `OUTBOUND_CHAIN_HEADERS.scope(...)`; absent on
    // call-paths that don't yet wire chain propagation.
    if let Some(pairs) = crate::services::call_chain::current_outbound_headers() {
        for (k, v) in pairs {
            req = req.header(k, v);
        }
    }

    // Cluster-wide RG / per-model concurrency gate for the stream
    // (`llm_global_limits.concurrency.max_concurrent`, group + model scope).
    // Moved into the SSE task below so the slot is held for the whole stream
    // and released on completion / disconnect / error via Drop. Saturation →
    // 429 before we open the upstream connection.
    let stream_conc_guard = match crate::services::llm_refusal::acquire_concurrency(
        state,
        target_group,
        model,
        &crate::services::llm_refusal::RefusalScope {
            auth_ctx,
            headers,
            correlation_id,
            endpoint: "/v1/messages",
            started: Some(start),
            model: Some((model, None)),
            group_id: None,
            run: Some(&run),
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

    let upstream_resp = match req.body(body_bytes).send().await {
        Ok(r) => r,
        Err(e) => {
            state.router_state.end_request(&model.id);
            let duration = start.elapsed().as_millis() as f64;
            proxy_metrics::record_request("llm", "POST", 502, &model.provider);
            proxy_metrics::record_latency("llm", "POST", &model.provider, duration / 1000.0);
            return anthropic_error(
                StatusCode::BAD_GATEWAY,
                &format!("Upstream connection error: {e}"),
            );
        }
    };
    crate::middleware::run_end::record_upstream_response(
        upstream_resp.status().as_u16(),
        upstream_resp.headers(),
    );

    if !upstream_resp.status().is_success() {
        state.router_state.end_request(&model.id);
        let status_code = upstream_resp.status().as_u16();
        let err_body: Value = upstream_resp
            .json()
            .await
            .unwrap_or(json!({"error": "Unknown upstream error"}));
        let duration = start.elapsed().as_millis() as f64;
        proxy_metrics::record_request("llm", "POST", status_code, &model.provider);
        proxy_metrics::record_latency("llm", "POST", &model.provider, duration / 1000.0);

        let status = StatusCode::from_u16(status_code).unwrap_or(StatusCode::BAD_GATEWAY);
        return (status, Json(err_body)).into_response();
    }

    // 8. Stream response with observer/tee pattern
    // A backend that does not stream Anthropic SSE (Bedrock:
    // `application/vnd.amazon.eventstream`) has a converter, and its stream
    // goes through it unless the upstream says the body is SSE. The stream
    // was read as SSE text whatever it was, and a Bedrock stream produced
    // no event at all.
    let upstream_is_sse = upstream_resp
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.to_ascii_lowercase().starts_with("text/event-stream"));
    let byte_stream = upstream_resp.bytes_stream();
    let (tx, rx) = tokio::sync::mpsc::channel::<Result<Event, axum::Error>>(32);

    let state_bg = state.clone();
    let auth_bg = auth_ctx.clone();
    let model_bg = model.clone();
    let corr_id = correlation_id.to_string();
    let tenant_id_bg = tenant_id.to_string();
    let target_group_bg: Option<ResourceGroup> = target_group.cloned();
    let model_requested = body.get("model").and_then(|m| m.as_str()).map(String::from);
    let user_agent = extract_user_agent(headers);
    let sanitized_req_headers = proxy_logger::sanitize_headers(headers);
    let req_headers_count = headers.len() as i32;
    let request_body_str = serde_json::to_string(&body).unwrap_or_default();
    let input_cost = model.cost_per_input_token.unwrap_or(0.0);
    let output_cost = model.cost_per_output_token.unwrap_or(0.0);
    let client_name = metadata.client_name.clone();
    let upstream_url_bg = upstream_url.clone();
    // Residency outcome stamped on the streaming success row.
    let residency_decision_bg = residency_decision.clone();
    let provider_region_bg = provider_region.clone();
    // Model id for the post-stream tool-call governance check.
    let model_id_bg = model.id.clone();
    // The run the stream's rows (outcome and any post-response block) name.
    let run_bg = run.clone();
    let headers_bg = headers.clone();

    // Output guardrails for the streamed response (chat_output surface).
    // This adapter produces the client-facing SSE for cloud-hosted Claude
    // (Bedrock / Vertex) on both the OpenAI surface (/chat/completions →
    // force_openai_normalization) and the native /v1/messages surface — so
    // without this, PII/secrets would egress unredacted on those models.
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
                    "/v1/messages",
                    &run,
                )
                .started(start),
                crate::constants::guardrail_surface::CHAT_OUTPUT,
                &e,
            );
        }
    };

    proxy_metrics::stream_opened();

    let mut converter = if backend.native_anthropic_streaming() || upstream_is_sse {
        None
    } else {
        backend.create_stream_converter()
    };

    tokio::spawn(async move {
        // Hold the RG concurrency slot for the whole stream.
        let _stream_conc_guard = stream_conc_guard;
        let mut buffer = String::with_capacity(4096);
        let mut total_chunks = 0u32;
        let mut total_response_bytes = 0usize;
        // On-the-wire (post-guard / post-redaction) response text, built
        // from the frames actually sent to the client so the audit log
        // mirrors the wire exactly.
        let mut accumulated_content = String::with_capacity(4096);
        // Raw upstream text observed for finish/usage extraction is kept
        // separate and discarded — we must not log unredacted PII.
        let mut observe_sink = String::with_capacity(4096);
        let mut finish_reason: Option<String> = None;
        let mut prompt_tokens: Option<i32> = None;
        let mut completion_tokens: Option<i32> = None;
        // What the provider says about its prompt cache, read off the
        // usage events as they pass.
        let mut cache_write_tokens: i32 = 0;
        let mut cache_read_tokens: i32 = 0;
        // Allocated even when unused so the borrow stays simple; the
        // normalizer is cheap to construct.
        let mut openai_normalizer = crate::services::llm_adapter::AnthropicStreamNormalizer::new();
        // Output-guardrail state machine for this stream.
        let mut stream_guard = crate::services::stream_guard::StreamGuard::new(
            stream_guardrail_configs.into_vec(),
            state_bg.http_client.clone(),
        )
        .for_run(tenant_id_bg.clone(), run_bg.root_task_id());
        let mut guardrail_blocked = false;
        // Accumulate emitted tool_use blocks across the stream so
        // post-response governance can scan them at stream end. Anthropic
        // streams a tool call as a `content_block_start` (carrying
        // id+name) followed by `input_json_delta` fragments on that
        // block's index, so we key by block index and concatenate the
        // partial_json fragments. (id, name, args_json_string)
        let mut tool_blocks: std::collections::BTreeMap<i64, (String, String, String)> =
            std::collections::BTreeMap::new();
        // The end of a turn that asks for tools is held until those calls are
        // judged (data scope, argument policies): a client runs a tool call
        // when the turn ends, so a refusal sent after `message_stop` came too
        // late. From the first `message_delta` / `message_stop` of such a
        // turn, frames go here, already processed, instead of to the client;
        // they are released if the calls pass and dropped if refused. A turn
        // ends with two events, so the capacity is never reached.
        let (held_tx, mut held_rx) =
            tokio::sync::mpsc::channel::<Result<Event, axum::Error>>(HELD_TURN_END_FRAMES);
        let mut holding = false;

        futures::pin_mut!(byte_stream);
        'outer: while let Some(chunk_result) = byte_stream.next().await {
            match chunk_result {
                Ok(bytes) => {
                    total_response_bytes += bytes.len();
                    if total_response_bytes > MAX_STREAM_BYTES {
                        tracing::warn!(
                            total_bytes = total_response_bytes,
                            "Native Anthropic stream exceeded safety ceiling, aborting"
                        );
                        break;
                    }
                    // The Anthropic events of this chunk: the SSE frames of
                    // a backend that streams them, or what the backend's
                    // converter decodes (Bedrock's binary event stream).
                    let events: Vec<(String, String)> = match converter.as_mut() {
                        Some(converter) => converter.convert(&bytes),
                        None => {
                            buffer.push_str(&String::from_utf8_lossy(&bytes));
                            let mut events = Vec::new();
                            // Parse SSE frames: split on double newline
                            while let Some(pos) = buffer.find("\n\n") {
                                let event_str = buffer[..pos].to_string();
                                buffer.drain(..pos + 2);

                                // Extract event type and data
                                let mut event_type = String::new();
                                let mut event_data = String::new();
                                for line in event_str.lines() {
                                    if let Some(et) = line.strip_prefix("event: ") {
                                        event_type = et.to_string();
                                    } else if let Some(data) = line.strip_prefix("data: ") {
                                        event_data = data.to_string();
                                    }
                                }
                                if !event_data.is_empty() {
                                    events.push((event_type, event_data));
                                }
                            }
                            events
                        }
                    };

                    for (event_type, event_data) in events {
                        // An operator can stop the run
                        // mid-stream; dropping the upstream connection is
                        // what stops the provider generating, and billing.
                        if stream_guard.run_aborted(&state_bg).await {
                            let abort =
                                crate::services::run_intervention::ABORTED_MESSAGE.to_string();
                            if force_openai_normalization {
                                let _ = emit_adapter_openai_action(
                                    &tx,
                                    crate::services::stream_guard::StreamAction::Block(abort),
                                    &mut total_chunks,
                                    &mut guardrail_blocked,
                                    &mut accumulated_content,
                                    &corr_id,
                                )
                                .await;
                            } else {
                                let _ = emit_anthropic_text_action(
                                    &tx,
                                    crate::services::stream_guard::TextAction::Block(abort),
                                    &mut total_chunks,
                                    &mut guardrail_blocked,
                                    &mut accumulated_content,
                                    &corr_id,
                                    0,
                                )
                                .await;
                            }
                            break 'outer;
                        }

                        // Observer: tap the stream for finish/usage extraction.
                        // Content goes to a discard sink — the logged body is
                        // rebuilt from the post-guard wire frames below so it
                        // mirrors the wire (no unredacted PII in the audit log).
                        observe_anthropic_sse_event(
                            &event_type,
                            &event_data,
                            &mut observe_sink,
                            &mut finish_reason,
                            &mut prompt_tokens,
                            &mut completion_tokens,
                        );
                        observe_anthropic_cache_usage(
                            &event_type,
                            &event_data,
                            &mut cache_write_tokens,
                            &mut cache_read_tokens,
                        );

                        // Harvest tool_use block starts + input_json_delta
                        // fragments for post-stream governance.
                        observe_anthropic_tool_use(&event_data, &mut tool_blocks);
                        holding |= !tool_blocks.is_empty()
                            && matches!(event_type.as_str(), "message_delta" | "message_stop");
                        let out = if holding { &held_tx } else { &tx };

                        if force_openai_normalization {
                            // OpenAI-surface bridge: translate each Anthropic SSE
                            // event into OpenAI-shape chunks, run each through the
                            // output guard, and forward the post-guard frames.
                            // Clients on /v1/proxy/llm/chat/completions don't
                            // understand Anthropic event types.
                            use crate::services::llm_adapter::StreamNormalizer;
                            // An Anthropic `error` event (the provider's, or
                            // the one a malformed Bedrock stream ends with)
                            // has no chat completion chunk: the normalizer
                            // drops it and the caller saw a stream that
                            // just stopped. It is the stream's error, in
                            // the shape the chat route reports one.
                            if event_type == "error" {
                                let error = serde_json::from_str::<Value>(&event_data)
                                    .ok()
                                    .and_then(|v| v.get("error").cloned())
                                    .unwrap_or_else(|| {
                                        json!({"type": "api_error",
                                               "message": "the provider's stream ended with an error"})
                                    });
                                let _ = tx
                                    .send(Ok(
                                        Event::default().data(json!({"error": error}).to_string())
                                    ))
                                    .await;
                                break 'outer;
                            }
                            for converted in openai_normalizer.convert(&event_data) {
                                let action = stream_guard.process(&converted).await;
                                if emit_adapter_openai_action(
                                    out,
                                    action,
                                    &mut total_chunks,
                                    &mut guardrail_blocked,
                                    &mut accumulated_content,
                                    &corr_id,
                                )
                                .await
                                {
                                    break 'outer;
                                }
                            }
                        } else {
                            // Native Anthropic passthrough: redact the
                            // content_block_delta text in place (sync mode) or
                            // scan for a Block (async mode), forwarding all
                            // structural events verbatim.
                            if redact_and_emit_anthropic_event(
                                out,
                                &event_type,
                                &event_data,
                                &mut stream_guard,
                                &mut total_chunks,
                                &mut guardrail_blocked,
                                &mut accumulated_content,
                                &corr_id,
                            )
                            .await
                            {
                                break 'outer;
                            }
                        }
                    }
                }
                Err(e) => {
                    tracing::warn!("Native Anthropic stream error: {e}");
                    break;
                }
            }
        }

        // Flush any sync-mode held-back tail (no-op in the common case —
        // the terminal event already flushed it).
        if !guardrail_blocked {
            if force_openai_normalization {
                let action = stream_guard.finish().await;
                let _ = emit_adapter_openai_action(
                    &tx,
                    action,
                    &mut total_chunks,
                    &mut guardrail_blocked,
                    &mut accumulated_content,
                    &corr_id,
                )
                .await;
            } else {
                let action = stream_guard.finish_text().await;
                let _ = emit_anthropic_text_action(
                    &tx,
                    action,
                    &mut total_chunks,
                    &mut guardrail_blocked,
                    &mut accumulated_content,
                    &corr_id,
                    0,
                )
                .await;
            }
        }

        // Post-response tool-call governance on the accumulated
        // tool_use blocks. `apply_response_governance` owns its own block
        // audit row. On a block we emit a terminal Anthropic error event so
        // the client stops (matching the streaming output-guard contract).
        let mut turn_refused = false;
        if !tool_blocks.is_empty() {
            let tool_calls: Vec<Value> = tool_blocks
                .values()
                .map(|(id, name, args)| {
                    json!({
                        "id": id,
                        "type": "function",
                        "function": {
                            "name": name,
                            // Empty fragment → "{}" so the reader sees valid JSON.
                            "arguments": if args.is_empty() { "{}" } else { args.as_str() }
                        }
                    })
                })
                .collect();
            let tool_view = json!({
                "choices": [{
                    "index": 0,
                    "message": { "role": "assistant", "tool_calls": tool_calls }
                }]
            });
            if crate::services::llm_preflight::apply_response_governance(
                &state_bg,
                &target_group_bg,
                &model_id_bg,
                &tool_view,
                anthropic_error,
                &crate::services::llm_refusal::RefusalScope::new(
                    &auth_bg,
                    &headers_bg,
                    &corr_id,
                    "/v1/messages",
                    &run_bg,
                )
                .started(start),
            )
            .await
            .is_err()
            {
                // The held end of the turn is dropped: the client never sees
                // the turn end with these calls to run.
                turn_refused = true;
                tracing::warn!(
                    correlation_id = %corr_id,
                    "Streamed native Anthropic tool-call response blocked by output governance"
                );
                let err = json!({
                    "type": "error",
                    "error": {
                        "type": "guardrail_block",
                        "message": "Response blocked by output governance policy"
                    }
                });
                let _ = tx
                    .send(Ok(Event::default().event("error").data(err.to_string())))
                    .await;
            }
        }
        // The calls passed (or the turn asked for none): the held end of the
        // turn goes out now, after everything before it.
        drop(held_tx);
        if !turn_refused {
            while let Some(frame) = held_rx.recv().await {
                let _ = tx.send(frame).await;
            }
        }

        // Background logging + metrics
        let total_tokens = prompt_tokens.zip(completion_tokens).map(|(p, c)| p + c);
        let duration = start.elapsed().as_millis() as f64;

        let mut log = proxy_logger::build_proxy_log(
            "llm",
            &auth_bg,
            "POST",
            "/v1/messages",
            Some(&upstream_url_bg),
            Some(&model_bg.id),
            200,
            duration,
            true,
            None,
            &corr_id,
        );
        log.resource_group_id = target_group_bg.as_ref().map(|g| g.id.clone());
        log.proxy_subtype = Some(format!("anthropic_native_{client_name}"));
        log.user_agent = user_agent.clone();
        log.request_size_bytes = Some(request_body_str.len() as i32);
        log.request_headers = Some(sanitized_req_headers.clone());
        log.request_body_preview = Some(body_preview(&request_body_str));
        log.response_size_bytes = Some(total_response_bytes as i32);
        log.response_body_preview = Some(
            serde_json::to_string(&json!({
                "streaming": true,
                "native_anthropic": true,
                "chunks": total_chunks,
                "finish_reason": finish_reason,
                "content_preview": if accumulated_content.len() > 200 {
                    format!("{}...", &accumulated_content[..200])
                } else {
                    accumulated_content.clone()
                },
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

        // Through the shared helper. A stream that closed
        // reporting only one side used to be logged with NO cost at all,
        // which the budget counters read as $0.
        log.llm_estimated_cost_usd = crate::routes::llm_helpers::estimate_llm_cost(
            prompt_tokens,
            completion_tokens,
            input_cost,
            output_cost,
        );
        // The cached tokens the provider reported. This pipeline is
        // the one that marks a request for Anthropic's cache, and it was the
        // one surface whose audit row never said what the cache then did.
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
        crate::routes::llm_helpers::apply_prompt_cache_usage(
            &mut log,
            &pc_usage,
            pc_mechanism,
            &model_bg.provider,
        );
        if input_pii_redacted {
            proxy_logger::apply_pii_detected(&mut log);
        }
        // Stamp residency outcome on the native streaming success row.
        match &residency_decision_bg {
            Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
            None => {
                proxy_logger::apply_residency_not_evaluated(&mut log, provider_region_bg.as_deref())
            }
        }
        // The row names its run and step, like every other LLM surface's.
        run_bg.stamp(&mut log, &auth_bg);
        // How the guard ended the stream (operator abort / guardrail block) is recorded on its own
        // outcome row — the row that names the run and step and carries the
        // tokens the provider billed before the guard stopped it.
        stream_guard.stamp_outcome(&mut log);
        let estimated_cost = log.llm_estimated_cost_usd;

        state_bg
            .background_writer
            .write_proxy_log(state_bg.db.clone(), log);

        let llm_metric_data = json!({
            "provider": model_bg.provider,
            "model_requested": model_requested,
            "model_used": model_bg.model_name,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "estimated_cost_usd": estimated_cost,
            "streaming": true,
            "native_anthropic": true,
            "client_adapter": client_name,
        });
        let metric = proxy_logger::build_proxy_metric(
            "llm",
            &auth_bg,
            "POST",
            "/v1/messages",
            &format!("anthropic_native_{client_name}"),
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
            &upstream_url_bg,
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

        proxy_metrics::record_request("llm", "POST", 200, &model_bg.provider);
        proxy_metrics::record_latency("llm", "POST", &model_bg.provider, duration / 1000.0);
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
        if let Some(cost) = estimated_cost {
            proxy_metrics::record_cost(&model_bg.provider, &model_bg.model_name, cost);
        }
        proxy_metrics::stream_closed();

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
    });

    let stream = ReceiverStream::new(rx);
    let mut resp = Sse::new(stream)
        .keep_alive(KeepAlive::default())
        .into_response();

    resp.headers_mut().insert(
        crate::constants::headers::X_CORRELATION_ID,
        correlation_id.parse().unwrap(),
    );
    resp.headers_mut().insert(
        crate::constants::headers::X_AUTH_TYPE,
        auth_ctx.auth_type.to_string().parse().unwrap(),
    );

    resp
}

// ---------------------------------------------------------------------------
// Native forwarding — non-streaming path
// ---------------------------------------------------------------------------

/// Forward a non-streaming Anthropic Messages request natively.
#[allow(clippy::too_many_arguments)]
pub async fn forward_native_non_streaming(
    state: &SharedState,
    headers: &HeaderMap,
    body: Value,
    model: &LlmModel,
    auth_ctx: &AuthContext,
    tenant_id: &str,
    target_group: Option<&ResourceGroup>,
    correlation_id: &str,
    start: Instant,
    // True when an input guardrail redacted a PII category from the
    // prompt — stamped on the success audit row (GDPR/HIPAA tagging).
    input_pii_redacted: bool,
    // Data-residency decision computed by the caller
    // (routes/anthropic.rs). Stamped on the success audit row
    // (allowed / not-evaluated). A Blocked decision never reaches here —
    // the caller 403s before dispatch. `provider_region` is the resolved
    // region used for the not-evaluated stamp.
    residency_decision: Option<crate::services::residency::ResidencyDecision>,
    provider_region: Option<String>,
    // The run this call belongs to. Without it the
    // throughput charge moves the group and tenant counters but never the
    // run's, so per-run ceilings read zero forever and the gate
    // that exists to stop a runaway loop has nothing to compare against.
    run: crate::services::llm_refusal::RunAttribution,
) -> Response {
    // 1. Detect client and select backend
    let client_adapter = client::detect_client(headers);
    let metadata = client_adapter.extract_client_headers(headers);

    let backend = match backend::get_backend(&model.provider) {
        Some(b) => b,
        None => {
            return anthropic_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                &format!("No native backend for provider: {}", model.provider),
            );
        }
    };

    // 2. Client preprocessing + backend transform
    let mut body = body;
    client_adapter.preprocess_request(&mut body, &metadata, backend.backend_name());
    backend.transform_request(&mut body, model);

    // 2.5. Prompt-cache marker injection.
    //
    // The OpenAI-shape route at /v1/proxy/llm/chat/completions runs
    // this step via `llm_router::forward_chat_completion_*`. The native
    // pipeline forwards the Anthropic body directly to the upstream
    // without going through llm_router, so we have to invoke the cache
    // layer here ourselves — otherwise tenants who use Claude Code or
    // any Anthropic-SDK client at /v1/messages silently skip prompt
    // caching even when their group has a policy bound. The
    // `apply_to_request_async` dispatcher works on Anthropic native
    // shape directly (the `mark_*` functions live in
    // `llm_adapter::anthropic_cache` and were always written for
    // native shape).
    let prompt_cache = state
        .prompt_cache_policy
        .session(state, tenant_id, target_group, model)
        .await;
    if let Some(decision) = prompt_cache.mark(&mut body, model).await {
        crate::services::prompt_caching::record_decision(&decision);
        if decision.applied {
            tracing::debug!(
                provider = %model.provider,
                breakpoints = decision.breakpoints_inserted,
                "Prompt cache markers applied (native Anthropic pipeline)",
            );
        }
    }

    // 3. Build upstream URL + headers
    let upstream_url = backend.messages_url(model, false);
    let mut req_headers: Vec<(String, String)> = backend.auth_headers(model);

    if let Some(ref ver) = metadata.anthropic_version {
        req_headers.push(("anthropic-version".into(), ver.clone()));
    } else {
        req_headers.push(("anthropic-version".into(), "2023-06-01".into()));
    }

    if let Some(ref beta) = metadata.anthropic_beta {
        if let Some(filtered) = backend.filter_beta_headers(beta) {
            req_headers.push(("anthropic-beta".into(), filtered));
        }
    }

    // Ensure stream=false
    if let Some(obj) = body.as_object_mut() {
        obj.insert("stream".into(), Value::Bool(false));
    }

    // 4. Resolve provider-specific auth (SigV4 for Bedrock, OAuth for Vertex)
    let body_bytes = serde_json::to_vec(&body).unwrap_or_default();
    match resolve_provider_auth(model, &upstream_url, &body_bytes).await {
        Ok(extra_headers) => req_headers.extend(extra_headers),
        Err(e) => {
            return anthropic_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                &format!("Provider auth failed: {e}"),
            );
        }
    }

    // Cluster-wide RG / per-model concurrency gate — held for the request
    // (drops at fn return, releasing the slot). Acquired before begin_request
    // so a saturation 429 needs no gauge cleanup.
    let _conc_guard = match crate::services::llm_refusal::acquire_concurrency(
        state,
        target_group,
        model,
        &crate::services::llm_refusal::RefusalScope {
            auth_ctx,
            headers,
            correlation_id,
            endpoint: "/v1/messages",
            started: Some(start),
            model: Some((model, None)),
            group_id: None,
            run: Some(&run),
            request_body: None,
        },
    )
    .await
    {
        Ok(guard) => guard,
        Err(resp) => return resp,
    };

    // 5. Track + send
    state.router_state.increment_rpm(&model.id);
    state.router_state.begin_request(&model.id);

    let timeout_secs = model.timeout.map(|t| t as u64).unwrap_or(120);
    let mut req = state
        .http_client
        .post(&upstream_url)
        .header("Content-Type", "application/json")
        .timeout(std::time::Duration::from_secs(timeout_secs));

    for (k, v) in &req_headers {
        req = req.header(k.as_str(), v.as_str());
    }

    // Chain headers on the native
    // non-streaming send (see forward_native_streaming above).
    if let Some(pairs) = crate::services::call_chain::current_outbound_headers() {
        for (k, v) in pairs {
            req = req.header(k, v);
        }
    }

    let request_body_str = serde_json::to_string(&body).unwrap_or_default();
    let model_requested = body.get("model").and_then(|m| m.as_str()).map(String::from);

    let result = req.body(body_bytes).send().await;
    state.router_state.end_request(&model.id);

    let upstream_resp = match result {
        Ok(r) => r,
        Err(e) => {
            let duration = start.elapsed().as_millis() as f64;
            proxy_metrics::record_request("llm", "POST", 502, &model.provider);
            proxy_metrics::record_latency("llm", "POST", &model.provider, duration / 1000.0);
            return anthropic_error(
                StatusCode::BAD_GATEWAY,
                &format!("Upstream connection error: {e}"),
            );
        }
    };

    let status_code = upstream_resp.status().as_u16();
    crate::middleware::run_end::record_upstream_response(status_code, upstream_resp.headers());
    let mut resp_body: Value = upstream_resp
        .json()
        .await
        .unwrap_or(json!({"error": "Failed to parse response"}));
    let duration = start.elapsed().as_millis() as f64;
    let success = (200..300).contains(&status_code);

    if success {
        state.router_state.record_latency(&model.id, duration);
    }

    // 5. Extract usage from Anthropic response format
    let usage_obj = resp_body.get("usage");
    let prompt_tokens = usage_obj
        .and_then(|u| u.get("input_tokens"))
        .and_then(|v| v.as_i64())
        .map(|v| v as i32);
    let completion_tokens = usage_obj
        .and_then(|u| u.get("output_tokens"))
        .and_then(|v| v.as_i64())
        .map(|v| v as i32);

    // 6. Background logging
    let response_body_str = serde_json::to_string(&resp_body).unwrap_or_default();
    let user_agent = extract_user_agent(headers);
    let client_name = metadata.client_name.clone();

    let mut log = proxy_logger::build_proxy_log(
        "llm",
        auth_ctx,
        "POST",
        "/v1/messages",
        Some(&upstream_url),
        Some(&model.id),
        status_code,
        duration,
        success,
        None,
        correlation_id,
    );
    log.resource_group_id = target_group.map(|g| g.id.clone());
    log.proxy_subtype = Some(format!("anthropic_native_{client_name}"));
    log.user_agent = user_agent.clone();
    log.request_size_bytes = Some(request_body_str.len() as i32);
    log.request_headers = Some(proxy_logger::sanitize_headers(headers));
    log.request_body_preview = Some(body_preview(&request_body_str));
    log.response_size_bytes = Some(response_body_str.len() as i32);
    log.response_body_preview = Some(body_preview(&response_body_str));
    log.llm_model_requested = model_requested.clone();
    log.llm_model_used = Some(model.model_name.clone());
    log.llm_provider = Some(model.provider.clone());
    log.llm_prompt_tokens = prompt_tokens;
    log.llm_completion_tokens = completion_tokens;
    log.llm_total_tokens = prompt_tokens.zip(completion_tokens).map(|(p, c)| p + c);
    log.llm_streaming = Some(false);
    log.llm_used_fallback = Some(false);
    // The cached tokens the provider reported (see the streamed twin).
    let pc_mechanism = crate::services::prompt_cache_usage::mechanism_for_provider(
        &model.provider,
        &model.model_name,
    );
    let pc_usage = crate::services::prompt_cache_usage::extract_usage_from_response(
        &resp_body,
        pc_mechanism,
        model,
    );
    crate::routes::llm_helpers::apply_prompt_cache_usage(
        &mut log,
        &pc_usage,
        pc_mechanism,
        &model.provider,
    );
    if input_pii_redacted {
        proxy_logger::apply_pii_detected(&mut log);
    }
    // Stamp residency outcome (allowed / not-evaluated) on the
    // native non-streaming success row.
    match &residency_decision {
        Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
        None => proxy_logger::apply_residency_not_evaluated(&mut log, provider_region.as_deref()),
    }
    populate_llm_request_fields(&mut log, &body);
    // The row names its run and step, like every other LLM surface's.
    run.stamp(&mut log, auth_ctx);

    // Through the shared helper, so this row agrees with
    // the streaming row above and with the OpenAI surface about what a
    // partially-reported usage block costs.
    let estimated_cost = crate::routes::llm_helpers::estimate_llm_cost(
        prompt_tokens,
        completion_tokens,
        model.cost_per_input_token.unwrap_or(0.0),
        model.cost_per_output_token.unwrap_or(0.0),
    );
    log.llm_estimated_cost_usd = estimated_cost;

    // Post-response governance and output guardrails on the
    // native Anthropic response, at PARITY with the chat path. This was the
    // CRITICAL gap: the native non-streaming path forwarded model output with
    // NO output enforcement. We capture a block rather than returning
    // immediately so the deferred audit row records the FINAL status; the
    // helpers own the authoritative block row, so on a block we SKIP the
    // deferred success write (single-audit-row contract).
    let mut guardrail_block: Option<Response> = None;

    // Tool-call governance. Normalize native `content[].tool_use`
    // blocks into the chat tool-call view the helper expects.
    if success {
        let tool_view = native_anthropic_response_to_chat_tool_view(&resp_body);
        if let Err(resp) = crate::services::llm_preflight::apply_response_governance(
            state,
            &target_group.cloned(),
            &model.id,
            &tool_view,
            anthropic_error,
            &crate::services::llm_refusal::RefusalScope::new(
                auth_ctx,
                headers,
                correlation_id,
                "/v1/messages",
                &run,
            )
            .started(start),
        )
        .await
        {
            guardrail_block = Some(resp);
        }
    }

    // Output guardrails on each `content[].text` block. A Block returns
    // the Anthropic-shaped 403; a Redacted result is spliced back into the
    // block so the returned body + audit preview reflect the scrubbed text.
    if success && guardrail_block.is_none() {
        let leaves = extract_native_anthropic_output_text(&resp_body);
        'guard: for (i, content) in leaves {
            match crate::services::llm_preflight::apply_output_guardrails(
                state,
                tenant_id,
                &crate::services::llm_refusal::RefusalScope::new(
                    auth_ctx,
                    headers,
                    correlation_id,
                    "/v1/messages",
                    &run,
                )
                .started(start),
                &content,
                crate::constants::guardrail_surface::CHAT_OUTPUT,
                Some(crate::services::llm_preflight::anthropic_guardrail_violation_response),
            )
            .await
            {
                Err(resp) => {
                    guardrail_block = Some(resp);
                    break 'guard;
                }
                Ok(crate::services::llm_preflight::OutputCheckResult::Pass) => {}
                Ok(crate::services::llm_preflight::OutputCheckResult::Redacted {
                    content: new_text,
                    pii,
                }) => {
                    if let Some(slot) = resp_body.pointer_mut(&format!("/content/{i}/text")) {
                        *slot = Value::String(new_text);
                    }
                    if pii {
                        proxy_logger::apply_pii_detected(&mut log);
                    }
                }
            }
        }
    }

    // Output checks read `text` blocks only, so readable thinking goes no
    // further where a policy can block or redact (the log is written from
    // this body below).
    if success
        && guardrail_block.is_none()
        && resp_body
            .get("content")
            .and_then(|c| c.as_array())
            .is_some_and(|blocks| {
                blocks.iter().any(|b| {
                    b.get("type").and_then(|t| t.as_str()) == Some("thinking")
                        && b.get("thinking")
                            .and_then(|t| t.as_str())
                            .is_some_and(|t| !t.is_empty())
                })
            })
    {
        match crate::services::llm_preflight::reasoning_must_be_withheld(
            state,
            tenant_id,
            &crate::services::llm_refusal::RefusalScope::new(
                auth_ctx,
                headers,
                correlation_id,
                "/v1/messages",
                &run,
            )
            .started(start),
        )
        .await
        {
            Ok(true) => {
                blank_native_thinking(&mut resp_body);
            }
            Ok(false) => {}
            Err(resp) => guardrail_block = Some(resp),
        }
    }

    // On a block, the helper already wrote the authoritative block audit row,
    // so SKIP the deferred write. On pass/redact, refresh the preview from the
    // (possibly redacted) body and write the deferred success row.
    if guardrail_block.is_none() {
        let final_body_str = serde_json::to_string(&resp_body).unwrap_or_default();
        log.response_size_bytes = Some(final_body_str.len() as i32);
        log.response_body_preview = Some(body_preview(&final_body_str));
        state
            .background_writer
            .write_proxy_log(state.db.clone(), log);
    }

    let llm_metric_data = json!({
        "provider": model.provider,
        "model_requested": model_requested,
        "model_used": model.model_name,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens.zip(completion_tokens).map(|(p,c)| p+c),
        "estimated_cost_usd": estimated_cost,
        "streaming": false,
        "native_anthropic": true,
        "client_adapter": client_name,
    });
    let metric = proxy_logger::build_proxy_metric(
        "llm",
        auth_ctx,
        "POST",
        "/v1/messages",
        &format!("anthropic_native_{client_name}"),
        Some(&model.id),
        status_code,
        duration,
        success,
        correlation_id,
        Some(request_body_str.len() as i32),
        Some(response_body_str.len() as i32),
        Some(headers.len() as i32),
        None,
        user_agent.as_deref(),
        Some(llm_metric_data),
        None,
    );
    state
        .background_writer
        .write_proxy_metric(state.db.clone(), metric);

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
            prompt_tokens,
            completion_tokens,
            estimated_cost,
            model.cost_per_input_token.unwrap_or(0.0),
            model.cost_per_output_token.unwrap_or(0.0),
            &upstream_url,
            correlation_id,
            status_code,
            duration,
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

    proxy_metrics::record_request("llm", "POST", status_code, &model.provider);
    proxy_metrics::record_latency("llm", "POST", &model.provider, duration / 1000.0);
    if let Some(pt) = prompt_tokens {
        proxy_metrics::record_tokens("prompt", &model.provider, &model.model_name, pt);
    }
    if let Some(ct) = completion_tokens {
        proxy_metrics::record_tokens("completion", &model.provider, &model.model_name, ct);
    }
    if let Some(cost) = estimated_cost {
        proxy_metrics::record_cost(&model.provider, &model.model_name, cost);
    }

    // An output-governance or output-guardrail block returns the
    // Anthropic-shaped 403 instead of the model output. Usage/metrics above
    // still recorded the burned tokens (the upstream call already happened).
    if let Some(resp) = guardrail_block {
        return resp;
    }

    // 7. Return native Anthropic response
    let status = StatusCode::from_u16(status_code).unwrap_or(StatusCode::OK);
    let mut resp = (status, Json(resp_body)).into_response();
    resp.headers_mut().insert(
        crate::constants::headers::X_CORRELATION_ID,
        correlation_id.parse().unwrap(),
    );
    resp.headers_mut().insert(
        crate::constants::headers::X_AUTH_TYPE,
        auth_ctx.auth_type.to_string().parse().unwrap(),
    );
    resp
}

// ---------------------------------------------------------------------------
// Streaming output-guardrail emitters (StreamGuard → SSE)
// ---------------------------------------------------------------------------

type SseSender = tokio::sync::mpsc::Sender<Result<Event, axum::Error>>;

/// Emit an OpenAI-shaped [`StreamAction`](crate::services::stream_guard::StreamAction)
/// onto the adapter's SSE channel (the `force_openai_normalization`
/// path). Accumulates the on-the-wire text into `wire_content` so the
/// proxy log mirrors the redacted wire. Returns `true` to stop forwarding.
async fn emit_adapter_openai_action(
    tx: &SseSender,
    action: crate::services::stream_guard::StreamAction,
    total_chunks: &mut u32,
    guardrail_blocked: &mut bool,
    wire_content: &mut String,
    corr_id: &str,
) -> bool {
    use crate::services::stream_guard::StreamAction;
    match action {
        StreamAction::Block(reason) => {
            *guardrail_blocked = true;
            tracing::warn!(correlation_id = %corr_id, %reason, "Streaming output guardrail block (cloud-Claude, OpenAI surface)");
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
                crate::services::stream_guard::append_chunk_loggable_text(&p, wire_content);
                if tx.send(Ok(Event::default().data(p))).await.is_err() {
                    return true;
                }
                *total_chunks += 1;
            }
            false
        }
    }
}

/// Emit a text-level [`TextAction`](crate::services::stream_guard::TextAction)
/// onto the native-Anthropic SSE channel as `content_block_delta` events
/// (the passthrough path). Returns `true` to stop forwarding.
async fn emit_anthropic_text_action(
    tx: &SseSender,
    action: crate::services::stream_guard::TextAction,
    total_chunks: &mut u32,
    guardrail_blocked: &mut bool,
    wire_content: &mut String,
    corr_id: &str,
    block_index: i64,
) -> bool {
    use crate::services::stream_guard::TextAction;
    match action {
        TextAction::Block(reason) => {
            *guardrail_blocked = true;
            tracing::warn!(correlation_id = %corr_id, %reason, "Streaming output guardrail block (cloud-Claude, native /v1/messages)");
            let err = json!({
                "type": "error",
                "error": {"type": "guardrail_block", "message": "Output blocked by guardrail policy"}
            });
            let _ = tx
                .send(Ok(Event::default().event("error").data(err.to_string())))
                .await;
            true
        }
        TextAction::Emit(segments) => {
            for s in segments {
                wire_content.push_str(&s);
                let evt = json!({
                    "type": "content_block_delta",
                    "index": block_index,
                    "delta": {"type": "text_delta", "text": s}
                });
                if tx
                    .send(Ok(Event::default()
                        .event("content_block_delta")
                        .data(evt.to_string())))
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

/// Forward one native Anthropic SSE event verbatim. Returns `true` if the
/// client disconnected.
async fn send_anthropic_verbatim(
    tx: &SseSender,
    event_type: &str,
    event_data: &str,
    total_chunks: &mut u32,
) -> bool {
    let event = if event_type.is_empty() {
        Event::default().data(event_data)
    } else {
        Event::default().event(event_type).data(event_data)
    };
    if tx.send(Ok(event)).await.is_err() {
        return true;
    }
    *total_chunks += 1;
    false
}

/// Redact (sync mode) or block-scan (async mode) one native Anthropic SSE
/// event, forwarding structural events verbatim. `content_block_delta`
/// text is run through the output guard and re-injected redacted; the
/// held-back tail is flushed before terminal events. Returns `true` to
/// stop forwarding.
async fn redact_and_emit_anthropic_event(
    tx: &SseSender,
    event_type: &str,
    event_data: &str,
    stream_guard: &mut crate::services::stream_guard::StreamGuard,
    total_chunks: &mut u32,
    guardrail_blocked: &mut bool,
    wire_content: &mut String,
    corr_id: &str,
) -> bool {
    use crate::services::stream_guard::TextAction;

    let parsed: Value = serde_json::from_str(event_data).unwrap_or(Value::Null);
    let etype = parsed
        .get("type")
        .and_then(|t| t.as_str())
        .unwrap_or(event_type);

    // Readable thinking is not scanned: where a policy can block or redact
    // (sync), it is not forwarded. The block's start and signature still are.
    if stream_guard.is_sync() && is_readable_thinking_delta(&parsed) {
        return false;
    }

    // Text-bearing delta → run through the output guard.
    if etype == "content_block_delta" {
        if let Some(text) = parsed
            .get("delta")
            .and_then(|d| d.get("text"))
            .and_then(|t| t.as_str())
        {
            let idx = parsed.get("index").and_then(|i| i.as_i64()).unwrap_or(0);
            if stream_guard.is_sync() {
                // Sync: re-inject redacted segments (or hold back).
                let action = stream_guard.process_text(text).await;
                return emit_anthropic_text_action(
                    tx,
                    action,
                    total_chunks,
                    guardrail_blocked,
                    wire_content,
                    corr_id,
                    idx,
                )
                .await;
            } else {
                // Async: forward verbatim, scan to decide Block.
                if let TextAction::Block(reason) = stream_guard.process_text(text).await {
                    return emit_anthropic_text_action(
                        tx,
                        TextAction::Block(reason),
                        total_chunks,
                        guardrail_blocked,
                        wire_content,
                        corr_id,
                        idx,
                    )
                    .await;
                }
                wire_content.push_str(text);
                return send_anthropic_verbatim(tx, event_type, event_data, total_chunks).await;
            }
        }
    }

    // Flush any held-back tail before a terminal boundary (sync mode).
    if stream_guard.is_sync()
        && matches!(
            etype,
            "content_block_stop" | "message_delta" | "message_stop"
        )
    {
        let action = stream_guard.finish_text().await;
        if emit_anthropic_text_action(
            tx,
            action,
            total_chunks,
            guardrail_blocked,
            wire_content,
            corr_id,
            0,
        )
        .await
        {
            return true;
        }
    }

    // Structural / non-text event — forward verbatim.
    send_anthropic_verbatim(tx, event_type, event_data, total_chunks).await
}

// ---------------------------------------------------------------------------
// Anthropic SSE observer — taps stream for logging/metrics
// ---------------------------------------------------------------------------

/// Observe a native Anthropic SSE event for logging/metrics purposes.
/// Extracts usage data, content, and finish reason without modifying the event.
fn observe_anthropic_sse_event(
    event_type: &str,
    data: &str,
    accumulated_content: &mut String,
    finish_reason: &mut Option<String>,
    prompt_tokens: &mut Option<i32>,
    completion_tokens: &mut Option<i32>,
) {
    let parsed: Value = match serde_json::from_str(data) {
        Ok(v) => v,
        Err(_) => return,
    };

    match event_type {
        "message_start" => {
            // Extract input tokens from message_start.message.usage.input_tokens
            if let Some(input) = parsed
                .get("message")
                .and_then(|m| m.get("usage"))
                .and_then(|u| u.get("input_tokens"))
                .and_then(|v| v.as_i64())
            {
                *prompt_tokens = Some(input as i32);
            }
        }
        "content_block_delta" => {
            // Accumulate text content for logging
            if let Some(text) = parsed
                .get("delta")
                .and_then(|d| d.get("text"))
                .and_then(|t| t.as_str())
            {
                accumulated_content.push_str(text);
            }
        }
        "message_delta" => {
            // Extract stop_reason and output token usage
            if let Some(sr) = parsed
                .get("delta")
                .and_then(|d| d.get("stop_reason"))
                .and_then(|r| r.as_str())
            {
                *finish_reason = Some(sr.to_string());
            }
            if let Some(output) = parsed
                .get("usage")
                .and_then(|u| u.get("output_tokens"))
                .and_then(|v| v.as_i64())
            {
                *completion_tokens = Some(output as i32);
            }
        }
        _ => {}
    }
}

/// Read the provider's prompt-cache counters off a native Anthropic
/// usage event. `message_start` carries them; `message_delta` may repeat
/// them as running totals, so the largest value seen is the call's.
fn observe_anthropic_cache_usage(
    event_type: &str,
    data: &str,
    cache_write_tokens: &mut i32,
    cache_read_tokens: &mut i32,
) {
    if !matches!(event_type, "message_start" | "message_delta") {
        return;
    }
    let Ok(parsed) = serde_json::from_str::<Value>(data) else {
        return;
    };
    let Some(usage) = parsed
        .get("message")
        .and_then(|m| m.get("usage"))
        .or_else(|| parsed.get("usage"))
    else {
        return;
    };
    let read = |key: &str| usage.get(key).and_then(Value::as_i64).unwrap_or(0) as i32;
    *cache_write_tokens = (*cache_write_tokens).max(read("cache_creation_input_tokens"));
    *cache_read_tokens = (*cache_read_tokens).max(read("cache_read_input_tokens"));
}

/// Accumulate native Anthropic tool_use blocks from streaming SSE
/// events into `tool_blocks` (keyed by content-block index). A
/// `content_block_start` whose block is `type:"tool_use"` opens an entry
/// (id + name); subsequent `content_block_delta` `input_json_delta` frames
/// on that index append their `partial_json` fragment to the args string.
fn observe_anthropic_tool_use(
    data: &str,
    tool_blocks: &mut std::collections::BTreeMap<i64, (String, String, String)>,
) {
    let parsed: Value = match serde_json::from_str(data) {
        Ok(v) => v,
        Err(_) => return,
    };
    match parsed.get("type").and_then(|t| t.as_str()) {
        Some("content_block_start") => {
            let idx = parsed.get("index").and_then(|i| i.as_i64()).unwrap_or(0);
            if let Some(cb) = parsed.get("content_block") {
                if cb.get("type").and_then(|t| t.as_str()) == Some("tool_use") {
                    let id = cb
                        .get("id")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string();
                    let name = cb
                        .get("name")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string();
                    tool_blocks.insert(idx, (id, name, String::new()));
                }
            }
        }
        Some("content_block_delta") => {
            let idx = parsed.get("index").and_then(|i| i.as_i64()).unwrap_or(0);
            if let Some(frag) = parsed
                .get("delta")
                .filter(|d| d.get("type").and_then(|t| t.as_str()) == Some("input_json_delta"))
                .and_then(|d| d.get("partial_json"))
                .and_then(|p| p.as_str())
            {
                if let Some(entry) = tool_blocks.get_mut(&idx) {
                    entry.2.push_str(frag);
                }
            }
        }
        _ => {}
    }
}

// ---------------------------------------------------------------------------
// Extract text from Anthropic content blocks (for guardrails)
// ---------------------------------------------------------------------------

/// Normalize a native Anthropic response into a chat-shaped `Value`
/// (`choices[0].message.tool_calls[]`) so the unchanged
/// `apply_response_governance` (banned-arg patterns + AST argument
/// policies + tool-call rate counters) can scan emitted tool calls (G9).
///
/// Anthropic tool calls are `content[]` blocks of `type == "tool_use"` with
/// top-level `name` + `input` (a JSON object). The chat governance reader
/// expects `function.arguments` as a JSON-encoded STRING, so we serialize
/// `input`.
fn native_anthropic_response_to_chat_tool_view(resp_body: &Value) -> Value {
    let mut tool_calls: Vec<Value> = Vec::new();
    if let Some(blocks) = resp_body.get("content").and_then(|c| c.as_array()) {
        for block in blocks {
            if block.get("type").and_then(|t| t.as_str()) == Some("tool_use") {
                let name = block.get("name").cloned().unwrap_or(Value::Null);
                let args_str = block
                    .get("input")
                    .map(|i| serde_json::to_string(i).unwrap_or_default())
                    .unwrap_or_default();
                tool_calls.push(json!({
                    "id": block.get("id").cloned().unwrap_or(Value::Null),
                    "type": "function",
                    "function": { "name": name, "arguments": args_str }
                }));
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

/// Extract assistant text from a native Anthropic response's `content[]`
/// `type:"text"` blocks for output-guardrail scanning (G1). Returns
/// `(block_index, text)` pairs so a redaction can be spliced back into the
/// exact `content[i].text` leaf.
fn extract_native_anthropic_output_text(resp_body: &Value) -> Vec<(usize, String)> {
    let mut out = Vec::new();
    if let Some(blocks) = resp_body.get("content").and_then(|c| c.as_array()) {
        for (i, block) in blocks.iter().enumerate() {
            if block.get("type").and_then(|t| t.as_str()) == Some("text") {
                if let Some(txt) = block.get("text").and_then(|t| t.as_str()) {
                    if !txt.is_empty() {
                        out.push((i, txt.to_string()));
                    }
                }
            }
        }
    }
    out
}

/// Empty the text of every native `thinking` block that has readable text,
/// keeping its signature. Output checks read `text` blocks only. Models from
/// Claude 4.7 on are asked for `display: "omitted"`, so this is their
/// fail-closed backstop; on older models, which cannot omit, this is how the
/// text is withheld. The emptied block still replays: Claude checks only its
/// signature. Returns whether anything was removed.
pub(crate) fn blank_native_thinking(resp_body: &mut Value) -> bool {
    let Some(blocks) = resp_body.get_mut("content").and_then(|c| c.as_array_mut()) else {
        return false;
    };
    let mut found = false;
    for block in blocks.iter_mut() {
        if block.get("type").and_then(|t| t.as_str()) != Some("thinking") {
            continue;
        }
        if let Some(text) = block.get_mut("thinking")
            && text.as_str().is_some_and(|t| !t.is_empty())
        {
            *text = Value::String(String::new());
            found = true;
        }
    }
    found
}

/// Whether a native stream event is a `thinking_delta` with readable text.
fn is_readable_thinking_delta(event: &Value) -> bool {
    event.get("type").and_then(|t| t.as_str()) == Some("content_block_delta")
        && event.pointer("/delta/type").and_then(|t| t.as_str()) == Some("thinking_delta")
        && event
            .pointer("/delta/thinking")
            .and_then(|t| t.as_str())
            .is_some_and(|t| !t.is_empty())
}

/// Extract plaintext from Anthropic Messages format for guardrails input checking.
/// Works directly on the native format — no OpenAI translation needed.
pub fn extract_input_text(body: &Value) -> String {
    let mut parts = Vec::new();

    // System prompt
    if let Some(system) = body.get("system") {
        match system {
            Value::String(s) => parts.push(s.clone()),
            Value::Array(blocks) => {
                for block in blocks {
                    if let Some(text) = block.get("text").and_then(|t| t.as_str()) {
                        parts.push(text.to_string());
                    }
                }
            }
            _ => {}
        }
    }

    // Messages
    if let Some(messages) = body.get("messages").and_then(|m| m.as_array()) {
        for msg in messages {
            if let Some(content) = msg.get("content") {
                match content {
                    Value::String(s) => parts.push(s.clone()),
                    Value::Array(blocks) => {
                        for block in blocks {
                            if let Some(text) = block.get("text").and_then(|t| t.as_str()) {
                                parts.push(text.to_string());
                            }
                        }
                    }
                    _ => {}
                }
            }
        }
    }

    parts.join("\n")
}

/// Inverse of [`extract_input_text`]. Walks `body` in the SAME
/// order as the extractor (system → messages → content blocks)
/// and overwrites each string slot with the next `\n`-separated
/// segment of `redacted`. Returns `false` (no mutation) when
/// the split count drifts from the extracted-leaf count — caller
/// then forwards the original body.
///
/// Used by routes/anthropic.rs when the input guardrail's
/// Redacted outcome carries a rewritten prompt.
pub fn apply_redactions_to_input(body: &mut Value, redacted: &str) -> bool {
    fn count(v: &Value) -> usize {
        match v {
            Value::String(_) => 1,
            Value::Array(blocks) => blocks
                .iter()
                .filter(|b| b.get("text").and_then(|t| t.as_str()).is_some())
                .count(),
            _ => 0,
        }
    }
    let mut expected = 0usize;
    if let Some(system) = body.get("system") {
        expected += count(system);
    }
    if let Some(messages) = body.get("messages").and_then(|m| m.as_array()) {
        for msg in messages {
            if let Some(content) = msg.get("content") {
                expected += count(content);
            }
        }
    }
    if expected == 0 {
        return false;
    }
    let parts: Vec<&str> = redacted.split('\n').collect();
    if parts.len() != expected {
        tracing::warn!(
            target: "guardrails",
            surface = "llm",
            "Anthropic redaction split-count mismatch (got {}, expected {}); forwarding original",
            parts.len(),
            expected,
        );
        return false;
    }
    let mut idx = 0usize;
    fn replace(v: &mut Value, parts: &[&str], idx: &mut usize) {
        match v {
            Value::String(s) => {
                if let Some(p) = parts.get(*idx) {
                    *s = (*p).to_string();
                }
                *idx += 1;
            }
            Value::Array(blocks) => {
                for block in blocks.iter_mut() {
                    if let Some(obj) = block.as_object_mut() {
                        if obj.get("text").and_then(|t| t.as_str()).is_some() {
                            if let Some(p) = parts.get(*idx) {
                                obj.insert("text".to_string(), Value::String((*p).to_string()));
                            }
                            *idx += 1;
                        }
                    }
                }
            }
            _ => {}
        }
    }
    if let Some(system) = body.get_mut("system") {
        replace(system, &parts, &mut idx);
    }
    if let Some(messages) = body.get_mut("messages").and_then(|m| m.as_array_mut()) {
        for msg in messages.iter_mut() {
            if let Some(content) = msg.get_mut("content") {
                replace(content, &parts, &mut idx);
            }
        }
    }
    true
}

// ---------------------------------------------------------------------------
// Error helper
// ---------------------------------------------------------------------------

/// Return errors in Anthropic API error format.
pub fn anthropic_error(status: StatusCode, message: &str) -> Response {
    let error_type = match status {
        StatusCode::BAD_REQUEST => "invalid_request_error",
        StatusCode::UNAUTHORIZED => "authentication_error",
        StatusCode::FORBIDDEN => "permission_error",
        StatusCode::NOT_FOUND => "not_found_error",
        StatusCode::TOO_MANY_REQUESTS => "rate_limit_error",
        _ => "api_error",
    };
    let body = json!({
        "type": "error",
        "error": {
            "type": error_type,
            "message": message,
        }
    });
    (status, Json(body)).into_response()
}

// ---------------------------------------------------------------------------
// OpenAI-surface → Anthropic-native bridge
// ---------------------------------------------------------------------------
//
// Cloud-hosted Anthropic models (Bedrock-Claude, Vertex-Claude) can't
// use Brutor's standard `forward_chat_completion_*` path because that
// path: (a) doesn't translate the OpenAI body shape to Anthropic
// native, (b) doesn't run SigV4 / the Google access token, and (c) doesn't know the
// provider-specific URL pattern. The native `/v1/messages` route
// already handles all of that for clients that speak Anthropic
// shape; this bridge gives OpenAI-shape clients access to the same
// pipeline.
//
// Same translate-on-exit shape as the Anthropic-direct path uses
// internally, but the translation runs HERE (in the OpenAI route's
// dispatcher) rather than via `AnthropicAdapter::transform_request`
// during prepare_request — because for `provider == "bedrock"` the
// dispatcher's adapter resolves to `BedrockAdapter`, not
// `AnthropicAdapter`.

/// True when an OpenAI-surface request (`/v1/proxy/llm/chat/completions`)
/// should be dispatched through the native Anthropic pipeline. Catches:
///
///   * Bedrock-Claude (`provider="bedrock"`, model name contains
///     `anthropic` or `claude`).
///   * Vertex-Claude (`provider="vertex_ai"`, model name contains
///     `claude`).
///
/// Anthropic-direct is NOT included — the standard adapter path
/// handles it correctly because `AnthropicAdapter::transform_request`
/// runs during `prepare_request` for that provider.
pub fn should_route_via_openai_surface(model: &LlmModel) -> bool {
    let model_lc = model.model_name.to_ascii_lowercase();
    match model.provider.as_str() {
        // A Claude row of the catalog's `bedrock_converse` provider too.
        // The gateway implements no Converse call: such
        // a row went out as an OpenAI chat completion (`…/chat/completions`,
        // unsigned, the catalog name as `model`, sampling parameters and
        // `reasoning_effort` as given), which no Bedrock endpoint accepts.
        // It is served through InvokeModel, the same model id, where the
        // Claude rules apply.
        "bedrock" | "bedrock_converse" => {
            model_lc.contains("anthropic") || model_lc.contains("claude")
        }
        "vertex_ai" | "vertex" => model_lc.contains("claude"),
        _ => false,
    }
}

/// Bridge a non-streaming OpenAI-surface request for a cloud-hosted
/// Anthropic model through the native pipeline.
///
/// The caller has the request in `ChatCompletionRequest` (OpenAI shape).
/// We translate it to Anthropic native shape, hand it to
/// `forward_native_non_streaming`, and translate the response back to
/// OpenAI shape before returning to the client.
#[allow(clippy::too_many_arguments)]
pub async fn forward_via_openai_surface_non_streaming(
    state: &SharedState,
    headers: &HeaderMap,
    openai_body: Value,
    model: &LlmModel,
    auth_ctx: &AuthContext,
    tenant_id: &str,
    target_group: Option<&ResourceGroup>,
    correlation_id: &str,
    start: Instant,
    // Threaded through to the native pipeline's success audit row so a
    // PII-redacted OpenAI-surface request stamps pii_detected.
    input_pii_redacted: bool,
    // The run this call belongs to. Without it the
    // throughput charge moves the group and tenant counters but never the
    // run's, so per-run ceilings read zero forever and the gate
    // that exists to stop a runaway loop has nothing to compare against.
    run: crate::services::llm_refusal::RunAttribution,
) -> Response {
    // Translate OpenAI body → Anthropic native using the same
    // AnthropicAdapter logic that `prepare_request` would have called
    // if `get_adapter` had returned AnthropicAdapter for this provider.
    let body = translate_openai_to_anthropic(openai_body, model);

    // The native pipeline does the rest: backend detection, SigV4 /
    // the Google access token, prompt-cache markers, upstream call, proxy_log.
    let native_response = forward_native_non_streaming(
        state,
        headers,
        body,
        model,
        auth_ctx,
        tenant_id,
        target_group,
        correlation_id,
        start,
        input_pii_redacted,
        // Residency for OpenAI-surface requests was already evaluated +
        // stamped on the chat path (routes/llm.rs) before this bridge ran.
        None,
        None,
        run,
    )
    .await;

    // Translate the response body Anthropic-native → OpenAI shape.
    // The proxy_log row written by the native pipeline carries the
    // upstream native-shape fields; only the client-visible body
    // needs to change.
    translate_anthropic_response_to_openai(native_response).await
}

/// Streaming variant of `forward_via_openai_surface_non_streaming`.
/// Same translation contract — translate the OpenAI request body to
/// Anthropic native shape on the way in, but on the way OUT each SSE
/// event is translated from Anthropic native SSE to OpenAI SSE chunks
/// (via `AnthropicStreamNormalizer` engaged in
/// `forward_native_streaming` by the `force_openai_normalization`
/// flag).
#[allow(clippy::too_many_arguments)]
pub async fn forward_via_openai_surface_streaming(
    state: &SharedState,
    headers: &HeaderMap,
    openai_body: Value,
    model: &LlmModel,
    auth_ctx: &AuthContext,
    tenant_id: &str,
    target_group: Option<&ResourceGroup>,
    correlation_id: &str,
    start: Instant,
    // Threaded through to the native pipeline's success audit row so a
    // PII-redacted OpenAI-surface request stamps pii_detected.
    input_pii_redacted: bool,
    // The run this call belongs to. Without it the
    // throughput charge moves the group and tenant counters but never the
    // run's, so per-run ceilings read zero forever and the gate
    // that exists to stop a runaway loop has nothing to compare against.
    run: crate::services::llm_refusal::RunAttribution,
) -> Response {
    let body = translate_openai_to_anthropic(openai_body, model);

    // `force_openai_normalization=true` is the single bit that swaps
    // verbatim Anthropic SSE for OpenAI-shape chunks. Everything else
    // — SigV4 / the Google access token, prompt-cache markers, proxy_log — is identical
    // to the native /v1/messages streaming path.
    forward_native_streaming(
        state,
        headers,
        body,
        model,
        auth_ctx,
        tenant_id,
        target_group,
        correlation_id,
        start,
        true,
        input_pii_redacted,
        // Residency already evaluated + stamped on the chat path before
        // this OpenAI-surface bridge ran.
        None,
        None,
        run,
    )
    .await
}

fn translate_openai_to_anthropic(mut body: Value, model: &LlmModel) -> Value {
    use crate::services::llm_adapter::AnthropicAdapter;
    use crate::services::llm_adapter::LlmAdapter;

    if let Some(obj) = body.as_object_mut() {
        // `reasoning_effort` is the OpenAI surface's parameter; Messages has
        // none of that name, and it must not reach Bedrock as it is.
        // A caller who asks for reasoning gets the
        // thinking block the model accepts, as the Anthropic adapter injects
        // it for a row with `thinking_enabled`: adaptive from Claude 4.6 on
        // (a fixed `budget_tokens` is a 400 from 4.7 on), a budget before.
        // `none` asks for no thinking: nothing is sent (the 5.5 models
        // refuse `disabled`). A `thinking` block of the caller's own stands.
        if let Some(effort) = obj.remove("reasoning_effort") {
            let effort = effort.as_str().unwrap_or("").to_ascii_lowercase();
            if !effort.is_empty() && effort != "none" && !obj.contains_key("thinking") {
                let name = &model.model_name;
                let block = if crate::services::providers::anthropic_uses_adaptive_thinking(name) {
                    if crate::services::providers::anthropic_thinking_summary_is_opt_in(name) {
                        serde_json::json!({ "type": "adaptive", "display": "summarized" })
                    } else {
                        serde_json::json!({ "type": "adaptive" })
                    }
                } else {
                    let budget = match effort.as_str() {
                        "low" | "minimal" => 1024,
                        "high" | "xhigh" | "max" => 15_000,
                        _ => 10_000,
                    };
                    serde_json::json!({ "type": "enabled", "budget_tokens": budget })
                };
                obj.insert("thinking".into(), block);
            }
        }
        let adapter = AnthropicAdapter;
        adapter.transform_request(obj, model);
        // The OpenAI surface attaches `stream` here too — Anthropic
        // accepts it but the native pipeline forces it false (or true
        // for streaming) below, so strip to avoid confusion.
        obj.remove("stream");
        obj.remove("model_id");
        // OpenAI shape sometimes includes `max_completion_tokens`
        // instead of `max_tokens`. Anthropic uses `max_tokens` only.
        if !obj.contains_key("max_tokens") {
            if let Some(mct) = obj.remove("max_completion_tokens") {
                obj.insert("max_tokens".into(), mct);
            }
        }
    }
    // What the catalog row (and its provider) says the upstream refuses, as
    // `prepare_request` applies it after the adapter on the direct path: the
    // row decides where it says so.
    crate::services::providers::drop_unsupported_params(&mut body, model);
    body
}

async fn translate_anthropic_response_to_openai(resp: Response) -> Response {
    use crate::services::llm_adapter::AnthropicAdapter;
    use crate::services::llm_adapter::LlmAdapter;
    use axum::body::to_bytes;
    use axum::http::header;

    let (mut parts, body) = resp.into_parts();
    let bytes = match to_bytes(body, 16 * 1024 * 1024).await {
        Ok(b) => b,
        Err(_) => {
            // If we can't read the body, fall through with an error
            // payload that's at least the right shape for OpenAI
            // clients (they expect `error.message`).
            return anthropic_error(
                StatusCode::BAD_GATEWAY,
                "Failed to read upstream response body for OpenAI translation",
            );
        }
    };

    let mut json: Value = match serde_json::from_slice(&bytes) {
        Ok(v) => v,
        Err(_) => {
            // Non-JSON response — pass through opaque (caller will
            // see the raw bytes; non-200 status carries the signal).
            return Response::from_parts(parts, axum::body::Body::from(bytes));
        }
    };

    // The native pipeline returned a body in Anthropic shape on
    // success; translate to OpenAI shape via the same
    // `normalize_response` the standard Anthropic adapter path uses.
    AnthropicAdapter.normalize_response(&mut json);

    let new_bytes = serde_json::to_vec(&json).unwrap_or_default();
    // Force Content-Type to application/json — the native pipeline
    // sets this correctly, but a translated body should be re-stamped
    // in case some upstream returned a Bedrock-specific content type.
    parts.headers.insert(
        header::CONTENT_TYPE,
        axum::http::HeaderValue::from_static("application/json"),
    );
    // Drop content-length; the new body has a different size.
    parts.headers.remove(header::CONTENT_LENGTH);

    Response::from_parts(parts, axum::body::Body::from(new_bytes))
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn readable_native_thinking_is_cleared_and_its_signature_kept() {
        let mut body = json!({"content": [
            {"type": "thinking", "thinking": "why", "signature": "s1"},
            {"type": "thinking", "thinking": "", "signature": "s2"},
            {"type": "redacted_thinking", "data": "d"},
            {"type": "text", "text": "answer"}
        ]});
        assert!(blank_native_thinking(&mut body));
        assert_eq!(
            body["content"][0],
            json!({"type": "thinking", "thinking": "", "signature": "s1"})
        );
        assert_eq!(body["content"][1]["signature"], "s2");
        assert_eq!(body["content"][2]["data"], "d");
        assert_eq!(body["content"][3]["text"], "answer");
        assert!(!blank_native_thinking(&mut body), "nothing left to clear");
    }

    #[test]
    fn only_a_thinking_delta_with_text_is_readable() {
        let delta = |d: Value| json!({"type": "content_block_delta", "index": 0, "delta": d});
        assert!(is_readable_thinking_delta(&delta(
            json!({"type": "thinking_delta", "thinking": "why"})
        )));
        assert!(!is_readable_thinking_delta(&delta(
            json!({"type": "thinking_delta", "thinking": ""})
        )));
        assert!(!is_readable_thinking_delta(&delta(
            json!({"type": "signature_delta", "signature": "s"})
        )));
        assert!(!is_readable_thinking_delta(&delta(
            json!({"type": "text_delta", "text": "why"})
        )));
    }

    #[test]
    fn observe_message_start_extracts_input_tokens() {
        let mut content = String::new();
        let mut finish = None;
        let mut pt = None;
        let mut ct = None;

        observe_anthropic_sse_event(
            "message_start",
            r#"{"type":"message_start","message":{"id":"msg_1","usage":{"input_tokens":42}}}"#,
            &mut content,
            &mut finish,
            &mut pt,
            &mut ct,
        );

        assert_eq!(pt, Some(42));
    }

    #[test]
    fn observe_content_delta_accumulates_text() {
        let mut content = String::new();
        let mut finish = None;
        let mut pt = None;
        let mut ct = None;

        observe_anthropic_sse_event(
            "content_block_delta",
            r#"{"type":"content_block_delta","delta":{"type":"text_delta","text":"Hello "}}"#,
            &mut content,
            &mut finish,
            &mut pt,
            &mut ct,
        );
        observe_anthropic_sse_event(
            "content_block_delta",
            r#"{"type":"content_block_delta","delta":{"type":"text_delta","text":"world!"}}"#,
            &mut content,
            &mut finish,
            &mut pt,
            &mut ct,
        );

        assert_eq!(content, "Hello world!");
    }

    #[test]
    fn observe_message_delta_extracts_finish_and_tokens() {
        let mut content = String::new();
        let mut finish = None;
        let mut pt = None;
        let mut ct = None;

        observe_anthropic_sse_event(
            "message_delta",
            r#"{"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":100}}"#,
            &mut content,
            &mut finish,
            &mut pt,
            &mut ct,
        );

        assert_eq!(finish.as_deref(), Some("end_turn"));
        assert_eq!(ct, Some(100));
    }

    #[test]
    fn extract_input_text_from_anthropic_body() {
        let body = json!({
            "system": "You are a helpful assistant.",
            "messages": [
                {"role": "user", "content": "Hello!"},
                {"role": "assistant", "content": [
                    {"type": "text", "text": "Hi there!"}
                ]},
                {"role": "user", "content": [
                    {"type": "text", "text": "How are you?"},
                    {"type": "tool_reference", "tool_name": "search"}
                ]}
            ]
        });

        let text = extract_input_text(&body);
        assert!(text.contains("You are a helpful assistant."));
        assert!(text.contains("Hello!"));
        assert!(text.contains("Hi there!"));
        assert!(text.contains("How are you?"));
        assert!(!text.contains("search")); // tool_reference has no "text" field
    }

    #[test]
    fn extract_input_text_system_as_array() {
        let body = json!({
            "system": [
                {"type": "text", "text": "System part 1"},
                {"type": "text", "text": "System part 2"}
            ],
            "messages": []
        });

        let text = extract_input_text(&body);
        assert!(text.contains("System part 1"));
        assert!(text.contains("System part 2"));
    }

    // ── should_route_via_openai_surface ──────────────────────────────

    #[test]
    fn should_route_bedrock_claude_via_openai_surface() {
        let model =
            crate::services::llm_adapter::make_test_model("bedrock", "anthropic.claude-sonnet-4-5");
        assert!(should_route_via_openai_surface(&model));
    }

    #[test]
    fn should_route_vertex_claude_via_openai_surface() {
        let model =
            crate::services::llm_adapter::make_test_model("vertex_ai", "claude-opus-4-5-20251124");
        assert!(should_route_via_openai_surface(&model));
    }

    #[test]
    fn does_not_route_bedrock_nova_via_openai_surface() {
        // Bedrock-Nova doesn't speak Anthropic shape; can't go through
        // the Anthropic-native pipeline.
        let model =
            crate::services::llm_adapter::make_test_model("bedrock", "amazon.nova-pro-v1:0");
        assert!(!should_route_via_openai_surface(&model));
    }

    #[test]
    fn does_not_route_vertex_gemini_via_openai_surface() {
        let model = crate::services::llm_adapter::make_test_model("vertex_ai", "gemini-2.5-pro");
        assert!(!should_route_via_openai_surface(&model));
    }

    #[test]
    fn does_not_route_anthropic_direct_via_openai_surface() {
        // Anthropic-direct works fine on the OpenAI surface via the
        // standard AnthropicAdapter path — no need to detour.
        let model =
            crate::services::llm_adapter::make_test_model("anthropic", "claude-haiku-4-5-20251001");
        assert!(!should_route_via_openai_surface(&model));
    }

    // ── apply_redactions_to_input ───────────────────────────────────

    #[test]
    fn anthropic_redact_string_system_and_string_content() {
        let mut body = json!({
            "system": "you handle PII carefully",
            "messages": [
                {"role": "user", "content": "my SSN is 123-45-6789"}
            ]
        });
        // Two leaves: system, then content.
        let redacted = "you handle PII carefully\nmy SSN is [REDACTED-SSN]";
        assert!(apply_redactions_to_input(&mut body, redacted));
        assert_eq!(body["system"], "you handle PII carefully");
        assert_eq!(body["messages"][0]["content"], "my SSN is [REDACTED-SSN]");
    }

    #[test]
    fn anthropic_redact_array_content_blocks() {
        // Anthropic supports `content` as either a string OR an
        // array of {type: "text", text: "..."} blocks. We only
        // touch text blocks.
        let mut body = json!({
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "ssn 123-45-6789"},
                    {"type": "image", "source": {"data": "..."}},
                    {"type": "text", "text": "credit-card 4111-1111-1111-1111"}
                ]}
            ]
        });
        // Two text leaves.
        let redacted = "ssn [REDACTED-SSN]\ncredit-card [REDACTED-CC]";
        assert!(apply_redactions_to_input(&mut body, redacted));
        assert_eq!(
            body["messages"][0]["content"][0]["text"],
            "ssn [REDACTED-SSN]"
        );
        // Image block unchanged.
        assert_eq!(body["messages"][0]["content"][1]["type"], "image");
        assert_eq!(
            body["messages"][0]["content"][2]["text"],
            "credit-card [REDACTED-CC]"
        );
    }

    #[test]
    fn anthropic_redact_no_op_on_count_mismatch() {
        let mut body = json!({
            "messages": [
                {"role": "user", "content": "a"},
                {"role": "user", "content": "b"}
            ]
        });
        let original = body.clone();
        assert!(!apply_redactions_to_input(&mut body, "single-line"));
        assert_eq!(body, original);
    }

    #[test]
    fn anthropic_redact_no_op_on_empty_payload() {
        let mut body = json!({"messages": []});
        let original = body.clone();
        assert!(!apply_redactions_to_input(&mut body, "anything"));
        assert_eq!(body, original);
    }
}
