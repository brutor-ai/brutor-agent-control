// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # A2A Protocol v1.0 — HTTP/REST route handlers.
//!
//! Exposes the eleven canonical methods under:
//!
//! ```text
//!   POST   /a2a/{tenant_id}/{card_name}/message:send
//!   POST   /a2a/{tenant_id}/{card_name}/message:stream                 (SSE)
//!   GET    /a2a/{tenant_id}/{card_name}/tasks
//!   GET    /a2a/{tenant_id}/{card_name}/tasks/{task_id}
//!   POST   /a2a/{tenant_id}/{card_name}/tasks/{task_id}:cancel
//!   GET    /a2a/{tenant_id}/{card_name}/tasks/{task_id}:subscribe      (SSE)
//!   GET    /a2a/{tenant_id}/{card_name}/extendedAgentCard
//!   POST   /a2a/{tenant_id}/{card_name}/tasks/{task_id}/pushNotificationConfigs
//!   GET    /a2a/{tenant_id}/{card_name}/tasks/{task_id}/pushNotificationConfigs
//!   GET    /a2a/{tenant_id}/{card_name}/tasks/{task_id}/pushNotificationConfigs/{config_id}
//!   DELETE /a2a/{tenant_id}/{card_name}/tasks/{task_id}/pushNotificationConfigs/{config_id}
//! ```
//!
//! The tenant segment replaces the spec's `/{tenant}` optional prefix
//! (section 3.6.1 of the A2A specification), extended with `/{card_name}` so a single tenant can host
//! multiple agents. The tenant middleware skips tenant resolution for
//! the `/a2a/` prefix — every handler extracts tenant + card from the
//! path, authenticates the caller, and loads the card from DB.
//!
//! Every error path emits the spec's `google.rpc.Status` envelope
//! (section 11.6) via [`A2aError::into_response`] — never the proxy's OpenAI
//! `{"error": "..."}` shape. Routes never leak v0.3 dialect.

use std::time::{Duration, Instant};

use axum::Router;
use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::response::{IntoResponse, Json, Response};
use axum::routing::{get, post};
use chrono::{DateTime, Utc};
use serde::Deserialize;
use serde_json::Value;
use sqlx::PgPool;
use tokio_stream::Stream;
use tokio_stream::wrappers::ReceiverStream;
use ulid::Ulid;

use crate::constants;
use crate::middleware::tenant::TenantContext;
use crate::models::auth::AuthContext;
use crate::models::proxy::SkillExecuteRequest;
use crate::routes::route_helpers;
use crate::services::a2a_v1::errors::{A2aError, A2aErrorKind};
use crate::services::a2a_v1::guardrail as a2a_guardrail;
use crate::services::a2a_v1::{
    AgentCard, Artifact, CancelTaskRequest, DeleteTaskPushNotificationConfigRequest,
    GetTaskPushNotificationConfigRequest, GetTaskRequest, ListTasksRequest, ListTasksResponse,
    Message, Part, PartContent, Role, SendMessageRequest, SendMessageResponse, StreamResponse,
    SubscribeToTaskRequest, Task, TaskArtifactUpdateEvent, TaskPushNotificationConfig, TaskState,
    TaskStatus, TaskStatusUpdateEvent, push_store, task_store, version,
};
use crate::services::run_controls::RunScope;
use crate::services::{
    SharedState, a2a_governance, agent_approval, auth, call_chain, proxy_logger, skill_orchestrator,
};

/// Proxy subtype for v1.0 inbound traffic — distinct from the legacy
/// v0.3 `PROXY_SUBTYPE_INBOUND` so Mission Control can pivot on
/// dialect when reading `proxy_logs`.
pub const PROXY_SUBTYPE_V1_INBOUND: &str = "v1-inbound";

/// Retention for an SSE stream that never finds a terminal state.
/// Section 3.3.3 of the A2A specification leaves this to the server; 5 minutes matches our
/// other long-polling surfaces (portal approvals, batch pollers).
const STREAM_MAX_DURATION: Duration = Duration::from_secs(300);

/// Safety-net reconcile interval. The stream is woken event-driven by the
/// `A2aStreamHub` (fed by `pg_notify` on every task write, cross-replica), so
/// this is NOT the primary latency path — it's the fallback that re-reads the
/// task in case a NOTIFY was missed (listener mid-reconnect, or a write that
/// landed in the window between the caller's read and the stream's subscribe).
/// Seconds, not milliseconds: event wakes carry the live updates; this only
/// has to bound worst-case staleness, not feel live.
const STREAM_SAFETY_NET_INTERVAL: Duration = Duration::from_secs(5);

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

/// Full v1.0 HTTP/REST router, mounted by `main.rs` at `/a2a`.
///
/// axum 0.8 forbids mixing a path parameter with a literal suffix in
/// the same segment (`{task_id}:cancel` is rejected). The spec's verb
/// suffix form (`/tasks/{id}:cancel`, `/tasks/{id}:subscribe`) is
/// therefore matched via a single `{task_ref}` parameter and split
/// inside `tasks_get_dispatch` / `tasks_post_dispatch`. The canonical
/// URL shape is preserved — the suffix just isn't its own path segment.
pub fn router() -> Router<SharedState> {
    Router::new()
        .route("/{tenant_id}/{card_name}/message:send", post(send_message))
        .route(
            "/{tenant_id}/{card_name}/message:stream",
            post(send_streaming_message),
        )
        .route("/{tenant_id}/{card_name}/tasks", get(list_tasks))
        .route(
            "/{tenant_id}/{card_name}/tasks/{task_ref}",
            get(tasks_get_dispatch).post(tasks_post_dispatch),
        )
        .route(
            "/{tenant_id}/{card_name}/extendedAgentCard",
            get(get_extended_agent_card),
        )
        .route(
            "/{tenant_id}/{card_name}/tasks/{task_id}/pushNotificationConfigs",
            post(create_push_config).get(list_push_configs),
        )
        .route(
            "/{tenant_id}/{card_name}/tasks/{task_id}/pushNotificationConfigs/{config_id}",
            get(get_push_config).delete(delete_push_config),
        )
}

/// Split `{task_ref}` into `(task_id, verb)` where the verb is the
/// portion after the first `:` (or `None` for the bare-id form).
fn split_task_ref(raw: &str) -> (String, Option<String>) {
    match raw.split_once(':') {
        Some((id, verb)) => (id.to_string(), Some(verb.to_string())),
        None => (raw.to_string(), None),
    }
}

/// Route-level dispatcher for `GET /tasks/{task_ref}`.
///
/// `task_ref` ends up as one of:
///   * `"abc123"` → [`get_task`]
///   * `"abc123:subscribe"` → [`subscribe_to_task`]
///
/// Any other suffix surfaces a spec `UNSUPPORTED_OPERATION`.
pub(crate) async fn tasks_get_dispatch(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    Path((tenant_id, card_name, task_ref)): Path<(String, String, String)>,
    Query(q): Query<GetTaskQuery>,
    headers: HeaderMap,
) -> Response {
    let (task_id, verb) = split_task_ref(&task_ref);
    match verb.as_deref() {
        None => {
            get_task(
                State(state),
                client_addr,
                Path((tenant_id, card_name, task_id)),
                Query(q),
                headers,
            )
            .await
        }
        Some("subscribe") => {
            subscribe_to_task(
                State(state),
                client_addr,
                Path((tenant_id, card_name, task_id)),
                headers,
            )
            .await
        }
        Some(other) => A2aError::new(
            A2aErrorKind::UnsupportedOperation,
            format!("Unknown task verb `{other}`"),
        )
        .into_response(),
    }
}

/// Route-level dispatcher for `POST /tasks/{task_ref}`.
///
/// `task_ref` must carry a verb:
///   * `"abc123:cancel"` → [`cancel_task`]
///
/// No spec operation posts to the bare id form, so a missing verb
/// surfaces `UNSUPPORTED_OPERATION`.
pub(crate) async fn tasks_post_dispatch(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    Path((tenant_id, card_name, task_ref)): Path<(String, String, String)>,
    headers: HeaderMap,
) -> Response {
    let (task_id, verb) = split_task_ref(&task_ref);
    match verb.as_deref() {
        Some("cancel") => {
            cancel_task(
                State(state),
                client_addr,
                Path((tenant_id, card_name, task_id)),
                headers,
            )
            .await
        }
        None => A2aError::new(
            A2aErrorKind::UnsupportedOperation,
            "POST to /tasks/{id} requires a verb suffix (e.g. `:cancel`)",
        )
        .into_response(),
        Some(other) => A2aError::new(
            A2aErrorKind::UnsupportedOperation,
            format!("Unknown task verb `{other}`"),
        )
        .into_response(),
    }
}

// ---------------------------------------------------------------------------
// DB rows — local to this module.
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, sqlx::FromRow)]
struct AgentCardRow {
    pub id: String,
    #[allow(dead_code)]
    pub tenant_id: String,
    #[allow(dead_code)]
    pub name: String,
    #[allow(dead_code)]
    pub card_json: Value,
    pub trust_tier: String,
    pub enabled: bool,
    pub revoked_at: Option<DateTime<Utc>>,
    pub extended_card_json: Option<Value>,
    /// The card's own A2A limits: one aggregate for the card, whatever
    /// route reaches it.
    pub tenant_limits: Option<Value>,
    /// The card's own A2A policy, judged with the caller's group chain
    /// (`services::a2a_governance`).
    pub tenant_governance: Option<Value>,
    /// The card's capabilities, in name order (off the card cache).
    pub capabilities: Vec<crate::services::cache::AgentCardCapability>,
}

// ---------------------------------------------------------------------------
// Card + auth resolution shared across handlers.
// ---------------------------------------------------------------------------

/// Outcome of the per-request setup: tenant context, auth context,
/// card row. Every handler walks through [`resolve_request`] first.
struct RequestCtx {
    #[allow(dead_code)]
    tenant: TenantContext,
    auth: AuthContext,
    card: AgentCardRow,
    correlation_id: String,
    resolved_group_id: Option<String>,
    /// Root derived from a client-asserted `x-brutor-run-id`, when the caller
    /// supplied one and no signed chain was present.
    ///
    /// Computed in `resolve_request` because that is where the headers are;
    /// consumed in `log_and_finalise_with_path`, which only sees the context.
    /// Without it an A2A step stamps the task id as its own root and drops out
    /// of the run its caller is building — the agent's escalation would show
    /// up as a separate run from the task that triggered it.
    asserted_run_root: Option<Ulid>,
    /// The run this call is a step of, decided BEFORE any gate (see
    /// [`V1Target`]): the asserted run, else the task the call lands on.
    /// The run controls check it, an approval hold names it, and every row
    /// of the call carries it.
    run_root: Option<String>,
    /// The id the task a `message:send` / `message:stream` creates adopts,
    /// minted before the gates so a brand-new run can be checked and held
    /// under the id it will have. `None` when the message names its task.
    new_task_id: Option<String>,
    /// The action a message was admitted as against its run's action
    /// ceiling (see `run_controls::check`): kept when the handler is
    /// dispatched ([`RequestCtx::step_dispatched`]), given back when the
    /// context is dropped before that (a later gate refused or held the
    /// message). Empty for a verb that is no step.
    step: std::sync::Mutex<Option<crate::services::run_controls::StepAdmitted>>,
    /// The capability a message is dispatched to, decided once, before the
    /// gates: the A2A policy judges its data classification, its hourly
    /// rate is admitted, and the handler that runs is its handler. `None`
    /// for a verb that is no message and for a card with no bound
    /// capability (nothing is dispatched).
    capability: Option<BoundCapability>,
    /// The request's approvals (`agent_approval::RequestApprovals`): the
    /// tokens it carries and what the gates above had answered, handed to
    /// the skill the message is dispatched to.
    approvals: std::sync::Arc<agent_approval::RequestApprovals>,
    start: Instant,
}

impl RequestCtx {
    /// The message's handler is dispatched: its run's action stays counted.
    async fn step_dispatched(&self) {
        let step = self.step.lock().ok().and_then(|mut s| s.take());
        if let Some(step) = step {
            step.commit().await;
        }
    }
}

/// What a v1.0 verb addresses — which decides the run the call is a step of.
///
/// A call that asserts a run (`x-brutor-run-id`) belongs to that run. One
/// that asserts none belongs to the task it works on: the rows stamp the
/// task as its own root (`write_v1_row`), so that is the run the ledger
/// shows and an operator aborts. For a `message:send` that starts a task,
/// that task did not exist when the gates ran, so the call used to reach
/// the run controls and the approval gate with no run at all — an aborted
/// or capped run was never checked on it and its hold named no run. The
/// gateway now mints the task id first; the new run is checked (an abort
/// or a cap by system applies; its budget is trivially within limits), a
/// hold names it, and the created task adopts the id.
pub(crate) enum V1Target<'a> {
    /// `message:send` / `message:stream`: the task the message lands on —
    /// the one it names, or a new one.
    Message(&'a Message),
    /// A verb on an existing task (get, cancel, subscribe, push configs).
    Task(&'a str),
    /// No task (list, extended card): only an asserted run applies.
    NoTask,
}

impl V1Target<'_> {
    /// Whether the call does work in its run — a message the agent acts
    /// on. Only such a call asks the run controls ("may this run take
    /// another step"): reading, listing, subscribing to or cancelling a
    /// task is no step, and an aborted run's caller must still be able to
    /// read and cancel the task an operator stopped.
    fn is_step(&self) -> bool {
        matches!(self, V1Target::Message(_))
    }
}

/// The run of a v1.0 call and, for a message that starts a task, the id
/// that task will adopt. A named task the caller may not see is `NotFound`
/// here, before any gate reads or writes the run it belongs to — the same
/// answer the verb itself would give (no existence leak).
async fn resolve_v1_run(
    state: &SharedState,
    tenant_id: &str,
    caller_group: Option<&str>,
    asserted: Option<Ulid>,
    target: &V1Target<'_>,
) -> Result<(Option<String>, Option<String>), A2aError> {
    let new_task_id = match target {
        V1Target::Message(m) if m.task_id.is_none() => Some(Ulid::new().to_string()),
        _ => None,
    };
    let named = match target {
        V1Target::Message(m) => m.task_id.as_deref(),
        V1Target::Task(id) => Some(*id),
        V1Target::NoTask => None,
    };
    let Some(id) = named else {
        return Ok(match asserted {
            Some(root) => (Some(root.to_string()), new_task_id),
            // A new task: its minted id is the run. No task at all: no run.
            None => (new_task_id.clone(), new_task_id),
        });
    };
    // Whether the caller may see the named task is decided for the task,
    // whatever run the caller asserts.
    let task_run = match task_store::visibility(&state.db, tenant_id, id, caller_group).await {
        Ok(Some(true)) => (Some(id.to_string()), None),
        // Another group's task: the answer the verb itself would give, before
        // any gate reads or writes that task's run.
        Ok(Some(false)) => {
            return Err(task_not_found(id));
        }
        Ok(None) => match target {
            // A message naming an unknown id creates the task under it
            // (callers may pre-allocate ids): that id is the new run.
            V1Target::Message(_) => (Some(id.to_string()), None),
            // A verb on a task that does not exist is no step of any run;
            // the verb answers NotFound itself.
            _ => (None, None),
        },
        Err(e) => {
            tracing::error!(error = %e, "a2a_v1: task lookup failed");
            return Err(A2aError::new(
                A2aErrorKind::InvalidAgentResponse,
                "Task lookup failed",
            ));
        }
    };
    Ok(match asserted {
        Some(root) => (Some(root.to_string()), new_task_id),
        None => task_run,
    })
}

/// What a verb answers for a task that does not exist and for one the caller
/// may not see: one answer.
fn task_not_found(task_id: &str) -> A2aError {
    A2aError::new(
        A2aErrorKind::TaskNotFound,
        "The specified task id does not exist",
    )
    .with_metadata("taskId", task_id)
}

/// The task a verb names, when the caller may see it
/// (`task_store::get_scoped`: the caller's group). Every verb that serves a
/// task or something that hangs off one (its stream, its push
/// configurations) reads it through here.
async fn visible_task(
    state: &SharedState,
    tenant_id: &str,
    task_id: &str,
    ctx: &RequestCtx,
) -> Result<Task, A2aError> {
    match task_store::get_scoped(
        &state.db,
        tenant_id,
        task_id,
        ctx.resolved_group_id.as_deref(),
    )
    .await
    {
        Ok(task) => Ok(task),
        Err(task_store::TaskStoreError::NotFound) => Err(task_not_found(task_id)),
        Err(e) => {
            tracing::error!(error = %e, "a2a_v1: task lookup failed");
            Err(A2aError::new(
                A2aErrorKind::InvalidAgentResponse,
                "Task lookup failed",
            ))
        }
    }
}

/// Common prelude: tenant status check → feature gate → auth →
/// card lookup + enabled/revoked gates → group-access gate.
///
/// On failure, returns the spec-shaped error envelope (`A2aError`)
/// directly so handlers can `?` their way through.
/// An authentication or identity refusal as an A2A error. All failure modes
/// (missing/invalid key, invalid JWT) surface as UNAUTHENTICATED — the HTTP
/// binding uses 401 for these and the spec keeps the table of A2A-specific
/// errors (section 5.4) free of auth entries. By the status authentication
/// assigned (401 / 403), not UNSUPPORTED_OPERATION / 400 for every failure.
fn a2a_auth_error(e: &auth::AuthError) -> A2aError {
    A2aError::new(
        A2aErrorKind::for_auth_status(e.status_code()),
        e.to_string(),
    )
    .with_metadata("code", e.status_code().as_u16().to_string())
}

async fn resolve_request(
    state: &SharedState,
    headers: &HeaderMap,
    client_ip: Option<String>,
    tenant_id: &str,
    card_name: &str,
    target: V1Target<'_>,
) -> Result<RequestCtx, A2aError> {
    let start = Instant::now();
    let correlation_id = route_helpers::extract_correlation_id(headers);

    // Tenant status — a suspended tenant is blocked here, and so is one the
    // installation's tenant gate refuses (the Enterprise Edition's expired
    // trial; none in the Community Edition).
    let tenant = load_tenant_context(&state.db, tenant_id).await?;
    if tenant.is_blocked() {
        return Err(
            A2aError::new(A2aErrorKind::UnsupportedOperation, "Tenant is suspended")
                .with_metadata("tenantId", tenant_id),
        );
    }
    if state.tenant_gate.gates() {
        match state.tenant_gate.check(&state.db, tenant_id).await {
            Ok(None) => {}
            Ok(Some(block)) => {
                return Err(
                    A2aError::new(A2aErrorKind::UnsupportedOperation, block.message)
                        .with_metadata("tenantId", tenant_id)
                        .with_metadata("reason", block.code),
                );
            }
            Err(crate::services::tenant_gate::TenantGateUnavailable) => {
                return Err(A2aError::new(
                    A2aErrorKind::InvalidAgentResponse,
                    "Tenant status lookup failed",
                ));
            }
        }
    }

    // A2A feature gate.
    if !check_a2a_enabled(state, tenant_id).await? {
        return Err(A2aError::new(
            A2aErrorKind::UnsupportedOperation,
            "A2A is not enabled for this tenant",
        )
        .with_metadata("tenantId", tenant_id));
    }

    // Auth — same waterfall as the rest of the proxy — and the identity
    // judge.
    let mut auth_ctx = auth::authenticate_request(
        headers,
        tenant_id,
        &state.config.jwt_secret,
        &state.db,
        &state.credential_verifiers,
        client_ip,
        true,
    )
    .await
    .map_err(|e| a2a_auth_error(&e))?;
    crate::services::identity_policy::judge_call(state, &mut auth_ctx)
        .await
        .map_err(|e| a2a_auth_error(&e))?;

    // Card lookup.
    let card = load_agent_card_by_name(&state, tenant_id, card_name)
        .await
        .map_err(|e| {
            tracing::error!(error = %e, "a2a_v1: card lookup failed");
            A2aError::new(A2aErrorKind::InvalidAgentResponse, "Card lookup failed")
        })?
        .ok_or_else(|| {
            A2aError::new(A2aErrorKind::TaskNotFound, "Agent card not found")
                .with_metadata("cardName", card_name)
        })?;

    // Group access — same rules as the v0.3 inbound route.
    let target_group =
        crate::services::usage::resolve_target_group(&state.db, &auth_ctx, tenant_id).await;
    // The checks below run only for a loaded group, so a failed load
    // refuses rather than skipping them.
    let group_data = match &target_group {
        Some(g) => match state
            .group_cache
            .try_get_effective_group(tenant_id, &g.id)
            .await
        {
            Ok(data) => data,
            Err(e) => {
                tracing::error!(error = %e, tenant_id = %tenant_id, group_id = %g.id, "A2A v1: caller group load failed; refusing (fail closed)");
                return Err(A2aError::new(
                    A2aErrorKind::PolicyBlocked,
                    crate::services::cache::ResourceGroupCache::UNAVAILABLE_MESSAGE,
                )
                .with_metadata("check", "group_unavailable"));
            }
        },
        None => None,
    };
    let resolved_group_id = target_group.as_ref().map(|g| g.id.clone());

    // Every refusal from here on is a governance verdict
    // and writes a row, so the writer seals it (`denied` / `blocked` /
    // `awaiting_human`). Before this, an A2A v1 refusal left no trace in
    // `proxy_logs` at all.
    //
    // The failures above deliberately write nothing: a blocked tenant, the
    // A2A feature gate, a failed credential and an unknown card are decided
    // before there is an authenticated caller *and* a target card, so there
    // is no action to attribute — a row would carry an unauthenticated
    // (attacker-chosen) tenant/card pair, and a sealed record would let an
    // anonymous caller append to a tenant's permanent evidence log. They are
    // counted by the auth/HTTP metrics instead. A disabled or revoked card
    // is different: the caller is authenticated and the card is the
    // tenant's, so that refusal is a verdict and is written below.
    let asserted_run_root = asserted_root_from_headers(state, tenant_id, headers);
    // The run this call is a step of, decided before any gate (V1Target).
    let (run_root, new_task_id) = resolve_v1_run(
        state,
        tenant_id,
        resolved_group_id.as_deref(),
        asserted_run_root,
        &target,
    )
    .await?;
    // The step: the task the call works on, when it is known now.
    let step_task_id = match &target {
        V1Target::Message(m) => m.task_id.clone().or_else(|| new_task_id.clone()),
        V1Target::Task(id) => Some(id.to_string()),
        V1Target::NoTask => None,
    };
    let refuse = |err: A2aError, stamp: &dyn Fn(&mut crate::models::db::ProxyLogInsert)| {
        let parts = RowParts {
            auth: &auth_ctx,
            card_id: &card.id,
            card_name: &card.name,
            correlation_id: &correlation_id,
            resolved_group_id: resolved_group_id.as_deref(),
            asserted_run_root,
            run_root: run_root.as_deref(),
            start,
        };
        write_v1_row(
            state,
            &parts,
            err.http_status(),
            Some(err.message()),
            None,
            LogBodies::default(),
            "v1",
            |log| stamp(log),
        );
        err
    };
    let access_denied = |log: &mut crate::models::db::ProxyLogInsert| {
        log.proxy_subtype = Some(crate::constants::proxy_subtype::ACCESS_DENIED.to_string());
    };

    // An operator switched the card off (or revoked its trust): the gate
    // refused an authenticated caller — `denied`, control `ai.brutor.access`.
    if !card.enabled {
        return Err(refuse(
            A2aError::new(A2aErrorKind::UnsupportedOperation, "Agent card is disabled")
                .with_metadata("cardId", &card.id),
            &access_denied,
        ));
    }
    if card.revoked_at.is_some() || card.trust_tier == "revoked" {
        return Err(refuse(
            A2aError::new(A2aErrorKind::UnsupportedOperation, "Agent card is revoked")
                .with_metadata("cardId", &card.id),
            &access_denied,
        ));
    }

    if let Some(ref gd) = group_data {
        if !gd.effective_agent_card_ids.is_empty()
            && !gd.effective_agent_card_ids.contains(&card.id)
        {
            return Err(refuse(
                A2aError::new(
                    A2aErrorKind::UnsupportedOperation,
                    "Agent card is not bound to the caller's resource group",
                )
                .with_metadata("cardId", &card.id),
                &access_denied,
            ));
        }
    }

    // This card may be a Brutor-hosted shared service that
    // enforces its accepted callers. The caller is identified by its
    // credential's group, walked to its nearest AI System (the outbound
    // gate's rule). Applies to every v1 verb: an unaccepted caller may not
    // send, read, list or cancel the service's tasks.
    {
        let caller_group = resolved_group_id
            .as_deref()
            .or(auth_ctx.resolved_group_id.as_deref());
        match crate::services::accepted_callers::check(state, tenant_id, caller_group, &card.id)
            .await
        {
            crate::services::accepted_callers::Verdict::Refused(r) => {
                tracing::warn!(
                    tenant_id = %tenant_id,
                    card_id = %card.id,
                    service = %r.service_system_id,
                    caller = ?r.caller_system_id,
                    "A2A v1 refused: caller is not an accepted client of the service",
                );
                let subtype = r.subtype();
                let mut err = A2aError::new(A2aErrorKind::PolicyBlocked, r.message.clone())
                    .with_metadata("cardId", &card.id)
                    .with_metadata("reason", subtype);
                if let Some(c) = r.caller_system_id.as_deref() {
                    err = err.with_metadata("callerSystemId", c);
                }
                return Err(refuse(err, &|log| {
                    log.proxy_subtype = Some(subtype.to_string());
                }));
            }
            crate::services::accepted_callers::Verdict::Unavailable(_) => {
                return Err(refuse(
                    A2aError::new(
                        A2aErrorKind::PolicyBlocked,
                        crate::services::accepted_callers::UNAVAILABLE_MESSAGE,
                    )
                    .with_metadata("cardId", &card.id),
                    &|log| {
                        log.proxy_subtype =
                            Some(crate::constants::proxy_subtype::ENGINE_FAILURE.to_string());
                    },
                ));
            }
            crate::services::accepted_callers::Verdict::Accepted
            | crate::services::accepted_callers::Verdict::NotApplicable => {}
        }
    }

    // Run-scoped controls — the run budget and an operator's
    // abort — for EVERY caller, before either authorization plane, so a
    // stopped run cannot even queue an approval. They used to sit in the
    // non-agent branch beside the autonomy gate, so an agent-keyed A2A v1
    // call skipped both. The run is the one this call's rows carry — the
    // asserted run, else the task the call lands on, minted above for a
    // message that starts one. Checked for the verbs that take a step
    // (`V1Target::is_step`); every verb's hold names the run.
    let run_scope = run_root.as_ref().map(|root| RunScope {
        parent_task_id: step_task_id.clone(),
        ..RunScope::for_run(root.clone())
    });
    //
    // A message is one action of its run, whatever its handler does (a
    // skill it runs is part of it). Held from here; see `RequestCtx::step`.
    let mut step = None;
    if let Some(run) = run_scope.as_ref().filter(|_| target.is_step()) {
        // This surface reads no delegation chain: a message's run is the
        // one its caller asserts, else the task it lands on. Either is a run
        // its credential starts if it is new.
        let starter = crate::services::run_controls::run_starter(&auth_ctx, false);
        match crate::services::run_controls::check(
            state,
            crate::services::run_controls::Step {
                tenant_id,
                group_id: resolved_group_id.as_deref(),
                run_id: &run.run_id,
                depth: run.depth,
                kind: crate::services::run_controls::StepKind::Action,
                starter: starter.as_deref(),
            },
        )
        .await
        {
            Ok(admitted) => step = Some(admitted),
            Err(refusal) => {
                let subtype = refusal.subtype();
                // One run too many for the credential is a rate refusal:
                // the 429 with `Retry-After` this surface answers a rate
                // limit with.
                let err = match refusal.rate_block() {
                    Some(block) => rate_limited(block),
                    None => A2aError::new(A2aErrorKind::PolicyBlocked, refusal.message())
                        .with_metadata("reason", refusal.reason()),
                };
                return Err(refuse(err, &|log| {
                    log.proxy_subtype = Some(subtype.to_string())
                }));
            }
        }
    }

    // Run data scope on the message. The caller's AI System
    // judges its run's messages to this agent (rule `a2a:<card name>`) by
    // the message's data parts, as it judges tool calls; a refusal is a
    // record-level access refusal. Only for a message, which does work: a
    // read of a task names no record.
    if let (V1Target::Message(msg), Some(run), Some(tg)) =
        (&target, run_scope.as_ref(), target_group.as_ref())
    {
        use crate::services::data_scope as ds;
        let conflicts = ds::capture(
            &state.db,
            &tg.tenant_id,
            &run.run_id,
            headers.get(ds::SCOPE_HEADER).and_then(|v| v.to_str().ok()),
        )
        .await;
        let tool = format!("{}{}", ds::surface::A2A, card.name);
        let arguments: Vec<&serde_json::Value> = msg
            .parts
            .iter()
            .filter_map(|p| match &p.content {
                crate::services::a2a_v1::PartContent::Data(v) => Some(v),
                _ => None,
            })
            .collect();
        if let Some(r) = ds::gate(
            &state.db,
            ds::Call {
                tenant_id: &tg.tenant_id,
                ai_system_id: &tg.id,
                tool: &tool,
                arguments: &arguments,
                root_task_id: &run.run_id,
                conflicts: &conflicts,
                auth_ctx: &auth_ctx,
                correlation_id: &correlation_id,
            },
        )
        .await
        {
            let subtype = r.subtype();
            let verdict = r.detail["verdict"].as_str().unwrap_or("").to_string();
            return Err(refuse(
                A2aError::new(A2aErrorKind::PolicyBlocked, r.message)
                    .with_metadata("cardId", &card.id)
                    .with_metadata("reason", "data_scope")
                    .with_metadata("dataScopeVerdict", verdict),
                &|log| {
                    log.proxy_subtype = Some(subtype.to_string());
                    crate::services::proxy_logger::apply_policy_deny(log);
                },
            ));
        }
    }

    // The caller's own plane (`a2a_governance::authorize_actor`) on the
    // INBOUND call: an agent's `a2a_call` grants for this card alongside the
    // RG card-binding above, or, for any other caller, its AI System's
    // autonomy. The inbound surface is spec-constrained (responses
    // must be A2A-shaped), so Deny and ApprovalRequired both surface as
    // `PolicyBlocked` (canonical PERMISSION_DENIED / 403); for approval the
    // request is enqueued and the poll metadata is attached so an agent caller
    // can obtain approval and retry with `X-Approval-Token`. Fails closed.
    // Whether a person already approved this call to this card: the agent's
    // grant asked for the approval and its token was redeemed below. The
    // card's and the group's A2A policy ask the same question, so they do
    // not ask it twice (`a2a_governance::Call::approved`).
    // The capability a message is dispatched to, decided once: what the
    // policy judges and the rate admits is what runs.
    let capability = match &target {
        V1Target::Message(msg) => match message_capability(state, tenant_id, &card, msg).await {
            Ok(capability) => capability,
            Err(e) => {
                tracing::error!(error = %e, card_id = %card.id, "a2a_v1: task lookup for the message's capability failed; refusing (fail closed)");
                return Err(refuse(
                    A2aError::new(A2aErrorKind::PolicyBlocked, "Task lookup failed")
                        .with_metadata("cardId", &card.id),
                    &|log| {
                        log.proxy_subtype =
                            Some(crate::constants::proxy_subtype::ENGINE_FAILURE.to_string());
                    },
                ));
            }
        },
        V1Target::Task(_) | V1Target::NoTask => None,
    };

    // A message a skill runs passes the skill's own gates after these. The
    // request's approvals are one object for all of them, and the skill's
    // gates are named to this route's, so a message both hold is held once,
    // for both (`agent_approval::RequestApprovals`).
    let approvals = std::sync::Arc::new(agent_approval::RequestApprovals::from_headers(headers));
    // A message always has a run (the one asserted, else its task's).
    let skill_gates = match (&target, capability.as_ref(), run_scope.as_ref()) {
        (V1Target::Message(message), Some(capability), Some(run)) => Some(GuardedSkillGates {
            state,
            auth: &auth_ctx,
            tenant_id,
            capability,
            message,
            correlation_id: &correlation_id,
            resolved_group_id: resolved_group_id.as_deref(),
            approvals: &approvals,
            run,
        }),
        _ => None,
    };
    let downstream = skill_gates
        .as_ref()
        .map(|gates| gates as &dyn agent_approval::Downstream);
    let approved = match a2a_governance::authorize_actor(
        state,
        a2a_governance::ActorCall {
            tenant_id,
            auth_ctx: &auth_ctx,
            card_id: &card.id,
            // A caller that is not an agent is judged by its group's AI
            // System: a suspended or read-only system must refuse inbound
            // work too — accepting a task it is suspended from performing
            // would be the same failure one step later.
            group_id: resolved_group_id
                .as_deref()
                .or(auth_ctx.api_key_group_id.as_deref()),
            run: run_scope.clone(),
            approvals: &approvals,
            downstream,
        },
    )
    .await
    {
        a2a_governance::ActorOutcome::Proceed { approved, shadow } => {
            // dry_run: record the would-block (shadow) and proceed.
            if let Some(o) = shadow {
                let duration_ms = start.elapsed().as_secs_f64() * 1000.0;
                let mut log = proxy_logger::build_proxy_log(
                    "a2a",
                    &auth_ctx,
                    "POST",
                    "/a2a/message:send",
                    None,
                    Some(card.id.as_str()),
                    200,
                    duration_ms,
                    true,
                    Some(&o.reason),
                    &correlation_id,
                );
                proxy_logger::apply_agent_authz_decision(&mut log, &o, false);
                log.proxy_subtype =
                    Some(crate::constants::proxy_subtype::AGENT_AUTHZ_SHADOW.to_string());
                state
                    .background_writer
                    .write_proxy_log(state.db.clone(), log);
            }
            approved
        }
        a2a_governance::ActorOutcome::Denied(o) => {
            let mut err = A2aError::new(
                A2aErrorKind::PolicyBlocked,
                format!("agent_not_authorized: {}", o.reason),
            )
            .with_metadata("cardId", &card.id);
            if let Some(agent_id) = auth_ctx.actor_agent_id.as_deref() {
                err = err.with_metadata("agentId", agent_id);
            }
            return Err(refuse(err, &|log| {
                proxy_logger::apply_agent_authz_decision(log, &o, true)
            }));
        }
        a2a_governance::ActorOutcome::Held {
            envelope, decision, ..
        } => {
            // The `awaiting_human` row; the enqueue recorded its request id
            // in the request scope, so the row names it.
            let err = held_for_approval(&card.id, new_task_id.as_deref(), Some(&envelope));
            return Err(refuse(err, &|log| {
                proxy_logger::apply_agent_authz_decision(log, &decision, false);
                log.proxy_subtype =
                    Some(crate::constants::proxy_subtype::APPROVAL_REQUIRED.to_string());
            }));
        }
        a2a_governance::ActorOutcome::HoldFailed { decision, .. } => {
            let err = held_for_approval(&card.id, new_task_id.as_deref(), None);
            return Err(refuse(err, &|log| {
                proxy_logger::apply_agent_authz_decision(log, &decision, false);
                log.proxy_subtype =
                    Some(crate::constants::proxy_subtype::APPROVAL_REQUIRED.to_string());
            }));
        }
        a2a_governance::ActorOutcome::TokenRefused(answer) => {
            return Err(refuse(
                A2aError::new(A2aErrorKind::PolicyBlocked, answer),
                &|log| {
                    log.proxy_subtype =
                        Some(crate::constants::proxy_subtype::APPROVAL_TOKEN_INVALID.to_string());
                },
            ));
        }
        a2a_governance::ActorOutcome::Unavailable => {
            // Fail-closed because the engine could not decide: sealed as
            // `engine_failure`, not as a policy denial it never made.
            return Err(refuse(
                A2aError::new(A2aErrorKind::PolicyBlocked, "agent_authz_unavailable")
                    .with_metadata("cardId", &card.id),
                &|log| {
                    log.proxy_subtype =
                        Some(crate::constants::proxy_subtype::ENGINE_FAILURE.to_string());
                },
            ));
        }
        a2a_governance::ActorOutcome::RefusedLater(refusal) => {
            // The skill the message is dispatched to would refuse it:
            // refused here with that answer, nothing held, no token spent.
            let verdict = DispatchRefusal::of_later_gate(&refusal);
            return Err(refuse(
                A2aError::new(A2aErrorKind::PolicyBlocked, refusal.message)
                    .with_metadata("cardId", &card.id)
                    .with_metadata("reason", "skill"),
                &|log| verdict.stamp(log),
            ));
        }
        a2a_governance::ActorOutcome::AutonomyDenied { reason, resolution } => {
            tracing::warn!(
                tenant_id = %tenant_id,
                card = %card.id,
                level = %resolution.level.as_str(),
                provenance = %resolution.provenance.as_str(),
                "autonomy block on a non-agent inbound caller — refusing",
            );
            return Err(refuse(
                A2aError::new(
                    A2aErrorKind::PolicyBlocked,
                    format!("autonomy_denied: {reason}"),
                )
                .with_metadata("cardId", &card.id),
                &|log| proxy_logger::apply_autonomy(log, &resolution, true),
            ));
        }
    };

    // The A2A policy of the caller's group chain (the tenant's roots for a
    // caller with no group) and of the card: the one evaluation every A2A
    // route uses (`services::a2a_governance`), here for every v1.0 verb.
    // `disabled` refuses them all; a denied classification and
    // `approval_required` concern the verbs that do work (a message).
    let policy = match a2a_governance::policy_for(
        &state.db,
        tenant_id,
        group_data.as_deref(),
        card.tenant_governance.as_ref(),
    )
    .await
    {
        Ok(policy) => policy,
        Err(e) => {
            tracing::error!(error = %e, tenant_id = %tenant_id, "a2a_v1: policy load failed; refusing (fail closed)");
            return Err(refuse(
                A2aError::new(
                    A2aErrorKind::PolicyBlocked,
                    a2a_governance::UNAVAILABLE_MESSAGE,
                )
                .with_metadata("cardId", &card.id),
                &|log| {
                    log.proxy_subtype =
                        Some(crate::constants::proxy_subtype::ENGINE_FAILURE.to_string());
                },
            ));
        }
    };
    // A message is judged by the capability it is dispatched to; one that
    // no capability is bound for is judged by every enabled capability of
    // the card, since the gateway cannot tell which it is for.
    let classifications: Vec<&Value> = match (&target, capability.as_ref()) {
        (V1Target::Message(_), Some(bound)) => card
            .capabilities
            .iter()
            .filter(|c| c.capability_name == bound.capability_name)
            .filter_map(|c| c.data_classification.as_ref())
            .collect(),
        (V1Target::Message(_), None) => card
            .capabilities
            .iter()
            .filter(|c| c.enabled)
            .filter_map(|c| c.data_classification.as_ref())
            .collect(),
        _ => Vec::new(),
    };
    match a2a_governance::enforce(
        state,
        &policy,
        a2a_governance::Call {
            tenant_id,
            auth_ctx: &auth_ctx,
            card_id: &card.id,
            subject: a2a_governance::Subject {
                direction: a2a_governance::Direction::Inbound,
                classifications,
                work: target.is_step(),
            },
            run: run_scope.clone(),
            approvals: &approvals,
            downstream,
            approved,
        },
    )
    .await
    {
        a2a_governance::Outcome::Proceed => {}
        a2a_governance::Outcome::Refused(reason) => {
            return Err(refuse(
                A2aError::new(A2aErrorKind::PolicyBlocked, reason)
                    .with_metadata("cardId", &card.id)
                    .with_metadata("reason", "a2a_policy"),
                &access_denied,
            ));
        }
        a2a_governance::Outcome::Held(_, envelope) => {
            return Err(refuse(
                held_for_approval(&card.id, new_task_id.as_deref(), Some(&envelope)),
                &|log| {
                    log.proxy_subtype =
                        Some(crate::constants::proxy_subtype::APPROVAL_REQUIRED.to_string());
                },
            ));
        }
        a2a_governance::Outcome::RefusedLater(refusal) => {
            // The skill the message is dispatched to would refuse it:
            // refused here with that answer, nothing held, no token spent.
            let verdict = DispatchRefusal::of_later_gate(&refusal);
            return Err(refuse(
                A2aError::new(A2aErrorKind::PolicyBlocked, refusal.message)
                    .with_metadata("cardId", &card.id)
                    .with_metadata("reason", "skill"),
                &|log| verdict.stamp(log),
            ));
        }
        a2a_governance::Outcome::TokenRefused(reason) => {
            return Err(refuse(
                A2aError::new(
                    A2aErrorKind::PolicyBlocked,
                    format!("Approval token invalid: {reason}"),
                ),
                &|log| {
                    log.proxy_subtype =
                        Some(crate::constants::proxy_subtype::APPROVAL_TOKEN_INVALID.to_string());
                },
            ));
        }
        a2a_governance::Outcome::HoldFailed(e) => {
            tracing::error!(error = %e, "a2a_v1: policy approval enqueue failed");
            return Err(refuse(
                held_for_approval(&card.id, new_task_id.as_deref(), None),
                &|log| {
                    log.proxy_subtype =
                        Some(crate::constants::proxy_subtype::APPROVAL_REQUIRED.to_string());
                },
            ));
        }
    }

    Ok(RequestCtx {
        tenant,
        auth: auth_ctx,
        card,
        correlation_id,
        resolved_group_id,
        asserted_run_root,
        run_root,
        new_task_id,
        step: std::sync::Mutex::new(step),
        capability,
        approvals,
        start,
    })
}

/// The answer to a v1.0 call that is held for a person: the surface's
/// policy error, naming the approval to poll (when the request for one was
/// made) and the task the approved retry lands on.
fn held_for_approval(
    card_id: &str,
    new_task_id: Option<&str>,
    envelope: Option<&Value>,
) -> A2aError {
    let mut err = A2aError::new(A2aErrorKind::PolicyBlocked, "approval_required")
        .with_metadata("cardId", card_id)
        .with_metadata("reason", "approval_required");
    // The hold names the run its new task will have. The caller retries on
    // that task (`message.taskId`) with the approval token, so the approved
    // work lands in the run the person approved — not in a fresh one.
    if let Some(t) = new_task_id {
        err = err.with_metadata("taskId", t);
    }
    if let Some(body) = envelope {
        if let Some(id) = body.get("approval_request_id").and_then(|v| v.as_str()) {
            err = err.with_metadata("approvalRequestId", id.to_string());
        }
        if let Some(p) = body.get("poll_url").and_then(|v| v.as_str()) {
            err = err.with_metadata("pollUrl", p.to_string());
        }
        // The call is held by more than one gate: every approval to poll,
        // comma-separated in the order of `pollUrls`, and how many tokens
        // the retry carries.
        if let Some((ids, urls, needed)) = held_approvals(body) {
            err = err
                .with_metadata("approvalRequestIds", ids)
                .with_metadata("pollUrls", urls)
                .with_metadata("approvalsNeeded", needed);
        }
    }
    err
}

/// Every approval of a held answer that names more than one
/// (`approvals`): their ids, their poll URLs, and how many tokens the retry
/// carries.
fn held_approvals(envelope: &Value) -> Option<(String, String, String)> {
    let holds = envelope.get("approvals")?.as_array()?;
    let field = |name: &str| {
        holds
            .iter()
            .filter_map(|h| h.get(name).and_then(Value::as_str))
            .collect::<Vec<_>>()
            .join(",")
    };
    let needed = envelope
        .get("approvals_needed")
        .and_then(Value::as_u64)
        .unwrap_or(holds.len() as u64);
    Some((
        field("approval_request_id"),
        field("poll_url"),
        needed.to_string(),
    ))
}

/// The run a v1.0 step belongs to: the caller's asserted run, else the task
/// (the same rule `write_v1_row` stamps as `root_task_id`).
fn run_root_for(ctx: &RequestCtx, task_id: &str) -> String {
    ctx.asserted_run_root
        .map(|r| r.to_string())
        .unwrap_or_else(|| task_id.to_string())
}

/// The run scope of a v1.0 step on `task_id`: the run it belongs to (see
/// [`run_root_for`]) with the task as the step.
fn run_scope_for(ctx: &RequestCtx, task_id: &str) -> RunScope {
    RunScope {
        parent_task_id: Some(task_id.to_string()),
        ..RunScope::for_run(run_root_for(ctx, task_id))
    }
}

/// Resolve a client-asserted run root from the request headers, if any.
///
/// Mirrors `parse_or_mint`'s precedence: a signed delegation chain outranks an
/// assertion, so this returns `None` the moment any chain header is present —
/// a step that belongs to someone else's delegation must not re-root itself.
fn asserted_root_from_headers(
    state: &SharedState,
    tenant_id: &str,
    headers: &HeaderMap,
) -> Option<Ulid> {
    let get = |n: &str| {
        headers
            .get(n)
            .and_then(|v| v.to_str().ok())
            .map(|s| s.to_string())
    };
    let tenant_key =
        call_chain::derive_tenant_key(state.config.encryption_key.as_bytes(), tenant_id);
    call_chain::asserted_root(&get, &tenant_key)
}

async fn load_tenant_context(db: &PgPool, tenant_id: &str) -> Result<TenantContext, A2aError> {
    let row = sqlx::query_as::<_, (bool,)>("SELECT suspended FROM tenants WHERE id = $1 LIMIT 1")
        .bind(tenant_id)
        .fetch_optional(db)
        .await
        .map_err(|e| {
            tracing::error!(error = %e, "a2a_v1: tenant status lookup failed");
            A2aError::new(
                A2aErrorKind::InvalidAgentResponse,
                "Tenant status lookup failed",
            )
        })?;

    let (suspended,) = row.ok_or_else(|| {
        A2aError::new(A2aErrorKind::TaskNotFound, "Tenant not found")
            .with_metadata("tenantId", tenant_id)
    })?;

    Ok(TenantContext {
        tenant_id: tenant_id.to_string(),
        suspended,
    })
}

/// The tenant's `a2a` switch (`tenant_features`), from the tenant status the
/// tenant layer cached. A tenant that does not exist has it off.
async fn check_a2a_enabled(state: &SharedState, tenant_id: &str) -> Result<bool, A2aError> {
    let features = crate::services::tenant_features::of(state, tenant_id)
        .await
        .map_err(|e| {
            tracing::error!(error = %e, "a2a_v1: feature gate lookup failed");
            A2aError::new(
                A2aErrorKind::InvalidAgentResponse,
                "Feature gate lookup failed",
            )
        })?;
    Ok(features.allows(crate::services::tenant_features::Feature::A2a))
}

/// Loads an agent card by name via the in-memory cache.
///
/// Replaces the previous direct `agent_cards` SELECT — the cache
/// itself handles the pre/post-a012 schema-fallback for the
/// `extended_card_json` column, so the legacy helper is no longer
/// needed in this module.
async fn load_agent_card_by_name(
    state: &SharedState,
    tenant_id: &str,
    card_name: &str,
) -> Result<Option<AgentCardRow>, sqlx::Error> {
    Ok(state
        .agent_card_cache
        .get_by_name(tenant_id, card_name)
        .await?
        .map(|cached| AgentCardRow {
            id: cached.id.clone(),
            tenant_id: cached.tenant_id.clone(),
            name: cached.name.clone(),
            card_json: cached.card_json.clone(),
            trust_tier: cached.trust_tier.clone(),
            enabled: cached.enabled,
            revoked_at: cached.revoked_at,
            extended_card_json: cached.extended_card_json.clone(),
            tenant_limits: cached.tenant_limits.clone(),
            tenant_governance: cached.tenant_governance.clone(),
            capabilities: cached.capabilities.values().cloned().collect(),
        }))
}

// ---------------------------------------------------------------------------
// send_message — POST /a2a/{tenant}/{card}/message:send
// ---------------------------------------------------------------------------

/// Semantic Governance Policy on the A2A inbound surface.
///
/// Extracted from `send_message` because `message:stream` never ran it. Both
/// verbs reach the same handlers with the same content, so a caller who
/// wanted to skip an enforcing policy only had to choose the streaming verb.
/// A policy that one verb enforces and the other ignores is not a policy.
///
/// `endpoint` names the surface on the audit row so a block is attributable
/// to the verb that made the call.
async fn enforce_a2a_semantic_policy(
    state: &SharedState,
    ctx: &RequestCtx,
    headers: &HeaderMap,
    tenant_id: &str,
    message: &crate::services::a2a_v1::Message,
    endpoint: &str,
) -> Result<(), Response> {
    use crate::services::semantic_policy::{self, PolicyContext, PolicyDecision};
    let content = a2a_guardrail::message_text(message);
    if content.is_empty() {
        return Ok(());
    }
    let sgp_ctx = PolicyContext {
        surface: "a2a_inbound".to_string(),
        actor_agent_id: ctx.auth.actor_agent_id.clone(),
        action_type: Some("a2a_call".to_string()),
        target: Some(ctx.card.id.clone()),
        subject: ctx.auth.gateway_user_id.clone(),
    };
    // The judge's call is a step of this message's run.
    // A v1.0 step is the task the message lands on, in the run
    // `run_root_for` names: the caller's asserted run, else the task. A task
    // id that is not a ULID (a caller may name any task it can see) has
    // nothing to fork from; the judge's call then carries the credential
    // alone, as before.
    let child = message
        .task_id
        .as_deref()
        .or(ctx.new_task_id.as_deref())
        .and_then(|task| Ulid::from_string(task).ok())
        .and_then(|step| {
            call_chain::CallChain::root(ctx.asserted_run_root.unwrap_or(step))
                .fork_child(step)
                .ok()
        });
    let auth_headers = crate::routes::llm_helpers::judge_self_call_headers_for_child(
        headers,
        child.as_ref(),
        &call_chain::derive_tenant_key(state.config.encryption_key.as_bytes(), tenant_id),
    );
    let outcome = semantic_policy::load_and_evaluate(
        &state.semantic_policy_cache,
        state.semantic_policy_judge.as_ref(),
        &state.db,
        tenant_id,
        ctx.resolved_group_id.as_deref(),
        &content,
        &sgp_ctx,
        &auth_headers,
        state.config.judge_model.as_deref(),
    )
    .await;
    let dur = ctx.start.elapsed().as_secs_f64() * 1000.0;
    let write = |pid: &str, blocked: bool, reason: &str| {
        let log = proxy_logger::build_semantic_policy_log(
            &ctx.auth,
            "a2a",
            endpoint,
            Some(ctx.card.id.as_str()),
            ctx.resolved_group_id.clone(),
            &ctx.correlation_id,
            dur,
            pid,
            blocked,
            reason,
        );
        state
            .background_writer
            .write_proxy_log(state.db.clone(), log);
    };
    for (pid, reason) in &outcome.shadow_hits {
        write(pid, false, reason);
    }
    if outcome.decision == PolicyDecision::Block {
        let pid = outcome.matched_policy_id.clone().unwrap_or_default();
        let reason = outcome.reason.clone().unwrap_or_default();
        write(&pid, true, &reason);
        return Err(A2aError::new(
            A2aErrorKind::PolicyBlocked,
            format!("semantic_policy_blocked: {reason}"),
        )
        .with_metadata("surface", "a2a_inbound")
        .into_response());
    }
    Ok(())
}

pub(crate) async fn send_message(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    Path((tenant_id, card_name)): Path<(String, String)>,
    headers: HeaderMap,
    Json(mut req): Json<SendMessageRequest>,
) -> Response {
    let ctx = match resolve_request(
        &state,
        &headers,
        client_addr.ip(),
        &tenant_id,
        &card_name,
        V1Target::Message(&req.message),
    )
    .await
    {
        Ok(c) => c,
        Err(e) => return e.into_response(),
    };

    if let Err(e) = reject_wrong_version(&headers, "/message:send") {
        return e.into_response();
    }

    // A2A request-count rate limits (per resource group).
    let a2a_hold = match admit_v1_a2a_limits(&state, &tenant_id, &ctx).await {
        Ok(hold) => hold,
        Err(resp) => return resp,
    };
    // The message as the caller sent it: what an approval of the skill it
    // is dispatched to is bound to, whatever the guardrail below removes.
    let message_as_sent = req.message.clone();

    // NOTE (G8 residency): v1.0 inbound dispatches to a LOCAL bound
    // handler (skill / multi_turn_demo) — there is no external
    // provider/region the call routes to, so data-residency has nothing
    // to evaluate on this surface. Residency is enforced on the v0.3
    // OUTBOUND route, where the call leaves our tenant for a remote
    // agent at a resolvable target URL.

    // Either resume an existing task (multi-turn continuation) or
    // create a new one. resolve_or_create_task appends the inbound
    // message to history when the caller supplied a known task_id
    // — matches the spec's "send another message on the same task"
    // semantics.
    // Serialise the inbound request once so the same Value is
    // available both to the success-path log call below and to any
    // error-path log call. Lets the proxy_logs row carry the
    // request body the operator submitted, regardless of which
    // branch we exit on.
    let request_body_value = serde_json::to_value(&req).ok();

    let created = match resolve_or_create_task(&state, &tenant_id, &ctx, &req.message).await {
        Ok(t) => t,
        Err(err) => {
            return log_and_finalise_with_path(
                &state,
                &ctx,
                err.kind().http_status(),
                Some(err.message()),
                None,
                LogBodies {
                    request: request_body_value.as_ref(),
                    response: None,
                },
                "message:send",
            )
            .await
            .unwrap_or_else(|| err.into_response());
        }
    };

    // Guardrails on inbound A2A. The Agent Card's bound
    // user/group resolves the same `guardrail_user_id` /
    // `guardrail_group_id` selectors the LLM and MCP paths use, so a
    // policy operators configure once applies uniformly across all
    // four surfaces. Block translates into a v1.0 PolicyBlocked
    // structured error with `surface=a2a_inbound` metadata.
    match a2a_guardrail::check(
        &state,
        &ctx.auth,
        &tenant_id,
        &a2a_guardrail::message_text(&req.message),
        "a2a_inbound",
    )
    .await
    {
        Err(err) => {
            return log_guardrail_block(
                &state,
                &ctx,
                err,
                // The task exists (created above): the block names its step.
                Some(created.id.as_str()),
                LogBodies {
                    request: request_body_value.as_ref(),
                    response: None,
                },
                "message:send",
            );
        }
        Ok(a2a_guardrail::CheckResult::Redacted(new_text)) => {
            // Splice the redacted text back into the message
            // BEFORE the synchronous handler dispatch below runs
            // — otherwise the handler sees the original PII /
            // banned content and the redaction is just a log line.
            a2a_guardrail::apply_message_redactions(&mut req.message, &new_text);
        }
        Ok(a2a_guardrail::CheckResult::Pass) => {}
    }

    // Semantic policy, shared with `message:stream`.
    if let Err(resp) = enforce_a2a_semantic_policy(
        &state,
        &ctx,
        &headers,
        &tenant_id,
        &req.message,
        "/a2a/message:send",
    )
    .await
    {
        return resp;
    }

    // Synchronous handler dispatch.
    //
    // We just persisted (or appended to) the task. Now look up the
    // bound capability handler on the card and run it. The outcome
    // (artifact + new state) is applied to the task before we
    // return the envelope, so callers see the final state on the
    // first response — no need to poll.
    //
    // If the card has no bound handler the task stays `submitted`,
    // matching the pre-dispatch behaviour. Multi-turn handlers can
    // return `input-required` to pause for more input; the next
    // call carrying the same task_id resumes via the
    // resolve_or_create_task path above.
    let dispatch_history = task_store::get(&state.db, &tenant_id, &created.id)
        .await
        .map(|t| t.history)
        .unwrap_or_else(|_| created.history.clone());
    // A handler that failed must say so.
    //
    // `if let Ok(Some(..))` discarded the Err arm, so a dispatch failure
    // returned 200 with the task stuck in `submitted` forever, no reason
    // recorded anywhere, and — on the streaming verb — a client waiting out
    // the 300-second cap for an answer that was never coming. Marking the task
    // failed is what turns a hang into an error the caller can act on.
    // Every gate has passed; for a two-phase system the
    // `planned` record is sealed now, before the handler runs.
    ctx.step_dispatched().await;
    a2a_hold.dispatched().await;
    crate::services::evidence::planned::dispatching(
        &state.db,
        &tenant_id,
        ctx.resolved_group_id.as_deref(),
        constants::proxy::A2A,
        "a2a:message",
        Some(run_root_for(&ctx, &created.id)),
    );
    // What a gate said about the handler, when one refused or held it.
    let mut dispatch_refusal: Option<DispatchRefusal> = None;
    match dispatch_bound_handler(
        &state,
        &ctx.auth,
        &ctx.card.id,
        ctx.capability.as_ref(),
        &ctx.correlation_id,
        ctx.resolved_group_id.as_deref(),
        Some(&req.message),
        Some(&message_as_sent),
        &dispatch_history,
        &run_scope_for(&ctx, &created.id),
        &ctx.approvals,
    )
    .await
    {
        Ok(Some(mut outcome)) => {
            dispatch_refusal = outcome.refusal.take();
            apply_dispatch_outcome(&state.db, &tenant_id, &created.id, outcome).await;
        }
        Ok(None) => {} // no bound handler — the task stays as submitted by design
        Err(e) => {
            tracing::error!(error = %e, task_id = %created.id, "A2A dispatch failed");
            let _ = task_store::update_status(
                &state.db,
                &tenant_id,
                &created.id,
                TaskState::TaskStateFailed,
                None,
            )
            .await;
            if e.kind().engine_failure() {
                return log_guardrail_block(
                    &state,
                    &ctx,
                    e,
                    Some(created.id.as_str()),
                    LogBodies {
                        request: request_body_value.as_ref(),
                        response: None,
                    },
                    "message:send",
                );
            }
        }
    }

    // Re-read so the response carries the final state + artifact
    // rather than the pre-dispatch shape.
    let mut final_task = task_store::get(&state.db, &tenant_id, &created.id)
        .await
        .unwrap_or(created);

    // OUTPUT guardrails on the dispatch RESULT we return to the
    // (external) caller. Surface is `a2a_outbound` (the artifact /
    // status text LEAVES our tenant). On block → PolicyBlocked error
    // frame (the handler output never reaches the caller); on redact →
    // splice the rewritten text into the task's artifacts / status
    // message before returning.
    match a2a_guardrail::check_output(
        &state,
        &ctx.auth,
        &tenant_id,
        &a2a_guardrail::task_output_text(&final_task),
        "a2a_outbound",
    )
    .await
    {
        Err(err) => {
            return log_guardrail_block(
                &state,
                &ctx,
                err,
                Some(final_task.id.as_str()),
                LogBodies {
                    request: request_body_value.as_ref(),
                    response: None,
                },
                "message:send",
            );
        }
        Ok(a2a_guardrail::CheckResult::Redacted(new_text)) => {
            a2a_guardrail::apply_task_output_redactions(&mut final_task, &new_text);
        }
        Ok(a2a_guardrail::CheckResult::Pass) => {}
    }

    let payload = SendMessageResponse::Task(final_task.clone());
    let response_body_value = serde_json::to_value(&payload).ok();
    // The answer is the task either way. When a gate refused or held the
    // handler, the row records that verdict (a policy denial flagged as
    // one, a hold as a hold), not a successful step.
    write_v1_row(
        &state,
        &ctx.parts(),
        dispatch_refusal
            .as_ref()
            .map_or(StatusCode::OK, |r| r.status),
        dispatch_refusal.as_ref().map(|r| r.message.as_str()),
        Some(final_task.id.as_str()),
        LogBodies {
            request: request_body_value.as_ref(),
            response: response_body_value.as_ref(),
        },
        "message:send",
        |log| {
            if let Some(refusal) = dispatch_refusal.as_ref() {
                refusal.stamp(log);
            }
        },
    );

    Json(payload).into_response()
}

/// Resolve the target Task for an inbound `message:send` /
/// `message:stream` call.
///
///  * If the message carries a `task_id` that exists for this tenant
///    and is not yet terminal, the message is appended to that
///    task's `history` and the existing task is returned. This is
///    the multi-turn continuation leg — same `task_id` across
///    `input-required` → next user reply.
///  * If the message carries a `task_id` that's already terminal
///    (completed/failed/canceled/rejected), we return an error —
///    the spec forbids resuming a closed task.
///  * Otherwise (no task_id, or task_id not found) a fresh task is
///    created via `task_store::create`. The fresh-task path is the
///    pre-multi-turn behaviour and is unchanged for callers that
///    don't reference an existing task.
async fn resolve_or_create_task(
    state: &SharedState,
    tenant_id: &str,
    ctx: &RequestCtx,
    msg: &Message,
) -> Result<Task, A2aError> {
    if let Some(existing_id) = msg.task_id.as_deref() {
        // Appending to an existing task is a write to it, so
        // the same containment applies: a caller cannot graft messages onto
        // another group's task by naming its id.
        match task_store::get_scoped(
            &state.db,
            tenant_id,
            existing_id,
            ctx.resolved_group_id.as_deref(),
        )
        .await
        {
            Ok(existing) => {
                if existing.status.state.is_terminal() {
                    return Err(A2aError::new(
                        A2aErrorKind::TaskNotCancelable,
                        "Cannot append a message to a terminal task; start a new one",
                    )
                    .with_metadata("taskId", existing_id));
                }
                // Tag the message with the resolved task/context ids
                // so downstream dispatch sees a fully-stamped Message.
                let mut to_append = msg.clone();
                to_append.task_id = Some(existing_id.to_string());
                to_append.context_id = existing.context_id.clone().or(to_append.context_id);
                let updated =
                    task_store::append_history(&state.db, tenant_id, existing_id, to_append)
                        .await
                        .map_err(|e| {
                            tracing::error!(error = %e, "a2a_v1.continue: append_history failed");
                            A2aError::new(
                                A2aErrorKind::InvalidAgentResponse,
                                "Failed to append message to existing task",
                            )
                        })?;
                return Ok(updated);
            }
            Err(task_store::TaskStoreError::NotFound) => {
                // Caller supplied an id we don't know about — fall
                // through and create a fresh task with their id, the
                // pre-existing semantics. Lets external systems
                // pre-allocate ids and have us create matching rows.
            }
            Err(e) => {
                tracing::error!(error = %e, "a2a_v1.continue: task lookup failed");
                return Err(A2aError::new(
                    A2aErrorKind::InvalidAgentResponse,
                    "Task lookup failed",
                ));
            }
        }
    }

    let task = build_task_from_message(msg, ctx);
    task_store::create(
        &state.db,
        tenant_id,
        ctx.resolved_group_id.as_deref(),
        &task,
    )
    .await
    .map_err(|e| {
        tracing::error!(error = %e, "a2a_v1.create: task create failed");
        A2aError::new(A2aErrorKind::InvalidAgentResponse, "Failed to persist task")
    })
}

fn build_task_from_message(msg: &Message, ctx: &RequestCtx) -> Task {
    // The id minted before the gates (the run they checked and any hold
    // named) — so the task and its run are the same id.
    let task_id = msg
        .task_id
        .clone()
        .or_else(|| ctx.new_task_id.clone())
        .unwrap_or_else(|| Ulid::new().to_string());
    let context_id = msg
        .context_id
        .clone()
        .or_else(|| Some(Ulid::new().to_string()));
    let now = Utc::now();

    let mut initial_message = msg.clone();
    initial_message.task_id = Some(task_id.clone());
    initial_message.context_id = context_id.clone();

    Task {
        id: task_id,
        context_id,
        status: TaskStatus {
            state: TaskState::TaskStateSubmitted,
            message: None,
            timestamp: Some(now),
        },
        artifacts: Vec::new(),
        history: vec![initial_message],
        metadata: Some({
            let mut m = serde_json::Map::new();
            m.insert("brutorCardId".into(), Value::String(ctx.card.id.clone()));
            m.insert(
                "brutorCorrelationId".into(),
                Value::String(ctx.correlation_id.clone()),
            );
            m
        }),
    }
}

/// The capability of `card` a message with this hint is dispatched to:
/// the hinted one when it is enabled and bound to a handler, else the first
/// bound capability in name order. `None` when the card binds none (the
/// task then stays `submitted`). A hint that names no bound capability
/// falls back rather than failing the call, so a typo does not refuse it.
fn bound_capability(
    capabilities: &[crate::services::cache::AgentCardCapability],
    capability_hint: Option<&str>,
) -> Option<BoundCapability> {
    let bound = |c: &crate::services::cache::AgentCardCapability| {
        if !c.enabled {
            return None;
        }
        Some(BoundCapability {
            id: c.id.clone(),
            capability_name: c.capability_name.clone(),
            handler_type: c.handler_type.clone()?,
            handler_ref: c.handler_ref.clone()?,
            rate_limit_per_hour: c.rate_limit_per_hour,
        })
    };
    let hint = capability_hint.map(str::trim).filter(|s| !s.is_empty());
    hint.and_then(|name| {
        capabilities
            .iter()
            .find(|c| c.capability_name == name)
            .and_then(bound)
    })
    .or_else(|| capabilities.iter().find_map(bound))
}

/// The capability a message is dispatched to. Hint priority: the message's
/// own, then an earlier message of the task it continues — once a
/// capability is chosen for a task it sticks across the input-required →
/// reply turns even if later messages omit the hint. The task is read only
/// when it can change the answer (the card binds more than one capability
/// and the message carries no hint).
async fn message_capability(
    state: &SharedState,
    tenant_id: &str,
    card: &AgentCardRow,
    msg: &Message,
) -> Result<Option<BoundCapability>, task_store::TaskStoreError> {
    let mut hint = capability_hint_from_message(Some(msg));
    let bound = card
        .capabilities
        .iter()
        .filter(|c| c.enabled && c.handler_type.is_some() && c.handler_ref.is_some())
        .count();
    if hint.is_none() && bound > 1 {
        if let Some(task_id) = msg.task_id.as_deref() {
            match task_store::get(&state.db, tenant_id, task_id).await {
                Ok(task) => {
                    hint = task
                        .history
                        .iter()
                        .find_map(|m| capability_hint_from_message(Some(m)));
                }
                // A message may name the id of a task it creates.
                Err(task_store::TaskStoreError::NotFound) => {}
                Err(e) => return Err(e),
            }
        }
    }
    Ok(bound_capability(&card.capabilities, hint.as_deref()))
}

/// Read the optional capability hint a client may have stamped on
/// `message.metadata.capability` to disambiguate which bound
/// capability to invoke when the card declares more than one.
fn capability_hint_from_message(msg: Option<&Message>) -> Option<String> {
    msg.and_then(|m| m.metadata.as_ref())
        .and_then(|m| m.get("capability"))
        .and_then(|v| v.as_str())
        .map(str::to_string)
}

#[derive(Debug, Clone, PartialEq)]
struct BoundCapability {
    id: String,
    capability_name: String,
    handler_type: String,
    handler_ref: String,
    /// The capability's own hourly rate (`usage::a2a_capability_dims`).
    rate_limit_per_hour: Option<i32>,
}

/// The gates of the skill a message is dispatched to, asked about the input
/// the skill would be given: the message as the input guardrail leaves it.
/// The request's gates run before the guardrail, and a hold they raise for
/// the skill must be for what the approved retry will carry to the skill's
/// own gate, and must not put into the approval queue what the guardrail
/// removes. A message the guardrail blocks has no skill hold: the route
/// refuses it when it gets there.
struct GuardedSkillGates<'a> {
    state: &'a SharedState,
    auth: &'a AuthContext,
    tenant_id: &'a str,
    capability: &'a BoundCapability,
    message: &'a Message,
    correlation_id: &'a str,
    resolved_group_id: Option<&'a str>,
    approvals: &'a std::sync::Arc<agent_approval::RequestApprovals>,
    /// The run the message is a step of: the skill's run.
    run: &'a RunScope,
}

impl agent_approval::Downstream for GuardedSkillGates<'_> {
    fn holds(
        &self,
    ) -> futures::future::BoxFuture<
        '_,
        Result<Vec<agent_approval::DownstreamHold>, agent_approval::DownstreamRefusal>,
    > {
        Box::pin(async move {
            let mut message = self.message.clone();
            match a2a_guardrail::check(
                self.state,
                self.auth,
                self.tenant_id,
                &a2a_guardrail::message_text(&message),
                "a2a_inbound",
            )
            .await
            {
                Err(_) => return Ok(Vec::new()),
                Ok(a2a_guardrail::CheckResult::Redacted(redacted)) => {
                    a2a_guardrail::apply_message_redactions(&mut message, &redacted);
                }
                Ok(a2a_guardrail::CheckResult::Pass) => {}
            }
            let Some(request) = bound_skill_request(
                self.capability,
                Some(&message),
                Some(self.message),
                self.correlation_id,
                self.resolved_group_id,
                self.approvals,
            ) else {
                return Ok(Vec::new());
            };
            let gates = skill_orchestrator::SkillGates {
                state: self.state,
                auth: self.auth,
                request: &request,
                correlation_id: self.correlation_id,
                run: self.run,
            };
            agent_approval::Downstream::holds(&gates).await
        })
    }
}

/// The skill execution a message dispatched to `cap` is: built the same way
/// for the gates of the request (which name the skill's gates beside their
/// own) and for the dispatch. `None` when the capability runs no skill.
fn bound_skill_request(
    cap: &BoundCapability,
    user_message: Option<&Message>,
    // The message as the caller sent it, before the input guardrail.
    message_as_sent: Option<&Message>,
    correlation_id: &str,
    resolved_group_id: Option<&str>,
    approvals: &std::sync::Arc<agent_approval::RequestApprovals>,
) -> Option<SkillExecuteRequest> {
    if cap.handler_type != "skill" {
        return None;
    }
    // The skill orchestrator accepts handler_ref as either a skill_id
    // or a name. Disambiguate by shape: a prefixed id (`skill-` current,
    // `ask-` legacy) or a bare ULID is a skill_id; anything else is a name.
    let (skill_id, skill_name) = if cap.handler_ref.starts_with("skill-")
        || cap.handler_ref.starts_with("ask-")
        || Ulid::from_string(&cap.handler_ref).is_ok()
    {
        (Some(cap.handler_ref.clone()), None)
    } else {
        (None, Some(cap.handler_ref.clone()))
    };
    Some(SkillExecuteRequest {
        skill_id,
        skill_name,
        input_params: user_message.map(|m| serde_json::to_value(m).unwrap_or(Value::Null)),
        group_id: resolved_group_id.map(str::to_owned),
        correlation_id: Some(correlation_id.to_string()),
        parent_execution_id: None,
        // The request's tokens are its headers'.
        approval_token: None,
        approvals: approvals.clone(),
        data_scope_conflicts: Vec::new(),
        input_as_sent: message_as_sent.map(|m| serde_json::to_value(m).unwrap_or(Value::Null)),
        input_view: crate::models::proxy::SkillInputView::A2aMessage,
    })
}

/// Run the bound handler for the first matching capability and turn
/// the result into a (state, artifact, status_message) triple the
/// caller can use to update the persisted Task.
///
/// Returns `Ok(None)` when the card has no bound capability — caller
/// leaves the task in `submitted`. Returns an `Err` only on internal
/// dispatch failure (sqlx error etc.); upstream skill failures are
/// returned as `Ok(Some((TaskStateFailed, ...)))` so the caller can
/// still mark the task and continue.
async fn dispatch_bound_handler(
    state: &SharedState,
    auth_ctx: &AuthContext,
    card_id: &str,
    capability: Option<&BoundCapability>,
    correlation_id: &str,
    resolved_group_id: Option<&str>,
    user_message: Option<&Message>,
    message_as_sent: Option<&Message>,
    task_history: &[Message],
    run: &RunScope,
    approvals: &std::sync::Arc<agent_approval::RequestApprovals>,
) -> Result<Option<DispatchOutcome>, A2aError> {
    // The capability was decided when the request was resolved
    // (`message_capability`): the one the policy judged is the one that
    // runs.
    let Some(cap) = capability else {
        return Ok(None);
    };

    if cap.handler_type == "multi_turn_demo" {
        return Ok(Some(dispatch_multi_turn_demo(task_history)));
    }

    if cap.handler_type != "skill" {
        // Non-skill handlers (other than the multi_turn_demo above)
        // are not yet wired into the v1.0 path; leave the task in
        // submitted rather than failing the whole request — same
        // forward-compatibility stance the legacy a2a route takes
        // for unknown handler types.
        return Ok(None);
    }

    let Some(req) = bound_skill_request(
        cap,
        user_message,
        message_as_sent,
        correlation_id,
        resolved_group_id,
        approvals,
    ) else {
        return Ok(None);
    };

    // The message was admitted as the run's action when it was resolved.
    match skill_orchestrator::execute_skill(
        state,
        auth_ctx,
        &req,
        correlation_id,
        run,
        crate::services::run_controls::StepKind::PartOfAction,
    )
    .await
    {
        Ok(mut result) => {
            // Run the dispatched execution and settle it. This path
            // used to stop at `execute_skill` and answer "dispatched … status
            // =RUNNING": nothing ran the skill and the RUNNING row held one of
            // the group's concurrent-execution slots for good.
            let settlement = skill_orchestrator::run_and_settle(
                state,
                auth_ctx,
                &auth_ctx.tenant_id,
                &mut result,
                req.input_params.as_ref(),
            )
            .await;
            // (task state, artifact text — None when nothing may leave, status text)
            let (final_state, artifact_text, status_text) = match settlement {
                skill_orchestrator::Settlement::Ran { output, .. }
                    if result.execution_status == "COMPLETED" =>
                {
                    (
                        TaskState::TaskStateCompleted,
                        Some(output.unwrap_or_default()),
                        format!(
                            "Skill '{}' v{} completed. execution_id={}",
                            result.skill_name, result.skill_version, result.execution_id,
                        ),
                    )
                }
                skill_orchestrator::Settlement::Ran { .. } => (
                    TaskState::TaskStateFailed,
                    None,
                    format!(
                        "Skill '{}' v{} failed. execution_id={}",
                        result.skill_name, result.skill_version, result.execution_id,
                    ),
                ),
                skill_orchestrator::Settlement::OutputBlocked { message, .. } => {
                    (TaskState::TaskStateFailed, None, message)
                }
                // The skill ran but its output cannot be checked: refused, not
                // a failed run. Both callers fail the task and record the
                // refusal sealed `engine_failure`.
                skill_orchestrator::Settlement::GuardrailsUnavailable(e) => {
                    tracing::error!(error = %e, "guardrail configs unavailable for A2A skill output; refusing (fail closed)");
                    return Err(A2aError::new(
                        A2aErrorKind::GuardrailsUnavailable,
                        "guardrails_unavailable: refusing to return an unchecked skill result",
                    )
                    .with_metadata("surface", "a2a_outbound")
                    .with_metadata("check", "guardrails_unavailable"));
                }
            };
            let mut metadata = serde_json::Map::new();
            metadata.insert(
                "capability".into(),
                Value::String(cap.capability_name.clone()),
            );
            metadata.insert(
                "execution_id".into(),
                Value::String(result.execution_id.clone()),
            );
            metadata.insert("skill_id".into(), Value::String(result.skill_id.clone()));
            metadata.insert(
                "skill_name".into(),
                Value::String(result.skill_name.clone()),
            );
            metadata.insert(
                "skill_version".into(),
                Value::String(result.skill_version.clone()),
            );
            metadata.insert(
                "execution_status".into(),
                Value::String(result.execution_status.clone()),
            );
            let artifact = artifact_text.map(|text| Artifact {
                artifact_id: Ulid::new().to_string(),
                name: Some(format!("{}-execution", cap.capability_name)),
                description: Some(format!(
                    "Result of inbound A2A capability '{}' bound to skill '{}'",
                    cap.capability_name, result.skill_name,
                )),
                parts: vec![Part {
                    content: PartContent::Text(text),
                    metadata: None,
                    filename: None,
                    media_type: Some("text/plain".to_string()),
                }],
                metadata: Some(metadata),
                extensions: Vec::new(),
            });
            let status_message = Message {
                message_id: Ulid::new().to_string(),
                context_id: None,
                task_id: None,
                role: Role::RoleAgent,
                parts: vec![Part {
                    content: PartContent::Text(status_text),
                    metadata: None,
                    filename: None,
                    media_type: None,
                }],
                metadata: None,
                extensions: Vec::new(),
                reference_task_ids: Vec::new(),
            };
            Ok(Some(DispatchOutcome {
                state: final_state,
                artifact,
                status_message: Some(status_message),
                refusal: None,
            }))
        }
        Err(e) => {
            tracing::warn!(
                card_id = %card_id, capability = %cap.capability_name,
                error = ?e, "a2a_v1.dispatch: skill execution failed",
            );
            // A gate's verdict on the execution, for the step's audit row:
            // a hold, an argument-policy denial, any other refusal a gate
            // named. An execution that merely failed has none.
            let refusal =
                (e.refusal.is_some() || e.argument_policy_denied).then(|| DispatchRefusal {
                    status: StatusCode::from_u16(e.status_code).unwrap_or(StatusCode::FORBIDDEN),
                    message: e.message.clone(),
                    subtype: e.refusal,
                    argument_policy_denied: e.argument_policy_denied,
                });
            // Held for a person: the task waits for an authorisation
            // (`TASK_STATE_AUTH_REQUIRED`, the specification's state for an
            // agent that needs human approval before it acts), and its
            // status message says which approval to poll. The caller sends
            // the message again on this task with `X-Approval-Token` once
            // the approval is granted.
            let held = e
                .body
                .as_ref()
                .filter(|_| e.refusal == Some(constants::proxy_subtype::APPROVAL_REQUIRED));
            let (state, text, metadata) = match held {
                Some(envelope) => {
                    let mut metadata = serde_json::Map::new();
                    metadata.insert("reason".into(), Value::String("approval_required".into()));
                    for (from, to) in [
                        ("approval_request_id", "approvalRequestId"),
                        ("poll_url", "pollUrl"),
                        ("timeout_seconds", "timeoutSeconds"),
                    ] {
                        if let Some(v) = envelope.get(from) {
                            metadata.insert(to.into(), v.clone());
                        }
                    }
                    (
                        TaskState::TaskStateAuthRequired,
                        "This call is held for approval. Poll the approval named in this \
                         message's metadata; once it is approved, send the same message again \
                         on this task with the approval token in the X-Approval-Token header."
                            .to_string(),
                        Some(metadata),
                    )
                }
                None => (
                    TaskState::TaskStateFailed,
                    format!("Inbound capability dispatch failed: {}", e.message),
                    None,
                ),
            };
            let status_message = Message {
                message_id: Ulid::new().to_string(),
                context_id: None,
                task_id: None,
                role: Role::RoleAgent,
                parts: vec![Part {
                    content: PartContent::Text(text),
                    metadata: None,
                    filename: None,
                    media_type: None,
                }],
                metadata,
                extensions: Vec::new(),
                reference_task_ids: Vec::new(),
            };
            Ok(Some(DispatchOutcome {
                state,
                artifact: None,
                status_message: Some(status_message),
                refusal,
            }))
        }
    }
}

/// Hand-coded multi-turn handler used by the trial bundle's
/// `hello-world-a2a` card. Demonstrates the `input-required` →
/// continuation → `completed` cycle without depending on a
/// multi-turn-aware skill orchestrator.
///
/// Walks a fixed two-step dialog by counting user messages already in
/// `task_history`:
///
///   * 0 user messages so far (impossible — the call would have at
///     least one) → ask for the user's name
///   * 1 user message  → ask for the user's mood
///   * 2 user messages → return a final greeting incorporating both
///   * 3+ user messages → completed (idempotent re-call)
///
/// State transitions written to the task store: `input-required`
/// while waiting on the next user reply, `completed` once both
/// answers are in.
fn dispatch_multi_turn_demo(task_history: &[Message]) -> DispatchOutcome {
    let user_messages: Vec<&Message> = task_history
        .iter()
        .filter(|m| m.role == Role::RoleUser)
        .collect();

    fn first_text(m: &Message) -> Option<&str> {
        m.parts.iter().find_map(|p| match &p.content {
            PartContent::Text(s) => Some(s.as_str()),
            _ => None,
        })
    }

    fn agent_message(text: impl Into<String>) -> Message {
        Message {
            message_id: Ulid::new().to_string(),
            context_id: None,
            task_id: None,
            role: Role::RoleAgent,
            parts: vec![Part {
                content: PartContent::Text(text.into()),
                metadata: None,
                filename: None,
                media_type: None,
            }],
            metadata: None,
            extensions: Vec::new(),
            reference_task_ids: Vec::new(),
        }
    }

    match user_messages.len() {
        0 | 1 => DispatchOutcome {
            state: TaskState::TaskStateInputRequired,
            artifact: None,
            status_message: Some(agent_message(
                "Hi! What's your name? Send another message on this task to answer.",
            )),
            refusal: None,
        },
        2 => DispatchOutcome {
            state: TaskState::TaskStateInputRequired,
            artifact: None,
            status_message: Some(agent_message(
                "Thanks. How are you feeling today? Send another message on this task with one or two words.",
            )),
            refusal: None,
        },
        _ => {
            let name = user_messages
                .get(1)
                .and_then(|m| first_text(m))
                .unwrap_or("friend")
                .trim();
            let mood = user_messages
                .last()
                .and_then(|m| first_text(m))
                .unwrap_or("good")
                .trim();
            let greeting = format!(
                "Nice to meet you, {name}. Hope feeling {mood} carries through the rest of your day."
            );
            let mut metadata = serde_json::Map::new();
            metadata.insert("name".into(), Value::String(name.to_string()));
            metadata.insert("mood".into(), Value::String(mood.to_string()));
            metadata.insert(
                "turns".into(),
                Value::Number((user_messages.len() as u64).into()),
            );
            let artifact = Artifact {
                artifact_id: Ulid::new().to_string(),
                name: Some("multi-turn-greeting".to_string()),
                description: Some("Final greeting from the multi-turn demo handler".to_string()),
                parts: vec![Part {
                    content: PartContent::Text(greeting.clone()),
                    metadata: None,
                    filename: None,
                    media_type: Some("text/plain".to_string()),
                }],
                metadata: Some(metadata),
                extensions: Vec::new(),
            };
            DispatchOutcome {
                state: TaskState::TaskStateCompleted,
                artifact: Some(artifact),
                status_message: Some(agent_message(greeting)),
                refusal: None,
            }
        }
    }
}

#[derive(Debug)]
struct DispatchOutcome {
    state: TaskState,
    artifact: Option<Artifact>,
    status_message: Option<Message>,
    /// A gate refused or held the handler (see [`DispatchRefusal`]).
    refusal: Option<DispatchRefusal>,
}

/// A gate's verdict on a dispatched handler, as the step's audit row
/// records it. The wire answer is the task (failed, or waiting for an
/// authorisation); the row says what refused it, so a policy denial is on
/// the record as a policy denial and a hold as a hold.
#[derive(Debug, Clone)]
struct DispatchRefusal {
    status: StatusCode,
    message: String,
    /// The refusal subtype of the gate that said no, when it names one.
    subtype: Option<&'static str>,
    /// An argument policy on the skill's input denied the call.
    argument_policy_denied: bool,
}

impl DispatchRefusal {
    /// The verdict of a gate the request had not reached yet.
    fn of_later_gate(refusal: &agent_approval::DownstreamRefusal) -> Self {
        Self {
            status: refusal.status,
            message: refusal.message.clone(),
            subtype: refusal.subtype,
            argument_policy_denied: refusal.argument_policy_denied,
        }
    }

    fn stamp(&self, log: &mut crate::models::db::ProxyLogInsert) {
        if let Some(subtype) = self.subtype {
            log.proxy_subtype = Some(subtype.to_string());
        }
        if self.argument_policy_denied {
            proxy_logger::apply_policy_deny(log);
        }
    }
}

/// Apply a [`DispatchOutcome`] to a persisted task: append the
/// artifact (if any), then transition the status. Errors are
/// logged but not surfaced — the caller has already returned (or is
/// about to) and a dropped task-store write is preferable to a
/// late-stage 500.
async fn apply_dispatch_outcome(
    db: &PgPool,
    tenant_id: &str,
    task_id: &str,
    outcome: DispatchOutcome,
) {
    if let Some(artifact) = outcome.artifact {
        if let Err(e) = task_store::append_artifact(db, tenant_id, task_id, artifact).await {
            tracing::warn!(task_id = %task_id, error = %e,
                "a2a_v1.dispatch: append_artifact failed");
        }
    }
    if let Err(e) = task_store::update_status(
        db,
        tenant_id,
        task_id,
        outcome.state,
        outcome.status_message,
    )
    .await
    {
        tracing::warn!(task_id = %task_id, error = %e,
            "a2a_v1.dispatch: update_status failed");
    }
}

/// A2A rate limits for an inbound v1.0 message.
///
/// The caller's effective (ancestor-merged) `a2a_global_limits` from the
/// group cache, the card's own limits and the hourly rate of the capability
/// the message is dispatched to, admitted together by the shared
/// counter-backed gate (`usage::enforce_a2a_limits`). Inert when none of
/// them states a limit.
///
/// On a trip returns a ready-to-emit `429 Too Many Requests` carrying
/// the A2A spec error envelope + an RFC 6585 `Retry-After` header. We
/// build the response here (rather than returning an `A2aError`)
/// because `A2aError::into_response` derives its HTTP status purely
/// from the error *kind*, and the v1.0 error table has no 429 kind —
/// stamping the envelope while overriding the status is the only way to
/// surface a true 429 without inventing a new kind.
///
/// Admitted under a hold: `Ok` carries its guard when anything was
/// admitted. A message a later gate refuses (the guardrail, the semantic
/// policy) gives the allowance back when the guard is dropped; a dispatched
/// one keeps it ([`A2aRateHold::dispatched`]).
async fn admit_v1_a2a_limits(
    state: &SharedState,
    tenant_id: &str,
    ctx: &RequestCtx,
) -> Result<A2aRateHold, Response> {
    let gd = match ctx.resolved_group_id.as_deref() {
        // A failed load refuses: it used to skip the quota entirely.
        Some(rg_id) => match state
            .group_cache
            .try_get_effective_group(tenant_id, rg_id)
            .await
        {
            Ok(gd) => gd,
            Err(e) => {
                tracing::error!(error = %e, tenant_id = %tenant_id, group_id = %rg_id, "A2A v1 quota: group load failed; refusing (fail closed)");
                let err = A2aError::new(
                    A2aErrorKind::PolicyBlocked,
                    crate::services::cache::ResourceGroupCache::UNAVAILABLE_MESSAGE,
                )
                .with_metadata("check", "group_unavailable");
                return Err(err.into_response());
            }
        },
        None => None,
    };
    let mut hold = crate::services::limits::PendingHold::of(crate::services::limits::HoldId::new(
        tenant_id, "a2a-rate",
    ));
    let block = crate::services::usage::admit_a2a_limits(
        &state.usage_state,
        crate::services::usage::A2aLimits {
            group: match (gd.as_ref(), ctx.resolved_group_id.as_deref()) {
                (Some(gd), Some(rg_id)) => Some((rg_id, gd.effective_a2a_limits.as_ref())),
                _ => None,
            },
            card: Some((
                tenant_id,
                ctx.card.id.as_str(),
                ctx.card.tenant_limits.as_ref(),
            )),
            capability: ctx.capability.as_ref().map(|c| {
                (
                    c.id.as_str(),
                    c.capability_name.as_str(),
                    c.rate_limit_per_hour,
                )
            }),
        },
        Some(&mut hold),
    )
    .await;
    let hold = A2aRateHold(hold.held().map(|h| state.usage_state.adopt_hold(h)));
    let Some(block) = block else {
        return Ok(hold);
    };
    tracing::warn!(
        tenant_id = %tenant_id,
        card_id = %ctx.card.id,
        scope = %block.scope,
        limit_type = %block.limit_type,
        retry_after_secs = block.retry_after_secs,
        "A2A v1 inbound rate limit exceeded — 429 returned",
    );
    // A quota refusal is a `denied` verdict; same subtype
    // shape as the MCP rate-limit rows.
    let scope = block.scope.to_string();
    write_v1_row(
        state,
        &ctx.parts(),
        StatusCode::TOO_MANY_REQUESTS,
        Some(block.message.as_str()),
        None,
        LogBodies::default(),
        "v1",
        |log| log.proxy_subtype = Some(format!("rate_limit_{scope}")),
    );
    Err(rate_limited(&block).into_response())
}

/// What the rate limits admitted for one message, until the message is
/// dispatched or refused.
struct A2aRateHold(Option<crate::services::cache::usage_state::HoldGuard>);

impl A2aRateHold {
    /// The message's handler is dispatched: the allowance stays counted.
    async fn dispatched(self) {
        if let Some(hold) = self.0 {
            hold.commit().await;
        }
    }
}

/// The error a rate limit refuses a v1.0 call with: the spec envelope shape
/// (UnsupportedOperation reason), answered as a 429 with `Retry-After` (the
/// v1.0 error table has no 429 kind).
fn rate_limited(block: &crate::services::usage::McpRateLimitBlock) -> A2aError {
    A2aError::new(A2aErrorKind::UnsupportedOperation, block.message.clone())
        .with_metadata("retryAfter", block.retry_after_secs.to_string())
        .too_many_requests(block.retry_after_secs)
}

fn reject_wrong_version(headers: &HeaderMap, path: &str) -> Result<(), A2aError> {
    match version::detect_from_headers_and_path(headers, path) {
        version::ProtocolVersion::V1_0 => Ok(()),
        version::ProtocolVersion::Unknown => {
            // No signal either way — the path reached us via the v1.0
            // router, so assume v1.0. (Callers wanting strict
            // rejection can send `A2A-Version: 1.0` explicitly.)
            Ok(())
        }
        version::ProtocolVersion::V0_3 => Err(A2aError::new(
            A2aErrorKind::VersionNotSupported,
            "This endpoint serves A2A v1.0 only; v0.3 is not accepted here",
        )
        .with_metadata("requested", "0.3")),
    }
}

// ---------------------------------------------------------------------------
// send_streaming_message — POST /a2a/{tenant}/{card}/message:stream (SSE)
// ---------------------------------------------------------------------------

pub(crate) async fn send_streaming_message(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    Path((tenant_id, card_name)): Path<(String, String)>,
    headers: HeaderMap,
    Json(mut req): Json<SendMessageRequest>,
) -> Response {
    let ctx = match resolve_request(
        &state,
        &headers,
        client_addr.ip(),
        &tenant_id,
        &card_name,
        V1Target::Message(&req.message),
    )
    .await
    {
        Ok(c) => c,
        Err(e) => return e.into_response(),
    };
    if let Err(e) = reject_wrong_version(&headers, "/message:stream") {
        return e.into_response();
    }

    // A2A request-count rate limits (per resource group).
    let a2a_hold = match admit_v1_a2a_limits(&state, &tenant_id, &ctx).await {
        Ok(hold) => hold,
        Err(resp) => return resp,
    };
    // The message as the caller sent it: what an approval of the skill it
    // is dispatched to is bound to, whatever the guardrail below removes.
    let message_as_sent = req.message.clone();

    // Same inbound guardrail check as `send_message`.
    // Run BEFORE we create or append to a task so a blocked payload
    // never lands a task row in the DB. Redacted outcome splices the
    // rewritten text back into the message in place. The block itself is
    // audited: every verdict is sealed.
    match a2a_guardrail::check(
        &state,
        &ctx.auth,
        &tenant_id,
        &a2a_guardrail::message_text(&req.message),
        "a2a_inbound",
    )
    .await
    {
        Err(err) => {
            let request_body = serde_json::to_value(&req).ok();
            return log_guardrail_block(
                &state,
                &ctx,
                err,
                None,
                LogBodies {
                    request: request_body.as_ref(),
                    response: None,
                },
                "message:stream",
            );
        }
        Ok(a2a_guardrail::CheckResult::Redacted(new_text)) => {
            a2a_guardrail::apply_message_redactions(&mut req.message, &new_text);
        }
        Ok(a2a_guardrail::CheckResult::Pass) => {}
    }

    // Semantic policy, the same evaluation `message:send`
    // runs. This verb never ran it, so an enforcing policy could be sidestepped
    // by asking for a stream. Placed here, before the task row is created, so a
    // blocked payload leaves no task behind.
    if let Err(resp) = enforce_a2a_semantic_policy(
        &state,
        &ctx,
        &headers,
        &tenant_id,
        &req.message,
        "/a2a/message:stream",
    )
    .await
    {
        return resp;
    }

    // Resume or create — same logic as the sync send_message path.
    // Multi-turn callers send the same task_id back; the helper
    // appends their reply to the existing task's history before
    // dispatch.
    let created = match resolve_or_create_task(&state, &tenant_id, &ctx, &req.message).await {
        Ok(t) => t,
        Err(e) => return e.into_response(),
    };

    // Capture request body + the initial Task envelope (what we
    // emit on the first SSE frame) for the proxy_logs row. The
    // streamed status / artifact updates that follow aren't
    // captured — SSE bodies are unbounded and the initial Task
    // envelope already tells the operator everything they need to
    // diagnose dispatch.
    let stream_request_body = serde_json::to_value(&req).ok();
    let stream_response_body = serde_json::to_value(&created).ok();
    // The handler is dispatched below; this row is the
    // step's outcome record, so a two-phase `planned` record comes first.
    ctx.step_dispatched().await;
    a2a_hold.dispatched().await;
    crate::services::evidence::planned::dispatching(
        &state.db,
        &tenant_id,
        ctx.resolved_group_id.as_deref(),
        constants::proxy::A2A,
        "a2a:message",
        Some(run_root_for(&ctx, &created.id)),
    );
    let _ = log_and_finalise_with_path(
        &state,
        &ctx,
        StatusCode::OK,
        None,
        Some(created.id.as_str()),
        LogBodies {
            request: stream_request_body.as_ref(),
            response: stream_response_body.as_ref(),
        },
        "message:stream",
    )
    .await;

    // Spawn the bound handler dispatch as a background task so the
    // SSE stream can emit state-change frames as the work progresses.
    // `build_task_stream` is woken event-driven by the stream hub
    // (each dispatch write fires pg_notify), re-reads the task, and
    // emits a TaskStatusUpdateEvent whenever the state moves; once the
    // dispatch helper writes the final state (completed/failed), that
    // write wakes the stream and it emits the terminal frame.
    {
        let dispatch_state = state.clone();
        let dispatch_auth = ctx.auth.clone();
        let card_id = ctx.card.id.clone();
        let capability = ctx.capability.clone();
        let correlation_id = ctx.correlation_id.clone();
        let group = ctx.resolved_group_id.clone();
        let user_message = req.message.clone();
        let dispatch_tenant = tenant_id.clone();
        let dispatch_task_id = created.id.clone();
        let dispatch_history = created.history.clone();
        let dispatch_run = run_scope_for(&ctx, &created.id);
        let dispatch_record = StreamGuardCtx::new(&state, &ctx, &tenant_id, "message:stream");
        let dispatch_approvals = ctx.approvals.clone();
        tokio::spawn(async move {
            // See the sync path: a swallowed Err left the
            // streaming client waiting out the 300-second cap.
            match dispatch_bound_handler(
                &dispatch_state,
                &dispatch_auth,
                &card_id,
                capability.as_ref(),
                &correlation_id,
                group.as_deref(),
                Some(&user_message),
                Some(&message_as_sent),
                &dispatch_history,
                &dispatch_run,
                &dispatch_approvals,
            )
            .await
            {
                Err(e) => {
                    tracing::error!(error = %e, task_id = %dispatch_task_id, "A2A dispatch failed");
                    let _ = task_store::update_status(
                        &dispatch_state.db,
                        &dispatch_tenant,
                        &dispatch_task_id,
                        TaskState::TaskStateFailed,
                        None,
                    )
                    .await;
                    if e.kind().engine_failure() {
                        dispatch_record.record_block(&dispatch_task_id, &e);
                    }
                }
                Ok(None) => {}
                Ok(Some(mut outcome)) => {
                    // The stream's opening row was written before the
                    // handler ran; a gate's verdict on it gets its own row.
                    if let Some(refusal) = outcome.refusal.take() {
                        dispatch_record.record_refusal(&dispatch_task_id, &refusal);
                    }
                    apply_dispatch_outcome(
                        &dispatch_state.db,
                        &dispatch_tenant,
                        &dispatch_task_id,
                        outcome,
                    )
                    .await;
                }
            }
        });
    }

    let db = state.db.clone();
    let shutdown = state.shutdown_token.clone();
    let tenant_for_stream = tenant_id.clone();
    let initial_task = created.clone();

    let guard = Some(StreamGuardCtx::new(
        &state,
        &ctx,
        &tenant_id,
        "message:stream",
    ));
    let stream = build_task_stream(
        db,
        tenant_for_stream,
        initial_task,
        shutdown,
        state.a2a_stream_hub.clone(),
        guard,
    );
    Sse::new(stream)
        .keep_alive(KeepAlive::default())
        .into_response()
}

/// Guardrail context threaded into [`build_task_stream`] so the
/// SSE path can run OUTPUT guardrails on every artifact / status frame
/// it emits (surface `a2a_outbound` — the frame LEAVES our tenant to
/// the subscriber), matching the non-streaming `message:send` path.
/// Cloned into the spawned stream task.
#[derive(Clone)]
struct StreamGuardCtx {
    state: SharedState,
    auth: AuthContext,
    tenant_id: String,
    /// The row facts of the request that opened the stream, so a block the
    /// guard decides later is recorded on the call's run like any other.
    card_id: String,
    card_name: String,
    correlation_id: String,
    resolved_group_id: Option<String>,
    asserted_run_root: Option<Ulid>,
    run_root: Option<String>,
    start: Instant,
    verb: &'static str,
}

impl StreamGuardCtx {
    fn new(state: &SharedState, ctx: &RequestCtx, tenant_id: &str, verb: &'static str) -> Self {
        Self {
            state: state.clone(),
            auth: ctx.auth.clone(),
            tenant_id: tenant_id.to_string(),
            card_id: ctx.card.id.clone(),
            card_name: ctx.card.name.clone(),
            correlation_id: ctx.correlation_id.clone(),
            resolved_group_id: ctx.resolved_group_id.clone(),
            asserted_run_root: ctx.asserted_run_root,
            run_root: ctx.run_root.clone(),
            start: ctx.start,
            verb,
        }
    }

    fn parts(&self) -> RowParts<'_> {
        RowParts {
            auth: &self.auth,
            card_id: &self.card_id,
            card_name: &self.card_name,
            correlation_id: &self.correlation_id,
            resolved_group_id: self.resolved_group_id.as_deref(),
            asserted_run_root: self.asserted_run_root,
            run_root: self.run_root.as_deref(),
            start: self.start,
        }
    }

    /// A gate that refused or held the streamed handler writes its row, on
    /// the task's run and step: flagged as the policy denial or the hold it
    /// is.
    fn record_refusal(&self, task_id: &str, refusal: &DispatchRefusal) {
        write_v1_row(
            &self.state,
            &self.parts(),
            refusal.status,
            Some(&refusal.message),
            Some(task_id),
            LogBodies::default(),
            self.verb,
            |log| refusal.stamp(log),
        );
    }

    /// A streamed output block writes its row, on the task's run and step:
    /// guardrail-blocked, or `engine_failure` when the guardrails could not
    /// be read. The stream's opening row read as a success and nothing
    /// recorded that the guard later stopped it.
    fn record_block(&self, task_id: &str, err: &A2aError) {
        write_v1_row(
            &self.state,
            &self.parts(),
            err.kind().http_status(),
            Some(err.message()),
            Some(task_id),
            LogBodies::default(),
            self.verb,
            |log| stamp_guardrail_refusal(log, err.kind()),
        );
    }
}

/// Run an OUTPUT guardrail over a single streamed text chunk.
/// `Ok(Some(text))` = pass-through (optionally redacted — caller
/// splices); `Ok(None)` = nothing to check / pass unchanged;
/// `Err(refusal)` = BLOCK (caller records it and terminates the stream).
async fn stream_guard_check(gctx: &StreamGuardCtx, text: &str) -> Result<Option<String>, A2aError> {
    if text.is_empty() {
        return Ok(None);
    }
    match a2a_guardrail::check_output(
        &gctx.state,
        &gctx.auth,
        &gctx.tenant_id,
        text,
        "a2a_outbound",
    )
    .await
    {
        Ok(a2a_guardrail::CheckResult::Pass) => Ok(None),
        Ok(a2a_guardrail::CheckResult::Redacted(t)) => Ok(Some(t)),
        Err(e) => Err(e),
    }
}

fn build_task_stream(
    db: PgPool,
    tenant_id: String,
    initial_task: Task,
    shutdown: tokio_util::sync::CancellationToken,
    hub: std::sync::Arc<crate::services::a2a_v1::stream_hub::A2aStreamHub>,
    guard: Option<StreamGuardCtx>,
) -> impl Stream<Item = Result<Event, axum::Error>> {
    let (tx, rx) = tokio::sync::mpsc::channel::<Result<Event, axum::Error>>(16);

    tokio::spawn(async move {
        let task_id = initial_task.id.clone();
        let started = Instant::now();

        // Subscribe to event-driven wakeups BEFORE emitting the first frame so
        // a write that lands while we set up is caught by the reconcile tick at
        // worst, not lost. Every `task_store::write` (any replica) wakes this.
        // The guard releases the hub entry however this task
        // ends: terminal state, time cap, or the client walking away.
        let (mut wake, _hub_subscription) = hub.subscribe_guarded(&tenant_id, &task_id);

        // First frame: the current Task envelope.
        let initial = StreamResponse::Task(initial_task.clone());
        if send_frame(&tx, &initial).await.is_err() {
            return;
        }
        let mut last_state = initial_task.status.state;
        let mut last_artifact_count = initial_task.artifacts.len();
        // Track the latest full status + context so the cap/shutdown paths can
        // send a proper terminal frame instead of silently dropping the socket.
        let mut last_status = initial_task.status.clone();
        let stream_context_id = initial_task.context_id.clone().unwrap_or_default();

        // Emit a final (`final: true`) status frame, then end. Used on the
        // duration-cap and shutdown paths so the client always sees a clean
        // termination signal even when the task is still non-terminal.
        async fn emit_final(
            tx: &tokio::sync::mpsc::Sender<Result<Event, axum::Error>>,
            task_id: &str,
            context_id: &str,
            status: &crate::services::a2a_v1::TaskStatus,
        ) {
            let ev = TaskStatusUpdateEvent {
                task_id: task_id.to_string(),
                context_id: context_id.to_string(),
                status: status.clone(),
                final_: true,
                metadata: None,
            };
            let _ = send_frame(tx, &StreamResponse::StatusUpdate(ev)).await;
        }

        // Emit a terminal `failed` status frame carrying a guardrail
        // refusal, then end. Used when an OUTPUT guardrail BLOCKS a
        // frame the stream was about to emit — the offending artifact /
        // status text is never sent; the subscriber sees a clean
        // terminal `failed` instead.
        async fn emit_guardrail_block(
            tx: &tokio::sync::mpsc::Sender<Result<Event, axum::Error>>,
            task_id: &str,
            context_id: &str,
        ) {
            let status = crate::services::a2a_v1::TaskStatus {
                state: TaskState::TaskStateFailed,
                message: Some(Message {
                    message_id: Ulid::new().to_string(),
                    context_id: None,
                    task_id: None,
                    role: Role::RoleAgent,
                    parts: vec![Part {
                        content: PartContent::Text(
                            "Response blocked by tenant output guardrail".to_string(),
                        ),
                        metadata: None,
                        filename: None,
                        media_type: None,
                    }],
                    metadata: None,
                    extensions: Vec::new(),
                    reference_task_ids: Vec::new(),
                }),
                timestamp: Some(Utc::now()),
            };
            let ev = TaskStatusUpdateEvent {
                task_id: task_id.to_string(),
                context_id: context_id.to_string(),
                status,
                final_: true,
                metadata: None,
            };
            let _ = send_frame(tx, &StreamResponse::StatusUpdate(ev)).await;
        }

        // Poll until terminal or shutdown.
        loop {
            if shutdown.is_cancelled() {
                tracing::info!(task_id = %task_id, "a2a_v1: stream canceled by shutdown");
                emit_final(&tx, &task_id, &stream_context_id, &last_status).await;
                break;
            }
            if started.elapsed() > STREAM_MAX_DURATION {
                tracing::info!(
                    task_id = %task_id,
                    "a2a_v1: stream hit max duration — emitting final frame"
                );
                emit_final(&tx, &task_id, &stream_context_id, &last_status).await;
                break;
            }

            // Wait for an event-driven wake (a task write, relayed via the hub),
            // the safety-net reconcile tick, or shutdown — whichever fires
            // first. Either wake reason just means "re-read and emit any new
            // frames"; the read below is the single source of truth.
            tokio::select! {
                _ = shutdown.cancelled() => {
                    emit_final(&tx, &task_id, &stream_context_id, &last_status).await;
                    break;
                }
                _ = tokio::time::sleep(STREAM_SAFETY_NET_INTERVAL) => {}
                r = wake.recv() => {
                    match r {
                        // Woken (or coalesced multiple wakes) — re-read below.
                        Ok(()) | Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => {}
                        // The hub entry was GC'd out from under us (a notify
                        // raced an apparent drop). Re-subscribe so later writes
                        // keep waking us; the reconcile tick bridges the gap.
                        Err(tokio::sync::broadcast::error::RecvError::Closed) => {
                            wake = hub.subscribe(&tenant_id, &task_id);
                        }
                    }
                }
            }

            let current = match task_store::get(&db, &tenant_id, &task_id).await {
                Ok(t) => t,
                Err(e) => {
                    tracing::warn!(task_id = %task_id, error = %e, "a2a_v1: stream read failed");
                    continue;
                }
            };

            // Emit any artifacts the dispatch helper appended since
            // the last poll. Each new artifact gets its own
            // TaskArtifactUpdateEvent — clients can render them
            // before the terminal status frame arrives.
            if current.artifacts.len() > last_artifact_count {
                let context_id = current.context_id.clone().unwrap_or_default();
                let new_artifacts = current.artifacts[last_artifact_count..].to_vec();
                let total = current.artifacts.len();
                for (idx, mut artifact) in new_artifacts.into_iter().enumerate() {
                    // OUTPUT guardrail on the artifact text before
                    // it leaves our tenant. Block → terminate the stream
                    // with a guardrail-failed frame; redact → splice the
                    // rewritten text into the artifact before emit.
                    if let Some(ref g) = guard {
                        match stream_guard_check(g, &a2a_guardrail::artifact_text(&artifact)).await
                        {
                            Ok(None) => {}
                            Ok(Some(new_text)) => {
                                a2a_guardrail::apply_artifact_redactions(&mut artifact, &new_text);
                            }
                            Err(refusal) => {
                                tracing::warn!(
                                    task_id = %task_id,
                                    "a2a_v1: stream artifact blocked by output guardrail",
                                );
                                g.record_block(&current.id, &refusal);
                                emit_guardrail_block(&tx, &current.id, &context_id).await;
                                return;
                            }
                        }
                    }
                    let ev = TaskArtifactUpdateEvent {
                        task_id: current.id.clone(),
                        context_id: context_id.clone(),
                        artifact,
                        append: false,
                        last_chunk: last_artifact_count + idx + 1 == total,
                        metadata: None,
                    };
                    if send_frame(&tx, &StreamResponse::ArtifactUpdate(ev))
                        .await
                        .is_err()
                    {
                        return;
                    }
                }
                last_artifact_count = current.artifacts.len();
            }

            last_status = current.status.clone();
            if current.status.state != last_state {
                let is_terminal = current.status.state.is_terminal();
                let mut status_for_frame = current.status.clone();
                // OUTPUT guardrail on the status message text
                // before it leaves our tenant. Block → terminate with a
                // guardrail-failed frame; redact → splice into the
                // status message before emit.
                if let Some(ref g) = guard {
                    let status_text = status_for_frame
                        .message
                        .as_ref()
                        .map(a2a_guardrail::message_text)
                        .unwrap_or_default();
                    match stream_guard_check(g, &status_text).await {
                        Ok(None) => {}
                        Ok(Some(new_text)) => {
                            if let Some(msg) = status_for_frame.message.as_mut() {
                                a2a_guardrail::apply_message_redactions(msg, &new_text);
                            }
                        }
                        Err(refusal) => {
                            tracing::warn!(
                                task_id = %task_id,
                                "a2a_v1: stream status blocked by output guardrail",
                            );
                            g.record_block(&current.id, &refusal);
                            let cid = current.context_id.clone().unwrap_or_default();
                            emit_guardrail_block(&tx, &current.id, &cid).await;
                            return;
                        }
                    }
                }
                let ev = TaskStatusUpdateEvent {
                    task_id: current.id.clone(),
                    context_id: current.context_id.clone().unwrap_or_default(),
                    status: status_for_frame,
                    // The terminal transition IS the final frame.
                    final_: is_terminal,
                    metadata: None,
                };
                if send_frame(&tx, &StreamResponse::StatusUpdate(ev))
                    .await
                    .is_err()
                {
                    break;
                }
                last_state = current.status.state;
                if is_terminal {
                    break;
                }
            }
        }
    });

    ReceiverStream::new(rx)
}

async fn send_frame(
    tx: &tokio::sync::mpsc::Sender<Result<Event, axum::Error>>,
    payload: &StreamResponse,
) -> Result<(), ()> {
    let s = serde_json::to_string(payload).map_err(|_| ())?;
    tx.send(Ok(Event::default().data(s))).await.map_err(|_| ())
}

// ---------------------------------------------------------------------------
// get_task — GET /a2a/{tenant}/{card}/tasks/{task_id}
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
pub struct GetTaskQuery {
    /// Optional cap on returned `history[]` entries. 0 = unbounded.
    #[serde(rename = "historyLength")]
    pub history_length: Option<i32>,
}

pub(crate) async fn get_task(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    Path((tenant_id, card_name, task_id)): Path<(String, String, String)>,
    Query(q): Query<GetTaskQuery>,
    headers: HeaderMap,
) -> Response {
    let ctx = match resolve_request(
        &state,
        &headers,
        client_addr.ip(),
        &tenant_id,
        &card_name,
        V1Target::Task(&task_id),
    )
    .await
    {
        Ok(c) => c,
        Err(e) => return e.into_response(),
    };

    let req = GetTaskRequest {
        tenant: Some(tenant_id.clone()),
        id: task_id.clone(),
        history_length: q.history_length,
    };
    // A caller may only read a task in their own group.
    match task_store::get_scoped(
        &state.db,
        &tenant_id,
        &req.id,
        ctx.resolved_group_id.as_deref(),
    )
    .await
    {
        Ok(mut task) => {
            if let Some(limit) = req.history_length {
                if limit >= 0 {
                    apply_history_limit(&mut task, limit as usize);
                }
            }
            let _ = log_and_finalise(
                &state,
                &ctx,
                StatusCode::OK,
                None,
                Some(&task.id),
                LogBodies::default(),
            )
            .await;
            Json(task).into_response()
        }
        Err(task_store::TaskStoreError::NotFound) => A2aError::new(
            A2aErrorKind::TaskNotFound,
            "The specified task id does not exist",
        )
        .with_metadata("taskId", req.id)
        .into_response(),
        Err(e) => {
            tracing::error!(error = %e, "a2a_v1.get_task: store error");
            A2aError::new(A2aErrorKind::InvalidAgentResponse, "Task lookup failed").into_response()
        }
    }
}

fn apply_history_limit(task: &mut Task, limit: usize) {
    if limit == 0 {
        task.history.clear();
    } else if task.history.len() > limit {
        let start = task.history.len() - limit;
        task.history = task.history.split_off(start);
    }
}

// ---------------------------------------------------------------------------
// list_tasks — GET /a2a/{tenant}/{card}/tasks
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
pub struct ListTasksQuery {
    #[serde(rename = "contextId")]
    pub context_id: Option<String>,
    #[serde(rename = "status")]
    pub status: Option<String>,
    #[serde(rename = "pageSize")]
    pub page_size: Option<i32>,
    #[serde(rename = "pageToken")]
    pub page_token: Option<String>,
    #[serde(rename = "historyLength")]
    pub history_length: Option<i32>,
    #[serde(rename = "statusTimestampAfter")]
    pub status_timestamp_after: Option<DateTime<Utc>>,
    #[serde(rename = "includeArtifacts")]
    pub include_artifacts: Option<bool>,
}

pub(crate) async fn list_tasks(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    Path((tenant_id, card_name)): Path<(String, String)>,
    Query(q): Query<ListTasksQuery>,
    headers: HeaderMap,
) -> Response {
    let ctx = match resolve_request(
        &state,
        &headers,
        client_addr.ip(),
        &tenant_id,
        &card_name,
        V1Target::NoTask,
    )
    .await
    {
        Ok(c) => c,
        Err(e) => return e.into_response(),
    };

    let state_filter = match q.status.as_deref().map(parse_task_state) {
        Some(Ok(s)) => Some(s),
        Some(Err(e)) => return e.into_response(),
        None => None,
    };

    let query = task_store::ListQuery {
        context_id: q.context_id.clone(),
        state: state_filter,
        page_size: q.page_size,
        page_token: q.page_token.clone(),
        status_timestamp_after: q.status_timestamp_after,
        // Only the caller's own group's tasks.
        caller_group: ctx.resolved_group_id.clone(),
    };

    let request = ListTasksRequest {
        tenant: Some(tenant_id.clone()),
        context_id: q.context_id,
        status: state_filter,
        page_size: q.page_size,
        page_token: q.page_token,
        history_length: q.history_length,
        status_timestamp_after: q.status_timestamp_after,
        include_artifacts: q.include_artifacts,
    };

    match task_store::list(&state.db, &tenant_id, &query).await {
        Ok(mut result) => {
            if let Some(limit) = request.history_length {
                if limit >= 0 {
                    for t in &mut result.tasks {
                        apply_history_limit(t, limit as usize);
                    }
                }
            }
            if matches!(request.include_artifacts, Some(false)) {
                for t in &mut result.tasks {
                    t.artifacts.clear();
                }
            }
            let _ = log_and_finalise(
                &state,
                &ctx,
                StatusCode::OK,
                None,
                None,
                LogBodies::default(),
            )
            .await;
            let body = ListTasksResponse {
                tasks: result.tasks,
                next_page_token: result.next_page_token,
                page_size: result.page_size,
                total_size: result.total_size,
            };
            Json(body).into_response()
        }
        Err(task_store::TaskStoreError::BadPageToken(msg)) => A2aError::new(
            A2aErrorKind::ContentTypeNotSupported,
            format!("Invalid page_token: {msg}"),
        )
        .into_response(),
        Err(e) => {
            tracing::error!(error = %e, "a2a_v1.list_tasks: store error");
            A2aError::new(A2aErrorKind::InvalidAgentResponse, "Task listing failed").into_response()
        }
    }
}

fn parse_task_state(s: &str) -> Result<TaskState, A2aError> {
    // Accept the exact wire value (`TASK_STATE_WORKING`) only. The
    // spec doesn't define aliases and we don't invent them — anything
    // else surfaces a CONTENT_TYPE_NOT_SUPPORTED-adjacent 400.
    let v = serde_json::from_value::<TaskState>(Value::String(s.to_string())).map_err(|_| {
        A2aError::new(
            A2aErrorKind::ContentTypeNotSupported,
            format!("Unknown task state `{s}`"),
        )
    })?;
    Ok(v)
}

// ---------------------------------------------------------------------------
// cancel_task — POST /a2a/{tenant}/{card}/tasks/{task_id}:cancel
// ---------------------------------------------------------------------------

pub(crate) async fn cancel_task(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    Path((tenant_id, card_name, task_id)): Path<(String, String, String)>,
    headers: HeaderMap,
) -> Response {
    let ctx = match resolve_request(
        &state,
        &headers,
        client_addr.ip(),
        &tenant_id,
        &card_name,
        V1Target::Task(&task_id),
    )
    .await
    {
        Ok(c) => c,
        Err(e) => return e.into_response(),
    };

    let _req = CancelTaskRequest {
        tenant: Some(tenant_id.clone()),
        id: task_id.clone(),
        metadata: None,
    };

    // Cancelling is scoped to the caller's group.
    match task_store::cancel(
        &state.db,
        &tenant_id,
        &task_id,
        ctx.resolved_group_id.as_deref(),
    )
    .await
    {
        Ok(task) => {
            let _ = log_and_finalise(
                &state,
                &ctx,
                StatusCode::OK,
                None,
                Some(&task.id),
                LogBodies::default(),
            )
            .await;
            Json(task).into_response()
        }
        Err(task_store::TaskStoreError::NotFound) => A2aError::new(
            A2aErrorKind::TaskNotFound,
            "The specified task id does not exist",
        )
        .with_metadata("taskId", task_id)
        .into_response(),
        Err(task_store::TaskStoreError::NotCancelable { state: st }) => A2aError::new(
            A2aErrorKind::TaskNotCancelable,
            format!("Task is in state {st:?} and cannot be cancelled"),
        )
        .with_metadata("taskId", task_id)
        .into_response(),
        Err(e) => {
            tracing::error!(error = %e, "a2a_v1.cancel_task: store error");
            A2aError::new(A2aErrorKind::InvalidAgentResponse, "Task cancel failed").into_response()
        }
    }
}

// ---------------------------------------------------------------------------
// subscribe_to_task — GET /a2a/{tenant}/{card}/tasks/{task_id}:subscribe (SSE)
// ---------------------------------------------------------------------------

pub(crate) async fn subscribe_to_task(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    Path((tenant_id, card_name, task_id)): Path<(String, String, String)>,
    headers: HeaderMap,
) -> Response {
    let ctx = match resolve_request(
        &state,
        &headers,
        client_addr.ip(),
        &tenant_id,
        &card_name,
        V1Target::Task(&task_id),
    )
    .await
    {
        Ok(c) => c,
        Err(e) => return e.into_response(),
    };

    let _req = SubscribeToTaskRequest {
        tenant: Some(tenant_id.clone()),
        id: task_id.clone(),
    };

    let current = match visible_task(&state, &tenant_id, &task_id, &ctx).await {
        Ok(t) => t,
        Err(e) => return e.into_response(),
    };

    let _ = log_and_finalise(
        &state,
        &ctx,
        StatusCode::OK,
        None,
        Some(&current.id),
        LogBodies::default(),
    )
    .await;

    let db = state.db.clone();
    let shutdown = state.shutdown_token.clone();
    let guard = Some(StreamGuardCtx::new(&state, &ctx, &tenant_id, "v1"));
    let stream = build_task_stream(
        db,
        tenant_id.clone(),
        current,
        shutdown,
        state.a2a_stream_hub.clone(),
        guard,
    );
    Sse::new(stream)
        .keep_alive(KeepAlive::default())
        .into_response()
}

// ---------------------------------------------------------------------------
// get_extended_agent_card — GET /a2a/{tenant}/{card}/extendedAgentCard
// ---------------------------------------------------------------------------

pub(crate) async fn get_extended_agent_card(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    Path((tenant_id, card_name)): Path<(String, String)>,
    headers: HeaderMap,
) -> Response {
    let ctx = match resolve_request(
        &state,
        &headers,
        client_addr.ip(),
        &tenant_id,
        &card_name,
        V1Target::NoTask,
    )
    .await
    {
        Ok(c) => c,
        Err(e) => return e.into_response(),
    };

    // GetExtendedAgentCard (section 3.3.1 of the A2A specification) — only valid when the card's
    // capabilities advertise `extended_agent_card = true` and the
    // tenant has configured an extended_card_json payload.
    let extended = match &ctx.card.extended_card_json {
        Some(v) => v.clone(),
        None => {
            return A2aError::new(
                A2aErrorKind::ExtendedAgentCardNotConfigured,
                "No extended Agent Card configured for this card",
            )
            .with_metadata("cardId", &ctx.card.id)
            .into_response();
        }
    };

    // Round-trip through the typed model so we reject mis-shaped rows
    // at the edge rather than echoing invalid JSON to callers.
    let parsed: AgentCard = match serde_json::from_value(extended.clone()) {
        Ok(c) => c,
        Err(e) => {
            tracing::error!(card_id = %ctx.card.id, error = %e,
                "a2a_v1.get_extended_agent_card: stored card failed v1.0 parse");
            return A2aError::new(
                A2aErrorKind::InvalidAgentResponse,
                "Extended Agent Card is not a valid v1.0 document",
            )
            .into_response();
        }
    };

    let _ = log_and_finalise(
        &state,
        &ctx,
        StatusCode::OK,
        None,
        Some(&ctx.card.id),
        LogBodies::default(),
    )
    .await;
    Json(parsed).into_response()
}

// ---------------------------------------------------------------------------
// Push notification config endpoints
// ---------------------------------------------------------------------------

pub(crate) async fn create_push_config(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    Path((tenant_id, card_name, task_id)): Path<(String, String, String)>,
    headers: HeaderMap,
    Json(mut config): Json<TaskPushNotificationConfig>,
) -> Response {
    let ctx = match resolve_request(
        &state,
        &headers,
        client_addr.ip(),
        &tenant_id,
        &card_name,
        V1Target::Task(&task_id),
    )
    .await
    {
        Ok(c) => c,
        Err(e) => return e.into_response(),
    };

    // Verify the task exists before writing a config for it — the FK
    // would block the insert too, but we surface a typed A2A error
    // instead of an opaque 500.
    if let Err(e) = visible_task(&state, &tenant_id, &task_id, &ctx).await {
        return e.into_response();
    }

    config.task_id = Some(task_id.clone());
    match push_store::create(&state.db, &tenant_id, &task_id, config).await {
        Ok(created) => {
            let _ = log_and_finalise(
                &state,
                &ctx,
                StatusCode::OK,
                None,
                Some(&task_id),
                LogBodies::default(),
            )
            .await;
            Json(created).into_response()
        }
        Err(e) => {
            tracing::error!(error = %e, "a2a_v1.create_push_config: store error");
            A2aError::new(
                A2aErrorKind::InvalidAgentResponse,
                "Failed to persist push-notification config",
            )
            .into_response()
        }
    }
}

pub(crate) async fn get_push_config(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    Path((tenant_id, card_name, task_id, config_id)): Path<(String, String, String, String)>,
    headers: HeaderMap,
) -> Response {
    let ctx = match resolve_request(
        &state,
        &headers,
        client_addr.ip(),
        &tenant_id,
        &card_name,
        V1Target::Task(&task_id),
    )
    .await
    {
        Ok(c) => c,
        Err(e) => return e.into_response(),
    };

    let _req = GetTaskPushNotificationConfigRequest {
        tenant: Some(tenant_id.clone()),
        task_id: task_id.clone(),
        id: config_id.clone(),
    };

    // The configurations are the task's: served to a caller that may see
    // the task.
    if let Err(e) = visible_task(&state, &tenant_id, &task_id, &ctx).await {
        return e.into_response();
    }
    match push_store::get(&state.db, &tenant_id, &task_id, &config_id).await {
        Ok(cfg) => {
            let _ = log_and_finalise(
                &state,
                &ctx,
                StatusCode::OK,
                None,
                Some(&task_id),
                LogBodies::default(),
            )
            .await;
            Json(cfg).into_response()
        }
        Err(push_store::PushStoreError::NotFound) => A2aError::new(
            A2aErrorKind::TaskNotFound,
            "Push notification config not found",
        )
        .with_metadata("configId", config_id)
        .into_response(),
        Err(e) => {
            tracing::error!(error = %e, "a2a_v1.get_push_config: store error");
            A2aError::new(
                A2aErrorKind::InvalidAgentResponse,
                "Push config lookup failed",
            )
            .into_response()
        }
    }
}

#[derive(Debug, Deserialize)]
pub struct ListPushConfigsQuery {
    #[serde(rename = "pageSize")]
    pub page_size: Option<i32>,
    #[serde(rename = "pageToken")]
    pub page_token: Option<String>,
}

pub(crate) async fn list_push_configs(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    Path((tenant_id, card_name, task_id)): Path<(String, String, String)>,
    Query(q): Query<ListPushConfigsQuery>,
    headers: HeaderMap,
) -> Response {
    let ctx = match resolve_request(
        &state,
        &headers,
        client_addr.ip(),
        &tenant_id,
        &card_name,
        V1Target::Task(&task_id),
    )
    .await
    {
        Ok(c) => c,
        Err(e) => return e.into_response(),
    };

    let query = push_store::ListQuery {
        page_size: q.page_size,
        page_token: q.page_token,
    };
    // The configurations are the task's: served to a caller that may see
    // the task.
    if let Err(e) = visible_task(&state, &tenant_id, &task_id, &ctx).await {
        return e.into_response();
    }
    match push_store::list(&state.db, &tenant_id, &task_id, &query).await {
        Ok(result) => {
            let _ = log_and_finalise(
                &state,
                &ctx,
                StatusCode::OK,
                None,
                Some(&task_id),
                LogBodies::default(),
            )
            .await;
            let body = crate::services::a2a_v1::ListTaskPushNotificationConfigsResponse {
                configs: result.configs,
                next_page_token: result.next_page_token,
            };
            Json(body).into_response()
        }
        Err(push_store::PushStoreError::BadPageToken(msg)) => A2aError::new(
            A2aErrorKind::ContentTypeNotSupported,
            format!("Invalid page_token: {msg}"),
        )
        .into_response(),
        Err(e) => {
            tracing::error!(error = %e, "a2a_v1.list_push_configs: store error");
            A2aError::new(
                A2aErrorKind::InvalidAgentResponse,
                "Push config list failed",
            )
            .into_response()
        }
    }
}

pub(crate) async fn delete_push_config(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    Path((tenant_id, card_name, task_id, config_id)): Path<(String, String, String, String)>,
    headers: HeaderMap,
) -> Response {
    let ctx = match resolve_request(
        &state,
        &headers,
        client_addr.ip(),
        &tenant_id,
        &card_name,
        V1Target::Task(&task_id),
    )
    .await
    {
        Ok(c) => c,
        Err(e) => return e.into_response(),
    };

    let _req = DeleteTaskPushNotificationConfigRequest {
        tenant: Some(tenant_id.clone()),
        task_id: task_id.clone(),
        id: config_id.clone(),
    };

    // The configurations are the task's: served to a caller that may see
    // the task.
    if let Err(e) = visible_task(&state, &tenant_id, &task_id, &ctx).await {
        return e.into_response();
    }
    match push_store::delete(&state.db, &tenant_id, &task_id, &config_id).await {
        Ok(()) => {
            let _ = log_and_finalise(
                &state,
                &ctx,
                StatusCode::NO_CONTENT,
                None,
                Some(&task_id),
                LogBodies::default(),
            )
            .await;
            StatusCode::NO_CONTENT.into_response()
        }
        Err(e) => {
            tracing::error!(error = %e, "a2a_v1.delete_push_config: store error");
            A2aError::new(
                A2aErrorKind::InvalidAgentResponse,
                "Push config delete failed",
            )
            .into_response()
        }
    }
}

// ---------------------------------------------------------------------------
// Proxy-log helper
// ---------------------------------------------------------------------------

/// Write one row to `proxy_logs` summarising the step. Returns `None`
/// on success so callers can use `.unwrap_or_else(...)` in the error
/// branch too without synthesising a value.
/// Body wrappers passed to `log_and_finalise` so the proxy_logs
/// row carries the request and response payloads. Both are
/// optional so callers in error paths (where we never read the
/// body, or already failed before serialising the response) can
/// pass `None` without ceremony.
#[derive(Default)]
struct LogBodies<'a> {
    request: Option<&'a serde_json::Value>,
    response: Option<&'a serde_json::Value>,
}

/// Build the spec-shaped path for the v1.0 inbound endpoint a
/// caller hit, used as both `endpoint` and the path component of
/// `target_url` on the proxy_log row. Lets operators filter logs
/// per verb (`message:send` vs `tasks/{id}:cancel`) instead of
/// every v1.0 row collapsing to the same bucket.
fn v1_inbound_path(tenant_id: &str, card_name: &str, verb: &str) -> String {
    format!("/a2a/{tenant_id}/{card_name}/{verb}")
}

async fn log_and_finalise(
    state: &SharedState,
    ctx: &RequestCtx,
    status: StatusCode,
    error: Option<&str>,
    task_id: Option<&str>,
    bodies: LogBodies<'_>,
) -> Option<Response> {
    log_and_finalise_with_path(state, ctx, status, error, task_id, bodies, "v1").await
}

/// Path-aware variant. Routes that know the spec verb they
/// implement (`message:send`, `tasks/{id}:cancel`, etc.) call this
/// with the verb so the proxy_log row records the precise endpoint
/// + a stamped `target_url`. Routes that haven't been migrated
///   yet fall back to the umbrella `log_and_finalise` above which
///   stamps `/a2a/v1` (legacy behaviour, harmless).
async fn log_and_finalise_with_path(
    state: &SharedState,
    ctx: &RequestCtx,
    status: StatusCode,
    error: Option<&str>,
    task_id: Option<&str>,
    bodies: LogBodies<'_>,
    verb: &str,
) -> Option<Response> {
    write_v1_row(
        state,
        &ctx.parts(),
        status,
        error,
        task_id,
        bodies,
        verb,
        |_| {},
    );
    None
}

/// The row-building parts of a request — everything [`write_v1_row`] needs,
/// available before a full [`RequestCtx`] exists (the refusals inside
/// [`resolve_request`] happen before one is built).
struct RowParts<'a> {
    auth: &'a AuthContext,
    card_id: &'a str,
    card_name: &'a str,
    correlation_id: &'a str,
    resolved_group_id: Option<&'a str>,
    asserted_run_root: Option<Ulid>,
    /// The call's run (see [`V1Target`]); a row with no task id yet still
    /// lands on it.
    run_root: Option<&'a str>,
    start: Instant,
}

impl RequestCtx {
    fn parts(&self) -> RowParts<'_> {
        RowParts {
            auth: &self.auth,
            card_id: &self.card.id,
            card_name: &self.card.name,
            correlation_id: &self.correlation_id,
            resolved_group_id: self.resolved_group_id.as_deref(),
            asserted_run_root: self.asserted_run_root,
            run_root: self.run_root.as_deref(),
            start: self.start,
        }
    }
}

/// Write one `proxy_logs` row for a v1.0 inbound step. `stamp` marks the
/// governance outcome (guardrail block, refusal subtype, agent decision) so
/// the writer seals the verdict it was (every verdict — an
/// A2A v1 refusal used to write no row at all).
#[allow(clippy::too_many_arguments)]
fn write_v1_row(
    state: &SharedState,
    parts: &RowParts<'_>,
    status: StatusCode,
    error: Option<&str>,
    task_id: Option<&str>,
    bodies: LogBodies<'_>,
    verb: &str,
    stamp: impl FnOnce(&mut crate::models::db::ProxyLogInsert),
) {
    let duration_ms = parts.start.elapsed().as_secs_f64() * 1000.0;
    // For non-tenant-scoped contexts (verb=="v1" fallback) the
    // tenant comes from the auth ctx. The spec-shaped routes always
    // path through resolve_request, which seeds it.
    let tenant_id = parts.auth.tenant_id.as_str();
    let endpoint = if verb == "v1" {
        "/a2a/v1".to_string()
    } else {
        v1_inbound_path(tenant_id, parts.card_name, verb)
    };
    let target_url = endpoint.clone();
    let mut log = proxy_logger::build_proxy_log(
        constants::proxy::A2A,
        parts.auth,
        "POST",
        &endpoint,
        Some(&target_url),
        Some(parts.card_id),
        status.as_u16(),
        duration_ms,
        status.is_success(),
        error,
        parts.correlation_id,
    );
    log.proxy_subtype = Some(PROXY_SUBTYPE_V1_INBOUND.to_string());
    log.resource_group_id = parts
        .resolved_group_id
        .map(str::to_owned)
        .or_else(|| parts.auth.resolved_group_id.clone());
    if let Some(tid) = task_id {
        log.current_task_id = Some(tid.to_string());
        // For inbound v1.0 calls with no upstream chain, the task is
        // its own root — stamp root_task_id so the row appears in the
        // delegation-graph aggregation (which keys on root_task_id).
        // When the v1.0 route eventually fans out to a downstream
        // agent, the outbound step will reuse this same root_task_id
        // so the graph shows the full tree.
        if log.root_task_id.is_none() {
            // An asserted run wins over "the task is its own root": the caller
            // told us this step belongs to a larger unit of work.
            log.root_task_id = Some(match parts.asserted_run_root {
                Some(root) => root.to_string(),
                None => tid.to_string(),
            });
            if parts.asserted_run_root.is_some() {
                log.trace_continuity = Some(
                    call_chain::TraceContinuity::ClientAsserted
                        .as_str()
                        .to_string(),
                );
            }
        }
        if log.delegation_depth.is_none() {
            log.delegation_depth = Some(0);
        }
    } else if let Some(root) = parts.run_root {
        // A refusal before any task exists still belongs to the call's run:
        // the asserted one, or the run its new task will be.
        log.root_task_id = Some(root.to_string());
        if log.delegation_depth.is_none() {
            log.delegation_depth = Some(0);
        }
    }

    // Bodies are stored verbatim (truncation/scrubbing happens
    // downstream of the writer). Sized in bytes so the admin UI's
    // size column reflects the actual payload, not the
    // metadata-only zero we'd otherwise leave. Same shape as the
    // outbound a2a logger.
    if let Some(body) = bodies.request {
        if let Ok(s) = serde_json::to_string(body) {
            log.request_size_bytes = Some(s.len() as i32);
            log.request_body_preview = Some(proxy_logger::body_preview(&s));
        }
    }
    if let Some(body) = bodies.response {
        if let Ok(s) = serde_json::to_string(body) {
            log.response_size_bytes = Some(s.len() as i32);
            log.response_body_preview = Some(proxy_logger::body_preview(&s));
        }
    }
    stamp(&mut log);

    state
        .background_writer
        .write_proxy_log(state.db.clone(), log);
}

/// Log a guardrail refusal (input or output) on a v1.0 verb and return the
/// spec error. A policy block is marked blocked, so it is sealed `blocked`;
/// the orchestrator already recorded which config and check fired. A refusal
/// because the guardrails could not be read is sealed `engine_failure`
/// ([`stamp_guardrail_refusal`]).
fn log_guardrail_block(
    state: &SharedState,
    ctx: &RequestCtx,
    err: A2aError,
    task_id: Option<&str>,
    bodies: LogBodies<'_>,
    verb: &str,
) -> Response {
    let kind = err.kind();
    write_v1_row(
        state,
        &ctx.parts(),
        kind.http_status(),
        Some(err.message()),
        task_id,
        bodies,
        verb,
        |log| stamp_guardrail_refusal(log, kind),
    );
    err.into_response()
}

/// The audit stamp for a guardrail refusal of `kind`: no policy decided an
/// `engine_failure` kind, so it must not read as a guardrail block.
fn stamp_guardrail_refusal(log: &mut crate::models::db::ProxyLogInsert, kind: A2aErrorKind) {
    if kind.engine_failure() {
        log.proxy_subtype = Some(constants::proxy_subtype::ENGINE_FAILURE.to_string());
    } else {
        proxy_logger::apply_guardrail_block(log);
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::a2a_v1::{Part, PartContent, Role};

    fn sample_message() -> Message {
        Message {
            message_id: "msg-1".into(),
            context_id: None,
            task_id: None,
            role: Role::RoleUser,
            parts: vec![Part {
                content: PartContent::Text("hi".into()),
                metadata: None,
                filename: None,
                media_type: None,
            }],
            metadata: None,
            extensions: Vec::new(),
            reference_task_ids: Vec::new(),
        }
    }

    fn stub_auth_ctx(tenant_id: &str) -> AuthContext {
        AuthContext {
            credential_kind: crate::models::auth::CredentialKind::Anonymous,
            credential_assurance: None,
            credential_ref: None,
            credential_detail: Default::default(),
            identity_verdict: None,
            auth_type: crate::models::auth::AuthType::Anonymous,
            tenant_id: tenant_id.to_string(),
            gateway_user_id: None,
            gateway_username: None,
            gateway_tenant_id: None,
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
            client_ip: None,
            resolved_group_id: None,
            actor_agent_id: None,
            session_token_type: None,
            agent_impl: crate::services::agent_impl::request_carriers(),
            admission_hold: None,
        }
    }

    #[test]
    fn build_task_sets_submitted_state() {
        let ctx_stub = stub_auth_ctx("t-1");
        let card = AgentCardRow {
            id: "card-1".into(),
            tenant_id: "t-1".into(),
            name: "svc".into(),
            card_json: serde_json::json!({}),
            trust_tier: "untrusted".into(),
            enabled: true,
            revoked_at: None,
            extended_card_json: None,
            tenant_limits: None,
            tenant_governance: None,
            capabilities: Vec::new(),
        };
        let rc = RequestCtx {
            tenant: TenantContext {
                tenant_id: "t-1".into(),
                suspended: false,
            },
            auth: ctx_stub,
            card,
            correlation_id: "corr".into(),
            resolved_group_id: None,
            asserted_run_root: None,
            run_root: None,
            new_task_id: None,
            step: std::sync::Mutex::new(None),
            capability: None,
            approvals: Default::default(),
            start: Instant::now(),
        };
        let task = build_task_from_message(&sample_message(), &rc);
        assert_eq!(task.status.state, TaskState::TaskStateSubmitted);
        assert_eq!(task.history.len(), 1);
        assert!(task.history[0].task_id.is_some());
        assert_eq!(task.history[0].task_id.as_deref(), Some(task.id.as_str()));
        assert!(task.context_id.is_some());
    }

    fn capability(
        name: &str,
        handler: Option<&str>,
        enabled: bool,
    ) -> crate::services::cache::AgentCardCapability {
        crate::services::cache::AgentCardCapability {
            id: format!("cap-{name}"),
            capability_name: name.into(),
            enabled,
            timeout_secs: None,
            rate_limit_per_hour: None,
            data_classification: None,
            handler_type: handler.map(str::to_owned),
            handler_ref: handler.map(|_| "ref".to_string()),
        }
    }

    #[test]
    fn a_message_is_dispatched_to_the_hinted_bound_capability_else_the_first_bound() {
        let caps = vec![
            capability("a-noop", None, true),
            capability("b-off", Some("skill"), false),
            capability("c-skill", Some("skill"), true),
            capability("d-demo", Some("multi_turn_demo"), true),
        ];
        let name = |hint: Option<&str>| bound_capability(&caps, hint).map(|c| c.capability_name);
        assert_eq!(name(None).as_deref(), Some("c-skill"));
        assert_eq!(name(Some(" d-demo ")).as_deref(), Some("d-demo"));
        // A hint that names no bound, enabled capability falls back.
        for hint in ["a-noop", "b-off", "nope", " "] {
            assert_eq!(name(Some(hint)).as_deref(), Some("c-skill"), "{hint}");
        }
        // A card that binds nothing dispatches nothing.
        assert_eq!(bound_capability(&caps[..2], None), None);
    }

    #[test]
    fn apply_history_limit_trims_from_front() {
        let msg = sample_message();
        let mut task = Task {
            id: "t".into(),
            context_id: None,
            status: TaskStatus {
                state: TaskState::TaskStateWorking,
                message: None,
                timestamp: None,
            },
            artifacts: Vec::new(),
            history: vec![msg.clone(), msg.clone(), msg.clone(), msg],
            metadata: None,
        };
        apply_history_limit(&mut task, 2);
        assert_eq!(task.history.len(), 2);
    }

    #[test]
    fn apply_history_limit_zero_clears() {
        let msg = sample_message();
        let mut task = Task {
            id: "t".into(),
            context_id: None,
            status: TaskStatus {
                state: TaskState::TaskStateWorking,
                message: None,
                timestamp: None,
            },
            artifacts: Vec::new(),
            history: vec![msg.clone(), msg],
            metadata: None,
        };
        apply_history_limit(&mut task, 0);
        assert!(task.history.is_empty());
    }

    #[test]
    fn parse_task_state_accepts_spec_form() {
        assert_eq!(
            parse_task_state("TASK_STATE_WORKING").unwrap(),
            TaskState::TaskStateWorking,
        );
    }

    #[test]
    fn parse_task_state_rejects_garbage() {
        let err = parse_task_state("NOPE").unwrap_err();
        assert_eq!(err.kind(), A2aErrorKind::ContentTypeNotSupported);
    }

    #[test]
    fn reject_wrong_version_accepts_v1_header() {
        let mut h = HeaderMap::new();
        h.insert("a2a-version", "1.0".parse().unwrap());
        assert!(reject_wrong_version(&h, "/message:send").is_ok());
    }

    #[test]
    fn reject_wrong_version_rejects_v03_header() {
        let mut h = HeaderMap::new();
        h.insert("a2a-version", "0.3".parse().unwrap());
        let err = reject_wrong_version(&h, "/message:send").unwrap_err();
        assert_eq!(err.kind(), A2aErrorKind::VersionNotSupported);
    }

    #[test]
    fn reject_wrong_version_allows_unknown() {
        // Unknown = no signal, proceed.
        let h = HeaderMap::new();
        assert!(reject_wrong_version(&h, "/message:send").is_ok());
    }

    #[test]
    fn router_builds_without_path_conflicts() {
        // Just constructing `router()` asserts axum can resolve the
        // path matrix. Any overlapping route (e.g. if we accidentally
        // had two handlers on the same method+path) would panic here.
        let _ = router();
    }

    #[test]
    fn split_task_ref_bare_id() {
        let (id, verb) = split_task_ref("abc123");
        assert_eq!(id, "abc123");
        assert!(verb.is_none());
    }

    #[test]
    fn split_task_ref_with_cancel_verb() {
        let (id, verb) = split_task_ref("abc123:cancel");
        assert_eq!(id, "abc123");
        assert_eq!(verb.as_deref(), Some("cancel"));
    }

    #[test]
    fn split_task_ref_with_subscribe_verb() {
        let (id, verb) = split_task_ref("abc123:subscribe");
        assert_eq!(id, "abc123");
        assert_eq!(verb.as_deref(), Some("subscribe"));
    }

    #[test]
    fn split_task_ref_only_splits_on_first_colon() {
        // Task IDs can contain any chars permitted by the spec; a
        // stray colon inside an opaque ID isn't our problem to
        // sanitise. We split on the first one only so multi-colon
        // verbs (hypothetical future) would still slot cleanly.
        let (id, verb) = split_task_ref("a:b:c");
        assert_eq!(id, "a");
        assert_eq!(verb.as_deref(), Some("b:c"));
    }
}
