// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Request helpers: who is calling, and from where
//!
//! Community helpers used by every surface that serves a person (files,
//! images, audio, video, approvals, run feedback, the portal routes) and by
//! every surface that logs a caller's address (LLM, MCP, A2A, batch).
//! One copy, where the portal, the route helpers and `mcp.rs` each had their
//! own: every surface resolves the address the same way.
//!
//! Two answers to "who is calling":
//!
//! * [`extract_portal_user`] — a person: the portal session token, the only
//!   human credential the gateway knows. Its verifier is installed by the
//!   Portal extension; a Community gateway has none, so the routes that ask
//!   for a person (approve, reject, run feedback) answer 401 there.
//! * [`extract_media_caller`] — the caller of a file, image, audio or video
//!   route: an API key, governed as on the chat
//!   routes, or a portal session where the Portal is composed.

use std::net::SocketAddr;

use axum::Json;
use axum::extract::{ConnectInfo, FromRequestParts};
use axum::http::request::Parts;
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use serde_json::json;

use crate::middleware::tenant::TenantContext;
use crate::models::auth::{AuthContext, AuthType};
use crate::services::SharedState;
use crate::services::auth;
use crate::services::credential_verifier::TOKEN_TYPE_PORTAL;
use crate::services::trusted_proxies::TrustedProxies;

/// Standard error response helper. Adds the RFC 6750 §3 Bearer
/// challenge on 401 so SDKs know which scheme to retry with.
fn error(status: StatusCode, msg: &str) -> Response {
    let mut resp = (status, Json(json!({"error": msg}))).into_response();
    if status == StatusCode::UNAUTHORIZED {
        crate::services::error_envelope::attach_bearer_challenge(&mut resp);
    }
    resp
}

/// A refusal that says in a machine-readable `code`, beside the `error`
/// sentence, what was refused: `{"error": "<sentence>", "code": "<code>"}`.
/// A 401 carries the RFC 6750 §3 Bearer challenge. The one place a coded
/// refusal of the person-serving routes is built.
pub fn coded_refusal(status: StatusCode, code: &str, msg: &str) -> Response {
    let mut resp = (status, Json(json!({"error": msg, "code": code}))).into_response();
    if status == StatusCode::UNAUTHORIZED {
        crate::services::error_envelope::attach_bearer_challenge(&mut resp);
    }
    resp
}

/// Why a route that serves a signed-in person refuses the session a request
/// carries. A client reads the `code` to tell a session that is over (sign
/// in again) from a refusal that is about something else: a 401 with none
/// of these codes is not about the session.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionRefusal {
    /// The token's expiry has passed.
    Expired,
    /// The session was signed out.
    Revoked,
    /// The session was issued before its user's cutoff: a password change,
    /// or an administrator's cutoff.
    Ended,
    /// The credential is not a portal session (another kind of session
    /// token, an API key).
    NotAPortalSession,
    /// The session's account is deactivated.
    AccountInactive,
    /// The session is of another tenant than the request's.
    TenantMismatch,
    /// The session's account does not exist in the request's tenant.
    AccountNotFound,
    /// No session, or a token that is not one this gateway issued.
    Invalid,
}

impl SessionRefusal {
    /// Every refusal, for the tests that pin the codes.
    pub const ALL: [SessionRefusal; 8] = [
        Self::Expired,
        Self::Revoked,
        Self::Ended,
        Self::NotAPortalSession,
        Self::AccountInactive,
        Self::TenantMismatch,
        Self::AccountNotFound,
        Self::Invalid,
    ];

    /// The machine-readable code. A credential that is not a session of
    /// the type the route serves is `not_a_<type>_session`, named from the
    /// token type (`TOKEN_TYPE_PORTAL`).
    pub fn code(self) -> std::borrow::Cow<'static, str> {
        match self {
            Self::Expired => "session_expired".into(),
            Self::Revoked => "session_revoked".into(),
            Self::Ended => "session_ended".into(),
            Self::NotAPortalSession => format!("not_a_{TOKEN_TYPE_PORTAL}_session").into(),
            Self::AccountInactive => "account_inactive".into(),
            Self::TenantMismatch => "tenant_mismatch".into(),
            Self::AccountNotFound => "account_not_found".into(),
            Self::Invalid => "session_invalid".into(),
        }
    }

    pub fn status(self) -> StatusCode {
        match self {
            Self::Expired
            | Self::Revoked
            | Self::Ended
            | Self::NotAPortalSession
            | Self::Invalid => StatusCode::UNAUTHORIZED,
            Self::AccountInactive | Self::TenantMismatch => StatusCode::FORBIDDEN,
            Self::AccountNotFound => StatusCode::NOT_FOUND,
        }
    }

    /// The refusal an authentication error is, when it is about the session.
    /// `None` for an error that says nothing about it (a lookup that failed,
    /// a caller no group governs): those are answered without a code.
    pub fn of(e: &auth::AuthError) -> Option<Self> {
        use auth::AuthError;
        match e {
            AuthError::ExpiredJwt => Some(Self::Expired),
            AuthError::SessionRevoked(_) => Some(Self::Revoked),
            AuthError::SessionEnded(_) => Some(Self::Ended),
            AuthError::NotASessionOfType(_) => Some(Self::NotAPortalSession),
            AuthError::TenantMismatch => Some(Self::TenantMismatch),
            AuthError::Unauthenticated | AuthError::InvalidJwt(_) | AuthError::InvalidApiKey(_) => {
                Some(Self::Invalid)
            }
            AuthError::UnboundKey(_)
            | AuthError::GroupNotMember
            | AuthError::AgentNotBound
            | AuthError::GroupRequired
            | AuthError::CredentialAssuranceInsufficient(_)
            | AuthError::Internal(_)
            | AuthError::Unavailable(_) => None,
        }
    }

    /// The answer: this refusal's status and code, with `msg` as the
    /// sentence.
    pub fn response(self, msg: &str) -> Response {
        coded_refusal(self.status(), &self.code(), msg)
    }
}

/// The request names no tenant and carries no token that names one.
pub const NO_TENANT_OR_SESSION: &str =
    "Missing or invalid credentials. Provide X-Tenant-ID header or a valid bearer token.";

/// The answer to an authentication error on a route that serves a signed-in
/// person: the error's status and sentence, with the session refusal's code
/// when the error is one ([`SessionRefusal::of`]).
pub fn session_auth_refusal(e: &auth::AuthError) -> Response {
    match SessionRefusal::of(e) {
        Some(refusal) => coded_refusal(e.status_code(), &refusal.code(), &e.to_string()),
        None => error(e.status_code(), &e.to_string()),
    }
}

/// The caller's IP address, as `BRUTOR_TRUSTED_PROXIES` decides it
/// ([`TrustedProxies::client_ip`]): the peer's address, or the right-most
/// untrusted `X-Forwarded-For` entry when the peer is a trusted proxy. `None`
/// without a peer (a request that did not come over a connection).
pub fn extract_client_ip(
    headers: &HeaderMap,
    peer: Option<SocketAddr>,
    trusted: &TrustedProxies,
) -> Option<String> {
    peer.map(|p| trusted.client_ip(headers, p.ip()).to_string())
}

/// The caller's IP address of a request: the extractor every handler takes
/// for its audit rows, proxy log, run and agent identities and logs, and
/// the only reader of the peer address (`tests/trusted_proxies_guard.rs`).
/// It never refuses: without a peer, the address is `None`.
#[derive(Debug, Clone, Default)]
pub struct ClientIp(Option<String>);

impl ClientIp {
    /// The address, for an `AuthContext` or a log row.
    pub fn ip(&self) -> Option<String> {
        self.0.clone()
    }
}

impl FromRequestParts<SharedState> for ClientIp {
    type Rejection = std::convert::Infallible;

    async fn from_request_parts(
        parts: &mut Parts,
        state: &SharedState,
    ) -> Result<Self, Self::Rejection> {
        let peer = parts
            .extensions
            .get::<ConnectInfo<SocketAddr>>()
            .map(|ConnectInfo(peer)| *peer);
        Ok(Self(extract_client_ip(
            &parts.headers,
            peer,
            &state.config.trusted_proxies,
        )))
    }
}

/// For portal endpoints, promote `Authorization: Bearer <token>` to
/// `X-Gateway-Authorization` when the latter is absent. This lets end-user
/// frontends use the standard Authorization header while the auth waterfall
/// still recognises the token as a Gateway JWT.
pub fn portal_headers(headers: &HeaderMap) -> HeaderMap {
    if headers.contains_key("x-gateway-authorization") {
        return headers.clone();
    }
    if let Some(auth_val) = headers.get("authorization") {
        let mut h = headers.clone();
        h.insert("x-gateway-authorization", auth_val.clone());
        return h;
    }
    headers.clone()
}

// ============================================================================
// Portal user authentication
// ============================================================================

/// Authenticate a portal user from request headers and return
/// `(tenant_id, user_id, username)`.
///
/// Checks:
/// - Valid tenant context
/// - Valid Gateway JWT
/// - User exists and is active
pub async fn extract_portal_user(
    state: &SharedState,
    headers: &HeaderMap,
    tenant: &Option<axum::Extension<TenantContext>>,
    client_ip: Option<String>,
) -> Result<(String, String, String), Response> {
    let (tenant_id, auth_ctx) = authenticate_caller(state, headers, tenant, client_ip).await?;
    portal_user_of(state, headers, tenant_id, auth_ctx).await
}

/// The request's tenant and its authenticated caller, whatever kind of
/// credential it carries. Not yet a decision that the route serves them.
async fn authenticate_caller(
    state: &SharedState,
    headers: &HeaderMap,
    tenant: &Option<axum::Extension<TenantContext>>,
    client_ip: Option<String>,
) -> Result<(String, AuthContext), Response> {
    let tenant_id = match tenant {
        Some(t) => t.tenant_id.clone(),
        None => return Err(SessionRefusal::Invalid.response(NO_TENANT_OR_SESSION)),
    };

    let effective_headers = portal_headers(headers);

    let auth_ctx = auth::authenticate_request(
        &effective_headers,
        &tenant_id,
        &state.config.jwt_secret,
        &state.db,
        &state.credential_verifiers,
        client_ip,
        true,
    )
    .await
    .map_err(|e| session_auth_refusal(&e))?;
    Ok((tenant_id, auth_ctx))
}

pub const ACCOUNT_INACTIVE: &str = "User account is inactive";
pub const ACCOUNT_NOT_FOUND: &str = "User not found";

/// The end user id of the portal session `auth_ctx` was made from, or the
/// refusal: the credential must be a session token, of the portal type, that
/// names its user. Whether that account exists and is active is the
/// caller's next question.
pub fn portal_user_id(auth_ctx: &AuthContext) -> Result<String, Response> {
    if auth_ctx.auth_type != AuthType::GatewayJwt {
        return Err(
            SessionRefusal::NotAPortalSession.response("Gateway JWT authentication required")
        );
    }
    auth::require_token_type(auth_ctx, TOKEN_TYPE_PORTAL).map_err(|e| session_auth_refusal(&e))?;
    auth_ctx
        .gateway_user_id
        .clone()
        .ok_or_else(|| SessionRefusal::Invalid.response("No user identity in token"))
}

/// The portal user an authenticated caller is, or the refusal.
async fn portal_user_of(
    state: &SharedState,
    headers: &HeaderMap,
    tenant_id: String,
    auth_ctx: AuthContext,
) -> Result<(String, String, String), Response> {
    // Internal health-check probes from the control plane carry a
    // global-scope API key + the `X-Brutor-Internal-Health-Check`
    // header. Accept those as a synthetic "internal" portal user
    // so the multimodal routes can finish a probe even though the
    // probe doesn't carry a JWT (it can't — there's no end-user
    // session behind a control-plane health check). Only honoured
    // when both flags line up; a browser-origin request can't
    // forge `X-Brutor-Internal-Health-Check` to gain access
    // because the global key is required.
    // Only the platform service key (a tenant admin cannot mint one).
    let is_health_probe =
        auth_ctx.api_key_is_platform && headers.get("x-brutor-internal-health-check").is_some();
    if is_health_probe {
        return Ok((tenant_id, "0".to_string(), "internal-probe".to_string()));
    }

    let user_id = portal_user_id(&auth_ctx)?;
    let username = auth_ctx.gateway_username.unwrap_or_default();

    // Check user is active. A lookup error refuses (503); it used to read
    // as "no such user".
    let user_active: Option<(bool,)> =
        sqlx::query_as("SELECT is_active FROM end_users WHERE id = $1::int AND tenant_id = $2")
            .bind(&user_id)
            .bind(&tenant_id)
            .fetch_optional(&state.db)
            .await
            .map_err(|e| {
                tracing::error!(error = %e, %tenant_id, "portal user lookup failed");
                error(
                    StatusCode::SERVICE_UNAVAILABLE,
                    "User lookup is unavailable; try again",
                )
            })?;

    match user_active {
        Some((false,)) => return Err(SessionRefusal::AccountInactive.response(ACCOUNT_INACTIVE)),
        None => return Err(SessionRefusal::AccountNotFound.response(ACCOUNT_NOT_FOUND)),
        _ => {}
    }

    Ok((tenant_id, user_id, username))
}

/// The workload agent an authenticated caller is, or the refusal.
///
/// `auth::authenticate_request` made the context from a token it verified
/// against an identity provider of the request's tenant (signature, issuer,
/// expiry, audience) and resolved to an enabled, active, unrevoked agent of
/// that tenant; a token of another tenant's provider, an expired or altered
/// one, and one of an unknown or disabled agent never get here (401).
///
/// The agent acts in its own resource group — the one authentication
/// resolved (`auth::agent_acting_group`): the member group it named, else its
/// only one; an agent in no group, or in several that named none, was
/// refused there (403) — and that group is the actor the route
/// counts the call against, as a group key's is.
/// On a route that names a model the preflight that follows decides the
/// rest from the same token, as on chat: model access through the group,
/// the agent's grants, the AI System's autonomy, limits, guardrails.
fn workload_agent_of(
    tenant_id: String,
    auth_ctx: AuthContext,
) -> Result<(String, String, String), Response> {
    let Some(agent_id) = auth_ctx.actor_agent_id else {
        return Err(error(StatusCode::UNAUTHORIZED, "The token names no agent"));
    };
    // Authentication made no workload context without its acting group; a
    // context without one is refused as authentication refuses it.
    let Some(group) = auth_ctx.resolved_group_id else {
        return Err(session_auth_refusal(&auth::AuthError::AgentNotBound));
    };
    Ok((tenant_id, group, agent_id))
}

// ============================================================================
// The caller of a file, image, audio or video route
// ============================================================================

/// Whether the route goes on to the LLM preflight
/// (`multimodal_pipeline::multimodal_preflight`), which authenticates the
/// same request again.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Preflight {
    /// The route names a model: the preflight follows and decides model
    /// access, quotas and guardrails for this caller, as on a chat route.
    Follows,
    /// The route names no model (files, a video job's status): this is the
    /// only authentication of the request.
    None,
}

/// Authenticate the caller of a file, image, audio (speech, transcription)
/// or video route and return `(tenant_id, actor_id, name)`.
///
/// Accepted:
///
/// * **an API key** that `auth::authenticate_request` accepts — enabled, not
///   revoked, not expired, bound to an active resource group (or the
///   platform service key), and a key of the request's tenant. The actor is
///   the key's end user when it has one, otherwise its resource group: the
///   identity the chat routes count the call against. Everything a chat
///   route decides for a key (model access through the key's group, every
///   limit, guardrails, the audit row) is decided by the preflight that
///   follows, from the same credential;
/// * **a portal session** of an active end user, where the Portal is
///   composed ([`extract_portal_user`], unchanged);
/// * the platform's health probe, as before.
///
/// * **a workload-identity token**: an agent's own
///   IdP-issued token, verified against an identity provider of the
///   request's tenant ([`workload_agent_of`]), as on the chat routes.
///
/// Refused (401): every other credential — an administrator's or a service's
/// session token, no credential — and a key whose tenant is not the
/// request's: the tenant that owns the key. Another tenant's key is never
/// handed this tenant's files or jobs.
pub async fn extract_media_caller(
    state: &SharedState,
    headers: &HeaderMap,
    tenant: &Option<axum::Extension<TenantContext>>,
    client_ip: Option<String>,
    preflight: Preflight,
) -> Result<(String, String, String), Response> {
    let (tenant_id, mut auth_ctx) = authenticate_caller(state, headers, tenant, client_ip).await?;
    // The credential against the call's identity policy, for every route that
    // authenticates its caller here (files, images, audio, video).
    if let Err(e) = crate::services::identity_policy::judge_call(state, &mut auth_ctx).await {
        return Err(crate::services::identity_policy::refusal_response(
            &e,
            session_auth_refusal(&e),
        )
        .await);
    }
    if auth_ctx.auth_type == AuthType::IdpOauth {
        return workload_agent_of(tenant_id, auth_ctx);
    }
    if auth_ctx.auth_type != AuthType::ApiKey {
        return portal_user_of(state, headers, tenant_id, auth_ctx).await;
    }
    if auth_ctx.tenant_id != tenant_id {
        tracing::warn!(
            api_key_id = ?auth_ctx.api_key_id,
            key_tenant = %auth_ctx.tenant_id,
            requested_tenant = %tenant_id,
            "API key refused on a media route: not a key of the request's tenant"
        );
        return Err(error(
            StatusCode::UNAUTHORIZED,
            "API key is not valid for this tenant",
        ));
    }
    // The key must be the request's tenant's own. `auth_ctx.tenant_id` is the
    // tenant the key ACTS for: a shared (`global`-scope) key of another
    // tenant acts for whichever tenant the request names unless the operator
    // pinned it. On a chat route such a key is then refused at model access
    // (its group is the other tenant's). A file or a video job is held to
    // its tenant and nothing else, so here the key's owner is checked. The
    // platform service key (seeded by the control plane, never minted by a
    // tenant) serves every tenant, as it does on the chat routes.
    if !auth_ctx.api_key_is_platform {
        let key_id = auth_ctx.api_key_id.as_deref().unwrap_or_default();
        let owner: Option<String> =
            sqlx::query_scalar("SELECT tenant_id FROM api_keys WHERE id = $1")
                .bind(key_id)
                .fetch_optional(&state.db)
                .await
                .map_err(|e| {
                    tracing::error!(error = %e, %tenant_id, "API key owner lookup failed");
                    error(
                        StatusCode::SERVICE_UNAVAILABLE,
                        "API key lookup is unavailable; try again",
                    )
                })?;
        if owner.as_deref() != Some(tenant_id.as_str()) {
            tracing::warn!(
                api_key_id = %key_id,
                requested_tenant = %tenant_id,
                "API key refused on a media route: a key of another tenant"
            );
            return Err(error(
                StatusCode::UNAUTHORIZED,
                "API key is not valid for this tenant",
            ));
        }
    }
    // The preflight authenticates this request again and counts the key's
    // use there; this authentication is the same use, not a second one.
    if preflight == Preflight::Follows {
        if let Some(key_id) = auth_ctx.api_key_id.as_deref() {
            crate::services::api_key_usage::uncount(key_id);
        }
    }
    // The key's end user when it has one, otherwise its resource group (the
    // platform service key, which has neither, is itself).
    let actor_id = match auth_ctx.gateway_user_id.clone() {
        Some(id) => id,
        None => {
            return Err(error(
                StatusCode::UNAUTHORIZED,
                "API key names no end user and no resource group",
            ));
        }
    };
    let name = auth_ctx.gateway_username.unwrap_or_default();
    Ok((tenant_id, actor_id, name))
}

/// Get the resource group IDs a user belongs to.
///
/// Unions **direct** membership (`resource_group_memberships`)
/// with **EndUserGroup-mediated** access (`resource_group_end_user_groups`
/// → `end_user_group_members`). Without the second edge a user whose only
/// access is via an end-user group sees none of the group's resources in
/// the portal. Mirrors `load_user_groups`.
/// A database error is returned, never "member of no group".
pub async fn get_user_group_ids(
    db: &sqlx::PgPool,
    user_id: &str,
    tenant_id: &str,
) -> Result<Vec<String>, sqlx::Error> {
    let rows: Vec<(String,)> = sqlx::query_as(
        r#"SELECT rg.id
           FROM resource_groups rg
           WHERE rg.tenant_id = $2
             AND rg.id IN (
                 SELECT group_id
                 FROM resource_group_memberships
                 WHERE user_id = $1::int
                   AND tenant_id = $2
                 UNION
                 SELECT rgeug.resource_group_id
                 FROM resource_group_end_user_groups rgeug
                 JOIN end_user_group_members eugm
                   ON eugm.end_user_group_id = rgeug.end_user_group_id
                 WHERE eugm.end_user_id = $1::int
             )"#,
    )
    .bind(user_id)
    .bind(tenant_id)
    .fetch_all(db)
    .await?;

    Ok(rows.into_iter().map(|(gid,)| gid).collect())
}
