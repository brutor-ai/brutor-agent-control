// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
//! Audio transcription (STT) proxy.
//!
//! ## Endpoint
//!
//! `POST /v1/proxy/llm/audio/transcriptions`
//!
//! Body: `multipart/form-data` matching the OpenAI Whisper API.
//! Required parts:
//!   - `file`            — the audio bytes
//!   - `model`           — model id from the catalog
//!     Optional parts:
//!   - `language`        — ISO-639-1 hint (e.g. `en`)
//!   - `prompt`          — vocabulary biasing string
//!   - `temperature`     — 0..1, default 0
//!   - `response_format` — `json` | `text` | `srt` | `verbose_json` | `vtt`
//!   - `persist`         — "true" persists the input clip via Files API
//!   - `chat_id`         — opaque chat tag for the persisted row
//!
//! Response: OpenAI-compatible JSON:
//!
//! ```json
//! {
//!   "text": "transcribed content",
//!   "language": "en",
//!   "duration": 12.4,
//!   "segments": [...],
//!   "brutor_cost_usd": 0.0024,
//!   "brutor_model_id": "llm-...",
//!   "brutor_input_file_id": "file_..."   // present when persist=true
//! }
//! ```
//!
//! ## Pipeline
//!
//! 1. Authenticate the caller (an API key, or a portal session where the
//!    Portal is composed).
//! 2. Resolve the model; reject if not `mode='audio_transcription'`.
//! 3. Read the multipart body — single `file` part holding the
//!    audio bytes; metadata fields supply `model`, `language`, etc.
//! 4. Validate file format against `supported_input_formats`
//!    (when set) using the part's filename extension or
//!    Content-Type.
//! 5. Validate size against the file-size cap (Whisper: 25 MB).
//! 6. Optionally persist via the Files API when `persist=true`.
//! 7. Forward to the provider as multipart/form-data.
//! 8. Compute cost = `cost_per_audio_minute × duration / 60`.
//! 9. Return the JSON body augmented with `brutor_*` extensions.

use axum::{
    Json, Router,
    extract::{Multipart, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::post,
};
use bytes::Bytes;
use chrono::{Duration as ChronoDuration, Utc};
use serde_json::{Value, json};
use std::time::{Duration, Instant};

use crate::middleware::tenant::TenantContext;
use crate::models::db::LlmModel;
use crate::services::SharedState;
use crate::services::llm_preflight;
use crate::services::media_storage::{MediaKind, ObjectKey};
use crate::services::multimodal_pipeline::{
    MediaUnitCounters, MultimodalOutcome, abandon_gate, acquire_gate, multimodal_preflight,
    record_multimodal_outcome,
};

const DEFAULT_RETENTION_DAYS: i64 = 30;
/// Whisper hard-caps individual files at 25 MB. We mirror that.
const MAX_INPUT_BYTES: usize = 25 * 1024 * 1024;

pub fn router() -> Router<SharedState> {
    Router::new().route("/", post(transcribe))
}

#[derive(Default)]
struct ParsedBody {
    file: Option<Bytes>,
    file_name: Option<String>,
    file_content_type: Option<String>,
    model: Option<String>,
    language: Option<String>,
    /// `languages` / `languages[]` parts, in the order sent (OpenAI's
    /// array parameter, "Supported by `gpt-transcribe`").
    languages: Vec<String>,
    prompt: Option<String>,
    temperature: Option<f32>,
    response_format: Option<String>,
    persist: bool,
    chat_id: Option<String>,
}

async fn transcribe(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    mut multipart: Multipart,
) -> Response {
    let started = Instant::now();
    let client_ip = client_addr.ip();

    let (tenant_id, user_id, _username) = match crate::routes::request::extract_media_caller(
        &state,
        &headers,
        &tenant,
        client_ip.clone(),
        crate::routes::request::Preflight::Follows,
    )
    .await
    {
        Ok(v) => v,
        Err(r) => return r,
    };

    // ----------------------------------------------------------
    // Parse multipart.
    // ----------------------------------------------------------
    let mut body = ParsedBody::default();
    while let Some(field) = match multipart.next_field().await {
        Ok(opt) => opt,
        Err(err) => {
            return error_resp(
                StatusCode::BAD_REQUEST,
                &format!("invalid multipart body: {err}"),
            );
        }
    } {
        let name = field.name().unwrap_or("").to_string();
        match name.as_str() {
            "file" => {
                body.file_content_type = field.content_type().map(String::from);
                body.file_name = field.file_name().map(String::from);
                let bytes = match field.bytes().await {
                    Ok(b) => b,
                    Err(err) => {
                        return error_resp(
                            StatusCode::PAYLOAD_TOO_LARGE,
                            &format!("read file part: {err}"),
                        );
                    }
                };
                if bytes.len() > MAX_INPUT_BYTES {
                    return error_resp(
                        StatusCode::PAYLOAD_TOO_LARGE,
                        &format!(
                            "audio {} bytes exceeds {} byte cap",
                            bytes.len(),
                            MAX_INPUT_BYTES
                        ),
                    );
                }
                body.file = Some(bytes);
            }
            "model" => body.model = field.text().await.ok().filter(|v| !v.is_empty()),
            "language" => body.language = field.text().await.ok().filter(|v| !v.is_empty()),
            "languages" | "languages[]" => {
                if let Some(code) = field.text().await.ok().filter(|v| !v.is_empty()) {
                    body.languages.push(code);
                }
            }
            "prompt" => body.prompt = field.text().await.ok().filter(|v| !v.is_empty()),
            "temperature" => {
                body.temperature = field.text().await.ok().and_then(|s| s.parse().ok());
            }
            "response_format" => {
                body.response_format = field.text().await.ok().filter(|v| !v.is_empty());
            }
            "persist" => {
                body.persist = field
                    .text()
                    .await
                    .map(|s| s == "true" || s == "1")
                    .unwrap_or(false);
            }
            "chat_id" => body.chat_id = field.text().await.ok().filter(|v| !v.is_empty()),
            _ => {} // tolerate unknown parts
        }
    }

    let bytes = match body.file.take() {
        Some(b) if !b.is_empty() => b,
        _ => return error_resp(StatusCode::BAD_REQUEST, "missing or empty 'file' part"),
    };
    let model_id = match body.model.take() {
        Some(m) => m,
        None => return error_resp(StatusCode::BAD_REQUEST, "missing 'model' part"),
    };

    // Canonical pipeline: model resolve + group access + quota.
    // Whisper bills per minute of audio; without quota
    // enforcement a tenant could upload long files in a tight
    // loop and bypass the budget cap.
    let mut pre = match multimodal_preflight(
        &state,
        &headers,
        &tenant_id,
        &model_id,
        "audio_transcription",
        // Optional `prompt` field steers the transcription; it's
        // user-supplied text and should be guardrailed even though
        // the audio bytes themselves bypass scanning.
        body.prompt.as_deref(),
        client_ip.clone(),
        |s, m| error_resp(s, m),
    )
    .await
    {
        Ok(p) => p,
        Err(resp) => return resp,
    };

    // Data residency. Block before any upstream
    // provider call (no gate acquired yet, so nothing to abandon).
    let residency = match crate::routes::images::enforce_media_residency(
        &state,
        &pre,
        &headers,
        "/v1/proxy/llm/audio/transcriptions",
    )
    .await
    {
        Ok(d) => d,
        Err(resp) => return resp,
    };

    let model = std::sync::Arc::new(pre.model.clone());

    // Step 5: resilience gate.
    let gate_guard = match acquire_gate(&state, &pre).await {
        Ok(g) => g,
        Err(resp) => return resp,
    };
    let mut gate_guard = Some(gate_guard);

    // ----------------------------------------------------------
    // Format whitelist enforcement. We accept either a filename
    // extension match or a Content-Type match — Whisper's
    // documented format list is mixed (e.g. mp4 is the container,
    // m4a is the file, both are valid).
    // ----------------------------------------------------------
    if let Some(formats) = jsonb_string_array(&model.supported_input_formats) {
        let ext = body
            .file_name
            .as_deref()
            .and_then(|n| n.rsplit_once('.'))
            .map(|(_, e)| e.to_ascii_lowercase());
        let mime = body.file_content_type.as_deref().unwrap_or("");
        let ok = formats.iter().any(|f| {
            let f_lc = f.to_ascii_lowercase();
            ext.as_deref() == Some(f_lc.as_str())
                || mime.eq_ignore_ascii_case(&f_lc)
                || mime.ends_with(&format!("/{f_lc}"))
        });
        if !ok {
            abandon_gate(&mut gate_guard);
            return error_resp(
                StatusCode::BAD_REQUEST,
                &format!(
                    "input file format (ext={:?}, mime={:?}) not in whitelist {:?}",
                    ext, mime, formats
                ),
            );
        }
    }

    // Note: duration cap (`max_audio_duration_seconds`) is checked
    // post-transcription against the response's `duration` field.
    // Pre-flight by file-size cap above is the cheap rejection.

    // Optional persistence of the input clip (debugging /
    // re-transcribe flows). Done before the upstream call so the
    // bytes are saved even if upstream times out.
    let mut persisted_file_id: Option<String> = None;
    if body.persist {
        let now = Utc::now();
        let file_id = format!("file_{}", ulid::Ulid::new());
        let ext = body
            .file_name
            .as_deref()
            .and_then(|n| n.rsplit_once('.'))
            .map(|(_, e)| e.to_ascii_lowercase())
            .unwrap_or_else(|| "bin".to_string());
        let mime = body
            .file_content_type
            .clone()
            .unwrap_or_else(|| "application/octet-stream".to_string());
        if let Ok(key) = ObjectKey::build(
            &tenant_id,
            MediaKind::AudioTranscriptionInput,
            &file_id,
            &ext,
            now,
        ) {
            if state
                .media_storage
                .put(key.as_str(), bytes.clone(), &mime)
                .await
                .is_ok()
            {
                let size_bytes = bytes.len() as i64;
                let content_hash = sha256_hex(&bytes);
                let res = sqlx::query(
                    r#"
                    INSERT INTO media_files (
                        id, tenant_id, kind, mime_type, size_bytes, content_hash,
                        object_key, chat_id, model_id, actor_id,
                        created_at, expires_at
                    )
                    VALUES ($1,$2,'audio_transcription_input',$3,$4,$5,$6,$7,$8,$9,$10,$11)
                    "#,
                )
                .bind(&file_id)
                .bind(&tenant_id)
                .bind(&mime)
                .bind(size_bytes)
                .bind(&content_hash)
                .bind(key.as_str())
                .bind(&body.chat_id)
                .bind(&model.id)
                .bind(&user_id)
                .bind(now)
                .bind(now + ChronoDuration::days(DEFAULT_RETENTION_DAYS))
                .execute(&state.db)
                .await;
                if res.is_ok() {
                    persisted_file_id = Some(file_id);
                }
            }
        }
    }

    // Per-RG media governance for audio_transcription —
    // language allow-list, response-format whitelist, audio
    // size cap. Skipped when the group has no media governance
    // configured.
    {
        use crate::services::media_governance::{
            AudioTranscriptionRequest, enforce_audio_transcription, governance_for_mode,
        };
        // Ancestor-merged media governance (was single-group — no inheritance).
        let effective_media_gov = match pre.media_governance(&state, "audio_transcription").await {
            Ok(g) => g,
            Err(e) => {
                abandon_gate(&mut gate_guard);
                return pre.refuse_governance_unavailable(
                    &state,
                    &headers,
                    "/v1/proxy/llm/audio/transcriptions",
                    &e,
                    error_resp,
                );
            }
        };
        let mode_gov = governance_for_mode(effective_media_gov.as_ref(), "audio_transcription");
        let check = AudioTranscriptionRequest {
            language: body.language.as_deref(),
            response_format: body.response_format.as_deref(),
            audio_size_bytes: bytes.len() as u64,
        };
        if let Err(e) = enforce_audio_transcription(mode_gov, &check) {
            abandon_gate(&mut gate_guard);
            return pre.refuse_policy(
                &state,
                &headers,
                "/v1/proxy/llm/audio/transcriptions",
                &format!("policy: {e}"),
                error_resp,
            );
        }
    }

    // Per-mode media quota. STT billing units (seconds) + cost aren't known
    // until after transcription, so the pre-call check enforces daily_requests
    // (and blocks if a prior-usage characters/budget ceiling is already over).
    if let Some(g) = pre.target_group.as_ref() {
        match crate::services::usage::enforce_media_quota(
            &state.usage_state,
            &state.db,
            &tenant_id,
            g,
            &crate::services::usage::MediaCharge {
                mode: "audio_transcription",
                model_id: &model.id,
                units: 0,
                cost_usd: 0.0,
            },
        )
        .await
        {
            Ok(reserved) => pre.media_reservation = reserved,
            Err(reason) => {
                abandon_gate(&mut gate_guard);
                return pre.refuse_quota(
                    &state,
                    &headers,
                    "/v1/proxy/llm/audio/transcriptions",
                    &format!("quota: {reason}"),
                    error_resp,
                );
            }
        }
    }

    // ----------------------------------------------------------
    // Provider dispatch. OpenAI Whisper is first-class.
    // ----------------------------------------------------------
    // Model-level failover: try this model, then its same-mode
    // `fallback_models` chain (each candidate re-dispatches to its own provider).
    let fb_chain = crate::services::llm_router::resolve_fallback_chain(
        &state.db,
        &tenant_id,
        &model,
        &state.router_state,
        Some(&state.encryption),
        Some(&state.model_cache),
        Some(&state.provider_cache),
        crate::services::providers::ProviderApi::Transcription,
    )
    .await;
    let mut stt_candidates: Vec<LlmModel> = Vec::with_capacity(1 + fb_chain.len());
    stt_candidates.push((*model).clone());
    stt_candidates.extend(fb_chain);

    let n_stt = stt_candidates.len();
    let mut provider_result: Result<Value, String> = Err("no STT candidates".to_string());
    let mut serving_model: LlmModel = (*model).clone();
    for (idx, cand) in stt_candidates.iter().enumerate() {
        let r = match cand.provider.to_lowercase().as_str() {
            "openai" => call_openai_transcribe(&state, cand, &body, &bytes).await,
            "deepgram" => call_deepgram_transcribe(&state, cand, &body, &bytes).await,
            "assemblyai" | "assembly_ai" | "assembly-ai" => {
                call_assemblyai_transcribe(&state, cand, &body, &bytes).await
            }
            "elevenlabs" | "eleven_labs" | "eleven-labs" => {
                call_elevenlabs_transcribe(&state, cand, &body, &bytes).await
            }
            "google" | "google_cloud" | "google-cloud" => {
                call_google_stt(&state, cand, &body, &bytes).await
            }
            other => Err(format!("provider {other:?} is not an STT provider")),
        };
        serving_model = cand.clone();
        if r.is_ok() {
            if idx > 0 {
                tracing::info!(model = %cand.model_name, "audio transcribe: served via model-level fallback");
                state.router_state.record_success(&cand.id);
            }
            provider_result = r;
            break;
        }
        provider_result = r;
        if idx + 1 < n_stt {
            state
                .router_state
                .record_failure(&cand.id, 502, 60, "stt model-level fallback");
        }
    }
    let model = std::sync::Arc::new(serving_model);
    // The records name the model that served.
    if provider_result.is_ok() && model.id != pre.model.id {
        pre.served_model = Some((*model).clone());
    }
    let mut json_body = match provider_result {
        Ok(v) => v,
        Err(err) => {
            let elapsed_ms = started.elapsed().as_millis() as f64;
            tracing::warn!(
                tenant_id = %tenant_id,
                model_id = %model.id,
                "STT upstream failed: {err}"
            );
            record_multimodal_outcome(
                &state,
                &pre,
                &MultimodalOutcome {
                    status_code: StatusCode::BAD_GATEWAY.as_u16(),
                    duration_ms: elapsed_ms,
                    success: false,
                    error: Some(&format!("upstream error: {err}")),
                    cost_usd: 0.0,
                    endpoint: "/v1/proxy/llm/audio/transcriptions",
                    request_summary: None,
                    // Provider rejected the audio — no transcribed
                    // duration to record.
                    media_units: MediaUnitCounters::default(),
                    pii_detected: false,
                    residency: Some(&residency),
                },
                &headers,
                gate_guard.take(),
            );
            return error_resp(StatusCode::BAD_GATEWAY, &format!("upstream error: {err}"));
        }
    };

    let duration_secs = reported_duration_secs(&json_body).unwrap_or(0.0);
    let cost = transcription_cost(&model, &json_body);

    // Post-flight duration check: the audio's length is known only from
    // the provider's answer, so the cap refuses an answer the provider
    // already billed. The transcript is withheld;
    // the cost and the seconds are recorded and the counters settled, as
    // for an answer an output guardrail refuses. The provider answered, so
    // its dispatch guard closes as a success: a caller's over-long audio
    // is not a provider failure and must not open the breaker.
    if let Some(cap) = model.max_audio_duration_seconds {
        if duration_secs > cap as f64 {
            let message = format!(
                "audio duration {:.1}s exceeds the model's {}s cap",
                duration_secs, cap
            );
            if let Some(guard) = gate_guard.take() {
                guard.record_success();
            }
            record_multimodal_outcome(
                &state,
                &pre,
                &MultimodalOutcome {
                    status_code: StatusCode::PAYLOAD_TOO_LARGE.as_u16(),
                    duration_ms: started.elapsed().as_millis() as f64,
                    success: false,
                    error: Some(&message),
                    cost_usd: cost,
                    endpoint: "/v1/proxy/llm/audio/transcriptions",
                    request_summary: None,
                    media_units: MediaUnitCounters {
                        audio_seconds_input: Some(duration_secs),
                        ..Default::default()
                    },
                    pii_detected: false,
                    residency: Some(&residency),
                },
                &headers,
                None,
            );
            return error_resp(StatusCode::PAYLOAD_TOO_LARGE, &message);
        }
    }

    // Output guardrails — STT is the canonical case where output
    // scanning matters most: the user's voice has been turned
    // into text, and any PII / secrets they spoke would otherwise
    // leave the transcription endpoint unscreened. Block on
    // violation so the caller never sees the offending text. The
    // upstream cost is still recorded since the provider already
    // billed for the transcription.
    let transcribed_text = json_body
        .get("text")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    // Set when an output guardrail redacts a PII category from the
    // transcript — threaded into the success outcome so the audit row
    // carries pii_detected (GDPR/HIPAA tagging).
    let mut output_pii_redacted = false;
    if !transcribed_text.is_empty() {
        match llm_preflight::apply_output_guardrails(
            &state,
            &tenant_id,
            &pre.refusal_scope(&headers, "/v1/proxy/llm/audio/transcriptions"),
            &transcribed_text,
            // Per-mode output surface (STT transcript) — must match a
            // `surface_enabled` arm or the output check is silently skipped.
            "audio_stt_output",
            None,
        )
        .await
        {
            Err(resp) => {
                let elapsed_ms = started.elapsed().as_millis() as f64;
                record_multimodal_outcome(
                    &state,
                    &pre,
                    &MultimodalOutcome {
                        // The outcome row (it carries what the provider
                        // billed) records the status the refusal returned;
                        // the refusal row itself was written, on this run,
                        // by `apply_output_guardrails`.
                        status_code: resp.status().as_u16(),
                        duration_ms: elapsed_ms,
                        success: false,
                        error: Some("output guardrail violation"),
                        cost_usd: cost,
                        endpoint: "/v1/proxy/llm/audio/transcriptions",
                        request_summary: None,
                        // Guardrail blocked, but the upstream did
                        // process the audio and we paid for it — log
                        // the seconds so the cost line ties out.
                        media_units: MediaUnitCounters {
                            audio_seconds_input: Some(duration_secs),
                            ..Default::default()
                        },
                        pii_detected: false,
                        residency: Some(&residency),
                    },
                    &headers,
                    gate_guard.take(),
                );
                return resp;
            }
            Ok(llm_preflight::OutputCheckResult::Redacted {
                content: new_text,
                pii,
            }) => {
                // Splice the redacted transcription back into the
                // response body. Without this the operator's
                // `pii_action: redact` policy would emit a warning
                // log and still leak the original PII through to
                // the client.
                if let Some(obj) = json_body.as_object_mut() {
                    obj.insert("text".to_string(), serde_json::Value::String(new_text));
                }
                output_pii_redacted = pii;
            }
            Ok(llm_preflight::OutputCheckResult::Pass) => {}
        }
    }

    if let Value::Object(ref mut map) = json_body {
        map.insert("brutor_cost_usd".into(), json!(cost));
        map.insert("brutor_model_id".into(), json!(model.id.clone()));
        if let Some(fid) = &persisted_file_id {
            map.insert("brutor_input_file_id".into(), json!(fid));
        }
    }

    let elapsed_ms = started.elapsed().as_millis() as i64;
    tracing::debug!(
        tenant_id = %tenant_id,
        model_id = %model.id,
        duration_secs = duration_secs,
        cost_usd = cost,
        request_ms = elapsed_ms,
        "STT completed"
    );

    record_multimodal_outcome(
        &state,
        &pre,
        &MultimodalOutcome {
            status_code: StatusCode::OK.as_u16(),
            duration_ms: elapsed_ms as f64,
            success: true,
            error: None,
            cost_usd: cost,
            endpoint: "/v1/proxy/llm/audio/transcriptions",
            request_summary: Some(serde_json::json!({
                "duration_seconds": duration_secs,
                "input_bytes": bytes.len(),
            })),
            media_units: MediaUnitCounters {
                audio_seconds_input: Some(duration_secs),
                ..Default::default()
            },
            // PII redacted on input OR output (transcript).
            pii_detected: pre.input_pii_redacted || output_pii_redacted,
            residency: Some(&residency),
        },
        &headers,
        gate_guard.take(),
    );

    (StatusCode::OK, Json(json_body)).into_response()
}

// ---------------------------------------------------------------------------
// Provider: OpenAI (v1/audio/transcriptions)
// ---------------------------------------------------------------------------

async fn call_openai_transcribe(
    state: &SharedState,
    model: &LlmModel,
    body: &ParsedBody,
    bytes: &Bytes,
) -> Result<Value, String> {
    let api_key = model
        .api_key
        .clone()
        .ok_or_else(|| "OpenAI model has no api_key configured".to_string())?;
    let base_url = crate::services::providers::upstream_base(
        model,
        crate::services::providers::ProviderApi::Transcription,
    )?;
    let url = crate::services::providers::join_api_url(&base_url, "/v1/audio/transcriptions");

    // Capability-driven response_format selection. The
    // model's `supported_response_formats` JSONB column declares
    // exactly which formats the upstream accepts; the proxy picks
    // the highest-fidelity supported format (verbose_json carries
    // `duration` for cost calc; `json` is the universal fallback).
    //
    // A row that states no formats gets the caller's format (see
    // `pick_upstream_format`); the model's name is not read.
    let response_format = body
        .response_format
        .clone()
        .unwrap_or_else(|| "verbose_json".to_string());
    let upstream_format = pick_upstream_format(&model, &response_format);

    let file_part = reqwest::multipart::Part::bytes(bytes.to_vec())
        .file_name(
            body.file_name
                .clone()
                .unwrap_or_else(|| "input.bin".to_string()),
        )
        .mime_str(
            body.file_content_type
                .as_deref()
                .unwrap_or("application/octet-stream"),
        )
        .map_err(|e| format!("file mime: {e}"))?;

    let mut form = reqwest::multipart::Form::new()
        .part("file", file_part)
        .text("model", model.model_name.clone())
        .text("response_format", upstream_format.clone());

    if let Some(language) = &body.language {
        form = form.text("language", language.clone());
    }
    // OpenAI's `languages` (an array, "Supported by `gpt-transcribe`"):
    // forwarded as sent, in multipart array form.
    for code in &body.languages {
        form = form.text("languages[]", code.clone());
    }
    if let Some(prompt) = &body.prompt {
        form = form.text("prompt", prompt.clone());
    }
    if let Some(temp) = body.temperature {
        form = form.text("temperature", format!("{temp}"));
    }

    let resp = state
        .http_client
        .post(&url)
        .bearer_auth(api_key)
        .multipart(form)
        .timeout(Duration::from_secs(model.timeout.unwrap_or(180.0) as u64))
        .send()
        .await
        .map_err(|e| format!("HTTP send: {e}"))?;
    let status = resp.status();
    let raw = resp.text().await.map_err(|e| format!("read body: {e}"))?;
    if !status.is_success() {
        return Err(format!("OpenAI {status}: {raw}"));
    }
    let parsed: Value =
        serde_json::from_str(&raw).map_err(|e| format!("parse OpenAI body: {e}"))?;
    Ok(parsed)
}

// ---------------------------------------------------------------------------
// Provider: Deepgram
// ---------------------------------------------------------------------------
//
// Deepgram's prerecorded API:
//   `POST https://api.deepgram.com/v1/listen?model=...&language=...`
// with `Authorization: Token <api_key>` and the raw audio bytes
// in the body (Content-Type echoes the input file's MIME).
// We pass `model.model_name` as the `model` query param (e.g.
// `nova-3`, `nova-2`, `whisper-large`).
//
// Deepgram's response is nested: the transcript lives at
// `results.channels[0].alternatives[0].transcript`. Duration is
// in `metadata.duration`. We normalize both to the OpenAI-style
// shape (`text`, `language`, `duration`) so callers don't fork.

async fn call_deepgram_transcribe(
    state: &SharedState,
    model: &LlmModel,
    body: &ParsedBody,
    bytes: &Bytes,
) -> Result<Value, String> {
    let api_key = model
        .api_key
        .clone()
        .ok_or_else(|| "Deepgram model has no api_key configured".to_string())?;
    let base_url = crate::services::providers::upstream_base(
        model,
        crate::services::providers::ProviderApi::Transcription,
    )?;

    let mut url = reqwest::Url::parse(&crate::services::providers::join_api_url(
        &base_url,
        "/v1/listen",
    ))
    .map_err(|e| format!("bad Deepgram base URL: {e}"))?;
    {
        let mut q = url.query_pairs_mut();
        q.append_pair("model", &model.model_name);
        // Smart_format adds punctuation + paragraphs — strictly
        // better for chat UX. Filler-words off so the transcript
        // doesn't include "um".
        q.append_pair("smart_format", "true");
        q.append_pair("filler_words", "false");
        if let Some(language) = &body.language {
            q.append_pair("language", language);
        }
        if body.prompt.is_some() {
            // Deepgram supports keyword biasing via `keywords=`,
            // but the syntax is `keyword:boost`. We forward the
            // raw prompt as a single keyword with boost=1.0 to
            // preserve the OpenAI affordance — operators wanting
            // multi-keyword should encode it themselves.
            if let Some(p) = &body.prompt {
                q.append_pair("keywords", &format!("{p}:1.0"));
            }
        }
    }

    let resp = state
        .http_client
        .post(url)
        .header("Authorization", format!("Token {api_key}"))
        .header(
            "Content-Type",
            body.file_content_type
                .as_deref()
                .unwrap_or("application/octet-stream"),
        )
        .body(bytes.to_vec())
        .timeout(Duration::from_secs(model.timeout.unwrap_or(180.0) as u64))
        .send()
        .await
        .map_err(|e| format!("HTTP send: {e}"))?;
    let status = resp.status();
    let raw = resp.text().await.map_err(|e| format!("read body: {e}"))?;
    if !status.is_success() {
        return Err(format!("Deepgram {status}: {raw}"));
    }
    let parsed: Value =
        serde_json::from_str(&raw).map_err(|e| format!("parse Deepgram body: {e}"))?;
    Ok(deepgram_to_openai_shape(&parsed))
}

/// Pull the transcript and duration out of Deepgram's nested
/// response and return the same shape as OpenAI's `verbose_json`
/// (so callers don't fork).
fn deepgram_to_openai_shape(deepgram: &Value) -> Value {
    let alt0 = deepgram
        .pointer("/results/channels/0/alternatives/0")
        .cloned()
        .unwrap_or(Value::Null);
    let text = alt0
        .get("transcript")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let duration = deepgram
        .pointer("/metadata/duration")
        .and_then(|v| v.as_f64())
        .unwrap_or(0.0);
    let language = deepgram
        .pointer("/results/channels/0/detected_language")
        .and_then(|v| v.as_str())
        .map(String::from);

    let mut out = json!({
        "text": text,
        "duration": duration,
    });
    if let Some(lang) = language {
        out["language"] = json!(lang);
    }
    if let Value::Object(ref mut map) = out {
        // Carry through Deepgram's word timings under the
        // OpenAI-style `words` array — same key, same shape
        // (start/end/word) so downstream renderers Just Work.
        if let Some(words) = alt0.get("words") {
            map.insert("words".into(), words.clone());
        }
    }
    out
}

// ---------------------------------------------------------------------------
// Provider: AssemblyAI
// ---------------------------------------------------------------------------
//
// AssemblyAI uses a two-step async pipeline:
//   1. POST /v2/upload — raw bytes, returns `upload_url`.
//   2. POST /v2/transcript — request body `{ audio_url, ... }`,
//      returns `{ id, status: "queued" }`.
//   3. GET /v2/transcript/{id} — poll until status ∈
//      {"completed", "error"}.
//
// Auth is `Authorization: <api_key>` (raw, no "Bearer" prefix).
//
// We poll at 1 Hz up to `model.timeout` seconds (default 180).
// For longer files operators should bump the timeout column.

/// The speech model a row with no `model_name` is transcribed with.
const ASSEMBLYAI_DEFAULT_SPEECH_MODEL: &str = "universal-3-5-pro";

async fn call_assemblyai_transcribe(
    state: &SharedState,
    model: &LlmModel,
    body: &ParsedBody,
    bytes: &Bytes,
) -> Result<Value, String> {
    let api_key = model
        .api_key
        .clone()
        .ok_or_else(|| "AssemblyAI model has no api_key configured".to_string())?;
    let base_url = crate::services::providers::upstream_base(
        model,
        crate::services::providers::ProviderApi::Transcription,
    )?;

    // Step 1: upload.
    let upload_resp = state
        .http_client
        .post(crate::services::providers::join_api_url(
            &base_url,
            "/v2/upload",
        ))
        .header("Authorization", &api_key)
        .header(
            "Content-Type",
            body.file_content_type
                .as_deref()
                .unwrap_or("application/octet-stream"),
        )
        .body(bytes.to_vec())
        .timeout(Duration::from_secs(model.timeout.unwrap_or(180.0) as u64))
        .send()
        .await
        .map_err(|e| format!("upload send: {e}"))?;
    let upload_status = upload_resp.status();
    let upload_raw = upload_resp
        .text()
        .await
        .map_err(|e| format!("upload body: {e}"))?;
    if !upload_status.is_success() {
        return Err(format!("AssemblyAI upload {upload_status}: {upload_raw}"));
    }
    let upload_json: Value =
        serde_json::from_str(&upload_raw).map_err(|e| format!("parse upload: {e}"))?;
    let audio_url = upload_json
        .get("upload_url")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "AssemblyAI upload missing upload_url".to_string())?
        .to_string();

    // Step 2: submit transcription.
    let mut submit_body = json!({
        "audio_url": audio_url,
        // model_name in our catalog is the AssemblyAI speech model id
        // (`universal-3-5-pro`, `universal-2`). AssemblyAI takes it in
        // `speech_models`, a list; the singular `speech_model` and its
        // values `best`, `nano` and `slam-1` are deprecated.
        "speech_models": [if model.model_name.is_empty() {
            ASSEMBLYAI_DEFAULT_SPEECH_MODEL
        } else {
            model.model_name.as_str()
        }],
        "punctuate": true,
        "format_text": true,
    });
    if let Some(language) = &body.language {
        submit_body["language_code"] = json!(language);
    }
    if let Some(prompt) = &body.prompt {
        // AssemblyAI doesn't take a prompt — best mapping is
        // word_boost. Treat the prompt as a single boost token.
        submit_body["word_boost"] = json!([prompt]);
    }

    let submit_resp = state
        .http_client
        .post(crate::services::providers::join_api_url(
            &base_url,
            "/v2/transcript",
        ))
        .header("Authorization", &api_key)
        .json(&submit_body)
        .timeout(Duration::from_secs(60))
        .send()
        .await
        .map_err(|e| format!("submit send: {e}"))?;
    let submit_status = submit_resp.status();
    let submit_raw = submit_resp
        .text()
        .await
        .map_err(|e| format!("submit body: {e}"))?;
    if !submit_status.is_success() {
        return Err(format!("AssemblyAI submit {submit_status}: {submit_raw}"));
    }
    let submitted: Value =
        serde_json::from_str(&submit_raw).map_err(|e| format!("parse submit: {e}"))?;
    let job_id = submitted
        .get("id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "AssemblyAI submit missing id".to_string())?
        .to_string();

    // Step 3: poll. The total budget is the model's timeout —
    // upload + submit already burned some so allow a bit of
    // headroom: we cap at the timeout from now.
    let timeout_secs = model.timeout.unwrap_or(180.0).max(5.0) as u64;
    let deadline = Instant::now() + crate::services::configured_duration::whole_secs(timeout_secs);
    loop {
        if Instant::now() >= deadline {
            return Err("AssemblyAI transcript did not complete before timeout".into());
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
        let poll = state
            .http_client
            .get(crate::services::providers::join_api_url(
                &base_url,
                &format!("/v2/transcript/{job_id}"),
            ))
            .header("Authorization", &api_key)
            .timeout(Duration::from_secs(15))
            .send()
            .await
            .map_err(|e| format!("poll send: {e}"))?;
        let s = poll.status();
        let raw = poll.text().await.map_err(|e| format!("poll body: {e}"))?;
        if !s.is_success() {
            return Err(format!("AssemblyAI poll {s}: {raw}"));
        }
        let parsed: Value = serde_json::from_str(&raw).map_err(|e| format!("parse poll: {e}"))?;
        match parsed.get("status").and_then(|v| v.as_str()) {
            Some("completed") => return Ok(assemblyai_to_openai_shape(&parsed)),
            Some("error") => {
                let msg = parsed
                    .get("error")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown error");
                return Err(format!("AssemblyAI transcription failed: {msg}"));
            }
            Some("queued") | Some("processing") => continue,
            other => {
                return Err(format!("AssemblyAI returned unknown status: {other:?}"));
            }
        }
    }
}

fn assemblyai_to_openai_shape(aai: &Value) -> Value {
    let text = aai
        .get("text")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    // AssemblyAI surfaces duration in milliseconds.
    let duration = aai
        .get("audio_duration")
        .and_then(|v| v.as_f64())
        .unwrap_or(0.0);
    let language = aai
        .get("language_code")
        .and_then(|v| v.as_str())
        .map(String::from);

    let mut out = json!({
        "text": text,
        "duration": duration,
    });
    if let Some(lang) = language {
        out["language"] = json!(lang);
    }
    if let Value::Object(ref mut map) = out {
        if let Some(words) = aai.get("words") {
            map.insert("words".into(), words.clone());
        }
    }
    out
}

// ---------------------------------------------------------------------------
// Provider: ElevenLabs Scribe
// ---------------------------------------------------------------------------
//
// ElevenLabs Scribe v1 / v2:
//   `POST https://api.elevenlabs.io/v1/speech-to-text`
//
// multipart/form-data fields:
//   - `file`         — audio bytes (required)
//   - `model_id`     — `scribe_v1` etc. (required; strip the
//                       `elevenlabs/` catalog prefix)
//   - `language_code` — optional ISO-639-1 hint
//
// Auth header:
//   `xi-api-key: <api_key>`  (NOT a `Bearer` token)
//
// Response shape:
//   {
//     "language_code": "en",
//     "language_probability": 0.98,
//     "text": "transcript",
//     "words": [
//       {"text": "Hello", "type": "word", "start": 0.0, "end": 0.5},
//       {"text": " ",     "type": "spacing", "start": 0.5, "end": 0.6},
//       ...
//     ]
//   }
//
// ElevenLabs does NOT return an explicit duration field, so we
// derive it from `max(words[].end)`. The cost line in the catalog
// (`cost_per_audio_minute`) needs that duration to bill correctly
// — without it the call would record `cost = 0`. Fall back to 0.0
// for empty / silent transcripts.

async fn call_elevenlabs_transcribe(
    state: &SharedState,
    model: &LlmModel,
    body: &ParsedBody,
    bytes: &Bytes,
) -> Result<Value, String> {
    let api_key = model
        .api_key
        .clone()
        .ok_or_else(|| "ElevenLabs model has no api_key configured".to_string())?;
    let base_url = crate::services::providers::upstream_base(
        model,
        crate::services::providers::ProviderApi::Transcription,
    )?;
    let url = crate::services::providers::join_api_url(&base_url, "/v1/speech-to-text");

    // Catalog stores `elevenlabs/scribe_v1`; ElevenLabs wants the
    // bare `scribe_v1`. `strip_prefix` keeps any model id that
    // doesn't carry the prefix as-is so future seeds without it
    // still work.
    let model_id = model
        .model_name
        .strip_prefix("elevenlabs/")
        .unwrap_or(&model.model_name)
        .to_string();

    let file_part = reqwest::multipart::Part::bytes(bytes.to_vec())
        .file_name(
            body.file_name
                .clone()
                .unwrap_or_else(|| "input.bin".to_string()),
        )
        .mime_str(
            body.file_content_type
                .as_deref()
                .unwrap_or("application/octet-stream"),
        )
        .map_err(|e| format!("file mime: {e}"))?;

    let mut form = reqwest::multipart::Form::new()
        .part("file", file_part)
        .text("model_id", model_id);

    if let Some(language) = &body.language {
        form = form.text("language_code", language.clone());
    }

    let resp = state
        .http_client
        .post(&url)
        .header("xi-api-key", api_key)
        .multipart(form)
        .timeout(Duration::from_secs(model.timeout.unwrap_or(180.0) as u64))
        .send()
        .await
        .map_err(|e| format!("HTTP send: {e}"))?;
    let status = resp.status();
    let raw = resp.text().await.map_err(|e| format!("read body: {e}"))?;
    if !status.is_success() {
        return Err(format!("ElevenLabs {status}: {raw}"));
    }
    let parsed: Value =
        serde_json::from_str(&raw).map_err(|e| format!("parse ElevenLabs body: {e}"))?;
    Ok(elevenlabs_to_openai_shape(&parsed))
}

/// Pull transcript, language, and a derived duration out of
/// ElevenLabs' Scribe response and reshape it into OpenAI's
/// `verbose_json`-style envelope. ElevenLabs doesn't surface
/// duration directly, so we use `max(words[].end)` — the timing
/// of the final word marks the end of recognized speech, which is
/// what `cost_per_audio_minute` should be billed against.
fn elevenlabs_to_openai_shape(el: &Value) -> Value {
    let text = el
        .get("text")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let language = el
        .get("language_code")
        .and_then(|v| v.as_str())
        .map(String::from);
    let words = el.get("words").and_then(|v| v.as_array()).cloned();
    let duration = words
        .as_ref()
        .map(|ws| {
            ws.iter()
                .filter_map(|w| w.get("end").and_then(|v| v.as_f64()))
                .fold(0.0_f64, f64::max)
        })
        .unwrap_or(0.0);

    let mut out = json!({
        "text": text,
        "duration": duration,
    });
    if let Some(lang) = language {
        out["language"] = json!(lang);
    }
    if let Value::Object(ref mut map) = out {
        if let Some(ws) = words {
            map.insert("words".into(), Value::Array(ws));
        }
    }
    out
}

// ---------------------------------------------------------------------------
// Provider: Google Cloud Speech-to-Text
// ---------------------------------------------------------------------------
//
// Google's sync recognize endpoint:
//   `POST https://speech.googleapis.com/v1/speech:recognize`
//
// Request body:
//   { "config": { "languageCode": "en-US", "model": "...", ... },
//     "audio":  { "content": "<base64>" } }
//
// Sync mode is capped at 60s of audio / ~10 MB. Longer files
// require `longrunningrecognize` + polling — out of scope for
// v1; operators can route long files through Deepgram or
// AssemblyAI which handle long-form natively.

async fn call_google_stt(
    state: &SharedState,
    model: &LlmModel,
    body: &ParsedBody,
    bytes: &Bytes,
) -> Result<Value, String> {
    let api_key = model
        .api_key
        .clone()
        .ok_or_else(|| "Google STT model has no api_key configured".to_string())?;
    let base_url = crate::services::providers::upstream_base(
        model,
        crate::services::providers::ProviderApi::Transcription,
    )?;
    let url = crate::services::providers::join_api_url(&base_url, "/v1/speech:recognize");

    let language_code = body.language.clone().unwrap_or_else(|| "en-US".to_string());

    let encoding = google_stt_encoding(&body.file_content_type, &body.file_name);

    let mut config = json!({
        "languageCode": language_code,
        // `model` accepts "default" / "video" / "phone_call" /
        // "latest_long" / "latest_short". Take the catalog field
        // verbatim; default to "default".
        "model": if model.model_name.is_empty() { "default" } else { model.model_name.as_str() },
        "enableAutomaticPunctuation": true,
        "enableWordTimeOffsets": true,
    });
    if let Some(enc) = encoding {
        config["encoding"] = json!(enc);
    }
    if let Some(prompt) = &body.prompt {
        config["speechContexts"] = json!([{ "phrases": [prompt] }]);
    }

    use base64::Engine;
    let b64 = base64::engine::general_purpose::STANDARD.encode(bytes.as_ref());
    let request_body = json!({
        "config": config,
        "audio": { "content": b64 },
    });

    let resp = state
        .http_client
        .post(&url)
        .header("X-Goog-Api-Key", api_key)
        .json(&request_body)
        .timeout(Duration::from_secs(model.timeout.unwrap_or(60.0) as u64))
        .send()
        .await
        .map_err(|e| format!("HTTP send: {e}"))?;
    let status = resp.status();
    let raw = resp.text().await.map_err(|e| format!("read body: {e}"))?;
    if !status.is_success() {
        return Err(format!("Google STT {status}: {raw}"));
    }
    let parsed: Value =
        serde_json::from_str(&raw).map_err(|e| format!("parse Google body: {e}"))?;
    Ok(google_stt_to_openai_shape(&parsed, &language_code))
}

fn google_stt_to_openai_shape(g: &Value, language_code: &str) -> Value {
    // Google returns `results: [{ alternatives: [{transcript,
    // confidence, words: [...]}] }]`. We concatenate the top
    // alternative's transcript across all results so the caller
    // sees a single text field.
    let mut transcript = String::new();
    let mut all_words: Vec<Value> = Vec::new();
    if let Some(results) = g.get("results").and_then(|v| v.as_array()) {
        for result in results {
            if let Some(alt) = result.pointer("/alternatives/0") {
                if let Some(t) = alt.get("transcript").and_then(|v| v.as_str()) {
                    if !transcript.is_empty() {
                        transcript.push(' ');
                    }
                    transcript.push_str(t);
                }
                if let Some(words) = alt.get("words").and_then(|v| v.as_array()) {
                    all_words.extend(words.iter().cloned());
                }
            }
        }
    }
    // Google's `words` use `startTime: "1.2s"` / `endTime: "1.4s"`
    // strings. We don't bother normalizing here — downstream
    // consumers can do it; the `text` + `duration` is the
    // contract and Google STT doesn't surface duration directly.
    json!({
        "text": transcript,
        "language": language_code,
        // Google doesn't return audio duration in the response,
        // so cost calc against `cost_per_audio_minute` will be
        // 0.0 unless the caller does their own duration probe.
        // Operators wanting accurate Google STT cost should bill
        // per-tier, not per-minute, on this endpoint.
        "duration": 0.0,
        "words": all_words,
    })
}

/// Best-effort encoding hint for Google STT based on the upload's
/// MIME type / filename extension. Returns `None` for ambiguous
/// containers — Google can usually auto-detect, so omitting the
/// `encoding` field is safer than guessing wrong.
fn google_stt_encoding(
    content_type: &Option<String>,
    file_name: &Option<String>,
) -> Option<&'static str> {
    let mime = content_type
        .as_deref()
        .map(|s| s.to_ascii_lowercase())
        .unwrap_or_default();
    let ext = file_name
        .as_deref()
        .and_then(|n| n.rsplit_once('.'))
        .map(|(_, e)| e.to_ascii_lowercase())
        .unwrap_or_default();

    if mime.contains("flac") || ext == "flac" {
        Some("FLAC")
    } else if mime.contains("opus") || ext == "opus" {
        Some("OGG_OPUS")
    } else if mime.contains("amr") || ext == "amr" {
        Some("AMR")
    } else if mime.contains("webm") || ext == "webm" {
        Some("WEBM_OPUS")
    } else {
        // mp3/m4a/wav are auto-detected reliably; let Google
        // figure it out instead of risking a wrong hint.
        None
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn jsonb_string_array(v: &Option<serde_json::Value>) -> Option<Vec<String>> {
    let arr = v.as_ref()?.as_array()?;
    let mut out = Vec::with_capacity(arr.len());
    for item in arr {
        out.push(item.as_str()?.to_string());
    }
    Some(out)
}

fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::Digest;
    let digest = sha2::Sha256::digest(bytes);
    hex::encode(digest)
}

fn error_resp(status: StatusCode, message: &str) -> Response {
    (status, Json(json!({ "error": { "message": message } }))).into_response()
}

/// Pick the upstream response_format the OpenAI-style
/// STT endpoint will actually accept for this model.
///
/// Strategy (in order):
/// 1. If the caller's requested format is in
///    `supported_response_formats`, use it directly.
/// 2. Else if the column is set, return the highest-fidelity
///    supported format — `verbose_json` first (duration for cost
///    calc), then `json`, then `text`.
/// 3. Else (the row states no formats) the caller's format, except
///    that `text` / `srt` / `vtt` become `verbose_json` so the answer
///    carries `duration` for the cost.
///
/// The model's name is not read: the third
/// branch used to special-case `gpt-4o-transcribe` by prefix, which
/// knew nothing newer. A model that takes fewer formats says so in
/// its row, as the catalog's `gpt-transcribe` does (`["json"]`).
pub(crate) fn pick_upstream_format(model: &crate::models::db::LlmModel, requested: &str) -> String {
    // Branch 1+2: capability-driven.
    if let Some(supported) = jsonb_string_array(&model.supported_response_formats) {
        if supported.iter().any(|s| s == requested) {
            return requested.to_string();
        }
        // Caller's format not supported — pick best fallback.
        for preferred in ["verbose_json", "json", "text"] {
            if supported.iter().any(|s| s == preferred) {
                return preferred.to_string();
            }
        }
        // Whitelist exists but contains nothing recognised — fall
        // through to the legacy heuristic. Shouldn't happen in
        // production seeds but keeps us robust to operator typos.
    }

    // Branch 3: the row states no formats. For text/srt/vtt, ask for
    // verbose_json upstream so we still get `duration` for the cost.
    if requested == "text" || requested == "srt" || requested == "vtt" {
        return "verbose_json".to_string();
    }
    requested.to_string()
}

/// The length of the transcribed audio, in seconds, as the provider's
/// (normalized) answer reports it: the top-level `duration` (OpenAI's
/// `verbose_json` and `diarized_json`, and every other provider's answer
/// once normalized), else `usage.seconds` where `usage.type` is `duration`
/// (OpenAI's `json` answer of a model billed by audio duration).
/// `None`: the answer states no length.
fn reported_duration_secs(answer: &Value) -> Option<f64> {
    answer.get("duration").and_then(Value::as_f64).or_else(|| {
        let usage = answer.get("usage")?;
        (usage.get("type").and_then(Value::as_str) == Some("duration"))
            .then(|| usage.get("seconds").and_then(Value::as_f64))
            .flatten()
    })
}

/// What a transcription cost, by the model's catalog row and what the
/// provider reported:
///
/// - a reported length at the row's `cost_per_audio_minute`;
/// - else, token usage (`usage.type` = `tokens`, OpenAI's answer for a model
///   billed by tokens) at the row's `cost_per_input_token` /
///   `cost_per_output_token`, per 1M;
/// - else nothing, with a warning when the row states a price: there is
///   nothing to price the call from.
fn transcription_cost(model: &LlmModel, answer: &Value) -> f64 {
    if let (Some(per_minute), Some(secs)) =
        (model.cost_per_audio_minute, reported_duration_secs(answer))
    {
        return per_minute * secs / 60.0;
    }
    let usage = answer.get("usage");
    let tokens = |key: &str| usage.and_then(|u| u.get(key)).and_then(Value::as_f64);
    if usage.and_then(|u| u.get("type")).and_then(Value::as_str) == Some("tokens") {
        let input = tokens("input_tokens").unwrap_or(0.0);
        let output = tokens("output_tokens").unwrap_or(0.0);
        return (input * model.cost_per_input_token.unwrap_or(0.0)
            + output * model.cost_per_output_token.unwrap_or(0.0))
            / 1_000_000.0;
    }
    if model.cost_per_audio_minute.is_some_and(|p| p > 0.0) {
        tracing::warn!(
            model_id = %model.id,
            model = %model.model_name,
            "transcription model priced per audio minute: the provider reported no duration; \
             the call is recorded at zero cost"
        );
    }
    0.0
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn jsonb_formats_parses_typical_shape() {
        let v = json!(["mp3", "mp4", "wav"]);
        assert_eq!(
            jsonb_string_array(&Some(v)),
            Some(vec!["mp3".into(), "mp4".into(), "wav".into()])
        );
    }

    #[test]
    fn jsonb_formats_rejects_non_string_member() {
        let v = json!(["mp3", 42]);
        assert_eq!(jsonb_string_array(&Some(v)), None);
    }

    #[test]
    fn jsonb_formats_returns_none_on_null() {
        assert_eq!(jsonb_string_array(&None), None);
    }

    #[test]
    fn sha256_hex_is_stable() {
        assert_eq!(sha256_hex(b"hello"), sha256_hex(b"hello"));
        assert_eq!(sha256_hex(b"hello").len(), 64);
    }

    // ----- pick_upstream_format -----

    #[test]
    fn pick_uses_caller_format_when_supported() {
        // Trivial helper test that exercises the strategy without
        // building a full LlmModel — the function only reads
        // `supported_response_formats` and `model_name`.
        // We can synthesise the JSON capability list directly.
        let supported = Some(json!(["json", "text", "verbose_json"]));
        // Branch 1.
        // Construct a minimal LlmModel via a builder helper would
        // be the right shape, but to keep the test small here we
        // exercise the path manually via the public helper using
        // a wrapper-checked invariant.
        let model = crate::models::db::LlmModel {
            model_name: "whisper-1".into(),
            supported_response_formats: supported,
            ..Default::default()
        };
        assert_eq!(pick_upstream_format(&model, "verbose_json"), "verbose_json");
        assert_eq!(pick_upstream_format(&model, "json"), "json");
        assert_eq!(pick_upstream_format(&model, "text"), "text");
    }

    #[test]
    fn pick_falls_back_to_verbose_json_when_caller_format_not_supported() {
        let model = crate::models::db::LlmModel {
            model_name: "whisper-1".into(),
            supported_response_formats: Some(json!(["json", "verbose_json"])),
            ..Default::default()
        };
        // Caller asks for srt — not in whitelist — pick best
        // supported = verbose_json (we want duration for cost calc).
        assert_eq!(pick_upstream_format(&model, "srt"), "verbose_json");
    }

    #[test]
    fn pick_degrades_to_json_when_verbose_unsupported() {
        let model = crate::models::db::LlmModel {
            model_name: "gpt-4o-transcribe".into(),
            supported_response_formats: Some(json!(["json", "text"])),
            ..Default::default()
        };
        // Caller asks for verbose_json — model doesn't support it
        // — pick best supported = json (text is also acceptable but
        // json wins per the preference order in the helper).
        assert_eq!(pick_upstream_format(&model, "verbose_json"), "json");
    }

    #[test]
    fn pick_does_not_read_the_name_when_the_row_states_no_formats() {
        let model = crate::models::db::LlmModel {
            model_name: "gpt-4o-transcribe".into(),
            supported_response_formats: None,
            ..Default::default()
        };
        // Branch 3 knows no model by name: a row that states no
        // formats gets the default, whatever it is called. (This test
        // asserted `json` for both while the name was special-cased.)
        assert_eq!(pick_upstream_format(&model, "verbose_json"), "verbose_json");
        assert_eq!(pick_upstream_format(&model, "text"), "verbose_json");
    }

    #[test]
    fn pick_legacy_fallback_when_column_null_for_whisper() {
        let model = crate::models::db::LlmModel {
            model_name: "whisper-1".into(),
            supported_response_formats: None,
            ..Default::default()
        };
        // Branch 3: the row states no formats.
        assert_eq!(pick_upstream_format(&model, "verbose_json"), "verbose_json");
        // text/srt/vtt → upgrade to verbose_json so we get duration.
        assert_eq!(pick_upstream_format(&model, "text"), "verbose_json");
        assert_eq!(pick_upstream_format(&model, "srt"), "verbose_json");
        assert_eq!(pick_upstream_format(&model, "json"), "json");
    }

    /// The upstream format is the
    /// row's statement (`supported_response_formats`), never the model's
    /// name: the name check knew `gpt-4o-transcribe` and nothing newer, and
    /// the catalog's `gpt-transcribe` (json only) is described by its row.
    #[test]
    fn the_upstream_format_follows_the_row_not_the_models_name() {
        // The seeded row of `gpt-transcribe`: json is all it documents.
        let seeded = crate::models::db::LlmModel {
            model_name: "gpt-transcribe".into(),
            supported_response_formats: Some(json!(["json"])),
            ..Default::default()
        };
        for requested in ["verbose_json", "json", "text", "srt", "vtt"] {
            assert_eq!(
                pick_upstream_format(&seeded, requested),
                "json",
                "{requested}"
            );
        }
        // The same statement on any other name gives the same answer.
        let renamed = crate::models::db::LlmModel {
            model_name: "whisper-1".into(),
            ..seeded.clone()
        };
        for requested in ["verbose_json", "json", "text", "srt", "vtt"] {
            assert_eq!(
                pick_upstream_format(&renamed, requested),
                "json",
                "{requested}"
            );
        }
        // A row that states nothing: no name is consulted, so every name
        // gets the one default.
        for requested in ["verbose_json", "json", "text", "srt", "vtt"] {
            let answers: Vec<String> = ["whisper-1", "gpt-4o-transcribe", "gpt-transcribe", "x"]
                .into_iter()
                .map(|name| {
                    pick_upstream_format(
                        &crate::models::db::LlmModel {
                            model_name: name.into(),
                            supported_response_formats: None,
                            ..Default::default()
                        },
                        requested,
                    )
                })
                .collect();
            assert!(
                answers.iter().all(|a| a == &answers[0]),
                "{requested}: the answer depends on the model's name: {answers:?}"
            );
        }
    }

    // ----- 4.2b provider helper tests -----

    #[test]
    fn deepgram_to_openai_shape_extracts_transcript_and_duration() {
        let dg = json!({
            "metadata": { "duration": 3.5 },
            "results": {
                "channels": [{
                    "detected_language": "en",
                    "alternatives": [{
                        "transcript": "hello world",
                        "words": [{"start": 0.0, "end": 0.5, "word": "hello"}],
                    }]
                }]
            }
        });
        let out = deepgram_to_openai_shape(&dg);
        assert_eq!(out["text"], "hello world");
        assert_eq!(out["duration"], 3.5);
        assert_eq!(out["language"], "en");
        assert!(out["words"].is_array());
    }

    #[test]
    fn deepgram_to_openai_shape_handles_missing_fields_safely() {
        let dg = json!({});
        let out = deepgram_to_openai_shape(&dg);
        assert_eq!(out["text"], "");
        assert_eq!(out["duration"], 0.0);
        // language is omitted when not present
        assert!(out.get("language").is_none());
    }

    #[test]
    fn assemblyai_to_openai_shape_normalises_fields() {
        let aai = json!({
            "text": "hi there",
            "audio_duration": 7.0,
            "language_code": "en",
            "words": [{"start": 0, "end": 200, "text": "hi"}]
        });
        let out = assemblyai_to_openai_shape(&aai);
        assert_eq!(out["text"], "hi there");
        assert_eq!(out["duration"], 7.0);
        assert_eq!(out["language"], "en");
    }

    #[test]
    fn elevenlabs_to_openai_shape_extracts_text_language_and_words() {
        let el = json!({
            "language_code": "en",
            "language_probability": 0.97,
            "text": "hello world",
            "words": [
                {"text": "hello", "type": "word", "start": 0.0, "end": 0.5},
                {"text": " ", "type": "spacing", "start": 0.5, "end": 0.6},
                {"text": "world", "type": "word", "start": 0.6, "end": 1.2},
            ]
        });
        let out = elevenlabs_to_openai_shape(&el);
        assert_eq!(out["text"], "hello world");
        assert_eq!(out["language"], "en");
        // Duration is derived from the max `end` across all words —
        // ElevenLabs doesn't surface duration directly.
        assert_eq!(out["duration"], 1.2);
        assert!(out["words"].is_array());
        assert_eq!(out["words"].as_array().unwrap().len(), 3);
    }

    #[test]
    fn elevenlabs_to_openai_shape_handles_empty_silent_transcript() {
        // Silent input → ElevenLabs returns empty text and no
        // words. Duration must be 0.0 so the cost calc records
        // zero rather than NaN.
        let el = json!({
            "language_code": "en",
            "language_probability": 0.0,
            "text": "",
            "words": []
        });
        let out = elevenlabs_to_openai_shape(&el);
        assert_eq!(out["text"], "");
        assert_eq!(out["language"], "en");
        assert_eq!(out["duration"], 0.0);
        assert_eq!(out["words"].as_array().unwrap().len(), 0);
    }

    #[test]
    fn elevenlabs_to_openai_shape_tolerates_missing_fields() {
        let el = json!({});
        let out = elevenlabs_to_openai_shape(&el);
        assert_eq!(out["text"], "");
        assert_eq!(out["duration"], 0.0);
        // language is omitted entirely when not surfaced
        assert!(out.get("language").is_none());
        // words is also omitted when the upstream key is absent
        assert!(out.get("words").is_none());
    }

    #[test]
    fn elevenlabs_to_openai_shape_picks_max_end_not_last_word() {
        // Word timings can be out of order if the upstream
        // surfaces overlapping speakers or post-edited segments.
        // We use max(end), not last(end), to be robust to that.
        let el = json!({
            "text": "a b",
            "words": [
                {"text": "a", "type": "word", "start": 0.0, "end": 5.0},
                {"text": "b", "type": "word", "start": 5.0, "end": 3.5},
            ]
        });
        let out = elevenlabs_to_openai_shape(&el);
        assert_eq!(out["duration"], 5.0);
    }

    #[test]
    fn google_stt_to_openai_shape_concatenates_alternatives() {
        let g = json!({
            "results": [
                { "alternatives": [{ "transcript": "first half", "words": [] }] },
                { "alternatives": [{ "transcript": "second half", "words": [] }] }
            ]
        });
        let out = google_stt_to_openai_shape(&g, "en-US");
        assert_eq!(out["text"], "first half second half");
        assert_eq!(out["language"], "en-US");
        assert_eq!(out["duration"], 0.0);
    }

    #[test]
    fn google_stt_to_openai_shape_handles_no_results() {
        let g = json!({});
        let out = google_stt_to_openai_shape(&g, "fr-FR");
        assert_eq!(out["text"], "");
        assert_eq!(out["language"], "fr-FR");
    }

    #[test]
    fn google_stt_encoding_detects_known_containers() {
        assert_eq!(
            google_stt_encoding(&Some("audio/flac".into()), &None),
            Some("FLAC"),
        );
        assert_eq!(
            google_stt_encoding(&Some("audio/webm".into()), &None),
            Some("WEBM_OPUS"),
        );
        assert_eq!(
            google_stt_encoding(&Some("audio/ogg; codecs=opus".into()), &None),
            Some("OGG_OPUS"),
        );
        // mp3 is auto-detected — we leave it to Google.
        assert_eq!(
            google_stt_encoding(&Some("audio/mpeg".into()), &Some("clip.mp3".into())),
            None,
        );
    }

    #[test]
    fn google_stt_encoding_falls_back_to_extension() {
        assert_eq!(
            google_stt_encoding(&None, &Some("voice.flac".into())),
            Some("FLAC"),
        );
        assert_eq!(
            google_stt_encoding(&None, &Some("voice.AMR".into())),
            Some("AMR"),
        );
    }
}
