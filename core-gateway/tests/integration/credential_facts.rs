// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Every way `auth::authenticate_request` accepts a caller states the
//! credential facts of the context it makes, once: the kind, the level on the
//! assurance ladder and a stable reference. The platform service key and a
//! first-party service token are the platform's own credential, outside the
//! ladder; a person's session is outside it too.

use axum::http::HeaderMap;
use brutor_gateway_core::models::auth::{AuthContext, CredentialAssurance, CredentialKind};
use brutor_gateway_core::services::auth::{AuthError, authenticate_request};
use brutor_gateway_core::services::credential_verifier::{CredentialVerifiers, RefuseUnregistered};
use sqlx::PgPool;

use crate::common;
use crate::workload_identity as wl;

const T: &str = "tenant-cred-facts";
const SECRET: &str = "credential-facts-jwt-secret-0123456789abcdef";

async fn authenticate(
    pool: &PgPool,
    headers: HeaderMap,
    require_auth: bool,
) -> Result<AuthContext, AuthError> {
    authenticate_request(
        &headers,
        T,
        SECRET,
        pool,
        &CredentialVerifiers::community(RefuseUnregistered),
        None,
        require_auth,
    )
    .await
}

fn bearer(token: &str) -> HeaderMap {
    let mut headers = HeaderMap::new();
    headers.insert("authorization", format!("Bearer {token}").parse().unwrap());
    headers
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn each_credential_states_its_kind_level_and_reference(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "rg-cred", T).await;
    common::seed_unbound_system_group(&pool, "rg-cred-unbound", T).await;

    // A shared key of a group.
    let raw = common::seed_shared_group_api_key(&pool, T, "rg-cred", "shared").await;
    let auth = authenticate(&pool, bearer(&raw), true).await.expect("key");
    assert_eq!(auth.credential_kind, CredentialKind::ApiKey);
    assert_eq!(auth.credential_assurance, Some(CredentialAssurance::Key));
    let key_id = auth.api_key_id.clone().expect("key id");
    assert_eq!(auth.credential_ref, Some(format!("api_key:{key_id}")));
    assert!(!auth.is_personal_key());

    // A person's own key: an API key, at level `key`, and personal.
    common::seed_end_user(&pool, 7101, T).await;
    let raw = common::seed_api_key(&pool, T, "rg-cred", Some(7101), "personal").await;
    let auth = authenticate(&pool, bearer(&raw), true)
        .await
        .expect("personal key");
    assert_eq!(auth.credential_kind, CredentialKind::ApiKey);
    assert_eq!(auth.credential_assurance, Some(CredentialAssurance::Key));
    assert!(auth.is_personal_key());

    // The platform service key: its own kind, outside the ladder.
    let raw = common::seed_key(&pool, T, None, true, "system-health-check").await;
    let auth = authenticate(&pool, bearer(&raw), true)
        .await
        .expect("platform key");
    assert_eq!(auth.credential_kind, CredentialKind::PlatformService);
    assert_eq!(auth.credential_assurance, None);
    let key_id = auth.api_key_id.clone().expect("key id");
    assert_eq!(auth.credential_ref, Some(format!("api_key:{key_id}")));

    // A first-party service token (gateway session type): the platform's.
    let now = common::db_now().timestamp();
    let claims = serde_json::json!({
        "typ": "gateway", "user_id": "svc-1", "username": "service", "tenant_id": T,
        "exp": now + 600, "iat": now,
    });
    let token = jsonwebtoken::encode(
        &jsonwebtoken::Header::default(),
        &claims,
        &jsonwebtoken::EncodingKey::from_secret(SECRET.as_bytes()),
    )
    .expect("sign");
    let mut headers = HeaderMap::new();
    headers.insert(
        "x-gateway-authorization",
        format!("Bearer {token}").parse().unwrap(),
    );
    let auth = authenticate(&pool, headers, true)
        .await
        .expect("service token");
    assert_eq!(auth.credential_kind, CredentialKind::PlatformService);
    assert_eq!(auth.credential_assurance, None);
    assert_eq!(auth.credential_ref.as_deref(), Some("gateway_token"));

    // A workload token: level `workload`, named by its IdP and subject.
    let jwks = wl::mock_jwks("kid-cred").await;
    let issuer = "https://idp.cred-facts.example/";
    wl::seed_idp(
        &pool,
        "idp-cred",
        T,
        issuer,
        &format!("{}/jwks", jwks.uri()),
    )
    .await;
    wl::seed_agent_with_idp(&pool, "agent-cred", T, "idp-cred", "spiffe://acme/cred").await;
    sqlx::query(
        "INSERT INTO resource_group_agent_members (tenant_id, agent_id, group_id, joined_at) \
         VALUES ($1, 'agent-cred', 'rg-cred', now())",
    )
    .bind(T)
    .execute(&pool)
    .await
    .expect("membership");
    let exp = (common::db_now().timestamp() + 600) as usize;
    let token = wl::mint_token(issuer, "spiffe://acme/cred", "kid-cred", exp);
    let auth = authenticate(&pool, bearer(&token), true)
        .await
        .expect("workload");
    assert_eq!(auth.credential_kind, CredentialKind::WorkloadToken);
    assert_eq!(
        auth.credential_assurance,
        Some(CredentialAssurance::Workload)
    );
    assert_eq!(
        auth.credential_ref.as_deref(),
        Some("token:idp-cred:spiffe://acme/cred")
    );

    // No credential, where one is not required.
    let auth = authenticate(&pool, HeaderMap::new(), false)
        .await
        .expect("anonymous");
    assert_eq!(auth.credential_kind, CredentialKind::Anonymous);
    assert_eq!(auth.credential_assurance, None);
    assert_eq!(auth.credential_ref, None);
}

/// Whatever authenticated, the kind is one its `AuthType` can carry.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_kind_is_one_the_auth_type_allows(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "rg-cred", T).await;
    let raw = common::seed_shared_group_api_key(&pool, T, "rg-cred", "shared").await;
    for auth in [
        authenticate(&pool, bearer(&raw), true).await.expect("key"),
        authenticate(&pool, HeaderMap::new(), false)
            .await
            .expect("anonymous"),
    ] {
        assert!(
            auth.auth_type
                .credential_kinds()
                .contains(&auth.credential_kind),
            "{:?} carried {:?}",
            auth.auth_type,
            auth.credential_kind
        );
    }
}
