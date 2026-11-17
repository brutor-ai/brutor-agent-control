// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Tool Approval Routes
//!
//! Endpoints for viewing and resolving tool approval requests, and the poll
//! route an agent is handed when its call is parked. Group members can
//! approve or reject approval requests for their groups.
//!
//! Mounted at `/v1/approvals`: approvals are a
//! Community concern — an agent's parked call must be decidable without the
//! portal. Humans decide here with a portal session, or through the control
//! plane's `/v1/admin/approvals` from the Admin Console; agents poll here
//! with their API key. The path is on the tenant layer's runtime list, so a
//! suspended tenant's agents are blocked like every other runtime call.
//!
//! ## Endpoints
//!
//! ```text
//! GET  /v1/approvals                  → List approval requests for my groups
//! GET  /v1/approvals/pending-count    → Pending count (for badge)
//! GET  /v1/approvals/{id}             → Get approval request detail
//! GET  /v1/approvals/{id}/poll        → Poll approval status (for agents)
//! POST /v1/approvals/{id}/approve     → Approve (group member)
//! POST /v1/approvals/{id}/reject      → Reject (group member)
//! ```
//!
//! ## Access Control
//!
//! Users can only see and act on approval requests belonging to groups
//! they are a member of. Authentication is via Gateway JWT.

use axum::Router;
use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Json, Response};
use axum::routing::{get, post};
use chrono::{DateTime, Utc};
use rand::Rng;
use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::middleware::tenant::TenantContext;
use crate::models::auth::{AuthContext, AuthType};
use crate::routes::request::{self, portal_headers};
use crate::services::db_errors::DbResultExt;
use crate::services::{SharedState, agent_approval, auth, proxy_logger};

// ---------------------------------------------------------------------------
// Status vocabulary
// ---------------------------------------------------------------------------

/// Every status the core writes to `tool_approval_requests.status`, and what
/// each one means to a person reading the queue.
///
/// This is the single place the vocabulary lives. The list filter, the
/// derived `decision` field and the Portal's status rendering all read from
/// it, and `tests::every_written_status_is_in_the_vocabulary` scans `src/` so
/// a new status written anywhere fails the build until it is added here.
///
/// | status     | decision   | meaning                                           |
/// |------------|------------|---------------------------------------------------|
/// | `pending`  | `pending`  | waiting for a person (lapses to `expired` at `expires_at`) |
/// | `approved` | `approved` | a person said yes; the one-time token is unredeemed |
/// | `consumed` | `approved` | a person said yes and the agent redeemed the token |
/// | `rejected` | `rejected` | a person said no                                  |
/// | `expired`  | `expired`  | nobody decided in time, or the run was aborted     |
pub mod approval_status {
    use chrono::{DateTime, Utc};

    pub const PENDING: &str = "pending";
    pub const APPROVED: &str = "approved";
    pub const CONSUMED: &str = "consumed";
    pub const REJECTED: &str = "rejected";
    pub const EXPIRED: &str = "expired";

    /// Every status the core can write.
    pub const ALL: &[&str] = &[PENDING, APPROVED, CONSUMED, REJECTED, EXPIRED];

    /// "A person said yes" — whether or not the token has been redeemed yet.
    pub const APPROVED_STATUSES: &[&str] = &[APPROVED, CONSUMED];

    /// The `?status=` value that disables the filter.
    pub const FILTER_ALL: &str = "all";

    /// The `?status=` values the list endpoint accepts, in display order.
    pub const FILTERS: &[&str] = &[PENDING, APPROVED, CONSUMED, REJECTED, EXPIRED, FILTER_ALL];

    /// A `?status=` value that is not one of [`FILTERS`].
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub struct UnknownFilter;

    /// The effective statuses a `?status=` filter selects. `Ok(None)` means
    /// no filter (`all`); `Err(UnknownFilter)` means the value is not a known filter.
    ///
    /// `approved` selects `consumed` too: redeeming the token does not undo
    /// the person's decision, so hiding it from the Approved view would hide
    /// nearly every real approval.
    pub fn filter_statuses(filter: &str) -> Result<Option<&'static [&'static str]>, UnknownFilter> {
        match filter {
            FILTER_ALL => Ok(None),
            PENDING => Ok(Some(&[PENDING])),
            APPROVED => Ok(Some(APPROVED_STATUSES)),
            CONSUMED => Ok(Some(&[CONSUMED])),
            REJECTED => Ok(Some(&[REJECTED])),
            EXPIRED => Ok(Some(&[EXPIRED])),
            _ => Err(UnknownFilter),
        }
    }

    /// SQL for a row's effective status. A `pending` row past its deadline
    /// is `expired` whether or not the 60-second sweeper has reached it yet,
    /// so it is neither counted as actionable nor missing from Expired.
    pub const EFFECTIVE_STATUS_SQL: &str =
        "(CASE WHEN status = 'pending' AND expires_at < NOW() THEN 'expired' ELSE status END)";

    /// The decision a person made, derived from the stored status so clients
    /// never re-derive it: `pending`, `approved`, `rejected`, `expired`, or
    /// `unknown` for a status this build does not know.
    pub fn decision(
        status: &str,
        expires_at: Option<DateTime<Utc>>,
        now: DateTime<Utc>,
    ) -> &'static str {
        match status {
            PENDING if expires_at.is_some_and(|e| e < now) => EXPIRED,
            PENDING => PENDING,
            APPROVED | CONSUMED => APPROVED,
            REJECTED => REJECTED,
            EXPIRED => EXPIRED,
            _ => "unknown",
        }
    }

    /// Whether the one-time approval token has been redeemed.
    pub fn token_used(status: &str) -> bool {
        status == CONSUMED
    }
}

pub fn router() -> Router<SharedState> {
    Router::new()
        .route("/", get(list_approvals))
        .route("/pending-count", get(pending_count))
        .route("/{id}", get(get_approval_detail))
        .route("/{id}/poll", get(poll_approval))
        .route("/{id}/approve", post(approve_request))
        .route("/{id}/reject", post(reject_request))
}

// ---------------------------------------------------------------------------
// Query / request / response types
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
pub struct ListApprovalsQuery {
    /// One of `approval_status::FILTERS`; defaults to `pending`.
    pub status: Option<String>,
    pub page: Option<i64>,
    pub page_size: Option<i64>,
}

#[derive(Debug, Deserialize)]
pub struct ResolveBody {
    pub note: Option<String>,
}

#[derive(Debug, Serialize, sqlx::FromRow)]
struct ApprovalRow {
    pub id: String,
    pub tenant_id: String,
    pub server_config_id: Option<String>,
    pub server_name: Option<String>,
    pub capability_type: String,
    pub capability_name: String,
    pub requester_type: String,
    pub requester_user_id: Option<String>,
    pub requester_api_key_id: Option<String>,
    pub requester_group_id: String,
    pub requester_display: Option<String>,
    pub call_arguments: Option<serde_json::Value>,
    pub request_metadata: Option<serde_json::Value>,
    pub status: String,
    pub approval_token: Option<String>,
    pub token_used: String,
    pub timeout_seconds: i32,
    pub expires_at: Option<DateTime<Utc>>,
    pub resolved_by: Option<String>,
    pub resolved_at: Option<DateTime<Utc>>,
    pub resolution_note: Option<String>,
    pub created_at: Option<DateTime<Utc>>,
    // Chain inherited from the
    // originating mcp.rs request. NULL on an approval row that carries none
    // (chain inheritance silently skipped).
    pub originating_trace_root_id: Option<String>,
    pub originating_parent_task_id: Option<String>,
    pub originating_trace_root_kind: Option<String>,
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Readable names for agent requesters, keyed by agent identity id.
///
/// `requester_display` on an agent-raised hold is the identity id
/// (`agent-01M3…`). The approver needs the name the operator gave it
/// (`brutor-demo-screening-worker`), so both the list and the detail resolve
/// it — in one query per page, never one per row. Unknown ids (identity
/// deleted since) simply have no entry and the card falls back to the id.
pub(crate) async fn agent_names(
    db: &sqlx::PgPool,
    tenant_id: &str,
    ids: &[String],
) -> std::collections::HashMap<String, String> {
    if ids.is_empty() {
        return Default::default();
    }
    sqlx::query_as::<_, (String, String)>(
        "SELECT id, name FROM agent_identities WHERE tenant_id = $1 AND id = ANY($2)",
    )
    .bind(tenant_id)
    .bind(ids)
    .fetch_all(db)
    .await
    // Display only (agent names on the list): degrade to ids, logged.
    .or_logged_default()
    .into_iter()
    .collect()
}

/// The requester id to resolve, when the requester is an agent identity.
fn agent_requester_id(requester_type: &str, requester_display: Option<&str>) -> Option<String> {
    match (requester_type, requester_display) {
        ("agent", Some(d)) if d.starts_with("agent-") => Some(d.to_string()),
        _ => None,
    }
}

// ---------------------------------------------------------------------------
// Row helpers
// ---------------------------------------------------------------------------

fn error(status: StatusCode, message: &str) -> Response {
    (status, Json(json!({"error": message}))).into_response()
}

fn generate_approval_token() -> String {
    use base64::Engine;
    let mut rng = rand::rng();
    let mut bytes = [0u8; 64];
    rng.fill(&mut bytes);
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes)
}

fn generate_ulid() -> String {
    ulid::Ulid::new().to_string().to_lowercase()
}

/// Emit a proxy_log row for an approval decision so the audit trail
/// includes the human-in-the-loop step alongside the original blocked
/// tools/call and the eventual approved retry. Approve and reject share
/// this writer; the only differences are the subtype tag and the
/// http_status_code on the row.
async fn write_approval_decision_log(
    state: &SharedState,
    row: &ApprovalRow,
    approver_user_id: &str,
    approver_username: &str,
    tenant_id: &str,
    client_ip: Option<String>,
    correlation_id: &str,
    decision: &str, // "approved" | "rejected"
    resolution_note: Option<&str>,
    duration_ms: f64,
) {
    let auth_ctx = AuthContext {
        auth_type: AuthType::GatewayJwt,
        tenant_id: tenant_id.to_string(),
        gateway_user_id: Some(approver_user_id.to_string()),
        gateway_username: Some(approver_username.to_string()),
        gateway_tenant_id: Some(tenant_id.to_string()),
        idp_user_id: None,
        idp_username: None,
        idp_email: None,
        api_key_id: None,
        api_key_name: None,
        api_key_scope: None,
        api_key_group_id: None,
        api_key_is_platform: false,
        api_key_user_id: None,
        allowed_skill_ids: None,
        session_key: None,
        client_ip,
        resolved_group_id: Some(row.requester_group_id.clone()),
        actor_agent_id: None,
        session_token_type: None,
        agent_impl: crate::services::agent_impl::request_carriers(),
        admission_hold: None,
    };

    // Surface determines the audit shape. MCP rows resolve the upstream server
    // for an accurate target_url/server_id; A2A/skill rows have no MCP upstream
    // and use the scope ref (card id / skill id) directly. Read from the
    // request_metadata.surface stamped at enqueue (defaults to "mcp" for a row
    // that carries none).
    let surface = row
        .request_metadata
        .as_ref()
        .and_then(|m| m.get("surface"))
        .and_then(|v| v.as_str())
        .unwrap_or("mcp")
        .to_string();

    let (proxy_type, server_id, target_url): (&str, String, Option<String>) = if surface == "mcp" {
        // Resolve the upstream MCP server so this audit row carries the same
        // target_url + server_id as the originating "approval_required" row and
        // the eventual approved retry.
        let upstream: Option<(String, String)> = match row.server_config_id.as_ref() {
            Some(sc_id) => sqlx::query_as(
                "SELECT ms.id, ms.base_url \
                 FROM server_configs sc \
                 JOIN mcp_servers ms ON sc.mcp_server_id = ms.id \
                 WHERE sc.id = $1 AND sc.tenant_id = $2",
            )
            .bind(sc_id)
            .bind(tenant_id)
            .fetch_optional(&state.db)
            .await
            // The decision row is written regardless; only this field is
            // left empty on a failed read (logged and counted).
            .or_logged(None),
            None => None,
        };
        match upstream {
            Some((id, url)) => ("mcp", id, Some(url)),
            None => (
                "mcp",
                row.server_config_id.clone().unwrap_or_default(),
                Some(format!("approval://{}", row.id)),
            ),
        }
    } else {
        // A2A / skill: scope ref (card id / skill id) is in server_name; no
        // upstream URL to resolve.
        let proxy_type = if surface == "a2a" { "a2a" } else { "skill" };
        (
            proxy_type,
            row.server_name.clone().unwrap_or_else(|| row.id.clone()),
            Some(format!("approval://{}", row.id)),
        )
    };

    let endpoint = format!("/v1/approvals/{}/{}", row.id, decision);

    // Synthesize the request/response previews so operators can see
    // who approved what and with which note — matching the body-
    // preview contract every other audit-row emitter holds.
    let req_preview = serde_json::to_string(&json!({
        "approval_request_id": row.id,
        "decision": decision,
        "note": resolution_note.unwrap_or(""),
    }))
    .unwrap_or_default();
    let resp_preview = serde_json::to_string(&json!({
        "status": decision,
        "approver_user_id": approver_user_id,
        "approver_username": approver_username,
        "capability": row.capability_name,
        "server": row.server_name.clone().unwrap_or_default(),
    }))
    .unwrap_or_default();

    let mut log = proxy_logger::build_proxy_log(
        proxy_type,
        &auth_ctx,
        "POST",
        &endpoint,
        target_url.as_deref(),
        Some(server_id.as_str()),
        200,
        duration_ms,
        true,
        None,
        correlation_id,
    );
    log.proxy_subtype = Some(format!("approval_{}", decision));
    if proxy_type == "mcp" {
        log.mcp_method = Some(format!("{}s/call", row.capability_type));
    }
    log.mcp_capability_name = Some(row.capability_name.clone());
    log.request_body_preview = Some(proxy_logger::body_preview(&req_preview));
    log.response_body_preview = Some(proxy_logger::body_preview(&resp_preview));
    log.request_size_bytes = Some(req_preview.len() as i32);
    log.response_size_bytes = Some(resp_preview.len() as i32);

    // Splice this decision row onto the
    // originating mcp.rs call's chain. We mint a new current_task_id
    // for *this* node (the decision is its own step — different actor,
    // different timestamp) and set parent_task_id to the mcp.rs
    // node's current_task_id we stashed at request creation. Depth =
    // parent_depth + 1; since we don't persist parent depth, infer it
    // from the chain shape (the originating row is depth 0 if there
    // was no inbound chain, otherwise N — best-effort: stamp depth=1
    // when we have a real parent, leaving the trace UI to walk the
    // tree via parent_task_id which is the authoritative link). If
    // the originating columns are NULL (pre-a072 approval), the
    // decision row is left chain-less and renders as its own root.
    if let (Some(root), Some(parent)) = (
        row.originating_trace_root_id.as_ref(),
        row.originating_parent_task_id.as_ref(),
    ) {
        log.root_task_id = Some(root.clone());
        log.parent_task_id = Some(parent.clone());
        log.current_task_id = Some(ulid::Ulid::new().to_string().to_lowercase());
        log.delegation_depth = Some(1);
        log.trace_root_kind = row.originating_trace_root_kind.clone();
        log.trace_continuity = Some("verified".to_string());
    }

    state
        .background_writer
        .write_proxy_log(state.db.clone(), log);
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

/// GET /v1/approvals — List approval requests for my groups
async fn list_approvals(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Query(params): Query<ListApprovalsQuery>,
) -> Response {
    let client_ip = client_addr.ip();
    let (tenant_id, user_id, _username) =
        match crate::routes::request::extract_portal_user(&state, &headers, &tenant, client_ip)
            .await
        {
            Ok(v) => v,
            Err(r) => return r,
        };

    let group_ids = match request::get_user_group_ids(&state.db, &user_id, &tenant_id).await {
        Ok(v) => v,
        Err(e) => return crate::services::db_errors::unavailable("your resource groups", &e),
    };
    if group_ids.is_empty() {
        return Json(json!({
            "items": [],
            "total": 0,
            "page": 1,
            "page_size": 50
        }))
        .into_response();
    }

    let status_filter = params.status.as_deref().unwrap_or(approval_status::PENDING);
    let statuses: Option<Vec<String>> = match approval_status::filter_statuses(status_filter) {
        Ok(s) => s.map(|s| s.iter().map(|v| v.to_string()).collect()),
        Err(approval_status::UnknownFilter) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({
                    "error": format!("Unknown approval status filter: {status_filter}"),
                    "valid_statuses": approval_status::FILTERS,
                })),
            )
                .into_response();
        }
    };
    let page = params.page.unwrap_or(1).max(1);
    let page_size = params.page_size.unwrap_or(50).clamp(1, 200);
    let offset = (page - 1) * page_size;

    // `$3` NULL = no filter (`all`, which also shows statuses this build does
    // not know); otherwise the row's effective status must be in the set.
    let predicate = format!(
        "tenant_id = $1 AND requester_group_id = ANY($2) \
         AND ($3::text[] IS NULL OR {} = ANY($3))",
        approval_status::EFFECTIVE_STATUS_SQL
    );

    // A failed count is a 503, never "0 approvals".
    let total: i64 = match sqlx::query_as::<_, (Option<i64>,)>(&format!(
        "SELECT COUNT(*) FROM tool_approval_requests WHERE {predicate}"
    ))
    .bind(&tenant_id)
    .bind(&group_ids)
    .bind(&statuses)
    .fetch_one(&state.db)
    .await
    {
        Ok((c,)) => c.unwrap_or(0),
        Err(e) => return crate::services::db_errors::unavailable("approvals", &e),
    };

    let items: Vec<ApprovalRow> = match sqlx::query_as::<_, ApprovalRow>(&format!(
        "SELECT * FROM tool_approval_requests WHERE {predicate} \
         ORDER BY created_at DESC LIMIT $4 OFFSET $5"
    ))
    .bind(&tenant_id)
    .bind(&group_ids)
    .bind(&statuses)
    .bind(page_size)
    .bind(offset)
    .fetch_all(&state.db)
    .await
    {
        Ok(v) => v,
        Err(e) => return crate::services::db_errors::unavailable("the approval", &e),
    };

    // The list stays free of call arguments (they can carry personal data and
    // most rows are never opened); the card fetches GET /approvals/{id} when
    // the approver expands it. Only the readable requester name is added here.
    let agent_ids: Vec<String> = items
        .iter()
        .filter_map(|r| agent_requester_id(&r.requester_type, r.requester_display.as_deref()))
        .collect();
    let names = agent_names(&state.db, &tenant_id, &agent_ids).await;

    let now = Utc::now();
    let items_json: Vec<serde_json::Value> = items
        .iter()
        .map(|r| {
            let requester_name =
                agent_requester_id(&r.requester_type, r.requester_display.as_deref())
                    .and_then(|id| names.get(&id).cloned());
            json!({
                "id": r.id,
                "server_name": r.server_name,
                "capability_type": r.capability_type,
                "capability_name": r.capability_name,
                "requester_type": r.requester_type,
                "requester_display": r.requester_display,
                "requester_name": requester_name,
                "requester_group_id": r.requester_group_id,
                "status": r.status,
                "decision": approval_status::decision(&r.status, r.expires_at, now),
                "token_used": approval_status::token_used(&r.status),
                "timeout_seconds": r.timeout_seconds,
                "expires_at": r.expires_at.map(|t| t.to_rfc3339()),
                "resolved_by": r.resolved_by,
                "resolved_at": r.resolved_at.map(|t| t.to_rfc3339()),
                "resolution_note": r.resolution_note,
                "created_at": r.created_at.map(|t| t.to_rfc3339()),
            })
        })
        .collect();

    Json(json!({
        "items": items_json,
        "total": total,
        "page": page,
        "page_size": page_size,
        "status_filter": status_filter,
    }))
    .into_response()
}

/// GET /v1/approvals/pending-count — Badge count
async fn pending_count(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
) -> Response {
    let client_ip = client_addr.ip();
    let (tenant_id, user_id, _username) =
        match crate::routes::request::extract_portal_user(&state, &headers, &tenant, client_ip)
            .await
        {
            Ok(v) => v,
            Err(r) => return r,
        };

    let group_ids = match request::get_user_group_ids(&state.db, &user_id, &tenant_id).await {
        Ok(v) => v,
        Err(e) => return crate::services::db_errors::unavailable("your resource groups", &e),
    };
    if group_ids.is_empty() {
        return Json(json!({"pending_count": 0})).into_response();
    }

    // Same effective status as the list's Pending view, so the badge never
    // counts a hold whose deadline has already passed.
    let count: i64 = match sqlx::query_as::<_, (Option<i64>,)>(&format!(
        "SELECT COUNT(*) FROM tool_approval_requests \
         WHERE tenant_id = $1 AND requester_group_id = ANY($2) AND {} = $3",
        approval_status::EFFECTIVE_STATUS_SQL
    ))
    .bind(&tenant_id)
    .bind(&group_ids)
    .bind(approval_status::PENDING)
    .fetch_one(&state.db)
    .await
    {
        Ok((c,)) => c.unwrap_or(0),
        // The badge must not read "0 pending" because a read failed.
        Err(e) => return crate::services::db_errors::unavailable("approvals", &e),
    };

    Json(json!({"pending_count": count})).into_response()
}

/// GET /v1/approvals/{id} — Approval request detail
async fn get_approval_detail(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Path(approval_id): Path<String>,
) -> Response {
    let client_ip = client_addr.ip();
    let (tenant_id, user_id, _username) =
        match crate::routes::request::extract_portal_user(&state, &headers, &tenant, client_ip)
            .await
        {
            Ok(v) => v,
            Err(r) => return r,
        };

    let group_ids = match request::get_user_group_ids(&state.db, &user_id, &tenant_id).await {
        Ok(v) => v,
        Err(e) => return crate::services::db_errors::unavailable("your resource groups", &e),
    };

    let row: Option<ApprovalRow> = match sqlx::query_as(
        "SELECT * FROM tool_approval_requests WHERE id = $1 AND tenant_id = $2",
    )
    .bind(&approval_id)
    .bind(&tenant_id)
    .fetch_optional(&state.db)
    .await
    {
        Ok(v) => v,
        Err(e) => return crate::services::db_errors::unavailable("the approval", &e),
    };

    let row = match row {
        Some(r) => r,
        None => return error(StatusCode::NOT_FOUND, "Approval request not found"),
    };

    // Verify group membership
    if !group_ids.contains(&row.requester_group_id) {
        return error(StatusCode::FORBIDDEN, "Not a member of the request's group");
    }

    let requester_name =
        match agent_requester_id(&row.requester_type, row.requester_display.as_deref()) {
            Some(id) => agent_names(&state.db, &tenant_id, std::slice::from_ref(&id))
                .await
                .remove(&id),
            None => None,
        };

    Json(json!({
        "approval": {
            "id": row.id,
            "tenant_id": row.tenant_id,
            "server_config_id": row.server_config_id,
            "server_name": row.server_name,
            "capability_type": row.capability_type,
            "capability_name": row.capability_name,
            "requester_type": row.requester_type,
            "requester_user_id": row.requester_user_id,
            "requester_api_key_id": row.requester_api_key_id,
            "requester_group_id": row.requester_group_id,
            "requester_display": row.requester_display,
            "requester_name": requester_name,
            // The run the held call belongs to, so the approver can quote it
            // and an admin can open it (Admin Console → AI System → Runs).
            "run_id": row.originating_trace_root_id,
            "call_arguments": row.call_arguments,
            "request_metadata": row.request_metadata,
            "status": row.status,
            "decision": approval_status::decision(&row.status, row.expires_at, Utc::now()),
            "token_used": approval_status::token_used(&row.status),
            "timeout_seconds": row.timeout_seconds,
            "expires_at": row.expires_at.map(|t| t.to_rfc3339()),
            "resolved_by": row.resolved_by,
            "resolved_at": row.resolved_at.map(|t| t.to_rfc3339()),
            "resolution_note": row.resolution_note,
            "created_at": row.created_at.map(|t| t.to_rfc3339()),
        }
    }))
    .into_response()
}

/// GET /v1/approvals/{id}/poll — Poll approval status (for agents)
///
/// Lightweight endpoint agents can poll to check whether their approval
/// request has been resolved. Returns status and, if approved, the
/// one-time approval_token for re-submitting the tool call.
///
/// The answer includes the token, so it goes to the party the approval
/// belongs to and to nobody else:
///
/// * the caller presents a credential the gateway accepts — the agent's API
///   key or workload token; a portal session where the Portal is composed.
///   No credential, or one that does not stand, is a 401 before the approval
///   is looked up;
/// * the caller is the principal the hold recorded as its requester
///   ([`agent_approval::Requester`], the rule `validate_approval_token`
///   redeems by: the agent, the key, or the person). Another key of the same
///   tenant — of the same resource group, even — is not.
///
/// Whoever is not the requester is answered exactly as for an approval that
/// does not exist, so the route does not say which ids are real.
async fn poll_approval(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Path(approval_id): Path<String>,
) -> Response {
    let tenant_id = match &tenant {
        Some(t) => t.tenant_id.clone(),
        None => return error(StatusCode::BAD_REQUEST, "Missing tenant context"),
    };

    // Agents use API keys; a person polls with the portal session.
    let effective_headers = portal_headers(&headers);
    let client_ip = client_addr.ip();

    let auth_ctx = match auth::authenticate_request(
        &effective_headers,
        &tenant_id,
        &state.config.jwt_secret,
        &state.db,
        &state.credential_verifiers,
        client_ip,
        true, // a credential is required: the answer carries the token
    )
    .await
    {
        Ok(ctx) => ctx,
        Err(e) => return error(e.status_code(), &e.to_string()),
    };

    type PollRow = (
        String,
        Option<String>,
        Option<DateTime<Utc>>,
        String,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
    );
    let row: Option<PollRow> = match sqlx::query_as(
        "SELECT status, approval_token, expires_at, \
                requester_type, requester_user_id, requester_api_key_id, \
                request_metadata->>'agent_id', request_metadata->>'requester_idp_user_id' \
           FROM tool_approval_requests WHERE id = $1 AND tenant_id = $2",
    )
    .bind(&approval_id)
    .bind(&tenant_id)
    .fetch_optional(&state.db)
    .await
    {
        Ok(v) => v,
        Err(e) => return crate::services::db_errors::unavailable("the approval", &e),
    };
    let not_found = || error(StatusCode::NOT_FOUND, "Approval request not found");

    let Some((status, token, expires_at, kind, user_id, api_key_id, agent_id, idp_user_id)) = row
    else {
        return not_found();
    };
    let requester = agent_approval::Requester {
        kind,
        user_id,
        api_key_id,
        agent_id,
        idp_user_id,
    };
    if !requester.is(&auth_ctx) {
        tracing::warn!(
            approval_id = %approval_id,
            requester_kind = %requester.kind,
            api_key_id = ?auth_ctx.api_key_id,
            actor_agent_id = ?auth_ctx.actor_agent_id,
            "approval polled by a principal other than its requester: answered as not found"
        );
        return not_found();
    }

    let mut resp = json!({
        "id": approval_id,
        "status": status,
        "decision": approval_status::decision(&status, expires_at, Utc::now()),
        "token_used": approval_status::token_used(&status),
    });
    if status == approval_status::APPROVED {
        if let Some(t) = token {
            resp["approval_token"] = json!(t);
        }
    }
    if let Some(exp) = expires_at {
        resp["expires_at"] = json!(exp.to_rfc3339());
    }
    Json(resp).into_response()
}

// ---------------------------------------------------------------------------
// The decision (one implementation)
// ---------------------------------------------------------------------------

/// What a person decided.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Decision {
    Approve,
    Reject,
}

impl Decision {
    /// The status the request moves to, and the suffix of the decision row's
    /// `proxy_subtype` (`approval_approved` / `approval_rejected`).
    pub fn status(self) -> &'static str {
        match self {
            Decision::Approve => approval_status::APPROVED,
            Decision::Reject => approval_status::REJECTED,
        }
    }
}

/// Who decided, as recorded on the request (`resolved_by`) and on the
/// decision row. A portal user is their end-user id; an administrator
/// deciding through the control plane is `admin:<id>` so the two id spaces
/// never read as each other.
#[derive(Debug, Clone)]
pub struct Approver {
    pub id: String,
    pub username: String,
}

/// A decision that was made.
#[derive(Debug)]
pub struct Decided {
    pub status: &'static str,
    pub resolved_at: DateTime<Utc>,
    /// The one-time token, for an approval.
    pub approval_token: Option<String>,
}

/// Why a decision was not made. Nothing was changed, except that `Expired`
/// moved a lapsed request to `expired` (and sealed it).
#[derive(Debug)]
pub enum DecideError {
    /// The request was not pending; carries what it is.
    AlreadyResolved(String),
    /// An approval arrived past the deadline.
    Expired,
    /// It stopped being pending between the read and the write.
    NoLongerPending,
    /// The write failed. Not a decision.
    Db(sqlx::Error),
}

/// Decide one pending request: move it, notify the requester, and write the
/// decision row on the originating call's chain — the row that mints the
/// `resolved` evidence record with a human disposition.
///
/// The one implementation behind the portal's approve and reject routes
/// (after their session and group-membership checks) and behind
/// `POST /v1/internal/approvals/decide`, through which the control plane
/// decides on an administrator's behalf. Authorisation is the caller's.
///
/// A decision is made once: both updates carry `status = 'pending'` and are
/// checked, so a decision never lands on top of another or of an expiry, and
/// a failed write is an error, never a 200.
async fn decide(
    state: &SharedState,
    row: &ApprovalRow,
    decision: Decision,
    note: Option<&str>,
    approver: &Approver,
    client_ip: Option<String>,
    started_at: std::time::Instant,
) -> Result<Decided, DecideError> {
    if row.status != approval_status::PENDING {
        return Err(DecideError::AlreadyResolved(row.status.clone()));
    }

    // An approval past its deadline is refused. Rejecting a call nobody may
    // make any more is harmless and is recorded as the verdict it is.
    if decision == Decision::Approve {
        if let Some(exp) = row.expires_at {
            if exp < Utc::now() {
                // Only the transition this call made is sealed: a sweeper
                // that got there first already sealed its own `expired`
                // record. A failed update is not an approval: the request
                // stays lapsed (refused here either way) and the sweeper
                // expires and seals it.
                if let Err(e) = expire_if_lapsed(&state.db, &row.tenant_id, &row.id).await {
                    tracing::warn!(error = %e, approval_id = %row.id, "lazy approval expiry failed");
                }
                return Err(DecideError::Expired);
            }
        }
    }

    let token = (decision == Decision::Approve).then(generate_approval_token);
    let now = Utc::now();

    // Check the write, and only decide what is still
    // pending. Approve used to be `let _ = …` with `WHERE id = $5` alone: a
    // failed write still returned 200 with a token that was never persisted,
    // and with no status predicate a decision could land after a concurrent
    // one or an expiry, quietly reversing it. Reject goes through the same
    // statement.
    // The status is a literal in each statement, never a bind parameter: the
    // guard below (`written_statuses`) reads every value this table's status
    // can be given straight from the source.
    let sql = match decision {
        Decision::Approve => {
            r#"UPDATE tool_approval_requests
               SET status = 'approved', approval_token = $1, resolved_by = $2,
                   resolved_at = $3, resolution_note = $4
               WHERE id = $5 AND tenant_id = $6 AND status = 'pending'"#
        }
        Decision::Reject => {
            r#"UPDATE tool_approval_requests
               SET status = 'rejected', approval_token = $1, resolved_by = $2,
                   resolved_at = $3, resolution_note = $4
               WHERE id = $5 AND tenant_id = $6 AND status = 'pending'"#
        }
    };
    let written = sqlx::query(sql)
        .bind(&token)
        .bind(&approver.id)
        .bind(now)
        .bind(note)
        .bind(&row.id)
        .bind(&row.tenant_id)
        .execute(&state.db)
        .await;
    match written {
        Ok(r) if r.rows_affected() == 1 => {}
        Ok(_) => return Err(DecideError::NoLongerPending),
        Err(e) => return Err(DecideError::Db(e)),
    }

    // Notify the requester (if different from the approver). Best-effort: a
    // non-numeric requester id is skipped, never coerced to user 0 (which
    // would deliver another user's notice to whoever holds id 0).
    if let Some(ref requester_user_id) = row.requester_user_id {
        if requester_user_id != &approver.id {
            match requester_user_id.parse::<i32>() {
                Ok(user_id_int) => {
                    let title = format!("Tool '{}' {}", row.capability_name, decision.status());
                    let mut body_text = format!(
                        "Your request to use {} on {} was {} by {}.",
                        row.capability_name,
                        row.server_name.as_deref().unwrap_or("unknown server"),
                        decision.status(),
                        approver.username,
                    );
                    if let (Decision::Reject, Some(n)) = (decision, note) {
                        body_text.push_str(&format!(" Reason: {}", n));
                    }
                    let metadata = json!({
                        "approval_request_id": row.id,
                        "tool_name": row.capability_name,
                        "status": decision.status(),
                    });

                    let _ = sqlx::query(
                        r#"INSERT INTO notifications (id, tenant_id, user_id, notification_type, title, body, metadata, is_read)
                           VALUES ($1, $2, $3, 'tool_approval_resolved', $4, $5, $6, FALSE)"#,
                    )
                    .bind(generate_ulid())
                    .bind(&row.tenant_id)
                    .bind(user_id_int)
                    .bind(&title)
                    .bind(&body_text)
                    .bind(&metadata)
                    .execute(&state.db)
                    .await;
                }
                Err(_) => tracing::warn!(
                    requester_user_id = %requester_user_id,
                    "skipping approval decision notification: non-numeric requester id"
                ),
            }
        }
    }

    tracing::debug!(
        approval_id = %row.id,
        approver = %approver.id,
        capability = %row.capability_name,
        decision = decision.status(),
        "tool call approval decided"
    );

    write_approval_decision_log(
        state,
        row,
        &approver.id,
        &approver.username,
        &row.tenant_id,
        client_ip,
        &row.id,
        decision.status(),
        note,
        started_at.elapsed().as_secs_f64() * 1000.0,
    )
    .await;

    Ok(Decided {
        status: decision.status(),
        resolved_at: now,
        approval_token: token,
    })
}

async fn load_row(
    db: &sqlx::PgPool,
    tenant_id: &str,
    approval_id: &str,
) -> Result<Option<ApprovalRow>, sqlx::Error> {
    sqlx::query_as("SELECT * FROM tool_approval_requests WHERE id = $1 AND tenant_id = $2")
        .bind(approval_id)
        .bind(tenant_id)
        .fetch_optional(db)
        .await
}

/// The portal routes: a group member decides with their portal session.
async fn decide_as_portal_user(
    state: SharedState,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    approval_id: String,
    body: Option<Json<ResolveBody>>,
    decision: Decision,
) -> Response {
    let started_at = std::time::Instant::now();
    let client_ip = client_addr.ip();
    let client_ip_for_log = client_ip.clone();
    let (tenant_id, user_id, username) =
        match crate::routes::request::extract_portal_user(&state, &headers, &tenant, client_ip)
            .await
        {
            Ok(v) => v,
            Err(r) => return r,
        };

    let group_ids = match request::get_user_group_ids(&state.db, &user_id, &tenant_id).await {
        Ok(v) => v,
        Err(e) => return crate::services::db_errors::unavailable("your resource groups", &e),
    };

    let row = match load_row(&state.db, &tenant_id, &approval_id).await {
        Ok(Some(r)) => r,
        Ok(None) => return error(StatusCode::NOT_FOUND, "Approval request not found"),
        Err(e) => return crate::services::db_errors::unavailable("the approval", &e),
    };

    let (verb, noun) = match decision {
        Decision::Approve => ("approve", "approval"),
        Decision::Reject => ("reject", "rejection"),
    };
    if !group_ids.contains(&row.requester_group_id) {
        return error(
            StatusCode::FORBIDDEN,
            &format!("You must be a member of the resource group to {verb} this request"),
        );
    }

    let note = body.as_ref().and_then(|b| b.note.clone());
    let approver = Approver {
        id: user_id.clone(),
        username,
    };
    let decided = match decide(
        &state,
        &row,
        decision,
        note.as_deref(),
        &approver,
        client_ip_for_log,
        started_at,
    )
    .await
    {
        Ok(d) => d,
        Err(DecideError::AlreadyResolved(status)) => {
            return error(
                StatusCode::CONFLICT,
                &format!("Request already resolved with status: {status}"),
            );
        }
        Err(DecideError::Expired) => {
            return error(StatusCode::GONE, "Approval request has expired");
        }
        Err(DecideError::NoLongerPending) => {
            return error(
                StatusCode::CONFLICT,
                "Approval is no longer pending — it was already resolved or has expired",
            );
        }
        Err(DecideError::Db(e)) => {
            tracing::error!(error = %e, approval_id = %approval_id, "failed to record {noun}");
            return error(
                StatusCode::INTERNAL_SERVER_ERROR,
                &format!("Failed to record {noun}"),
            );
        }
    };

    let mut resp = json!({
        "id": approval_id,
        "status": decided.status,
        "resolved_by": user_id,
        "resolved_at": decided.resolved_at.to_rfc3339(),
        "message": format!("Tool call '{}' {}", row.capability_name, decided.status),
    });
    if let Some(token) = decided.approval_token {
        resp["approval_token"] = json!(token);
    }
    Json(resp).into_response()
}

/// POST /v1/approvals/{id}/approve — Approve a tool call (group member)
async fn approve_request(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Path(approval_id): Path<String>,
    body: Option<Json<ResolveBody>>,
) -> Response {
    decide_as_portal_user(
        state,
        client_addr,
        headers,
        tenant,
        approval_id,
        body,
        Decision::Approve,
    )
    .await
}

/// POST /v1/approvals/{id}/reject — Reject a tool call (group member)
async fn reject_request(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Path(approval_id): Path<String>,
    body: Option<Json<ResolveBody>>,
) -> Response {
    decide_as_portal_user(
        state,
        client_addr,
        headers,
        tenant,
        approval_id,
        body,
        Decision::Reject,
    )
    .await
}

// ---------------------------------------------------------------------------
// The control plane decides on an administrator's behalf
// ---------------------------------------------------------------------------

/// Outcomes of `POST /v1/internal/approvals/decide` (response member
/// `outcome`). Wire contract with the control plane
/// (`app/constants/approval_decision.py`).
pub mod decide_outcome {
    pub const APPROVED: &str = "approved";
    pub const REJECTED: &str = "rejected";
    pub const EXPIRED: &str = "expired";
    pub const ALREADY_RESOLVED: &str = "already_resolved";
    pub const NO_LONGER_PENDING: &str = "no_longer_pending";
    pub const NOT_FOUND: &str = "not_found";
}

/// Mounted at `/v1/internal/approvals`: system callers only.
pub fn internal_router() -> Router<SharedState> {
    Router::new().route("/decide", post(decide_internal))
}

#[derive(Debug, Deserialize)]
struct InternalApprover {
    id: String,
    username: String,
}

#[derive(Debug, Deserialize)]
struct InternalDecideRequest {
    tenant_id: String,
    approval_request_id: String,
    decision: String,
    #[serde(default)]
    note: Option<String>,
    approver: InternalApprover,
}

/// POST /v1/internal/approvals/decide — the control plane, having authorised
/// an administrator (`tool_approval:manage`), asks the core to decide.
///
/// The control plane does not write the request itself: a decision made
/// outside the core has no decision row, so no `resolved` evidence record,
/// and a lapsed request "expired" there is never sealed.
///
/// Only the platform service key may call. Every outcome of the decision
/// itself is a 200 with an `outcome` member — so the caller never mistakes a
/// proxy's or an older core's 404 for "no such request"; HTTP errors mean
/// the question was not answered (401/403 caller, 422 malformed, 503 store).
async fn decide_internal(
    State(state): State<SharedState>,
    headers: HeaderMap,
    Json(body): Json<serde_json::Value>,
) -> Response {
    let started_at = std::time::Instant::now();
    if let Err(r) = crate::routes::evidence::require_system_caller(&state, &headers).await {
        return r;
    }
    let req: InternalDecideRequest = match serde_json::from_value(body) {
        Ok(r) => r,
        Err(e) => return error(StatusCode::UNPROCESSABLE_ENTITY, &e.to_string()),
    };
    let decision = match req.decision.as_str() {
        "approve" => Decision::Approve,
        "reject" => Decision::Reject,
        other => {
            return error(
                StatusCode::UNPROCESSABLE_ENTITY,
                &format!("decision must be approve or reject, got {other:?}"),
            );
        }
    };
    // A decision is somebody's. One nobody made is refused.
    let (admin_id, username) = (req.approver.id.trim(), req.approver.username.trim());
    if admin_id.is_empty() || username.is_empty() {
        return error(
            StatusCode::UNPROCESSABLE_ENTITY,
            "approver.id and approver.username are required",
        );
    }
    let approver = Approver {
        id: format!("admin:{admin_id}"),
        username: username.to_string(),
    };

    let row = match load_row(&state.db, &req.tenant_id, &req.approval_request_id).await {
        Ok(Some(r)) => r,
        Ok(None) => return Json(json!({"outcome": decide_outcome::NOT_FOUND})).into_response(),
        Err(e) => return crate::services::db_errors::unavailable("the approval", &e),
    };

    match decide(
        &state,
        &row,
        decision,
        req.note.as_deref(),
        &approver,
        None,
        started_at,
    )
    .await
    {
        Ok(d) => {
            let mut resp = json!({
                "outcome": d.status,
                "id": row.id,
                "status": d.status,
                "resolved_by": approver.id,
                "resolved_at": d.resolved_at.to_rfc3339(),
            });
            if let Some(token) = d.approval_token {
                resp["approval_token"] = json!(token);
            }
            Json(resp).into_response()
        }
        Err(DecideError::AlreadyResolved(status)) => Json(json!({
            "outcome": decide_outcome::ALREADY_RESOLVED,
            "status": status,
        }))
        .into_response(),
        Err(DecideError::Expired) => {
            Json(json!({"outcome": decide_outcome::EXPIRED})).into_response()
        }
        Err(DecideError::NoLongerPending) => {
            Json(json!({"outcome": decide_outcome::NO_LONGER_PENDING})).into_response()
        }
        Err(DecideError::Db(e)) => crate::services::db_errors::unavailable("the approval", &e),
    }
}

// ---------------------------------------------------------------------------
// Expiry of one request (the lazy check)
// ---------------------------------------------------------------------------

/// Move one request to `expired` if it is still pending and past its
/// deadline, and seal the `expired` record for the transition this call
/// made. Returns whether it moved the row.
///
/// The database decides both conditions in the one statement, so a caller
/// cannot expire a request that is inside its window or already resolved,
/// and two callers (this, the sweeper, the control plane through
/// `POST /v1/evidence/approvals/expire`) never seal the same expiry twice.
pub async fn expire_if_lapsed(
    db: &sqlx::PgPool,
    tenant_id: &str,
    approval_id: &str,
) -> Result<bool, sqlx::Error> {
    let moved = sqlx::query(
        "UPDATE tool_approval_requests SET status = 'expired' \
         WHERE id = $1 AND tenant_id = $2 AND status = 'pending' AND expires_at < NOW()",
    )
    .bind(approval_id)
    .bind(tenant_id)
    .execute(db)
    .await?
    .rows_affected()
        > 0;
    if moved {
        crate::services::evidence::approval::seal_expired(
            db,
            &[(approval_id.to_string(), tenant_id.to_string())],
        )
        .await;
    }
    Ok(moved)
}

// ---------------------------------------------------------------------------
// Background task: expire pending approvals
// ---------------------------------------------------------------------------

/// Expire pending approval requests that have passed their deadline.
/// Call this periodically from main.rs (every 60 seconds).
///
/// Uses `FOR UPDATE SKIP LOCKED` to prevent multiple pods from processing
/// the same expired rows concurrently.
pub async fn expire_pending_approvals(db: &sqlx::PgPool) -> Result<u64, sqlx::Error> {
    // RETURNING feeds the assurance inbox: an approval nobody
    // actioned inside its window is a human-process finding.
    let rows: Vec<(String, String, String)> = sqlx::query_as(
        r#"WITH expired AS (
            SELECT id FROM tool_approval_requests
            WHERE status = 'pending' AND expires_at < NOW()
            FOR UPDATE SKIP LOCKED
        )
        UPDATE tool_approval_requests SET status = 'expired'
        WHERE id IN (SELECT id FROM expired)
        RETURNING id, tenant_id, capability_name"#,
    )
    .fetch_all(db)
    .await?;

    for (id, tenant_id, capability_name) in &rows {
        crate::services::assurance_inbox::raise_approval_timeout(
            db,
            tenant_id,
            id,
            Some(capability_name.as_str()),
        )
        .await;
    }

    // The lapse is a verdict: seal `expired`, chained
    // `supersedes` to the `awaiting_human` record raised at enqueue.
    let expired: Vec<(String, String)> = rows
        .iter()
        .map(|(id, tenant_id, _)| (id.clone(), tenant_id.clone()))
        .collect();
    crate::services::evidence::approval::seal_expired(db, &expired).await;

    Ok(rows.len() as u64)
}

#[cfg(test)]
mod tests {
    use super::approval_status::{self, *};
    use chrono::{Duration, Utc};
    use regex_lite::Regex;
    use std::collections::BTreeSet;
    use std::path::Path;

    #[test]
    fn approved_filter_includes_redeemed_approvals() {
        let approved = filter_statuses(APPROVED).unwrap().unwrap();
        assert!(approved.contains(&APPROVED));
        assert!(
            approved.contains(&CONSUMED),
            "a redeemed token is still an approval"
        );
        assert_eq!(filter_statuses(CONSUMED).unwrap(), Some(&[CONSUMED][..]));
        assert_eq!(filter_statuses(FILTER_ALL).unwrap(), None);
        assert!(filter_statuses("bogus").is_err());
        assert!(filter_statuses("").is_err());
    }

    #[test]
    fn every_status_is_reachable_through_a_filter_and_has_a_decision() {
        let now = Utc::now();
        for s in ALL {
            let reachable = FILTERS
                .iter()
                .filter(|f| **f != FILTER_ALL)
                .any(|f| filter_statuses(f).unwrap().unwrap().contains(s));
            assert!(reachable, "status {s} is not selected by any filter");
            assert_ne!(
                decision(s, None, now),
                "unknown",
                "status {s} has no decision"
            );
        }
    }

    #[test]
    fn decision_is_derived_from_status() {
        let now = Utc::now();
        let past = Some(now - Duration::minutes(1));
        let future = Some(now + Duration::minutes(1));
        assert_eq!(decision(CONSUMED, past, now), "approved");
        assert_eq!(decision(APPROVED, None, now), "approved");
        assert_eq!(decision(REJECTED, None, now), "rejected");
        assert_eq!(decision(EXPIRED, None, now), "expired");
        assert_eq!(decision(PENDING, future, now), "pending");
        assert_eq!(
            decision(PENDING, past, now),
            "expired",
            "a lapsed hold is not actionable"
        );
        assert_eq!(decision("escalated", None, now), "unknown");
        assert!(token_used(CONSUMED));
        assert!(!token_used(APPROVED));
    }

    /// Everything under `src/` that writes `tool_approval_requests.status`:
    /// `(file, written value)`. A value that is a bind parameter rather than
    /// a literal is returned as-is (e.g. `$3`) so the guard can refuse it.
    fn written_statuses(root: &Path) -> Vec<(String, String)> {
        // Patterns are split with `\s+` so this file's own source never
        // matches them.
        let update = Regex::new(
            r"(?is)UPDATE\s+tool_approval_requests\s+SET\s+(.*?)(?:\bWHERE\b|\bRETURNING\b|$)",
        )
        .unwrap();
        let assign = Regex::new(r"(?i)(?:^|[\s,])status\s*=\s*('[^']*'|\$\d+|[A-Za-z_]+)").unwrap();
        let insert = Regex::new(
            r"(?is)INSERT\s+INTO\s+tool_approval_requests\s*\((.*?)\)\s*VALUES\s*\((.*?)\)",
        )
        .unwrap();

        let mut out = Vec::new();
        let mut stack = vec![root.to_path_buf()];
        while let Some(dir) = stack.pop() {
            for entry in std::fs::read_dir(&dir).unwrap() {
                let path = entry.unwrap().path();
                if path.is_dir() {
                    stack.push(path);
                    continue;
                }
                if path.extension().and_then(|e| e.to_str()) != Some("rs") {
                    continue;
                }
                let src = std::fs::read_to_string(&path).unwrap();
                let file = path.display().to_string();
                // A SQL statement lives inside one string literal; cut each
                // match at the literal's closing quote.
                let literals = src.split('"');
                for lit in literals {
                    if let Some(c) = update.captures(lit) {
                        for a in assign.captures_iter(&c[1]) {
                            out.push((file.clone(), a[1].trim_matches('\'').to_string()));
                        }
                    }
                    if let Some(c) = insert.captures(lit) {
                        let cols: Vec<&str> = c[1].split(',').map(str::trim).collect();
                        let vals: Vec<&str> = c[2].split(',').map(str::trim).collect();
                        let idx = cols.iter().position(|c| c.eq_ignore_ascii_case("status"));
                        match idx.and_then(|i| vals.get(i)) {
                            Some(v) => out.push((file.clone(), v.trim_matches('\'').to_string())),
                            None => {
                                out.push((file.clone(), "<insert without a status literal>".into()))
                            }
                        }
                    }
                }
            }
        }
        out
    }

    /// Static guard: every status the core can write must be in the shared
    /// vocabulary. `consumed` was written by the token-redeem path but known
    /// to neither the list filter nor the Portal, so nearly every real
    /// approval vanished from the Approved view.
    #[test]
    fn every_written_status_is_in_the_vocabulary() {
        let src = Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
        let writes = written_statuses(&src);
        let written: BTreeSet<&str> = writes.iter().map(|(_, s)| s.as_str()).collect();

        // The scanner must actually see the known writers, or a broken regex
        // would make this test pass vacuously.
        for expected in approval_status::ALL {
            assert!(
                written.contains(expected),
                "scanner found no write of '{expected}' — it is either dead vocabulary or the scanner is broken; found {writes:?}"
            );
        }

        let unknown: Vec<&(String, String)> = writes
            .iter()
            .filter(|(_, s)| !approval_status::ALL.contains(&s.as_str()))
            .collect();
        assert!(
            unknown.is_empty(),
            "tool_approval_requests.status written with a value outside approval_status::ALL \
             (add it to the vocabulary, the list filter and the Portal's approvalStatus.ts): {unknown:?}"
        );
    }
}
