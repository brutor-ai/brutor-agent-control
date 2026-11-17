// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
//! Audio TTS (text-to-speech) proxy.
//!
//! ## Endpoint
//!
//! `POST /v1/proxy/llm/audio/speech`
//!
//! OpenAI-compatible request body:
//!
//! ```json
//! {
//!   "model": "tts-1",
//!   "voice": "alloy",
//!   "input": "Hello world",
//!   "response_format": "mp3",
//!   "speed": 1.0,
//!   "persist": false
//! }
//! ```
//!
//! Response body is **the audio bytes** with `Content-Type` set
//! per `response_format`. We surface Brutor metadata via response
//! headers — the body is a streamable binary, not JSON:
//!
//! - `X-Brutor-Cost-USD`: per-request cost roll-up (computed from
//!   the model's `cost_per_audio_char` × character count).
//! - `X-Brutor-Model-ID`: which model fulfilled the request.
//! - `X-Brutor-File-Id`: present only when `persist=true`.
//! - `X-Brutor-Mime-Type`: echoed media type for clients.
//!
//! ## Pipeline
//!
//! 1. Authenticate the caller (an API key, or a portal session where the
//!    Portal is composed).
//! 2. Resolve the model from the cache; reject if not
//!    `mode='audio_speech'`.
//! 3. Validate `voice` against `supported_voices` (if set) and
//!    `response_format` against `supported_audio_formats`.
//! 4. Apply input-size cap so a 10 MB blob can't get queued for
//!    upstream. Hard ceiling at 50 KB of input text — every
//!    practical TTS use case fits.
//! 5. Dispatch to the provider adapter. OpenAI is first-class;
//!    other providers are a 501 with a clear hint.
//! 6. Optionally persist via the Files API (`persist=true`).
//! 7. Return the audio bytes.

use axum::{
    Json, Router,
    extract::State,
    http::{HeaderMap, HeaderValue, StatusCode, header},
    response::{IntoResponse, Response},
    routing::post,
};
use bytes::Bytes;
use chrono::{Duration as ChronoDuration, Utc};
use serde::Deserialize;
use serde_json::json;
use std::time::{Duration, Instant};

use crate::middleware::tenant::TenantContext;
use crate::models::db::LlmModel;
use crate::services::SharedState;
use crate::services::media_storage::{MediaKind, ObjectKey};
use crate::services::multimodal_pipeline::{
    MediaUnitCounters, MultimodalOutcome, abandon_gate, acquire_gate, multimodal_preflight,
    record_multimodal_outcome,
};

const DEFAULT_RETENTION_DAYS: i64 = 30;
const MAX_INPUT_CHARS: usize = 50_000;

pub fn router() -> Router<SharedState> {
    Router::new().route("/", post(speech))
}

#[derive(Debug, Deserialize)]
struct SpeechRequest {
    model: String,
    voice: String,
    input: String,
    #[serde(default = "default_format")]
    response_format: String,
    #[serde(default = "default_speed")]
    speed: f32,
    /// When true, persist the audio via the Files API and
    /// surface the `brutor_file_id` in `X-Brutor-File-Id`.
    /// Default false — most callers want bytes-only, especially
    /// for "Speak this message" style flows where the audio is
    /// played once and discarded.
    #[serde(default)]
    persist: bool,
    /// Optional opaque chat tag for the persisted row.
    #[serde(default)]
    chat_id: Option<String>,
}

fn default_format() -> String {
    "mp3".into()
}
fn default_speed() -> f32 {
    1.0
}

async fn speech(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Json(mut req): Json<SpeechRequest>,
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

    // Canonical pipeline: auth + model resolve + group access +
    // quota enforcement. Without this the route bypasses the
    // tenant's RPM/TPM/budget caps — `enforce_llm_quota` runs
    // inside `run_preflight`, so multimodal traffic now sees the
    // same caps as chat completions.
    let mut pre = match multimodal_preflight(
        &state,
        &headers,
        &tenant_id,
        &req.model,
        "audio_speech",
        Some(req.input.as_str()),
        client_ip.clone(),
        |s, m| error_resp(s, m),
    )
    .await
    {
        Ok(p) => p,
        Err(resp) => return resp,
    };
    // Honour the input guardrail's Redacted outcome — overwrite
    // req.input so the TTS upstream synthesises the redacted text
    // rather than the original (which may carry PII).
    if let Some(redacted) = &pre.redacted_prompt {
        req.input = redacted.clone();
    }

    // Data residency. Block before any upstream
    // provider call (no gate acquired yet, so nothing to abandon).
    let residency = match crate::routes::images::enforce_media_residency(
        &state,
        &pre,
        &headers,
        "/v1/proxy/llm/audio/speech",
    )
    .await
    {
        Ok(d) => d,
        Err(resp) => return resp,
    };

    let model = std::sync::Arc::new(pre.model.clone());

    // Step 5: resilience gate. Refuses the dispatch when the
    // model is health-flagged unhealthy, the circuit breaker is
    // open, or the per-model concurrency cap is saturated. Same
    // gate the chat handler runs.
    let gate_guard = match acquire_gate(&state, &pre).await {
        Ok(g) => g,
        Err(resp) => return resp,
    };
    let mut gate_guard = Some(gate_guard);

    if req.input.is_empty() {
        abandon_gate(&mut gate_guard);
        return error_resp(StatusCode::BAD_REQUEST, "input is empty");
    }
    if req.input.len() > MAX_INPUT_CHARS {
        abandon_gate(&mut gate_guard);
        return error_resp(
            StatusCode::PAYLOAD_TOO_LARGE,
            &format!(
                "input {} chars exceeds {} char cap",
                req.input.len(),
                MAX_INPUT_CHARS
            ),
        );
    }
    if let Some(voices) = jsonb_string_array(&model.supported_voices) {
        if !voices.iter().any(|v| v == &req.voice) {
            abandon_gate(&mut gate_guard);
            return error_resp(
                StatusCode::BAD_REQUEST,
                &format!(
                    "voice {:?} not in this model's whitelist {:?}",
                    req.voice, voices
                ),
            );
        }
    }
    if let Some(formats) = jsonb_string_array(&model.supported_audio_formats) {
        if !formats.iter().any(|f| f == &req.response_format) {
            abandon_gate(&mut gate_guard);
            return error_resp(
                StatusCode::BAD_REQUEST,
                &format!(
                    "response_format {:?} not in this model's whitelist {:?}",
                    req.response_format, formats
                ),
            );
        }
    }
    if !(0.25..=4.0).contains(&req.speed) {
        abandon_gate(&mut gate_guard);
        return error_resp(
            StatusCode::BAD_REQUEST,
            &format!("speed {} outside [0.25, 4.0]", req.speed),
        );
    }

    // Per-RG media governance — voice/format whitelists, input
    // cap, speed bounds. Layered on top of the model whitelist
    // (above) so policy can narrow but not widen the model's
    // own constraints. Skipped when the group has no media
    // governance configured.
    {
        use crate::services::media_governance::{
            AudioSpeechRequest, enforce_audio_speech, governance_for_mode,
        };
        // Ancestor-merged media governance (was single-group — no inheritance).
        let effective_media_gov = match pre.media_governance(&state, "audio_speech").await {
            Ok(g) => g,
            Err(e) => {
                abandon_gate(&mut gate_guard);
                return pre.refuse_governance_unavailable(
                    &state,
                    &headers,
                    "/v1/proxy/llm/audio/speech",
                    &e,
                    error_resp,
                );
            }
        };
        let mode_gov = governance_for_mode(effective_media_gov.as_ref(), "audio_speech");
        let check = AudioSpeechRequest {
            voice: &req.voice,
            response_format: &req.response_format,
            input_chars: req.input.chars().count(),
            speed: req.speed,
        };
        if let Err(e) = enforce_audio_speech(mode_gov, &check) {
            abandon_gate(&mut gate_guard);
            return pre.refuse_policy(
                &state,
                &headers,
                "/v1/proxy/llm/audio/speech",
                &format!("policy: {e}"),
                error_resp,
            );
        }
    }

    // Per-mode media quota (daily requests / characters / budget, inherited).
    if let Some(g) = pre.target_group.as_ref() {
        let input_chars = req.input.chars().count() as i64;
        let est_cost = model.cost_per_audio_char.unwrap_or(0.0) * input_chars as f64;
        match crate::services::usage::enforce_media_quota(
            &state.usage_state,
            &state.db,
            &tenant_id,
            g,
            &crate::services::usage::MediaCharge {
                mode: "audio_speech",
                model_id: &model.id,
                units: input_chars,
                cost_usd: est_cost,
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
                    "/v1/proxy/llm/audio/speech",
                    &format!("quota: {reason}"),
                    error_resp,
                );
            }
        }
    }

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
        crate::services::providers::ProviderApi::Speech,
    )
    .await;
    let mut tts_candidates: Vec<LlmModel> = Vec::with_capacity(1 + fb_chain.len());
    tts_candidates.push((*model).clone());
    tts_candidates.extend(fb_chain);

    let n_tts = tts_candidates.len();
    let mut bytes_result: Result<Bytes, String> = Err("no TTS candidates".to_string());
    let mut serving_model: LlmModel = (*model).clone();
    for (idx, cand) in tts_candidates.iter().enumerate() {
        let r = match cand.provider.to_lowercase().as_str() {
            "openai" => call_openai_speech(&state, cand, &req).await,
            "elevenlabs" | "eleven_labs" | "eleven-labs" => {
                call_elevenlabs_speech(&state, cand, &req).await
            }
            "google" | "google_cloud" | "google-cloud" => call_google_tts(&state, cand, &req).await,
            other => Err(format!("provider {other:?} is not a TTS provider")),
        };
        serving_model = cand.clone();
        if r.is_ok() {
            if idx > 0 {
                tracing::info!(model = %cand.model_name, "audio speech: served via model-level fallback");
                state.router_state.record_success(&cand.id);
            }
            bytes_result = r;
            break;
        }
        bytes_result = r;
        if idx + 1 < n_tts {
            state
                .router_state
                .record_failure(&cand.id, 502, 60, "tts model-level fallback");
        }
    }
    let model = std::sync::Arc::new(serving_model);
    // The records name the model that served.
    if bytes_result.is_ok() && model.id != pre.model.id {
        pre.served_model = Some((*model).clone());
    }
    let audio_bytes = match bytes_result {
        Ok(b) => b,
        Err(err) => {
            let elapsed_ms = started.elapsed().as_millis() as f64;
            tracing::warn!(
                tenant_id = %tenant_id,
                model_id = %model.id,
                "TTS upstream failed: {err}"
            );
            // Audit the failed call into proxy_logs / usage_records
            // so the admin sees the upstream error in the dashboard
            // instead of a silent miss. Also feeds the breaker via
            // the dispatch guard.
            record_multimodal_outcome(
                &state,
                &pre,
                &MultimodalOutcome {
                    status_code: StatusCode::BAD_GATEWAY.as_u16(),
                    duration_ms: elapsed_ms,
                    success: false,
                    error: Some(&format!("upstream error: {err}")),
                    cost_usd: 0.0,
                    endpoint: "/v1/proxy/llm/audio/speech",
                    request_summary: None,
                    // Upstream failed — no audio synthesised. We
                    // still log the input chars on the success
                    // path because that's the billable unit, but
                    // for failures it's safer to leave them off
                    // so dashboards don't double-count failed +
                    // retried requests.
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

    // Cost. Per-character is the OpenAI/most-providers axis; per-
    // second is used by Google/ElevenLabs and would require us to
    // know the output duration (which we don't until we decode the
    // audio). v1 uses per-char only.
    let cost = model
        .cost_per_audio_char
        .map(|c| c * req.input.chars().count() as f64)
        .unwrap_or(0.0);
    let mime = mime_for_format(&req.response_format);

    // Optional persistence via the Files API.
    let mut file_id_header: Option<String> = None;
    if req.persist {
        let now = Utc::now();
        let file_id = format!("file_{}", ulid::Ulid::new());
        let ext = ext_for_format(&req.response_format);
        match ObjectKey::build(&tenant_id, MediaKind::AudioSpeech, &file_id, &ext, now) {
            Ok(key) => {
                if state
                    .media_storage
                    .put(key.as_str(), audio_bytes.clone(), &mime)
                    .await
                    .is_ok()
                {
                    let size_bytes = audio_bytes.len() as i64;
                    let content_hash = sha256_hex(&audio_bytes);
                    let res = sqlx::query(
                        r#"
                        INSERT INTO media_files (
                            id, tenant_id, kind, mime_type, size_bytes, content_hash,
                            object_key, chat_id, model_id, actor_id,
                            created_at, expires_at
                        )
                        VALUES ($1,$2,'audio_speech',$3,$4,$5,$6,$7,$8,$9,$10,$11)
                        "#,
                    )
                    .bind(&file_id)
                    .bind(&tenant_id)
                    .bind(&mime)
                    .bind(size_bytes)
                    .bind(&content_hash)
                    .bind(key.as_str())
                    .bind(&req.chat_id)
                    .bind(&model.id)
                    .bind(&user_id)
                    .bind(now)
                    .bind(now + ChronoDuration::days(DEFAULT_RETENTION_DAYS))
                    .execute(&state.db)
                    .await;
                    if res.is_ok() {
                        file_id_header = Some(file_id);
                    } else {
                        tracing::warn!("media_files INSERT failed for TTS persist");
                    }
                }
            }
            Err(err) => {
                tracing::warn!("object key build failed for TTS persist: {err}");
            }
        }
    }

    let elapsed_ms = started.elapsed().as_millis() as i64;
    tracing::debug!(
        tenant_id = %tenant_id,
        model_id = %model.id,
        chars = req.input.chars().count(),
        cost_usd = cost,
        duration_ms = elapsed_ms,
        "TTS completed"
    );

    // Canonical audit + usage record. Routes through the
    // background writer so the response path stays fast. The
    // `cost_usd` here is what `enforce_llm_quota` reads on the
    // *next* request to enforce daily/monthly budget caps —
    // skipping this row would leave multimodal traffic invisible
    // to the existing budget caps. Also finalises the dispatch
    // guard with `record_success` so the breaker resets its
    // failure counter.
    record_multimodal_outcome(
        &state,
        &pre,
        &MultimodalOutcome {
            status_code: StatusCode::OK.as_u16(),
            duration_ms: elapsed_ms as f64,
            success: true,
            error: None,
            cost_usd: cost,
            endpoint: "/v1/proxy/llm/audio/speech",
            request_summary: Some(serde_json::json!({
                "input_chars": req.input.chars().count(),
                "voice": req.voice,
                "response_format": req.response_format,
            })),
            media_units: MediaUnitCounters {
                audio_chars: Some(req.input.chars().count() as i64),
                ..Default::default()
            },
            // PII redacted on the input (TTS prompt).
            pii_detected: pre.input_pii_redacted,
            residency: Some(&residency),
        },
        &headers,
        gate_guard.take(),
    );

    let mut resp_headers = HeaderMap::new();
    if let Ok(v) = HeaderValue::from_str(&mime) {
        resp_headers.insert(header::CONTENT_TYPE, v);
    }
    if let Ok(v) = HeaderValue::from_str(&format!("{cost:.6}")) {
        resp_headers.insert("X-Brutor-Cost-USD", v);
    }
    if let Ok(v) = HeaderValue::from_str(&model.id) {
        resp_headers.insert("X-Brutor-Model-ID", v);
    }
    if let Ok(v) = HeaderValue::from_str(&mime) {
        resp_headers.insert("X-Brutor-Mime-Type", v);
    }
    if let Some(fid) = file_id_header {
        if let Ok(v) = HeaderValue::from_str(&fid) {
            resp_headers.insert("X-Brutor-File-Id", v);
        }
    }
    (StatusCode::OK, resp_headers, audio_bytes).into_response()
}

// ---------------------------------------------------------------------------
// Provider: OpenAI Audio Speech
// ---------------------------------------------------------------------------

// OpenAI's `/v1/audio/speech` rejects inputs longer than 4096
// characters with HTTP 400 `string_too_long`. Anything above
// that has to be split client-side. We chunk on paragraph /
// sentence boundaries (so prosody isn't shredded mid-thought)
// at this conservative cap, well below the OpenAI hard limit
// to leave headroom for any prompt-template additions on
// future API versions.
const OPENAI_TTS_CHUNK_CHARS: usize = 3800;

async fn call_openai_speech(
    state: &SharedState,
    model: &LlmModel,
    req: &SpeechRequest,
) -> Result<Bytes, String> {
    let api_key = model
        .api_key
        .clone()
        .ok_or_else(|| "OpenAI model has no api_key configured".to_string())?;
    let base_url = crate::services::providers::upstream_base(
        model,
        crate::services::providers::ProviderApi::Speech,
    )?;
    let url = crate::services::providers::join_api_url(&base_url, "/v1/audio/speech");

    let chunks = split_text_for_tts(&req.input, OPENAI_TTS_CHUNK_CHARS);

    // Single-chunk fast path — same shape as the original call,
    // no extra allocation.
    if chunks.len() <= 1 {
        return openai_speech_single(state, &api_key, &url, model, req, &req.input).await;
    }

    // Multi-chunk path. Only formats whose raw frames are
    // self-synchronising are safe to concat as raw bytes:
    //   - mp3 (MPEG audio layer III frame headers)
    //   - aac (ADTS frames)
    //   - flac (FLAC frames after fLaC magic; first chunk's
    //     header is reused — close enough for our use-case)
    //   - pcm (raw samples, no header)
    // wav has a RIFF header with embedded length fields, opus
    // is wrapped in ogg containers — concat would yield an
    // invalid file. Refuse those upfront with a clear error
    // instead of producing silent corrupted audio.
    let format = req.response_format.to_lowercase();
    let concat_safe = matches!(format.as_str(), "mp3" | "aac" | "pcm" | "flac");
    if !concat_safe {
        return Err(format!(
            "input is {} chars — exceeds OpenAI's 4096 cap. \
             Long inputs require a concat-safe format (mp3, aac, flac, pcm); \
             got {:?}.",
            req.input.chars().count(),
            req.response_format
        ));
    }

    tracing::debug!(
        chunks = chunks.len(),
        total_chars = req.input.chars().count(),
        "OpenAI TTS chunking long input"
    );

    // Concatenate the chunks. For mp3/aac, each OpenAI response
    // starts with an ID3v2 metadata tag (or other container
    // header). Splatting those into the middle of the
    // playback stream can confuse browser mp3 decoders into
    // stopping early — Chrome especially has been observed to
    // halt playback at an in-stream ID3 boundary even though
    // there are more frames after it. Keep chunk 1's tag for
    // the player's "track length / artist" metadata, but trim
    // everything before the first MPEG frame sync (`0xFF 0xEx`/
    // `0xFx`) on every subsequent chunk so the decoder sees a
    // contiguous frame stream.
    let strip_leading = matches!(format.as_str(), "mp3" | "aac");
    let mut combined: Vec<u8> = Vec::new();
    let total_chunks = chunks.len();
    for (i, chunk) in chunks.iter().enumerate() {
        let raw = openai_speech_single(state, &api_key, &url, model, req, chunk)
            .await
            .map_err(|e| format!("chunk {}/{} failed: {e}", i + 1, total_chunks))?;

        let bytes_to_append: &[u8] = if i == 0 || !strip_leading {
            &raw
        } else {
            mp3_strip_leading_metadata(&raw)
        };

        tracing::debug!(
            chunk_index = i,
            chunk_total = total_chunks,
            input_chars = chunk.chars().count(),
            raw_bytes = raw.len(),
            appended_bytes = bytes_to_append.len(),
            "OpenAI TTS chunk concatenated"
        );

        combined.extend_from_slice(bytes_to_append);
    }
    tracing::debug!(
        chunks = total_chunks,
        total_bytes = combined.len(),
        "OpenAI TTS chunked response assembled"
    );
    Ok(Bytes::from(combined))
}

/// Find the first MPEG audio frame sync in `bytes` and return
/// a slice starting there. Frame sync is `0xFF` followed by a
/// byte whose top 3 bits are all 1 (i.e. `0xE0..=0xFF`). If no
/// sync is found within the first 4 KiB, return the whole slice
/// (giving up beats accidentally dropping audio bytes — the
/// only cost is the original problem may persist for that one
/// chunk).
fn mp3_strip_leading_metadata(bytes: &[u8]) -> &[u8] {
    let scan_limit = bytes.len().min(4096);
    for i in 0..scan_limit.saturating_sub(1) {
        if bytes[i] == 0xFF && (bytes[i + 1] & 0xE0) == 0xE0 {
            return &bytes[i..];
        }
    }
    bytes
}

async fn openai_speech_single(
    state: &SharedState,
    api_key: &str,
    url: &str,
    model: &LlmModel,
    req: &SpeechRequest,
    input: &str,
) -> Result<Bytes, String> {
    let body = json!({
        "model": model.model_name,
        "voice": req.voice,
        "input": input,
        "response_format": req.response_format,
        "speed": req.speed,
    });

    let resp = state
        .http_client
        .post(url)
        .bearer_auth(api_key)
        .json(&body)
        .timeout(Duration::from_secs(model.timeout.unwrap_or(120.0) as u64))
        .send()
        .await
        .map_err(|e| format!("HTTP send: {e}"))?;
    let status = resp.status();
    let body = resp.bytes().await.map_err(|e| format!("read body: {e}"))?;
    if !status.is_success() {
        return Err(format!(
            "OpenAI {status}: {}",
            String::from_utf8_lossy(&body)
        ));
    }
    Ok(body)
}

/// Split text into TTS chunks that respect prosody.
///
/// Strategy, in priority order:
///   1. Paragraph (`\n\n`) — preserves topic boundaries.
///   2. Sentence terminator (`. `, `! `, `? `) — preserves
///      breath / intonation.
///   3. Hard char split — last resort for a single
///      run-on sentence longer than `max_chars`.
///
/// All splits are Unicode-safe (operate on chars, never on
/// raw byte indices that could land mid-codepoint).
fn split_text_for_tts(input: &str, max_chars: usize) -> Vec<String> {
    if input.chars().count() <= max_chars {
        return vec![input.to_string()];
    }

    let mut chunks: Vec<String> = Vec::new();
    let mut current = String::new();
    let mut current_len = 0usize;
    let push_current = |chunks: &mut Vec<String>, current: &mut String, current_len: &mut usize| {
        if !current.is_empty() {
            chunks.push(std::mem::take(current));
            *current_len = 0;
        }
    };

    for paragraph in input.split("\n\n") {
        let para_len = paragraph.chars().count();

        if para_len > max_chars {
            // Paragraph alone exceeds the cap — break into sentences.
            push_current(&mut chunks, &mut current, &mut current_len);
            for sentence_chunk in split_into_sentence_chunks(paragraph, max_chars) {
                chunks.push(sentence_chunk);
            }
            continue;
        }

        // Paragraph fits. Try to append to the current chunk.
        let separator_len = if current.is_empty() { 0 } else { 2 };
        if current_len + separator_len + para_len > max_chars {
            push_current(&mut chunks, &mut current, &mut current_len);
        }
        if !current.is_empty() {
            current.push_str("\n\n");
            current_len += 2;
        }
        current.push_str(paragraph);
        current_len += para_len;
    }
    push_current(&mut chunks, &mut current, &mut current_len);
    chunks
}

fn split_into_sentence_chunks(paragraph: &str, max_chars: usize) -> Vec<String> {
    // Walk the paragraph one char at a time looking for sentence
    // terminators (`. `, `! `, `? `). Keep the terminator with
    // the preceding sentence so the audio doesn't drop the
    // intonation cue.
    let chars: Vec<(usize, char)> = paragraph.char_indices().collect();
    let mut sentences: Vec<&str> = Vec::new();
    let mut start_byte = 0usize;
    let mut i = 0;
    while i < chars.len() {
        let (_off, c) = chars[i];
        if matches!(c, '.' | '!' | '?') && i + 1 < chars.len() {
            let (_, next) = chars[i + 1];
            if next == ' ' || next == '\n' {
                // End of sentence. The sentence runs from
                // start_byte to the byte right after the
                // whitespace.
                let end_byte = chars.get(i + 2).map(|(o, _)| *o).unwrap_or(paragraph.len());
                sentences.push(&paragraph[start_byte..end_byte]);
                start_byte = end_byte;
                i += 2;
                continue;
            }
        }
        i += 1;
    }
    if start_byte < paragraph.len() {
        sentences.push(&paragraph[start_byte..]);
    }

    // Greedily pack sentences into chunks. If a single
    // sentence is itself longer than max_chars, hard-split it.
    let mut out: Vec<String> = Vec::new();
    let mut current = String::new();
    let mut current_len = 0usize;

    for sentence in sentences {
        let s_len = sentence.chars().count();
        if s_len > max_chars {
            if !current.is_empty() {
                out.push(std::mem::take(&mut current));
                current_len = 0;
            }
            out.extend(hard_split_by_chars(sentence, max_chars));
            continue;
        }
        if current_len + s_len > max_chars && !current.is_empty() {
            out.push(std::mem::take(&mut current));
            current_len = 0;
        }
        current.push_str(sentence);
        current_len += s_len;
    }
    if !current.is_empty() {
        out.push(current);
    }
    out
}

fn hard_split_by_chars(s: &str, max_chars: usize) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    let mut cur = String::new();
    let mut n = 0usize;
    for c in s.chars() {
        cur.push(c);
        n += 1;
        if n >= max_chars {
            out.push(std::mem::take(&mut cur));
            n = 0;
        }
    }
    if !cur.is_empty() {
        out.push(cur);
    }
    out
}

// ---------------------------------------------------------------------------
// Provider: ElevenLabs
// ---------------------------------------------------------------------------
//
// ElevenLabs uses voice IDs in the URL path:
//   `POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}`
// with `xi-api-key: <api_key>` and a JSON body of
//   `{ "text": "...", "model_id": "...", "voice_settings": {...} }`.
//
// In our catalog:
//   - `model.model_name` → the ElevenLabs model id (e.g.
//     `eleven_multilingual_v2`, `eleven_turbo_v2_5`).
//   - `req.voice` → the ElevenLabs voice ID. Operators put the
//     voice IDs in the model's `supported_voices` whitelist so the
//     UI dropdown picks them up the same way as OpenAI voices.
//
// Output format is selected via the `?output_format=` query
// parameter. We map the OpenAI-style format names to the closest
// ElevenLabs option (mp3 default; pcm and opus where supported).

async fn call_elevenlabs_speech(
    state: &SharedState,
    model: &LlmModel,
    req: &SpeechRequest,
) -> Result<Bytes, String> {
    let api_key = model
        .api_key
        .clone()
        .ok_or_else(|| "ElevenLabs model has no api_key configured".to_string())?;
    let base_url = crate::services::providers::upstream_base(
        model,
        crate::services::providers::ProviderApi::Speech,
    )?;
    let output_format = elevenlabs_output_format(&req.response_format)?;
    let url = crate::services::providers::join_api_url(
        &base_url,
        &format!(
            "/v1/text-to-speech/{}?output_format={}",
            urlencode_path(&req.voice),
            output_format,
        ),
    );
    let body = json!({
        "text": req.input,
        "model_id": model.model_name,
        // Speed is honored on `eleven_turbo_v2*`/`eleven_multilingual_v2`
        // via voice_settings. ElevenLabs ignores unknown fields.
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "speed": req.speed,
        },
    });

    let resp = state
        .http_client
        .post(&url)
        .header("xi-api-key", api_key)
        .header("accept", "audio/mpeg")
        .json(&body)
        .timeout(Duration::from_secs(model.timeout.unwrap_or(120.0) as u64))
        .send()
        .await
        .map_err(|e| format!("HTTP send: {e}"))?;
    let status = resp.status();
    let bytes = resp.bytes().await.map_err(|e| format!("read body: {e}"))?;
    if !status.is_success() {
        return Err(format!(
            "ElevenLabs {status}: {}",
            String::from_utf8_lossy(&bytes)
        ));
    }
    Ok(bytes)
}

/// Map our generic `response_format` to ElevenLabs' specific
/// `output_format` enum. Returns an `Err` for formats ElevenLabs
/// doesn't expose (e.g. flac, aac), so callers see a clear 502
/// instead of a bewildering upstream 400.
fn elevenlabs_output_format(fmt: &str) -> Result<&'static str, String> {
    match fmt.to_ascii_lowercase().as_str() {
        "mp3" => Ok("mp3_44100_128"),
        "opus" => Ok("opus_48000_64"),
        "pcm" => Ok("pcm_44100"),
        // Operators wanting WAV today should go via PCM — the
        // browser's <audio> tag plays mp3 fine and that's the
        // default. Spelling this out so callers don't silently get
        // a non-spec response.
        other => Err(format!(
            "ElevenLabs does not support response_format={other:?} (use mp3/opus/pcm)"
        )),
    }
}

// ---------------------------------------------------------------------------
// Provider: Google Cloud Text-to-Speech
// ---------------------------------------------------------------------------
//
// Google's REST API:
//   `POST https://texttospeech.googleapis.com/v1/text:synthesize`
// with either an OAuth2 bearer token or an API key. We support
// the API-key path because it's simpler to provision and works
// for the same per-request-paid TTS operations.
//
// Body shape:
//   {
//     "input": { "text": "..." },
//     "voice": { "languageCode": "en-US", "name": "en-US-Wavenet-D" },
//     "audioConfig": { "audioEncoding": "MP3", "speakingRate": 1.0 }
//   }
//
// Response: `{ "audioContent": "<base64>" }`. We decode and return
// raw bytes — Google's MP3 has the right header so the browser
// plays it directly.
//
// `req.voice` is the Google voice name (e.g. `en-US-Wavenet-D`);
// we derive the language code from the leading two segments
// (`en-US`).

async fn call_google_tts(
    state: &SharedState,
    model: &LlmModel,
    req: &SpeechRequest,
) -> Result<Bytes, String> {
    let api_key = model
        .api_key
        .clone()
        .ok_or_else(|| "Google TTS model has no api_key configured".to_string())?;
    let base_url = crate::services::providers::upstream_base(
        model,
        crate::services::providers::ProviderApi::Speech,
    )?;
    let url = crate::services::providers::join_api_url(&base_url, "/v1/text:synthesize");

    let language_code = google_language_code_from_voice(&req.voice).ok_or_else(|| {
        format!(
            "voice {:?} doesn't look like a Google voice name (expected e.g. 'en-US-Wavenet-D')",
            req.voice
        )
    })?;
    let audio_encoding = google_audio_encoding(&req.response_format)?;

    let body = json!({
        "input": { "text": req.input },
        "voice": {
            "languageCode": language_code,
            "name": req.voice,
        },
        "audioConfig": {
            "audioEncoding": audio_encoding,
            "speakingRate": req.speed,
        },
    });

    let resp = state
        .http_client
        .post(&url)
        // Google accepts the API key in the X-Goog-Api-Key header
        // (preferred) or as a `?key=` query param; we use the
        // header so the key never lands in proxy logs as a URL
        // segment.
        .header("X-Goog-Api-Key", api_key)
        .json(&body)
        .timeout(Duration::from_secs(model.timeout.unwrap_or(120.0) as u64))
        .send()
        .await
        .map_err(|e| format!("HTTP send: {e}"))?;
    let status = resp.status();
    let raw = resp.text().await.map_err(|e| format!("read body: {e}"))?;
    if !status.is_success() {
        return Err(format!("Google TTS {status}: {raw}"));
    }
    let parsed: serde_json::Value =
        serde_json::from_str(&raw).map_err(|e| format!("parse Google body: {e}"))?;
    let b64 = parsed
        .get("audioContent")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "Google response missing audioContent".to_string())?;
    use base64::Engine;
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(b64)
        .map_err(|e| format!("Google audio base64: {e}"))?;
    Ok(Bytes::from(bytes))
}

fn google_language_code_from_voice(voice: &str) -> Option<String> {
    // Google voice names are `<lang>-<region>-<flavor>-<id>` (e.g.
    // `en-US-Wavenet-D`, `cmn-CN-Standard-A`). Take the first two
    // hyphen-separated segments.
    let parts: Vec<&str> = voice.split('-').collect();
    if parts.len() < 2 {
        return None;
    }
    Some(format!("{}-{}", parts[0], parts[1]))
}

fn google_audio_encoding(fmt: &str) -> Result<&'static str, String> {
    match fmt.to_ascii_lowercase().as_str() {
        "mp3" => Ok("MP3"),
        "opus" => Ok("OGG_OPUS"),
        // Google calls 16-bit PCM `LINEAR16` — the bytes are raw,
        // not WAV-wrapped. Surface this through `pcm` only so
        // callers asking for `wav` get a clear error.
        "pcm" => Ok("LINEAR16"),
        // ALAW / MULAW are Google-specific telephony codecs;
        // we don't expose them.
        other => Err(format!(
            "Google TTS does not support response_format={other:?} (use mp3/opus/pcm)"
        )),
    }
}

/// Percent-encode a path segment (used for the ElevenLabs voice
/// id). We intentionally don't pull in the `urlencoding` crate
/// for one call site — voice ids are alphanum/underscore in
/// practice, but a stray space or slash would corrupt the URL
/// silently and we'd rather encode defensively.
fn urlencode_path(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.as_bytes() {
        let c = *b as char;
        if c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.' | '~') {
            out.push(c);
        } else {
            out.push_str(&format!("%{:02X}", *b));
        }
    }
    out
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

fn mime_for_format(fmt: &str) -> String {
    match fmt.to_ascii_lowercase().as_str() {
        "mp3" => "audio/mpeg",
        "opus" => "audio/ogg; codecs=opus",
        "aac" => "audio/aac",
        "flac" => "audio/flac",
        "wav" => "audio/wav",
        "pcm" => "audio/L16",
        _ => "application/octet-stream",
    }
    .to_string()
}

fn ext_for_format(fmt: &str) -> String {
    match fmt.to_ascii_lowercase().as_str() {
        "mp3" => "mp3",
        "opus" => "opus",
        "aac" => "aac",
        "flac" => "flac",
        "wav" => "wav",
        "pcm" => "pcm",
        _ => "bin",
    }
    .to_string()
}

fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::Digest;
    let digest = sha2::Sha256::digest(bytes);
    hex::encode(digest)
}

fn error_resp(status: StatusCode, message: &str) -> Response {
    (status, Json(json!({ "error": { "message": message } }))).into_response()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn mime_map_handles_known_formats() {
        assert_eq!(mime_for_format("mp3"), "audio/mpeg");
        assert_eq!(mime_for_format("MP3"), "audio/mpeg");
        assert_eq!(mime_for_format("opus"), "audio/ogg; codecs=opus");
        assert_eq!(mime_for_format("flac"), "audio/flac");
        assert_eq!(mime_for_format("xyz"), "application/octet-stream");
    }

    #[test]
    fn ext_map_handles_known_formats() {
        assert_eq!(ext_for_format("mp3"), "mp3");
        assert_eq!(ext_for_format("OPUS"), "opus");
        assert_eq!(ext_for_format("xyz"), "bin");
    }

    #[test]
    fn jsonb_voices_parses() {
        let v = json!(["alloy", "echo", "fable"]);
        assert_eq!(
            jsonb_string_array(&Some(v)),
            Some(vec!["alloy".into(), "echo".into(), "fable".into()])
        );
    }

    #[test]
    fn jsonb_voices_returns_none_on_non_array() {
        assert_eq!(jsonb_string_array(&Some(json!("alloy"))), None);
        assert_eq!(jsonb_string_array(&None), None);
    }

    #[test]
    fn sha256_hex_is_stable() {
        assert_eq!(sha256_hex(b"hello"), sha256_hex(b"hello"));
        assert_eq!(sha256_hex(b"hello").len(), 64);
    }

    // ----- 3.2b provider helper tests -----

    #[test]
    fn elevenlabs_output_format_maps_known() {
        assert_eq!(elevenlabs_output_format("mp3").unwrap(), "mp3_44100_128");
        assert_eq!(elevenlabs_output_format("MP3").unwrap(), "mp3_44100_128");
        assert_eq!(elevenlabs_output_format("opus").unwrap(), "opus_48000_64");
        assert_eq!(elevenlabs_output_format("pcm").unwrap(), "pcm_44100");
    }

    #[test]
    fn elevenlabs_output_format_rejects_unsupported() {
        assert!(elevenlabs_output_format("flac").is_err());
        assert!(elevenlabs_output_format("aac").is_err());
        assert!(elevenlabs_output_format("wav").is_err());
    }

    #[test]
    fn google_audio_encoding_maps_known() {
        assert_eq!(google_audio_encoding("mp3").unwrap(), "MP3");
        assert_eq!(google_audio_encoding("opus").unwrap(), "OGG_OPUS");
        assert_eq!(google_audio_encoding("pcm").unwrap(), "LINEAR16");
    }

    #[test]
    fn google_audio_encoding_rejects_wav() {
        // wav is not LINEAR16 — Google returns raw PCM.
        let err = google_audio_encoding("wav").unwrap_err();
        assert!(err.contains("does not support"));
    }

    #[test]
    fn google_language_code_handles_typical_names() {
        assert_eq!(
            google_language_code_from_voice("en-US-Wavenet-D"),
            Some("en-US".to_string())
        );
        assert_eq!(
            google_language_code_from_voice("cmn-CN-Standard-A"),
            Some("cmn-CN".to_string())
        );
    }

    #[test]
    fn google_language_code_returns_none_for_malformed() {
        assert_eq!(google_language_code_from_voice("alloy"), None);
        assert_eq!(google_language_code_from_voice(""), None);
    }

    #[test]
    fn urlencode_path_passes_safe_chars() {
        assert_eq!(urlencode_path("voice_abc-123"), "voice_abc-123");
        assert_eq!(urlencode_path("AbCxYz09"), "AbCxYz09");
    }

    #[test]
    fn urlencode_path_escapes_unsafe_chars() {
        assert_eq!(urlencode_path("a/b"), "a%2Fb");
        assert_eq!(urlencode_path("a b"), "a%20b");
        assert_eq!(urlencode_path("a?b"), "a%3Fb");
    }

    // ----- TTS chunking tests -----

    #[test]
    fn tts_split_returns_single_chunk_when_short() {
        let out = split_text_for_tts("hello world", 100);
        assert_eq!(out, vec!["hello world".to_string()]);
    }

    #[test]
    fn tts_split_returns_single_chunk_when_at_limit() {
        let s = "a".repeat(100);
        let out = split_text_for_tts(&s, 100);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0], s);
    }

    #[test]
    fn tts_split_breaks_on_paragraph_boundary() {
        // Two 60-char paragraphs separated by `\n\n`. Cap of 80
        // forces them apart since 60+2+60 > 80.
        let p1 = "a".repeat(60);
        let p2 = "b".repeat(60);
        let s = format!("{p1}\n\n{p2}");
        let out = split_text_for_tts(&s, 80);
        assert_eq!(out.len(), 2);
        assert!(out[0].chars().count() <= 80);
        assert!(out[1].chars().count() <= 80);
        // Original content fully present.
        assert_eq!(out.join("\n\n"), s);
    }

    #[test]
    fn tts_split_breaks_on_sentence_boundary_when_paragraph_too_long() {
        // One paragraph of three sentences, total ~150 chars.
        let s = format!(
            "{} {} {}",
            "Sentence one is here.", "Sentence two follows.", "Sentence three caps it."
        );
        let out = split_text_for_tts(&s, 30);
        assert!(out.len() >= 3);
        // No chunk exceeds the cap.
        for c in &out {
            assert!(
                c.chars().count() <= 30,
                "chunk too long: {} chars",
                c.chars().count()
            );
        }
        // Sentence terminators kept with the preceding content.
        assert!(out.iter().any(|c| c.contains("one")));
        assert!(out.iter().any(|c| c.contains("two")));
        assert!(out.iter().any(|c| c.contains("three")));
    }

    #[test]
    fn tts_split_handles_unicode_safely() {
        // 4-byte emoji + multi-byte CJK + curly quotes — must
        // never split mid-codepoint or panic.
        let s = "Café — naïve résumé 你好世界 🎉. ".repeat(20);
        let out = split_text_for_tts(&s, 50);
        // Round-trip the chars to make sure we lost nothing.
        let joined: String = out.join("");
        assert_eq!(joined.chars().count(), s.chars().count());
        for c in &out {
            assert!(
                c.chars().count() <= 50 + 5, // small slack for sentence packing
                "chunk too long: {} chars",
                c.chars().count()
            );
        }
    }

    #[test]
    fn tts_split_hard_splits_a_sentence_longer_than_cap() {
        // Single run-on with no terminator anywhere. Must still
        // split into sub-`max_chars` chunks.
        let s = "a".repeat(250);
        let out = split_text_for_tts(&s, 100);
        assert!(out.len() >= 3);
        for c in &out {
            assert!(c.chars().count() <= 100);
        }
        assert_eq!(out.join(""), s);
    }

    #[test]
    fn tts_split_packs_short_sentences_together() {
        // Three sentences, each ~25 chars. With cap of 80 they
        // should pack into 1–2 chunks rather than 3.
        let s = "First short one. Second short one. Third short one.";
        let out = split_text_for_tts(s, 80);
        assert!(
            out.len() <= 2,
            "expected packing into ≤2 chunks, got {}",
            out.len()
        );
        for c in &out {
            assert!(c.chars().count() <= 80);
        }
    }

    #[test]
    fn tts_split_into_sentence_chunks_keeps_terminators() {
        // The terminator + space must travel with the sentence.
        let out = split_into_sentence_chunks("Hello. World!", 100);
        // Either 1 chunk packed, or 2 — both fine, but the
        // terminators must appear.
        let joined: String = out.join("");
        assert!(joined.contains("Hello."));
        assert!(joined.contains("World!"));
    }

    #[test]
    fn tts_hard_split_chunks_evenly() {
        let out = hard_split_by_chars("abcdefghij", 3);
        assert_eq!(out, vec!["abc", "def", "ghi", "j"]);
    }

    // ----- mp3 metadata stripping tests -----

    #[test]
    fn strip_leading_metadata_skips_id3v2_header() {
        // Build "ID3" + version + flags + 4 synchsafe-size
        // bytes (10-byte header) + 100 bytes of "tag body" +
        // an mp3 frame sync at the end.
        let mut buf = Vec::new();
        buf.extend_from_slice(b"ID3"); // magic
        buf.push(0x04); // major version
        buf.push(0x00); // minor version
        buf.push(0x00); // flags
        buf.extend_from_slice(&[0x00, 0x00, 0x00, 100]); // synchsafe(100)
        buf.extend_from_slice(&[0u8; 100]); // tag body
        buf.extend_from_slice(&[0xFF, 0xFB]); // frame sync
        buf.extend_from_slice(&[0x90, 0x00]); // rest of frame header

        let stripped = mp3_strip_leading_metadata(&buf);
        assert_eq!(stripped[0], 0xFF);
        assert_eq!(stripped[1] & 0xE0, 0xE0);
    }

    #[test]
    fn strip_leading_metadata_passes_through_bare_frame() {
        // Already starts with a frame sync — slice is
        // returned as-is.
        let buf = vec![0xFF, 0xFB, 0x90, 0x00, 0x12, 0x34];
        let stripped = mp3_strip_leading_metadata(&buf);
        assert_eq!(stripped.len(), buf.len());
        assert_eq!(stripped[0], 0xFF);
    }

    #[test]
    fn strip_leading_metadata_returns_input_when_no_sync_found() {
        // No frame sync within the scan limit — return the
        // input rather than dropping bytes.
        let buf = vec![0u8; 200];
        let stripped = mp3_strip_leading_metadata(&buf);
        assert_eq!(stripped.len(), buf.len());
    }

    #[test]
    fn strip_leading_metadata_recognises_ff_e0_through_ff_ff() {
        // Frame sync is 0xFF followed by a byte where the
        // top 3 bits are 1 — that's 0xE0..=0xFF. All eight
        // of those should pass the gate.
        for second in [0xE0u8, 0xE7, 0xEF, 0xF0, 0xF3, 0xFB, 0xFF] {
            let buf = [0x00u8, 0x00, 0xFF, second, 0x90, 0x00];
            let stripped = mp3_strip_leading_metadata(&buf);
            assert_eq!(stripped[0], 0xFF);
            assert_eq!(stripped[1], second);
        }
    }

    #[test]
    fn strip_leading_metadata_rejects_ff_followed_by_low_byte() {
        // `0xFF 0x00` is NOT a frame sync — top 3 bits
        // not all 1. Helper should keep scanning.
        let mut buf = Vec::new();
        buf.extend_from_slice(&[0x00, 0x00, 0xFF, 0x00, 0x12, 0x34]);
        buf.extend_from_slice(&[0xFF, 0xFB, 0x90, 0x00]); // real sync later
        let stripped = mp3_strip_leading_metadata(&buf);
        assert_eq!(stripped[0], 0xFF);
        assert_eq!(stripped[1], 0xFB);
    }

    #[test]
    fn tts_hard_split_keeps_unicode_codepoints_intact() {
        // 4-byte chars must not be split mid-codepoint.
        let out = hard_split_by_chars("🎉🎉🎉🎉🎉", 2);
        assert_eq!(out.len(), 3);
        // Each chunk decodes cleanly.
        for c in &out {
            assert!(c.chars().count() <= 2);
            assert!(c.chars().all(|ch| ch == '🎉'));
        }
    }
}
