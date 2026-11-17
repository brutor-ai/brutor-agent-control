// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Authentication for the routes the control plane calls on the core
//! (`/v1/audit/*`, `/v1/assurance/*`).
//!
//! These are backends for admin features, not runtime proxy surfaces.
//! They used to check nothing at all — `/v1/audit` was reachable by
//! anyone on the proxy port, and `/v1/assurance` by anyone who typed a
//! tenant id into `X-Tenant-ID`. The control plane always sent the
//! `BRUTOR_GATEWAY_SERVICE_API_KEY` (a `global`-scope API key) and the core
//! simply never read it.
//!
//! One rule for all of them: the caller must present either that
//! service key or an admin session token issued by the control plane.
//! Which of those is enough for a given route is the route's decision,
//! made with [`ServicePrincipal::require_system_scope`] or
//! [`ServicePrincipal::require_tenant_scope`].

use axum::Json;
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use serde_json::json;

use crate::models::auth::{AuthContext, AuthType};
use crate::services::{SharedState, auth, error_envelope};

/// Who is calling a control-plane-facing route.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ServicePrincipal {
    /// The platform service key (`api_keys.is_platform_service`, set only by
    /// the control plane's seed): the control plane and the KB uploader.
    /// This used to be ANY `scope = 'global'` key, but a tenant admin
    /// mints those ("shared" keys bound to a group), so any tenant's shared
    /// key passed the system-scoped and every tenant-scoped route.
    PlatformKey,
    /// A system administrator's session token.
    SystemAdmin,
    /// A tenant administrator's session token, pinned to its tenant.
    TenantAdmin(String),
}

impl ServicePrincipal {
    /// Routes whose data is not owned by one tenant (audit chains span
    /// every tenant a proxy process served) accept the service key or a
    /// system admin only.
    pub fn require_system_scope(&self) -> Result<(), Response> {
        match self {
            ServicePrincipal::PlatformKey | ServicePrincipal::SystemAdmin => Ok(()),
            ServicePrincipal::TenantAdmin(_) => Err(deny(
                StatusCode::FORBIDDEN,
                "this route is system-scoped: a service key or system admin session is required",
            )),
        }
    }

    /// Routes that act on one tenant accept the service key, a system
    /// admin, or a tenant admin of exactly that tenant.
    pub fn require_tenant_scope(&self, tenant_id: &str) -> Result<(), Response> {
        match self {
            ServicePrincipal::PlatformKey | ServicePrincipal::SystemAdmin => Ok(()),
            ServicePrincipal::TenantAdmin(own) if own == tenant_id => Ok(()),
            ServicePrincipal::TenantAdmin(_) => Err(deny(
                StatusCode::FORBIDDEN,
                "admin session is not valid for the requested tenant",
            )),
        }
    }
}

/// Error body shared by every service-facing route. A 401 carries the
/// RFC 6750 challenge so SDKs know to re-authenticate.
pub fn deny(status: StatusCode, message: &str) -> Response {
    let mut resp = (status, Json(json!({ "error": message }))).into_response();
    if status == StatusCode::UNAUTHORIZED {
        error_envelope::attach_bearer_challenge(&mut resp);
    }
    resp
}

/// Authenticate a control-plane-facing request and classify the caller.
///
/// `tenant_id` is the tenant the request acts for (`"n/a"` when the route
/// is not tenant-scoped); it scopes API-key lookup and is compared against
/// the token's `tenant_id` claim exactly as on the proxy surfaces.
///
/// Refuses (401) anything `authenticate_request` refuses, and (403) any
/// credential that is neither the global service key nor an admin
/// session: tenant-, group- and user-scoped API keys, portal end-user
/// tokens, workload-identity tokens and anonymous callers all stop here.
pub async fn require_service_principal(
    state: &SharedState,
    headers: &HeaderMap,
    tenant_id: &str,
    client_ip: Option<String>,
) -> Result<(AuthContext, ServicePrincipal), Response> {
    let ctx = auth::authenticate_request(
        headers,
        tenant_id,
        &state.config.jwt_secret,
        &state.db,
        &state.credential_verifiers,
        client_ip,
        true,
    )
    .await
    .map_err(|e| deny(e.status_code(), &e.to_string()))?;

    let principal = match ctx.auth_type {
        AuthType::ApiKey if ctx.api_key_is_platform => ServicePrincipal::PlatformKey,
        AuthType::ApiKey => {
            return Err(deny(
                StatusCode::FORBIDDEN,
                "the platform service key is required for this route",
            ));
        }
        AuthType::GatewayJwt => {
            // `AuthContext` does not carry `admin_type`; read it from the
            // token `authenticate_request` has already verified.
            let claims = auth::gateway_claims_from_headers(
                headers,
                &state.config.jwt_secret,
                &state.credential_verifiers,
            );
            match claims.as_ref().and_then(|c| c.admin_type.as_deref()) {
                Some("system") => ServicePrincipal::SystemAdmin,
                Some("tenant") => match claims.as_ref().and_then(|c| c.tenant_id.clone()) {
                    Some(t) => ServicePrincipal::TenantAdmin(t),
                    None => {
                        return Err(deny(
                            StatusCode::FORBIDDEN,
                            "tenant admin token carries no tenant",
                        ));
                    }
                },
                _ => {
                    return Err(deny(
                        StatusCode::FORBIDDEN,
                        "an admin session or global-scope service key is required for this route",
                    ));
                }
            }
        }
        _ => {
            return Err(deny(
                StatusCode::FORBIDDEN,
                "an admin session or global-scope service key is required for this route",
            ));
        }
    };

    Ok((ctx, principal))
}

/// The platform service key and nothing else: for
/// the routes only the control plane calls. An administrator's session is a
/// service principal elsewhere; here it is refused (403) with every other
/// credential, and no credential is 401.
pub async fn require_platform_key(
    state: &SharedState,
    headers: &HeaderMap,
    tenant_id: &str,
    client_ip: Option<String>,
) -> Result<AuthContext, Response> {
    let (ctx, principal) = require_service_principal(state, headers, tenant_id, client_ip).await?;
    if principal != ServicePrincipal::PlatformKey {
        return Err(deny(
            StatusCode::FORBIDDEN,
            "the platform service key is required for this route",
        ));
    }
    Ok(ctx)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn system_scope_admits_service_key_and_system_admin_only() {
        assert!(ServicePrincipal::PlatformKey.require_system_scope().is_ok());
        assert!(ServicePrincipal::SystemAdmin.require_system_scope().is_ok());
        let denied = ServicePrincipal::TenantAdmin("acme".into())
            .require_system_scope()
            .unwrap_err();
        assert_eq!(denied.status(), StatusCode::FORBIDDEN);
    }

    #[test]
    fn tenant_scope_pins_a_tenant_admin_to_its_own_tenant() {
        let acme = ServicePrincipal::TenantAdmin("acme".into());
        assert!(acme.require_tenant_scope("acme").is_ok());
        let denied = acme.require_tenant_scope("globex").unwrap_err();
        assert_eq!(denied.status(), StatusCode::FORBIDDEN);
        assert!(
            ServicePrincipal::PlatformKey
                .require_tenant_scope("globex")
                .is_ok()
        );
        assert!(
            ServicePrincipal::SystemAdmin
                .require_tenant_scope("globex")
                .is_ok()
        );
    }

    #[test]
    fn a_401_carries_the_bearer_challenge() {
        let resp = deny(StatusCode::UNAUTHORIZED, "nope");
        assert!(
            resp.headers()
                .contains_key(axum::http::header::WWW_AUTHENTICATE)
        );
        let resp = deny(StatusCode::FORBIDDEN, "nope");
        assert!(
            !resp
                .headers()
                .contains_key(axum::http::header::WWW_AUTHENTICATE)
        );
    }
}
