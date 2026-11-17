// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
//! Files API.
//!
//! Persistent media (generated images, uploaded audio for STT,
//! generated video) is stored in object storage and addressed by a
//! stable `brutor_file_id`. Chat messages reference the id; the
//! portal calls these endpoints to mint short-lived signed
//! download URLs at render time.
//!
//! ## Endpoints
//!
//! - `POST   /v1/proxy/files`             — multipart upload.
//!   Body parts: `file` (the bytes), `kind` (image | audio_speech |
//!   audio_transcription_input | video | document | other),
//!   plus optional `chat_id`, `model_id`, `display_name`.
//!
//! - `GET    /v1/proxy/files/{id}`        — metadata (no bytes).
//!
//! - `GET    /v1/proxy/files/{id}/url`    — fresh signed URL.
//!   The TTL is `BRUTOR_MEDIA_SIGNED_URL_TTL_SECS` (default 900s).
//!
//! - `DELETE /v1/proxy/files/{id}`        — soft-delete. The
//!   underlying object lives on for 7 days (the GC sweep window)
//!   so an operator can restore by clearing `deleted_at`.
//!
//! ## Tenant isolation
//!
//! Every handler resolves `tenant_id` and the caller's actor id from the
//! authenticated caller (`routes::request::extract_media_caller`: an API key
//! of the tenant, a workload identity, or a portal session where the Portal
//! is composed). A file is read and deleted by the actor that wrote it (the
//! end user, or the resource group a group key or an agent acts in); for
//! any other caller it does not exist. Access to a row whose `tenant_id`
//! doesn't match is rejected. This is the only place cross-tenant leakage can
//! occur (object-key paths embed the tenant prefix; bucket policy
//! denies cross-tenant reads in S3 — but those are belt-and-
//! braces, the row-level check here is the primary control).

use axum::{
    Json, Router,
    body::Bytes as BodyBytes,
    extract::{Multipart, Path, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use bytes::Bytes;
use chrono::{Duration as ChronoDuration, Utc};
use serde::Serialize;
use serde_json::json;
use std::time::Duration;

use crate::middleware::tenant::TenantContext;
use crate::services::SharedState;
use crate::services::media_storage::{KeyError, MediaKind, ObjectKey, StorageError};

/// Default retention for new uploads.
/// Per-tenant overrides come from `tenants.media_retention_days`
/// when that column lands; until then this is the fixed default.
const DEFAULT_RETENTION_DAYS: i64 = 30;

/// Hard cap on a single upload. Below the per-modality limits
/// (Whisper-1: 25 MB, video: ~250 MB) so uploads don't make us
/// allocate gigabytes of buffer. Multipart bodies above this
/// return 413 before any allocation.
const MAX_UPLOAD_BYTES: usize = 256 * 1024 * 1024;

pub fn router() -> Router<SharedState> {
    Router::new()
        .route("/", post(upload_file))
        .route("/{file_id}", get(get_metadata).delete(delete_file))
        .route("/{file_id}/url", get(get_signed_url))
        .route("/{file_id}/content", get(get_content))
}

// ---------------------------------------------------------------------------
// Response shapes
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
struct FileMetadata {
    id: String,
    tenant_id: String,
    kind: String,
    mime_type: String,
    size_bytes: i64,
    content_hash: Option<String>,
    chat_id: Option<String>,
    model_id: Option<String>,
    display_name: Option<String>,
    created_at: String,
    expires_at: String,
    deleted_at: Option<String>,
    access_count: i32,
}

#[derive(Debug, Serialize)]
struct SignedUrlResponse {
    url: String,
    expires_in_secs: u64,
    file_id: String,
    mime_type: String,
}

// ---------------------------------------------------------------------------
// POST /v1/proxy/files — multipart upload
// ---------------------------------------------------------------------------

async fn upload_file(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    mut multipart: Multipart,
) -> Response {
    let client_ip = client_addr.ip();

    let (tenant_id, user_id, _username) = match crate::routes::request::extract_media_caller(
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

    // ------------------------------------------------------------------
    // Parse the multipart body. Required parts: `file`, `kind`.
    // Optional: `chat_id`, `model_id`, `display_name`.
    // ------------------------------------------------------------------
    let mut bytes_opt: Option<Bytes> = None;
    let mut mime_type = String::from("application/octet-stream");
    let mut kind_opt: Option<MediaKind> = None;
    let mut chat_id: Option<String> = None;
    let mut model_id: Option<String> = None;
    let mut display_name: Option<String> = None;
    let mut original_filename: Option<String> = None;

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
                if let Some(ct) = field.content_type() {
                    mime_type = ct.to_string();
                }
                if let Some(fname) = field.file_name() {
                    original_filename = Some(fname.to_string());
                }
                let body = match field.bytes().await {
                    Ok(b) => b,
                    Err(err) => {
                        return error_resp(
                            StatusCode::PAYLOAD_TOO_LARGE,
                            &format!("read body: {err}"),
                        );
                    }
                };
                if body.len() > MAX_UPLOAD_BYTES {
                    return error_resp(
                        StatusCode::PAYLOAD_TOO_LARGE,
                        &format!(
                            "upload {} bytes exceeds {} byte cap",
                            body.len(),
                            MAX_UPLOAD_BYTES
                        ),
                    );
                }
                bytes_opt = Some(body);
            }
            "kind" => {
                let raw = match field.text().await {
                    Ok(s) => s,
                    Err(_) => {
                        return error_resp(StatusCode::BAD_REQUEST, "kind: read text failed");
                    }
                };
                kind_opt = MediaKind::from_str_strict(raw.trim());
                if kind_opt.is_none() {
                    return error_resp(
                        StatusCode::BAD_REQUEST,
                        &format!("kind {raw:?} is not a valid MediaKind"),
                    );
                }
            }
            "chat_id" => chat_id = field.text().await.ok().filter(|v| !v.is_empty()),
            "model_id" => model_id = field.text().await.ok().filter(|v| !v.is_empty()),
            "display_name" => display_name = field.text().await.ok().filter(|v| !v.is_empty()),
            _ => {
                // Tolerate unknown parts. The pipeline ignores them
                // rather than 400-ing — older clients shouldn't break
                // when we add new optional metadata fields.
            }
        }
    }

    let bytes = match bytes_opt {
        Some(b) if !b.is_empty() => b,
        _ => return error_resp(StatusCode::BAD_REQUEST, "missing or empty 'file' part"),
    };
    let kind = match kind_opt {
        Some(k) => k,
        None => return error_resp(StatusCode::BAD_REQUEST, "missing 'kind' part"),
    };

    // ------------------------------------------------------------------
    // Quota enforcement — synchronous before we touch storage.
    // We compare the *new* total (sum of size_bytes for non-deleted
    // rows + this upload) against the tenant's limit.
    // ------------------------------------------------------------------
    if let Err(resp) = enforce_quota(&state, &tenant_id, bytes.len() as i64).await {
        return resp;
    }

    // ------------------------------------------------------------------
    // Compose the row.
    // ------------------------------------------------------------------
    let file_id = format!("file_{}", ulid::Ulid::new());
    let ext = pick_extension(&mime_type, original_filename.as_deref());

    let object_key = match ObjectKey::build(&tenant_id, kind, &file_id, &ext, Utc::now()) {
        Ok(k) => k,
        Err(KeyError::InvalidTenantId(msg)) => {
            return error_resp(
                StatusCode::BAD_REQUEST,
                &format!("tenant id rejected by storage layout: {msg}"),
            );
        }
        Err(err) => {
            return error_resp(
                StatusCode::INTERNAL_SERVER_ERROR,
                &format!("object key build: {err}"),
            );
        }
    };

    let content_hash = sha256_hex(&bytes);
    let size_bytes = bytes.len() as i64;
    let now = Utc::now();
    let expires_at = now + ChronoDuration::days(DEFAULT_RETENTION_DAYS);
    let actor_id = user_id.clone();

    // ------------------------------------------------------------------
    // Write bytes first, then row. If the DB insert fails, the
    // bytes leak — the GC sweep won't see them (no row), so we
    // leave a `tracing::error!` breadcrumb. This is the lesser
    // evil vs. a row pointing at bytes that aren't yet there.
    // ------------------------------------------------------------------
    if let Err(err) = state
        .media_storage
        .put(object_key.as_str(), bytes, &mime_type)
        .await
    {
        return classify_storage_error_for_write(err, object_key.as_str());
    }

    let res = sqlx::query(
        r#"
        INSERT INTO media_files (
            id, tenant_id, kind, mime_type, size_bytes, content_hash,
            object_key, chat_id, model_id, actor_id, display_name,
            created_at, expires_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
        "#,
    )
    .bind(&file_id)
    .bind(&tenant_id)
    .bind(kind.as_str())
    .bind(&mime_type)
    .bind(size_bytes)
    .bind(&content_hash)
    .bind(object_key.as_str())
    .bind(&chat_id)
    .bind(&model_id)
    .bind(&actor_id)
    .bind(&display_name)
    .bind(now)
    .bind(expires_at)
    .execute(&state.db)
    .await;

    if let Err(err) = res {
        tracing::error!(
            tenant_id = %tenant_id,
            file_id = %file_id,
            object_key = %object_key.as_str(),
            "media_files INSERT failed after storage write — orphaned object will be GC'd: {err}"
        );
        // Best-effort cleanup of the orphan. Failure here is just
        // logged; the GC sweep's "delete object missing row" pass
        // catches the rest.
        let _ = state.media_storage.delete(object_key.as_str()).await;
        return error_resp(
            StatusCode::INTERNAL_SERVER_ERROR,
            "persisting media metadata failed",
        );
    }

    let payload = FileMetadata {
        id: file_id,
        tenant_id,
        kind: kind.as_str().to_string(),
        mime_type,
        size_bytes,
        content_hash: Some(content_hash),
        chat_id,
        model_id,
        display_name,
        created_at: now.to_rfc3339(),
        expires_at: expires_at.to_rfc3339(),
        deleted_at: None,
        access_count: 0,
    };
    (StatusCode::CREATED, Json(payload)).into_response()
}

// ---------------------------------------------------------------------------
// GET /v1/proxy/files/{id} — metadata
// ---------------------------------------------------------------------------

async fn get_metadata(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Path(file_id): Path<String>,
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

    match load_file_row(&state, &tenant_id, &actor_id, &file_id).await {
        Ok(Some(row)) => Json(row.into_response_payload()).into_response(),
        Ok(None) => not_found(&file_id),
        Err(resp) => resp,
    }
}

// ---------------------------------------------------------------------------
// GET /v1/proxy/files/{id}/url — fresh signed URL
// ---------------------------------------------------------------------------

async fn get_signed_url(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Path(file_id): Path<String>,
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

    let row = match load_file_row(&state, &tenant_id, &actor_id, &file_id).await {
        Ok(Some(row)) => row,
        Ok(None) => return not_found(&file_id),
        Err(resp) => return resp,
    };

    if row.deleted_at.is_some() {
        return error_resp(StatusCode::GONE, "file is deleted");
    }
    if row.is_expired_at(Utc::now()) {
        return error_resp(StatusCode::GONE, "file retention expired");
    }

    let ttl_secs = state.config.media_signed_url_ttl_secs.max(60);
    let raw_url = match state
        .media_storage
        .presigned_get_url(&row.object_key, Duration::from_secs(ttl_secs))
        .await
    {
        Ok(u) => u,
        Err(err) => {
            tracing::warn!(
                tenant_id = %tenant_id,
                file_id = %file_id,
                "presigned_get_url failed: {err}"
            );
            return error_resp(
                StatusCode::INTERNAL_SERVER_ERROR,
                "could not mint download URL",
            );
        }
    };

    // Rewrite non-fetchable schemes to a same-origin proxy
    // streaming endpoint. The dev `InMemoryStorage` returns
    // `memory://...` sentinels, which a browser can't navigate
    // to — so a `<video src=...>` or anchor `<a href=...>` would
    // silently fail to load. The `/content` route serves the
    // bytes directly with normal portal auth headers, so the
    // portal's `ensureBlobUrl` flow Just Works in dev as well as
    // against S3 / R2 / etc. in production.
    //
    // We treat anything that ISN'T `https://` or `http://` as
    // not-browser-fetchable (covers `memory://`, future custom
    // schemes, and accidental returns of a bare object key).
    let url = if raw_url.starts_with("https://") || raw_url.starts_with("http://") {
        raw_url
    } else {
        format!("/v1/proxy/files/{file_id}/content")
    };

    // Best-effort audit bump. Failure to update the counter must
    // not block URL issuance — the caller is waiting on bytes.
    let _ = sqlx::query(concat!(
        r#"
        UPDATE media_files
        SET access_count = access_count + 1,
            last_accessed_at = NOW()
        WHERE id = $1 AND "#,
        crate::written_by_caller_sql!(),
        r#"
        "#,
    ))
    .bind(&file_id)
    .bind(&tenant_id)
    .bind(&actor_id)
    .execute(&state.db)
    .await;

    Json(SignedUrlResponse {
        url,
        expires_in_secs: ttl_secs,
        file_id,
        mime_type: row.mime_type,
    })
    .into_response()
}

// ---------------------------------------------------------------------------
// GET /v1/proxy/files/{id}/content — stream the bytes directly
// ---------------------------------------------------------------------------
//
// Same auth, tenant and owner scoping as `/url`, but the response IS the
// file body. Rationale: object-storage backends like S3 / R2
// return presigned `https://` URLs the browser can fetch
// directly, but the dev `InMemoryStorage` returns `memory://`
// sentinels that aren't browser-fetchable. Rather than have the
// portal special-case dev vs prod URL shapes, we expose this
// uniform same-origin streaming endpoint and have `/url` rewrite
// non-http schemes to it. In production with S3, this route is
// rarely hit (the presigned URL is browser-fetchable); in dev
// it's the load-bearing path that makes generated images / videos
// actually playable.

async fn get_content(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Path(file_id): Path<String>,
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

    let row = match load_file_row(&state, &tenant_id, &actor_id, &file_id).await {
        Ok(Some(row)) => row,
        Ok(None) => return not_found(&file_id),
        Err(resp) => return resp,
    };
    if row.deleted_at.is_some() {
        return error_resp(StatusCode::GONE, "file is deleted");
    }
    if row.is_expired_at(Utc::now()) {
        return error_resp(StatusCode::GONE, "file retention expired");
    }

    let bytes = match state.media_storage.get(&row.object_key).await {
        Ok(b) => b,
        Err(StorageError::NotFound { .. }) => {
            return error_resp(StatusCode::GONE, "underlying object missing");
        }
        Err(err) => {
            tracing::warn!(
                tenant_id = %tenant_id,
                file_id = %file_id,
                "media_storage.get failed: {err}"
            );
            return error_resp(
                StatusCode::INTERNAL_SERVER_ERROR,
                "could not read file bytes",
            );
        }
    };

    // Best-effort access bump — same pattern as `/url`. Fail
    // silently so a hot DB doesn't block the response.
    let _ = sqlx::query(concat!(
        r#"
        UPDATE media_files
        SET access_count = access_count + 1,
            last_accessed_at = NOW()
        WHERE id = $1 AND "#,
        crate::written_by_caller_sql!(),
        r#"
        "#,
    ))
    .bind(&file_id)
    .bind(&tenant_id)
    .bind(&actor_id)
    .execute(&state.db)
    .await;

    let mut response_headers = axum::http::HeaderMap::new();
    response_headers.insert(
        axum::http::header::CONTENT_TYPE,
        row.mime_type
            .parse()
            .unwrap_or_else(|_| axum::http::HeaderValue::from_static("application/octet-stream")),
    );
    // `Content-Length` lets the browser show download progress
    // and supports `<video>` element seeking. Bytes are already
    // in memory at this point so the cost is just stamping the
    // header.
    response_headers.insert(axum::http::header::CONTENT_LENGTH, bytes.len().into());
    // Friendly download filename when present. Browsers
    // ignore the disposition header for inline `<video>` /
    // `<img>` rendering, but honour it for `Save As` / the
    // existing portal Download button.
    if let Some(name) = row.display_name.as_deref() {
        if !name.is_empty() {
            // Sanitise to avoid header injection — strip CR/LF and
            // wrap in quotes per RFC 6266.
            let safe = name.replace(['\r', '\n', '"'], "_");
            if let Ok(v) = format!("inline; filename=\"{safe}\"").parse() {
                response_headers.insert(axum::http::header::CONTENT_DISPOSITION, v);
            }
        }
    }

    (StatusCode::OK, response_headers, bytes).into_response()
}

// ---------------------------------------------------------------------------
// DELETE /v1/proxy/files/{id} — soft-delete
// ---------------------------------------------------------------------------

async fn delete_file(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Path(file_id): Path<String>,
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

    let updated = sqlx::query(concat!(
        r#"
        UPDATE media_files
        SET deleted_at = NOW()
        WHERE id = $1
          AND "#,
        crate::written_by_caller_sql!(),
        r#"
          AND deleted_at IS NULL
        "#,
    ))
    .bind(&file_id)
    .bind(&tenant_id)
    .bind(&actor_id)
    .execute(&state.db)
    .await;

    match updated {
        Ok(res) if res.rows_affected() == 1 => {
            (StatusCode::NO_CONTENT, BodyBytes::new()).into_response()
        }
        Ok(_) => {
            // The row doesn't exist, is somebody else's, or is already
            // deleted: one answer for all three.
            // Idempotency: treat 'already deleted' as 204 — the
            // caller's intent has been achieved.
            (StatusCode::NO_CONTENT, BodyBytes::new()).into_response()
        }
        Err(err) => {
            tracing::error!(
                tenant_id = %tenant_id,
                file_id = %file_id,
                "media_files soft-delete failed: {err}"
            );
            error_resp(StatusCode::INTERNAL_SERVER_ERROR, "delete failed")
        }
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

#[derive(sqlx::FromRow)]
struct FileRow {
    id: String,
    tenant_id: String,
    kind: String,
    mime_type: String,
    size_bytes: i64,
    content_hash: Option<String>,
    object_key: String,
    chat_id: Option<String>,
    model_id: Option<String>,
    display_name: Option<String>,
    created_at: chrono::DateTime<Utc>,
    expires_at: chrono::DateTime<Utc>,
    deleted_at: Option<chrono::DateTime<Utc>>,
    access_count: i32,
}

impl FileRow {
    fn into_response_payload(self) -> FileMetadata {
        FileMetadata {
            id: self.id,
            tenant_id: self.tenant_id,
            kind: self.kind,
            mime_type: self.mime_type,
            size_bytes: self.size_bytes,
            content_hash: self.content_hash,
            chat_id: self.chat_id,
            model_id: self.model_id,
            display_name: self.display_name,
            created_at: self.created_at.to_rfc3339(),
            expires_at: self.expires_at.to_rfc3339(),
            deleted_at: self.deleted_at.map(|t| t.to_rfc3339()),
            access_count: self.access_count,
        }
    }

    fn is_expired_at(&self, now: chrono::DateTime<Utc>) -> bool {
        now >= self.expires_at
    }
}

/// The file `file_id` of `tenant_id` when it is `actor_id`'s: the caller that
/// wrote it. A file records one writer (`media_files.actor_id`, what
/// `extract_media_caller` names the caller): the end user, or the resource
/// group a key without an end user or an agent acts in. `None` for a file
/// that does not exist and for one that is another caller's, which get the
/// same answer; a row that records no writer is nobody's.
async fn load_file_row(
    state: &SharedState,
    tenant_id: &str,
    actor_id: &str,
    file_id: &str,
) -> Result<Option<FileRow>, Response> {
    let row: Option<FileRow> = sqlx::query_as(concat!(
        r#"
        SELECT id, tenant_id, kind, mime_type, size_bytes, content_hash,
               object_key, chat_id, model_id, display_name,
               created_at, expires_at, deleted_at, access_count
        FROM media_files
        WHERE id = $1 AND "#,
        crate::written_by_caller_sql!(),
        r#"
        "#,
    ))
    .bind(file_id)
    .bind(tenant_id)
    .bind(actor_id)
    .fetch_optional(&state.db)
    .await
    .map_err(|err| {
        tracing::error!(
            tenant_id = %tenant_id,
            file_id = %file_id,
            "media_files SELECT failed: {err}"
        );
        error_resp(StatusCode::INTERNAL_SERVER_ERROR, "lookup failed")
    })?;
    Ok(row)
}

async fn enforce_quota(
    state: &SharedState,
    tenant_id: &str,
    new_bytes: i64,
) -> Result<(), Response> {
    let quota: Option<(Option<i64>,)> =
        sqlx::query_as("SELECT media_storage_quota_bytes FROM tenants WHERE id = $1")
            .bind(tenant_id)
            .fetch_optional(&state.db)
            .await
            .map_err(|err| {
                tracing::error!(
                    tenant_id = %tenant_id,
                    "media quota lookup failed: {err}"
                );
                error_resp(StatusCode::INTERNAL_SERVER_ERROR, "quota lookup failed")
            })?;

    // The tenant's own quota (NULL = unlimited), under the installation's
    // ceiling when it has one.
    let limit =
        match crate::services::installation_limits::media_quota(state, quota.and_then(|(q,)| q)) {
            Some(n) => n,
            None => return Ok(()),
        };

    let used: (Option<i64>,) = sqlx::query_as(
        r#"
        SELECT COALESCE(SUM(size_bytes), 0)::BIGINT
        FROM media_files
        WHERE tenant_id = $1 AND deleted_at IS NULL
        "#,
    )
    .bind(tenant_id)
    .fetch_one(&state.db)
    .await
    .map_err(|err| {
        tracing::error!(
            tenant_id = %tenant_id,
            "media quota usage SUM failed: {err}"
        );
        error_resp(StatusCode::INTERNAL_SERVER_ERROR, "quota lookup failed")
    })?;
    let used = used.0.unwrap_or(0);

    if used + new_bytes > limit {
        return Err(error_resp(
            StatusCode::PAYLOAD_TOO_LARGE,
            &format!(
                "media storage quota exceeded: used={} + new={} > limit={}",
                used, new_bytes, limit
            ),
        ));
    }
    Ok(())
}

fn classify_storage_error_for_write(err: StorageError, key: &str) -> Response {
    match err {
        StorageError::Invalid(msg) => {
            error_resp(StatusCode::BAD_REQUEST, &format!("storage rejected: {msg}"))
        }
        // The filesystem store's size limit: the message names the setting.
        StorageError::Full(msg) => error_resp(
            StatusCode::INSUFFICIENT_STORAGE,
            &format!("media store is full: {msg}"),
        ),
        StorageError::PermissionDenied(_) => {
            tracing::error!(key = %key, "storage denied write — bucket policy misconfig");
            error_resp(
                StatusCode::INTERNAL_SERVER_ERROR,
                "storage permission denied",
            )
        }
        other => {
            tracing::error!(key = %key, "storage write failed: {other}");
            error_resp(StatusCode::BAD_GATEWAY, "storage write failed")
        }
    }
}

fn pick_extension(mime: &str, filename: Option<&str>) -> String {
    // Prefer the original filename's extension when supplied
    // (preserves "user uploaded foo.HEIC" through the round-trip).
    if let Some(name) = filename {
        if let Some((_stem, ext)) = name.rsplit_once('.') {
            let ext = ext.to_ascii_lowercase();
            // ObjectKey only accepts [a-z0-9]{1,8}, so we validate
            // the extension before adopting it.
            if ext.len() <= 8
                && !ext.is_empty()
                && ext
                    .chars()
                    .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
            {
                return ext;
            }
        }
    }
    match mime.to_ascii_lowercase().as_str() {
        "image/png" => "png",
        "image/jpeg" | "image/jpg" => "jpg",
        "image/webp" => "webp",
        "image/gif" => "gif",
        "audio/mpeg" | "audio/mp3" => "mp3",
        "audio/wav" | "audio/x-wav" => "wav",
        "audio/ogg" => "ogg",
        "audio/webm" => "webm",
        "audio/flac" => "flac",
        "video/mp4" => "mp4",
        "video/webm" => "webm",
        "application/pdf" => "pdf",
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

fn not_found(file_id: &str) -> Response {
    error_resp(StatusCode::NOT_FOUND, &format!("file {file_id} not found"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pick_extension_prefers_filename_when_valid() {
        assert_eq!(pick_extension("image/png", Some("logo.HEIC")), "heic");
        assert_eq!(pick_extension("image/png", Some("noext")), "png");
        assert_eq!(pick_extension("image/jpeg", Some("a.jpg")), "jpg");
    }

    #[test]
    fn pick_extension_rejects_filename_with_bad_chars() {
        // dot-only suffix and non-alphanumeric chars fall through
        assert_eq!(pick_extension("image/png", Some("file.")), "png");
        assert_eq!(pick_extension("image/png", Some("file.weird-one")), "png");
    }

    #[test]
    fn pick_extension_handles_unknown_mime() {
        assert_eq!(pick_extension("application/x-weird", None), "bin");
    }

    #[test]
    fn pick_extension_lowercases_filename_ext() {
        assert_eq!(pick_extension("image/png", Some("a.PNG")), "png");
    }

    #[test]
    fn sha256_hex_is_deterministic() {
        assert_eq!(
            sha256_hex(b"hello"),
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        );
    }
}
