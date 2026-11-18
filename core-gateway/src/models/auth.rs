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

    /// How the caller authenticated, computed once by the authentication
    /// that made this context ([`Credential`]). One of
    /// `auth_type.credential_kinds()`.
    pub credential_kind: CredentialKind,
    /// The credential's level on the assurance ladder; `None` outside it.
    pub credential_assurance: Option<CredentialAssurance>,
    /// A stable, non-secret reference to the credential.
    pub credential_ref: Option<String>,
    /// The workload credential's identity provider and token lifetime.
    pub credential_detail: CredentialDetail,
    /// The identity judge's verdict on this context, stored the first time
    /// a surface judges it (`services::identity_policy::judge_call`); a
    /// second judgement of the same context returns it.
    pub identity_verdict: Option<crate::services::identity_policy::IdentityVerdict>,
}

impl AuthContext {
    /// The three credential facts, as one value.
    pub fn credential(&self) -> Credential {
        Credential {
            kind: self.credential_kind,
            assurance: self.credential_assurance,
            reference: self.credential_ref.clone(),
        }
    }

    /// Whether the caller acts with a person's own key: an API key of scope
    /// `user`, or one bound to an end user (either says so; both are held).
    pub fn is_personal_key(&self) -> bool {
        self.credential_kind == CredentialKind::ApiKey
            && (self.api_key_scope.as_deref() == Some("user") || self.api_key_user_id.is_some())
    }

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
    Anonymous,
}

impl AuthType {
    /// Every variant, in declaration order. The class-level test in this
    /// file holds it equal to the declaration.
    pub const ALL: [AuthType; 4] = [
        AuthType::ApiKey,
        AuthType::GatewayJwt,
        AuthType::IdpOauth,
        AuthType::Anonymous,
    ];

    /// The credential kinds a context of this type can carry. No catch-all
    /// arm: a new way to authenticate does not compile until it says what
    /// kind of credential it is.
    pub const fn credential_kinds(&self) -> &'static [CredentialKind] {
        match self {
            AuthType::ApiKey => &[CredentialKind::ApiKey, CredentialKind::PlatformService],
            // A person's session, or a first-party service token (`gateway`
            // type: signed with the installation's secret, never minted by a
            // tenant).
            AuthType::GatewayJwt => &[
                CredentialKind::HumanSession,
                CredentialKind::PlatformService,
            ],
            AuthType::IdpOauth => &[CredentialKind::WorkloadToken],
            AuthType::Anonymous => &[CredentialKind::Anonymous],
        }
    }
}

impl std::fmt::Display for AuthType {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            AuthType::ApiKey => write!(f, "api_key"),
            AuthType::GatewayJwt => write!(f, "gateway_jwt"),
            AuthType::IdpOauth => write!(f, "idp_oauth"),
            AuthType::Anonymous => write!(f, "anonymous"),
        }
    }
}

/// What kind of credential a caller authenticated with. Computed once, at
/// authentication, and carried on [`AuthContext::credential_kind`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum CredentialKind {
    /// A Brutor API key a tenant minted (shared or personal).
    ApiKey,
    /// The platform's own credential: the platform service key
    /// (`api_keys.is_platform_service`, seeded by the control plane) or a
    /// first-party service token. Never minted by a tenant. Outside the
    /// assurance ladder: no AI System's identity policy judges it.
    PlatformService,
    /// A workload token verified against an identity provider of the tenant.
    WorkloadToken,
    /// An X.509 workload certificate presented over mutual TLS. The judge
    /// knows it; no listener authenticates with it yet.
    X509Svid,
    /// A token the gateway issued in exchange for a workload credential. The
    /// judge knows it; no route issues one yet.
    BrutorToken,
    /// A person's session (administrator, portal). Outside the ladder.
    HumanSession,
    /// No credential, on a route that lets one in.
    Anonymous,
}

impl CredentialKind {
    pub const fn as_str(self) -> &'static str {
        match self {
            CredentialKind::ApiKey => "api_key",
            CredentialKind::PlatformService => "platform_service",
            CredentialKind::WorkloadToken => "workload_token",
            CredentialKind::X509Svid => "x509_svid",
            CredentialKind::BrutorToken => "brutor_token",
            CredentialKind::HumanSession => "human_session",
            CredentialKind::Anonymous => "anonymous",
        }
    }

    /// The kind a stored name is; `None` for a name that is no kind.
    pub fn parse(name: &str) -> Option<Self> {
        Some(match name {
            "api_key" => CredentialKind::ApiKey,
            "platform_service" => CredentialKind::PlatformService,
            "workload_token" => CredentialKind::WorkloadToken,
            "x509_svid" => CredentialKind::X509Svid,
            "brutor_token" => CredentialKind::BrutorToken,
            "human_session" => CredentialKind::HumanSession,
            "anonymous" => CredentialKind::Anonymous,
            _ => return None,
        })
    }

    /// A credential issued to a workload by an identity provider (the kinds
    /// an `allowed_issuers` list speaks about).
    pub const fn is_workload(self) -> bool {
        matches!(
            self,
            CredentialKind::WorkloadToken | CredentialKind::X509Svid | CredentialKind::BrutorToken
        )
    }

    /// Outside the assurance ladder: never judged by an identity policy.
    pub const fn is_exempt(self) -> bool {
        matches!(
            self,
            CredentialKind::HumanSession | CredentialKind::PlatformService
        )
    }
}

/// How strongly a machine credential proves it is the caller it names,
/// weakest first (the ordering is the ladder).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum CredentialAssurance {
    /// An API key with no network binding: usable anywhere until revoked.
    Key,
    /// An API key bound to networks and an expiry within the ceiling.
    BoundKey,
    /// A verified workload token within the lifetime ceiling.
    Workload,
    /// A credential bound to a private key the caller holds.
    WorkloadPop,
}

impl CredentialAssurance {
    /// The ladder, weakest first.
    pub const LADDER: [CredentialAssurance; 4] = [
        CredentialAssurance::Key,
        CredentialAssurance::BoundKey,
        CredentialAssurance::Workload,
        CredentialAssurance::WorkloadPop,
    ];

    pub const fn as_str(self) -> &'static str {
        match self {
            CredentialAssurance::Key => "key",
            CredentialAssurance::BoundKey => "bound_key",
            CredentialAssurance::Workload => "workload",
            CredentialAssurance::WorkloadPop => "workload_pop",
        }
    }

    /// The level a stored name is; `None` for a name that is no level.
    pub fn parse(name: &str) -> Option<Self> {
        Self::LADDER
            .into_iter()
            .find(|level| level.as_str() == name)
    }
}

/// The longest credential reference: the evidence column's width.
pub const CREDENTIAL_REF_MAX: usize = 512;

/// The reference of a first-party service token: the token type is the
/// principal, so it names no id.
pub const GATEWAY_TOKEN_REF: &str = "gateway_token";

/// The three credential facts of an authenticated caller, made by one
/// constructor per kind so every place that builds an [`AuthContext`] states
/// them the same way.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Credential {
    pub kind: CredentialKind,
    /// `None` outside the ladder (human sessions, the platform service key,
    /// anonymous callers).
    pub assurance: Option<CredentialAssurance>,
    /// A stable, non-secret reference to the credential, at most
    /// [`CREDENTIAL_REF_MAX`] bytes.
    pub reference: Option<String>,
}

impl Credential {
    /// A Brutor API key, by its id. The platform service key is its own kind.
    pub fn api_key(key_id: &str, is_platform_service: bool) -> Self {
        let (kind, assurance) = if is_platform_service {
            (CredentialKind::PlatformService, None)
        } else {
            (CredentialKind::ApiKey, Some(CredentialAssurance::Key))
        };
        Self {
            kind,
            assurance,
            reference: reference(format!("api_key:{key_id}")),
        }
    }

    /// A verified workload token: the identity provider and the subject.
    /// `None` when the reference would be longer than its column: such a
    /// token is no credential (it is refused, never recorded shortened).
    pub fn workload_token(idp_server_id: &str, subject: &str) -> Option<Self> {
        Some(Self {
            kind: CredentialKind::WorkloadToken,
            assurance: Some(CredentialAssurance::Workload),
            reference: Some(reference(format!("token:{idp_server_id}:{subject}"))?),
        })
    }

    /// A first-party service token (a gateway session token of the
    /// `gateway` type, signed with the installation's secret).
    pub fn gateway_token() -> Self {
        Self {
            kind: CredentialKind::PlatformService,
            assurance: None,
            reference: Some(GATEWAY_TOKEN_REF.to_string()),
        }
    }

    /// A person's session, by its type and user when known.
    pub fn human_session(token_type: Option<&str>, user_id: Option<&str>) -> Self {
        Self {
            kind: CredentialKind::HumanSession,
            assurance: None,
            reference: user_id.and_then(|user| match token_type {
                Some(typ) => reference(format!("session:{typ}:{user}")),
                None => reference(format!("session:{user}")),
            }),
        }
    }

    pub const fn anonymous() -> Self {
        Self {
            kind: CredentialKind::Anonymous,
            assurance: None,
            reference: None,
        }
    }
}

/// `value` when it fits the column, else `None`.
fn reference(value: String) -> Option<String> {
    (value.len() <= CREDENTIAL_REF_MAX).then_some(value)
}

/// What else authentication learned about the credential, for the
/// identity judge: the identity provider of a workload credential and the
/// lifetime (`exp - iat`) of a workload token.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct CredentialDetail {
    pub idp_server_id: Option<String>,
    pub token_lifetime_s: Option<i64>,
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
    let credential = match auth_type {
        AuthType::ApiKey => Credential::api_key("test-key", false),
        AuthType::GatewayJwt => Credential::human_session(None, None),
        AuthType::IdpOauth => {
            Credential::workload_token("test-idp", "test-subject").expect("a short reference")
        }
        AuthType::Anonymous => Credential::anonymous(),
    };
    AuthContext {
        credential_kind: credential.kind,
        credential_assurance: credential.assurance,
        credential_ref: credential.reference,
        credential_detail: Default::default(),
        identity_verdict: None,
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
        assert_eq!(AuthType::Anonymous.to_string(), "anonymous");
    }

    /// The variants `pub enum AuthType` declares in this file's source.
    fn declared_auth_types() -> Vec<String> {
        let src = include_str!("auth.rs");
        let start = src
            .find("pub enum AuthType {")
            .expect("AuthType is declared here");
        let body = &src[start + "pub enum AuthType {".len()..];
        let body = &body[..body.find('}').expect("enum body ends")];
        body.lines()
            .map(str::trim)
            .filter(|l| !l.is_empty() && !l.starts_with("//") && !l.starts_with("#["))
            .map(|l| l.trim_end_matches(',').to_string())
            .collect()
    }

    /// Class-level: every way a caller can authenticate maps to a credential
    /// kind. A new `AuthType` variant that is not in `AuthType::ALL` fails
    /// here (and one missing from `credential_kinds` does not compile: the
    /// match has no catch-all, which the source check below holds).
    #[test]
    fn every_auth_type_has_a_credential_kind() {
        let declared = declared_auth_types();
        let listed: Vec<String> = AuthType::ALL.iter().map(|t| format!("{t:?}")).collect();
        assert_eq!(
            declared, listed,
            "AuthType::ALL must list every declared variant, in order"
        );
        for auth_type in AuthType::ALL {
            let kinds = auth_type.credential_kinds();
            assert!(
                !kinds.is_empty(),
                "{auth_type:?} maps to no credential kind"
            );
        }
        let src = include_str!("auth.rs");
        let at = src
            .find("pub const fn credential_kinds(&self)")
            .expect("the mapping is here");
        let body = &src[at..];
        let body = &body[..body.find("\n    }\n").expect("fn ends")];
        assert!(
            !body.contains("_ =>"),
            "the AuthType -> credential kind mapping must not have a catch-all arm"
        );
    }

    #[test]
    fn the_mapping_from_auth_type_to_credential_kind() {
        assert_eq!(
            AuthType::ApiKey.credential_kinds(),
            &[CredentialKind::ApiKey, CredentialKind::PlatformService]
        );
        assert_eq!(
            AuthType::GatewayJwt.credential_kinds(),
            &[
                CredentialKind::HumanSession,
                CredentialKind::PlatformService
            ]
        );
        assert_eq!(
            AuthType::IdpOauth.credential_kinds(),
            &[CredentialKind::WorkloadToken]
        );
        assert_eq!(
            AuthType::Anonymous.credential_kinds(),
            &[CredentialKind::Anonymous]
        );
    }

    #[test]
    fn credential_facts_per_kind() {
        let key = Credential::api_key("k-1", false);
        assert_eq!(key.kind, CredentialKind::ApiKey);
        assert_eq!(key.assurance, Some(CredentialAssurance::Key));
        assert_eq!(key.reference.as_deref(), Some("api_key:k-1"));

        // The platform service key: its own kind, outside the ladder.
        let platform = Credential::api_key("svc-1", true);
        assert_eq!(platform.kind, CredentialKind::PlatformService);
        assert_eq!(platform.assurance, None);
        assert_eq!(platform.reference.as_deref(), Some("api_key:svc-1"));

        let token = Credential::workload_token("idp-1", "spiffe://acme/agent").expect("fits");
        assert_eq!(token.kind, CredentialKind::WorkloadToken);
        assert_eq!(token.assurance, Some(CredentialAssurance::Workload));
        assert_eq!(
            token.reference.as_deref(),
            Some("token:idp-1:spiffe://acme/agent")
        );

        let human = Credential::human_session(Some("portal"), Some("42"));
        assert_eq!(human.kind, CredentialKind::HumanSession);
        assert_eq!(human.assurance, None);
        assert_eq!(human.reference.as_deref(), Some("session:portal:42"));
        assert_eq!(
            Credential::human_session(None, None).reference.as_deref(),
            None
        );

        let service = Credential::gateway_token();
        assert_eq!(service.kind, CredentialKind::PlatformService);
        assert_eq!(service.assurance, None);
        assert_eq!(service.reference.as_deref(), Some("gateway_token"));

        let anonymous = Credential::anonymous();
        assert_eq!(anonymous.kind, CredentialKind::Anonymous);
        assert_eq!(anonymous.assurance, None);
        assert_eq!(anonymous.reference, None);
    }

    /// The stored names (the evidence columns are `VARCHAR(16)`).
    #[test]
    fn credential_names_fit_their_columns() {
        for (kind, name) in [
            (CredentialKind::ApiKey, "api_key"),
            (CredentialKind::PlatformService, "platform_service"),
            (CredentialKind::WorkloadToken, "workload_token"),
            (CredentialKind::X509Svid, "x509_svid"),
            (CredentialKind::BrutorToken, "brutor_token"),
            (CredentialKind::HumanSession, "human_session"),
            (CredentialKind::Anonymous, "anonymous"),
        ] {
            assert_eq!(kind.as_str(), name);
            assert_eq!(CredentialKind::parse(name), Some(kind));
            assert!(name.len() <= 16);
        }
        for (level, name) in [
            (CredentialAssurance::Key, "key"),
            (CredentialAssurance::BoundKey, "bound_key"),
            (CredentialAssurance::Workload, "workload"),
            (CredentialAssurance::WorkloadPop, "workload_pop"),
        ] {
            assert_eq!(level.as_str(), name);
            assert_eq!(CredentialAssurance::parse(name), Some(level));
            assert!(name.len() <= 16);
        }
        // The ladder is ordered.
        assert!(CredentialAssurance::Key < CredentialAssurance::BoundKey);
        assert!(CredentialAssurance::BoundKey < CredentialAssurance::Workload);
        assert!(CredentialAssurance::Workload < CredentialAssurance::WorkloadPop);
    }

    /// A credential reference is never longer than its column: a subject
    /// that would make it longer is no credential (refused, not shortened).
    #[test]
    fn a_reference_longer_than_its_column_is_no_credential() {
        let fits = "s".repeat(CREDENTIAL_REF_MAX - "token:idp-1:".len());
        assert!(Credential::workload_token("idp-1", &fits).is_some());
        let long = format!("{fits}s");
        assert!(Credential::workload_token("idp-1", &long).is_none());
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
