// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Tenant-scoped CRUD over `a2a_push_notification_configs`.
//!
//! Implements the storage layer for the four v1.0 methods:
//!
//!   * `CreateTaskPushNotificationConfig`
//!   * `GetTaskPushNotificationConfig`
//!   * `ListTaskPushNotificationConfigs`
//!   * `DeleteTaskPushNotificationConfig`
//!
//! The on-wire `TaskPushNotificationConfig` object is stored verbatim
//! in the `config_json` JSONB column. Identity lives on dedicated
//! columns (id, tenant_id, task_id) for fast per-task lookups without
//! having to parse JSONB.
//!
//! Semantics:
//!
//!   * The server (us) assigns the config `id` on create. If the
//!     caller supplied one we replace it — spec leaves this as a
//!     server-assigned handle.
//!   * Delete is idempotent: deleting an already-missing config
//!     returns success. Callers can't distinguish "never existed"
//!     from "just deleted" without a dedicated read, which matches
//!     the spec's information-hiding stance (§3.3.2).
//!   * Pagination uses the same opaque base64url(JSON) cursor
//!     scheme as [`super::task_store`] so operators see one token
//!     format across the whole v1.0 surface.

use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD as B64URL;
use serde::{Deserialize, Serialize};
use sqlx::{FromRow, PgPool};

use super::TaskPushNotificationConfig;

/// Spec §ListTaskPushNotificationConfigsRequest upper bound.
pub const LIST_PUSH_CONFIGS_MAX_PAGE_SIZE: i32 = 100;
/// Default page size when unspecified.
pub const LIST_PUSH_CONFIGS_DEFAULT_PAGE_SIZE: i32 = 50;

// =============================================================================
// Errors
// =============================================================================

#[derive(Debug)]
pub enum PushStoreError {
    Db(sqlx::Error),
    Decode(String),
    NotFound,
    BadPageToken(String),
}

impl std::fmt::Display for PushStoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PushStoreError::Db(e) => write!(f, "push store DB error: {e}"),
            PushStoreError::Decode(s) => write!(f, "push config decode failed: {s}"),
            PushStoreError::NotFound => write!(f, "push config not found"),
            PushStoreError::BadPageToken(s) => write!(f, "bad page token: {s}"),
        }
    }
}

impl std::error::Error for PushStoreError {}

impl From<sqlx::Error> for PushStoreError {
    fn from(e: sqlx::Error) -> Self {
        if matches!(e, sqlx::Error::RowNotFound) {
            PushStoreError::NotFound
        } else {
            PushStoreError::Db(e)
        }
    }
}

// =============================================================================
// DB row
// =============================================================================

#[derive(Debug, Clone, FromRow)]
struct PushRow {
    #[allow(dead_code)]
    id: String,
    #[allow(dead_code)]
    tenant_id: String,
    #[allow(dead_code)]
    task_id: String,
    config_json: serde_json::Value,
}

impl PushRow {
    fn into_config(self) -> Result<TaskPushNotificationConfig, PushStoreError> {
        serde_json::from_value(self.config_json).map_err(|e| PushStoreError::Decode(e.to_string()))
    }
}

// =============================================================================
// List query parameters
// =============================================================================

pub struct ListQuery {
    pub page_size: Option<i32>,
    pub page_token: Option<String>,
}

pub struct ListResult {
    pub configs: Vec<TaskPushNotificationConfig>,
    pub next_page_token: String,
}

// =============================================================================
// Core operations
// =============================================================================

/// Create a new push-notification config under a task. The server
/// assigns the `id`; the caller's value (if any) is overwritten.
pub async fn create(
    db: &PgPool,
    tenant_id: &str,
    task_id: &str,
    mut config: TaskPushNotificationConfig,
) -> Result<TaskPushNotificationConfig, PushStoreError> {
    // Server-assigned id. Even if the caller supplied one we replace
    // it so every row carries a unique server-generated handle.
    let id = ulid::Ulid::new().to_string();
    config.id = Some(id.clone());
    config.task_id = Some(task_id.to_string());
    let config_json =
        serde_json::to_value(&config).map_err(|e| PushStoreError::Decode(e.to_string()))?;

    sqlx::query(
        r#"INSERT INTO a2a_push_notification_configs
           (id, tenant_id, task_id, config_json)
           VALUES ($1, $2, $3, $4)"#,
    )
    .bind(&id)
    .bind(tenant_id)
    .bind(task_id)
    .bind(&config_json)
    .execute(db)
    .await?;
    Ok(config)
}

/// Fetch a push config by id, scoped to its task + tenant.
pub async fn get(
    db: &PgPool,
    tenant_id: &str,
    task_id: &str,
    config_id: &str,
) -> Result<TaskPushNotificationConfig, PushStoreError> {
    let row = sqlx::query_as::<_, PushRow>(
        r#"SELECT id, tenant_id, task_id, config_json
           FROM a2a_push_notification_configs
           WHERE id = $1 AND tenant_id = $2 AND task_id = $3"#,
    )
    .bind(config_id)
    .bind(tenant_id)
    .bind(task_id)
    .fetch_optional(db)
    .await?;
    row.ok_or(PushStoreError::NotFound)?.into_config()
}

/// List push configs for a task. Stable cursor ordering: `(created_at DESC, id ASC)`.
pub async fn list(
    db: &PgPool,
    tenant_id: &str,
    task_id: &str,
    query: &ListQuery,
) -> Result<ListResult, PushStoreError> {
    let page_size = query
        .page_size
        .unwrap_or(LIST_PUSH_CONFIGS_DEFAULT_PAGE_SIZE)
        .clamp(1, LIST_PUSH_CONFIGS_MAX_PAGE_SIZE);
    let cursor = match &query.page_token {
        Some(t) if !t.is_empty() => Some(decode_page_token(t)?),
        _ => None,
    };

    let mut qb = sqlx::QueryBuilder::<sqlx::Postgres>::new(
        r#"SELECT id, tenant_id, task_id, config_json
           FROM a2a_push_notification_configs
           WHERE tenant_id = "#,
    );
    qb.push_bind(tenant_id);
    qb.push(" AND task_id = ");
    qb.push_bind(task_id);
    if let Some(c) = &cursor {
        qb.push(" AND (created_at, id) < (");
        qb.push_bind(c.created_at);
        qb.push(", ");
        qb.push_bind(&c.id);
        qb.push(")");
    }
    qb.push(" ORDER BY created_at DESC, id ASC LIMIT ");
    qb.push_bind((page_size + 1) as i64);

    let rows: Vec<PushRow> = qb.build_query_as().fetch_all(db).await?;
    let has_next = rows.len() > page_size as usize;
    let visible = if has_next {
        &rows[..page_size as usize]
    } else {
        &rows[..]
    };

    let next_token = if has_next {
        // We don't have the created_at on the row struct; re-fetch
        // just for the last row's created_at. Cheap single-row lookup.
        let last = &visible[visible.len() - 1];
        let created_at: chrono::DateTime<chrono::Utc> = sqlx::query_scalar(
            "SELECT created_at FROM a2a_push_notification_configs WHERE id = $1",
        )
        .bind(&last.id)
        .fetch_one(db)
        .await?;
        encode_page_token(&Cursor {
            created_at,
            id: last.id.clone(),
        })
    } else {
        String::new()
    };

    let mut out = Vec::with_capacity(visible.len());
    for row in visible {
        out.push(row.clone().into_config()?);
    }
    Ok(ListResult {
        configs: out,
        next_page_token: next_token,
    })
}

/// Delete a push config. Idempotent — returns Ok even if no row
/// matched (the spec doesn't require we distinguish "didn't exist"
/// from "just deleted").
pub async fn delete(
    db: &PgPool,
    tenant_id: &str,
    task_id: &str,
    config_id: &str,
) -> Result<(), PushStoreError> {
    sqlx::query(
        r#"DELETE FROM a2a_push_notification_configs
           WHERE id = $1 AND tenant_id = $2 AND task_id = $3"#,
    )
    .bind(config_id)
    .bind(tenant_id)
    .bind(task_id)
    .execute(db)
    .await?;
    Ok(())
}

// =============================================================================
// Cursor helpers (same layout as task_store for operator consistency)
// =============================================================================

#[derive(Debug, Serialize, Deserialize)]
struct Cursor {
    created_at: chrono::DateTime<chrono::Utc>,
    id: String,
}

fn encode_page_token(c: &Cursor) -> String {
    let json = serde_json::to_vec(c).expect("cursor encodes");
    B64URL.encode(&json)
}

fn decode_page_token(s: &str) -> Result<Cursor, PushStoreError> {
    let bytes = B64URL
        .decode(s.as_bytes())
        .map_err(|e| PushStoreError::BadPageToken(format!("base64url: {e}")))?;
    serde_json::from_slice(&bytes).map_err(|e| PushStoreError::BadPageToken(format!("json: {e}")))
}

// =============================================================================
// Unit tests — pure helpers only
// =============================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::{TimeZone, Utc};

    #[test]
    fn page_token_round_trip() {
        let c = Cursor {
            created_at: Utc.with_ymd_and_hms(2026, 4, 24, 10, 0, 0).unwrap(),
            id: "config-1".into(),
        };
        let token = encode_page_token(&c);
        let back = decode_page_token(&token).unwrap();
        assert_eq!(back.id, c.id);
        assert_eq!(back.created_at, c.created_at);
    }

    #[test]
    fn page_token_rejects_garbage() {
        let e = decode_page_token("!!!").unwrap_err();
        assert!(matches!(e, PushStoreError::BadPageToken(_)));
    }
}
