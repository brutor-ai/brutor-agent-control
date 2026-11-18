// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # A2A (Agent-to-Agent) Proxy — Inbound, Outbound, and Discovery
//!
//! This module exposes three endpoints that together make Brutor a
//! participant in the A2A ecosystem:
//!
//! ## `POST /v1/proxy/a2a/outbound` — Outbound invocation
//!
//! The local authenticated caller asks us to invoke a capability on a
//! *remote* agent. The proxy authenticates the caller, enforces the
//! caller's resource-group A2A governance (which restrictively
//! inherits the root-group's tenant ceiling), mints or forks a signed
//! delegation chain, and issues the HTTP call.
//!
//! ## `POST /v1/proxy/a2a/inbound` — Inbound invocation
//!
//! A *remote* agent asks us to run a capability on *their* behalf.
//! The proxy authenticates them (API key / OAuth), verifies any
//! supplied delegation chain, looks up the capability's handler
//! binding (see `agent_card_capabilities.handler_type`), and
//! dispatches. Currently supports:
//!
//!   * `skill`   — delegates to `skill_orchestrator::execute_skill`
//!     with `handler_ref` as the skill name/ID.
//!   * `noop`    — returns a non-executing acknowledgement. Default
//!     for newly-created cards so unconfigured handlers never
//!     silently execute something.
//!
//! The inbound route writes a proxy_log row with `proxy_type='a2a'`
//! and `proxy_subtype='inbound'` so Mission Control can distinguish
//! the two directions when reconstructing a delegation graph.
//!
//! ## `GET /.well-known/agent-card/{tenant_id}/{card_name}.json`
//!
//! Unauthenticated discovery. Returns the verbatim `card_json` plus
//! the current Ed25519 signature block (if the card is
//! tenant-signed), so external verifiers can resolve the card
//! without needing API credentials. Disabled / revoked / feature-
//! flag-off tenants return 404 to avoid leaking existence.
//!
//! ## Delegation chain
//!
//! The HMAC-signed `x-brutor-delegation-*` headers are the
//! anti-spoofing primitive. A rogue downstream can at most forward
//! the chain it was given; it cannot forge a chain that claims a
//! lower depth or a different root, because the tenant key never
//! leaves the gateway.
//!
//! Authentication
//! ==============
//! - `/v1/proxy/a2a/outbound` and `/v1/proxy/a2a/inbound`: API Key,
//!   Gateway JWT, or OAuth; `features.a2a` must not be explicitly
//!   `false`.
//! - `/.well-known/agent-card/*`: public. No auth, no tenant header.

use std::time::Instant;

use axum::Router;
use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Json, Response};
use axum::routing::{get, post};
use reqwest::header::{HeaderName as ReqHeaderName, HeaderValue as ReqHeaderValue};
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::PgPool;
use ulid::Ulid;

use crate::constants;
use crate::middleware::tenant::TenantContext;
use crate::models::auth::AuthContext;
use crate::models::proxy::SkillExecuteRequest;
use crate::routes::route_helpers;
use crate::services::a2a_delegation::{DelegationChain, DelegationError, derive_tenant_key};
use crate::services::a2a_v1::errors::A2aError;
use crate::services::a2a_v1::guardrail as a2a_guardrail;
use crate::services::run_controls::RunScope;
use crate::services::skill_orchestrator::{self, SkillError};
use crate::services::{
    SharedState, a2a_governance, agent_approval, auth, call_chain, proxy_logger,
};

// Handler type constants — kept in sync with the Python
// `agent_card_capability.HANDLER_TYPE_*` module constants.
pub const HANDLER_TYPE_SKILL: &str = "skill";
pub const HANDLER_TYPE_NOOP: &str = "noop";

// Proxy subtype values for proxy_logs.proxy_subtype — distinguishes
// inbound vs outbound steps in Mission Control queries.
pub const PROXY_SUBTYPE_INBOUND: &str = "inbound";
pub const PROXY_SUBTYPE_OUTBOUND: &str = "outbound";

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

pub fn router() -> Router<SharedState> {
    Router::new()
        .route("/outbound", post(outbound))
        .route("/inbound", post(inbound))
}

/// Public unauthenticated discovery. Mounted by `main.rs` at the root
/// `/.well-known/agent-card/{tenant_id}/{card_name}.json` path so
/// external agents can resolve cards without any credentials.
///
/// Also mounts the tenant JWK Set at
/// `/.well-known/a2a-jwks/{tenant_id}` (RFC 7517) so verifiers can
/// resolve the `kid` on an Agent Card signature to the signing public
/// key without hitting the admin API.
pub fn discovery_router() -> Router<SharedState> {
    // axum 0.8 forbids path segments that mix a parameter with a literal
    // suffix (e.g. `{card_name}.json`), so the discovery path ends on the
    // card name alone. Brokers still get `application/json` back via the
    // Content-Type header, which is what standard A2A discovery checks.
    Router::new()
        .route(
            "/.well-known/agent-card/{tenant_id}/{card_name}",
            get(discover_card),
        )
        .route("/.well-known/a2a-jwks/{tenant_id}", get(discover_jwks))
}

// ---------------------------------------------------------------------------
// Request / response shapes
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize, utoipa::ToSchema)]
pub struct A2AOutboundRequest {
    /// The gateway's internal ID of the target Agent Card.
    /// (Not the card's `id` field — the DB primary key.)
    pub target_card_id: String,
    /// Dotted A2A capability name, e.g. `billing.refund`. Must match
    /// one of the capabilities advertised on the card.
    pub capability: String,
    /// The A2A message body forwarded verbatim to the remote agent.
    pub message: Value,
}

// ---------------------------------------------------------------------------
// DB rows — scoped to this route; we don't expose them elsewhere.
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, sqlx::FromRow)]
struct AgentCardRow {
    pub id: String,
    #[allow(dead_code)]
    pub tenant_id: String,
    pub name: String,
    pub card_json: Value,
    pub trust_tier: String,
    pub enabled: bool,
    pub revoked_at: Option<chrono::DateTime<chrono::Utc>>,
    /// Tenant-wide A2A limits — served from the card cache so the
    /// quota/delegation checks below never re-SELECT the row.
    pub tenant_limits: Option<Value>,
    /// The card's own A2A policy, judged with the caller's group chain
    /// (`services::a2a_governance`).
    pub tenant_governance: Option<Value>,
}

/// Discovery row carries the signature block alongside the card body
/// so external verifiers can validate the Ed25519 signature against
/// the tenant's public key (fetched via the tenant-key
/// endpoint).
#[derive(Debug, Clone, sqlx::FromRow)]
struct AgentCardDiscoveryRow {
    pub id: String,
    pub tenant_id: String,
    pub name: String,
    pub version: String,
    pub card_json: Value,
    pub signature: Option<String>,
    pub signature_algorithm: Option<String>,
    pub signed_by: Option<String>,
    pub signed_at: Option<chrono::DateTime<chrono::Utc>>,
    pub trust_tier: String,
    pub enabled: bool,
    pub revoked_at: Option<chrono::DateTime<chrono::Utc>>,
}

#[derive(Debug, Clone, sqlx::FromRow)]
struct AgentCardCapabilityRow {
    pub id: String,
    pub capability_name: String,
    pub enabled: bool,
    pub timeout_secs: Option<i32>,
    /// The capability's own hourly rate (`usage::a2a_capability_dims`).
    pub rate_limit_per_hour: Option<i32>,
    pub data_classification: Option<Value>,
    pub handler_type: Option<String>,
    pub handler_ref: Option<String>,
}

// ---------------------------------------------------------------------------
// Handler
// ---------------------------------------------------------------------------

#[utoipa::path(
    post,
    path = "/v1/proxy/a2a/outbound",
    tag = "A2A",
    summary = "Outbound A2A invocation",
    description = "Invoke a capability on a remote A2A agent, enforcing tenant outbound \
                   policy and signing the delegation chain.",
    request_body = A2AOutboundRequest,
    responses(
        (status = 200, description = "Remote agent response (relayed)"),
        (status = 400, description = "Malformed request or card"),
        (status = 401, description = "Authentication required"),
        (status = 403, description = "Blocked by outbound policy"),
        (status = 404, description = "Agent card or capability not found"),
        (status = 502, description = "Remote agent returned an error"),
    ),
    security(("api_key" = []), ("bearer" = []))
)]
pub(crate) async fn outbound(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Json(request): Json<A2AOutboundRequest>,
) -> Response {
    let start = Instant::now();
    let tenant_id = match route_helpers::extract_tenant_id(tenant) {
        Ok(t) => t,
        Err(r) => return r,
    };
    let correlation_id = route_helpers::extract_correlation_id(&headers);

    let mut auth_ctx = match auth::authenticate_request(
        &headers,
        &tenant_id,
        &state.config.jwt_secret,
        &state.db,
        &state.credential_verifiers,
        client_addr.ip(),
        true,
    )
    .await
    {
        Ok(ctx) => ctx,
        Err(e) => return error_response(e.status_code(), &e.to_string()),
    };
    if let Err(e) = crate::services::identity_policy::judge_call(&state, &mut auth_ctx).await {
        return error_response(e.status_code(), &e.to_string());
    }

    // 1. Tenant feature gate: `features.a2a` must not be explicitly false.
    //    Missing entry or missing `features` column = enabled-by-default,
    //    matching the Python tenant `is_feature_enabled()` semantics.
    match check_a2a_enabled(&state, &tenant_id).await {
        Ok(true) => {}
        Ok(false) => {
            return log_and_fail(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                None,
                None,
                StatusCode::FORBIDDEN,
                "A2A feature is disabled for this tenant",
            )
            .await;
        }
        Err(e) => {
            tracing::error!(error = %e, "a2a.outbound: feature gate query failed");
            return log_and_fail(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                None,
                None,
                StatusCode::INTERNAL_SERVER_ERROR,
                "Feature gate lookup failed",
            )
            .await;
        }
    }

    // 2. Load the agent card.
    let card = match load_agent_card(&state, &tenant_id, &request.target_card_id).await {
        Ok(Some(c)) => c,
        Ok(None) => {
            return log_and_fail(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&request.target_card_id),
                None,
                StatusCode::NOT_FOUND,
                "Agent card not found",
            )
            .await;
        }
        Err(e) => {
            tracing::error!(error = %e, "a2a.outbound: card lookup failed");
            return log_and_fail(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&request.target_card_id),
                None,
                StatusCode::INTERNAL_SERVER_ERROR,
                "Card lookup failed",
            )
            .await;
        }
    };

    if !card.enabled {
        return log_and_fail(
            &state,
            &auth_ctx,
            &correlation_id,
            start,
            Some(&card.id),
            None,
            StatusCode::FORBIDDEN,
            "Agent card is disabled",
        )
        .await;
    }
    if card.revoked_at.is_some() || card.trust_tier == "revoked" {
        return log_and_fail(
            &state,
            &auth_ctx,
            &correlation_id,
            start,
            Some(&card.id),
            None,
            StatusCode::FORBIDDEN,
            "Agent card is revoked",
        )
        .await;
    }

    // 2b. Parse the delegation chain — the run this call belongs to.
    //
    // Parsed here, before either authorization plane, because both need it:
    // the run-scoped controls below refuse a run that is over budget or
    // aborted (for agent and non-agent callers alike), and an approval hold
    // raised by the agent plane must name its run so the ledger and the run
    // clock can see the time spent waiting on a person. It used to be parsed
    // after authorization, so A2A holds were created with no run at all.
    let tenant_key = derive_tenant_key(state.config.encryption_key.as_bytes(), &tenant_id);
    let current_step_task_id = Ulid::new();
    let inbound_chain =
        DelegationChain::parse_and_verify(|name| header_value(&headers, name), &tenant_key);

    // Replay guard: a verified inbound chain must also be FRESH. The parent
    // ULID's embedded (signed) timestamp bounds how old a captured header set
    // may be — a stale chain is a replay. Cross-tenant replay is already
    // blocked by the per-tenant key; this bounds same-tenant replay to the
    // freshness window instead of forever.
    if let Ok(ref chain) = inbound_chain {
        let now_ms = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0);
        if let Err(e) = chain.check_fresh(
            now_ms,
            crate::services::a2a_delegation::delegation_max_age_secs(),
            crate::services::a2a_delegation::DELEGATION_FRESHNESS_LEEWAY_SECS,
        ) {
            let msg = e.to_string();
            return log_and_fail(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                None,
                StatusCode::FORBIDDEN,
                &msg,
            )
            .await;
        }
    }

    // Capture this step's own graph-tree IDs before we fork. The fork
    // produces the OUTGOING chain's depth (= downstream's depth); for
    // proxy_logs we want THIS step's depth, which equals the inbound
    // chain's depth (or 0 if this is the root).
    #[allow(clippy::type_complexity)]
    let (my_root, my_depth, my_parent_ulid, my_actor, my_subject): (
        Ulid,
        u32,
        Option<Ulid>,
        Option<String>,
        Option<String>,
    ) = match &inbound_chain {
        Ok(parent) => (
            parent.root_task_id,
            parent.depth,
            Some(parent.parent_task_id),
            parent.actor_agent_id.clone(),
            parent.subject.clone(),
        ),
        // No verifiable inbound chain. Before minting a fresh root, honour a
        // run id the caller asserted: a self-driving agent has no chain to
        // present, so without this its A2A step — and everything the peer then
        // does under the chain we sign from it — starts a run of its own,
        // split from the caller's other steps for the same task.
        //
        // `asserted_root` yields None unless the caller is entitled to the
        // assertion (credential-bound, tenant-keyed, and no chain header
        // present), so this cannot graft a step onto someone else's run.
        Err(_) => {
            let get = |n: &str| header_value(&headers, n);
            match call_chain::asserted_root(&get, &tenant_key) {
                Some(root) => (root, 0, None, None, None),
                None => (current_step_task_id, 0, None, None, None),
            }
        }
    };
    let my_ids = StepLogIds::from_step(current_step_task_id, my_parent_ulid, my_root, my_depth)
        .with_principal(my_actor, my_subject);

    // Run-scoped controls — the run budget and an operator's
    // abort — for EVERY caller, before either authorization plane, so a
    // stopped run cannot even queue an approval. See `run_controls::check`.
    let trace_root_kind = proxy_logger::root_kind_for_auth(&auth_ctx.auth_type, my_depth == 0);
    let run_scope = RunScope {
        run_id: my_root.to_string(),
        parent_task_id: Some(current_step_task_id.to_string()),
        root_kind: trace_root_kind.map(str::to_owned),
        depth: my_depth,
        starter: None,
    };
    // The caller's active group — a group-scoped key carries it; the old
    // check read only `resolved_group_id` (the X-Resource-Group header), so a
    // group key's run was never budgeted here.
    let run_group = auth_ctx.active_resource_group();
    // The hop is one action of the run: held from here, kept when the
    // message leaves for the remote agent, given back if a later gate
    // refuses or holds it.
    // A hop on a verified inbound chain continues its run; any other starts
    // one for its credential if the run is new.
    let starter = crate::services::run_controls::run_starter(&auth_ctx, inbound_chain.is_ok());
    let step = match crate::services::run_controls::check(
        &state,
        crate::services::run_controls::Step {
            tenant_id: &tenant_id,
            group_id: run_group,
            run_id: &run_scope.run_id,
            depth: run_scope.depth,
            kind: crate::services::run_controls::StepKind::Action,
            starter: starter.as_deref(),
        },
    )
    .await
    {
        Ok(admitted) => admitted,
        Err(refusal) => {
            return log_and_refuse(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                run_group,
                &my_ids,
                &refusal,
            );
        }
    };

    // The caller's resource group (see step 4b for how it is used). Resolved
    // here because the accepted-caller gate below needs it.
    let target_group =
        crate::services::usage::resolve_target_group(&state.db, &auth_ctx, &tenant_id).await;
    // Every check below runs only `if let Some(gd) = group_data`, so a
    // group that fails to load must refuse here, not read as "no group".
    let group_data = match &target_group {
        Some(g) => match state
            .group_cache
            .try_get_effective_group(&tenant_id, &g.id)
            .await
        {
            Ok(data) => data,
            Err(e) => {
                tracing::error!(error = %e, tenant_id = %tenant_id, group_id = %g.id, "A2A outbound: caller group load failed; refusing (fail closed)");
                return log_and_refuse_as(
                    &state,
                    &auth_ctx,
                    &correlation_id,
                    start,
                    Some(&card.id),
                    Some(&g.id),
                    &my_ids,
                    StatusCode::FORBIDDEN,
                    constants::proxy_subtype::ENGINE_FAILURE,
                    crate::services::cache::ResourceGroupCache::UNAVAILABLE_MESSAGE,
                );
            }
        },
        None => None,
    };
    let resolved_group_id = target_group.as_ref().map(|g| g.id.clone());

    // Run data scope on the outbound message. The caller's AI
    // System judges its run's message to this agent (rule `a2a:<card name>`)
    // by the message's data parts, before anything is sent.
    if let Some(tg) = target_group.as_ref() {
        use crate::services::data_scope as ds;
        let conflicts = ds::capture(
            &state.db,
            &tg.tenant_id,
            &run_scope.run_id,
            headers.get(ds::SCOPE_HEADER).and_then(|v| v.to_str().ok()),
        )
        .await;
        let tool = format!("{}{}", ds::surface::A2A, card.name);
        if let Some(r) = ds::gate(
            &state.db,
            ds::Call {
                tenant_id: &tg.tenant_id,
                ai_system_id: &tg.id,
                tool: &tool,
                arguments: &ds::a2a_arguments(&request.message),
                root_task_id: &run_scope.run_id,
                conflicts: &conflicts,
                auth_ctx: &auth_ctx,
                correlation_id: &correlation_id,
            },
        )
        .await
        {
            return log_and_refuse_as(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                Some(&tg.id),
                &my_ids,
                StatusCode::FORBIDDEN,
                r.subtype(),
                &r.message,
            );
        }
    }

    // Accepted callers. When the card is a shared service
    // that enforces its accepted callers, a caller system it did not list is
    // refused here: before either authorization plane (an unaccepted caller
    // must not even queue an approval) and before anything is sent. The
    // caller is the group this step's row is attributed to — the one the run
    // ledger resolves to the caller's AI System.
    {
        let caller_group = resolved_group_id
            .as_deref()
            .or(auth_ctx.resolved_group_id.as_deref());
        match crate::services::accepted_callers::check(&state, &tenant_id, caller_group, &card.id)
            .await
        {
            crate::services::accepted_callers::Verdict::Refused(r) => {
                tracing::warn!(
                    tenant_id = %tenant_id,
                    card_id = %card.id,
                    service = %r.service_system_id,
                    caller = ?r.caller_system_id,
                    "A2A outbound refused: caller is not an accepted client of the service",
                );
                return log_and_refuse_as(
                    &state,
                    &auth_ctx,
                    &correlation_id,
                    start,
                    Some(&card.id),
                    caller_group,
                    &my_ids,
                    StatusCode::FORBIDDEN,
                    r.subtype(),
                    &r.message,
                );
            }
            crate::services::accepted_callers::Verdict::Unavailable(_) => {
                return log_and_refuse_as(
                    &state,
                    &auth_ctx,
                    &correlation_id,
                    start,
                    Some(&card.id),
                    caller_group,
                    &my_ids,
                    StatusCode::FORBIDDEN,
                    constants::proxy_subtype::ENGINE_FAILURE,
                    crate::services::accepted_callers::UNAVAILABLE_MESSAGE,
                );
            }
            crate::services::accepted_callers::Verdict::Accepted
            | crate::services::accepted_callers::Verdict::NotApplicable => {}
        }
    }

    // The caller's own plane (`a2a_governance::authorize_actor`): an agent's
    // `a2a_call` grants for this card ("actions, not access"), or, for any
    // other caller, its AI System's autonomy. Runs alongside the
    // resource-group card-binding check below (either plane can block); fails
    // closed on a lookup error. actor/subject are auto-stamped on the audit
    // row by build_proxy_log.
    // Whether a person already approved this call to this card: the agent's
    // grant asked for the approval and its token was redeemed below. The
    // card's and the group's A2A policy ask the same question, so they do
    // not ask it twice (`a2a_governance::Call::approved`).
    let approvals = agent_approval::RequestApprovals::from_headers(&headers);
    let approved = match a2a_governance::authorize_actor(
        &state,
        a2a_governance::ActorCall {
            tenant_id: &tenant_id,
            auth_ctx: &auth_ctx,
            card_id: &card.id,
            // A caller that is not an agent is judged by its group's AI
            // System: delegating work to another agent is a side-effecting
            // action, so a suspended or read-only system stops it however
            // the caller authenticated.
            group_id: auth_ctx
                .api_key_group_id
                .as_deref()
                .or(auth_ctx.resolved_group_id.as_deref()),
            run: Some(run_scope.clone()),
            approvals: &approvals,
            // The call leaves for a remote agent: no later gate here.
            downstream: None,
        },
    )
    .await
    {
        a2a_governance::ActorOutcome::Proceed { approved, shadow } => {
            // dry_run: record the would-block (shadow) and proceed.
            if let Some(o) = shadow {
                let duration_ms = start.elapsed().as_secs_f64() * 1000.0;
                let mut log = proxy_logger::build_proxy_log(
                    constants::proxy::A2A,
                    &auth_ctx,
                    "POST",
                    "/v1/proxy/a2a/outbound",
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
            // Recorded as what it is, as on the inbound and v1.0 routes:
            // the agent's grants refused the call, with the decision and
            // the grant that made it.
            return log_actor_denial(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                &card.id,
                &my_ids,
                ActorDenial::Grants(&o),
            );
        }
        a2a_governance::ActorOutcome::Held {
            status,
            envelope,
            decision,
        } => {
            // The agent's grant holds the delegation for a person: the
            // 428/202 envelope, and the row that records the hold.
            let duration_ms = start.elapsed().as_secs_f64() * 1000.0;
            let mut log = proxy_logger::build_proxy_log(
                constants::proxy::A2A,
                &auth_ctx,
                "POST",
                "/v1/proxy/a2a/outbound",
                None,
                Some(card.id.as_str()),
                status.as_u16(),
                duration_ms,
                false,
                Some("approval_required"),
                &correlation_id,
            );
            log.proxy_subtype =
                Some(crate::constants::proxy_subtype::APPROVAL_REQUIRED.to_string());
            log.mcp_capability_name = Some(card.id.clone());
            log.resource_group_id = auth_ctx.resolved_group_id.clone();
            proxy_logger::apply_agent_authz_decision(&mut log, &decision, false);
            state
                .background_writer
                .write_proxy_log(state.db.clone(), log);
            return (status, Json(envelope)).into_response();
        }
        a2a_governance::ActorOutcome::TokenRefused(answer) => {
            return log_and_fail(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                None,
                StatusCode::FORBIDDEN,
                &answer,
            )
            .await;
        }
        a2a_governance::ActorOutcome::HoldFailed { reason, .. } => {
            tracing::error!(error = %reason, "a2a agent approval enqueue failed");
            return log_and_fail(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                None,
                StatusCode::INTERNAL_SERVER_ERROR,
                "Failed to create approval request",
            )
            .await;
        }
        a2a_governance::ActorOutcome::Unavailable => {
            return log_and_fail(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                None,
                StatusCode::FORBIDDEN,
                "agent_authz_unavailable",
            )
            .await;
        }
        // Nothing is behind the outbound route's gates.
        a2a_governance::ActorOutcome::RefusedLater(refusal) => {
            return log_and_fail_scoped(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                resolved_group_id.as_deref(),
                refusal.status,
                &refusal.message,
            )
            .await;
        }
        a2a_governance::ActorOutcome::AutonomyDenied { reason, resolution } => {
            tracing::warn!(
                tenant_id = %tenant_id,
                card = %card.id,
                level = %resolution.level.as_str(),
                provenance = %resolution.provenance.as_str(),
                "autonomy block on a non-agent caller — refusing 403",
            );
            return log_actor_denial(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                &card.id,
                &my_ids,
                ActorDenial::Autonomy(&reason, &resolution),
            );
        }
    };

    // 3. Pick the outbound binding. A2A v1.0 cards advertise a
    //    `supported_interfaces[]` array; we prefer the strictest
    //    (JSONRPC 1.0 > HTTP+JSON 1.0 > JSONRPC 0.3 > HTTP+JSON 0.3)
    //    via `pick_interface`. A card that only carries a top-level
    //    `url` field (where a 0.3 card puts it) is addressed there, with
    //    the request in the `{capability, message}` shape.
    let (remote_url, outbound_dialect) = match pick_outbound_target(&card.card_json) {
        Some((url, dialect)) => (url, dialect),
        None => {
            return log_and_fail(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                None,
                StatusCode::BAD_REQUEST,
                "Agent card has no usable URL (missing `supported_interfaces[]` and the A2A 0.3 `url` field)",
            )
            .await;
        }
    };
    // Tenant-configured destination: an IP-literal URL is judged here
    // (hyper never resolves those); hostnames are judged by the policy
    // resolver on the shared client. See services::outbound_policy.
    if let Err(denied) = crate::services::outbound_policy::check_url(&remote_url) {
        tracing::warn!(card_id = %card.id, %denied, "A2A remote refused by outbound policy");
        return log_and_fail(
            &state,
            &auth_ctx,
            &correlation_id,
            start,
            Some(&card.id),
            None,
            StatusCode::FORBIDDEN,
            "Remote agent destination is refused by the outbound policy",
        )
        .await;
    }

    // 4. Load the capability row (source of truth for data_classification
    //    + per-capability timeout overrides). The card_json also carries
    //    capabilities but we trust the denormalised row because
    //    sync_capabilities() rebuilds it on every card upsert.
    let capability = match load_capability(&state, &tenant_id, &card.id, &request.capability).await
    {
        Ok(Some(c)) => c,
        Ok(None) => {
            return log_and_fail(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                None,
                StatusCode::NOT_FOUND,
                "Capability not found on this agent card",
            )
            .await;
        }
        Err(e) => {
            tracing::error!(error = %e, "a2a.outbound: capability lookup failed");
            return log_and_fail(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                None,
                StatusCode::INTERNAL_SERVER_ERROR,
                "Capability lookup failed",
            )
            .await;
        }
    };

    if !capability.enabled {
        return log_and_fail(
            &state,
            &auth_ctx,
            &correlation_id,
            start,
            Some(&card.id),
            None,
            StatusCode::FORBIDDEN,
            "Capability is disabled",
        )
        .await;
    }

    // 4b. Resolve caller's resource group + per-group A2A policy.
    // The cache returns:
    //   * effective_agent_card_ids — cards the group (direct + inherited)
    //     is allowed to invoke. Used here for access-control.
    //   * effective_a2a_limits — the chain's A2A limits, merged
    //     restrictively; a2a_governance_levels — each level's own A2A
    //     policy document, judged at step 5.
    // A caller with no resolvable group has no chain: the group-scoped
    // checks below are skipped and the tenant's root policy judges it
    // (`a2a_governance::policy_for`).
    // (`target_group` / `group_data` / `resolved_group_id` are resolved
    // above, before the accepted-caller gate, which needs the caller's group.)
    if let Some(ref gd) = group_data {
        if !gd.effective_agent_card_ids.is_empty()
            && !gd.effective_agent_card_ids.contains(&card.id)
        {
            return log_and_fail_scoped(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                resolved_group_id.as_deref(),
                StatusCode::FORBIDDEN,
                "Agent card is not bound to the caller's resource group",
            )
            .await;
        }
    }

    // 5. The A2A policy of the caller's group chain (the tenant's roots for
    //    a caller with no group) and of the card: the one evaluation every
    //    A2A route uses (`services::a2a_governance`). A refusal ends the
    //    call; `approval_required` holds it for a person.
    let policy = match a2a_governance::policy_for(
        &state.db,
        &tenant_id,
        group_data.as_deref(),
        card.tenant_governance.as_ref(),
    )
    .await
    {
        Ok(policy) => policy,
        Err(e) => {
            tracing::error!(error = %e, tenant_id = %tenant_id, "A2A outbound: policy load failed; refusing (fail closed)");
            return log_and_refuse_as(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                resolved_group_id.as_deref(),
                &my_ids,
                StatusCode::FORBIDDEN,
                constants::proxy_subtype::ENGINE_FAILURE,
                a2a_governance::UNAVAILABLE_MESSAGE,
            );
        }
    };
    match a2a_governance::enforce(
        &state,
        &policy,
        a2a_governance::Call {
            tenant_id: &tenant_id,
            auth_ctx: &auth_ctx,
            card_id: &card.id,
            subject: a2a_governance::Subject {
                direction: a2a_governance::Direction::Outbound {
                    remote_url: &remote_url,
                },
                classifications: capability.data_classification.iter().collect(),
                work: true,
            },
            run: Some(run_scope.clone()),
            approvals: &approvals,
            downstream: None,
            approved,
        },
    )
    .await
    {
        a2a_governance::Outcome::Proceed => {}
        a2a_governance::Outcome::Refused(reason) => {
            return log_and_fail_scoped(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                resolved_group_id.as_deref(),
                StatusCode::FORBIDDEN,
                &reason,
            )
            .await;
        }
        a2a_governance::Outcome::Held(status, envelope) => {
            let mut log = proxy_logger::build_proxy_log(
                constants::proxy::A2A,
                &auth_ctx,
                "POST",
                "/v1/proxy/a2a/outbound",
                None,
                Some(card.id.as_str()),
                status.as_u16(),
                start.elapsed().as_secs_f64() * 1000.0,
                false,
                Some("approval_required"),
                &correlation_id,
            );
            log.proxy_subtype = Some(constants::proxy_subtype::APPROVAL_REQUIRED.to_string());
            log.mcp_capability_name = Some(card.id.clone());
            log.resource_group_id = resolved_group_id
                .clone()
                .or_else(|| auth_ctx.resolved_group_id.clone());
            state
                .background_writer
                .write_proxy_log(state.db.clone(), log);
            return (status, Json(envelope)).into_response();
        }
        // Nothing is behind the outbound route's gates.
        a2a_governance::Outcome::RefusedLater(refusal) => {
            return log_and_fail_scoped(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                resolved_group_id.as_deref(),
                refusal.status,
                &refusal.message,
            )
            .await;
        }
        a2a_governance::Outcome::TokenRefused(reason) => {
            return log_and_refuse_as(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                resolved_group_id.as_deref(),
                &my_ids,
                StatusCode::FORBIDDEN,
                constants::proxy_subtype::APPROVAL_TOKEN_INVALID,
                &format!("Approval token invalid: {reason}"),
            );
        }
        a2a_governance::Outcome::HoldFailed(e) => {
            tracing::error!(error = %e, "a2a policy approval enqueue failed");
            return log_and_fail_scoped(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                resolved_group_id.as_deref(),
                StatusCode::INTERNAL_SERVER_ERROR,
                "Failed to create approval request",
            )
            .await;
        }
    }

    // 6a. A2A rate limits: the resource group's (the optional
    //     `a2a_global_limits.frequency` and `invocations` blocks, merged
    //     across the ancestor chain by the group cache), the card's
    //     TENANT-WIDE limits (`agent_cards.tenant_limits`, one aggregate
    //     across all groups, off the cached card) and the capability's own
    //     hourly rate. Admitted TOGETHER on the shared counters: a call one
    //     scope refuses spends none of the others' allowance. Returns 429.
    //     Admitted under a hold: a call a later gate refuses (residency, the
    //     semantic policy, the message guardrail) gives back what was
    //     admitted here (the guard's Drop); a dispatched call keeps it.
    let a2a_hold = {
        let mut a2a_hold = crate::services::limits::PendingHold::of(
            crate::services::limits::HoldId::new(&tenant_id, "a2a-rate"),
        );
        let block = crate::services::usage::admit_a2a_limits(
            &state.usage_state,
            crate::services::usage::A2aLimits {
                group: match (group_data.as_ref(), resolved_group_id.as_deref()) {
                    (Some(gd), Some(rg_id)) => Some((rg_id, gd.effective_a2a_limits.as_ref())),
                    _ => None,
                },
                card: Some((
                    tenant_id.as_str(),
                    card.id.as_str(),
                    card.tenant_limits.as_ref(),
                )),
                capability: Some((
                    capability.id.as_str(),
                    capability.capability_name.as_str(),
                    capability.rate_limit_per_hour,
                )),
            },
            Some(&mut a2a_hold),
        )
        .await;
        let a2a_hold = a2a_hold.held().map(|h| state.usage_state.adopt_hold(h));
        if let Some(block) = block {
            tracing::warn!(
                tenant_id = %tenant_id,
                card_id = %card.id,
                scope = %block.scope,
                limit_type = %block.limit_type,
                retry_after_secs = block.retry_after_secs,
                "A2A outbound rate limit exceeded — 429 returned",
            );
            return log_and_fail_scoped(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                resolved_group_id.as_deref(),
                StatusCode::TOO_MANY_REQUESTS,
                &block.message,
            )
            .await;
        }
        a2a_hold
    };

    // 6b. Data residency. The outbound call
    //     leaves our tenant for the remote agent at `remote_url`.
    //     Agent Cards carry no dedicated `region` column, so we read a
    //     best-effort `region` string off the card JSON (operators can
    //     stamp it when registering a partner whose hosting region is
    //     known). When absent the region is treated as unspecified —
    //     residency::check then applies the tenant's block_global /
    //     specific-region-required rules just like an MCP server with a
    //     NULL region. No profile = not evaluated; the request proceeds.
    let target_region = card
        .card_json
        .get("region")
        .and_then(|v| v.as_str())
        .map(str::to_owned);
    let residency_profile =
        crate::services::residency::load_tenant_profile(&state.db, &tenant_id).await;
    if let Some(crate::services::residency::ResidencyDecision::Blocked { reason }) =
        crate::services::residency::check(residency_profile.as_ref(), target_region.as_deref())
    {
        tracing::warn!(
            tenant_id = %tenant_id,
            card_id = %card.id,
            target_region = ?target_region,
            %reason,
            "A2A outbound residency block — refusing 403",
        );
        return log_residency_violation(
            &state,
            &auth_ctx,
            &correlation_id,
            start,
            Some(&card.id),
            Some(&remote_url),
            resolved_group_id.as_deref(),
            &reason,
            Some(&request.capability),
        );
    }

    // 7. Fork the delegation chain parsed at step 2b.
    let outgoing_chain = match inbound_chain {
        Ok(parent) => match parent.fork_child(current_step_task_id) {
            Ok(c) => c,
            Err(DelegationError::DepthOverflow { depth }) => {
                let msg = format!(
                    "Delegation chain exceeds absolute depth cap ({} > {})",
                    depth,
                    crate::services::a2a_delegation::ABSOLUTE_MAX_DEPTH
                );
                return log_and_fail(
                    &state,
                    &auth_ctx,
                    &correlation_id,
                    start,
                    Some(&card.id),
                    None,
                    StatusCode::FORBIDDEN,
                    &msg,
                )
                .await;
            }
            Err(e) => {
                tracing::error!(error = %e, "a2a.outbound: fork_child unexpectedly failed");
                return log_and_fail(
                    &state,
                    &auth_ctx,
                    &correlation_id,
                    start,
                    Some(&card.id),
                    None,
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "Delegation chain construction failed",
                )
                .await;
            }
        },
        Err(DelegationError::MalformedHeader(_)) => {
            // No inbound chain (or partial header set — treated the same).
            //
            // If the caller asserted a run id we honoured above, the chain we
            // sign has to root there as well. Rooting it at this step instead
            // would put the *delegation* inside the caller's run while
            // everything the peer then does — its model calls, its own tool
            // calls — lands in a different one, which is the more misleading
            // of the two failure modes: the run looks complete and simply
            // omits the work that was delegated.
            //
            // The peer still gets a genuinely signed chain; only its root
            // differs. Trust is unaffected — the assertion decided which run
            // this belongs to, the HMAC decides whether the chain is real, and
            // the ledger keeps the two apart (`client_asserted` on the steps
            // that asserted, `verified` on the steps that carried the chain).
            if my_root != current_step_task_id {
                match DelegationChain::root(my_root).fork_child(current_step_task_id) {
                    Ok(c) => c,
                    // Depth 1 cannot overflow the cap; fall back rather than
                    // fail a call over a bookkeeping id.
                    Err(_) => DelegationChain::root(current_step_task_id),
                }
            } else {
                // Mint a new root. The root and current-step task IDs are the
                // same ULID at root, which is what `DelegationChain::root`
                // enforces — but we also advance depth to 0 explicitly.
                DelegationChain::root(current_step_task_id)
            }
        }
        Err(e) => {
            // MissingSignature, SignatureMismatch, DepthOverflow —
            // these are all "caller claimed a chain but we can't trust
            // it". Refuse rather than silently mint a new root.
            let msg = format!("Inbound delegation chain rejected: {}", e);
            return log_and_fail(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                None,
                StatusCode::FORBIDDEN,
                &msg,
            )
            .await;
        }
    };

    // Carry the agent principal on the outgoing chain. `fork_child` already
    // propagates an inbound principal unchanged; only a fresh root (or a
    // principal-less inbound chain) needs this step's actor/subject stamped, and
    // only when the caller is itself an agent. Non-agent A2A traffic stays a
    // legacy (v1) chain with no principal headers.
    let outgoing_chain =
        if auth_ctx.actor_agent_id.is_some() && outgoing_chain.actor_agent_id.is_none() {
            let subject = auth_ctx
                .api_key_user_id
                .map(|u| format!("user:{}", u))
                .or_else(|| {
                    auth_ctx
                        .gateway_user_id
                        .clone()
                        .map(|u| format!("user:{}", u))
                });
            outgoing_chain.with_principal(auth_ctx.actor_agent_id.clone(), subject)
        } else {
            outgoing_chain
        };

    // Policy-level max_depth check (in addition to the hard cap).
    // Evaluated against the DOWNSTREAM step's depth because the rule
    // configures "don't let me fan out past this depth" — if my depth
    // is 5 and I'd push downstream to depth 6 with cap 5, block.
    //
    // Tenant-wide cap lives on the root resource group's
    // `a2a_global_limits.chain.max_delegation_depth` and inherits
    // down via the cache's restrictive-merged effective view. Child
    // groups can only narrow.
    //
    // A per-card override (`resource_group_agent_cards.limits` for the
    // active group) is restrictive-merged on top, so an operator can
    // tighten the delegation cap for one specific card. (Most-restrictive
    // wins — a per-card row can only narrow the group cap, never widen it.)
    let mut effective_a2a_limits: Option<serde_json::Value> = group_data
        .as_ref()
        .and_then(|gd| gd.effective_a2a_limits.clone());
    // Fold the card's TENANT-WIDE default limits (min-wins) — applies regardless
    // of group. Set via Resources → Agents → <card> → Limits. Read off the
    // cached card (≤300s stale on admin edits, like every resource cache).
    if let Some(ref tl) = card.tenant_limits {
        match effective_a2a_limits {
            Some(ref mut base) => crate::services::usage::restrictive_merge(
                base,
                tl,
                crate::services::usage::LimitFamily::A2a,
            ),
            None => effective_a2a_limits = Some(tl.clone()),
        }
    }
    if let Some(ref rg_id) = resolved_group_id {
        let per_card: Option<(Option<serde_json::Value>,)> = match sqlx::query_as(
            "SELECT limits FROM resource_group_agent_cards \
             WHERE resource_group_id = $1 AND agent_card_id = $2",
        )
        .bind(rg_id)
        .bind(&card.id)
        .fetch_optional(&state.db)
        .await
        {
            Ok(row) => row,
            // An unreadable per-card cap is not an absent one (fail closed):
            // it may be the tighter delegation depth.
            Err(e) => {
                tracing::error!(error = %e, card_id = %card.id, "per-card A2A limits unreadable; refusing");
                return log_and_fail_with_chain(
                    &state,
                    &auth_ctx,
                    &correlation_id,
                    start,
                    Some(&card.id),
                    resolved_group_id.as_deref(),
                    &my_ids,
                    StatusCode::SERVICE_UNAVAILABLE,
                    "Refused: this agent's delegation limits cannot be read right now",
                )
                .await;
            }
        };
        if let Some((Some(card_limits),)) = per_card {
            match effective_a2a_limits {
                Some(ref mut base) => crate::services::usage::restrictive_merge(
                    base,
                    &card_limits,
                    crate::services::usage::LimitFamily::A2a,
                ),
                None => effective_a2a_limits = Some(card_limits),
            }
        }
    }
    let effective_cap = group_max_delegation_depth(effective_a2a_limits.as_ref());
    if let Some(max) = effective_cap {
        if (outgoing_chain.depth as i32) > max {
            let msg = format!(
                "A2A max_delegation_depth {} exceeded (chain depth {})",
                max, outgoing_chain.depth
            );
            return log_and_fail_with_chain(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                resolved_group_id.as_deref(),
                &my_ids,
                StatusCode::FORBIDDEN,
                &msg,
            )
            .await;
        }
    }

    // 8. Issue the outbound call.
    //
    //    For v1.0 targets we:
    //      * POST to `{base}/message:send` (the v1.0 HTTP path suffix
    //        mandated by section 3.6.1 of the A2A specification for the `SendMessage` method).
    //      * Set `A2A-Version: 1.0` so the receiving gateway can route
    //        through its v1.0 dispatcher without path sniffing.
    //      * Advertise the delegation extension via `A2A-Extensions`,
    //        matching the extension URI the card publishes.
    //      * Wrap the caller's `message` in a `SendMessageRequest`
    //        envelope. If the caller already supplied the full v1.0
    //        envelope (with `message`/`configuration` top-level
    //        fields) we pass it through verbatim.
    //
    //    For 0.3 targets the body is `{capability, message}`: the form
    //    this gateway's own inbound A2A route takes.
    let timeout_secs = capability.timeout_secs.filter(|t| *t > 0).unwrap_or(30) as u64;
    let final_url = compose_outbound_url(&remote_url, outbound_dialect);
    let mut req_builder = state
        .http_client
        .post(&final_url)
        .timeout(std::time::Duration::from_secs(timeout_secs))
        .header(
            constants::headers::X_CORRELATION_ID,
            correlation_id.as_str(),
        )
        .header("content-type", "application/json");
    if outbound_dialect == OutboundDialect::V1 {
        req_builder = req_builder
            .header(
                crate::services::a2a_v1::version::HEADER_A2A_VERSION,
                crate::services::a2a_v1::version::VERSION_V1_0,
            )
            .header(
                "a2a-extensions",
                crate::services::a2a_v1::BRUTOR_DELEGATION_EXTENSION_URI,
            );
    }

    for (name, value) in outgoing_chain.to_header_pairs(&tenant_key) {
        if let (Ok(n), Ok(v)) = (
            ReqHeaderName::from_bytes(name.as_bytes()),
            ReqHeaderValue::from_str(&value),
        ) {
            req_builder = req_builder.header(n, v);
        }
    }

    let mut outbound_body =
        build_outbound_body(outbound_dialect, &request.capability, &request.message);

    // Guardrails on outbound A2A. Scan the payload before
    // we sign and send, so tenant data leaving the platform is
    // checked against the same policies the LLM and MCP surfaces use.
    // The Agent Card's bound user/group resolves the same selectors,
    // so a single tenant-wide policy covers all four surfaces. On
    // block we return a v1.0 PolicyBlocked structured error to the
    // *local* caller (the Brutor-hosted agent that initiated the
    // delegation) — the peer agent never sees the request. On
    // Redacted we splice the rewritten text back into the outbound
    // JSON before signing + sending, so the peer agent receives the
    // redacted version, not the original PII.
    let outbound_text = a2a_guardrail::extract_outbound_text(&outbound_body);

    // Semantic Governance Policy on the a2a_outbound surface — the marquee
    // "agents may not send PII to external peers" case. Shadow logs a
    // would-block and proceeds; enforce+block returns a 403 to the local
    // caller (the peer never sees the request).
    {
        use crate::services::semantic_policy::{self, PolicyContext, PolicyDecision};
        if let Ok(policies) = state
            .semantic_policy_cache
            .get_policies(&tenant_id, resolved_group_id.as_deref(), &state.db)
            .await
        {
            if !policies.is_empty() {
                let ctx = PolicyContext {
                    surface: "a2a_outbound".to_string(),
                    actor_agent_id: auth_ctx.actor_agent_id.clone(),
                    action_type: Some("a2a_call".to_string()),
                    target: Some(card.id.clone()),
                    subject: auth_ctx.gateway_user_id.clone(),
                };
                // The judge's call is a step of this call's run:
                // a child of this step. The chain forked for
                // the peer above already is one (its parent is this step),
                // except when this step starts the run: the peer is then
                // sent the root itself, and the judge's call is forked from
                // it.
                let judge_chain = if outgoing_chain.depth == 0 {
                    outgoing_chain.fork_child(current_step_task_id).ok()
                } else {
                    Some(outgoing_chain.clone())
                };
                let auth_headers = crate::routes::llm_helpers::judge_self_call_headers_for_child(
                    &headers,
                    judge_chain.as_ref(),
                    &tenant_key,
                );
                let outcome = semantic_policy::evaluate(
                    &state.db,
                    &tenant_id,
                    &policies,
                    &outbound_text,
                    &ctx,
                    state.semantic_policy_judge.as_ref(),
                    &auth_headers,
                    state.config.judge_model.as_deref(),
                )
                .await;
                let write_sgp = |policy_id: &str, blocked: bool, reason: &str| {
                    let duration_ms = start.elapsed().as_secs_f64() * 1000.0;
                    let mut log = proxy_logger::build_proxy_log(
                        constants::proxy::A2A,
                        &auth_ctx,
                        "POST",
                        "/v1/proxy/a2a/outbound",
                        None,
                        Some(card.id.as_str()),
                        if blocked { 403 } else { 200 },
                        duration_ms,
                        !blocked,
                        Some(reason),
                        &correlation_id,
                    );
                    log.root_task_id = my_ids.root_task_id.clone();
                    log.current_task_id = my_ids.current_task_id.clone();
                    log.parent_task_id = my_ids.parent_task_id.clone();
                    log.delegation_depth = my_ids.depth;
                    log.resource_group_id = resolved_group_id.clone();
                    proxy_logger::apply_semantic_policy_decision(&mut log, policy_id, blocked);
                    state
                        .background_writer
                        .write_proxy_log(state.db.clone(), log);
                };
                for (pid, reason) in &outcome.shadow_hits {
                    write_sgp(pid, false, reason);
                }
                if outcome.decision == PolicyDecision::Block {
                    let pid = outcome.matched_policy_id.clone().unwrap_or_default();
                    let reason = outcome.reason.clone().unwrap_or_default();
                    write_sgp(&pid, true, &reason);
                    return error_response(
                        StatusCode::FORBIDDEN,
                        &format!("semantic_policy_blocked: {reason}"),
                    );
                }
            }
        }
    }

    match a2a_guardrail::check(
        &state,
        &auth_ctx,
        &tenant_id,
        &outbound_text,
        "a2a_outbound",
    )
    .await
    {
        Err(err) => {
            let req_body = serde_json::to_string(&outbound_body).ok();
            return log_guardrail_block(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                Some(final_url.as_str()),
                resolved_group_id.as_deref(),
                &my_ids,
                PROXY_SUBTYPE_OUTBOUND,
                &err,
                None,
                req_body.as_deref(),
                None,
            );
        }
        Ok(a2a_guardrail::CheckResult::Redacted(new_text)) => {
            a2a_guardrail::apply_outbound_redactions(&mut outbound_body, &new_text);
        }
        Ok(a2a_guardrail::CheckResult::Pass) => {}
    }

    // Every gate has passed; the message leaves for the
    // remote agent now. For a two-phase system the `planned` record is
    // sealed before the outcome row (off the request path).
    crate::services::evidence::planned::dispatching(
        &state.db,
        &tenant_id,
        resolved_group_id.as_deref(),
        constants::proxy::A2A,
        "a2a:message",
        my_ids.root_task_id.clone(),
    );
    // The hop is dispatched: the run's action and the rate allowance stay
    // counted.
    step.commit().await;
    if let Some(hold) = a2a_hold {
        hold.commit().await;
    }

    let upstream = match req_builder.json(&outbound_body).send().await {
        Ok(r) => r,
        Err(e) => {
            // Downstream failure — WARN per error-taxonomy memory, not ERROR.
            tracing::warn!(
                error = %e,
                remote_url = %remote_url,
                card_id = %card.id,
                "a2a.outbound: upstream agent request failed"
            );
            return log_and_fail_with_chain(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                resolved_group_id.as_deref(),
                &my_ids,
                StatusCode::BAD_GATEWAY,
                &format!("Remote agent request failed: {}", e),
            )
            .await;
        }
    };

    let status = upstream.status();
    // The delegate's outcome, if it sent it as a header
    // (sync). Read before the body consumes the response.
    let header_outcome = upstream
        .headers()
        .get(crate::services::run_outcome::OUTCOME_ID_HEADER)
        .and_then(crate::services::run_outcome::stored_from_header);
    let body_bytes = match upstream.bytes().await {
        Ok(b) => b,
        Err(e) => {
            tracing::warn!(error = %e, "a2a.outbound: upstream body read failed");
            return log_and_fail_with_chain(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                resolved_group_id.as_deref(),
                &my_ids,
                StatusCode::BAD_GATEWAY,
                "Remote agent response body read failed",
            )
            .await;
        }
    };

    // OUTPUT guardrails on the peer's RESPONSE coming back into
    // our tenant. Surface is `a2a_inbound` (data ENTERING the tenant).
    // Only JSON bodies are scanned — non-JSON (binary artifacts) is
    // relayed unchanged, matching the text-oriented guardrail contract.
    // On block we refuse the response to the local caller (the peer's
    // output never reaches them). On redact we splice the rewritten
    // text back into the relayed JSON before forwarding + logging.
    let mut relay_bytes = body_bytes;
    if let Ok(mut resp_json) = serde_json::from_slice::<Value>(&relay_bytes) {
        let resp_text = a2a_guardrail::extract_outbound_text(&resp_json);
        match a2a_guardrail::check_output(&state, &auth_ctx, &tenant_id, &resp_text, "a2a_inbound")
            .await
        {
            Err(err) => {
                tracing::warn!(
                    tenant_id = %tenant_id,
                    card_id = %card.id,
                    "a2a.outbound: peer response blocked by output guardrail",
                );
                let req_body = serde_json::to_string(&outbound_body).ok();
                return log_guardrail_block(
                    &state,
                    &auth_ctx,
                    &correlation_id,
                    start,
                    Some(&card.id),
                    Some(final_url.as_str()),
                    resolved_group_id.as_deref(),
                    &my_ids,
                    PROXY_SUBTYPE_OUTBOUND,
                    &err,
                    None,
                    req_body.as_deref(),
                    Some(resp_text.as_str()),
                );
            }
            Ok(a2a_guardrail::CheckResult::Redacted(new_text)) => {
                if a2a_guardrail::apply_outbound_redactions(&mut resp_json, &new_text) {
                    if let Ok(b) = serde_json::to_vec(&resp_json) {
                        relay_bytes = Bytes::from(b);
                    }
                }
            }
            Ok(a2a_guardrail::CheckResult::Pass) => {}
        }
    }

    // Judge the delegate's outcome against the DELEGATE's
    // active contract and hand the checked pair to the caller in both
    // carriers (headers below, final-Task metadata here), so the caller
    // branches on the gateway's verdict, never on the delegate's word.
    let delegate_outcome = judge_delegate_outcome(
        &state,
        &tenant_id,
        &card.id,
        header_outcome
            .or_else(|| crate::services::a2a_task_state::outcome_from_reply(&relay_bytes)),
    )
    .await;
    if let Some(b) = crate::services::a2a_task_state::annotate_reply(
        &relay_bytes,
        delegate_outcome.outcome_id.as_deref(),
        delegate_outcome.conformance,
    ) {
        relay_bytes = Bytes::from(b);
    }

    let duration_ms = start.elapsed().as_secs_f64() * 1000.0;
    let success = status.is_success();

    // Stringify the bodies for the proxy_logs row. The outbound body
    // is a serde_json::Value built above; serialize once and reuse.
    // The response body is raw bytes — try as UTF-8, fall back to a
    // marker so the row still records *something* useful for non-text
    // payloads (rare for A2A, but possible for embedded artifacts).
    let request_body_str = serde_json::to_string(&outbound_body).ok();
    let response_body_str = std::str::from_utf8(&relay_bytes)
        .ok()
        .map(str::to_owned)
        .or_else(|| Some(format!("<{} bytes of non-utf8 body>", relay_bytes.len())));

    let mut log = build_outbound_log(
        &auth_ctx,
        &correlation_id,
        Some(&card.id),
        Some(&remote_url),
        resolved_group_id.as_deref(),
        status.as_u16(),
        duration_ms,
        success,
        if success {
            None
        } else {
            Some("remote agent returned non-2xx")
        },
        &my_ids,
        Some(&request.capability),
        request_body_str.as_deref(),
        response_body_str.as_deref(),
    );
    // The task state the caller is about to receive,
    // decided once from the relayed bytes (JSON-RPC, v1/REST or a stream's
    // final status). The single source for run segments and boundary
    // metrics; the stored body may later be pruned by retention.
    log.a2a_task_state = crate::services::a2a_task_state::from_reply(&relay_bytes);
    // This row carries the DELEGATE's reported outcome (the run
    // ledger judges the delegate's segment on it; it is never the caller's
    // run outcome).
    log.outcome_id = delegate_outcome.outcome_id.clone();
    state
        .background_writer
        .write_proxy_log(state.db.clone(), log);

    // Relay response (post-output-guardrail). Axum status code has the
    // same u16 range.
    let axum_status = StatusCode::from_u16(status.as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
    let mut response = (axum_status, relay_body(relay_bytes)).into_response();
    set_outcome_headers(response.headers_mut(), &delegate_outcome);
    response
}

/// The delegate's reported outcome, judged against the
/// delegate's ACTIVE contract. The delegate is the AI System a declared
/// `shared_service` edge names for the card (the resolver the accepted-caller
/// gate uses). A card no edge names is no AI System: it has no catalogue, so
/// whatever it reports is `undeclared`.
///
/// Fail-closed toward the caller: if the delegate or its contract cannot be
/// read, the outcome is judged with NO catalogue (`undeclared`, or
/// `unreported` when nothing was reported) — never `declared` on a guess. The
/// request is never failed over the label; the run ledger re-judges the
/// segment at finalize.
pub(crate) async fn judge_delegate_outcome(
    state: &SharedState,
    tenant_id: &str,
    card_id: &str,
    reported: Option<String>,
) -> crate::services::run_outcome::Judgement {
    use crate::services::run_outcome::{active_contracts, judge};
    let system = match state
        .group_cache
        .card_service_system(tenant_id, card_id)
        .await
    {
        Ok(s) => s,
        Err(e) => {
            tracing::warn!(error = %e, card_id, "a2a.outbound: delegate unresolvable; outcome judged without a contract");
            metrics::counter!("brutor_a2a_outcome_judge_unavailable_total").increment(1);
            return judge(reported.as_deref(), None);
        }
    };
    let Some(system) = system else {
        return judge(reported.as_deref(), None);
    };
    match active_contracts(&state.db, tenant_id, std::slice::from_ref(&system)).await {
        Ok(m) => judge(
            reported.as_deref(),
            m.get(&system).and_then(|c| c.catalogue.as_ref()),
        ),
        Err(e) => {
            tracing::warn!(error = %e, card_id, "a2a.outbound: delegate contract unreadable; outcome judged without a contract");
            metrics::counter!("brutor_a2a_outcome_judge_unavailable_total").increment(1);
            judge(reported.as_deref(), None)
        }
    }
}

/// The checked pair on the response to the caller. An id that cannot be a
/// header value (it is malformed anyway, so `undeclared`) is left out; the
/// conformance header is always present.
pub(crate) fn set_outcome_headers(
    headers: &mut HeaderMap,
    judged: &crate::services::run_outcome::Judgement,
) {
    use crate::services::run_outcome::{OUTCOME_CONFORMANCE_HEADER, OUTCOME_ID_HEADER};
    headers.remove(OUTCOME_ID_HEADER);
    if let Some(v) = judged
        .outcome_id
        .as_deref()
        .and_then(|o| axum::http::HeaderValue::from_str(o).ok())
    {
        headers.insert(OUTCOME_ID_HEADER, v);
    }
    headers.insert(
        OUTCOME_CONFORMANCE_HEADER,
        axum::http::HeaderValue::from_static(judged.conformance),
    );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// What an outbound step sends its target.
///
/// Picked from the target card's `supported_interfaces[]` array per
/// the v1.0 priority order (see `a2a_v1::version::pick_interface`). A card
/// with no such array and a top-level `url` (where an A2A 0.3 card puts the
/// agent's endpoint) is addressed at that URL, as a 0.3 interface is.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum OutboundDialect {
    /// Target advertises an `HTTP+JSON` or `JSONRPC` interface at
    /// `protocolVersion = "1.0"`. Emit v1.0 envelope + headers.
    V1,
    /// The target advertises a `0.3` interface in `supported_interfaces[]`
    /// (or one whose version is not stated as 1.0), or is a card with only
    /// a top-level `url`. It is sent `{capability, message}` at that URL:
    /// the body this gateway's own inbound route takes
    /// ([`build_outbound_body`]).
    V03,
}

/// Pick the outbound URL + dialect from a card's JSON form.
///
/// Preference order:
///   1. `supported_interfaces[]` → best interface via
///      [`a2a_v1::version::pick_interface`].
///   2. Top-level `url` (an A2A 0.3 card) → 0.3.
///   3. Nothing usable → `None` (the handler surfaces a 400).
pub(crate) fn pick_outbound_target(card_json: &Value) -> Option<(String, OutboundDialect)> {
    if let Some(arr) = card_json
        .get("supportedInterfaces")
        .and_then(|v| v.as_array())
    {
        let ifaces: Vec<crate::services::a2a_v1::AgentInterface> = arr
            .iter()
            .filter_map(|v| serde_json::from_value(v.clone()).ok())
            .collect();
        if let Some(best) = crate::services::a2a_v1::version::pick_interface(&ifaces) {
            let dialect = match crate::services::a2a_v1::version::ProtocolVersion::from_str(
                &best.protocol_version,
            ) {
                crate::services::a2a_v1::version::ProtocolVersion::V1_0 => OutboundDialect::V1,
                crate::services::a2a_v1::version::ProtocolVersion::V0_3
                | crate::services::a2a_v1::version::ProtocolVersion::Unknown => {
                    OutboundDialect::V03
                }
            };
            return Some((best.url.clone(), dialect));
        }
    }
    if let Some(url) = card_json.get("url").and_then(|v| v.as_str()) {
        return Some((url.to_string(), OutboundDialect::V03));
    }
    None
}

/// Tack the v1.0 `/message:send` suffix onto the picked URL for v1
/// targets. For 0.3 the URL is already the endpoint.
pub(crate) fn compose_outbound_url(base: &str, dialect: OutboundDialect) -> String {
    if dialect != OutboundDialect::V1 {
        return base.to_string();
    }
    // Guard against double-suffixing — operators sometimes register
    // cards where `url` already embeds the method path.
    if base.ends_with("/message:send") || base.contains(":send") {
        return base.to_string();
    }
    let trimmed = base.trim_end_matches('/');
    format!("{trimmed}/message:send")
}

/// Build the outbound request body for the chosen dialect.
///
/// v1.0: `SendMessageRequest { message }` at the top level. The
/// caller may pre-supply that envelope (with `message` /
/// `configuration` already shaped correctly), in which case we pass
/// it through verbatim so advanced callers can set configuration
/// fields like `return_immediately` and `history_length`.
///
/// [`OutboundDialect::V03`]: `{capability, message}`.
pub(crate) fn build_outbound_body(
    dialect: OutboundDialect,
    capability: &str,
    message: &Value,
) -> Value {
    match dialect {
        OutboundDialect::V1 => {
            // If the caller supplied a `message` object at the top
            // level, treat the whole thing as a SendMessageRequest.
            if let Value::Object(obj) = message {
                if obj.contains_key("message") {
                    return Value::Object(obj.clone());
                }
            }
            json!({ "message": message })
        }
        OutboundDialect::V03 => {
            json!({
                "capability": capability,
                "message": message,
            })
        }
    }
}

fn error_response(status: StatusCode, message: &str) -> Response {
    crate::services::error_envelope::canonical_error_response(status, message)
}

fn relay_body(body: Bytes) -> Response {
    // Try to pass through as JSON; fall back to raw bytes if not JSON.
    match serde_json::from_slice::<Value>(&body) {
        Ok(v) => Json(v).into_response(),
        Err(_) => (
            [(axum::http::header::CONTENT_TYPE, "application/octet-stream")],
            body,
        )
            .into_response(),
    }
}

fn header_value(headers: &HeaderMap, name: &str) -> Option<String> {
    headers
        .get(name)
        .and_then(|v| v.to_str().ok())
        .map(str::to_owned)
}

/// The tenant's `a2a` switch (`tenant_features`), from the tenant status the
/// tenant layer cached. A tenant that does not exist has it off.
async fn check_a2a_enabled(state: &SharedState, tenant_id: &str) -> Result<bool, sqlx::Error> {
    Ok(crate::services::tenant_features::of(state, tenant_id)
        .await?
        .allows(crate::services::tenant_features::Feature::A2a))
}

/// Loads an agent card by id via the in-memory cache.
///
/// Uses `AgentCardCache` so the request hot path doesn't pay a
/// per-invocation Postgres round-trip for the card row. The cache
/// loads from DB on miss, populates both id-key and name-key entries
/// with the same `Arc`, and respects pub/sub invalidations from the
/// control plane (admin edits to a card are reflected without waiting
/// for the 5-minute TTL).
///
/// A database error is returned: the cache used to swallow it and
/// answer `None`, so an outage read as "no such agent card".
async fn load_agent_card(
    state: &SharedState,
    tenant_id: &str,
    card_id: &str,
) -> Result<Option<AgentCardRow>, sqlx::Error> {
    Ok(state
        .agent_card_cache
        .get_by_id(tenant_id, card_id)
        .await?
        .map(|cached| AgentCardRow {
            id: cached.id.clone(),
            tenant_id: cached.tenant_id.clone(),
            name: cached.name.clone(),
            card_json: cached.card_json.clone(),
            trust_tier: cached.trust_tier.clone(),
            enabled: cached.enabled,
            revoked_at: cached.revoked_at,
            tenant_limits: cached.tenant_limits.clone(),
            tenant_governance: cached.tenant_governance.clone(),
        }))
}

/// Looks up a capability on a cached agent card.
///
/// Capabilities are loaded together with the card on cache miss
/// (`AgentCardCache::load_capabilities`), so this is an in-memory
/// `BTreeMap` lookup. The route still expects to be told its
/// `tenant_id` because the lookup needs to materialise the cached
/// card first — `card_id` alone wouldn't be enough to walk back to
/// the cache key, since the cache is tenant-scoped.
async fn load_capability(
    state: &SharedState,
    tenant_id: &str,
    card_id: &str,
    capability_name: &str,
) -> Result<Option<AgentCardCapabilityRow>, sqlx::Error> {
    let card = match state.agent_card_cache.get_by_id(tenant_id, card_id).await? {
        Some(c) => c,
        None => return Ok(None),
    };
    Ok(card
        .capabilities
        .get(capability_name)
        .map(|cap| AgentCardCapabilityRow {
            id: cap.id.clone(),
            capability_name: cap.capability_name.clone(),
            enabled: cap.enabled,
            timeout_secs: cap.timeout_secs,
            rate_limit_per_hour: cap.rate_limit_per_hour,
            data_classification: cap.data_classification.clone(),
            handler_type: cap.handler_type.clone(),
            handler_ref: cap.handler_ref.clone(),
        }))
}

fn is_missing_table(e: &dyn sqlx::error::DatabaseError) -> bool {
    // Postgres SQLSTATE 42P01 = undefined_table. Used by the JWKS
    // loader below for the `tenant_a2a_keys` table, which may not
    // exist on older deployments mid-rollout.
    e.code().as_deref() == Some("42P01")
}

/// Pull `max_delegation_depth` from the effective group A2A limits tree
/// (`{chain: {max_delegation_depth}}`). Returns `None` when the group
/// has no rule — the caller then relies on the tenant-wide rule or
/// the absolute cap alone.
fn group_max_delegation_depth(limits: Option<&Value>) -> Option<i32> {
    limits
        .and_then(|v| v.get("chain"))
        .and_then(|c| c.get("max_delegation_depth"))
        .and_then(|n| n.as_i64())
        .map(|n| n as i32)
}

// ---------------------------------------------------------------------------
// Logging helpers
// ---------------------------------------------------------------------------

#[allow(clippy::too_many_arguments)]
/// Graph-tree identifiers for a single A2A step.
///
/// Recorded on every proxy_logs row so Mission Control can reconstruct
/// the delegation graph as an actual tree. `current` is the ULID the
/// gateway minted when it entered this step — the "node id". `parent`
/// is the current_task_id of the step that invoked us, or `None` at
/// the root. `root` is always set for A2A traffic.
#[derive(Debug, Clone, Default)]
struct StepLogIds {
    pub root_task_id: Option<String>,
    pub current_task_id: Option<String>,
    pub parent_task_id: Option<String>,
    pub depth: Option<i32>,
    /// Propagated agent principal lifted off the verified delegation chain, so
    /// the proxy_log shows who originated the chain even on a downstream step.
    pub actor_agent_id: Option<String>,
    /// Propagated subject, encoded `kind:id`.
    pub subject: Option<String>,
}

impl StepLogIds {
    fn none() -> Self {
        Self::default()
    }

    /// Attach the propagated principal (actor + `kind:id` subject) carried on a
    /// verified inbound delegation chain.
    fn with_principal(mut self, actor: Option<String>, subject: Option<String>) -> Self {
        self.actor_agent_id = actor;
        self.subject = subject;
        self
    }

    /// Build the IDs for a step that is SENDING an outbound call or
    /// ENTERING an inbound call. `current_step` is the ULID minted
    /// locally. `inbound_parent` is the `parent_task_id` lifted off an
    /// incoming delegation-chain header — the ULID of the step that
    /// called us. `root_override` lets the caller explicitly set the
    /// root when there is no inbound chain (at the root step,
    /// `root_task_id == current_step`).
    fn from_step(
        current_step: Ulid,
        inbound_parent: Option<Ulid>,
        root_override: Ulid,
        depth: u32,
    ) -> Self {
        Self {
            root_task_id: Some(root_override.to_string()),
            current_task_id: Some(current_step.to_string()),
            parent_task_id: inbound_parent.map(|p| p.to_string()),
            depth: Some(depth as i32),
            actor_agent_id: None,
            subject: None,
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn write_proxy_log(
    state: &SharedState,
    auth_ctx: &AuthContext,
    correlation_id: &str,
    card_id: Option<&str>,
    target_url: Option<&str>,
    resource_group_id: Option<&str>,
    status_code: u16,
    duration_ms: f64,
    success: bool,
    error: Option<&str>,
    ids: &StepLogIds,
    capability: Option<&str>,
    request_body: Option<&str>,
    response_body: Option<&str>,
) {
    let log = build_outbound_log(
        auth_ctx,
        correlation_id,
        card_id,
        target_url,
        resource_group_id,
        status_code,
        duration_ms,
        success,
        error,
        ids,
        capability,
        request_body,
        response_body,
    );
    state
        .background_writer
        .write_proxy_log(state.db.clone(), log);
}

#[allow(clippy::too_many_arguments)]
fn build_outbound_log(
    auth_ctx: &AuthContext,
    correlation_id: &str,
    card_id: Option<&str>,
    target_url: Option<&str>,
    resource_group_id: Option<&str>,
    status_code: u16,
    duration_ms: f64,
    success: bool,
    error: Option<&str>,
    ids: &StepLogIds,
    capability: Option<&str>,
    request_body: Option<&str>,
    response_body: Option<&str>,
) -> crate::models::db::ProxyLogInsert {
    let mut log = proxy_logger::build_proxy_log(
        constants::proxy::A2A,
        auth_ctx,
        "POST",
        "/v1/proxy/a2a/outbound",
        target_url,
        card_id,
        status_code,
        duration_ms,
        success,
        error,
        correlation_id,
    );
    log.proxy_subtype = Some(PROXY_SUBTYPE_OUTBOUND.to_string());
    log.root_task_id = ids.root_task_id.clone();
    log.delegation_depth = ids.depth;
    log.current_task_id = ids.current_task_id.clone();
    log.parent_task_id = ids.parent_task_id.clone();
    // A principal propagated on the verified chain takes precedence over the
    // local api-key-derived attribution, so the delegation graph shows who
    // ORIGINATED the chain (and on whose behalf) even on a downstream step.
    if ids.actor_agent_id.is_some() {
        log.actor_agent_id = ids.actor_agent_id.clone();
    }
    if let Some(subject) = ids.subject.as_deref() {
        match subject.split_once(':') {
            Some((kind, id)) => {
                log.subject_kind = Some(kind.to_string());
                log.subject_id = Some(id.to_string());
            }
            None => log.subject_id = Some(subject.to_string()),
        }
    }
    // Capability is the dotted name the caller asked us to invoke
    // (e.g. `hello`, `billing.refund`). Stored on
    // `proxy_logs.mcp_capability_name` — the column is named for its
    // historical first user, but A2A reuses it so the admin UI's
    // unified Capability column works for both proxy types without
    // a schema rename.
    log.mcp_capability_name = capability.map(str::to_owned);
    // Request + response bodies. `body_preview` strips null bytes so
    // PostgreSQL text columns accept the value; size is capped by the
    // DB column / scrubber downstream rather than truncated here.
    log.request_body_preview = request_body.map(proxy_logger::body_preview);
    log.response_body_preview = response_body.map(proxy_logger::body_preview);
    // Mark sizes so the UI's "size" cell shows the real payload, not
    // the metadata-only zero we'd otherwise leave.
    if let Some(rb) = request_body {
        log.request_size_bytes = Some(rb.len() as i32);
    }
    if let Some(rb) = response_body {
        log.response_size_bytes = Some(rb.len() as i32);
    }
    // The proxy_logs row picks up resource_group_id so Mission Control
    // and billing queries can aggregate A2A traffic per group. Falls
    // back to what the auth layer resolved (API-key attribution) when
    // we don't have a concrete group for this call.
    log.resource_group_id = resource_group_id
        .map(str::to_owned)
        .or_else(|| auth_ctx.resolved_group_id.clone());
    log
}

/// [`log_and_fail`] for a run-scoped refusal: the row carries the refusal's
/// subtype so the evidence writer seals the verdict it was (a
/// run-cap or abort refusal is `denied`, not a failed call), and the step's
/// own ids so the refusal lands on the run it stopped.
#[allow(clippy::too_many_arguments)]
fn log_and_refuse(
    state: &SharedState,
    auth_ctx: &AuthContext,
    correlation_id: &str,
    start: Instant,
    card_id: Option<&str>,
    resource_group_id: Option<&str>,
    ids: &StepLogIds,
    refusal: &crate::services::run_controls::RunRefusal,
) -> Response {
    log_and_refuse_as(
        state,
        auth_ctx,
        correlation_id,
        start,
        card_id,
        resource_group_id,
        ids,
        refusal.status(),
        refusal.subtype(),
        refusal.message(),
    )
}

/// An outbound refusal a gate decided before the call left: the row carries
/// the gate's subtype (sealed as the verdict it was) and this step's ids (so
/// it lands on the run it stopped, where the ledger reads it as an attempted
/// delegation). Nothing is sent to the remote agent.
#[allow(clippy::too_many_arguments)]
fn log_and_refuse_as(
    state: &SharedState,
    auth_ctx: &AuthContext,
    correlation_id: &str,
    start: Instant,
    card_id: Option<&str>,
    resource_group_id: Option<&str>,
    ids: &StepLogIds,
    status: StatusCode,
    subtype: &str,
    message: &str,
) -> Response {
    let mut log = build_outbound_log(
        auth_ctx,
        correlation_id,
        card_id,
        None,
        resource_group_id,
        status.as_u16(),
        start.elapsed().as_secs_f64() * 1000.0,
        false,
        Some(message),
        ids,
        None,
        None,
        None,
    );
    log.proxy_subtype = Some(subtype.to_string());
    state
        .background_writer
        .write_proxy_log(state.db.clone(), log);
    error_response(status, message)
}

/// Which part of the caller's own plane refused an outbound call.
enum ActorDenial<'a> {
    /// The agent's grants, with the decision.
    Grants(&'a crate::services::agent_authz::Outcome),
    /// The autonomy of the caller's AI System: the reason and the level.
    Autonomy(&'a str, &'a crate::services::autonomy::Resolution),
}

/// An outbound call the caller's own plane refused (an agent's grants, or
/// the autonomy of a caller's AI System): 403, and a row that says which
/// plane refused it and on what decision, on the step it stopped.
fn log_actor_denial(
    state: &SharedState,
    auth_ctx: &AuthContext,
    correlation_id: &str,
    start: Instant,
    card_id: &str,
    ids: &StepLogIds,
    denial: ActorDenial<'_>,
) -> Response {
    let message = match denial {
        ActorDenial::Grants(o) => format!("agent_not_authorized: {}", o.reason),
        ActorDenial::Autonomy(reason, _) => format!("autonomy_denied: {reason}"),
    };
    let message = message.as_str();
    let mut log = build_outbound_log(
        auth_ctx,
        correlation_id,
        Some(card_id),
        None,
        auth_ctx.resolved_group_id.as_deref(),
        StatusCode::FORBIDDEN.as_u16(),
        start.elapsed().as_secs_f64() * 1000.0,
        false,
        Some(message),
        ids,
        None,
        None,
        None,
    );
    match denial {
        ActorDenial::Grants(o) => proxy_logger::apply_agent_authz_decision(&mut log, o, true),
        ActorDenial::Autonomy(_, resolution) => {
            proxy_logger::apply_autonomy(&mut log, resolution, true)
        }
    }
    state
        .background_writer
        .write_proxy_log(state.db.clone(), log);
    error_response(StatusCode::FORBIDDEN, message)
}

async fn log_and_fail(
    state: &SharedState,
    auth_ctx: &AuthContext,
    correlation_id: &str,
    start: Instant,
    card_id: Option<&str>,
    target_url: Option<&str>,
    status: StatusCode,
    message: &str,
) -> Response {
    let duration_ms = start.elapsed().as_secs_f64() * 1000.0;
    write_proxy_log(
        state,
        auth_ctx,
        correlation_id,
        card_id,
        target_url,
        None,
        status.as_u16(),
        duration_ms,
        false,
        Some(message),
        &StepLogIds::none(),
        None,
        None,
        None,
    );
    error_response(status, message)
}

#[allow(clippy::too_many_arguments)]
async fn log_and_fail_scoped(
    state: &SharedState,
    auth_ctx: &AuthContext,
    correlation_id: &str,
    start: Instant,
    card_id: Option<&str>,
    resource_group_id: Option<&str>,
    status: StatusCode,
    message: &str,
) -> Response {
    let duration_ms = start.elapsed().as_secs_f64() * 1000.0;
    write_proxy_log(
        state,
        auth_ctx,
        correlation_id,
        card_id,
        None,
        resource_group_id,
        status.as_u16(),
        duration_ms,
        false,
        Some(message),
        &StepLogIds::none(),
        None,
        None,
        None,
    );
    error_response(status, message)
}

#[allow(clippy::too_many_arguments)]
async fn log_and_fail_with_chain(
    state: &SharedState,
    auth_ctx: &AuthContext,
    correlation_id: &str,
    start: Instant,
    card_id: Option<&str>,
    resource_group_id: Option<&str>,
    ids: &StepLogIds,
    status: StatusCode,
    message: &str,
) -> Response {
    let duration_ms = start.elapsed().as_secs_f64() * 1000.0;
    write_proxy_log(
        state,
        auth_ctx,
        correlation_id,
        card_id,
        None,
        resource_group_id,
        status.as_u16(),
        duration_ms,
        false,
        Some(message),
        ids,
        None,
        None,
        None,
    );
    error_response(status, message)
}

/// Write a single A2A **guardrail-block** row with `guardrail_blocked = true`
/// (or, when the guardrails could not be read, sealed `engine_failure`)
/// (and `pii_detected` when the failing check was PII) then return the block
/// response. Mirrors `log_residency_violation`. Without this, A2A guardrail
/// blocks land with `guardrail_blocked = false` and are invisible to Mission
/// Control's Governance tab, whose "Guardrail Blocks" counter is
/// `count(proxy_logs where guardrail_blocked)` — otherwise fed only by the
/// LLM and MCP surfaces. `direction` is the inbound/outbound proxy_subtype so
/// the row still slots into the delegation graph.
#[allow(clippy::too_many_arguments)]
fn log_guardrail_block(
    state: &SharedState,
    auth_ctx: &AuthContext,
    correlation_id: &str,
    start: Instant,
    card_id: Option<&str>,
    target_url: Option<&str>,
    resource_group_id: Option<&str>,
    ids: &StepLogIds,
    direction: &str,
    err: &A2aError,
    capability: Option<&str>,
    request_body: Option<&str>,
    response_body: Option<&str>,
) -> Response {
    let status = err.kind().http_status();
    let message = err.message();
    let duration_ms = start.elapsed().as_secs_f64() * 1000.0;
    let path = if direction == PROXY_SUBTYPE_INBOUND {
        "/v1/proxy/a2a/inbound"
    } else {
        "/v1/proxy/a2a/outbound"
    };
    let mut log = proxy_logger::build_proxy_log(
        constants::proxy::A2A,
        auth_ctx,
        "POST",
        path,
        target_url,
        card_id,
        status.as_u16(),
        duration_ms,
        false,
        Some(message),
        correlation_id,
    );
    log.proxy_subtype = Some(direction.to_string());
    log.root_task_id = ids.root_task_id.clone();
    log.delegation_depth = ids.depth;
    log.current_task_id = ids.current_task_id.clone();
    log.parent_task_id = ids.parent_task_id.clone();
    log.mcp_capability_name = capability.map(str::to_owned);
    // Request + response bodies so a blocked A2A step carries the same
    // forensic payload detail as `write_proxy_log` / `log_and_fail`.
    log.request_body_preview = request_body.map(proxy_logger::body_preview);
    log.response_body_preview = response_body.map(proxy_logger::body_preview);
    if let Some(rb) = request_body {
        log.request_size_bytes = Some(rb.len() as i32);
    }
    if let Some(rb) = response_body {
        log.response_size_bytes = Some(rb.len() as i32);
    }
    log.resource_group_id = resource_group_id
        .map(str::to_owned)
        .or_else(|| auth_ctx.resolved_group_id.clone());
    if err.kind().engine_failure() {
        // The guardrails could not be read: no policy decided this, so it is
        // sealed `engine_failure`, not counted as a guardrail block.
        log.proxy_subtype = Some(constants::proxy_subtype::ENGINE_FAILURE.to_string());
    } else {
        log.guardrail_blocked = true;
        // The guardrail error message carries the failing check, e.g. "(check=pii)".
        log.pii_detected = message.contains("check=pii");
    }
    state
        .background_writer
        .write_proxy_log(state.db.clone(), log);
    error_response(status, message)
}

/// Write a single `residency_violation` audit row and return the 403.
///
/// Mirrors the MCP/LLM residency-block contract: one proxy_log row
/// stamped `proxy_subtype = residency_violation` +
/// `residency_class = blocked` + the resolved `resource_group_id`, then
/// a `region_not_allowed` 403. Used by both A2A directions so the
/// monthly residency report off `proxy_logs` sees A2A blocks too.
#[allow(clippy::too_many_arguments)]
fn log_residency_violation(
    state: &SharedState,
    auth_ctx: &AuthContext,
    correlation_id: &str,
    start: Instant,
    card_id: Option<&str>,
    target_url: Option<&str>,
    resource_group_id: Option<&str>,
    reason: &str,
    capability: Option<&str>,
) -> Response {
    let duration_ms = start.elapsed().as_secs_f64() * 1000.0;
    let mut log = proxy_logger::build_proxy_log(
        constants::proxy::A2A,
        auth_ctx,
        "POST",
        "/v1/proxy/a2a/outbound",
        target_url,
        card_id,
        StatusCode::FORBIDDEN.as_u16(),
        duration_ms,
        false,
        Some(reason),
        correlation_id,
    );
    log.proxy_subtype = Some(crate::constants::proxy_subtype::RESIDENCY_VIOLATION.to_string());
    log.mcp_capability_name = capability.map(str::to_owned);
    log.resource_group_id = resource_group_id
        .map(str::to_owned)
        .or_else(|| auth_ctx.resolved_group_id.clone());
    proxy_logger::apply_residency_audit(
        &mut log,
        &crate::services::residency::ResidencyDecision::Blocked {
            reason: reason.to_string(),
        },
    );
    state
        .background_writer
        .write_proxy_log(state.db.clone(), log);
    error_response(
        StatusCode::FORBIDDEN,
        &format!("region_not_allowed: {reason}"),
    )
}

// ===========================================================================
// Public discovery — GET /.well-known/agent-card/{tenant_id}/{card_name}.json
// ===========================================================================

/// Response shape for the public discovery endpoint.
///
/// Brutor-specific wrapper around the v1.0 Agent Card. The wrapper
/// carries registry metadata (stable `id`, `trust_tier`, detected
/// dialect) that the spec doesn't cover — external verifiers that
/// only understand the v1.0 spec body read the `card` field and
/// ignore the wrapper.
///
/// The `card` field is **always** a v1.0-shaped AgentCard (see
/// [`a2a_v1::AgentCard`]). Cards stored in the v0.3 form are
/// translated on-the-fly via [`a2a_v1::translator::translate_v03_to_v1`],
/// so a partner never sees the dialect the gateway happens to have
/// at rest.
#[derive(serde::Serialize, utoipa::ToSchema)]
pub struct A2ACardDiscoveryResponse {
    /// Stable ULID — external agents pass this back as `target_card_id`
    /// when invoking capabilities, so they never need to resolve names.
    pub id: String,
    pub tenant_id: String,
    pub name: String,
    pub version: String,
    pub trust_tier: String,
    /// `"1.0"` if the card was either stored as or translated into v1.0
    /// shape. `"0.3"` if translation was skipped (never surfaces today —
    /// we always translate — but kept in the schema for forward-compat).
    pub a2a_version: String,
    /// The v1.0 Agent Card body. Verifiers consume only this field.
    pub card: Value,
    /// Legacy v0.3 flat signature fields — still populated from the
    /// stored row during the v1.0 rollout so pre-v1.0 consumers don't
    /// break. Under v1.0 rules the authoritative signatures live in
    /// `card.signatures[]` and partners should key on those.
    #[serde(skip_serializing_if = "Option::is_none")]
    #[deprecated(note = "Use card.signatures[] per A2A v1.0")]
    pub signature: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[deprecated(note = "Use card.signatures[] per A2A v1.0")]
    pub signature_algorithm: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[deprecated(note = "Use card.signatures[] per A2A v1.0")]
    pub signed_by: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[schema(value_type = Option<String>, format = DateTime)]
    #[deprecated(note = "Use card.signatures[] per A2A v1.0")]
    pub signed_at: Option<chrono::DateTime<chrono::Utc>>,
}

#[utoipa::path(
    get,
    path = "/.well-known/agent-card/{tenant_id}/{card_name}",
    tag = "A2A",
    summary = "Public Agent Card discovery",
    description = "Unauthenticated. Returns the verbatim card JSON + signature \
                   block for external verifiers. 404 if the tenant has A2A \
                   disabled, the card is disabled, or the card has been revoked.",
    params(
        ("tenant_id" = String, Path, description = "Tenant ID"),
        ("card_name" = String, Path, description = "Agent card name (unique per tenant)"),
    ),
    responses(
        (status = 200, description = "Agent card document", body = A2ACardDiscoveryResponse),
        (status = 404, description = "Card not available for public discovery"),
    ),
)]
pub(crate) async fn discover_card(
    State(state): State<SharedState>,
    Path((tenant_id, card_name)): Path<(String, String)>,
) -> Response {
    // Feature gate: same 404-on-missing behaviour as the Python admin
    // surface so the presence of a tenant's A2A opt-in is not
    // leaked via differential responses.
    match check_a2a_enabled(&state, &tenant_id).await {
        Ok(true) => {}
        Ok(false) => return not_found(),
        Err(e) => {
            tracing::error!(error = %e, "a2a.discover: feature gate query failed");
            return not_found();
        }
    }

    let card = match load_discovery_card(&state, &tenant_id, &card_name).await {
        Ok(Some(c)) => c,
        Ok(None) => return not_found(),
        Err(e) => {
            tracing::error!(error = %e, "a2a.discover: card lookup failed");
            return not_found();
        }
    };

    if !card.enabled {
        return not_found();
    }
    if card.revoked_at.is_some() || card.trust_tier == "revoked" {
        // Revoked cards are never discoverable — they stay in the
        // registry for audit, not for external consumption.
        return not_found();
    }

    // Translate v0.3 → v1.0 on read. Cards stored in v1.0 shape are
    // deep-cloned and augmented with the Brutor delegation extension
    // (idempotent). Translation failures yield 404 rather than 500 so
    // the discovery surface never tells a partner "we have a card here
    // but it's broken" — they just see "no such card" and move on.
    let translated = match crate::services::a2a_v1::translator::translate_v03_to_v1(&card.card_json)
    {
        Ok(v) => v,
        Err(e) => {
            tracing::warn!(
                card_id = %card.id,
                tenant_id = %card.tenant_id,
                error = %e,
                "a2a.discover: card translation failed — serving 404 to avoid leaking malformed cards",
            );
            return not_found();
        }
    };

    #[allow(deprecated)]
    let body = A2ACardDiscoveryResponse {
        id: card.id,
        tenant_id: card.tenant_id,
        name: card.name,
        version: card.version,
        trust_tier: card.trust_tier,
        a2a_version: "1.0".to_string(),
        card: translated,
        signature: card.signature,
        signature_algorithm: card.signature_algorithm,
        signed_by: card.signed_by,
        signed_at: card.signed_at,
    };
    (
        StatusCode::OK,
        [
            (axum::http::header::CACHE_CONTROL, "public, max-age=60"),
            (
                axum::http::header::HeaderName::from_static(
                    crate::services::a2a_v1::version::HEADER_A2A_VERSION,
                ),
                crate::services::a2a_v1::version::VERSION_V1_0,
            ),
        ],
        Json(body),
    )
        .into_response()
}

fn not_found() -> Response {
    // Plain JSON 404 — matches the rest of the proxy's error shape.
    (StatusCode::NOT_FOUND, Json(json!({"error": "not_found"}))).into_response()
}

/// Discovery uses the same cache as the invocation paths — the
/// signature columns are part of `CachedAgentCard` so external
/// verifiers benefit from the cache too.
async fn load_discovery_card(
    state: &SharedState,
    tenant_id: &str,
    card_name: &str,
) -> Result<Option<AgentCardDiscoveryRow>, sqlx::Error> {
    Ok(state
        .agent_card_cache
        .get_by_name(tenant_id, card_name)
        .await?
        .map(|cached| AgentCardDiscoveryRow {
            id: cached.id.clone(),
            tenant_id: cached.tenant_id.clone(),
            name: cached.name.clone(),
            version: cached.version.clone(),
            card_json: cached.card_json.clone(),
            signature: cached.signature.clone(),
            signature_algorithm: cached.signature_algorithm.clone(),
            signed_by: cached.signed_by.clone(),
            signed_at: cached.signed_at,
            trust_tier: cached.trust_tier.clone(),
            enabled: cached.enabled,
            revoked_at: cached.revoked_at,
        }))
}

// ===========================================================================
// Public JWK Set — GET /.well-known/a2a-jwks/{tenant_id}
// ===========================================================================

/// Fetch the tenant's A2A Ed25519 public key (base64url-encoded).
///
/// Returns `Ok(None)` if the row is missing (tenant hasn't provisioned
/// a signing key yet) OR if the `tenant_a2a_keys` table doesn't exist
/// on an early-schema deployment. Missing table is a permanent 404 at
/// the route layer; missing row is a temporary 404 until the admin UI
/// has generated the key.
async fn load_tenant_public_key(
    db: &PgPool,
    tenant_id: &str,
) -> Result<Option<String>, sqlx::Error> {
    let res = sqlx::query_scalar::<_, String>(
        "SELECT public_key FROM tenant_a2a_keys WHERE tenant_id = $1 LIMIT 1",
    )
    .bind(tenant_id)
    .fetch_optional(db)
    .await;
    match res {
        Ok(opt) => Ok(opt),
        Err(sqlx::Error::Database(e)) if is_missing_table(&*e) => Ok(None),
        Err(e) => Err(e),
    }
}

#[utoipa::path(
    get,
    path = "/.well-known/a2a-jwks/{tenant_id}",
    tag = "A2A",
    summary = "Public JWK Set for a tenant's Agent Card signatures",
    description = "Unauthenticated. Returns an RFC 7517 JWK Set containing the \
                   tenant's Ed25519 public signing key, keyed by tenant id \
                   (used as `kid`). External verifiers resolve Agent Card \
                   signatures by matching the `kid` in the JWS protected \
                   header to the `kid` field on a key in this set.",
    params(
        ("tenant_id" = String, Path, description = "Tenant ID"),
    ),
    responses(
        (status = 200, description = "JWK Set"),
        (status = 404, description = "Tenant unknown, A2A disabled, or no signing key yet"),
    ),
)]
pub(crate) async fn discover_jwks(
    State(state): State<SharedState>,
    Path(tenant_id): Path<String>,
) -> Response {
    // Feature gate first — a disabled tenant's existence is hidden
    // behind a 404 for symmetry with `discover_card`.
    match check_a2a_enabled(&state, &tenant_id).await {
        Ok(true) => {}
        Ok(false) => return not_found(),
        Err(e) => {
            tracing::error!(error = %e, "a2a.jwks: feature gate query failed");
            return not_found();
        }
    }

    let public_key = match load_tenant_public_key(&state.db, &tenant_id).await {
        Ok(Some(k)) => k,
        Ok(None) => return not_found(),
        Err(e) => {
            tracing::error!(error = %e, "a2a.jwks: public key lookup failed");
            return not_found();
        }
    };

    let set = crate::services::a2a_v1::jwks::public_key_to_jwk_set(&public_key, &tenant_id);
    (
        StatusCode::OK,
        [
            (axum::http::header::CACHE_CONTROL, "public, max-age=300"),
            // Spec-compliant JWK Sets use `application/jwk-set+json`
            // per RFC 7517 §8.5, but `application/json` is also
            // accepted. We emit both so old parsers and new ones are
            // happy — Axum will set `application/json` from `Json`,
            // and any JWK-aware client checks only the charset.
            (axum::http::header::CONTENT_TYPE, "application/jwk-set+json"),
        ],
        Json(set),
    )
        .into_response()
}

// ===========================================================================
// Inbound — POST /v1/proxy/a2a/inbound
// ===========================================================================

#[derive(Debug, Deserialize, utoipa::ToSchema)]
pub struct A2AInboundRequest {
    /// Stable card ID (not name). External callers get this from the
    /// discovery response.
    pub target_card_id: String,
    /// Dotted capability name, e.g. `billing.refund`.
    pub capability: String,
    /// Opaque message body passed to the handler. For `handler_type=skill`
    /// this is mapped into `SkillExecuteRequest.input_params`.
    pub message: Value,
    /// Optional group override for skill dispatch. Ignored by `noop`.
    #[serde(default)]
    pub group_id: Option<String>,
}

#[utoipa::path(
    post,
    path = "/v1/proxy/a2a/inbound",
    tag = "A2A",
    summary = "Inbound A2A invocation",
    description = "Accept a capability invocation from a remote agent and \
                   dispatch it to the configured local handler.",
    request_body = A2AInboundRequest,
    responses(
        (status = 200, description = "Handler result"),
        (status = 202, description = "Handler accepted (async dispatch)"),
        (status = 400, description = "Malformed request"),
        (status = 401, description = "Authentication required"),
        (status = 403, description = "Blocked by policy or feature flag"),
        (status = 404, description = "Card or capability not found"),
        (status = 501, description = "Handler not configured"),
    ),
    security(("api_key" = []), ("bearer" = []))
)]
pub(crate) async fn inbound(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Json(request): Json<A2AInboundRequest>,
) -> Response {
    let start = Instant::now();
    let tenant_id = match route_helpers::extract_tenant_id(tenant) {
        Ok(t) => t,
        Err(r) => return r,
    };
    let correlation_id = route_helpers::extract_correlation_id(&headers);

    let mut auth_ctx = match auth::authenticate_request(
        &headers,
        &tenant_id,
        &state.config.jwt_secret,
        &state.db,
        &state.credential_verifiers,
        client_addr.ip(),
        true,
    )
    .await
    {
        Ok(ctx) => ctx,
        Err(e) => return error_response(e.status_code(), &e.to_string()),
    };
    if let Err(e) = crate::services::identity_policy::judge_call(&state, &mut auth_ctx).await {
        return error_response(e.status_code(), &e.to_string());
    }

    // Feature gate.
    match check_a2a_enabled(&state, &tenant_id).await {
        Ok(true) => {}
        Ok(false) => {
            return log_inbound_and_fail(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                None,
                None,
                &StepLogIds::none(),
                StatusCode::FORBIDDEN,
                "A2A feature is disabled for this tenant",
                Some(&request.capability),
            );
        }
        Err(e) => {
            tracing::error!(error = %e, "a2a.inbound: feature gate query failed");
            return log_inbound_and_fail(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                None,
                None,
                &StepLogIds::none(),
                StatusCode::INTERNAL_SERVER_ERROR,
                "Feature gate lookup failed",
                Some(&request.capability),
            );
        }
    }

    // Resolve caller's group — same logic as outbound. For external
    // callers the auth context's api_key_group_id is the primary
    // driver; authenticated portal users fall through to their group
    // membership.
    let target_group =
        crate::services::usage::resolve_target_group(&state.db, &auth_ctx, &tenant_id).await;
    // As on the outbound route — the checks below run only for a
    // loaded group, so a failed load refuses rather than skipping them.
    let group_data = match &target_group {
        Some(g) => match state
            .group_cache
            .try_get_effective_group(&tenant_id, &g.id)
            .await
        {
            Ok(data) => data,
            Err(e) => {
                tracing::error!(error = %e, tenant_id = %tenant_id, group_id = %g.id, "A2A inbound: caller group load failed; refusing (fail closed)");
                return log_inbound_and_fail(
                    &state,
                    &auth_ctx,
                    &correlation_id,
                    start,
                    Some(&request.target_card_id),
                    Some(&g.id),
                    &StepLogIds::none(),
                    StatusCode::FORBIDDEN,
                    crate::services::cache::ResourceGroupCache::UNAVAILABLE_MESSAGE,
                    Some(&request.capability),
                );
            }
        },
        None => None,
    };
    let resolved_group_id = target_group.as_ref().map(|g| g.id.clone());

    // Card lookup — same shape as the outbound route.
    let card = match load_agent_card(&state, &tenant_id, &request.target_card_id).await {
        Ok(Some(c)) => c,
        Ok(None) => {
            return log_inbound_and_fail(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&request.target_card_id),
                resolved_group_id.as_deref(),
                &StepLogIds::none(),
                StatusCode::NOT_FOUND,
                "Agent card not found",
                Some(&request.capability),
            );
        }
        Err(e) => {
            tracing::error!(error = %e, "a2a.inbound: card lookup failed");
            return log_inbound_and_fail(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&request.target_card_id),
                resolved_group_id.as_deref(),
                &StepLogIds::none(),
                StatusCode::INTERNAL_SERVER_ERROR,
                "Card lookup failed",
                Some(&request.capability),
            );
        }
    };

    if !card.enabled || card.revoked_at.is_some() || card.trust_tier == "revoked" {
        return log_inbound_and_fail(
            &state,
            &auth_ctx,
            &correlation_id,
            start,
            Some(&card.id),
            resolved_group_id.as_deref(),
            &StepLogIds::none(),
            StatusCode::FORBIDDEN,
            "Agent card is disabled or revoked",
            Some(&request.capability),
        );
    }

    // Group access — if the caller has a resolvable group, the target
    // card must be bound (direct or inherited) to that group. Unbound
    // cards are registry-only; no group can invoke them.
    if let Some(ref gd) = group_data {
        if !gd.effective_agent_card_ids.is_empty()
            && !gd.effective_agent_card_ids.contains(&card.id)
        {
            return log_inbound_and_fail(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                resolved_group_id.as_deref(),
                &StepLogIds::none(),
                StatusCode::FORBIDDEN,
                "Agent card is not bound to the caller's resource group",
                Some(&request.capability),
            );
        }
    }

    // This card may be a Brutor-hosted shared service that
    // enforces its accepted callers. The caller is identified by its
    // credential's group (the group this row is attributed to), walked to
    // its nearest AI System — the same rule as the outbound gate.
    {
        let caller_group = resolved_group_id
            .as_deref()
            .or(auth_ctx.resolved_group_id.as_deref());
        let refused = match crate::services::accepted_callers::check(
            &state,
            &tenant_id,
            caller_group,
            &card.id,
        )
        .await
        {
            crate::services::accepted_callers::Verdict::Refused(r) => {
                tracing::warn!(
                    tenant_id = %tenant_id,
                    card_id = %card.id,
                    service = %r.service_system_id,
                    caller = ?r.caller_system_id,
                    "A2A inbound refused: caller is not an accepted client of the service",
                );
                Some((r.subtype(), r.message))
            }
            crate::services::accepted_callers::Verdict::Unavailable(_) => Some((
                constants::proxy_subtype::ENGINE_FAILURE,
                crate::services::accepted_callers::UNAVAILABLE_MESSAGE.to_string(),
            )),
            _ => None,
        };
        if let Some((subtype, message)) = refused {
            return log_inbound_and_refuse_as(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                caller_group,
                &StepLogIds::none(),
                StatusCode::FORBIDDEN,
                subtype,
                &message,
                Some(&request.capability),
            );
        }
    }

    let capability = match load_capability(&state, &tenant_id, &card.id, &request.capability).await
    {
        Ok(Some(c)) => c,
        Ok(None) => {
            return log_inbound_and_fail(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                resolved_group_id.as_deref(),
                &StepLogIds::none(),
                StatusCode::NOT_FOUND,
                "Capability not found on this agent card",
                Some(&request.capability),
            );
        }
        Err(e) => {
            tracing::error!(error = %e, "a2a.inbound: capability lookup failed");
            return log_inbound_and_fail(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                resolved_group_id.as_deref(),
                &StepLogIds::none(),
                StatusCode::INTERNAL_SERVER_ERROR,
                "Capability lookup failed",
                Some(&request.capability),
            );
        }
    };

    if !capability.enabled {
        return log_inbound_and_fail(
            &state,
            &auth_ctx,
            &correlation_id,
            start,
            Some(&card.id),
            resolved_group_id.as_deref(),
            &StepLogIds::none(),
            StatusCode::FORBIDDEN,
            "Capability is disabled",
            Some(&request.capability),
        );
    }

    // NOTE (G8 residency): the inbound route dispatches to a LOCAL
    // handler (skill / noop) — there is no external provider/region the
    // call routes to, so data-residency has nothing to evaluate here.
    // Residency is enforced on the OUTBOUND route, where the call
    // actually leaves our tenant for a remote agent.

    // Parse an inbound delegation chain if the caller supplied one.
    // Missing chain is fine — this is often the root step (external
    // caller starts a new delegation graph). Present-but-invalid is
    // NOT fine; we refuse rather than silently minting a root.
    let tenant_key = derive_tenant_key(state.config.encryption_key.as_bytes(), &tenant_id);
    let current_step_task_id = Ulid::new();
    let inbound_chain =
        DelegationChain::parse_and_verify(|name| header_value(&headers, name), &tenant_key);
    // Pre-compute this step's own graph-tree IDs before dispatch, so
    // both success and error paths record the same node.
    #[allow(clippy::type_complexity)]
    let (my_root, my_depth, my_parent_ulid, my_actor, my_subject): (
        Ulid,
        u32,
        Option<Ulid>,
        Option<String>,
        Option<String>,
    ) = match &inbound_chain {
        Ok(parent) => (
            parent.root_task_id,
            parent.depth,
            Some(parent.parent_task_id),
            parent.actor_agent_id.clone(),
            parent.subject.clone(),
        ),
        // No verifiable inbound chain. Before minting a fresh root, honour a
        // run id the caller asserted: a self-driving agent has no chain to
        // present, so without this its A2A step — and everything the peer then
        // does under the chain we sign from it — starts a run of its own,
        // split from the caller's other steps for the same task.
        //
        // `asserted_root` yields None unless the caller is entitled to the
        // assertion (credential-bound, tenant-keyed, and no chain header
        // present), so this cannot graft a step onto someone else's run.
        Err(_) => {
            let get = |n: &str| header_value(&headers, n);
            match call_chain::asserted_root(&get, &tenant_key) {
                Some(root) => (root, 0, None, None, None),
                None => (current_step_task_id, 0, None, None, None),
            }
        }
    };
    let my_ids = StepLogIds::from_step(current_step_task_id, my_parent_ulid, my_root, my_depth)
        .with_principal(my_actor, my_subject);
    // A task on a verified inbound chain continues its run; any other
    // starts one for its credential if the run is new (the run controls
    // below).
    let verified_chain = inbound_chain.is_ok();

    // Replay guard: a verified inbound chain must also be FRESH (see the
    // outbound path for the rationale — the parent ULID's signed timestamp
    // bounds replay of a captured header set to the freshness window).
    if let Ok(ref chain) = inbound_chain {
        let now_ms = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0);
        if let Err(e) = chain.check_fresh(
            now_ms,
            crate::services::a2a_delegation::delegation_max_age_secs(),
            crate::services::a2a_delegation::DELEGATION_FRESHNESS_LEEWAY_SECS,
        ) {
            let msg = e.to_string();
            return log_inbound_and_fail(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                resolved_group_id.as_deref(),
                &my_ids,
                StatusCode::FORBIDDEN,
                &msg,
                Some(&request.capability),
            );
        }
    }

    // We fork the chain only to validate the absolute depth cap and
    // surface the same errors as outbound — the forked chain itself
    // is not used again (inbound doesn't re-emit a downstream chain).
    match inbound_chain {
        Ok(parent) => match parent.fork_child(current_step_task_id) {
            Ok(_) => {}
            Err(DelegationError::DepthOverflow { depth }) => {
                let msg = format!(
                    "Delegation chain exceeds absolute depth cap ({} > {})",
                    depth,
                    crate::services::a2a_delegation::ABSOLUTE_MAX_DEPTH
                );
                return log_inbound_and_fail(
                    &state,
                    &auth_ctx,
                    &correlation_id,
                    start,
                    Some(&card.id),
                    resolved_group_id.as_deref(),
                    &my_ids,
                    StatusCode::FORBIDDEN,
                    &msg,
                    Some(&request.capability),
                );
            }
            Err(e) => {
                tracing::error!(error = %e, "a2a.inbound: fork_child unexpectedly failed");
                return log_inbound_and_fail(
                    &state,
                    &auth_ctx,
                    &correlation_id,
                    start,
                    Some(&card.id),
                    resolved_group_id.as_deref(),
                    &my_ids,
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "Delegation chain construction failed",
                    Some(&request.capability),
                );
            }
        },
        Err(DelegationError::MalformedHeader(_)) => {}
        Err(e) => {
            let msg = format!("Inbound delegation chain rejected: {}", e);
            return log_inbound_and_fail(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                resolved_group_id.as_deref(),
                &StepLogIds::none(),
                StatusCode::FORBIDDEN,
                &msg,
                Some(&request.capability),
            );
        }
    };

    // Run-scoped controls — the run budget and an operator's abort — for
    // EVERY caller, before any handler runs. Inbound had neither: a stopped
    // run could still hand work to a local handler. See
    // `run_controls::check`.
    let run_scope = RunScope {
        run_id: my_root.to_string(),
        parent_task_id: Some(current_step_task_id.to_string()),
        root_kind: proxy_logger::root_kind_for_auth(&auth_ctx.auth_type, my_depth == 0)
            .map(str::to_owned),
        depth: my_depth,
        starter: None,
    };
    // The task is one action of the run, whatever its handler does (a
    // skill it runs is part of it).
    let starter = crate::services::run_controls::run_starter(&auth_ctx, verified_chain);
    let step = match crate::services::run_controls::check(
        &state,
        crate::services::run_controls::Step {
            tenant_id: &tenant_id,
            group_id: resolved_group_id.as_deref(),
            run_id: &run_scope.run_id,
            depth: run_scope.depth,
            kind: crate::services::run_controls::StepKind::Action,
            starter: starter.as_deref(),
        },
    )
    .await
    {
        Ok(admitted) => admitted,
        Err(refusal) => {
            return log_inbound_and_refuse_as(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                resolved_group_id.as_deref(),
                &my_ids,
                refusal.status(),
                refusal.subtype(),
                refusal.message(),
                Some(&request.capability),
            );
        }
    };

    // The caller's own plane (`a2a_governance::authorize_actor`), as on the
    // outbound route and the v1.0 routes and, as there, before the A2A
    // policy: an agent's `a2a_call` grants for this card, or, for any other
    // caller, its AI System's autonomy. It runs here, where the run is
    // known, so a hold names the run it holds; a refused or held call gives
    // back the action admitted above.
    //
    // A task a skill runs passes the skill's own gates after these. The
    // request's approvals are one object for all of them, and the skill's
    // gates are named to this route's, so a call both hold is held once,
    // for both (`agent_approval::RequestApprovals`).
    let approvals = std::sync::Arc::new(agent_approval::RequestApprovals::from_headers(&headers));
    let skill_request = (capability.handler_type.as_deref() == Some(HANDLER_TYPE_SKILL))
        .then(|| {
            inbound_skill_request(
                &capability,
                &request,
                &request.message,
                &correlation_id,
                &approvals,
            )
        })
        .flatten();
    let skill_gates = skill_request.as_ref().map(|request| GuardedSkillGates {
        state: &state,
        auth: &auth_ctx,
        tenant_id: &tenant_id,
        request,
        correlation_id: &correlation_id,
        run: &run_scope,
    });
    let downstream = skill_gates
        .as_ref()
        .map(|gates| gates as &dyn agent_approval::Downstream);
    let approved = match a2a_governance::authorize_actor(
        &state,
        a2a_governance::ActorCall {
            tenant_id: &tenant_id,
            auth_ctx: &auth_ctx,
            card_id: &card.id,
            group_id: resolved_group_id
                .as_deref()
                .or(auth_ctx.api_key_group_id.as_deref()),
            run: Some(run_scope.clone()),
            approvals: &approvals,
            downstream,
        },
    )
    .await
    {
        a2a_governance::ActorOutcome::Proceed { approved, shadow } => {
            // dry_run: record the would-block (shadow) and proceed.
            if let Some(o) = shadow {
                let mut log = build_inbound_log(
                    &auth_ctx,
                    &correlation_id,
                    Some(&card.id),
                    resolved_group_id.as_deref(),
                    200,
                    start.elapsed().as_secs_f64() * 1000.0,
                    true,
                    Some(&o.reason),
                    &my_ids,
                    Some(&request.capability),
                );
                proxy_logger::apply_agent_authz_decision(&mut log, &o, false);
                log.proxy_subtype = Some(constants::proxy_subtype::AGENT_AUTHZ_SHADOW.to_string());
                state
                    .background_writer
                    .write_proxy_log(state.db.clone(), log);
            }
            approved
        }
        a2a_governance::ActorOutcome::Denied(o) => {
            let message = format!("agent_not_authorized: {}", o.reason);
            let mut log = build_inbound_log(
                &auth_ctx,
                &correlation_id,
                Some(&card.id),
                resolved_group_id.as_deref(),
                StatusCode::FORBIDDEN.as_u16(),
                start.elapsed().as_secs_f64() * 1000.0,
                false,
                Some(&message),
                &my_ids,
                Some(&request.capability),
            );
            proxy_logger::apply_agent_authz_decision(&mut log, &o, true);
            state
                .background_writer
                .write_proxy_log(state.db.clone(), log);
            return error_response(StatusCode::FORBIDDEN, &message);
        }
        a2a_governance::ActorOutcome::Held {
            status,
            envelope,
            decision,
        } => {
            let mut log = build_inbound_log(
                &auth_ctx,
                &correlation_id,
                Some(&card.id),
                resolved_group_id.as_deref(),
                status.as_u16(),
                start.elapsed().as_secs_f64() * 1000.0,
                false,
                Some("approval_required"),
                &my_ids,
                Some(&request.capability),
            );
            proxy_logger::apply_agent_authz_decision(&mut log, &decision, false);
            log.proxy_subtype = Some(constants::proxy_subtype::APPROVAL_REQUIRED.to_string());
            let preview = envelope.to_string();
            log.response_size_bytes = Some(preview.len() as i32);
            log.response_body_preview = Some(proxy_logger::body_preview(&preview));
            state
                .background_writer
                .write_proxy_log(state.db.clone(), log);
            return (status, Json(envelope)).into_response();
        }
        a2a_governance::ActorOutcome::TokenRefused(answer) => {
            return log_inbound_and_refuse_as(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                resolved_group_id.as_deref(),
                &my_ids,
                StatusCode::FORBIDDEN,
                constants::proxy_subtype::APPROVAL_TOKEN_INVALID,
                &answer,
                Some(&request.capability),
            );
        }
        a2a_governance::ActorOutcome::HoldFailed { reason, .. } => {
            tracing::error!(error = %reason, "a2a inbound agent approval enqueue failed");
            return log_inbound_and_fail(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                resolved_group_id.as_deref(),
                &my_ids,
                StatusCode::INTERNAL_SERVER_ERROR,
                "Failed to create approval request",
                Some(&request.capability),
            );
        }
        a2a_governance::ActorOutcome::Unavailable => {
            // Fail closed because the engine could not decide: recorded as
            // an engine failure, not as a denial nobody made.
            return log_inbound_and_refuse_as(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                resolved_group_id.as_deref(),
                &my_ids,
                StatusCode::FORBIDDEN,
                constants::proxy_subtype::ENGINE_FAILURE,
                "agent_authz_unavailable",
                Some(&request.capability),
            );
        }
        // The skill the task is dispatched to would refuse it: refused
        // here with that answer, nothing held, no token spent.
        a2a_governance::ActorOutcome::RefusedLater(refusal) => {
            return inbound_skill_refusal(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                InboundStep {
                    card_id: &card.id,
                    resource_group_id: resolved_group_id.as_deref(),
                    ids: &my_ids,
                    capability: &request.capability,
                },
                SkillRefusal {
                    status: refusal.status,
                    message: &refusal.message,
                    subtype: refusal.subtype,
                    argument_policy_denied: refusal.argument_policy_denied,
                },
            );
        }
        a2a_governance::ActorOutcome::AutonomyDenied { reason, resolution } => {
            tracing::warn!(
                tenant_id = %tenant_id,
                card = %card.id,
                level = %resolution.level.as_str(),
                provenance = %resolution.provenance.as_str(),
                "autonomy block on a non-agent inbound caller — refusing 403",
            );
            let message = format!("autonomy_denied: {reason}");
            let mut log = build_inbound_log(
                &auth_ctx,
                &correlation_id,
                Some(&card.id),
                resolved_group_id.as_deref(),
                StatusCode::FORBIDDEN.as_u16(),
                start.elapsed().as_secs_f64() * 1000.0,
                false,
                Some(&message),
                &my_ids,
                Some(&request.capability),
            );
            proxy_logger::apply_autonomy(&mut log, &resolution, true);
            state
                .background_writer
                .write_proxy_log(state.db.clone(), log);
            return error_response(StatusCode::FORBIDDEN, &message);
        }
    };

    // The A2A policy of the caller's group chain (the tenant's roots for a
    // caller with no group) and of the card: the one evaluation every A2A
    // route uses (`services::a2a_governance`). It runs here, where the run
    // is known, so a hold names the run it holds. The step admitted above
    // stays counted only if the call is dispatched: a refused or held call
    // gives its action back.
    let policy = match a2a_governance::policy_for(
        &state.db,
        &tenant_id,
        group_data.as_deref(),
        card.tenant_governance.as_ref(),
    )
    .await
    {
        Ok(policy) => policy,
        Err(e) => {
            tracing::error!(error = %e, tenant_id = %tenant_id, "A2A inbound: policy load failed; refusing (fail closed)");
            return log_inbound_and_refuse_as(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                resolved_group_id.as_deref(),
                &my_ids,
                StatusCode::FORBIDDEN,
                constants::proxy_subtype::ENGINE_FAILURE,
                a2a_governance::UNAVAILABLE_MESSAGE,
                Some(&request.capability),
            );
        }
    };
    match a2a_governance::enforce(
        &state,
        &policy,
        a2a_governance::Call {
            tenant_id: &tenant_id,
            auth_ctx: &auth_ctx,
            card_id: &card.id,
            subject: a2a_governance::Subject {
                direction: a2a_governance::Direction::Inbound,
                classifications: capability.data_classification.iter().collect(),
                work: true,
            },
            run: Some(run_scope.clone()),
            approvals: &approvals,
            downstream,
            approved,
        },
    )
    .await
    {
        a2a_governance::Outcome::Proceed => {}
        a2a_governance::Outcome::Refused(reason) => {
            return log_inbound_and_fail(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                resolved_group_id.as_deref(),
                &my_ids,
                StatusCode::FORBIDDEN,
                &reason,
                Some(&request.capability),
            );
        }
        a2a_governance::Outcome::Held(status, envelope) => {
            let mut log = build_inbound_log(
                &auth_ctx,
                &correlation_id,
                Some(&card.id),
                resolved_group_id.as_deref(),
                status.as_u16(),
                start.elapsed().as_secs_f64() * 1000.0,
                false,
                Some("approval_required"),
                &my_ids,
                Some(&request.capability),
            );
            log.proxy_subtype = Some(constants::proxy_subtype::APPROVAL_REQUIRED.to_string());
            let preview = envelope.to_string();
            log.response_size_bytes = Some(preview.len() as i32);
            log.response_body_preview = Some(proxy_logger::body_preview(&preview));
            state
                .background_writer
                .write_proxy_log(state.db.clone(), log);
            return (status, Json(envelope)).into_response();
        }
        // The skill the task is dispatched to would refuse it: refused
        // here with that answer, nothing held, no token spent.
        a2a_governance::Outcome::RefusedLater(refusal) => {
            return inbound_skill_refusal(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                InboundStep {
                    card_id: &card.id,
                    resource_group_id: resolved_group_id.as_deref(),
                    ids: &my_ids,
                    capability: &request.capability,
                },
                SkillRefusal {
                    status: refusal.status,
                    message: &refusal.message,
                    subtype: refusal.subtype,
                    argument_policy_denied: refusal.argument_policy_denied,
                },
            );
        }
        a2a_governance::Outcome::TokenRefused(reason) => {
            return log_inbound_and_refuse_as(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                resolved_group_id.as_deref(),
                &my_ids,
                StatusCode::FORBIDDEN,
                constants::proxy_subtype::APPROVAL_TOKEN_INVALID,
                &format!("Approval token invalid: {reason}"),
                Some(&request.capability),
            );
        }
        a2a_governance::Outcome::HoldFailed(e) => {
            tracing::error!(error = %e, "a2a policy approval enqueue failed");
            return log_inbound_and_fail(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                resolved_group_id.as_deref(),
                &my_ids,
                StatusCode::INTERNAL_SERVER_ERROR,
                "Failed to create approval request",
                Some(&request.capability),
            );
        }
    }
    // The caller may make this call and the policy lets it through: now
    // what it costs. A2A rate limits: the caller group's, the card's own
    // (one aggregate for the card, whatever route reaches it) and the
    // capability's hourly rate, admitted together under a hold. A call the
    // gates above refused or held never reaches this, so it spends no
    // allowance; one the input guardrail below blocks gives back what was
    // admitted here (the guard's Drop); a dispatched call keeps it.
    let mut a2a_hold = crate::services::limits::PendingHold::of(
        crate::services::limits::HoldId::new(&tenant_id, "a2a-rate"),
    );
    let block = crate::services::usage::admit_a2a_limits(
        &state.usage_state,
        crate::services::usage::A2aLimits {
            group: match (group_data.as_ref(), resolved_group_id.as_deref()) {
                (Some(gd), Some(rg_id)) => Some((rg_id, gd.effective_a2a_limits.as_ref())),
                _ => None,
            },
            card: Some((
                tenant_id.as_str(),
                card.id.as_str(),
                card.tenant_limits.as_ref(),
            )),
            capability: Some((
                capability.id.as_str(),
                capability.capability_name.as_str(),
                capability.rate_limit_per_hour,
            )),
        },
        Some(&mut a2a_hold),
    )
    .await;
    let a2a_hold = a2a_hold.held().map(|h| state.usage_state.adopt_hold(h));
    if let Some(block) = block {
        tracing::warn!(
            tenant_id = %tenant_id,
            card_id = %card.id,
            scope = %block.scope,
            limit_type = %block.limit_type,
            retry_after_secs = block.retry_after_secs,
            "A2A inbound rate limit exceeded — 429 returned",
        );
        return log_inbound_and_fail(
            &state,
            &auth_ctx,
            &correlation_id,
            start,
            Some(&card.id),
            resolved_group_id.as_deref(),
            &my_ids,
            StatusCode::TOO_MANY_REQUESTS,
            &block.message,
            Some(&request.capability),
        );
    }

    // INPUT guardrails on the external agent's inbound message,
    // run BEFORE dispatch so unguarded ingress can't feed skill / agent
    // execution. Surface is `a2a_inbound` (data ENTERING our tenant).
    // The inbound message is free-form JSON, so we use the outbound
    // text walker + splice helpers (same pair the outbound request path
    // uses). On block → 403 PolicyBlocked to the caller; on redact →
    // splice the rewritten text into `request.message` in place so the
    // handler sees the sanitised payload.
    // The message as the caller sent it: what an approval of the skill it
    // is dispatched to is bound to, whatever the guardrail removes.
    let message_as_sent = request.message.clone();
    let mut request = request;
    {
        let input_text = a2a_guardrail::extract_outbound_text(&request.message);
        match a2a_guardrail::check(&state, &auth_ctx, &tenant_id, &input_text, "a2a_inbound").await
        {
            Err(err) => {
                tracing::warn!(
                    tenant_id = %tenant_id,
                    card_id = %card.id,
                    "a2a.inbound: message blocked by input guardrail",
                );
                // Inbound dispatches to a LOCAL handler — there is no external
                // target URL, so target_url stays None.
                let req_body = serde_json::to_string(&request.message).ok();
                return log_guardrail_block(
                    &state,
                    &auth_ctx,
                    &correlation_id,
                    start,
                    Some(&card.id),
                    None,
                    resolved_group_id.as_deref(),
                    &my_ids,
                    PROXY_SUBTYPE_INBOUND,
                    &err,
                    Some(&request.capability),
                    req_body.as_deref(),
                    None,
                );
            }
            Ok(a2a_guardrail::CheckResult::Redacted(new_text)) => {
                a2a_guardrail::apply_outbound_redactions(&mut request.message, &new_text);
            }
            Ok(a2a_guardrail::CheckResult::Pass) => {}
        }
    }
    // The handler is given the message as the guardrail left it.
    let skill_request = (capability.handler_type.as_deref() == Some(HANDLER_TYPE_SKILL))
        .then(|| {
            inbound_skill_request(
                &capability,
                &request,
                &message_as_sent,
                &correlation_id,
                &approvals,
            )
        })
        .flatten();

    // Every gate of this surface has passed and the handler is dispatched
    // next: the run's action and the rate allowance stay counted.
    step.commit().await;
    if let Some(hold) = a2a_hold {
        hold.commit().await;
    }

    // Dispatch based on handler_type.
    let handler_type = capability
        .handler_type
        .as_deref()
        .unwrap_or(HANDLER_TYPE_NOOP);
    let (status, response_body) = match handler_type {
        HANDLER_TYPE_NOOP => dispatch_noop(&card, &request),
        HANDLER_TYPE_SKILL => {
            match dispatch_skill(
                &state,
                &auth_ctx,
                &correlation_id,
                // The request the gates above were told about: it carries
                // the request's approvals, so the skill's gate finds its
                // token (or its question answered).
                skill_request.as_ref(),
                &run_scope,
            )
            .await
            {
                Ok(v) => (StatusCode::ACCEPTED, v),
                Err(e) => {
                    let status =
                        StatusCode::from_u16(e.status_code).unwrap_or(StatusCode::BAD_GATEWAY);
                    // The skill execution is held for a person. The answer
                    // is the approval envelope itself (the request to poll,
                    // where to poll it), as on the skill and MCP routes: the
                    // caller retries the same call with `X-Approval-Token`
                    // once it is approved.
                    if e.refusal == Some(constants::proxy_subtype::APPROVAL_REQUIRED) {
                        if let Some(envelope) = e.body {
                            let mut log = build_inbound_log(
                                &auth_ctx,
                                &correlation_id,
                                Some(&card.id),
                                resolved_group_id.as_deref(),
                                status.as_u16(),
                                start.elapsed().as_secs_f64() * 1000.0,
                                false,
                                Some(&e.message),
                                &my_ids,
                                Some(&request.capability),
                            );
                            log.proxy_subtype =
                                Some(constants::proxy_subtype::APPROVAL_REQUIRED.to_string());
                            let preview = envelope.to_string();
                            log.response_size_bytes = Some(preview.len() as i32);
                            log.response_body_preview =
                                Some(crate::services::proxy_logger::body_preview(&preview));
                            state
                                .background_writer
                                .write_proxy_log(state.db.clone(), log);
                            return (status, Json(envelope)).into_response();
                        }
                    }
                    return inbound_skill_refusal(
                        &state,
                        &auth_ctx,
                        &correlation_id,
                        start,
                        InboundStep {
                            card_id: &card.id,
                            resource_group_id: resolved_group_id.as_deref(),
                            ids: &my_ids,
                            capability: &request.capability,
                        },
                        SkillRefusal {
                            status,
                            message: &e.message,
                            subtype: e.refusal,
                            argument_policy_denied: e.argument_policy_denied,
                        },
                    );
                }
            }
        }
        other => {
            let msg = format!("Handler type {other:?} not implemented on this gateway");
            return log_inbound_and_fail(
                &state,
                &auth_ctx,
                &correlation_id,
                start,
                Some(&card.id),
                resolved_group_id.as_deref(),
                &my_ids,
                StatusCode::NOT_IMPLEMENTED,
                &msg,
                Some(&request.capability),
            );
        }
    };

    // OUTPUT guardrails on the dispatch RESULT we return to the
    // external caller. Surface is `a2a_outbound` (the result LEAVES our
    // tenant). On block → 403 PolicyBlocked (the handler output never
    // reaches the caller); on redact → splice the rewritten text into
    // the response body before returning + logging.
    let mut response_body = response_body;
    {
        let out_text = a2a_guardrail::extract_outbound_text(&response_body);
        match a2a_guardrail::check_output(&state, &auth_ctx, &tenant_id, &out_text, "a2a_outbound")
            .await
        {
            Err(err) => {
                tracing::warn!(
                    tenant_id = %tenant_id,
                    card_id = %card.id,
                    "a2a.inbound: dispatch result blocked by output guardrail",
                );
                // Inbound dispatch is a LOCAL handler — no external target URL.
                let req_body = serde_json::to_string(&request.message).ok();
                let resp_body = serde_json::to_string(&response_body).ok();
                return log_guardrail_block(
                    &state,
                    &auth_ctx,
                    &correlation_id,
                    start,
                    Some(&card.id),
                    None,
                    resolved_group_id.as_deref(),
                    &my_ids,
                    PROXY_SUBTYPE_INBOUND,
                    &err,
                    Some(&request.capability),
                    req_body.as_deref(),
                    resp_body.as_deref(),
                );
            }
            Ok(a2a_guardrail::CheckResult::Redacted(new_text)) => {
                a2a_guardrail::apply_outbound_redactions(&mut response_body, &new_text);
            }
            Ok(a2a_guardrail::CheckResult::Pass) => {}
        }
    }

    let duration_ms = start.elapsed().as_secs_f64() * 1000.0;
    write_inbound_proxy_log(
        &state,
        &auth_ctx,
        &correlation_id,
        Some(&card.id),
        resolved_group_id.as_deref(),
        status.as_u16(),
        duration_ms,
        status.is_success(),
        None,
        &my_ids,
        Some(&request.capability),
    );
    (status, Json(response_body)).into_response()
}

fn dispatch_noop(card: &AgentCardRow, request: &A2AInboundRequest) -> (StatusCode, Value) {
    let body = json!({
        "handler": "noop",
        "card_id": card.id,
        "capability": request.capability,
        "received_message": request.message,
        "note": "Capability is declared but no handler is bound yet.",
    });
    (StatusCode::OK, body)
}

/// The gates of the skill behind an inbound task, asked about the input the
/// skill would be given: the task's message as the input guardrail leaves
/// it. The route's gates run before the guardrail (a refused or held call
/// costs nothing), and a hold they raise for the skill must be for what the
/// approved retry will carry to the skill's own gate, and must not put into
/// the approval queue what the guardrail removes. A message the guardrail
/// blocks has no skill hold: the route refuses it when it gets there.
struct GuardedSkillGates<'a> {
    state: &'a SharedState,
    auth: &'a AuthContext,
    tenant_id: &'a str,
    request: &'a SkillExecuteRequest,
    correlation_id: &'a str,
    /// The run the task is a step of: the skill's run.
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
            let mut request = self.request.clone();
            if let Some(message) = request.input_params.as_mut() {
                let text = a2a_guardrail::extract_outbound_text(message);
                match a2a_guardrail::check(
                    self.state,
                    self.auth,
                    self.tenant_id,
                    &text,
                    "a2a_inbound",
                )
                .await
                {
                    Err(_) => return Ok(Vec::new()),
                    Ok(a2a_guardrail::CheckResult::Redacted(redacted)) => {
                        a2a_guardrail::apply_outbound_redactions(message, &redacted);
                    }
                    Ok(a2a_guardrail::CheckResult::Pass) => {}
                }
            }
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

/// The skill execution an inbound task bound to a skill is: built once, for
/// the gates of the route (which name the skill's gates beside their own)
/// and for the dispatch. `None` when the capability names no skill.
fn inbound_skill_request(
    capability: &AgentCardCapabilityRow,
    request: &A2AInboundRequest,
    // The message as the caller sent it, before the input guardrail.
    message_as_sent: &Value,
    correlation_id: &str,
    approvals: &std::sync::Arc<agent_approval::RequestApprovals>,
) -> Option<SkillExecuteRequest> {
    let handler_ref = capability.handler_ref.as_deref()?;

    // handler_ref may be either a skill_id or a name. skill_orchestrator
    // accepts both via separate params — we disambiguate by shape: a
    // prefixed id (`skill-` current, `ask-` legacy) or a bare ULID is a
    // skill_id; anything else is a skill name.
    let (skill_id, skill_name) = if handler_ref.starts_with("skill-")
        || handler_ref.starts_with("ask-")
        || Ulid::from_string(handler_ref).is_ok()
    {
        (Some(handler_ref.to_string()), None)
    } else {
        (None, Some(handler_ref.to_string()))
    };

    Some(SkillExecuteRequest {
        skill_id,
        skill_name,
        input_params: Some(request.message.clone()),
        group_id: request.group_id.clone(),
        correlation_id: Some(correlation_id.to_string()),
        parent_execution_id: None,
        // The request's tokens are its headers'.
        approval_token: None,
        approvals: approvals.clone(),
        data_scope_conflicts: Vec::new(),
        input_as_sent: Some(message_as_sent.clone()),
        input_view: crate::models::proxy::SkillInputView::A2aMessage,
    })
}

async fn dispatch_skill(
    state: &SharedState,
    auth_ctx: &AuthContext,
    correlation_id: &str,
    skill_req: Option<&SkillExecuteRequest>,
    run: &RunScope,
) -> Result<Value, SkillError> {
    let skill_req = skill_req.ok_or_else(|| SkillError {
        status_code: 404,
        message: "Skill handler_ref is null; handler not fully configured".to_string(),
        body: None,
        refusal: None,
        argument_policy_denied: false,
    })?;

    // The inbound route admitted this task as the run's action.
    let mut result = skill_orchestrator::execute_skill(
        state,
        auth_ctx,
        skill_req,
        correlation_id,
        run,
        crate::services::run_controls::StepKind::PartOfAction,
    )
    .await?;

    // Run the dispatched execution and settle it. This path used to
    // return here with `execution_status: RUNNING`: nothing ran the skill and
    // the RUNNING row held one of the group's concurrent-execution slots for
    // good.
    let output = match skill_orchestrator::run_and_settle(
        state,
        auth_ctx,
        &auth_ctx.tenant_id,
        &mut result,
        skill_req.input_params.as_ref(),
    )
    .await
    {
        skill_orchestrator::Settlement::Ran { output, .. } => output,
        skill_orchestrator::Settlement::OutputBlocked { message, .. } => {
            return Err(SkillError {
                status_code: 403,
                message,
                body: None,
                refusal: None,
                argument_policy_denied: false,
            });
        }
        skill_orchestrator::Settlement::GuardrailsUnavailable(e) => {
            tracing::error!(error = %e, "guardrail configs unavailable for A2A skill output; refusing (fail closed)");
            return Err(SkillError {
                status_code: 503,
                message: "guardrails_unavailable: refusing to return an unchecked skill result"
                    .to_string(),
                body: None,
                refusal: Some(constants::proxy_subtype::ENGINE_FAILURE),
                argument_policy_denied: false,
            });
        }
    };

    Ok(json!({
        "handler": "skill",
        "execution_id": result.execution_id,
        "skill_id": result.skill_id,
        "skill_name": result.skill_name,
        "skill_version": result.skill_version,
        "execution_status": result.execution_status,
        "correlation_id": result.correlation_id,
        "group_id": result.group_id,
        "group_name": result.group_name,
        "output": output,
    }))
}

#[allow(clippy::too_many_arguments)]
fn write_inbound_proxy_log(
    state: &SharedState,
    auth_ctx: &AuthContext,
    correlation_id: &str,
    card_id: Option<&str>,
    resource_group_id: Option<&str>,
    status_code: u16,
    duration_ms: f64,
    success: bool,
    error: Option<&str>,
    ids: &StepLogIds,
    capability: Option<&str>,
) {
    let log = build_inbound_log(
        auth_ctx,
        correlation_id,
        card_id,
        resource_group_id,
        status_code,
        duration_ms,
        success,
        error,
        ids,
        capability,
    );
    state
        .background_writer
        .write_proxy_log(state.db.clone(), log);
}

/// The inbound step's audit row, for callers that stamp more on it.
#[allow(clippy::too_many_arguments)]
fn build_inbound_log(
    auth_ctx: &AuthContext,
    correlation_id: &str,
    card_id: Option<&str>,
    resource_group_id: Option<&str>,
    status_code: u16,
    duration_ms: f64,
    success: bool,
    error: Option<&str>,
    ids: &StepLogIds,
    capability: Option<&str>,
) -> crate::models::db::ProxyLogInsert {
    let mut log = proxy_logger::build_proxy_log(
        constants::proxy::A2A,
        auth_ctx,
        "POST",
        "/v1/proxy/a2a/inbound",
        None,
        card_id,
        status_code,
        duration_ms,
        success,
        error,
        correlation_id,
    );
    log.proxy_subtype = Some(PROXY_SUBTYPE_INBOUND.to_string());
    log.root_task_id = ids.root_task_id.clone();
    log.delegation_depth = ids.depth;
    log.current_task_id = ids.current_task_id.clone();
    log.parent_task_id = ids.parent_task_id.clone();
    // Capability is the dotted name the caller asked us to invoke
    // (e.g. `hello`, `billing.refund`). Stored on
    // `proxy_logs.mcp_capability_name` — same column the outbound
    // writer uses so the admin Agent Usage panel can aggregate
    // across both directions. Without this, inbound rows land with
    // NULL and the panel's per-capability rollup shows
    // "Capability calls: 0" even when requests succeeded.
    log.mcp_capability_name = capability.map(str::to_owned);
    log.resource_group_id = resource_group_id
        .map(str::to_owned)
        .or_else(|| auth_ctx.resolved_group_id.clone());
    log
}

/// The inbound task a refusal answers: its card, its group, its step's ids
/// and the capability it asked for.
struct InboundStep<'a> {
    card_id: &'a str,
    resource_group_id: Option<&'a str>,
    ids: &'a StepLogIds,
    capability: &'a str,
}

/// A gate's refusal of the skill an inbound task is dispatched to.
struct SkillRefusal<'a> {
    status: StatusCode,
    message: &'a str,
    /// The refusal subtype of the gate that said no, when it names one.
    subtype: Option<&'a str>,
    /// An argument policy on the skill's input denied the call.
    argument_policy_denied: bool,
}

/// The answer to an inbound task the skill's gates refuse, and its audit
/// row: the same whether the refusal is met at the skill or foreseen by the
/// route's own gate (`a2a_governance::Outcome::RefusedLater`). A gate's
/// refusal keeps its subtype (data scope, run controls, engine failure), so
/// it is sealed as the verdict it is, not as a failed step; an
/// argument-policy denial is the flagged denial the REST skill route
/// records.
fn inbound_skill_refusal(
    state: &SharedState,
    auth_ctx: &AuthContext,
    correlation_id: &str,
    start: Instant,
    step: InboundStep<'_>,
    refusal: SkillRefusal<'_>,
) -> Response {
    let InboundStep {
        card_id,
        resource_group_id,
        ids,
        capability,
    } = step;
    let SkillRefusal {
        status,
        message,
        subtype,
        argument_policy_denied,
    } = refusal;
    if argument_policy_denied {
        let mut log = build_inbound_log(
            auth_ctx,
            correlation_id,
            Some(card_id),
            resource_group_id,
            status.as_u16(),
            start.elapsed().as_secs_f64() * 1000.0,
            false,
            Some(message),
            ids,
            Some(capability),
        );
        crate::services::proxy_logger::apply_policy_deny(&mut log);
        state
            .background_writer
            .write_proxy_log(state.db.clone(), log);
        return error_response(status, message);
    }
    match subtype {
        Some(subtype) => log_inbound_and_refuse_as(
            state,
            auth_ctx,
            correlation_id,
            start,
            Some(card_id),
            resource_group_id,
            ids,
            status,
            subtype,
            message,
            Some(capability),
        ),
        None => log_inbound_and_fail(
            state,
            auth_ctx,
            correlation_id,
            start,
            Some(card_id),
            resource_group_id,
            ids,
            status,
            message,
            Some(capability),
        ),
    }
}

/// An inbound refusal a gate decided before any handler ran, stamped with
/// the gate's subtype so it is sealed as the verdict it was.
#[allow(clippy::too_many_arguments)]
fn log_inbound_and_refuse_as(
    state: &SharedState,
    auth_ctx: &AuthContext,
    correlation_id: &str,
    start: Instant,
    card_id: Option<&str>,
    resource_group_id: Option<&str>,
    ids: &StepLogIds,
    status: StatusCode,
    subtype: &str,
    message: &str,
    capability: Option<&str>,
) -> Response {
    let mut log = build_inbound_log(
        auth_ctx,
        correlation_id,
        card_id,
        resource_group_id,
        status.as_u16(),
        start.elapsed().as_secs_f64() * 1000.0,
        false,
        Some(message),
        ids,
        capability,
    );
    log.proxy_subtype = Some(subtype.to_string());
    state
        .background_writer
        .write_proxy_log(state.db.clone(), log);
    error_response(status, message)
}

#[allow(clippy::too_many_arguments)]
fn log_inbound_and_fail(
    state: &SharedState,
    auth_ctx: &AuthContext,
    correlation_id: &str,
    start: Instant,
    card_id: Option<&str>,
    resource_group_id: Option<&str>,
    ids: &StepLogIds,
    status: StatusCode,
    message: &str,
    capability: Option<&str>,
) -> Response {
    let duration_ms = start.elapsed().as_secs_f64() * 1000.0;
    write_inbound_proxy_log(
        state,
        auth_ctx,
        correlation_id,
        card_id,
        resource_group_id,
        status.as_u16(),
        duration_ms,
        false,
        Some(message),
        ids,
        capability,
    );
    error_response(status, message)
}

// ---------------------------------------------------------------------------
// Unit tests — dispatch, discovery and wire shapes. The A2A policy is
// tested where it is evaluated (`services::a2a_governance`) and over HTTP
// in `tests/integration/a2a_governance.rs`.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // -----------------------------------------------------------------
    // Inbound handler dispatch — decision logic only, no HTTP.
    // -----------------------------------------------------------------

    fn stub_card() -> AgentCardRow {
        AgentCardRow {
            id: "card-stub".into(),
            tenant_id: "t1".into(),
            name: "billing-agent".into(),
            card_json: json!({
                "name": "billing-agent",
                "description": "test",
                "version": "1.0.0",
                "capabilities": [{"name": "billing.refund"}],
            }),
            trust_tier: "tenant_signed".into(),
            enabled: true,
            revoked_at: None,
            tenant_limits: None,
            tenant_governance: None,
        }
    }

    fn inbound_req(capability: &str, message: Value) -> A2AInboundRequest {
        A2AInboundRequest {
            target_card_id: "card-stub".into(),
            capability: capability.into(),
            message,
            group_id: None,
        }
    }

    #[test]
    fn dispatch_noop_echoes_capability_and_message() {
        let card = stub_card();
        let req = inbound_req("billing.refund", json!({"amount": 100}));
        let (status, body) = dispatch_noop(&card, &req);
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["handler"], "noop");
        assert_eq!(body["card_id"], "card-stub");
        assert_eq!(body["capability"], "billing.refund");
        assert_eq!(body["received_message"]["amount"], 100);
    }

    #[test]
    #[allow(deprecated)]
    fn discovery_response_omits_unset_signature_fields() {
        // serde(skip_serializing_if = "Option::is_none") must hide
        // `signature*` fields for unsigned cards so the public
        // surface stays minimal and predictable for verifiers.
        let resp = A2ACardDiscoveryResponse {
            id: "x".into(),
            tenant_id: "t".into(),
            name: "n".into(),
            version: "1.0.0".into(),
            trust_tier: "unsigned".into(),
            a2a_version: "1.0".into(),
            card: json!({"name": "n"}),
            signature: None,
            signature_algorithm: None,
            signed_by: None,
            signed_at: None,
        };
        let v = serde_json::to_value(&resp).unwrap();
        assert!(v.get("signature").is_none());
        assert!(v.get("signature_algorithm").is_none());
        assert!(v.get("signed_by").is_none());
        assert!(v.get("signed_at").is_none());
        assert_eq!(v["trust_tier"], "unsigned");
        assert_eq!(v["a2a_version"], "1.0");
    }

    #[test]
    #[allow(deprecated)]
    fn discovery_response_includes_signature_when_present() {
        let resp = A2ACardDiscoveryResponse {
            id: "x".into(),
            tenant_id: "t".into(),
            name: "n".into(),
            version: "1.0.0".into(),
            trust_tier: "tenant_signed".into(),
            a2a_version: "1.0".into(),
            card: json!({"name": "n"}),
            signature: Some("sig-b64".into()),
            signature_algorithm: Some("ed25519".into()),
            signed_by: Some("admin-1".into()),
            signed_at: None,
        };
        let v = serde_json::to_value(&resp).unwrap();
        assert_eq!(v["signature"], "sig-b64");
        assert_eq!(v["signature_algorithm"], "ed25519");
        assert_eq!(v["signed_by"], "admin-1");
    }

    #[test]
    fn handler_type_constants_match_python() {
        // Synchronisation guard — the Python model declares
        // HANDLER_TYPE_SKILL="skill" / HANDLER_TYPE_NOOP="noop".
        // If either side changes a string, the inbound route would
        // silently dispatch to the wrong branch. Asserting here keeps
        // both sides honest.
        assert_eq!(HANDLER_TYPE_SKILL, "skill");
        assert_eq!(HANDLER_TYPE_NOOP, "noop");
    }

    #[test]
    fn discovery_router_builds_without_path_conflicts() {
        // `/.well-known/agent-card/{tenant_id}/{card_name}` and
        // `/.well-known/a2a-jwks/{tenant_id}` share the well-known
        // prefix; constructing the router proves axum resolves
        // them without ambiguity.
        let _ = discovery_router();
    }

    #[test]
    fn proxy_subtype_constants_are_stable() {
        // Mission Control queries will key on these strings when
        // rendering the inbound/outbound columns of the delegation
        // graph — silent renames would break the UI.
        assert_eq!(PROXY_SUBTYPE_INBOUND, "inbound");
        assert_eq!(PROXY_SUBTYPE_OUTBOUND, "outbound");
    }

    // -----------------------------------------------------------------
    // Graph-tree identifier construction
    // -----------------------------------------------------------------

    #[test]
    fn step_log_ids_none_are_all_empty() {
        let ids = StepLogIds::none();
        assert!(ids.root_task_id.is_none());
        assert!(ids.current_task_id.is_none());
        assert!(ids.parent_task_id.is_none());
        assert!(ids.depth.is_none());
    }

    #[test]
    fn step_log_ids_at_root_have_no_parent() {
        let root = Ulid::new();
        let ids = StepLogIds::from_step(root, None, root, 0);
        assert_eq!(
            ids.root_task_id.as_deref(),
            Some(root.to_string()).as_deref()
        );
        assert_eq!(
            ids.current_task_id.as_deref(),
            Some(root.to_string()).as_deref()
        );
        assert!(
            ids.parent_task_id.is_none(),
            "root step must have null parent"
        );
        assert_eq!(ids.depth, Some(0));
    }

    #[test]
    fn step_log_ids_mid_chain_record_own_depth_and_parent() {
        // An A2A step that received an inbound chain at depth=2 logs
        // its OWN depth (2), not the downstream's depth (3). This
        // was the semantic bug corrected when StepLogIds was
        // introduced.
        let root = Ulid::new();
        let parent = Ulid::new();
        let me = Ulid::new();
        let ids = StepLogIds::from_step(me, Some(parent), root, 2);
        assert_eq!(
            ids.root_task_id.as_deref(),
            Some(root.to_string()).as_deref()
        );
        assert_eq!(
            ids.current_task_id.as_deref(),
            Some(me.to_string()).as_deref()
        );
        assert_eq!(
            ids.parent_task_id.as_deref(),
            Some(parent.to_string()).as_deref()
        );
        assert_eq!(ids.depth, Some(2));
    }

    #[test]
    fn step_log_ids_distinguish_root_from_descendant_at_depth_zero() {
        // Paranoia guard: at the root, parent_task_id MUST be null
        // even though depth=0. Confusing these lets Mission Control
        // draw a self-loop edge which renders as a broken tree.
        let me = Ulid::new();
        let root_ids = StepLogIds::from_step(me, None, me, 0);
        assert!(root_ids.parent_task_id.is_none());

        // Contrast — if somehow a depth=0 row appeared with a parent,
        // the fields carry the parent through so the operator sees
        // the inconsistency instead of it being silently normalised.
        let fake_parent = Ulid::new();
        let odd_ids = StepLogIds::from_step(me, Some(fake_parent), me, 0);
        assert_eq!(
            odd_ids.parent_task_id.as_deref(),
            Some(fake_parent.to_string()).as_deref()
        );
    }

    // -- Outbound dialect selection -----------------------------------------

    #[test]
    fn pick_outbound_prefers_v1_jsonrpc_interface() {
        let card = json!({
            "supportedInterfaces": [
                {"url": "https://x/rpc", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
                {"url": "https://x/rest", "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"},
                {"url": "https://x/legacy", "protocolBinding": "JSONRPC", "protocolVersion": "0.3"}
            ]
        });
        let (url, dialect) = pick_outbound_target(&card).unwrap();
        assert_eq!(url, "https://x/rpc");
        assert_eq!(dialect, OutboundDialect::V1);
    }

    #[test]
    fn pick_outbound_falls_back_to_v03_interface() {
        let card = json!({
            "supportedInterfaces": [
                {"url": "https://x/legacy", "protocolBinding": "JSONRPC", "protocolVersion": "0.3"}
            ]
        });
        let (url, dialect) = pick_outbound_target(&card).unwrap();
        assert_eq!(url, "https://x/legacy");
        assert_eq!(dialect, OutboundDialect::V03);
    }

    #[test]
    fn pick_outbound_speaks_0_3_to_a_card_with_only_a_top_level_url() {
        let card = json!({ "url": "https://peer03.example/agent" });
        let (url, dialect) = pick_outbound_target(&card).unwrap();
        assert_eq!(url, "https://peer03.example/agent");
        assert_eq!(dialect, OutboundDialect::V03);
    }

    #[test]
    fn pick_outbound_none_when_card_has_nothing_usable() {
        let card = json!({ "supportedInterfaces": [] });
        assert!(pick_outbound_target(&card).is_none());
        let empty = json!({});
        assert!(pick_outbound_target(&empty).is_none());
    }

    #[test]
    fn compose_outbound_url_v1_appends_method_suffix() {
        assert_eq!(
            compose_outbound_url("https://x/agent", OutboundDialect::V1),
            "https://x/agent/message:send",
        );
    }

    #[test]
    fn compose_outbound_url_v1_idempotent_when_base_already_ends_with_suffix() {
        let same = "https://x/agent/message:send";
        assert_eq!(compose_outbound_url(same, OutboundDialect::V1), same,);
    }

    #[test]
    fn compose_outbound_url_v03_does_not_change_path() {
        assert_eq!(
            compose_outbound_url("https://peer/agent", OutboundDialect::V03),
            "https://peer/agent",
        );
    }

    #[test]
    fn build_outbound_body_v1_wraps_plain_message_in_envelope() {
        let msg = json!({"messageId": "m1", "role": "ROLE_USER", "parts": []});
        let body = build_outbound_body(OutboundDialect::V1, "billing.refund", &msg);
        assert_eq!(body, json!({"message": msg}));
    }

    #[test]
    fn build_outbound_body_v1_passes_preformed_envelope_through() {
        // Caller already shaped the envelope themselves — don't double-wrap.
        let envelope = json!({
            "message": {"messageId": "m1", "role": "ROLE_USER", "parts": []},
            "configuration": {"returnImmediately": true},
        });
        let body = build_outbound_body(OutboundDialect::V1, "unused", &envelope);
        assert_eq!(body, envelope);
    }

    #[test]
    fn build_outbound_body_v03_uses_capability_and_message_shape() {
        let msg = json!({"any": "json"});
        let body = build_outbound_body(OutboundDialect::V03, "billing.refund", &msg);
        assert_eq!(
            body,
            json!({"capability": "billing.refund", "message": msg})
        );
    }
}
