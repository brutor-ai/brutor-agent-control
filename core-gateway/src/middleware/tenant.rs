// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
// Tenant resolution middleware — extracts tenant_id from:
//   1. X-Tenant-ID header (explicit)
//   2. JWT token claim (verified)
//   3. API key lookup (database)

use std::task::{Context, Poll};

use axum::body::Body;
use axum::http::{Request, StatusCode};
use axum::response::Response;
use futures::future::BoxFuture;
use tower::{Layer, Service};

use crate::services::SharedState;
use crate::services::system_alerts::{SystemAlertInput, spawn_raise};

/// Extension that carries the resolved tenant ID and its suspension through
/// the request.
#[derive(Clone, Debug)]
pub struct TenantContext {
    pub tenant_id: String,
    pub suspended: bool,
}

impl TenantContext {
    /// Returns `true` when the tenant is suspended: the one standing the
    /// Community Edition knows. (Another reason to refuse a tenant, such as
    /// the Enterprise Edition's expired trial, is the installation's
    /// `services::tenant_gate::TenantGate`.)
    pub fn is_blocked(&self) -> bool {
        self.suspended
    }
}

// What a path requires of this layer — whether a tenant is resolved at all,
// whether a request without one is refused, whether a suspended tenant is
// blocked — is not decided here. It is the path's rule in the registry
// `services::tenant_paths` (`AppState.tenant_paths`), which the Community
// Edition fills with its own paths and which other pieces add to when they
// are installed. A path nobody registered gets the most restrictive rule.
// This file names no path of its own:
// `tests/tenant_paths_guard.rs`.

/// Tower layer for tenant resolution.
#[derive(Clone)]
pub struct TenantLayer {
    state: SharedState,
}

impl TenantLayer {
    pub fn new(state: SharedState) -> Self {
        Self { state }
    }
}

impl<S> Layer<S> for TenantLayer {
    type Service = TenantMiddleware<S>;

    fn layer(&self, inner: S) -> Self::Service {
        TenantMiddleware {
            inner,
            state: self.state.clone(),
        }
    }
}

#[derive(Clone)]
pub struct TenantMiddleware<S> {
    inner: S,
    state: SharedState,
}

impl<S> Service<Request<Body>> for TenantMiddleware<S>
where
    S: Service<Request<Body>, Response = Response> + Clone + Send + 'static,
    S::Future: Send + 'static,
{
    type Response = Response;
    type Error = S::Error;
    type Future = BoxFuture<'static, Result<Self::Response, Self::Error>>;

    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        self.inner.poll_ready(cx)
    }

    fn call(&mut self, mut req: Request<Body>) -> Self::Future {
        let mut inner = self.inner.clone();
        let state = self.state.clone();

        // What authentication, deeper in, does not know about this request:
        // its route, and this instance's table of refused agent credentials.
        // Every route is behind this middleware.
        let refusal_scope = crate::services::refused_agent_credential::RequestScope::new(
            req.uri().path(),
            &self.state.route_surfaces,
            self.state.refused_credentials.clone(),
        );

        Box::pin(crate::services::refused_agent_credential::scope(
            refusal_scope,
            async move {
                let path = req.uri().path();
                let method = req.method().clone();

                tracing::debug!(%path, %method, "Tenant middleware processing request");

                // The path's rule: registered, or the most restrictive one.
                let rule = state.tenant_paths.rule_for(path);

                // Paths that carry their own tenant, or need none (health
                // probes, public discovery, the OAuth proxy).
                if rule.skips_resolution() {
                    return inner.call(req).await;
                }

                // 1. From the Gateway JWT (X-Gateway-Authorization only — not
                //    Authorization). The VERIFIED claim is preferred over the
                //    unauthenticated X-Tenant-ID header: a cryptographically
                //    signed tenant must win over one an attacker can type. (This
                //    only sets the resolution string; `authenticate_request`
                //    downstream still re-scopes credentials to this tenant, but
                //    preferring the verified source keeps a spoofed header from
                //    ever becoming the acting tenant for a JWT-bearing request.)
                let tenant_id = req
                    .headers()
                    .get(crate::constants::headers::X_GATEWAY_AUTHORIZATION)
                    .and_then(|v| v.to_str().ok())
                    .and_then(|v| v.strip_prefix("Bearer "))
                    .and_then(|token| {
                        // Reuse the auth-service decoder so tenant resolution
                        // honours the same expiry enforcement as authentication —
                        // an expired token must not silently resolve a tenant.
                        crate::services::auth::decode_gateway_jwt(
                            token,
                            &state.config.jwt_secret,
                            &state.credential_verifiers,
                        )
                        .ok()
                        .and_then(|claims| claims.tenant_id)
                    });

                tracing::debug!(?tenant_id, "Step 1: verified gateway JWT tenant");

                // 2. Explicit X-Tenant-ID header — only when no verified JWT tenant
                //    was present. Still required for API-key requests (the key
                //    itself is re-scoped to this tenant in authenticate_request)
                //    and for header-only tenant contexts.
                let tenant_id = tenant_id.or_else(|| {
                    req.headers()
                        .get(crate::constants::headers::X_TENANT_ID)
                        .and_then(|v| v.to_str().ok())
                        .map(String::from)
                });

                tracing::debug!(?tenant_id, "Step 2: after X-Tenant-ID header");

                // 3. From API key lookup (Authorization: Bearer sk_brutor_api_... or X-API-Key)
                let tenant_id = if tenant_id.is_some() {
                    tenant_id
                } else {
                    let raw_auth = req
                        .headers()
                        .get("authorization")
                        .and_then(|v| v.to_str().ok())
                        .map(String::from);

                    let raw_x_api_key = req
                        .headers()
                        .get(crate::constants::headers::X_API_KEY)
                        .and_then(|v| v.to_str().ok())
                        .map(String::from);

                    tracing::debug!(
                        auth_header_present = raw_auth.is_some(),
                        x_api_key_present = raw_x_api_key.is_some(),
                        auth_prefix = raw_auth.as_deref().map(|v| &v[..v.len().min(30)]),
                        "Step 3: API key extraction"
                    );

                    // Extract the key value: strip "Bearer " or "Bearer: " if present.
                    // Some clients (e.g. Goose) send "Bearer: <key>" with a colon.
                    let api_key = raw_auth
                        .as_deref()
                        .and_then(|v| {
                            v.strip_prefix("Bearer ")
                                .or_else(|| v.strip_prefix("Bearer: "))
                        })
                        .map(|v| v.trim())
                        .filter(|v| v.starts_with(crate::constants::headers::BEARER_API_KEY_PREFIX))
                        .or_else(|| {
                            raw_x_api_key.as_deref().map(|v| {
                                v.strip_prefix("Bearer ")
                                    .or_else(|| v.strip_prefix("Bearer: "))
                                    .unwrap_or(v)
                                    .trim()
                            })
                        });

                    if let Some(key) = api_key {
                        use sha2::{Digest, Sha256};
                        let hash = format!("{:x}", Sha256::digest(key.as_bytes()));
                        // Try the TTL cache before the DB.
                        // 30 s cache window; revoked keys still hit 401
                        // within that window.
                        if let Some(cached) = state.tenant_cache.get_api_key(&hash) {
                            match &cached {
                                Some(tid) => tracing::debug!(
                                    tenant_id = %tid,
                                    cache = "hit",
                                    "API key resolved from cache"
                                ),
                                None => tracing::debug!(
                                    key_prefix = &key[..key.len().min(20)],
                                    cache = "hit_negative",
                                    "API key cached as not-found"
                                ),
                            }
                            cached
                        } else {
                            tracing::debug!(
                                key_prefix = &key[..key.len().min(20)],
                                hash_prefix = &hash[..12],
                                cache = "miss",
                                "Looking up API key in database"
                            );
                            let result = sqlx::query_scalar::<_, String>(
                            "SELECT tenant_id FROM api_keys WHERE key_hash = $1 AND enabled = true AND revoked = false"
                        )
                        .bind(&hash)
                        .fetch_optional(&state.db)
                        .await;
                            let resolved: Option<String> = match &result {
                                Ok(Some(tid)) => {
                                    tracing::debug!(tenant_id = %tid, "API key resolved tenant");
                                    Some(tid.clone())
                                }
                                Ok(None) => {
                                    tracing::warn!(
                                        key_prefix = &key[..key.len().min(20)],
                                        "API key not found in database"
                                    );
                                    None
                                }
                                Err(e) => {
                                    // DB error — DO NOT poison the cache
                                    // with this transient failure.
                                    tracing::error!(error = %e, "API key DB lookup failed");
                                    return Ok(Response::builder()
                                        .status(StatusCode::INTERNAL_SERVER_ERROR)
                                        .header("content-type", "application/json")
                                        .body(Body::from(r#"{"error":"Auth lookup failed"}"#))
                                        .unwrap());
                                }
                            };
                            // Cache the resolution (positive AND negative)
                            // — the negative case is the credential-stuffing
                            // mitigation, see tenant_cache.rs.
                            state.tenant_cache.insert_api_key(hash, resolved.clone());
                            resolved
                        }
                    } else {
                        tracing::debug!("No API key found in headers");
                        None
                    }
                };

                let tenant_id = match tenant_id {
                    Some(id) => {
                        tracing::debug!(tenant_id = %id, "Tenant resolved");
                        id
                    }
                    None => {
                        // The rule requires a tenant (the proxy surfaces, and
                        // every path nobody registered).
                        if rule.requires_tenant() {
                            // System MCP paths may get an initial unauthenticated probe
                            // before the client retries with proper auth — log at debug level.
                            if path.starts_with("/v1/proxy/mcp/system-") {
                                tracing::debug!(%path, "No tenant context for system MCP path — rejecting (portal will retry with auth)");
                            } else {
                                tracing::warn!(%path, "No tenant context found — rejecting request");
                            }
                            // RFC 7235 §3.1: 401 is the correct status when a
                            // request lacks valid auth credentials for a
                            // protected resource. Previously this returned 400,
                            // which signals "malformed request" — wrong semantic
                            // and prevents SDKs from invoking their refresh-
                            // and-retry logic. RFC 6750 §3 also requires
                            // WWW-Authenticate on a Bearer-protected 401.
                            let resp = Response::builder()
                            .status(StatusCode::UNAUTHORIZED)
                            .header("content-type", "application/json")
                            .header(
                                axum::http::header::WWW_AUTHENTICATE,
                                crate::services::error_envelope::WWW_AUTHENTICATE_BEARER,
                            )
                            .body(Body::from(
                                r#"{"error":{"message":"Missing or invalid credentials. Provide X-Tenant-ID header or a valid bearer token / API key."}}"#,
                            ))
                            .unwrap();
                            return Ok(resp);
                        }
                        // Registered as "tenant optional": the route resolves
                        // its caller itself.
                        return inner.call(req).await;
                    }
                };

                // -----------------------------------------------------------------
                // Load tenant status from DB (the suspension check).
                // TTL cache (5 s); pub/sub-driven invalidation on
                // suspend/unsuspend brings staleness down to ms.
                // -----------------------------------------------------------------
                let (suspended, suspension_reason) = match state.tenant_cache.get_tenant(&tenant_id)
                {
                    Some(s) => {
                        tracing::debug!(
                            tenant_id = %tenant_id,
                            cache = "hit",
                            "tenant status from cache"
                        );
                        (s.suspended, s.suspension_reason)
                    }
                    None => {
                        let tenant_row =
                            crate::services::tenant_features::load_status(&state.db, &tenant_id)
                                .await;
                        match tenant_row {
                            Ok(Some(status)) => {
                                // Cache the success — most tenants will see
                                // hundreds of requests within the 5 s TTL.
                                let row = (status.suspended, status.suspension_reason.clone());
                                state.tenant_cache.insert_tenant(tenant_id.clone(), status);
                                row
                            }
                            Ok(None) => {
                                tracing::warn!(tenant_id = %tenant_id, "Tenant not found in database");
                                let resp = Response::builder()
                                    .status(StatusCode::FORBIDDEN)
                                    .header("content-type", "application/json")
                                    .body(Body::from(r#"{"error":"Tenant not found."}"#))
                                    .unwrap();
                                return Ok(resp);
                            }
                            Err(e) => {
                                // Local DB lookup failed. Previously this path
                                // silently continued with default tenant state
                                // (suspended=false), which is a latent auth-bypass
                                // hazard — a suspended tenant would still be
                                // served during a DB flake.
                                //
                                // Correct behavior: surface the failure (system
                                // alert + ERROR log) and return 503. The alert
                                // dedup key covers the whole storm of concurrent
                                // failures; only one alert row is written even
                                // under heavy traffic.
                                tracing::error!(
                                    tenant_id = %tenant_id,
                                    error = %e,
                                    "Tenant status query failed — returning 503"
                                );
                                spawn_raise(
                                    state.db.clone(),
                                    SystemAlertInput::internal_error(
                                        Some(tenant_id.clone()),
                                        "middleware.tenant.status_query",
                                        Some(e.to_string()),
                                        format!(
                                            "Failed to load tenant row for the suspension check: {e}"
                                        ),
                                    ),
                                );
                                let resp = Response::builder()
                                .status(StatusCode::SERVICE_UNAVAILABLE)
                                .header("content-type", "application/json")
                                .header("Retry-After", "5")
                                .body(Body::from(
                                    r#"{"error":"Tenant status temporarily unavailable. Retry shortly."}"#,
                                ))
                                .unwrap();
                                return Ok(resp);
                            }
                        }
                    }
                };

                let ctx = TenantContext {
                    tenant_id,
                    suspended,
                };

                // Block runtime paths for a suspended tenant: the paths whose
                // rule says so, and every path nobody registered. The wire error
                // is the tenant's standing, `tenant_suspended`, never
                // `trial_expired`, which is for a trial that ran out.
                if rule.blocks_suspended() {
                    let block = if suspended {
                        Some(crate::services::tenant_gate::TenantBlock {
                            code: "tenant_suspended",
                            message: suspension_reason
                                .as_deref()
                                .unwrap_or("Account suspended")
                                .to_string(),
                        })
                    } else {
                        // Any further reason is the installation's:
                        // none in the Community Edition, the trial gate in
                        // the Enterprise Edition. A gate that cannot decide
                        // refuses, as an unreadable tenant status does.
                        match state.tenant_gate.gates() {
                            false => None,
                            true => {
                                match state.tenant_gate.check(&state.db, &ctx.tenant_id).await {
                                    Ok(block) => block,
                                    Err(crate::services::tenant_gate::TenantGateUnavailable) => {
                                        tracing::error!(
                                            tenant_id = %ctx.tenant_id,
                                            "Tenant gate could not decide — returning 503"
                                        );
                                        let resp = Response::builder()
                                    .status(StatusCode::SERVICE_UNAVAILABLE)
                                    .header("content-type", "application/json")
                                    .header("Retry-After", "5")
                                    .body(Body::from(
                                        r#"{"error":"Tenant status temporarily unavailable. Retry shortly."}"#,
                                    ))
                                    .unwrap();
                                        return Ok(resp);
                                    }
                                }
                            }
                        }
                    };
                    if let Some(block) = block {
                        tracing::info!(
                            tenant_id = %ctx.tenant_id,
                            %suspended,
                            code = block.code,
                            %path,
                            "Blocking request — the tenant is refused"
                        );
                        let body = serde_json::json!({
                            "error": block.code,
                            "message": block.message,
                        });
                        let resp = Response::builder()
                            .status(StatusCode::FORBIDDEN)
                            .header("content-type", "application/json")
                            .body(Body::from(body.to_string()))
                            .unwrap();
                        return Ok(resp);
                    }
                }

                req.extensions_mut().insert(ctx);

                inner.call(req).await
            },
        ))
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn make_ctx(suspended: bool) -> TenantContext {
        TenantContext {
            tenant_id: "test-tenant".to_string(),
            suspended,
        }
    }

    // -- TenantContext -------------------------------------------------------

    #[test]
    fn tenant_context_stores_tenant_id() {
        let ctx = make_ctx(false);
        assert_eq!(ctx.tenant_id, "test-tenant");
    }

    #[test]
    fn tenant_context_clone() {
        let ctx = make_ctx(false);
        let cloned = ctx.clone();
        assert_eq!(cloned.tenant_id, "test-tenant");
    }

    #[test]
    fn tenant_context_debug_format() {
        let ctx = make_ctx(false);
        let debug = format!("{:?}", ctx);
        assert!(debug.contains("test-tenant"));
    }

    // -- is_blocked -----------------------------------------------------------

    #[test]
    fn not_blocked_normal_tenant() {
        let ctx = make_ctx(false);
        assert!(!ctx.is_blocked());
    }

    #[test]
    fn blocked_when_suspended() {
        let ctx = make_ctx(true);
        assert!(ctx.is_blocked());
    }

    // A trial is not the Community Edition's concern:
    // the context has no trial fields, and the three tests that held
    // "blocked when the trial is over / not while it runs / not without an
    // end date" are the Enterprise trial gate's (`enterprise/src/trial.rs`).

    // The path tests (`runtime_path_*`, `skip_*`, `requires_tenant_*`) moved
    // with the lists they tested: `services::tenant_paths::tests` for the
    // Community paths, `services::portal_session::tests` for the portal's.
}
