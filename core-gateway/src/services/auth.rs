// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Brutor Agent Control Core Gateway — Authentication Service
//!
//! This module implements the multi-strategy authentication "waterfall" for the gateway.
//! It resolves raw credentials into a unified `AuthContext` used for downstream
//! rate-limiting, guardrails, and provider routing.
//!
//! ### Authentication Strategies (in priority order):
//! 1. **API Keys**: Validates `sk_brutor_api_` keys against the database using SHA-256 hashing.
//! 2. **Gateway JWT**: Decodes internal `X-Gateway-Authorization` tokens for inter-service or dashboard access.
//!    Whether a token is a credential, and whether its session stands, is asked of the token-type
//!    registry (`services::credential_verifier`): a `typ` nobody registered is refused.
//! 3. **OAuth Passthrough**: Supports bearer tokens intended for upstream providers or MCP servers.
//! 4. **Anonymous**: Optional fallback for public-facing health or version endpoints.
//!
//! ### Performance & Security:
//! - **Background Updates**: API key usage counts and timestamps are updated via non-blocking `tokio::spawn`.
//! - **Hashed Verification**: Plaintext API keys are never compared; only SHA-256 hashes are used for DB lookups.
//! - **Context Injection**: The resulting `AuthContext` is passed through the request extensions to all handlers.

use std::time::{SystemTime, UNIX_EPOCH};

use axum::http::HeaderMap;
use jsonwebtoken::{DecodingKey, Validation, decode};
use sha2::{Digest, Sha256};
use sqlx::PgPool;

use crate::models::auth::{AuthContext, AuthType, GatewayJwtClaims};
use crate::models::db::ApiKey;
use crate::services::credential_verifier::{CredentialVerifiers, VerifiedCredential};

/// Authenticate a proxy request using the multi-strategy priority:
///   1. API Key (X-API-Key or Authorization: Bearer sk_brutor_api_...)
///   2. Gateway JWT (X-Gateway-Authorization)
///   3. IdP OAuth (Authorization header — verified via token manager)
///   4. OAuth passthrough (unverified Authorization)
///   5. Anonymous
///      Extract the caller's active resource group from the
///      `x-resource-group` request header. The portal stamps this on every
///      request to identify the workspace the user is currently in.
///
/// Returns `None` for a missing, empty, or whitespace-only header.
/// The value is **client-supplied** — callers MUST validate it against
/// the user's real group memberships before trusting it for access
/// scoping (see `system_server::filter_tools_by_group_access`).
pub(crate) fn active_group_from_headers(headers: &HeaderMap) -> Option<String> {
    headers
        .get("x-resource-group")
        .and_then(|v| v.to_str().ok())
        .map(str::trim)
        .filter(|v| !v.is_empty())
        .map(String::from)
}

/// The resource group a workload-token agent acts in for this request:
/// ONE group, the binding an API key carries for a
/// key-authenticated caller.
///
/// * The agent's memberships are its rows in `resource_group_agent_members`
///   whose group is an active group of the request's tenant. A membership
///   of an inactive group, or of another tenant's group, is no membership
///   (a key of an inactive group is refused the same way).
/// * `requested` is the `x-resource-group` header. When the agent names a
///   group it is a member of, that group is the acting group. When it names
///   any other group — one it is not in, one that does not exist, another
///   tenant's — the request is refused ([`AuthError::GroupNotMember`]): the
///   agent is never served in the group it named and never moved to another
///   one.
/// * With no group named: the oldest membership, so an agent in several
///   groups resolves the same way on every request.
///
/// `Ok(None)` = the agent is a member of no group and named none: it
/// authenticates and no group's models, servers or limits are its. A lookup
/// that fails is an error, never "no group".
pub async fn agent_acting_group(
    db: &PgPool,
    tenant_id: &str,
    agent_id: &str,
    requested: Option<&str>,
) -> Result<Option<String>, AuthError> {
    let memberships: Vec<String> = sqlx::query_scalar(
        "SELECT m.group_id FROM resource_group_agent_members m \
           JOIN resource_groups rg ON rg.id = m.group_id \
          WHERE m.tenant_id = $1 AND m.agent_id = $2 \
            AND rg.tenant_id = $1 AND rg.is_active = true \
          ORDER BY m.joined_at, m.id",
    )
    .bind(tenant_id)
    .bind(agent_id)
    .fetch_all(db)
    .await
    .map_err(|e| AuthError::Internal(format!("agent group lookup failed: {e}")))?;
    match requested {
        None => Ok(memberships.into_iter().next()),
        Some(named) => match memberships.into_iter().find(|g| g == named) {
            Some(group) => Ok(Some(group)),
            None => {
                tracing::warn!(
                    %tenant_id, %agent_id, requested_group = %named,
                    "workload agent refused: X-Resource-Group names a group it is not a member of"
                );
                Err(AuthError::GroupNotMember)
            }
        },
    }
}

/// Whether the agent identity an API key is bound to stands, as a column of
/// the statement that reads the key: enabled,
/// `active`, not revoked and not past `expires_at`, judged on the
/// database's clock exactly as the workload-token path judges it
/// (`workload_identity::resolve_agent_from_token`). NULL for a key bound to
/// no agent. Read with the key, on every request, so ending the agent ends
/// its key on the next call and no request pays a second round trip.
macro_rules! agent_stands_sql {
    () => {
        "CASE WHEN ak.agent_id IS NULL THEN NULL ELSE EXISTS ( \
           SELECT 1 FROM agent_identities ai \
            WHERE ai.id = ak.agent_id AND ai.tenant_id = ak.tenant_id \
              AND ai.enabled = true AND ai.status = 'active' \
              AND ai.revoked_at IS NULL \
              AND (ai.expires_at IS NULL OR ai.expires_at > now())) END AS agent_stands"
    };
}

pub async fn authenticate_request(
    headers: &HeaderMap,
    tenant_id: &str,
    jwt_secret: &str,
    db: &PgPool,
    verifiers: &CredentialVerifiers,
    client_ip: Option<String>,
    require_auth: bool,
) -> Result<AuthContext, AuthError> {
    // The caller's active workspace, stamped by the portal on every
    // request. Carried onto AuthContext.resolved_group_id so group-
    // scoped surfaces (the Agent Skills MCP server's tools/list) can
    // scope to the workspace the caller is in, not the union of every
    // group they belong to.
    let resolved_group_id = active_group_from_headers(headers);

    // 1. Check API key
    let api_key_value = headers
        .get(crate::constants::headers::X_API_KEY)
        .and_then(|v| v.to_str().ok())
        .map(String::from)
        .or_else(|| {
            headers
                .get("authorization")
                .and_then(|v| v.to_str().ok())
                .and_then(|v| {
                    v.strip_prefix("Bearer ")
                        .or_else(|| v.strip_prefix("Bearer: "))
                })
                .map(|v| v.trim())
                .filter(|v| v.starts_with(crate::constants::headers::BEARER_API_KEY_PREFIX))
                .map(String::from)
        });

    if let Some(ref key_value) = api_key_value {
        let key_hash = hex::encode(Sha256::digest(key_value.as_bytes()));

        // A key stands while it is enabled and not revoked AND, when it is
        // bound to an end user, while that user is active:
        // the key acts as the user, so deactivating the user ends it
        // exactly as revoking the key does — the row is not found, and the
        // request goes on as one that carries no key (and is refused 401 if
        // nothing else it carries is a credential). Read on every request
        // (no cache holds this decision), so a deactivation takes effect on
        // the next call. A user row that is gone or whose flag is NULL is
        // not active.

        // Match Python: only filter by tenant_id if we have a real one (not from the key itself)
        let api_key: Option<ApiKey> = if tenant_id == "n/a" || tenant_id.is_empty() {
            sqlx::query_as(concat!(
                "SELECT ak.*, rg.name as group_name, rg.is_active as group_active, ",
                agent_stands_sql!(),
                r#"
                   FROM api_keys ak
                   LEFT JOIN resource_groups rg ON ak.group_id = rg.id
                   WHERE ak.key_hash = $1 AND ak.enabled = true AND ak.revoked = false
                     AND (ak.user_id IS NULL OR EXISTS (
                           SELECT 1 FROM end_users eu
                            WHERE eu.id = ak.user_id AND eu.is_active = true))
                   LIMIT 1"#
            ))
            .bind(&key_hash)
            .fetch_optional(db)
            .await
        } else {
            // Look up by tenant_id first, fall back to global scope (system health check key)
            sqlx::query_as(concat!(
                "SELECT ak.*, rg.name as group_name, rg.is_active as group_active, ",
                agent_stands_sql!(),
                r#"
                   FROM api_keys ak
                   LEFT JOIN resource_groups rg ON ak.group_id = rg.id
                   WHERE ak.key_hash = $1
                     AND (ak.tenant_id = $2 OR ak.scope = 'global')
                     AND ak.enabled = true AND ak.revoked = false
                     AND (ak.user_id IS NULL OR EXISTS (
                           SELECT 1 FROM end_users eu
                            WHERE eu.id = ak.user_id AND eu.is_active = true))
                   LIMIT 1"#
            ))
            .bind(&key_hash)
            .bind(tenant_id)
            .fetch_optional(db)
            .await
        }
        .map_err(|e| AuthError::Internal(format!("DB error: {e}")))?;

        // The tenant the key acts for. A shared
        // (`global`-scope) key is its own tenant's; it acts for another
        // tenant only where the installation lists that tenant
        // (`configure_global_key_targets`). The platform service key,
        // which no tenant can mint, serves every tenant unless a list bounds
        // it. A key that may not act for the tenant the request names is no
        // credential of this request: the request goes on as one that
        // carries no key, exactly as with a revoked one (401 wherever a
        // credential is required). It is never quietly moved to its own
        // tenant: the route would then act on the tenant the request named
        // with another tenant's principal.
        //
        // Where it does act for another tenant, logs, costs and quotas are
        // the target tenant's, and the `api_key_*` fields still name the
        // key, so an audit sees "key X acting for Y".
        'key: {
            if let Some(key) = api_key {
                // Asked only of a global-scope key that names another
                // tenant: the only case the list decides.
                let names_another_tenant = key.scope == "global"
                    && !tenant_id.is_empty()
                    && tenant_id != "n/a"
                    && tenant_id != key.tenant_id;
                let targets = if names_another_tenant {
                    global_key_allowed_targets(db).await
                } else {
                    None
                };
                let Some(resolved_tenant) = resolve_effective_tenant(
                    &key.scope,
                    &key.tenant_id,
                    tenant_id,
                    targets.as_deref(),
                    key.is_platform_service,
                ) else {
                    tracing::warn!(
                        api_key_id = %key.id,
                        api_key_name = %key.name,
                        owner_tenant = %key.tenant_id,
                        requested_tenant = %tenant_id,
                        "global API key refused: it may not act for the tenant the request names \
                         (it is not on the installation's list of tenants a shared key acts for)"
                    );
                    break 'key;
                };
                if !key.is_valid() {
                    return Err(AuthError::InvalidApiKey(
                        "API key is expired or revoked".into(),
                    ));
                }
                // The key of an agent identity that does not stand is no
                // credential of this request: the
                // request goes on as one that carries no key, as with a
                // revoked key and as a workload token of such an agent does.
                // Anything but a read `true` refuses.
                if key.agent_id.is_some() && key.agent_stands != Some(true) {
                    tracing::warn!(
                        api_key_id = %key.id,
                        api_key_name = %key.name,
                        agent_id = ?key.agent_id,
                        "API key refused: the agent identity it is bound to is disabled, \
                         revoked, expired or not active"
                    );
                    // The caller learns 401 and nothing else; the operator
                    // gets an audit record, attributed to the agent and to
                    // the key's own tenant, bounded per key.
                    if let Some(agent_id) = key.agent_id.clone() {
                        crate::services::refused_agent_credential::record(
                            crate::services::refused_agent_credential::Refused {
                                credential:
                                    crate::services::refused_agent_credential::Credential::ApiKey,
                                tenant_id: key.tenant_id.clone(),
                                agent_id,
                                api_key_id: Some(key.id.clone()),
                                api_key_name: Some(key.name.clone()),
                                group_id: key.group_id.clone(),
                                client_ip: client_ip.clone(),
                            },
                        );
                    }
                    break 'key;
                }
                // Every call is bound to a resource group: that group's
                // limits and policies are what governs it. The one exception is
                // the platform service key (health checks, the assurance judge,
                // knowledge-base embeddings, the KB uploader), billed to the
                // tenant's Unbound group. Any other key with no group, or whose
                // group is inactive or gone, is refused here, before any route
                // runs, so no surface can treat "no group" as "no rules".
                unbound_key_refusal(&key)?;

                // Update usage count in background (fire-and-forget)
                // Batched: one bulk UPDATE every few seconds instead of a
                // spawned hot-row write per request (see api_key_usage.rs).
                crate::services::api_key_usage::record(&key.id);

                // Audit every cross-tenant attribution: this is the capability a
                // leaked global key would abuse, so it is never silent.
                if resolved_tenant != key.tenant_id {
                    tracing::warn!(
                        api_key_id = %key.id,
                        api_key_name = %key.name,
                        owner_tenant = %key.tenant_id,
                        acting_for_tenant = %resolved_tenant,
                        "global API key acting for a different tenant"
                    );
                }
                let (gateway_user_id, gateway_username) = if let Some(ref uid) = key.user_id {
                    (Some(uid.to_string()), key.user_email.clone())
                } else if let Some(ref gid) = key.group_id {
                    // Group-scoped key: use group_id as user identity
                    (Some(gid.clone()), key.group_name.clone())
                } else {
                    // Global key: use key identity
                    (
                        Some(format!("api_key_{}", key.id)),
                        Some(format!("api_key:{}", key.name)),
                    )
                };

                // Parse allowed_skill_ids from JSON array
                let allowed_skill_ids: Option<Vec<String>> = key
                    .allowed_skill_ids
                    .as_ref()
                    .and_then(|v| serde_json::from_value::<Vec<String>>(v.clone()).ok());

                return Ok(AuthContext {
                    auth_type: AuthType::ApiKey,
                    tenant_id: resolved_tenant.clone(),
                    gateway_user_id,
                    gateway_username,
                    gateway_tenant_id: Some(resolved_tenant),
                    idp_user_id: None,
                    idp_username: None,
                    idp_email: None,
                    api_key_id: Some(key.id),
                    api_key_name: Some(key.name),
                    api_key_scope: Some(key.scope),
                    api_key_group_id: key.group_id,
                    api_key_is_platform: key.is_platform_service,
                    api_key_user_id: key.user_id,
                    allowed_skill_ids,
                    client_ip,
                    session_key: None,
                    session_token_type: None,
                    resolved_group_id: resolved_group_id.clone(),
                    actor_agent_id: key.agent_id,
                    agent_impl: crate::services::agent_impl::request_carriers(),
                    admission_hold: None,
                });
            }
        }
    }

    // 2. Gateway JWT (X-Gateway-Authorization)
    if let Some(gateway_token) = headers
        .get("x-gateway-authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
    {
        match decode_gateway_jwt(gateway_token, jwt_secret, verifiers) {
            Ok(claims) => {
                return gateway_session(
                    claims,
                    tenant_id,
                    db,
                    verifiers,
                    client_ip,
                    resolved_group_id,
                )
                .await;
            }
            // Signed with our secret and not a credential of this
            // installation (its type has no verifier, or it has no type):
            // a refusal, on every surface. Falling through would make the
            // request anonymous where anonymous is let in.
            Err(GatewayJwtError::NotACredentialHere(e)) => return Err(e),
            Err(GatewayJwtError::NotAGatewayJwt(e)) => {
                tracing::debug!("Gateway JWT decode failed: {e}");
            }
        }
    }

    // 3/4. Authorization header — non-API-key bearer.
    //
    // SECURITY: this branch used to silently accept *any* non-API-key
    // bearer as `AuthType::OauthPassthrough`, which made the header a
    // no-op authentication: a garbage string like
    // `Bearer eyJ...garbage.garbage` passed auth, the request reached
    // model-access check, and returned a misleading 403 ("no access")
    // instead of the correct 401 ("not authenticated"). `OauthPassthrough`
    // is only used as an audit label — nothing gates on it — so the
    // silent-accept gave the operator no protection at all.
    //
    // New behaviour: the bearer must verify as a Gateway JWT. Some
    // clients ship the gateway token via the plain `Authorization`
    // header instead of `X-Gateway-Authorization`; accept that here.
    // Anything else is rejected as InvalidJwt → 401 + WWW-Authenticate.
    if let Some(auth_header) = headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| {
            v.strip_prefix("Bearer ")
                .or_else(|| v.strip_prefix("Bearer: "))
        })
        .map(|v| v.trim())
    {
        if !auth_header.starts_with(crate::constants::headers::BEARER_API_KEY_PREFIX) {
            match decode_gateway_jwt(auth_header, jwt_secret, verifiers) {
                Ok(claims) => {
                    return gateway_session(
                        claims,
                        tenant_id,
                        db,
                        verifiers,
                        client_ip,
                        resolved_group_id,
                    )
                    .await;
                }
                Err(GatewayJwtError::NotACredentialHere(e)) => return Err(e),
                Err(GatewayJwtError::NotAGatewayJwt(AuthError::ExpiredJwt)) => {
                    // A well-formed but expired gateway JWT — report expiry
                    // distinctly (still 401) so clients know to refresh.
                    return Err(AuthError::ExpiredJwt);
                }
                Err(GatewayJwtError::NotAGatewayJwt(_)) => {
                    // Not a gateway JWT — try external-IdP **workload identity**:
                    // an agent authenticating with its own OIDC/SPIFFE token.
                    // Verifies signature + iss + exp against the tenant's IdP
                    // JWKS and resolves the agent by (idp_server_id, sub).
                    use crate::services::workload_identity::WorkloadToken;
                    let workload = crate::services::workload_identity::resolve_workload_token(
                        db,
                        tenant_id,
                        auth_header,
                    )
                    .await;
                    // A verified token of an agent that does not stand: no
                    // credential (the 401 below, as for a token of no
                    // agent), and a record of the refusal.
                    if let Ok(WorkloadToken::FallenAgent { agent_id }) = &workload {
                        crate::services::refused_agent_credential::record(
                            crate::services::refused_agent_credential::Refused {
                                credential:
                                    crate::services::refused_agent_credential::Credential::WorkloadToken,
                                tenant_id: tenant_id.to_string(),
                                agent_id: agent_id.clone(),
                                api_key_id: None,
                                api_key_name: None,
                                group_id: None,
                                client_ip: client_ip.clone(),
                            },
                        );
                    }
                    if let Ok(WorkloadToken::Agent(resolved)) = workload {
                        // The ONE resource group the agent acts in: the
                        // member group it names, else its oldest
                        // membership (`agent_acting_group`). A failed
                        // lookup, and a named group the agent is not a
                        // member of, refuse the request: neither may
                        // re-scope the agent.
                        let agent_group = agent_acting_group(
                            db,
                            tenant_id,
                            &resolved.agent_id,
                            resolved_group_id.as_deref(),
                        )
                        .await?;
                        return Ok(AuthContext {
                            auth_type: AuthType::IdpOauth,
                            tenant_id: tenant_id.to_string(),
                            gateway_user_id: None,
                            gateway_username: None,
                            gateway_tenant_id: Some(tenant_id.to_string()),
                            idp_user_id: Some(resolved.idp_subject.clone()),
                            idp_username: Some(resolved.idp_subject),
                            idp_email: None,
                            api_key_id: None,
                            api_key_name: None,
                            api_key_scope: None,
                            api_key_group_id: None,
                            api_key_is_platform: false,
                            api_key_user_id: None,
                            allowed_skill_ids: None,
                            client_ip,
                            session_key: None,
                            session_token_type: None,
                            // Never the header's value as sent: the acting
                            // group, or none.
                            resolved_group_id: agent_group,
                            actor_agent_id: Some(resolved.agent_id),
                            // The ONLY place a
                            // `verified` release can enter: the token's
                            // software claims, after its signature, issuer
                            // and expiry verified above.
                            agent_impl: crate::services::agent_impl::request_carriers()
                                .with_verified_token(resolved.software),
                            admission_hold: None,
                        });
                    }
                    // Bearer was supplied but verifies against nothing
                    // we recognise. Per RFC 6750 §3.1 this is `invalid_token`.
                    return Err(AuthError::InvalidJwt(
                        "Bearer token is not a recognised credential (not an API key, not a valid gateway JWT)".into(),
                    ));
                }
            }
        }
    }

    // 5. Anonymous: a request that carries no credential, on a route that
    // lets one in. A request that presented an API key which is not a
    // credential of it (unknown, revoked, or the key of an agent identity
    // that does not stand) is not that request: the credential is invalid,
    // and the answer is the 401 a route that requires a credential gives,
    // byte for byte. Going on as anonymous answered
    // the MCP route's access rule instead (403, and a served call where the
    // server is open to anonymous callers) while the sealed record of the
    // same refusal said 401. The MCP authorization specification says the
    // same: "Invalid or expired tokens MUST receive a HTTP 401 response".
    if require_auth || api_key_value.is_some() {
        return Err(AuthError::Unauthenticated);
    }

    Ok(AuthContext {
        auth_type: AuthType::Anonymous,
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
        client_ip,
        session_key: None,
        session_token_type: None,
        resolved_group_id: None,
        actor_agent_id: None,
        agent_impl: crate::services::agent_impl::request_carriers(),
        admission_hold: None,
    })
}

/// The request carries a gateway session token of a registered type: hold
/// it to the request's tenant and to its type's verifier, and make the
/// context of it. Every refusal here is final: a token that is ours and
/// does not stand is not retried as some other kind of credential.
async fn gateway_session(
    claims: GatewayJwtClaims,
    tenant_id: &str,
    db: &PgPool,
    verifiers: &CredentialVerifiers,
    client_ip: Option<String>,
    resolved_group_id: Option<String>,
) -> Result<AuthContext, AuthError> {
    // Reject a valid token replayed against another tenant.
    check_jwt_tenant_matches(claims.tenant_id.as_deref(), tenant_id)?;
    // The registry. An error is the refusal: there is no
    // path from here to a context except through `Ok`.
    let VerifiedCredential { token_type } = verifiers.verify(db, &claims).await?;
    Ok(AuthContext {
        auth_type: AuthType::GatewayJwt,
        tenant_id: claims
            .tenant_id
            .clone()
            .unwrap_or_else(|| tenant_id.to_string()),
        gateway_user_id: claims.user_id.clone(),
        gateway_username: claims.username.clone(),
        gateway_tenant_id: claims.tenant_id,
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
        client_ip,
        session_key: None,
        session_token_type: Some(token_type),
        resolved_group_id,
        actor_agent_id: None,
        agent_impl: crate::services::agent_impl::request_carriers(),
        admission_hold: None,
    })
}

/// The tenant an API key acts for in a request that names `header_tenant`,
/// or `None` when the key may not act for that tenant.
/// The one place this is decided.
///
/// - A key that is not `global`-scope acts for its own tenant. (The lookup
///   finds such a key only for its own tenant, or for a request that names
///   none.)
/// - A `global`-scope key in a request that names no tenant (empty, or the
///   `n/a` of a route that is not tenant-scoped) or names its own acts for
///   its own tenant.
/// - A `global`-scope key in a request that names ANOTHER tenant:
///   - with a list configured (`allowed_targets`), acts for that tenant if
///     the tenant is on the list, and is refused otherwise — the platform
///     service key included;
///   - with no list, is refused, unless it is the platform service key
///     (`is_platform`: seeded by the control plane, never minted by a
///     tenant), which serves every tenant.
///
/// It used to be the other way round: with no list every shared key acted
/// for whichever tenant the request named, and a refused target fell back to
/// the key's own tenant while the request went on naming the other one.
///
/// Pure function — extracted for unit testing without DB dependencies.
fn resolve_effective_tenant(
    key_scope: &str,
    key_tenant_id: &str,
    header_tenant: &str,
    allowed_targets: Option<&[String]>,
    is_platform: bool,
) -> Option<String> {
    let names_another_tenant =
        !header_tenant.is_empty() && header_tenant != "n/a" && header_tenant != key_tenant_id;
    if key_scope != "global" || !names_another_tenant {
        return Some(key_tenant_id.to_string());
    }
    let permitted = match allowed_targets {
        Some(list) => list.iter().any(|t| t == header_tenant),
        None => is_platform,
    };
    permitted.then(|| header_tenant.to_string())
}

/// Where the tenants a global-scope key may act for besides its own come
/// from. The gateway itself has no such setting: an installation with one
/// tenant has no other tenant to name. A distribution that serves several
/// tenants states where its list is ([`configure_global_key_targets`]).
#[async_trait::async_trait]
pub trait GlobalKeyTargets: Send + Sync {
    /// `Ok(None)`: no list (only the platform service key acts for other
    /// tenants). `Ok(Some(list))`: a global key acts for a listed tenant
    /// only. `Err`: the list cannot be known; the caller refuses.
    async fn allowed_targets(&self, db: &PgPool) -> Result<Option<Vec<String>>, String>;
}

/// A list fixed for the life of the process: for a test of the rule.
pub struct FixedGlobalKeyTargets(pub Option<Vec<String>>);

#[async_trait::async_trait]
impl GlobalKeyTargets for FixedGlobalKeyTargets {
    async fn allowed_targets(&self, _db: &PgPool) -> Result<Option<Vec<String>>, String> {
        Ok(self.0.clone().filter(|l| !l.is_empty()))
    }
}

static GLOBAL_KEY_TARGETS: std::sync::OnceLock<std::sync::Arc<dyn GlobalKeyTargets>> =
    std::sync::OnceLock::new();

/// State where the list comes from. Once per process; `false` when it was
/// already stated (the call changed nothing). For an extension, at start,
/// before a request is served.
pub fn configure_global_key_targets(source: std::sync::Arc<dyn GlobalKeyTargets>) -> bool {
    GLOBAL_KEY_TARGETS.set(source).is_ok()
}

/// [`configure_global_key_targets`] with a fixed list. For a test of the
/// rule.
pub fn configure_global_key_allowed_targets(list: Option<Vec<String>>) -> bool {
    configure_global_key_targets(std::sync::Arc::new(FixedGlobalKeyTargets(list)))
}

/// The list in force for this request. Nothing stated: no list. A source
/// that cannot answer: an empty list, so no global key acts for another
/// tenant, the platform service key included (fail closed: never "no list",
/// which would let the platform service key act for every tenant).
pub async fn global_key_allowed_targets(db: &PgPool) -> Option<Vec<String>> {
    let source = GLOBAL_KEY_TARGETS.get()?;
    match source.allowed_targets(db).await {
        Ok(list) => list,
        Err(why) => {
            tracing::error!(
                error = %why,
                "the tenants a shared key may act for cannot be read: every global key is refused \
                 for any tenant but its own until they can"
            );
            Some(Vec::new())
        }
    }
}

/// Clock-skew tolerance (seconds) applied when validating JWT `exp`.
///
/// The control plane (issuer) and proxy (verifier) are separate processes,
/// often on separate hosts, so a small drift must not spuriously reject a
/// freshly-minted token. Matches the leeway used elsewhere
/// (`skill_runner_client`).
const JWT_EXP_LEEWAY_SECS: u64 = 60;

/// How long a "session is active" answer is reused before the session
/// table is consulted again. Bounds the lag between an admin logging out
/// (or being revoked) in the control plane and the proxy refusing the
/// token. Negative answers are cached for the same window; a revoked
/// session never comes back.
const ADMIN_SESSION_CACHE_TTL_SECS: u64 = 30;

fn admin_session_cache() -> &'static moka::sync::Cache<String, bool> {
    static CACHE: std::sync::OnceLock<moka::sync::Cache<String, bool>> = std::sync::OnceLock::new();
    CACHE.get_or_init(|| {
        moka::sync::Cache::builder()
            .max_capacity(50_000)
            .time_to_live(std::time::Duration::from_secs(ADMIN_SESSION_CACHE_TTL_SECS))
            .build()
    })
}

/// Test hook: forget every cached session answer so a test that revokes a
/// session sees the refusal without waiting out the TTL.
pub fn clear_admin_session_cache() {
    admin_session_cache().invalidate_all();
}

/// Honour control-plane session revocation at the proxy.
///
/// Admin tokens (`admin_type` claim) are backed by a session row keyed by
/// `jti` in the session table of that kind of administrator; logout,
/// revocation and expiry deactivate that row. The control plane checks it
/// on every request — the proxy did not, so a logged-out or revoked admin
/// token kept working here until `exp`. Both planes share the database, so
/// the proxy can ask the same question.
///
/// Which table that is, is the caller's: the verifier registered for the
/// token's `admin_type` (`credential_verifier::CredentialVerifiers`).
/// This module names none. `table` is a constant of
/// the verifier's, never a value from a request.
///
/// Fails CLOSED: an admin token without a `jti`, whose session is
/// missing/inactive/expired, or whose lookup errors, is refused.
pub async fn verify_admin_session_in(
    db: &PgPool,
    table: &'static str,
    claims: &GatewayJwtClaims,
) -> Result<(), AuthError> {
    let sql = format!("SELECT is_active AND expires_at > NOW() FROM {table} WHERE id = $1");
    verify_admin_session_by(db, table, &sql, claims).await
}

/// [`verify_admin_session_in`] for a tenant administrator: the session must
/// be live AND its administrator must not owe a password change. An
/// administrator whose password was generated for it
/// (`tenant_admins.password_change_required`) may only change it, on the
/// control plane; the gateway admits nothing until then, or it would be the
/// way around that rule.
pub async fn verify_tenant_admin_session(
    db: &PgPool,
    claims: &GatewayJwtClaims,
) -> Result<(), AuthError> {
    const SQL: &str = "SELECT s.is_active AND s.expires_at > NOW() \
                       AND NOT a.password_change_required \
                       FROM tenant_admin_sessions s \
                       JOIN tenant_admins a ON a.id = s.admin_id AND a.tenant_id = s.tenant_id \
                       WHERE s.id = $1";
    verify_admin_session_by(db, "tenant_admin_sessions", SQL, claims).await
}

/// The session answer for `claims`' `jti`, from `sql` (one boolean column,
/// `$1` the session id), cached under `table`. Missing row = not active.
async fn verify_admin_session_by(
    db: &PgPool,
    table: &'static str,
    sql: &str,
    claims: &GatewayJwtClaims,
) -> Result<(), AuthError> {
    let Some(jti) = claims.jti.as_deref().filter(|j| !j.is_empty()) else {
        return Err(AuthError::InvalidJwt(
            "admin token has no session id (jti)".into(),
        ));
    };

    let cache_key = format!("{table}:{jti}");
    let active = match admin_session_cache().get(&cache_key) {
        Some(active) => active,
        None => {
            let row: Option<(bool,)> = sqlx::query_as(sql)
                .bind(jti)
                .fetch_optional(db)
                .await
                .map_err(|e| AuthError::Internal(format!("admin session lookup failed: {e}")))?;
            let active = row.map(|(a,)| a).unwrap_or(false);
            admin_session_cache().insert(cache_key, active);
            active
        }
    };

    if active {
        Ok(())
    } else {
        Err(AuthError::InvalidJwt(
            "admin session is not active (logged out, revoked or expired)".into(),
        ))
    }
}

/// Refuse a request that was not authenticated with a session token of
/// `token_type`.
///
/// Tokens of every registered type are signed with the same `JWT_SECRET`
/// and pass [`authenticate_request`] as [`AuthType::GatewayJwt`]. A route
/// that acts for one kind of principal must ask for that kind: the portal
/// identifies the end user by the token's `user_id`, so an admin token
/// whose id happens to be numeric would act as that end user, and a
/// system-admin token (no tenant) in any tenant. Every route that
/// gates on [`AuthType::GatewayJwt`] calls this right after
/// (`tests/portal_token_guard.rs` enforces it).
///
/// It reads the type the registry verified the request's own token as
/// ([`AuthContext::session_token_type`]), not a second decoding of the headers.
pub fn require_token_type(ctx: &AuthContext, token_type: &str) -> Result<(), AuthError> {
    match ctx.session_token_type {
        Some(verified) if verified == token_type => Ok(()),
        _ => Err(AuthError::NotASessionOfType(token_type.to_string())),
    }
}

/// Decode the gateway JWT a request carries, if any, without
/// authenticating the request. Lets a route read claims that
/// [`AuthContext`] does not carry (notably `admin_type`) after
/// [`authenticate_request`] has already accepted the token.
pub fn gateway_claims_from_headers(
    headers: &HeaderMap,
    jwt_secret: &str,
    verifiers: &CredentialVerifiers,
) -> Option<GatewayJwtClaims> {
    let from_gateway_header = headers
        .get("x-gateway-authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "));
    let from_authorization = headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| {
            v.strip_prefix("Bearer ")
                .or_else(|| v.strip_prefix("Bearer: "))
        })
        .map(str::trim)
        .filter(|v| !v.starts_with(crate::constants::headers::BEARER_API_KEY_PREFIX));
    from_gateway_header
        .or(from_authorization)
        .and_then(|token| decode_gateway_jwt(token, jwt_secret, verifiers).ok())
}

/// Why a token is not a gateway session token.
#[derive(Debug)]
pub(crate) enum GatewayJwtError {
    /// Not ours, or not a session: the signature does not verify, it is a
    /// pre-session token, it has expired or has no expiry. The request may
    /// still carry some other credential.
    NotAGatewayJwt(AuthError),
    /// Signed with our secret, and of a type this installation has no
    /// verifier for (or of no type). Never a credential, and never
    /// something else.
    NotACredentialHere(AuthError),
}

/// Decode and validate a Gateway JWT, **enforcing expiry**.
///
/// SECURITY: every gateway JWT the control plane issues carries an `exp`
/// claim (see `app/core/end_user/services/authentication.py` and
/// `unified_auth_service.py`). We reject a token whose `exp` is in the past
/// (beyond [`JWT_EXP_LEEWAY_SECS`] of clock skew) and a token that omits
/// `exp` entirely — otherwise a leaked or revoked-by-expiry token would
/// authenticate forever. Expiry is enforced manually here rather than via
/// `jsonwebtoken`'s `validate_exp` so the policy (leeway, a distinct
/// [`AuthError::ExpiredJwt`], and the missing-`exp` rejection) is explicit.
pub(crate) fn decode_gateway_jwt(
    token: &str,
    secret: &str,
    verifiers: &CredentialVerifiers,
) -> Result<GatewayJwtClaims, GatewayJwtError> {
    let claims =
        decode_signed_session_claims(token, secret).map_err(GatewayJwtError::NotAGatewayJwt)?;
    // The token-type registry: a `typ` nobody registered,
    // and a token with none, is not a credential. Asked before expiry, as
    // the fixed list it replaces was.
    verifiers
        .token_type_of(&claims)
        .map_err(GatewayJwtError::NotACredentialHere)?;
    check_not_expired(&claims).map_err(GatewayJwtError::NotAGatewayJwt)?;
    Ok(claims)
}

/// The claims of a token signed with `secret` that is not a pre-session
/// token. Its type and expiry are not checked here.
fn decode_signed_session_claims(token: &str, secret: &str) -> Result<GatewayJwtClaims, AuthError> {
    let key = DecodingKey::from_secret(secret.as_bytes());
    let mut validation = Validation::default();
    validation.set_required_spec_claims::<&str>(&[]);
    validation.validate_exp = false; // enforced manually below

    let data = decode::<GatewayJwtClaims>(token, &key, &validation)
        .map_err(|e| AuthError::InvalidJwt(e.to_string()))?;
    let claims = data.claims;

    // SECURITY: one `JWT_SECRET` also signs the admin and portal 2FA
    // *challenge* tokens (minted after the password, before the second
    // factor) and the control plane's service tokens. Those carry a `type`
    // claim; a session token never does. Refuse anything with `type`, so a
    // challenge token can never drive the proxy. (Whether its `typ` is one
    // this installation accepts is the registry's question, asked by
    // `decode_gateway_jwt`.)
    if let Some(kind) = claims.token_type.as_deref() {
        return Err(AuthError::InvalidJwt(format!(
            "a '{kind}' token is not a gateway session credential"
        )));
    }
    Ok(claims)
}

fn check_not_expired(claims: &GatewayJwtClaims) -> Result<(), AuthError> {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| AuthError::Internal("system clock is before the UNIX epoch".into()))?
        .as_secs();
    match claims.exp {
        Some(exp) if exp.saturating_add(JWT_EXP_LEEWAY_SECS) < now => {
            return Err(AuthError::ExpiredJwt);
        }
        Some(_) => {}
        None => {
            return Err(AuthError::InvalidJwt(
                "gateway JWT is missing the required exp claim".into(),
            ));
        }
    }
    Ok(())
}

/// Reject a Gateway JWT whose `tenant_id` claim disagrees with the tenant
/// the request is acting for (resolved by the tenant middleware, normally
/// from `X-Tenant-ID`, and passed to [`authenticate_request`]).
///
/// SECURITY: a portal/end-user or tenant-admin token is scoped to exactly
/// one tenant. Without this check a caller could present a valid tenant-A
/// token together with `X-Tenant-ID: B` — access/quota would be evaluated
/// against B (the middleware tenant) while audit/billing attribute to A
/// (the claim), splitting one request across two tenants. We require the
/// two to agree whenever both are known.
///
/// A claim of `None` is allowed to act for the requested tenant: system-admin
/// tokens carry no `tenant_id`, and cross-tenant admin access is intentional.
/// A non-real request tenant (`""`/`"n/a"` — e.g. no header and nothing else
/// resolved) leaves the claim authoritative; there is nothing to contradict.
fn check_jwt_tenant_matches(
    claim_tenant: Option<&str>,
    request_tenant: &str,
) -> Result<(), AuthError> {
    let request_known = !request_tenant.is_empty() && request_tenant != "n/a";
    match claim_tenant {
        Some(claim) if request_known && claim != request_tenant => Err(AuthError::TenantMismatch),
        _ => Ok(()),
    }
}

/// The single rule for keys without a governing group.
///
/// * A key bound to a group is accepted only while that group exists and is
///   active: a deactivated group's keys stop working, rather than falling
///   back to some other group's access (this used to land on Default).
/// * A key bound to no group is accepted only if it is the platform service
///   key; its calls are billed to the tenant's Unbound group.
pub(crate) fn unbound_key_refusal(key: &ApiKey) -> Result<(), AuthError> {
    match (&key.group_id, key.is_platform_service) {
        (Some(_), _) if key.group_active != Some(true) => Err(AuthError::UnboundKey(
            "API key's resource group is inactive or no longer exists".into(),
        )),
        (None, false) => Err(AuthError::UnboundKey(
            "API key is not bound to a resource group: every call must be governed by a group's limits and policies".into(),
        )),
        _ => Ok(()),
    }
}

#[derive(Debug, thiserror::Error)]
pub enum AuthError {
    #[error("Authentication required")]
    Unauthenticated,
    #[error("Invalid API key: {0}")]
    InvalidApiKey(String),
    #[error("Invalid JWT: {0}")]
    InvalidJwt(String),
    #[error("Authentication token has expired")]
    ExpiredJwt,
    /// A session token of a type other than the one the route serves
    /// ([`require_token_type`]). Carries the type that was required.
    #[error("Invalid JWT: a {0} session token is required")]
    NotASessionOfType(String),
    /// A session that was signed out: its token is on the revocation list.
    #[error("Invalid JWT: {0}")]
    SessionRevoked(String),
    /// A session issued before its user's session cutoff (a password change,
    /// an administrator's cutoff).
    #[error("Invalid JWT: {0}")]
    SessionEnded(String),
    #[error("Token is not valid for the requested tenant")]
    TenantMismatch,
    /// A valid key that no resource group governs.
    #[error("{0}")]
    UnboundKey(String),
    /// A workload agent named, in `X-Resource-Group`, a resource group it
    /// is not a member of.
    #[error(
        "resource_group_not_member: X-Resource-Group names a resource group the agent is not a member of"
    )]
    GroupNotMember,
    #[error("Internal auth error: {0}")]
    Internal(String),

    /// The answer could not be read: fails closed, and says "try again", not
    /// "your credential is bad".
    #[error("Authentication is temporarily unavailable: {0}")]
    Unavailable(String),
}

impl AuthError {
    pub fn status_code(&self) -> axum::http::StatusCode {
        match self {
            AuthError::Unauthenticated => axum::http::StatusCode::UNAUTHORIZED,
            AuthError::InvalidApiKey(_) => axum::http::StatusCode::UNAUTHORIZED,
            AuthError::InvalidJwt(_) => axum::http::StatusCode::UNAUTHORIZED,
            AuthError::ExpiredJwt => axum::http::StatusCode::UNAUTHORIZED,
            AuthError::NotASessionOfType(_)
            | AuthError::SessionRevoked(_)
            | AuthError::SessionEnded(_) => axum::http::StatusCode::UNAUTHORIZED,
            // Valid credential, but presented against the wrong tenant — an
            // authorization (isolation) failure, not an authentication one.
            AuthError::TenantMismatch => axum::http::StatusCode::FORBIDDEN,
            // A valid credential that no group governs: an authorization
            // refusal, not an authentication one.
            AuthError::UnboundKey(_) => axum::http::StatusCode::FORBIDDEN,
            // A valid agent, acting where it has no membership.
            AuthError::GroupNotMember => axum::http::StatusCode::FORBIDDEN,
            AuthError::Internal(_) => axum::http::StatusCode::INTERNAL_SERVER_ERROR,
            AuthError::Unavailable(_) => axum::http::StatusCode::SERVICE_UNAVAILABLE,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::StatusCode;
    use jsonwebtoken::{EncodingKey, Header, encode};

    use crate::services::credential_verifier::{
        CredentialVerifier, RefuseUnregistered, TOKEN_TYPE_PORTAL,
    };

    struct Accepts;
    #[async_trait::async_trait]
    impl CredentialVerifier for Accepts {
        async fn verify(&self, _: &PgPool, _: &GatewayJwtClaims) -> Result<(), AuthError> {
            Ok(())
        }
    }

    /// The Community registry.
    fn community() -> CredentialVerifiers {
        CredentialVerifiers::community(RefuseUnregistered)
    }

    /// A registry with every type the platform issues: Community's, and
    /// `portal` (verifier irrelevant: decoding asks only whether the type
    /// is registered).
    fn every_type() -> CredentialVerifiers {
        let mut registry = community();
        registry
            .register(TOKEN_TYPE_PORTAL, std::sync::Arc::new(Accepts))
            .expect("register");
        registry
    }

    /// `decode_gateway_jwt` against [`every_type`], as the error a caller
    /// sees.
    fn decode(token: &str, secret: &str) -> Result<GatewayJwtClaims, AuthError> {
        decode_with(token, secret, &every_type())
    }

    fn decode_with(
        token: &str,
        secret: &str,
        verifiers: &CredentialVerifiers,
    ) -> Result<GatewayJwtClaims, AuthError> {
        decode_gateway_jwt(token, secret, verifiers).map_err(|e| match e {
            GatewayJwtError::NotAGatewayJwt(e) | GatewayJwtError::NotACredentialHere(e) => e,
        })
    }

    /// Helper: encode a GatewayJwtClaims into a signed JWT string.
    fn make_jwt(claims: &GatewayJwtClaims, secret: &str) -> String {
        encode(
            &Header::default(),
            claims,
            &EncodingKey::from_secret(secret.as_bytes()),
        )
        .expect("failed to encode JWT")
    }

    fn full_claims() -> GatewayJwtClaims {
        GatewayJwtClaims {
            typ: Some("gateway".into()),
            token_type: None,
            user_id: Some("user-123".into()),
            tenant_id: Some("tenant-abc".into()),
            email: Some("alice@example.com".into()),
            username: Some("alice".into()),
            user_type: Some("admin".into()),
            permissions: Some(vec!["read".into(), "write".into()]),
            admin_type: Some("super".into()),
            exp: Some(9_999_999_999),
            iat: Some(1_700_000_000),
            jti: Some("jwt-id-1".into()),
        }
    }

    // -----------------------------------------------------------------------
    // decode_gateway_jwt tests
    // -----------------------------------------------------------------------

    #[test]
    fn test_decode_gateway_jwt_all_fields() {
        let secret = "test-secret-key";
        let claims = full_claims();
        let token = make_jwt(&claims, secret);

        let decoded = decode(&token, secret).expect("should decode");
        assert_eq!(decoded.user_id.as_deref(), Some("user-123"));
        assert_eq!(decoded.tenant_id.as_deref(), Some("tenant-abc"));
        assert_eq!(decoded.email.as_deref(), Some("alice@example.com"));
        assert_eq!(decoded.username.as_deref(), Some("alice"));
        assert_eq!(decoded.user_type.as_deref(), Some("admin"));
        assert_eq!(
            decoded.permissions,
            Some(vec!["read".into(), "write".into()])
        );
        assert_eq!(decoded.admin_type.as_deref(), Some("super"));
        assert_eq!(decoded.exp, Some(9_999_999_999));
        assert_eq!(decoded.iat, Some(1_700_000_000));
        assert_eq!(decoded.jti.as_deref(), Some("jwt-id-1"));
    }

    #[test]
    fn test_decode_gateway_jwt_minimal_fields() {
        let secret = "minimal-secret";
        let claims = GatewayJwtClaims {
            typ: Some("gateway".into()),
            token_type: None,
            user_id: None,
            tenant_id: Some("tenant-only".into()),
            email: None,
            username: None,
            user_type: None,
            permissions: None,
            admin_type: None,
            // exp is required (enforced); everything else is optional.
            exp: Some(ts_from_now(3600)),
            iat: None,
            jti: None,
        };
        let token = make_jwt(&claims, secret);

        let decoded = decode(&token, secret).expect("should decode");
        assert_eq!(decoded.tenant_id.as_deref(), Some("tenant-only"));
        assert!(decoded.user_id.is_none());
        assert!(decoded.email.is_none());
        assert!(decoded.username.is_none());
    }

    #[test]
    fn test_decode_gateway_jwt_wrong_secret() {
        let claims = full_claims();
        let token = make_jwt(&claims, "correct-secret");

        let result = decode(&token, "wrong-secret");
        assert!(result.is_err());
        match result.unwrap_err() {
            AuthError::InvalidJwt(msg) => assert!(!msg.is_empty()),
            other => panic!("expected InvalidJwt, got {other:?}"),
        }
    }

    #[test]
    fn test_decode_gateway_jwt_garbage_token() {
        let result = decode("not.a.jwt.at.all", "any-secret");
        assert!(result.is_err());
        match result.unwrap_err() {
            AuthError::InvalidJwt(_) => {}
            other => panic!("expected InvalidJwt, got {other:?}"),
        }
    }

    // -----------------------------------------------------------------------
    // AuthError::status_code tests
    // -----------------------------------------------------------------------

    #[test]
    fn test_auth_error_status_unauthenticated() {
        assert_eq!(
            AuthError::Unauthenticated.status_code(),
            StatusCode::UNAUTHORIZED
        );
    }

    #[test]
    fn test_auth_error_status_invalid_api_key() {
        assert_eq!(
            AuthError::InvalidApiKey("bad".into()).status_code(),
            StatusCode::UNAUTHORIZED
        );
    }

    #[test]
    fn test_auth_error_status_invalid_jwt() {
        assert_eq!(
            AuthError::InvalidJwt("bad".into()).status_code(),
            StatusCode::UNAUTHORIZED
        );
    }

    #[test]
    fn test_auth_error_status_expired_jwt() {
        assert_eq!(
            AuthError::ExpiredJwt.status_code(),
            StatusCode::UNAUTHORIZED
        );
    }

    #[test]
    fn test_auth_error_status_tenant_mismatch() {
        assert_eq!(
            AuthError::TenantMismatch.status_code(),
            StatusCode::FORBIDDEN
        );
    }

    // -----------------------------------------------------------------------
    // check_jwt_tenant_matches tests
    // -----------------------------------------------------------------------

    #[test]
    fn test_jwt_tenant_matches_same_tenant_ok() {
        assert!(check_jwt_tenant_matches(Some("tenant-a"), "tenant-a").is_ok());
    }

    #[test]
    fn test_jwt_tenant_matches_different_tenant_rejected() {
        // Valid tenant-A token + X-Tenant-ID: tenant-b → isolation violation.
        match check_jwt_tenant_matches(Some("tenant-a"), "tenant-b") {
            Err(AuthError::TenantMismatch) => {}
            other => panic!("expected TenantMismatch, got {other:?}"),
        }
    }

    #[test]
    fn test_jwt_tenant_matches_no_claim_acts_for_request_tenant() {
        // System-admin tokens carry no tenant_id — allowed to act for any.
        assert!(check_jwt_tenant_matches(None, "tenant-b").is_ok());
    }

    #[test]
    fn test_jwt_tenant_matches_unknown_request_tenant_allows_claim() {
        // No header / nothing resolved: the claim is authoritative.
        assert!(check_jwt_tenant_matches(Some("tenant-a"), "").is_ok());
        assert!(check_jwt_tenant_matches(Some("tenant-a"), "n/a").is_ok());
    }

    #[test]
    fn test_auth_error_status_internal() {
        assert_eq!(
            AuthError::Internal("oops".into()).status_code(),
            StatusCode::INTERNAL_SERVER_ERROR
        );
    }

    // -----------------------------------------------------------------------
    // Additional JWT validation tests
    // -----------------------------------------------------------------------

    #[test]
    fn test_decode_gateway_jwt_only_exp_present() {
        // All optional claims absent is tolerated, as long as the required
        // exp is present and unexpired.
        let secret = "empty-claims-secret";
        let claims = GatewayJwtClaims {
            typ: Some("gateway".into()),
            token_type: None,
            user_id: None,
            tenant_id: None,
            email: None,
            username: None,
            user_type: None,
            permissions: None,
            admin_type: None,
            exp: Some(ts_from_now(3600)),
            iat: None,
            jti: None,
        };
        let token = make_jwt(&claims, secret);
        let decoded = decode(&token, secret).expect("should decode");
        assert!(decoded.user_id.is_none());
        assert!(decoded.tenant_id.is_none());
        assert!(decoded.email.is_none());
        assert!(decoded.permissions.is_none());
    }

    /// Returns a unix timestamp `offset_secs` away from now (negative = past).
    fn ts_from_now(offset_secs: i64) -> u64 {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;
        (now + offset_secs).max(0) as u64
    }

    fn claims_with_exp(exp: Option<u64>) -> GatewayJwtClaims {
        GatewayJwtClaims {
            typ: Some("gateway".into()),
            token_type: None,
            user_id: Some("user-exp".into()),
            tenant_id: Some("tenant-1".into()),
            email: None,
            username: None,
            user_type: None,
            permissions: None,
            admin_type: None,
            exp,
            iat: Some(0),
            jti: None,
        }
    }

    #[test]
    fn test_decode_gateway_jwt_expired_token_is_rejected() {
        // SECURITY: an expired token must NOT authenticate. (Previously this
        // path accepted expired tokens forever — see ExpiredJwt enforcement.)
        let secret = "exp-test-secret";
        let token = make_jwt(&claims_with_exp(Some(1)), secret); // 1970 — long expired
        match decode(&token, secret) {
            Err(AuthError::ExpiredJwt) => {}
            other => panic!("expected ExpiredJwt, got {other:?}"),
        }
    }

    #[test]
    fn test_decode_gateway_jwt_just_expired_outside_leeway_is_rejected() {
        let secret = "exp-test-secret";
        // Expired well beyond the clock-skew leeway.
        let token = make_jwt(
            &claims_with_exp(Some(ts_from_now(-(JWT_EXP_LEEWAY_SECS as i64) - 30))),
            secret,
        );
        assert!(matches!(decode(&token, secret), Err(AuthError::ExpiredJwt)));
    }

    #[test]
    fn test_decode_gateway_jwt_recently_expired_within_leeway_is_accepted() {
        let secret = "exp-test-secret";
        // Expired a few seconds ago — inside the clock-skew leeway window.
        let token = make_jwt(&claims_with_exp(Some(ts_from_now(-5))), secret);
        let decoded = decode(&token, secret).expect("token within leeway should decode");
        assert_eq!(decoded.user_id.as_deref(), Some("user-exp"));
    }

    #[test]
    fn test_decode_gateway_jwt_future_exp_is_accepted() {
        let secret = "exp-test-secret";
        let token = make_jwt(&claims_with_exp(Some(ts_from_now(3600))), secret);
        let decoded = decode(&token, secret).expect("unexpired token should decode");
        assert_eq!(decoded.tenant_id.as_deref(), Some("tenant-1"));
    }

    #[test]
    fn test_decode_gateway_jwt_missing_exp_is_rejected() {
        // Every gateway JWT the control plane issues carries exp; a token
        // without one is not a credential we mint and must be rejected.
        let secret = "exp-test-secret";
        let token = make_jwt(&claims_with_exp(None), secret);
        match decode(&token, secret) {
            Err(AuthError::InvalidJwt(msg)) => assert!(msg.contains("exp")),
            other => panic!("expected InvalidJwt about exp, got {other:?}"),
        }
    }

    #[test]
    fn a_2fa_challenge_token_is_never_a_gateway_credential() {
        // The control plane's admin challenge (`type: 2fa_challenge`) and the
        // portal's (`type: portal_2fa_challenge`) are signed with the same
        // secret. Both carry a `type` claim; a session token never does.
        let secret = "shared-secret";
        for kind in [
            "2fa_challenge",
            "portal_2fa_challenge",
            // Issued to a user who must enrol before signing in.
            "portal_2fa_enrollment",
            "2fa_enrollment",
            "anything",
        ] {
            let token = make_jwt(
                &GatewayJwtClaims {
                    typ: None,
                    token_type: Some(kind.into()),
                    tenant_id: Some("tenant-1".into()),
                    exp: Some(ts_from_now(300)),
                    ..Default::default()
                },
                secret,
            );
            match decode(&token, secret) {
                Err(AuthError::InvalidJwt(msg)) => assert!(msg.contains(kind), "{msg}"),
                other => panic!("challenge token '{kind}' must be refused, got {other:?}"),
            }
            // And it is refused for being a pre-session token, whatever
            // session type it also claims: a `typ` does not make it one.
            for typ in ["portal", "admin", "gateway"] {
                let token = make_jwt(
                    &GatewayJwtClaims {
                        typ: Some(typ.into()),
                        token_type: Some(kind.into()),
                        tenant_id: Some("tenant-1".into()),
                        jti: Some("j".into()),
                        exp: Some(ts_from_now(300)),
                        ..Default::default()
                    },
                    secret,
                );
                assert!(
                    matches!(
                        decode_gateway_jwt(&token, secret, &every_type()),
                        Err(GatewayJwtError::NotAGatewayJwt(AuthError::InvalidJwt(_)))
                    ),
                    "a '{kind}' token that says typ={typ} must be refused"
                );
            }
        }
    }

    fn typed(typ: Option<&str>) -> GatewayJwtClaims {
        GatewayJwtClaims {
            typ: typ.map(String::from),
            exp: Some(ts_from_now(300)),
            ..Default::default()
        }
    }

    #[test]
    fn a_registered_type_decodes_and_the_rest_are_refused() {
        let secret = "shared-secret";
        let registry = every_type();
        assert_eq!(registry.token_types(), vec!["admin", "gateway", "portal"]);
        for typ in registry.token_types() {
            let token = make_jwt(&typed(Some(typ)), secret);
            assert!(decode(&token, secret).is_ok(), "typ={typ}");
        }
        for typ in ["registry", "service", "refresh", ""] {
            let token = make_jwt(&typed(Some(typ)), secret);
            assert!(
                matches!(
                    decode_gateway_jwt(&token, secret, &registry),
                    Err(GatewayJwtError::NotACredentialHere(AuthError::InvalidJwt(
                        _
                    )))
                ),
                "typ={typ:?} must be refused"
            );
        }
    }

    #[test]
    fn a_portal_token_is_not_a_credential_of_the_community_registry() {
        // Community has no verifier for `portal`.
        let secret = "shared-secret";
        let token = make_jwt(&typed(Some("portal")), secret);
        assert!(decode(&token, secret).is_ok(), "with the portal registered");
        match decode_gateway_jwt(&token, secret, &community()) {
            Err(GatewayJwtError::NotACredentialHere(AuthError::InvalidJwt(msg))) => {
                assert!(msg.contains("portal"), "{msg}")
            }
            other => panic!("expected a refusal, got {other:?}"),
        }
        for typ in ["admin", "gateway"] {
            let token = make_jwt(&typed(Some(typ)), secret);
            assert!(
                decode_with(&token, secret, &community()).is_ok(),
                "typ={typ}"
            );
        }
    }

    #[test]
    fn a_token_without_a_type_is_refused() {
        // Every issuer mints `typ`, and accepting a token
        // with none also accepted what other services sign with the same
        // secret: the service tokens the control plane and the MCP registry
        // sign for each other, and the skill runner's execution token where
        // the runner is given the gateway's secret.
        let secret = "s";
        let token = make_jwt(&typed(None), secret);
        for registry in [community(), every_type()] {
            assert!(matches!(
                decode_gateway_jwt(&token, secret, &registry),
                Err(GatewayJwtError::NotACredentialHere(AuthError::InvalidJwt(
                    _
                )))
            ));
        }
        // Typeless and unknown-type shapes, signed with the gateway's secret.
        for claims in [
            serde_json::json!({"exec": "e", "tnt": "t", "skl": "s", "exp": ts_from_now(300)}),
            serde_json::json!({"sub": "admin:a", "iss": "brutor-control-plane",
                               "iat": ts_from_now(0), "exp": ts_from_now(60)}),
        ] {
            let token = encode(
                &Header::default(),
                &claims,
                &EncodingKey::from_secret(secret.as_bytes()),
            )
            .expect("encode");
            assert!(decode(&token, secret).is_err(), "{claims}");
        }
    }

    #[test]
    fn an_expired_token_of_an_unregistered_type_is_still_not_a_credential_here() {
        // The type is asked before the expiry, as the fixed list was: the
        // answer for a portal token on a Community assembly does not
        // depend on its age.
        let secret = "s";
        let expired = GatewayJwtClaims {
            exp: Some(1),
            ..typed(Some("portal"))
        };
        let token = make_jwt(&expired, secret);
        assert!(matches!(
            decode_gateway_jwt(&token, secret, &community()),
            Err(GatewayJwtError::NotACredentialHere(_))
        ));
        assert!(matches!(
            decode_gateway_jwt(&token, secret, &every_type()),
            Err(GatewayJwtError::NotAGatewayJwt(AuthError::ExpiredJwt))
        ));
    }

    fn ctx_with(session_token_type: Option<&'static str>) -> AuthContext {
        AuthContext {
            auth_type: AuthType::GatewayJwt,
            tenant_id: "t1".into(),
            gateway_user_id: Some("7".into()),
            gateway_username: None,
            gateway_tenant_id: Some("t1".into()),
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
            session_token_type,
            resolved_group_id: None,
            actor_agent_id: None,
            agent_impl: crate::services::agent_impl::request_carriers(),
            admission_hold: None,
        }
    }

    #[test]
    fn require_token_type_asks_for_the_type_that_was_verified() {
        assert!(require_token_type(&ctx_with(Some("portal")), "portal").is_ok());
        for other in [Some("admin"), Some("gateway"), None] {
            match require_token_type(&ctx_with(other), "portal") {
                Err(e @ AuthError::NotASessionOfType(_)) => {
                    assert_eq!(
                        e.to_string(),
                        "Invalid JWT: a portal session token is required"
                    );
                    assert_eq!(e.status_code(), StatusCode::UNAUTHORIZED);
                }
                got => panic!("{other:?} must not pass as a portal session, got {got:?}"),
            }
        }
    }

    #[test]
    fn gateway_claims_from_headers_reads_either_header_and_skips_api_keys() {
        let secret = "hdr-secret";
        let token = make_jwt(&claims_with_exp(Some(ts_from_now(300))), secret);
        let mut h = HeaderMap::new();
        h.insert("authorization", format!("Bearer {token}").parse().unwrap());
        assert_eq!(
            gateway_claims_from_headers(&h, secret, &every_type()).and_then(|c| c.user_id),
            Some("user-exp".to_string())
        );
        let mut h = HeaderMap::new();
        h.insert(
            "x-gateway-authorization",
            format!("Bearer {token}").parse().unwrap(),
        );
        assert!(gateway_claims_from_headers(&h, secret, &every_type()).is_some());
        let mut h = HeaderMap::new();
        h.insert(
            "authorization",
            "Bearer sk_brutor_api_not_a_jwt".parse().unwrap(),
        );
        assert!(gateway_claims_from_headers(&h, secret, &every_type()).is_none());
        assert!(gateway_claims_from_headers(&h, "other", &every_type()).is_none());
    }

    #[test]
    fn test_decode_gateway_jwt_empty_string_token() {
        let result = decode("", "any-secret");
        assert!(result.is_err());
        match result.unwrap_err() {
            AuthError::InvalidJwt(_) => {}
            other => panic!("expected InvalidJwt, got {other:?}"),
        }
    }

    #[test]
    fn test_decode_gateway_jwt_with_permissions_list() {
        let secret = "perm-test";
        let claims = GatewayJwtClaims {
            typ: Some("gateway".into()),
            token_type: None,
            user_id: Some("u1".into()),
            tenant_id: Some("t1".into()),
            email: None,
            username: None,
            user_type: None,
            permissions: Some(vec![
                "admin:read".into(),
                "admin:write".into(),
                "models:manage".into(),
            ]),
            admin_type: None,
            exp: Some(ts_from_now(3600)),
            iat: None,
            jti: None,
        };
        let token = make_jwt(&claims, secret);
        let decoded = decode(&token, secret).unwrap();
        let perms = decoded.permissions.unwrap();
        assert_eq!(perms.len(), 3);
        assert!(perms.contains(&"admin:read".to_string()));
        assert!(perms.contains(&"models:manage".to_string()));
    }

    // -----------------------------------------------------------------------
    // active_group_from_headers — x-resource-group propagation
    // -----------------------------------------------------------------------

    #[test]
    fn test_active_group_from_headers_reads_x_resource_group() {
        let mut h = HeaderMap::new();
        h.insert("x-resource-group", "group-engineering".parse().unwrap());
        assert_eq!(
            active_group_from_headers(&h),
            Some("group-engineering".to_string()),
        );
    }

    #[test]
    fn test_active_group_from_headers_none_when_absent_or_blank() {
        // Absent header → None.
        assert_eq!(active_group_from_headers(&HeaderMap::new()), None);
        // Whitespace-only → None (treated as not supplied).
        let mut h = HeaderMap::new();
        h.insert("x-resource-group", "   ".parse().unwrap());
        assert_eq!(active_group_from_headers(&h), None);
    }

    // -----------------------------------------------------------------------
    // AuthContext helper method tests
    // -----------------------------------------------------------------------

    #[test]
    fn test_auth_context_guardrail_user_id_from_api_key_user() {
        let ctx = AuthContext {
            auth_type: AuthType::ApiKey,
            tenant_id: "t1".into(),
            gateway_user_id: Some("group-uuid".into()),
            gateway_username: None,
            gateway_tenant_id: Some("t1".into()),
            idp_user_id: None,
            idp_username: None,
            idp_email: None,
            api_key_id: Some("key-1".into()),
            api_key_name: Some("test-key".into()),
            api_key_scope: Some("user".into()),
            api_key_group_id: None,
            api_key_is_platform: false,
            api_key_user_id: Some(42),
            allowed_skill_ids: None,
            client_ip: None,
            session_key: None,
            resolved_group_id: None,
            actor_agent_id: None,
            session_token_type: None,
            agent_impl: crate::services::agent_impl::request_carriers(),
            admission_hold: None,
        };
        // api_key_user_id takes priority
        assert_eq!(ctx.guardrail_user_id(), Some("42".to_string()));
    }

    #[test]
    fn test_auth_context_guardrail_user_id_from_gateway_jwt() {
        let ctx = AuthContext {
            auth_type: AuthType::GatewayJwt,
            tenant_id: "t1".into(),
            gateway_user_id: Some("user-789".into()),
            gateway_username: Some("alice".into()),
            gateway_tenant_id: Some("t1".into()),
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
        };
        // No api_key_user_id, no group_id → returns gateway_user_id
        assert_eq!(ctx.guardrail_user_id(), Some("user-789".to_string()));
    }

    #[test]
    fn test_auth_context_guardrail_group_scoped_key_returns_none() {
        let ctx = AuthContext {
            auth_type: AuthType::ApiKey,
            tenant_id: "t1".into(),
            gateway_user_id: Some("group-uuid-123".into()),
            gateway_username: Some("engineering".into()),
            gateway_tenant_id: Some("t1".into()),
            idp_user_id: None,
            idp_username: None,
            idp_email: None,
            api_key_id: Some("key-2".into()),
            api_key_name: Some("group-key".into()),
            api_key_scope: Some("group".into()),
            api_key_group_id: Some("group-uuid-123".into()),
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
        };
        // Group-scoped key with no api_key_user_id → returns None
        assert!(ctx.guardrail_user_id().is_none());
        // guardrail_group_id returns the group
        assert_eq!(ctx.guardrail_group_id(), Some("group-uuid-123"));
    }

    // -----------------------------------------------------------------------
    // AuthType display tests
    // -----------------------------------------------------------------------

    #[test]
    fn test_auth_type_display() {
        assert_eq!(AuthType::ApiKey.to_string(), "api_key");
        assert_eq!(AuthType::GatewayJwt.to_string(), "gateway_jwt");
        assert_eq!(AuthType::IdpOauth.to_string(), "idp_oauth");
        assert_eq!(AuthType::OauthPassthrough.to_string(), "oauth_passthrough");
        assert_eq!(AuthType::Anonymous.to_string(), "anonymous");
    }

    #[test]
    fn test_auth_error_display_messages() {
        let e1 = AuthError::Unauthenticated;
        assert_eq!(e1.to_string(), "Authentication required");

        let e2 = AuthError::InvalidApiKey("expired".into());
        assert!(e2.to_string().contains("expired"));

        let e3 = AuthError::InvalidJwt("bad signature".into());
        assert!(e3.to_string().contains("bad signature"));

        let e4 = AuthError::Internal("db down".into());
        assert!(e4.to_string().contains("db down"));
    }

    // ── resolve_effective_tenant: the tenant a key acts for ─────

    const SHARED: bool = false;
    const PLATFORM: bool = true;

    /// With no list, a shared key acts
    /// for its own tenant only.
    #[test]
    fn effective_tenant_shared_key_naming_another_tenant_is_refused_with_no_list() {
        assert_eq!(
            resolve_effective_tenant("global", "default", "ten-abc", None, SHARED),
            None,
            "a tenant's shared key is no credential of another tenant"
        );
    }

    /// The platform service key (the control plane's, the KB uploader's)
    /// serves every tenant with no list, as before.
    #[test]
    fn effective_tenant_platform_key_with_explicit_header_redirects() {
        assert_eq!(
            resolve_effective_tenant("global", "default", "ten-abc", None, PLATFORM).as_deref(),
            Some("ten-abc"),
            "the platform key attributes to the header tenant, not the key owner"
        );
    }

    #[test]
    fn effective_tenant_global_key_without_header_stays_with_owner() {
        for platform in [SHARED, PLATFORM] {
            let resolved = resolve_effective_tenant("global", "default", "", None, platform);
            assert_eq!(resolved.as_deref(), Some("default"));
        }
    }

    #[test]
    fn effective_tenant_global_key_with_na_header_stays_with_owner() {
        // "n/a" is the sentinel the middleware uses when no tenant
        // could be resolved — treat as "not provided".
        for platform in [SHARED, PLATFORM] {
            let resolved = resolve_effective_tenant("global", "default", "n/a", None, platform);
            assert_eq!(resolved.as_deref(), Some("default"));
        }
    }

    #[test]
    fn effective_tenant_global_key_header_matches_owner() {
        // Its own tenant needs no list, and no list can take it away.
        let other = vec!["ten-other".to_string()];
        for allowed in [None, Some(other.as_slice())] {
            for platform in [SHARED, PLATFORM] {
                assert_eq!(
                    resolve_effective_tenant("global", "ten-abc", "ten-abc", allowed, platform)
                        .as_deref(),
                    Some("ten-abc"),
                    "a key acts for its own tenant"
                );
            }
        }
    }

    #[test]
    fn effective_tenant_tenant_scoped_key_ignores_header() {
        // Tenant-scoped key cannot be redirected to another tenant even if
        // the request carries a different X-Tenant-ID, and no list widens
        // it. Prevents leakage.
        let listed = vec!["ten-xyz".to_string()];
        for allowed in [None, Some(listed.as_slice())] {
            let resolved =
                resolve_effective_tenant("tenant", "ten-abc", "ten-xyz", allowed, SHARED);
            assert_eq!(
                resolved.as_deref(),
                Some("ten-abc"),
                "tenant-scoped keys are pinned to their owning tenant"
            );
        }
    }

    #[test]
    fn effective_tenant_user_scoped_key_ignores_header() {
        let resolved = resolve_effective_tenant("user", "ten-abc", "ten-xyz", None, SHARED);
        assert_eq!(resolved.as_deref(), Some("ten-abc"));
    }

    #[test]
    fn effective_tenant_group_scoped_key_ignores_header() {
        let resolved = resolve_effective_tenant("group", "ten-abc", "ten-xyz", None, SHARED);
        assert_eq!(resolved.as_deref(), Some("ten-abc"));
    }

    #[test]
    fn effective_tenant_allowlist_permits_listed_target() {
        let allow = vec!["ten-abc".to_string(), "ten-def".to_string()];
        for platform in [SHARED, PLATFORM] {
            let resolved =
                resolve_effective_tenant("global", "default", "ten-abc", Some(&allow), platform);
            assert_eq!(
                resolved.as_deref(),
                Some("ten-abc"),
                "target on the allowlist is honoured"
            );
        }
    }

    /// A target off the list is refused. (Before: the key fell back to its
    /// own tenant and the request went on naming the other one.) The list
    /// bounds the platform key too.
    #[test]
    fn effective_tenant_allowlist_refuses_unlisted_target() {
        let allow = vec!["ten-abc".to_string()];
        for platform in [SHARED, PLATFORM] {
            assert_eq!(
                resolve_effective_tenant("global", "default", "ten-evil", Some(&allow), platform),
                None,
                "target off the allowlist is refused"
            );
        }
    }
}
