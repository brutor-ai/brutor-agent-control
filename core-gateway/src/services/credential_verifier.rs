// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # The credential-verifier hook
//!
//! One `JWT_SECRET` signs every session token of the platform: an
//! administrator's (minted by the control plane), an end user's portal
//! session (minted by the portal's sign-in, an Enterprise feature) and
//! first-party service tokens. Each says which it is in its `typ` claim.
//! Before this hook `auth::authenticate_request` accepted any `typ` on a
//! fixed list, and a token with none, and ran the administrator's and the
//! end user's session checks on every one. It now asks a **registry**:
//! `typ` → [`CredentialVerifier`]. What makes a portal session valid — its
//! revocation list, the user's session cutoff and their caches — is the
//! portal's business and lives with the portal (`services::portal_session`).
//!
//! ## The rule
//!
//! This is a control, so what it does not know it refuses:
//!
//! * a token whose `typ` nobody registered is not a credential;
//! * a token with **no** `typ` is not a credential (every issuer mints one;
//!   before the registry such a token was accepted, which also admitted the
//!   service tokens the control plane and the MCP registry sign for each
//!   other with the same secret; see
//!   `tests/integration/credential_surface_matrix.rs`);
//! * a verifier that cannot decide — its store cannot be read — returns an
//!   error, and an error is a refusal. [`CredentialVerifiers::verify`] has no
//!   path from `Err` to an accepted credential.
//!
//! ## Shape
//!
//! * [`CredentialVerifier::verify`] is given the claims of a token whose
//!   signature and expiry have been checked, whose `typ` is the one the
//!   verifier is registered for, and whose tenant agrees with the request's.
//!   It answers whether that session stands.
//! * [`CredentialVerifiers`] is the registry, held in `AppState` and handed
//!   to `auth::authenticate_request` by every surface. Registration happens
//!   while the gateway is assembled, before the state is shared; a type can
//!   be registered once.
//! * [`VerifiedCredential`] is what a verified token is: today its type,
//!   which `AuthContext::session_token_type` carries so that a route can ask for one
//!   kind of session (`auth::require_token_type`). Further attributes of a
//!   credential (a class, capabilities) belong here, not in a second
//!   authentication path.
//!
//! ## What stays with the caller
//!
//! Everything that is true of every session token: the signature, the
//! refusal of pre-session tokens (the `type` claim), expiry, and the match
//! between the token's tenant and the request's (`services::auth`). An API
//! key, an agent's workload-identity token and anonymous access never reach
//! the registry.
//!
//! ## The default
//!
//! [`RefuseUnregistered`] answers for every type that has no verifier.
//! `AppState::new` builds [`CredentialVerifiers::community`]: an
//! administrator's session ([`TOKEN_TYPE_ADMIN`]) and a service token
//! ([`TOKEN_TYPE_GATEWAY`]). A portal session ([`TOKEN_TYPE_PORTAL`]) is
//! then refused on every surface, like any other token the installation
//! does not issue. That is the Community behaviour.

use std::sync::Arc;

use async_trait::async_trait;
use sqlx::PgPool;

use crate::models::auth::GatewayJwtClaims;
use crate::services::auth::{self, AuthError};

/// An administrator's session, minted by the control plane.
pub const TOKEN_TYPE_ADMIN: &str = "admin";
/// A first-party service token.
pub const TOKEN_TYPE_GATEWAY: &str = "gateway";
/// An end user's portal session. Community has no verifier for it; the
/// portal registers one. Named here because Community routes that act for
/// an end user (`routes::request::extract_portal_user`) ask for this type.
pub const TOKEN_TYPE_PORTAL: &str = "portal";

/// A session token that verified.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct VerifiedCredential {
    /// The type it was registered and verified as.
    pub token_type: &'static str,
}

/// Decides whether a session token of one type stands.
#[async_trait]
pub trait CredentialVerifier: Send + Sync {
    /// `Ok` only when the session is known to stand. An answer that could
    /// not be read is an `Err` ([`AuthError::Unavailable`] or
    /// [`AuthError::Internal`]), never `Ok`.
    async fn verify(&self, db: &PgPool, claims: &GatewayJwtClaims) -> Result<(), AuthError>;
}

/// The answer for a token type nobody registered: it is not a credential.
/// The Community default.
#[derive(Debug, Clone, Copy, Default)]
pub struct RefuseUnregistered;

impl RefuseUnregistered {
    fn refusal(claims: &GatewayJwtClaims) -> AuthError {
        match claims.typ.as_deref() {
            Some(typ) => AuthError::InvalidJwt(format!(
                "token type '{typ}' is not accepted as a gateway credential"
            )),
            None => AuthError::InvalidJwt(
                "a token with no type is not accepted as a gateway credential".into(),
            ),
        }
    }
}

#[async_trait]
impl CredentialVerifier for RefuseUnregistered {
    async fn verify(&self, _db: &PgPool, claims: &GatewayJwtClaims) -> Result<(), AuthError> {
        Err(Self::refusal(claims))
    }
}

/// The kind of administrator the Community Edition has: a tenant's.
pub const ADMIN_TYPE_TENANT: &str = "tenant";

/// An administrator's session: what is true of every kind. Which kind it
/// is, and whether that kind's session stands, is decided after this by the
/// verifier registered for the token's `admin_type`
/// ([`CredentialVerifiers::verify`]).
///
/// A token of type `admin` with no `admin_type` used to pass with no
/// session check at all, so it could not be revoked. Every issuer mints the
/// claim; a token without it is refused.
pub struct AdminSessionVerifier;

#[async_trait]
impl CredentialVerifier for AdminSessionVerifier {
    async fn verify(&self, _db: &PgPool, claims: &GatewayJwtClaims) -> Result<(), AuthError> {
        refuse_end_user_claims(claims, TOKEN_TYPE_ADMIN)?;
        if claims.admin_type.is_none() {
            return Err(AuthError::InvalidJwt(
                "admin token does not say which administrator it is (admin_type)".into(),
            ));
        }
        Ok(())
    }
}

/// A tenant administrator's session: live in the control plane's
/// `tenant_admin_sessions`, of an administrator who owes no password change.
pub struct TenantAdminSessionVerifier;

#[async_trait]
impl CredentialVerifier for TenantAdminSessionVerifier {
    async fn verify(&self, db: &PgPool, claims: &GatewayJwtClaims) -> Result<(), AuthError> {
        auth::verify_tenant_admin_session(db, claims).await
    }
}

/// A first-party service token: nothing beyond its signature, expiry and
/// tenant. It names no administrator and no end user; one that claims to
/// would escape that principal's session check, so it is refused.
pub struct GatewayTokenVerifier;

#[async_trait]
impl CredentialVerifier for GatewayTokenVerifier {
    async fn verify(&self, _db: &PgPool, claims: &GatewayJwtClaims) -> Result<(), AuthError> {
        refuse_end_user_claims(claims, TOKEN_TYPE_GATEWAY)?;
        if claims.admin_type.is_some() {
            return Err(AuthError::InvalidJwt(
                "a gateway token does not carry an administrator's identity".into(),
            ));
        }
        Ok(())
    }
}

/// Before the registry, a token that said `user_type: end_user` was held to
/// the portal's revocation list whatever its `typ`. A token of another type
/// that says so is now refused outright: no issuer mints one.
fn refuse_end_user_claims(claims: &GatewayJwtClaims, token_type: &str) -> Result<(), AuthError> {
    if claims.user_type.as_deref() == Some("end_user") {
        return Err(AuthError::InvalidJwt(format!(
            "a '{token_type}' token does not carry an end user's identity"
        )));
    }
    Ok(())
}

/// A token type was registered twice.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TokenTypeTaken(pub &'static str);

impl std::fmt::Display for TokenTypeTaken {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "a credential verifier for token type '{}' is already registered",
            self.0
        )
    }
}

impl std::error::Error for TokenTypeTaken {}

/// The token-type registry of an installation.
pub struct CredentialVerifiers {
    /// A handful of entries, looked up once per session-token request: a
    /// scan is cheaper than a hash.
    registered: Vec<(&'static str, Arc<dyn CredentialVerifier>)>,
    /// The kinds of administrator (`admin_type`) whose sessions this
    /// installation accepts, each with the verifier of its session.
    /// The Community Edition has
    /// tenant administrators; a kind nobody registered is refused.
    admin_types: Vec<(&'static str, Arc<dyn CredentialVerifier>)>,
    unregistered: RefuseUnregistered,
}

impl CredentialVerifiers {
    /// The Community registry: administrators' sessions and service tokens.
    /// `unregistered` answers for every other type.
    pub fn community(unregistered: RefuseUnregistered) -> Self {
        Self {
            registered: vec![
                (TOKEN_TYPE_ADMIN, Arc::new(AdminSessionVerifier)),
                (TOKEN_TYPE_GATEWAY, Arc::new(GatewayTokenVerifier)),
            ],
            admin_types: vec![(ADMIN_TYPE_TENANT, Arc::new(TenantAdminSessionVerifier))],
            unregistered,
        }
    }

    /// Register the verifier of a kind of administrator: the session check
    /// of admin tokens whose `admin_type` is `admin_type`. Refused when the
    /// kind already has one, or is empty.
    pub fn register_admin_type(
        &mut self,
        admin_type: &'static str,
        verifier: Arc<dyn CredentialVerifier>,
    ) -> Result<(), TokenTypeTaken> {
        if admin_type.is_empty() || self.admin_types.iter().any(|(name, _)| *name == admin_type) {
            return Err(TokenTypeTaken(admin_type));
        }
        self.admin_types.push((admin_type, verifier));
        Ok(())
    }

    /// The kinds of administrator registered, in registration order.
    pub fn admin_types(&self) -> Vec<&'static str> {
        self.admin_types.iter().map(|(name, _)| *name).collect()
    }

    /// Register the verifier of a token type. Refused when the type already
    /// has one: two answers for one credential is an assembly nobody
    /// described.
    pub fn register(
        &mut self,
        token_type: &'static str,
        verifier: Arc<dyn CredentialVerifier>,
    ) -> Result<(), TokenTypeTaken> {
        if token_type.is_empty() || self.lookup(Some(token_type)).is_some() {
            return Err(TokenTypeTaken(token_type));
        }
        self.registered.push((token_type, verifier));
        Ok(())
    }

    /// The registered types, in registration order.
    pub fn token_types(&self) -> Vec<&'static str> {
        self.registered.iter().map(|(name, _)| *name).collect()
    }

    fn lookup(&self, typ: Option<&str>) -> Option<&(&'static str, Arc<dyn CredentialVerifier>)> {
        let typ = typ?;
        self.registered.iter().find(|(name, _)| *name == typ)
    }

    /// The registered type `claims` are of, or the refusal of a token that
    /// is of none. Synchronous: what `auth::decode_gateway_jwt` asks, so
    /// that a token of no registered type is no more a gateway token than
    /// one with a bad signature.
    pub fn token_type_of(&self, claims: &GatewayJwtClaims) -> Result<&'static str, AuthError> {
        match self.lookup(claims.typ.as_deref()) {
            Some((name, _)) => Ok(name),
            None => Err(RefuseUnregistered::refusal(claims)),
        }
    }

    /// Verify the session `claims` describe with the verifier of its type.
    /// Any error is the refusal.
    pub async fn verify(
        &self,
        db: &PgPool,
        claims: &GatewayJwtClaims,
    ) -> Result<VerifiedCredential, AuthError> {
        let Some((token_type, verifier)) = self.lookup(claims.typ.as_deref()) else {
            self.unregistered.verify(db, claims).await?;
            // `RefuseUnregistered` has no `Ok`; if it ever grew one, an
            // unregistered type would still not be a credential.
            return Err(RefuseUnregistered::refusal(claims));
        };
        verifier.verify(db, claims).await?;
        if *token_type == TOKEN_TYPE_ADMIN {
            // An administrator's session stands only when the verifier of
            // its kind says so. A kind nobody registered is not a
            // credential of this installation: a Community gateway has no
            // system administrators, and an unknown kind was always
            // refused. Never `Ok` without a kind's verifier having answered.
            let admin_type = claims.admin_type.as_deref().unwrap_or_default();
            let Some((_, kind)) = self
                .admin_types
                .iter()
                .find(|(name, _)| *name == admin_type)
            else {
                return Err(AuthError::InvalidJwt(format!(
                    "unknown admin_type '{admin_type}' on admin token"
                )));
            };
            kind.verify(db, claims).await?;
        }
        Ok(VerifiedCredential { token_type })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pool() -> PgPool {
        // Never connected: the verifiers under test here read nothing.
        sqlx::postgres::PgPoolOptions::new()
            .connect_lazy("postgresql://nobody:nothing@127.0.0.1:1/none")
            .expect("lazy pool")
    }

    fn claims(typ: Option<&str>) -> GatewayJwtClaims {
        GatewayJwtClaims {
            typ: typ.map(String::from),
            exp: Some(u64::MAX / 2),
            ..Default::default()
        }
    }

    struct Accepts;
    #[async_trait]
    impl CredentialVerifier for Accepts {
        async fn verify(&self, _: &PgPool, _: &GatewayJwtClaims) -> Result<(), AuthError> {
            Ok(())
        }
    }

    struct CannotDecide;
    #[async_trait]
    impl CredentialVerifier for CannotDecide {
        async fn verify(&self, _: &PgPool, _: &GatewayJwtClaims) -> Result<(), AuthError> {
            Err(AuthError::Unavailable("the session store is down".into()))
        }
    }

    fn admin_claims(admin_type: Option<&str>) -> GatewayJwtClaims {
        GatewayJwtClaims {
            typ: Some("admin".into()),
            admin_type: admin_type.map(String::from),
            jti: Some("a-session".into()),
            exp: Some(u64::MAX / 2),
            ..Default::default()
        }
    }

    /// The Community registry knows tenant administrators only.
    /// A system administrator's token is refused before anything is read
    /// (the pool here cannot connect: reaching it would be another error).
    #[tokio::test]
    async fn community_refuses_a_system_administrator_and_every_unregistered_kind() {
        let registry = CredentialVerifiers::community(RefuseUnregistered);
        assert_eq!(registry.admin_types(), vec!["tenant"]);
        for kind in ["system", "root", "", "Tenant", "tenant "] {
            let refused = registry.verify(&pool(), &admin_claims(Some(kind))).await;
            match refused {
                Err(AuthError::InvalidJwt(why)) => {
                    assert!(why.contains("unknown admin_type"), "{kind:?}: {why}")
                }
                other => panic!("{kind:?}: {other:?}"),
            }
        }
        // No kind at all is refused by the type's own verifier.
        assert!(matches!(
            registry.verify(&pool(), &admin_claims(None)).await,
            Err(AuthError::InvalidJwt(why)) if why.contains("admin_type")
        ));
        // A tenant administrator's token reaches its session check, which
        // cannot read here and so refuses: never `Ok` on an error.
        assert!(matches!(
            registry
                .verify(&pool(), &admin_claims(Some("tenant")))
                .await,
            Err(AuthError::Internal(_))
        ));
    }

    /// A registered kind is asked, its refusal is the refusal, and a kind
    /// registers once. Registering a kind does not make another one stand.
    #[tokio::test]
    async fn a_registered_kind_of_administrator_is_asked_and_registers_once() {
        let mut registry = CredentialVerifiers::community(RefuseUnregistered);
        registry
            .register_admin_type("system", Arc::new(Accepts))
            .expect("a new kind registers");
        assert_eq!(registry.admin_types(), vec!["tenant", "system"]);
        assert_eq!(
            registry
                .verify(&pool(), &admin_claims(Some("system")))
                .await
                .expect("the kind's verifier accepted"),
            VerifiedCredential {
                token_type: TOKEN_TYPE_ADMIN
            }
        );
        assert!(
            registry
                .verify(&pool(), &admin_claims(Some("root")))
                .await
                .is_err()
        );
        for taken in ["system", "tenant", ""] {
            assert_eq!(
                registry.register_admin_type(taken, Arc::new(Accepts)),
                Err(TokenTypeTaken(taken)),
                "{taken:?}"
            );
        }
        // A kind whose session cannot be read is refused with that error.
        let mut registry = CredentialVerifiers::community(RefuseUnregistered);
        registry
            .register_admin_type("system", Arc::new(CannotDecide))
            .expect("registers");
        assert!(matches!(
            registry
                .verify(&pool(), &admin_claims(Some("system")))
                .await,
            Err(AuthError::Unavailable(_))
        ));
        // And an end user's claims on an admin token are refused before any
        // kind is asked.
        let mut end_user = admin_claims(Some("system"));
        end_user.user_type = Some("end_user".into());
        assert!(registry.verify(&pool(), &end_user).await.is_err());
    }

    #[test]
    fn community_registers_administrators_and_service_tokens_and_nothing_else() {
        let registry = CredentialVerifiers::community(RefuseUnregistered);
        assert_eq!(registry.token_types(), vec!["admin", "gateway"]);
        assert!(registry.token_type_of(&claims(Some("admin"))).is_ok());
        assert!(registry.token_type_of(&claims(Some("gateway"))).is_ok());
        assert!(matches!(
            registry.token_type_of(&claims(Some(TOKEN_TYPE_PORTAL))),
            Err(AuthError::InvalidJwt(_))
        ));
    }

    #[tokio::test]
    async fn a_type_nobody_registered_is_refused() {
        let registry = CredentialVerifiers::community(RefuseUnregistered);
        for typ in [
            "portal", "registry", "service", "refresh", "Admin", "admin ", "",
        ] {
            assert!(
                matches!(
                    registry.token_type_of(&claims(Some(typ))),
                    Err(AuthError::InvalidJwt(_))
                ),
                "typ={typ:?}"
            );
            assert!(
                matches!(
                    registry.verify(&pool(), &claims(Some(typ))).await,
                    Err(AuthError::InvalidJwt(_))
                ),
                "typ={typ:?}"
            );
        }
    }

    #[tokio::test]
    async fn a_token_with_no_type_is_refused_not_given_a_type() {
        let mut registry = CredentialVerifiers::community(RefuseUnregistered);
        registry
            .register(TOKEN_TYPE_PORTAL, Arc::new(Accepts))
            .expect("register");
        // Claims that would satisfy every registered verifier if the missing
        // type were read as theirs.
        let untyped = claims(None);
        assert!(matches!(
            registry.token_type_of(&untyped),
            Err(AuthError::InvalidJwt(_))
        ));
        assert!(matches!(
            registry.verify(&pool(), &untyped).await,
            Err(AuthError::InvalidJwt(_))
        ));
    }

    #[tokio::test]
    async fn a_registered_type_is_verified_by_its_own_verifier() {
        let mut registry = CredentialVerifiers::community(RefuseUnregistered);
        registry
            .register("portal", Arc::new(Accepts))
            .expect("register");
        assert_eq!(registry.token_types(), vec!["admin", "gateway", "portal"]);
        assert_eq!(
            registry
                .verify(&pool(), &claims(Some("portal")))
                .await
                .expect("verified"),
            VerifiedCredential {
                token_type: "portal"
            }
        );
        assert_eq!(
            registry
                .verify(&pool(), &claims(Some("gateway")))
                .await
                .expect("verified")
                .token_type,
            "gateway"
        );
    }

    #[tokio::test]
    async fn a_verifier_that_cannot_decide_refuses() {
        let mut registry = CredentialVerifiers::community(RefuseUnregistered);
        registry
            .register("portal", Arc::new(CannotDecide))
            .expect("register");
        match registry.verify(&pool(), &claims(Some("portal"))).await {
            Err(AuthError::Unavailable(_)) => {}
            other => panic!("an error from the verifier must be the refusal, got {other:?}"),
        }
    }

    #[test]
    fn a_type_is_registered_once() {
        let mut registry = CredentialVerifiers::community(RefuseUnregistered);
        assert_eq!(
            registry.register("admin", Arc::new(Accepts)).unwrap_err(),
            TokenTypeTaken("admin")
        );
        registry
            .register("portal", Arc::new(Accepts))
            .expect("first");
        assert_eq!(
            registry.register("portal", Arc::new(Accepts)).unwrap_err(),
            TokenTypeTaken("portal")
        );
        assert!(registry.register("", Arc::new(Accepts)).is_err());
        assert_eq!(registry.token_types(), vec!["admin", "gateway", "portal"]);
    }

    #[tokio::test]
    async fn a_service_token_that_names_a_person_is_refused() {
        let registry = CredentialVerifiers::community(RefuseUnregistered);
        let plain = claims(Some("gateway"));
        assert!(registry.verify(&pool(), &plain).await.is_ok());
        let as_admin = GatewayJwtClaims {
            admin_type: Some("system".into()),
            ..claims(Some("gateway"))
        };
        assert!(registry.verify(&pool(), &as_admin).await.is_err());
        let as_end_user = GatewayJwtClaims {
            user_type: Some("end_user".into()),
            ..claims(Some("gateway"))
        };
        assert!(registry.verify(&pool(), &as_end_user).await.is_err());
    }

    #[tokio::test]
    async fn an_admin_token_must_say_which_administrator_it_is() {
        // `typ: admin` with no `admin_type` used to pass with no
        // session check at all. Refused before any lookup.
        let registry = CredentialVerifiers::community(RefuseUnregistered);
        let no_admin_type = GatewayJwtClaims {
            jti: Some("a-session".into()),
            ..claims(Some("admin"))
        };
        assert!(matches!(
            registry.verify(&pool(), &no_admin_type).await,
            Err(AuthError::InvalidJwt(_))
        ));
        let end_user = GatewayJwtClaims {
            admin_type: Some("tenant".into()),
            user_type: Some("end_user".into()),
            jti: Some("a-session".into()),
            ..claims(Some("admin"))
        };
        assert!(matches!(
            registry.verify(&pool(), &end_user).await,
            Err(AuthError::InvalidJwt(_))
        ));
    }
}
