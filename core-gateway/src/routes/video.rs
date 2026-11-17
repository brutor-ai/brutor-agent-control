// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
//! Video generation proxy.
//!
//! ## Endpoints
//!
//! - `POST /v1/proxy/llm/video/generations` — submit a job.
//!   Returns the job id immediately; the upstream provider's
//!   queue API does the actual work asynchronously.
//! - `GET  /v1/proxy/llm/video/jobs/{id}` — fetch status.
//!   Portal polls this endpoint while a clip is rendering.
//!
//! ## Pipeline (today)
//!
//! 1. Authenticate the caller (an API key, or a portal session where the
//!    Portal is composed).
//! 2. Resolve the model; reject if not `mode='video_generation'`.
//! 3. Validate resolution/duration against the model's
//!    whitelists.
//! 4. Insert a `video_generation_jobs` row with `status='pending'`.
//! 5. Return `{job_id, status: 'pending'}`.
//!
//! Provider submission + polling lives in `services::video_pipeline`: a tokio
//! background task drives `pending` rows through `submitted` →
//! terminal status. This module ships the foundation.

use axum::{
    Json, Router,
    extract::{Path, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use chrono::Utc;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::time::Instant;

use crate::middleware::tenant::TenantContext;
use crate::services::SharedState;
use crate::services::multimodal_pipeline::{
    MediaUnitCounters, MultimodalOutcome, acquire_gate, multimodal_preflight,
    record_multimodal_outcome,
};

pub fn router() -> Router<SharedState> {
    Router::new().route("/", post(submit_job))
}

pub fn jobs_router() -> Router<SharedState> {
    Router::new().route("/{job_id}", get(get_job).delete(cancel_job))
}

#[derive(Debug, Deserialize)]
struct SubmitRequest {
    model: String,
    prompt: String,
    /// Resolution token from the model's
    /// `supported_video_resolutions` whitelist (e.g. `"720p"`).
    #[serde(default)]
    resolution: Option<String>,
    /// Clip duration in seconds. Must be ≤
    /// `max_video_duration_seconds` and (when set) in
    /// `supported_video_durations`.
    #[serde(default)]
    duration_seconds: Option<i32>,
    /// Opaque chat-id tag for "delete this chat" fanout.
    #[serde(default)]
    chat_id: Option<String>,
}

#[derive(Debug, Serialize)]
struct JobResponse {
    job_id: String,
    status: String,
    /// Set when status='succeeded' — call
    /// `GET /v1/proxy/files/{id}/url` to render.
    #[serde(skip_serializing_if = "Option::is_none")]
    output_file_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    cost_usd: Option<f64>,
    brutor_model_id: String,
    created_at: String,
    updated_at: String,
}

async fn submit_job(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Json(mut req): Json<SubmitRequest>,
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

    if req.prompt.trim().is_empty() {
        return error_resp(StatusCode::BAD_REQUEST, "prompt is empty");
    }

    // Canonical pipeline. Video is the most expensive modality
    // ($-per-second), so quota enforcement + audit trail are
    // critical here — a runaway loop without these would burn
    // through monthly budget in minutes.
    let mut pre = match multimodal_preflight(
        &state,
        &headers,
        &tenant_id,
        &req.model,
        "video_generation",
        Some(req.prompt.as_str()),
        client_ip.clone(),
        |s, m| error_resp(s, m),
    )
    .await
    {
        Ok(p) => p,
        Err(resp) => return resp,
    };
    // Honour input-guardrail Redacted outcome — splice the redacted
    // prompt back in before the video upstream sees it.
    if let Some(redacted) = &pre.redacted_prompt {
        req.prompt = redacted.clone();
    }

    // Data residency. Block before any upstream
    // provider call (no gate acquired yet, so nothing to abandon).
    let residency = match crate::routes::images::enforce_media_residency(
        &state,
        &pre,
        &headers,
        "/v1/proxy/llm/video/generations",
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

    // Resolution whitelist.
    if let (Some(res), Some(allowed)) = (
        &req.resolution,
        jsonb_string_array(&model.supported_video_resolutions),
    ) {
        if !allowed.iter().any(|r| r == res) {
            return error_resp(
                StatusCode::BAD_REQUEST,
                &format!("resolution {res:?} not in whitelist {allowed:?}"),
            );
        }
    }

    // Duration whitelist + ceiling.
    let duration = req.duration_seconds;
    if let Some(d) = duration {
        if d <= 0 {
            // Probe accounting centralized — see notes on the
            // matching short-circuit in routes/images.rs.
            // Real-client errors still bump the model_metrics
            // counter so failed real traffic shows in the admin
            // UI; probes skip because gateway-control posts the
            // outcome via record-probe-result.
            if !crate::services::catalog_probe::is_health_probe_request(&headers, &pre.auth_ctx) {
                state.model_metrics.record(&model.id, true, 0);
            }
            return error_resp(StatusCode::BAD_REQUEST, "duration_seconds must be positive");
        }
        if let Some(cap) = model.max_video_duration_seconds {
            if d > cap {
                return error_resp(
                    StatusCode::BAD_REQUEST,
                    &format!("duration_seconds {d} exceeds the model's {cap}s cap"),
                );
            }
        }
        if let Some(allowed) = jsonb_int_array(&model.supported_video_durations) {
            if !allowed.contains(&(d as i64)) {
                return error_resp(
                    StatusCode::BAD_REQUEST,
                    &format!("duration_seconds {d} not in whitelist {allowed:?}"),
                );
            }
        }
    }

    // Per-RG media governance for video_generation —
    // duration cap, resolution whitelist, prompt length cap.
    // `require_image_input` is recognised in the schema but
    // the current SubmitRequest doesn't carry an image input
    // field, so we can't truly enforce it; passing false leaves
    // the cap inert until image-to-video lands as a first-class
    // route parameter. Operators will get a clear policy
    // rejection then.
    {
        use crate::services::media_governance::{
            VideoGenerationRequest, enforce_video_generation, governance_for_mode,
        };
        // Ancestor-merged media governance (was single-group — no inheritance).
        let effective_media_gov = match pre.media_governance(&state, "video_generation").await {
            Ok(g) => g,
            Err(e) => {
                return pre.refuse_governance_unavailable(
                    &state,
                    &headers,
                    "/v1/proxy/llm/video/generations",
                    &e,
                    error_resp,
                );
            }
        };
        let mode_gov = governance_for_mode(effective_media_gov.as_ref(), "video_generation");
        let check = VideoGenerationRequest {
            duration_seconds: req.duration_seconds.map(|d| d as u32),
            resolution: req.resolution.as_deref(),
            has_image_input: false,
            prompt_chars: req.prompt.chars().count(),
        };
        if let Err(e) = enforce_video_generation(mode_gov, &check) {
            return pre.refuse_policy(
                &state,
                &headers,
                "/v1/proxy/llm/video/generations",
                &format!("policy: {e}"),
                error_resp,
            );
        }
    }

    // Per-mode media quota (daily requests / seconds / budget, inherited).
    if let Some(g) = pre.target_group.as_ref() {
        let secs = req.duration_seconds.unwrap_or(0) as i64;
        let est_cost = model.cost_per_video_second.unwrap_or(0.0) * secs as f64;
        match crate::services::usage::enforce_media_quota(
            &state.usage_state,
            &state.db,
            &tenant_id,
            g,
            &crate::services::usage::MediaCharge {
                mode: "video_generation",
                model_id: &model.id,
                units: secs,
                cost_usd: est_cost,
            },
        )
        .await
        {
            Ok(reserved) => pre.media_reservation = reserved,
            Err(reason) => {
                return pre.refuse_quota(
                    &state,
                    &headers,
                    "/v1/proxy/llm/video/generations",
                    &format!("quota: {reason}"),
                    error_resp,
                );
            }
        }
    }

    let job_id = format!("vjob_{}", ulid::Ulid::new());
    let now = Utc::now();
    let res = sqlx::query(
        r#"
        INSERT INTO video_generation_jobs (
            id, tenant_id, model_id, actor_id, prompt,
            resolution, duration_seconds, chat_id,
            status, created_at, updated_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending', $9, $9)
        "#,
    )
    .bind(&job_id)
    .bind(&tenant_id)
    .bind(&model.id)
    .bind(&user_id)
    .bind(&req.prompt)
    .bind(&req.resolution)
    .bind(duration)
    .bind(&req.chat_id)
    .bind(now)
    .execute(&state.db)
    .await;
    if let Err(err) = res {
        let elapsed_ms = started.elapsed().as_millis() as f64;
        tracing::error!(
            tenant_id = %tenant_id,
            model_id = %model.id,
            "video_generation_jobs INSERT failed: {err}"
        );
        record_multimodal_outcome(
            &state,
            &pre,
            &MultimodalOutcome {
                status_code: StatusCode::INTERNAL_SERVER_ERROR.as_u16(),
                duration_ms: elapsed_ms,
                success: false,
                error: Some("could not enqueue job"),
                cost_usd: 0.0,
                endpoint: "/v1/proxy/llm/video/generations",
                request_summary: None,
                // Job never enqueued — no work to count.
                media_units: MediaUnitCounters::default(),
                pii_detected: false,
                residency: Some(&residency),
            },
            &headers,
            gate_guard.take(),
        );
        return error_resp(StatusCode::INTERNAL_SERVER_ERROR, "could not enqueue job");
    }

    let elapsed_ms = started.elapsed().as_millis() as i64;
    tracing::debug!(
        tenant_id = %tenant_id,
        model_id = %model.id,
        job_id = %job_id,
        request_ms = elapsed_ms,
        "video gen job queued"
    );

    // Estimated cost at submit time (per-second × requested
    // duration, if both are known). Final cost is reconciled when
    // the async pipeline completes the job — the poller writes a
    // follow-up usage_records row with the actual delivered
    // duration. This row gives admins real-time visibility into
    // queued spend.
    let estimated_cost = model
        .cost_per_video_second
        .zip(req.duration_seconds)
        .map(|(rate, secs)| rate * secs as f64)
        .unwrap_or(0.0);

    record_multimodal_outcome(
        &state,
        &pre,
        &MultimodalOutcome {
            status_code: StatusCode::ACCEPTED.as_u16(),
            duration_ms: elapsed_ms as f64,
            success: true,
            error: None,
            cost_usd: estimated_cost,
            endpoint: "/v1/proxy/llm/video/generations",
            request_summary: Some(serde_json::json!({
                "job_id": job_id,
                "duration_seconds": req.duration_seconds,
                "resolution": req.resolution,
                "estimated": true,
            })),
            // Submit-time estimate — if the user requested an
            // explicit duration we log that; otherwise leave None
            // so dashboards don't accumulate a 0 for jobs whose
            // length isn't known until the poller reconciles.
            // Only one proxy_logs row is emitted per video job
            // (this submit row) so this is also the only place
            // the unit gets recorded.
            media_units: MediaUnitCounters {
                video_seconds_output: req.duration_seconds.map(|s| s as f64),
                ..Default::default()
            },
            // PII redacted on the input (video prompt).
            pii_detected: pre.input_pii_redacted,
            residency: Some(&residency),
        },
        &headers,
        gate_guard.take(),
    );

    let payload = JobResponse {
        job_id,
        status: "pending".into(),
        output_file_id: None,
        error: None,
        cost_usd: None,
        brutor_model_id: model.id.clone(),
        created_at: now.to_rfc3339(),
        updated_at: now.to_rfc3339(),
    };
    (StatusCode::ACCEPTED, Json(payload)).into_response()
}

async fn get_job(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Path(job_id): Path<String>,
) -> Response {
    let client_ip = client_addr.ip();

    let (tenant_id, actor_id, _username) = match crate::routes::request::extract_media_caller(
        &state,
        &headers,
        &tenant,
        client_ip,
        crate::routes::request::Preflight::None,
    )
    .await
    {
        Ok(v) => v,
        Err(r) => return r,
    };

    #[derive(sqlx::FromRow)]
    struct Row {
        id: String,
        model_id: Option<String>,
        status: String,
        output_file_id: Option<String>,
        error: Option<String>,
        cost_usd: Option<f64>,
        created_at: chrono::DateTime<Utc>,
        updated_at: chrono::DateTime<Utc>,
    }

    let row: Result<Option<Row>, sqlx::Error> = sqlx::query_as(concat!(
        r#"
        SELECT id, model_id, status, output_file_id, error, cost_usd,
               created_at, updated_at
        FROM video_generation_jobs
        WHERE id = $1 AND "#,
        crate::written_by_caller_sql!(),
    ))
    .bind(&job_id)
    .bind(&tenant_id)
    .bind(&actor_id)
    .fetch_optional(&state.db)
    .await;

    let row = match row {
        Ok(Some(r)) => r,
        Ok(None) => {
            return error_resp(StatusCode::NOT_FOUND, &format!("job {job_id} not found"));
        }
        Err(e) => return crate::services::db_errors::unavailable("the video job", &e),
    };

    let payload = JobResponse {
        job_id: row.id,
        status: row.status,
        output_file_id: row.output_file_id,
        error: row.error,
        cost_usd: row.cost_usd,
        brutor_model_id: row.model_id.unwrap_or_default(),
        created_at: row.created_at.to_rfc3339(),
        updated_at: row.updated_at.to_rfc3339(),
    };
    Json(payload).into_response()
}

/// Cancel an in-flight video job.
///
/// **Scope:** marks the DB row as `cancelled` so the portal stops
/// polling and the user is immediately unblocked. The upstream
/// (fal / Sora / Luma) is NOT cancelled provider-side here — that
/// requires provider-specific endpoints (`POST {queue}/{id}/cancel`
/// on fal, no equivalent on OpenAI Sora today). For Sora the
/// upstream will run to completion and the tenant will still be
/// billed for the clip; for fal a follow-up `VideoProvider::cancel`
/// trait method can issue the queue-cancel best-effort once that
/// plumbing lands.
///
/// Idempotent: cancelling an already-terminal row (succeeded /
/// failed / cancelled) returns the row's current state without
/// re-stamping `updated_at`.
async fn cancel_job(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Path(job_id): Path<String>,
) -> Response {
    let client_ip = client_addr.ip();

    let (tenant_id, actor_id, _username) = match crate::routes::request::extract_media_caller(
        &state,
        &headers,
        &tenant,
        client_ip,
        crate::routes::request::Preflight::None,
    )
    .await
    {
        Ok(v) => v,
        Err(r) => return r,
    };

    #[derive(sqlx::FromRow)]
    struct Row {
        id: String,
        model_id: Option<String>,
        status: String,
        output_file_id: Option<String>,
        error: Option<String>,
        cost_usd: Option<f64>,
        created_at: chrono::DateTime<Utc>,
        updated_at: chrono::DateTime<Utc>,
    }

    // Single statement: flip status to cancelled iff it's still
    // in a non-terminal state. The RETURNING clause lets the
    // portal see the post-update row in one round-trip — no
    // SELECT-then-UPDATE race where two browser tabs both press
    // cancel at the same moment.
    let row: Result<Option<Row>, sqlx::Error> = sqlx::query_as(concat!(
        r#"
        UPDATE video_generation_jobs
        SET status = 'cancelled',
            updated_at = NOW(),
            error = COALESCE(error, 'cancelled by user')
        WHERE id = $1
          AND "#,
        crate::written_by_caller_sql!(),
        r#"
          AND status NOT IN ('succeeded', 'failed', 'cancelled')
        RETURNING id, model_id, status, output_file_id, error, cost_usd,
                  created_at, updated_at
        "#,
    ))
    .bind(&job_id)
    .bind(&tenant_id)
    .bind(&actor_id)
    .fetch_optional(&state.db)
    .await;
    // A failed cancel is a 503, never "already terminal".
    let row = match row {
        Ok(r) => r,
        Err(e) => return crate::services::db_errors::unavailable("the video job", &e),
    };

    // If the row was already terminal (or is not this caller's job, or
    // does not exist), the UPDATE returned 0 rows — fall back to a fresh
    // SELECT so the caller still gets a well-formed JobResponse
    // rather than a 404 they then have to special-case.
    let row = match row {
        Some(r) => r,
        None => {
            let fallback: Result<Option<Row>, sqlx::Error> = sqlx::query_as(concat!(
                r#"
                SELECT id, model_id, status, output_file_id, error, cost_usd,
                       created_at, updated_at
                FROM video_generation_jobs
                WHERE id = $1 AND "#,
                crate::written_by_caller_sql!(),
            ))
            .bind(&job_id)
            .bind(&tenant_id)
            .bind(&actor_id)
            .fetch_optional(&state.db)
            .await;
            match fallback {
                Ok(Some(r)) => r,
                Ok(None) => {
                    return error_resp(StatusCode::NOT_FOUND, &format!("job {job_id} not found"));
                }
                Err(e) => return crate::services::db_errors::unavailable("the video job", &e),
            }
        }
    };

    let payload = JobResponse {
        job_id: row.id,
        status: row.status,
        output_file_id: row.output_file_id,
        error: row.error,
        cost_usd: row.cost_usd,
        brutor_model_id: row.model_id.unwrap_or_default(),
        created_at: row.created_at.to_rfc3339(),
        updated_at: row.updated_at.to_rfc3339(),
    };
    Json(payload).into_response()
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn jsonb_string_array(v: &Option<Value>) -> Option<Vec<String>> {
    let arr = v.as_ref()?.as_array()?;
    let mut out = Vec::with_capacity(arr.len());
    for item in arr {
        out.push(item.as_str()?.to_string());
    }
    Some(out)
}

fn jsonb_int_array(v: &Option<Value>) -> Option<Vec<i64>> {
    let arr = v.as_ref()?.as_array()?;
    let mut out = Vec::with_capacity(arr.len());
    for item in arr {
        out.push(item.as_i64()?);
    }
    Some(out)
}

fn error_resp(status: StatusCode, message: &str) -> Response {
    (status, Json(json!({ "error": { "message": message } }))).into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn jsonb_string_array_handles_resolutions() {
        let v = json!(["720p", "1080p"]);
        assert_eq!(
            jsonb_string_array(&Some(v)),
            Some(vec!["720p".into(), "1080p".into()])
        );
    }

    #[test]
    fn jsonb_int_array_handles_durations() {
        let v = json!([5, 10]);
        assert_eq!(jsonb_int_array(&Some(v)), Some(vec![5, 10]));
    }

    #[test]
    fn jsonb_int_array_rejects_non_int_member() {
        assert_eq!(jsonb_int_array(&Some(json!([5, "ten"]))), None);
    }

    #[test]
    fn jsonb_string_array_rejects_non_string_member() {
        assert_eq!(jsonb_string_array(&Some(json!(["720p", 1080]))), None);
    }
}
