// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

// Workload identity: an agent authenticates with its own external-IdP (OIDC)
// token. The gateway fetches the IdP's JWKS (served here by wiremock), verifies
// RS256 signature + issuer + audience + exp, and resolves the agent by
// (idp_server_id, sub). Covers the happy path, an unknown subject, a tampered
// token, and audience binding and algorithm pinning.

use jsonwebtoken::{EncodingKey, Header, encode};
use serde::Serialize;
use sqlx::PgPool;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::services::workload_identity::resolve_agent_from_token;

use crate::common;

// A fixed 2048-bit RSA test key (generated for tests only — not a real secret).
pub(crate) const TEST_RSA_PEM: &str = "-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCdMxLGj/N+bxFr
mKtDDvHIbqwg061AF9LLn0pyq9GYm6eljkgVhoSqZtdAVlhRLU8imov7yfe2lb8n
u2JOyKKc9yE0c0LaKGFk7Y9rkGHYhi4YanLC0LD4toDjTWrKDFtA1moGikn4fIto
wizAItXylckjXlrKMAOtr2jzO+2w+p5tDFddbhn+EA9GvYyIG5naExYoUx6qwrcW
FjF4Bn5+UKF4CENp/H61wOk+UDw41J0tgVtrwGaZemRGjQC2DX4nW2ONEtY35IEa
zz9rlgTuhGFMm6y+2aQd0GFYzpKWkYrRL8u5+7C7lmbiOwY4UAYRjJvYpXa7NwzH
Hn9smXKNAgMBAAECggEABZ/zNwmlk8uTmv7+ecrKGfrjy9TnKRjxIPoVJt9BA63m
JMVfjO2gR5ygbRvp0xXE9B8FjT/fzjezfE93KAyDzQwGsyLArJ+vRHY/oICtD5tV
gOgmLRRckLmvCPMXNB2XEk3Zw1wlY2/JaTkxcrqMXz6+0LYql3t6VjelkLZ/Pcrb
gWbpsmEBZUQZMulkbR9f+NDj1/JXUDMdHKaf+nQcSVpYV/4K/mt5L6AXA0THxa9r
qjV7QGUSF1wX+NFyRAvbFQFz22GbzsMgXS/T3dQYt735xuGLob+tCi+BzCKC8ItK
AywYu7G45Gh5PTA3qq3iF47258sYFYpgiHR1W0TiwQKBgQDavT3ycyWpdi2KqcCC
4Dco6Z/xz2le3LKTEoEJX4rwhLRTMV0qiFMeuxmWICSftvGSr/VYyGLRUHWAmhbR
BNeKp7ZxYUy5kVWYhKCBTdKVxtcaHpubLB00BIka8WU6PzesGcxEKv62i/aOGJAl
PTePAXRC2+gTjWI5AOZCWfqq/QKBgQC3+jajp7kEULNqaWV5m0Ah4WZMGjrxMv9L
cxKFwOt7Cc7GUxJDTCi1VosW7UkVFnGvl/MfzLSJjqX8WvnxhYe0TiH1dtS4SMos
WJc2BVBPn3fWiJMr9bfgOte06+iEqFQ93GKOrWV/I5NwrY+2MrYhGV5GIKgA96Iq
QQj9PSti0QKBgC6p4nY/hFBTisGjFxOUe4/5zwpKAiOfl2TytJ5oSH6pAMMiq8pw
DLNonLu/HbEI2R4+9Ws8TIB01qxZsvZiPw0wXCZx6GlKDOWdnZQYIw2avrauwulm
jtBTC2C5EKxNYjYPxtjgBziwQSftK4sWNS0H1WKSAT9ykPqyK20KPwSNAoGBAJ7o
qY88DMwUZJQySyr5lTBufyyWr3iswuRJlaMgGoWg2BBnqmukebj/8o2wESL8yt+P
kLlT2FDEfMx3TFJ4MtnjbuOVa3FttesN7jYD6vWLWThdqTkcOWGdkbm81o7W0kTI
efyg+v0RHoYB9BK8xKHcRpiuFk8XleOHXxtVXYcxAoGAa2gx4T0q+uGVkht1h5jc
pMizGEBkPSRGjFSMRdDyy1mrzZBFbV7QW3ug0lxojRvLfxxSWVojxMIgBxGqLSPo
HGgjAuD9LBnujhWTdnNYGt+St6NFvGSlmdv4iyBoZf/EbSPC8fWy1qjVpmME/YZY
gSn/Sm1snlXNWmevs9ek8sk=
-----END PRIVATE KEY-----";

const TEST_JWK_N: &str = "nTMSxo_zfm8Ra5irQw7xyG6sINOtQBfSy59KcqvRmJunpY5IFYaEqmbXQFZYUS1PIpqL-8n3tpW_J7tiTsiinPchNHNC2ihhZO2Pa5Bh2IYuGGpywtCw-LaA401qygxbQNZqBopJ-HyLaMIswCLV8pXJI15ayjADra9o8zvtsPqebQxXXW4Z_hAPRr2MiBuZ2hMWKFMeqsK3FhYxeAZ-flCheAhDafx-tcDpPlA8ONSdLYFba8BmmXpkRo0Atg1-J1tjjRLWN-SBGs8_a5YE7oRhTJusvtmkHdBhWM6SlpGK0S_Lufuwu5Zm4jsGOFAGEYyb2KV2uzcMxx5_bJlyjQ";

/// The audience `seed_idp` configures and `mint_token` puts in `aud`.
pub(crate) const WL_AUDIENCE: &str = "brutor-gateway";

#[derive(Serialize)]
struct Claims {
    iss: String,
    sub: String,
    exp: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    aud: Option<serde_json::Value>,
}

fn mint(
    alg: jsonwebtoken::Algorithm,
    key: &EncodingKey,
    issuer: &str,
    sub: &str,
    kid: &str,
    exp: usize,
    aud: Option<serde_json::Value>,
) -> String {
    let mut header = Header::new(alg);
    header.kid = Some(kid.to_string());
    let claims = Claims {
        iss: issuer.to_string(),
        sub: sub.to_string(),
        exp,
        aud,
    };
    encode(&header, &claims, key).expect("mint token")
}

fn rsa_key() -> EncodingKey {
    EncodingKey::from_rsa_pem(TEST_RSA_PEM.as_bytes()).expect("test pem")
}

pub(crate) fn mint_token(issuer: &str, sub: &str, kid: &str, exp: usize) -> String {
    mint(
        jsonwebtoken::Algorithm::RS256,
        &rsa_key(),
        issuer,
        sub,
        kid,
        exp,
        Some(serde_json::json!(WL_AUDIENCE)),
    )
}

pub(crate) async fn mock_jwks(kid: &str) -> MockServer {
    serve_jwks(serde_json::json!({
        "keys": [{
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": kid,
            "n": TEST_JWK_N,
            "e": "AQAB"
        }]
    }))
    .await
}

async fn serve_jwks(jwks: serde_json::Value) -> MockServer {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/jwks"))
        .respond_with(ResponseTemplate::new(200).set_body_json(jwks))
        .mount(&server)
        .await;
    server
}

/// An enabled IdP that accepts workload tokens for [`WL_AUDIENCE`].
pub(crate) async fn seed_idp(pool: &PgPool, id: &str, tenant: &str, issuer: &str, jwks_uri: &str) {
    seed_idp_with_audiences(
        pool,
        id,
        tenant,
        issuer,
        jwks_uri,
        Some(serde_json::json!([WL_AUDIENCE])),
    )
    .await;
}

async fn seed_idp_with_audiences(
    pool: &PgPool,
    id: &str,
    tenant: &str,
    issuer: &str,
    jwks_uri: &str,
    audiences: Option<serde_json::Value>,
) {
    sqlx::query(
        "INSERT INTO idp_servers \
         (id, tenant_id, name, issuer, type, jwks_uri, supports_pkce, supports_refresh_tokens, \
          supports_client_credentials, token_endpoint_auth_methods, scopes_supported, \
          response_types_supported, grant_types_supported, discovery_cache_ttl, enabled, verified, \
          workload_audiences, created_at, updated_at, protocol) \
         VALUES ($1, $2, $3, $4, 'oidc', $5, false, false, false, '[]'::json, '[]'::json, \
                 '[]'::json, '[]'::json, 3600, true, true, $6, now(), now(), 'oidc')",
    )
    .bind(id)
    .bind(tenant)
    .bind(id)
    .bind(issuer)
    .bind(jwks_uri)
    .bind(audiences.map(sqlx::types::Json))
    .execute(pool)
    .await
    .expect("seed_idp");
}

pub(crate) async fn seed_agent_with_idp(
    pool: &PgPool,
    agent_id: &str,
    tenant: &str,
    idp_id: &str,
    sub: &str,
) {
    sqlx::query(
        "INSERT INTO agent_identities \
         (id, tenant_id, name, source, idp_server_id, idp_subject, enabled, status, default_deny) \
         VALUES ($1, $2, $3, 'idp', $4, $5, true, 'active', true)",
    )
    .bind(agent_id)
    .bind(tenant)
    .bind(agent_id)
    .bind(idp_id)
    .bind(sub)
    .execute(pool)
    .await
    .expect("seed_agent_with_idp");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn workload_token_resolves_to_agent(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-wl").await;
    let jwks = mock_jwks("kid-1").await;
    let issuer = "https://idp.wl.example/";
    // Unique idp id per test → no cross-test JWKS-cache leak.
    seed_idp(
        &pool,
        "idp-wl-1",
        "tenant-wl",
        issuer,
        &format!("{}/jwks", jwks.uri()),
    )
    .await;
    seed_agent_with_idp(
        &pool,
        "agent-wl-1",
        "tenant-wl",
        "idp-wl-1",
        "spiffe://acme/agent/1",
    )
    .await;

    let exp = (crate::common::db_now().timestamp() + 3600) as usize;
    let token = mint_token(issuer, "spiffe://acme/agent/1", "kid-1", exp);

    let resolved = resolve_agent_from_token(&pool, "tenant-wl", &token)
        .await
        .expect("no db error")
        .expect("token must resolve to an agent");
    assert_eq!(resolved.agent_id, "agent-wl-1");
    assert_eq!(resolved.idp_server_id, "idp-wl-1");
    assert_eq!(resolved.idp_subject, "spiffe://acme/agent/1");
}

/// An identity provider stored with the SAML protocol verifies no workload
/// token, even when its issuer is the token's and it has a key set.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_saml_identity_provider_verifies_no_workload_token(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-wl").await;
    let jwks = mock_jwks("kid-saml").await;
    let issuer = "https://idp.wl-saml.example/";
    seed_idp(
        &pool,
        "idp-wl-saml",
        "tenant-wl",
        issuer,
        &format!("{}/jwks", jwks.uri()),
    )
    .await;
    seed_agent_with_idp(
        &pool,
        "agent-wl-saml",
        "tenant-wl",
        "idp-wl-saml",
        "spiffe://acme/agent/saml",
    )
    .await;
    sqlx::query("UPDATE idp_servers SET protocol = 'saml' WHERE id = 'idp-wl-saml'")
        .execute(&pool)
        .await
        .expect("make it SAML");

    let exp = (crate::common::db_now().timestamp() + 3600) as usize;
    let token = mint_token(issuer, "spiffe://acme/agent/saml", "kid-saml", exp);
    let resolved = resolve_agent_from_token(&pool, "tenant-wl", &token)
        .await
        .expect("no db error");
    assert!(
        resolved.is_none(),
        "a SAML identity provider resolved a workload token"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn workload_token_unknown_subject_does_not_resolve(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-wl").await;
    let jwks = mock_jwks("kid-2").await;
    let issuer = "https://idp.wl2.example/";
    seed_idp(
        &pool,
        "idp-wl-2",
        "tenant-wl",
        issuer,
        &format!("{}/jwks", jwks.uri()),
    )
    .await;
    seed_agent_with_idp(
        &pool,
        "agent-wl-2",
        "tenant-wl",
        "idp-wl-2",
        "spiffe://acme/agent/2",
    )
    .await;

    let exp = (crate::common::db_now().timestamp() + 3600) as usize;
    // Valid signature, but a subject no agent maps to.
    let token = mint_token(issuer, "spiffe://acme/agent/UNKNOWN", "kid-2", exp);

    let resolved = resolve_agent_from_token(&pool, "tenant-wl", &token)
        .await
        .expect("no db error");
    assert!(
        resolved.is_none(),
        "unknown subject must not resolve to an agent"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn tampered_workload_token_is_rejected(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-wl").await;
    let jwks = mock_jwks("kid-3").await;
    let issuer = "https://idp.wl3.example/";
    seed_idp(
        &pool,
        "idp-wl-3",
        "tenant-wl",
        issuer,
        &format!("{}/jwks", jwks.uri()),
    )
    .await;
    seed_agent_with_idp(
        &pool,
        "agent-wl-3",
        "tenant-wl",
        "idp-wl-3",
        "spiffe://acme/agent/3",
    )
    .await;

    let exp = (crate::common::db_now().timestamp() + 3600) as usize;
    let token = mint_token(issuer, "spiffe://acme/agent/3", "kid-3", exp);
    // Flip the last char of the signature segment to break verification.
    let mut parts: Vec<&str> = token.split('.').collect();
    let sig = parts[2].to_string();
    let tampered_sig = if sig.ends_with('A') {
        format!("{}B", &sig[..sig.len() - 1])
    } else {
        format!("{}A", &sig[..sig.len() - 1])
    };
    parts[2] = &tampered_sig;
    let tampered = parts.join(".");

    let resolved = resolve_agent_from_token(&pool, "tenant-wl", &tampered)
        .await
        .expect("no db error");
    assert!(resolved.is_none(), "tampered signature must not verify");
}

/// Regression: the workload branch selected a column that does
/// not exist (`resource_group_id`; the column is `group_id`) and swallowed
/// the error, so every workload-token agent fell back to whatever group the
/// caller named in `x-resource-group`. The agent must act in its OWN group.
///
/// The header selects among the agent's own groups.
/// With none named the agent acts in its oldest membership; naming a group
/// it is a member of selects it; naming any other group is a refusal (403).
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_workload_agent_acts_in_its_own_group_not_the_header_group(pool: PgPool) {
    let t = "tenant-wl-grp";
    common::seed_tenant(&pool, t).await;
    common::seed_resource_group(&pool, "rg-own", t).await;
    common::seed_resource_group(&pool, "rg-second", t).await;
    common::seed_resource_group(&pool, "rg-other", t).await;
    let jwks = mock_jwks("kid-grp").await;
    let issuer = "https://idp.grp.example/";
    seed_idp(
        &pool,
        "idp-wl-grp",
        t,
        issuer,
        &format!("{}/jwks", jwks.uri()),
    )
    .await;
    seed_agent_with_idp(&pool, "agent-wl-grp", t, "idp-wl-grp", "spiffe://acme/grp").await;
    for group in ["rg-own", "rg-second"] {
        sqlx::query(
            "INSERT INTO resource_group_agent_members (tenant_id, agent_id, group_id, joined_at) \
             VALUES ($1, 'agent-wl-grp', $2, clock_timestamp())",
        )
        .bind(t)
        .bind(group)
        .execute(&pool)
        .await
        .expect("membership");
    }

    let exp = (crate::common::db_now().timestamp() + 3600) as usize;
    let token = mint_token(issuer, "spiffe://acme/grp", "kid-grp", exp);
    let authenticate = |group: Option<&'static str>| {
        let mut headers = axum::http::HeaderMap::new();
        headers.insert("authorization", format!("Bearer {token}").parse().unwrap());
        if let Some(group) = group {
            headers.insert("x-resource-group", group.parse().unwrap());
        }
        let pool = pool.clone();
        async move {
            brutor_gateway_core::services::auth::authenticate_request(
                &headers,
                t,
                "unused-gateway-jwt-secret-0123456789abcdef",
                &pool,
                &brutor_gateway_core::services::credential_verifier::CredentialVerifiers::community(
                    brutor_gateway_core::services::credential_verifier::RefuseUnregistered,
                ),
                None,
                true,
            )
            .await
        }
    };

    for (named, acts_in) in [
        (None, "rg-own"),
        (Some("rg-own"), "rg-own"),
        (Some("rg-second"), "rg-second"),
    ] {
        let auth = authenticate(named)
            .await
            .expect("the workload token authenticates");
        assert_eq!(auth.actor_agent_id.as_deref(), Some("agent-wl-grp"));
        assert_eq!(
            auth.resolved_group_id.as_deref(),
            Some(acts_in),
            "named {named:?}: the agent's memberships decide its group"
        );
        assert_eq!(auth.bound_group_id(), Some(acts_in), "named {named:?}");
    }

    // A group the agent is not a member of, and one that does not exist: a
    // refusal, never that group and never another.
    for named in ["rg-other", "rg-nowhere"] {
        let refused = authenticate(Some(named))
            .await
            .expect_err("naming a group the agent is not in is refused");
        assert!(
            matches!(
                refused,
                brutor_gateway_core::services::auth::AuthError::GroupNotMember
            ),
            "{named}: {refused:?}"
        );
        assert_eq!(refused.status_code(), axum::http::StatusCode::FORBIDDEN);
    }

    // In no group: authenticated, bound to none; a named group is refused.
    sqlx::query("DELETE FROM resource_group_agent_members WHERE agent_id = 'agent-wl-grp'")
        .execute(&pool)
        .await
        .expect("leave");
    let auth = authenticate(None).await.expect("authenticates");
    assert_eq!(auth.resolved_group_id, None);
    assert_eq!(auth.bound_group_id(), None);
    assert!(authenticate(Some("rg-own")).await.is_err());
}

/// One IdP + agent per test (unique ids keep the JWKS cache apart), and
/// whether `token` resolves.
async fn resolves(
    pool: &PgPool,
    n: &str,
    jwks: &MockServer,
    audiences: Option<serde_json::Value>,
    token: impl FnOnce(&str) -> String,
) -> bool {
    let t = format!("tenant-wl-{n}");
    let idp = format!("idp-wl-{n}");
    let issuer = format!("https://idp.{n}.example/");
    common::seed_tenant(pool, &t).await;
    seed_idp_with_audiences(
        pool,
        &idp,
        &t,
        &issuer,
        &format!("{}/jwks", jwks.uri()),
        audiences,
    )
    .await;
    seed_agent_with_idp(
        pool,
        &format!("agent-wl-{n}"),
        &t,
        &idp,
        "spiffe://acme/agent",
    )
    .await;
    resolve_agent_from_token(pool, &t, &token(&issuer))
        .await
        .expect("no db error")
        .is_some()
}

fn exp() -> usize {
    (crate::common::db_now().timestamp() + 3600) as usize
}

/// A token the same IdP minted for another application. Accepted
/// before the fix (`validate_aud = false`).
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_workload_token_for_another_audience_is_rejected(pool: PgPool) {
    let jwks = mock_jwks("kid-aud").await;
    let other = |iss: &str| {
        mint(
            jsonwebtoken::Algorithm::RS256,
            &rsa_key(),
            iss,
            "spiffe://acme/agent",
            "kid-aud",
            exp(),
            Some(serde_json::json!("some-other-app")),
        )
    };
    assert!(
        !resolves(
            &pool,
            "aud",
            &jwks,
            Some(serde_json::json!([WL_AUDIENCE])),
            other
        )
        .await
    );
}

/// No `aud` at all. jsonwebtoken lets this through unless `aud` is a
/// required claim.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_workload_token_with_no_audience_is_rejected(pool: PgPool) {
    let jwks = mock_jwks("kid-noaud").await;
    let none = |iss: &str| {
        mint(
            jsonwebtoken::Algorithm::RS256,
            &rsa_key(),
            iss,
            "spiffe://acme/agent",
            "kid-noaud",
            exp(),
            None,
        )
    };
    assert!(
        !resolves(
            &pool,
            "noaud",
            &jwks,
            Some(serde_json::json!([WL_AUDIENCE])),
            none
        )
        .await
    );
}

/// An IdP with no workload audiences authenticates no workloads, even
/// with an otherwise valid token. Fail closed: there is nothing to bind to.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_idp_with_no_workload_audiences_authenticates_no_workload(pool: PgPool) {
    let jwks = mock_jwks("kid-noconf").await;
    let ok = |iss: &str| mint_token(iss, "spiffe://acme/agent", "kid-noconf", exp());
    assert!(!resolves(&pool, "noconf-null", &jwks, None, ok).await);
    let ok = |iss: &str| mint_token(iss, "spiffe://acme/agent", "kid-noconf", exp());
    assert!(
        !resolves(
            &pool,
            "noconf-empty",
            &jwks,
            Some(serde_json::json!([])),
            ok
        )
        .await
    );
    // Control: the same token resolves once the audience is configured.
    let ok = |iss: &str| mint_token(iss, "spiffe://acme/agent", "kid-noconf", exp());
    assert!(
        resolves(
            &pool,
            "noconf-set",
            &jwks,
            Some(serde_json::json!(["x", WL_AUDIENCE])),
            ok
        )
        .await
    );
}

/// A symmetric (`oct`) key in a JWKS is public, so anyone could sign
/// HS256 tokens with it. Accepted before the fix.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_symmetric_jwks_key_never_verifies_a_workload_token(pool: PgPool) {
    let secret = b"a-symmetric-key-published-in-the-jwks-000";
    let jwks = serve_jwks(serde_json::json!({
        "keys": [{
            "kty": "oct",
            "kid": "kid-oct",
            "k": base64::Engine::encode(&base64::engine::general_purpose::URL_SAFE_NO_PAD, secret),
        }]
    }))
    .await;
    let forged = |iss: &str| {
        mint(
            jsonwebtoken::Algorithm::HS256,
            &EncodingKey::from_secret(secret),
            iss,
            "spiffe://acme/agent",
            "kid-oct",
            exp(),
            Some(serde_json::json!(WL_AUDIENCE)),
        )
    };
    assert!(
        !resolves(
            &pool,
            "oct",
            &jwks,
            Some(serde_json::json!([WL_AUDIENCE])),
            forged
        )
        .await
    );
}

/// The JWK states `alg: RS256`; a PS256 token signed with the same
/// RSA key is a different algorithm and must not verify. Accepted before the
/// fix (the header's algorithm was trusted within the key family).
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_token_algorithm_other_than_the_keys_stated_one_is_rejected(pool: PgPool) {
    let jwks = mock_jwks("kid-ps").await;
    let ps = |iss: &str| {
        mint(
            jsonwebtoken::Algorithm::PS256,
            &rsa_key(),
            iss,
            "spiffe://acme/agent",
            "kid-ps",
            exp(),
            Some(serde_json::json!(WL_AUDIENCE)),
        )
    };
    assert!(
        !resolves(
            &pool,
            "ps",
            &jwks,
            Some(serde_json::json!([WL_AUDIENCE])),
            ps
        )
        .await
    );
}

/// Regression guard (passes before and after): HS256 signed with the RSA
/// public key's bytes, the classic algorithm-confusion forgery. jsonwebtoken
/// refuses a family mismatch; `key_accepts` refuses HMAC outright.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_hs256_token_signed_with_the_public_rsa_key_is_rejected(pool: PgPool) {
    let jwks = mock_jwks("kid-confuse").await;
    let forged = |iss: &str| {
        mint(
            jsonwebtoken::Algorithm::HS256,
            &EncodingKey::from_secret(TEST_JWK_N.as_bytes()),
            iss,
            "spiffe://acme/agent",
            "kid-confuse",
            exp(),
            Some(serde_json::json!(WL_AUDIENCE)),
        )
    };
    assert!(
        !resolves(
            &pool,
            "confuse",
            &jwks,
            Some(serde_json::json!([WL_AUDIENCE])),
            forged
        )
        .await
    );
}
