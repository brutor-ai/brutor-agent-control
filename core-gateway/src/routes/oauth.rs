// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # OAuth Proxy Routes
//!
//! Implements a "Mirror Proxy" for OAuth 2.0 and OpenID Connect (OIDC) flows.
//!
//! ## Why an OAuth Proxy?
//!
//! MCP servers that require OAuth authentication (e.g. GitHub, Google, Slack)
//! need users to complete an OAuth flow before the proxy can call them with
//! a valid access token. The OAuth proxy sits between the MCP client and the
//! Identity Provider (IdP) to solve two problems:
//!
//! ### 1. Discovery URL Rewriting
//!
//! Many auth servers return incomplete or missing `.well-known` documents
//! (e.g. no `oauth-protected-resource` metadata, missing endpoints in
//! `openid-configuration`). The proxy rewrites these discovery documents
//! so that all URLs point through the gateway, ensuring the OAuth
//! flow works reliably regardless of IdP quirks. This makes life much
//! easier for MCP clients — they always get well-formed discovery docs
//! with working endpoints.
//!
//! ### 2. Access Token Interception & Gateway JWT Issuance
//!
//! The real access token from the IdP **never reaches the client**. During
//! the token exchange (`authorization_code` → token), the proxy:
//!     1. Exchanges the auth code with the IdP (server-side)
//!     2. Encrypts and stores the access token + refresh token in the DB
//!        (`oauth_tokens` table, encrypted via `EncryptionService`)
//!     3. Issues a **Gateway JWT** back to the client instead
//!
//! The Gateway JWT is a short-lived, Brutor-signed token that the client
//! uses for subsequent MCP requests. When the MCP proxy receives a request
//! with a Gateway JWT, it looks up the stored OAuth token and injects it
//! into the upstream request. This means:
//!     - The real access token stays server-side (never exposed to browser/client)
//!     - Token refresh happens transparently (the proxy auto-refreshes expired tokens)
//!     - The client only ever sees the Gateway JWT
//!
//! ## Endpoints
//!
//! ```text
//! GET  /v1/proxy/oauth/{mcp_server_id}/{idp_server_id}/.well-known/oauth-protected-resource
//!      → Rewritten OAuth Protected Resource metadata
//!
//! GET  /v1/proxy/oauth/{mcp_server_id}/{idp_server_id}/.well-known/openid-configuration
//!      → Rewritten OpenID Connect discovery document
//!
//! POST /v1/proxy/oauth/{mcp_server_id}/{idp_server_id}/token
//!      → Token exchange: auth code → stored access token + Gateway JWT to client
//! ```
//!
//! ## Key Features
//!
//! - **Discovery Rewriting**: Dynamically modifies `.well-known` metadata to
//!   route all OAuth traffic through the gateway
//! - **Token Exchange**: Handles the `authorization_code` flow entirely within
//!   Rust — exchanges with IdP, stores tokens, returns Gateway JWT
//! - **Idempotency**: Uses Redis-backed locking to prevent duplicate token
//!   exchange attempts (race condition from retry-happy clients)
//! - **Universal Identity**: Normalizes user profile data from disparate
//!   Identity Providers (Google, GitHub, Microsoft, etc.) into a consistent
//!   format for the Gateway JWT claims
//!
//! ## Flow Diagram
//!
//! ```text
//! MCP Client                    Core Gateway                    IdP (GitHub, etc.)
//!     │                              │                               │
//!     │  GET .well-known/*           │                               │
//!     │─────────────────────────────>│  GET .well-known/*            │
//!     │                              │──────────────────────────────>│
//!     │                              │  (original discovery doc)     │
//!     │  (rewritten URLs)            │<──────────────────────────────│
//!     │<─────────────────────────────│                               │
//!     │                              │                               │
//!     │  (user completes OAuth       │                               │
//!     │   consent in browser)        │                               │
//!     │                              │                               │
//!     │  POST /token (auth_code)     │                               │
//!     │─────────────────────────────>│  POST /token (auth_code)      │
//!     │                              │──────────────────────────────>│
//!     │                              │  (access_token, refresh_token)│
//!     │                              │<──────────────────────────────│
//!     │                              │  encrypt + store tokens       │
//!     │  (Gateway JWT only)          │                               │
//!     │<─────────────────────────────│                               │
//!     │                              │                               │
//!     │  MCP request + Gateway JWT   │                               │
//!     │─────────────────────────────>│  inject stored access_token   │
//!     │                              │  ──> upstream MCP server      │
//! ```

use std::time::Instant;

use axum::Router;
use axum::body::Body;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Json, Response};
use axum::routing::{get, post};
use serde_json::{Value, json};

use crate::middleware::tenant::TenantContext;
use crate::models::db::{IdpServer, McpServer, OAuthAppClient};
use crate::services::oauth_token_manager::OAuthTokenManager;
use crate::services::proxy_logger;
use crate::services::{SharedState, auth};

pub fn router() -> Router<SharedState> {
    Router::new()
        .route(
            "/{mcp_server_id}/{idp_server_id}/.well-known/oauth-protected-resource",
            get(oauth_protected_resource),
        )
        .route(
            "/{mcp_server_id}/{idp_server_id}/.well-known/openid-configuration",
            get(openid_configuration),
        )
        .route(
            "/{mcp_server_id}/{idp_server_id}/token",
            post(token_exchange),
        )
        .route(
            "/{mcp_server_id}/{idp_server_id}/revoke",
            post(revoke_token),
        )
        .route("/{mcp_server_id}/{idp_server_id}/tokens", get(list_tokens))
        .route(
            "/{mcp_server_id}/{idp_server_id}/register",
            post(register_client),
        )
        .route("/{mcp_server_id}/{idp_server_id}/authorize", get(authorize))
        .route(
            "/{mcp_server_id}/{idp_server_id}/callback",
            get(cimd_callback),
        )
}

#[utoipa::path(
    get,
    path = "/v1/proxy/oauth/{mcp_server_id}/{idp_server_id}/.well-known/oauth-protected-resource",
    tag = "OAuth",
    summary = "OAuth Protected Resource metadata",
    description = "Returns OAuth discovery metadata with gateway-rewritten URLs.",
    params(
        ("mcp_server_id" = String, Path, description = "MCP server ID"),
        ("idp_server_id" = String, Path, description = "Identity Provider ID"),
    ),
    responses(
        (status = 200, description = "OAuth protected resource metadata"),
        (status = 404, description = "Server or IdP not found"),
    )
)]
pub(crate) async fn oauth_protected_resource(
    State(state): State<SharedState>,
    tenant: Option<axum::Extension<TenantContext>>,
    Path((mcp_server_id, idp_server_id)): Path<(String, String)>,
) -> Response {
    let tenant_id = match extract_tenant_id(&state, tenant, &mcp_server_id).await {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    // Load MCP server
    let mcp_server = match load_mcp_server(&state, &mcp_server_id, &tenant_id).await {
        Ok(s) => s,
        Err(resp) => return resp,
    };

    // Load IdP
    let idp = match load_idp(&state, &idp_server_id, &tenant_id).await {
        Ok(s) => s,
        Err(resp) => return resp,
    };

    // Build gateway base URL from the request
    let gateway_base = gateway_base(&state);

    // Build discovery response with rewritten URLs
    let response = json!({
        "resource": mcp_server.base_url,
        "authorization_servers": [
            format!("{gateway_base}/v1/proxy/oauth/{mcp_server_id}/{idp_server_id}")
        ],
        "bearer_methods_supported": ["header"],
        "scopes_supported": idp.scopes_supported.clone().unwrap_or(json!(["openid", "profile", "email"])),
    });

    Json(response).into_response()
}

#[utoipa::path(
    get,
    path = "/v1/proxy/oauth/{mcp_server_id}/{idp_server_id}/.well-known/openid-configuration",
    tag = "OAuth",
    summary = "OpenID Connect discovery",
    description = "Returns OIDC discovery metadata with token_endpoint rewritten to the gateway.",
    params(
        ("mcp_server_id" = String, Path, description = "MCP server ID"),
        ("idp_server_id" = String, Path, description = "Identity Provider ID"),
    ),
    responses(
        (status = 200, description = "OIDC configuration"),
        (status = 404, description = "Server or IdP not found"),
    )
)]
pub(crate) async fn openid_configuration(
    State(state): State<SharedState>,
    tenant: Option<axum::Extension<TenantContext>>,
    Path((mcp_server_id, idp_server_id)): Path<(String, String)>,
) -> Response {
    let tenant_id = match extract_tenant_id(&state, tenant, &mcp_server_id).await {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    let _mcp_server = match load_mcp_server(&state, &mcp_server_id, &tenant_id).await {
        Ok(s) => s,
        Err(resp) => return resp,
    };

    let idp = match load_idp(&state, &idp_server_id, &tenant_id).await {
        Ok(s) => s,
        Err(resp) => return resp,
    };

    let gateway_base = gateway_base(&state);
    let gateway_issuer = format!("{gateway_base}/v1/proxy/oauth/{mcp_server_id}/{idp_server_id}");

    // Start with cached IdP config if available, or build from stored endpoints
    let mut config = idp
        .cached_config
        .clone()
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default();

    // Rewrite endpoints through gateway
    config.insert("issuer".into(), Value::String(gateway_issuer.clone()));
    config.insert(
        "token_endpoint".into(),
        Value::String(format!("{gateway_issuer}/token")),
    );

    // RFC 9207 / SEP-2468: we rewrite `issuer` to
    // the gateway facade, but `authorization_endpoint` still points at
    // the real IdP, whose authorization responses carry the IdP's own
    // `iss`. A client validating RFC 9207 against our advertised
    // issuer would therefore hard-fail. Until the authorize endpoint
    // is proxied too (future work), we must not advertise support:
    // strip the flag so spec-compliant clients don't enforce a check
    // that cannot succeed against this facade.
    config.insert(
        "authorization_response_iss_parameter_supported".into(),
        Value::Bool(false),
    );

    // Authorize now runs through the gateway (the CIMD bridge):
    // registered clients are 302-passed straight to the IdP (behavior
    // unchanged); CIMD URL client ids are bridged. Only advertise it
    // when the IdP actually has an authorize endpoint to land on.
    if idp
        .authorization_endpoint
        .as_deref()
        .is_some_and(|e| !e.is_empty())
    {
        config.insert(
            "authorization_endpoint".into(),
            Value::String(format!("{gateway_issuer}/authorize")),
        );
        // CIMD requires the bridge (URL client ids mean nothing to the
        // IdP) and the bridge requires DCR upstream for its own client.
        config.insert(
            "client_id_metadata_document_supported".into(),
            Value::Bool(
                idp.registration_endpoint
                    .as_deref()
                    .is_some_and(|e| !e.is_empty()),
            ),
        );
    }
    if let Some(ref jwks) = idp.jwks_uri {
        config.insert("jwks_uri".into(), Value::String(jwks.clone()));
    }
    if let Some(ref userinfo) = idp.userinfo_endpoint {
        config.insert("userinfo_endpoint".into(), Value::String(userinfo.clone()));
    }

    // Standard OIDC fields
    config
        .entry("response_types_supported".to_string())
        .or_insert(json!(["code"]));
    config
        .entry("grant_types_supported".to_string())
        .or_insert(json!(["authorization_code", "refresh_token"]));
    config
        .entry("token_endpoint_auth_methods_supported".to_string())
        .or_insert(json!(["client_secret_post", "none"]));
    config
        .entry("code_challenge_methods_supported".to_string())
        .or_insert(json!(["S256"]));

    if let Some(ref scopes) = idp.scopes_supported {
        config
            .entry("scopes_supported".to_string())
            .or_insert(scopes.clone());
    }

    // Include registration endpoint (rewritten through gateway) if IdP supports DCR
    if let Some(ref _reg_ep) = idp.registration_endpoint {
        config.insert(
            "registration_endpoint".into(),
            Value::String(format!("{gateway_issuer}/register")),
        );
    }

    Json(Value::Object(config)).into_response()
}

#[utoipa::path(
    post,
    path = "/v1/proxy/oauth/{mcp_server_id}/{idp_server_id}/token",
    tag = "OAuth",
    summary = "OAuth token exchange",
    description = "Exchanges an authorization code for tokens, stores encrypted tokens, and returns an MCP JWT.",
    params(
        ("mcp_server_id" = String, Path, description = "MCP server ID"),
        ("idp_server_id" = String, Path, description = "Identity Provider ID"),
    ),
    responses(
        (status = 200, description = "Token exchange result with MCP JWT"),
        (status = 400, description = "Invalid request or grant type"),
        (status = 404, description = "Server, IdP, or OAuth client not found"),
    ),
    security(("api_key" = []), ("bearer" = []))
)]
pub(crate) async fn token_exchange(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Path((mcp_server_id, idp_server_id)): Path<(String, String)>,
    body: Body,
) -> Response {
    let start = Instant::now();
    let correlation_id = headers
        .get(crate::constants::headers::X_CORRELATION_ID)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    let correlation_id = if correlation_id.is_empty() {
        uuid::Uuid::new_v4().to_string()
    } else {
        correlation_id
    };

    let tenant_id = match extract_tenant_id(&state, tenant, &mcp_server_id).await {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    // Authenticate request (optional for token exchange: OAuth clients
    // present no gateway credential). A lookup that FAILED is not "no
    // credential": the token would be stored without its gateway user.
    let auth_ctx = match auth::authenticate_request(
        &headers,
        &tenant_id,
        &state.config.jwt_secret,
        &state.db,
        &state.credential_verifiers,
        client_addr.ip(),
        false,
    )
    .await
    {
        Ok(ctx) => Some(ctx),
        Err(auth::AuthError::Internal(e) | auth::AuthError::Unavailable(e)) => {
            tracing::error!(error = %e, "oauth token exchange: authentication lookup failed");
            return db_unavailable();
        }
        Err(_) => None,
    };

    let gateway_user_id = auth_ctx.as_ref().and_then(|a| a.gateway_user_id.clone());

    // Parse form body
    let body_bytes = match axum::body::to_bytes(body, 1024 * 1024).await {
        Ok(b) => b,
        Err(e) => return error_response(StatusCode::BAD_REQUEST, &e.to_string()),
    };

    let form: std::collections::HashMap<String, String> =
        serde_urlencoded::from_bytes(&body_bytes).unwrap_or_default();

    let grant_type = form.get("grant_type").map(|s| s.as_str()).unwrap_or("");

    // -----------------------------------------------------------------------
    // Route based on grant_type
    // -----------------------------------------------------------------------
    match grant_type {
        "authorization_code" => {
            handle_authorization_code(
                &state,
                &tenant_id,
                &mcp_server_id,
                &idp_server_id,
                &form,
                gateway_user_id,
                auth_ctx,
                &correlation_id,
                start,
            )
            .await
        }
        "refresh_token" => {
            handle_refresh_token(
                &state,
                &tenant_id,
                &mcp_server_id,
                &idp_server_id,
                &form,
                auth_ctx,
                &correlation_id,
                start,
            )
            .await
        }
        other => error_response(
            StatusCode::BAD_REQUEST,
            &format!(
                "Unsupported grant_type: {other}. Supported: authorization_code, refresh_token"
            ),
        ),
    }
}

/// Handle grant_type=authorization_code
async fn handle_authorization_code(
    state: &SharedState,
    tenant_id: &str,
    mcp_server_id: &str,
    idp_server_id: &str,
    form: &std::collections::HashMap<String, String>,
    gateway_user_id: Option<String>,
    auth_ctx: Option<crate::models::auth::AuthContext>,
    correlation_id: &str,
    start: Instant,
) -> Response {
    let mut code = form.get("code").cloned().unwrap_or_default();
    let mut client_id = form.get("client_id").cloned().unwrap_or_default();
    let mut redirect_uri = form.get("redirect_uri").cloned();
    let client_secret_form = form.get("client_secret").map(|s| s.as_str());
    let mut code_verifier = form.get("code_verifier").cloned();
    // CIMD bridge: set when the code is gateway-minted; the
    // upstream exchange then uses the gateway-owned IdP client.
    let mut cimd_bridge_secret: Option<String> = None;
    let mut is_cimd_bridge = false;

    if code.is_empty() || client_id.is_empty() {
        return error_response(
            StatusCode::BAD_REQUEST,
            "Missing required parameters: code, client_id",
        );
    }

    // Load servers
    let mcp_server = match load_mcp_server(state, mcp_server_id, tenant_id).await {
        Ok(s) => s,
        Err(resp) => return resp,
    };

    let idp = match load_idp(state, idp_server_id, tenant_id).await {
        Ok(s) => s,
        Err(resp) => return resp,
    };

    // Idempotency check (if Redis available)
    let idempotency_key = OAuthTokenManager::create_idempotency_key(
        idp_server_id,
        &code,
        mcp_server_id,
        &client_id,
        code_verifier.as_deref().or(client_secret_form),
    );

    if let Some(ref redis) = state.redis {
        let mut redis_conn = redis.clone();
        match OAuthTokenManager::check_idempotency(&mut redis_conn, &idempotency_key).await {
            Ok(Some(cached_result)) => {
                return Json(cached_result).into_response();
            }
            Err(e) => {
                return error_response(e.status_code(), &e.to_string());
            }
            Ok(None) => {}
        }
    }

    // CIMD bridge leg 3: a `gwc_…` code was minted by our
    // callback. Verify the CLIENT's PKCE against the challenge captured
    // at authorize, then swap in the stored upstream leg (IdP code +
    // gateway client + gateway verifier) and continue the normal flow.
    if code.starts_with(crate::services::cimd::GW_CODE_PREFIX) {
        let Some(redis) = state.redis.clone() else {
            cleanup_idempotency(state, &idempotency_key).await;
            return error_response(
                StatusCode::SERVICE_UNAVAILABLE,
                "CIMD bridging requires Redis",
            );
        };
        let mut redis = redis;
        let taken: Result<Option<crate::services::cimd::CodeState>, String> =
            crate::services::cimd::redis_take_json(
                &mut redis,
                &format!("{}{code}", crate::services::cimd::CODE_KEY_PREFIX),
            )
            .await;
        match taken {
            Ok(Some(cs)) => {
                // A code is redeemed where it was issued: redeemed elsewhere,
                // the upstream credential would be filed under another
                // server or another tenant.
                if cs.bridge.tenant_id != tenant_id
                    || cs.bridge.mcp_server_id != mcp_server_id
                    || cs.bridge.idp_server_id != idp_server_id
                {
                    cleanup_idempotency(state, &idempotency_key).await;
                    return error_response(
                        StatusCode::BAD_REQUEST,
                        "authorization code was not issued for this server",
                    );
                }
                if cs.bridge.cimd_url != client_id {
                    cleanup_idempotency(state, &idempotency_key).await;
                    return error_response(
                        StatusCode::BAD_REQUEST,
                        "client_id does not match the authorization request",
                    );
                }
                // RFC 6749 §4.1.3: a redirect_uri named here must be the one
                // the authorization request used.
                if redirect_uri
                    .as_deref()
                    .is_some_and(|sent| sent != cs.bridge.client_redirect_uri)
                {
                    cleanup_idempotency(state, &idempotency_key).await;
                    return error_response(
                        StatusCode::BAD_REQUEST,
                        "redirect_uri does not match the authorization request",
                    );
                }
                let Some(verifier) = code_verifier.as_deref() else {
                    cleanup_idempotency(state, &idempotency_key).await;
                    return error_response(
                        StatusCode::BAD_REQUEST,
                        "code_verifier is required for CIMD clients",
                    );
                };
                if !crate::services::cimd::verify_s256(verifier, &cs.bridge.client_code_challenge) {
                    cleanup_idempotency(state, &idempotency_key).await;
                    return error_response(StatusCode::BAD_REQUEST, "PKCE verification failed");
                }
                cimd_bridge_secret = match cs.bridge.idp_client_secret_enc.as_deref() {
                    Some(enc) => match state.encryption.decrypt(enc) {
                        Ok(secret) => Some(secret),
                        Err(e) => {
                            cleanup_idempotency(state, &idempotency_key).await;
                            return error_response(
                                StatusCode::INTERNAL_SERVER_ERROR,
                                &format!("CIMD client secret decryption failed: {e}"),
                            );
                        }
                    },
                    None => None,
                };
                code = cs.upstream_code;
                client_id = cs.bridge.idp_client_id;
                redirect_uri = Some(cs.bridge.gateway_callback);
                code_verifier = Some(cs.bridge.gateway_verifier);
                is_cimd_bridge = true;
            }
            Ok(None) => {
                cleanup_idempotency(state, &idempotency_key).await;
                return error_response(
                    StatusCode::BAD_REQUEST,
                    "Unknown, expired, or already-used authorization code",
                );
            }
            Err(e) => {
                cleanup_idempotency(state, &idempotency_key).await;
                return error_response(StatusCode::INTERNAL_SERVER_ERROR, &e);
            }
        }
    }

    // Resolve client_secret (CIMD bridge already carries its own)
    let client_secret = if is_cimd_bridge {
        // The bridge bound the code to the pair it
        // was started on; whether the tenant configured that pair is a
        // separate question, asked here as for every other client.
        if let Err(resp) = require_server_idp_pairing(state, tenant_id, &mcp_server, &idp).await {
            cleanup_idempotency(state, &idempotency_key).await;
            return resp;
        }
        cimd_bridge_secret
    } else {
        match resolve_client_secret(
            state,
            tenant_id,
            &mcp_server,
            &idp,
            &client_id,
            client_secret_form,
            code_verifier.is_some(),
        )
        .await
        {
            Ok(secret) => secret,
            Err(resp) => {
                cleanup_idempotency(state, &idempotency_key).await;
                return resp;
            }
        }
    };

    // Exchange authorization code with IdP
    let tokens = match state
        .oauth_token_manager
        .exchange_authorization_code(
            &idp,
            &code,
            &client_id,
            redirect_uri.as_deref(),
            client_secret.as_deref(),
            code_verifier.as_deref(),
        )
        .await
    {
        Ok(t) => t,
        Err(e) => {
            cleanup_idempotency(state, &idempotency_key).await;
            let log = proxy_logger::build_proxy_log(
                "oauth",
                auth_ctx.as_ref().unwrap_or(&anonymous_auth(tenant_id)),
                "POST",
                &format!("/{mcp_server_id}/{idp_server_id}/token"),
                None,
                Some(mcp_server_id),
                e.status_code().as_u16(),
                start.elapsed().as_millis() as f64,
                false,
                Some(&e.to_string()),
                correlation_id,
            );
            state
                .background_writer
                .write_proxy_log(state.db.clone(), log);

            return error_response(e.status_code(), &e.to_string());
        }
    };

    // Get user info from IdP's userinfo endpoint
    let user_info = if let Some(ref userinfo_ep) = idp.userinfo_endpoint {
        tracing::debug!(
            userinfo_endpoint = %userinfo_ep,
            idp_name = %idp.name,
            "Fetching user info from IdP"
        );
        match state
            .oauth_token_manager
            .get_user_info(&tokens.access_token, userinfo_ep)
            .await
        {
            Ok(info) => {
                tracing::debug!(
                    user_id = %info.user_id,
                    username = ?info.username,
                    email = ?info.email,
                    name = ?info.name,
                    "User info fetched from IdP"
                );
                info
            }
            Err(e) => {
                tracing::warn!(
                    userinfo_endpoint = %userinfo_ep,
                    error = %e,
                    "Failed to get user info from IdP — falling back"
                );
                crate::services::oauth_token_manager::UserInfo {
                    user_id: gateway_user_id
                        .clone()
                        .unwrap_or_else(|| "unknown".to_string()),
                    username: None,
                    email: None,
                    name: None,
                }
            }
        }
    } else {
        tracing::debug!(idp_name = %idp.name, "No userinfo_endpoint configured for IdP");
        crate::services::oauth_token_manager::UserInfo {
            user_id: gateway_user_id
                .clone()
                .unwrap_or_else(|| "unknown".to_string()),
            username: None,
            email: None,
            name: None,
        }
    };

    // Refuse to store a credential we cannot attribute.
    //
    // When the IdP has no `userinfo_endpoint` (or the call to it fails) and
    // the exchange carried no gateway user, the subject fell back to the
    // literal "unknown". Every such user's upstream token was then stored
    // under the SAME key, the lookup takes the newest row for that key, and
    // the minted MCP JWT carries `sub = "unknown"` — so any holder of one of
    // those JWTs used whichever user most recently authorised. That is one
    // person acting on another's credential at the upstream SaaS, which is
    // the precise thing per-user OAuth exists to prevent.
    //
    // A credential with no owner is not storable. Better to fail the exchange
    // with an explanation the operator can act on (configure `userinfo`, or
    // authenticate the exchange) than to file it under a shared name.
    if user_info.user_id == "unknown" {
        tracing::error!(
            idp_name = %idp.name,
            mcp_server_id,
            "OAuth exchange produced no stable subject; refusing to store an unattributable token"
        );
        cleanup_idempotency(state, &idempotency_key).await;
        return error_response(
            StatusCode::BAD_REQUEST,
            "oauth_subject_unavailable: the identity provider returned no subject for this \
             exchange. Configure a `userinfo_endpoint` on the IdP, or perform the exchange \
             as an authenticated gateway user, so the stored token can be tied to a person.",
        );
    }

    // Store encrypted tokens
    if let Err(e) = state
        .oauth_token_manager
        .store_token(
            &state.db,
            gateway_user_id.as_deref(),
            &user_info.user_id,
            mcp_server_id,
            tenant_id,
            idp_server_id,
            &tokens,
            Some(&client_id),
            client_secret.as_deref(),
            // SEP-2352 — bind the stored credential to the issuer it
            // was minted under.
            Some(&idp.issuer),
        )
        .await
    {
        cleanup_idempotency(state, &idempotency_key).await;
        return error_response(StatusCode::INTERNAL_SERVER_ERROR, &e.to_string());
    }

    // Discover capabilities if not yet discovered
    // For OAuth-protected MCP servers, this is the first time we have a valid access token
    if !has_capabilities(&mcp_server) {
        tracing::debug!(
            mcp_server_id,
            mcp_server_name = %mcp_server.name,
            "Discovering capabilities for OAuth MCP server (first token exchange)"
        );
        match discover_capabilities(state, &mcp_server, &tokens.access_token).await {
            Ok(discovery) => {
                if let Err(e) = sqlx::query(
                    "UPDATE mcp_servers SET capabilities = $1, wants_listener = $2, \
                     protocol_version = $3, dialect = $4, discovered_at = NOW() WHERE id = $5",
                )
                .bind(&discovery.capabilities)
                .bind(discovery.wants_listener)
                .bind(&discovery.protocol_version)
                .bind(&discovery.dialect)
                .bind(&mcp_server.id)
                .execute(&state.db)
                .await
                {
                    tracing::warn!(
                        mcp_server_id,
                        error = %e,
                        "Failed to store discovered capabilities"
                    );
                } else {
                    tracing::debug!(
                        mcp_server_id,
                        tools = discovery
                            .capabilities
                            .get("tools")
                            .and_then(|v| v.as_array())
                            .map(|a| a.len())
                            .unwrap_or(0),
                        resources = discovery
                            .capabilities
                            .get("resources")
                            .and_then(|v| v.as_array())
                            .map(|a| a.len())
                            .unwrap_or(0),
                        prompts = discovery
                            .capabilities
                            .get("prompts")
                            .and_then(|v| v.as_array())
                            .map(|a| a.len())
                            .unwrap_or(0),
                        resource_templates = discovery
                            .capabilities
                            .get("resource_templates")
                            .and_then(|v| v.as_array())
                            .map(|a| a.len())
                            .unwrap_or(0),
                        wants_listener = discovery.wants_listener,
                        "Capabilities discovered and stored"
                    );
                }
            }
            Err(e) => {
                tracing::warn!(
                    mcp_server_id,
                    error = %e,
                    "Capability discovery failed — server will work but capabilities unknown"
                );
            }
        }
    }

    // Create MCP JWT
    let mcp_jwt = match state.oauth_token_manager.create_mcp_jwt(
        tenant_id,
        &user_info.user_id,
        mcp_server_id,
        idp_server_id,
        user_info.username.as_deref(),
        user_info.email.as_deref(),
        user_info.name.as_deref(),
    ) {
        Ok(jwt) => jwt,
        Err(e) => {
            cleanup_idempotency(state, &idempotency_key).await;
            return error_response(StatusCode::INTERNAL_SERVER_ERROR, &e.to_string());
        }
    };

    // Build response
    let has_refresh = tokens.refresh_token.is_some();
    let result = json!({
        "access_token": mcp_jwt,
        "token_type": "Bearer",
        "expires_in": state.config.jwt_expires_in,
        "scope": tokens.scope.as_deref().unwrap_or(""),
        // The IdP's refresh token stays server-side like its access token. The
        // client's handle for the refresh grant is the gateway JWT, which is
        // what `handle_refresh_token` accepts.
        "refresh_token": has_refresh.then(|| mcp_jwt.clone()),
        "user": {
            "user_id": user_info.user_id,
            "username": user_info.username,
            "email": user_info.email,
            "name": user_info.name,
        },
        "token_info": {
            "type": "mcp_auth",
            "description": "Use this token for all requests to MCP server via the gateway",
            "oauth_stored": true,
            "auto_refresh": has_refresh,
        }
    });

    // Mark idempotency as completed
    if let Some(ref redis) = state.redis {
        let mut redis_conn = redis.clone();
        OAuthTokenManager::mark_exchange_completed(&mut redis_conn, &idempotency_key, &result)
            .await;
    }

    // Background logging
    let mut log = proxy_logger::build_proxy_log(
        "oauth",
        auth_ctx.as_ref().unwrap_or(&anonymous_auth(tenant_id)),
        "POST",
        &format!("/{mcp_server_id}/{idp_server_id}/token"),
        idp.token_endpoint.as_deref(),
        Some(mcp_server_id),
        200,
        start.elapsed().as_millis() as f64,
        true,
        None,
        correlation_id,
    );
    log.oauth_flow_type = Some("authorization_code".into());
    log.idp_user_id = Some(user_info.user_id.clone());
    log.idp_user_username = user_info.username.clone();
    log.idp_user_email = user_info.email.clone();
    log.idp_server_id = Some(idp_server_id.to_string());
    state
        .background_writer
        .write_proxy_log(state.db.clone(), log);

    let metric = proxy_logger::build_proxy_metric(
        "oauth",
        auth_ctx.as_ref().unwrap_or(&anonymous_auth(tenant_id)),
        "POST",
        &format!("/{mcp_server_id}/{idp_server_id}/token"),
        "oauth_token_exchange",
        Some(mcp_server_id),
        200,
        start.elapsed().as_millis() as f64,
        true,
        correlation_id,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    );
    state
        .background_writer
        .write_proxy_metric(state.db.clone(), metric);

    Json(result).into_response()
}

/// Handle grant_type=refresh_token
async fn handle_refresh_token(
    state: &SharedState,
    tenant_id: &str,
    mcp_server_id: &str,
    idp_server_id: &str,
    form: &std::collections::HashMap<String, String>,
    auth_ctx: Option<crate::models::auth::AuthContext>,
    correlation_id: &str,
    start: Instant,
) -> Response {
    // The client sends their MCP JWT as the refresh_token
    let refresh_token = form.get("refresh_token").map(|s| s.as_str()).unwrap_or("");
    if refresh_token.is_empty() {
        return error_response(
            StatusCode::BAD_REQUEST,
            "Missing required parameter: refresh_token (must be the MCP JWT)",
        );
    }

    // Verify the MCP JWT (allow expired — that's why they're refreshing)
    let claims = match state
        .oauth_token_manager
        .verify_mcp_jwt_allow_expired(refresh_token)
    {
        Ok(c) => c,
        Err(e) => {
            return error_response(
                StatusCode::UNAUTHORIZED,
                &format!("Invalid refresh token: {e}"),
            );
        }
    };

    // Validate that the JWT's mcp_server_id matches the URL path
    if claims.mcp_server_id != mcp_server_id {
        return error_response(
            StatusCode::BAD_REQUEST,
            &format!(
                "Token mcp_server_id '{}' does not match URL path '{}'",
                claims.mcp_server_id, mcp_server_id
            ),
        );
    }

    // … and its IdP: the stored credential is looked
    // up under the JWT's own IdP, and the route is that IdP's.
    if claims.idp_id != idp_server_id {
        return error_response(
            StatusCode::BAD_REQUEST,
            "Token was not issued through this identity provider",
        );
    }

    // Refresh the stored OAuth token and reissue MCP JWT
    match state
        .oauth_token_manager
        .refresh_and_reissue_jwt(&state.db, &claims)
        .await
    {
        Ok((new_jwt, has_refresh)) => {
            let result = json!({
                "access_token": new_jwt,
                "token_type": "Bearer",
                "expires_in": state.config.jwt_expires_in,
                "token_info": {
                    "type": "mcp_auth",
                    "description": "Refreshed MCP JWT",
                    "oauth_stored": true,
                    "auto_refresh": has_refresh,
                }
            });

            // Background logging
            let mut log = proxy_logger::build_proxy_log(
                "oauth",
                auth_ctx.as_ref().unwrap_or(&anonymous_auth(tenant_id)),
                "POST",
                &format!("/{mcp_server_id}/{idp_server_id}/token"),
                None,
                Some(mcp_server_id),
                200,
                start.elapsed().as_millis() as f64,
                true,
                None,
                correlation_id,
            );
            log.oauth_flow_type = Some("refresh_token".into());
            log.idp_user_id = Some(claims.user_id.clone());
            log.idp_user_username = claims.username.clone();
            log.idp_user_email = claims.email.clone();
            log.idp_server_id = Some(idp_server_id.to_string());
            state
                .background_writer
                .write_proxy_log(state.db.clone(), log);

            Json(result).into_response()
        }
        Err(e) => {
            let mut log = proxy_logger::build_proxy_log(
                "oauth",
                auth_ctx.as_ref().unwrap_or(&anonymous_auth(tenant_id)),
                "POST",
                &format!("/{mcp_server_id}/{idp_server_id}/token"),
                None,
                Some(mcp_server_id),
                e.status_code().as_u16(),
                start.elapsed().as_millis() as f64,
                false,
                Some(&e.to_string()),
                correlation_id,
            );
            log.oauth_flow_type = Some("refresh_token".into());
            log.idp_user_id = Some(claims.user_id.clone());
            log.idp_server_id = Some(idp_server_id.to_string());
            state
                .background_writer
                .write_proxy_log(state.db.clone(), log);

            error_response(e.status_code(), &e.to_string())
        }
    }
}

// ---------------------------------------------------------------------------
// Token revocation
// ---------------------------------------------------------------------------

#[utoipa::path(
    post,
    path = "/v1/proxy/oauth/{mcp_server_id}/{idp_server_id}/revoke",
    tag = "OAuth",
    summary = "Revoke OAuth token",
    description = "Revokes the stored OAuth token for the authenticated user.",
    params(
        ("mcp_server_id" = String, Path, description = "MCP server ID"),
        ("idp_server_id" = String, Path, description = "Identity Provider ID"),
    ),
    responses(
        (status = 200, description = "Token revoked"),
        (status = 401, description = "Invalid or missing token"),
        (status = 404, description = "No token found to revoke"),
    ),
    security(("bearer" = []))
)]
pub(crate) async fn revoke_token(
    State(state): State<SharedState>,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Path((mcp_server_id, idp_server_id)): Path<(String, String)>,
    body: Body,
) -> Response {
    let tenant_id = match extract_tenant_id(&state, tenant, &mcp_server_id).await {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    // Accept MCP JWT from Authorization header or from request body (token field)
    let mcp_jwt = headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .map(String::from);

    let mcp_jwt = if let Some(jwt) = mcp_jwt {
        jwt
    } else {
        // Try form body
        let body_bytes = match axum::body::to_bytes(body, 1024 * 64).await {
            Ok(b) => b,
            Err(e) => return error_response(StatusCode::BAD_REQUEST, &e.to_string()),
        };
        let form: std::collections::HashMap<String, String> =
            serde_urlencoded::from_bytes(&body_bytes).unwrap_or_default();
        match form.get("token") {
            Some(t) if !t.is_empty() => t.clone(),
            _ => {
                return error_response(
                    StatusCode::BAD_REQUEST,
                    "Missing token: provide MCP JWT in Authorization header or 'token' form field",
                );
            }
        }
    };

    // Verify MCP JWT (allow expired)
    let claims = match state
        .oauth_token_manager
        .verify_mcp_jwt_allow_expired(&mcp_jwt)
    {
        Ok(c) => c,
        Err(e) => {
            return error_response(StatusCode::UNAUTHORIZED, &format!("Invalid token: {e}"));
        }
    };

    // A JWT speaks for its own tenant, server and IdP. The revocation below
    // keys on the subject, which is an IdP id: the same subject in another
    // tenant, or under another IdP of this tenant,
    // is another person's credential.
    if claims.tenant_id != tenant_id
        || claims.mcp_server_id != mcp_server_id
        || claims.idp_id != idp_server_id
    {
        return error_response(
            StatusCode::FORBIDDEN,
            "Token was not issued for this tenant, server and identity provider",
        );
    }

    match state
        .oauth_token_manager
        .revoke_token(
            &state.db,
            &claims.user_id,
            &mcp_server_id,
            &tenant_id,
            &claims.idp_id,
        )
        .await
    {
        Ok(true) => {
            tracing::debug!(
                user_id = %claims.user_id,
                mcp_server_id = %mcp_server_id,
                "OAuth token revoked"
            );
            Json(json!({"revoked": true})).into_response()
        }
        Ok(false) => error_response(StatusCode::NOT_FOUND, "No active token found to revoke"),
        Err(e) => error_response(e.status_code(), &e.to_string()),
    }
}

// ---------------------------------------------------------------------------
// Token listing
// ---------------------------------------------------------------------------

#[utoipa::path(
    get,
    path = "/v1/proxy/oauth/{mcp_server_id}/{idp_server_id}/tokens",
    tag = "OAuth",
    summary = "List OAuth tokens for user",
    description = "Returns metadata about stored OAuth tokens for the authenticated user.",
    params(
        ("mcp_server_id" = String, Path, description = "MCP server ID"),
        ("idp_server_id" = String, Path, description = "Identity Provider ID"),
    ),
    responses(
        (status = 200, description = "List of token metadata"),
        (status = 401, description = "Authentication required"),
    ),
    security(("bearer" = []), ("api_key" = []))
)]
pub(crate) async fn list_tokens(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Path((mcp_server_id, _idp_server_id)): Path<(String, String)>,
) -> Response {
    let tenant_id = match extract_tenant_id(&state, tenant, &mcp_server_id).await {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    // The MCP JWT this route issued is a credential for the caller's own token
    // list. `authenticate_request` refuses it (a JWT with a `type` claim is not
    // a gateway session), so it is verified here, live and for this tenant.
    let mcp_claims = headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .and_then(|bearer| state.oauth_token_manager.verify_mcp_jwt(bearer).ok());

    // The IdP whose subject the caller is: the JWT's own,
    // so the same subject string of another IdP — another person —
    // is not listed. A gateway session names a gateway user, not an IdP
    // subject, and lists what is stored under that user.
    let (idp_user_id, listed_idp) = match mcp_claims {
        Some(claims) if claims.tenant_id == tenant_id => {
            (Some(claims.user_id), Some(claims.idp_id))
        }
        Some(_) => {
            return error_response(
                StatusCode::UNAUTHORIZED,
                "Token was not issued for this tenant",
            );
        }
        None => {
            let auth_ctx = match auth::authenticate_request(
                &headers,
                &tenant_id,
                &state.config.jwt_secret,
                &state.db,
                &state.credential_verifiers,
                client_addr.ip(),
                true, // require auth
            )
            .await
            {
                Ok(ctx) => ctx,
                Err(e) => {
                    return error_response(StatusCode::UNAUTHORIZED, &e.to_string());
                }
            };
            (auth_ctx.idp_user_id.or(auth_ctx.gateway_user_id), None)
        }
    };

    let idp_user_id = match idp_user_id {
        Some(id) => id,
        None => {
            return error_response(
                StatusCode::BAD_REQUEST,
                "Cannot determine user identity for token listing",
            );
        }
    };

    match state
        .oauth_token_manager
        .get_all_tokens_for_user(&state.db, &idp_user_id, &tenant_id, listed_idp.as_deref())
        .await
    {
        Ok(tokens) => Json(json!({
            "user_id": idp_user_id,
            "tenant_id": tenant_id,
            "tokens": tokens,
        }))
        .into_response(),
        Err(e) => error_response(e.status_code(), &e.to_string()),
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async fn load_mcp_server(
    state: &SharedState,
    id: &str,
    tenant_id: &str,
) -> Result<McpServer, Response> {
    let server: Option<McpServer> = sqlx::query_as(
        "SELECT * FROM mcp_servers WHERE id = $1 AND tenant_id = $2 AND enabled = true",
    )
    .bind(id)
    .bind(tenant_id)
    .fetch_optional(&state.db)
    .await
    .map_err(|e| lookup_failed("mcp server", e))?;

    let server = server
        .ok_or_else(|| error_response(StatusCode::NOT_FOUND, "MCP server not found or disabled"))?;
    // Capability discovery POSTs to base_url; same check as the MCP
    // proxy's own server resolution.
    crate::services::outbound_policy::check_mcp_server_url(&server.base_url).map_err(|_| {
        error_response(
            StatusCode::FORBIDDEN,
            "MCP server destination is refused by the outbound policy",
        )
    })?;
    Ok(server)
}

async fn load_idp(state: &SharedState, id: &str, tenant_id: &str) -> Result<IdpServer, Response> {
    let idp: Option<IdpServer> = sqlx::query_as(
        "SELECT * FROM idp_servers WHERE id = $1 AND tenant_id = $2 AND enabled = true \
         AND protocol = 'oidc'",
    )
    .bind(id)
    .bind(tenant_id)
    .fetch_optional(&state.db)
    .await
    .map_err(|e| lookup_failed("identity provider", e))?;

    let idp = idp.ok_or_else(|| {
        error_response(
            StatusCode::NOT_FOUND,
            "Identity Provider not found or disabled",
        )
    })?;
    // The proxy calls this IdP's token / registration / userinfo
    // endpoints; refuse one whose endpoints the outbound policy denies.
    crate::services::oauth_token_manager::check_idp_destinations(&idp).map_err(|_| {
        error_response(
            StatusCode::FORBIDDEN,
            "Identity Provider endpoint is refused by the outbound policy",
        )
    })?;
    Ok(idp)
}

/// Is `idp` an identity provider the tenant configured for `mcp_server`?
///
/// The token route's path names both, and the two
/// were only ever checked one at a time (each exists, is enabled and belongs
/// to the tenant). What pairs them is configuration the control plane writes:
/// the server's own `idp_id`, or an active OAuth client of that IdP attached
/// to the server through a `server_configs` row. Without either, a credential
/// issued by the IdP has no business being filed under the server. A lookup
/// that fails refuses.
async fn require_server_idp_pairing(
    state: &SharedState,
    tenant_id: &str,
    mcp_server: &McpServer,
    idp: &IdpServer,
) -> Result<(), Response> {
    if mcp_server.idp_id.as_deref() == Some(idp.id.as_str()) {
        return Ok(());
    }
    let linked: bool = sqlx::query_scalar(
        r#"SELECT EXISTS (
               SELECT 1 FROM oauth_app_clients oac
               JOIN server_configs sc ON oac.id = sc.oauth_client_id
               WHERE sc.mcp_server_id = $1 AND sc.tenant_id = $2
                 AND oac.tenant_id = $2 AND oac.idp_id = $3
                 AND oac.is_active = true
           )"#,
    )
    .bind(&mcp_server.id)
    .bind(tenant_id)
    .bind(&idp.id)
    .fetch_one(&state.db)
    .await
    .map_err(|e| lookup_failed("server and identity provider pairing", e))?;
    if linked {
        return Ok(());
    }
    tracing::warn!(
        mcp_server_id = %mcp_server.id,
        idp_id = %idp.id,
        "OAuth code exchange refused: the identity provider is not configured for this MCP server"
    );
    Err(error_response(
        StatusCode::FORBIDDEN,
        "Identity Provider is not configured for this MCP server",
    ))
}

/// Resolve client_secret from form data or database.
///
/// Every `Ok` of this function has established that the server and the IdP
/// are paired: by the client row itself, whose join
/// links the two, or by [`require_server_idp_pairing`] for a client that has
/// no row (a dynamically registered or public PKCE client, or one that sends
/// its own secret).
async fn resolve_client_secret(
    state: &SharedState,
    tenant_id: &str,
    mcp_server: &McpServer,
    idp: &IdpServer,
    client_id: &str,
    form_secret: Option<&str>,
    is_pkce: bool,
) -> Result<Option<String>, Response> {
    let (mcp_server_id, idp_server_id) = (mcp_server.id.as_str(), idp.id.as_str());
    let form_secret = form_secret.filter(|secret| !secret.is_empty());

    // Look up OAuthAppClient to determine if we need a secret
    let oauth_client: Option<OAuthAppClient> = sqlx::query_as(
        r#"SELECT oac.* FROM oauth_app_clients oac
           JOIN server_configs sc ON oac.id = sc.oauth_client_id
           WHERE sc.mcp_server_id = $1 AND sc.tenant_id = $2
             AND oac.idp_id = $3 AND oac.client_id = $4
             AND oac.is_active = true
           LIMIT 1"#,
    )
    .bind(mcp_server_id)
    .bind(tenant_id)
    .bind(idp_server_id)
    .bind(client_id)
    .fetch_optional(&state.db)
    .await
    .map_err(|e| lookup_failed("oauth client", e))?;

    let oauth_client = match oauth_client {
        Some(c) => c,
        None => {
            // No client row for this client id. A client that brings its own
            // proof (a secret in the form, or PKCE for a public client) needs
            // none — but the server and the IdP must still be a configured
            // pair. This returned `Ok` for any enabled IdP of the tenant.
            if form_secret.is_some() || is_pkce {
                require_server_idp_pairing(state, tenant_id, mcp_server, idp).await?;
                return Ok(form_secret.map(String::from));
            }

            // Run diagnostic queries to give a specific reason
            let reason = diagnose_oauth_client_not_found(
                state,
                tenant_id,
                mcp_server_id,
                idp_server_id,
                client_id,
            )
            .await;

            return Err(error_response(
                StatusCode::NOT_FOUND,
                &format!("OAuth client configuration not found: {reason}"),
            ));
        }
    };

    // If provided in form, use it
    if let Some(secret) = form_secret {
        return Ok(Some(secret.to_string()));
    }

    // PKCE flows with plain public clients don't need a secret
    if is_pkce && !oauth_client.needs_secret_for_pkce() {
        return Ok(None);
    }

    // Decrypt stored secret
    match oauth_client.encrypted_client_secret {
        Some(ref encrypted) => {
            let decrypted = state.encryption.decrypt(encrypted).map_err(|e| {
                error_response(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    &format!("Failed to decrypt client secret: {e}"),
                )
            })?;
            Ok(Some(decrypted))
        }
        None => {
            if oauth_client.is_confidential() {
                Err(error_response(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "Confidential client has no stored secret",
                ))
            } else {
                Ok(None)
            }
        }
    }
}

/// Diagnose why the OAuth client lookup failed and return a human-readable reason.
async fn diagnose_oauth_client_not_found(
    state: &SharedState,
    tenant_id: &str,
    mcp_server_id: &str,
    idp_server_id: &str,
    client_id: &str,
) -> String {
    // Check if server_config exists for this MCP server
    let sc_exists: Result<Option<(String,)>, sqlx::Error> = sqlx::query_as(
        "SELECT oauth_client_id FROM server_configs WHERE mcp_server_id = $1 AND tenant_id = $2 LIMIT 1",
    )
    .bind(mcp_server_id)
    .bind(tenant_id)
    .fetch_optional(&state.db)
    .await;
    let sc_exists = match sc_exists {
        Ok(row) => row,
        Err(e) => return format!("server_config lookup failed: {e}"),
    };

    let oauth_client_id = match sc_exists {
        Some((ref id,)) => id.clone(),
        None => {
            return format!(
                "no server_config found for mcp_server_id='{}' in tenant '{}'",
                mcp_server_id, tenant_id,
            );
        }
    };

    // Check if the OAuth client record exists at all
    let oac: Result<Option<(bool, String, String)>, sqlx::Error> = sqlx::query_as(
        "SELECT is_active, idp_id, client_id FROM oauth_app_clients WHERE id = $1 AND tenant_id = $2 LIMIT 1",
    )
    .bind(&oauth_client_id)
    .bind(tenant_id)
    .fetch_optional(&state.db)
    .await;
    let oac = match oac {
        Ok(row) => row,
        Err(e) => return format!("oauth_app_clients lookup failed: {e}"),
    };

    match oac {
        None => {
            format!(
                "server_config references oauth_client_id='{}' but no such oauth_app_clients record exists",
                oauth_client_id,
            )
        }
        Some((is_active, ref stored_idp_id, ref stored_client_id)) => {
            let mut issues = Vec::new();
            if !is_active {
                issues
                    .push("oauth client is_active=false (must be activated by admin)".to_string());
            }
            if stored_idp_id != idp_server_id {
                issues.push(format!(
                    "idp_id mismatch: stored='{}', requested='{}'",
                    stored_idp_id, idp_server_id,
                ));
            }
            if stored_client_id != client_id {
                issues.push(format!(
                    "client_id mismatch: stored='{}', requested='{}'",
                    stored_client_id, client_id,
                ));
            }
            if issues.is_empty() {
                "unknown reason (all fields appear to match)".to_string()
            } else {
                issues.join("; ")
            }
        }
    }
}

/// Clean up idempotency state on error.
async fn cleanup_idempotency(state: &SharedState, key: &str) {
    if let Some(ref redis) = state.redis {
        let mut redis_conn = redis.clone();
        OAuthTokenManager::cleanup_idempotency(&mut redis_conn, key).await;
    }
}

/// Extract tenant ID from middleware context, or fall back to looking up the
/// MCP server's tenant_id in the database.  The fallback is needed because
/// OAuth endpoints are exempt from the tenant middleware (discovery endpoints
/// are unauthenticated per RFC 8414 / OpenID Connect Discovery, and the token
/// exchange may arrive without X-Tenant-ID from some clients).
async fn extract_tenant_id(
    state: &SharedState,
    tenant: Option<axum::Extension<TenantContext>>,
    mcp_server_id: &str,
) -> Result<String, Response> {
    // Prefer the middleware-resolved tenant if available
    if let Some(t) = tenant {
        return Ok(t.tenant_id.clone());
    }

    // Fallback: resolve tenant from the MCP server record
    let result: Option<(String,)> =
        sqlx::query_as("SELECT tenant_id FROM mcp_servers WHERE id = $1")
            .bind(mcp_server_id)
            .fetch_optional(&state.db)
            .await
            .map_err(|e| lookup_failed("mcp server tenant", e))?;

    result
        .map(|(tid,)| tid)
        .ok_or_else(|| error_response(StatusCode::BAD_REQUEST, "Missing tenant context"))
}

/// The base of every URL the OAuth routes advertise (issuer, metadata,
/// redirect URIs): `BRUTOR_GATEWAY_PUBLIC_URL`, always. Never a request
/// header: a caller must not choose the issuer or token endpoint a client is
/// told to use. Behind a proxy, a public URL that is missing or says
/// localhost is the operator's setting to fix (the gateway warns at start).
fn gateway_base(state: &crate::services::AppState) -> String {
    state.config.public_url.trim_end_matches('/').to_string()
}

fn anonymous_auth(tenant_id: &str) -> crate::models::auth::AuthContext {
    crate::models::auth::AuthContext {
        auth_type: crate::models::auth::AuthType::Anonymous,
        tenant_id: tenant_id.to_string(),
        gateway_user_id: None,
        gateway_username: Some("anonymous".into()),
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
        client_ip: None,
        session_key: None,
        resolved_group_id: None,
        actor_agent_id: None,
        session_token_type: None,
        agent_impl: crate::services::agent_impl::request_carriers(),
        admission_hold: None,
    }
}

fn error_response(status: StatusCode, message: &str) -> Response {
    crate::services::error_envelope::canonical_error_response(status, message)
}

/// A configuration lookup failed: 503, never "not found". Telling an OAuth
/// client its server, IdP or client registration does not exist because the
/// database blipped sends it down the wrong recovery path.
fn db_unavailable() -> Response {
    error_response(
        StatusCode::SERVICE_UNAVAILABLE,
        "configuration store temporarily unavailable; retry",
    )
}

/// [`db_unavailable`], logging which lookup failed.
fn lookup_failed(what: &str, e: sqlx::Error) -> Response {
    tracing::error!(error = %e, lookup = what, "oauth: database lookup failed");
    db_unavailable()
}

// =============================================================================
// MCP Capability Discovery
// =============================================================================

/// Check whether the MCP server already has discovered capabilities.
///
/// Returns false for:
///   - NULL capabilities
///   - empty object `{}`
///   - object where all values are empty arrays (e.g. `{"tools":[],"resources":[]}`)
///     which indicates a previous failed/incomplete discovery
///     SEP-2549: honor an upstream's advertised
///     `ttlMs`. Stateless discovery embeds `_brutor: {ttl_ms, fetched_epoch}`
///     in the capabilities JSON; when the window has lapsed, the lazy
///     background refresh re-discovers. Servers without the marker (legacy
///     dialect, rows discovered without one) never expire this way — they refresh via
///     push invalidation exactly as before.
pub(crate) fn capabilities_expired(server: &McpServer) -> bool {
    let Some(caps) = &server.capabilities else {
        return false;
    };
    let (Some(ttl_ms), Some(fetched)) = (
        caps.pointer("/_brutor/ttl_ms").and_then(|v| v.as_u64()),
        caps.pointer("/_brutor/fetched_epoch")
            .and_then(|v| v.as_u64()),
    ) else {
        return false;
    };
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    now.saturating_sub(fetched) > ttl_ms / 1000
}

pub(crate) fn has_capabilities(server: &McpServer) -> bool {
    match &server.capabilities {
        None => false,
        Some(v) => {
            if let Some(obj) = v.as_object() {
                if obj.is_empty() {
                    return false;
                }
                // Check if ALL capability arrays are empty — means nothing was actually discovered
                let all_empty = obj
                    .values()
                    .all(|val| val.as_array().map(|a| a.is_empty()).unwrap_or(false));
                !all_empty
            } else {
                false
            }
        }
    }
}

/// Result of a single capability-discovery cycle.
///
/// Exposes:
///   * `capabilities`: the resolved tools/resources/prompts/
///     resource_templates JSON, suitable for storing in
///     `mcp_servers.capabilities` (same shape as before — no
///     downstream change in semantics).
///   * `wants_listener`: derived from the *initialize* response
///     and **independent** of whether the lists are populated.
///     A server can have zero tools today and still want the
///     listener (because it may push `tools.listChanged` later);
///     conversely a server with a hundred tools but no
///     `listChanged`/`subscribe`/`logging` flag declared has
///     nothing to push and the listener is provably useless.
///     This flag is what gates the gateway's 405 short-circuit
///     and the portal SDK's listener policy.
pub(crate) struct CapabilityDiscovery {
    pub capabilities: Value,
    pub wants_listener: bool,
    /// Newest MCP protocol version the upstream negotiated
    /// (persisted to `mcp_servers.protocol_version`). `None` when the
    /// server answered but named no version.
    pub protocol_version: Option<String>,
    /// Wire dialect: `stateless` (2026-07-28 `server/discover` answered)
    /// or `handshake` (legacy initialize flow). Persisted to
    /// `mcp_servers.dialect`; the proxy speaks this dialect upstream.
    pub dialect: Option<String>,
}

/// Inspect the `result.capabilities` object from an MCP
/// `initialize` response and decide whether the server wants
/// its SSE listener channel opened.
///
/// Per the MCP spec, the listener is OPTIONAL and only useful
/// for servers that emit at least one of:
///   * `tools.listChanged`
///   * `resources.listChanged` or `resources.subscribe`
///   * `prompts.listChanged`
///   * `logging` (any value — its presence means the server
///     emits log notifications)
///
/// Anything else — empty `capabilities`, only `experimental`,
/// or just info-only fields — means the server has nothing to
/// push and we save everyone the round-trip.
///
/// Defensive default: when we can't parse the initialize
/// response (malformed, surprise wire format), return `true`
/// — that preserves the old "always open the listener"
/// behaviour for unknown servers, the same way the gate's
/// `unwrap_or(true)` does.
fn derive_wants_listener(init_body: &Value) -> bool {
    let caps = match init_body.get("result").and_then(|r| r.get("capabilities")) {
        Some(c) => c,
        None => return true, // Unknown shape — assume listener wanted.
    };
    if !caps.is_object() {
        return true;
    }

    let any_list_changed = ["tools", "resources", "prompts"].iter().any(|k| {
        caps.get(*k)
            .and_then(|v| v.get("listChanged"))
            .and_then(|v| v.as_bool())
            .unwrap_or(false)
    });
    let resources_subscribe = caps
        .get("resources")
        .and_then(|v| v.get("subscribe"))
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let logging_present = caps.get("logging").is_some();

    any_list_changed || resources_subscribe || logging_present
}

/// Discover capabilities (tools, resources, prompts, resource_templates) from
/// an MCP server using the Streamable HTTP transport.
///
/// Sends JSON-RPC `initialize`, then `tools/list`, `resources/list`,
/// `prompts/list`, and `resources/templates/list`.  Each list call is
/// best-effort — if the server doesn't support a capability the error is
/// logged and we move on.
///
/// Also derives the `wants_listener` flag from the initialize
/// response (see [`derive_wants_listener`]) so callers can
/// persist it to `mcp_servers.wants_listener` in the same write
/// that stores the capabilities JSON. This is the canonical
/// place to compute the flag — every server passes through here
/// once it has a usable token.
pub(crate) async fn discover_capabilities(
    state: &SharedState,
    server: &McpServer,
    access_token: &str,
) -> Result<CapabilityDiscovery, String> {
    let target_url = if let Some(ref mcp_path) = server.mcp_endpoint_path {
        format!("{}{}", server.base_url, mcp_path)
    } else {
        server.base_url.clone()
    };

    // Era detection, per the spec's own flow: probe
    // `server/discover` first; a DiscoverResult means the upstream is a
    // stateless 2026-07-28 server; any error/absence falls back to the
    // legacy initialize handshake. Flag-gated so the probe traffic only
    // starts when stateless support is switched on.
    use crate::constants::mcp_protocol as mp;
    struct ModernProbe {
        version: String,
        ttl_ms: Option<u64>,
        /// SEP-2133: negotiated extensions map from
        /// the DiscoverResult (e.g. `io.modelcontextprotocol/tasks`).
        extensions: Option<Value>,
    }
    // Always probe the modern `server/discover` first, then
    // fall back to the legacy handshake below. Detecting a stateless-only
    // upstream depends on this probe running, so it's unconditional (a
    // bare scope keeps the probe's locals contained).
    let mut modern_version: Option<ModernProbe> = None;
    {
        let probe = json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "server/discover",
            "params": {
                "_meta": {
                    mp::meta::PROTOCOL_VERSION: mp::V2026_07_28,
                    mp::meta::CLIENT_CAPABILITIES: {},
                    mp::meta::CLIENT_INFO: {
                        "name": "brutor-gateway-core",
                        "version": env!("CARGO_PKG_VERSION")
                    }
                }
            }
        });
        match send_jsonrpc(
            &state.http_client,
            &target_url,
            &probe,
            access_token,
            None,
            Some(mp::V2026_07_28),
        )
        .await
        {
            Ok(resp) => {
                let supported: Vec<String> = resp
                    .body
                    .get("result")
                    .and_then(|r| r.get("supportedVersions"))
                    .and_then(|v| v.as_array())
                    .map(|arr| {
                        arr.iter()
                            .filter_map(|v| v.as_str().map(String::from))
                            .collect()
                    })
                    .unwrap_or_default();
                let ttl_ms = resp.body.pointer("/result/ttlMs").and_then(|v| v.as_u64());
                let extensions = resp
                    .body
                    .pointer("/result/capabilities/extensions")
                    .filter(|e| e.is_object())
                    .cloned();
                modern_version = supported
                    .iter()
                    .find(|v| v.as_str() == mp::V2026_07_28)
                    .or_else(|| supported.first())
                    .cloned()
                    .map(|version| ModernProbe {
                        version,
                        ttl_ms,
                        extensions,
                    });
                if let Some(ref probe) = modern_version {
                    tracing::info!(
                        mcp_server_id = %server.id,
                        protocol_version = %probe.version,
                        ttl_ms = ?probe.ttl_ms,
                        extensions = ?probe.extensions,
                        "server/discover answered — upstream speaks the stateless dialect"
                    );
                }
            }
            Err(e) => {
                tracing::debug!(
                    mcp_server_id = %server.id,
                    error = %e,
                    "server/discover probe failed — falling back to legacy handshake"
                );
            }
        }
    }

    if let Some(probe) = modern_version {
        let ModernProbe {
            version,
            ttl_ms: discover_ttl_ms,
            extensions,
        } = probe;
        // Stateless flow: no handshake, no session, no GET listener
        // (modern servers 405 it). Every list request carries `_meta`.
        let mv = Some(version.as_str());
        let tools = discover_list(
            &state.http_client,
            &target_url,
            access_token,
            None,
            "tools/list",
            "tools",
            mv,
        )
        .await;
        let resources = discover_list(
            &state.http_client,
            &target_url,
            access_token,
            None,
            "resources/list",
            "resources",
            mv,
        )
        .await;
        let prompts = discover_list(
            &state.http_client,
            &target_url,
            access_token,
            None,
            "prompts/list",
            "prompts",
            mv,
        )
        .await;
        let resource_templates = discover_list(
            &state.http_client,
            &target_url,
            access_token,
            None,
            "resources/templates/list",
            "resourceTemplates",
            mv,
        )
        .await;
        // SEP-2549 — remember the upstream's advertised freshness
        // window so the lazy refresh honors it (`capabilities_expired`).
        let now_epoch = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let mut capabilities = json!({
            "tools": tools,
            "resources": resources,
            "prompts": prompts,
            "resource_templates": resource_templates,
        });
        if let Some(ttl_ms) = discover_ttl_ms {
            capabilities["_brutor"] = json!({"ttl_ms": ttl_ms, "fetched_epoch": now_epoch});
        }
        // SEP-2133 — persist the negotiated extensions map so admins
        // (and future per-extension policy) can see what an upstream
        // speaks: MCP Apps, Tasks, auth extensions, vendor extensions.
        if let Some(ext) = extensions {
            capabilities["extensions"] = ext;
        }
        return Ok(CapabilityDiscovery {
            capabilities,
            wants_listener: false,
            protocol_version: Some(version),
            dialect: Some(mp::dialect::STATELESS.to_string()),
        });
    }

    // Step 1: Initialize (legacy handshake dialect)
    let init_req = json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {
                "name": "brutor-gateway-core",
                "version": env!("CARGO_PKG_VERSION")
            }
        }
    });

    let init_resp = send_jsonrpc(
        &state.http_client,
        &target_url,
        &init_req,
        access_token,
        None,
        None,
    )
    .await?;

    // Extract Mcp-Session-Id from response if provided (needed for subsequent requests)
    let session_id = init_resp.session_id;

    // The version the upstream actually negotiated — persisted so the
    // proxy knows which dialect/version each server speaks.
    let negotiated_version: Option<String> = init_resp
        .body
        .get("result")
        .and_then(|r| r.get("protocolVersion"))
        .and_then(|v| v.as_str())
        .map(String::from);

    // Derive the listener-affinity flag from the initialize
    // response BEFORE we move on — once the discovery proceeds
    // to the list calls, we no longer hold the init body.
    let wants_listener = derive_wants_listener(&init_resp.body);
    tracing::debug!(
        mcp_server_id = %server.id,
        wants_listener,
        "Derived wants_listener from initialize-response capabilities"
    );

    // Step 2: Send initialized notification
    let initialized_notif = json!({
        "jsonrpc": "2.0",
        "method": "notifications/initialized"
    });
    // Fire-and-forget notification (no id = no response expected)
    let _ = send_jsonrpc(
        &state.http_client,
        &target_url,
        &initialized_notif,
        access_token,
        session_id.as_deref(),
        None,
    )
    .await;

    // Step 3: Discover each capability type
    let tools = discover_list(
        &state.http_client,
        &target_url,
        access_token,
        session_id.as_deref(),
        "tools/list",
        "tools",
        None,
    )
    .await;
    let resources = discover_list(
        &state.http_client,
        &target_url,
        access_token,
        session_id.as_deref(),
        "resources/list",
        "resources",
        None,
    )
    .await;
    let prompts = discover_list(
        &state.http_client,
        &target_url,
        access_token,
        session_id.as_deref(),
        "prompts/list",
        "prompts",
        None,
    )
    .await;
    let resource_templates = discover_list(
        &state.http_client,
        &target_url,
        access_token,
        session_id.as_deref(),
        "resources/templates/list",
        "resourceTemplates",
        None,
    )
    .await;

    Ok(CapabilityDiscovery {
        capabilities: json!({
            "tools": tools,
            "resources": resources,
            "prompts": prompts,
            "resource_templates": resource_templates,
        }),
        wants_listener,
        protocol_version: negotiated_version,
        dialect: Some(mp::dialect::HANDSHAKE.to_string()),
    })
}

struct JsonRpcResponse {
    body: Value,
    session_id: Option<String>,
}

/// Send a JSON-RPC request to the MCP server and return the parsed response.
async fn send_jsonrpc(
    client: &reqwest::Client,
    url: &str,
    payload: &Value,
    access_token: &str,
    session_id: Option<&str>,
    modern_version: Option<&str>,
) -> Result<JsonRpcResponse, String> {
    let mut req = client
        .post(url)
        .header("Content-Type", "application/json")
        .header("Accept", "application/json, text/event-stream")
        .header("Authorization", format!("Bearer {access_token}"))
        .json(payload);

    if let Some(sid) = session_id {
        req = req.header("Mcp-Session-Id", sid);
    }

    // Speaking the 2026-07-28 dialect: the version header is
    // required on every POST, Mcp-Method on every request. (Mcp-Name is
    // only defined for tools/call / resources/read / prompts/get — none
    // of which discovery sends.)
    if let Some(version) = modern_version {
        req = req.header(
            crate::constants::mcp_protocol::headers::PROTOCOL_VERSION,
            version,
        );
        if let Some(method) = payload.get("method").and_then(|m| m.as_str()) {
            req = req.header(crate::constants::mcp_protocol::headers::METHOD, method);
        }
    }

    let resp = req
        .send()
        .await
        .map_err(|e| format!("HTTP request failed: {e}"))?;

    let session_id = resp
        .headers()
        .get("mcp-session-id")
        .and_then(|v| v.to_str().ok())
        .map(String::from);

    let status = resp.status();
    let content_type = resp
        .headers()
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    let resp_text = resp
        .text()
        .await
        .map_err(|e| format!("Failed to read response: {e}"))?;

    if !status.is_success() {
        return Err(format!("Server returned {status}: {resp_text}"));
    }

    // Handle SSE responses — some servers return text/event-stream even for
    // non-streaming requests.  Extract the first JSON-RPC result from the
    // event stream.
    let body = if content_type.contains("text/event-stream") {
        parse_sse_jsonrpc(&resp_text)?
    } else {
        serde_json::from_str(&resp_text).map_err(|e| format!("Invalid JSON response: {e}"))?
    };

    Ok(JsonRpcResponse { body, session_id })
}

/// Parse the first JSON-RPC result out of an SSE event stream.
fn parse_sse_jsonrpc(sse_text: &str) -> Result<Value, String> {
    for line in sse_text.lines() {
        if let Some(data) = line.strip_prefix("data: ") {
            let data = data.trim();
            if data.is_empty() {
                continue;
            }
            if let Ok(parsed) = serde_json::from_str::<Value>(data) {
                // Return the first valid JSON-RPC message that has a result
                if parsed.get("result").is_some() || parsed.get("error").is_some() {
                    return Ok(parsed);
                }
            }
        }
    }
    Err("No JSON-RPC result found in SSE stream".into())
}

/// Discover a single capability list (e.g. tools/list → "tools" array).
/// Returns an empty array on any error.
async fn discover_list(
    client: &reqwest::Client,
    url: &str,
    access_token: &str,
    session_id: Option<&str>,
    method: &str,
    result_key: &str,
    modern_version: Option<&str>,
) -> Vec<Value> {
    // 2026-07-28 requests must carry protocolVersion + clientCapabilities
    // in params._meta on EVERY request; legacy requests keep empty params.
    let params = match modern_version {
        Some(version) => json!({
            "_meta": {
                crate::constants::mcp_protocol::meta::PROTOCOL_VERSION: version,
                crate::constants::mcp_protocol::meta::CLIENT_CAPABILITIES: {},
                crate::constants::mcp_protocol::meta::CLIENT_INFO: {
                    "name": "brutor-gateway-core",
                    "version": env!("CARGO_PKG_VERSION")
                }
            }
        }),
        None => json!({}),
    };
    let req = json!({
        "jsonrpc": "2.0",
        "id": 2,
        "method": method,
        "params": params
    });

    match send_jsonrpc(client, url, &req, access_token, session_id, modern_version).await {
        Ok(resp) => resp
            .body
            .get("result")
            .and_then(|r| r.get(result_key))
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default(),
        Err(e) => {
            tracing::debug!(method, error = %e, "Capability list discovery failed (server may not support this)");
            vec![]
        }
    }
}

// =============================================================================
// RFC 7591 Dynamic Client Registration — thin proxy
// =============================================================================

// /// Proxy RFC 7591 Dynamic Client Registration requests to the IdP's
// /// registration endpoint. This allows MCP clients following the OIDC
// /// discovery document to register through the gateway.
// ════════════════════════════════════════════════════════════════════
// CIMD authorize bridge. See services::cimd for the
// three-leg design. Non-CIMD client ids pass through untouched.
// ════════════════════════════════════════════════════════════════════
/// Inbound authorize endpoint. Advertised in the rewritten IdP
/// metadata; user-agents land here from MCP clients building an
/// authorization-code flow.
pub(crate) async fn authorize(
    State(state): State<SharedState>,
    Path((mcp_server_id, idp_server_id)): Path<(String, String)>,
    axum::extract::Query(params): axum::extract::Query<std::collections::HashMap<String, String>>,
) -> Response {
    let tenant_id = match extract_tenant_id(&state, None, &mcp_server_id).await {
        Ok(id) => id,
        Err(resp) => return resp,
    };
    let idp = match load_idp(&state, &idp_server_id, &tenant_id).await {
        Ok(s) => s,
        Err(resp) => return resp,
    };
    let Some(idp_authorize) = idp.authorization_endpoint.clone().filter(|e| !e.is_empty()) else {
        return error_response(StatusCode::BAD_REQUEST, "IdP has no authorization endpoint");
    };

    let client_id = params.get("client_id").cloned().unwrap_or_default();

    // Registered (non-CIMD) clients: pure pass-through — same query, the
    // client's own redirect_uri, the IdP does everything. Behavior is
    // identical to the pre-bridge direct flow.
    if !crate::services::cimd::is_cimd_client_id(&client_id) {
        let qs = serde_urlencoded::to_string(&params).unwrap_or_default();
        let sep = if idp_authorize.contains('?') {
            '&'
        } else {
            '?'
        };
        return axum::response::Redirect::temporary(&format!("{idp_authorize}{sep}{qs}"))
            .into_response();
    }

    // ── CIMD path ──────────────────────────────────────────────────
    // Validate the request shape first — a malformed authorize is a 400
    // regardless of whether the bridge's Redis backend is available.
    if params.get("response_type").map(String::as_str) != Some("code") {
        return error_response(StatusCode::BAD_REQUEST, "response_type must be 'code'");
    }
    // Public clients — PKCE S256 is mandatory.
    let Some(code_challenge) = params.get("code_challenge").cloned() else {
        return error_response(
            StatusCode::BAD_REQUEST,
            "CIMD clients must use PKCE (code_challenge missing)",
        );
    };
    if params.get("code_challenge_method").map(String::as_str) != Some("S256") {
        return error_response(
            StatusCode::BAD_REQUEST,
            "code_challenge_method must be S256",
        );
    }
    let Some(redirect_uri) = params.get("redirect_uri").cloned() else {
        return error_response(StatusCode::BAD_REQUEST, "redirect_uri is required");
    };

    let Some(redis) = state.redis.clone() else {
        return error_response(
            StatusCode::SERVICE_UNAVAILABLE,
            "CIMD bridging requires Redis",
        );
    };
    let mut redis = redis;

    // Fetch + validate the metadata document (SSRF-guarded).
    let doc_json = match crate::services::cimd::fetch_document(&client_id).await {
        Ok(d) => d,
        Err(e) => {
            tracing::warn!(cimd_url = %client_id, error = %e, "CIMD document fetch rejected");
            return error_response(StatusCode::BAD_REQUEST, &e);
        }
    };
    let doc = match crate::services::cimd::validate_doc(&client_id, &doc_json) {
        Ok(d) => d,
        Err(e) => return error_response(StatusCode::BAD_REQUEST, &e),
    };
    if !doc.redirect_uris.iter().any(|u| u == &redirect_uri) {
        return error_response(
            StatusCode::BAD_REQUEST,
            "redirect_uri is not listed in the CIMD document",
        );
    }

    let gateway_base = gateway_base(&state);
    let gateway_issuer = format!("{gateway_base}/v1/proxy/oauth/{mcp_server_id}/{idp_server_id}");
    let gateway_callback = format!("{gateway_issuer}/callback");

    // One gateway-owned IdP client per (idp, cimd_url), created lazily.
    let (idp_client_id, idp_client_secret_enc) = match ensure_cimd_idp_client(
        &state,
        &tenant_id,
        &idp,
        &client_id,
        &doc,
        &gateway_callback,
        &doc_json,
    )
    .await
    {
        Ok(pair) => pair,
        Err(e) => return error_response(StatusCode::BAD_GATEWAY, &e),
    };

    // Stash the client leg; send the user-agent upstream with OUR leg.
    let gateway_verifier = crate::services::cimd::random_token(32);
    let bridge_id = crate::services::cimd::random_token(24);
    let bridge = crate::services::cimd::BridgeState {
        tenant_id: tenant_id.clone(),
        mcp_server_id: mcp_server_id.clone(),
        idp_server_id: idp_server_id.clone(),
        cimd_url: client_id.clone(),
        client_redirect_uri: redirect_uri,
        client_state: params.get("state").cloned(),
        client_code_challenge: code_challenge,
        gateway_verifier: gateway_verifier.clone(),
        gateway_callback: gateway_callback.clone(),
        idp_client_id: idp_client_id.clone(),
        idp_client_secret_enc,
    };
    if let Err(e) = crate::services::cimd::redis_put_json(
        &mut redis,
        &format!("{}{bridge_id}", crate::services::cimd::BRIDGE_KEY_PREFIX),
        &bridge,
        crate::services::cimd::BRIDGE_TTL_SECS,
    )
    .await
    {
        return error_response(StatusCode::INTERNAL_SERVER_ERROR, &e);
    }

    let mut upstream = vec![
        ("response_type".to_string(), "code".to_string()),
        ("client_id".to_string(), idp_client_id),
        ("redirect_uri".to_string(), gateway_callback),
        ("state".to_string(), bridge_id),
        (
            "code_challenge".to_string(),
            crate::services::cimd::s256_challenge(&gateway_verifier),
        ),
        ("code_challenge_method".to_string(), "S256".to_string()),
    ];
    for passthrough in ["scope", "resource", "audience", "prompt", "login_hint"] {
        if let Some(v) = params.get(passthrough) {
            upstream.push((passthrough.to_string(), v.clone()));
        }
    }
    let qs = serde_urlencoded::to_string(&upstream).unwrap_or_default();
    let sep = if idp_authorize.contains('?') {
        '&'
    } else {
        '?'
    };
    tracing::info!(
        cimd_url = %client_id,
        idp_id = %idp_server_id,
        "CIMD authorize bridged to IdP"
    );
    axum::response::Redirect::temporary(&format!("{idp_authorize}{sep}{qs}")).into_response()
}

/// IdP redirect target for bridged flows: swap the IdP code for a
/// gateway `gwc_…` code and bounce the user-agent back to the CIMD
/// client's own redirect_uri.
pub(crate) async fn cimd_callback(
    State(state): State<SharedState>,
    Path((mcp_server_id, idp_server_id)): Path<(String, String)>,
    axum::extract::Query(params): axum::extract::Query<std::collections::HashMap<String, String>>,
) -> Response {
    let Some(redis) = state.redis.clone() else {
        return error_response(
            StatusCode::SERVICE_UNAVAILABLE,
            "CIMD bridging requires Redis",
        );
    };
    let mut redis = redis;

    let Some(bridge_id) = params.get("state") else {
        return error_response(StatusCode::BAD_REQUEST, "Missing state");
    };
    let bridge: crate::services::cimd::BridgeState = match crate::services::cimd::redis_take_json(
        &mut redis,
        &format!("{}{bridge_id}", crate::services::cimd::BRIDGE_KEY_PREFIX),
    )
    .await
    {
        Ok(Some(b)) => b,
        Ok(None) => {
            return error_response(
                StatusCode::BAD_REQUEST,
                "Unknown or expired authorization request",
            );
        }
        Err(e) => return error_response(StatusCode::INTERNAL_SERVER_ERROR, &e),
    };

    // The state opened a flow for one server and IdP; it completes there.
    if bridge.mcp_server_id != mcp_server_id || bridge.idp_server_id != idp_server_id {
        return error_response(
            StatusCode::BAD_REQUEST,
            "Unknown or expired authorization request",
        );
    }

    let redirect_sep = if bridge.client_redirect_uri.contains('?') {
        '&'
    } else {
        '?'
    };

    // IdP-side error → propagate to the client per RFC 6749 §4.1.2.1.
    if let Some(error) = params.get("error") {
        let mut q = vec![("error".to_string(), error.clone())];
        if let Some(d) = params.get("error_description") {
            q.push(("error_description".to_string(), d.clone()));
        }
        if let Some(s) = bridge.client_state {
            q.push(("state".to_string(), s));
        }
        let qs = serde_urlencoded::to_string(&q).unwrap_or_default();
        return axum::response::Redirect::temporary(&format!(
            "{}{redirect_sep}{qs}",
            bridge.client_redirect_uri
        ))
        .into_response();
    }

    let Some(upstream_code) = params.get("code").cloned() else {
        return error_response(StatusCode::BAD_REQUEST, "Missing code");
    };

    let gw_code = format!(
        "{}{}",
        crate::services::cimd::GW_CODE_PREFIX,
        crate::services::cimd::random_token(32)
    );
    let client_state = bridge.client_state.clone();
    let client_redirect = bridge.client_redirect_uri.clone();
    let code_state = crate::services::cimd::CodeState {
        bridge,
        upstream_code,
    };
    if let Err(e) = crate::services::cimd::redis_put_json(
        &mut redis,
        &format!("{}{gw_code}", crate::services::cimd::CODE_KEY_PREFIX),
        &code_state,
        crate::services::cimd::CODE_TTL_SECS,
    )
    .await
    {
        return error_response(StatusCode::INTERNAL_SERVER_ERROR, &e);
    }

    let mut q = vec![("code".to_string(), gw_code)];
    if let Some(s) = client_state {
        q.push(("state".to_string(), s));
    }
    let qs = serde_urlencoded::to_string(&q).unwrap_or_default();
    axum::response::Redirect::temporary(&format!("{client_redirect}{redirect_sep}{qs}"))
        .into_response()
}

/// Lazily DCR one gateway-owned client at the IdP per (idp, cimd_url)
/// and persist it in `oauth_cimd_clients`. Subsequent authorizes
/// reuse the row; the fetched document is cached on it too.
async fn ensure_cimd_idp_client(
    state: &SharedState,
    tenant_id: &str,
    idp: &IdpServer,
    cimd_url: &str,
    doc: &crate::services::cimd::CimdDoc,
    gateway_callback: &str,
    raw_doc: &Value,
) -> Result<(String, Option<String>), String> {
    let existing: Option<(String, Option<String>)> = sqlx::query_as(
        "SELECT idp_client_id, idp_client_secret_encrypted FROM oauth_cimd_clients \
         WHERE tenant_id = $1 AND idp_id = $2 AND cimd_url = $3",
    )
    .bind(tenant_id)
    .bind(&idp.id)
    .bind(cimd_url)
    .fetch_optional(&state.db)
    .await
    .map_err(|e| format!("cimd client lookup failed: {e}"))?;

    if let Some((client_id, secret_enc)) = existing {
        // Refresh the cached doc opportunistically (best-effort).
        let _ = sqlx::query(
            "UPDATE oauth_cimd_clients SET doc = $1, doc_fetched_at = NOW() \
             WHERE tenant_id = $2 AND idp_id = $3 AND cimd_url = $4",
        )
        .bind(raw_doc)
        .bind(tenant_id)
        .bind(&idp.id)
        .bind(cimd_url)
        .execute(&state.db)
        .await;
        return Ok((client_id, secret_enc));
    }

    let registration_endpoint = idp
        .registration_endpoint
        .clone()
        .filter(|e| !e.is_empty())
        .ok_or("IdP does not support dynamic client registration — CIMD bridging unavailable")?;

    let display_name = doc
        .client_name
        .clone()
        .unwrap_or_else(|| cimd_url.to_string());
    let registration = json!({
        "client_name": format!("Brutor CIMD bridge — {display_name}"),
        "redirect_uris": [gateway_callback],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post",
        "application_type": "web"
    });
    let resp = state
        .http_client
        .post(&registration_endpoint)
        .header("content-type", "application/json")
        .json(&registration)
        .send()
        .await
        .map_err(|e| format!("IdP registration for CIMD bridge failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!(
            "IdP registration for CIMD bridge returned {}",
            resp.status()
        ));
    }
    let body: Value = resp
        .json()
        .await
        .map_err(|e| format!("IdP registration response invalid: {e}"))?;
    let idp_client_id = body
        .get("client_id")
        .and_then(|v| v.as_str())
        .ok_or("IdP registration response missing client_id")?
        .to_string();
    let secret_enc = match body.get("client_secret").and_then(|v| v.as_str()) {
        Some(secret) => Some(
            state
                .encryption
                .encrypt(secret)
                .map_err(|e| format!("secret encryption failed: {e}"))?,
        ),
        None => None,
    };

    sqlx::query(
        "INSERT INTO oauth_cimd_clients \
         (id, tenant_id, idp_id, cimd_url, idp_client_id, idp_client_secret_encrypted, doc, doc_fetched_at, created_at, updated_at) \
         VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), NOW(), NOW()) \
         ON CONFLICT (tenant_id, idp_id, cimd_url) DO UPDATE SET \
         idp_client_id = EXCLUDED.idp_client_id, \
         idp_client_secret_encrypted = EXCLUDED.idp_client_secret_encrypted, \
         doc = EXCLUDED.doc, doc_fetched_at = NOW(), updated_at = NOW()",
    )
    .bind(format!("cimd-{}", crate::services::cimd::random_token(12)))
    .bind(tenant_id)
    .bind(&idp.id)
    .bind(cimd_url)
    .bind(&idp_client_id)
    .bind(&secret_enc)
    .bind(raw_doc)
    .execute(&state.db)
    .await
    .map_err(|e| format!("cimd client persist failed: {e}"))?;

    tracing::info!(
        cimd_url,
        idp_id = %idp.id,
        "Registered gateway-owned IdP client for CIMD bridge"
    );
    Ok((idp_client_id, secret_enc))
}

pub(crate) async fn register_client(
    State(state): State<SharedState>,
    Path((_mcp_server_id, idp_server_id)): Path<(String, String)>,
    headers: HeaderMap,
    body: axum::body::Body,
) -> Response {
    let tenant_id = match headers
        .get("x-tenant-id")
        .and_then(|v| v.to_str().ok())
        .map(String::from)
    {
        Some(t) => t,
        None => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": "Missing tenant context"})),
            )
                .into_response();
        }
    };

    // Load IdP
    let idp: Result<Option<IdpServer>, sqlx::Error> = sqlx::query_as(
        "SELECT * FROM idp_servers WHERE id = $1 AND tenant_id = $2 AND enabled = true \
         AND protocol = 'oidc'",
    )
    .bind(&idp_server_id)
    .bind(&tenant_id)
    .fetch_optional(&state.db)
    .await;
    let idp = match idp {
        Ok(row) => row,
        Err(e) => {
            tracing::error!(error = %e, "oauth: identity provider lookup failed");
            return db_unavailable();
        }
    };

    let idp = match idp {
        Some(i) => i,
        None => {
            return (
                StatusCode::NOT_FOUND,
                Json(json!({"error": "Identity provider not found"})),
            )
                .into_response();
        }
    };
    // Refuse an IdP whose endpoints the outbound policy denies.
    let idp = match crate::services::oauth_token_manager::check_idp_destinations(&idp) {
        Ok(()) => idp,
        Err(_) => {
            return (
                StatusCode::FORBIDDEN,
                Json(json!({"error": "Identity provider endpoint is refused by the outbound policy"})),
            )
                .into_response();
        }
    };

    let registration_endpoint = match idp.registration_endpoint {
        Some(ref ep) if !ep.is_empty() => ep.clone(),
        _ => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": "IdP does not support dynamic client registration"})),
            )
                .into_response();
        }
    };

    // Read and forward the request body
    let body_bytes = match axum::body::to_bytes(body, 64 * 1024).await {
        Ok(b) => b,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": format!("Invalid request body: {e}")})),
            )
                .into_response();
        }
    };

    // SEP-837: DCR `application_type` shaping.
    // CLI/desktop MCP clients frequently omit `application_type`, and
    // IdPs then default to "web", giving native clients web-client
    // security posture. When absent, infer it from the redirect URIs:
    // loopback or custom-scheme redirects → "native"; else "web".
    // Explicit values are never overridden.
    let forwarded_body: Vec<u8> = match serde_json::from_slice::<Value>(&body_bytes) {
        Ok(mut reg) if reg.get("application_type").is_none() => {
            let is_native = reg
                .get("redirect_uris")
                .and_then(|u| u.as_array())
                .map(|uris| {
                    uris.iter().filter_map(|u| u.as_str()).any(|u| {
                        u.starts_with("http://localhost")
                            || u.starts_with("http://127.0.0.1")
                            || (!u.starts_with("http://") && !u.starts_with("https://"))
                    })
                })
                .unwrap_or(false);
            let app_type = if is_native { "native" } else { "web" };
            tracing::info!(
                inferred_application_type = app_type,
                "DCR request had no application_type — inferred from redirect_uris (SEP-837)"
            );
            reg["application_type"] = Value::String(app_type.into());
            serde_json::to_vec(&reg).unwrap_or_else(|_| body_bytes.to_vec())
        }
        _ => body_bytes.to_vec(),
    };

    // Forward to IdP registration endpoint
    let resp = state
        .http_client
        .post(&registration_endpoint)
        .header("content-type", "application/json")
        .body(forwarded_body)
        .send()
        .await;

    match resp {
        Ok(upstream_resp) => {
            let status = upstream_resp.status();
            let resp_body = upstream_resp.bytes().await.unwrap_or_default();

            tracing::debug!(
                idp_id = %idp_server_id,
                registration_endpoint = %registration_endpoint,
                status = %status,
                "DCR registration proxy response"
            );

            let response = axum::response::Response::builder()
                .status(status.as_u16())
                .header("content-type", "application/json");
            response.body(Body::from(resp_body)).unwrap()
        }
        Err(e) => {
            tracing::warn!(
                idp_id = %idp_server_id,
                error = %e,
                "DCR registration proxy failed"
            );
            (
                StatusCode::BAD_GATEWAY,
                Json(json!({"error": format!("Failed to reach IdP registration endpoint: {e}")})),
            )
                .into_response()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn listener_is_wanted_only_for_what_a_server_can_push() {
        for caps in [
            json!({"tools": {"listChanged": true}}),
            json!({"resources": {"listChanged": true}}),
            json!({"prompts": {"listChanged": true}}),
            json!({"resources": {"subscribe": true}}),
            json!({"logging": {}}),
        ] {
            assert!(
                derive_wants_listener(&json!({"result": {"capabilities": caps.clone()}})),
                "{caps}"
            );
        }
        for caps in [
            json!({}),
            json!({"tools": {"listChanged": false}}),
            json!({"experimental": {}}),
        ] {
            assert!(
                !derive_wants_listener(&json!({"result": {"capabilities": caps.clone()}})),
                "{caps}"
            );
        }
        // Unknown shapes keep the old behaviour: assume it is wanted.
        assert!(derive_wants_listener(&json!({"result": {}})));
        assert!(derive_wants_listener(
            &json!({"result": {"capabilities": []}})
        ));
        assert!(derive_wants_listener(&json!({})));
    }

    #[test]
    fn the_first_jsonrpc_answer_is_taken_from_an_sse_stream() {
        let v = parse_sse_jsonrpc(
            "event: x\ndata: \ndata: not-json\ndata: {\"method\":\"note\"}\n\
             data: {\"id\":1,\"result\":{\"a\":1}}\ndata: {\"id\":2,\"result\":{\"a\":2}}\n",
        )
        .unwrap();
        assert_eq!(v["result"]["a"], 1);
        assert!(parse_sse_jsonrpc("data: {\"error\":{\"code\":1}}").is_ok());
        assert!(parse_sse_jsonrpc("data: {\"method\":\"note\"}\n").is_err());
        assert!(parse_sse_jsonrpc("").is_err());
    }

    #[test]
    fn the_anonymous_context_carries_no_identity() {
        let a = anonymous_auth("t");
        assert_eq!(a.tenant_id, "t");
        assert!(a.gateway_user_id.is_none());
        assert!(a.api_key_id.is_none());
        assert_eq!(a.auth_type, crate::models::auth::AuthType::Anonymous);
    }

    #[cfg(feature = "db-tests")]
    mod cimd_registration {
        use super::*;
        use crate::config::AppConfig;
        use crate::services::AppState;
        use crate::services::telemetry::TelemetryState;
        use std::sync::Arc;
        use wiremock::matchers::{method, path};
        use wiremock::{Mock, MockServer, ResponseTemplate};

        const CIMD: &str = "https://cli.example.com/client.json";

        fn state(pool: sqlx::PgPool) -> SharedState {
            Arc::new(AppState::new(
                AppConfig::for_test("postgresql://placeholder/replaced".to_string()),
                pool,
                None,
                reqwest::Client::new(),
                TelemetryState {
                    prometheus_handle: None,
                },
            ))
        }

        async fn seed_tenant(pool: &sqlx::PgPool) {
            sqlx::query(
                "INSERT INTO tenants (id, name, display_name, primary_contact_email, is_active, \
                 is_default, is_trial, suspended, features, settings) \
                 VALUES ('t', 't', 't', 'a@test.local', true, false, false, false, '{}'::json, \
                 '{}'::json) ON CONFLICT (id) DO NOTHING",
            )
            .execute(pool)
            .await
            .unwrap();
        }

        async fn idp(pool: &sqlx::PgPool, registration: Option<&str>) -> IdpServer {
            seed_tenant(pool).await;
            sqlx::query(
                "INSERT INTO idp_servers (id, tenant_id, name, issuer, type, registration_endpoint, \
                 supports_pkce, supports_refresh_tokens, supports_client_credentials, \
                 token_endpoint_auth_methods, scopes_supported, response_types_supported, \
                 grant_types_supported, discovery_cache_ttl, enabled, verified, protocol) \
                 VALUES ('idp', 't', 'idp', 'https://i', 'oidc', $1, true, true, false, '[]'::json, \
                 '[]'::json, '[]'::json, '[]'::json, 3600, true, false, 'oidc')",
            )
            .bind(registration)
            .execute(pool)
            .await
            .unwrap();
            sqlx::query_as("SELECT * FROM idp_servers WHERE id = 'idp'")
                .fetch_one(pool)
                .await
                .unwrap()
        }

        fn doc() -> crate::services::cimd::CimdDoc {
            crate::services::cimd::CimdDoc {
                client_id: CIMD.into(),
                redirect_uris: vec!["http://127.0.0.1/cb".into()],
                client_name: Some("CLI".into()),
            }
        }

        async fn ensure(
            s: &SharedState,
            i: &IdpServer,
        ) -> Result<(String, Option<String>), String> {
            ensure_cimd_idp_client(s, "t", i, CIMD, &doc(), "https://gw/cb", &json!({"v": 1})).await
        }

        /// One gateway-owned client per (idp, document): registered once, its
        /// secret stored encrypted, reused after.
        #[sqlx::test(migrations = "tests/migrations")]
        async fn a_client_is_registered_once_and_its_secret_encrypted(pool: sqlx::PgPool) {
            let mock = MockServer::start().await;
            Mock::given(method("POST"))
                .and(path("/reg"))
                .respond_with(ResponseTemplate::new(201).set_body_json(json!({
                    "client_id": "gw-client", "client_secret": "gw-secret",
                })))
                .expect(1)
                .mount(&mock)
                .await;
            let i = idp(&pool, Some(&format!("{}/reg", mock.uri()))).await;
            let s = state(pool.clone());

            let (id, secret) = ensure(&s, &i).await.unwrap();
            assert_eq!(id, "gw-client");
            let secret = secret.expect("a secret");
            assert_ne!(secret, "gw-secret", "stored encrypted");
            assert_eq!(s.encryption.decrypt(&secret).unwrap(), "gw-secret");

            let sent: Value =
                serde_json::from_slice(&mock.received_requests().await.unwrap()[0].body).unwrap();
            assert_eq!(sent["redirect_uris"], json!(["https://gw/cb"]));
            assert!(sent["client_name"].as_str().unwrap().contains("CLI"));

            // The second call reuses the row, and refreshes the cached document.
            let (again, _) = ensure_cimd_idp_client(
                &s,
                "t",
                &i,
                CIMD,
                &doc(),
                "https://gw/cb",
                &json!({"v": 2}),
            )
            .await
            .unwrap();
            assert_eq!(again, "gw-client");
            let stored: Value =
                sqlx::query_scalar("SELECT doc::json FROM oauth_cimd_clients WHERE cimd_url = $1")
                    .bind(CIMD)
                    .fetch_one(&pool)
                    .await
                    .unwrap();
            assert_eq!(stored["v"], 2);
        }

        #[sqlx::test(migrations = "tests/migrations")]
        async fn registration_failures_are_refused_and_nothing_is_stored(pool: sqlx::PgPool) {
            let i = idp(&pool, None).await;
            let s = state(pool.clone());
            assert!(ensure(&s, &i).await.unwrap_err().contains("dynamic client"));

            let mock = MockServer::start().await;
            let mut i = i;
            i.registration_endpoint = Some(format!("{}/reg", mock.uri()));
            Mock::given(method("POST"))
                .respond_with(ResponseTemplate::new(500))
                .up_to_n_times(1)
                .mount(&mock)
                .await;
            assert!(ensure(&s, &i).await.unwrap_err().contains("500"));
            Mock::given(method("POST"))
                .respond_with(ResponseTemplate::new(200).set_body_string("nope"))
                .up_to_n_times(1)
                .mount(&mock)
                .await;
            assert!(ensure(&s, &i).await.unwrap_err().contains("invalid"));
            Mock::given(method("POST"))
                .respond_with(ResponseTemplate::new(200).set_body_json(json!({})))
                .up_to_n_times(1)
                .mount(&mock)
                .await;
            assert!(
                ensure(&s, &i)
                    .await
                    .unwrap_err()
                    .contains("missing client_id")
            );

            i.registration_endpoint = Some("http://127.0.0.1:1/reg".into());
            assert!(ensure(&s, &i).await.unwrap_err().contains("failed"));

            let n: i64 = sqlx::query_scalar("SELECT count(*) FROM oauth_cimd_clients")
                .fetch_one(&pool)
                .await
                .unwrap();
            assert_eq!(n, 0);
        }

        /// A public client (no secret) is a valid registration.
        #[sqlx::test(migrations = "tests/migrations")]
        async fn a_public_registration_has_no_secret(pool: sqlx::PgPool) {
            let mock = MockServer::start().await;
            Mock::given(method("POST"))
                .respond_with(
                    ResponseTemplate::new(200).set_body_json(json!({"client_id": "public"})),
                )
                .mount(&mock)
                .await;
            let i = idp(&pool, Some(&format!("{}/reg", mock.uri()))).await;
            let (id, secret) = ensure(&state(pool.clone()), &i).await.unwrap();
            assert_eq!((id.as_str(), secret), ("public", None));
        }

        /// The capability freshness window, on real rows.
        #[sqlx::test(migrations = "tests/migrations")]
        async fn capability_state_on_a_server_row(pool: sqlx::PgPool) {
            seed_tenant(&pool).await;
            sqlx::query(
                "INSERT INTO mcp_servers (id, tenant_id, name, base_url, mcp_endpoint_path, enabled) \
                 VALUES ('s', 't', 's', 'https://u', '', true)",
            )
            .execute(&pool)
            .await
            .unwrap();
            let row = |caps: Option<Value>| {
                let pool = pool.clone();
                async move {
                    sqlx::query("UPDATE mcp_servers SET capabilities = $1 WHERE id = 's'")
                        .bind(caps)
                        .execute(&pool)
                        .await
                        .unwrap();
                    let s: McpServer = sqlx::query_as("SELECT * FROM mcp_servers WHERE id = 's'")
                        .fetch_one(&pool)
                        .await
                        .unwrap();
                    s
                }
            };
            assert!(!has_capabilities(&row(None).await));
            assert!(!has_capabilities(&row(Some(json!({}))).await));
            assert!(!has_capabilities(
                &row(Some(json!({"tools": [], "resources": []}))).await
            ));
            assert!(!has_capabilities(&row(Some(json!([1]))).await));
            assert!(has_capabilities(
                &row(Some(json!({"tools": [{"name": "t"}]}))).await
            ));
            assert!(has_capabilities(
                &row(Some(json!({"note": "not an array"}))).await
            ));

            assert!(!capabilities_expired(&row(None).await));
            assert!(!capabilities_expired(
                &row(Some(json!({"tools": []}))).await
            ));
            let now = chrono::Utc::now().timestamp();
            let fresh = json!({"_brutor": {"ttl_ms": 60_000, "fetched_epoch": now}});
            assert!(!capabilities_expired(&row(Some(fresh)).await));
            let stale = json!({"_brutor": {"ttl_ms": 60_000, "fetched_epoch": now - 3600}});
            assert!(capabilities_expired(&row(Some(stale)).await));
        }
    }
}
