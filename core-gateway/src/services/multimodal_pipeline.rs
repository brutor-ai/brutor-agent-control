// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Multimodal Pipeline Helpers
//!
//! The chat-completions handler in `routes/llm.rs` runs every
//! request through a well-defined pipeline:
//!
//!   1. Authenticate
//!   2. Quota pre-check (RPM / TPM / budget)
//!   3. Group access check, then the actor planes (agent authorization,
//!      the autonomy gate, semantic policy on the input)
//!   4. Input guardrails
//!   5. Resilience gate (circuit breaker, concurrency)
//!   6. Forward upstream
//!   7. Output guardrails — on per-route text-bearing fields:
//!      * `audio_transcribe` → the transcript `text`
//!      * `images` → the per-image `revised_prompt`
//!      * `audio_speech`, `video` → response is binary (no text
//!        to guard; input text is checked at step 4)
//!   8. Usage record (`usage_records` row)
//!   9. proxy_logs row (audit)
//!  10. Resilience signal (record_success / record_failure)
//!
//! The multimodal routes (image, audio_speech, audio_transcribe,
//! video) used to skip steps 2, 3, 8, 9, leaving a financial-
//! exposure + audit gap. This module is the shared helper layer
//! that brings them into compliance:
//!
//!   * `multimodal_preflight` — auth + model + group access + quota.
//!     Returns the resolved `AuthContext`, `LlmModel`, and target
//!     `ResourceGroup` so the route can use them downstream.
//!
//!   * `record_multimodal_outcome` — write a `proxy_logs` row AND a
//!     `usage_records` row in one call. Use after the upstream
//!     completes (success or failure). Cost is supplied by the
//!     caller because each modality computes it differently
//!     (per-character TTS, per-image, per-second video, …).
//!
//! Routes call `multimodal_preflight` first, then their modality-
//! specific upstream code, then `record_multimodal_outcome`. Same
//! shape every time. The `record_multimodal_outcome` call doubles
//! as the audit row that admins see in the proxy-logs UI.

use axum::http::{HeaderMap, StatusCode};
use axum::response::Response;
use chrono::Utc;
use sqlx::types::Uuid;

use crate::models::auth::AuthContext;
use crate::models::db::{LlmModel, ResourceGroup, UsageLogInsert};
use crate::models::proxy::UsageEnforcementResult;
use crate::services::SharedState;
use crate::services::llm_preflight::{
    LlmAuditTarget, PreflightOptions, apply_guardrails_with_redaction, run_preflight,
};
use crate::services::llm_refusal::{self, Refusal, RefusalScope, RunAttribution};
use crate::services::llm_resilience::{self, DispatchGuard, GateRejection};
use crate::services::proxy_logger;

/// Outcome of `multimodal_preflight`. The route holds onto this
/// for the lifetime of the request and passes it to
/// `record_multimodal_outcome` after the upstream call returns.
pub struct MultimodalPreflight {
    pub auth_ctx: AuthContext,
    pub model: LlmModel,
    pub target_group: Option<ResourceGroup>,
    /// The per-request UUID stamped onto the proxy_logs row and the
    /// `correlation_id` propagated through telemetry. Generated
    /// fresh per call so each multimodal request is independently
    /// traceable.
    pub correlation_id: String,
    /// Carried through so `record_multimodal_outcome` can populate
    /// the `enforcement` warning headers (e.g. budget-at-80%) onto
    /// the response.
    pub enforcement: UsageEnforcementResult,
    /// If the input guardrail fired a Redact action, this carries
    /// the rewritten prompt. The route MUST use this in place of
    /// its original prompt when building the upstream body —
    /// otherwise the operator's `pii_action: redact` policy is
    /// a no-op on the multimodal surface (image gen / TTS / video).
    /// `None` means the original prompt is unchanged.
    pub redacted_prompt: Option<String>,
    /// Where this request sits in its run (the call chain), parsed or
    /// minted at entry. Every row the route writes — refusal or outcome —
    /// lands on it.
    pub run: RunAttribution,
    /// True when the input redaction above was driven by a PII-category
    /// check. The route threads this into
    /// `MultimodalOutcome.pii_detected` so the success audit row carries
    /// `pii_detected` (GDPR/HIPAA compliance tagging).
    pub input_pii_redacted: bool,
    /// What the per-mode media quota reserved for this call
    /// (`usage::enforce_media_quota`), set by the route once the call is
    /// admitted. `record_multimodal_outcome` raises the group's per-mode
    /// counters from it to what the call consumed. `None`: no per-mode
    /// limit applies to the call.
    pub media_reservation: Option<crate::services::usage::MediaReservation>,
    /// The model that served, when it is not `model` (a model-level
    /// fallback), set by the route. The outcome's
    /// records name it as the model used and `model` as the one requested.
    /// Admissions were reserved against `model` and are settled against it.
    pub served_model: Option<LlmModel>,
}

/// Run the canonical preflight pipeline for a multimodal route.
///
/// Steps performed (matches `chat_completions_handler`):
///   1. Authenticate (JWT or API key)
///   2. Resolve model by name
///   3. Verify mode matches `expected_mode` — wrong mode at this
///      endpoint is a 400 (e.g. POSTing a chat model to
///      `/audio/speech`).
///   4. Verify the calling user's resource group exposes this
///      model (group access check).
///   5. Enforce LLM usage quota (RPM, TPM, budget — the existing
///      `enforce_llm_quota` is mode-agnostic; budget+request caps
///      apply to multimodal as soon as the route writes its
///      cost-tagged proxy_logs row).
///
/// Returns `Ok(MultimodalPreflight)` on success; the caller
/// proceeds to the upstream call. Returns `Err(Response)` with the
/// appropriate HTTP error on auth/access/quota failure — that
/// response is already a fully-formed proxy log entry (the inner
/// quota path writes its own proxy_logs row before short-circuiting).
/// Runs the canonical pipeline for a multimodal route.
///
/// The `prompt` argument is the user-supplied text payload to
/// scan against input guardrails. Pass `None` when the user
/// input isn't textual at the request boundary (e.g. STT — the
/// audio itself is the input; output guardrails handle the
/// transcribed text instead).
pub async fn multimodal_preflight(
    state: &SharedState,
    headers: &HeaderMap,
    tenant_id: &str,
    model_name: &str,
    expected_mode: &str,
    prompt: Option<&str>,
    client_ip: Option<String>,
    error_fn: fn(StatusCode, &str) -> Response,
) -> Result<MultimodalPreflight, Response> {
    let correlation_id = Uuid::new_v4().to_string();

    // The call chain, as on every other LLM surface, so
    // this request's rows (refusals included) land on the run they belong to.
    let tenant_key = crate::services::a2a_delegation::derive_tenant_key(
        state.config.encryption_key.as_bytes(),
        tenant_id,
    );
    let (chain, current_task_id, continuity) = crate::services::a2a_delegation::parse_or_mint(
        |n| {
            headers
                .get(n)
                .and_then(|v| v.to_str().ok())
                .map(str::to_string)
        },
        &tenant_key,
    );
    let run = RunAttribution::chain(&chain, &current_task_id.to_string(), continuity);

    // Steps 1-2 + 4-5: auth, model resolve, access, quota.
    // Guardrails are off for multimodal — text-only prompts are
    // captured in the upstream payload (image prompt, TTS input)
    // and applied in a follow-on patch when the canonical pipeline
    // gets an output-scan helper too.
    let pre = run_preflight(
        state,
        headers,
        tenant_id,
        &correlation_id,
        Some(model_name),
        None,
        &PreflightOptions::quota_only(run.clone())
            .not_run_accounted()
            .no_output(),
        error_fn,
        client_ip,
        None,
        None,
    )
    .await?;

    // Step 3: mode check. We do this AFTER `run_preflight` so the
    // user's quota counter doesn't get charged for a "wrong mode"
    // request — quota is enforced at the entry, mode is enforced
    // before we forward.
    if pre.model.mode.as_deref() != Some(expected_mode) {
        return Err(error_fn(
            StatusCode::BAD_REQUEST,
            &format!(
                "model {:?} is mode={:?}, not {:?}",
                pre.model.model_name,
                pre.model.mode.as_deref().unwrap_or("unknown"),
                expected_mode
            ),
        ));
    }

    // The actor planes of the chat routes (`enforce_actor_planes`): the
    // per-agent runtime authorization of a key bound to an agent, the AI
    // System's autonomy gate (the kill switch) for every other caller, and
    // the semantic policy on the input text. A media route serves an API key,
    // so an agent can call it: without this a
    // default-deny agent with no grant, and a suspended system, would be
    // refused on the chat routes and served here.
    let route_endpoint = match expected_mode {
        "image_generation" => "/v1/proxy/llm/images/generations",
        "audio_speech" => "/v1/proxy/llm/audio/speech",
        "audio_transcription" => "/v1/proxy/llm/audio/transcriptions",
        "video_generation" => "/v1/proxy/llm/video/generations",
        _ => "/v1/proxy/llm",
    };
    crate::services::llm_preflight::enforce_actor_planes(
        state,
        &pre.auth_ctx,
        tenant_id,
        &pre.model,
        pre.target_group.as_ref(),
        Some(model_name.to_string()),
        prompt.unwrap_or_default(),
        route_endpoint,
        headers,
        &correlation_id,
        std::time::Instant::now(),
        &chain,
        &current_task_id.to_string(),
        continuity,
        proxy_logger::root_kind_for_auth(&pre.auth_ctx.auth_type, chain.depth == 0),
        error_fn,
    )
    .await?;

    // Step 4: input guardrails. Scans the user-supplied prompt
    // (image prompt, TTS text, video prompt) against tenant
    // guardrail configs. The same checks the chat handler
    // applies — keeps PII / prompt-injection policy uniform
    // across modalities. Skipped when the route has no textual
    // request payload (audio_transcribe — its "input" is bytes).
    // On Redact we surface the rewritten prompt via
    // `MultimodalPreflight.redacted_prompt`; the caller route MUST
    // use it in place of the original when building the upstream
    // body — otherwise the operator's `pii_action: redact` policy
    // is a no-op for image / TTS / video.
    let mut redacted_prompt: Option<String> = None;
    let mut input_pii_redacted = false;
    if let Some(text) = prompt {
        let trimmed = text.trim();
        if !trimmed.is_empty() {
            // Per-mode surface dispatch. Each multimodal route
            // maps to its own surface so operators can configure
            // distinct policies per mode (e.g. NSFW filter on
            // image_gen_input but not chat_input). Unknown modes fall
            // back to the legacy `"llm"` surface so any future
            // multimodal mode added without a per-mode entry here
            // still picks up the chat-style defaults.
            let surface = match expected_mode {
                "image_generation" => crate::constants::guardrail_surface::IMAGE_GEN_INPUT,
                "audio_speech" => crate::constants::guardrail_surface::AUDIO_TTS_INPUT,
                "audio_transcription" => crate::constants::guardrail_surface::AUDIO_STT_INPUT,
                "video_generation" => crate::constants::guardrail_surface::VIDEO_GEN_INPUT,
                _ => "llm",
            };
            // Per-mode upstream endpoint so a guardrail block records the
            // URL the request was aimed at (the adapter resolves the
            // provider-specific path from this).
            let upstream_endpoint = match expected_mode {
                "image_generation" => "/images/generations",
                "audio_speech" => "/audio/speech",
                "audio_transcription" => "/audio/transcriptions",
                "video_generation" => "/video/generations",
                _ => "/chat/completions",
            };
            let guard_target = LlmAuditTarget::from_model(
                &pre.model,
                Some(model_name.to_string()),
                upstream_endpoint,
            );
            if let Some(redaction) = apply_guardrails_with_redaction(
                state,
                tenant_id,
                &RefusalScope::new(
                    &pre.auth_ctx,
                    headers,
                    &correlation_id,
                    upstream_endpoint,
                    &run,
                )
                .model(&pre.model, Some(model_name.to_string())),
                trimmed,
                surface,
                None,
                Some(&guard_target),
            )
            .await?
            {
                redacted_prompt = Some(redaction.content);
                input_pii_redacted = redaction.pii;
            }
        }
    }

    Ok(MultimodalPreflight {
        auth_ctx: pre.auth_ctx,
        model: pre.model,
        target_group: pre.target_group,
        correlation_id,
        enforcement: pre.enforcement,
        redacted_prompt,
        input_pii_redacted,
        media_reservation: None,
        served_model: None,
        run,
    })
}

impl MultimodalPreflight {
    /// The refusal scope of this request, for [`llm_refusal::refuse`].
    pub fn refusal_scope<'a>(
        &'a self,
        headers: &'a HeaderMap,
        endpoint: &'a str,
    ) -> RefusalScope<'a> {
        RefusalScope {
            auth_ctx: &self.auth_ctx,
            headers,
            correlation_id: &self.correlation_id,
            endpoint,
            started: None,
            model: Some((&self.model, Some(self.model.model_name.clone()))),
            group_id: self.target_group.as_ref().map(|g| g.id.clone()),
            run: Some(&self.run),
            request_body: None,
        }
    }

    /// A per-mode media-governance policy refused the request (403).
    pub fn refuse_policy(
        &self,
        state: &SharedState,
        headers: &HeaderMap,
        endpoint: &str,
        message: &str,
        error_fn: fn(StatusCode, &str) -> Response,
    ) -> Response {
        llm_refusal::refuse(
            state,
            &self.refusal_scope(headers, endpoint),
            Refusal::new(
                crate::constants::proxy_subtype::LLM_GOVERNANCE_BLOCK,
                message,
            )
            .error_type("media_governance"),
            error_fn(StatusCode::FORBIDDEN, message),
        )
    }

    /// This request's effective media governance (group chain folded with
    /// the model's tenant default; see
    /// `usage::collect_effective_media_governance_for_model`). `Ok(None)`
    /// without a group; `Err` when a layer cannot be read (fail closed).
    pub async fn media_governance(
        &self,
        state: &SharedState,
        mode: &str,
    ) -> Result<Option<serde_json::Value>, sqlx::Error> {
        match self.target_group.as_ref() {
            Some(g) => {
                crate::services::usage::collect_effective_media_governance_for_model(
                    &state.db,
                    g,
                    mode,
                    &self.model.id,
                )
                .await
            }
            None => Ok(None),
        }
    }

    /// The media governance could not be read: refuse (503), never read it
    /// as "no policy".
    pub fn refuse_governance_unavailable(
        &self,
        state: &SharedState,
        headers: &HeaderMap,
        endpoint: &str,
        err: &sqlx::Error,
        error_fn: fn(StatusCode, &str) -> Response,
    ) -> Response {
        tracing::error!(error = %err, "media governance unreadable; refusing (fail closed)");
        let message = "Refused: this group's media governance cannot be read right now";
        llm_refusal::refuse(
            state,
            &self.refusal_scope(headers, endpoint),
            Refusal::new(
                crate::constants::proxy_subtype::LLM_GOVERNANCE_BLOCK,
                message,
            )
            .error_type("governance_unavailable"),
            error_fn(StatusCode::SERVICE_UNAVAILABLE, message),
        )
    }

    /// A per-mode media quota refused the request (429).
    pub fn refuse_quota(
        &self,
        state: &SharedState,
        headers: &HeaderMap,
        endpoint: &str,
        message: &str,
        error_fn: fn(StatusCode, &str) -> Response,
    ) -> Response {
        llm_refusal::refuse(
            state,
            &self.refusal_scope(headers, endpoint),
            Refusal::rate_limit(crate::services::llm_preflight::MEDIA_QUOTA_SCOPE, message),
            error_fn(StatusCode::TOO_MANY_REQUESTS, message),
        )
    }
}

/// Residency outcome carried into the media SUCCESS proxy_log so it
/// stamps the same audit fields as the LLM path. `Evaluated` = a tenant
/// residency profile ran (carries the `Allowed { region }` decision);
/// `NotEvaluated` = the tenant has no profile, so we still stamp the
/// `not_evaluated` class with the provider region (mirrors
/// `apply_residency_not_evaluated` on the chat path — lets the dashboard
/// distinguish "no policy" from "policy ran and allowed").
pub enum MediaResidencyStamp {
    Evaluated(crate::services::residency::ResidencyDecision),
    NotEvaluated(Option<String>),
}

/// What the upstream call ended up doing — the route synthesises
/// this from the provider response and hands it to
/// `record_multimodal_outcome`.
pub struct MultimodalOutcome<'a> {
    /// HTTP status returned to the client (200, 502, etc.).
    pub status_code: u16,
    /// Total request duration in milliseconds, measured from the
    /// route's `Instant::now()` at request start.
    pub duration_ms: f64,
    /// True for 2xx responses, false otherwise.
    pub success: bool,
    /// Upstream error message verbatim. Stored on the proxy_logs
    /// row so admins can debug from the Proxy Logs UI without
    /// rerunning. None on success.
    pub error: Option<&'a str>,
    /// Provider-side compute cost in USD. Caller computes this
    /// from the model's pricing fields:
    ///   * audio_speech: `cost_per_audio_char * chars`
    ///   * audio_transcription: `cost_per_audio_second * seconds`
    ///   * image: `cost_per_image_<size>` (size-specific column)
    ///   * video: `cost_per_video_second * seconds`
    ///     Pass 0.0 if pricing is unset — the row still records but
    ///     budget enforcement won't see anything to subtract.
    pub cost_usd: f64,
    /// Endpoint label used in the proxy_logs row's `endpoint`
    /// field. Should match the route's path
    /// (`/v1/proxy/llm/audio/speech` etc.).
    pub endpoint: &'a str,
    /// Optional request body summary (mode-specific). Currently
    /// stored on the usage_records row's `metric_metadata` so
    /// admins can see, for image, the prompt length and image
    /// count without re-querying provider logs.
    pub request_summary: Option<serde_json::Value>,
    /// Mode-specific billing-unit counters. Each multimodal route
    /// populates the one that matches its mode and leaves the
    /// others as `None`. SUM() over these columns at dashboard
    /// time gives a clean per-mode usage roll-up — see the LLM
    /// Media panel in mission-control + group usage. Splitting
    /// the units out (vs cramming everything into the token
    /// columns) keeps token-priced analytics honest and gives
    /// each modality its own dimension to chart against cost.
    pub media_units: MediaUnitCounters,
    /// True when an output guardrail redacted a PII category from the
    /// response (e.g. image-gen `revised_prompt`). Stamps
    /// `pii_detected` on the proxy_logs row so the compliance tagger
    /// derives has_pii/has_phi for GDPR/HIPAA. Defaults to false.
    pub pii_detected: bool,
    /// The non-Blocked residency decision from
    /// `enforce_media_residency` (the handler's `residency.as_ref()`).
    /// `Some(Allowed)` stamps `region_routed_to` + `residency_class`
    /// onto the SUCCESS proxy_log row so media rows carry the same
    /// residency audit fields as the chat path; `None` (no tenant
    /// profile) leaves them unstamped. The BLOCK path never reaches
    /// this struct (it short-circuits with a 403 + its own audit row).
    pub residency: Option<&'a MediaResidencyStamp>,
}

/// Per-mode billing-unit counters surfaced on the proxy_logs row.
/// Tokens stay on `llm_*_tokens`; these are the *non-token* units
/// that the four media modes actually bill in. The four fields
/// map 1:1 onto the `media_*` columns in `proxy_log` — fill in
/// the one that matches the request's mode and leave the rest
/// `None`.
#[derive(Default, Clone, Copy)]
pub struct MediaUnitCounters {
    /// `image_generation`: number of images returned.
    pub images_generated: Option<i32>,
    /// `audio_speech` (TTS): input character count.
    pub audio_chars: Option<i64>,
    /// `audio_transcription` (STT): duration in seconds of the
    /// audio that was transcribed.
    pub audio_seconds_input: Option<f64>,
    /// `video_generation`: duration in seconds of the output clip.
    pub video_seconds_output: Option<f64>,
}

/// Acquire a resilience-layer dispatch slot for a multimodal
/// model. Wraps `llm_resilience::gate_dispatch` and translates
/// `GateRejection` into the canonical 503/429 response so each
/// route can stay a one-liner:
///
/// ```ignore
/// let guard = match acquire_gate(&state, &pre).await {
///     Ok(g) => g,
///     Err(resp) => return resp,
/// };
/// ```
///
/// The returned guard MUST be passed to `record_multimodal_outcome`
/// so the breaker sees the success/failure outcome for this
/// request. Dropping the guard without a finalize logs a warning
/// (see `DispatchGuard::Drop`).
pub async fn acquire_gate(
    state: &SharedState,
    pre: &MultimodalPreflight,
) -> Result<DispatchGuard, Response> {
    match llm_resilience::gate_dispatch(
        state,
        &pre.model,
        &pre.auth_ctx.tenant_id,
        pre.target_group.as_ref(),
    )
    .await
    {
        Ok(guard) => Ok(guard),
        Err(rej) => {
            // The rejection itself is informative enough — log at
            // info level and return the standard error response.
            // The breaker's `record_failure` does NOT fire here:
            // we never started the upstream call, so we don't
            // count the gate refusal as another upstream failure
            // (that would amplify the cooldown).
            tracing::debug!(
                model = %pre.model.model_name,
                reason = %rej,
                "Multimodal dispatch gated out — returning resilience error",
            );
            Err(_record_gate_rejection(state, pre, &rej, headers_for_log()))
        }
    }
}

// Internal: write a proxy_logs row for a gate rejection so admins
// can see "this model is unhealthy / circuit open / saturated"
// blocks in the Proxy Logs UI without having to diff request
// volumes against `usage_records`. We don't write `usage_records`
// here because no upstream cost was incurred — the gate refused
// the dispatch.
fn _record_gate_rejection(
    state: &SharedState,
    pre: &MultimodalPreflight,
    rej: &GateRejection,
    headers: HeaderMap,
) -> Response {
    // A concurrency ceiling is a governed refusal (the operator's
    // `max_concurrent` on the model or resource group): the single refusal
    // path, `rate_limit_concurrency`. Health / breaker 503s are availability,
    // not governance, and keep their plain error row below.
    if matches!(
        rej,
        GateRejection::ConcurrencySaturated { .. } | GateRejection::RgConcurrencySaturated { .. }
    ) {
        let endpoint_label = match rej {
            GateRejection::RgConcurrencySaturated { .. } => "gate_rg_concurrency",
            _ => "gate_concurrency",
        };
        return llm_refusal::refuse(
            state,
            &pre.refusal_scope(&headers, endpoint_label),
            Refusal::rate_limit(
                crate::services::llm_preflight::CONCURRENCY_SCOPE,
                rej.to_string(),
            ),
            llm_resilience::gate_rejection_response(rej),
        );
    }
    let (status, endpoint_label) = match rej {
        GateRejection::Unhealthy { .. } => (503u16, "gate_unhealthy"),
        GateRejection::CircuitOpen { .. } => (503u16, "gate_circuit_open"),
        GateRejection::ConcurrencySaturated { .. } => (429u16, "gate_concurrency"),
        GateRejection::RgConcurrencySaturated { .. } => (429u16, "gate_rg_concurrency"),
    };
    let mut log = proxy_logger::build_proxy_log(
        "llm",
        &pre.auth_ctx,
        "POST",
        endpoint_label,
        pre.model.api_url.as_deref(),
        Some(&pre.model.id),
        status,
        0.0,
        false,
        Some(&rej.to_string()),
        &pre.correlation_id,
    );
    log.user_agent = proxy_logger::extract_user_agent(&headers);
    log.llm_model_requested = Some(pre.model.model_name.clone());
    log.llm_model_used = Some(pre.model.model_name.clone());
    log.llm_provider = Some(pre.model.provider.clone());
    log.resource_group_id = pre.target_group.as_ref().map(|g| g.id.clone());
    if let RunAttribution::Chain {
        chain,
        current_task_id,
        continuity,
    } = &pre.run
    {
        proxy_logger::apply_call_chain(
            &mut log,
            chain,
            current_task_id,
            *continuity,
            proxy_logger::root_kind_for_auth(&pre.auth_ctx.auth_type, chain.depth == 0),
        );
    }
    state
        .background_writer
        .write_proxy_log(state.db.clone(), log);

    llm_resilience::gate_rejection_response(rej)
}

// Returns an empty header map — used by the gate-rejection log
// path which doesn't have request headers in scope. Keeping the
// stub in one place so a future enhancement (capturing user-agent
// on rejected requests) only needs one edit.
fn headers_for_log() -> HeaderMap {
    HeaderMap::new()
}

/// Abandon a gate guard on an early-return path that ISN'T an
/// upstream failure (caller-side input-validation rejection: file
/// format whitelist, media-governance policy, unsupported provider,
/// etc). One-liner so route handlers don't have to think about the
/// `take().map(...)` dance:
///
/// ```ignore
/// if !ok {
///     abandon_gate(&mut gate_guard);
///     return error_resp(StatusCode::BAD_REQUEST, "...");
/// }
/// ```
///
/// Calling on `None` is a no-op — safe to use unconditionally on
/// any early-return path that's "after" the gate, even if the
/// guard was already moved out (e.g. by `record_multimodal_outcome`).
pub fn abandon_gate(gate_guard: &mut Option<DispatchGuard>) {
    if let Some(g) = gate_guard.take() {
        g.abandon();
    }
}

/// Write the proxy_logs row + the usage_records row for a
/// multimodal request. Both writes go through the background
/// writer so they don't add latency to the response path.
///
/// `gate_guard` is the `DispatchGuard` returned by `acquire_gate`,
/// or `None` for callers that didn't acquire a gate (legacy / test
/// paths). When present, it's finalized via `record_success` /
/// `record_failure` based on `outcome.success` so the circuit
/// breaker sees this request's outcome.
///
/// Why this is a single helper (vs two separate calls in each
/// route): keeping them paired prevents drift — a future change
/// to either schema (e.g. adding a `cost_currency` column) gets
/// applied uniformly to all four multimodal routes by editing
/// this one function.
pub fn record_multimodal_outcome(
    state: &SharedState,
    pre: &MultimodalPreflight,
    outcome: &MultimodalOutcome<'_>,
    headers: &HeaderMap,
    gate_guard: Option<DispatchGuard>,
) {
    // Health-probe traffic skips ALL metric writes here. Probe
    // accounting is centralized via gateway-control's call to
    // `POST /v1/proxy/llm/admin/{model_id}/record-probe-result`
    // (see services/catalog_probe::record_probe_outcome). Without
    // this short-circuit, probes would double-count: once from
    // this helper, once from the centralized record-probe path.
    //
    // The resilience-gate signal IS still fed below so the
    // circuit breaker correctly opens on repeated probe failures
    // — the breaker is about routing decisions, not metrics.
    let is_health_probe =
        crate::services::catalog_probe::is_health_probe_request(headers, &pre.auth_ctx);

    // Step 10 (resilience signal) — feed the breaker exactly
    // once. Consuming the guard here keeps the success/failure
    // call colocated with the audit row and prevents a
    // mid-handler unwind from leaving the breaker in a stale
    // state.
    if let Some(guard) = gate_guard {
        if outcome.success {
            guard.record_success();
        } else {
            guard.record_failure(
                outcome
                    .error
                    .map(|s| s.to_string())
                    .unwrap_or_else(|| "upstream failure".to_string()),
            );
        }
    }

    let user_agent = proxy_logger::extract_user_agent(headers);
    // The model that answered: the requested one, or the fallback that
    // served in its place.
    let used = pre.served_model.as_ref().unwrap_or(&pre.model);

    // proxy_logs row.
    let mut log = proxy_logger::build_proxy_log(
        "llm",
        &pre.auth_ctx,
        "POST",
        outcome.endpoint,
        used.api_url.as_deref(),
        Some(&used.id),
        outcome.status_code,
        outcome.duration_ms,
        outcome.success,
        outcome.error,
        &pre.correlation_id,
    );
    log.user_agent = user_agent.clone();
    log.llm_model_requested = Some(pre.model.model_name.clone());
    log.llm_model_used = Some(used.model_name.clone());
    log.llm_provider = Some(used.provider.clone());
    log.llm_estimated_cost_usd = Some(outcome.cost_usd);
    // Mode-specific billing-unit counters — each route fills in
    // the one that matches its mode (images / chars / seconds),
    // others stay None so dashboard SUM()s ignore them.
    log.media_images_generated = outcome.media_units.images_generated;
    log.media_audio_chars = outcome.media_units.audio_chars;
    log.media_audio_seconds_input = outcome.media_units.audio_seconds_input;
    log.media_video_seconds_output = outcome.media_units.video_seconds_output;
    log.resource_group_id = pre.target_group.as_ref().map(|g| g.id.clone());
    // Stamp residency audit fields onto the
    // media SUCCESS row (region_routed_to / residency_class) so media
    // rows match the chat path. `enforce_media_residency` returns only
    // the non-Blocked decision (Some(Allowed)); a None here means the
    // tenant has no residency profile, so nothing is stamped.
    match outcome.residency {
        Some(MediaResidencyStamp::Evaluated(dec)) => {
            crate::services::proxy_logger::apply_residency_audit(&mut log, dec);
        }
        Some(MediaResidencyStamp::NotEvaluated(region)) => {
            crate::services::proxy_logger::apply_residency_not_evaluated(
                &mut log,
                region.as_deref(),
            );
        }
        None => {}
    }
    // An output guardrail that redacted PII drives the compliance
    // tagger's has_pii/has_phi (GDPR/HIPAA) — same as the chat path's
    // success-row stamping.
    if outcome.pii_detected {
        proxy_logger::apply_pii_detected(&mut log);
    }
    // The outcome lands on the run it belongs to, like its refusals.
    if let RunAttribution::Chain {
        chain,
        current_task_id,
        continuity,
    } = &pre.run
    {
        proxy_logger::apply_call_chain(
            &mut log,
            chain,
            current_task_id,
            *continuity,
            proxy_logger::root_kind_for_auth(&pre.auth_ctx.auth_type, chain.depth == 0),
        );
    }
    state
        .background_writer
        .write_proxy_log(state.db.clone(), log);

    // usage_records row — same shape `enforce_llm_quota` reads
    // when summing daily/monthly cost. Crucially, `resource_type`
    // is `"llm"` so the existing budget+request caps see this
    // request just like a chat completion.
    // Same coalescing rule as `usage::resolve_usage_user_id`:
    // portal/JWT flows populate `gateway_user_id`, key-based flows
    // populate `api_key_user_id` — both point at `end_users.id`. The
    // per-member Usage modal would otherwise miss every media
    // request a portal user made.
    let usage_user_id = pre.auth_ctx.api_key_user_id.or_else(|| {
        pre.auth_ctx
            .gateway_user_id
            .as_deref()
            .and_then(|s| s.parse::<i32>().ok())
    });

    // Probe traffic skips this entire metric-write block. The
    // centralized record-probe-result endpoint handles all three
    // counters for probes; double-writing here would inflate
    // every counter for image/video/TTS/STT probes.
    if is_health_probe {
        let _summary = outcome.request_summary.clone();
        return;
    }

    let usage = UsageLogInsert {
        tenant_id: pre.auth_ctx.tenant_id.clone(),
        user_id: usage_user_id,
        group_id: pre.target_group.as_ref().map(|g| g.id.clone()),
        ancestry_path: pre
            .target_group
            .as_ref()
            .and_then(|g| g.hierarchy_path.clone()),
        group_name: pre.target_group.as_ref().map(|g| g.name.clone()),
        resource_type: "llm".to_string(),
        resource_id: Some(used.id.clone()),
        resource_name: Some(used.model_name.clone()),
        // Token fields are unused for multimodal; populating with
        // None is correct (vs 0, which would skew analytics).
        prompt_tokens: None,
        completion_tokens: None,
        total_tokens: None,
        model_name: Some(used.model_name.clone()),
        provider: Some(used.provider.clone()),
        mcp_tool_name: None,
        mcp_call_count: None,
        mcp_execution_time_ms: None,
        mcp_data_size_bytes: None,
        estimated_cost: Some(outcome.cost_usd),
        currency: Some("USD".to_string()),
        prompt_cost: None,
        completion_cost: None,
        request_id: pre.correlation_id.clone(),
        status_code: Some(outcome.status_code as i32),
        response_time_ms: Some(outcome.duration_ms as i32),
        http_method: Some("POST".to_string()),
        endpoint: Some(outcome.endpoint.to_string()),
        client_ip: pre.auth_ctx.client_ip.clone(),
        user_agent: user_agent.clone(),
        is_inherited: false,
        inherited_from_group_id: None,
        created_at: Utc::now(),
    };
    state
        .background_writer
        .write_usage_log(state.db.clone(), usage);
    // The group's per-mode media counters, raised from what the admission
    // reserved to what the call consumed.
    if let Some(reserved) = pre.media_reservation.as_ref() {
        let u = &outcome.media_units;
        let units = u
            .images_generated
            .map(i64::from)
            .or(u.audio_chars)
            .or(u.audio_seconds_input.map(|s| s.ceil() as i64))
            .or(u.video_seconds_output.map(|s| s.ceil() as i64))
            .unwrap_or(0);
        crate::services::usage::settle_media_call(
            &state.usage_state,
            reserved,
            units,
            outcome.cost_usd,
        );
    }
    // Settle the call's admission with what it cost, so the group's (and
    // the tenant's, the ancestors') spend counters carry media spend live —
    // not only once the reconcile pass raises them to the ledger.
    crate::services::usage::settle_llm_call(
        &state.usage_state,
        &crate::services::usage::LlmCallUsage {
            tenant_id: &pre.auth_ctx.tenant_id,
            hold_id: pre.auth_ctx.admission_hold.as_deref(),
            group_id: pre.target_group.as_ref().map(|g| g.id.as_str()),
            model_id: &pre.model.id,
            prompt_tokens: Some(0),
            completion_tokens: Some(0),
            estimated_cost: Some(outcome.cost_usd),
            status_code: outcome.status_code,
        },
    );

    // proxy_metrics row — Mission Control's `get_performance_metrics`
    // reads exclusively from `proxy_metrics`, so without this write
    // every multimodal request is invisible to the dashboard while
    // resource-group usage (which reads `usage_logs`) sees it. The
    // visible symptom: Mission Control under-counts requests vs
    // group usage, with the gap exactly equal to multimodal volume.
    //
    // `endpoint_category` is keyed on the model's `mode` so the
    // dashboard's per-mode breakdown matches the route taxonomy
    // (audio_transcription / audio_speech / image_generation /
    // video_generation). Falling back to "llm" preserves the chat
    // category for any future caller that wires this helper without
    // a mode set.
    let endpoint_category: &str = used.mode.as_deref().unwrap_or("llm");
    let metric_data = serde_json::json!({
        "provider": used.provider,
        "model_used": used.model_name,
        "estimated_cost_usd": outcome.cost_usd,
        "media_images_generated": outcome.media_units.images_generated,
        "media_audio_chars": outcome.media_units.audio_chars,
        "media_audio_seconds_input": outcome.media_units.audio_seconds_input,
        "media_video_seconds_output": outcome.media_units.video_seconds_output,
    });
    let metric = proxy_logger::build_proxy_metric(
        "llm",
        &pre.auth_ctx,
        "POST",
        outcome.endpoint,
        endpoint_category,
        Some(&used.id),
        outcome.status_code,
        outcome.duration_ms,
        outcome.success,
        &pre.correlation_id,
        // Multimodal request/response sizes aren't carried through
        // the outcome struct (the request is a multipart form, the
        // response a JSON envelope plus a media URL/blob ref). We
        // pass None rather than fabricating a zero — analytics SUM()
        // over these columns ignores NULLs but not zeros.
        None,
        None,
        None,
        None,
        user_agent.as_deref(),
        Some(metric_data),
        None,
    );
    state
        .background_writer
        .write_proxy_metric(state.db.clone(), metric);

    // Model-level counters routed through the in-process
    // aggregator. Multimodal modes don't surface token counts so we
    // pass 0; total_tokens_consumed is correctly left untouched.
    let is_failed = outcome.status_code >= 400;
    state.model_metrics.record(&used.id, is_failed, 0);
    let _summary = outcome.request_summary.clone();
}
