// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

use serde::{Deserialize, Serialize};

/// Decoded JWT claims from the Gateway authorization header.
///
/// `typ` and `token_type` exist so the verifier can tell session tokens
/// apart from the other tokens that are minted with the same
/// `JWT_SECRET` (admin/portal 2FA challenges, service tokens). See
/// [`crate::services::auth::decode_gateway_jwt`] for the rules.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct GatewayJwtClaims {
    /// Session-token discriminator: the type the credential-verifier
    /// registry looks the token's verifier up by (`admin`, `gateway`, and
    /// `portal` where the portal is installed). A token without it is not a
    /// credential.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub typ: Option<String>,
    /// Present only on non-session tokens (`2fa_challenge`,
    /// `portal_2fa_challenge`, `portal_2fa_enrollment`). Never a valid
    /// gateway credential.
    #[serde(default, rename = "type", skip_serializing_if = "Option::is_none")]
    pub token_type: Option<String>,
    pub user_id: Option<String>,
    pub tenant_id: Option<String>,
    pub email: Option<String>,
    pub username: Option<String>,
    pub user_type: Option<String>,
    pub permissions: Option<Vec<String>>,
    pub admin_type: Option<String>,
    pub exp: Option<u64>,
    pub iat: Option<u64>,
    pub jti: Option<String>,
}

/// The result of authenticating a proxy request.
/// Mirrors Python's AuthenticationContext / EndUserContext.
#[derive(Debug, Clone)]
pub struct AuthContext {
    pub auth_type: AuthType,
    pub tenant_id: String,

    // Gateway user (from X-Gateway-Authorization JWT)
    pub gateway_user_id: Option<String>,
    pub gateway_username: Option<String>,
    pub gateway_tenant_id: Option<String>,

    // IdP user (from Authorization header — OAuth token verified via token_manager)
    pub idp_user_id: Option<String>,
    pub idp_username: Option<String>,
    pub idp_email: Option<String>,

    // API key context
    pub api_key_id: Option<String>,
    pub api_key_name: Option<String>,
    pub api_key_scope: Option<String>,
    pub api_key_group_id: Option<String>,
    /// The platform service key (`api_keys.is_platform_service`, set
    /// only by the control plane's seed). The only key allowed to have no
    /// resource group: its calls are billed to the tenant's `Unbound` group.
    pub api_key_is_platform: bool,
    pub api_key_user_id: Option<i32>,

    // API key skill restrictions
    pub allowed_skill_ids: Option<Vec<String>>,

    // Session key for MCP session management
    pub session_key: Option<String>,

    /// The registered type of the gateway session token the request was
    /// authenticated with (`admin`, `gateway`, `portal`, ...), as the
    /// credential-verifier registry verified it. `None` for every other
    /// kind of credential. A route that acts for one kind of principal asks
    /// for it with `services::auth::require_token_type`.
    pub session_token_type: Option<&'static str>,

    // Client info
    pub client_ip: Option<String>,

    // Resource group resolved for this user
    pub resolved_group_id: Option<String>,

    // Agent Identity — when the caller authenticates as an agent principal
    // (api_keys.agent_id set, or an IdP workload claim), this is the acting
    // agent's id. Drives the per-agent runtime authorization engine and is
    // stamped onto proxy_logs as the actor. None for human / plain api-key /
    // anonymous callers.
    pub actor_agent_id: Option<String>,

    // What software the caller says (or its verified token
    // claims) it is, snapshotted from the request scope when the context is
    // built, so rows written after a spawn still carry it. DESCRIBES the
    // caller; never read by an access path (guarded by
    // `tests/agent_impl_guards.rs`). The only reader is
    // `proxy_logger::build_proxy_log`.
    pub agent_impl: crate::services::agent_impl::Carriers,

    /// The admission hold of the LLM call this context authenticated, set
    /// by `llm_preflight::run_preflight` when it admits the call. The
    /// context travels with the request into the tasks that report its
    /// usage after the handler returned, and `usage::settle_llm_call`
    /// settles THIS hold: a hold is never found again by a value the caller
    /// supplied (the correlation id). `None` when nothing was admitted
    /// under a hold.
    pub admission_hold: Option<String>,
}

impl AuthContext {
    /// Return the integer user ID suitable for guardrail/membership lookups.
    ///
    /// For user-scoped API keys: `api_key_user_id`.
    /// For JWT auth: `gateway_user_id` (which is the integer user ID as string).
    /// For group-scoped API keys: `None` (gateway_user_id is the group UUID, not a user).
    pub fn guardrail_user_id(&self) -> Option<String> {
        if let Some(uid) = self.api_key_user_id {
            return Some(uid.to_string());
        }
        // For non-API-key auth (JWT), gateway_user_id is the real user ID.
        // For group-scoped API keys, gateway_user_id is the group UUID — skip it.
        if self.api_key_group_id.is_none() {
            return self.gateway_user_id.clone();
        }
        None
    }

    /// Whether the caller is an agent that authenticated with its own
    /// IdP-issued token (workload identity), not with an API key.
    pub fn is_workload_agent(&self) -> bool {
        self.auth_type == AuthType::IdpOauth && self.actor_agent_id.is_some()
    }

    /// The ONE resource group this caller is bound to, if it is bound to
    /// one: a group-scoped API key's group, or the group a workload agent
    /// acts in — which `auth::authenticate_request`
    /// read from the agent's memberships, never from a header as sent.
    /// Model access, MCP server access, billing, quotas and guardrails are
    /// decided in this group and no other. `None` for an end user (whose
    /// memberships are weighed) and for a caller no group governs.
    pub fn bound_group_id(&self) -> Option<&str> {
        self.api_key_group_id.as_deref().or_else(|| {
            if self.is_workload_agent() {
                self.resolved_group_id.as_deref()
            } else {
                None
            }
        })
    }

    /// Return the resource group ID for direct guardrail lookup: the
    /// caller's bound group (a group-scoped API key's, a workload agent's).
    pub fn guardrail_group_id(&self) -> Option<&str> {
        self.bound_group_id()
    }

    /// Resolve the resource group this request is operating in — the
    /// caller's *active workspace*, never the union of every group they
    /// belong to.
    ///
    ///   - group-scoped API key  → `api_key_group_id` carries it
    ///   - JWT / user-scoped key → `resolved_group_id`, stamped by the
    ///     tenant/auth middleware
    ///
    /// Used to scope group-filtered surfaces — notably the Agent Skills
    /// system MCP server, whose `tools/list` must expose only the
    /// skills assigned to the active workspace.
    pub fn active_resource_group(&self) -> Option<&str> {
        self.api_key_group_id
            .as_deref()
            .or(self.resolved_group_id.as_deref())
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum AuthType {
    ApiKey,
    GatewayJwt,
    IdpOauth,
    OauthPassthrough,
    Anonymous,
}

impl std::fmt::Display for AuthType {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            AuthType::ApiKey => write!(f, "api_key"),
            AuthType::GatewayJwt => write!(f, "gateway_jwt"),
            AuthType::IdpOauth => write!(f, "idp_oauth"),
            AuthType::OauthPassthrough => write!(f, "oauth_passthrough"),
            AuthType::Anonymous => write!(f, "anonymous"),
        }
    }
}

/// Admin JWT claims for admin-only endpoints (reload router, stats, etc.)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AdminJwtClaims {
    pub user_id: i64,
    pub admin_type: String,
    pub tenant_id: Option<String>,
    pub jti: String,
    pub exp: u64,
    pub iat: u64,
}

/// Build a minimal `AuthContext` for unit tests across the crate (the
/// struct has no `Default` because `AuthType` deliberately has none).
#[cfg(test)]
pub(crate) fn make_auth_context(auth_type: AuthType, tenant_id: &str) -> AuthContext {
    AuthContext {
        auth_type,
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_auth_type_display() {
        assert_eq!(AuthType::ApiKey.to_string(), "api_key");
        assert_eq!(AuthType::GatewayJwt.to_string(), "gateway_jwt");
        assert_eq!(AuthType::IdpOauth.to_string(), "idp_oauth");
        assert_eq!(AuthType::OauthPassthrough.to_string(), "oauth_passthrough");
        assert_eq!(AuthType::Anonymous.to_string(), "anonymous");
    }

    #[test]
    fn test_guardrail_user_id_from_api_key_user() {
        let mut ctx = make_auth_context(AuthType::ApiKey, "tenant-1");
        ctx.api_key_user_id = Some(42);
        // Even if gateway_user_id is set, api_key_user_id takes priority
        ctx.gateway_user_id = Some("999".to_string());

        assert_eq!(ctx.guardrail_user_id(), Some("42".to_string()));
    }

    #[test]
    fn test_guardrail_user_id_from_jwt() {
        let mut ctx = make_auth_context(AuthType::GatewayJwt, "tenant-1");
        ctx.gateway_user_id = Some("123".to_string());

        assert_eq!(ctx.guardrail_user_id(), Some("123".to_string()));
    }

    #[test]
    fn test_guardrail_user_id_none_for_group_scoped_api_key() {
        let mut ctx = make_auth_context(AuthType::ApiKey, "tenant-1");
        ctx.api_key_group_id = Some("group-uuid".to_string());
        ctx.gateway_user_id = Some("group-uuid".to_string()); // not a real user ID

        // No api_key_user_id and api_key_group_id is set, so returns None
        assert!(ctx.guardrail_user_id().is_none());
    }

    /// A caller is bound to one group by its key or,
    /// for a workload agent, by the group authentication resolved. An end
    /// user's `resolved_group_id` (the header as sent) binds nothing.
    #[test]
    fn the_bound_group_is_a_keys_or_a_workload_agents_and_never_a_header() {
        let mut ctx = make_auth_context(AuthType::GatewayJwt, "tenant-1");
        ctx.resolved_group_id = Some("named-in-a-header".to_string());
        for auth_type in [
            AuthType::GatewayJwt,
            AuthType::OauthPassthrough,
            AuthType::Anonymous,
            AuthType::IdpOauth,
        ] {
            ctx.auth_type = auth_type;
            assert!(!ctx.is_workload_agent());
            assert_eq!(ctx.bound_group_id(), None, "{:?}", ctx.auth_type);
            assert_eq!(ctx.guardrail_group_id(), None);
        }
        // An agent that authenticated with a key is bound by the key.
        ctx.auth_type = AuthType::ApiKey;
        ctx.actor_agent_id = Some("agent".to_string());
        assert!(!ctx.is_workload_agent());
        assert_eq!(ctx.bound_group_id(), None);
        ctx.api_key_group_id = Some("key-group".to_string());
        assert_eq!(ctx.bound_group_id(), Some("key-group"));

        // A workload agent: the group authentication resolved.
        ctx.api_key_group_id = None;
        ctx.auth_type = AuthType::IdpOauth;
        ctx.resolved_group_id = Some("member-group".to_string());
        assert!(ctx.is_workload_agent());
        assert_eq!(ctx.bound_group_id(), Some("member-group"));
        assert_eq!(ctx.guardrail_group_id(), Some("member-group"));
        ctx.resolved_group_id = None;
        assert_eq!(ctx.bound_group_id(), None);
    }

    #[test]
    fn test_guardrail_group_id() {
        let mut ctx = make_auth_context(AuthType::ApiKey, "tenant-1");
        assert!(ctx.guardrail_group_id().is_none());

        ctx.api_key_group_id = Some("group-abc".to_string());
        assert_eq!(ctx.guardrail_group_id(), Some("group-abc"));
    }

    #[test]
    fn test_active_resource_group_prefers_group_scoped_api_key() {
        // Regression: the Agent Skills tools/list filter must scope to
        // the active workspace. A group-scoped API key carries that
        // workspace directly and wins over any resolved_group_id.
        let mut ctx = make_auth_context(AuthType::ApiKey, "tenant-1");
        ctx.api_key_group_id = Some("group-from-key".to_string());
        ctx.resolved_group_id = Some("group-resolved".to_string());
        assert_eq!(ctx.active_resource_group(), Some("group-from-key"));
    }

    #[test]
    fn test_active_resource_group_falls_back_to_resolved() {
        let mut ctx = make_auth_context(AuthType::GatewayJwt, "tenant-1");
        ctx.resolved_group_id = Some("group-resolved".to_string());
        assert_eq!(ctx.active_resource_group(), Some("group-resolved"));
    }

    #[test]
    fn test_active_resource_group_none_when_unscoped() {
        let ctx = make_auth_context(AuthType::Anonymous, "tenant-1");
        assert!(ctx.active_resource_group().is_none());
    }

    #[test]
    fn test_gateway_jwt_claims_serde_round_trip() {
        let claims = GatewayJwtClaims {
            typ: None,
            token_type: None,
            user_id: Some("user-1".to_string()),
            tenant_id: Some("tenant-1".to_string()),
            email: Some("test@example.com".to_string()),
            username: Some("testuser".to_string()),
            user_type: Some("standard".to_string()),
            permissions: Some(vec!["read".to_string(), "write".to_string()]),
            admin_type: None,
            exp: Some(9999999999),
            iat: Some(1000000000),
            jti: Some("jti-123".to_string()),
        };
        let json = serde_json::to_value(&claims).unwrap();
        let deserialized: GatewayJwtClaims = serde_json::from_value(json).unwrap();
        assert_eq!(deserialized.user_id, Some("user-1".to_string()));
        assert_eq!(deserialized.tenant_id, Some("tenant-1".to_string()));
        assert_eq!(deserialized.permissions.unwrap().len(), 2);
    }
}
