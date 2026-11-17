// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Tenant-scoped CRUD over `a2a_tasks`.
//!
//! The A2A v1.0 HTTP route handlers hit this store on every request.
//! Design goals, in priority order:
//!
//!   1. **Tenant isolation** — every query is keyed on `tenant_id`
//!      (and, where relevant, `resource_group_id`). There is no
//!      read path that skips the tenant scope.
//!   2. **Wire fidelity** — the full [`Task`] object is stored in the
//!      `task_json` JSONB column. Reads round-trip through the Rust
//!      v1.0 types so wire output and stored form are bit-equal.
//!   3. **Pagination that keeps pace** — `ListTasks` uses
//!      `(status_timestamp DESC, id ASC)` as a stable key and an
//!      opaque `page_token` that encodes the cursor. No `OFFSET`;
//!      the table can grow without plan degradation.
//!
//! A task's lifecycle flows:
//!
//!   1. `create` — inserts a row with state = SUBMITTED, status
//!      timestamp = now, task_json = initial Task object.
//!   2. `update_status` / `append_artifact` / `append_history` —
//!      mutates `task_json` + re-denormalises `state`/
//!      `status_timestamp`.
//!   3. `get`, `list`, `cancel` — reads. Cancel writes state =
//!      CANCELED iff the current state is non-terminal.
//!
//! Cancellation is spec-idempotent: hitting `cancel` on a task
//! that's already cancelled returns the cancelled task with no
//! error; hitting it on a task that's already in another terminal
//! state returns [`TaskStoreError::NotCancelable`].

use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD as B64URL;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use sqlx::types::chrono as sqlx_chrono;
use sqlx::{FromRow, PgPool};

use super::{Artifact, Message, Task, TaskState, TaskStatus};

/// Maximum page size `ListTasks` will honour. Matches the spec's
/// upper bound (§ListTasksRequest, `page_size` field).
pub const LIST_TASKS_MAX_PAGE_SIZE: i32 = 100;

/// Default page size when the caller supplies no value. Matches the
/// spec (§ListTasksRequest: "If unspecified, at most 50 tasks will be returned").
pub const LIST_TASKS_DEFAULT_PAGE_SIZE: i32 = 50;

// =============================================================================
// Errors
// =============================================================================

#[derive(Debug)]
pub enum TaskStoreError {
    /// Underlying SQL error (network / integrity / etc).
    Db(sqlx::Error),
    /// Task did not deserialize back into a v1.0 shape — almost always
    /// a bug where a writer side-stepped the typed API. Surfaces as a
    /// 500 to callers.
    Decode(String),
    /// Task was found but didn't belong to the requested tenant — we
    /// conflate this with "not found" on the wire (spec §3.3.2 says
    /// servers MUST NOT reveal existence to unauthorized callers),
    /// but keep the distinction in logs.
    NotFound,
    /// Cancel requested on a task already in a terminal state other
    /// than CANCELED.
    NotCancelable { state: TaskState },
    /// Supplied `page_token` did not parse. The route handler maps
    /// this to `INVALID_ARGUMENT`.
    BadPageToken(String),
}

impl std::fmt::Display for TaskStoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            TaskStoreError::Db(e) => write!(f, "task store DB error: {e}"),
            TaskStoreError::Decode(s) => write!(f, "task row decode failed: {s}"),
            TaskStoreError::NotFound => write!(f, "task not found"),
            TaskStoreError::NotCancelable { state } => {
                write!(f, "task in state {state:?} is not cancelable")
            }
            TaskStoreError::BadPageToken(s) => write!(f, "bad page token: {s}"),
        }
    }
}

impl std::error::Error for TaskStoreError {}

impl From<sqlx::Error> for TaskStoreError {
    fn from(e: sqlx::Error) -> Self {
        if matches!(e, sqlx::Error::RowNotFound) {
            TaskStoreError::NotFound
        } else {
            TaskStoreError::Db(e)
        }
    }
}

// =============================================================================
// DB row — what we read from PostgreSQL
// =============================================================================

#[derive(Debug, Clone, FromRow)]
struct TaskRow {
    #[allow(dead_code)]
    id: String,
    #[allow(dead_code)]
    tenant_id: String,
    #[allow(dead_code)]
    resource_group_id: Option<String>,
    #[allow(dead_code)]
    context_id: Option<String>,
    #[allow(dead_code)]
    state: String,
    status_timestamp: sqlx_chrono::DateTime<sqlx_chrono::Utc>,
    task_json: serde_json::Value,
}

impl TaskRow {
    fn into_task(self) -> Result<Task, TaskStoreError> {
        serde_json::from_value(self.task_json).map_err(|e| TaskStoreError::Decode(e.to_string()))
    }
}

// =============================================================================
// List query parameters
// =============================================================================

/// Inputs for [`list`]. Mirrors the spec `ListTasksRequest` minus the
/// `tenant` path param (the caller plumbs that into the tenant scope).
#[derive(Debug, Clone, Default)]
pub struct ListQuery {
    pub context_id: Option<String>,
    pub state: Option<TaskState>,
    pub page_size: Option<i32>,
    pub page_token: Option<String>,
    /// Spec `status_timestamp_after`: only tasks whose status changed
    /// at or after this moment. Used by Mission Control incremental
    /// refreshes.
    pub status_timestamp_after: Option<DateTime<Utc>>,
    /// The resource group the caller was authorised under.
    /// `None` only for internal callers with no group scope; a caller-facing
    /// route must always set it, or the list is tenant-wide.
    pub caller_group: Option<String>,
}

/// Result of [`list`]. Mirrors `ListTasksResponse` — the caller
/// wraps it in the HTTP envelope.
pub struct ListResult {
    pub tasks: Vec<Task>,
    pub next_page_token: String,
    pub page_size: i32,
    pub total_size: i32,
}

// =============================================================================
// Core operations
// =============================================================================

/// Insert a task row. Fails if a row with the same `id` already
/// exists under the same tenant (UNIQUE on PK).
///
/// Caller supplies the resource_group_id separately because tasks
/// are scoped per-group for billing / access control; the group is
/// not part of the [`Task`] object itself.
pub async fn create(
    db: &PgPool,
    tenant_id: &str,
    resource_group_id: Option<&str>,
    task: &Task,
) -> Result<Task, TaskStoreError> {
    let task_json =
        serde_json::to_value(task).map_err(|e| TaskStoreError::Decode(e.to_string()))?;
    let state = state_string(&task.status.state);
    let timestamp = task.status.timestamp.unwrap_or_else(Utc::now);
    let context_id = task.context_id.as_deref();

    sqlx::query(
        r#"INSERT INTO a2a_tasks
           (id, tenant_id, resource_group_id, context_id,
            state, status_timestamp, task_json)
           VALUES ($1, $2, $3, $4, $5, $6, $7)"#,
    )
    .bind(&task.id)
    .bind(tenant_id)
    .bind(resource_group_id)
    .bind(context_id)
    .bind(&state)
    .bind(timestamp)
    .bind(&task_json)
    .execute(db)
    .await?;
    Ok(task.clone())
}

/// Fetch a task by id, scoped to the caller's tenant.
///
/// Prefer [`get_scoped`] on any path that serves a caller: tenant scope alone
/// is not containment. This variant stays for internal
/// callers — the dispatcher and the sweeper — that legitimately act on a task
/// without a caller's group.
pub async fn get(db: &PgPool, tenant_id: &str, task_id: &str) -> Result<Task, TaskStoreError> {
    let row = sqlx::query_as::<_, TaskRow>(
        r#"SELECT id, tenant_id, resource_group_id, context_id,
                  state, status_timestamp, task_json
           FROM a2a_tasks
           WHERE id = $1 AND tenant_id = $2"#,
    )
    .bind(task_id)
    .bind(tenant_id)
    .fetch_optional(db)
    .await?;
    row.ok_or(TaskStoreError::NotFound)?.into_task()
}

/// Fetch a task the caller is entitled to see.
///
/// Every read, cancel and append was scoped by tenant alone,
/// so any principal who could reach any agent card could read another team's
/// task, cancel it, or append messages to it. A task carries the conversation
/// that produced it, so this is a content disclosure as much as a control one.
///
/// `caller_group` is the resource group the request was authorised under.
/// `None` means the caller has no group scope (an internal or tenant-wide
/// path) and the tenant boundary is all there is — the same rule the run
/// ledger uses. A task with no recorded group cannot be contained by one, so
/// it stays visible rather than becoming unreachable.
pub async fn get_scoped(
    db: &PgPool,
    tenant_id: &str,
    task_id: &str,
    caller_group: Option<&str>,
) -> Result<Task, TaskStoreError> {
    let row = sqlx::query_as::<_, TaskRow>(
        r#"SELECT id, tenant_id, resource_group_id, context_id,
                  state, status_timestamp, task_json
           FROM a2a_tasks
           WHERE id = $1 AND tenant_id = $2
             AND ($3::text IS NULL
                  OR resource_group_id IS NULL
                  OR resource_group_id = $3)"#,
    )
    .bind(task_id)
    .bind(tenant_id)
    .bind(caller_group)
    .fetch_optional(db)
    .await?;
    // Deliberately NotFound rather than Forbidden: a caller who may not see a
    // task must not learn it exists.
    row.ok_or(TaskStoreError::NotFound)?.into_task()
}

/// Whether a task exists and whether `caller_group` may see it — the
/// containment rule of [`get_scoped`], without reading the task body.
/// `None` when no task has this id in the tenant; `Some(false)` when it
/// belongs to a group the caller is not in.
pub async fn visibility(
    db: &PgPool,
    tenant_id: &str,
    task_id: &str,
    caller_group: Option<&str>,
) -> Result<Option<bool>, TaskStoreError> {
    let row: Option<(bool,)> = sqlx::query_as(
        r#"SELECT ($3::text IS NULL
                   OR resource_group_id IS NULL
                   OR resource_group_id = $3)
           FROM a2a_tasks
           WHERE id = $1 AND tenant_id = $2"#,
    )
    .bind(task_id)
    .bind(tenant_id)
    .bind(caller_group)
    .fetch_optional(db)
    .await?;
    Ok(row.map(|(visible,)| visible))
}

/// List tasks with spec-conformant filtering + pagination.
///
/// Pagination token encoding: `base64url(JSON({ts, id}))`. Opaque to
/// the caller — they just treat it as an atomic handle and pass it
/// back to the next `list` call.
pub async fn list(
    db: &PgPool,
    tenant_id: &str,
    query: &ListQuery,
) -> Result<ListResult, TaskStoreError> {
    let page_size = query
        .page_size
        .unwrap_or(LIST_TASKS_DEFAULT_PAGE_SIZE)
        .clamp(1, LIST_TASKS_MAX_PAGE_SIZE);

    // Parse the cursor. `None` means "from the top".
    let cursor = match &query.page_token {
        Some(t) if !t.is_empty() => Some(decode_page_token(t)?),
        _ => None,
    };

    // Build the SQL dynamically — sqlx's macro-based query_as! doesn't
    // cope well with optional WHERE chunks; we use QueryBuilder.
    let mut qb = sqlx::QueryBuilder::<sqlx::Postgres>::new(
        r#"SELECT id, tenant_id, resource_group_id, context_id,
                  state, status_timestamp, task_json
           FROM a2a_tasks
           WHERE tenant_id = "#,
    );
    qb.push_bind(tenant_id);
    // Listing is scoped to the caller's group as well. Without
    // it, `tasks/list` returned every task in the tenant to anyone holding any
    // card's credentials.
    if let Some(group) = query.caller_group.as_deref() {
        qb.push(" AND (resource_group_id IS NULL OR resource_group_id = ");
        qb.push_bind(group.to_string());
        qb.push(")");
    }
    if let Some(ctx) = &query.context_id {
        qb.push(" AND context_id = ");
        qb.push_bind(ctx);
    }
    if let Some(state) = &query.state {
        qb.push(" AND state = ");
        qb.push_bind(state_string(state));
    }
    if let Some(after) = query.status_timestamp_after {
        qb.push(" AND status_timestamp >= ");
        qb.push_bind(after);
    }
    if let Some(c) = &cursor {
        // Strict "<" on timestamp OR tie-break by id. Mirrors the
        // ORDER BY below so pagination can't skip or duplicate.
        qb.push(" AND (status_timestamp, id) < (");
        qb.push_bind(c.status_timestamp);
        qb.push(", ");
        qb.push_bind(&c.id);
        qb.push(")");
    }
    qb.push(" ORDER BY status_timestamp DESC, id ASC LIMIT ");
    qb.push_bind((page_size + 1) as i64); // +1 so we know if there's another page.

    let rows: Vec<TaskRow> = qb.build_query_as().fetch_all(db).await?;

    let has_next = rows.len() > page_size as usize;
    let visible = if has_next {
        &rows[..page_size as usize]
    } else {
        &rows[..]
    };

    let next_token = if has_next {
        let last = &visible[visible.len() - 1];
        encode_page_token(&Cursor {
            status_timestamp: last.status_timestamp,
            id: last.id.clone(),
        })
    } else {
        String::new()
    };

    // Total count — separate query scoped to the same filter set
    // (without cursor). Spec says this is `total_size` on the response.
    // We keep it cheap via the same indexed WHERE.
    let mut cqb = sqlx::QueryBuilder::<sqlx::Postgres>::new(
        "SELECT COUNT(*) FROM a2a_tasks WHERE tenant_id = ",
    );
    cqb.push_bind(tenant_id);
    if let Some(ctx) = &query.context_id {
        cqb.push(" AND context_id = ");
        cqb.push_bind(ctx);
    }
    if let Some(state) = &query.state {
        cqb.push(" AND state = ");
        cqb.push_bind(state_string(state));
    }
    if let Some(after) = query.status_timestamp_after {
        cqb.push(" AND status_timestamp >= ");
        cqb.push_bind(after);
    }
    let total: i64 = cqb.build_query_scalar().fetch_one(db).await?;

    let mut tasks = Vec::with_capacity(visible.len());
    for row in visible {
        tasks.push(row.clone().into_task()?);
    }

    Ok(ListResult {
        tasks,
        next_page_token: next_token,
        page_size,
        total_size: total as i32,
    })
}

/// Cancel a task.
///
/// Idempotent for already-cancelled tasks. Returns `NotCancelable`
/// when the task is in a terminal state other than CANCELED (spec
/// §3.3.1: "A duplicate cancellation request MAY return
/// TaskNotFoundError if the task has already been canceled and
/// purged" — we don't purge, so we return the cancelled task).
/// Cancel a task the caller is entitled to cancel.
///
/// `caller_group` is checked on the read, so a caller cannot
/// cancel another group's work. Cancelling is a write to someone else's run,
/// not merely a read of it.
pub async fn cancel(
    db: &PgPool,
    tenant_id: &str,
    task_id: &str,
    caller_group: Option<&str>,
) -> Result<Task, TaskStoreError> {
    let current = get_scoped(db, tenant_id, task_id, caller_group).await?;
    if current.status.state == TaskState::TaskStateCanceled {
        return Ok(current); // already cancelled — idempotent
    }
    if current.status.state.is_terminal() {
        return Err(TaskStoreError::NotCancelable {
            state: current.status.state,
        });
    }

    let mut updated = current;
    updated.status = TaskStatus {
        state: TaskState::TaskStateCanceled,
        message: updated.status.message.take(),
        timestamp: Some(Utc::now()),
    };
    write(db, tenant_id, &updated).await?;
    Ok(updated)
}

/// Persist a full task update. The caller owns the merge semantics —
/// the store never mutates fields it wasn't told about. State +
/// status_timestamp + context_id denormalised from `task_json` so
/// filter indexes stay in sync.
pub async fn write(db: &PgPool, tenant_id: &str, task: &Task) -> Result<(), TaskStoreError> {
    let task_json =
        serde_json::to_value(task).map_err(|e| TaskStoreError::Decode(e.to_string()))?;
    let state = state_string(&task.status.state);
    let timestamp = task.status.timestamp.unwrap_or_else(Utc::now);

    let affected = sqlx::query(
        r#"UPDATE a2a_tasks
           SET state = $1,
               status_timestamp = $2,
               context_id = $3,
               task_json = $4,
               updated_at = NOW()
           WHERE id = $5 AND tenant_id = $6"#,
    )
    .bind(&state)
    .bind(timestamp)
    .bind(task.context_id.as_deref())
    .bind(&task_json)
    .bind(&task.id)
    .bind(tenant_id)
    .execute(db)
    .await?
    .rows_affected();

    if affected == 0 {
        return Err(TaskStoreError::NotFound);
    }

    // Wake any SSE streams tracking this task — on THIS replica and every
    // other one — so `build_task_stream` emits the new frames without waiting
    // for its reconcile tick. Best-effort: a failed NOTIFY only costs latency
    // (the streams' safety-net poll still picks the change up), so we never
    // fail the write on it. See `stream_hub.rs`.
    let notice = crate::services::a2a_v1::stream_hub::TaskUpdateNotice {
        tenant_id: tenant_id.to_string(),
        task_id: task.id.clone(),
    };
    if let Ok(payload) = serde_json::to_string(&notice) {
        if let Err(e) = sqlx::query("SELECT pg_notify($1, $2)")
            .bind(crate::services::a2a_v1::stream_hub::A2A_TASK_UPDATE_CHANNEL)
            .bind(&payload)
            .execute(db)
            .await
        {
            tracing::warn!(error = %e, task_id = %task.id, "a2a: pg_notify for task update failed (stream will fall back to poll)");
        }
    }

    Ok(())
}

/// Update only the task status (state + message + timestamp), leaving
/// history and artifacts untouched. Common at state-transition
/// checkpoints (SUBMITTED → WORKING → COMPLETED).
pub async fn update_status(
    db: &PgPool,
    tenant_id: &str,
    task_id: &str,
    new_state: TaskState,
    status_message: Option<Message>,
) -> Result<Task, TaskStoreError> {
    let mut task = get(db, tenant_id, task_id).await?;
    task.status = TaskStatus {
        state: new_state,
        message: status_message,
        timestamp: Some(Utc::now()),
    };
    write(db, tenant_id, &task).await?;
    Ok(task)
}

/// Append an artifact to an existing task.
pub async fn append_artifact(
    db: &PgPool,
    tenant_id: &str,
    task_id: &str,
    artifact: Artifact,
) -> Result<Task, TaskStoreError> {
    let mut task = get(db, tenant_id, task_id).await?;
    task.artifacts.push(artifact);
    write(db, tenant_id, &task).await?;
    Ok(task)
}

/// Append a message to the task history. Used by multi-turn
/// interactions.
pub async fn append_history(
    db: &PgPool,
    tenant_id: &str,
    task_id: &str,
    message: Message,
) -> Result<Task, TaskStoreError> {
    let mut task = get(db, tenant_id, task_id).await?;
    task.history.push(message);
    write(db, tenant_id, &task).await?;
    Ok(task)
}

// =============================================================================
// Helpers
// =============================================================================

fn state_string(s: &TaskState) -> String {
    // Round-trips through serde — the Rust enum is already serialized
    // as SCREAMING_SNAKE_CASE per its #[serde(rename_all = "...")]
    // attribute.
    serde_json::to_value(s)
        .ok()
        .and_then(|v| v.as_str().map(str::to_owned))
        .unwrap_or_else(|| "TASK_STATE_UNSPECIFIED".to_string())
}

#[derive(Debug, Serialize, Deserialize)]
struct Cursor {
    status_timestamp: DateTime<Utc>,
    id: String,
}

fn encode_page_token(c: &Cursor) -> String {
    let json = serde_json::to_vec(c).expect("cursor encodes");
    B64URL.encode(&json)
}

fn decode_page_token(s: &str) -> Result<Cursor, TaskStoreError> {
    let bytes = B64URL
        .decode(s.as_bytes())
        .map_err(|e| TaskStoreError::BadPageToken(format!("base64url: {e}")))?;
    serde_json::from_slice(&bytes).map_err(|e| TaskStoreError::BadPageToken(format!("json: {e}")))
}

// =============================================================================
// Unit tests — pure helpers only; DB round-trip is covered by an
// integration test.
// =============================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn state_string_matches_wire_form() {
        assert_eq!(
            state_string(&TaskState::TaskStateWorking),
            "TASK_STATE_WORKING"
        );
        assert_eq!(
            state_string(&TaskState::TaskStateCompleted),
            "TASK_STATE_COMPLETED"
        );
        assert_eq!(
            state_string(&TaskState::TaskStateCanceled),
            "TASK_STATE_CANCELED"
        );
    }

    #[test]
    fn page_token_round_trip() {
        let c = Cursor {
            status_timestamp: Utc.with_ymd_and_hms(2026, 4, 24, 10, 0, 0).unwrap(),
            id: "01HZZZZZZZZZZZZZZZZZZZZZZZ".into(),
        };
        let token = encode_page_token(&c);
        let back = decode_page_token(&token).unwrap();
        assert_eq!(back.id, c.id);
        assert_eq!(back.status_timestamp, c.status_timestamp);
    }

    #[test]
    fn page_token_rejects_garbage() {
        // Not base64url.
        let e = decode_page_token("!!!not-base64!!!").unwrap_err();
        assert!(matches!(e, TaskStoreError::BadPageToken(_)));

        // Valid base64url but not JSON.
        let e = decode_page_token(&B64URL.encode(b"\xff\xff\xff")).unwrap_err();
        assert!(matches!(e, TaskStoreError::BadPageToken(_)));

        // Valid base64url + JSON but wrong shape.
        let bad_json = B64URL.encode(br#"{"foo":"bar"}"#);
        let e = decode_page_token(&bad_json).unwrap_err();
        assert!(matches!(e, TaskStoreError::BadPageToken(_)));
    }

    #[test]
    fn not_cancelable_carries_state() {
        let e = TaskStoreError::NotCancelable {
            state: TaskState::TaskStateCompleted,
        };
        assert!(format!("{e}").contains("TaskStateCompleted"));
    }
}
