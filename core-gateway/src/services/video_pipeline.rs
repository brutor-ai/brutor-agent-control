// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
//! Video-generation async pipeline.
//!
//! Drives `video_generation_jobs` rows through their lifecycle:
//!
//! ```text
//!   pending  ── submit ──▶  submitted  ── poll ──▶  succeeded
//!                    │                       │
//!                    └── failed              └── failed
//! ```
//!
//! Runs as a single tokio task that ticks every
//! `VIDEO_PIPELINE_TICK_SECS`. Per tick:
//!
//! 1. Pull a small batch of `pending` rows. For each:
//!    - Call the provider's submit API to enqueue.
//!    - On 2xx → set `provider_request_id` and flip to
//!      `submitted`.
//!    - On error → flip to `failed` with the error body.
//! 2. Pull a small batch of `submitted` rows. For each:
//!    - Call the provider's status endpoint.
//!    - On `COMPLETED` → fetch the video bytes, persist via the
//!      Files API, set `output_file_id`, compute cost from the
//!      requested duration, flip to `succeeded`.
//!    - On `FAILED` → flip to `failed` with the error body.
//!    - On in-progress → leave the row untouched; we'll see it
//!      again next tick.
//!
//! Bounded per-tick batches keep DB locks short and prevent a
//! provider outage from running us through every queued row in
//! one go.
//!
//! Provider abstraction: the [`VideoProvider`] trait wraps the
//! two operations we need. fal.ai is the only first-class
//! provider today (`FalProvider`). Replicate / Runway / Pika are
//! staged for follow-ups; the dispatch site uses
//! `model.provider` to pick the adapter.

use std::sync::Arc;
use std::time::Duration;

use bytes::Bytes;
use chrono::{DateTime, Duration as ChronoDuration, Utc};
use serde_json::{Value, json};
use sqlx::PgPool;

use crate::routes::images::parse_provider_error_message;
use crate::services::media_storage::{MediaKind, ObjectKey, Storage};

/// Default cadence — 10 s gives a snappy "in-progress" feel
/// without burning provider quota on a low-traffic dev cluster.
pub const DEFAULT_TICK_SECS: u64 = 10;

/// How many rows we touch per tick per phase. Two phases
/// (submit, poll) so the worst case is `2 × MAX_BATCH` provider
/// calls per tick.
pub const MAX_BATCH: i64 = 25;

/// After this long with no progress, a stuck `submitted` job is
/// flagged `failed` so it stops eating poller cycles. Operators
/// can re-submit by clearing the row.
pub const STUCK_AFTER: ChronoDuration = ChronoDuration::hours(1);

/// Default video retention — same 30 days as the rest of the
/// Files-API media types.
const DEFAULT_RETENTION_DAYS: i64 = 30;

// ---------------------------------------------------------------------------
// Provider abstraction
// ---------------------------------------------------------------------------

/// Outcome of a `submit_job` call.
pub struct SubmitOutcome {
    /// Upstream queue id. Saved into the row so the next tick
    /// can poll status.
    pub provider_request_id: String,
}

/// Outcome of a `poll_status` call.
pub enum StatusOutcome {
    /// Still working — leave the row in `submitted`.
    InProgress,
    /// Done. We've already fetched the bytes; the pipeline
    /// persists them via the Files API.
    Completed {
        bytes: Bytes,
        mime_type: String,
        /// Empirical clip duration if the provider reported it.
        /// Falls back to the requested duration on the row.
        duration_seconds: Option<f64>,
    },
    /// Provider says the job failed.
    Failed { error: String },
}

/// Per-provider implementation. Keep it small — pipeline holds
/// the DB and the row, providers only know how to talk to their
/// own queue API.
#[async_trait::async_trait]
pub trait VideoProvider: Send + Sync {
    async fn submit(&self, ctx: &ProviderCtx<'_>) -> Result<SubmitOutcome, String>;

    async fn poll(
        &self,
        ctx: &ProviderCtx<'_>,
        provider_request_id: &str,
    ) -> Result<StatusOutcome, String>;
}

/// Read-only view of the row + the model being used. Passed to
/// provider adapters so they don't take a copy of the world.
pub struct ProviderCtx<'a> {
    pub http: &'a reqwest::Client,
    pub model_provider: &'a str,
    pub model_name: &'a str,
    pub api_url: Option<&'a str>,
    pub api_key: Option<&'a str>,
    pub prompt: &'a str,
    pub resolution: Option<&'a str>,
    pub duration_seconds: Option<i32>,
    pub timeout_secs: u64,
}

impl ProviderCtx<'_> {
    /// Where this model's video API is: the row's `api_url`, or the known
    /// base of the row's own provider (`providers::upstream_base_for`).
    fn base(&self) -> Result<String, String> {
        crate::services::providers::upstream_base_for(
            self.model_provider,
            self.model_name,
            self.api_url,
            crate::services::providers::ProviderApi::Video,
        )
    }
}

// ---------------------------------------------------------------------------
// Pipeline tick
// ---------------------------------------------------------------------------

/// Run one full tick. Pulled out so unit/integration tests can
/// drive the loop synchronously.
pub async fn run_once(
    db: &PgPool,
    storage: Arc<dyn Storage>,
    http: &reqwest::Client,
    encryption: &crate::services::encryption::EncryptionService,
) {
    if let Err(err) = drive_pending(db, http, encryption).await {
        tracing::warn!("video pipeline: drive_pending failed: {err}");
    }
    if let Err(err) = drive_submitted(db, storage, http, encryption).await {
        tracing::warn!("video pipeline: drive_submitted failed: {err}");
    }
}

/// Spawn the pipeline loop on the runtime. Returns the join
/// handle for completeness; the task only exits at shutdown.
pub fn spawn_loop(
    db: PgPool,
    storage: Arc<dyn Storage>,
    http: reqwest::Client,
    encryption: crate::services::encryption::EncryptionService,
    tick_secs: u64,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        let secs = tick_secs.max(1);
        let mut ticker = tokio::time::interval(Duration::from_secs(secs));
        // Skip the immediate first tick — boot races with other
        // services and we don't want to enqueue load there.
        ticker.tick().await;
        loop {
            ticker.tick().await;
            run_once(&db, storage.clone(), &http, &encryption).await;
        }
    })
}

// ---------------------------------------------------------------------------
// Submit pending rows
// ---------------------------------------------------------------------------

#[derive(sqlx::FromRow)]
struct PendingRow {
    id: String,
    tenant_id: String,
    model_id: Option<String>,
    prompt: String,
    resolution: Option<String>,
    duration_seconds: Option<i32>,
}

async fn drive_pending(
    db: &PgPool,
    http: &reqwest::Client,
    encryption: &crate::services::encryption::EncryptionService,
) -> Result<(), sqlx::Error> {
    let rows: Vec<PendingRow> = sqlx::query_as(
        r#"
        SELECT id, tenant_id, model_id, prompt, resolution, duration_seconds
        FROM video_generation_jobs
        WHERE status = 'pending'
        ORDER BY created_at ASC
        LIMIT $1
        "#,
    )
    .bind(MAX_BATCH)
    .fetch_all(db)
    .await?;

    for row in rows {
        let Some(model_id) = row.model_id.clone() else {
            mark_failed(db, &row.id, "model_id is null").await;
            continue;
        };
        let model = match load_model(db, &row.tenant_id, &model_id, encryption).await {
            Ok(Some(m)) => m,
            Ok(None) => {
                mark_failed(db, &row.id, "model row missing").await;
                continue;
            }
            // A failed read is not a missing model: marking the job
            // failed would be permanent. Left for the next tick.
            Err(e) => {
                crate::services::db_errors::record(&e);
                continue;
            }
        };
        // The model's endpoint must be a permitted destination.
        if let Some(denied) = model.api_url.as_deref().and_then(outbound_denied) {
            mark_failed(db, &row.id, &denied).await;
            continue;
        }
        let provider = pick_provider(&model.provider);
        let provider = match provider {
            Some(p) => p,
            None => {
                mark_failed(
                    db,
                    &row.id,
                    &format!("no video provider implementation for {:?}", model.provider),
                )
                .await;
                continue;
            }
        };

        let api_key = model.decrypted_api_key(encryption);
        let ctx = ProviderCtx {
            http,
            model_provider: &model.provider,
            model_name: &model.model_name,
            api_url: model.api_url.as_deref(),
            api_key: api_key.as_deref(),
            prompt: &row.prompt,
            resolution: row.resolution.as_deref(),
            duration_seconds: row.duration_seconds,
            timeout_secs: model.timeout.unwrap_or(60.0) as u64,
        };

        match provider.submit(&ctx).await {
            Ok(out) => {
                let _ = sqlx::query(
                    r#"
                    UPDATE video_generation_jobs
                    SET status = 'submitted',
                        provider_request_id = $1,
                        updated_at = NOW()
                    WHERE id = $2
                    "#,
                )
                .bind(&out.provider_request_id)
                .bind(&row.id)
                .execute(db)
                .await;
                tracing::info!(
                    job_id = %row.id,
                    provider = %model.provider,
                    "video job submitted"
                );
            }
            Err(err) => {
                tracing::warn!(
                    job_id = %row.id,
                    provider = %model.provider,
                    "video submit failed: {err}"
                );
                mark_failed(db, &row.id, &format!("submit failed: {err}")).await;
            }
        }
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Poll submitted rows
// ---------------------------------------------------------------------------

#[derive(sqlx::FromRow)]
struct SubmittedRow {
    id: String,
    tenant_id: String,
    actor_id: Option<String>,
    chat_id: Option<String>,
    model_id: Option<String>,
    duration_seconds: Option<i32>,
    provider_request_id: Option<String>,
    updated_at: DateTime<Utc>,
}

async fn drive_submitted(
    db: &PgPool,
    storage: Arc<dyn Storage>,
    http: &reqwest::Client,
    encryption: &crate::services::encryption::EncryptionService,
) -> Result<(), sqlx::Error> {
    let rows: Vec<SubmittedRow> = sqlx::query_as(
        r#"
        SELECT id, tenant_id, actor_id, chat_id, model_id, duration_seconds,
               provider_request_id, updated_at
        FROM video_generation_jobs
        WHERE status = 'submitted'
        ORDER BY updated_at ASC
        LIMIT $1
        "#,
    )
    .bind(MAX_BATCH)
    .fetch_all(db)
    .await?;

    let now = Utc::now();
    for row in rows {
        // Stuck-job tripwire — provider is silent for too long.
        if now - row.updated_at > STUCK_AFTER {
            mark_failed(db, &row.id, "no progress for 1h — flagging stuck").await;
            continue;
        }

        let Some(provider_request_id) = row.provider_request_id.clone() else {
            mark_failed(db, &row.id, "submitted without provider_request_id").await;
            continue;
        };
        let Some(model_id) = row.model_id.clone() else {
            mark_failed(db, &row.id, "model_id is null").await;
            continue;
        };
        let model = match load_model(db, &row.tenant_id, &model_id, encryption).await {
            Ok(Some(m)) => m,
            Ok(None) => {
                mark_failed(db, &row.id, "model row missing").await;
                continue;
            }
            // A failed read is not a missing model: marking the job
            // failed would be permanent. Left for the next tick.
            Err(e) => {
                crate::services::db_errors::record(&e);
                continue;
            }
        };
        // The model's endpoint must be a permitted destination.
        if let Some(denied) = model.api_url.as_deref().and_then(outbound_denied) {
            mark_failed(db, &row.id, &denied).await;
            continue;
        }
        let provider = match pick_provider(&model.provider) {
            Some(p) => p,
            None => {
                mark_failed(
                    db,
                    &row.id,
                    &format!("provider {:?} not wired", model.provider),
                )
                .await;
                continue;
            }
        };

        let api_key = model.decrypted_api_key(encryption);
        let ctx = ProviderCtx {
            http,
            model_provider: &model.provider,
            model_name: &model.model_name,
            api_url: model.api_url.as_deref(),
            api_key: api_key.as_deref(),
            prompt: "",
            resolution: None,
            duration_seconds: row.duration_seconds,
            timeout_secs: model.timeout.unwrap_or(60.0) as u64,
        };

        match provider.poll(&ctx, &provider_request_id).await {
            Ok(StatusOutcome::InProgress) => {
                // Clear any prior `error` from a transient poll
                // failure — the upstream is responsive now, so a
                // stale "polling: HTTP 502" string from earlier
                // would confuse the operator.
                let _ = sqlx::query(
                    r#"
                    UPDATE video_generation_jobs
                    SET updated_at = NOW(),
                        error = NULL
                    WHERE id = $1
                    "#,
                )
                .bind(&row.id)
                .execute(db)
                .await;
            }
            Ok(StatusOutcome::Completed {
                bytes,
                mime_type,
                duration_seconds,
            }) => {
                if let Err(err) = finalize_completed(
                    db,
                    storage.clone(),
                    &row,
                    &model,
                    bytes,
                    &mime_type,
                    duration_seconds,
                )
                .await
                {
                    mark_failed(db, &row.id, &format!("finalize: {err}")).await;
                }
            }
            Ok(StatusOutcome::Failed { error }) => {
                mark_failed(db, &row.id, &error).await;
            }
            Err(err) => {
                tracing::warn!(
                    job_id = %row.id,
                    "video poll failed (will retry): {err}"
                );
                // Surface the last upstream error to the row's
                // `error` column so `GET /v1/proxy/llm/video/jobs/{id}`
                // exposes it while the row stays at status='submitted'
                // and continues retrying. Without this the portal
                // (and operators) saw an opaque "still submitted"
                // loop for up to STUCK_AFTER (1h) when the upstream
                // was actually erroring on every poll. Truncate to
                // 500 chars so a verbose stack trace doesn't bloat
                // every poll response.
                let truncated: String = err.chars().take(500).collect();
                let _ = sqlx::query(
                    r#"
                    UPDATE video_generation_jobs
                    SET updated_at = NOW(),
                        error = $1
                    WHERE id = $2
                    "#,
                )
                .bind(format!("polling: {truncated}"))
                .bind(&row.id)
                .execute(db)
                .await;
            }
        }
    }
    Ok(())
}

async fn finalize_completed(
    db: &PgPool,
    storage: Arc<dyn Storage>,
    row: &SubmittedRow,
    model: &ModelLite,
    bytes: Bytes,
    mime_type: &str,
    reported_duration: Option<f64>,
) -> Result<(), String> {
    let now = Utc::now();
    let file_id = format!("file_{}", ulid::Ulid::new());
    let ext = ext_for_mime(mime_type);
    let key = ObjectKey::build(&row.tenant_id, MediaKind::Video, &file_id, &ext, now)
        .map_err(|e| format!("object key: {e}"))?;

    storage
        .put(key.as_str(), bytes.clone(), mime_type)
        .await
        .map_err(|e| format!("storage put: {e}"))?;

    let size_bytes = bytes.len() as i64;
    let content_hash = sha256_hex(&bytes);
    let res = sqlx::query(
        r#"
        INSERT INTO media_files (
            id, tenant_id, kind, mime_type, size_bytes, content_hash,
            object_key, chat_id, model_id, actor_id,
            created_at, expires_at
        )
        VALUES ($1,$2,'video',$3,$4,$5,$6,$7,$8,$9,$10,$11)
        "#,
    )
    .bind(&file_id)
    .bind(&row.tenant_id)
    .bind(mime_type)
    .bind(size_bytes)
    .bind(&content_hash)
    .bind(key.as_str())
    .bind(&row.chat_id)
    .bind(&model.id)
    .bind(&row.actor_id)
    .bind(now)
    .bind(now + ChronoDuration::days(DEFAULT_RETENTION_DAYS))
    .execute(db)
    .await;
    if let Err(err) = res {
        // Best-effort cleanup of the orphan object.
        let _ = storage.delete(key.as_str()).await;
        return Err(format!("media_files insert: {err}"));
    }

    let duration = reported_duration
        .or_else(|| row.duration_seconds.map(|d| d as f64))
        .unwrap_or(0.0);
    let cost = model.cost_per_video_second.unwrap_or(0.0) * duration;

    let _ = sqlx::query(
        r#"
        UPDATE video_generation_jobs
        SET status = 'succeeded',
            output_file_id = $1,
            cost_usd = $2,
            updated_at = NOW(),
            completed_at = NOW()
        WHERE id = $3
        "#,
    )
    .bind(&file_id)
    .bind(cost)
    .bind(&row.id)
    .execute(db)
    .await;

    tracing::info!(
        job_id = %row.id,
        file_id = %file_id,
        duration_secs = duration,
        cost_usd = cost,
        "video job succeeded"
    );
    Ok(())
}

async fn mark_failed(db: &PgPool, job_id: &str, error: &str) {
    let _ = sqlx::query(
        r#"
        UPDATE video_generation_jobs
        SET status = 'failed',
            error = $1,
            updated_at = NOW(),
            completed_at = NOW()
        WHERE id = $2
        "#,
    )
    .bind(error)
    .bind(job_id)
    .execute(db)
    .await;
}

// ---------------------------------------------------------------------------
// Model loader (the pipeline doesn't share the route's
// `model_cache` because we run on a separate task; we just read
// straight from the DB on each pass).
// ---------------------------------------------------------------------------

/// Compile-time guard against the "ship the ciphertext" bug.
///
/// The `api_key_ciphertext` column is Fernet-at-rest. Earlier
/// versions of `load_model` returned a raw `String` field named
/// `api_key`, and callers that forgot to decrypt it (or where the
/// decrypt step was removed during refactoring) silently shipped
/// ciphertext as the upstream `Authorization: Key …` header — fal
/// rejected those calls as *"Cannot access application 'fal-ai/
/// luma-dream-machine'. Authentication is required to access this
/// application."*, which looks like operator misconfiguration but
/// is really a Brutor decrypt bug.
///
/// This struct now uses two defences:
///
///   1. **Field is named `api_key_ciphertext`** — anyone reading
///      the code sees "this is encrypted" before they touch the
///      value.
///   2. **The only public accessor is `decrypted_api_key()`** —
///      which forces an `&EncryptionService` argument. Forgetting
///      to decrypt is a compile error, not a runtime surprise.
///
/// The previous `decrypt_optional` call inside `load_model` was a
/// runtime safety net. This typestate makes that safety net
/// load-bearing — removing it from the read path no longer
/// compiles.
#[derive(sqlx::FromRow)]
struct ModelLite {
    id: String,
    provider: String,
    model_name: String,
    api_url: Option<String>,
    /// Fernet-encrypted at rest. Use `decrypted_api_key()` —
    /// never read this field directly.
    #[sqlx(rename = "api_key")]
    api_key_ciphertext: Option<String>,
    timeout: Option<f64>,
    cost_per_video_second: Option<f64>,
}

impl ModelLite {
    /// Decrypt the api_key for upstream forwarding. The presence
    /// of this method (and the absence of any other accessor on
    /// `api_key_ciphertext`) is what prevents the "ship the
    /// ciphertext" regression — call sites have no way to obtain
    /// a plaintext bearer without going through here, and an
    /// `&EncryptionService` is non-trivial to fabricate, so a
    /// future contributor can't reintroduce the bug "by accident".
    fn decrypted_api_key(
        &self,
        enc: &crate::services::encryption::EncryptionService,
    ) -> Option<String> {
        enc.decrypt_optional(
            self.api_key_ciphertext.as_deref(),
            "video_pipeline:llm_model.api_key",
        )
    }
}

/// `Some(reason)` when `url` is not a permitted outbound destination.
/// Applied to the model's endpoint and to every URL a provider hands back
/// (fal's `response_url`, the rendered clip's URL): a tenant-configured or
/// compromised upstream could answer with `http://169.254.169.254/…`, which
/// the DNS resolver never sees.
fn outbound_denied(url: &str) -> Option<String> {
    let url = url.trim();
    if url.is_empty() {
        return None;
    }
    crate::services::outbound_policy::check_url(url)
        .err()
        .map(|denied| {
            tracing::warn!(host = %denied.host, reason = denied.reason, "video URL refused by outbound policy");
            denied.to_string()
        })
}

async fn load_model(
    db: &PgPool,
    tenant_id: &str,
    model_id: &str,
    _encryption: &crate::services::encryption::EncryptionService,
) -> Result<Option<ModelLite>, sqlx::Error> {
    // The provider slug is not `llm_model.provider`: providers are in
    // The provider slug is not `llm_model.provider`: providers are in a
    // (`provider_id`). Most code paths now stamp the slug from
    // `ProviderCache` after a slug-less SELECT, but the video pipeline
    // — being a background poller, not a request-path consumer — never
    // got rewired and was still selecting the dropped column. The
    // sqlx column-not-found error short-circuited as
    // `fetch_optional → Ok(None)`, which the caller mistook for a
    // missing row and surfaced as "model row missing" (job-side
    // failure status, no upstream call). Joining `llm_providers` here
    // is a one-line fix that restores the slug without inventing a
    // new ProviderCache plumbing path; we only need it for
    // `pick_provider`'s string match.
    sqlx::query_as::<_, ModelLite>(
        r#"
        SELECT m.id, p.key AS provider, m.model_name, m.api_url, m.api_key,
               m.timeout, m.cost_per_video_second
        FROM llm_model m
        JOIN llm_providers p ON p.id = m.provider_id
        WHERE m.id = $1 AND m.tenant_id = $2
        "#,
    )
    .bind(model_id)
    .bind(tenant_id)
    .fetch_optional(db)
    .await
    // No decrypt step here — the row is intentionally returned
    // with the ciphertext intact. Decryption happens at the use
    // site via `ModelLite::decrypted_api_key`, which forces the
    // caller to thread an `&EncryptionService`. See the doc
    // comment on `ModelLite` for why this is structured as
    // type-system-enforced rather than a here-be-dragons runtime
    // call inside the loader.
}

// ---------------------------------------------------------------------------
// Provider dispatch
// ---------------------------------------------------------------------------

fn pick_provider(name: &str) -> Option<Box<dyn VideoProvider>> {
    match name.to_lowercase().as_str() {
        "fal" | "fal_ai" => Some(Box::new(fal::FalProvider)),
        // OpenAI's Sora 2 / Sora 2 Pro use the OpenAI base URL and
        // the tenant's existing OpenAI API key — see
        // `openai_multimodal_models_init.py` for the catalogue
        // entries. Submit + poll lifecycle is OpenAI's `/v1/videos`
        // queue, completely different shape from fal's queue.
        "openai" => Some(Box::new(openai_sora::SoraProvider)),
        _ => None,
    }
}

/// Map a friendly resolution string ("720p", "1080p", "480p") to
/// the WxH form OpenAI's `size` parameter expects on `/v1/videos`.
/// Passes through anything that's already WxH or unknown so
/// operators with a custom seed value don't get blocked here —
/// Sora itself does the final validation and surfaces a clear
/// error when the size isn't supported by the chosen tier.
fn openai_size_for(resolution: Option<&str>) -> Option<String> {
    let r = resolution?.trim().to_lowercase();
    Some(match r.as_str() {
        "480p" => "854x480".to_string(),
        "720p" => "1280x720".to_string(),
        "1080p" => "1920x1080".to_string(),
        _ => r,
    })
}

// ---------------------------------------------------------------------------
// fal.ai provider
// ---------------------------------------------------------------------------

mod fal {
    use super::*;

    pub struct FalProvider;

    #[async_trait::async_trait]
    impl VideoProvider for FalProvider {
        async fn submit(&self, ctx: &ProviderCtx<'_>) -> Result<SubmitOutcome, String> {
            let api_key = ctx
                .api_key
                .ok_or_else(|| "fal model has no api_key configured".to_string())?;
            // model_name is e.g. "fal-ai/luma-dream-machine/ray-2-flash".
            //
            // FAL app slugs ARE namespaced under `fal-ai/...` (it's the
            // canonical first path segment, not metadata). Stripping
            // it leaves an unknown slug and FAL returns
            // `Application not found`.
            //
            // The legacy `fal_ai/` prefix (underscore) IS
            // metadata and should still be stripped:
            // stored rows are normalised but this defends
            // against any operator-typed slug that still carries it.
            let path = ctx
                .model_name
                .strip_prefix("fal_ai/")
                .unwrap_or(ctx.model_name);
            let base = ctx.base()?;
            let base = base.as_str();
            let url = crate::services::providers::join_api_url(base, &format!("/{path}"));

            let mut body = json!({ "prompt": ctx.prompt });
            if let Some(res) = ctx.resolution {
                body["resolution"] = json!(res);
            }
            if let Some(dur) = ctx.duration_seconds {
                // Per-model duration encoding. Fal's older endpoints
                // (e.g. the deprecated luma-dream-machine, kling)
                // accept a bare integer; the newer Luma Ray 2 family
                // (`ray-2-flash`, `ray-2`) requires a stringified
                // suffix form (`"5s"` / `"9s"`) and 422s otherwise:
                //
                //   {"type":"literal_error","loc":["body","duration"],
                //    "msg":"Input should be '5s' or '9s'"}
                //
                // We branch on the model path so adding new
                // suffix-style models is one-line: just append to
                // the predicate.
                let needs_suffix = path.contains("ray-2") // ray-2-flash, ray-2
                    || path.contains("ray2");
                body["duration"] = if needs_suffix {
                    json!(format!("{dur}s"))
                } else {
                    json!(dur)
                };
            }

            let resp = ctx
                .http
                .post(&url)
                .header("Authorization", format!("Key {api_key}"))
                .json(&body)
                .timeout(Duration::from_secs(ctx.timeout_secs.max(30)))
                .send()
                .await
                .map_err(|e| format!("submit send: {e}"))?;
            let status = resp.status();
            let text = resp.text().await.map_err(|e| format!("read: {e}"))?;
            if !status.is_success() {
                // Surface the inner provider message verbatim
                // when fal gives us one (safety / quota / auth /
                // unsupported-resolution). Same extraction used
                // by the image route — fal returns the same
                // ``{"error": {"message": "..."}}`` envelope as
                // OpenAI for the most part.
                if let Some(msg) = parse_provider_error_message(&text) {
                    return Err(msg);
                }
                return Err(format!("submit {status}: {text}"));
            }
            let parsed: Value =
                serde_json::from_str(&text).map_err(|e| format!("parse submit: {e}"))?;
            // Log the full submit URL + response body so operators
            // can see what URL fal handed us back. fal's recent
            // URL migrations have shifted some models to nested
            // sub-routes, and a stale `response_url` from submit
            // is the difference between "result fetch works" and
            // "deprecation 500 every poll" — without this log the
            // root cause is invisible.
            tracing::info!(
                target: "fal.submit",
                model = ctx.model_name,
                submit_url = %url,
                submit_body = %text,
                "fal video submit OK"
            );
            let request_id = parsed
                .get("request_id")
                .and_then(|v| v.as_str())
                .ok_or_else(|| "submit response missing request_id".to_string())?;
            // Fal's queue API returns `response_url` / `status_url`
            // alongside `request_id`; those URLs are the canonical
            // forward-compatible endpoints for this specific request.
            // Older code constructed `{base}/{model_path}/requests/{id}`
            // ourselves, but fal has migrated several models (e.g.
            // luma-dream-machine) to new URL paths and the rebuilt
            // URL hits an `internal_server_error` deprecation page:
            //   "This endpoint is deprecated and no longer supported."
            // Persist the full `response_url` instead — `poll` below
            // uses it verbatim, and `<response_url>/status` is the
            // documented status endpoint shape that fal guarantees.
            //
            // Back-compat: if fal omits `response_url` (older deploys,
            // self-hosted forks), fall through to the request_id and
            // `poll` reconstructs the legacy URL.
            let provider_id = parsed
                .get("response_url")
                .and_then(|v| v.as_str())
                .map(String::from)
                .unwrap_or_else(|| request_id.to_string());
            Ok(SubmitOutcome {
                provider_request_id: provider_id,
            })
        }

        async fn poll(
            &self,
            ctx: &ProviderCtx<'_>,
            provider_request_id: &str,
        ) -> Result<StatusOutcome, String> {
            let api_key = ctx
                .api_key
                .ok_or_else(|| "fal model has no api_key configured".to_string())?;
            // `provider_request_id` may carry one of two shapes,
            // depending on whether the row was submitted before or
            // after the response_url-capture fix:
            //   - URL form: `https://queue.fal.run/.../requests/{id}`
            //     (use verbatim; status URL = `{url}/status`)
            //   - bare id  : `abc123` (legacy; reconstruct with the
            //     model_path prefix — works for models that haven't
            //     been migrated and keeps in-flight pre-fix rows
            //     completing).
            let (response_url, status_url) = if provider_request_id.starts_with("http://")
                || provider_request_id.starts_with("https://")
            {
                let response = provider_request_id.to_string();
                let status = format!("{response}/status");
                (response, status)
            } else {
                // Same FAL slug rule as `submit`: only the
                // legacy `fal_ai/` prefix is metadata.
                // `fal-ai/` is the real namespace and must stay.
                let path = ctx
                    .model_name
                    .strip_prefix("fal_ai/")
                    .unwrap_or(ctx.model_name);
                let base = ctx.base()?;
                let base = base.as_str();
                let response = crate::services::providers::join_api_url(
                    base,
                    &format!("/{path}/requests/{provider_request_id}"),
                );
                let status = crate::services::providers::join_api_url(
                    base,
                    &format!("/{path}/requests/{provider_request_id}/status"),
                );
                (response, status)
            };

            if let Some(denied) = outbound_denied(&status_url) {
                return Err(denied);
            }
            let resp = ctx
                .http
                .get(&status_url)
                .header("Authorization", format!("Key {api_key}"))
                .timeout(Duration::from_secs(ctx.timeout_secs.max(15)))
                .send()
                .await
                .map_err(|e| format!("status send: {e}"))?;
            let status_code = resp.status();
            let text = resp.text().await.map_err(|e| format!("read: {e}"))?;
            if !status_code.is_success() {
                return Err(format!("status {status_code}: {text}"));
            }
            let parsed: Value =
                serde_json::from_str(&text).map_err(|e| format!("parse status: {e}"))?;
            let phase = parsed
                .get("status")
                .and_then(|v| v.as_str())
                .unwrap_or("UNKNOWN");
            match phase {
                "IN_QUEUE" | "IN_PROGRESS" => Ok(StatusOutcome::InProgress),
                "COMPLETED" => {
                    // Use the response URL we captured at submit
                    // time (or rebuilt above for legacy rows). The
                    // earlier code re-derived it here from the model
                    // path, which broke for models fal had migrated
                    // to a new URL pattern (`internal_server_error`
                    // deprecation response, surfacing in the proxy
                    // logs as repeated "video poll failed (will
                    // retry): result fetch: ..." warnings).
                    // Robust result extraction. Three strategies in
                    // order of preference:
                    //
                    // 1. **Inline** — newer fal video models return
                    //    the result body directly inside the status
                    //    response under `response` (or `result`)
                    //    when status is `COMPLETED`. Using it
                    //    sidesteps the separate result-fetch HTTP
                    //    round-trip entirely, AND avoids fal's
                    //    deprecation warning for models that have
                    //    migrated their result endpoint.
                    //
                    // 2. **response_url** — the canonical pattern
                    //    we already capture at submit time. Works
                    //    for the bulk of fal's catalogue.
                    //
                    // 3. **fallback=true** — if (2) responds with
                    //    fal's deprecation envelope (literal text:
                    //    "endpoint is deprecated"), retry with
                    //    `?fallback=true` per the hint fal includes
                    //    in that error: "You can still enable
                    //    fallbacks to use this endpoint." Some
                    //    providers leave the legacy URL reachable
                    //    behind this flag during the migration
                    //    window.
                    let result: Value = if let Some(inline) = parsed
                        .get("response")
                        .or_else(|| parsed.get("result"))
                        .filter(|v| v.is_object())
                    {
                        inline.clone()
                    } else {
                        let resp = ctx
                            .http
                            .get(&response_url)
                            .header("Authorization", format!("Key {api_key}"))
                            .timeout(Duration::from_secs(ctx.timeout_secs.max(60)))
                            .send()
                            .await
                            .map_err(|e| format!("result send: {e}"))?;
                        let primary_status = resp.status();
                        let primary_body = resp.text().await.unwrap_or_default();
                        let needs_fallback = !primary_status.is_success()
                            && (primary_body.contains("endpoint is deprecated")
                                || primary_body.contains("enable fallbacks"));
                        if primary_status.is_success() {
                            serde_json::from_str(&primary_body)
                                .map_err(|e| format!("parse result: {e}"))?
                        } else if needs_fallback {
                            let fallback_url = if response_url.contains('?') {
                                format!("{response_url}&fallback=true")
                            } else {
                                format!("{response_url}?fallback=true")
                            };
                            let resp2 = ctx
                                .http
                                .get(&fallback_url)
                                .header("Authorization", format!("Key {api_key}"))
                                .header("X-Fal-Fallback", "true")
                                .timeout(Duration::from_secs(ctx.timeout_secs.max(60)))
                                .send()
                                .await
                                .map_err(|e| format!("result fallback send: {e}"))?;
                            if !resp2.status().is_success() {
                                let body = resp2.text().await.unwrap_or_default();
                                return Err(format!("result fetch (incl. fallback): {body}"));
                            }
                            resp2
                                .json()
                                .await
                                .map_err(|e| format!("parse fallback result: {e}"))?
                        } else {
                            return Err(format!("result fetch: {primary_body}"));
                        }
                    };

                    // Most fal video models surface the clip at
                    // `video.url` (Luma) or top-level `url`. Fall
                    // back to the first nested string-url we find.
                    let video_url = result
                        .get("video")
                        .and_then(|v| v.get("url"))
                        .and_then(|v| v.as_str())
                        .or_else(|| result.get("url").and_then(|v| v.as_str()))
                        .ok_or_else(|| "result has no video URL".to_string())?
                        .to_string();
                    if let Some(denied) = outbound_denied(&video_url) {
                        return Err(denied);
                    }
                    let duration = result
                        .get("video")
                        .and_then(|v| v.get("duration"))
                        .and_then(|v| v.as_f64())
                        .or_else(|| result.get("duration_seconds").and_then(|v| v.as_f64()));

                    // The finished video is a public CDN URL: fetched
                    // without the provider key, so it may redirect.
                    let bytes_resp = crate::services::outbound_policy::public_fetch_client()
                        .get(&video_url)
                        .timeout(Duration::from_secs(ctx.timeout_secs.max(120)))
                        .send()
                        .await
                        .map_err(|e| format!("video fetch send: {e}"))?;
                    if !bytes_resp.status().is_success() {
                        return Err(format!("video fetch HTTP {}", bytes_resp.status()));
                    }
                    let mime_type = bytes_resp
                        .headers()
                        .get(reqwest::header::CONTENT_TYPE)
                        .and_then(|v| v.to_str().ok())
                        .unwrap_or("video/mp4")
                        .to_string();
                    let bytes = bytes_resp
                        .bytes()
                        .await
                        .map_err(|e| format!("video read: {e}"))?;
                    Ok(StatusOutcome::Completed {
                        bytes,
                        mime_type,
                        duration_seconds: duration,
                    })
                }
                "FAILED" | "CANCELLED" => {
                    let err = parsed
                        .get("error")
                        .and_then(|v| v.as_str())
                        .unwrap_or("provider reported failure");
                    Ok(StatusOutcome::Failed {
                        error: err.to_string(),
                    })
                }
                other => Err(format!("unknown fal status {other:?}: {text}")),
            }
        }
    }
}

// ---------------------------------------------------------------------------
// OpenAI Sora provider
// ---------------------------------------------------------------------------
//
// Wire shape (Q2 2026, see https://platform.openai.com/docs/api-reference/videos):
//
//   POST   /v1/videos
//          { "model": "sora-2", "prompt": "...", "size": "1280x720",
//            "seconds": "8" }
//          → 200 { "id": "video_…", "object": "video", "status": "queued",
//                  "model": "sora-2", "created_at": …, … }
//
//   GET    /v1/videos/{id}
//          → 200 { "id": …, "status": "queued"|"in_progress"|"completed"
//                  |"failed", "error": { "message": "…" } | null,
//                  "duration": <seconds>, "size": "WxH", … }
//
//   GET    /v1/videos/{id}/content?variant=video
//          → 200, body = mp4 bytes
//
// `Authorization: Bearer <OPENAI_API_KEY>` on every call. The tenant's
// existing OpenAI key is reused (the seed entry doesn't carry its own).
mod openai_sora {
    use super::*;

    pub struct SoraProvider;

    // The base is OpenAI's (`providers::upstream_base_for`) unless the
    // model row's `api_url` points at a private mirror or a recording
    // proxy.

    #[async_trait::async_trait]
    impl VideoProvider for SoraProvider {
        async fn submit(&self, ctx: &ProviderCtx<'_>) -> Result<SubmitOutcome, String> {
            let api_key = ctx.api_key.ok_or_else(|| {
                "openai sora model has no api_key configured \
                     (set OPENAI_API_KEY on the tenant or override on \
                     the model row)"
                    .to_string()
            })?;
            let base = ctx.base()?;
            let base = base.as_str();
            let url = crate::services::providers::join_api_url(base, "/videos");

            // Build the request body. `size` and `seconds` are
            // string-typed on the wire (OpenAI's choice); `seconds`
            // is omitted when the caller didn't pin a duration so
            // Sora can default per its own tier rules.
            let mut body = json!({
                "model": ctx.model_name,
                "prompt": ctx.prompt,
            });
            if let Some(size) = openai_size_for(ctx.resolution) {
                body["size"] = json!(size);
            }
            if let Some(dur) = ctx.duration_seconds {
                body["seconds"] = json!(dur.to_string());
            }

            let resp = ctx
                .http
                .post(&url)
                .header("Authorization", format!("Bearer {api_key}"))
                .json(&body)
                .timeout(Duration::from_secs(ctx.timeout_secs.max(30)))
                .send()
                .await
                .map_err(|e| format!("submit send: {e}"))?;
            let status = resp.status();
            let text = resp.text().await.map_err(|e| format!("read: {e}"))?;
            if !status.is_success() {
                // Surface the inner provider message verbatim when
                // OpenAI gives us one. Same helper the image and fal
                // paths use; OpenAI's error envelope is the
                // canonical `{"error":{"message":"…","code":"…"}}`
                // shape that `parse_provider_error_message` is
                // designed for.
                if let Some(msg) = parse_provider_error_message(&text) {
                    return Err(msg);
                }
                return Err(format!("submit {status}: {text}"));
            }

            let parsed: Value =
                serde_json::from_str(&text).map_err(|e| format!("parse submit: {e}"))?;
            let video_id = parsed
                .get("id")
                .and_then(|v| v.as_str())
                .ok_or_else(|| "submit response missing id".to_string())?;
            Ok(SubmitOutcome {
                provider_request_id: video_id.to_string(),
            })
        }

        async fn poll(
            &self,
            ctx: &ProviderCtx<'_>,
            provider_request_id: &str,
        ) -> Result<StatusOutcome, String> {
            let api_key = ctx
                .api_key
                .ok_or_else(|| "openai sora model has no api_key configured".to_string())?;
            let base = ctx.base()?;
            let base = base.as_str();
            let status_url = crate::services::providers::join_api_url(
                base,
                &format!("/videos/{provider_request_id}"),
            );

            let resp = ctx
                .http
                .get(&status_url)
                .header("Authorization", format!("Bearer {api_key}"))
                .timeout(Duration::from_secs(ctx.timeout_secs.max(15)))
                .send()
                .await
                .map_err(|e| format!("status send: {e}"))?;
            let status_code = resp.status();
            let text = resp.text().await.map_err(|e| format!("read: {e}"))?;
            if !status_code.is_success() {
                return Err(format!("status {status_code}: {text}"));
            }
            let parsed: Value =
                serde_json::from_str(&text).map_err(|e| format!("parse status: {e}"))?;
            let phase = parsed
                .get("status")
                .and_then(|v| v.as_str())
                .unwrap_or("unknown");

            match phase {
                "queued" | "in_progress" => Ok(StatusOutcome::InProgress),
                "completed" => {
                    // Download the rendered mp4 bytes. Sora exposes
                    // `/v1/videos/{id}/content` with a `variant`
                    // query param — `video` is the rendered clip,
                    // `thumbnail` is a still. We only want the
                    // clip; thumbnails are a separate feature we
                    // can plumb later if needed.
                    let content_url = crate::services::providers::join_api_url(
                        base,
                        &format!("/videos/{provider_request_id}/content?variant=video"),
                    );
                    let bytes_resp = ctx
                        .http
                        .get(&content_url)
                        .header("Authorization", format!("Bearer {api_key}"))
                        .timeout(Duration::from_secs(ctx.timeout_secs.max(120)))
                        .send()
                        .await
                        .map_err(|e| format!("content send: {e}"))?;
                    if !bytes_resp.status().is_success() {
                        let body = bytes_resp.text().await.unwrap_or_default();
                        return Err(format!("content fetch: {body}"));
                    }
                    let mime_type = bytes_resp
                        .headers()
                        .get(reqwest::header::CONTENT_TYPE)
                        .and_then(|v| v.to_str().ok())
                        .unwrap_or("video/mp4")
                        .to_string();
                    let bytes = bytes_resp
                        .bytes()
                        .await
                        .map_err(|e| format!("content read: {e}"))?;

                    // Sora's status payload reports the rendered
                    // duration as a string-typed `seconds` field
                    // (matching the submit param's wire type).
                    // Try numeric, then string, then fall back to
                    // the requested duration.
                    let duration = parsed
                        .get("seconds")
                        .and_then(|v| v.as_f64())
                        .or_else(|| {
                            parsed
                                .get("seconds")
                                .and_then(|v| v.as_str())
                                .and_then(|s| s.parse::<f64>().ok())
                        })
                        .or_else(|| parsed.get("duration").and_then(|v| v.as_f64()));

                    Ok(StatusOutcome::Completed {
                        bytes,
                        mime_type,
                        duration_seconds: duration,
                    })
                }
                "failed" => {
                    let err = parsed
                        .get("error")
                        .and_then(|e| e.get("message"))
                        .and_then(|v| v.as_str())
                        .or_else(|| parsed.get("error").and_then(|v| v.as_str()))
                        .unwrap_or("provider reported failure");
                    Ok(StatusOutcome::Failed {
                        error: err.to_string(),
                    })
                }
                other => Err(format!("unknown openai sora status {other:?}: {text}")),
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn ext_for_mime(mime: &str) -> String {
    match mime.to_ascii_lowercase().split(';').next().unwrap_or("") {
        "video/mp4" => "mp4",
        "video/webm" => "webm",
        "video/quicktime" => "mov",
        _ => "bin",
    }
    .to_string()
}

fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::Digest;
    let digest = sha2::Sha256::digest(bytes);
    hex::encode(digest)
}

#[cfg(test)]
mod tests {
    use super::*;

    // ---- ModelLite typestate regression coverage. Pins the
    //     "you can only get the plaintext via decrypted_api_key"
    //     guarantee so a future contributor can't add a getter
    //     that returns the ciphertext directly.

    fn make_model(api_key_ciphertext: Option<String>) -> ModelLite {
        ModelLite {
            id: "llm-test".into(),
            provider: "fal_ai".into(),
            model_name: "fal_ai/fal-ai/luma-dream-machine".into(),
            api_url: None,
            api_key_ciphertext,
            timeout: None,
            cost_per_video_second: None,
        }
    }

    #[test]
    fn decrypted_api_key_round_trips_real_ciphertext() {
        let enc = crate::services::encryption::EncryptionService::new("video-pipeline-test-key");
        let plaintext = "fal-real-key-uuid:secret-half";
        let ciphertext = enc.encrypt(plaintext).unwrap();
        let model = make_model(Some(ciphertext));

        let recovered = model.decrypted_api_key(&enc);
        assert_eq!(recovered.as_deref(), Some(plaintext));
    }

    #[test]
    fn decrypted_api_key_returns_none_for_plaintext_in_encrypted_column() {
        // Regression — the exact bug the user hit. If an operator
        // (or an old pre-encryption seed) put plaintext into the
        // api_key column, `decrypted_api_key` MUST return None
        // rather than passing the plaintext through. The submit
        // call site then sees `None` and short-circuits to a
        // clear "no api_key configured" error rather than
        // confusing the upstream provider with garbage.
        let enc = crate::services::encryption::EncryptionService::new("any-key");
        let model = make_model(Some("plaintext-fal-key".into()));
        assert!(model.decrypted_api_key(&enc).is_none());
    }

    #[test]
    fn decrypted_api_key_passes_none_through() {
        let enc = crate::services::encryption::EncryptionService::new("any-key");
        let model = make_model(None);
        assert!(model.decrypted_api_key(&enc).is_none());
    }

    /// Compile-fence test. If anyone ever adds a public getter
    /// that returns ciphertext (e.g. `pub fn api_key(&self) ->
    /// Option<&str>`), this comment + the field's `pub(self)`
    /// visibility flag means the change has to be deliberate.
    /// We can't statically assert "no method named X exists" in
    /// stable Rust, so this test just *uses* the safe accessor —
    /// any change that breaks the safe accessor breaks this
    /// test, and the discussion in the PR review catches the
    /// regression. Belt-and-suspenders to the typestate.
    #[test]
    fn modellite_safe_accessor_is_the_only_documented_path() {
        let enc = crate::services::encryption::EncryptionService::new("test");
        let model = make_model(Some(enc.encrypt("k").unwrap()));
        // The compiler accepts this:
        let _ok: Option<String> = model.decrypted_api_key(&enc);
        // The compiler MUST reject any attempt to read the
        // ciphertext field directly from outside the module —
        // `api_key_ciphertext` has module-private visibility
        // (no `pub`). Anyone removing the `decrypted_api_key`
        // method or changing the field name is performing a
        // visible refactor, not a silent bug.
    }

    #[test]
    fn ext_for_mime_handles_known_video_types() {
        assert_eq!(ext_for_mime("video/mp4"), "mp4");
        assert_eq!(ext_for_mime("video/mp4; codecs=avc1"), "mp4");
        assert_eq!(ext_for_mime("video/webm"), "webm");
        assert_eq!(ext_for_mime("video/quicktime"), "mov");
        assert_eq!(ext_for_mime("text/plain"), "bin");
    }

    #[test]
    fn pick_provider_returns_some_for_fal() {
        assert!(pick_provider("fal_ai").is_some());
        assert!(pick_provider("FAL").is_some());
    }

    #[test]
    fn pick_provider_returns_some_for_openai() {
        // Sora 2 / Sora 2 Pro both register under provider="openai".
        assert!(pick_provider("openai").is_some());
        assert!(pick_provider("OpenAI").is_some());
    }

    #[test]
    fn pick_provider_returns_none_for_unknown() {
        assert!(pick_provider("anthropic").is_none());
        assert!(pick_provider("").is_none());
    }

    #[test]
    fn openai_size_maps_friendly_names_to_wxh() {
        assert_eq!(openai_size_for(Some("720p")).as_deref(), Some("1280x720"));
        assert_eq!(openai_size_for(Some("1080p")).as_deref(), Some("1920x1080"));
        assert_eq!(openai_size_for(Some("480p")).as_deref(), Some("854x480"));
    }

    #[test]
    fn openai_size_passes_through_wxh_form() {
        // Unknown names pass through verbatim — Sora itself
        // validates and returns a clear error if the size isn't
        // supported by the chosen tier.
        assert_eq!(
            openai_size_for(Some("1920x1080")).as_deref(),
            Some("1920x1080"),
        );
        assert_eq!(openai_size_for(Some("custom")).as_deref(), Some("custom"),);
    }

    #[test]
    fn openai_size_handles_none_and_whitespace() {
        assert!(openai_size_for(None).is_none());
        // Trim + lowercase normalises operator-typo'd values.
        assert_eq!(
            openai_size_for(Some("  720P  ")).as_deref(),
            Some("1280x720")
        );
    }

    #[test]
    fn sha256_hex_is_stable() {
        assert_eq!(sha256_hex(b"hi"), sha256_hex(b"hi"));
        assert_eq!(sha256_hex(b"hi").len(), 64);
    }
}
